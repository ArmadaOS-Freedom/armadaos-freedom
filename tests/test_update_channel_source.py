"""Integration tests for authenticated channel metadata retrieval.

Real cryptographic signing/verification; only the HTTP opener is replaced.
"""

import io
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
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

    def settimeout(self, value):
        self.timeouts.append(value)


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
        patchers = (
            mock.patch("updates.channel_source.build_opener", return_value=self.opener),
            mock.patch("updates.channel_source.monotonic", new=self.clock),
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


if __name__ == "__main__":
    unittest.main()
