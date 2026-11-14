import hashlib
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates.decky_release import (
    DeckyRelease,
    DeckyReleaseError,
    select_preview,
    verify_payload,
)

SHA = "a" * 64
URL = "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/{tag}/PluginLoader-arm64"


def rel(rid, tag, published, assets=None, draft=False, prerelease=True):
    return {
        "id": rid,
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": published,
        "assets": assets if assets is not None else [asset(tag)],
    }


def asset(tag, **kw):
    a = {
        "id": 700,
        "name": "PluginLoader-arm64",
        "size": 123,
        "browser_download_url": URL.format(tag=tag),
        "digest": "sha256:" + SHA,
    }
    a.update(kw)
    return a


class TestSelectPreview(unittest.TestCase):
    def select(self, releases):
        return select_preview(releases)

    def test_newest_preview_ignores_draft_stable(self):
        releases = [
            rel(1, "v1", "2024-01-01T00:00:00Z", prerelease=True),
            rel(2, "v2", "2024-06-01T00:00:00Z", draft=True),
            rel(3, "v3", "2024-07-01T00:00:00Z", prerelease=False),
            rel(4, "v4", "2024-05-01T00:00:00Z"),
        ]
        r = self.select(releases)
        self.assertEqual((r.release_id, r.tag, r.asset_id, r.size, r.sha256), (4, "v4", 700, 123, SHA))

    def test_tz_equivalent_ordering_and_tie_break(self):
        same = rel(5, "v5", "2024-05-01T01:00:00+01:00")
        tie = rel(6, "v6", "2024-05-01T00:00:00Z")
        self.assertEqual(self.select([same, tie]).release_id, 6)
        self.assertEqual(self.select([tie, same]).release_id, 6)

    def test_no_arm64_no_fallback(self):
        releases = [
            rel(1, "v1", "2024-01-01T00:00:00Z"),
            rel(2, "v2", "2024-02-01T00:00:00Z", assets=[{"id": 1, "name": "other", "size": 1}]),
        ]
        with self.assertRaises(DeckyReleaseError):
            self.select(releases)

    def test_duplicate_asset_fails(self):
        releases = [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1"), asset("v1")])]
        with self.assertRaises(DeckyReleaseError):
            self.select(releases)

    def test_invalid_payload_shapes(self):
        for bad in [
            "notalist",
            ["notdict"],
            [rel(1, "v1", "2024-01-01T00:00:00Z", draft=1)],
            [rel(1, "v1", "2024-01-01T00:00:00Z", prerelease="yes")],
            [rel(True, "v1", "2024-01-01T00:00:00Z")],
            [rel(0, "v1", "2024-01-01T00:00:00Z")],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", id=0)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", id=True)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", size=0)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", size=True)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", digest=None)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", digest="md5:" + SHA)])],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", digest="sha256:" + SHA + "\n")])],
            [rel(1, "bad tag", "2024-01-01T00:00:00Z")],
            [rel(1, "v1\n", "2024-01-01T00:00:00Z")],
            [rel(1, "..", "2024-01-01T00:00:00Z")],
            [rel(1, "v1", "2024-01-01T00:00:00")],
            [rel(1, "v1", "nonsense-date")],
            [rel(1, "v1", 5)],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets="x")],
            [rel(1, "v1", "2024-01-01T00:00:00Z", assets=["x"])],
            [],
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(DeckyReleaseError):
                    self.select(bad)

    def test_invalid_urls(self):
        urls = [
            "http://github.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64",
            "https://gitlab.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64",
            "https://github.com/Other/decky-loader/releases/download/v1/PluginLoader-arm64",
            "https://github.com/SteamDeckHomebrew/other/releases/download/v1/PluginLoader-arm64",
            "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v2/PluginLoader-arm64",
            "https://user:pw@github.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64",
            "https://github.com:443/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64",
            URL.format(tag="v1") + "?x=1",
            URL.format(tag="v1") + "#frag",
            URL.format(tag="v1") + "?",
            URL.format(tag="v1") + "#",
            "\x01" + URL.format(tag="v1"),
            URL.format(tag="v1").replace("github.com", "git\nhub.com"),
            URL.format(tag="v1") + "\\",
            "",
            5,
        ]
        for u in urls:
            with self.subTest(url=u):
                releases = [rel(1, "v1", "2024-01-01T00:00:00Z", assets=[asset("v1", browser_download_url=u)])]
                with self.assertRaises(DeckyReleaseError):
                    self.select(releases)

    def test_encoded_plus_tag_accepted(self):
        tag = "v1.2+build"
        releases = [rel(1, tag, "2024-01-01T00:00:00Z", assets=[asset(tag, browser_download_url=URL.format(tag="v1.2%2Bbuild"))])]
        r = self.select(releases)
        self.assertEqual(r.tag, tag)
        self.assertEqual(r.url, URL.format(tag="v1.2%2Bbuild"))

    def test_identity_and_digest_lowered(self):
        up = SHA.upper()
        releases = [rel(42, "v9", "2024-02-02T02:02:02Z", assets=[asset("v9", id=77, size=5, digest="sha256:" + up)])]
        r = self.select(releases)
        self.assertEqual((r.release_id, r.asset_id, r.size), (42, 77, 5))
        self.assertEqual(r.sha256, up.lower())


