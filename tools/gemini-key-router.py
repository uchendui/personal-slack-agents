#!/usr/bin/env python3

import hashlib
import hmac
import json
import logging
import os
import random
import re
import select
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = "127.0.0.1"
PORT = 3460
KEY_FILE = Path.home() / ".gemini_api_keys"
TOKEN_FILE = Path.home() / ".claude-code-router" / "gemini-key-router-token"
KEY_SPLIT_PATTERN = re.compile(r"[,\s]+")
EFFORT_HEADER = "x-ccr-reasoning-effort"
VERSION = 5
RETRYABLE_STATUSES = {401, 403, 408, 409, 425, 429}
BAD_KEY_STATUSES = {400, 401, 403}
COOLDOWN_STATUSES = BAD_KEY_STATUSES | {429}
# ccr abandons a request to this router after its own API_TIMEOUT_MS, read as
# 600000 from the app_config row of ~/.claude-code-router/config.sqlite and
# applied by its gateway as upstreamTimeoutMs on the provider call.
CLIENT_TIMEOUT_SECONDS = 600
# Sit just under the caller's patience. This is deliberately not a claim about
# how long a generation takes: for a non-streaming generateContent the response
# headers do not arrive until the model is essentially done, so a shorter
# timeout would not be detecting a stuck connection, it would be abandoning a
# generation that is still running and still being paid for while the retry
# starts a second one. Expiring only after the caller has already given up
# means at most one generation is ever in flight per request. The cost is that
# a genuinely dead connection now holds its slot for the full timeout instead
# of a minute, which the caller's own timeout bounds.
UPSTREAM_ATTEMPT_TIMEOUT = CLIENT_TIMEOUT_SECONDS - 30
BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_DELAY_SECONDS = 60
BACKOFF_JITTER = 0.25
# A streamed body that goes silent: Gemini sometimes stops after the thought
# parts and never ends the stream (corp fork transcripts, 2026-09-09: last
# record a thinking-only response, then nothing until the 600 s fork kill).
# The CLI recovers from an ended stream in seconds, so end it for it.
STREAM_IDLE_TIMEOUT_SECONDS = 30
LOG = logging.getLogger("gemini-key-router")
# A stream that ends with thoughts only and no finishReason is Google closing
# it early; keep the request so it can be replayed against Google directly.
TRUNCATED_DIR = Path.home() / ".cache" / "gemini-key-router" / "truncated"
FINISH_REASON_RE = re.compile(rb'"finishReason"\s*:\s*"([A-Z_]+)"')
# A thought part is {"text": "...", "thought": true}; the flag follows the text.
THOUGHT_PART_RE = re.compile(rb'"thought"\s*:\s*true')
TEXT_PART_RE = re.compile(rb'"text"\s*:')
CLIENT_POLL_INTERVAL_SECONDS = 0.5
ATTEMPT_TRACE_LIMIT = 20
LONG_QUOTA_COOLDOWN = 24 * 60 * 60
REQUEST_HEADER_BLOCKLIST = {
    # Never ask Google to compress: is_retryable reads the error body for
    # API_KEY_INVALID, and a gzipped 400 hid the marker, so a bad key was
    # forwarded to the caller instead of rotated (2 of 60 requests, 2026-09-08).
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "transfer-encoding",
    "x-goog-api-key",
    EFFORT_HEADER,
}
RESPONSE_HEADER_BLOCKLIST = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

cooldowns = {}
# (key, scope) pairs cooling for a key-specific reason (bad credential, quota
# spent for the day): a round never waits for these to recover.
terminal_cooldowns = set()
cooldown_lock = threading.Lock()
random_source = random.SystemRandom()


def load_keys():
    keys = []
    if KEY_FILE.is_file():
        keys.extend(
            key for key in KEY_SPLIT_PATTERN.split(KEY_FILE.read_text())
            if len(key) >= 20
        )
    preferred = os.environ.get("GEMINI_API_KEY", "").strip()
    if len(preferred) >= 20:
        keys.append(preferred)
    return list(dict.fromkeys(keys))


def load_router_token():
    if not TOKEN_FILE.is_file():
        raise RuntimeError(f"Missing router token: {TOKEN_FILE}")
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise RuntimeError(f"Empty router token: {TOKEN_FILE}")
    return token


