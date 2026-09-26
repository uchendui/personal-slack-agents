#!/usr/bin/env python3
"""Register private Slack bridge identities."""

from __future__ import annotations
import argparse, asyncio, contextlib, dataclasses, fcntl, json, logging, os
import shlex, shutil, socket, stat, subprocess, sys, time, urllib.parse, urllib.request, uuid
from pathlib import Path
from typing import Any

import pty_broker
import slack_live_delivery
from slack_send import NAME_RE, SendError, load_agent

CONFIG_DIR = Path.home() / ".config/slack-bridge"
AGENTS_DIR = CONFIG_DIR / "agents"
REGISTRY_PATH = CONFIG_DIR / "registry.json"
LOCK_PATH = CONFIG_DIR / "registry.lock"
PTY_STATE_DIR = Path.home() / ".local" / "state" / "slack-bridge" / "pty"
ANTIGRAVITY_PROFILE_DIR = Path.home() / ".gemini" / "antigravity-cli"
SLACK_CREDENTIALS = Path.home() / ".slack/credentials.json"
KEEP_ALIVE_ROTATE_WITHIN = 7 * 3600
API_BASE = "https://slack.com/api/"
LOG = logging.getLogger("slack-register")
BOT_SCOPES = (
    "chat:write chat:write.public files:write files:read im:history im:write im:read "
    "channels:history channels:join channels:read channels:manage groups:history "
    "groups:read groups:write mpim:history mpim:read mpim:write users:read "
    "reactions:read reactions:write pins:read pins:write"
).split()
class RegisterError(RuntimeError):
    pass


class SlackAPIError(RegisterError):
    def __init__(self, method, reason):
        self.reason = reason
        super().__init__(f"Slack {method} failed: {reason}")


