#!/usr/bin/env python3
"""Post once to Slack as one registered local agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.parse
import urllib.request
from pathlib import Path

AGENTS_DIR = Path.home() / ".config" / "slack-bridge" / "agents"
API_BASE = "https://slack.com/api/"
FORM_METHODS = {"files.getUploadURLExternal", "conversations.replies"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
CHANNEL_ID_RE = re.compile(r"[CDG][A-Z0-9]+\Z")
COMMON = {
    "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_ID", "BOT_USER_ID",
    "AGENT_KIND", "WORKDIR", "DELIVER_CHANNEL_MESSAGES", "OPERATOR_USER_ID",
}
SCHEMAS = {
    "claude": COMMON | {"CLAUDE_CONFIG_DIR", "CLAUDE_ARGS"},
    "codex": COMMON | {"CODEX_HOME", "CODEX_ARGS"},
    "antigravity": COMMON,
}
OPTIONAL = {"claude": {"CLAUDE_LAUNCHER", "CLAUDE_LAUNCHER_PATH"}}
EMPTY_OK = {"CLAUDE_ARGS", "CODEX_ARGS"}
MESSAGE_CHAR_CAP = 1000


class SendError(RuntimeError):
    pass


def load_agent(name: str, directory: Path = AGENTS_DIR) -> dict[str, str]:
    if not NAME_RE.fullmatch(name):
        raise SendError("invalid agent name")
    path = directory / f"{name}.env"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise SendError(f"cannot open agent credentials: {exc}") from exc
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SendError("agent credentials have invalid owner or type")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SendError("agent credentials must have mode 0600")
        lines = stream.read().splitlines()
    values = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not line or line.startswith("#"):
            continue
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise SendError("agent credentials have invalid syntax")
        values[key] = value
    kind = values.get("AGENT_KIND")
    expected = SCHEMAS.get(kind)
    if expected is None or set(values) - OPTIONAL.get(kind, set()) != expected:
        raise SendError("agent credentials have invalid shape")
    if any(not values[key] for key in set(values) - EMPTY_OK):
        raise SendError("agent credentials contain an empty required value")
    return values


def api(http, token: str, method: str, fields: dict) -> dict:
    request = urllib.request.Request(
        API_BASE + method,
        data=(urllib.parse.urlencode(fields).encode() if method in FORM_METHODS else json.dumps(fields).encode()),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-www-form-urlencoded" if method in FORM_METHODS else "application/json"},
    )
    try:
        with http(request, timeout=60) as response:
            result = json.load(response)
    except Exception as exc:
        raise SendError(f"Slack {method} transport failed: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        reason = result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"
        raise SendError(f"Slack {method} failed: {reason}")
    return result


def resolve_channel(http, token: str, supplied: str) -> str:
    if CHANNEL_ID_RE.fullmatch(supplied):
        return supplied
    name = supplied.removeprefix("#")
    cursor = None
    matches = set()
    while True:
        result = api(http, token, "conversations.list", {
            "types": "public_channel,private_channel", "limit": 200,
            "cursor": cursor,
        })
        channels = result.get("channels")
        if not isinstance(channels, list):
            raise SendError("Slack returned invalid channel data")
        matches.update(item.get("id") for item in channels if item.get("name") == name)
        metadata = result.get("response_metadata", {})
        cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
        if not cursor:
            break
    if len(matches) != 1 or not all(isinstance(item, str) for item in matches):
        raise SendError(f"channel {supplied!r} did not resolve uniquely")
    return matches.pop()


def verify_thread(http, token: str, channel: str, thread: str) -> None:
    try:
        found = api(http, token, "conversations.replies", {
            "channel": channel, "ts": thread, "limit": 1,
        }).get("messages")
    except SendError as exc:
        raise SendError(
            f"thread {thread} is not readable in channel {channel}: {exc}"
        ) from exc
    if not found:
        raise SendError(f"thread {thread} is not in channel {channel}")


def upload(http, token: str, channel: str, thread: str | None, text: str, source: str) -> str:
    path = Path(source).expanduser()
    if not path.is_file():
        raise SendError(f"file is not regular: {path}")
    content = path.read_bytes()
    ticket = api(http, token, "files.getUploadURLExternal", {
        "filename": path.name, "length": len(content),
    })
    url, file_id = ticket.get("upload_url"), ticket.get("file_id")
    if not isinstance(url, str) or not url.startswith("https://") or not isinstance(file_id, str):
        raise SendError("Slack returned an invalid upload ticket")
    try:
        with http(urllib.request.Request(url, data=content, method="POST"), timeout=120) as response:
            if response.getcode() != 200:
                raise SendError("Slack file transfer failed")
    except SendError:
        raise
    except Exception as exc:
        raise SendError(f"Slack file transfer failed: {exc}") from exc
    fields = {"files": [{"id": file_id, "title": path.name}], "channel_id": channel}
    if thread:
        fields["thread_ts"] = thread
    if text:
        fields["initial_comment"] = text
    api(http, token, "files.completeUploadExternal", fields)
    return file_id


def run(argv=None, http=urllib.request.urlopen, stdin=None, agents_dir=AGENTS_DIR):
    parser = argparse.ArgumentParser(prog="slack-send")
    parser.add_argument("--as", dest="sender", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--thread")
    parser.add_argument("--mention")
    parser.add_argument("--text")
    parser.add_argument("--file")
    args = parser.parse_args(argv)
    sender = load_agent(args.sender, agents_dir)
    text = args.text if args.text is not None else ("" if args.file else (stdin or sys.stdin).read())
    if args.mention:
        mention = f"<@{load_agent(args.mention, agents_dir)['BOT_USER_ID']}>"
        text = f"{mention} {text}" if text else mention
    text = text.replace("\\n", "\n")
    if not text.strip() and not args.file:
        raise SendError("message text is empty")
    if len(text) > MESSAGE_CHAR_CAP:
        raise SendError(
            f"Message is {len(text)} chars (cap {MESSAGE_CHAR_CAP}). Answer "
            "the question asked in <=3 sentences or a <=6-line labeled data "
            "list; put long content in a file and send it with --file, or "
            "reference its path."
        )
    channel = resolve_channel(http, sender["SLACK_BOT_TOKEN"], args.channel)
    if args.thread:
        verify_thread(http, sender["SLACK_BOT_TOKEN"], channel, args.thread)
    if args.file:
        return upload(http, sender["SLACK_BOT_TOKEN"], channel, args.thread, text, args.file)
    fields = {"channel": channel, "text": text}
    if args.thread:
        fields["thread_ts"] = args.thread
    timestamp = api(
        http, sender["SLACK_BOT_TOKEN"], "chat.postMessage", fields
    ).get("ts")
    if not isinstance(timestamp, str) or not timestamp:
        raise SendError("Slack returned an invalid message timestamp")
    return timestamp


def main() -> int:
    try:
        result = run()
    except (OSError, UnicodeError, SendError) as exc:
        print(f"slack-send: FATAL: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
