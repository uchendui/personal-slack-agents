#!/usr/bin/env python3

import importlib.util
import json
import sys
import tempfile
import time
import types
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
core_spec = importlib.util.spec_from_file_location("pty_broker", TOOLS / "pty_broker.py")
core = importlib.util.module_from_spec(core_spec)
sys.modules["pty_broker"] = core
core_spec.loader.exec_module(core)
spec = importlib.util.spec_from_file_location(
    "slack_live_delivery_tested", TOOLS / "slack_live_delivery.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules["slack_live_delivery_tested"] = module
spec.loader.exec_module(module)


async def control_reply(reply):
    client = mock.MagicMock()
    client.__enter__.return_value = client
    client.recv.return_value = reply
    delivery = module.Delivery(socket_factory=mock.Mock(return_value=client))
    parent = module.ParentRef("session", "agent", "claude", 42, "7", Path("42.json"))
    with (
        mock.patch.object(delivery, "verify_parent", mock.AsyncMock(return_value=True)),
        mock.patch.object(delivery, "_wire", return_value=("/socket", "token", 9)),
    ):
        return await delivery.control(parent, "effort", "xhigh"), client


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_identity_survives_rename_but_not_replacement(self):
        delivery = module.Delivery()
        ref = module.ParentRef("session", "old-name", "claude", 42, "7", Path("/tmp/42.json"))
        entry = {"sessionId": "session", "name": "new-name", "pid": 42, "procStart": "7"}
        with mock.patch.object(delivery, "_entry", return_value=entry):
            self.assertEqual(delivery._read_ref(ref), entry)
        with (
            mock.patch.object(delivery, "_entry", return_value=dict(entry, pid=43)),
            self.assertRaises(LookupError),
        ):
            delivery._read_ref(ref)

    async def test_find_session_skips_a_stale_entry_sharing_the_session_id(self):
        delivery = module.Delivery()
        live_entry = {
            "sessionId": "session", "name": "live", "pid": 42, "procStart": "7",
        }
        stale_entry = {
            "sessionId": "session", "name": "stale", "pid": 43, "procStart": "9",
        }
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "sessions"
            state_dir.mkdir()
            live_path = state_dir / "42.json"
            stale_path = state_dir / "43.json"
            live_path.write_text(json.dumps(live_entry))
            stale_path.write_text(json.dumps(stale_entry))

            def process_stat(pid):
                if pid == 42:
                    return (0, "7")
                raise LookupError(f"process {pid} is not live")

            with mock.patch.object(module.pty_broker, "process_stat", side_effect=process_stat):
                path, entry = delivery._find_session("session", Path(directory))
        self.assertEqual((path, entry), (live_path, live_entry))

    async def test_find_session_distinguishes_missing_from_duplicate_live_entries(self):
        delivery = module.Delivery()
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "sessions"
            state_dir.mkdir()
            with self.assertRaises(module.SessionNotFound):
                delivery._find_session("session", Path(directory))

            for pid in (42, 43):
                (state_dir / f"{pid}.json").write_text(json.dumps({
                    "sessionId": "session", "name": "agent", "pid": pid,
                    "procStart": str(pid),
                }))
            with mock.patch.object(
                module.pty_broker, "process_stat",
                side_effect=lambda pid: (0, str(pid)),
            ):
                with self.assertRaises(LookupError) as raised:
                    delivery._find_session("session", Path(directory))
        self.assertNotIsInstance(raised.exception, module.SessionNotFound)

    async def test_discover_parent_reads_the_agent_config_dir_sessions(self):
        entry = {
            "sessionId": "session", "name": "agent", "pid": 42,
            "procStart": "7", "kind": "interactive",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for profile_dir, sessions in (
                (root / "profile", root / "profile" / "sessions"),
                (None, root / ".claude" / "sessions"),
            ):
                with self.subTest(profile_dir=profile_dir):
                    sessions.mkdir(parents=True)
                    (sessions / "42.json").write_text(json.dumps(entry))
                    config = SimpleNamespace(
                        kind="claude", session_id="session", name="agent", profile_dir=profile_dir,
                    )
                    delivery = module.Delivery()
                    with (
                        mock.patch.dict(os.environ, {"HOME": directory}),
                        mock.patch.object(module.pty_broker, "process_stat", return_value=(0, "7")),
                        mock.patch.object(delivery, "verify_parent", mock.AsyncMock(return_value=True)),
                    ):
                        parent = await delivery.discover_parent(config)
                    self.assertEqual(parent.entry_path, sessions / "42.json")

    async def test_discover_parent_names_a_live_session_with_the_wrong_agent_name(self):
        delivery = module.Delivery()
        entry = {
            "sessionId": "session", "name": "auto-name", "pid": 42,
            "procStart": "7", "kind": "interactive",
        }
        config = SimpleNamespace(kind="claude", session_id="session", name="agent", profile_dir=None)
        with mock.patch.object(delivery, "_find_session", return_value=(Path("42.json"), entry)):
            with self.assertRaisesRegex(
                LookupError,
                "session session is live as 'auto-name', not 'agent'; run /rename agent in it",
            ):
                await delivery.discover_parent(config)

    async def test_broker_request_uses_existing_authenticated_protocol(self):
        _, client = await control_reply(b"OK\n")
        self.assertEqual(
            client.sendall.call_args.args[0],
            b'{"token":"token","target_pid":42,"control":{"command":"effort","argument":"xhigh"}}',
        )

    async def test_control_returns_the_broker_acknowledgment(self):
        result, _ = await control_reply(b"OK Set effort level to xhigh\n")
        self.assertEqual(result, "Set effort level to xhigh")

    async def test_control_raises_the_broker_error_text(self):
        with self.assertRaisesRegex(RuntimeError, "Set effort level to xhigh"):
            await control_reply(b"ERR session printed no 'Set effort level to xhigh' within 5 s\n")

    async def test_pty_discovery_rejects_malformed_and_duplicate_candidates(self):
        def advertisement(broker_pid):
            return {
                "broker_pid": broker_pid,
                "socket": f"/tmp/{broker_pid}.sock",
                "proc_start": f"broker-{broker_pid}",
                "child_pid": broker_pid + 1,
                "child_proc_start": f"child-{broker_pid}",
                "conversation_id": "658e78b0-9cff-48ec-b427-b4885bdfcd50",
            }

        runtime = SimpleNamespace(session_discovery_mode="pty_advertisement")
        config = SimpleNamespace(
            kind="antigravity",
            session_id="658e78b0-9cff-48ec-b427-b4885bdfcd50",
            name="agent",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "100.json").write_text(
                json.dumps({"broker_pid": 100, "conversation_id": config.session_id}),
            )
            with (
                mock.patch.object(module, "STATE_DIR", state_dir),
                mock.patch.dict(module.RUNTIMES, {"antigravity": runtime}),
                self.assertRaises(LookupError),
            ):
                await module.Delivery().discover_parent(config)

            (state_dir / "100.json").write_text(json.dumps(advertisement(100)))
            (state_dir / "101.json").write_text(json.dumps(advertisement(101)))
            with (
                mock.patch.object(module, "STATE_DIR", state_dir),
                mock.patch.dict(module.RUNTIMES, {"antigravity": runtime}),
                self.assertRaises(LookupError),
            ):
                await module.Delivery().discover_parent(config)

    async def test_pty_discovery_distinguishes_missing_from_duplicate_live_advertisements(self):
        def advertisement(broker_pid):
            return {
                "broker_pid": broker_pid,
                "socket": f"/tmp/{broker_pid}.sock",
                "proc_start": f"broker-{broker_pid}",
                "child_pid": broker_pid + 1,
                "child_proc_start": f"child-{broker_pid}",
                "conversation_id": "658e78b0-9cff-48ec-b427-b4885bdfcd50",
            }

        runtime = SimpleNamespace(session_discovery_mode="pty_advertisement")
        config = SimpleNamespace(
            kind="antigravity",
            session_id="658e78b0-9cff-48ec-b427-b4885bdfcd50",
            name="agent",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with (
                mock.patch.object(module, "STATE_DIR", state_dir),
                mock.patch.dict(module.RUNTIMES, {"antigravity": runtime}),
            ):
                with self.assertRaises(module.SessionNotFound):
                    await module.Delivery().discover_parent(config)

            for broker_pid in (100, 101):
                (state_dir / f"{broker_pid}.json").write_text(
                    json.dumps(advertisement(broker_pid))
                )
            with (
                mock.patch.object(module, "STATE_DIR", state_dir),
                mock.patch.dict(module.RUNTIMES, {"antigravity": runtime}),
            ):
                with self.assertRaises(LookupError) as raised:
                    await module.Delivery().discover_parent(config)
        self.assertNotIsInstance(raised.exception, module.SessionNotFound)

    async def test_pty_verification_rejects_stale_child_or_broker_identity(self):
        runtime = SimpleNamespace(session_discovery_mode="pty_advertisement")
        for field in ("child_proc_start", "proc_start"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "999.json"
                advertised = {
                    "broker_pid": 999,
                    "socket": "/socket",
                    "proc_start": "live-broker" if field != "proc_start" else "stale-broker",
                    "child_pid": 123,
                    "child_proc_start": "child-start" if field != "child_proc_start" else "stale-child",
                    "conversation_id": "session",
                }
                path.write_text(json.dumps(advertised))
                ref = module.ParentRef("session", "agent", "antigravity", 123, "child-start", path)
                delivery = module.Delivery()
                with (
                    mock.patch.dict(module.RUNTIMES, {"antigravity": runtime}),
                    mock.patch.object(delivery, "_wire", return_value=("/socket", "token", 999)),
                    mock.patch.object(
                        module.pty_broker,
                        "process_stat",
                        side_effect=[(123, "child-start"), (999, "live-broker")],
                    ),
                ):
                    self.assertFalse(await delivery.verify_parent(ref))


class ReviveParentTest(unittest.IsolatedAsyncioTestCase):
    def _config(self):
        return SimpleNamespace(
            profile_dir=Path("/profile"), session_id="sess-1", launcher="/usr/bin/claude", launcher_path="/usr/bin",
            runtime_args="--model sonnet", name="agent", workdir=Path("/work"),
        )

    async def test_falls_back_to_new_session_when_no_tmux_server_and_then_polls_discovery(self):
        window_fail = mock.Mock()
        window_fail.communicate.return_value = (b"", b"no server running on default")
        window_fail.returncode = 1
        session_ok = mock.Mock()
        session_ok.communicate.return_value = (b"", b"")
        session_ok.returncode = 0
        popen = mock.Mock(side_effect=[window_fail, session_ok])
        delivery = module.Delivery(popen=popen)
        parent = module.ParentRef("sess-1", "agent", "claude", 42, "7", Path("42.json"))
        with (
            mock.patch.object(delivery, "discover_parent", mock.AsyncMock(side_effect=[module.SessionNotFound(), parent])),
            mock.patch.object(module, "REVIVE_POLL_SECONDS", 0),
        ):
            result = await delivery.revive_parent(self._config())
        self.assertIs(result, parent)
        self.assertEqual(popen.call_args_list[0].args[0][:2], ["tmux", "new-window"])
        self.assertEqual(popen.call_args_list[1].args[0][:2], ["tmux", "new-session"])
        launched = popen.call_args_list[1].args[0]
        self.assertEqual(
            launched[-1],
            f"env CLAUDE_CONFIG_DIR=/profile PATH=/usr/bin {Path.home()}/.local/bin/claude-pty-broker -- "
            "/usr/bin/claude --resume sess-1 --model sonnet",
        )

    async def test_missing_stored_launcher_fails_before_tmux(self):
        config = self._config()
        config.launcher = None
        popen = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "rerun slack-spawn agent"):
            await module.Delivery(popen=popen).revive_parent(config)
        popen.assert_not_called()

    async def test_stored_launcher_resumes_behind_the_broker(self):
        config = self._config()
        config.launcher = "/opt/bin/ccr cc-gemini cli --"
        config.launcher_path = "/opt/node/bin:/usr/bin"
        config.runtime_args = "--model 'Gemini API,gemini-3.8-flash' --dangerously-skip-permissions"
        delivery = module.Delivery()
        parent = module.ParentRef("sess-1", "agent", "claude", 42, "7", Path("42.json"))
        with (
            mock.patch.object(delivery, "_run_tmux", return_value=(0, b"")) as run_tmux,
            mock.patch.object(delivery, "discover_parent", mock.AsyncMock(return_value=parent)),
        ):
            await delivery.revive_parent(config)
        self.assertEqual(module.shlex.split(run_tmux.call_args.args[0][-1]), [
            "env", "CLAUDE_CONFIG_DIR=/profile", "PATH=/opt/node/bin:/usr/bin",
            f"{Path.home()}/.local/bin/claude-pty-broker", "--",
            "/opt/bin/ccr", "cc-gemini", "cli", "--", "--resume", "sess-1",
            "--model", "Gemini API,gemini-3.8-flash", "--dangerously-skip-permissions",
        ])

    async def test_stops_at_once_when_revived_session_has_a_name_mismatch(self):
        delivery = module.Delivery()
        mismatch = LookupError("session sess-1 is live as 'auto-name', not 'agent'")
        with (
            mock.patch.object(delivery, "_run_tmux", return_value=(0, b"")),
            mock.patch.object(
                delivery, "discover_parent", mock.AsyncMock(side_effect=mismatch)
            ),
            mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()) as sleep,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "revived session is not addressable: session sess-1 is live as 'auto-name', not 'agent'",
            ):
                await delivery.revive_parent(self._config())
        sleep.assert_not_awaited()

    async def test_new_window_failure_other_than_no_server_raises_without_a_second_attempt(self):
        failure = mock.Mock()
        failure.communicate.return_value = (b"", b"tmux: command not found")
        failure.returncode = 127
        popen = mock.Mock(return_value=failure)
        delivery = module.Delivery(popen=popen)
        with self.assertRaisesRegex(RuntimeError, "tmux: command not found"):
            await delivery.revive_parent(self._config())
        popen.assert_called_once()


class ForkWorkerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fork = types.ModuleType("slack_fork")
        self.fork.MARKER = ".slack-fork-source"
        self.fork.delete_fork = mock.Mock()
        patcher = mock.patch.dict(sys.modules, {"slack_fork": self.fork})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _spawn(self, runtime, root, token_path):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        popen = mock.Mock(return_value=process)
        delivery = module.Delivery(popen=popen)
        parent = module.ParentRef("parent-id", "agent", runtime, 42, "7", Path("42.json"))
        table = dict(module.FORK_RUNTIMES)
        table[runtime] = dict(table[runtime], root=root)
        with (
            mock.patch.object(module, "FORK_RUNTIMES", table),
            mock.patch.object(module, "GEMINI_TOKEN_PATH", token_path),
            mock.patch.object(module, "PROC_ROOT", root / "proc"),
            mock.patch.object(module.threading, "Thread"),
        ):
            worker = await delivery.spawn_fork_worker(runtime, parent, "do the work")
        # A fresh fork learns its conversation id from the broker
        # advertisement; the tests below stand in for that discovery.
        worker.fork_id = "fork-id"
        return worker, popen

    async def test_spawn_builds_the_broker_argv_and_router_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret\n")
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                worker, popen = await self._spawn("antigravity", root, token_path)
            self.assertEqual(popen.call_args.args[0], [
                str(Path.home() / ".local" / "bin" / "antigravity-pty-broker"), "--",
                str(Path.home() / ".local" / "bin" / "agy"),
                "--dangerously-skip-permissions", "-i", "do the work",
            ])
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["GOOGLE_GEMINI_BASE_URL"], "http://127.0.0.1:3460")
            self.assertEqual(environment["GEMINI_API_KEY"], "secret")
            self.assertTrue(environment["PATH"].startswith(str(Path.home() / ".local" / "bin") + ":"))
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(worker.pid, 4321)

            # The live session's --effort is carried into the fork's argv.
            cmdline = root / "proc" / "42" / "cmdline"
            cmdline.parent.mkdir(parents=True)
            cmdline.write_bytes(b"python3\0antigravity-pty-broker\0--\0agy\0--effort\0high\0")
            _, popen = await self._spawn("antigravity", root, token_path)
            self.assertEqual(popen.call_args.args[0][3:5], ["--effort", "high"])

            token_path.unlink()
            with self.assertRaises(OSError):
                await self._spawn("antigravity", root, token_path)

    async def test_wait_done_releases_only_on_a_final_response_without_tool_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            worker._broker_ref = mock.AsyncMock(return_value="ref")
            # Fixed stamps: this machine's wall clock steps backwards, which
            # would put a record written now before a `since` taken earlier.
            worker.since = "2026-01-01T00:00:00Z"
            fresh = "2026-01-01T00:00:01Z"
            transcript = worker.transcript
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                # A finished turn copied from the parent (old timestamp) and a
                # fresh step that still carries tool calls: neither ends the turn.
                json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [], "created_at": "2000-01-01T00:00:00Z"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "run"}], "created_at": fresh}) + "\n"
            )
            with self.assertRaises(TimeoutError):
                await worker.wait_done(0.3)
            with open(transcript, "a") as handle:
                handle.write(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [], "content": "done", "created_at": fresh}) + "\n")
            await worker.wait_done(5.0)

    async def test_wait_done_ignores_a_thinking_only_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            worker._broker_ref = mock.AsyncMock(return_value="ref")
            worker.since = fresh = "2026-01-01T00:00:00Z"
            transcript = worker.transcript
            transcript.parent.mkdir(parents=True)
            # The CLI writes a thinking-only step as DONE without tool calls
            # and then continues the turn itself; it must not end the turn.
            transcript.write_text(json.dumps({
                "type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [],
                "thinking": "**Considering the command**", "created_at": fresh,
            }) + "\n")
            with self.assertRaises(TimeoutError):
                await worker.wait_done(0.3)
            with open(transcript, "a") as handle:
                handle.write(json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [], "content": "NO_OP", "created_at": fresh}) + "\n")
            await worker.wait_done(5.0)

    async def test_wait_done_fails_fast_when_the_prompt_never_reached_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            worker.transcript.parent.mkdir(parents=True)
            worker.prompt = "first ask"
            worker._broker_ref = mock.AsyncMock(return_value="ref")
            worker.delivery.inject = mock.AsyncMock()
            with mock.patch.object(module, "FORK_START_TIMEOUT", 0.0):
                clock = time.monotonic()
                with self.assertRaises(TimeoutError):
                    await worker.wait_done(10.0)
                self.assertLess(time.monotonic() - clock, 2.0)
                # The dropped prompt is sent once more before giving up.
                worker.delivery.inject.assert_awaited_once_with("ref", "first ask")
                # The record agy appends when it accepts a prompt: the turn is
                # running, so wait_done keeps waiting for its end instead.
                # The resend stamped `since` from the wall clock, which steps
                # backwards here, so both stamps are fixed.
                worker.since = "2026-01-01T00:00:00Z"
                worker.transcript.write_text(json.dumps({
                    "step_index": 8, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                    "status": "DONE", "created_at": "2026-01-01T00:00:01Z",
                    "content": "<USER_REQUEST>\nReply with the single word pong.\n</USER_REQUEST>",
                }) + "\n")
                clock = time.monotonic()
                with self.assertRaises(TimeoutError):
                    await worker.wait_done(1.0)
                self.assertGreaterEqual(time.monotonic() - clock, 1.0)

    async def test_terminate_kills_the_group_and_deletes_the_fork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            worker.transcript.parent.mkdir(parents=True)
            worker.process.poll.return_value = 0
            with (
                mock.patch.object(module.os, "getpgid", return_value=4321),
                mock.patch.object(module.os, "killpg") as killpg,
                mock.patch.object(module.os, "close"),
            ):
                await worker.terminate()
            killpg.assert_called_once_with(4321, module.signal.SIGTERM)
            self.fork.delete_fork.assert_called_once_with(root, "fork-id")
            self.assertTrue((root / "brain" / "fork-id" / ".slack-fork-source").exists())
            await worker.terminate()
            self.fork.delete_fork.assert_called_once_with(root, "fork-id")

    async def test_lines_follow_the_cli_to_the_conversation_with_a_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            # The advertised id ("fork-id") has no transcript; the CLI wrote
            # its turn under a sibling id it holds open.
            real = root / "brain" / "real-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
            real.parent.mkdir(parents=True)
            real.write_text(json.dumps({"type": "USER_INPUT", "created_at": module._utc_now_iso()}) + "\n")
            with mock.patch.object(module, "open_conversation_ids", return_value={"fork-id", "real-id"}):
                self.assertEqual(len(worker._lines()), 1)
            self.assertEqual(worker.fork_id, "real-id")

    async def test_lines_never_adopt_a_conversation_that_predates_the_fork(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            # The live session's transcript exists before the fork starts and
            # keeps getting newer writes; the fork's own sibling id appears later.
            live = root / "brain" / "live-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
            live.parent.mkdir(parents=True)
            live.write_text("")
            worker, _ = await self._spawn("antigravity", root, token_path)
            real = root / "brain" / "real-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
            real.parent.mkdir(parents=True)
            real.write_text(json.dumps({"type": "USER_INPUT", "created_at": module._utc_now_iso()}) + "\n")
            live.write_text("old\n")
            os.utime(live, (time.time() + 100, time.time() + 100))
            with mock.patch.object(module, "open_conversation_ids", return_value={"fork-id", "real-id", "live-id"}):
                self.assertEqual(len(worker._lines()), 1)
            self.assertEqual(worker.fork_id, "real-id")

    async def test_terminate_deletes_the_fork_when_the_process_already_died(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "token"
            token_path.write_text("secret")
            worker, _ = await self._spawn("antigravity", root, token_path)
            worker.transcript.parent.mkdir(parents=True)
            with mock.patch.object(
                module.os, "getpgid", side_effect=ProcessLookupError
            ):
                await worker.terminate()
            self.fork.delete_fork.assert_called_once_with(root, "fork-id")
            await worker.terminate()
            self.fork.delete_fork.assert_called_once_with(root, "fork-id")


if __name__ == "__main__":
    unittest.main()


class OpenConversationIdsTest(unittest.TestCase):
    def test_collects_ids_across_the_process_tree_and_refuses_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory) / "proc"
            root = Path(directory) / "antigravity-cli"
            fd = proc / "77" / "fd"
            fd.mkdir(parents=True)
            cid = "11111111-2222-3333-4444-555555555555"
            # The database may be held by a child process.
            (fd / "5").symlink_to(root / "presence" / f"{cid}.lock")
            (proc / "77" / "task" / "77").mkdir(parents=True)
            (proc / "77" / "task" / "77" / "children").write_text("78 \n")
            child_fd = proc / "78" / "fd"
            child_fd.mkdir(parents=True)
            (child_fd / "3").symlink_to(root / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl")
            (child_fd / "4").symlink_to(root / "conversations" / f"{cid}.db-wal")
            self.assertEqual(module.open_conversation_ids(77, root, proc), {cid})
            other = "66666666-2222-3333-4444-555555555555"
            (fd / "6").symlink_to(root / "conversations" / f"{other}.db")
            self.assertEqual(module.open_conversation_ids(77, root, proc), {cid, other})
            with self.assertRaises(RuntimeError):
                module.open_conversation_ids(79, root, proc)

