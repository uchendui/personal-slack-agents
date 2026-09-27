#!/usr/bin/env python3

import importlib.util
import io
import socket
import threading
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "tools" / "gemini-key-router.py"
SPEC = importlib.util.spec_from_file_location("gemini_key_router", MODULE_PATH)
gemini_key_router = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gemini_key_router)


def http_error(status, retry_after=None, body=b'{"error":"retryable"}'):
    headers = Message()
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com",
        status,
        "upstream",
        headers,
        io.BytesIO(body),
    )


class FakeResponse:
    """The shape forward()'s success path uses: status, headers, read()."""

    def __init__(self, body=b'{"candidates":[]}', status=200):
        self.status = status
        self.headers = Message()
        self.headers["content-type"] = "application/json"
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def read1(self, size=-1):
        return self._body.read(size)


class FakeClock:
    """A monotonic clock the test advances by hand, so nothing sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def raising(*args, **kwargs):
    """A urlopen side effect that raises a FRESH http_error on every call.

    Reusing one HTTPError would not do: forward() calls error.read(), which
    drains its body stream, so a second attempt would see an empty body and a
    400 carrying API_KEY_INVALID would stop looking retryable.
    """
    def raise_it(*_args, **_kwargs):
        raise http_error(*args, **kwargs)
    return raise_it


class RouterTestCase(unittest.TestCase):
    """Base case that keeps the module's global cooldown state out of tests."""

    def setUp(self):
        gemini_key_router.cooldowns.clear()
        self.addCleanup(gemini_key_router.cooldowns.clear)

    def make_handler(self):
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.path = "/v1beta/models/gemini-flash-latest:generateContent"
        handler.headers = {
            "x-goog-api-key": "router-token",
            "content-length": "0",
        }
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.command = "POST"
        handler.requestline = f"POST {handler.path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.send_json = mock.Mock()
        handler.send_upstream = mock.Mock()
        handler.client_gone = mock.Mock(return_value=False)
        return handler

    def run_forward(self, handler, pool, urlopen_side_effect, waits):
        """Drive forward() against the REAL ordered_keys, cooldowns and mark_failure.

        Only load_keys is patched, so the key pool is a literal but every
        cooldown decision on the retry path is the production one. `waits`
        collects each computed delay in place of a real sleep.
        """
        def record_wait(delay):
            waits.append(delay)
            return not handler.client_gone()

        handler.wait_before_retry = mock.Mock(side_effect=record_wait)
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router, "load_keys", side_effect=lambda: list(pool)
            ),
            mock.patch.object(gemini_key_router.time, "sleep") as sleep,
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                side_effect=urlopen_side_effect,
            ) as urlopen,
        ):
            handler.forward()
        return urlopen, sleep

    def cooled(self):
        return sorted(key for key, _scope in gemini_key_router.cooldowns)


