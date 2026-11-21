import contextlib
import io
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify
import fetch
import refresh


class VersionTests(unittest.TestCase):
    def test_version_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = pathlib.Path(directory) / "manifest"
            for contents, accepted in (
                ('"linuxarm64" { "version" "123" }', True),
                ('"linuxarm64" { "version" "124" }', False),
                ('"linuxarm64" {}', False),
                ('"linuxarm64" { "version" "invalid" }', False),
            ):
                with self.subTest(contents=contents):
                    manifest.write_text(contents)
                    if accepted:
                        verify.verify_version(manifest, "123")
                    else:
                        with self.assertRaisesRegex(SystemExit, "version mismatch"):
                            verify.verify_version(manifest, "123")


class OverrideTests(unittest.TestCase):
    def test_mismatched_override_is_rejected(self):
        contents = '"linuxarm64" { "version" "123" }'
        with self.assertRaisesRegex(ValueError, "version differs"):
            fetch.packages(contents, "124")

    def test_override_requires_package_checksums(self):
        contents = '"linuxarm64" { "version" "123" "bins" { "file" "bins.zip.abc" } }'
        with self.assertRaisesRegex(ValueError, "SHA256"):
            fetch.packages(contents, "123")


class ManifestSelectionTests(unittest.TestCase):
    def test_override_or_live_manifest_uses_the_same_package_checks(self):
        for version, saved_version in (("123", "123"), ("124", "123"), ("123", None)):
            with self.subTest(version=version, saved_version=saved_version), tempfile.TemporaryDirectory() as directory:
                with contextlib.chdir(directory):
                    feed = pathlib.Path("feed")
                    feed.mkdir()
                    contents = ('"linuxarm64" { "version" "' + version
                                + '" "bins" { "file" "bins.zip.abc" "sha2" "' + "a" * 64 + '" } }')
                    if saved_version:
                        pathlib.Path("manifests").mkdir()
                        pathlib.Path("manifests", saved_version).write_text(contents)

                    def download(command, **kwargs):
                        self.assertEqual(command[-1], f"{fetch.CDN}/{fetch.MANIFEST}")
                        (feed / fetch.MANIFEST).write_text(contents)

                    with patch("fetch.subprocess.run", side_effect=download) as request:
                        self.assertEqual(fetch.prepare_manifest(version, feed), [("bins.zip.abc", "a" * 64)])
                        self.assertEqual(request.call_count, int(version != saved_version))

    def test_bad_override_does_not_fall_back_to_network(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            pathlib.Path("feed").mkdir()
            pathlib.Path("manifests").mkdir()
            pathlib.Path("manifests/123").write_text('"version" "124"')
            with patch("fetch.subprocess.run") as request:
                with self.assertRaises(ValueError):
                    fetch.prepare_manifest("123", pathlib.Path("feed"))
                request.assert_not_called()


class RefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls._tmp.name)
        cls.manifests = cls.root / "manifests"
        cls.manifests.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for entry in self.manifests.iterdir():
            entry.unlink()
        self.base = self.root / "BASE.env"
        self.base.write_text("STEAM_CLIENT_VERSION=0\n")

    @staticmethod
    def manifest(version):
        return ('"linuxarm64"\n{\n'
                '\t"version"\t"' + version + '"\n'
                '\t"bins"\n'
                '\t{\n'
                '\t\t"file"\t"steam-bootstrap-bins.zip.' + 'b' * 64 + '"\n'
                '\t\t"sha2"\t"' + 'b' * 64 + '"\n'
                '\t}\n'
                '}\n')

    def invoke(self, version):
        source = self.root / "source.manifest"
        source.write_text(self.manifest(version))
        stdout = io.StringIO()
        with patch.object(refresh, "BASE", self.base), \
             patch.object(refresh, "MANIFESTS", self.manifests), \
             patch.object(sys, "argv", ["refresh.py", str(source)]), \
             contextlib.redirect_stdout(stdout):
            refresh.main()
        return stdout.getvalue()

    def test_numeric(self):
        self.base.write_text("STEAM_CLIENT_VERSION=99\n")
        out = self.invoke("100")
        self.assertEqual(self.base.read_text(), "STEAM_CLIENT_VERSION=100\n")
        self.assertEqual((self.manifests / "100").read_bytes(), self.manifest("100").encode("utf-8"))
        self.assertEqual(out, "100\n")

    def test_equal_or_older(self):
        self.base.write_text("STEAM_CLIENT_VERSION=100\n")
        before = self.base.read_bytes()
        for version in ("100", "99"):
            with self.subTest(version=version):
                out = self.invoke(version)
                self.assertEqual(out, "")
                self.assertEqual(self.base.read_bytes(), before)
                self.assertEqual(list(self.manifests.iterdir()), [])

    def test_conflicting_manifest(self):
        self.base.write_text("STEAM_CLIENT_VERSION=99\n")
        conflict = self.manifest("100").replace("bins", "other")
        target = self.manifests / "100"
        target.write_text(conflict)
        with self.assertRaises(ValueError):
            self.invoke("100")
        self.assertEqual(self.base.read_text(), "STEAM_CLIENT_VERSION=99\n")
        self.assertEqual(target.read_text(), conflict)

    def test_retry_with_saved_manifest(self):
        self.base.write_text("STEAM_CLIENT_VERSION=99\n")
        saved = self.manifest("100")
        (self.manifests / "100").write_text(saved)
        out = self.invoke("100")
        self.assertEqual(self.base.read_text(), "STEAM_CLIENT_VERSION=100\n")
        self.assertEqual(out, "100\n")
        self.assertEqual(sorted(p.name for p in self.manifests.iterdir()), ["100"])


if __name__ == "__main__":
    unittest.main()
