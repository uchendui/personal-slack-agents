"""Tests for the runtime-neutral PTY broker core.

These belong to pty_broker.py itself, not to any adapter, so they stay
passing on a machine that carries the core without the Claude runtime.
"""

import importlib.util
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "pty_broker.py"
SPEC = importlib.util.spec_from_file_location("pty_broker", MODULE_PATH)
core_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core_module)


def profile(**overrides):
    arguments = dict(
        program="probe-pty-broker",
        state_dir=Path("/tmp/probe-state"),
        session_dir=Path("/tmp/probe-sessions"),
        socket_env="PROBE_PTY_BROKER_SOCKET",
        token_env="PROBE_PTY_BROKER_TOKEN",
        pid_env="PROBE_PTY_BROKER_PID",
        controls={"status": core_module.Control()},
    )
    arguments.update(overrides)
    return core_module.Profile(**arguments)


def bare_broker(**attributes):
    """A Broker with only the attributes a test drives, never a started one."""
    broker = object.__new__(core_module.Broker)
    broker.__dict__.update(attributes)
    return broker


def injection_broker():
    broker = core_module.Broker(["/bin/true"], profile())
    broker.target_status = mock.Mock(return_value="idle")
    broker.write_master = mock.Mock()
    broker.journal = mock.Mock()
    return broker


def idle_compact_broker(session_dir: Path, seconds: float | None):
    broker = injection_broker()
    broker.profile = profile(
        session_dir=session_dir,
        controls={"compact": core_module.Control(keystrokes="/compact")},
        idle_compact_seconds=seconds,
    )
    broker.child_pid = 4321
    broker.compact_armed = True
    broker.last_input_at = 100.0
    broker.next_idle_compact_check_at = 0.0
    return broker


def write_session(broker, status: str, status_updated_at: int):
    (broker.profile.session_dir / f"{broker.child_pid}.json").write_text(
        json.dumps(
            {
                "pid": broker.child_pid,
                "status": status,
                "statusUpdatedAt": str(status_updated_at),
            }
        )
    )


# A runtime whose commands are not Claude's, so the core is exercised through
# keystrokes and framing it cannot have hard-coded.
FOREIGN_CONTROLS = {
    "status": core_module.Control(),
    "compact": core_module.Control(keystrokes="/summarize"),
    "stop": core_module.Control(keystrokes=b"\x03", raw=True),
    "model": core_module.Control(
        keystrokes="/use-model {argument}", argument=("fast", "slow")
    ),
}


