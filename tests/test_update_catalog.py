import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ARMADA_LIB = REPO_ROOT / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates.catalog import CatalogError, load_channel, require_compatible, validate_channel
from updates.signature import SignatureError


def _fixture():
    return {
        "schemaVersion": 1,
        "kind": "freedom-channel",
        "product": "armadaos-freedom",
        "channel": "stable",
        "releaseId": "rel-1",
        "version": "1.2.3",
        "sourceRevision": "a" * 40,
        "ociDigest": "sha256:" + "b" * 64,
        "ostreeCommit": "c" * 64,
        "supportedModels": ["TB322FC", "TB710FU"],
        "packageDescriptor": {
            "url": "https://updates.example.com/pkg.tar",
            "signatureUrl": "https://updates.example.com/pkg.tar.sig",
            "bytes": 1024,
            "sha256": "d" * 64,
        },
        "minimumUpdaterVersion": "1.0.0",
        "publishedAt": "2024-01-01T00:00:00+00:00",
        "withdrawn": False,
    }


class TestUpdateCatalog(unittest.TestCase):
    def _check(self, mutate, expected_channel="stable"):
        data = _fixture()
        mutate(data)
        return validate_channel(data, expected_channel)

    def test_valid_document_accepted_all_channels(self):
        for channel in ("stable", "beta", "testing"):
            with self.subTest(channel=channel):
                result = self._check(lambda d: d.__setitem__("channel", channel), channel)
                self.assertEqual(result["channel"], channel)

    def test_expected_channel_must_be_known(self):
        for bad in ("dev", "", None, 1, ["stable"]):
            with self.subTest(expected=bad):
                with self.assertRaises(CatalogError):
                    validate_channel(_fixture(), bad)

    def test_channel_mismatch_and_unknown(self):
        with self.assertRaises(CatalogError):
            self._check(lambda d: None, expected_channel="beta")
        for bad in ("dev", None):
            with self.subTest(channel=bad):
                with self.assertRaises(CatalogError):
                    self._check(lambda d: d.__setitem__("channel", bad))

    def test_malformed_channel_unhashable(self):
        for bad in (["stable"], {"stable": 1}):
            with self.subTest(channel=bad):
                with self.assertRaises(CatalogError):
                    self._check(lambda d: d.__setitem__("channel", bad))

    def test_schema_unknown_or_missing_key(self):
        for mutate in (
            lambda d: d.__setitem__("extra", 1),
            lambda d: d.pop("withdrawn"),
            lambda d: d.__setitem__("schemaVersion", 2),
            lambda d: d.__setitem__("schemaVersion", True),
        ):
            with self.subTest(mutate=mutate):
                with self.assertRaises(CatalogError):
                    self._check(mutate)

    def test_bad_identity_and_hash_fields(self):
        self.assertEqual(self._check(lambda d: d.__setitem__("version", "1.2"))["version"], "1.2")
        cases = [
            (lambda d: d.__setitem__("releaseId", "latest"), "releaseId"),
            (lambda d: d.__setitem__("releaseId", ""), "releaseId"),
            (lambda d: d.__setitem__("version", "bad version"), "version"),
            (lambda d: d.__setitem__("sourceRevision", "A" * 40), "sourceRevision"),
            (lambda d: d.__setitem__("ociDigest", "b" * 64), "ociDigest"),
            (lambda d: d.__setitem__("ostreeCommit", "z" * 64), "ostreeCommit"),
        ]
        for mutate, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(CatalogError):
                    self._check(mutate)

    def test_supported_models_duplicates_unknown_empty(self):
        for models in (["TB322FC", "TB322FC"], ["UNKNOWN"], [], [1]):
            with self.subTest(models=models):
                with self.assertRaises(CatalogError):
                    self._check(lambda d: d.__setitem__("supportedModels", models))

    def test_invalid_urls(self):
        urls = [
            "http://updates.example.com/pkg",
            "https://user:pw@example.com/pkg",
            "https://example.com/pkg#frag",
            "https://example.com/pkg\\other",
            "https://example.com:99999/pkg",
            "https://example.com/pkg\x01",
            "https://example.com/pa\th",
            "/relative/path",
            "",
            5,
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(CatalogError):
                    self._check(lambda d: d["packageDescriptor"].__setitem__("url", url))
        with self.assertRaises(CatalogError):
            self._check(
                lambda d: d["packageDescriptor"].__setitem__(
                    "signatureUrl", d["packageDescriptor"]["url"]
                )
            )

    def test_package_descriptor_bytes_and_sha(self):
        for mutate in (
            lambda d: d["packageDescriptor"].__setitem__("bytes", True),
            lambda d: d["packageDescriptor"].__setitem__("bytes", 0),
            lambda d: d["packageDescriptor"].__setitem__("bytes", 2 * 1024 * 1024 + 1),
            lambda d: d["packageDescriptor"].__setitem__("sha256", "e" * 63),
            lambda d: d["packageDescriptor"].pop("signatureUrl"),
        ):
            with self.subTest(mutate=mutate):
                with self.assertRaises(CatalogError):
                    self._check(mutate)

    def test_published_at_and_withdrawn_types(self):
        for mutate in (
            lambda d: d.__setitem__("publishedAt", "2024-01-01T00:00:00"),
            lambda d: d.__setitem__("publishedAt", "not-a-date"),
            lambda d: d.__setitem__("publishedAt", 5),
            lambda d: d.__setitem__("withdrawn", 1),
            lambda d: d.__setitem__("withdrawn", "false"),
        ):
            with self.subTest(mutate=mutate):
                with self.assertRaises(CatalogError):
                    self._check(mutate)

    def test_validate_returns_isolated_copy(self):
        data = _fixture()
        result = validate_channel(data, "stable")
        result["releaseId"] = "changed"
        result["supportedModels"].append("TB323FU")
        result["packageDescriptor"]["bytes"] = 9999
        self.assertEqual(data["releaseId"], "rel-1")
        self.assertEqual(data["supportedModels"], ["TB322FC", "TB710FU"])
        self.assertEqual(data["packageDescriptor"]["bytes"], 1024)

    def test_require_compatible(self):
        with self.assertRaises(CatalogError):
            require_compatible(_fixture(), "TB322FC", "0.9.0")
        require_compatible(_fixture(), "TB322FC", "1.0.0")
        require_compatible(_fixture(), "TB322FC", "1.10.0")

        doc_0_9 = _fixture()
        doc_0_9["minimumUpdaterVersion"] = "0.9.0"
        require_compatible(doc_0_9, "TB322FC", "0.10.0")

        doc_0_10 = _fixture()
        doc_0_10["minimumUpdaterVersion"] = "0.10.0"
        with self.assertRaises(CatalogError):
            require_compatible(doc_0_10, "TB322FC", "0.9.0")

        for bad_ver in ("1.2", "bad", "", None, "01.0.0", "1.0.0-alpha"):
            with self.subTest(updater_version=bad_ver):
                with self.assertRaises(CatalogError):
                    require_compatible(_fixture(), "TB322FC", bad_ver)

        withdrawn = _fixture()
        withdrawn["withdrawn"] = True
        with self.assertRaises(CatalogError):
            require_compatible(withdrawn, "TB322FC", "1.0.0")
        with self.assertRaises(CatalogError):
            require_compatible(_fixture(), "TB323FU", "1.0.0")

    def test_load_channel_wiring_signature_errors(self):
        with mock.patch(
            "updates.catalog.load_signed_json", side_effect=SignatureError("bad")
        ) as mock_load:
            with self.assertRaises(CatalogError):
                load_channel(b"{}", b"sig", expected_channel="stable")
        mock_load.assert_called_once()

    def test_load_channel_wiring_valid_and_wrong_channel(self):
        with mock.patch(
            "updates.catalog.load_signed_json", return_value=_fixture()
        ):
            result = load_channel(b"doc", b"sig", expected_channel="stable")
        self.assertEqual(result["channel"], "stable")
        with mock.patch(
            "updates.catalog.load_signed_json", return_value=_fixture()
        ):
            with self.assertRaises(CatalogError):
                load_channel(b"doc", b"sig", expected_channel="beta")


if __name__ == "__main__":
    unittest.main()
