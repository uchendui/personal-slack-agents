#!/usr/bin/env python3

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_spawn.py"
sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("slack_spawn_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SpawnTest(unittest.TestCase):
    def test_registers_then_opens_window_in_pane_workdir(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            stdout = "/work/admin\n" if argv[1] == "display-message" else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(module.slack_register, "_live_claude_args", return_value="--model opus"), \
             mock.patch.object(module.slack_register, "_register") as register:
            module.run(["admin-agent", "--tmux-session", "admin", "--join", "ops",
                        "--claude-config-dir", "/profiles/claude"])

        self.assertEqual(calls[0], ("tmux", "display-message", "-p", "-t", "admin:", "#{pane_current_path}"))
        args, kwargs = register.call_args
        self.assertEqual(args, ("admin-agent", "claude", Path("/work/admin"), Path("/profiles/claude"),
                                "--model opus --dangerously-skip-permissions", None, ("all-agents", "ops")))
        session = kwargs["session"]
        home_bin = Path.home() / ".local" / "bin"
        self.assertEqual(calls[1], (
            "tmux", "new-window", "-d", "-t", "admin:", "-n", "admin-agent", "-c", "/work/admin",
            f"env CLAUDE_CONFIG_DIR=/profiles/claude {home_bin / 'claude-pty-broker'} -- "
            f"{home_bin / 'claude'} --session-id {session} --name admin-agent --model opus "
            "--dangerously-skip-permissions",
        ))

    def test_custom_launcher_runs_behind_the_broker_and_is_stored(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            stdout = "/work/admin\n" if argv[1] == "display-message" else ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with mock.patch.object(module.slack_register.shutil, "which", return_value="/opt/bin/ccr"), \
             mock.patch.dict(module.os.environ, {"PATH": "/opt/node/bin:/usr/bin"}), \
             mock.patch.object(module.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(module.slack_register, "_live_claude_args") as live, \
             mock.patch.object(module.slack_register, "_register") as register:
            module.run(["flash-agent", "--tmux-session", "admin", "--launcher", "ccr cc-gemini cli --",
                        "--claude-config-dir", "/profiles/cc-gemini/claude",
                        "--model", "Gemini API,gemini-3.8-flash"])

        live.assert_not_called()
        args, kwargs = register.call_args
        runtime = "--model 'Gemini API,gemini-3.8-flash' --dangerously-skip-permissions"
        self.assertEqual(args, ("flash-agent", "claude", Path("/work/admin"), Path("/profiles/cc-gemini/claude"),
                                runtime, None, ("all-agents",)))
        self.assertEqual(kwargs["launcher"], "/opt/bin/ccr cc-gemini cli --")
        home_bin = Path.home() / ".local" / "bin"
        self.assertEqual(module.shlex.split(calls[1][-1]), [
            "env", "CLAUDE_CONFIG_DIR=/profiles/cc-gemini/claude", "PATH=/opt/node/bin:/usr/bin",
            str(home_bin / "claude-pty-broker"), "--",
            "/opt/bin/ccr", "cc-gemini", "cli", "--", "--session-id", kwargs["session"],
            "--name", "flash-agent", "--model", "Gemini API,gemini-3.8-flash", "--dangerously-skip-permissions",
        ])

    def test_unusable_launcher_dies_before_registering(self):
        for launcher in ("nope cli --", "ccr 'unbalanced"):
            with self.subTest(launcher=launcher), \
                 mock.patch.object(module.slack_register.shutil, "which", return_value=None), \
                 mock.patch.object(module.subprocess, "run") as run, \
                 mock.patch.object(module.slack_register, "_register") as register, \
                 mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    module.run(["x", "--tmux-session", "work", "--launcher", launcher,
                                "--claude-config-dir", "/p"])
                run.assert_not_called()
                register.assert_not_called()

    def test_missing_tmux_session_dies_before_registering(self):
        failed = subprocess.CompletedProcess((), 1, "", "can't find session: nope")
        with mock.patch.object(module.subprocess, "run", return_value=failed), \
             mock.patch.object(module.slack_register, "_register") as register:
            with self.assertRaisesRegex(module.slack_register.RegisterError, "can't find session"):
                module.run(["x", "--tmux-session", "nope", "--claude-config-dir", "/p"])
        register.assert_not_called()


if __name__ == "__main__":
    unittest.main()
