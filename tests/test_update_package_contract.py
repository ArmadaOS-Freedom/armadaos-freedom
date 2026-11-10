"""Tests for signed Freedom package and split-media descriptor validation."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files/usr/lib/armada"))

from updates.package_contract import (  # noqa: E402
    PackageError,
    load_package,
    require_operation,
    validate_package,
)
from updates.signature import SignatureError  # noqa: E402

_GIB = 1024 ** 3
_FIXTURE_HASH = "0123456789abcdef" * 4  # deterministic fixture, not crypto


def _h(seed):
    return f"{seed:064x}"


def _file_record(path, role, size=1024, seed=1, compression="none",
                 expanded_size=None, expanded_seed=None):
    return {
        "path": path,
        "bytes": size,
        "sha256": _h(seed),
        "expandedBytes": size if expanded_size is None else expanded_size,
        "expandedSha256": _h(seed if expanded_seed is None else expanded_seed),
        "compression": compression,
        "role": role,
    }


def _base_package():
    return {
        "schemaVersion": 1,
        "kind": "freedom-universal-package",
        "product": "armadaos-freedom",
        "architecture": "aarch64",
        "releaseId": "2026.04.19-rc1",
        "version": "0.10.0",
        "sourceRevision": "a" * 40,
        "ociDigest": f"sha256:{_FIXTURE_HASH}",
        "ostreeCommit": _h(99),
        "minimumInstallerVersion": "0.9.0",
        "minimumUpdaterVersion": "0.9.0",
        "supportedModels": ["TB322FC", "TB323FU"],
        "operations": ["install"],
        "archive": {
            "fileName": "armadaos-freedom-0.10.0.zip",
            "bytes": 1048576,
            "sha256": _FIXTURE_HASH,
        },
        "parts": [],
        "files": [
            _file_record("bundle.json", "manifest", 512, 1),
            _file_record("manifests/tb322fc.json", "manifest", 512, 2),
            _file_record("manifests/tb323fu.json", "manifest", 512, 3),
            _file_record("images/linux-root.img", "linux-root", 65536, 4),
            _file_record("images/linux-boot.img", "linux-boot", 16384, 5),
        ],
        "modelManifests": {
            "TB322FC": "manifests/tb322fc.json",
            "TB323FU": "manifests/tb323fu.json",
        },
        "recoveryManifests": {},
        "deckySnapshot": None,
    }


class PackageContractTests(unittest.TestCase):
    def test_valid_single_and_deep_copy(self):
        pkg = _base_package()
        validated = validate_package(pkg)
        self.assertEqual(validated, pkg)
        self.assertIsNot(validated, pkg)
        validated["supportedModels"].append("TB710FU")
        validated["files"][0]["path"] = "mutated.json"
        self.assertEqual(pkg["supportedModels"], ["TB322FC", "TB323FU"])
        self.assertEqual(pkg["files"][0]["path"], "bundle.json")

    def test_valid_split_and_part_constraints(self):
        pkg = _base_package()
        name = pkg["archive"]["fileName"]
        max_part = 2 * _GIB - 1
        pkg["archive"]["bytes"] = max_part + 100
        pkg["parts"] = [
            {"fileName": f"{name}.001", "bytes": max_part, "sha256": _h(10)},
            {"fileName": f"{name}.002", "bytes": 100, "sha256": _h(11)},
        ]
        self.assertEqual(validate_package(pkg)["parts"][0]["bytes"], max_part)

        bad_sum = deepcopy(pkg)
        bad_sum["parts"][1]["bytes"] = 99
        with self.assertRaisesRegex(PackageError, "sum exactly"):
            validate_package(bad_sum)

        exact_2gib = deepcopy(pkg)
        exact_2gib["archive"]["bytes"] = 2 * _GIB + 100
        exact_2gib["parts"][0]["bytes"] = 2 * _GIB
        with self.assertRaisesRegex(PackageError, r"parts\[0\]\.bytes"):
            validate_package(exact_2gib)

        for wrong_name in (f"{name}.002", f"{name}.001".upper(), "other.zip.001"):
            with self.subTest(wrong_name=wrong_name):
                bad_order = deepcopy(pkg)
                bad_order["parts"][0]["fileName"] = wrong_name
                with self.assertRaisesRegex(PackageError, "fileName"):
                    validate_package(bad_order)

    def test_archive_and_part_portable_basenames(self):
        for bad_name in ("sub/pkg.zip", "sub\\pkg.zip", "C:pkg.zip", "../pkg.zip",
                         "CON.zip", "aux.zip", "lpt1.zip", "pkg.zip ", ""):
            with self.subTest(bad_name=bad_name):
                pkg = _base_package()
                pkg["archive"]["fileName"] = bad_name
                with self.assertRaises(PackageError):
                    validate_package(pkg)

                split = _base_package()
                split["parts"] = [{"fileName": bad_name,
                                   "bytes": split["archive"]["bytes"],
                                   "sha256": _h(12)}]
                with self.assertRaises(PackageError):
                    validate_package(split)

    def test_invalid_fields_hashes_types_and_paths(self):
        mutators = [
            lambda d: d.update(schemaVersion=True),
            lambda d: d.update(supportedModels=["UNKNOWN"]),
            lambda d: d.update(sourceRevision="G" * 40),
            lambda d: d.update(ociDigest="sha256:short"),
            lambda d: d.update(ostreeCommit="ABCD" * 16),
            lambda d: d["files"][0].update(path="../bundle.json"),
            lambda d: d["files"][0].update(path="dir\\bundle.json"),
            lambda d: d["files"][0].update(path="dir/NUL.txt"),
            lambda d: d["files"][0].update(bytes=True),
        ]
        for i, mutator in enumerate(mutators):
            with self.subTest(mutator=i):
                pkg = _base_package()
                mutator(pkg)
                with self.assertRaises(PackageError):
                    validate_package(pkg)

    def test_path_case_collision_and_parent_collision(self):
        dup = _base_package()
        dup["files"].append(_file_record("Manifests/TB322FC.json", "metadata", 64, 20))
        with self.assertRaisesRegex(PackageError, "case-insensitively"):
            validate_package(dup)

        parent = _base_package()
        parent["files"].append(_file_record("bundle.json/child.txt", "metadata", 64, 21))
        with self.assertRaisesRegex(PackageError, "collide with parent"):
            validate_package(parent)

    def test_compression_rules_and_tiny_gzip_expansion(self):
        for kwargs in ({"expanded_size": 2048}, {"expanded_seed": 999}):
            with self.subTest(kwargs=kwargs):
                bad_none = _base_package()
                bad_none["files"].append(
                    _file_record("meta/info.txt", "metadata", 1024, 30, **kwargs))
                with self.assertRaisesRegex(PackageError,
                                            "uncompressed size and digest"):
                    validate_package(bad_none)

        tiny_gz = _base_package()
        tiny_gz["files"].append(_file_record(
            "meta/tiny.bin.gz", "metadata", size=48, seed=31,
            compression="gzip", expanded_size=20, expanded_seed=32,
        ))
        self.assertEqual(validate_package(tiny_gz)["files"][-1]["expandedBytes"], 20)

    def test_roles_and_manifest_indexing(self):
        dup_root = _base_package()
        dup_root["files"].append(_file_record("images/root2.img", "linux-root", 1024, 40))
        with self.assertRaisesRegex(PackageError, "linux-root"):
            validate_package(dup_root)

        wrong_role = _base_package()
        wrong_role["files"][1]["role"] = "metadata"
        with self.assertRaisesRegex(PackageError, "manifest file"):
            validate_package(wrong_role)

        missing = _base_package()
        del missing["modelManifests"]["TB323FU"]
        with self.assertRaisesRegex(PackageError, "supportedModels keys"):
            validate_package(missing)

        shared = _base_package()
        shared["modelManifests"]["TB323FU"] = "manifests/tb322fc.json"
        with self.assertRaisesRegex(PackageError, "distinct"):
            validate_package(shared)

    def test_offline_upgrade_and_decky_repair_contracts(self):
        pkg = _base_package()
        pkg["operations"].append("offline-upgrade")
        with self.assertRaisesRegex(PackageError, "gzip root and boot"):
            validate_package(pkg)

        for idx, exp in ((3, 131072), (4, 32768)):
            pkg["files"][idx].update(compression="gzip", expandedBytes=exp,
                                     expandedSha256=_h(60 + idx))
        self.assertIn("offline-upgrade", validate_package(pkg)["operations"])
        self.assertEqual(validate_package(pkg)["recoveryManifests"], {})

        pkg["operations"].append("decky-repair")
        with self.assertRaisesRegex(PackageError, "deckySnapshot"):
            validate_package(pkg)
        pkg["deckySnapshot"] = {
            "releaseId": 1, "tag": "v1.0.0",
            "publishedAt": "2026-04-19T00:00:00Z",
            "assetId": 2, "url": "https://example.invalid/decky.zip",
            "size": 4096, "sha256": _h(70),
        }
        self.assertEqual(validate_package(pkg)["deckySnapshot"]["tag"], "v1.0.0")

    def test_offline_upgrade_accepts_absent_or_partial_recovery_manifests(self):
        pkg = _base_package()
        pkg["operations"].append("offline-upgrade")
        for idx, exp in ((3, 131072), (4, 32768)):
            pkg["files"][idx].update(compression="gzip", expandedBytes=exp,
                                     expandedSha256=_h(60 + idx))
        pkg["files"].append(_file_record("recovery/tb322fc.json", "manifest", 512, 50))
        pkg["recoveryManifests"] = {"TB322FC": "recovery/tb322fc.json"}
        self.assertEqual(validate_package(pkg)["recoveryManifests"],
                         {"TB322FC": "recovery/tb322fc.json"})

        unknown = deepcopy(pkg)
        unknown["recoveryManifests"] = {"TB710FU": "recovery/tb322fc.json"}
        with self.assertRaisesRegex(PackageError, "subset"):
            validate_package(unknown)

        missing = deepcopy(pkg)
        missing["recoveryManifests"] = {"TB322FC": "recovery/missing.json"}
        with self.assertRaisesRegex(PackageError, "indexed file"):
            validate_package(missing)

    def test_require_operation_versions_and_errors(self):
        pkg = _base_package()
        require_operation(pkg, "TB322FC", "install", installer_version="0.10.0")
        require_operation(pkg, "TB322FC", "install", updater_version="0.9.0")

        checks = [
            ({"installer_version": "0.8.9"}, "older than minimumInstallerVersion"),
            ({"updater_version": "0.8.10"}, "older than minimumUpdaterVersion"),
            ({"model": "TB710FU", "installer_version": "0.10.0"}, "model is not supported"),
            ({"operation": "offline-upgrade", "updater_version": "0.10.0"},
             "operation is not declared"),
            ({}, "at least one"),
        ]
        for kwargs, pattern in checks:
            with self.subTest(kwargs=kwargs):
                call = dict(model="TB322FC", operation="install")
                call.update(kwargs)
                with self.assertRaisesRegex(PackageError, pattern):
                    require_operation(pkg, **call)

        for bad_ver in ("not-a-version", "1.0", 123):
            with self.subTest(bad_ver=bad_ver):
                with self.assertRaises(PackageError) as ctx:
                    require_operation(pkg, "TB322FC", "install",
                                      updater_version=bad_ver)
                self.assertIs(type(ctx.exception), PackageError)

    def test_load_package_verifies_signature_before_validation(self):
        key_path = Path("/nonexistent/trusted.pub")
        with patch("updates.package_contract.load_signed_json",
                   side_effect=SignatureError("bad sig")) as mock_sig, \
             patch("updates.package_contract.validate_package") as mock_val:
            with self.assertRaisesRegex(PackageError,
                                        "signature verification failed"):
                load_package(b"not-even-json", b"bad-sig", trusted_key=key_path)
            mock_sig.assert_called_once_with(b"not-even-json", b"bad-sig",
                                             trusted_key=key_path)
            mock_val.assert_not_called()

        with patch("updates.package_contract.load_signed_json",
                   return_value=_base_package()):
            loaded = load_package(b"{}", b"sig", trusted_key=key_path)
        self.assertEqual(loaded["version"], "0.10.0")

        invalid_pkg = _base_package()
        invalid_pkg["schemaVersion"] = 2
        with patch("updates.package_contract.load_signed_json",
                   return_value=invalid_pkg):
            with self.assertRaisesRegex(PackageError, "schemaVersion"):
                load_package(b"{}", b"sig", trusted_key=key_path)


if __name__ == "__main__":
    unittest.main()
