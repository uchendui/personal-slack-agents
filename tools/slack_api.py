#!/usr/bin/env python3
"""Credential-aware Slack I/O boundary for the in-memory bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

import websockets

LOG = logging.getLogger("slack-api")

API_BASE = "https://slack.com/api/"
MAX_FILE_BYTES = 1024 * 1024 * 1024
# A file posted within a second of its upload can still answer 403 or 404 to
# another app while Slack finishes hosting it; retry before refusing.
FILE_RETRY_ATTEMPTS = 4
FILE_RETRY_SECONDS = 2.0
# Slack answers a burst (a backlog replay calls conversations.replies per
# message) with HTTP 429 and a Retry-After; without honoring it the bridge
# died on the first 429 and systemd restarted it into the same burst.
RATE_LIMIT_ATTEMPTS = 5


class SlackError(RuntimeError):
    pass


class RateLimited(SlackError):
    def __init__(self, method: str, retry_after: float) -> None:
        super().__init__(f"Slack {method} rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


class SlackAPI:
    def __init__(
        self, config: Any, opener=urllib.request.urlopen, peers: Iterable[Any] = ()
    ) -> None:
        self.config = config
        self._open = opener
        self._websocket = None
        self._closed = False
        # Local identity map only (agent configs + operator id); never a
        # network lookup. Own id last so it wins over the peer entry.
        self.labels = {peer.bot_user_id: peer.name for peer in peers}
        import slack_register
        for operator in slack_register.operator_user_ids() | {config.operator_user_id}:
            self.labels[operator] = "the operator"
        self.labels[config.bot_user_id] = f"YOU ({config.name})"

    def label(self, sender: str) -> str:
        return self.labels.get(sender, sender)

    def _call_sync(self, method: str, token: str, **fields: Any) -> dict[str, Any]:
        data = urllib.parse.urlencode(
            {key: value for key, value in fields.items() if value is not None}
        ).encode()
        request = urllib.request.Request(
            API_BASE + method,
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with self._open(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after is None or not retry_after.strip().isdigit():
                    raise SlackError(
                        f"Slack {method} rate limited without a Retry-After header"
                    ) from exc
                raise RateLimited(method, float(retry_after)) from exc
            raise SlackError(f"Slack {method} transport failed: {exc}") from exc
        except Exception as exc:
            raise SlackError(f"Slack {method} transport failed: {exc}") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("error", "invalid response") if isinstance(result, dict) else "invalid response"
            raise SlackError(f"Slack {method} failed: {reason}")
        return result

    async def _call(self, method: str, token: str, **fields: Any) -> dict[str, Any]:
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(self._call_sync, method, token, **fields)
            except RateLimited as exc:
                if attempt == RATE_LIMIT_ATTEMPTS:
                    raise SlackError(
                        f"Slack {method} rate limited {RATE_LIMIT_ATTEMPTS} times"
                    ) from exc
                await asyncio.sleep(exc.retry_after)
        raise AssertionError("unreachable")

    async def auth_test(self) -> dict[str, Any]:
        return await self._call("auth.test", self.config.bot_token)

    async def run(self, callback, on_reconnect=None) -> None:
        connections = 0
        backoff = 1.0
        while not self._closed:
            try:
                opened = await self._call("apps.connections.open", self.config.app_token)
                url = opened.get("url")
                if not isinstance(url, str):
                    raise SlackError("apps.connections.open returned no WebSocket URL")
                queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

                async def consume() -> None:
                    while (payload := await queue.get()) is not None:
                        await callback(payload)

                async with websockets.connect(url, max_size=2**22) as websocket:
                    connections += 1
                    backoff = 1.0
                    LOG.info("socket opened (connection %d)", connections)
                    self._websocket = websocket
                    consumer = asyncio.create_task(consume())
                    consumer.add_done_callback(
                        lambda _: asyncio.create_task(websocket.close())
                    )
                    # A replay runs as a task so _serve acks frames while it reads
                    # history; awaiting it first would stall every ack past the
                    # three seconds after which Slack redelivers the event.
                    replay = (
                        asyncio.create_task(on_reconnect())
                        if on_reconnect is not None and connections > 1
                        else None
                    )
                    try:
                        LOG.info("socket closed: %s", await self._serve(websocket, queue))
                    finally:
                        if replay is not None:
                            await replay
                        queue.put_nowait(None)
                        await consumer
                        self._websocket = None
            except Exception as exc:
                if self._closed:
                    break
                if isinstance(exc, SlackError) and any(
                    err in str(exc) for err in ("invalid_auth", "token_revoked", "account_inactive")
                ):
                    raise
                LOG.warning("socket connection failed: %s; retrying in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _serve(self, websocket, queue) -> str:
        """One Socket Mode connection; a close from Slack ends it quietly so
        run() reconnects instead of killing the bridge and its forks."""
        try:
            async for raw in websocket:
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise SlackError("invalid Socket Mode frame") from exc
                if not isinstance(envelope, dict):
                    raise SlackError("invalid Socket Mode envelope")
                envelope_id = envelope.get("envelope_id")
                if isinstance(envelope_id, str):
                    await websocket.send(json.dumps({"envelope_id": envelope_id}))
                if envelope.get("type") == "disconnect":
                    return f"disconnect frame ({envelope.get('reason')})"
                if envelope.get("type") == "events_api":
                    payload = envelope.get("payload")
                    if not isinstance(payload, dict):
                        raise SlackError("invalid events_api payload")
                    if (envelope.get("retry_attempt") or 0) > 0:
                        event = payload.get("event")
                        LOG.info(
                            "Slack redelivery: retry_attempt=%s retry_reason=%s ts=%s",
                            envelope.get("retry_attempt"),
                            envelope.get("retry_reason"),
                            event.get("ts") if isinstance(event, dict) else None,
                        )
                    queue.put_nowait(payload)
        except websockets.ConnectionClosed:
            pass
        return "connection closed"

    async def close(self) -> None:
        self._closed = True
        if self._websocket is not None:
            await self._websocket.close()

    async def _replies(self, channel: str, root: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor = None
        seen = set()
        while True:
            page = await self._call(
                "conversations.replies",
                self.config.bot_token,
                channel=channel,
                ts=root,
                cursor=cursor,
                limit=200,
            )
            batch = page.get("messages")
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise SlackError("conversations.replies returned invalid messages")
            messages.extend(batch)
            metadata = page.get("response_metadata", {})
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                return messages
            if not isinstance(cursor, str) or cursor in seen:
                raise SlackError("conversations.replies returned invalid cursor")
            seen.add(cursor)

    async def thread_messages(
        self, channel: str, root: str, after_ts: str | None = None
    ) -> list[tuple[str, str, str]]:
        return [
            (message["ts"], self.label(message.get("user") or "unknown"), message.get("text", ""))
            for message in sorted(
                await self._replies(channel, root), key=lambda message: float(message["ts"])
            )
            if message.get("text", "")
            and (after_ts is None or float(message["ts"]) > float(after_ts))
        ]

    async def replies_from(
        self, channel: str, root: str, user_id: str, after_ts: str
    ) -> list[str]:
        return [
            message["ts"]
            for message in sorted(
                await self._replies(channel, root), key=lambda message: float(message["ts"])
            )
            if message.get("user") == user_id
            and float(message["ts"]) > float(after_ts)
        ]

    async def bot_channels(self) -> list[str]:
        channels: list[str] = []
        cursor = None
        seen = set()
        while True:
            page = await self._call(
                "users.conversations",
                self.config.bot_token,
                types="public_channel,private_channel,im,mpim",
                limit=200,
                cursor=cursor,
            )
            channels += [
                item["id"] for item in page.get("channels", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            metadata = page.get("response_metadata", {})
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                return channels
            if not isinstance(cursor, str) or cursor in seen:
                raise SlackError("users.conversations returned invalid cursor")
            seen.add(cursor)

    async def since(
        self, method: str, channel: str, oldest: str, **fields: Any
    ) -> list[dict[str, Any]]:
        """Messages strictly newer than `oldest`, for catch-up after a restart."""
        messages: list[dict[str, Any]] = []
        cursor = None
        seen = set()
        while True:
            page = await self._call(
                method,
                self.config.bot_token,
                channel=channel,
                oldest=oldest,
                inclusive="false",
                cursor=cursor,
                limit=200,
                **fields,
            )
            batch = page.get("messages")
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise SlackError(f"{method} returned invalid messages")
            messages.extend(batch)
            metadata = page.get("response_metadata", {})
            cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
            if not cursor:
                return messages
            if not isinstance(cursor, str) or cursor in seen:
                raise SlackError(f"{method} returned invalid cursor")
            seen.add(cursor)

    async def history(self, channel: str, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent messages in a channel, newest first."""
        page = await self._call(
            "conversations.history", self.config.bot_token, channel=channel, limit=limit
        )
        messages = page.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(item, dict) for item in messages
        ):
            raise SlackError("conversations.history returned invalid messages")
        return messages

    async def thread_role(self, channel: str, root: str) -> str:
        """"root" when this agent wrote the thread root, "member" when it wrote or was named in a reply or the root, else "none"."""
        me = self.config.bot_user_id
        messages = await self._replies(channel, root)
        mine = [
            message["ts"] == root
            for message in messages
            if message.get("user") == me or message.get("bot_id") == me
        ]
        named = any(f"<@{me}>" in message.get("text", "") for message in messages)
        return "root" if any(mine) else "member" if mine or named else "none"

    async def post_operational(self, channel: str, root: str, text: str) -> None:
        await self._call(
            "chat.postMessage",
            self.config.bot_token,
            channel=channel,
            thread_ts=root,
            text=text,
        )

    async def add_reaction(self, channel: str, timestamp: str) -> None:
        await self._call(
            "reactions.add",
            self.config.bot_token,
            channel=channel,
            timestamp=timestamp,
            name="eyes",
        )

    @staticmethod
    def validate_current_files(files: list[dict[str, Any]]) -> None:
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("invalid file metadata")
            size = item.get("size")
            if not isinstance(size, int) or size < 0:
                raise ValueError("invalid file size")
            if size > MAX_FILE_BYTES:
                raise ValueError("file exceeds 1 GiB")
            if not isinstance(item.get("url_private_download") or item.get("url_private"), str):
                raise ValueError("file has no private download URL")

    def _download_sync(self, item: dict[str, Any], directory: Path) -> Path:
        self.validate_current_files([item])
        url = item.get("url_private_download") or item["url_private"]
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.config.bot_token}"}
        )
        name = Path(str(item.get("name") or item.get("id") or "upload")).name
        path = directory / name
        suffix = 1
        while path.exists():
            path = directory / f"{suffix}-{name}"
            suffix += 1
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output, self._open(request, timeout=60) as response:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise ValueError("file exceeds 1 GiB")
                    output.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    async def download_current_files(
        self, files: list[dict[str, Any]], directory: Path
    ) -> list[Path]:
        self.validate_current_files(files)
        saved: list[Path] = []
        try:
            for item in files:
                for attempt in range(FILE_RETRY_ATTEMPTS):
                    try:
                        saved.append(await asyncio.to_thread(self._download_sync, item, directory))
                        break
                    except urllib.error.HTTPError as exc:
                        if exc.code not in (403, 404) or attempt == FILE_RETRY_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(FILE_RETRY_SECONDS)
        except Exception as exc:
            for path in saved:
                path.unlink(missing_ok=True)
            raise ValueError(f"file download failed: {exc}") from exc
        return saved
