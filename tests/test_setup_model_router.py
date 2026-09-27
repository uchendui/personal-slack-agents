#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "setup" / "setup-model-router.py"
HOME = tempfile.TemporaryDirectory()
with mock.patch.dict(os.environ, {"HOME": HOME.name}):
    SPEC = importlib.util.spec_from_file_location("setup_model_router", MODULE_PATH)
    router_setup = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(router_setup)


class NodeVersionTest(unittest.TestCase):
    def test_node_below_engines_requirement_exits(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            for tool in ("node", "npm"):
                path = Path(bin_dir) / tool
                path.write_text("#!/bin/sh\necho \"$NODE_VERSION\"\n")
                path.chmod(0o755)
            environment = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
            with mock.patch.dict(os.environ, {**environment, "NODE_VERSION": "v18.19.1"}):
                with self.assertRaises(SystemExit) as raised:
                    router_setup.require_node()
            self.assertIn("v18.19.1", str(raised.exception))
            with mock.patch.dict(os.environ, {**environment, "NODE_VERSION": "v22.0.0"}):
                router_setup.require_node()


class CodexAliasModelsTest(unittest.TestCase):
    def test_alias_fields_map_tiers_and_refuse_missing_models(self):
        models = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        fields = router_setup.codex_alias_fields(models)
        self.assertEqual(fields["fableModel"], "Codex/gpt-6-astra")
        self.assertEqual(fields["opusModel"], "Codex/gpt-5.6-sol")
        self.assertEqual(fields["sonnetModel"], "Codex/gpt-5.6-terra")
        self.assertEqual(fields["haikuModel"], "Codex/gpt-5.6-luna")
        without_astra = router_setup.codex_alias_fields(models[1:])
        self.assertEqual(without_astra["fableModel"], "Codex/gpt-5.6-sol")
        with self.assertRaises(RuntimeError):
            router_setup.codex_alias_fields(["gpt-5.6-sol"])


class ContextWindowTest(unittest.TestCase):
    def test_profiles_use_native_context_and_provider_compact_percent(self):
        metadata = {
            "gpt-5.6-sol": {"contextWindow": 272000, "maxContextWindow": 872000},
            "gemini-flash-latest": {"contextWindow": 1048576},
        }
        self.assertEqual(
            router_setup.profile_context_env(metadata, "gpt-5.6-sol", "Codex"),
            {
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "272000",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "90",
            },
        )
        self.assertEqual(
            router_setup.profile_context_env(
                metadata, "gemini-flash-latest", router_setup.GEMINI_PROVIDER_NAME
            ),
            {
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1048576",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50",
            },
        )


class GeminiCatalogTest(unittest.TestCase):
    def test_merge_keeps_stored_models_and_requires_flash_alias(self):
        alias = router_setup.GEMINI_FLASH_ALIAS
        metadata = {alias: {"contextWindow": 1048576}}
        stored = {router_setup.GEMINI_PROVIDER_ID: {"models": ["gemini-old"]}}
        models, _, _ = router_setup.merge_gemini_catalog(stored, [alias], {}, metadata)
        self.assertEqual(models, sorted([alias, "gemini-old"]))
        # Metadata still carries the alias, so only the discovery-list guard
        # can refuse this call.
        with self.assertRaises(RuntimeError):
            router_setup.merge_gemini_catalog({}, ["gemini-3.7-flash"], {}, metadata)
        with self.assertRaises(RuntimeError):
            router_setup.merge_gemini_catalog({}, [alias], {}, {})


class ConfigureTest(unittest.TestCase):
    def test_global_default_profiles_are_disabled(self):
        config = {
            "Providers": [],
            "providerPlugins": [],
            "profile": {"profiles": [
                {"id": "default-claude-code", "enabled": True, "scope": "global"},
                {"id": "default-codex", "enabled": True, "scope": "global"},
            ]},
            "APIKEYS": [{"id": "profile:cc-codex"}],
        }
        saved = []

        def fake_rpc(method, *args):
            if method == "saveConfig":
                saved.append(args[0])
            return {"clients": []} if method == "applyProfile" else config

        models = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
        codex_login = ({}, models, {}, {"gpt-5.6-sol": {"contextWindow": 272000}})
        with mock.patch.object(router_setup, "rpc", fake_rpc), \
                mock.patch.object(router_setup, "ensure_codex_login", return_value=codex_login):
            router_setup.configure()
        enabled = {profile["id"]: profile["enabled"] for profile in saved[0]["profile"]["profiles"]}
        self.assertEqual(
            enabled,
            {"default-claude-code": False, "default-codex": False, "cc-codex": True},
        )


class GatewayToolResultImageTest(unittest.TestCase):
    def test_patch_is_idempotent(self):
        bundle = "head;" + router_setup.AI_GATEWAY_TOOL_RESULT_SOURCE + ";tail"
        patched = router_setup.patch_ai_gateway_tool_result_images(bundle)
        self.assertEqual(
            patched, "head;" + router_setup.AI_GATEWAY_TOOL_RESULT_PATCHED + ";tail"
        )
        self.assertEqual(
            router_setup.patch_ai_gateway_tool_result_images(patched), patched
        )

    def test_patch_refuses_an_unknown_bundle(self):
        with self.assertRaises(RuntimeError):
            router_setup.patch_ai_gateway_tool_result_images("head;other;tail")

    def test_patched_parser_emits_input_image_after_tool_result(self):
        bundle = router_setup.patch_ai_gateway_tool_result_images(
            "R=" + router_setup.AI_GATEWAY_TOOL_RESULT_SOURCE
        )
        body = bundle[len("R="):]
        script = (
            "const l=e=>typeof e==='string'?e:undefined;"
            "const p=e=>!!e&&typeof e==='object'&&!Array.isArray(e);"
            "const yc=(e,n)=>typeof e==='string'&&e.trim()?`data:${n||'image/png'};base64,${e.trim()}`:undefined;"
            "const fF=c=>typeof c==='string'?c:Array.isArray(c)?(c.map(x=>x&&x.text||'').filter(Boolean).join('\\n')||(c.length?JSON.stringify(c):'')):'';"
            "const pF=()=>[];const W=e=>typeof e==='boolean'?e:undefined;"
            "const e='user',t=[];"
            "for(const o of JSON.parse(process.argv[1])){const i=o.type;" + body + "}"
            "console.log(JSON.stringify(t));"
        )
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}
                ],
            },
            {"type": "tool_result", "tool_use_id": "call_2", "content": [{"type": "text", "text": "plain"}]},
        ]
        result = subprocess.run(
            ["node", "-e", script, json.dumps(blocks)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "[image tool result: the image follows this tool output]",
                },
                {"type": "input_image", "image_url": "data:image/jpeg;base64,QUJD"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "plain"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
