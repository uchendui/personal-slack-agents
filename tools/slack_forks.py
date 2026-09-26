#!/usr/bin/env python3
"""Print every fork the Slack bridge is running or has queued, one line each.

Reads forks.json, written by the bridge on every fork state change, and derives
each fork's STATE from the newest transcript record since its prompt."""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

STATUS_PATH = Path.home() / ".config" / "slack-bridge" / "forks.json"
COLUMNS = ("AGENT", "THREAD", "TASK", "FORK", "AGE", "STATE", "LAST")


def _parse_time(text: str) -> datetime.datetime:
    value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


def _age(since: str, now: datetime.datetime) -> str:
    seconds = max(0, int((now - _parse_time(since)).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _last_event(transcript: str, since: str) -> dict | None:
    """The newest record written at or after `since`, or None when there is
    none; the empty dict means the transcript file is missing or empty."""
    try:
        lines = Path(transcript).read_text().splitlines()
    except OSError:
        return {}
    if not any(line.strip() for line in lines):
        return {}
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and str(event.get("created_at", "")) >= since:
            return event
    return None


def _state(event: dict | None) -> str:
    if event == {}:
        return "no-transcript"
    if event is None:
        return "waiting-for-prompt"
    kind = str(event.get("type", ""))
    status = event.get("status")
    if kind == "USER_INPUT":
        return "prompt-taken"
    if kind == "PLANNER_RESPONSE" and status == "DONE" and event.get("content"):
        return "answered"
    if kind == "GENERIC":
        return "tool-done"
    if status != "DONE":
        return f"working:{kind.lower()}"
    return "working"


def rows(status: dict, now: datetime.datetime) -> list[tuple[str, ...]]:
    out = []
    for entry in status.get("running", []):
        event = _last_event(entry["transcript"], entry["since"])
        state = _state(event)
        if entry.get("closing"):
            state = "closing " + state
        if entry.get("idle"):
            state = "idle " + state
        last = " ".join(str((event or {}).get("content", "")).splitlines())[:60]
        out.append((
            entry["agent"],
            f"{entry['channel']}/{entry['thread_ts']}",
            entry.get("task", "")[:50],
            (entry.get("fork_id") or "-")[:8],
            _age(entry["since"], now),
            state,
            last,
        ))
    for entry in status.get("queued", []):
        out.append((
            entry["agent"],
            f"{entry['channel']}/{entry['thread_ts']}",
            entry.get("task", "")[:50],
            "-",
            _age(entry["first_ts"], now) if "T" in str(entry["first_ts"]) else "-",
            f"queued({entry['prompts']})",
            "",
        ))
    return out


def render(status: dict, now: datetime.datetime) -> str:
    table = [COLUMNS, *rows(status, now)]
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in table]
    written = int((now - _parse_time(status["written_at"])).total_seconds())
    lines.append(f"bridge status written {written}s ago")
    return "\n".join(lines)


def main(argv=None, path: Path = STATUS_PATH) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="dump forks.json raw")
    args = parser.parse_args(argv)
    try:
        text = path.read_text()
    except FileNotFoundError:
        print(
            "no forks.json: the bridge has not written status yet "
            "(needs bridge version with slack-forks)", file=sys.stderr,
        )
        return 1
    if args.json:
        sys.stdout.write(text)
        return 0
    print(render(json.loads(text), datetime.datetime.now(datetime.timezone.utc)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
