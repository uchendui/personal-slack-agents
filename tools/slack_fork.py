#!/usr/bin/env python3
"""Fork and delete Antigravity conversations by copying their on-disk state."""

from __future__ import annotations

import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

MARKER = ".slack-fork-source"
SIDECARS = ("-wal", "-shm")


def _check_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"not a conversation uuid: {value}") from exc
    return str(parsed)


def _db_paths(root: Path, conversation_id: str) -> list[Path]:
    base = root / "conversations" / f"{conversation_id}.db"
    return [base] + [base.with_name(base.name + suffix) for suffix in SIDECARS]


def fork_conversation(root: Path, source_id: str) -> str:
    """Copy conversation source_id under root to a fresh id and return that id."""
    source_id = _check_uuid(source_id)
    source_db = root / "conversations" / f"{source_id}.db"
    if not source_db.exists():
        raise FileNotFoundError(f"no conversation database: {source_db}")
    new_id = str(uuid.uuid4())
    old_bytes = source_id.encode()
    new_bytes = new_id.encode()

    shutil.copytree(root / "brain" / source_id, root / "brain" / new_id)
    (root / "brain" / new_id / MARKER).write_text(source_id + "\n")

    copies = []
    for src in _db_paths(root, source_id):
        if not src.exists():
            continue
        dst = src.with_name(src.name.replace(source_id, new_id, 1))
        shutil.copyfile(src, dst)
        copies.append(dst)

    # Checkpoint before rewriting: wal frames carry checksums, so an edited
    # frame would be discarded instead of merged into the database.
    connection = sqlite3.connect(root / "conversations" / f"{new_id}.db")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        status = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if status != "ok":
        shutil.rmtree(root / "brain" / new_id)
        for dst in copies:
            dst.unlink(missing_ok=True)
        raise RuntimeError(f"forked database failed integrity_check: {status}")

    for dst in copies:
        if dst.exists() and not dst.name.endswith("-shm"):
            dst.write_bytes(dst.read_bytes().replace(old_bytes, new_bytes))
    return new_id


def delete_fork(root: Path, fork_id: str) -> None:
    """Remove a forked conversation's brain tree and database files."""
    _check_uuid(fork_id)
    brain = root / "brain" / fork_id
    if not (brain / MARKER).exists():
        raise ValueError(f"not a fork created by slack_fork: {fork_id}")
    shutil.rmtree(brain)
    for path in _db_paths(root, fork_id):
        path.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("fork", "delete"):
        print("usage: slack_fork.py {fork|delete} <root> <id>", file=sys.stderr)
        return 2
    command, root, conversation_id = argv
    if command == "fork":
        print(fork_conversation(Path(root), conversation_id))
    else:
        delete_fork(Path(root), conversation_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
