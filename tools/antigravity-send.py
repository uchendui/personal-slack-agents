#!/usr/bin/env python3
"""Send a message to an advertised local Antigravity PTY session.

Usage:
  antigravity-send <conversation-id-or-agent-name> "<message>"
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from typing import Any

import slack_register

PTY_STATE_DIR = Path.home() / ".local" / "state" / "slack-bridge" / "pty"
REGISTRY_PATH = Path.home() / ".config" / "slack-bridge" / "registry.json"
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)


def _process_stat(pid: int, proc_root: Path) -> tuple[int, str]:
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
    except OSError as exc:
        raise LookupError(f"process {pid} is not live") from exc
    closing_paren = raw.rfind(")")
    fields = raw[closing_paren + 2 :].split()
    if closing_paren < 0 or len(fields) <= 19:
        raise LookupError(f"cannot parse process state for {pid}")
    return int(fields[1]), fields[19]


def _secure_path(path: Path, expected_type: int, mode: int, label: str) -> None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise LookupError(f"{label} is unavailable: {path}") from exc
    if info.st_uid != os.getuid() or not expected_type(info.st_mode):
        raise PermissionError(f"{label} owner or type is invalid: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise PermissionError(f"{label} mode is invalid: {path}")


def _read_advertisement(path: Path) -> dict[str, Any]:
    _secure_path(path, stat.S_ISREG, 0o600, "PTY advertisement")
    try:
        advertisement = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"PTY advertisement is unreadable: {path}") from exc
    if not isinstance(advertisement, dict):
        raise ValueError(f"PTY advertisement is not an object: {path}")
    return advertisement


def _resolve_advertisement(
    target: str, state_dir: Path, registry_path: Path
) -> tuple[Path, dict[str, Any]]:
    normalized = target.strip()
    if not normalized:
        raise ValueError("target must not be empty")
    conversation_id = normalized.lower() if UUID_RE.fullmatch(normalized) else None
    registry = slack_register.load_registry(registry_path)
    agent = registry["agents"].get(normalized)
    agent_session = agent.get("session_id") if agent is not None else None
    if conversation_id is None and agent is None:
        raise LookupError(f"Slack agent is not actively registered: {normalized}")
    _secure_path(state_dir, stat.S_ISDIR, 0o700, "PTY state directory")
    matches = []
    for path in sorted(state_dir.glob("*.json")):
        if not path.stem.isdigit():
            continue
        advertisement = _read_advertisement(path)
        advertised_conversation = advertisement.get("conversation_id")
        if not isinstance(advertised_conversation, str):
            continue
        advertised_conversation = advertised_conversation.lower()
        if advertised_conversation in {conversation_id, agent_session}:
            matches.append((path, advertisement))
    if len(matches) != 1:
        raise LookupError(f"expected one live Antigravity target {target!r}, found {len(matches)}")
    return matches[0]


def _validate_advertisement(
    path: Path, advertisement: dict[str, Any], state_dir: Path, proc_root: Path
) -> tuple[Path, str, int]:
    required = {
        "broker_pid": int,
        "socket": str,
        "pgid": int,
        "proc_start": str,
        "kind": str,
        "child_pid": int,
        "child_proc_start": str,
        "token": str,
    }
    if any(not isinstance(advertisement.get(key), value_type) for key, value_type in required.items()):
        raise ValueError(f"PTY advertisement is incomplete: {path}")
    broker_pid = advertisement["broker_pid"]
    child_pid = advertisement["child_pid"]
    if (
        broker_pid <= 0
        or child_pid <= 0
        or advertisement["pgid"] <= 0
        or advertisement["kind"] != "interactive"
        or not advertisement["token"]
        or broker_pid != int(path.stem)
    ):
        raise ValueError(f"PTY advertisement has invalid session state: {path}")
    socket_path = Path(advertisement["socket"])
    if socket_path != state_dir / f"{broker_pid}.sock":
        raise ValueError(f"PTY advertisement socket does not match its broker: {path}")
    _, broker_start = _process_stat(broker_pid, proc_root)
    child_parent, child_start = _process_stat(child_pid, proc_root)
    if str(advertisement["proc_start"]) != broker_start:
        raise LookupError(f"PTY broker identity changed for target in {path}")
    if child_parent != broker_pid or str(advertisement["child_proc_start"]) != child_start:
        raise LookupError(f"PTY child identity changed for target in {path}")
    _secure_path(socket_path, stat.S_ISSOCK, 0o600, "PTY broker socket")
    return socket_path, advertisement["token"], child_pid


def send_message(
    target: str,
    message: str,
    *,
    state_dir: Path = PTY_STATE_DIR,
    proc_root: Path = Path("/proc"),
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, str]:
    path, advertisement = _resolve_advertisement(target, state_dir, registry_path)
    socket_path, token, child_pid = _validate_advertisement(
        path, advertisement, state_dir, proc_root
    )
    payload = json.dumps(
        {"token": token, "target_pid": child_pid, "text": message},
        separators=(",", ":"),
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(65)
        client.connect(str(socket_path))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        response = client.recv(4096)
    if response != b"OK\n":
        raise RuntimeError(f"PTY broker rejected delivery: {response!r}")
    return {
        "status": "delivered",
        "recipient": advertisement.get("conversation_id") or advertisement["agent_name"],
        "transport": "pty",
    }


def main() -> None:
    if len(sys.argv) != 3:
        print(
            'Usage: antigravity-send <conversation-id-or-agent-name> "<message>"',
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        print(json.dumps(send_message(sys.argv[1], sys.argv[2])))
    except Exception as exc:
        print(f"antigravity-send: error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
