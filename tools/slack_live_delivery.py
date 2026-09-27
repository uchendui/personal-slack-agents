#!/usr/bin/env python3
"""Authenticated PTY delivery into verified live parent sessions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pty_broker

LOG = logging.getLogger("slack-bridge")
from runtimes import RUNTIMES

SESSION_DIR = Path.home() / ".claude-sessions-shared"
STATE_DIR = Path.home() / ".local" / "state" / "slack-bridge" / "pty"
GEMINI_BASE_URL = "http://127.0.0.1:3460"
GEMINI_TOKEN_PATH = Path.home() / ".claude-code-router" / "gemini-key-router-token"
PROC_ROOT = Path("/proc")
FORK_READY_TIMEOUT = 60.0
# The USER_INPUT record can surface under a second conversation id well after
# the paste (at 20 s the bridge resent into a fork that had already answered,
# then killed it), so the wait stays at 90 s.
FORK_START_TIMEOUT = 90.0
# One revive attempt polls this often, up to this long, for the resumed
# process to re-register its PTY broker advertisement.
REVIVE_POLL_SECONDS = 2.0
REVIVE_TIMEOUT = 90.0
FORK_RUNTIMES = {
    "antigravity": {
        "root": Path.home() / ".gemini" / "antigravity-cli",
        "broker": str(Path.home() / ".local" / "bin" / "antigravity-pty-broker"),
        "cli": str(Path.home() / ".local" / "bin" / "agy"),
    },
}

_BRAIN_DIR_RE = re.compile(r"/brain/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/")
_PRESENCE_LOCK_RE = re.compile(r"/presence/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock$")
_CONVERSATION_DB_RE = re.compile(r"/conversations/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.db(?:-wal|-shm|-journal)?$")


def _parent_flag(pid: int, flag: str) -> str | None:
    """The value of `flag` on the live session's command line, or None."""
    try:
        args = (PROC_ROOT / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    wanted = flag.encode()
    for index, arg in enumerate(args):
        if arg == wanted and index + 1 < len(args):
            return args[index + 1].decode()
        if arg.startswith(wanted + b"="):
            return arg.partition(b"=")[2].decode()
    return None


def _parent_model(pid: int) -> list[str]:
    """The live session's --model flag, or nothing when it was launched
    without one."""
    model = _parent_flag(pid, "--model")
    return ["--model", model] if model else []


def _parent_effort(pid: int) -> list[str]:
    """The live session's --effort flag, so a fork thinks at the same level.
    Read from the parent's command line; a parent launched
    without the flag gives a fork without it."""
    try:
        args = (PROC_ROOT / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return []
    for index, arg in enumerate(args):
        if arg == b"--effort" and index + 1 < len(args):
            return ["--effort", args[index + 1].decode()]
        if arg.startswith(b"--effort="):
            return ["--effort", arg.partition(b"=")[2].decode()]
    return []


def _process_tree(pid: int, proc_root: Path) -> list[int]:
    """pid and every descendant, via /proc/<pid>/task/<pid>/children."""
    pids, queue = [], [pid]
    while queue:
        current = queue.pop()
        pids.append(current)
        try:
            text = (proc_root / str(current) / "task" / str(current) / "children").read_text()
        except OSError:
            continue
        queue.extend(int(child) for child in text.split())
    return pids


def open_conversation_ids(pid: int, root: Path, proc_root: Path = Path("/proc")) -> set[str]:
    """Every conversation id the process tree holds open under root."""
    found: set[str] = set()
    db_prefix = str(root) + "/conversations/"
    brain_prefix = str(root) + "/brain/"
    presence_prefix = str(root) + "/presence/"
    readable = 0
    for member in _process_tree(pid, proc_root):
        fd_dir = proc_root / str(member) / "fd"
        try:
            entries = list(fd_dir.iterdir())
        except OSError:
            continue
        readable += 1
        for entry in entries:
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            # The database may be open only during a turn, but the presence
            # lock and files under brain/<id>/ stay open; either names the conversation.
            match = _CONVERSATION_DB_RE.search(target)
            if match and target.startswith(db_prefix):
                found.add(match.group(1))
            match = _BRAIN_DIR_RE.search(target)
            if match and target.startswith(brain_prefix):
                found.add(match.group(1))
            match = _PRESENCE_LOCK_RE.search(target)
            if match and target.startswith(presence_prefix):
                found.add(match.group(1))
    if not readable:
        raise RuntimeError(f"cannot read open files of process {pid} or its children")
    return found


def launch_command(
    profile_dir: Path | None, launcher: str, launcher_path: str | None, session_flags: str, runtime_args: str
) -> str:
    """The command a tmux window runs to start or resume a Claude session
    under profile_dir.

    launcher is "claude" or a stored, already quoted command prefix that
    runs Claude Code (such as a claude-code-router profile). Either runs
    under claude-pty-broker: discover_parent finds a live session only
    through the broker's advertisement, and the bridge's systemd PATH lacks
    ~/.local/bin, so the binaries are named absolutely. A custom launcher
    also runs under launcher_path, the PATH of the shell that spawned it,
    because its own children (node for ccr, then claude) are found by PATH."""
    flags = f"{session_flags} {runtime_args}".rstrip()
    local_bin = Path.home() / ".local" / "bin"
    # No profile_dir leaves CLAUDE_CONFIG_DIR unset, as a plain `claude` runs.
    environment = "" if profile_dir is None else f" CLAUDE_CONFIG_DIR={shlex.quote(str(profile_dir))}"
    if launcher == "claude":
        program = shlex.quote(str(local_bin / "claude"))
    else:
        program = launcher
        environment += f" PATH={shlex.quote(launcher_path)}"
    return (
        f"env{environment} "
        f"{shlex.quote(str(local_bin / 'claude-pty-broker'))} -- "
        f"{program} {flags}"
    )


class SessionNotFound(LookupError):
    """Zero live sessions are the only discovery failure a revive can fix; others need an operator."""


@dataclasses.dataclass(frozen=True)
class ParentRef:
    session_id: str
    name: str
    runtime: str
    pid: int
    proc_start: str
    entry_path: Path


class Delivery:
    def __init__(self, socket_factory=socket.socket, popen=subprocess.Popen) -> None:
        self.socket_factory = socket_factory
        self.popen = popen

    @staticmethod
    def _entry(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise LookupError(f"session registry entry is unreadable: {path}") from exc
        if not isinstance(value, dict) or value.get("pid") != int(path.stem):
            raise ValueError(f"session registry entry has invalid shape: {path}")
        return value

    def _find_session(self, session_id: str) -> tuple[Path, dict]:
        matches = []
        for path in SESSION_DIR.glob("*.json"):
            try:
                entry = self._entry(path)
            except (OSError, LookupError, ValueError):
                continue
            if entry.get("sessionId") != session_id:
                continue
            # A resumed session keeps its sessionId, so a stale registry file
            # left behind by a dead process can share it with the live one;
            # skip any entry whose process is gone or has been reused.
            try:
                _, proc_start = pty_broker.process_stat(entry.get("pid"))
            except LookupError:
                continue
            if proc_start != entry.get("procStart"):
                continue
            matches.append((path, entry))
        if len(matches) != 1:
            raise (SessionNotFound if not matches else LookupError)(
                f"expected one session {session_id}, found {len(matches)}"
            )
        return matches[0]

    @staticmethod
    def _ref(path: Path, entry: dict, runtime: str) -> ParentRef:
        required = ("sessionId", "name", "pid", "procStart")
        if not all(isinstance(entry.get(key), (str if key != "pid" else int)) for key in required):
            raise ValueError("session identity is incomplete")
        if entry.get("kind") != "interactive":
            raise ValueError("session is not interactive")
        return ParentRef(
            entry["sessionId"], entry["name"], runtime,
            entry["pid"], entry["procStart"], path,
        )

    def _read_ref(self, ref: ParentRef) -> dict:
        # name is a mutable label; sessionId + pid + procStart prove it is
        # the same process, so a rename never breaks delivery.
        entry = self._entry(ref.entry_path)
        actual = (entry.get("sessionId"), entry.get("pid"), entry.get("procStart"))
        if actual != (ref.session_id, ref.pid, ref.proc_start):
            raise LookupError("pinned session identity changed")
        return entry

    @staticmethod
    def _pty_advertisement(path: Path, value: Any) -> dict[str, Any]:
        if (
            type(value) is not dict
            or not path.stem.isdigit()
            or type(value.get("broker_pid")) is not int
            or value["broker_pid"] != int(path.stem)
            or type(value.get("socket")) is not str
            or not value["socket"]
            or type(value.get("proc_start")) is not str
            or not value["proc_start"]
            or type(value.get("child_pid")) is not int
            or type(value.get("child_proc_start")) is not str
            or not value["child_proc_start"]
            or type(value.get("conversation_id")) is not str
            or not value["conversation_id"]
        ):
            raise ValueError(f"PTY advertisement has invalid shape: {path}")
        return value

    @staticmethod
    def _wire(ref: ParentRef) -> tuple[str, str, int]:
        environment = pty_broker.process_environment(ref.pid)
        prefix = ref.runtime.upper()
        path = environment.get(f"{prefix}_PTY_BROKER_SOCKET")
        token = environment.get(f"{prefix}_PTY_BROKER_TOKEN")
        broker = environment.get(f"{prefix}_PTY_BROKER_PID")
        if not path or not token or not broker or not broker.isdigit():
            raise LookupError("session has no complete PTY broker advertisement")
        info = Path(path).stat(follow_symlinks=False)
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise PermissionError("PTY broker socket owner or type is invalid")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError("PTY broker socket mode is invalid")
        return path, token, int(broker)

    async def discover_parent(self, config: Any) -> ParentRef:
        runtime_name = getattr(config, "kind", "claude")
        spec = RUNTIMES[runtime_name]
        if spec.session_discovery_mode == "pty_advertisement":
            matches = []
            for path in STATE_DIR.glob("*.json") if STATE_DIR.is_dir() else ():
                if not path.stem.isdigit():
                    continue
                try:
                    value = json.loads(path.read_text())
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("conversation_id"), str)
                    or value["conversation_id"].lower() != str(config.session_id).lower()
                ):
                    continue
                try:
                    data = self._pty_advertisement(path, value)
                except ValueError as exc:
                    raise LookupError(f"malformed matching PTY advertisement: {path}") from exc
                matches.append((path, data))
            if len(matches) != 1:
                raise (SessionNotFound if not matches else LookupError)(
                    f"expected one live {runtime_name} session for {config.session_id}, found {len(matches)}"
                )
            path, data = matches[0]
            ref = ParentRef(
                session_id=str(config.session_id).lower(),
                name=config.name,
                runtime=runtime_name,
                pid=data["child_pid"],
                proc_start=data["child_proc_start"],
                entry_path=path,
            )
        else:
            path, entry = self._find_session(config.session_id)
            ref = self._ref(path, entry, "claude" if runtime_name in ("claude", "codex") else runtime_name)

        if ref.name != config.name:
            raise LookupError(
                f"session {config.session_id} is live as {ref.name!r}, not {config.name!r}; "
                f"run /rename {config.name} in it"
            )
        if not await self.verify_parent(ref):
            raise LookupError("registered parent session failed broker verification")
        return ref

    async def verify_parent(self, ref: ParentRef) -> bool:
        spec = RUNTIMES[ref.runtime]
        try:
            if spec.session_discovery_mode == "pty_advertisement":
                _, target_start = pty_broker.process_stat(ref.pid)
                path, _, broker = self._wire(ref)
                _, broker_start = pty_broker.process_stat(broker)
                advertisement = self._pty_advertisement(
                    ref.entry_path, json.loads(ref.entry_path.read_text())
                )
                return (
                    target_start == ref.proc_start
                    and advertisement["conversation_id"].lower() == ref.session_id.lower()
                    and advertisement["child_pid"] == ref.pid
                    and advertisement["child_proc_start"] == target_start
                    and advertisement["broker_pid"] == broker
                    and advertisement["socket"] == path
                    and advertisement["proc_start"] == broker_start
                )
            self._read_ref(ref)
            _, target_start = pty_broker.process_stat(ref.pid)
            path, _, broker = self._wire(ref)
            _, broker_start = pty_broker.process_stat(broker)
            advertisement = json.loads((STATE_DIR / f"{broker}.json").read_text())
            return (
                target_start == ref.proc_start
                and set(advertisement) == {"broker_pid", "socket", "pgid", "proc_start"}
                and advertisement["broker_pid"] == broker
                and advertisement["socket"] == path
                and advertisement["proc_start"] == broker_start
            )
        except (OSError, ValueError, LookupError, KeyError, json.JSONDecodeError):
            return False

    async def _send(self, ref: ParentRef, field: str, value: Any) -> str | None:
        if field not in {"text", "control"} or not await self.verify_parent(ref):
            raise LookupError("target is not a verified live session")

        def request() -> str | None:
            path, token, _ = self._wire(ref)
            payload = json.dumps({
                "token": token, "target_pid": ref.pid, field: value,
            }, separators=(",", ":")).encode()
            with self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(65)
                client.connect(path)
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                reply = client.recv(4096).decode("utf-8")
            if reply == "OK\n":
                return None
            if reply.startswith("OK "):
                return reply[3:].removesuffix("\n")
            raise RuntimeError(
                reply.removeprefix("ERR ").removesuffix("\n")
                or "PTY broker sent no reply"
            )
        return await asyncio.to_thread(request)

    async def spawn_fork_worker(
        self, runtime: str, parent: ParentRef, prompt: str
    ) -> "ForkWorker":
        spec = FORK_RUNTIMES[runtime]
        root = spec["root"]
        # A missing router token means every fork would talk to the wrong
        # endpoint, so fail loudly instead of launching.
        token = GEMINI_TOKEN_PATH.read_text().strip()
        if not token:
            raise RuntimeError(f"gemini router token is empty: {GEMINI_TOKEN_PATH}")
        # A fresh conversation, not a copy of the parent: the prompt already
        # carries the whole Slack thread, and a copied day-long conversation
        # (5,000+ steps) runs its turn without ever recording it, so the
        # bridge could not see the fork start. The CLI creates the
        # conversation itself; its id is read from the broker advertisement.
        argv = [
            spec["broker"], "--", spec["cli"],
            *_parent_effort(parent.pid),
            "--dangerously-skip-permissions", "-i", prompt,
        ]
        environment = dict(os.environ)
        # The bridge runs under systemd with a bare PATH; the fork must find
        # slack-send and the *-send tools in ~/.local/bin by name.
        environment["PATH"] = f"{Path.home() / '.local' / 'bin'}:{environment.get('PATH', '')}"
        environment["GOOGLE_GEMINI_BASE_URL"] = GEMINI_BASE_URL
        environment["GEMINI_API_KEY"] = token
        # The broker refuses to run without TTYs on stdin and stdout.
        master, slave = os.openpty()
        # Conversations that exist before the CLI starts belong to other
        # sessions; _relocate must never adopt one (forks that
        # did followed the live session's transcript and were
        # closed as failures while their own answers went unread).
        try:
            preexisting = frozenset(entry.name for entry in (root / "brain").iterdir())
        except OSError:
            preexisting = frozenset()
        try:
            process = self.popen(
                argv, stdin=slave, stdout=slave, stderr=slave,
                env=environment, start_new_session=True, close_fds=True,
            )
        finally:
            os.close(slave)
        drain = threading.Thread(
            target=_drain,
            args=(master, STATE_DIR / f"fork-{process.pid}.out"),
            daemon=True,
        )
        drain.start()
        worker = ForkWorker(self, runtime, None, root, process, master, 0, drain, preexisting)
        worker.prompt = prompt
        return worker

    async def inject(self, ref: ParentRef, text: str) -> None:
        await self._send(ref, "text", text)

    async def control(
        self, ref: ParentRef, command: str, argument: str | None = None
    ) -> str | None:
        payload = {"command": command}
        if argument is not None:
            payload["argument"] = argument
        return await self._send(ref, "control", payload)

    def _run_tmux(self, argv: list[str]) -> tuple[int, bytes]:
        process = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = process.communicate()
        return process.returncode, stderr

    async def revive_parent(self, config: Any) -> ParentRef:
        """Resume a dead claude session in a detached tmux window and poll
        discover_parent until it re-registers."""
        command = launch_command(
            config.profile_dir, config.launcher, config.launcher_path, f"--resume {shlex.quote(config.session_id)}", config.runtime_args
        )
        window = ["tmux", "new-window", "-d", "-n", config.name, "-c", str(config.workdir), command]
        code, stderr = await asyncio.to_thread(self._run_tmux, window)
        if code != 0 and b"no server running" in stderr.lower():
            session = [
                "tmux", "new-session", "-d", "-s", "revive", "-n", config.name,
                "-c", str(config.workdir), command,
            ]
            code, stderr = await asyncio.to_thread(self._run_tmux, session)
        if code != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "tmux exited nonzero")
        deadline = time.monotonic() + REVIVE_TIMEOUT
        while True:
            try:
                return await self.discover_parent(config)
            except SessionNotFound:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"parent did not re-register within {REVIVE_TIMEOUT:.0f}s")
                await asyncio.sleep(REVIVE_POLL_SECONDS)
            except LookupError as exc:
                raise RuntimeError(f"revived session is not addressable: {exc}") from exc