class PtyBrokerCoreTest(unittest.TestCase):
    def test_complete_and_incomplete_profiles(self):
        self.assertEqual(profile().program, "probe-pty-broker")
        cases = {
            "blank program": ({"program": ""}, ValueError),
            "relative state dir": ({"state_dir": Path("state")}, ValueError),
            "relative session dir": ({"session_dir": Path("sessions")}, ValueError),
            "lowercase env name": ({"socket_env": "probe_socket"}, ValueError),
            "duplicate env names": (
                {"token_env": "PROBE_PTY_BROKER_SOCKET"},
                ValueError,
            ),
            "no controls": ({"controls": {}}, ValueError),
            "relative required command": ({"required_command": "bin/echo"}, ValueError),
            "control is not a Control": (
                {"controls": {"status": "/status"}},
                TypeError,
            ),
        }
        for label, (override, expected) in cases.items():
            with self.subTest(label), self.assertRaises(expected):
                profile(**override)

    def test_control_shape_is_validated_at_declaration(self):
        with self.assertRaises(ValueError):  # argument declared, never interpolated
            core_module.Control(keystrokes="/model", argument=("opus",))
        with self.assertRaises(ValueError):  # interpolated, but no argument declared
            core_module.Control(keystrokes="/model {argument}")
        with self.assertRaises(ValueError):  # raw control that types nothing
            core_module.Control(raw=True)
        with self.assertRaises(ValueError):  # bytes cannot carry an argument
            core_module.Control(keystrokes=b"\x1b", argument=("opus",))

    def test_broker_refuses_to_construct_without_a_profile(self):
        with self.assertRaises(TypeError):
            core_module.Broker(["/bin/true"], None)

    def test_account_cycle_requires_a_session_registry(self):
        cycle = core_module.AccountCycle(
            config_env="PROBE_CONFIG_DIR",
            cycle_env="PROBE_ACCOUNT_CYCLE",
            limit_re=core_module.re.compile("limit"),
            resume_flag="--resume",
        )
        with self.assertRaises(ValueError):
            profile(session_dir=None, identity_env="PROBE_ID", account_cycle=cycle)

    def test_swap_login_resumes_the_registered_session_under_the_next_login(self):
        cycle = core_module.AccountCycle(
            config_env="PROBE_CONFIG_DIR",
            cycle_env="PROBE_ACCOUNT_CYCLE",
            limit_re=core_module.re.compile("limit"),
            resume_flag="--resume",
        )
        logins = ("/accounts/one", "/accounts/two", "/accounts/three")
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory)
            (session_dir / "123.json").write_text(
                json.dumps({"pid": 123, "sessionId": "abc"})
            )
            broker = core_module.Broker(
                [
                    "/bin/claude",
                    "--resume",
                    "old",
                    "--resume=older",
                    "--session-id=oldest",
                    "--continue",
                    "prompt",
                    "--model",
                    "opus",
                    "-r",
                ],
                profile(session_dir=session_dir, account_cycle=cycle),
            )
            broker.child_pid = 123
            broker.master_fd = 44
            broker.journal = mock.Mock()
            broker.terminate_child = mock.Mock()
            broker.spawn_child = mock.Mock()
            broker.resize_child = mock.Mock()
            with (
                mock.patch.dict(
                    core_module.os.environ,
                    {
                        "PROBE_CONFIG_DIR": logins[0],
                        "PROBE_ACCOUNT_CYCLE": ":".join(logins),
                    },
                ),
                mock.patch.object(
                    core_module.os, "waitpid", return_value=(123, 0)
                ) as waitpid,
                mock.patch.object(core_module.threading, "Thread") as thread,
            ):
                self.assertTrue(broker.swap_login("abc"))

            self.assertEqual(broker.environment_overrides["PROBE_CONFIG_DIR"], logins[1])
            self.assertEqual(
                broker.command,
                [
                    "/bin/claude",
                    "prompt",
                    "--model",
                    "opus",
                    "--resume",
                    "abc",
                ],
            )
            self.assertEqual(
                broker.terminate_child.call_args_list,
                [
                    mock.call(core_module.signal.SIGTERM),
                    mock.call(core_module.signal.SIGKILL),
                ],
            )
            waitpid.assert_called_once_with(123, core_module.os.WNOHANG)
            broker.spawn_child.assert_called_once_with()
            broker.resize_child.assert_called_once_with()
            thread.return_value.start.assert_called_once_with()

    def test_swap_login_clears_swapping_after_an_exception(self):
        cycle = core_module.AccountCycle(
            config_env="PROBE_CONFIG_DIR",
            cycle_env="PROBE_ACCOUNT_CYCLE",
            limit_re=core_module.re.compile("limit"),
            resume_flag="--resume",
        )
        broker = core_module.Broker(["/bin/claude"], profile(account_cycle=cycle))
        broker.child_pid = 123
        broker.master_fd = 44
        broker.journal = mock.Mock()
        broker.terminate_child = mock.Mock(side_effect=RuntimeError("boom"))
        with (
            mock.patch.dict(
                core_module.os.environ,
                {
                    "PROBE_CONFIG_DIR": "/accounts/one",
                    "PROBE_ACCOUNT_CYCLE": "/accounts/one:/accounts/two",
                },
            ),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            broker.swap_login("abc")

        self.assertFalse(broker.swapping)

    def test_swap_login_stops_after_every_other_login_was_tried(self):
        cycle = core_module.AccountCycle(
            config_env="PROBE_CONFIG_DIR",
            cycle_env="PROBE_ACCOUNT_CYCLE",
            limit_re=core_module.re.compile("limit"),
            resume_flag="--resume",
        )
        logins = ("/accounts/one", "/accounts/two", "/accounts/three")
        broker = core_module.Broker(["/bin/claude"], profile(account_cycle=cycle))
        broker.journal = mock.Mock()
        broker.terminate_child = mock.Mock()
        tried_at = core_module.time.monotonic()
        broker.tried_logins.update(
            {logins[1]: tried_at, logins[2]: tried_at}
        )
        with mock.patch.dict(
            core_module.os.environ,
            {
                "PROBE_CONFIG_DIR": logins[0],
                "PROBE_ACCOUNT_CYCLE": ":".join(logins),
            },
        ):
            self.assertFalse(broker.swap_login("abc"))

        broker.terminate_child.assert_not_called()

    def test_capture_output_records_activity(self):
        broker = core_module.Broker(["/bin/true"], profile())
        with mock.patch.object(core_module.time, "monotonic", return_value=42.0):
            broker.capture_output(b"output")
        self.assertEqual(broker.last_output_at, 42.0)

    def test_output_loop_continues_on_the_new_pty_after_swap_eio(self):
        broker = core_module.Broker(["/bin/true"], profile())
        broker.master_fd = 10
        broker.swapping = True
        reads = []

        def read(fd, _size):
            reads.append(fd)
            if fd == 10:
                with broker.output_condition:
                    broker.master_fd = 11
                    broker.swapping = False
                    broker.output_condition.notify_all()
                raise OSError(5, "EIO")
            return b""

        with (
            mock.patch.object(core_module.os, "read", side_effect=read),
            mock.patch.object(core_module.os, "close") as close,
        ):
            broker.output_loop()

        self.assertEqual(reads, [10, 11])
        close.assert_called_once_with(10)

    def test_limit_confirmation_requires_a_new_transcript_error(self):
        cycle = core_module.AccountCycle(
            config_env="PROBE_CONFIG_DIR",
            cycle_env="PROBE_ACCOUNT_CYCLE",
            limit_re=core_module.re.compile("limit"),
            resume_flag="--resume",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            login = root / "login"
            session_dir = root / "sessions"
            transcript = login / "projects" / "x" / "abc.jsonl"
            session_dir.mkdir()
            transcript.parent.mkdir(parents=True)
            (session_dir / "123.json").write_text(
                json.dumps({"pid": 123, "sessionId": "abc"})
            )
            broker = core_module.Broker(
                ["/bin/true"],
                profile(session_dir=session_dir, account_cycle=cycle),
            )
            broker.child_pid = 123
            broker.last_limit_at = core_module.datetime.datetime.fromisoformat(
                "2026-09-23T15:41:20+00:00"
            ).timestamp()
            record = {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": "2026-09-23T15:41:21.938Z",
                "message": {
                    "content": [{"type": "text", "text": "You've reached your limit."}]
                },
            }
            with mock.patch.dict(
                core_module.os.environ,
                {"PROBE_CONFIG_DIR": str(login)},
            ):
                transcript.write_text(json.dumps(record) + "\n")
                self.assertTrue(broker.limit_confirmed("abc"))

                record["timestamp"] = "2026-09-23T15:41:19.938Z"
                transcript.write_text(json.dumps(record) + "\n")
                self.assertFalse(broker.limit_confirmed("abc"))

    def test_announce_swap_injects_after_the_new_session_is_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory)
            broker = core_module.Broker(
                ["/bin/true"], profile(session_dir=session_dir)
            )
            broker.child_pid = 123
            broker.last_output_at = 10.0
            broker._write_text_injection = mock.Mock()
            broker.journal = mock.Mock()
            (session_dir / "123.json").write_text("{}")

            with mock.patch.object(core_module.time, "monotonic", return_value=12.0):
                broker.announce_swap("/accounts/one", "/accounts/two")

            broker._write_text_injection.assert_called_once()

    def test_deferred_text_injection_keeps_its_confirm(self):
        broker = injection_broker()
        broker.forward_user_data(b"draft")
        broker.write_master.reset_mock()
        broker.inject_request(
            4321, b"reply", core_module.time.monotonic() + 1, confirm=b"\r"
        )
        broker.write_master.assert_not_called()

        with mock.patch.object(core_module.time, "sleep"):
            broker.forward_user_data(b"\r")
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                b"\r",
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
                b"\r",
            ],
        )

    def test_acknowledged_control_returns_ansi_wrapped_terminal_text(self):
        broker = injection_broker()
        acknowledgment = b"Set effort level to xhigh"
        with mock.patch.object(
            broker,
            "_write_text_injection",
            side_effect=lambda *_: broker.capture_output(
                b"\x1b[32mSet effort level to xhigh\x1b[0m"
            ),
        ):
            result = broker.inject_request(
                4321,
                b"/effort xhigh",
                core_module.time.monotonic() + 1,
                acknowledgment=acknowledgment,
            )
        self.assertEqual(result, "Set effort level to xhigh")

    def test_missing_acknowledgment_names_the_expected_terminal_text(self):
        broker = injection_broker()
        with (
            mock.patch.object(core_module, "ACKNOWLEDGMENT_SECONDS", 0.001),
            mock.patch.object(broker, "_write_text_injection"),
            self.assertRaisesRegex(RuntimeError, "Set effort level to xhigh"),
        ):
            broker.inject_request(
                4321,
                b"/effort xhigh",
                core_module.time.monotonic() + 1,
                acknowledgment=b"Set effort level to xhigh",
            )
        self.assertIsNone(broker.output_capture)

    def test_ctrl_c_clears_input_and_delivers_deferred_text(self):
        broker = injection_broker()
        broker.forward_user_data(b"draft")
        broker.inject_request(4321, b"reply", core_module.time.monotonic() + 1)
        broker.write_master.reset_mock()

        with mock.patch.object(core_module.time, "sleep"):
            broker.forward_user_data(b"\x03")
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                b"\x03",
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
            ],
        )

    def test_arrow_sequence_does_not_defer_text_injection(self):
        broker = injection_broker()
        broker.forward_user_data(b"\x1b[A")
        broker.write_master.reset_mock()

        with mock.patch.object(core_module.time, "sleep"):
            broker.inject_request(4321, b"reply", core_module.time.monotonic() + 1)
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
            ],
        )

    def test_idle_timeout_flushes_deferred_text(self):
        payload = (
            core_module.BRACKETED_PASTE_START
            + b"reply"
            + core_module.BRACKETED_PASTE_END
        )
        for elapsed, expected_writes in (
            (core_module.IDLE_FLUSH_SECONDS - 1, []),
            (core_module.IDLE_FLUSH_SECONDS, [payload, b"\r"]),
        ):
            with self.subTest(elapsed=elapsed):
                broker = injection_broker()
                broker.pending_input_bytes = 1
                broker.last_input_at = 100
                broker.deferred_injections.append((b"reply", None))
                with (
                    mock.patch.object(core_module.time, "monotonic", return_value=100 + elapsed),
                    mock.patch.object(core_module.time, "sleep"),
                ):
                    broker._flush_deferred()
                self.assertEqual(
                    [call.args[0] for call in broker.write_master.call_args_list],
                    expected_writes,
                )

    def test_idle_compaction_writes_once_after_idle_registry_state(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), 5.0)
            write_session(broker, "idle", 994000)
            with (
                mock.patch.object(core_module.time, "monotonic", return_value=105.0),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
                mock.patch.object(core_module.time, "sleep"),
            ):
                broker._maybe_idle_compact()
            self.assertEqual(
                [call.args[0] for call in broker.write_master.call_args_list],
                [
                    core_module.BRACKETED_PASTE_START
                    + b"/compact"
                    + core_module.BRACKETED_PASTE_END,
                    b"\r",
                ],
            )
            self.assertFalse(broker.compact_armed)

            broker.write_master.reset_mock()
            with mock.patch.object(core_module.time, "monotonic", return_value=110.0):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()
            broker.journal.assert_called_once_with(
                "idle 5 s: injected /compact", to_terminal=False
            )

    def test_idle_compaction_busy_registry_state_blocks_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), 5.0)
            write_session(broker, "busy", 994000)
            with (
                mock.patch.object(core_module.time, "monotonic", return_value=105.0),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
            ):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()
            self.assertTrue(broker.compact_armed)

    def test_idle_compaction_checks_the_registry_once_per_second(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), 5.0)
            write_session(broker, "busy", 994000)
            with (
                mock.patch.object(core_module.time, "monotonic", return_value=105.0),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
            ):
                broker._maybe_idle_compact()

            write_session(broker, "idle", 994000)
            with (
                mock.patch.object(core_module.time, "monotonic", return_value=105.5),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
            ):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()

            with (
                mock.patch.object(core_module.time, "monotonic", return_value=106.0),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
                mock.patch.object(core_module.time, "sleep"),
            ):
                broker._maybe_idle_compact()
            broker.write_master.assert_called()

    def test_idle_compaction_fresh_idle_registry_state_blocks_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), 5.0)
            write_session(broker, "idle", 999000)
            with (
                mock.patch.object(core_module.time, "monotonic", return_value=105.0),
                mock.patch.object(core_module.time, "time", return_value=1000.0),
            ):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()
            self.assertTrue(broker.compact_armed)

    def test_idle_compaction_keeps_an_armed_draft_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), 5.0)
            write_session(broker, "idle", 994000)
            broker.pending_input_bytes = 1
            with mock.patch.object(core_module.time, "monotonic", return_value=105.0):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()
            self.assertTrue(broker.compact_armed)

    def test_idle_compaction_disabled_by_profile_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = idle_compact_broker(Path(directory), None)
            write_session(broker, "idle", 0)
            with mock.patch.object(core_module.time, "monotonic", return_value=1000.0):
                broker._maybe_idle_compact()
            broker.write_master.assert_not_called()
            self.assertTrue(broker.compact_armed)

    def test_terminal_replies_do_not_reset_idle_and_flush_clears_pending(self):
        broker = injection_broker()
        with mock.patch.object(core_module.time, "monotonic", return_value=100):
            broker.forward_user_data(b"draft")
        broker.deferred_injections.append((b"reply", None))
        with mock.patch.object(core_module.time, "monotonic", return_value=140):
            broker.forward_user_data(b"\x1b[24;80R\x1b[I")  # cursor report, focus in
        broker.write_master.reset_mock()
        with (
            mock.patch.object(core_module.time, "monotonic", return_value=100 + core_module.IDLE_FLUSH_SECONDS),
            mock.patch.object(core_module.time, "sleep"),
        ):
            broker._flush_deferred()
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [core_module.BRACKETED_PASTE_START + b"reply" + core_module.BRACKETED_PASTE_END, b"\r"],
        )
        self.assertEqual(broker.pending_input_bytes, 0)

    def test_bare_escape_clears_pending_input(self):
        broker = injection_broker()
        broker.forward_user_data(b"draft")
        broker.forward_user_data(b"\x1b")
        self.assertEqual(broker.pending_input_bytes, 0)
        broker.write_master.reset_mock()

        with mock.patch.object(core_module.time, "sleep"):
            broker.inject_request(4321, b"reply", core_module.time.monotonic() + 1)
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
            ],
        )

    def test_ctrl_c_ends_an_unterminated_paste_and_delivers_deferred_text(self):
        broker = injection_broker()
        broker.forward_user_data(b"\x1b[200~line1\rline2")
        broker.inject_request(4321, b"reply", core_module.time.monotonic() + 1)
        broker.write_master.reset_mock()
        with mock.patch.object(core_module.time, "sleep"):
            broker.forward_user_data(b"\x03")
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                b"\x03",
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
            ],
        )

    def test_multiline_paste_keeps_text_deferred_until_submission(self):
        broker = injection_broker()
        broker.forward_user_data(b"\x1b[200~line1\rline2")
        broker.write_master.reset_mock()
        broker.inject_request(4321, b"reply", core_module.time.monotonic() + 1)
        broker.write_master.assert_not_called()
        broker._flush_deferred()
        broker.write_master.assert_not_called()

        broker.forward_user_data(b"\x1b[201~")
        broker.write_master.reset_mock()
        with mock.patch.object(core_module.time, "sleep"):
            broker.forward_user_data(b"\r")
        self.assertEqual(
            [call.args[0] for call in broker.write_master.call_args_list],
            [
                b"\r",
                core_module.BRACKETED_PASTE_START
                + b"reply"
                + core_module.BRACKETED_PASTE_END,
                b"\r",
            ],
        )

    def test_a_declared_registry_reports_only_a_matching_entry(self):
        for entry, expected in (
            (None, LookupError),
            ({"pid": 9999, "status": "busy"}, LookupError),
            ({"pid": 4321, "status": "busy"}, "busy"),
        ):
            with (
                self.subTest(entry=entry),
                tempfile.TemporaryDirectory() as directory,
            ):
                if entry is not None:
                    (Path(directory) / "4321.json").write_text(json.dumps(entry))
                target = bare_broker(profile=profile(session_dir=Path(directory)))
                result = target.target_status
                if isinstance(expected, type):
                    with self.assertRaises(expected):
                        result(4321)
                else:
                    self.assertEqual(result(4321), expected)


    def test_foreign_controls_define_their_own_keystrokes_and_framing(self):
        cases = (
            ({"command": "status"}, None, False),
            ({"command": "compact"}, b"/summarize", False),
            ({"command": "model", "argument": "fast"}, b"/use-model fast", False),
            ({"command": "stop"}, b"\x03", True),
        )
        for request, expected, raw in cases:
            with self.subTest(request=request):
                command = request["command"]
                self.assertEqual(
                    core_module.normalize_control(request, FOREIGN_CONTROLS), expected
                )
                self.assertEqual(FOREIGN_CONTROLS[command].raw, raw)

    def test_claude_semantics_are_not_hard_coded_in_the_core(self):
        rejected = (
            {"command": "effort", "argument": "xhigh"},  # a command it never declared
            {"command": "model", "argument": "opus"},    # declared, argument outside its set
            {"command": "rename"},                       # a shape another control would accept
        )
        for request in rejected:
            with self.subTest(request=request), self.assertRaises(ValueError):
                core_module.normalize_control(request, FOREIGN_CONTROLS)

    def test_the_command_must_match_its_optional_pin(self):
        cases = (
            (["/bin/echo", "--flag", "value"], "/bin/echo", None),
            (["/bin/cat"], "/bin/echo", RuntimeError),
            (["/bin/cat", "-v"], None, None),
        )
        for command, required, expected in cases:
            with self.subTest(command=command, required=required):
                broker = bare_broker(command=command,
                                     profile=profile(required_command=required))
                if expected is not None:
                    with self.assertRaises(expected):
                        broker.validate_command()
                else:
                    broker.validate_command()
                    self.assertEqual(broker.command, command)

    def test_an_unresolved_pinned_command_is_refused_before_launch(self):
        broker = bare_broker(command=["probe"], profile=profile(required_command="/bin/echo"))
        with mock.patch.object(core_module.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError):
                broker.validate_command()

    def test_only_a_plain_single_argument_field_is_allowed(self):
        rejected = (
            "/model {argument} {argument}",
            "/model {argument!r}",
            "/model {argument:>8}",
            "/model {other}",
            "/model {argument} {other}",
        )
        for template in rejected:
            with self.subTest(template), self.assertRaises(ValueError):
                core_module.Control(keystrokes=template, argument=("opus",))

    def test_an_escaped_brace_does_not_count_as_interpolation(self):
        with self.assertRaises(ValueError):  # declares an argument it never uses
            core_module.Control(keystrokes="/model {{argument}}", argument=("opus",))
        literal = core_module.Control(keystrokes="/model {{argument}}")
        self.assertEqual(literal.render(None), b"/model {argument}")

    def test_resolve_control_returns_the_spec_that_framed_the_bytes(self):
        spec, encoded = core_module.resolve_control(
            {"command": "stop"}, FOREIGN_CONTROLS)
        self.assertIs(spec, FOREIGN_CONTROLS["stop"])
        self.assertTrue(spec.raw)
        self.assertEqual(encoded, b"\x03")

    def test_a_validated_profile_cannot_gain_a_control_afterwards(self):
        with self.assertRaises(TypeError):
            profile().controls["shell"] = core_module.Control(keystrokes="/shell")


if __name__ == "__main__":
    unittest.main()