class RetryPolicyTest(RouterTestCase):
    def test_delay_is_bounded_and_attempts_are_not(self):
        self.assertEqual(gemini_key_router.VERSION, 5)
        self.assertEqual(gemini_key_router.BACKOFF_BASE_SECONDS, 1)
        self.assertEqual(gemini_key_router.BACKOFF_MAX_DELAY_SECONDS, 60)
        self.assertEqual(gemini_key_router.BACKOFF_JITTER, 0.25)
        self.assertEqual(gemini_key_router.CLIENT_TIMEOUT_SECONDS, 600)
        self.assertEqual(gemini_key_router.UPSTREAM_ATTEMPT_TIMEOUT, 570)
        self.assertLess(
            gemini_key_router.UPSTREAM_ATTEMPT_TIMEOUT,
            gemini_key_router.CLIENT_TIMEOUT_SECONDS,
            "the caller must give up before the router does",
        )
        self.assertEqual(gemini_key_router.COOLDOWN_STATUSES, {400, 401, 403, 429})

    def test_backoff_grows_then_plateaus_at_the_maximum(self):
        with mock.patch.object(
            gemini_key_router.random_source, "uniform", return_value=1.0
        ):
            delays = [gemini_key_router.backoff_delay(n) for n in range(1, 13)]
        self.assertEqual(delays[:7], [1, 2, 4, 8, 16, 32, 60])
        self.assertEqual(delays[7:], [60] * 5)

    def test_jitter_is_applied_and_never_breaks_the_growth(self):
        samples = [gemini_key_router.backoff_delay(5) for _ in range(40)]
        self.assertGreater(len(set(samples)), 1)
        self.assertTrue(all(12.0 <= sample <= 20.0 for sample in samples))
        for index in range(1, 6):
            self.assertLess(
                gemini_key_router.backoff_delay(index),
                gemini_key_router.backoff_delay(index + 1),
            )
        self.assertLessEqual(gemini_key_router.backoff_delay(40), 75.0)

    def test_retry_after_is_honoured_in_full_above_the_maximum_delay(self):
        self.assertEqual(gemini_key_router.retry_delay(1, 120), 120.0)
        handler = self.make_handler()
        pool = ["key-one-abcdefghijklmnop", "key-two-abcdefghijklmnop"]
        waits = []
        def rate_limited_until_client_leaves(*_args, **_kwargs):
            if holder["mock"].call_count >= 2:
                handler.client_gone.return_value = True
            raise http_error(429, retry_after=120)
        holder = {}
        def record_wait(delay):
            waits.append(delay)
            return not handler.client_gone()
        handler.wait_before_retry = mock.Mock(side_effect=record_wait)
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router, "load_keys", side_effect=lambda: list(pool)
            ),
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                side_effect=rate_limited_until_client_leaves,
            ) as urlopen,
        ):
            holder["mock"] = urlopen
            handler.forward()
        # Both keys are tried back to back; the one wait comes after the round.
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(waits, [120.0])

    def test_a_round_tries_every_key_before_any_wait(self):
        handler = self.make_handler()
        pool = [f"key-{index}-abcdefghijklmnop" for index in range(3)]
        waits = []
        calls = {"n": 0}
        def two_rate_limits_then_success(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise http_error(429)
            return FakeResponse(b'{"ok":true}')
        urlopen, _ = self.run_forward(handler, pool, two_rate_limits_then_success, waits)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(waits, [])

    def test_thirty_consecutive_failures_keep_retrying_across_the_key_pool(self):
        handler = self.make_handler()
        pool = [f"key-{index}-abcdefghijklmnop" for index in range(4)]
        waits = []

        def fail_until_client_leaves(*_args, **_kwargs):
            if holder["mock"].call_count >= 30:
                handler.client_gone.return_value = True
            raise http_error(503)

        holder = {}
        handler.wait_before_retry = mock.Mock(
            side_effect=lambda delay: waits.append(delay) is None
            and not handler.client_gone()
        )
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router, "load_keys", side_effect=lambda: list(pool)
            ),
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                side_effect=fail_until_client_leaves,
            ) as urlopen,
        ):
            holder["mock"] = urlopen
            handler.forward()

        self.assertEqual(urlopen.call_count, 30)
        self.assertGreater(urlopen.call_count, len(pool) * 7 - 1)
        # 30 attempts over 4 keys is 8 rounds; only the 7 round changes wait,
        # and the wait doubles per failed round up to the cap.
        self.assertEqual(len(waits), 7)
        self.assertLessEqual(waits[0], 1.25)
        self.assertLessEqual(waits[-1], 75.0)
        self.assertEqual(self.cooled(), [])
        self.assertTrue(handler.close_connection)
        handler.send_json.assert_not_called()

    def test_server_errors_never_cool_down_a_key_but_rate_limits_do(self):
        for status, expected in ((503, []), (500, []), (429, ["key-one"])):
            with self.subTest(status=status):
                gemini_key_router.cooldowns.clear()
                handler = self.make_handler()
                waits = []
                calls = {"n": 0}

                def fail_once(*_args, **_kwargs):
                    calls["n"] += 1
                    handler.client_gone.return_value = True
                    raise http_error(status)

                urlopen, _ = self.run_forward(
                    handler, ["key-one"], fail_once, waits
                )
                self.assertEqual(urlopen.call_count, 1)
                self.assertEqual(self.cooled(), expected)

    def test_transport_failures_never_cool_down_a_key(self):
        handler = self.make_handler()
        waits = []
        calls = {"n": 0}

        def fail_once(*_args, **_kwargs):
            calls["n"] += 1
            handler.client_gone.return_value = True
            raise urllib.error.URLError("name resolution failed")

        urlopen, _ = self.run_forward(handler, ["key-one"], fail_once, waits)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.cooled(), [])
        self.assertEqual(len(waits), 1)

    def test_every_key_cooling_from_a_bad_key_status_fails_loudly_and_fast(self):
        cases = (
            (401, b'{"error":"unauthorized"}'),
            (403, b'{"error":"forbidden"}'),
            (400, b'{"error":{"status":"API_KEY_INVALID"}}'),
        )
        for status, body in cases:
            with self.subTest(status=status):
                gemini_key_router.cooldowns.clear()
                handler = self.make_handler()
                pool = [f"key-{index}-abcdefghijklmnop" for index in range(4)]
                waits = []
                urlopen, sleep = self.run_forward(
                    handler, pool, raising(status, body=body), waits
                )

                self.assertEqual(urlopen.call_count, len(pool))
                self.assertEqual(waits, [])
                sleep.assert_not_called()
                self.assertEqual(len(self.cooled()), len(pool))
                sent_status, payload = handler.send_json.call_args.args
                self.assertEqual(sent_status, 502)
                self.assertEqual(payload["reason"], "all_keys_cooling")
                self.assertEqual(payload["attempts"], len(pool))
                self.assertGreater(payload["cooldown_seconds_remaining"], 0)
                self.assertNotIn(pool[0], str(payload))

    def test_no_keys_left_in_the_file_fails_loudly_with_its_own_reason(self):
        handler = self.make_handler()
        pool = ["key-one-abcdefghijklmnop"]
        waits = []

        def fail_and_empty_the_file(*_args, **_kwargs):
            pool.clear()
            raise http_error(503)

        urlopen, _ = self.run_forward(handler, pool, fail_and_empty_the_file, waits)
        self.assertEqual(urlopen.call_count, 1)
        status, payload = handler.send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["reason"], "no_keys_available")
        self.assertIsNone(payload["cooldown_seconds_remaining"])

    def test_a_slow_response_that_eventually_returns_is_not_retried(self):
        handler = self.make_handler()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        clock = FakeClock()
        waits = []

        def slow_success(*_args, **kwargs):
            # Five minutes of thinking: far past the old 60 second timeout and
            # still well inside the 570 second one.
            self.assertEqual(kwargs["timeout"], 570)
            clock.now += 300.0
            return FakeResponse()

        with mock.patch.object(gemini_key_router.time, "monotonic", clock):
            urlopen, sleep = self.run_forward(
                handler, ["key-one-abcdefghijklmnop"], slow_success, waits
            )

        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(waits, [])
        sleep.assert_not_called()
        self.assertEqual(handler.wfile.getvalue(), b'{"candidates":[]}')
        self.assertLess(300.0, gemini_key_router.UPSTREAM_ATTEMPT_TIMEOUT)

    def test_a_dead_connection_is_still_retried(self):
        cases = (
            ("connect failure", ConnectionRefusedError("connection refused")),
            ("socket timeout", TimeoutError("timed out")),
            ("dns failure", urllib.error.URLError("name resolution failed")),
        )
        for label, error in cases:
            with self.subTest(failure=label):
                gemini_key_router.cooldowns.clear()
                handler = self.make_handler()
                waits = []

                def fail_twice(*_args, **_kwargs):
                    if holder["mock"].call_count >= 2:
                        handler.client_gone.return_value = True
                    raise error

                holder = {}
                handler.wait_before_retry = mock.Mock(
                    side_effect=lambda delay: waits.append(delay) is None
                    and not handler.client_gone()
                )
                with (
                    mock.patch.object(
                        gemini_key_router,
                        "load_router_token",
                        return_value="router-token",
                    ),
                    mock.patch.object(
                        gemini_key_router,
                        "load_keys",
                        side_effect=lambda: ["key-one-abcdefghijklmnop"],
                    ),
                    mock.patch.object(
                        gemini_key_router.urllib.request,
                        "urlopen",
                        side_effect=fail_twice,
                    ) as urlopen,
                ):
                    holder["mock"] = urlopen
                    handler.forward()

                self.assertEqual(urlopen.call_count, 2)
                self.assertEqual(len(waits), 2)
                self.assertEqual(self.cooled(), [])

    def test_invalid_key_is_rotated_even_when_the_caller_accepts_gzip(self):
        import gzip
        handler = self.make_handler()
        handler.headers["accept-encoding"] = "gzip, deflate"
        invalid = b'{"error":{"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}'

        def upstream(request, timeout=None):
            # Google honours accept-encoding on error bodies too.
            if request.get_header("Accept-encoding"):
                headers = Message()
                headers["content-encoding"] = "gzip"
                raise urllib.error.HTTPError(
                    request.full_url, 400, "upstream", headers, io.BytesIO(gzip.compress(invalid))
                )
            if request.get_header("X-goog-api-key") == "key-one-abcdefghijklmnop":
                raise http_error(400, body=invalid)
            return FakeResponse(b'{"ok":true}')

        waits = []
        # ordered_keys shuffles the pool; the bad key must be tried first.
        with mock.patch.object(gemini_key_router.random_source, "shuffle"):
            urlopen, _ = self.run_forward(
                handler,
                ["key-one-abcdefghijklmnop", "key-two-abcdefghijklmnop"],
                upstream,
                waits,
            )
        self.assertEqual(urlopen.call_count, 2)
        handler.send_upstream.assert_not_called()
        self.assertEqual(self.cooled(), ["key-one-abcdefghijklmnop"])

    def test_non_retryable_status_returns_immediately_without_sleeping(self):
        handler = self.make_handler()
        waits = []
        urlopen, sleep = self.run_forward(
            handler,
            ["key-one-abcdefghijklmnop"],
            raising(404, body=b"nope"),
            waits,
        )
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(waits, [])
        sleep.assert_not_called()
        self.assertEqual(self.cooled(), [])
        handler.send_upstream.assert_called_once()
        self.assertEqual(handler.send_upstream.call_args.args[0], 404)


