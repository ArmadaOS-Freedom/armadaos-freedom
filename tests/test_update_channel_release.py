"""Tests for signed Freedom package to immutable channel release binding."""

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_package_contract as fixture  # noqa: E402
from updates.catalog import CatalogError  # noqa: E402
from updates.channel_release import bind_package, make_channel  # noqa: E402
from updates.package_contract import validate_package  # noqa: E402

_DESC_URL = "https://updates.example.invalid/releases/0.10.0/package.json"
_SIG_URL = "https://updates.example.invalid/releases/0.10.0/package.json.sig"
_PUBLISHED_AT = "2026-04-19T12:00:00Z"


def _generate_keypair(directory: Path, prefix: str) -> tuple[Path, Path]:
    priv = directory / f"{prefix}-private.pem"
    pub = directory / f"{prefix}-public.pem"
    subprocess.run(
        ["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(priv)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "ec", "-in", str(priv), "-pubout", "-out", str(pub)],
        check=True,
        capture_output=True,
    )
    return priv, pub


def _sign_bytes(document: bytes, private_key: Path) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key)],
        input=document,
        check=True,
        capture_output=True,
    )
    return base64.b64encode(proc.stdout) + b"\n"


def _upgrade_package_dict() -> dict:
    pkg = fixture._base_package()
    pkg["operations"] = ["install", "offline-upgrade"]
    for idx, record in enumerate(pkg["files"]):
        if record["role"] in ("linux-root", "linux-boot"):
            record["compression"] = "gzip"
            record["expandedBytes"] = record["bytes"] * 2
            record["expandedSha256"] = fixture._h(50 + idx)
    for idx, model in enumerate(pkg["supportedModels"]):
        rel_path = f"recovery/{model}.json"
        pkg["files"].append(fixture._file_record(rel_path, "manifest", size=128, seed=20 + idx))
        pkg["recoveryManifests"][model] = rel_path
    return pkg


class ChannelReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        keydir = Path(cls._tmpdir.name)
        cls.priv_key, cls.pub_key = _generate_keypair(keydir, "trusted")
        cls.other_priv_key, cls.other_pub_key = _generate_keypair(keydir, "other")

        cls.pkg_dict = validate_package(_upgrade_package_dict())
        cls.pkg_bytes = json.dumps(cls.pkg_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        cls.pkg_sig = _sign_bytes(cls.pkg_bytes, cls.priv_key)
        cls.oci_digest = cls.pkg_dict["ociDigest"]

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _make(self, channel="stable", **overrides):
        kwargs = {
            "channel": channel,
            "descriptor_url": _DESC_URL,
            "signature_url": _SIG_URL,
            "published_at": _PUBLISHED_AT,
            "expected_oci_digest": self.oci_digest,
            "trusted_key": self.pub_key,
        }
        kwargs.update(overrides)
        return make_channel(self.pkg_bytes, self.pkg_sig, **kwargs)

    def test_promote_same_package_across_channels_and_bind(self):
        channels = {name: self._make(channel=name) for name in ("stable", "beta", "testing")}
        for name, doc in channels.items():
            self.assertEqual(doc["channel"], name)
            self.assertEqual(doc["ociDigest"], self.oci_digest)
            self.assertEqual(doc["ostreeCommit"], self.pkg_dict["ostreeCommit"])
            self.assertEqual(doc["version"], self.pkg_dict["version"])
            self.assertEqual(doc["packageDescriptor"]["bytes"], len(self.pkg_bytes))
            self.assertEqual(doc["packageDescriptor"]["sha256"], sha256(self.pkg_bytes).hexdigest())
            bound = bind_package(doc, self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)
            self.assertEqual(bound["releaseId"], self.pkg_dict["releaseId"])

        reordered = deepcopy(channels["stable"])
        reordered["supportedModels"] = list(reversed(reordered["supportedModels"]))
        bound_reordered = bind_package(reordered, self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)
        self.assertEqual(bound_reordered["ociDigest"], self.oci_digest)

    def test_withdrawn_channel_remains_parseable_and_bindable(self):
        doc = self._make(channel="stable", withdrawn=True)
        self.assertTrue(doc["withdrawn"])
        bound = bind_package(doc, self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)
        self.assertEqual(bound["version"], self.pkg_dict["version"])

    def test_expected_oci_and_channel_validation_errors(self):
        for bad_oci in ("", None, f"sha256:{fixture._h(123)}"):
            with self.assertRaises(CatalogError):
                self._make(expected_oci_digest=bad_oci)
        with self.assertRaises(CatalogError):
            self._make(channel="nightly")
        with self.assertRaises(CatalogError):
            self._make(descriptor_url="http://insecure.example.invalid/pkg.json")
        with self.assertRaises(CatalogError):
            bind_package("not-a-dict", self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)
        with self.assertRaises(CatalogError):
            bind_package(self._make(), "not-bytes", self.pkg_sig, trusted_key=self.pub_key)

    def test_cryptographic_verification_failures_rejected(self):
        channel = self._make()
        other_sig = _sign_bytes(self.pkg_bytes, self.other_priv_key)
        for bad_sig in (b"", b"not-a-valid-sig\n", other_sig):
            with self.assertRaises(CatalogError):
                make_channel(
                    self.pkg_bytes, bad_sig, channel="stable", descriptor_url=_DESC_URL,
                    signature_url=_SIG_URL, published_at=_PUBLISHED_AT,
                    expected_oci_digest=self.oci_digest, trusted_key=self.pub_key,
                )
            with self.assertRaises(CatalogError):
                bind_package(channel, self.pkg_bytes, bad_sig, trusted_key=self.pub_key)
        with self.assertRaises(CatalogError):
            bind_package(channel, self.pkg_bytes, self.pkg_sig, trusted_key=self.other_pub_key)

    def test_install_only_package_rejected_by_make_and_bind(self):
        install_pkg = validate_package(fixture._base_package())
        install_bytes = json.dumps(install_pkg, sort_keys=True, separators=(",", ":")).encode("utf-8")
        install_sig = _sign_bytes(install_bytes, self.priv_key)
        with self.assertRaisesRegex(CatalogError, "offline-upgrade"):
            make_channel(
                install_bytes, install_sig, channel="stable", descriptor_url=_DESC_URL,
                signature_url=_SIG_URL, published_at=_PUBLISHED_AT,
                expected_oci_digest=install_pkg["ociDigest"], trusted_key=self.pub_key,
            )
        forged_channel = deepcopy(self._make())
        forged_channel["packageDescriptor"]["bytes"] = len(install_bytes)
        forged_channel["packageDescriptor"]["sha256"] = sha256(install_bytes).hexdigest()
        with self.assertRaisesRegex(CatalogError, "offline-upgrade"):
            bind_package(forged_channel, install_bytes, install_sig, trusted_key=self.pub_key)

    def test_descriptor_and_identity_mismatches_rejected(self):
        base_channel = self._make()
        padded_bytes = self.pkg_bytes + b"\n"
        with self.assertRaisesRegex(CatalogError, "byte length"):
            bind_package(base_channel, padded_bytes, self.pkg_sig, trusted_key=self.pub_key)

        rebound_channel = deepcopy(base_channel)
        rebound_channel["packageDescriptor"]["bytes"] = len(padded_bytes)
        rebound_channel["packageDescriptor"]["sha256"] = sha256(padded_bytes).hexdigest()
        with self.assertRaisesRegex(CatalogError, "package verification failed"):
            bind_package(rebound_channel, padded_bytes, self.pkg_sig, trusted_key=self.pub_key)

        bad_hash_channel = deepcopy(base_channel)
        bad_hash_channel["packageDescriptor"]["sha256"] = fixture._h(77)
        with self.assertRaisesRegex(CatalogError, "digest does not match"):
            bind_package(bad_hash_channel, self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)

        mutations = {
            "releaseId": "2026.04.20-rc2",
            "version": "0.10.1",
            "sourceRevision": "b" * 40,
            "ociDigest": f"sha256:{fixture._h(88)}",
            "ostreeCommit": fixture._h(98),
            "minimumUpdaterVersion": "0.9.1",
            "supportedModels": ["TB322FC"],
        }
        for field, value in mutations.items():
            mutated = deepcopy(base_channel)
            mutated[field] = value
            with self.assertRaises(CatalogError):
                bind_package(mutated, self.pkg_bytes, self.pkg_sig, trusted_key=self.pub_key)


if __name__ == "__main__":
    unittest.main()
