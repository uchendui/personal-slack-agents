#!/usr/bin/env python3
"""Run an interactive command behind an authenticated PTY injection socket.

This module is the runtime-neutral core. It owns the PTY, the authenticated
Unix socket, target validation, and bracketed-paste injection, and it knows
nothing about any particular command-line agent. Each runtime supplies a
Profile naming its program, its state and session directories, its three
environment variable names, and the control commands it has actually been
verified to accept. Nothing is inferred and nothing falls back to a default:
an incomplete or invalid Profile raises before the broker starts.
"""

import argparse
import atexit
import ctypes
import dataclasses
import datetime
import errno
import fcntl
import hmac
import json
import os
import pty
import re
import secrets
import shutil
import select
import signal
import socket
import stat
import struct
import sys
import tempfile
import termios
import threading
import time
import traceback
import tty
import string
import types
import typing
from collections import deque
from pathlib import Path

MAX_MESSAGE_BYTES = 128 * 1024
# How often the watcher asks whether this session has a conversation yet. It
# runs for the life of a session that has not been spoken to, so it is a
# directory read every second rather than a busy loop.
CONVERSATION_POLL_SECONDS = 1.0
MAX_REQUEST_BYTES = MAX_MESSAGE_BYTES * 6 + 4096
REQUEST_TIMEOUT_SECONDS = 60
CONFIRM_DELAY_SECONDS = 0.6
RESUME_SETTLE_SECONDS = 2.0
RESUME_REGISTRY_POLL_SECONDS = 0.5
SWAP_TERMINATE_SECONDS = 5.0
LIMIT_POLL_SECONDS = 1.0
LOGIN_RETRY_SECONDS = 3600.0
ACKNOWLEDGMENT_SECONDS = 5.0
IDLE_FLUSH_SECONDS = 45
IDLE_COMPACT_REGISTRY_POLL_SECONDS = 1.0
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
AGENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
# Strip CSI, OSC, charset selects, and CR because Ink wraps the acknowledgment
# in SGR codes, so a raw substring match fails.
TERMINAL_ESCAPE_RE = re.compile(
    rb"\x1b\[[\x20-\x3f]*[\x40-\x7e]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\([\x20-\x7e]|\r"
)
# Reported for a runtime whose profile states it keeps no session registry.
NO_SESSION_REGISTRY_STATUS = "unregistered"


def process_stat(pid: int) -> tuple[int, str]:
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError as exc:
        raise LookupError(f"process {pid} is not live") from exc
    closing_paren = raw.rfind(")")
    fields = raw[closing_paren + 2 :].split()
    if closing_paren < 0 or len(fields) <= 19:
        raise RuntimeError(f"cannot parse /proc/{pid}/stat")
    return int(fields[1]), fields[19]


def process_environment(pid: int) -> dict[str, str]:
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError as exc:
        raise LookupError(f"cannot read environment for process {pid}") from exc
    result = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator:
            result[key.decode(errors="surrogateescape")] = value.decode(
                errors="surrogateescape"
            )
    return result


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def set_parent_death_signal() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGHUP, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() == 1:
        raise RuntimeError("launcher parent exited before PTY broker initialization")


def normalize_injected_text(text: str) -> bytes:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "".join(
        character
        if character in ("\n", "\t")
        or (ord(character) >= 32 and not 127 <= ord(character) <= 159)
        else " "
        for character in text
    )
    if normalized.lstrip().startswith("/"):
        normalized = chr(0x2060) + normalized  # Prevent slash-command dispatch.
    encoded = normalized.encode("utf-8")
    if not encoded:
        raise ValueError("message text is empty")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} bytes")
    return encoded


@dataclasses.dataclass(frozen=True)
class Control:
    """One control command a runtime has been verified to accept.

    keystrokes is None for a control that only proves the session is
    reachable and types nothing at all. A str is formatted with the
    validated argument; bytes are sent exactly as given. argument is None
    when the command takes no argument, a tuple of the exact accepted
    values, or a compiled pattern the argument must fullmatch. raw sends
    the keystrokes without the bracketed-paste wrapper and without the
    trailing Enter, which is what a bare control byte such as Escape needs.
    confirm is an extra raw keystroke sent CONFIRM_DELAY_SECONDS after the
    command, for a control whose CLI answers with a confirmation dialog.
    acknowledgment is terminal text required after the control is typed.
    """

    keystrokes: str | bytes | None = None
    argument: tuple[str, ...] | re.Pattern | None = None
    raw: bool = False
    confirm: bytes | None = None
    acknowledgment: str | None = None

    def __post_init__(self) -> None:
        if self.keystrokes is not None and not isinstance(
            self.keystrokes, (str, bytes)
        ):
            raise TypeError("control keystrokes must be str, bytes, or None")
        if self.acknowledgment is not None and not isinstance(self.acknowledgment, str):
            raise TypeError("control acknowledgment must be str or None")
        if self.acknowledgment is not None and self.keystrokes is None:
            raise ValueError("an acknowledgment requires control keystrokes")
        if self.argument is not None and not isinstance(
            self.argument, (tuple, re.Pattern)
        ):
            raise TypeError(
                "control argument must be a tuple, a compiled pattern, or None"
            )
        if isinstance(self.argument, tuple) and not all(
            isinstance(value, str) and value for value in self.argument
        ):
            raise ValueError("control argument values must be non-empty strings")
        for label, template in (
            ("keystrokes", self.keystrokes),
            ("acknowledgment", self.acknowledgment),
        ):
            if not isinstance(template, str):
                continue
            # Parse rather than substring-match. "{{argument}}" is an escaped
            # literal and interpolates nothing, "{argument!r}" and
            # "{argument:>8}" would reshape the validated value on its way to
            # the terminal, and a second field would either duplicate it or
            # raise at injection time, when the request is already accepted.
            fields = [
                (name, spec, conversion)
                for _, name, spec, conversion in string.Formatter().parse(template)
                if name is not None
            ]
            if any(
                name != "argument" or spec not in ("", None) or conversion is not None
                for name, spec, conversion in fields
            ):
                raise ValueError(
                    f"control {label} may interpolate only a plain {{argument}}"
                )
            if len(fields) > 1:
                raise ValueError(
                    f"control {label} must interpolate {{argument}} at most once"
                )
            if label == "keystrokes" and bool(fields) != (self.argument is not None):
                raise ValueError(
                    f"control {label} must interpolate {{argument}} exactly when "
                    "the control takes an argument"
                )
            if label == "acknowledgment" and fields and self.argument is None:
                raise ValueError(
                    "control acknowledgment may interpolate {argument} only when "
                    "the control takes an argument"
                )
        if not isinstance(self.keystrokes, str) and self.argument is not None:
            raise ValueError("only str keystrokes can carry an argument")
        if self.raw and self.keystrokes is None:
            raise ValueError("a control that types nothing cannot be raw")

    def render(self, argument: str | None) -> bytes | None:
        if self.keystrokes is None:
            return None
        if isinstance(self.keystrokes, bytes):
            return self.keystrokes
        return self.keystrokes.format(argument=argument).encode("utf-8")

    def render_acknowledgment(self, argument: str | None) -> bytes | None:
        if self.acknowledgment is None:
            return None
        return self.acknowledgment.format(argument=argument).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class AccountCycle:
    config_env: str
    cycle_env: str
    limit_re: re.Pattern[str]
    resume_flag: str

    def __post_init__(self) -> None:
        if not isinstance(self.limit_re, re.Pattern) or not isinstance(
            self.limit_re.pattern, str
        ):
            raise ValueError("account cycle limit_re must be a compiled string pattern")
        for name in (self.config_env, self.cycle_env):
            if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"account cycle environment name is not usable: {name!r}")
        if self.config_env == self.cycle_env:
            raise ValueError("account cycle environment names must be distinct")


