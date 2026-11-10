import dataclasses
import http.client
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates import decky_source
from updates.decky_release import DeckyReleaseError
from test_decky_payload import URL, make_elf, make_rel


class FakeResponse(io.BytesIO):
    def __init__(self, data=b"", status=200):
        super().__init__(data)
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def mock_opener(response=None, error=None):
    opener = Mock()
    if error is not None:
        opener.open.side_effect = error
    else:
        opener.open.return_value = response
    return opener


class TestDeckySource(unittest.TestCase):
    def test_metadata_request_and_selected_identity(self):
        raw = json.dumps([{
            "id": 1,
            "tag_name": "v1",
            "published_at": "2024-01-01T00:00:00Z",
            "draft": False,
            "prerelease": True,
            "assets": [{
                "name": "PluginLoader-arm64",
                "id": 10,
                "size": 80,
                "browser_download_url": URL,
                "digest": "sha256:" + "a" * 64,
            }],
        }]).encode()
        opener = mock_opener(FakeResponse(raw))
        with patch.object(decky_source.urllib.request, "build_opener", return_value=opener):
            release = decky_source.fetch_preview(timeout=7)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/SteamDeckHomebrew/decky-loader/releases?per_page=100")
        self.assertEqual(request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7.0)
        self.assertEqual((release.release_id, release.asset_id, release.url), (1, 10, URL))

    def test_metadata_failures_and_nonfinite_timeouts(self):
        cases = [
            ("json", FakeResponse(b"{"), None, None),
            ("utf8", FakeResponse(b"\xff"), None, None),
            ("status", FakeResponse(b"[]", status=503), None, None),
            ("url", None, urllib.error.URLError("down"), None),
            ("timeout", None, TimeoutError("slow"), None),
            ("incomplete", FakeResponse(b"[]"), None, http.client.IncompleteRead(b"")),
        ]
        for name, response, open_error, read_error in cases:
            with self.subTest(name=name):
                if read_error is not None:
                    response.read = Mock(side_effect=read_error)
                opener = mock_opener(response, open_error)
                with patch.object(decky_source.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(DeckyReleaseError):
                        decky_source.fetch_preview()
        opener = mock_opener(FakeResponse(b"123456789"))
        with patch.object(decky_source, "MAX_METADATA_BYTES", 8):
            with patch.object(decky_source.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(DeckyReleaseError):
                    decky_source.fetch_preview()
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout), self.assertRaises(DeckyReleaseError):
                decky_source.fetch_preview(timeout=timeout)

    def test_good_download_replaces_only_destination(self):
        data = make_elf()
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "dest"
            other = Path(td) / "other"
            destination.write_bytes(b"old")
            other.write_bytes(b"keep")
            opener = mock_opener(FakeResponse(data))
            with patch.object(decky_source.urllib.request, "build_opener", return_value=opener):
                result = decky_source.download_payload(make_rel(data), destination, timeout=9)
            request = opener.open.call_args.args[0]
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(other.read_bytes(), b"keep")
            self.assertEqual(request.full_url, URL)
            self.assertEqual(request.get_header("Accept"), "application/octet-stream")
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 9.0)
            self.assertEqual({p.name for p in Path(td).iterdir()}, {"dest", "other"})

    def test_failed_downloads_preserve_destination_and_remove_temps(self):
        data = make_elf()
        bad_arch = make_elf(machine=62)
        wrong_hash = "0" * 64
        cases = [
            ("wrong-sha", data, make_rel(data, sha256=wrong_hash), None),
            ("truncated", data[:-1], make_rel(data), None),
            ("oversize", data + b"x", make_rel(data), None),
            ("wrong-arch", bad_arch, make_rel(bad_arch), None),
            ("disconnect", data, make_rel(data), http.client.IncompleteRead(b"x")),
        ]
        for name, response_data, release, read_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                destination = Path(td) / "dest"
                destination.write_bytes(b"original\x00bytes")
                response = FakeResponse(response_data)
                if read_error is not None:
                    response.read = Mock(side_effect=[response_data[:32], read_error])
                opener = mock_opener(response)
                with patch.object(decky_source.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(DeckyReleaseError):
                        decky_source.download_payload(release, destination)
                self.assertEqual(destination.read_bytes(), b"original\x00bytes")
                self.assertEqual([p.name for p in Path(td).iterdir()], ["dest"])

    def test_invalid_frozen_metadata_precedes_network_and_files(self):
        release = make_rel(make_elf())
        invalid = [
            dataclasses.replace(release, url="https://example.com/wrong"),
            dataclasses.replace(release, asset_id=0),
            dataclasses.replace(release, sha256="A" * 64),
        ]
        with tempfile.TemporaryDirectory() as td:
            destination = Path(td) / "dest"
            with patch.object(decky_source.urllib.request, "build_opener") as build:
                for candidate in invalid:
                    with self.subTest(candidate=candidate), self.assertRaises(DeckyReleaseError):
                        decky_source.download_payload(candidate, destination)
                build.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_redirect_policy(self):
        handler = decky_source._HTTPSOnlyRedirectHandler()
        request = urllib.request.Request(URL)
        cdn = "https://objects.githubusercontent.com/asset"
        redirected = handler.redirect_request(request, None, 302, "Found", {}, cdn)
        self.assertEqual(redirected.full_url, cdn)
        for target in ("http://objects.githubusercontent.com/asset", "file:///tmp/asset"):
            with self.subTest(target=target), self.assertRaises(DeckyReleaseError):
                handler.redirect_request(request, None, 302, "Found", {}, target)


if __name__ == "__main__":
    unittest.main()