class AppLimitError(RegisterError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentConfig:
    name: str
    app_token: str
    bot_token: str
    app_id: str
    bot_user_id: str
    kind: str
    workdir: Path
    deliver_channel_messages: bool
    operator_user_id: str
    profile_dir: Path
    runtime_args: str
    launcher: str
    launcher_path: str | None
    session_id: str | None


def _directory(path: Path, create=False):
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RegisterError(f"private directory must be owned with mode 0700: {path}")


def operator_user_ids() -> set[str]:
    """Every Slack user id in operator.txt, one per line: all of the operator's accounts."""
    try:
        return {line.strip() for line in (CONFIG_DIR / "operator.txt").read_text().splitlines() if line.strip()}
    except OSError:
        return set()


def _read(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise RegisterError(f"cannot open private file {path}: {exc}") from exc
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise RegisterError(f"private file must be owned with mode 0600: {path}")
        return stream.read()


def _record(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == {"app_id", "session_id", "last_registered"}
        and (type(value["app_id"]) is str)
        and bool(value["app_id"])
        and (value["session_id"] is None or (type(value["session_id"]) is str and bool(value["session_id"])))
        and (type(value["last_registered"]) in (int, float))
    )


def load_registry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(path))
    except json.JSONDecodeError as exc:
        raise RegisterError(f"registry is not valid JSON: {exc}") from exc
    keys = {"version", "machine_id", "agents", "tombstones"}
    if (
        type(value) is not dict
        or set(value) != keys
        or value["version"] != 2
        or (type(value["machine_id"]) is not str)
        or (not value["machine_id"])
    ):
        raise RegisterError("registry has invalid top-level shape")
    for field in ("agents", "tombstones"):
        entries = value[field]
        if type(entries) is not dict or any(
            (type(name) is not str or not NAME_RE.fullmatch(name) or (not _record(record)) for name, record in entries.items())
        ):
            raise RegisterError(f"registry {field} map is invalid")
    if set(value["agents"]) & set(value["tombstones"]):
        raise RegisterError("registry identity is both active and retired")
    return value


def _env(path: Path) -> dict[str, str]:
    try:
        return load_agent(path.stem, path.parent)
    except SendError as exc:
        raise RegisterError(str(exc)) from exc


def load_agent_configs(directory: Path) -> dict[str, AgentConfig]:
    _directory(directory)
    registry = load_registry(directory.parent / "registry.json")
    paths = sorted(directory.iterdir())
    if any((not path.name.endswith(".env") or not NAME_RE.fullmatch(path.stem) for path in paths)) or {path.stem for path in paths} != set(
        registry["agents"]
    ):
        raise RegisterError("agent files and registry do not match")
    configs = {}
    for path in paths:
        values, record = (_env(path), registry["agents"][path.stem])
        if values["SLACK_APP_ID"] != record["app_id"]:
            raise RegisterError(f"agent app id does not match registry: {path.stem}")
        kind = values["AGENT_KIND"]
        workdir = Path(values["WORKDIR"])
        if kind == "claude":
            profile = Path(values["CLAUDE_CONFIG_DIR"])
            runtime_args = values["CLAUDE_ARGS"]
        elif kind == "codex":
            profile = Path(values["CODEX_HOME"])
            runtime_args = values["CODEX_ARGS"]
        elif kind == "antigravity":
            profile = ANTIGRAVITY_PROFILE_DIR
            runtime_args = ""
        else:
            raise RegisterError(f"unsupported agent kind: {kind}")
        if not workdir.is_absolute() or not profile.is_absolute():
            raise RegisterError(f"agent paths are not absolute: {path.stem}")
        configs[path.stem] = AgentConfig(
            path.stem,
            values["SLACK_APP_TOKEN"],
            values["SLACK_BOT_TOKEN"],
            values["SLACK_APP_ID"],
            values["BOT_USER_ID"],
            kind,
            workdir,
            values["DELIVER_CHANNEL_MESSAGES"] == "true",
            values["OPERATOR_USER_ID"],
            profile,
            runtime_args,
            values.get("CLAUDE_LAUNCHER", "claude"),
            values.get("CLAUDE_LAUNCHER_PATH"),
            record["session_id"],
        )
    return configs


def _write(path: Path, text: str):
    _directory(path.parent, create=True)
    pty_broker.atomic_write(path, text)


@contextlib.contextmanager
def _lock():
    _directory(CONFIG_DIR, create=True)
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _credential_lock():
    path = SLACK_CREDENTIALS.with_name("credentials.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _registry():
    if REGISTRY_PATH.exists():
        return load_registry(REGISTRY_PATH)
    machine = socket.gethostname()
    if not machine:
        raise RegisterError("machine id is empty")
    return {"version": 2, "machine_id": machine, "agents": {}, "tombstones": {}}


def _serialize(values):
    if any(("\n" in value for value in values.values())):
        raise RegisterError("agent credential values cannot contain newlines")
    return "".join((f"{key}={value}\n" for key, value in sorted(values.items())))


def _save(registry):
    _write(REGISTRY_PATH, json.dumps(registry, indent=2, sort_keys=True) + "\n")


def _api(method, token=None, json_body=False, **fields):
    fields = {k: v for k, v in fields.items() if v is not None}
    content_type = "application/json" if json_body else "application/x-www-form-urlencoded"
    data = json.dumps(fields).encode() if json_body else urllib.parse.urlencode(fields).encode()
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(API_BASE + method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except Exception as exc:
        raise RegisterError(f"Slack {method} transport failed: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        reason = result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"
        if "app" in reason.lower() and "limit" in reason.lower():
            raise AppLimitError(reason)
        raise SlackAPIError(method, reason)
    return result


def _announce(name, values):
    try:
        opened = _api("conversations.open", values["SLACK_BOT_TOKEN"], users=values["OPERATOR_USER_ID"])
        channel = opened.get("channel", {}).get("id")
        if not isinstance(channel, str) or not channel:
            raise RegisterError("Slack returned no operator DM channel")
        _api("chat.postMessage", values["SLACK_BOT_TOKEN"], channel=channel, text=f"<@{values['BOT_USER_ID']}> is online")
    except Exception as exc:
        LOG.error("registration announcement failed for %s: %s", name, exc)


def _slack_credentials(team):
    try:
        credentials = json.loads(_read(SLACK_CREDENTIALS))
    except json.JSONDecodeError as exc:
        raise RegisterError(f"Slack CLI credentials are not valid JSON: {exc}") from exc
    if not isinstance(credentials, dict) or not credentials:
        raise RegisterError("Slack CLI credentials have invalid shape")
    if team is None:
        if len(credentials) != 1:
            raise RegisterError("--team is required when several Slack workspaces are logged in")
        team = next(iter(credentials))
    entry = credentials.get(team)
    if not isinstance(entry, dict) or any(
        not isinstance(entry.get(key), str) or not entry[key]
        for key in ("token", "refresh_token")
    ):
        raise RegisterError(f"Slack CLI credentials are invalid for team {team}")
    return credentials, team, entry


def _refresh_user_token(team, stale):
    credentials, _, latest = _slack_credentials(team)
    if latest["refresh_token"] != stale["refresh_token"]:
        return latest
    rotated = _api("tooling.tokens.rotate", refresh_token=stale["refresh_token"])
    credentials, _, latest = _slack_credentials(team)
    if latest["refresh_token"] != stale["refresh_token"]:
        return latest
    for key in ("token", "refresh_token"):
        if not isinstance(rotated.get(key), str) or not rotated[key]:
            raise RegisterError("Slack token rotation returned invalid credentials")
        latest[key] = rotated[key]
    if "exp" in rotated:
        latest["exp"] = rotated["exp"]
    pty_broker.atomic_write(
        SLACK_CREDENTIALS,
        json.dumps(credentials, indent=2) + "\n",
    )
    return latest


def _service_teams():
    return {path.name.removeprefix("service-token-") for path in CONFIG_DIR.glob("service-token-*")}


def _service_token(team):
    # A service token from `slack auth token` never expires and is separate from the `slack login`
    # pair, so machines sharing one Slack user no longer invalidate each other by rotating.
    teams = _service_teams()
    if team is None and len(teams) == 1:
        team = next(iter(teams))
    if team not in teams:
        return None
    path = CONFIG_DIR / f"service-token-{team}"
    token = _read(path).strip()
    if not token:
        raise RegisterError(f"Slack service token file is empty: {path}")
    return token


def _user_api(method, team, json_body=False, **fields):
    service = _service_token(team)
    if service:
        return _api(method, service, json_body=json_body, **fields)
    with _credential_lock():
        _, team, entry = _slack_credentials(team)
        try:
            return _api(method, entry["token"], json_body=json_body, **fields)
        except SlackAPIError as exc:
            if exc.reason not in {"token_expired", "invalid_auth"}:
                raise
        entry = _refresh_user_token(team, entry)
        return _api(method, entry["token"], json_body=json_body, **fields)


def _manifest(name):
    return {
        "display_information": {"name": name},
        "features": {
            "bot_user": {"display_name": name, "always_online": True},
            "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
        },
        "oauth_config": {"scopes": {"bot": BOT_SCOPES}},
        "settings": {
            "event_subscriptions": {"bot_events": ["message.im", "message.channels", "message.groups", "message.mpim"]},
            "socket_mode_enabled": True,
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _join_channels(values, joins):
    listing = None
    token = values["SLACK_BOT_TOKEN"]
    for channel in joins:
        if listing is None:
            listing = _api("conversations.list", token, limit=200)
        channel_name = channel.removeprefix("#")
        matches = [
            item["id"] for item in listing.get("channels", [])
            if item.get("name") == channel_name
        ]
        if len(matches) != 1:
            raise RegisterError(f"channel does not resolve uniquely: {channel}")
        channel_id = matches[0]
        _api("conversations.join", token, channel=channel_id)
        membership = _api("conversations.info", token, channel=channel_id)
        if membership.get("channel", {}).get("is_member") is not True:
            raise RegisterError(
                f"Slack membership not confirmed for #{channel_name} ({channel_id})"
            )
        print(f"Verified Slack membership: #{channel_name} ({channel_id})")


def _provision(name, app_id, team, joins):
    manifest = _manifest(name)
    encoded = json.dumps(manifest)
    if app_id is None:
        created = _user_api(
            "apps.manifest.create", team, json_body=True, manifest=encoded
        )
        app_id = created.get("app_id")
        if not isinstance(app_id, str) or not app_id:
            raise RegisterError("Slack app creation returned no app id")
    else:
        try:
            _user_api(
                "apps.manifest.update", team, json_body=True,
                app_id=app_id, manifest=encoded,
            )
        except SlackAPIError as exc:
            if exc.reason != "app_not_found":
                raise
            # The recorded app was deleted externally; create a fresh one.
            created = _user_api(
                "apps.manifest.create", team, json_body=True, manifest=encoded
            )
            app_id = created.get("app_id")
            if not isinstance(app_id, str) or not app_id:
                raise RegisterError("Slack app creation returned no app id")
    installed = _user_api(
        "apps.developerInstall", team, json_body=True,
        app_id=app_id, bot_scopes=BOT_SCOPES,
    )
    tokens = installed.get("api_access_tokens")
    if not isinstance(tokens, dict):
        raise RegisterError("Slack developer install returned invalid tokens")
    values = {
        "SLACK_APP_TOKEN": tokens.get("app_level"),
        "SLACK_BOT_TOKEN": tokens.get("bot"),
        "SLACK_APP_ID": app_id,
    }
    bot = _api("auth.test", values["SLACK_BOT_TOKEN"])
    operator = _user_api("auth.test", team)
    values["BOT_USER_ID"] = bot.get("user_id")
    values["OPERATOR_USER_ID"] = operator.get("user_id")
    if values["OPERATOR_USER_ID"] not in operator_user_ids():
        raise RegisterError("Slack CLI user does not match operator.txt")
    if any(type(value) is not str or not value for value in values.values()):
        raise RegisterError("Slack identity lookup returned invalid credentials")
    _join_channels(values, joins)
    return values


def _credentials(name, app_id, team, joins, registry):
    try:
        return _provision(name, app_id, team, joins)
    except AppLimitError:
        retired = [(record["last_registered"], old, record) for old, record in registry["tombstones"].items() if record["app_id"] != app_id]
        if not retired:
            raise
        _, old, record = min(retired)
        _user_api(
            "apps.manifest.delete", team, json_body=True,
            app_id=record["app_id"],
        )
        registry["tombstones"].pop(old)
        return _provision(name, app_id, team, joins)


def _existing(name, kind, registry):
    record = registry["agents"].get(name)
    if record is None:
        return None
    try:
        values = _env(AGENTS_DIR / f"{name}.env")
        if values["SLACK_APP_ID"] != record["app_id"]:
            raise RegisterError("agent app id does not match registry")
    except RegisterError as exc:
        LOG.warning("existing credentials invalid for %s; provisioning: %s", name, exc)
        return None
    if values["AGENT_KIND"] != kind:
        raise RegisterError(
            f"agent {name} is already registered as {values['AGENT_KIND']}; unregister the name, then register it with the new kind"
        )
    try:
        if _api("auth.test", values["SLACK_BOT_TOKEN"]).get("user_id") != values["BOT_USER_ID"]:
            raise RegisterError("agent bot identity does not match credentials")
    except RegisterError as exc:
        LOG.warning("existing credentials invalid for %s; provisioning: %s", name, exc)
        return None
    return values


def _antigravity_session_id() -> str:
    broker_pid = os.environ.get("ANTIGRAVITY_PTY_BROKER_PID")
    if not broker_pid or not broker_pid.isdigit():
        raise RegisterError(
            "ANTIGRAVITY_PTY_BROKER_PID must identify the live broker"
        )
    advertisement_path = PTY_STATE_DIR / f"{broker_pid}.json"
    try:
        advertisement = json.loads(_read(advertisement_path))
    except Exception as exc:
        raise RegisterError(
            f"Antigravity PTY advertisement is unreadable: {advertisement_path}"
        ) from exc
    if (
        type(advertisement) is not dict
        or type(advertisement.get("broker_pid")) is not int
        or advertisement["broker_pid"] != int(broker_pid)
    ):
        raise RegisterError(
            f"Antigravity PTY advertisement has invalid shape: {advertisement_path}"
        )
    session = advertisement.get("conversation_id")
    if not isinstance(session, str):
        raise RegisterError(
            f"Antigravity PTY advertisement has no conversation UUID: {advertisement_path}"
        )
    try:
        normalized = str(uuid.UUID(session))
    except (ValueError, AttributeError) as exc:
        raise RegisterError(
            f"Antigravity PTY advertisement has an invalid conversation UUID: {advertisement_path}"
        ) from exc
    if normalized != session.lower():
        raise RegisterError(
            f"Antigravity PTY advertisement has a non-canonical conversation UUID: {advertisement_path}"
        )
    return normalized


def _live_claude_args() -> str:
    """The --model/--effort of the ancestor claude process that ran this
    registration, so a later revive resumes at the same tier (subagent
    shells do not inherit $CLAUDE_EFFORT). Walks parents by pid, the same
    walk pty_broker.process_stat does for a process tree, until it finds a
    process named claude; an ancestor cannot exit while this process runs,
    so a missing /proc entry or a walk to pid 1 means no claude ancestor."""
    pid = os.getppid()
    while pid > 1:
        comm = (Path("/proc") / str(pid) / "comm").read_text().strip()
        if comm == "claude":
            flags = slack_live_delivery._parent_model(pid) + slack_live_delivery._parent_effort(pid)
            return shlex.join(flags)
        pid, _ = pty_broker.process_stat(pid)
    raise RegisterError("no claude ancestor process; pass --claude-args explicitly")


LAUNCHER_KEYS = ("CLAUDE_LAUNCHER", "CLAUDE_LAUNCHER_PATH")


def resolve_launcher(text: str) -> str:
    """The launcher command prefix with its program made absolute, because
    the bridge revives under systemd, whose PATH lacks the user's. Raises
    ValueError for unbalanced quotes or a program not on PATH."""
    if text == "claude":
        return text
    words = shlex.split(text)
    program = shutil.which(words[0]) if words else None
    if program is None:
        raise ValueError(f"program not found on PATH: {text!r}")
    return shlex.join([program, *words[1:]])


def _register(name, kind, workdir, profile, runtime_args, team, joins, session=None, launcher=None):
    if not NAME_RE.fullmatch(name):
        raise RegisterError("invalid agent name")
    if kind == "antigravity":
        session = _antigravity_session_id()
    with _lock():
        _directory(AGENTS_DIR, create=True)
        registry = _registry()
        previous = registry["agents"].get(name) or registry["tombstones"].get(name)
        values = _existing(name, kind, registry)
        if values is None:
            credentials = _credentials(name, previous and previous["app_id"], team, joins, registry)
            values = credentials | {"AGENT_KIND": kind, "DELIVER_CHANNEL_MESSAGES": "false"}
        else:
            _join_channels(values, joins)
        if kind == "claude":
            profile_key, args_key = "CLAUDE_CONFIG_DIR", "CLAUDE_ARGS"
        elif kind == "codex":
            profile_key, args_key = "CODEX_HOME", "CODEX_ARGS"
        elif kind == "antigravity":
            profile_key, args_key = None, None
        else:
            raise RegisterError(f"unsupported agent kind: {kind}")
        values.update({"WORKDIR": str(workdir)})
        if profile_key is not None:
            values[profile_key] = str(profile)
        if args_key is not None:
            values[args_key] = runtime_args
        # A session registering itself passes no launcher and keeps the one
        # slack-spawn stored; "claude" is the default and is not stored. A
        # custom launcher is stored with this shell's PATH, which it needs.
        if launcher == "claude":
            for key in LAUNCHER_KEYS:
                values.pop(key, None)
        elif launcher is not None:
            values.update({"CLAUDE_LAUNCHER": launcher, "CLAUDE_LAUNCHER_PATH": os.environ["PATH"]})
        if kind != "antigravity":
            session = session or os.environ.get("CLAUDE_CODE_SESSION_ID") or (previous or {}).get("session_id")
        # A session re-registering under a new name (harness /rename) retires
        # its old identity; one session holds one identity.
        retired = [
            other for other, record in registry["agents"].items()
            if other != name and session and record.get("session_id") == session
        ]
        # Only a rename inherits channels, from the identity it retires;
        # a new agent joins nothing beyond --join.
        peers: dict[str, str] = {}
        for other in retired:
            peers = _bot_channels(AGENTS_DIR / f"{other}.env") | peers
            # A rename runs the same session, so it keeps its launcher.
            if launcher is None:
                old = _env(AGENTS_DIR / f"{other}.env")
                values.update({key: old[key] for key in LAUNCHER_KEYS if key in old})
        for other in retired:
            registry["tombstones"][other] = registry["agents"].pop(other) | {"session_id": None}
        registry["agents"][name] = {
            "app_id": values["SLACK_APP_ID"],
            "session_id": session,
            "last_registered": time.time(),
        }
        registry["tombstones"].pop(name, None)
        path = AGENTS_DIR / f"{name}.env"
        old = _read(path) if path.exists() else None
        _write(path, _serialize(values))
        try:
            _save(registry)
        except Exception:
            _write(path, old) if old is not None else path.unlink(missing_ok=True)
            raise
        for other in retired:
            (AGENTS_DIR / f"{other}.env").unlink(missing_ok=True)
        for channel, host in sorted(peers.items()):
            try:
                _api("conversations.invite", host, channel=channel, users=values["BOT_USER_ID"])
            except SlackAPIError as exc:
                if exc.reason == "already_in_channel":
                    continue
                LOG.error("channel inheritance failed for %s in %s: %s", name, channel, exc)
            except Exception as exc:
                LOG.error("channel inheritance failed for %s in %s: %s", name, channel, exc)
        _announce(name, values)


def _bot_channels(env_path):
    # Best-effort, like _announce: a retired identity whose memberships
    # cannot be read costs channel inheritance, never the registration.
    try:
        token = next(
            line.partition("=")[2] for line in _read(env_path).splitlines()
            if line.startswith("SLACK_BOT_TOKEN=")
        )
        channels: dict[str, str] = {}
        cursor = None
        while True:
            kwargs = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            page = _api("users.conversations", token, **kwargs)
            channels |= {
                item["id"]: token for item in page.get("channels", [])
                if isinstance(item.get("id"), str)
            }
            metadata = page.get("response_metadata", {})
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                return channels
    except Exception as exc:
        LOG.error("could not read channels of identity %s: %s", env_path.stem, exc)
        return {}


def _unregister(name):
    with _lock():
        registry = load_registry(REGISTRY_PATH)
        if name not in registry["agents"]:
            raise RegisterError(f"agent is not registered: {name}")
        path = AGENTS_DIR / f"{name}.env"
        text = _read(path)
        record = registry["agents"].pop(name)
        registry["tombstones"][name] = record | {"session_id": None}
        path.unlink()
        try:
            _save(registry)
        except Exception:
            _write(path, text)
            raise


async def unregister_agent(name: str) -> None:
    await asyncio.to_thread(_unregister, name)


def _rename(old, new):
    if not NAME_RE.fullmatch(new):
        raise RegisterError("invalid new agent name")
    with _lock():
        registry = load_registry(REGISTRY_PATH)
        if old not in registry["agents"] or new in registry["agents"]:
            raise RegisterError("rename source or destination is invalid")
        old_path = AGENTS_DIR / f"{old}.env"
        old_text = _read(old_path)
        values = _env(old_path)
        team_id = _api("auth.test", values["SLACK_BOT_TOKEN"]).get("team_id")
        if not isinstance(team_id, str) or not team_id:
            raise RegisterError("Slack bot identity lookup returned no team id")
        record = registry["agents"].pop(old)
        retired = registry["tombstones"].pop(new, None)
        if retired is not None:
            _user_api("apps.manifest.delete", team_id, json_body=True, app_id=retired["app_id"])
        # Same Slack app, bot user, and channel memberships; only the name changes.
        values.update(_provision(new, record["app_id"], team_id, ()))
        registry["agents"][new] = record | {"last_registered": time.time()}
        new_path = AGENTS_DIR / f"{new}.env"
        _write(new_path, _serialize(values))
        old_path.unlink()
        try:
            _save(registry)
        except Exception:
            new_path.unlink(missing_ok=True)
            _write(old_path, old_text)
            raise
    return False


async def rename_agent(old: str, new: str) -> bool:
    return await asyncio.to_thread(_rename, old, new)


def _keep_alive(team):
    service_teams = _service_teams()
    if team:
        teams = [team]
    elif service_teams and not SLACK_CREDENTIALS.exists():
        teams = sorted(service_teams)
    else:
        try:
            credentials = json.loads(_read(SLACK_CREDENTIALS))
        except json.JSONDecodeError as exc:
            raise RegisterError(f"Slack CLI credentials are not valid JSON: {exc}") from exc
        if not isinstance(credentials, dict) or not credentials:
            raise RegisterError("Slack CLI credentials have invalid shape")
        teams = sorted(service_teams | set(credentials))
    failures = []
    for team_id in teams:
        try:
            service = _service_token(team_id)
            if service:
                _api("auth.test", service)
                continue
            with _credential_lock():
                _, _, entry = _slack_credentials(team_id)
                if type(entry.get("exp")) is not int:
                    raise RegisterError(f"Slack CLI credentials have invalid exp for team {team_id}")
                if entry["exp"] - int(time.time()) < KEEP_ALIVE_ROTATE_WITHIN:
                    entry = _refresh_user_token(team_id, entry)
                _api("auth.test", entry["token"])
        except RegisterError as exc:
            failures.append(f"{team_id}: {exc}")
    if failures:
        raise RegisterError("; ".join(failures))


def joins(extra):
    # Every agent joins the workspace-wide channel, so a message
    # posted there reaches it.
    return tuple(dict.fromkeys((os.environ.get("SLACK_DEFAULT_CHANNEL", "all-agents"), *(extra or ()))))


def run(argv=None):
    parser = argparse.ArgumentParser(prog="slack-register")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--kind", choices=("claude", "codex", "antigravity"))
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--claude-config-dir", type=Path)
    parser.add_argument("--claude-args")
    parser.add_argument("--launcher", help="claude or a command prefix that runs Claude Code; omitted keeps the stored one")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--codex-args", default="")
    parser.add_argument("--team")
    parser.add_argument("--join", action="append")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--unregister", nargs="?", const=True, metavar="NAME")
    parser.add_argument("--keep-alive", action="store_true")
    args = parser.parse_args(argv)
    if sum((args.list, bool(args.unregister), args.keep_alive)) > 1:
        parser.error("choose one lifecycle action")
    if args.list:
        if args.name:
            parser.error("--list does not accept a name")
        print("\n".join(load_agent_configs(AGENTS_DIR)))
    elif args.unregister:
        target = args.name if args.unregister is True else args.unregister
        if not target or (args.unregister is not True and args.name):
            parser.error("--unregister requires exactly one name")
        _unregister(target)
        print(f"slack-bridge picks up {target} within 5 s")
    elif args.keep_alive:
        if args.name:
            parser.error("--keep-alive does not accept a name")
        _keep_alive(args.team)
    else:
        if not args.name or not args.kind or args.workdir is None:
            parser.error("registration requires name, --kind, and --workdir")
        if args.kind == "claude":
            if args.claude_config_dir is None or args.codex_home is not None or args.codex_args:
                parser.error("claude registration requires only Claude profile flags")
            runtime_args = args.claude_args if args.claude_args is not None else _live_claude_args()
            profile = args.claude_config_dir
            try:
                launcher = None if args.launcher is None else resolve_launcher(args.launcher)
            except ValueError as exc:
                parser.error(f"--launcher: {exc}")
        elif args.kind == "codex":
            launcher = None
            if args.codex_home is None or args.claude_config_dir is not None or args.claude_args or args.launcher:
                parser.error("codex registration requires only Codex profile flags")
            profile, runtime_args = (args.codex_home, args.codex_args)
        else:
            launcher = None
            if args.claude_config_dir is not None or args.claude_args or args.codex_home is not None or args.codex_args or args.launcher:
                parser.error("antigravity registration accepts no profile or runtime flags")
            profile, runtime_args = (ANTIGRAVITY_PROFILE_DIR, "")
        _register(
            args.name,
            args.kind,
            args.workdir.expanduser().resolve(),
            profile.expanduser().resolve(),
            runtime_args,
            args.team,
            joins(args.join),
            launcher=launcher,
        )
        print(f"slack-bridge picks up {args.name} within 5 s")
    return 0


def main():
    try:
        return run()
    except (OSError, UnicodeError, RegisterError, subprocess.SubprocessError) as exc:
        print(f"slack-register: FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