@dataclasses.dataclass(frozen=True, eq=False)
class Profile:
    """Everything a runtime must state before the broker will run it.

    program names the adapter for journal lines and fatal errors. state_dir
    holds the socket, advertisement, and journal for each live broker, and
    is what the Slack bridge scans to find one. The three environment names
    are stamped into the child so a request can prove it is talking about
    that exact child.

    conversation_discovery belongs to a runtime whose sessions are identified
    by a conversation id that does not exist when the command starts. It is
    asked, repeatedly and off the input path, whether the launched process has
    one yet; the first id it returns is advertised and nothing is advertised
    before then. Startup never waits for it, because the person at the terminal
    is what creates the conversation in the first place.

    session_dir holds the runtime's own per-pid session registry, which the
    broker reads to report the target's status. Not every runtime keeps one:
    Claude Code writes a sessions entry per live session, while Antigravity
    keeps its state elsewhere and writes nothing the broker can read. A runtime without a registry must say so by passing
    session_dir=None, and it then reports NO_SESSION_REGISTRY_STATUS. That is
    a declaration, not a fallback: a profile that names a session_dir still
    fails loudly when the entry for a target is missing, which is what
    catches a request aimed at the wrong process. Status is reported, never
    trusted — a target is authorized by validate_target_environment, which
    checks descent, the three stamped environment values, and PTY foreground
    ownership, none of which the registry participates in.

    account_cycle lets a runtime resume the same registered session under the
    next configured login when its terminal reports a usage limit. It requires
    session_dir because the registry supplies the session id used to resume.
    """

    program: str
    state_dir: Path
    session_dir: Path | None
    socket_env: str
    token_env: str
    pid_env: str
    controls: dict
    required_command: str | None = None
    identity_env: str | None = None
    conversation_discovery: typing.Callable[[int], str | None] | None = None
    idle_compact_seconds: float | None = None
    account_cycle: AccountCycle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.program, str) or not self.program:
            raise ValueError("profile program must be a non-empty name")
        if not isinstance(self.state_dir, Path) or not self.state_dir.is_absolute():
            raise ValueError("profile state_dir must be an absolute Path")
        if self.session_dir is not None and (
            not isinstance(self.session_dir, Path) or not self.session_dir.is_absolute()
        ):
            raise ValueError(
                "profile session_dir must be an absolute Path, or None when the "
                "runtime keeps no session registry"
            )
        if self.account_cycle is not None:
            if not isinstance(self.account_cycle, AccountCycle):
                raise TypeError("profile account_cycle must be an AccountCycle")
            if self.session_dir is None:
                raise ValueError("a profile with an account cycle must name a session_dir")
        environment_names = (self.socket_env, self.token_env, self.pid_env)
        for name in environment_names:
            if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"profile environment name is not usable: {name!r}")
        if len(set(environment_names)) != len(environment_names):
            raise ValueError("profile environment names must be distinct")
        if not isinstance(self.controls, (dict, types.MappingProxyType)) or not self.controls:
            raise ValueError("profile must declare at least one control")
        for command, control in self.controls.items():
            if not isinstance(command, str) or not command:
                raise ValueError("control names must be non-empty strings")
            if not isinstance(control, Control):
                raise TypeError(f"control {command!r} is not a Control")
        if self.identity_env is not None and (
            not isinstance(self.identity_env, str)
            or not ENV_NAME_RE.fullmatch(self.identity_env)
            or self.identity_env in environment_names
        ):
            raise ValueError(
                "profile identity_env must be a distinct environment variable "
                "name, or None when the runtime keeps its own session registry"
            )
        if (
            self.session_dir is None
            and self.identity_env is None
            and self.conversation_discovery is None
        ):
            raise ValueError(
                "a profile with no session registry must name an identity_env or "
                "conversation_discovery, or nothing can discover its sessions"
            )
        if self.required_command is not None and (
            not isinstance(self.required_command, str)
            or not Path(self.required_command).is_absolute()
        ):
            raise ValueError(
                "profile required_command must be an absolute path, or None when "
                "the runtime accepts any command"
            )
        if self.idle_compact_seconds is not None:
            if (
                not isinstance(self.idle_compact_seconds, float)
                or not float("-inf") < self.idle_compact_seconds < float("inf")
                or self.idle_compact_seconds <= 0
            ):
                raise ValueError(
                    "profile idle_compact_seconds must be a positive finite float, "
                    "or None to disable idle compaction"
                )
            compact = self.controls.get("compact")
            if compact is None or compact.raw or compact.render(None) is None:
                raise ValueError(
                    "a profile with idle compaction must declare a text compact control"
                )
        # A profile is a security statement, so the caller must not be able to
        # add a control to it after it has been validated.
        object.__setattr__(self, "controls", types.MappingProxyType(dict(self.controls)))


