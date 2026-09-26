#!/usr/bin/env python3

import importlib.util
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "slack_fork.py"
spec = importlib.util.spec_from_file_location("slack_fork_tested", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_conversation(root: Path, conversation_id: str) -> sqlite3.Connection:
    """Build brain tree plus a WAL-mode database holding the id, leaving the wal live."""
    tree = root / "brain" / conversation_id / "steps"
    tree.mkdir(parents=True)
    (tree / "step0.json").write_text('{"role": "user"}')
    (root / "conversations").mkdir(parents=True)
    connection = sqlite3.connect(root / "conversations" / f"{conversation_id}.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE trajectory (executor TEXT)")
    connection.executemany(
        "INSERT INTO trajectory VALUES (?)", [(conversation_id,)] * 3
    )
    connection.commit()
    return connection


class SlackForkTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.source_id = str(uuid.uuid4())
        self.connection = make_conversation(self.root, self.source_id)
        self.addCleanup(self.connection.close)

    def test_fork_copies_tree_and_rewrites_every_id_occurrence(self):
        db = self.root / "conversations" / f"{self.source_id}.db"
        wal = db.with_name(db.name + "-wal")
        self.assertTrue(wal.exists())
        before = {path: path.read_bytes() for path in (db, wal)}
        occurrences = sum(
            data.count(self.source_id.encode()) for data in before.values()
        )
        self.assertGreater(occurrences, 0)

        fork_id = module.fork_conversation(self.root, self.source_id)

        self.assertEqual(
            (self.root / "brain" / fork_id / "steps" / "step0.json").read_text(),
            '{"role": "user"}',
        )
        fork_db = self.root / "conversations" / f"{fork_id}.db"
        forked = fork_db.read_bytes()
        self.assertEqual(forked.count(self.source_id.encode()), 0)
        self.assertEqual(forked.count(fork_id.encode()), occurrences)
        with sqlite3.connect(fork_db) as check:
            self.assertEqual(
                check.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                check.execute("SELECT executor FROM trajectory").fetchall(),
                [(fork_id,)] * 3,
            )
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_fork_refuses_a_conversation_with_no_database(self):
        missing = str(uuid.uuid4())
        (self.root / "brain" / missing).mkdir()

        with self.assertRaises(FileNotFoundError):
            module.fork_conversation(self.root, missing)

        self.assertEqual(
            sorted(p.name for p in (self.root / "brain").iterdir()),
            sorted([self.source_id, missing]),
        )
        self.assertEqual(
            sorted(p.name for p in (self.root / "conversations").iterdir()),
            [f"{self.source_id}.db", f"{self.source_id}.db-shm", f"{self.source_id}.db-wal"],
        )

    def test_delete_removes_only_the_fork_and_refuses_the_source(self):
        fork_id = module.fork_conversation(self.root, self.source_id)

        module.delete_fork(self.root, fork_id)

        self.assertFalse((self.root / "brain" / fork_id).exists())
        self.assertEqual(
            sorted(p.name for p in (self.root / "conversations").iterdir()),
            [f"{self.source_id}.db", f"{self.source_id}.db-shm", f"{self.source_id}.db-wal"],
        )
        self.assertTrue((self.root / "brain" / self.source_id).exists())
        with self.assertRaises(ValueError):
            module.delete_fork(self.root, self.source_id)
        with self.assertRaises(ValueError):
            module.delete_fork(self.root, "not-a-uuid")


if __name__ == "__main__":
    unittest.main()