def payload(machine=183, size=128):
    buf = bytearray(size)
    buf[:4] = b"\x7fELF"
    buf[4] = 2
    buf[5] = 1
    struct.pack_into("<H", buf, 18, machine)
    return bytes(buf)


def release_for(data, size=None, digest=None):
    return DeckyRelease(
        release_id=1,
        tag="v1",
        published_at="2024-01-01T00:00:00Z",
        asset_id=700,
        url=URL.format(tag="v1"),
        size=len(data) if size is None else size,
        sha256=hashlib.sha256(data).hexdigest() if digest is None else digest,
    )


class TestVerifyPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "PluginLoader-arm64"

    def test_regular_file_matching_size_digest_and_header_passes(self):
        data = payload()
        self.path.write_bytes(data)
        verify_payload(release_for(data), self.path)
        self.assertEqual(self.path.read_bytes(), data)

    def test_declared_size_is_checked_against_the_descriptor(self):
        data = payload()
        self.path.write_bytes(data)
        for declared in (len(data) - 1, len(data) + 1):
            with self.subTest(declared=declared):
                with self.assertRaisesRegex(DeckyReleaseError, "Size mismatch"):
                    verify_payload(release_for(data, size=declared), self.path)

    def test_early_eof_is_rejected(self):
        data = payload()
        self.path.write_bytes(data)
        real_read = os.read
        served = {"bytes": 0}

        def truncated_read(fd, count):
            if served["bytes"] >= len(data) // 2:
                return b""
            chunk = real_read(fd, min(count, 8))
            served["bytes"] += len(chunk)
            return chunk

        with mock.patch.object(os, "read", side_effect=truncated_read):
            with self.assertRaisesRegex(DeckyReleaseError, "Size mismatch"):
                verify_payload(release_for(data), self.path)

    def test_late_size_change_is_rejected(self):
        data = payload()
        self.path.write_bytes(data)
        real_fstat = os.fstat
        calls = {"count": 0}

        def grown(fd):
            st = real_fstat(fd)
            calls["count"] += 1
            if calls["count"] > 1:
                values = list(st)
                values[6] = st.st_size + 1
                return os.stat_result(tuple(values))
            return st

        with mock.patch.object(os, "fstat", side_effect=grown):
            with self.assertRaisesRegex(DeckyReleaseError, "changed while reading"):
                verify_payload(release_for(data), self.path)

    def test_digest_header_and_non_regular_inputs_rejected(self):
        data = payload()
        self.path.write_bytes(data)
        with self.assertRaisesRegex(DeckyReleaseError, "SHA256 mismatch"):
            verify_payload(release_for(data, digest="0" * 64), self.path)
        for bad in (payload(machine=62), payload(size=63)):
            with self.subTest(size=len(bad)):
                self.path.write_bytes(bad)
                with self.assertRaises(DeckyReleaseError):
                    verify_payload(release_for(bad), self.path)
        directory = Path(self.tmp.name) / "directory"
        directory.mkdir()
        with self.assertRaisesRegex(DeckyReleaseError, "not a regular file"):
            verify_payload(release_for(data), directory)
        with self.assertRaisesRegex(DeckyReleaseError, "Payload read failed"):
            verify_payload(release_for(data), Path(self.tmp.name) / "missing")


if __name__ == "__main__":
    unittest.main()
