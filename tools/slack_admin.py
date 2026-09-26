#!/usr/bin/env python3
"""Operator Slack cleanup: bulk thread deletion and agent app removal."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_register
from slack_register import RegisterError, SlackAPIError, _read, _user_api, _write
from slack_send import SendError, resolve_channel

CONFIG_DIR = Path.home() / ".config/slack-bridge"
APP_PATH = CONFIG_DIR / "admin-app.json"
USER_ENV_PATH = CONFIG_DIR / "admin-user.env"
API_BASE = "https://slack.com/api/"
APP_NAME = os.environ.get("SLACK_ADMIN_APP_NAME", "slack-admin")
# None lets slack_register pick the only logged-in workspace and fail when there are several.
TEAM = os.environ.get("SLACK_TEAM_ID")
REDIRECT_PORT = 8919
REDIRECT_URL = f"http://localhost:{REDIRECT_PORT}/slack-admin-cb"
USER_SCOPES = (
    "chat:write", "channels:history", "channels:read",
    "groups:history", "groups:read", "im:history", "im:read",
    "mpim:history", "mpim:read", "channels:write", "groups:write",
)
APP_ID_RE = re.compile(r"A[A-Z0-9]+\Z")
USER_ID_RE = re.compile(r"[UW][A-Z0-9]+\Z")


class AdminError(RuntimeError):
    pass


def api(token: str, method: str, **fields) -> dict:
    data = urllib.parse.urlencode(
        {key: value for key, value in fields.items() if value is not None}
    ).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    while True:
        request = urllib.request.Request(API_BASE + method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    delay = int(exc.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1
                time.sleep(max(delay, 1))
                continue
            raise AdminError(f"Slack {method} transport failed: {exc}") from exc
        except Exception as exc:
            raise AdminError(f"Slack {method} transport failed: {exc}") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"
            raise AdminError(f"Slack {method} failed: {reason}")
        return result


def _manifest() -> dict:
    return {
        "display_information": {"name": APP_NAME},
        "oauth_config": {
            "redirect_urls": [REDIRECT_URL],
            "scopes": {"user": list(USER_SCOPES)},
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _load_app() -> dict:
    try:
        record = json.loads(_read(APP_PATH))
    except json.JSONDecodeError as exc:
        raise AdminError(f"{APP_PATH} is not valid JSON: {exc}") from exc
    keys = ("name", "app_id", "client_id", "client_secret")
    if (
        not isinstance(record, dict)
        or any(not isinstance(record.get(key), str) or not record[key] for key in keys)
    ):
        raise AdminError(f"{APP_PATH} has invalid shape; rerun setup after removing it")
    return record


def _setup() -> None:
    manifest = json.dumps(_manifest())
    if APP_PATH.exists():
        record = _load_app()
        try:
            _user_api(
                "apps.manifest.update", TEAM, json_body=True,
                app_id=record["app_id"], manifest=manifest,
            )
            print(f"reused app {record['app_id']} ({record['name']}); manifest refreshed")
            return
        except SlackAPIError as exc:
            if exc.reason != "app_not_found":
                raise
            print(f"recorded app {record['app_id']} is gone from Slack; creating a fresh one")
    created = _user_api("apps.manifest.create", TEAM, json_body=True, manifest=manifest)
    credentials = created.get("credentials")
    if not isinstance(credentials, dict):
        raise AdminError("apps.manifest.create returned no credentials object")
    record = {
        "name": APP_NAME,
        "app_id": created.get("app_id"),
        "client_id": credentials.get("client_id"),
        "client_secret": credentials.get("client_secret"),
    }
    if any(not isinstance(value, str) or not value for value in record.values()):
        raise AdminError("apps.manifest.create returned invalid app credentials")
    _write(APP_PATH, json.dumps(record, indent=2) + "\n")
    print(f"created app {record['app_id']} ({APP_NAME}); credentials saved to {APP_PATH}")


def _wait_for_code(state: str) -> str:
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if parsed.path != urllib.parse.urlparse(REDIRECT_URL).path:
                self.send_error(404)
                return
            if params.get("state") != state or not params.get("code"):
                self.send_error(400, "state mismatch or missing code")
                return
            captured["code"] = params["code"]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"slack-admin authorized; you can close this tab.\n")

        def log_message(self, *_args):
            pass

    with http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler) as server:
        while "code" not in captured:
            server.handle_request()
    return captured["code"]


def _authorize() -> None:
    record = _load_app()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "client_id": record["client_id"],
        "user_scope": ",".join(USER_SCOPES),
        "redirect_uri": REDIRECT_URL,
        "state": state,
    })
    print("Open this URL in your browser and approve the app:")
    print(f"https://slack.com/oauth/v2/authorize?{query}")
    print(f"Waiting for the redirect on {REDIRECT_URL} ...")
    code = _wait_for_code(state)
    exchanged = api(
        "", "oauth.v2.access",
        client_id=record["client_id"], client_secret=record["client_secret"],
        code=code, redirect_uri=REDIRECT_URL,
    )
    authed = exchanged.get("authed_user")
    if not isinstance(authed, dict) or not isinstance(authed.get("access_token"), str):
        raise AdminError("oauth.v2.access returned no user access token")
    values = {"SLACK_ADMIN_USER_TOKEN": authed["access_token"]}
    if isinstance(authed.get("id"), str):
        values["SLACK_ADMIN_USER_ID"] = authed["id"]
    if isinstance(authed.get("refresh_token"), str):
        values["SLACK_ADMIN_REFRESH_TOKEN"] = authed["refresh_token"]
    _write(USER_ENV_PATH, "".join(f"{key}={value}\n" for key, value in sorted(values.items())))
    print(f"user token saved to {USER_ENV_PATH}")


def _admin_token() -> str:
    for line in _read(USER_ENV_PATH).splitlines():
        key, separator, value = line.partition("=")
        if key == "SLACK_ADMIN_USER_TOKEN" and separator and value:
            return value
    raise AdminError(f"no SLACK_ADMIN_USER_TOKEN in {USER_ENV_PATH}; run authorize first")


def delete_thread(token: str, channel: str, root_ts: str, only: str | None = None) -> tuple[int, int, int]:
    messages: list[dict] = []
    cursor = None
    while True:
        page = api(
            token, "conversations.replies",
            channel=channel, ts=root_ts, limit=200, cursor=cursor,
        )
        found = page.get("messages")
        if not isinstance(found, list):
            raise AdminError("Slack returned invalid thread data")
        messages.extend(item for item in found if isinstance(item, dict))
        metadata = page.get("response_metadata", {})
        cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
        if not cursor:
            break
    deleted = skipped = failed = 0
    seen: set[str] = set()
    # Replies first, root last, so deleting the root never orphans replies.
    ordered = [m for m in messages if m.get("ts") != root_ts] + [m for m in messages if m.get("ts") == root_ts]
    for message in ordered:
        timestamp = message.get("ts")
        if not isinstance(timestamp, str) or not timestamp or timestamp in seen:
            continue
        seen.add(timestamp)
        if only is not None and only not in message.get("text", ""):
            skipped += 1
            continue
        try:
            api(token, "chat.delete", channel=channel, ts=timestamp)
            deleted += 1
        except AdminError as exc:
            failed += 1
            print(f"failed {timestamp}: {exc}", file=sys.stderr)
    print(f"deleted {deleted} / skipped {skipped} / failed {failed}")
    return deleted, skipped, failed


def _any_bot_token() -> str:
    configs = slack_register.load_agent_configs(slack_register.AGENTS_DIR)
    if not configs:
        raise AdminError("no local agents registered; cannot resolve names via users.info")
    return next(iter(configs.values())).bot_token


def _resolve_app(target: str, registry: dict) -> tuple[str | None, str]:
    """Return (registered_name_or_None, app_id) for a name, user id, or app id."""
    if APP_ID_RE.fullmatch(target):
        name = next(
            (name for section in ("agents", "tombstones")
             for name, record in registry[section].items() if record["app_id"] == target),
            None,
        )
        return name, target
    record = registry["agents"].get(target) or registry["tombstones"].get(target)
    if record is not None:
        return target, record["app_id"]
    token = _any_bot_token()
    if USER_ID_RE.fullmatch(target):
        candidates = [api(token, "users.info", user=target).get("user", {})]
    else:
        candidates = []
        cursor = None
        while True:
            page = api(token, "users.list", limit=200, cursor=cursor)
            for user in page.get("members", []):
                profile = user.get("profile", {})
                if target in (user.get("name"), profile.get("display_name"), profile.get("real_name")):
                    candidates.append(user)
            metadata = page.get("response_metadata", {})
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                break
    app_ids = {
        user.get("profile", {}).get("api_app_id")
        for user in candidates if user.get("profile", {}).get("api_app_id")
    }
    if len(app_ids) != 1:
        raise AdminError(f"{target!r} did not resolve to exactly one app (found {sorted(app_ids)})")
    return None, app_ids.pop()


def _drop_tombstone(name: str) -> None:
    with slack_register._lock():
        registry = slack_register.load_registry(slack_register.REGISTRY_PATH)
        if name in registry["tombstones"]:
            registry["tombstones"].pop(name)
            slack_register._save(registry)


def _delete_apps(targets: list[str]) -> int:
    registry = (
        slack_register.load_registry(slack_register.REGISTRY_PATH)
        if slack_register.REGISTRY_PATH.exists()
        else {"agents": {}, "tombstones": {}}
    )
    failures = 0
    for target in targets:
        try:
            name, app_id = _resolve_app(target, registry)
            _user_api("apps.manifest.delete", TEAM, json_body=True, app_id=app_id)
            if name in registry["agents"]:
                slack_register._unregister(name)
                print(f"slack-bridge picks up {name} within 5 s")
            elif name in registry["tombstones"]:
                _drop_tombstone(name)
            print(f"deleted: {target} (app {app_id})")
        except (AdminError, RegisterError) as exc:
            failures += 1
            print(f"failed: {target}: {exc}", file=sys.stderr)
    return failures


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="slack-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup")
    commands.add_parser("authorize")
    thread = commands.add_parser("delete-thread")
    thread.add_argument("--channel", required=True)
    thread.add_argument("--ts", required=True)
    thread.add_argument("--only")
    apps = commands.add_parser("delete-app")
    apps.add_argument("targets", nargs="+", metavar="name-or-app-id")
    args = parser.parse_args(argv)
    if args.command == "setup":
        _setup()
    elif args.command == "authorize":
        _authorize()
    elif args.command == "delete-thread":
        token = _admin_token()
        channel = resolve_channel(urllib.request.urlopen, token, args.channel)
        delete_thread(token, channel, args.ts, args.only)
    else:
        return 1 if _delete_apps(args.targets) else 0
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, UnicodeError, AdminError, RegisterError, SendError, subprocess.SubprocessError) as exc:
        print(f"slack-admin: FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
