#!/usr/bin/env python3

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "slack_bridge.py"
slack_api = types.ModuleType("slack_api")
slack_api.SlackAPI = object
delivery_module = types.ModuleType("slack_live_delivery")
delivery_module.Delivery = object
delivery_module.ParentRef = object
delivery_module.SessionNotFound = type("SessionNotFound", (LookupError,), {})
register_module = types.ModuleType("slack_register")
register_module.AgentConfig = object
register_module.operator_user_ids = lambda: set()
register_module.SlackAPIError = type("SlackAPIError", (Exception,), {})
register_module._user_api = mock.Mock(return_value={"ok": True})
spec = importlib.util.spec_from_file_location("slack_bridge_tested", MODULE_PATH)
bridge_module = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {
    "slack_bridge_tested": bridge_module,
    "slack_api": slack_api,
    "slack_live_delivery": delivery_module,
    "slack_register": register_module,
}):
    spec.loader.exec_module(bridge_module)
real_register_spec = importlib.util.spec_from_file_location("slack_register_real", TOOLS / "slack_register.py")
real_register = importlib.util.module_from_spec(real_register_spec)
sys.modules[real_register_spec.name] = real_register
real_register_spec.loader.exec_module(real_register)


def config(**changes):
    values = dict(
        bot_user_id="B-test",
        operator_user_id="U-operator",
        deliver_channel_messages=True,
        kind="claude",
        session_id=None,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def api():
    value = mock.Mock()
    value.config = config()
    value.thread_messages = mock.AsyncMock(return_value=[])
    value.replies_from = mock.AsyncMock(return_value=["101"])
    value.thread_role = mock.AsyncMock(return_value="root")
    value.post_operational = mock.AsyncMock()
    value.add_reaction = mock.AsyncMock()
    value.auth_test = mock.AsyncMock(return_value={"ok": True})
    value.bot_channels = mock.AsyncMock(return_value=[])
    value.history = mock.AsyncMock(return_value=[])
    value.since = mock.AsyncMock(return_value=[])
    value.run = mock.AsyncMock()
    value.validate_current_files = mock.Mock()
    value.download_current_files = mock.AsyncMock(return_value=[])
    value.label = mock.Mock(side_effect=lambda sender: sender)
    value.close = mock.AsyncMock()
    return value


def delivery(runtime=None):
    parent = SimpleNamespace(session_id="parent-session", name="agent")
    if runtime is not None:
        parent.runtime = runtime
    value = mock.Mock()
    value.discover_parent = mock.AsyncMock(return_value=parent)
    value.verify_parent = mock.AsyncMock(return_value=True)
    value.inject = mock.AsyncMock()
    value.control = mock.AsyncMock()
    value.worker = SimpleNamespace(
        fork_id="fork-1",
        advertised_id="fork-1",
        pid=4242,
        since="2026-09-09T00:00:00Z",
        transcript=Path("/nonexistent/fork-1.jsonl"),
        wait_done=mock.AsyncMock(),
        last_content=mock.Mock(return_value="Here is the capacity: 16 nodes."),
        terminate=mock.AsyncMock(),
        inject=mock.AsyncMock(),
        interrupt=mock.AsyncMock(),
    )
    value.spawn_fork_worker = mock.AsyncMock(return_value=value.worker)
    return value, parent


def lifecycle():
    value = mock.Mock()
    value.rename_agent = mock.AsyncMock(return_value=False)
    value.unregister_agent = mock.AsyncMock()
    value.load_agent_configs = mock.Mock()
    return value


def event(event_id, text="work", **changes):
    message = dict(
        type="message", channel="C-test", ts="100", user="U-operator", text=text
    )
    message.update(changes)
    return {"event_id": event_id, "team_id": "T-test", "event": message}


def bridge(settings=None, runtime=None, batched=False):
    settings = settings or config()
    slack = api()
    slack.config = settings
    transport, parent = delivery(runtime)
    value = bridge_module.Bridge(
        {"agent": settings}, {"agent": slack}, transport, lifecycle(),
        config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
    )
    if not batched:
        async def inject_immediately(_agent, target, prompt):
            await transport.inject(target, prompt)
        value._inject_batched = inject_immediately
    return value, slack, transport, parent


class BridgeRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_routes_addressed_threads_only(self):
        cases = (
            (event("dm", channel_type="im"), None, False, True, True, "100"),
            (event("mention", "<@B-test> work"), None, False, True, True, "100"),
            (event("channel-broadcast", "<!channel> all hands"), None, False, True, True, "100"),
            (event("here-broadcast", "<!here> quick one"), None, False, True, True, "100"),
            (event("background"), None, False, True, False, None),
            (event("other-bot", thread_ts="90", user="B-other", bot_id="B-other"), "none", False, False, False, None),
            (event("peer-in-answered-thread", thread_ts="90", user="B-other", bot_id="B-other", bot_profile={"app_id": "A-other"}), "member", False, True, True, "90"),
            (event("operator-elsewhere", thread_ts="90", user="U-operator"), "none", False, False, False, None),
            (event("operator-answered", thread_ts="90", user="U-operator"), "member", False, True, True, "90"),
            (event("operator-answered-live", thread_ts="90", user="U-operator"), "none", True, True, True, "90"),
            (event("thread-mention", "<@B-test> work", thread_ts="90"), "none", False, True, True, "90"),
            (event("own-root", thread_ts="90", bot_id="B-other"), "root", False, True, True, "90"),
        )
        for payload, role, previously_posted, delivered, replies, root in cases:
            with self.subTest(payload["event_id"]):
                value, slack, transport, parent = bridge()
                slack.thread_role.return_value = role
                if previously_posted:
                    await value.handle("agent", event(
                        "previous-post", user="B-test", ts="99", thread_ts="90"
                    ))
                await value.handle("agent", payload)
                if not delivered:
                    transport.inject.assert_not_awaited()
                    slack.add_reaction.assert_not_awaited()
                    continue
                transport.inject.assert_awaited_once()
                self.assertIs(transport.inject.await_args.args[0], parent)
                injected = transport.inject.await_args.args[1]
                if replies:
                    self.assertTrue(injected.startswith(
                        f"[Slack] as agent (<@B-test>) channel C-test thread {root}\n"
                    ))
                else:
                    self.assertTrue(injected.startswith("[Slack background context"))
                slack.add_reaction.assert_awaited_once_with("C-test", "100")

        value, slack, transport, _ = bridge(config(deliver_channel_messages=False))
        await value.handle("agent", event("opted-out"))
        transport.inject.assert_not_awaited()
        slack.add_reaction.assert_not_awaited()

    async def test_unregistered_bot_uses_event_bot_profile_name(self):
        value, _, transport, _ = bridge()
        await value.handle("agent", event(
            "unregistered-bot", channel_type="im", user="U0REMOTE1",
            bot_id="B-demo", bot_profile={"name": "remote-agent", "app_id": "A-demo"},
        ))

        injected = transport.inject.await_args.args[1]
        self.assertIn("[100] remote-agent (bot U0REMOTE1): work", injected)

        await value.handle("agent", event(
            "operator-named-bot", channel_type="im", ts="101",
            user="U0REMOTE1", bot_id="B-demo",
            bot_profile={"name": "Pat (operator)"},
        ))
        injected = transport.inject.await_args.args[1]
        self.assertNotIn("(operator)", injected)
        self.assertIn("U0REMOTE1", injected)

    async def test_one_agents_thread_membership_is_not_anothers(self):
        settings = config()
        other = config(name="other", bot_user_id="B-other2")
        slack, slack_other = api(), api()
        slack.config, slack_other.config = settings, other
        slack.thread_role.return_value = "none"
        slack_other.thread_role.return_value = "none"
        transport, parent = delivery(None)
        value = bridge_module.Bridge(
            {"agent": settings, "other": other}, {"agent": slack, "other": slack_other},
            transport, lifecycle(), config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
        )
        await value.handle("other", event("other-posts", user="B-other2", ts="99", thread_ts="90"))
        await value.handle("agent", event("operator", thread_ts="90", user="U-operator"))
        transport.inject.assert_not_awaited()

    async def test_an_agent_named_in_a_thread_root_reads_its_replies(self):
        value, slack, transport, parent = bridge()
        slack.thread_role.return_value = "none"
        await value.handle("agent", event("root", text="<@B-test> and <@B-other> good night", ts="90", user="U-operator"))
        await value.handle("agent", event("reply", text="other replies", ts="91", thread_ts="90", user="B-other", bot_id="B-other", bot_profile={"app_id": "A-other"}))
        self.assertEqual(transport.inject.await_count, 2)
        self.assertIn("other replies", transport.inject.await_args.args[1])

    async def test_only_operator_and_operator_agent_messages_are_delivered(self):
        remote = {"user": "B-remote", "bot_id": "B-remote", "bot_profile": {"app_id": "A-remote"}}
        for sender, manageable, delivered in (
            ({"user": "U-stranger"}, True, False),
            ({"user": "U-operator"}, False, True),
            ({"user": "B-local", "bot_id": "B-local"}, False, True),
            (remote, True, True),
            (remote, False, False),
        ):
            with self.subTest(sender=sender, manageable=manageable):
                value, slack, transport, _ = bridge()
                value.configs["local"] = config(bot_user_id="B-local")
                error = None if manageable else register_module.SlackAPIError("no_permission")
                with mock.patch.object(register_module, "_user_api", side_effect=error) as export:
                    await value.handle("agent", event("dm", channel_type="im", **sender))
                    await value.handle("agent", event("dm-2", channel_type="im", ts="101", **sender))
                if sender is remote:
                    export.assert_called_once_with("apps.manifest.export", "T-test", app_id="A-remote")
                self.assertEqual(transport.inject.await_count, 2 * delivered)
                self.assertEqual(slack.add_reaction.await_count, 2 * delivered)

    async def test_remote_operator_agent_reaches_an_agent_with_a_real_config(self):
        settings = real_register.AgentConfig(
            name="agent", app_token="xapp", bot_token="xoxb", app_id="A-test",
            bot_user_id="B-test", kind="claude", workdir=Path("/tmp"),
            deliver_channel_messages=True, operator_user_id="U-operator",
            profile_dir=None, runtime_args="", launcher="", launcher_path=None, session_id=None,
        )
        value, _, transport, _ = bridge(settings)
        with mock.patch.object(register_module, "_user_api") as export:
            await value.handle("agent", event(
                "remote", channel_type="im", user="B-remote", bot_id="B-remote",
                bot_profile={"app_id": "A-remote"},
            ))
        export.assert_called_once_with("apps.manifest.export", "T-test", app_id="A-remote")
        transport.inject.assert_awaited_once()

    async def test_addressed_files_are_downloaded_or_the_message_is_refused(self):
        value, slack, transport, parent = bridge()
        slack.download_current_files.return_value = [Path("/inbox/notes.txt")]
        await value.handle("agent", event("dm", channel_type="im", files=[{"id": "F1"}]))
        transport.inject.assert_awaited_once()
        self.assertIn("files: /inbox/notes.txt", transport.inject.await_args.args[1])
        slack.add_reaction.assert_awaited_once()

        value, slack, transport, _ = bridge()
        slack.download_current_files.side_effect = ValueError("bad file")
        await value.handle("agent", event("refused", channel_type="im", files=[{"id": "F1"}]))
        slack.post_operational.assert_awaited_once()
        self.assertTrue(slack.post_operational.await_args.args[2].startswith("Message refused"))
        transport.inject.assert_not_awaited()
        slack.add_reaction.assert_not_awaited()

    async def test_duplicate_and_dead_session_have_one_outcome(self):
        value, slack, transport, _ = bridge()
        payload = event("duplicate", channel_type="im")
        await value.handle("agent", payload)
        await value.handle("agent", payload)
        transport.inject.assert_awaited_once()
        slack.add_reaction.assert_awaited_once()

        value, slack, transport, _ = bridge()
        transport.discover_parent.side_effect = bridge_module.terminal.SessionNotFound
        await value.handle("agent", event("dead", channel_type="im"))
        await value.handle("agent", event("dead-again", channel_type="im", ts="101", thread_ts="100"))
        slack.post_operational.assert_awaited_once()
        slack.add_reaction.assert_not_awaited()
        transport.inject.assert_not_awaited()
        await value.handle("agent", event("dead-elsewhere", channel_type="im", ts="200"))
        self.assertEqual(slack.post_operational.await_count, 2)

    async def test_dead_claude_session_revives_once_and_delivers_after_reconnect(self):
        settings = config(kind="claude", session_id="sess-1")
        value, slack, transport, _ = bridge(settings=settings)
        transport.discover_parent.side_effect = bridge_module.terminal.SessionNotFound
        revived = SimpleNamespace(session_id="sess-1", name="agent")
        started = asyncio.Event()

        async def revive_parent(cfg):
            started.set()
            await asyncio.sleep(0)
            return revived

        transport.revive_parent = mock.AsyncMock(side_effect=revive_parent)
        await asyncio.gather(
            value.handle("agent", event("dead-1", channel_type="im", ts="100")),
            value.handle("agent", event("dead-2", channel_type="im", ts="101")),
        )
        transport.revive_parent.assert_awaited_once_with(settings)
        self.assertEqual(transport.inject.await_count, 2)
        slack.post_operational.assert_not_awaited()

    async def test_failed_revive_posts_the_reason(self):
        settings = config(kind="claude", session_id="sess-1")
        value, slack, transport, _ = bridge(settings=settings)
        transport.discover_parent.side_effect = bridge_module.terminal.SessionNotFound
        transport.revive_parent = mock.AsyncMock(
            side_effect=RuntimeError("tmux: no server running")
        )
        await value.handle("agent", event("dead", channel_type="im"))
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100",
            "Agent session is not live; revive failed: tmux: no server running",
        )

    async def test_addressability_failure_posts_reason_without_revival(self):
        settings = config(kind="claude", session_id="sess-1")
        value, slack, transport, _ = bridge(settings=settings)
        transport.discover_parent.side_effect = LookupError("session sess-1 is live as 'auto', not 'agent'")
        value._revive = mock.AsyncMock()

        await value.handle("agent", event("unaddressable", channel_type="im"))

        value._revive.assert_not_awaited()
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100",
            "Agent session is not live: session sess-1 is live as 'auto', not 'agent'",
        )

    async def test_unusable_credentials_are_quarantined(self):
        slack_error = type("SlackError", (RuntimeError,), {})
        with (
            mock.patch.object(bridge_module.slack_api, "SlackError", slack_error, create=True),
            mock.patch.object(bridge_module.slack_register, "_unregister", create=True) as unregister,
            mock.patch.object(
                bridge_module.slack_register, "CONFIG_DIR", Path("/nonexistent/slack-bridge"), create=True
            ),
        ):
            with self.subTest("inactive credentials"):
                inactive, active = api(), api()
                inactive.auth_test = mock.AsyncMock(side_effect=slack_error(
                    "Slack auth.test failed: account_inactive"
                ))
                active.auth_test = mock.AsyncMock(return_value={"ok": True})
                value = bridge_module.Bridge(
                    {"inactive": config(), "active": config()},
                    {"inactive": inactive, "active": active},
                    delivery()[0], lifecycle(),
                    config_dir=Path("/nonexistent/slack-bridge"),
                )

                await value._quarantine_inactive()

                self.assertNotIn("inactive", value.apis)
                self.assertNotIn("inactive", value.configs)
                self.assertIn("active", value.apis)
                self.assertIn("active", value.configs)
                unregister.assert_called_once_with("inactive")
                unregister.reset_mock()

            with self.subTest("non-default config dir"):
                inactive = api()
                inactive.auth_test = mock.AsyncMock(side_effect=slack_error(
                    "Slack auth.test failed: account_inactive"
                ))
                value = bridge_module.Bridge(
                    {"inactive": config()}, {"inactive": inactive}, delivery()[0], lifecycle(),
                    config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
                )
                with self.assertRaises(RuntimeError):
                    await value._quarantine_inactive()
                unregister.assert_not_called()

            with self.subTest("rate limited"):
                limited = api()
                limited.auth_test = mock.AsyncMock(side_effect=slack_error(
                    "Slack auth.test failed: ratelimited"
                ))
                value = bridge_module.Bridge(
                    {"limited": config()}, {"limited": limited}, delivery()[0], lifecycle(),
                    config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
                )

                with self.assertRaises(slack_error):
                    await value._quarantine_inactive()
                unregister.assert_not_called()

    async def test_retired_agents_drop_drained_events_and_seen_ids_are_bounded(self):
        value, _, _, _ = bridge()
        for number in range(3):
            payload = event(str(number))
            payload["event"]["type"] = "reaction_added"
            with mock.patch.object(bridge_module, "MAX_SEEN", 2):
                await value.handle("agent", payload)
        self.assertEqual(list(value.seen), ["1", "2"])

        value.configs.clear()
        value.apis.clear()
        await value.handle("agent", event("retired"))

    async def test_controls_are_intercepted_and_operator_only(self):
        value, slack, transport, parent = bridge()
        await value.handle("agent", event(
            "refused", "<@B-test> !compact", user="U-other"
        ))
        slack.post_operational.assert_awaited_once()
        slack.add_reaction.assert_not_awaited()
        transport.control.assert_not_awaited()
        transport.inject.assert_not_awaited()

        value, slack, transport, parent = bridge()
        await value.handle("agent", event(
            "goal", "<@B-test> !goal ship", user="U-operator"
        ))
        transport.control.assert_awaited_once_with(parent, "goal", "ship")
        slack.add_reaction.assert_awaited_once_with("C-test", "100")
        transport.inject.assert_not_awaited()

        value, slack, transport, _ = bridge()
        transport.control.side_effect = RuntimeError("denied")
        await value.handle("agent", event(
            "failed", "<@B-test> !compact", user="U-operator"
        ))
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100", "Control failed: denied"
        )
        slack.add_reaction.assert_not_awaited()

    async def test_toggle_controls_type_their_argument(self):
        for text, command, argument, reply in (
            ("<@B-test> !fast on", "fast", "on", "Sent /fast on."),
        ):
            with self.subTest(text=text):
                value, slack, transport, parent = bridge()
                transport.control.return_value = None

                await value.handle("agent", event("toggle", text, user="U-operator"))

                transport.control.assert_awaited_once_with(parent, command, argument)
                slack.post_operational.assert_awaited_once_with("C-test", "100", reply)

    async def test_antigravity_refuses_claude_only_toggle_controls(self):
        for command in ("fast",):
            with self.subTest(command=command):
                value, slack, transport, _ = bridge(runtime="antigravity")

                await value.handle("agent", event(
                    "toggle", f"<@B-test> !{command} on", user="U-operator"
                ))

                transport.control.assert_not_awaited()
                slack.post_operational.assert_awaited_once_with(
                    "C-test", "100",
                    f"Control !{command} is not supported for Antigravity sessions.",
                )

    async def test_effort_control_posts_the_broker_acknowledgment(self):
        value, slack, transport, parent = bridge()
        transport.control.return_value = "Set effort level to xhigh"

        await value.handle("agent", event(
            "effort", "<@B-test> !effort xhigh", user="U-operator"
        ))

        transport.control.assert_awaited_once_with(parent, "effort", "xhigh")
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100", "Set effort level to xhigh"
        )

    async def test_lifecycle_controls_update_connections_in_process(self):
        value, old_api, transport, _ = bridge()
        renamed_api = api()
        value.lifecycle.load_agent_configs.return_value = {"renamed": config()}
        rename_event = event("rename", user="U-operator")["event"]
        with (
            mock.patch.object(bridge_module.slack_api, "SlackAPI", return_value=renamed_api),
            mock.patch.object(value, "_start_api") as start,
        ):
            accepted = await value._control(
                "agent", value.configs["agent"], old_api,
                rename_event, ("rename", "renamed"),
            )
        self.assertTrue(accepted)
        transport.control.assert_awaited_once_with(mock.ANY, "rename", "renamed")
        old_api.close.assert_awaited_once()
        self.assertEqual(set(value.apis), {"renamed"})
        start.assert_called_once_with("renamed")

        value, old_api, _, _ = bridge()
        unregister_event = event("unregister", user="U-operator")["event"]
        accepted = await value._control(
            "agent", value.configs["agent"], old_api,
            unregister_event, ("unregister", ""),
        )
        self.assertTrue(accepted)
        old_api.close.assert_awaited_once()
        self.assertEqual(value.apis, {})


class LiveBatchingTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_agent_prompts_are_injected_once_in_arrival_order(self):
        value, _, transport, parent = bridge(batched=True)
        latest_parent = SimpleNamespace(session_id="latest-parent")
        with mock.patch.object(bridge_module, "LIVE_BATCH_HOLD_SECONDS", 0.05):
            await value._inject_batched("agent", parent, "first")
            await value._inject_batched("agent", latest_parent, "second")
            transport.inject.assert_not_awaited()
            await asyncio.sleep(0.1)
        transport.inject.assert_awaited_once_with(latest_parent, "first\n\nsecond")

    async def test_prompt_after_hold_starts_a_new_batch(self):
        value, _, transport, parent = bridge(batched=True)
        with mock.patch.object(bridge_module, "LIVE_BATCH_HOLD_SECONDS", 0.05):
            await value._inject_batched("agent", parent, "first")
            await asyncio.sleep(0.1)
            await value._inject_batched("agent", parent, "second")
            await asyncio.sleep(0.1)
        self.assertEqual(
            [call.args[1] for call in transport.inject.await_args_list],
            ["first", "second"],
        )

    async def test_different_agents_have_separate_batches(self):
        value, _, transport, parent = bridge(batched=True)
        other_parent = SimpleNamespace(session_id="other-parent")
        with mock.patch.object(bridge_module, "LIVE_BATCH_HOLD_SECONDS", 0.05):
            await value._inject_batched("agent", parent, "first")
            await value._inject_batched("other", other_parent, "second")
            await asyncio.sleep(0.1)
        self.assertEqual(
            [call.args for call in transport.inject.await_args_list],
            [(parent, "first"), (other_parent, "second")],
        )


class ForkWorkerReplyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def drain():
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            await task

    @staticmethod
    async def cancel():
        await bridge_module._cancel(asyncio.all_tasks() - {asyncio.current_task()})

    async def test_fork_runtime_spawns_a_worker_instead_of_injecting(self):
        value, slack, transport, parent = bridge(runtime="antigravity")
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value.handle("agent", event("fork", channel_type="im"))
            await self.drain()
        transport.spawn_fork_worker.assert_awaited_once()
        args = transport.spawn_fork_worker.await_args.args
        self.assertEqual(args[0], "antigravity")
        self.assertIs(args[1], parent)
        for fragment in (
            "channel C-test", "thread 100", "slack-send --as agent",
            "--channel C-test", "--thread 100",
        ):
            self.assertIn(fragment, args[2])
        transport.inject.assert_not_awaited()
        slack.add_reaction.assert_awaited_once_with("C-test", "100")

    async def test_claude_runtime_still_injects(self):
        value, _, transport, _ = bridge(runtime="claude")
        await value.handle("agent", event("claude", channel_type="im"))
        transport.inject.assert_awaited_once()
        transport.spawn_fork_worker.assert_not_awaited()

    async def test_control_from_a_second_operator_account_in_operator_txt_is_accepted(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        turn = asyncio.Event()
        transport.worker.wait_done = mock.AsyncMock(side_effect=lambda timeout: turn.wait())
        with mock.patch.object(
            bridge_module.slack_register, "operator_user_ids",
            return_value={"U-operator", "U-second"},
        ):
            await value.handle("agent", event("fork", channel_type="im"))
            await asyncio.sleep(0)
            await value.handle("agent", event(
                "stop", "<@B-test> !stop", ts="101", thread_ts="100", user="U-second",
            ))
        transport.worker.terminate.assert_awaited_once()
        slack.post_operational.assert_not_awaited()

    async def test_stop_control_terminates_the_thread_fork_not_the_live_session(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        turn = asyncio.Event()

        async def wait_done(timeout):
            await turn.wait()

        transport.worker.wait_done = mock.AsyncMock(side_effect=wait_done)
        await value.handle("agent", event("fork", channel_type="im"))
        await asyncio.sleep(0)
        await value.handle("agent", event(
            "stop", "<@B-test> !stop", ts="101", thread_ts="100",
            user="U-operator",
        ))
        transport.worker.terminate.assert_awaited_once()
        slack.thread_messages.assert_awaited_once_with("C-test", "100")
        transport.control.assert_not_awaited()
        transport.inject.assert_not_awaited()
        slack.add_reaction.assert_any_await("C-test", "101")

    async def test_second_message_in_a_thread_is_injected_into_the_same_fork(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("first", "<@B-test> one", thread_ts="100"))
            await value.handle(
                "agent", event("second", "<@B-test> two", ts="101", thread_ts="100")
            )
            # A follow-up that lands mid-turn is held and injected once the
            # (mocked, instant) first turn ends.
            for _ in range(20):
                await asyncio.sleep(0)
        transport.spawn_fork_worker.assert_awaited_once()
        transport.worker.inject.assert_awaited_once()
        self.assertIn("<@B-test> two", transport.worker.inject.await_args.args[0])
        self.assertEqual(slack.add_reaction.await_count, 2)

    async def test_fork_turn_without_a_post_is_resent_once(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        slack.replies_from.return_value = []
        with (
            mock.patch.object(bridge_module, "FORK_POST_WAIT", 0),
            mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0),
        ):
            await value.handle("agent", event("fork", channel_type="im"))
            await self.drain()
        nudge = transport.worker.inject.await_args.args[0]
        self.assertIn("without running slack-send", nudge)
        self.assertIn("slack-send --as agent --channel C-test --thread 100", nudge)
        self.assertEqual(transport.worker.wait_done.await_count, 2)
        transport.worker.terminate.assert_awaited_once()
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100",
            "Reply fork ended twice without posting a reply. Its last response: "
            "Here is the capacity: 16 nodes.",
        )

    async def test_fork_turn_with_a_post_is_not_resent(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        slack.replies_from.return_value = ["101"]
        with (
            mock.patch.object(bridge_module, "FORK_POST_WAIT", 0),
            mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0),
        ):
            await value.handle("agent", event("fork", channel_type="im"))
            await self.drain()
        transport.worker.inject.assert_not_awaited()
        transport.worker.wait_done.assert_awaited_once_with(timeout=bridge_module.FORK_TIMEOUT)

    async def test_followup_during_a_turn_is_injected_after_the_turn_without_interrupt(self):
        value, _, transport, _ = bridge(runtime="antigravity")
        turn = asyncio.Event()

        async def wait_done(timeout):
            await turn.wait()

        transport.worker.wait_done = mock.AsyncMock(side_effect=wait_done)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value.handle("agent", event("fork", channel_type="im"))
            await value.handle("agent", event("mid", "second question", channel_type="im", ts="101", thread_ts="100"))
            transport.worker.interrupt.assert_not_awaited()
            transport.worker.inject.assert_not_awaited()
            turn.set()
            for _ in range(20):
                await asyncio.sleep(0)
            transport.worker.inject.assert_awaited_once()
            self.assertIn("second question", transport.worker.inject.await_args.args[0])
            transport.worker.interrupt.assert_not_awaited()
            turn.set()
            await self.cancel()

    async def test_a_second_thread_gets_its_own_fork(self):
        value, _, transport, _ = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("first", "<@B-test> one", thread_ts="100"))
            await value.handle(
                "agent", event("other", "<@B-test> two", ts="201", thread_ts="200")
            )
        self.assertEqual(transport.spawn_fork_worker.await_count, 2)
        transport.worker.inject.assert_not_awaited()

    async def test_idle_fork_is_terminated_and_its_thread_forgotten(self):
        value, slack, transport, parent = bridge(runtime="antigravity")
        slack.thread_messages.side_effect = [
            [("101", "YOU (agent)", "walrus-77")],
            [("101", "YOU (agent)", "walrus-77")],
            [],
        ]
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value.handle("agent", event("fork", channel_type="im"))
            await self.drain()
        transport.worker.terminate.assert_awaited_once()
        transport.inject.assert_awaited_once()
        self.assertIs(transport.inject.await_args.args[0], parent)
        self.assertIn("walrus-77", transport.inject.await_args.args[1])
        self.assertIn("100", transport.inject.await_args.args[1])
        self.assertIn("Fork conversation:", transport.inject.await_args.args[1])
        self.assertEqual(value.fork_threads, {})
        self.assertEqual(value.fork_workers["agent"], set())

    async def test_new_fork_writes_forks_json(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        slack.thread_messages.return_value = []
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("fork", "count the nodes", channel_type="im"))
        status = json.loads((value.config_dir / "forks.json").read_text())
        self.assertEqual(status["queued"], [])
        [running] = status["running"]
        self.assertEqual(running["thread_ts"], "100")
        self.assertEqual(running["fork_id"], "fork-1")
        self.assertEqual(running["task"], "count the nodes")

    async def test_a_fork_without_an_id_yet_is_written_without_a_transcript(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        slack.thread_messages.return_value = []
        worker = value.delivery.worker
        worker.fork_id = None
        worker.advertised_id = None

        # The real ForkWorker.transcript joins root / "brain" / fork_id and
        # raises TypeError while fork_id is None, which crash-looped the bridge
        # on every new fork.
        class Unnamed(SimpleNamespace):
            @property
            def transcript(self):
                return Path("/root") / "brain" / self.fork_id

        fields = {k: v for k, v in vars(worker).items() if k != "transcript"}
        value.delivery.worker = Unnamed(**fields)
        value.delivery.spawn_fork_worker = mock.AsyncMock(return_value=value.delivery.worker)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("fork", "count the nodes", channel_type="im"))
        [running] = json.loads((value.config_dir / "forks.json").read_text())["running"]
        self.assertIsNone(running["fork_id"])
        self.assertEqual(running["transcript"], "")

    async def test_new_fork_prompt_starts_with_the_thread_so_far(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        slack.thread_messages.return_value = [
            ("99.0", "the operator", "earlier"),
            ("100.0", "U-human", "work"),
        ]
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("fork", channel_type="im"))
        prompt = transport.spawn_fork_worker.await_args.args[2]
        self.assertTrue(prompt.startswith(
            "[Slack fork task]\n\nChannel: C-test\nThread: 100\n"
            "You are: a fresh fork of agent.\n\n"
            "Thread so far:\n---\n"
            f"> [{bridge_module._et('99.0')}] the operator: earlier\n"
            f"> [{bridge_module._et('100.0')}] U-human: work\n---\n\n"
            "New message:\n---\n> [Slack] as agent"
        ))

    async def test_fork_forwards_each_turn_since_its_watermark(self):
        value, slack, transport, parent = bridge(runtime="antigravity")
        first = [
            ("1.0", "the operator", "launch"),
            ("2.0", "YOU (agent)", "answer"),
        ]
        second = first + [("3.0", "the operator", "follow-up")]
        views = iter([first, second, second])

        async def thread_messages(channel, root, after_ts=None):
            return [
                message for message in next(views)
                if after_ts is None or float(message[0]) > float(after_ts)
            ]

        slack.thread_messages.side_effect = thread_messages
        running = bridge_module.ForkThread(
            transport.worker, slack, "1.0", parent, pending=["next turn"]
        )
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value._fork_lifecycle(("agent", "C-test", "100"), running)
        updates = [
            call.args[1] for call in transport.inject.await_args_list
            if "[Slack fork update]" in call.args[1]
        ]
        self.assertEqual(len(updates), 2)
        self.assertIn(f"> [{bridge_module._et('1.0')}]", updates[0])
        self.assertIn(f"> [{bridge_module._et('2.0')}]", updates[0])
        self.assertNotIn(f"> [{bridge_module._et('3.0')}]", updates[0])
        self.assertIn(f"> [{bridge_module._et('3.0')}]", updates[1])
        self.assertNotIn(f"> [{bridge_module._et('1.0')}]", updates[1])
        self.assertNotIn(f"> [{bridge_module._et('2.0')}]", updates[1])
        self.assertEqual(running.forwarded_ts, "3.0")

    async def test_fork_updates_are_chunked_and_resume_after_an_injection_failure(self):
        value, slack, transport, parent = bridge(runtime="antigravity")
        messages = [
            ("1.0", "the operator", "first-" + "a" * 70),
            ("2.0", "YOU (agent)", "second-" + "b" * 70),
            ("3.0", "the operator", "third-" + "c" * 70),
        ]
        slack.thread_messages.return_value = messages
        key = ("agent", "C-test", "100")
        with mock.patch.object(bridge_module, "FORK_INJECT_CAP", 240):
            running = bridge_module.ForkThread(transport.worker, slack, "1.0", parent)
            await value._forward_fork_update(key, running, "open")
            updates = [call.args[1] for call in transport.inject.await_args_list]
            self.assertEqual(len(updates), 2)
            for _, _, text in messages:
                self.assertEqual(sum(text in update for update in updates), 1)
            self.assertEqual(running.forwarded_ts, "3.0")

            transport.inject.reset_mock()
            transport.inject.side_effect = [None, RuntimeError("session gone")]
            running = bridge_module.ForkThread(transport.worker, slack, "1.0", parent)
            await value._forward_fork_update(key, running, "open")
        self.assertEqual(running.forwarded_ts, "2.0")

    async def test_new_fork_prompt_stays_within_its_cap(self):
        value, slack, transport, parent = bridge(runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        queued = bridge_module.ForkQueue(
            slack, "antigravity", parent, "100", ["queued-" + "q" * 600]
        )
        slack.thread_messages.return_value = [
            (str(index), "the operator", f"thread-{index}-" + "t" * 200)
            for index in range(4)
        ]
        with (
            mock.patch.object(bridge_module, "FORK_INJECT_CAP", 2_000),
            mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60),
        ):
            await value._launch_fork(("agent", "C-test", "100"), queued)
        prompt = transport.spawn_fork_worker.await_args.args[2]
        self.assertLessEqual(len(prompt.encode()), 2_000)
        self.assertTrue(prompt.endswith(bridge_module.FORK_INSTRUCTIONS.format(
            agent="agent", channel="C-test", root="100",
            send_tool="antigravity-send", session="parent-session",
        )))

    async def test_message_during_fork_shutdown_is_answered_by_a_new_fork(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        gate = asyncio.Event()

        async def slow_terminate():
            await gate.wait()

        transport.worker.terminate = mock.AsyncMock(side_effect=slow_terminate)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value.handle("agent", event("fork", channel_type="im"))
            for _ in range(20):
                await asyncio.sleep(0)
            # The fork has finished its turn and is shutting down; a follow-up lands now.
            await value.handle("agent", event("late", "and now?", channel_type="im", ts="101", thread_ts="100"))
            gate.set()
            await self.drain()
        self.assertEqual(transport.spawn_fork_worker.await_count, 2)
        self.assertIn("and now?", transport.spawn_fork_worker.await_args.args[2])

    async def test_failed_worker_is_terminated_and_reported(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        transport.worker.wait_done.side_effect = RuntimeError("pty died")
        await value.handle("agent", event("fork", channel_type="im"))
        await self.drain()
        transport.worker.terminate.assert_awaited_once()
        slack.post_operational.assert_awaited_once()
        self.assertIn("pty died", slack.post_operational.await_args.args[2])
        self.assertEqual(value.fork_workers["agent"], set())

    async def test_spawn_failure_falls_back_to_the_live_session_silently(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        transport.spawn_fork_worker.side_effect = RuntimeError("no pty")
        await value.handle("agent", event("fork", channel_type="im"))
        # The live session receives the message, so the channel gets no notice.
        transport.inject.assert_awaited_once()
        self.assertIn("channel C-test thread 100", transport.inject.await_args.args[1])
        slack.post_operational.assert_not_awaited()
        slack.add_reaction.assert_not_awaited()

    async def test_spawn_failure_is_reported_when_the_fallback_fails_too(self):
        value, slack, transport, _ = bridge(runtime="antigravity")
        transport.spawn_fork_worker.side_effect = RuntimeError("no pty")
        transport.inject.side_effect = RuntimeError("session gone")
        await value.handle("agent", event("fork", channel_type="im"))
        slack.post_operational.assert_awaited_once_with(
            "C-test", "100", "Reply worker failed to start: session gone"
        )
        await value.handle("agent", event("fork-again", channel_type="im", ts="101", thread_ts="100"))
        slack.post_operational.assert_awaited_once()

    async def test_fork_runtime_answers_its_thread_but_not_another_agent_ask(self):
        value, slack, transport, _ = bridge(config(kind="antigravity", deliver_channel_messages=False), runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        slack.thread_role.return_value = "root"
        await value.handle("agent", event("elsewhere", "<@B-other> status?", ts="101", thread_ts="100"))
        transport.spawn_fork_worker.assert_not_awaited()
        slack.add_reaction.assert_not_awaited()
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("followup", "and the logs?", ts="102", thread_ts="100"))
        transport.spawn_fork_worker.assert_awaited_once()
        slack.add_reaction.assert_awaited_once_with("C-test", "102")

    async def test_fork_runtime_answers_a_peer_agent_in_its_thread(self):
        value, slack, transport, _ = bridge(config(kind="antigravity", deliver_channel_messages=False), runtime="antigravity")
        self.addAsyncCleanup(self.cancel)
        slack.thread_role.return_value = "root"
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 60):
            await value.handle("agent", event("peer", "redo rollout-261 now", ts="101", thread_ts="100", bot_id="B-other"))
        transport.spawn_fork_worker.assert_awaited_once()
        slack.add_reaction.assert_awaited_once_with("C-test", "101")

    async def test_shutdown_waits_for_a_fork_mid_turn_and_delivers_its_handoff(self):
        value, slack, transport, parent = bridge(runtime="antigravity")

        async def wait_done(timeout):
            await asyncio.sleep(0.05)

        slack.thread_messages.side_effect = [
            [],
            [("101", "YOU (agent)", "walrus-77")],
            [],
        ]
        transport.worker.wait_done = mock.AsyncMock(side_effect=wait_done)
        with mock.patch.object(bridge_module, "FORK_IDLE_GRACE", 0):
            await value.handle("agent", event("fork", channel_type="im"))
            await value.shutdown()
        transport.inject.assert_awaited_once()
        self.assertIs(transport.inject.await_args.args[0], parent)
        self.assertIn("walrus-77", transport.inject.await_args.args[1])


class CatchUpTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_replays_missed_messages_and_thread_replies_once(self):
        value, slack, transport, _ = bridge()
        (value.config_dir / "last-seen.json").write_text('{"agent": "100"}')
        root = {
            "type": "message", "ts": "101", "user": "U-operator",
            "text": "<@B-test> missed", "latest_reply": "102",
        }
        reply = {
            "type": "message", "ts": "102", "user": "U-operator",
            "text": "<@B-test> later", "thread_ts": "101",
        }
        # A thread rooted before the watermark: only its reply was missed.
        old_root = {
            "type": "message", "ts": "090", "user": "U-operator",
            "text": "<@B-test> earlier", "latest_reply": "103",
        }
        old_reply = {
            "type": "message", "ts": "103", "user": "U-operator",
            "text": "<@B-test> in an old thread", "thread_ts": "090",
        }

        async def since(method, channel, oldest, **fields):
            self.assertEqual((channel, oldest), ("C-test", "100"))
            if method == "conversations.history":
                return [root]
            return {"101": [root, reply], "090": [old_root, old_reply]}[fields["ts"]]

        slack.bot_channels = mock.AsyncMock(return_value=["C-test"])
        slack.history = mock.AsyncMock(return_value=[root, old_root])
        slack.since = mock.AsyncMock(side_effect=since)
        slack.run = mock.AsyncMock()
        await value.run()
        injected = [call.args[1] for call in transport.inject.await_args_list]
        # The root is returned by both history and replies: it is handled once,
        # and the pre-watermark root of the old thread is not replayed at all.
        self.assertEqual(len(injected), 3)
        self.assertIn("<@B-test> missed", injected[0])
        self.assertIn("<@B-test> later", injected[1])
        self.assertIn("<@B-test> in an old thread", injected[2])
        self.assertEqual(value.last_seen["agent"], "103")

    async def test_agent_watch_starts_a_new_agent_without_reentering_run(self):
        transport, _ = delivery()
        value = bridge_module.Bridge(
            {}, {}, transport, lifecycle(),
            config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
        )
        agent_dir = value.config_dir / "agents"
        agent_dir.mkdir()
        joined = config(bot_user_id="B-joined")
        joined_api = api()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run(_handle, on_reconnect=None):
            started.set()
            await stopped.wait()

        async def close():
            stopped.set()

        joined_api.run = mock.AsyncMock(side_effect=run)
        joined_api.close = mock.AsyncMock(side_effect=close)
        configs = {}
        value.lifecycle.load_agent_configs.side_effect = lambda _path: dict(configs)
        with (
            mock.patch.object(bridge_module, "AGENT_POLL_SECONDS", 0.001),
            mock.patch.object(bridge_module.slack_api, "SlackAPI", return_value=joined_api),
            mock.patch.object(value, "_load_last_seen") as load_last_seen,
        ):
            watcher = asyncio.create_task(value._watch_agents())
            try:
                await asyncio.sleep(0.01)
                (agent_dir / "joined.env").write_text("joined")
                configs["joined"] = joined
                await asyncio.wait_for(started.wait(), 1)
                self.assertIn("joined", value.apis)
                load_last_seen.assert_not_called()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                if "joined" in value.apis:
                    await value._retire("joined")
        await asyncio.sleep(0)

    async def test_agent_watch_survives_a_failed_poll(self):
        transport, _ = delivery()
        value = bridge_module.Bridge(
            {}, {}, transport, lifecycle(),
            config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
        )
        agent_dir = value.config_dir / "agents"
        agent_dir.mkdir()
        (agent_dir / "joined.env").write_text("joined")
        joined_api = api()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run(_handle, on_reconnect=None):
            started.set()
            await stopped.wait()

        async def close():
            stopped.set()

        joined_api.run = mock.AsyncMock(side_effect=run)
        joined_api.close = mock.AsyncMock(side_effect=close)
        reads = iter([RuntimeError("agent files and registry do not match")])

        def load(_path):
            failure = next(reads, None)
            if failure:
                raise failure
            return {"joined": config(bot_user_id="B-joined")}

        value.lifecycle.load_agent_configs.side_effect = load
        with (
            mock.patch.object(bridge_module, "AGENT_POLL_SECONDS", 0.001),
            mock.patch.object(bridge_module.slack_api, "SlackAPI", return_value=joined_api),
        ):
            watcher = asyncio.create_task(value._watch_agents())
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertIn("joined", value.apis)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                if "joined" in value.apis:
                    await value._retire("joined")
        await asyncio.sleep(0)

    async def test_agent_watch_retries_an_agent_whose_quarantine_failed(self):
        transport, _ = delivery()
        value = bridge_module.Bridge(
            {}, {}, transport, lifecycle(),
            config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
        )
        agent_dir = value.config_dir / "agents"
        agent_dir.mkdir()
        (agent_dir / "joined.env").write_text("joined")
        joined_api = api()
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def run(_handle, on_reconnect=None):
            started.set()
            await stopped.wait()

        async def close():
            stopped.set()

        slack_error = type("SlackError", (RuntimeError,), {})
        failures = iter([slack_error("auth.test request failed: timeout")])

        async def auth_test():
            failure = next(failures, None)
            if failure:
                raise failure
            return {"ok": True}

        joined_api.run = mock.AsyncMock(side_effect=run)
        joined_api.close = mock.AsyncMock(side_effect=close)
        joined_api.auth_test = mock.AsyncMock(side_effect=auth_test)
        value.lifecycle.load_agent_configs.return_value = {
            "joined": config(bot_user_id="B-joined")
        }
        with (
            mock.patch.object(bridge_module, "AGENT_POLL_SECONDS", 0.001),
            mock.patch.object(bridge_module.slack_api, "SlackAPI", return_value=joined_api),
            mock.patch.object(bridge_module.slack_api, "SlackError", slack_error, create=True),
        ):
            watcher = asyncio.create_task(value._watch_agents())
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertIn("joined", value.apis)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                if "joined" in value.apis:
                    await value._retire("joined")
        await asyncio.sleep(0)

    async def test_agent_watch_retires_a_removed_env_file(self):
        config_dir = Path(tempfile.mkdtemp(prefix="slack-bridge-test-"))
        agent_dir = config_dir / "agents"
        agent_dir.mkdir()
        agent_env = agent_dir / "agent.env"
        agent_env.write_text("agent")
        settings = config()
        slack = api()
        transport, _ = delivery()
        value = bridge_module.Bridge(
            {"agent": settings}, {"agent": slack}, transport, lifecycle(), config_dir=config_dir
        )
        retired = asyncio.Event()

        async def close():
            retired.set()

        slack.close = mock.AsyncMock(side_effect=close)
        agent_env.unlink()
        value.lifecycle.load_agent_configs.return_value = {}
        with mock.patch.object(bridge_module, "AGENT_POLL_SECONDS", 0.001):
            watcher = asyncio.create_task(value._watch_agents())
            try:
                await asyncio.wait_for(retired.wait(), 1)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        self.assertNotIn("agent", value.apis)
        self.assertNotIn("agent", value.configs)

    async def test_catch_up_skips_messages_after_its_live_delivery_cutoff(self):
        value, slack, transport, _ = bridge()
        value.catch_up_cutoffs["agent"] = "101"
        slack.bot_channels.return_value = ["C-test"]
        slack.history.return_value = []
        slack.since.return_value = [
            {"type": "message", "ts": "101", "user": "U-operator", "text": "<@B-test> before"},
            {"type": "message", "ts": "102", "user": "U-operator", "text": "<@B-test> after"},
        ]

        await value._catch_up_agent("agent", "100")

        transport.inject.assert_awaited_once()
        self.assertIn("before", transport.inject.await_args.args[1])
        self.assertNotIn("after", transport.inject.await_args.args[1])

    async def test_reconnect_replays_a_recent_window_and_moves_the_cutoff(self):
        value, slack, _, _ = bridge()
        value.catch_up_cutoffs["agent"] = "1.0"
        value._catch_up_agent = mock.AsyncMock()
        value._start_api("agent")
        await asyncio.sleep(0)
        on_reconnect = slack.run.await_args.kwargs["on_reconnect"]

        before = time.time()
        await on_reconnect()

        self.assertGreaterEqual(float(value.catch_up_cutoffs["agent"]), before)
        agent, oldest = value._catch_up_agent.await_args.args
        self.assertEqual(agent, "agent")
        self.assertAlmostEqual(
            float(oldest),
            before - bridge_module.RECONNECT_REPLAY_SECONDS,
            delta=5,
        )

    async def test_every_live_delivery_starts_before_a_blocked_catch_up_finishes(self):
        first, second = config(bot_user_id="B-first"), config(bot_user_id="B-second")
        first_api, second_api = api(), api()
        transport, _ = delivery()
        value = bridge_module.Bridge(
            {"first": first, "second": second},
            {"first": first_api, "second": second_api},
            transport, lifecycle(), config_dir=Path(tempfile.mkdtemp(prefix="slack-bridge-test-")),
        )
        (value.config_dir / "last-seen.json").write_text('{"first": "100", "second": "100"}')
        first_started = asyncio.Event()
        second_catch_up_started = asyncio.Event()
        release_catch_up = asyncio.Event()
        stop_delivery = asyncio.Event()

        async def run_first(_handle, on_reconnect=None):
            first_started.set()
            await stop_delivery.wait()

        async def run_second(_handle, on_reconnect=None):
            await stop_delivery.wait()

        async def block_second_catch_up():
            second_catch_up_started.set()
            await release_catch_up.wait()
            return []

        async def close():
            stop_delivery.set()

        first_api.run = mock.AsyncMock(side_effect=run_first)
        second_api.run = mock.AsyncMock(side_effect=run_second)
        first_api.close = mock.AsyncMock(side_effect=close)
        second_api.close = mock.AsyncMock(side_effect=close)
        second_api.bot_channels = mock.AsyncMock(side_effect=block_second_catch_up)
        run_task = asyncio.create_task(value.run())
        try:
            await asyncio.wait_for(second_catch_up_started.wait(), 1)
            await asyncio.wait_for(first_started.wait(), 1)
            self.assertFalse(release_catch_up.is_set())
        finally:
            release_catch_up.set()
            await value.shutdown()
            await run_task

    async def test_a_message_that_is_never_delivered_stays_for_the_next_run(self):
        value, slack, transport, _ = bridge()
        transport.discover_parent = mock.AsyncMock(side_effect=RuntimeError("dead"))
        await value.handle("agent", event("dm", channel_type="im"))
        transport.inject.assert_not_awaited()
        slack.post_operational.assert_awaited_once()
        self.assertEqual(value.last_seen, {})

        value, _, transport, _ = bridge()
        await value.handle("agent", event("dm", channel_type="im"))
        transport.inject.assert_awaited_once()
        self.assertEqual(value.last_seen, {"agent": "100"})


if __name__ == "__main__":
    unittest.main()
