#!/usr/bin/env python3
"""Declarative runtime specifications for Slack bridge adapters."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class RuntimeSpec:
    name: str
    adapter_file: str
    cli_command: tuple[str, ...]
    config_env_var: str | None
    session_discovery_mode: str  # "registry" or "pty_advertisement"
    transcript_mode: str  # "status_file" or "jsonl_done"
    unsupported_controls: frozenset[str] = frozenset()


BACKGROUND_CONTEXT_TEMPLATE = "[Slack background context; no reply expected]\n[{timestamp}] {user}: {text}"

RUNTIMES: dict[str, RuntimeSpec] = {
    "antigravity": RuntimeSpec(
        name="antigravity",
        adapter_file="antigravity-pty-broker.py",
        cli_command=(str(Path.home() / ".local" / "bin" / "agy"),),
        config_env_var=None,
        session_discovery_mode="pty_advertisement",
        transcript_mode="jsonl_done",
        unsupported_controls=frozenset({"goal", "clear-goal", "clear", "compact"}),
    ),
    "claude": RuntimeSpec(
        name="claude",
        adapter_file="claude-pty-broker.py",
        cli_command=("claude",),
        config_env_var="CLAUDE_CONFIG_DIR",
        session_discovery_mode="registry",
        transcript_mode="status_file",
        unsupported_controls=frozenset(),
    ),
}
