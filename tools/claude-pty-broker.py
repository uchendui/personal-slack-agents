#!/usr/bin/env python3
"""Run Claude Code behind an authenticated PTY injection socket.

This is a thin adapter. The PTY, the authenticated socket, target
validation, and bracketed-paste injection all live in pty_broker.py and are
shared with every other runtime. What belongs here is only what is specific
to Claude Code: where its state and session registries live, the three
environment variable names its children are stamped with, and the exact
slash commands it has been verified to accept.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

PROGRAM = "claude-pty-broker"
CORE_PATH = Path(__file__).resolve().parent / "pty_broker.py"


def load_core(program: str, core_path: Path):
    """Load the shared broker core exactly once per process.

    Every adapter loads the core by path, because the adapters are scripts
    rather than an installed package. Registering the module under its own
    name first means a second adapter in the same process reuses this module
    object instead of executing a second copy with its own Profile class,
    which would make isinstance checks fail across the two. A core that is
    missing, unreadable, or does not parse is an initialization failure and
    exits 97, the same code the broker uses for dying before it ever ran, so
    a launcher can tell that apart from a child's own exit status.
    """
    existing = sys.modules.get("pty_broker")
    if existing is not None and getattr(existing, "__file__", None) == str(core_path):
        return existing
    try:
        spec = importlib.util.spec_from_file_location("pty_broker", core_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {core_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pty_broker"] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop("pty_broker", None)
        print(
            f"{program}: FATAL: cannot load PTY broker core at {core_path}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(97) from exc
    return module


pty_broker = load_core(PROGRAM, CORE_PATH)

Control = pty_broker.Control
Profile = pty_broker.Profile

MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}\Z")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultracode")
GOAL_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,4096}\Z")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")

CONTROLS = {
    "compact": Control(keystrokes="/compact"),
    "stop": Control(keystrokes=b"\x03", raw=True),
    # /model <name> opens an "are you sure" dialog; confirm it like /effort.
    "model": Control(keystrokes="/model {argument}", argument=MODEL_RE, confirm=b"\r"),
    "effort": Control(
        keystrokes="/effort {argument}",
        argument=EFFORT_LEVELS,
        confirm=b"\r",
        acknowledgment="Set effort level to {argument}",
    ),
    "goal": Control(keystrokes="/goal {argument}", argument=GOAL_RE),
    "rename": Control(keystrokes="/rename {argument}", argument=NAME_RE),
    "clear-goal": Control(keystrokes="/goal clear"),
    "clear": Control(keystrokes="/clear"),
}


def idle_compact_seconds_from_environment() -> float | None:
    value = os.environ.get("CLAUDE_PTY_IDLE_COMPACT_SECONDS")
    if value == "0":
        return None
    return 600.0 if value is None else float(value)


PROFILE = Profile(
    program=PROGRAM,
    state_dir=Path.home() / ".local" / "state" / "slack-bridge" / "pty",
    session_dir=Path.home() / ".claude-sessions-shared",
    socket_env="CLAUDE_PTY_BROKER_SOCKET",
    token_env="CLAUDE_PTY_BROKER_TOKEN",
    pid_env="CLAUDE_PTY_BROKER_PID",
    controls=CONTROLS,
    idle_compact_seconds=idle_compact_seconds_from_environment(),
    account_cycle=pty_broker.AccountCycle(
        config_env="CLAUDE_CONFIG_DIR",
        cycle_env="CLAUDE_ACCOUNT_CYCLE",
        limit_re=re.compile(r"You've (?:reached|hit) your .{0,40}?(?:limit|budget)"),
        resume_flag="--resume",
    ),
)


def normalize_control(control: dict) -> bytes | None:
    """Validate a control request against Claude's own allowlist."""
    return pty_broker.normalize_control(control, CONTROLS)


def main() -> None:
    pty_broker.main(PROFILE)


if __name__ == "__main__":
    main()