def _drain(master: int, log_path: Path) -> None:
    # The tail of a fork's terminal output is the only evidence of why it
    # died; keep the newest chunk on disk, owner-only.
    tail = b""
    try:
        while chunk := os.read(master, 65536):
            tail = (tail + chunk)[-65536:]
    except OSError:
        pass
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(mode=0o600)
        log_path.write_bytes(tail)
    except OSError:
        pass
    # This thread is the only owner of the master fd: closing it elsewhere
    # while os.read blocks here would free a descriptor still in use.
    try:
        os.close(master)
    except OSError:
        pass


def _event_since(line: str, since: str) -> dict:
    """The record on `line` when it was written at or after `since` (ISO-8601
    UTC, as agy writes `created_at`), else an empty record. Lines
    copied from the parent, or rewritten by the CLI when it loads the fork,
    are older and never count."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {}
    if not isinstance(event, dict) or str(event.get("created_at", "")) < since:
        return {}
    return event


def _turn_finished(line: str, since: str) -> bool:
    """A model turn that ended at or after `since`.

    A thinking-only step is also written as PLANNER_RESPONSE DONE without
    tool_calls, but it has no content and the CLI continues the turn by
    itself, so only a record with content ends the turn."""
    event = _event_since(line, since)
    return (
        event.get("type") == "PLANNER_RESPONSE"
        and event.get("status") == "DONE"
        and bool(event.get("content"))
        and not event.get("tool_calls")
    )


def _turn_started(line: str, since: str) -> bool:
    """The prompt sent at `since` reached the CLI: both runtimes append a
    USER_INPUT record when they accept one. A prompt the CLI drops -- it was
    still starting up, or the paste landed before its input box was ready --
    writes no such record, and no turn end can ever follow it."""
    return _event_since(line, since).get("type") == "USER_INPUT"


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ForkWorker:
    def __init__(
        self, delivery: Delivery, runtime: str, fork_id: str | None, root: Path,
        process, master: int, baseline: int, drain,
        preexisting: frozenset[str] = frozenset(),
    ) -> None:
        self.delivery = delivery
        self.runtime = runtime
        self.ref: ParentRef | None = None
        # Unknown until the CLI has created its conversation and the broker
        # advertised it (see _broker_ref). The CLI may then write its turn
        # under a second id (the advertised directory stays empty and a
        # sibling created moments later holds the transcript), so _lines
        # follows the open files to the id that has a transcript.
        self.fork_id = fork_id
        self.advertised_id: str | None = fork_id
        self.root = root
        self.process = process
        self.pid = process.pid
        self.master = master
        self.baseline = baseline
        self.since = _utc_now_iso()
        self.preexisting = preexisting
        self.prompt = ""
        self.drain = drain
        self.terminated = False

    @property
    def transcript(self) -> Path:
        return (
            self.root / "brain" / self.fork_id
            / ".system_generated" / "logs" / "transcript_full.jsonl"
        )

    def _relocate(self) -> None:
        """Point fork_id at the open conversation that has a transcript."""
        try:
            ids = open_conversation_ids(self.pid, self.root)
        except RuntimeError:
            return
        with_transcript = []
        for candidate in ids - self.preexisting:
            path = (
                self.root / "brain" / candidate
                / ".system_generated" / "logs" / "transcript_full.jsonl"
            )
            try:
                with_transcript.append((path.stat().st_mtime, candidate))
            except OSError:
                continue
        if with_transcript:
            self.fork_id = max(with_transcript)[1]

    def _lines(self) -> list[str]:
        if self.fork_id is None:
            return []
        if not self.transcript.exists():
            self._relocate()
        try:
            return self.transcript.read_text().splitlines()
        except OSError:
            return []

    def last_content(self) -> str:
        """The text of the fork's newest response with content, for the
        failure post: a fork that never posted says here what it did instead."""
        for line in reversed(self._lines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "PLANNER_RESPONSE" and event.get("content"):
                return str(event["content"])
        return ""

    async def _broker_ref(self) -> ParentRef:
        """The fork's own broker, addressed like any other live session.

        The fork is launched as the broker itself, so it advertises under its
        own pid; the advertised conversation id is what verify_parent checks.
        """
        if self.ref is not None:
            return self.ref
        path = STATE_DIR / f"{self.pid}.json"
        deadline = time.monotonic() + FORK_READY_TIMEOUT
        while True:
            if self.process.poll() is not None:
                raise RuntimeError(f"fork {self.fork_id} exited before advertising a broker")
            try:
                data = Delivery._pty_advertisement(path, json.loads(path.read_text()))
            except (OSError, ValueError, json.JSONDecodeError):
                data = None
            if data is not None:
                if self.fork_id is None:
                    self.fork_id = data["conversation_id"].lower()
                    self.advertised_id = self.fork_id
                ref = ParentRef(
                    session_id=data["conversation_id"].lower(),
                    name=self.fork_id,
                    runtime=self.runtime,
                    pid=data["child_pid"],
                    proc_start=data["child_proc_start"],
                    entry_path=path,
                )
                if await self.delivery.verify_parent(ref):
                    self.ref = ref
                    return ref
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"fork {self.fork_id} did not advertise a usable broker "
                    f"within {FORK_READY_TIMEOUT}s"
                )
            await asyncio.sleep(0.2)

    async def inject(self, text: str) -> None:
        ref = await self._broker_ref()
        # The next turn starts here, so DONE lines already on disk are old.
        self.baseline = len(self._lines())
        self.since = _utc_now_iso()
        self.prompt = text
        await self.delivery.inject(ref, text)

    async def interrupt(self) -> None:
        await self.delivery.control(await self._broker_ref(), "stop")

    async def wait_done(self, timeout: float) -> None:
        await self._broker_ref()  # learns the conversation id on first use
        deadline = time.monotonic() + timeout
        start_deadline = time.monotonic() + FORK_START_TIMEOUT
        resent = False
        while True:
            lines = self._lines()
            if any(_turn_finished(line, self.since) for line in lines):
                self.since = _utc_now_iso()
                await asyncio.sleep(1.0)
                return
            if time.monotonic() >= start_deadline and not any(
                _turn_started(line, self.since) for line in lines
            ):
                if not resent:
                    # The CLI drops a prompt now and then (seen 2 in 12
                    # launches on wsl): send it once more before giving up.
                    LOG.warning("fork %s did not take its prompt; resending", self.fork_id)
                    await self.inject(self.prompt)
                    resent = True
                    start_deadline = time.monotonic() + FORK_START_TIMEOUT
                    continue
                raise TimeoutError(
                    f"fork {self.fork_id} never started a turn: the prompt sent at "
                    f"{self.since} left no USER_INPUT record within {FORK_START_TIMEOUT}s"
                )
            if self.process.poll() is not None:
                raise RuntimeError(f"fork {self.fork_id} exited before finishing its turn")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"fork {self.fork_id} did not finish within {timeout}s")
            await asyncio.sleep(0.2)

    async def terminate(self, keep_transcript: bool = False) -> None:
        """Stop the fork. With keep_transcript the conversation stays on disk
        so a fork that never posted can be read afterwards."""
        from slack_fork import delete_fork

        if self.terminated:
            return
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        else:
            deadline = time.monotonic() + 5.0
            while self.process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if self.process.poll() is None:
                try:
                    os.killpg(os.getpgid(self.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        # The child's exit closes the slave, so the drain read fails with EIO,
        # writes the tail, and closes the master itself.
        self.drain.join(timeout=5.0)
        self.terminated = True
        if keep_transcript:
            LOG.warning("fork %s kept for inspection: %s", self.fork_id, self.transcript)
            return
        from slack_fork import MARKER

        for conversation in {self.fork_id, self.advertised_id} - {None}:
            brain = self.root / "brain" / conversation
            if not brain.is_dir():
                continue
            # The conversation is the bridge's: mark it so delete_fork
            # accepts it, then remove it.
            (brain / MARKER).write_text("fresh\n")
            delete_fork(self.root, conversation)
