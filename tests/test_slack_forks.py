#!/usr/bin/env python3

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_forks.py"
spec = importlib.util.spec_from_file_location("slack_forks_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SINCE = "2026-09-09T10:00:00Z"


def status_file(directory: Path, transcript_lines: list[dict]) -> Path:
    transcript = directory / "fork.jsonl"
    transcript.write_text("".join(json.dumps(line) + "\n" for line in transcript_lines))
    path = directory / "forks.json"
    path.write_text(json.dumps({
        "written_at": "2026-09-09T10:00:30+00:00",
        "running": [{
            "agent": "agent", "channel": "C-test", "thread_ts": "100.5",
            "fork_id": "abcdef0123", "advertised_id": "abcdef0123", "pid": 1,
            "since": SINCE, "transcript": str(transcript), "idle": False,
            "closing": False, "resent": False, "last_prompt": "p",
            "task": "count the nodes",
        }],
        "queued": [],
    }))
    return path


def run_cli(path: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = module.main([], path=path)
    return code, out.getvalue()


class SlackForksTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="slack-forks-test-"))

    def test_user_input_after_since_prints_prompt_taken(self):
        path = status_file(self.directory, [
            {"type": "PLANNER_RESPONSE", "status": "DONE", "content": "old",
             "created_at": "2026-09-09T09:00:00Z", "source": "s"},
            {"type": "USER_INPUT", "status": "DONE", "content": "[Slack] work",
             "created_at": "2026-09-09T10:00:01Z", "source": "s"},
        ])
        code, text = run_cli(path)
        self.assertEqual(code, 0)
        line = text.splitlines()[1]
        self.assertIn("prompt-taken", line)
        self.assertIn("C-test/100.5", line)
        self.assertIn("count the nodes", line)
        self.assertIn("abcdef01", line)

    def test_planner_done_with_content_prints_answered(self):
        path = status_file(self.directory, [
            {"type": "USER_INPUT", "status": "DONE", "content": "[Slack] work",
             "created_at": "2026-09-09T10:00:01Z", "source": "s"},
            {"type": "PLANNER_RESPONSE", "status": "DONE",
             "content": "Posted the\nanswer.", "created_at": "2026-09-09T10:00:09Z",
             "source": "s"},
        ])
        code, text = run_cli(path)
        self.assertEqual(code, 0)
        line = text.splitlines()[1]
        self.assertIn("answered", line)
        self.assertIn("Posted the answer.", line)

    def test_missing_status_file_exits_1(self):
        code, text = run_cli(self.directory / "forks.json")
        self.assertEqual(code, 1)
        self.assertIn("no forks.json", text)


if __name__ == "__main__":
    unittest.main()
