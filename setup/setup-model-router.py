#!/usr/bin/env python3
"""Install claude-code-router and configure Claude Code profiles for it.

cc-codex routes Claude Code to Codex through the `codex login` in ~/.codex.
cc-gemini routes it to Gemini through setup/setup-gemini-key-router.sh and
is configured only when ~/.gemini_api_keys exists.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CCR_DIR = Path.home() / ".claude-code-router"
CCR_BIN = Path.home() / ".local" / "bin" / "ccr"
CCR_VERSION = "3.0.20"
CCR_PACKAGE = "@musistudio/claude-code-router"
CCR_PACKAGE_DIR = Path.home() / ".local" / "lib" / "node_modules" / CCR_PACKAGE
# engines.node in claude-code-router 3.0.20's package.json is ">=22".
NODE_MAJOR = 22
CODEX_AUTOCOMPACT_PERCENT = 90
# The router's gateway package. 1.0.21 forwards user-message images to Codex
# and Gemini as real images; tool-result images (the Read tool on a PNG) are
# still JSON-stringified as base64 text, about 280k tokens per plot, which
# blew the context and looped compaction on 2026-09-08. The patch below moves
# tool-result image blocks into input_image items after the tool result.
AI_GATEWAY_VERSION = "1.0.21"
AI_GATEWAY_PACKAGE = "@the-next-ai/ai-gateway"
AI_GATEWAY_DIR = CCR_PACKAGE_DIR / "node_modules" / "@the-next-ai" / "ai-gateway"
AI_GATEWAY_BUNDLE = AI_GATEWAY_DIR / "dist" / "index.js"
AI_GATEWAY_TOOL_RESULT_SOURCE = (
    'if(i==="tool_result"&&e==="user"){let u=l(o.tool_use_id)||l(o.tool_call_id)'
    '||l(o.id);if(!u)continue;let d={type:"tool_result",tool_use_id:u,'
    'content:fF(o.content)},c=pF(o.content);c.length>0&&(d.tool_references=c);'
    'let f=W(o.is_error);f!==void 0&&(d.is_error=f),t.push(d);continue}'
)
AI_GATEWAY_TOOL_RESULT_PATCHED = (
    'if(i==="tool_result"&&e==="user"){let u=l(o.tool_use_id)||l(o.tool_call_id)'
    '||l(o.id);if(!u)continue;let ccrImages=[],ccrRest=o.content;'
    'if(Array.isArray(o.content)){ccrRest=[];for(let b of o.content){'
    'let s=p(b)&&l(b.type)==="image"&&p(b.source)?b.source:void 0,'
    'g=s?l(s.url)||yc(l(s.data),s.media_type):void 0;'
    'g?ccrImages.push(g):ccrRest.push(b)}}'
    'let d={type:"tool_result",tool_use_id:u,'
    'content:ccrImages.length>0&&ccrRest.length===0'
    '?"[image tool result: the image follows this tool output]":fF(ccrRest)},'
    'c=pF(o.content);c.length>0&&(d.tool_references=c);'
    'let f=W(o.is_error);f!==void 0&&(d.is_error=f),t.push(d);'
    'for(let g of ccrImages)t.push({type:"input_image",image_url:g});continue}'
)
SERVICE_FILE = CCR_DIR / "service.json"

CODEX_HOME = Path.home() / ".codex"
CODEX_PROVIDER_ID = "codex"
CODEX_PROVIDER_NAME = "Codex"
CODEX_PROFILE_ID = "cc-codex"
# Pin the full registered model list: codex only writes models_cache.json
# after its first run, so a fresh machine must not depend on the cache.
REQUIRED_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-sol-wm",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "codex-auto-review",
)
# Claude Code's model slots map to Codex tiers: Fable -> Astra, Opus -> Sol,
# Sonnet -> Terra, Haiku and small-fast -> Luna. An account without Astra
# uses Sol for Fable and the default model.
FRONTIER_MODELS = ("gpt-6-astra", "gpt-5.6-sol")
CODEX_ALIAS_MODELS = {
    "opusModel": "gpt-5.6-sol",
    "sonnetModel": "gpt-5.6-terra",
    "haikuModel": "gpt-5.6-luna",
    "smallFastModel": "gpt-5.6-luna",
}

GEMINI_KEY_FILE = Path.home() / ".gemini_api_keys"
GEMINI_PROVIDER_ID = "gemini-api"
GEMINI_PROVIDER_NAME = "Gemini API"
GEMINI_PROFILE_ID = "cc-gemini"
GEMINI_FLASH_ALIAS = "gemini-flash-latest"
GEMINI_KEY_ROUTER_BASE_URL = "http://127.0.0.1:3460"
GEMINI_KEY_ROUTER_VERSION = 5
GEMINI_KEY_ROUTER_SETUP = REPO / "setup" / "setup-gemini-key-router.sh"
GEMINI_KEY_ROUTER_TOKEN_FILE = CCR_DIR / "gemini-key-router-token"
GEMINI_EFFORT_HEADER = "x-ccr-reasoning-effort"
# Gemini CLI compacts at half the context window (DEFAULT_COMPRESSION_TOKEN_THRESHOLD = 0.5).
GEMINI_AUTOCOMPACT_PERCENT = 50
GEMINI_MODELS_URL = f"{GEMINI_KEY_ROUTER_BASE_URL}/v1beta/models"


def frontier_model(provider_name, models):
    for name in FRONTIER_MODELS:
        if name in models:
            if name != FRONTIER_MODELS[0]:
                print(f"{provider_name} has no {FRONTIER_MODELS[0]}; Fable and the default model are {name}")
            return name
    raise RuntimeError(f"{provider_name} lacks every frontier model: {', '.join(FRONTIER_MODELS)}")


def codex_alias_fields(models):
    missing = sorted({m for m in CODEX_ALIAS_MODELS.values() if m not in models})
    if missing:
        raise RuntimeError(
            f"{CODEX_PROVIDER_NAME} lacks models required for alias mapping: {', '.join(missing)}"
        )
    aliases = {"fableModel": frontier_model(CODEX_PROVIDER_NAME, models), **CODEX_ALIAS_MODELS}
    return {field: f"{CODEX_PROVIDER_NAME}/{name}" for field, name in aliases.items()}


def rpc(method, *args):
    service = json.loads(SERVICE_FILE.read_text())
    parsed = urllib.parse.urlsplit(service["url"])
    token = urllib.parse.parse_qs(parsed.query)["ccr_web_token"][0]
    request = urllib.request.Request(
        f"{parsed.scheme}://{parsed.netloc}/api/ccr/rpc",
        data=json.dumps({"method": method, "args": list(args)}).encode(),
        headers={"content-type": "application/json", "x-ccr-web-auth": token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", {}).get("message", "CCR RPC failed"))
    return payload["value"]


def restart_ccr():
    subprocess.run(
        [str(CCR_BIN), "stop"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [str(CCR_BIN), "ui", "--no-open"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for _ in range(20):
        try:
            rpc("getConfig")
            return
        except (FileNotFoundError, KeyError, OSError, json.JSONDecodeError):
            time.sleep(0.25)
    raise RuntimeError("CCR management service did not become ready")


def require_node():
    for tool in ("node", "npm"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH; install Node.js {NODE_MAJOR} or newer")
    version = subprocess.run(
        ["node", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < NODE_MAJOR:
        sys.exit(f"node {version} is too old; {CCR_PACKAGE} {CCR_VERSION} needs Node.js {NODE_MAJOR} or newer")


def install_ccr():
    package_file = CCR_PACKAGE_DIR / "package.json"
    if package_file.is_file() and json.loads(package_file.read_text())["version"] == CCR_VERSION:
        return
    subprocess.run(
        ["npm", "install", "-g", "--prefix", str(Path.home() / ".local"),
         f"{CCR_PACKAGE}@{CCR_VERSION}"],
        check=True,
    )


def installed_ai_gateway_version():
    package_file = AI_GATEWAY_DIR / "package.json"
    if not package_file.is_file():
        raise RuntimeError(f"Missing gateway package: {package_file}")
    return json.loads(package_file.read_text())["version"]


def install_ai_gateway():
    if installed_ai_gateway_version() == AI_GATEWAY_VERSION:
        return
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "npm", "pack", f"{AI_GATEWAY_PACKAGE}@{AI_GATEWAY_VERSION}",
                "--silent", "--pack-destination", tmp,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        tarballs = list(Path(tmp).glob("*.tgz"))
        if len(tarballs) != 1:
            raise RuntimeError(f"npm pack produced {len(tarballs)} tarballs in {tmp}")
        with tarfile.open(tarballs[0]) as archive:
            archive.extractall(tmp, filter="data")
        shutil.rmtree(AI_GATEWAY_DIR)
        shutil.move(str(Path(tmp) / "package"), str(AI_GATEWAY_DIR))
    installed = installed_ai_gateway_version()
    if installed != AI_GATEWAY_VERSION:
        raise RuntimeError(
            f"Gateway install produced {installed}, wanted {AI_GATEWAY_VERSION}"
        )
    print(f"Installed {AI_GATEWAY_PACKAGE} {AI_GATEWAY_VERSION}")


def patch_ai_gateway_tool_result_images(bundle_source):
    if AI_GATEWAY_TOOL_RESULT_PATCHED in bundle_source:
        return bundle_source
    if bundle_source.count(AI_GATEWAY_TOOL_RESULT_SOURCE) != 1:
        raise RuntimeError(
            "Gateway bundle does not contain the expected tool_result parser; "
            f"re-check the patch against {AI_GATEWAY_PACKAGE} {AI_GATEWAY_VERSION}"
        )
    return bundle_source.replace(
        AI_GATEWAY_TOOL_RESULT_SOURCE, AI_GATEWAY_TOOL_RESULT_PATCHED, 1
    )


def apply_ai_gateway_patch():
    source = AI_GATEWAY_BUNDLE.read_text()
    patched = patch_ai_gateway_tool_result_images(source)
    if patched != source:
        AI_GATEWAY_BUNDLE.write_text(patched)
        print(f"Patched tool-result images into {AI_GATEWAY_BUNDLE}")


def read_gemini_profile(api_key):
    request = urllib.request.Request(
        GEMINI_MODELS_URL,
        headers={"x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(
            f"Gemini model discovery failed with HTTP {error.code}: {body[:400]}"
        ) from None

    models = []
    display_names = {}
    metadata = {}
    for model in payload.get("models", []):
        if "generateContent" not in model.get("supportedGenerationMethods", []):
            continue
        name = model.get("name", "").removeprefix("models/")
        if not name:
            continue
        models.append(name)
        if model.get("displayName"):
            display_names[name] = model["displayName"]
        input_limit = model.get("inputTokenLimit")
        output_limit = model.get("outputTokenLimit")
        model_metadata = {}
        if input_limit:
            model_metadata.update({
                "contextWindow": input_limit,
                "maxContextWindow": input_limit,
            })
        if output_limit:
            model_metadata["maxOutputTokens"] = output_limit
        if model_metadata:
            metadata[name] = model_metadata
    if not models:
        raise RuntimeError("The Gemini keys expose no generateContent models")
    return models, display_names, metadata


def profile_context_env(metadata, model, provider_name):
    context_window = metadata.get(model, {}).get("contextWindow")
    if not isinstance(context_window, int) or context_window <= 0:
        raise RuntimeError(
            f"{provider_name} model {model!r} has no valid native context window"
        )
    auto_compact_percent = (
        GEMINI_AUTOCOMPACT_PERCENT
        if provider_name == GEMINI_PROVIDER_NAME
        else CODEX_AUTOCOMPACT_PERCENT
    )
    return {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(context_window),
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(auto_compact_percent),
    }


class CodexLoginRejected(RuntimeError):
    """The Codex login is missing or model discovery answered 401 for it."""


def fetch_codex_models():
    auth_file = CODEX_HOME / "auth.json"
    if not auth_file.is_file():
        raise CodexLoginRejected(f"Missing Codex login: {auth_file}")
    tokens = (json.loads(auth_file.read_text()).get("tokens") or {})
    access_token = tokens.get("access_token")
    if not access_token:
        raise CodexLoginRejected(f"Codex login has no access token: {auth_file}")
    # Use the installed binary version because the endpoint filters models by client_version.
    version_result = subprocess.run(
        ["codex", "--version"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
    )
    version_match = re.fullmatch(r"codex-cli (\S+)\n?", version_result.stdout)
    if version_result.returncode != 0 or version_match is None:
        raise RuntimeError(
            f"codex --version failed; output: {version_result.stdout + version_result.stderr}"
        )
    client_version = version_match.group(1)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": f"codex-cli/{client_version}",
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    url = "https://chatgpt.com/backend-api/codex/models?" + urllib.parse.urlencode(
        {"client_version": client_version})
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        message = f"Codex model discovery failed with HTTP {error.code}: {body[:400]}"
        if error.code == 401:
            raise CodexLoginRejected(message) from None
        raise RuntimeError(message) from None

    models = []
    display_names = {}
    metadata = {}
    for model in payload.get("models", []):
        slug = model.get("slug")
        if not slug:
            continue
        models.append(slug)
        if model.get("display_name"):
            display_names[slug] = model["display_name"]
        model_metadata = {}
        if model.get("context_window"):
            model_metadata["contextWindow"] = model["context_window"]
        if model.get("max_context_window"):
            model_metadata["maxContextWindow"] = model["max_context_window"]
        if model_metadata:
            metadata[slug] = model_metadata
    if not models:
        raise RuntimeError("Codex model discovery returned no models")
    return tokens, models, display_names, metadata


def codex_oauth_from_tokens(tokens):
    oauth = {
        "accessToken": tokens.get("access_token"),
        "accountId": tokens.get("account_id"),
        "refreshIfMissingAccessToken": True,
        "refreshToken": tokens.get("refresh_token"),
        # Codex-issued scope claims vary across CLI/auth-flow versions. Let the
        # mandatory end-to-end request below determine whether the credential
        # works instead of rejecting a valid token in the gateway preflight.
        "required": False,
    }
    return {key: value for key, value in oauth.items() if value is not None}


def run_codex_login():
    print("Starting `codex login`...")
    subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="file"', "login"],
        check=True,
        env={**os.environ, "CODEX_HOME": str(CODEX_HOME)},
    )


def ensure_codex_login():
    """Block until ~/.codex holds a login that model discovery accepts,
    running `codex login` as needed; returns the tokens and models."""
    while True:
        try:
            return fetch_codex_models()
        except CodexLoginRejected as error:
            print(error)
            run_codex_login()


def add_gemini_effort_plugins(plugins):
    gemini_plugin = {
        "request": {
            "headers": {
                GEMINI_EFFORT_HEADER: "{{ standardRequest.output_config.effort }}",
            },
        },
    }
    plugins["ccr-gemini-reasoning-effort"] = {
        **gemini_plugin,
        "key": "ccr-gemini-reasoning-effort",
        "providerName": GEMINI_PROVIDER_NAME,
    }
    plugins["ccr-gemini-reasoning-effort-internal"] = {
        **gemini_plugin,
        "key": "ccr-gemini-reasoning-effort-internal",
        "providerName": f"{GEMINI_PROVIDER_ID}::gemini_generate_content",
    }


def merge_gemini_catalog(providers, models, display_names, metadata):
    # The key router rotates the keys in ~/.gemini_api_keys and model
    # visibility varies per key, so a single fetch can transiently lose
    # models. Union with the stored catalog so a rotation never shrinks the
    # picker or invalidates the selected model.
    stored = providers.get(GEMINI_PROVIDER_ID) or {}
    models = sorted(set(models) | set(stored.get("models") or []))
    display_names = {**(stored.get("modelDisplayNames") or {}), **display_names}
    metadata = {**(stored.get("modelMetadata") or {}), **metadata}
    if GEMINI_FLASH_ALIAS not in models:
        raise RuntimeError(
            f"Gemini model discovery is missing required alias {GEMINI_FLASH_ALIAS!r}"
        )
    if GEMINI_FLASH_ALIAS not in metadata:
        raise RuntimeError(
            f"Gemini model metadata is missing required alias {GEMINI_FLASH_ALIAS!r}"
        )
    return models, display_names, metadata


def configure():
    """Write the providers and profiles; return the profile ids and the
    model each one is verified with."""
    config = rpc("getConfig")
    providers = {provider.get("id"): provider for provider in config["Providers"]}
    plugins = {
        plugin.get("key"): plugin
        for plugin in config.get("providerPlugins", [])
        if isinstance(plugin, dict) and plugin.get("key")
    }
    profiles = {profile.get("id"): profile for profile in config["profile"]["profiles"]}

    tokens, models, display_names, metadata = ensure_codex_login()
    models = list(dict.fromkeys([*REQUIRED_MODELS, *models]))
    providers[CODEX_PROVIDER_ID] = {
        "api_base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "ccr-local-agent-login",
        "id": CODEX_PROVIDER_ID,
        "modelDisplayNames": display_names,
        "modelMetadata": metadata,
        "models": models,
        "name": CODEX_PROVIDER_NAME,
        "type": "openai_responses",
    }
    plugin_base = {
        "request": {"bodyRemove": ["max_output_tokens", "metadata"]},
        "codexOauth": codex_oauth_from_tokens(tokens),
    }
    display_key = f"ccr-local-agent-{CODEX_PROVIDER_ID}-codex-oauth"
    plugins[display_key] = {
        **plugin_base,
        "key": display_key,
        "providerName": CODEX_PROVIDER_NAME,
    }
    plugins[f"{display_key}-internal"] = {
        **plugin_base,
        "key": f"{display_key}-internal",
        "providerName": f"{CODEX_PROVIDER_ID}::openai_responses",
    }
    codex_model = frontier_model(CODEX_PROVIDER_NAME, models)
    profiles[CODEX_PROFILE_ID] = {
        "agent": "claude-code",
        "enabled": True,
        "env": {
            "BASH_MAX_OUTPUT_LENGTH": "8000",
            # Gateway discovery would list every provider's models in /model;
            # the Codex picker shows only its alias slots. CCR merges its own
            # "1" under the profile env, so the value must be "0".
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "0",
            **profile_context_env(metadata, codex_model, CODEX_PROVIDER_NAME),
        },
        **codex_alias_fields(models),
        "id": CODEX_PROFILE_ID,
        # Router-side compaction: Claude Code's own compaction wedged a
        # Codex-routed session at 95% context.
        "managedCompact": True,
        "model": f"{CODEX_PROVIDER_NAME}/{codex_model}",
        "precomputeCompactionEnabled": True,
        "name": "Claude Code Codex",
        "scope": "ccr",
        "surface": "cli",
    }
    verified = {CODEX_PROFILE_ID: f"{CODEX_PROVIDER_NAME}/{codex_model}"}

    if GEMINI_KEY_FILE.is_file():
        subprocess.run([str(GEMINI_KEY_ROUTER_SETUP)], check=True)
        gemini_key = GEMINI_KEY_ROUTER_TOKEN_FILE.read_text().strip()
        gemini_models, gemini_display_names, gemini_metadata = merge_gemini_catalog(
            providers, *read_gemini_profile(gemini_key))
        providers[GEMINI_PROVIDER_ID] = {
            "api_base_url": GEMINI_KEY_ROUTER_BASE_URL,
            "api_key": gemini_key,
            "extraHeaders": {"x-goog-api-key": gemini_key},
            "id": GEMINI_PROVIDER_ID,
            "modelDisplayNames": gemini_display_names,
            "modelMetadata": gemini_metadata,
            "models": gemini_models,
            "name": GEMINI_PROVIDER_NAME,
            "type": "gemini_generate_content",
        }
        add_gemini_effort_plugins(plugins)
        gemini_model = f"{GEMINI_PROVIDER_NAME}/{GEMINI_FLASH_ALIAS}"
        profiles[GEMINI_PROFILE_ID] = {
            "agent": "claude-code",
            "enabled": True,
            "env": {
                "BASH_MAX_OUTPUT_LENGTH": "8000",
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
                **profile_context_env(
                    gemini_metadata, GEMINI_FLASH_ALIAS, GEMINI_PROVIDER_NAME),
            },
            "fableModel": gemini_model,
            "haikuModel": gemini_model,
            "id": GEMINI_PROFILE_ID,
            "managedCompact": False,
            "model": gemini_model,
            "precomputeCompactionEnabled": True,
            "name": "Claude Code Gemini",
            "opusModel": gemini_model,
            "scope": "ccr",
            "smallFastModel": gemini_model,
            "sonnetModel": gemini_model,
            "surface": "cli",
        }
        verified[GEMINI_PROFILE_ID] = gemini_model

    # The router's enabled global defaults rewrite ~/.claude/settings.json and
    # ~/.codex/config.toml so plain `claude` and `codex` route through it.
    for profile_id in ("default-claude-code", "default-codex"):
        if profile_id in profiles:
            profiles[profile_id]["enabled"] = False

    config["Providers"] = list(providers.values())
    config["providerPlugins"] = list(plugins.values())
    config["profile"]["profiles"] = list(profiles.values())
    if not config.get("preferredProvider"):
        config["preferredProvider"] = CODEX_PROVIDER_NAME
    rpc("saveConfig", config, {"applyProfile": False})
    result = rpc("applyProfile")
    failures = [client for client in result.get("clients", []) if not client.get("ok")]
    if failures:
        raise RuntimeError(f"CCR profile application failed: {failures}")

    api_key_ids = {api_key.get("id") for api_key in rpc("getConfig")["APIKEYS"]}
    missing_keys = [
        profile_id for profile_id in verified
        if f"profile:{profile_id}" not in api_key_ids
    ]
    if missing_keys:
        raise RuntimeError(f"CCR did not create profile API keys: {missing_keys}")
    print("Configured: " + ", ".join(verified))
    return verified


def verify_profile(profile_id, model):
    """Probe the applied profile through the same CLI path sessions use."""
    result = subprocess.run(
        [
            str(CCR_BIN), profile_id, "cli", "--",
            "--print", "--model", model, "Reply with ok.",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip()


def verify(verified):
    """Fail setup on any end-to-end error, quota and capacity included:
    a profile that only reached the provider is not proven."""
    for profile_id, model in verified.items():
        error = verify_profile(profile_id, model)
        if profile_id == CODEX_PROFILE_ID and error is not None and (
            "token_expired" in error
            or "resolved token is missing required scopes" in error
        ):
            # Expiry and missing OAuth scopes are invisible to model
            # discovery. Log in again, re-embed the tokens, and restart the
            # gateway so the re-check uses the new credential.
            print(f"{profile_id}: stored Codex credential needs re-login.")
            run_codex_login()
            configure()
            restart_ccr()
            error = verify_profile(profile_id, model)
        if error is not None:
            raise RuntimeError(
                f"{profile_id} failed the end-to-end check: {error[:2000]}")
        print(f"Verified end-to-end: {profile_id}")


def main():
    require_node()
    install_ccr()
    install_ai_gateway()
    apply_ai_gateway_patch()
    # A daemon started before the install or the patch still runs the old
    # code, and applyProfile writes the Claude settings from whatever the
    # daemon runs, so configure() must talk to the installed release.
    restart_ccr()
    verified = configure()
    restart_ccr()
    verify(verified)


if __name__ == "__main__":
    main()