def ordered_keys(scope):
    keys = load_keys()
    now = time.monotonic()
    with cooldown_lock:
        for cooldown_key in list(cooldowns):
            key, _scope = cooldown_key
            if cooldowns[cooldown_key] <= now or key not in keys:
                cooldowns.pop(cooldown_key, None)
                terminal_cooldowns.discard(cooldown_key)
        available = [key for key in keys if (key, scope) not in cooldowns]
        if not available and keys:
            # Every key is cooling. Hand back the least-cooled one anyway: a
            # success on it calls clear_failure, which is the only way a fully
            # cooled pool recovers short of waiting the cooldowns out or
            # restarting. Do not delete this as dead code — attempt_keys uses
            # uncooled_keys, never this, to decide a sequence is exhausted.
            available = [
                min(keys, key=lambda key: cooldowns.get((key, scope), now))
            ]
    random_source.shuffle(available)
    return available


def mark_failure(key, scope, status=None, retry_after=None, body=b""):
    if status == 429:
        text = body.decode(errors="replace").lower()
        if "exceeded your current quota" in text or "prepayment credits" in text:
            delay = LONG_QUOTA_COOLDOWN
        else:
            delay = parse_retry_after(retry_after) or 60
    elif status in {400, 401, 403}:
        delay = 3600
    else:
        delay = 30
    with cooldown_lock:
        cooldowns[(key, scope)] = time.monotonic() + delay
        if status in BAD_KEY_STATUSES or delay == LONG_QUOTA_COOLDOWN:
            terminal_cooldowns.add((key, scope))
        else:
            terminal_cooldowns.discard((key, scope))


def clear_failure(key, scope):
    with cooldown_lock:
        cooldowns.pop((key, scope), None)
        terminal_cooldowns.discard((key, scope))


def recoverable_keys(scope):
    """Keys that are usable now or cooling for a passing reason (429, 5xx)."""
    keys = load_keys()
    now = time.monotonic()
    with cooldown_lock:
        return [
            key for key in keys
            if cooldowns.get((key, scope), now) <= now
            or (key, scope) not in terminal_cooldowns
        ]


def recoverable_cooldown_remaining(scope):
    """Seconds until some non-terminal key is usable again; 0 if one already is."""
    keys = load_keys()
    now = time.monotonic()
    with cooldown_lock:
        waits = [
            max(0.0, cooldowns.get((key, scope), now) - now)
            for key in keys if (key, scope) not in terminal_cooldowns
        ]
    return min(waits) if waits else 0.0


def parse_retry_after(value):
    try:
        return max(1, min(600, int(float(value))))
    except (TypeError, ValueError):
        return None


def is_retryable(status, body):
    if status in RETRYABLE_STATUSES or status >= 500:
        return True
    if status != 400:
        return False
    text = body.decode(errors="replace")
    return "API_KEY_INVALID" in text or "API key not valid" in text


def upstream_url(path):
    parsed = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(name, value) for name, value in query if name.lower() != "key"]
    return urllib.parse.urlunsplit((
        "https",
        "generativelanguage.googleapis.com",
        parsed.path,
        urllib.parse.urlencode(query),
        "",
    ))


def gemini_model(path):
    match = re.search(r"/models/([^:/?]+)", urllib.parse.urlsplit(path).path)
    return match.group(1) if match else ""


def gemini_thinking_level(path, effort):
    model = gemini_model(path)
    if not model.startswith("gemini-3") or not effort:
        return None
    normalized = re.sub(r"[-_\s]+", "", effort.strip().lower())
    if normalized in {"xhigh", "max", "ultra"}:
        return "high"
    if normalized in {"low", "medium", "high"}:
        if "flash-lite-image" in model and normalized in {"low", "medium"}:
            return "high"
        return normalized
    return None


def apply_gemini_thinking_level(path, body, effort):
    level = gemini_thinking_level(path, effort)
    if body is None or level is None:
        return body, None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return body, None
    if not isinstance(payload, dict):
        return body, None
    generation_config = payload.setdefault("generationConfig", {})
    if not isinstance(generation_config, dict):
        return body, None
    thinking_config = generation_config.setdefault("thinkingConfig", {})
    if not isinstance(thinking_config, dict):
        return body, None
    if "thinkingLevel" in thinking_config or any(
        key in thinking_config
        for key in ("thinkingBudget", "thinking_budget", "budgetTokens", "budget_tokens")
    ):
        return body, None
    thinking_config["thinkingLevel"] = level
    return json.dumps(payload, separators=(",", ":")).encode(), level


