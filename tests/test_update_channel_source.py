"""Integration tests for authenticated channel metadata retrieval.

Real cryptographic signing/verification; only the HTTP opener is replaced.
"""

import errno
import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from socket import SHUT_RDWR
from threading import Event
from types import SimpleNamespace
from unittest import mock
from urllib.error import URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as fixture  # noqa: E402
from updates.catalog import CatalogError  # noqa: E402
from updates.channel_release import make_channel  # noqa: E402
from updates.channel_source import (  # noqa: E402
    HTTPSOnlyRedirectHandler,
    _READ_SIZE,
    _fetch_bytes,
    fetch_update,
)

CH_URL = "https://updates.example.invalid/stable.json"
CH_SIG_URL = "https://updates.example.invalid/stable.json.sig"
PKG_URL = "https://updates.example.invalid/rel/1.0.0/package.json"
PKG_SIG_URL = "https://updates.example.invalid/rel/1.0.0/package.json.sig"
PUBLISHED_AT = "2026-04-19T12:00:00Z"


class FakeClock:
    """Deterministic monotonic clock advanced by the fake reader."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSocket:
    """Record per-read socket timeouts without any network access."""

    def __init__(self):
        self.timeouts = []
        self.shutdowns = []
        self.closed = False

    def settimeout(self, value):
        if self.closed:
            raise OSError(errno.EBADF, "Bad file descriptor")
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shutdowns.append(how)
        if self.closed:
            raise OSError(errno.EBADF, "Bad file descriptor")

    def close(self):
        self.closed = True


class FakeTimer:
    """Inert stand-in for the per-response cancellation timer.

    ``start`` only records the intent; ``fire`` runs the expiry callback
    synchronously at the point a test chooses, so no thread is scheduled.
    """

    def __init__(self, interval, action):
        self.interval = interval
        self.action = action
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.joined = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self):
        self.joined = True

    def fire(self):
        self.action()


class FakeResponse:
    def __init__(
        self,
        data,
        url,
        status=200,
        closed=None,
        chunk=None,
        on_read=None,
        sock=None,
    ):
        self._buf = io.BytesIO(data)
        self._url = url
        self._status = status
        self._closed = closed
        self._chunk = chunk
        self._on_read = on_read
        self._sock = sock
        self.fp = (
            SimpleNamespace(raw=SimpleNamespace(_sock=sock))
            if sock is not None
            else None
        )
        self.closed = False
        self.read_sizes = []
        self.bytes_read = 0

    def _record(self, n):
        self.read_sizes.append(n)
        if self._on_read is not None:
            self._on_read(self)

    def read(self, n=-1):
        self._record(n)
        if self._chunk is not None and n is not None and n >= 0:
            n = min(n, self._chunk)
        data = self._buf.read(n)
        self.bytes_read += len(data)
        return data

    def read1(self, n=-1):
        # The production path only uses read1; delegate to the mock reader.
        return self.read(n)

    def getcode(self):
        return self._status

    def geturl(self):
        return self._url

    def close(self):
        if not self.closed:
            self.closed = True
            self._buf.close()
            if self._closed is not None:
                self._closed.append(self._url)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class CloseAfterFinalReadResponse(FakeResponse):
    """Fixed-length body whose last data read closes fp and its socket.

    ``http.client`` can call ``_close_conn()`` from inside a ``read1`` that
    still returns the final body bytes, which unsets ``response.fp`` and
    closes the descriptor while those bytes are already handed back. This
    mock reproduces that ordering with an in-memory body only.
    """

    def read1(self, n=-1):
        data = super().read1(n)
        if data and self._buf.tell() >= len(self._buf.getvalue()):
            self.fp = None
            if self._sock is not None:
                self._sock.close()
        return data


class ShutdownReleasesReadSocket(FakeSocket):
    """Fake socket whose shutdown releases a blocked body read."""

    def __init__(self):
        super().__init__()
        self.released = Event()

    def shutdown(self, how):
        super().shutdown(how)
        self.released.set()


class BlockedBodyResponse(FakeResponse):
    """Body read that only completes once the socket is shut down.

    The read fires the fake timer first, then waits for the fake socket's
    shutdown to release it and returns EOF, mirroring a real blocked
    ``read1`` that a deadline shutdown unblocks.
    """

    def __init__(self, url, sock, timers):
        super().__init__(b"", url, sock=sock)
        self._timers = timers

    def read1(self, n=-1):
        self._record(n)
        self._timers[-1].fire()
        if not self._sock.released.wait(1):
            raise AssertionError("socket shutdown did not release the read")
        return b""


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes  # url -> bytes | Exception | FakeResponse factory
        self.calls = []
        self.closed = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, timeout))
        if isinstance(request, Request):
            assert request.headers.get("Accept") is not None
        route = self.routes.get(url)
        if route is None:
            raise URLError(f"no route for {url}")
        if isinstance(route, Exception):
            raise route
        if isinstance(route, FakeResponse):
            return route
        return FakeResponse(route, url, closed=self.closed)


class ChannelSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        tmp = Path(cls._tmp.name)
        cls.priv, cls.pub = fixture._generate_keypair(tmp, "release")
        _, cls.other_pub = fixture._generate_keypair(tmp, "other")
        pkg = fixture._upgrade_package_dict()
        pkg_doc = json.dumps(pkg, indent=2).encode()
        pkg_sig = fixture._sign_bytes(pkg_doc, cls.priv)
        cls.pkg_doc = pkg_doc
        cls.pkg_sig = pkg_sig
        channel = make_channel(
            pkg_doc, pkg_sig, channel="stable",
            descriptor_url=PKG_URL, signature_url=PKG_SIG_URL,
            published_at=PUBLISHED_AT,
            expected_oci_digest=pkg["ociDigest"], trusted_key=cls.pub,
        )
        cls.channel = channel
        cls.base_routes = {
            CH_URL: cls._channel_document(channel),
            CH_SIG_URL: cls._channel_signature(channel),
            PKG_URL: pkg_doc,
            PKG_SIG_URL: pkg_sig,
        }

    @staticmethod
    def _channel_document(channel):
        return json.dumps(channel, indent=2).encode()

    @classmethod
    def _channel_signature(cls, channel):
        return fixture._sign_bytes(cls._channel_document(channel), cls.priv)

    @classmethod
    def _signed_variant(cls, mutate):
        doc = deepcopy(cls.channel)
        mutate(doc)
        return cls._channel_document(doc), cls._channel_signature(doc)

    def setUp(self):
        self.opener = FakeOpener(dict(self.base_routes))
        self.clock = FakeClock()
        self.timers = []

        def make_timer(interval, action):
            timer = FakeTimer(interval, action)
            self.timers.append(timer)
            return timer

        patchers = (
            mock.patch("updates.channel_source.build_opener", return_value=self.opener),
            mock.patch("updates.channel_source.monotonic", new=self.clock),
            mock.patch("updates.channel_source.Timer", new=make_timer),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fetch(self, **overrides):
        kwargs = dict(
            channel_url=CH_URL, signature_url=CH_SIG_URL,
            expected_channel="stable", model="TB322FC",
            updater_version="1.0.0", trusted_key=self.pub,
        )
        kwargs.update(overrides)
        return fetch_update(**kwargs)

    def test_valid_fetch_returns_verified_channel_and_package(self):
        result = self._fetch()
        self.assertEqual(result["channel"], self.channel)
        self.assertEqual(result["package"]["version"], self.channel["version"])
        self.assertEqual(len(self.opener.calls), 4)
        self.assertTrue(all(u.startswith("https://") for u, _ in self.opener.calls))
        self.assertEqual(
            self.opener.closed, [CH_URL, CH_SIG_URL, PKG_URL, PKG_SIG_URL]
        )

    def test_channel_404_and_urlerror_single_request(self):
        self.opener.routes[CH_URL] = FakeResponse(b"{}", CH_URL, status=404)
        with self.assertRaises(CatalogError):
            self._fetch()
        self.assertEqual(len(self.opener.calls), 1)
        self.opener.routes[CH_URL] = URLError("unreachable")
        with self.assertRaises(CatalogError):
            self._fetch()
        self.assertEqual(len(self.opener.calls), 2)

    def _rejects_before_package(self, doc_bytes, sig_bytes, msg):
        self.opener.calls.clear()
        self.opener.routes[CH_URL] = doc_bytes
        self.opener.routes[CH_SIG_URL] = sig_bytes
        with self.assertRaises(CatalogError, msg=msg):
            self._fetch()
        self.assertEqual(
            [url for url, _ in self.opener.calls],
            [CH_URL, CH_SIG_URL],
            msg,
        )

    def test_rejections_before_package_fetch(self):
        self._rejects_before_package(
            *self._signed_variant(lambda d: d.__setitem__("withdrawn", True)),
            "withdrawn",
        )
        self._rejects_before_package(
            *self._signed_variant(
                lambda d: d.__setitem__("supportedModels", ["TB323FU"])
            ),
            "unsupported model",
        )
        self._rejects_before_package(
            *self._signed_variant(
                lambda d: d.__setitem__("minimumUpdaterVersion", "99.0.0")
            ),
            "updater too old",
        )
        self._rejects_before_package(
            *self._signed_variant(lambda d: d.__setitem__("channel", "beta")),
            "expected channel mismatch",
        )

    def test_package_tamper_and_identity_mismatch_fail(self):
        tampered = bytearray(self.pkg_doc)
        tampered[0] ^= 0x01
        self.opener.routes[PKG_URL] = bytes(tampered)
        with self.assertRaises(CatalogError):
            self._fetch()
        self.opener.routes[PKG_URL] = self.pkg_doc
        self.opener.routes[CH_URL], self.opener.routes[CH_SIG_URL] = (
            self._signed_variant(lambda d: d.__setitem__("version", "9.9.9"))
        )
        with self.assertRaises(CatalogError):
            self._fetch()

    def test_wrong_trusted_key_fails_after_two_requests(self):
        with self.assertRaises(CatalogError):
            self._fetch(trusted_key=self.other_pub)
        self.assertEqual(len(self.opener.calls), 2)

    def test_invalid_arguments_make_zero_requests(self):
        for kwargs in (
            {"expected_channel": "nightly"},
            {"channel_url": 5},
            {"channel_url": "ftp://x/y"},
            {"timeout": 0},
            {"timeout": True},
            {"signature_url": CH_URL},
        ):
            with self.assertRaises(CatalogError):
                self._fetch(**kwargs)
            self.assertEqual(self.opener.calls, [])

    def test_fetch_bytes_bounds_status_and_final_url(self):
        response = FakeResponse(b"x" * 64, CH_URL)
        self.opener.routes[CH_URL] = response
        with self.assertRaises(CatalogError):
            _fetch_bytes(CH_URL, 10, 5)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes, [11])

        response = FakeResponse(b"{}", CH_URL, status=503)
        self.opener.routes[CH_URL] = response
        with self.assertRaises(CatalogError):
            _fetch_bytes(CH_URL, 100, 5)
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes, [])

        for bad in ("http://updates.example.invalid/stable.json",
                    "https://user:pw@example.invalid/a"):
            response = FakeResponse(b"{}", bad)
            self.opener.routes[CH_URL] = response
            with self.assertRaises(CatalogError):
                _fetch_bytes(CH_URL, 100, 5)
            self.assertTrue(response.closed)
            self.assertEqual(response.read_sizes, [])

    def test_https_only_redirect_handler(self):
        handler = HTTPSOnlyRedirectHandler()
        req = Request(CH_URL)
        bad_targets = (
            "http://updates.example.invalid/x",
            "file:///etc/passwd",
            "https://user:pw@example.invalid/x",
            "https://updates.example.invalid/x#frag",
        )
        for target in bad_targets:
            with self.assertRaises(CatalogError):
                handler.redirect_request(req, None, 302, "Found", {}, target)
        result = handler.redirect_request(
            req, None, 302, "Found", {}, "https://updates.example.invalid/x"
        )
        self.assertIsInstance(result, Request)
        self.assertTrue(result.full_url.startswith("https://"))

    def test_slow_trickle_hits_monotonic_deadline(self):
        # Each read makes progress, so the socket never times out, yet the
        # whole fetch must still stop at the single elapsed deadline.
        response = FakeResponse(
            b"x" * 64, CH_URL, chunk=1,
            on_read=lambda _response: self.clock.advance(4),
        )
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "timed out after 5 seconds"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertTrue(response.closed)
        self.assertLess(response.bytes_read, 64)
        self.assertGreater(response.bytes_read, 0)
        self.assertLessEqual(response.bytes_read, 2)

    def test_eof_after_deadline_is_rejected(self):
        # EOF itself must be deadline-checked: a body that only ends after
        # the budget is spent is not a complete document.
        response = FakeResponse(
            b"", CH_URL, on_read=lambda _response: self.clock.advance(6)
        )
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "timed out after 5 seconds"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertTrue(response.closed)
        self.assertEqual(response.bytes_read, 0)

    def test_socket_read_timeout_tracks_remaining_budget(self):
        sock = FakeSocket()
        response = FakeResponse(
            b"abc", CH_URL, chunk=1, sock=sock,
            on_read=lambda _response: self.clock.advance(0.5),
        )
        self.opener.routes[CH_URL] = response
        self.assertEqual(_fetch_bytes(CH_URL, 64, 5), b"abc")
        self.assertAlmostEqual(sock.timeouts[0], 5.0)
        self.assertAlmostEqual(sock.timeouts[1], 4.5)
        self.assertTrue(
            all(a > b for a, b in zip(sock.timeouts, sock.timeouts[1:]))
        )

    def test_socket_closed_by_final_read_still_returns_whole_body(self):
        # The last read hands back the remaining bytes and closes the body
        # socket, so response.fp is gone by the next iteration. The loop must
        # look the socket up again and skip settimeout on the closed object
        # instead of failing an otherwise complete fetch with EBADF.
        sock = FakeSocket()
        body = b"payload"
        response = CloseAfterFinalReadResponse(
            body, CH_URL, chunk=len(body), sock=sock,
            on_read=lambda _response: self.clock.advance(0.5),
        )
        self.opener.routes[CH_URL] = response
        self.assertEqual(_fetch_bytes(CH_URL, 64, 5), body)
        self.assertTrue(sock.closed)
        self.assertIsNone(response.fp)
        self.assertEqual(response.bytes_read, len(body))
        self.assertEqual(len(sock.timeouts), 1)
        self.assertAlmostEqual(sock.timeouts[0], 5.0)

    def test_chunked_reads_progress_before_deadline_and_respect_cap(self):
        body = b"z" * 40
        response = FakeResponse(
            body, CH_URL, chunk=7,
            on_read=lambda _response: self.clock.advance(0.5),
        )
        self.opener.routes[CH_URL] = response
        self.assertEqual(_fetch_bytes(CH_URL, 100, 5), body)
        self.assertTrue(response.closed)
        self.assertEqual(response.bytes_read, len(body))
        self.assertTrue(response.read_sizes)
        self.assertTrue(all(0 < size <= _READ_SIZE for size in response.read_sizes))

    def test_blocked_body_read_is_released_by_deadline_shutdown(self):
        # A read can be stuck inside several framing chunks and never reach
        # the between-read check; the timer must shut the socket down and
        # the resulting EOF must fail closed as a timeout.
        sock = ShutdownReleasesReadSocket()
        response = BlockedBodyResponse(CH_URL, sock, self.timers)
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "timed out after 5 seconds"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertEqual(sock.shutdowns, [SHUT_RDWR])
        self.assertTrue(sock.released.is_set())
        self.assertTrue(response.closed)
        self.assertEqual(len(self.timers), 1)
        timer = self.timers[0]
        self.assertEqual(timer.interval, 5.0)
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.cancelled)
        self.assertTrue(timer.joined)

    def test_shutdown_unblocked_read_error_fails_closed_as_timeout(self):
        # Shutdown can unblock a read with an error instead of EOF; that
        # must still be a timeout, not a generic transport failure.
        sock = FakeSocket()

        def expire_and_fail(_response):
            self.timers[-1].fire()
            raise OSError(errno.ECONNRESET, "reset")

        response = FakeResponse(
            b"abc", CH_URL, chunk=1, sock=sock, on_read=expire_and_fail
        )
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "timed out after 5 seconds"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertEqual(sock.shutdowns, [SHUT_RDWR])
        self.assertTrue(response.closed)
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[0].joined)

    def test_shutdown_of_already_closed_socket_is_ignored(self):
        # http.client may close the socket from inside read1. shutdown on
        # the invalidated object raises EBADF and must be swallowed while
        # the fetch still fails closed.
        sock = FakeSocket()

        def close_then_expire(_response):
            sock.close()
            self.timers[-1].fire()

        response = FakeResponse(
            b"abc", CH_URL, chunk=1, sock=sock, on_read=close_then_expire
        )
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "timed out after 5 seconds"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertEqual(sock.shutdowns, [SHUT_RDWR])
        self.assertTrue(response.closed)
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[0].joined)

    def test_timer_is_stopped_after_fast_completion(self):
        sock = FakeSocket()
        response = FakeResponse(b"abc", CH_URL, chunk=3, sock=sock)
        self.opener.routes[CH_URL] = response
        self.assertEqual(_fetch_bytes(CH_URL, 64, 5), b"abc")
        self.assertTrue(response.closed)
        self.assertEqual(sock.shutdowns, [])
        self.assertEqual(len(self.timers), 1)
        timer = self.timers[0]
        self.assertEqual(timer.interval, 5.0)
        self.assertTrue(timer.daemon)
        self.assertTrue(timer.cancelled)
        self.assertTrue(timer.joined)

    def test_timer_is_stopped_when_cap_is_exceeded(self):
        sock = FakeSocket()
        response = FakeResponse(b"x" * 8, CH_URL, chunk=8, sock=sock)
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "exceeds 4 bytes"):
            _fetch_bytes(CH_URL, 4, 5)
        self.assertEqual(sock.shutdowns, [])
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[0].joined)

    def test_timer_is_stopped_when_body_read_raises(self):
        class BrokenReadResponse(FakeResponse):
            def read1(self, n=-1):
                self._record(n)
                raise OSError(errno.ECONNRESET, "reset")

        sock = FakeSocket()
        response = BrokenReadResponse(b"", CH_URL, sock=sock)
        self.opener.routes[CH_URL] = response
        with self.assertRaisesRegex(CatalogError, "failed to fetch"):
            _fetch_bytes(CH_URL, 64, 5)
        self.assertEqual(sock.shutdowns, [])
        self.assertTrue(self.timers[0].cancelled)
        self.assertTrue(self.timers[0].joined)

    def test_mocked_transport_without_socket_arms_no_timer(self):
        response = FakeResponse(b"abc", CH_URL, chunk=3)
        self.opener.routes[CH_URL] = response
        self.assertEqual(_fetch_bytes(CH_URL, 64, 5), b"abc")
        self.assertEqual(self.timers, [])
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
