#!/usr/bin/env python3

import asyncio
import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_api.py"
spec = importlib.util.spec_from_file_location("slack_api_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def api(opener=None, peers=()):
    config = SimpleNamespace(
        app_token="xapp-test",
        bot_token="fake-bot-test",
        name="agent",
        bot_user_id="U-self",
        operator_user_id="U-operator",
    )
    return module.SlackAPI(config, opener=opener or mock.Mock(), peers=peers)


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.frames)
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, value):
        self.sent.append(value)

    async def close(self):
        pass


class SlackAPITest(unittest.IsolatedAsyncioTestCase):
    async def test_auth_test_uses_the_bot_token(self):
        slack = api()
        response = {"ok": True, "user_id": "U-self"}
        slack._call = mock.AsyncMock(return_value=response)

        self.assertIs(await slack.auth_test(), response)
        slack._call.assert_awaited_once_with("auth.test", "fake-bot-test")

    async def test_socket_acks_immediately_and_consumes_in_order(self):
        socket = Socket([
            json.dumps({"envelope_id": "one", "type": "events_api", "payload": {"n": 1}}),
            json.dumps({"envelope_id": "two", "type": "events_api", "payload": {"n": 2}}),
        ])
        slack = api()
        slack._call = mock.AsyncMock(return_value={"url": "wss://test"})
        started, release = asyncio.Event(), asyncio.Event()
        handled = []

        async def callback(payload):
            handled.append(payload["n"])
            if payload["n"] == 1:
                started.set()
                await release.wait()
            else:
                await slack.close()

        with mock.patch.object(module.websockets, "connect", return_value=socket):
            task = asyncio.create_task(slack.run(callback))
            await started.wait()
            self.assertEqual(len(socket.sent), 2)
            release.set()
            await task
        self.assertEqual(handled, [1, 2])

    async def test_socket_disconnect_opens_a_fresh_connection(self):
        sockets = [
            Socket([json.dumps({"type": "disconnect"})]),
            Socket([json.dumps({"type": "events_api", "payload": {"n": 1}})]),
        ]
        slack = api()
        slack._call = mock.AsyncMock(
            side_effect=[{"url": "wss://first"}, {"url": "wss://second"}]
        )
        handled = []

        async def callback(payload):
            handled.append(payload["n"])
            await slack.close()

        with mock.patch.object(module.websockets, "connect", side_effect=sockets) as connect:
            await slack.run(callback)
        self.assertEqual(handled, [1])
        self.assertEqual(connect.call_count, 2)

    async def test_reconnect_replay_runs_only_after_the_first_connection(self):
        sockets = [
            Socket([json.dumps({"type": "disconnect"})]),
            Socket([json.dumps({"type": "events_api", "payload": {"n": 1}})]),
        ]
        slack = api()
        slack._call = mock.AsyncMock(
            side_effect=[{"url": "wss://first"}, {"url": "wss://second"}]
        )
        replayed_on = []

        async def on_reconnect():
            replayed_on.append(connect.call_count)

        async def callback(payload):
            await slack.close()

        with mock.patch.object(module.websockets, "connect", side_effect=sockets) as connect:
            await slack.run(callback, on_reconnect=on_reconnect)
        self.assertEqual(replayed_on, [2])

    async def test_socket_reconnect_retries_transport_errors_with_backoff(self):
        sockets = [
            Socket([json.dumps({"type": "disconnect"})]),
            Socket([json.dumps({"type": "events_api", "payload": {"n": 1}})]),
        ]
        slack = api()
        # First call succeeds; second call fails with transient network error; third call succeeds.
        slack._call = mock.AsyncMock(
            side_effect=[
                {"url": "wss://first"},
                module.SlackError("Slack apps.connections.open transport failed: network down"),
                {"url": "wss://third"},
            ]
        )
        handled = []

        async def callback(payload):
            handled.append(payload["n"])
            await slack.close()

        with mock.patch.object(module.websockets, "connect", side_effect=sockets), \
             mock.patch.object(module.asyncio, "sleep", return_value=None) as sleep_mock:
            await slack.run(callback)

        self.assertEqual(handled, [1])
        sleep_mock.assert_awaited_once_with(1.0)

    async def test_cursor_pages_are_all_required_and_returned_in_order(self):
        slack = api()
        slack._call = mock.AsyncMock(side_effect=[
            {"messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "next"}},
            {"messages": [{"ts": "2"}], "response_metadata": {"next_cursor": ""}},
        ])
        self.assertEqual(
            await slack._replies("C", "1"), [{"ts": "1"}, {"ts": "2"}]
        )
        self.assertEqual(slack._call.await_count, 2)

    async def test_thread_messages_orders_filters_and_labels_messages(self):
        slack = api()
        slack._replies = mock.AsyncMock(return_value=[
            {"ts": "100.4", "user": "U-self", "text": "after"},
            {"ts": "100.35", "user": "U-self"},
            {"ts": "100.1", "user": "U-self", "text": "before"},
            {"ts": "100.3", "user": "U-other", "text": "other"},
        ])
        self.assertEqual(
            await slack.thread_messages("C", "100", "100.2"),
            [("100.3", "U-other", "other"), ("100.4", "YOU (agent)", "after")],
        )

    async def test_thread_role_is_root_member_or_none(self):
        slack = api()
        messages = [{"ts": "1", "user": "U-root"}, {"ts": "2", "user": "U-other"}]
        slack._call = mock.AsyncMock(return_value={"messages": messages})
        self.assertEqual(await slack.thread_role("C", "1"), "none")
        messages[1]["text"] = f"<@{slack.config.bot_user_id}> look"
        self.assertEqual(await slack.thread_role("C", "1"), "member")
        messages.append({"ts": "3", "user": slack.config.bot_user_id})
        self.assertEqual(await slack.thread_role("C", "1"), "member")
        messages[0]["user"] = slack.config.bot_user_id
        self.assertEqual(await slack.thread_role("C", "1"), "root")

    async def test_labels_own_peer_operator_and_unknown_senders(self):
        # peers mirrors production: every agent config, the owner included;
        # the owner's line must still read YOU, not its registry handle.
        peers = (
            SimpleNamespace(name="agent", bot_user_id="U-self"),
            SimpleNamespace(name="peer-agent", bot_user_id="U-peer"),
        )
        slack = api(peers=peers)
        self.assertEqual(
            [slack.label(user) for user in ("U-self", "U-peer", "U-operator", "U-stranger")],
            ["YOU (agent)", "peer-agent", "the operator", "U-stranger"],
        )

    async def test_current_file_cap_is_checked_before_delivery(self):
        slack = api()
        for files in (
            [{"size": module.MAX_FILE_BYTES + 1, "url_private": "https://file"}],
            [{"size": "1", "url_private": "https://file"}],
            [{"size": 1}],
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                slack.validate_current_files(files)

    async def test_fresh_file_download_retries_a_403_then_refuses(self):
        slack = api()
        slack.validate_current_files = mock.Mock()
        item = {"id": "F1", "name": "a.png", "url_private": "https://files.slack.com/a.png"}
        forbidden = urllib.error.HTTPError(item["url_private"], 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "a.png"
            slack._download_sync = mock.Mock(side_effect=[forbidden, saved])
            with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()) as sleep:
                self.assertEqual(await slack.download_current_files([item], Path(directory)), [saved])
            sleep.assert_awaited_once_with(module.FILE_RETRY_SECONDS)
            slack._download_sync = mock.Mock(side_effect=forbidden)
            with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()):
                with self.assertRaisesRegex(ValueError, "file download failed"):
                    await slack.download_current_files([item], Path(directory))
            self.assertEqual(slack._download_sync.call_count, module.FILE_RETRY_ATTEMPTS)

    async def test_rate_limited_call_waits_retry_after_then_gives_up(self):
        import io
        limited = urllib.error.HTTPError(
            "https://slack.com/api/x", 429, "Too Many Requests", {"Retry-After": "3"}, None
        )
        ok = mock.MagicMock()
        ok.__enter__.return_value = io.StringIO(json.dumps({"ok": True, "v": 1}))
        slack = api(mock.Mock(side_effect=[limited, ok]))
        with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()) as sleep:
            self.assertEqual(await slack._call("x", "fake-bot-test"), {"ok": True, "v": 1})
        sleep.assert_awaited_once_with(3.0)

        opener = mock.Mock(side_effect=limited)
        slack = api(opener)
        with mock.patch.object(module.asyncio, "sleep", mock.AsyncMock()):
            with self.assertRaisesRegex(module.SlackError, "rate limited 5 times"):
                await slack._call("x", "fake-bot-test")
        self.assertEqual(opener.call_count, module.RATE_LIMIT_ATTEMPTS)

        bare = urllib.error.HTTPError("https://slack.com/api/x", 429, "Too Many Requests", {}, None)
        slack = api(mock.Mock(side_effect=bare))
        with self.assertRaisesRegex(module.SlackError, "without a Retry-After"):
            await slack._call("x", "fake-bot-test")

    async def test_read_receipt_uses_eyes_reaction(self):
        slack = api()
        slack._call = mock.AsyncMock(return_value={"ok": True})
        await slack.add_reaction("C", "1")
        slack._call.assert_awaited_once_with(
            "reactions.add", "fake-bot-test", channel="C", timestamp="1", name="eyes"
        )

    async def test_operational_post_makes_one_attempt(self):
        opener = mock.Mock(side_effect=OSError("lost response"))
        slack = api(opener)
        with self.assertRaises(module.SlackError):
            await slack.post_operational("C", "1", "notice")
        opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