def key_id(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def backoff_delay(failure_index):
    """Capped exponential backoff with jitter for the failure_index-th failure.

    The delay doubles from BACKOFF_BASE_SECONDS and then plateaus at
    BACKOFF_MAX_DELAY_SECONDS; it is the delay that is bounded, never the
    number of attempts or the total duration.
    """
    exponent = min(max(failure_index, 1) - 1, 32)
    base = min(BACKOFF_MAX_DELAY_SECONDS, BACKOFF_BASE_SECONDS * (2 ** exponent))
    return base * random_source.uniform(1 - BACKOFF_JITTER, 1 + BACKOFF_JITTER)


def retry_delay(failure_index, retry_after):
    """Seconds to wait before the next attempt.

    An explicit Retry-After is honoured verbatim even when it exceeds
    BACKOFF_MAX_DELAY_SECONDS, because the server named that number itself.
    """
    if retry_after is not None:
        return float(retry_after)
    return backoff_delay(failure_index)


def uncooled_keys(scope):
    """Keys that are not in cooldown right now.

    Unlike ordered_keys this has no least-cooled fallback, so an empty result
    genuinely means every key is cooling.
    """
    keys = load_keys()
    now = time.monotonic()
    with cooldown_lock:
        return [key for key in keys if cooldowns.get((key, scope), now) <= now]


def cooldown_remaining(scope):
    """Seconds until the soonest key leaves cooldown, or None if none are configured."""
    keys = load_keys()
    if not keys:
        return None
    now = time.monotonic()
    with cooldown_lock:
        soonest = min(cooldowns.get((key, scope), now) for key in keys)
    return max(0.0, soonest - now)


def attempt_keys(scope, keys):
    """Yield keys to try, cycling the pool so one dead key never parks a retry.

    This is unbounded on purpose for server-side failures, which never cool a
    key, so an outage is retried for as long as the caller waits. It ends only
    when every key is cooling, and because only COOLDOWN_STATUSES cool a key
    that means every key failed for a key-specific reason: a broken credential
    set rather than an outage, which dies loudly instead of hanging.

    cooldowns is process-wide and keyed by (key, scope), so that verdict is
    about the process and not about this request. A request whose own attempts
    were all 5xx can still end here because concurrent requests cooled the pool
    with their 429s or 401s, and its attempt_trace will show only 5xx.
    """
    round_index = 0
    while keys:
        for key in keys:
            yield round_index, key
        if not recoverable_keys(scope):
            return
        keys = ordered_keys(scope)
        round_index += 1


def attempt_record(attempt, key, outcome, started_at, **details):
    return {
        "attempt": attempt,
        "key": key_id(key),
        "outcome": outcome,
        "elapsed_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        **details,
    }


def append_attempt(trace, record):
    trace.append(record)
    if len(trace) > ATTEMPT_TRACE_LIMIT:
        del trace[0]


def request_excerpt(body, limit=70):
    """The start of the newest user-role text in a generateContent body, for the
    per-request journal line; empty when the body is not that shape."""
    try:
        payload = json.loads(body or b"")
        contents = payload["contents"]
        latest = next(c for c in reversed(contents) if c.get("role", "user") == "user")
        text = " ".join(
            p["text"] for p in latest.get("parts", []) if isinstance(p.get("text"), str)
        )
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
        return ""
    return " ".join(text.split())[:limit]


def stream_socket(response):
    """The upstream socket under an HTTPResponse, so a per-read idle timeout
    can be set once the headers have arrived."""
    return response.fp.raw._sock


def abort_connection(handler):
    """Abort a client socket with TCP RST so truncation is unmistakable.

    Close only the underlying socket, not handler.wfile or rfile. With
    wbufsize=0 the writer is unbuffered and StreamRequestHandler.finish
    guards its flush with 'if not self.wfile.closed', whereas closing wfile
    here makes BaseHTTPRequestHandler.handle_one_request's unguarded post-method
    flush raise ValueError on closed file.
    """
    if handler is None:
        return
    conn = getattr(handler, "connection", None)
    if conn is not None and hasattr(conn, "setsockopt"):
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except (AttributeError, OSError):
            pass
        try:
            conn.close()
        except (AttributeError, OSError):
            pass


class GeminiKeyRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_PUT(self):
        self.forward()

    def do_PATCH(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def log_message(self, _format, *_args):
        return

    def client_gone(self):
        """True once the caller's socket has reached EOF, so nobody is waiting.

        A closed peer makes the socket readable with nothing to read; a live
        caller is either quiet (not readable) or has sent a pipelined byte we
        can peek at without consuming. This assumes callers do not half-close:
        a caller that shuts down its write side and then waits for the response
        reads as gone here. Nothing between cc-gemini, ccr and this port does
        that, but a caller that did would be abandoned.
        """
        connection = getattr(self, "connection", None)
        if connection is None:
            return True
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if not readable:
                return False
            return connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def wait_before_retry(self, delay):
        """Wait `delay` seconds in short slices, watching the caller between them.

        Returns False as soon as the caller disconnects. A disconnect is the
        only thing that ends a retry sequence, so it has to be noticed while
        waiting and not just between attempts.
        """
        remaining = float(delay)
        while remaining > 0:
            if self.client_gone():
                return False
            slice_seconds = min(remaining, CLIENT_POLL_INTERVAL_SECONDS)
            time.sleep(slice_seconds)
            remaining -= slice_seconds
        return not self.client_gone()

    def forward(self):
        token = load_router_token()
        parsed = urllib.parse.urlsplit(self.path)
        query_keys = urllib.parse.parse_qs(parsed.query).get("key", [])
        provided = self.headers.get("x-goog-api-key", "")
        requested_effort = self.headers.get(EFFORT_HEADER, "")
        if not provided and query_keys:
            provided = query_keys[0]
        if not hmac.compare_digest(provided, token):
            self.send_json(401, {"error": "Unauthorized"})
            return

        if self.path == "/__ccr_gemini_key_router__/health":
            keys = load_keys()
            self.send_json(200, {
                "ok": True,
                "version": VERSION,
                "keys": len(keys),
            })
            return

        scope = urllib.parse.urlsplit(self.path).path
        keys = ordered_keys(scope)
        if not keys:
            self.send_json(503, {"error": "No Gemini API keys are configured"})
            return

        length = int(self.headers.get("content-length", "0") or 0)
        body = self.rfile.read(length) if length else None
        excerpt = request_excerpt(body)
        agent = (self.headers.get("user-agent") or "-").split(" ")[0][:40]
        body, applied_thinking_level = apply_gemini_thinking_level(
            self.path, body, requested_effort
        )
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in REQUEST_HEADER_BLOCKLIST
        }
        url = upstream_url(self.path)
        attempt_trace = []
        attempts = 0
        current_round = 0
        round_retry_after = None
        exhaustion_reason = None

        # Same policy as veoplace's query_gemini_local: one round tries every
        # key back to back, and only a round that fails on every key sleeps,
        # for the server's Retry-After if one was named, else the backoff for
        # that round; keys are never waited on one at a time.
        for round_index, key in attempt_keys(scope, keys):
            if round_index != current_round:
                gone = not self.wait_before_retry(max(
                    retry_delay(round_index, round_retry_after),
                    recoverable_cooldown_remaining(scope),
                ))
                current_round = round_index
                round_retry_after = None
            else:
                gone = self.client_gone()
            if gone:
                exhaustion_reason = "client_disconnected"
                break
            attempts += 1
            attempt_started = time.monotonic()
            request = urllib.request.Request(
                url,
                data=body,
                headers={**headers, "x-goog-api-key": key},
                method=self.command,
            )
            try:
                response = urllib.request.urlopen(
                    request, timeout=UPSTREAM_ATTEMPT_TIMEOUT
                )
            except urllib.error.HTTPError as error:
                error_body = error.read()
                if not is_retryable(error.code, error_body):
                    self.send_upstream(error.code, error.headers, error_body, attempts)
                    return
                retry_after = error.headers.get("retry-after")
                if error.code in COOLDOWN_STATUSES:
                    mark_failure(key, scope, error.code, retry_after, error_body)
                named = parse_retry_after(retry_after)
                if named is not None:
                    round_retry_after = max(round_retry_after or 0.0, float(named))
                append_attempt(attempt_trace, attempt_record(
                    attempts, key, "retryable_http", attempt_started,
                    status=error.code,
                ))
                continue
            except (OSError, urllib.error.URLError) as error:
                # A transport failure reaching the upstream says nothing about
                # the key, so like a 5xx it must not cool one.
                append_attempt(attempt_trace, attempt_record(
                    attempts, key, "network_error", attempt_started,
                    error=type(error).__name__,
                ))
                continue

            clear_failure(key, scope)
            streaming = "streamGenerateContent" in scope
            if streaming:
                try:
                    stream_socket(response).settimeout(STREAM_IDLE_TIMEOUT_SECONDS)
                except AttributeError as error:
                    LOG.warning("no upstream socket for idle timeout: %s", error)
            body_bytes = 0
            body_end = "complete"
            finish_reason = "-"
            thought_parts = 0
            text_parts = 0
            try:
                self.send_response(response.status)
                for name, value in response.headers.items():
                    if name.lower() not in RESPONSE_HEADER_BLOCKLIST:
                        self.send_header(name, value)
                self.send_header("connection", "close")
                self.send_header("x-gemini-key-router-attempts", str(attempts))
                self.send_header("x-gemini-key-router-key", key_id(key))
                if applied_thinking_level:
                    self.send_header(
                        "x-gemini-key-router-thinking-level", applied_thinking_level
                    )
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
                return

            while True:
                try:
                    # read(amt) on a chunked response blocks until amt bytes or end of stream.
                    # read1 returns each chunk as it arrives.
                    chunk = response.read1(65536)
                except Exception as error:
                    # Any failure to read the upstream body after headers were sent
                    # must reach the client as a connection reset; narrowing this to
                    # specific errors would reintroduce silent stream truncation.
                    body_end = (
                        "idle_timeout" if isinstance(error, socket.timeout)
                        else f"read_error:{type(error).__name__}"
                    )
                    abort_connection(self)
                    break
                if not chunk:
                    break
                body_bytes += len(chunk)
                # What the stream carried, for the journal: a response that is
                # all thought parts and no text ends the CLI's turn with nothing.
                for match in FINISH_REASON_RE.finditer(chunk):
                    finish_reason = match.group(1).decode()
                thought_parts += len(THOUGHT_PART_RE.findall(chunk))
                text_parts += len(TEXT_PART_RE.findall(chunk)) - len(THOUGHT_PART_RE.findall(chunk))
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    body_end = "client_gone"
                    break
            saved = ""
            if body_end == "complete" and finish_reason == "-" and text_parts <= 0 and body:
                TRUNCATED_DIR.mkdir(parents=True, exist_ok=True)
                saved_path = TRUNCATED_DIR / f"{time.strftime('%H%M%S')}-{key_id(key)}.json"
                fd = os.open(saved_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(body)
                saved = f" request_saved={saved_path}"
            LOG.info(
                "%s key=%s status=%s attempts=%d bytes=%d elapsed_ms=%d end=%s "
                "finish=%s thought_parts=%d text_parts=%d ua=%s q=%r%s",
                scope.rsplit("/", 1)[-1], key_id(key), response.status, attempts,
                body_bytes, int((time.monotonic() - attempt_started) * 1000), body_end,
                finish_reason, thought_parts, max(0, text_parts), agent, excerpt, saved,
            )
            self.close_connection = True
            return

        if exhaustion_reason == "client_disconnected":
            self.close_connection = True
            return

        remaining = cooldown_remaining(scope)
        self.send_json(502, {
            "error": "Gemini request has no usable API key to retry with",
            "reason": (
                "no_keys_available" if remaining is None else "all_keys_cooling"
            ),
            "cooldown_seconds_remaining": (
                None if remaining is None else round(remaining, 1)
            ),
            "attempts": attempts,
            "attempt_trace": attempt_trace,
            "attempt_trace_truncated": attempts > len(attempt_trace),
            "backoff_max_delay_seconds": BACKOFF_MAX_DELAY_SECONDS,
            "upstream_attempt_timeout_seconds": UPSTREAM_ATTEMPT_TIMEOUT,
        }, extra_headers={
            "x-gemini-key-router-attempts": str(attempts),
        })

    def send_upstream(self, status, headers, body, attempts):
        try:
            self.send_response(status)
            for name, value in headers.items():
                if name.lower() not in RESPONSE_HEADER_BLOCKLIST:
                    self.send_header(name, value)
            self.send_header("connection", "close")
            self.send_header("x-gemini-key-router-attempts", str(attempts))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("connection", "close")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_router_token()
    if not load_keys():
        raise RuntimeError(
            f"No Gemini API keys found in {KEY_FILE} or GEMINI_API_KEY"
        )
    server = ThreadingHTTPServer((HOST, PORT), GeminiKeyRouterHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
