#!/usr/bin/env python3
"""Delete Slack apps of bots that have not posted in the workspace for 72 hours."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pty_broker
import slack_admin
from slack_admin import AdminError, api

STATE_PATH = Path.home() / ".local" / "state" / "slack-bridge" / "last-post.json"
DEFAULT_HOURS = 72.0


def _load_state() -> dict[str, float]:
    try:
        value = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    if type(value) is not dict:
        return {}
    return {key: float(stamp) for key, stamp in value.items() if type(stamp) in (int, float)}


def _save_state(state: dict[str, float]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pty_broker.atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _paged(token: str, method: str, key: str, **fields):
    cursor = None
    while True:
        page = api(token, method, limit=200, cursor=cursor, **fields)
        items = page.get(key)
        if not isinstance(items, list):
            raise AdminError(f"Slack {method} returned invalid {key}")
        yield from (item for item in items if isinstance(item, dict))
        metadata = page.get("response_metadata", {})
        cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
        if not cursor:
            return


def bots(token: str) -> dict[str, dict]:
    return {
        user["id"]: user
        for user in _paged(token, "users.list", "members")
        if user.get("is_bot") and not user.get("deleted") and user.get("id") not in (None, "USLACKBOT")
    }


def recent_posts(token: str, cutoff: float) -> dict[str, float]:
    newest: dict[str, float] = {}
    channels = [
        channel["id"]
        for channel in _paged(
            token, "conversations.list", "channels",
            types="public_channel,private_channel,im,mpim", exclude_archived="true",
        )
        if channel.get("is_member") and isinstance(channel.get("id"), str)
    ]
    for channel in channels:
        for message in _paged(token, "conversations.history", "messages", channel=channel, oldest=f"{cutoff:.6f}"):
            sender, timestamp = (message.get("user"), message.get("ts"))
            if isinstance(sender, str) and isinstance(timestamp, str):
                newest[sender] = max(newest.get(sender, 0.0), float(timestamp))
    return newest


def sweep(hours: float, dry_run: bool) -> None:
    token = slack_admin._admin_token()
    now = time.time()
    cutoff = now - hours * 3600
    state = _load_state() | recent_posts(token, cutoff)
    # The admin user token has no users:read; agent bot tokens do, as
    # slack_admin._resolve_app already relies on.
    for user_id, user in sorted(bots(slack_admin._any_bot_token()).items()):
        if state.get(user_id, 0.0) >= cutoff:
            continue
        name = user.get("name") or user_id
        app_id = user.get("profile", {}).get("api_app_id")
        if not isinstance(app_id, str) or not app_id:
            print(f"skipped {name} ({user_id}): no api_app_id")
            continue
        if dry_run:
            print(f"would delete {name} ({user_id}, app {app_id}): no post in {hours:g}h")
            continue
        slack_admin._delete_apps([app_id])
    if not dry_run:
        _save_state(state)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="slack-sweep")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sweep(args.hours, args.dry_run)
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, UnicodeError, AdminError, slack_admin.RegisterError) as exc:
        print(f"slack-sweep: FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
