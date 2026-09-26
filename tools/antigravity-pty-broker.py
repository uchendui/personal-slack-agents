#!/usr/bin/env python3
"""Run Antigravity CLI behind the shared authenticated PTY broker."""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

PROGRAM = "antigravity-pty-broker"
CORE_PATH = Path(__file__).resolve().parent / "pty_broker.py"
CLI_PATH = Path.home() / ".local" / "bin" / "agy"
SETTINGS_PATH = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
PRESENCE_DIR = Path.home() / ".gemini" / "antigravity-cli" / "presence"
PROC_ROOT = Path("/proc")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def load_core(program: str, core_path: Path):
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


def conversation_id_from_target(target: str) -> str | None:
    if target.endswith(" (deleted)"):
        return None
    path = Path(target)
    if path.parent != PRESENCE_DIR or path.suffix != ".lock":
        return None
    conversation_id = path.stem
    if not UUID_RE.fullmatch(conversation_id):
        return None
    return conversation_id.lower()


def conversation_ids_for_process(pid: int, proc_root: Path = PROC_ROOT) -> set[str]:
    fd_dir = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_dir.iterdir())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read open descriptors of process {pid} at {fd_dir}: {exc}"
        ) from exc
    found = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"cannot read descriptor {entry} of process {pid}: {exc}"
            ) from exc
        conversation_id = conversation_id_from_target(target)
        if conversation_id is not None:
            found.add(conversation_id)
    return found


def discover_conversation_id(pid: int, proc_root: Path = PROC_ROOT) -> str | None:
    found = conversation_ids_for_process(pid, proc_root)
    if not found:
        return None
    if len(found) > 1:
        raise RuntimeError(
            "the launched process has more than one Antigravity conversation open: "
            + ", ".join(sorted(found))
        )
    return found.pop()


def validate_settings() -> None:
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid Antigravity settings file {SETTINGS_PATH}: {exc}") from exc
    if not isinstance(settings, dict) or settings.get("modelProvider") != "gemini":
        raise RuntimeError(
            f"Antigravity settings file must set modelProvider to gemini: {SETTINGS_PATH}"
        )


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}\Z")
EFFORT_LEVELS = ("low", "medium", "high")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
CONTROLS = {
    "stop": Control(keystrokes=b"\x03", raw=True),
    "model": Control(keystrokes="/model {argument}", argument=MODEL_RE),
    "effort": Control(keystrokes="/effort {argument}", argument=EFFORT_LEVELS),
    "rename": Control(keystrokes="/rename {argument}", argument=NAME_RE),
}
PROFILE = Profile(
    program=PROGRAM,
    state_dir=Path.home() / ".local" / "state" / "slack-bridge" / "pty",
    session_dir=None,
    socket_env="ANTIGRAVITY_PTY_BROKER_SOCKET",
    token_env="ANTIGRAVITY_PTY_BROKER_TOKEN",
    pid_env="ANTIGRAVITY_PTY_BROKER_PID",
    controls=CONTROLS,
    required_command=str(CLI_PATH),
    conversation_discovery=discover_conversation_id,
)


def main() -> None:
    validate_settings()
    pty_broker.main(PROFILE)


if __name__ == "__main__":
    main()
