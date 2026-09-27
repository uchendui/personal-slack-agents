#!/usr/bin/env python3

import importlib.util
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "claude-pty-broker.py"
SPEC = importlib.util.spec_from_file_location("claude_pty_broker", MODULE_PATH)
broker_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker_module)
# The Claude entry point is a thin adapter; the machinery under test for
# terminal, socket, and write behaviour lives in the shared core it loads.
core_module = broker_module.pty_broker

def bare_broker(**attributes):
    """A Broker with only the attributes a test drives, never a started one."""
    broker = object.__new__(core_module.Broker)
    broker.__dict__.update(attributes)
    return broker


class BrokerInjectionTest(unittest.TestCase):
    def broker(self):
        broker = core_module.Broker(["/bin/true"], broker_module.PROFILE)
        broker.write_master = mock.Mock()
        broker.target_status = mock.Mock(return_value="shell")
        broker.journal = mock.Mock()
        return broker

    def test_all_alive_statuses_inject_immediately(self):
        broker = self.broker()
        statuses = ("idle", "shell", "busy", "waiting", "")
        broker.target_status.side_effect = statuses

        deadline = core_module.time.monotonic() + 100
        results = [
            broker.inject_request(123, b"payload", deadline=deadline)
            for _ in statuses
        ]

        expected_calls = []
        for _ in statuses:
            expected_calls.append((core_module.BRACKETED_PASTE_START + b"payload" + core_module.BRACKETED_PASTE_END, deadline))
            expected_calls.append((b"\r", deadline))
        self.assertEqual(tuple(results), (None,) * len(statuses))
        # Every status injects, and each write carries the request deadline.
        self.assertEqual([call.args for call in broker.write_master.call_args_list], expected_calls)
        # Injection and its best-effort acceptance each record one report.
        self.assertEqual(broker.journal.call_count, len(statuses) * 2)

    def test_acceptance_is_always_best_effort(self):
        broker = self.broker()

        for status in ("idle", "shell", "busy", "waiting", ""):
            broker.record_best_effort_acceptance(123, status)

        broker.target_status.assert_not_called()
        self.assertEqual(broker.journal.call_count, 5)

    def test_normal_messages_still_cannot_dispatch_slash_commands(self):
        encoded = core_module.normalize_injected_text("/compact")

        self.assertTrue(encoded.startswith("⁠".encode()))

    def test_a_control_outside_the_allowlist_is_refused(self):
        for control in (
            {"command": "rename", "argument": None},            # no argument at all
            {"command": "effort", "argument": "turbo"},         # outside its allowlist
            {"command": "compact", "extra": True},              # extra, takes no argument
            {"command": "model", "argument": "opus", "extra": True},  # extra, takes one
            {"command": "model", "argument": "opus\n/compact"},  # a second command
            {"command": "shell", "argument": "rm"},             # not a command at all
        ):
            with self.subTest(control=control), self.assertRaises(ValueError):
                broker_module.normalize_control(control)

    def test_raw_stop_injects_only_one_interrupt_byte(self):
        broker = self.broker()
        deadline = core_module.time.monotonic() + 100

        broker.inject_request(123, b"\x03", deadline, raw=True)

        broker.write_master.assert_called_once_with(b"\x03", deadline)

    def test_model_and_effort_controls_confirm_their_dialog_and_compact_does_not(self):
        for command, confirm, acknowledgment in (
            ("model", b"\r", None),
            ("effort", b"\r", b"Set effort level to low"),
            ("compact", None, None),
        ):
            with self.subTest(command=command):
                broker = self.broker()
                broker.token, broker.profile = "token", broker_module.PROFILE
                broker.record_best_effort_acceptance = mock.Mock()
                control = {"command": command}
                if command == "effort":
                    control["argument"] = "low"
                elif command == "model":
                    control["argument"] = "opus"
                broker.read_request = mock.Mock(return_value={
                    "token": "token", "target_pid": 123, "control": control,
                })
                broker.validate_target_environment = mock.Mock()
                broker.inject_request = mock.Mock(return_value="idle")
                with mock.patch.object(
                    core_module.struct, "unpack",
                    return_value=(456, core_module.os.getuid(), 0),
                ):
                    broker.handle_connection(mock.Mock())
                self.assertEqual(broker.inject_request.call_args.kwargs["confirm"], confirm)
                self.assertEqual(
                    broker.inject_request.call_args.kwargs["acknowledgment"], acknowledgment
                )
                broker.write_master.assert_not_called()

    def test_a_request_is_refused_before_it_can_reach_a_target(self):
        broker = self.broker()
        broker.token, broker.profile = "token", broker_module.PROFILE
        ours = core_module.os.getuid()
        request = {"token": "token", "target_pid": 123,
                   "control": {"command": "compact"}}
        for case, peer_uid, request, refusal in (
            ("a peer belonging to someone else", ours + 1, request, PermissionError),
            ("a token that does not match", ours, dict(request, token="guessed"),
             PermissionError),
            ("a token that is not a string", ours, dict(request, token=17),
             PermissionError),
            ("fields the request may not carry", ours, dict(request, extra=True),
             ValueError),
        ):
            with self.subTest(case=case):
                broker.read_request = mock.Mock(return_value=request)
                broker.validate_target_environment = mock.Mock()
                broker.inject_request = mock.Mock()
                with (
                    mock.patch.object(core_module.struct, "unpack",
                                      return_value=(456, peer_uid, 0)),
                    self.assertRaises(refusal),
                ):
                    broker.handle_connection(mock.Mock())

                # Refused before the target is validated, let alone typed into.
                broker.validate_target_environment.assert_not_called()
                broker.inject_request.assert_not_called()

    def test_a_target_must_be_ours_stamped_by_us_and_in_the_foreground(self):
        broker = self.broker()
        broker.pid, broker.master_fd, broker.token = 500, 7, "tok"
        broker.socket_path = Path("/run/user/1000/broker/500.sock")
        broker.profile = broker_module.PROFILE
        stamped = {
            broker_module.PROFILE.socket_env: str(broker.socket_path),
            broker_module.PROFILE.token_env: "tok",
            broker_module.PROFILE.pid_env: "500",
        }
        for case, parent, environment, target_pgid, foreground, refused in (
            ("ours, stamped, in the foreground", 500, stamped, 900, 900, False),
            ("a process this broker does not own", 999, stamped, 900, 900, True),
            ("stamped with another broker's socket", 500,
             dict(stamped, **{broker_module.PROFILE.socket_env: "/tmp/other.sock"}),
             900, 900, True),
            ("stamped with another broker's token", 500,
             dict(stamped, **{broker_module.PROFILE.token_env: "other"}), 900, 900, True),
            ("stamped with another broker's pid", 500,
             dict(stamped, **{broker_module.PROFILE.pid_env: "501"}), 900, 900, True),
            ("not the group the PTY is talking to", 500, stamped, 900, 901, True),
        ):
            with self.subTest(case=case):
                with (
                    mock.patch.object(core_module, "process_stat",
                                      return_value=(parent, "1")),
                    mock.patch.object(core_module, "process_environment",
                                      return_value=environment),
                    mock.patch.object(core_module.os, "getpgid",
                                      return_value=target_pgid),
                    mock.patch.object(core_module.os, "tcgetpgrp",
                                      return_value=foreground),
                ):
                    if refused:
                        with self.assertRaises(LookupError):
                            broker.validate_target_environment(900)
                    else:
                        self.assertIsNone(broker.validate_target_environment(900))

    def test_a_request_body_stops_at_the_size_bound(self):
        broker = self.broker()
        chunk = b"x" * 65536
        reads = []

        class Connection:
            def recv(self, size):
                reads.append(size)
                return chunk if len(reads) <= 40 else b""

        with self.assertRaises(ValueError):
            broker.read_request(Connection())

        # Refused within one chunk of the bound rather than draining the peer,
        # which is what stops a client dictating the broker's memory.
        self.assertLessEqual(len(reads) * len(chunk),
                             core_module.MAX_REQUEST_BYTES + len(chunk))

    def test_cleanup_removes_the_socket_it_advertised(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = bare_broker(
                socket_path=Path(directory) / "500.sock", journal=mock.Mock(),
                stop=threading.Event(),
                advertisement_path=Path(directory) / "500.json", child_status=0,
                listener=None, child_pid=None, master_fd=None, saved_termios=None,
                output_thread=None, injector_thread=None)
            broker.socket_path.write_text("")

            broker.cleanup()

            self.assertFalse(broker.socket_path.exists())

    def test_write_master_retries_eagain_and_partial_writes_in_order(self):
        broker = bare_broker(master_fd=99, master_write_lock=threading.Lock(),
                             stop=threading.Event())
        writes = []

        def write(_fd, data):
            if not writes:
                writes.append("eagain")
                raise BlockingIOError(core_module.errno.EAGAIN, "full")
            if len(writes) == 1:
                writes.append(bytes(data[:2]))
                return 2
            writes.append(bytes(data))
            return len(data)

        with (
            mock.patch.object(
                core_module.select,
                "select",
                return_value=([], [99], []),
            ),
            mock.patch.object(core_module.os, "write", side_effect=write),
        ):
            broker.write_master(
                b"abcd",
                deadline=core_module.time.monotonic() + 10,
            )

        self.assertEqual(writes, ["eagain", b"ab", b"cd"])

    def test_write_master_waits_without_busy_spin_and_honors_deadline(self):
        broker = bare_broker(master_fd=99, master_write_lock=threading.Lock(),
                             stop=threading.Event())
        with (
            mock.patch.object(
                core_module.time,
                "monotonic",
                side_effect=[0.0, 0.1, 0.3],
            ),
            mock.patch.object(
                core_module.select,
                "select",
                return_value=([], [], []),
            ) as select_call,
            self.assertRaises(TimeoutError),
        ):
            broker.write_master(b"data", deadline=0.2)

        # Two waits and no write: the deadline ended it, not a failed write.
        self.assertEqual(select_call.call_count, 2)

    def test_injector_loop_recovers_after_unhandled_error(self):
        broker = self.broker()

        class Listener:
            calls = 0

            def accept(listener_self):
                listener_self.calls += 1
                if listener_self.calls == 1:
                    raise RuntimeError("boom")
                broker.stop.set()
                raise OSError("stopped")

        listener = Listener()
        broker.listener = listener
        with mock.patch.object(core_module.time, "sleep") as sleep:
            broker.injector_loop()

        self.assertEqual(listener.calls, 2)
        sleep.assert_called_once_with(0.1)
        # The second accept stops the loop before it can report, so the one
        # record is the recovery from the first.
        self.assertEqual(broker.journal.call_count, 1)


class BrokerJournalTest(unittest.TestCase):
    def test_journal_persists_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = bare_broker(journal_path=Path(directory) / "broker.log",
                                 journal_lock=threading.Lock(), profile=broker_module.PROFILE)

            broker.journal("step reached")

            self.assertEqual(
                len(broker.journal_path.read_text().splitlines()), 1)
            self.assertEqual(stat.S_IMODE(broker.journal_path.stat().st_mode), 0o600)


class ClaudeAdapterParityTest(unittest.TestCase):
    def test_every_claude_control_renders_its_declared_keystrokes(self):
        expected = {
            ("compact", None): b"/compact",
            ("stop", None): b"\x03",
            ("model", "opus"): b"/model opus",
            ("effort", "xhigh"): b"/effort xhigh",
            ("goal", "ship it"): b"/goal ship it",
            ("rename", "new-name"): b"/rename new-name",
            ("clear-goal", None): b"/goal clear",
            ("clear", None): b"/clear",
            ("fast", "off"): b"/fast off",
        }
        for (command, argument), keystrokes in expected.items():
            control = {"command": command}
            if argument is not None:
                control["argument"] = argument
            with self.subTest(command):
                self.assertEqual(broker_module.normalize_control(control), keystrokes)

    def test_adapter_profile_keeps_the_claude_environment_names(self):
        self.assertEqual(
            (
                broker_module.PROFILE.socket_env,
                broker_module.PROFILE.token_env,
                broker_module.PROFILE.pid_env,
            ),
            (
                "CLAUDE_PTY_BROKER_SOCKET",
                "CLAUDE_PTY_BROKER_TOKEN",
                "CLAUDE_PTY_BROKER_PID",
            ),
        )
        self.assertEqual(broker_module.PROFILE.program, "claude-pty-broker")
        self.assertTrue(broker_module.PROFILE.state_dir.is_absolute())
        self.assertTrue(broker_module.PROFILE.session_dir.is_absolute())
        self.assertEqual(broker_module.PROFILE.idle_compact_seconds, 600.0)
        self.assertEqual(
            (
                broker_module.PROFILE.account_cycle.config_env,
                broker_module.PROFILE.account_cycle.cycle_env,
                broker_module.PROFILE.account_cycle.resume_flag,
            ),
            ("CLAUDE_CONFIG_DIR", "CLAUDE_ACCOUNT_CYCLE", "--resume"),
        )

    def test_account_cycle_matches_only_claude_usage_limit_messages(self):
        limit_re = broker_module.PROFILE.account_cycle.limit_re
        self.assertIsNotNone(
            limit_re.search(
                "You've reached your Fable limit. Switch to another model, or "
                "manage usage credits at claude.ai/settings/usage?from=cc_cli_limit_message, "
                "to continue."
            )
        )
        self.assertIsNotNone(limit_re.search("You've reached your monthly spend limit"))
        self.assertIsNotNone(limit_re.search("You've hit your team's shared budget"))
        self.assertIsNone(limit_re.search("Set effort level to high"))

    def test_idle_compact_environment_override(self):
        with mock.patch.dict(
            broker_module.os.environ,
            {"CLAUDE_PTY_IDLE_COMPACT_SECONDS": "0"},
        ):
            self.assertIsNone(broker_module.idle_compact_seconds_from_environment())
        with mock.patch.dict(
            broker_module.os.environ,
            {"CLAUDE_PTY_IDLE_COMPACT_SECONDS": "12.5"},
        ):
            self.assertEqual(
                broker_module.idle_compact_seconds_from_environment(), 12.5
            )


if __name__ == "__main__":
    unittest.main()