def resolve_control(control: dict, controls: dict) -> tuple["Control", bytes | None]:
    """Return the validated Control and its keystrokes for one request.

    The caller needs both the bytes and how to frame them, and reading the
    framing from a second lookup would let a request validated against one
    control be framed by another. They come from the same validated spec.
    """
    if not isinstance(control, dict):
        raise ValueError("control must be a JSON object")
    command = control.get("command")
    spec = controls.get(command) if isinstance(command, str) else None
    if spec is None:
        raise ValueError(
            "control command must be one of: " + ", ".join(sorted(controls))
        )
    if spec.argument is None:
        if set(control) != {"command"}:
            raise ValueError(f"{command} control accepts no other fields")
        return spec, spec.render(None)
    if set(control) != {"command", "argument"}:
        raise ValueError(f"{command} control requires only command and argument")
    argument = control.get("argument")
    if not isinstance(argument, str):
        raise ValueError(f"{command} control has an invalid argument")
    if isinstance(spec.argument, tuple):
        accepted = argument in spec.argument
    else:
        accepted = bool(spec.argument.fullmatch(argument))
    if not accepted:
        raise ValueError(f"{command} control has an invalid argument")
    return spec, spec.render(argument)


def normalize_control(control: dict, controls: dict) -> bytes | None:
    """Validate one control request and return only its keystrokes."""
    return resolve_control(control, controls)[1]