class ClientWatchTest(RouterTestCase):
    def test_client_gone_distinguishes_quiet_live_and_closed_callers(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.connection = server

        self.assertFalse(handler.client_gone())
        client.sendall(b"x")
        self.assertFalse(handler.client_gone())
        server.recv(1)
        client.close()
        self.assertTrue(handler.client_gone())

        server.close()
        self.assertTrue(handler.client_gone())

    def test_wait_before_retry_slices_the_wait_and_stops_on_disconnect(self):
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.client_gone = mock.Mock(return_value=False)
        with mock.patch.object(gemini_key_router.time, "sleep") as sleep:
            self.assertTrue(handler.wait_before_retry(2.0))
        slices = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(sum(slices), 2.0)
        self.assertTrue(
            all(s <= gemini_key_router.CLIENT_POLL_INTERVAL_SECONDS for s in slices)
        )

        handler.client_gone = mock.Mock(side_effect=[False, False, True])
        with mock.patch.object(gemini_key_router.time, "sleep") as sleep:
            self.assertFalse(handler.wait_before_retry(60.0))
        self.assertEqual(sleep.call_count, 2)

    def test_disconnected_caller_ends_the_sequence_and_returns(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.path = "/v1beta/models/gemini-flash-latest:generateContent"
        handler.headers = {"x-goog-api-key": "router-token", "content-length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.command = "POST"
        handler.connection = server
        handler.send_json = mock.Mock()
        handler.send_upstream = mock.Mock()

        def fail_and_hang_up(*_args, **_kwargs):
            if holder["mock"].call_count >= 3:
                client.close()
            raise http_error(503)

        holder = {}
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router,
                "load_keys",
                side_effect=lambda: ["key-one-abcdefghijklmnop"],
            ),
            mock.patch.object(gemini_key_router.time, "sleep") as sleep,
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                side_effect=fail_and_hang_up,
            ) as urlopen,
        ):
            holder["mock"] = urlopen
            handler.forward()

        self.assertEqual(urlopen.call_count, 3)
        self.assertTrue(handler.close_connection)
        handler.send_json.assert_not_called()
        slept = sum(call.args[0] for call in sleep.call_args_list)
        self.assertTrue(2.25 <= slept <= 3.75, slept)


