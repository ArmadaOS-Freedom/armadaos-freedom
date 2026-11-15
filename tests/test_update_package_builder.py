import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates.package_contract import (
    AUTHORIZED_MODELS, OFFLINE_UPGRADE_MODELS, PackageError, require_operation,
    validate_package,
)

CONFIG_PATH = ROOT / "build_files" / "edl-devices.json"
CUSTOM_MODEL = "THIRD"


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SPLITTER = _load_module("split_package_archive", "build_files/split-package-archive.py")
_BUILDER = _load_module("universal_package", "build_files/universal_package.py")
build_descriptor = _BUILDER.build_descriptor

REV = "a" * 40
COMMIT = "b" * 64
DIGEST = "sha256:" + "c" * 64
ALT_REV = "d" * 40
ALT_COMMIT = "e" * 64
ALT_DIGEST = "sha256:" + "f" * 64


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gzip_bytes(raw: bytes) -> bytes:
    return gzip.compress(raw, mtime=0)


def _artifact_entry(role: str, logical_file: str, stored: bytes, raw: bytes | None = None) -> dict:
    entry = {
        "role": role,
        "file": logical_file,
        "bytes": len(stored),
        "sha256": _sha256(stored),
    }
    if raw is not None:
        entry.update({
            "compression": "gzip",
            "expandedBytes": len(raw),
            "expandedSha256": _sha256(raw),
        })
    return entry


def _write_stored_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, payload)
    return path


def _split_to_manifest(archive_path: Path, out_dir: Path, part_bytes: int = 256) -> tuple[Path, dict]:
    manifest = _SPLITTER.split_archive(archive_path, out_dir, part_bytes=part_bytes)
    return out_dir / f"{archive_path.name}.parts.json", manifest