class Broker:
    def __init__(self, command: list[str], profile: Profile):
        if not isinstance(profile, Profile):
            raise TypeError("broker requires an explicit Profile")
        self.command = command
        self.profile = profile
        self.conversation_id = None
        self.pending_advertisement = None
        self.watcher_thread = None
        self.pid = os.getpid()
        self.socket_path = profile.state_dir / f"{self.pid}.sock"
        self.advertisement_path = profile.state_dir / f"{self.pid}.json"
        self.journal_path = profile.state_dir / f"{self.pid}.log"
        self.token = secrets.token_hex(16)
        self.listener = None
        self.child_pid = None
        self.child_pgid = None
        self.master_fd = None
        self.saved_termios = None
        self.stop = threading.Event()
        self.master_write_lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.journal_lock = threading.Lock()
        self.output_condition = threading.Condition()
        # None means no capture is in progress.
        self.output_capture = None
        self.last_output_at = 0.0
        self.last_limit_at = time.time()
        self.limit_transcript_path = None
        self.transcript_stat_cache = None
        self.last_limit_watch_error = None
        self.environment_overrides: dict[str, str] = {}
        self.tried_logins: dict[str, float] = {}
        self.account_cycle_exhausted = False
        self.swapping = False
        self.pending_input_bytes = 0
        self.input_escape = bytearray()
        self.in_paste = False
        self.last_input_at = time.monotonic()
        self.compact_armed = False
        self.next_idle_compact_check_at = 0.0
        self.deferred_injections = deque()
        self.child_status = None
        self.initialized = False
        self.injector_thread = None
        self.output_thread = None
        self.watcher_thread = None
        self.limit_thread = None
        self.agent_name = None

    def journal(self, message: str, to_terminal: bool = True) -> None:
        """Record one event, on stderr as well unless it would be noise.

        stderr is the user's terminal, and during a session that terminal is
        raw and being drawn by a full-screen program, so anything written there
        lands on top of what they are reading. Failures earn that; routine
        progress does not.
        """
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}"
        with self.journal_lock:
            if to_terminal:
                print(f"{self.profile.program}: {message}", file=sys.stderr, flush=True)
            try:
                descriptor = os.open(
                    self.journal_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    os.write(descriptor, (line + "\n").encode("utf-8"))
                finally:
                    os.close(descriptor)
            except OSError as exc:
                print(
                    f"{self.profile.program}: journal write failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def setup_state_dir(self) -> None:
        state_dir = self.profile.state_dir
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = state_dir.stat()
        if state.st_uid != os.getuid():
            raise RuntimeError(f"state directory has wrong owner: {state_dir}")
        if stat.S_IMODE(state.st_mode) != 0o700:
            state_dir.chmod(0o700)
        self.sweep_stale()

    def sweep_stale(self) -> None:
        for advertisement in self.profile.state_dir.glob("*.json"):
            if not advertisement.stem.isdigit():
                continue
            broker_pid = int(advertisement.stem)
            stale = True
            try:
                data = json.loads(advertisement.read_text())
                _, actual_start = process_stat(broker_pid)
                stale = (
                    data.get("broker_pid") != broker_pid
                    or str(data.get("proc_start")) != actual_start
                )
            except (OSError, ValueError, LookupError, json.JSONDecodeError):
                stale = True
            if stale:
                advertisement.unlink(missing_ok=True)
                (self.profile.state_dir / f"{broker_pid}.sock").unlink(missing_ok=True)

    def setup_listener(self) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.listener.listen(8)
        self.listener.settimeout(0.2)

    def resolve_identity(self) -> None:
        """Read and validate the agent identity this launcher was told to run as.

        Only a runtime without its own session registry needs this. The value
        has to be right before anything is advertised, because the whole point
        of putting it in the advertisement is that its presence is proof the
        launcher supplied a valid one. A missing or malformed value kills the
        broker rather than producing an entry nothing can trust.
        """
        if self.profile.identity_env is None:
            return
        name = os.environ.get(self.profile.identity_env)
        if not name:
            raise RuntimeError(
                f"{self.profile.identity_env} is not set; the launcher must set it "
                "to this session's registered agent name"
            )
        if not AGENT_NAME_RE.fullmatch(name):
            raise RuntimeError(
                f"{self.profile.identity_env} is not a usable agent name: {name!r}"
            )
        self.agent_name = name

    def watch_for_conversation(self) -> None:
        """Advertise this session once its conversation exists, and not before.

        Runs off the input path for the life of the session. The runtime has no
        conversation until the person at the terminal sends their first prompt,
        so there is nothing to wait for at startup and waiting would only hold
        the terminal hostage; the relay is already running while this looks.

        The first id found is published and the watch ends. Two ids at once is
        ambiguity, and a broker that cannot say which session it is must not
        say anything: it journals the failure and publishes nothing, leaving
        the terminal working and the session simply not reachable from Slack,
        which is what /slack then reports.
        """
        discover = self.profile.conversation_discovery
        while not self.stop.wait(CONVERSATION_POLL_SECONDS):
            if self.child_pid is None:
                continue
            try:
                conversation_id = discover(self.child_pid)
            except Exception as exc:
                self.journal(
                    "conversation discovery failed, so this session is not "
                    f"advertised: {exc}"
                )
                return
            if conversation_id is None:
                continue
            try:
                self.publish_conversation(conversation_id)
            except Exception as exc:
                # Publishing is the last step and it can fail on its own: this
                # thread is a daemon, so an escaping exception would print a
                # traceback over the raw terminal and leave no journal line,
                # which is the opposite of the promise made above.
                self.journal(
                    f"conversation {conversation_id} was discovered but could "
                    f"not be advertised, so this session is not reachable from "
                    f"Slack: {exc}"
                )
            return

    def publish_conversation(self, conversation_id: str) -> None:
        """Write the advertisement for a session that now has an identity."""
        advertisement = dict(self.pending_advertisement or {})
        if not advertisement:
            self.journal(
                "no advertisement was prepared, so conversation "
                f"{conversation_id} is not published"
            )
            return
        advertisement["conversation_id"] = conversation_id
        self.conversation_id = conversation_id
        atomic_write(
            self.advertisement_path,
            json.dumps(advertisement, separators=(",", ":")) + "\n",
        )
        # Not to the terminal: this happens moments after the user's first
        # prompt, while the runtime is drawing its own screen there.
        self.journal(f"advertised conversation {conversation_id}", to_terminal=False)

    def validate_command(self) -> None:
        """Refuse to launch anything but the executable this profile allows."""
        required = self.profile.required_command
        if required is None:
            return
        resolved = shutil.which(self.command[0])
        if resolved is None:
            raise RuntimeError(f"command is not executable: {self.command[0]!r}")
        if os.path.realpath(resolved) != os.path.realpath(required):
            raise RuntimeError(
                f"{self.profile.program} may launch only {required}, not "
                f"{os.path.realpath(resolved)}"
            )

    def spawn_child(self) -> None:
        child_pid, master_fd = pty.fork()
        if child_pid == 0:
            environment = os.environ.copy()
            environment[self.profile.socket_env] = str(self.socket_path)
            environment[self.profile.token_env] = self.token
            environment[self.profile.pid_env] = str(self.pid)
            environment.update(self.environment_overrides)
            try:
                os.execvpe(self.command[0], self.command, environment)
            except OSError as exc:
                print(
                    f"{self.profile.program}: FATAL: cannot exec {self.command[0]!r}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                os._exit(127)
        self.child_pid = child_pid
        self.master_fd = master_fd
        self.child_pgid = os.getpgid(child_pid)
        _, broker_start = process_stat(self.pid)
        advertisement = {
            "broker_pid": self.pid,
            "socket": str(self.socket_path),
            "pgid": self.child_pgid,
            "proc_start": broker_start,
        }
        if self.agent_name is not None or self.profile.conversation_discovery is not None:
            # A runtime with no session registry is discoverable only through
            # this file, so it carries the identity and the means to detect a
            # recycled child pid. A runtime that has a registry writes none of
            # these, and their absence is what says "ask the registry".
            _, child_start = process_stat(child_pid)
            if self.agent_name is not None:
                advertisement["agent_name"] = self.agent_name
            advertisement["kind"] = "interactive"
            advertisement["child_pid"] = child_pid
            advertisement["child_proc_start"] = child_start
            advertisement["token"] = self.token
        if self.profile.conversation_discovery is not None:
            # Held, not written. This session has no conversation until its
            # first prompt creates one, and an advertisement is what the bridge
            # delivers by: publishing one now would offer a session that cannot
            # be addressed, and filling in an id later means guessing which.
            # The watcher publishes this exact entry once discovery answers.
            self.pending_advertisement = advertisement
            return
        atomic_write(
            self.advertisement_path,
            json.dumps(advertisement, separators=(",", ":")) + "\n",
        )

    def set_terminal_raw(self) -> None:
        if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
            raise RuntimeError("interactive stdin and stdout TTYs are required")
        self.saved_termios = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())
        self.resize_child()

    def restore_terminal(self) -> None:
        if self.saved_termios is None:
            return
        try:
            termios.tcsetattr(
                sys.stdin.fileno(), termios.TCSAFLUSH, self.saved_termios
            )
        except termios.error:
            pass
        self.saved_termios = None

    def resize_child(self) -> None:
        if self.master_fd is None or not os.isatty(sys.stdin.fileno()):
            return
        try:
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)
        except OSError:
            if not self.stop.is_set():
                raise

    def install_signal_handlers(self) -> None:
        def resize(_signum, _frame):
            self.resize_child()

        def terminate(signum, _frame):
            self.stop.set()
            self.terminate_child(signum)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGWINCH, resize)
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGHUP, terminate)
        atexit.register(self.restore_terminal)

    def terminate_child(self, signum: int = signal.SIGTERM) -> None:
        if self.child_pgid is None:
            return
        try:
            os.killpg(self.child_pgid, signum)
        except ProcessLookupError:
            pass

    def write_master(self, data: bytes, deadline: float | None = None) -> None:
        if self.master_fd is None:
            raise RuntimeError("PTY master is unavailable")
        view = memoryview(data)
        with self.master_write_lock:
            while view:
                if self.stop.is_set():
                    raise RuntimeError("broker stopped during PTY write")
                timeout = 0.2
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "PTY write deadline expired before buffer drained"
                        )
                    timeout = min(timeout, remaining)
                _, writable, _ = select.select(
                    [], [self.master_fd], [], timeout
                )
                if not writable:
                    continue
                try:
                    written = os.write(self.master_fd, view)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                        continue
                    raise
                if written <= 0:
                    raise RuntimeError("PTY write made no progress")
                view = view[written:]

    def capture_output(self, data: bytes) -> None:
        with self.output_condition:
            self.last_output_at = time.monotonic()
            if self.output_capture is not None:
                self.output_capture.extend(data)
                # Keep a 64 KiB tail to bound memory while the renderer redraws.
                del self.output_capture[:-65536]
            self.output_condition.notify_all()

    def output_fd_replaced(self, fd: int) -> bool:
        with self.output_condition:
            while self.swapping and not self.stop.is_set():
                self.output_condition.wait()
            if self.master_fd == fd:
                return False
        os.close(fd)
        return True

    def output_loop(self) -> None:
        while not self.stop.is_set():
            fd = self.master_fd
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                # A child's exit during a swap raises EIO; pick up the new PTY.
                if self.output_fd_replaced(fd):
                    continue
                if exc.errno != 5 and not self.stop.is_set():
                    print(f"{self.profile.program}: PTY read failed: {exc}", file=sys.stderr)
                break
            if not data:
                if self.output_fd_replaced(fd):
                    continue
                break
            self.capture_output(data)
            view = memoryview(data)
            while view:
                try:
                    written = os.write(sys.stdout.fileno(), view)
                except OSError:
                    self.stop.set()
                    return
                view = view[written:]
        self.stop.set()

    def limit_watch(self) -> None:
        while not self.stop.wait(LIMIT_POLL_SECONDS):
            try:
                session_id = self.session_id_for_child()
                if session_id is not None and (
                    self.account_cycle_exhausted or self.limit_confirmed(session_id)
                ):
                    self.swap_login(session_id)
            except Exception as exc:
                message = f"login cycle check failed: {exc}"
                if message != self.last_limit_watch_error:
                    self.journal(message, to_terminal=False)
                    self.last_limit_watch_error = message

    def session_id_for_child(self) -> str | None:
        registry_path = self.profile.session_dir / f"{self.child_pid}.json"
        try:
            with registry_path.open() as registry_file:
                return json.load(registry_file).get("sessionId")
        except (OSError, json.JSONDecodeError):
            return None

    def limit_confirmed(self, session_id: str) -> bool:
        account_cycle = self.profile.account_cycle
        login = self.environment_overrides.get(account_cycle.config_env) or os.environ.get(
            account_cycle.config_env
        )
        if login is None:
            return False
        transcript = self.limit_transcript_path
        if transcript is None:
            transcript = next(
                Path(login).glob(f"projects/*/{session_id}.jsonl"),
                None,
            )
            if transcript is None:
                return False
            self.limit_transcript_path = transcript
        transcript_stat = transcript.stat()
        stat_key = (transcript_stat.st_size, transcript_stat.st_mtime_ns)
        if stat_key == self.transcript_stat_cache:
            return False
        self.transcript_stat_cache = stat_key
        with transcript.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            start = max(0, stream.tell() - 65536)
            stream.seek(start)
            data = stream.read()
        if start:
            # Drop the partial record cut at the 64 KiB seek point.
            _, _, data = data.partition(b"\n")
        for line in reversed(data.split(b"\n")):
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A tail record still being written is retried on the next poll.
                continue
            if record.get("type") not in ("assistant", "user"):
                continue
            if record.get("type") != "assistant" or not record.get("isApiErrorMessage"):
                return False
            content = record.get("message", {}).get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                return False
            try:
                timestamp = datetime.datetime.fromisoformat(
                    record["timestamp"].replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError):
                return False
            # The transcript record is the only signal that is not repainted.
            return bool(
                account_cycle.limit_re.search(text)
                and timestamp > self.last_limit_at
            )
        return False

    def swap_login(self, session_id: str) -> bool:
        account_cycle = self.profile.account_cycle
        cycle_value = os.environ.get(account_cycle.cycle_env, "")
        cycle = [entry for entry in cycle_value.split(":") if entry]
        current = self.environment_overrides.get(account_cycle.config_env) or os.environ.get(
            account_cycle.config_env
        )
        if not cycle:
            self.journal(f"{account_cycle.cycle_env} is unset; cannot cycle login")
            return False
        if current not in cycle:
            self.journal(
                f"current login {current} is not in {account_cycle.cycle_env}; cannot cycle login"
            )
            return False
        now = time.monotonic()
        self.tried_logins = {
            login: tried_at
            for login, tried_at in self.tried_logins.items()
            if now - tried_at < LOGIN_RETRY_SECONDS
        }
        current_index = cycle.index(current)
        candidates = cycle[current_index + 1 :] + cycle[:current_index]
        next_login = next(
            (login for login in candidates if login not in self.tried_logins),
            None,
        )
        if next_login is None:
            if not self.account_cycle_exhausted:
                self.journal(
                    f"every login in {account_cycle.cycle_env} has hit its limit; "
                    f"staying on {current}"
                )
            self.account_cycle_exhausted = True
            return False
        self.account_cycle_exhausted = False

        child_pid = self.child_pid

        command = []
        index = 0
        while index < len(self.command):
            argument = self.command[index]
            if argument in (account_cycle.resume_flag, "-r", "--session-id"):
                has_value = (
                    index + 1 < len(self.command)
                    and not self.command[index + 1].startswith("-")
                )
                index += 2 if has_value else 1
                continue
            if argument.startswith(
                (f"{account_cycle.resume_flag}=", "--session-id=")
            ) or argument in ("--continue", "-c"):
                index += 1
                continue
            # A positional prompt is re-submitted on resume and accepted by the CLI.
            command.append(argument)
            index += 1
        command.extend((account_cycle.resume_flag, session_id))

        # The locks keep an injection and its Enter on one PTY during replacement.
        with self.input_lock, self.master_write_lock:
            with self.output_condition:
                self.swapping = True
            try:
                self.terminate_child(signal.SIGTERM)
                terminate_deadline = time.monotonic() + SWAP_TERMINATE_SECONDS
                while True:
                    waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
                    if waited_pid == child_pid:
                        break
                    if time.monotonic() >= terminate_deadline:
                        self.terminate_child(signal.SIGKILL)
                        os.waitpid(child_pid, 0)
                        break
                    time.sleep(0.1)
                self.terminate_child(signal.SIGKILL)
                self.child_status = None
                # The draft died with the old PTY.
                self.pending_input_bytes = 0
                self.in_paste = False
                self.input_escape.clear()
                self.tried_logins[current] = time.monotonic()
                self.environment_overrides[account_cycle.config_env] = next_login
                self.command = command
                self.last_output_at = 0.0
                self.last_limit_at = time.time()
                self.limit_transcript_path = None
                self.transcript_stat_cache = None
                self.journal(
                    f"usage limit under {current}; resuming session {session_id} under {next_login}"
                )
                self.spawn_child()
                self.resize_child()
            finally:
                with self.output_condition:
                    self.swapping = False
                    self.output_condition.notify_all()

        threading.Thread(
            target=self.announce_swap,
            args=(current, next_login),
            daemon=True,
        ).start()
        return True

    def announce_swap(self, previous: str, current: str) -> None:
        child_pid = self.child_pid
        registry_path = self.profile.session_dir / f"{child_pid}.json"
        with self.output_condition:
            # The registry file proves the CLI reached its prompt past any startup dialog.
            while not registry_path.exists() and not self.stop.is_set():
                self.output_condition.wait(RESUME_REGISTRY_POLL_SECONDS)
            # swap_login resets last_output_at so settling starts after the new CLI speaks.
            while not self.stop.is_set():
                now = time.monotonic()
                if self.last_output_at and now - self.last_output_at >= RESUME_SETTLE_SECONDS:
                    break
                wait_for = RESUME_SETTLE_SECONDS
                if self.last_output_at:
                    wait_for -= now - self.last_output_at
                self.output_condition.wait(wait_for)
            if self.stop.is_set() or self.child_pid != child_pid:
                return
        text = (
            f"Login {Path(previous).name} reached its usage limit, so this session was "
            f"resumed under {Path(current).name}. The message that hit the limit was not "
            "answered. Continue the task you were on from where it stopped."
        )
        encoded = normalize_injected_text(text)
        with self.input_lock:
            if self.child_pid != child_pid:
                return
            self._write_text_injection(encoded)
        self.journal(f"announced login swap to {current}", to_terminal=False)

    def suspend(self) -> None:
        self.restore_terminal()
        if self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        # SIGSTOP works even though forkpty process groups are orphaned for
        # job-control purposes; SIGTSTP can be discarded by the kernel.
        os.kill(self.pid, signal.SIGSTOP)
        if self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        self.set_terminal_raw()

    def _track_user_data(self, data: bytes) -> None:
        # Only typed bytes count as activity: terminal replies (cursor
        # reports, focus events) arrive on stdin too and must not keep a
        # deferred injection waiting forever.
        for index, byte in enumerate(data):
            if not self.input_escape and byte != 0x1B:
                self.last_input_at = time.monotonic()
            if self.input_escape:
                self.input_escape.append(byte)
                if len(self.input_escape) == 2 and byte not in (ord("["), ord("O")):
                    self.input_escape.clear()
                elif len(self.input_escape) >= 3 and 0x40 <= byte <= 0x7E:
                    sequence = bytes(self.input_escape)
                    if sequence == BRACKETED_PASTE_START:
                        self.in_paste = True
                    elif sequence == BRACKETED_PASTE_END:
                        self.in_paste = False
                    self.input_escape.clear()
                continue
            if byte == 0x1B:
                if index + 1 < len(data):
                    self.input_escape.append(byte)
                else:
                    self.pending_input_bytes = 0
            elif byte in (0x0D, 0x0A):
                if self.in_paste:
                    self.pending_input_bytes += 1
                else:
                    self.pending_input_bytes = 0
                    self.compact_armed = True
            elif byte in (0x03, 0x15):
                self.pending_input_bytes = 0
                self.in_paste = False
            elif byte in (0x08, 0x7F):
                self.pending_input_bytes = max(0, self.pending_input_bytes - 1)
            elif 0x20 <= byte <= 0x7E or byte >= 0x80:
                self.pending_input_bytes += 1

    def _write_text_injection(
        self, encoded: bytes, confirm: bytes | None = None,
        deadline: float | None = None,
    ) -> None:
        paste_payload = BRACKETED_PASTE_START + encoded + BRACKETED_PASTE_END
        self.write_master(paste_payload, deadline)
        time.sleep(0.05)
        self.write_master(b"\r", deadline)
        # The Enter submits whatever was on the prompt line along with the
        # injection, so nothing is pending afterwards.
        self.pending_input_bytes = 0
        self.in_paste = False
        self.compact_armed = True
        if confirm is not None:
            time.sleep(CONFIRM_DELAY_SECONDS)
            self.write_master(confirm, deadline)

    def _flush_deferred(self) -> None:
        with self.input_lock:
            if not self.deferred_injections or (
                (self.in_paste or self.pending_input_bytes != 0)
                and time.monotonic() - self.last_input_at < IDLE_FLUSH_SECONDS
            ):
                return
            while self.deferred_injections:
                self._write_text_injection(*self.deferred_injections.popleft())

    def _maybe_idle_compact(self) -> None:
        seconds = self.profile.idle_compact_seconds
        if seconds is None:
            return
        with self.input_lock:
            now = time.monotonic()
            if (
                not self.compact_armed
                or self.pending_input_bytes != 0
                or self.in_paste
                or now - self.last_input_at < seconds
                or now < self.next_idle_compact_check_at
            ):
                return
            self.next_idle_compact_check_at = now + IDLE_COMPACT_REGISTRY_POLL_SECONDS
            session_dir = self.profile.session_dir
            child_pid = self.child_pid
            if session_dir is None or child_pid is None:
                return
            try:
                entry = json.loads((session_dir / f"{child_pid}.json").read_text())
                status_updated_at = int(entry["statusUpdatedAt"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                return
            if (
                entry.get("pid") != child_pid
                or entry.get("status") != "idle"
                or time.time() * 1000 - status_updated_at < seconds * 1000
            ):
                return
            encoded = self.profile.controls["compact"].render(None)
            self._write_text_injection(encoded)
            self.compact_armed = False
            self.journal(
                f"idle {seconds:g} s: injected /compact", to_terminal=False
            )

    def forward_user_data(self, data: bytes) -> None:
        with self.input_lock:
            pieces = data.split(b"\x1a")
            for index, piece in enumerate(pieces):
                if piece:
                    self.write_master(piece)
                    self._track_user_data(piece)
                if index + 1 < len(pieces):
                    self.suspend()
        self._flush_deferred()

    def target_status(self, target_pid: int) -> str:
        if self.profile.session_dir is None:
            return NO_SESSION_REGISTRY_STATUS
        try:
            entry = json.loads(
                (self.profile.session_dir / f"{target_pid}.json").read_text()
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise LookupError(f"target session {target_pid} is not registered") from exc
        if entry.get("pid") != target_pid:
            raise LookupError(f"target session registry changed for pid {target_pid}")
        return entry.get("status", "")

    def is_descendant(self, target_pid: int) -> bool:
        current = target_pid
        seen = set()
        while current > 1 and current not in seen:
            seen.add(current)
            parent, _ = process_stat(current)
            if parent == self.pid:
                return True
            current = parent
        return False

    def validate_target_environment(self, target_pid: int) -> None:
        if not self.is_descendant(target_pid):
            raise LookupError(f"target pid {target_pid} is not brokered by {self.pid}")
        environment = process_environment(target_pid)
        if environment.get(self.profile.socket_env) != str(self.socket_path):
            raise LookupError("target broker socket advertisement does not match")
        if environment.get(self.profile.token_env) != self.token:
            raise LookupError("target broker token advertisement does not match")
        if environment.get(self.profile.pid_env) != str(self.pid):
            raise LookupError("target broker pid advertisement does not match")
        target_pgid = os.getpgid(target_pid)
        foreground_pgid = os.tcgetpgrp(self.master_fd)
        if target_pgid != foreground_pgid:
            raise LookupError(
                f"target pgid {target_pgid} is not PTY foreground pgid {foreground_pgid}"
            )

    def _await_acknowledgment(self, acknowledgment: bytes) -> str:
        deadline = time.monotonic() + ACKNOWLEDGMENT_SECONDS
        with self.output_condition:
            while True:
                output = TERMINAL_ESCAPE_RE.sub(b"", bytes(self.output_capture))
                if acknowledgment in output:
                    text = acknowledgment.decode("utf-8")
                    self.journal(f"acknowledged: {text}", to_terminal=False)
                    return text
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    text = acknowledgment.decode("utf-8")
                    raise RuntimeError(
                        f"session printed no {text!r} within {ACKNOWLEDGMENT_SECONDS:g} s"
                    )
                self.output_condition.wait(remaining)

    def inject_request(
        self,
        target_pid: int,
        encoded: bytes,
        deadline: float,
        raw: bool = False,
        confirm: bytes | None = None,
        acknowledgment: bytes | None = None,
    ) -> str | None:
        if self.stop.is_set():
            raise RuntimeError("broker stopped before delivery")
        if time.monotonic() >= deadline:
            raise TimeoutError("delivery deadline expired before injection")
        status = self.target_status(target_pid)
        self.journal(
            f"injecting {len(encoded)} bytes into target pid={target_pid} "
            f"status={status!r} raw={raw}",
            to_terminal=False,
        )
        if acknowledgment is not None:
            with self.output_condition:
                # Start before writing because the acknowledgment can print during
                # CONFIRM_DELAY_SECONDS before the confirm Enter.
                self.output_capture = bytearray()
        deferred = False
        try:
            with self.input_lock:
                if raw:
                    self.write_master(encoded, deadline)
                    if confirm is not None:
                        time.sleep(CONFIRM_DELAY_SECONDS)
                        self.write_master(confirm, deadline)
                elif self.pending_input_bytes or self.in_paste:
                    self.deferred_injections.append((encoded, confirm))
                    deferred = True
                    self.journal(
                        f"deferred {len(encoded)} bytes: {self.pending_input_bytes} bytes "
                        "of unsubmitted input on the prompt line",
                        to_terminal=False,
                    )
                else:
                    self._write_text_injection(encoded, confirm, deadline)
            if acknowledgment is not None and not deferred:
                return self._await_acknowledgment(acknowledgment)
            if acknowledgment is not None:
                self.journal(
                    "acknowledgment was not awaited because the control was deferred",
                    to_terminal=False,
                )
            self.record_best_effort_acceptance(target_pid, status)
            return None
        finally:
            with self.output_condition:
                self.output_capture = None

    def record_best_effort_acceptance(
        self, target_pid: int, injected_status: str
    ) -> None:
        self.journal(
            f"injected into target pid={target_pid}; acceptance is not "
            f"status-verifiable (injected status={injected_status!r})",
            to_terminal=False,
        )

    def read_request(self, connection: socket.socket) -> dict:
        payload = bytearray()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_REQUEST_BYTES:
                raise ValueError("request exceeds broker size limit")
        self.journal(f"received request bytes={len(payload)}", to_terminal=False)
        try:
            request = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("request is not valid JSON") from exc
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        return request

    def handle_connection(self, connection: socket.socket) -> str | None:
        peer_pid, peer_uid, _peer_gid = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        self.journal(
            f"accepted connection from peer pid={peer_pid} uid={peer_uid}",
            to_terminal=False,
        )
        if peer_uid != os.getuid():
            raise PermissionError(f"peer pid {peer_pid} has uid {peer_uid}")
        request = self.read_request(connection)
        token = request.get("token")
        target_pid = request.get("target_pid")
        has_text = "text" in request
        has_control = "control" in request
        if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
            raise PermissionError("invalid broker token")
        if has_text == has_control:
            raise ValueError("request must contain exactly one of text or control")
        expected_keys = {"token", "target_pid", "text" if has_text else "control"}
        if set(request) != expected_keys:
            raise ValueError("request contains unsupported fields")
        if not isinstance(target_pid, int) or target_pid <= 0:
            raise ValueError("target_pid must be a positive integer")
        self.validate_target_environment(target_pid)
        self.journal(f"validated target pid={target_pid}", to_terminal=False)
        raw = False
        acknowledgment = None
        if has_control:
            control = request["control"]
            spec, encoded = resolve_control(control, self.profile.controls)
            self.journal(
                f"validated control command={control.get('command')!r}",
                to_terminal=False,
            )
            if encoded is None:
                return None
            raw = spec.raw
            acknowledgment = spec.render_acknowledgment(control.get("argument"))
        else:
            text = request["text"]
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            encoded = normalize_injected_text(text)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        return self.inject_request(
            target_pid,
            encoded,
            deadline,
            raw=raw,
            confirm=spec.confirm if has_control else None,
            acknowledgment=acknowledgment,
        )

    def send_response(self, connection: socket.socket, response: str) -> None:
        try:
            connection.sendall(response.encode("utf-8"))
        except OSError:
            pass

    def injector_loop(self) -> None:
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
                with connection:
                    connection.settimeout(REQUEST_TIMEOUT_SECONDS + 5)
                    try:
                        acknowledgment = self.handle_connection(connection)
                    except Exception as exc:
                        self.journal(
                            f"request failed: {exc}\n{traceback.format_exc().rstrip()}"
                        )
                        self.send_response(connection, f"ERR {exc}\n")
                    else:
                        response = f"OK {acknowledgment}\n" if acknowledgment else "OK\n"
                        self.send_response(connection, response)
                        self.journal("request acknowledged", to_terminal=False)
            except TimeoutError:
                continue
            except Exception as exc:
                if self.stop.is_set():
                    return
                self.journal(
                    f"injector loop recovered from error: {exc}\n"
                    f"{traceback.format_exc().rstrip()}"
                )
                time.sleep(0.1)

    def forward_input(self) -> None:
        while not self.stop.is_set():
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.1)
            if readable:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    self.stop.set()
                    break
                self.forward_user_data(data)
            self._flush_deferred()
            self._maybe_idle_compact()
            # A login swap holds this lock while replacing the pid and PTY.
            with self.master_write_lock:
                child_pid = self.child_pid
                try:
                    waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
                except ChildProcessError:
                    self.stop.set()
                    break
                if waited_pid == child_pid:
                    self.child_status = status
                    self.stop.set()
                    break

    def cleanup(self) -> None:
        self.stop.set()
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        if self.child_pid is not None and self.child_status is None:
            self.terminate_child(signal.SIGTERM)
            try:
                _, self.child_status = os.waitpid(self.child_pid, 0)
            except ChildProcessError:
                pass
        if getattr(self, "output_thread", None) is not None:
            self.output_thread.join(timeout=1)
        if getattr(self, "injector_thread", None) is not None:
            self.injector_thread.join(timeout=1)
        if getattr(self, "watcher_thread", None) is not None:
            self.watcher_thread.join(timeout=1)
        if getattr(self, "limit_thread", None) is not None:
            self.limit_thread.join(timeout=1)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.socket_path.unlink(missing_ok=True)
        self.advertisement_path.unlink(missing_ok=True)
        self.restore_terminal()

    def run(self) -> int:
        try:
            set_parent_death_signal()
            self.resolve_identity()
            self.validate_command()
            self.setup_state_dir()
            self.setup_listener()
            self.spawn_child()
            self.install_signal_handlers()
            self.set_terminal_raw()
            self.output_thread = threading.Thread(target=self.output_loop, daemon=True)
            self.injector_thread = threading.Thread(target=self.injector_loop, daemon=True)
            self.output_thread.start()
            self.injector_thread.start()
            if self.profile.account_cycle is not None:
                self.limit_thread = threading.Thread(
                    target=self.limit_watch, daemon=True
                )
                self.limit_thread.start()
            if self.profile.conversation_discovery is not None:
                # After the relay is up: the session is usable from the first
                # keystroke, and discovery happens beside it rather than in
                # front of it.
                self.watcher_thread = threading.Thread(
                    target=self.watch_for_conversation, daemon=True
                )
                self.watcher_thread.start()
            self.initialized = True
            self.forward_input()
        finally:
            self.cleanup()
        if self.child_status is None:
            return 1
        return os.waitstatus_to_exitcode(self.child_status)


def main(profile: Profile) -> None:
    """Run one runtime's broker. The adapter owns the profile; this owns the rest."""
    if not isinstance(profile, Profile):
        raise TypeError("main requires an explicit Profile")
    parser = argparse.ArgumentParser(prog=profile.program, description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")
    broker = Broker(command, profile)
    try:
        exit_code = broker.run()
    except Exception as exc:
        print(f"{profile.program}: FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1 if broker.initialized else 97)
    raise SystemExit(exit_code)