def tcp_pair():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    server, _ = srv.accept()
    srv.close()
    return server, client


class StreamTruncationTest(RouterTestCase):
    """Verifies socket-level stream outcomes (complete body vs mid-stream TCP RST).

    Note: these tests drive handler.forward() directly to verify socket-level
    data and reset semantics rather than running the framework's post-method
    handle_one_request loop.
    """

    def test_a_streamed_body_silent_past_the_idle_timeout_is_aborted(self):
        handler = self.make_handler()
        handler.path = "/v1beta/models/gemini-flash-latest:streamGenerateContent?alt=sse"
        handler.requestline = f"POST {handler.path} HTTP/1.1"
        pool = ["key-one-abcdefghijklmnop"]
        sock = mock.Mock()
        class StalledResponse(FakeResponse):
            def __init__(self):
                super().__init__(b"data: first\n\n")
                self.reads = 0
            def read1(self, size=-1):
                self.reads += 1
                if self.reads == 1:
                    return self._body.read(size)
                raise socket.timeout("timed out")
        with (
            mock.patch.object(gemini_key_router, "stream_socket", return_value=sock),
            mock.patch.object(gemini_key_router, "abort_connection") as abort,
        ):
            self.run_forward(handler, pool, lambda *a, **k: StalledResponse(), [])
        sock.settimeout.assert_called_once_with(
            gemini_key_router.STREAM_IDLE_TIMEOUT_SECONDS
        )
        abort.assert_called_once_with(handler)
        self.assertIn(b"data: first", handler.wfile.getvalue())

    def test_normal_complete_stream_is_untouched(self):
        server, client = tcp_pair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.path = "/v1beta/models/gemini-flash-latest:generateContent"
        handler.headers = {"x-goog-api-key": "router-token", "content-length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = server.makefile("wb")
        handler.command = "POST"
        handler.requestline = f"POST {handler.path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.connection = server
        handler.send_json = mock.Mock()
        handler.send_upstream = mock.Mock()
        handler.client_gone = mock.Mock(return_value=False)

        fake_response = FakeResponse(
            body=b'{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'
        )
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router,
                "load_keys",
                side_effect=lambda: ["key-one-abcdefghijklmnop"],
            ),
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                return_value=fake_response,
            ),
        ):
            handler.forward()

        handler.wfile.close()
        server.close()

        client_file = client.makefile("rb")
        status_line = client_file.readline()
        self.assertIn(b"200 OK", status_line)
        while True:
            line = client_file.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        body = client_file.read()
        self.assertEqual(
            body, b'{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'
        )
        client_file.close()

    def test_streamed_chunks_reach_the_client_before_the_upstream_finishes(self):
        server, client = tcp_pair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
        handler.path = "/v1beta/models/gemini-flash-latest:generateContent"
        handler.headers = {"x-goog-api-key": "router-token", "content-length": "0"}
        handler.rfile = io.BytesIO()
        handler.wfile = server.makefile("wb")
        handler.command = "POST"
        handler.requestline = f"POST {handler.path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.connection = server
        handler.send_json = mock.Mock()
        handler.send_upstream = mock.Mock()
        handler.client_gone = mock.Mock(return_value=False)
        upstream_finished = threading.Event()
        first_chunk = b'{"first": true, '
        second_chunk = b'"second": true}'

        class ChunkedResponse:
            def __init__(self):
                self.status = 200
                self.headers = Message()
                self.headers["content-type"] = "application/json"
                self._chunks_sent = 0

            def read1(self, size=-1):
                if self._chunks_sent == 0:
                    self._chunks_sent += 1
                    return first_chunk
                if self._chunks_sent == 1:
                    upstream_finished.wait()
                    self._chunks_sent += 1
                    return second_chunk
                return b""

            def read(self, size=-1):
                chunks = []
                while size < 0 or sum(len(chunk) for chunk in chunks) < size:
                    chunk = self.read1(size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

        response = ChunkedResponse()
        with (
            mock.patch.object(
                gemini_key_router, "load_router_token", return_value="router-token"
            ),
            mock.patch.object(
                gemini_key_router,
                "load_keys",
                side_effect=lambda: ["key-one-abcdefghijklmnop"],
            ),
            mock.patch.object(
                gemini_key_router.urllib.request,
                "urlopen",
                return_value=response,
            ),
        ):
            forward_thread = threading.Thread(target=handler.forward)
            self.addCleanup(handler.wfile.close)
            self.addCleanup(forward_thread.join, 3)
            self.addCleanup(upstream_finished.set)
            forward_thread.start()

            client.settimeout(3)
            client_file = client.makefile("rb")
            self.addCleanup(client_file.close)
            self.assertIn(b"200 OK", client_file.readline())
            while True:
                line = client_file.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            self.assertEqual(client_file.read(len(first_chunk)), first_chunk)

            upstream_finished.set()
            forward_thread.join(3)
            self.assertFalse(forward_thread.is_alive())
            handler.wfile.close()
            server.close()
            self.assertEqual(client_file.read(len(second_chunk)), second_chunk)
            self.assertEqual(client_file.read(), b"")
            client_file.close()

    def test_upstream_raising_mid_body_aborts_connection_so_client_observes_failure(self):
        cases = (
            ("timeout error", TimeoutError("upstream timeout")),
            ("url error", urllib.error.URLError("connection reset")),
            ("connection reset", ConnectionResetError("upstream closed")),
        )
        for label, error in cases:
            with self.subTest(failure=label):
                server, client = tcp_pair()
                self.addCleanup(server.close)
                self.addCleanup(client.close)
                handler = object.__new__(gemini_key_router.GeminiKeyRouterHandler)
                handler.path = "/v1beta/models/gemini-flash-latest:generateContent"
                handler.headers = {
                    "x-goog-api-key": "router-token",
                    "content-length": "0",
                }
                handler.rfile = io.BytesIO()
                handler.wfile = server.makefile("wb")
                handler.command = "POST"
                handler.requestline = f"POST {handler.path} HTTP/1.1"
                handler.request_version = "HTTP/1.1"
                handler.connection = server
                handler.send_json = mock.Mock()
                handler.send_upstream = mock.Mock()
                handler.client_gone = mock.Mock(return_value=False)

                class ExplodingResponse:
                    def __init__(self):
                        self.status = 200
                        self.headers = Message()
                        self.headers["content-type"] = "application/json"
                        self._called = False

                    def read(self, size=-1):
                        if not self._called:
                            self._called = True
                            return b'{"partial": true, '
                        raise error

                    def read1(self, size=-1):
                        return self.read(size)

                with (
                    mock.patch.object(
                        gemini_key_router,
                        "load_router_token",
                        return_value="router-token",
                    ),
                    mock.patch.object(
                        gemini_key_router,
                        "load_keys",
                        side_effect=lambda: ["key-one-abcdefghijklmnop"],
                    ),
                    mock.patch.object(
                        gemini_key_router.urllib.request,
                        "urlopen",
                        return_value=ExplodingResponse(),
                    ),
                ):
                    handler.forward()

                handler.wfile.close()

                client_file = client.makefile("rb")
                status_line = client_file.readline()
                self.assertIn(b"200 OK", status_line)
                while True:
                    line = client_file.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break

                with self.assertRaises(ConnectionResetError):
                    while True:
                        chunk = client.recv(1024)
                        if not chunk:
                            self.fail("Client observed clean EOF on truncated stream")
                client_file.close()


if __name__ == "__main__":
    unittest.main()
