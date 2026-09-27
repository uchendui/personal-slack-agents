#!/usr/bin/env python3

import importlib.util
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_sweep.py"
sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("slack_sweep_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def bot(user_id, name, app_id="A-app"):
    return {
        "id": user_id,
        "name": name,
        "is_bot": True,
        "profile": {"api_app_id": app_id} if app_id else {},
    }


class SlackSweepTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "last-post.json"
        self.now = time.time()
        patches = (
            mock.patch.object(module, "STATE_PATH", self.state),
            mock.patch.object(module.slack_admin, "_admin_token", return_value="fake-user-test"),
            mock.patch.object(module.slack_admin, "_any_bot_token", return_value="fake-bot-test"),
            mock.patch.object(module.slack_admin, "_delete_apps"),
            mock.patch.object(module.slack_register, "load_registry", return_value={
                "agents": {"quiet": {"app_id": "A-quiet"}}, "tombstones": {"old": {"app_id": "A-old"}},
            }),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.delete = module.slack_admin._delete_apps

    def stub(self, members, messages):
        def responses(token, method, **fields):
            if method == "users.list":
                return {"members": members}
            if method == "conversations.list":
                return {"channels": [{"id": "C1", "is_member": True}]}
            if method == "conversations.history":
                return {"messages": messages}
            raise AssertionError(method)

        patch = mock.patch.object(module, "api", side_effect=responses)
        patch.start()
        self.addCleanup(patch.stop)

    def sweep(self, dry_run=False):
        output = io.StringIO()
        with redirect_stdout(output):
            module.sweep(12.0, dry_run)
        return output.getvalue()

    def test_bot_posting_inside_window_is_kept(self):
        self.stub(
            [bot("B1", "chatty")],
            [{"user": "B1", "ts": f"{self.now - 600:.6f}"}],
        )
        self.assertEqual(self.sweep(), "")
        self.delete.assert_not_called()

    def test_silent_registered_agent_is_kept_and_silent_retired_app_is_deleted(self):
        self.stub([bot("B2", "quiet", "A-quiet"), bot("B5", "retired", "A-old")], [])
        self.assertEqual(self.sweep(), "")
        self.delete.assert_called_once_with(["A-old"])

    def test_silent_bot_of_another_machine_is_kept(self):
        self.stub([bot("B4", "foreign", "A-foreign"), bot("B5", "retired", "A-old")], [])
        self.assertEqual(self.sweep(), "")
        self.delete.assert_called_once_with(["A-old"])

    def test_unresolvable_app_is_skipped(self):
        self.stub([bot("B3", "orphan", app_id=None)], [])
        self.assertIn("skipped orphan (B3): no api_app_id", self.sweep())
        self.delete.assert_not_called()

    def test_dry_run_lists_without_deleting(self):
        self.stub([bot("B5", "retired", "A-old")], [])
        self.assertIn("would delete retired (B5, app A-old)", self.sweep(dry_run=True))
        self.delete.assert_not_called()
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
