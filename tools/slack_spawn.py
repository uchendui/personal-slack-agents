#!/usr/bin/env python3
"""Register a Slack identity and start a new Claude session bound to it in a
window of an existing tmux session."""

from __future__ import annotations
import argparse, os, shlex, subprocess, sys, uuid
from pathlib import Path

import slack_register

def _tmux(*argv: str) -> str:
    result = subprocess.run(("tmux", *argv), capture_output=True, text=True)
    if result.returncode != 0:
        raise slack_register.RegisterError(result.stderr.strip() or "tmux exited nonzero")
    return result.stdout.strip()


def run(argv=None):
    parser = argparse.ArgumentParser(prog="slack-spawn")
    parser.add_argument("name")
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("--join", action="append")
    parser.add_argument("--claude-config-dir", type=Path)
    parser.add_argument("--launcher", default="claude",
                        help="command prefix that runs Claude Code, e.g. 'ccr cc-work cli --'")
    parser.add_argument("--model", help="replaces the live session's --model/--effort")
    args = parser.parse_args(argv)
    config_dir = args.claude_config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir is None:
        parser.error("--claude-config-dir is required when CLAUDE_CONFIG_DIR is unset")
    profile = Path(config_dir).expanduser().resolve()
    try:
        launcher = slack_register.resolve_launcher(args.launcher)
    except ValueError as exc:
        parser.error(f"--launcher: {exc}")
    target = f"{args.tmux_session}:"
    workdir = Path(_tmux("display-message", "-p", "-t", target, "#{pane_current_path}"))
    # A custom launcher can talk to another provider, so the live session's
    # model flags never carry over to it.
    if args.model:
        runtime_args = f"--model {shlex.quote(args.model)}"
    elif launcher == "claude":
        runtime_args = slack_register._live_claude_args()
    else:
        runtime_args = ""
    # Stored with the other args so revive_parent restarts it the same way.
    runtime_args = f"{runtime_args} --dangerously-skip-permissions".lstrip()
    # The bridge binds a name to the registry's session_id and delivers only
    # when the live Claude session with that id also carries the name, so
    # the id is chosen here and both are passed to claude.
    session = str(uuid.uuid4())
    slack_register._register(
        args.name, "claude", workdir, profile, runtime_args, None,
        slack_register.joins(args.join), session=session, launcher=launcher,
    )
    command = slack_register.slack_live_delivery.launch_command(
        profile, launcher, os.environ["PATH"], f"--session-id {session} --name {shlex.quote(args.name)}", runtime_args
    )
    _tmux("new-window", "-d", "-t", target, "-n", args.name, "-c", str(workdir), command)
    print(f"{args.name} started in tmux session {args.tmux_session} at {workdir}")
    return 0


def main():
    try:
        return run()
    except (OSError, slack_register.RegisterError, subprocess.SubprocessError) as exc:
        print(f"slack-spawn: FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
