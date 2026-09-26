#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_send.py"
spec = importlib.util.spec_from_file_location("slack_send_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SlackSendTest(unittest.TestCase):
    def test_message_resolves_identity_mention_and_posts_once(self):
        sender = {"SLACK_BOT_TOKEN": "fake-bot-sender"}
        mentioned = {"BOT_USER_ID": "B-mentioned"}
        with (
            mock.patch.object(
                module, "load_agent", side_effect=[sender, mentioned]
            ),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "verify_thread"),
            mock.patch.object(module, "api", return_value={"ok": True, "ts": "1"}) as post,
        ):
            result = module.run([
                "--as", "test-sender", "--channel", "#test", "--thread", "0",
                "--mention", "test-recipient", "--text", "work",
            ])
        self.assertEqual(result, "1")
        post.assert_called_once_with(mock.ANY, "fake-bot-sender", "chat.postMessage", {
            "channel": "C-resolved", "text": "<@B-mentioned> work", "thread_ts": "0",
        })

    def test_literal_backslash_n_becomes_real_newline_in_posted_text(self):
        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api", return_value={"ok": True, "ts": "1"}) as post,
        ):
            module.run(["--as", "s", "--channel", "#c", "--text", "line one\\nline two"])
        post.assert_called_once_with(mock.ANY, "xoxb", "chat.postMessage", {
            "channel": "C-resolved", "text": "line one\nline two",
        })

    def test_over_cap_text_is_refused_without_posting(self):
        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api") as post,
        ):
            with self.assertRaises(module.SendError) as caught:
                module.run(["--as", "s", "--channel", "#c", "--text", "a" * 1001])
        self.assertIn("1001 chars (cap 1000)", str(caught.exception))
        post.assert_not_called()

    def test_exactly_at_cap_text_posts(self):
        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api", return_value={"ok": True, "ts": "1"}) as post,
        ):
            result = module.run(["--as", "s", "--channel", "#c", "--text", "a" * 1000])
        self.assertEqual(result, "1")
        post.assert_called_once_with(mock.ANY, "xoxb", "chat.postMessage", {
            "channel": "C-resolved", "text": "a" * 1000,
        })

    def test_over_cap_initial_comment_on_file_path_is_refused_without_posting(self):
        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api") as post,
        ):
            with self.assertRaises(module.SendError) as caught:
                module.run([
                    "--as", "s", "--channel", "#c", "--file", "/tmp/report.txt",
                    "--text", "b" * 1001,
                ])
        self.assertIn("cap 1000", str(caught.exception))
        post.assert_not_called()

    def test_thread_must_belong_to_the_resolved_channel(self):
        def api_router(http, token, method, fields):
            if method == "conversations.replies":
                self.assertEqual(fields["channel"], "C-resolved")
                return {"ok": True, "messages": []}
            return {"ok": True, "ts": "1"}

        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api", side_effect=api_router),
        ):
            with self.assertRaises(module.SendError) as caught:
                module.run(["--as", "s", "--channel", "#c", "--thread", "9.9", "--text", "hi"])
        self.assertIn("9.9", str(caught.exception))
        self.assertIn("C-resolved", str(caught.exception))

        def api_ok(http, token, method, fields):
            if method == "conversations.replies":
                return {"ok": True, "messages": [{"ts": "9.9"}]}
            return {"ok": True, "ts": "2"}

        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C-resolved"),
            mock.patch.object(module, "api", side_effect=api_ok),
        ):
            self.assertEqual(
                module.run(["--as", "s", "--channel", "#c", "--thread", "9.9", "--text", "hi"]),
                "2",
            )

    def test_invalid_post_timestamp_fails(self):
        with (
            mock.patch.object(module, "load_agent", return_value={"SLACK_BOT_TOKEN": "xoxb"}),
            mock.patch.object(module, "resolve_channel", return_value="C"),
            mock.patch.object(module, "api", return_value={"ok": True}),
        ):
            with self.assertRaises(module.SendError):
                module.run(["--as", "sender", "--channel", "C", "--text", "work"])

    def test_upload_ticket_uses_form_and_completion_stays_json(self):
        response = mock.MagicMock()
        response.__enter__.return_value = mock.Mock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        http = mock.Mock(return_value=response)
        module.api(http, "xoxb", "files.getUploadURLExternal", {
            "filename": "report.txt", "length": 4,
        })
        module.api(http, "xoxb", "files.completeUploadExternal", {
            "files": [{"id": "F-test"}], "channel_id": "C-test",
        })
        ticket, completion = [call.args[0] for call in http.call_args_list]
        self.assertEqual(ticket.data, b"filename=report.txt&length=4")
        self.assertEqual(
            ticket.get_header("Content-type"), "application/x-www-form-urlencoded"
        )
        self.assertEqual(json.loads(completion.data), {
            "files": [{"id": "F-test"}], "channel_id": "C-test",
        })
        self.assertEqual(completion.get_header("Content-type"), "application/json")

    def test_lost_response_is_one_failed_attempt(self):
        http = mock.Mock(side_effect=OSError("lost response"))
        with self.assertRaises(module.SendError):
            module.api(http, "fake-bot-test", "chat.postMessage", {
                "channel": "C", "text": "message",
            })
        http.assert_called_once()

    def test_load_agent_accepts_antigravity_schema(self):
        values = {
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_BOT_TOKEN": "fake-bot-test",
            "SLACK_APP_ID": "A-test",
            "BOT_USER_ID": "B-test",
            "AGENT_KIND": "antigravity",
            "WORKDIR": "/work",
            "DELIVER_CHANNEL_MESSAGES": "false",
            "OPERATOR_USER_ID": "U-operator",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test-antigravity.env"
            path.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items()),
                encoding="utf-8",
            )
            path.chmod(0o600)
            self.assertEqual(
                module.load_agent("test-antigravity", Path(temporary)), values,
            )


if __name__ == "__main__":
    unittest.main()
