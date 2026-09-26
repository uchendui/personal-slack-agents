#!/usr/bin/env python3

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_admin.py"
sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("slack_admin_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeleteThreadTest(unittest.TestCase):
    def test_paginates_deletes_each_ts_once_and_root_last(self):
        root = {"ts": "1.0", "text": "root"}
        pages = [
            {"ok": True, "messages": [root, {"ts": "1.1", "text": "a"}],
             "response_metadata": {"next_cursor": "c2"}},
            {"ok": True, "messages": [root, {"ts": "1.2", "text": "b"}],
             "response_metadata": {"next_cursor": ""}},
        ]
        deletions = []

        def fake_api(token, method, **fields):
            if method == "conversations.replies":
                self.assertEqual(token, "fake-user-admin")
                self.assertEqual(fields["channel"], "C1")
                self.assertEqual(fields["ts"], "1.0")
                return pages.pop(0)
            self.assertEqual(method, "chat.delete")
            deletions.append(fields["ts"])
            return {"ok": True}

        with mock.patch.object(module, "api", side_effect=fake_api):
            with redirect_stdout(io.StringIO()) as out:
                counts = module.delete_thread("fake-user-admin", "C1", "1.0")
        self.assertEqual(counts, (3, 0, 0))
        self.assertEqual(deletions, ["1.1", "1.2", "1.0"])
        self.assertIn("deleted 3 / skipped 0 / failed 0", out.getvalue())

    def test_only_substring_skips_non_matching_messages(self):
        def fake_api(token, method, **fields):
            if method == "conversations.replies":
                return {"ok": True, "messages": [
                    {"ts": "1.0", "text": "root"},
                    {"ts": "1.1", "text": "spam alpha"},
                    {"ts": "1.2", "text": "keep me"},
                    {"ts": "1.3"},
                ]}
            deletions.append(fields["ts"])
            return {"ok": True}

        deletions = []
        with mock.patch.object(module, "api", side_effect=fake_api):
            with redirect_stdout(io.StringIO()):
                counts = module.delete_thread("fake-user-admin", "C1", "1.0", only="spam")
        self.assertEqual(counts, (1, 3, 0))
        self.assertEqual(deletions, ["1.1"])

    def test_failed_delete_is_counted_and_does_not_stop_the_sweep(self):
        def fake_api(token, method, **fields):
            if method == "conversations.replies":
                return {"ok": True, "messages": [
                    {"ts": "1.0", "text": "root"},
                    {"ts": "1.1", "text": "a"},
                    {"ts": "1.2", "text": "b"},
                ]}
            if fields["ts"] == "1.1":
                raise module.AdminError("Slack chat.delete failed: cant_delete_message")
            deletions.append(fields["ts"])
            return {"ok": True}

        deletions = []
        with mock.patch.object(module, "api", side_effect=fake_api):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                counts = module.delete_thread("fake-user-admin", "C1", "1.0")
        self.assertEqual(counts, (2, 0, 1))
        self.assertEqual(deletions, ["1.2", "1.0"])
        self.assertIn("cant_delete_message", err.getvalue())

    def test_delete_registered_app_reports_when_the_bridge_picks_up_removal(self):
        registry = {"agents": {"agent": {}}, "tombstones": {}}
        with (
            mock.patch.object(module.slack_register, "load_registry", return_value=registry),
            mock.patch.object(module, "_resolve_app", return_value=("agent", "A-test")),
            mock.patch.object(module, "_user_api"),
            mock.patch.object(module.slack_register, "_unregister") as unregister,
            mock.patch("builtins.print") as printed,
        ):
            failures = module._delete_apps(["agent"])
        self.assertEqual(failures, 0)
        unregister.assert_called_once_with("agent")
        printed.assert_has_calls([
            mock.call("slack-bridge picks up agent within 5 s"),
            mock.call("deleted: agent (app A-test)"),
        ])


if __name__ == "__main__":
    unittest.main()