class UpdatePackageBuilderTests(unittest.TestCase):
    def _make_bundle(self, directory: Path, models=("TB322FC", "TB323FU"),
                     raw_root: bytes = b"root-filesystem-payload-" * 16,
                     raw_boot: bytes = b"boot-filesystem-payload-" * 12,
                     mutate=None, archive_name: str = "update-bundle.zip") -> Path:
        gz_root = _gzip_bytes(raw_root)
        gz_boot = _gzip_bytes(raw_boot)
        kernel = b"shared-kernel-image"
        initramfs = b"shared-initramfs-image"

        members: dict[str, bytes] = {
            "common/images/root.img.gz": gz_root,
            "common/images/boot.img.gz": gz_boot,
            "common/boot/Image": kernel,
            "common/boot/initramfs.cpio.gz": initramfs,
        }
        shared_files = [
            {"source": "common/images/root.img.gz", "destination": "images/root.img.gz"},
            {"source": "common/images/boot.img.gz", "destination": "images/boot.img.gz"},
            {"source": "common/boot/Image", "destination": "boot/Image"},
            {"source": "common/boot/initramfs.cpio.gz", "destination": "boot/initramfs.cpio.gz"},
        ]

        bundle_packages = []
        model_docs = {}
        for model in models:
            pkg_dir = f"packages/{model}"
            runtime = f"runtime-{model}".encode("utf-8")
            dtb_norm = f"dtb-normal-{model}".encode("utf-8")
            dtb_diag = f"dtb-diagnostic-{model}".encode("utf-8")
            members[f"{pkg_dir}/runtime/runtime.img"] = runtime
            members[f"{pkg_dir}/dtb/normal.dtb"] = dtb_norm
            members[f"{pkg_dir}/dtb/diagnostic.dtb"] = dtb_diag

            bundle_packages.append({
                "deviceModel": model,
                "directory": pkg_dir,
                "sharedFiles": [dict(item) for item in shared_files],
            })
            model_docs[model] = {
                "schemaVersion": 2,
                "deviceModel": model,
                "version": "2026.04.0",
                "testOnly": False,
                "sourceCommit": REV,
                "installTargets": ["internal"],
                "deployment": {
                    "kind": "edl-raw-v1",
                    "commit": COMMIT,
                    "manifestDigest": DIGEST,
                },
                "artifacts": [
                    _artifact_entry("linux-root-raw", "images/root.img.gz", gz_root, raw_root),
                    _artifact_entry("linux-boot-raw", "images/boot.img.gz", gz_boot, raw_boot),
                    _artifact_entry("kernel", "boot/Image", kernel),
                    _artifact_entry("initramfs", "boot/initramfs.cpio.gz", initramfs),
                    _artifact_entry("runtime", "runtime/runtime.img", runtime),
                    _artifact_entry("dtb-normal", "dtb/normal.dtb", dtb_norm),
                    _artifact_entry("dtb-diagnostic", "dtb/diagnostic.dtb", dtb_diag),
                ],
            }

        bundle = {
            "schemaVersion": 2,
            "bundleId": "armadaos-freedom",
            "version": "2026.04.0",
            "packages": bundle_packages,
        }
        if mutate is not None:
            mutate(bundle, model_docs, members)

        members["bundle.json"] = json.dumps(bundle).encode("utf-8")
        for pkg in bundle["packages"]:
            model = pkg["deviceModel"]
            pkg_dir = pkg["directory"]
            if model in model_docs:
                members[f"{pkg_dir}/package.json"] = json.dumps(model_docs[model]).encode("utf-8")

        return _write_stored_zip(directory / archive_name, members)

    def test_two_and_three_model_success_and_archive_unchanged(self):
        for models in (("TB322FC", "TB323FU"), ("TB322FC", "TB323FU", "TB710FU")):
            with self.subTest(models=models), tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                archive = self._make_bundle(tmp_dir, models=models)
                before = archive.read_bytes()
                desc = build_descriptor(archive, release_id="rel-2026.04.0")
                self.assertEqual(archive.read_bytes(), before)
                self.assertEqual(validate_package(desc), desc)
                self.assertEqual(desc["supportedModels"], list(models))
                self.assertEqual(desc["operations"], ["install"])
                self.assertEqual(desc["recoveryManifests"], {})
                self.assertIsNone(desc["deckySnapshot"])
                self.assertEqual(desc["sourceRevision"], REV)
                self.assertEqual(desc["ostreeCommit"], COMMIT)
                self.assertEqual(desc["ociDigest"], DIGEST)
                self.assertEqual(desc["parts"], [])
                by_path = {entry["path"]: entry for entry in desc["files"]}
                for shared_path, role in (
                    ("common/images/root.img.gz", "linux-root"),
                    ("common/images/boot.img.gz", "linux-boot"),
                    ("common/boot/Image", "kernel"),
                    ("common/boot/initramfs.cpio.gz", "initramfs"),
                ):
                    matches = [f for f in desc["files"] if f["path"] == shared_path]
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(by_path[shared_path]["role"], role)

    def test_valid_split_matches_whole_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = self._make_bundle(tmp_dir)
            whole = build_descriptor(archive, release_id="rel-1")
            manifest_path, split_doc = _split_to_manifest(archive, tmp_dir / "split", part_bytes=300)
            split_desc = build_descriptor(archive, release_id="rel-1", parts_manifest=manifest_path)
            self.assertEqual(split_desc["files"], whole["files"])
            self.assertEqual(split_desc["archive"], whole["archive"])
            self.assertEqual(split_desc["parts"], split_doc["parts"])
            self.assertTrue(split_desc["parts"][0]["fileName"].endswith(".zip.001"))

    def test_missing_and_corrupt_split_parts_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = self._make_bundle(tmp_dir)
            manifest_a, doc_a = _split_to_manifest(archive, tmp_dir / "corrupt", part_bytes=300)
            first_a = manifest_a.parent / doc_a["parts"][0]["fileName"]
            data = bytearray(first_a.read_bytes())
            data[0] ^= 0xFF
            first_a.write_bytes(bytes(data))
            with self.assertRaisesRegex(PackageError, r"(?i)(sha256|digest|hash|mismatch)"):
                build_descriptor(archive, release_id="rel-1", parts_manifest=manifest_a)

            manifest_b, doc_b = _split_to_manifest(archive, tmp_dir / "missing", part_bytes=300)
            (manifest_b.parent / doc_b["parts"][0]["fileName"]).unlink()
            with self.assertRaisesRegex(PackageError, r"(?i)(missing|cannot|open|read|no such)"):
                build_descriptor(archive, release_id="rel-1", parts_manifest=manifest_b)

    def test_inconsistent_model_identities_rejected(self):
        cases = [
            ("version", lambda b, m, z: m["TB323FU"].__setitem__("version", "2026.04.1"), r"invalid model manifest"),
            ("revision", lambda b, m, z: m["TB323FU"].__setitem__("sourceCommit", ALT_REV), r"identities differ"),
            ("oci", lambda b, m, z: m["TB323FU"]["deployment"].__setitem__("manifestDigest", ALT_DIGEST), r"identities differ"),
            ("commit", lambda b, m, z: m["TB323FU"]["deployment"].__setitem__("commit", ALT_COMMIT), r"identities differ"),
        ]
        for label, mutator, pattern in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = self._make_bundle(Path(tmp), mutate=mutator)
                with self.assertRaisesRegex(PackageError, pattern):
                    build_descriptor(archive, release_id="rel-1")

    def test_wrong_size_hash_expanded_hash_and_corrupt_gzip_rejected(self):
        def bad_size(b, m, z):
            for doc in m.values():
                doc["artifacts"][2]["bytes"] += 1

        def bad_sha(b, m, z):
            for doc in m.values():
                doc["artifacts"][2]["sha256"] = "0" * 64

        def bad_expanded_sha(b, m, z):
            for doc in m.values():
                doc["artifacts"][0]["expandedSha256"] = "0" * 64

        def corrupt_gzip(b, m, z):
            bad_gz = z["common/images/root.img.gz"][:-4]
            z["common/images/root.img.gz"] = bad_gz
            for doc in m.values():
                doc["artifacts"][0]["bytes"] = len(bad_gz)
                doc["artifacts"][0]["sha256"] = _sha256(bad_gz)

        for label, mutator, pattern in (
            ("size", bad_size, r"size or digest mismatch"),
            ("sha256", bad_sha, r"size or digest mismatch"),
            ("expanded_sha256", bad_expanded_sha, r"(?i)(expanded|digest|sha256|mismatch)"),
            ("corrupt_gzip", corrupt_gzip, r"(?i)(gzip|decompress|corrupt|eof|end-of-stream|invalid|expanded)"),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = self._make_bundle(Path(tmp), mutate=mutator)
                with self.assertRaisesRegex(PackageError, pattern):
                    build_descriptor(archive, release_id="rel-1")

    def test_tiny_gzip_larger_than_raw_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_tiny = b"x"
            gz_tiny = _gzip_bytes(raw_tiny)
            self.assertGreater(len(gz_tiny), len(raw_tiny))
            archive = self._make_bundle(Path(tmp), raw_root=raw_tiny, raw_boot=b"y")
            desc = build_descriptor(archive, release_id="rel-1")
            root_entry = next(f for f in desc["files"] if f["path"] == "common/images/root.img.gz")
            self.assertGreater(root_entry["bytes"], root_entry["expandedBytes"])
            self.assertEqual(root_entry["expandedBytes"], 1)

    def test_missing_artifact_duplicate_model_and_test_only_rejected(self):
        def missing_role(b, m, z):
            m["TB322FC"]["artifacts"].pop()

        def duplicate_model(b, m, z):
            b["packages"][1]["deviceModel"] = "TB322FC"

        def test_only_true(b, m, z):
            m["TB322FC"]["testOnly"] = True

        for label, mutator, pattern in (
            ("missing_artifact", missing_role, r"seven required roles"),
            ("duplicate_model", duplicate_model, r"invalid or duplicated"),
            ("test_only", test_only_true, r"invalid model manifest"),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = self._make_bundle(Path(tmp), mutate=mutator)
                with self.assertRaisesRegex(PackageError, pattern):
                    build_descriptor(archive, release_id="rel-1")


    def test_configured_custom_model_is_an_install_only_descriptor(self):
        registered = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["profiles"]
        self.assertEqual(set(registered), set(AUTHORIZED_MODELS))
        self.assertNotIn(CUSTOM_MODEL, registered)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = self._make_bundle(tmp_dir, models=("TB322FC", CUSTOM_MODEL))
            manifest_path, _split_doc = _split_to_manifest(
                archive, tmp_dir / "split", part_bytes=300
            )
            desc = build_descriptor(
                archive,
                release_id="rel-third",
                parts_manifest=manifest_path,
                install_models=(CUSTOM_MODEL,),
            )
            self.assertEqual(desc["supportedModels"], ["TB322FC", CUSTOM_MODEL])
            self.assertEqual(desc["operations"], ["install"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertTrue(desc["parts"])
            self.assertEqual(
                validate_package(desc, install_models=(CUSTOM_MODEL,)), desc
            )
            # Package consumers keep the shipped, closed model list.
            with self.assertRaisesRegex(PackageError, "supported values"):
                validate_package(desc)

    def test_unregistered_custom_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(Path(tmp), models=("TB322FC", CUSTOM_MODEL))
            with self.assertRaisesRegex(PackageError, "non-default package models"):
                build_descriptor(archive, release_id="rel-third")

    def test_custom_model_cannot_declare_offline_upgrade(self):
        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=("TB322FC", CUSTOM_MODEL), mutate=offline_capable
            )
            with self.assertRaisesRegex(
                PackageError, "offline-upgrade is restricted"
            ):
                build_descriptor(
                    archive,
                    release_id="rel-third",
                    enable_offline_upgrade=True,
                    install_models=(CUSTOM_MODEL,),
                )

    def test_upgrade_capable_models_keep_offline_upgrade(self):
        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        models = ("TB322FC", "TB323FU")
        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=models, mutate=offline_capable
            )
            desc = build_descriptor(
                archive, release_id="rel-default", enable_offline_upgrade=True
            )
            self.assertEqual(desc["supportedModels"], list(models))
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(validate_package(desc), desc)

    def test_single_upgrade_model_keeps_offline_upgrade(self):
        # The descriptor declares operations per package, so a per-model build
        # of either compatible model may still advertise upgrade.
        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        for model in sorted(OFFLINE_UPGRADE_MODELS):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp:
                archive = self._make_bundle(
                    Path(tmp), models=(model,), mutate=offline_capable
                )
                desc = build_descriptor(
                    archive, release_id="rel-single", enable_offline_upgrade=True
                )
                self.assertEqual(desc["supportedModels"], [model])
                self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
                self.assertEqual(validate_package(desc), desc)

    def test_tb710fu_is_install_only_even_with_offline_capable_bundle(self):
        # TB710FU installs but the offline updater rejects it before any slot
        # work, so its package must never advertise upgrade.
        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=("TB710FU",), mutate=offline_capable
            )
            desc = build_descriptor(archive, release_id="rel-gt")
            self.assertEqual(desc["supportedModels"], ["TB710FU"])
            self.assertEqual(desc["operations"], ["install"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertEqual(validate_package(desc), desc)
            with self.assertRaisesRegex(
                PackageError, r"(?i)(offline-upgrade|dual-slot|upgrade)"
            ):
                build_descriptor(
                    archive, release_id="rel-gt", enable_offline_upgrade=True
                )

    def test_mixed_tb710fu_package_upgrades_only_the_dual_slot_models(self):
        # A package-level operations list may declare upgrade even though
        # TB710FU is bundled; the installer gate rejects the request for that
        # model before any slot work, so TB710FU must not suppress the claim.
        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=("TB322FC", "TB710FU"), mutate=offline_capable
            )
            desc = build_descriptor(
                archive, release_id="rel-mixed", enable_offline_upgrade=True
            )
            self.assertEqual(desc["supportedModels"], ["TB322FC", "TB710FU"])
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(validate_package(desc), desc)
            require_operation(desc, "TB322FC", "offline-upgrade",
                              updater_version="0.10.0")
            require_operation(desc, "TB710FU", "install",
                              installer_version="0.10.0")
            with self.assertRaisesRegex(
                PackageError, r"(?i)(offline-upgrade|dual-slot|upgrade)"
            ):
                require_operation(desc, "TB710FU", "offline-upgrade",
                                  updater_version="0.10.0")

    def test_mixed_upgrade_requires_the_verified_bundle_updater(self):
        # A package may not claim upgrade unless the producer recorded that
        # the image carries the normal updater, even in a mixed package.
        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=("TB322FC", "TB323FU", "TB710FU")
            )
            with self.assertRaisesRegex(
                PackageError, "requires a bundle built from an image"
            ):
                build_descriptor(
                    archive, release_id="rel-mixed", enable_offline_upgrade=True
                )

    def test_configured_default_selection_is_install_only(self):
        # The devices config also registers TB710FU. Without an explicit
        # upgrade request the universal package stays install-only.
        models = tuple(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["profiles"])
        self.assertEqual(set(models), set(AUTHORIZED_MODELS))

        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=models, mutate=offline_capable
            )
            desc = build_descriptor(archive, release_id="rel-default")
            self.assertEqual(desc["supportedModels"], list(models))
            self.assertEqual(desc["operations"], ["install"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertEqual(validate_package(desc), desc)

    def test_full_configuration_descriptor_declares_install_and_upgrade(self):
        # The universal package is the single release artifact: every
        # registered model installs, both Y700 models upgrade, and TB710FU
        # fails the operation gate before execution.
        models = tuple(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["profiles"])
        self.assertEqual(set(models), set(AUTHORIZED_MODELS))

        def offline_capable(bundle, docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = self._make_bundle(
                Path(tmp), models=models, mutate=offline_capable
            )
            desc = build_descriptor(
                archive, release_id="rel-default", enable_offline_upgrade=True
            )
            self.assertEqual(desc["supportedModels"], list(models))
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertEqual(validate_package(desc), desc)

            for model in models:
                require_operation(desc, model, "install",
                                  installer_version="0.10.0")
            for model in sorted(OFFLINE_UPGRADE_MODELS):
                require_operation(desc, model, "offline-upgrade",
                                  updater_version="0.10.0")
            with self.assertRaisesRegex(
                PackageError, r"(?i)(offline-upgrade|dual-slot|upgrade)"
            ):
                require_operation(desc, "TB710FU", "offline-upgrade",
                                  updater_version="0.10.0")


if __name__ == "__main__":
    unittest.main()
