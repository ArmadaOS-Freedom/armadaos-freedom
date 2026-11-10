"""Tests for recovery manifests and universal builder offline-upgrade support."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import test_update_package_builder as builder  # noqa: E402  (also sets up sys.path)

from updates.package_contract import PackageError, validate_package  # noqa: E402
from updates.recovery_manifest import (  # noqa: E402
    MAX_RECOVERY_MANIFEST_BYTES,
    validate_recovery_manifest,
)

BASE = builder.UpdatePackageBuilderTests("test_two_and_three_model_success_and_archive_unchanged")
_sha256 = builder._sha256
_UNSET = object()

KERNEL_RELEASE = "6.18.0-test"
REC_INITRAMFS = b"recovery-initramfs-image"


def _base_doc(model):
    return {
        "schemaVersion": 1,
        "kind": "freedom-update-recovery",
        "deviceModel": model,
        "version": "2026.04.0",
        "sourceRevision": builder.REV,
        "ostreeCommit": builder.COMMIT,
        "protocol": "offline-rpc-v1",
        "kernelRelease": KERNEL_RELEASE,
        "artifacts": {
            "kernel": "common/boot/Image",
            "initramfs": "recovery/common/initramfs.img",
            "dtb": f"recovery/{model}/recovery.dtb",
            "runtime": f"packages/{model}/runtime/runtime.img",
        },
    }


def _doc_bytes(doc):
    return doc if isinstance(doc, bytes) else json.dumps(doc).encode("utf-8")


def make_recovery_mutate(models, hook=None):
    """Return a mutate() that installs complete recovery materials for models.

    hook(bundle, members, docs, rec_map) may adjust docs (dict or raw bytes for
    crafted JSON), rec_map, or return a replacement bundle.recoveryManifests value.
    """
    def mutate(bundle, model_docs, members):
        # The producer only marks a bundle after it verified the normal updater.
        bundle["offlineUpgrade"] = True
        members["recovery/common/initramfs.img"] = REC_INITRAMFS
        docs = {}
        for model in models:
            members[f"recovery/{model}/recovery.dtb"] = f"recovery-dtb-{model}".encode("utf-8")
            docs[model] = _base_doc(model)
        rec_map = {model: f"recovery/{model}/recovery.json" for model in models}
        replacement = hook(bundle, members, docs, rec_map) if hook else _UNSET
        for model in models:
            members[f"recovery/{model}/recovery.json"] = _doc_bytes(docs[model])
        if replacement is None or replacement is _UNSET:
            bundle["recoveryManifests"] = dict(rec_map)
        else:
            bundle["recoveryManifests"] = replacement
    return mutate


def _recovery_archive(directory, models=("TB322FC", "TB323FU"), hook=None,
                      enable=None, **kwargs):
    archive = BASE._make_bundle(directory, models=models,
                                mutate=make_recovery_mutate(models, hook))
    if enable is None:
        return archive, builder.build_descriptor(archive, release_id="rel-1", **kwargs)
    return archive, builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=enable, **kwargs)


def _rebind(desc, model, doc):
    """Update the manifest file record in a descriptor copy to match new bytes."""
    from copy import deepcopy
    data = deepcopy(desc)
    raw = _doc_bytes(doc)
    path = data["recoveryManifests"][model]
    record = next(f for f in data["files"] if f["path"] == path)
    record["bytes"] = len(raw)
    record["sha256"] = _sha256(raw)
    record["expandedBytes"] = len(raw)
    record["expandedSha256"] = _sha256(raw)
    return data


class BuilderRecoveryTests(unittest.TestCase):
    def test_valid_two_and_three_models_enable(self):
        for models in (("TB322FC", "TB323FU"), ("TB322FC", "TB323FU", "TB710FU")):
            with self.subTest(models=models), tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                archive, desc = _recovery_archive(tmp_dir, models, enable=True)
                self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
                self.assertEqual(desc["supportedModels"], list(models))
                self.assertEqual(set(desc["recoveryManifests"]), set(models))
                self.assertEqual(validate_package(desc), desc)
                by_path = {f["path"]: f for f in desc["files"]}
                # shared recovery initramfs indexed exactly once
                matches = [f for f in desc["files"] if f["path"] == "recovery/common/initramfs.img"]
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0]["role"], "recovery")
                self.assertEqual(matches[0]["sha256"], _sha256(REC_INITRAMFS))
                for model in models:
                    dtb = by_path[f"recovery/{model}/recovery.dtb"]
                    self.assertEqual(dtb["role"], "recovery")
                    self.assertEqual(dtb["sha256"], _sha256(f"recovery-dtb-{model}".encode()))
                    self.assertEqual(by_path[f"recovery/{model}/recovery.json"]["role"], "manifest")
                    self.assertEqual(by_path[f"packages/{model}/runtime/runtime.img"]["role"], "runtime")
                    self.assertEqual(by_path["common/boot/Image"]["role"], "kernel")

    def test_positive_noop_hook_with_enable(self):
        def noop_hook(bundle, members, docs, rec_map):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            archive, desc = _recovery_archive(Path(tmp), hook=noop_hook, enable=True)
            self.assertIn("offline-upgrade", desc["operations"])
            self.assertEqual(set(desc["recoveryManifests"]), {"TB322FC", "TB323FU"})
            self.assertEqual(validate_package(desc), desc)

    def test_archive_unchanged_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = BASE._make_bundle(
                Path(tmp), mutate=make_recovery_mutate(("TB322FC", "TB323FU"))
            )
            before = archive.read_bytes()
            desc = builder.build_descriptor(
                archive, release_id="rel-1", enable_offline_upgrade=True
            )
            self.assertEqual(archive.read_bytes(), before)
            self.assertIn("offline-upgrade", desc["operations"])

    def test_default_flag_install_only_even_with_materials(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, desc = _recovery_archive(Path(tmp))
            self.assertEqual(desc["operations"], ["install"])
            self.assertEqual(set(desc["recoveryManifests"]), {"TB322FC", "TB323FU"})

    def test_absent_or_empty_map_default_success(self):
        for hook in (None, lambda b, z, d, r: {}):
            with self.subTest(hook=hook is not None), tempfile.TemporaryDirectory() as tmp:
                if hook is None:
                    archive = BASE._make_bundle(Path(tmp))
                    desc = builder.build_descriptor(archive, release_id="rel-1")
                else:
                    _, desc = _recovery_archive(Path(tmp), hook=hook)
                self.assertEqual(desc["operations"], ["install"])
                self.assertEqual(desc["recoveryManifests"], {})

    def test_null_map_rejected_with_or_without_flag(self):
        def mutate_null(bundle, model_docs, members):
            bundle["recoveryManifests"] = None

        with tempfile.TemporaryDirectory() as tmp:
            archive = BASE._make_bundle(Path(tmp), mutate=mutate_null)
            with self.assertRaises(PackageError):
                builder.build_descriptor(archive, release_id="rel-1")
            with self.assertRaises(PackageError):
                builder.build_descriptor(archive, release_id="rel-1",
                                         enable_offline_upgrade=True)

    def test_enable_without_recovery_map_succeeds(self):
        def capable(bundle, model_docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            archive = BASE._make_bundle(Path(tmp), mutate=capable)
            desc = builder.build_descriptor(archive, release_id="rel-1",
                                            enable_offline_upgrade=True)
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertEqual(validate_package(desc), desc)

    def test_enable_with_empty_map_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, desc = _recovery_archive(Path(tmp), hook=lambda b, z, d, r: {},
                                        enable=True)
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(desc["recoveryManifests"], {})
            self.assertEqual(validate_package(desc), desc)

    def test_enable_without_updater_capability_fails(self):
        def mutate(bundle, model_docs, members):
            make_recovery_mutate(("TB322FC", "TB323FU"))(bundle, model_docs, members)
            bundle.pop("offlineUpgrade")

        with tempfile.TemporaryDirectory() as tmp:
            archive = BASE._make_bundle(Path(tmp), mutate=mutate)
            with self.assertRaisesRegex(PackageError, r"(?i)normal updater"):
                builder.build_descriptor(archive, release_id="rel-1",
                                         enable_offline_upgrade=True)

    def test_flag_type_strict_before_file_io(self):
        for bad in (0, 1, "yes", None):
            with self.subTest(flag=bad), tempfile.TemporaryDirectory() as tmp:
                missing = Path(tmp) / "does-not-exist.zip"
                with self.assertRaisesRegex(PackageError, r"(?i)boolean"):
                    builder.build_descriptor(missing, release_id="rel-1",
                                             enable_offline_upgrade=bad)

    def test_partial_recovery_manifests_still_validated(self):
        def partial(b, z, d, r):
            r.pop("TB323FU")

        with tempfile.TemporaryDirectory() as tmp:
            archive, desc = _recovery_archive(Path(tmp), hook=partial, enable=True)
            self.assertEqual(desc["operations"], ["install", "offline-upgrade"])
            self.assertEqual(set(desc["recoveryManifests"]), {"TB322FC"})

    def test_invalid_recovery_manifests_maps(self):
        def unknown_model(b, z, d, r):
            r["TB710FU"] = "recovery/TB710FU/recovery.json"

        def duplicate_path(b, z, d, r):
            r["TB323FU"] = r["TB322FC"]

        def missing_member(b, z, d, r):
            r["TB322FC"] = "recovery/ghost.json"

        def non_object_list(b, z, d, r):
            return ["recovery/TB322FC/recovery.json"]

        def non_object_string(b, z, d, r):
            return "recovery/TB322FC/recovery.json"

        for label, hook in (
            ("unknown_model", unknown_model),
            ("duplicate_path", duplicate_path), ("missing_member", missing_member),
            ("list", non_object_list), ("string", non_object_string),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = BASE._make_bundle(Path(tmp), mutate=make_recovery_mutate(
                    ("TB322FC", "TB323FU"), hook))
                with self.assertRaises(PackageError):
                    builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True)

    def test_stale_identity_fields_rejected(self):
        cases = (
            ("version", "2026.04.1"), ("sourceRevision", builder.ALT_REV),
            ("ostreeCommit", builder.ALT_COMMIT), ("protocol", "offline-rpc-v2"),
            ("kind", "freedom-update"), ("deviceModel", "TB323FU"),
        )
        for field, value in cases:
            def hook(b, z, d, r, field=field, value=value):
                d["TB322FC"][field] = value
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                archive = BASE._make_bundle(Path(tmp), mutate=make_recovery_mutate(
                    ("TB322FC", "TB323FU"), hook))
                with self.assertRaises(PackageError):
                    builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True)

    def test_cross_references_rejected(self):
        def runtime_other_model(b, z, d, r):
            d["TB322FC"]["artifacts"]["runtime"] = "packages/TB323FU/runtime/runtime.img"

        def kernel_other_role(b, z, d, r):
            d["TB322FC"]["artifacts"]["kernel"] = "packages/TB322FC/dtb/normal.dtb"

        def kernel_missing(b, z, d, r):
            d["TB322FC"]["artifacts"]["kernel"] = "common/boot/Image.missing"

        def dtb_shared(b, z, d, r):
            d["TB323FU"]["artifacts"]["dtb"] = "recovery/TB322FC/recovery.dtb"

        def dtb_is_other_initramfs(b, z, d, r):
            z["recovery/TB323FU/initramfs.img"] = b"private-initramfs"
            d["TB323FU"]["artifacts"]["initramfs"] = "recovery/TB323FU/initramfs.img"
            d["TB322FC"]["artifacts"]["dtb"] = "recovery/TB323FU/initramfs.img"

        def dtb_is_install_dtb(b, z, d, r):
            d["TB322FC"]["artifacts"]["dtb"] = "packages/TB322FC/dtb/normal.dtb"

        def dtb_is_manifest(b, z, d, r):
            d["TB322FC"]["artifacts"]["dtb"] = "packages/TB322FC/package.json"

        def initramfs_is_install_initrd(b, z, d, r):
            d["TB322FC"]["artifacts"]["initramfs"] = "common/boot/initramfs.cpio.gz"

        for label, hook in (
            ("runtime_other_model", runtime_other_model),
            ("kernel_other_role", kernel_other_role),
            ("kernel_missing", kernel_missing),
            ("dtb_shared", dtb_shared),
            ("dtb_is_other_initramfs", dtb_is_other_initramfs),
            ("dtb_is_install_dtb", dtb_is_install_dtb),
            ("dtb_is_manifest", dtb_is_manifest),
            ("initramfs_is_install_initrd", initramfs_is_install_initrd),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = BASE._make_bundle(Path(tmp), mutate=make_recovery_mutate(
                    ("TB322FC", "TB323FU"), hook))
                with self.assertRaises(PackageError):
                    builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True)

    def test_duplicate_keys_excess_key_and_paths_rejected(self):
        def dup_keys(b, z, d, r):
            d["TB322FC"] = b'{"schemaVersion":1,"schemaVersion":1}'

        def command_field(b, z, d, r):
            d["TB322FC"]["command"] = "poweroff"

        def path_escape(b, z, d, r):
            d["TB322FC"]["artifacts"]["dtb"] = "../escape.dtb"

        def path_reserved(b, z, d, r):
            d["TB322FC"]["artifacts"]["dtb"] = "recovery/con/x.dtb"

        def path_absolute(b, z, d, r):
            d["TB322FC"]["artifacts"]["dtb"] = "/etc/passwd"

        for label, hook in (
            ("duplicate_keys", dup_keys), ("command_field", command_field),
            ("path_escape", path_escape), ("path_reserved", path_reserved),
            ("path_absolute", path_absolute),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                archive = BASE._make_bundle(Path(tmp), mutate=make_recovery_mutate(
                    ("TB322FC", "TB323FU"), hook))
                with self.assertRaises(PackageError):
                    builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True)


class RecoverySplitTests(unittest.TestCase):
    def test_split_matches_whole_and_original_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = BASE._make_bundle(
                tmp_dir, mutate=make_recovery_mutate(("TB322FC", "TB323FU")))
            original = archive.read_bytes()
            whole = builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True)
            manifest_path, split_doc = builder._split_to_manifest(
                archive, tmp_dir / "split", part_bytes=300)
            split = builder.build_descriptor(archive, release_id="rel-1",
                                             enable_offline_upgrade=True,
                                             parts_manifest=manifest_path)
            self.assertEqual(split["files"], whole["files"])
            self.assertEqual(split["archive"], whole["archive"])
            self.assertEqual(split["operations"], whole["operations"])
            self.assertEqual(split["recoveryManifests"], whole["recoveryManifests"])
            self.assertEqual(split["parts"], split_doc["parts"])
            self.assertTrue(split["parts"][0]["fileName"].endswith(".zip.001"))
            self.assertEqual(archive.read_bytes(), original)

    def test_split_missing_or_corrupt_part_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            archive = BASE._make_bundle(
                tmp_dir, mutate=make_recovery_mutate(("TB322FC", "TB323FU")))
            manifest_a, doc_a = builder._split_to_manifest(archive, tmp_dir / "c", part_bytes=300)
            part = manifest_a.parent / doc_a["parts"][0]["fileName"]
            data = bytearray(part.read_bytes())
            data[0] ^= 0xFF
            part.write_bytes(bytes(data))
            with self.assertRaisesRegex(PackageError, r"(?i)(sha256|digest|hash|mismatch)"):
                builder.build_descriptor(archive, release_id="rel-1",
                                         enable_offline_upgrade=True,
                                         parts_manifest=manifest_a)
            manifest_b, doc_b = builder._split_to_manifest(archive, tmp_dir / "m", part_bytes=300)
            (manifest_b.parent / doc_b["parts"][0]["fileName"]).unlink()
            with self.assertRaisesRegex(PackageError, r"(?i)(missing|cannot|open|read|no such)"):
                builder.build_descriptor(archive, release_id="rel-1",
                                         enable_offline_upgrade=True,
                                         parts_manifest=manifest_b)


class DirectValidateTests(unittest.TestCase):
    def _package(self, tmp, enable=True):
        return _recovery_archive(Path(tmp), enable=enable)

    def test_returns_copy_and_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, desc = self._package(tmp)
            doc = _doc_bytes(_base_doc("TB322FC"))
            frozen_pkg = json.dumps(desc, sort_keys=True)
            result = validate_recovery_manifest(doc, desc, "TB322FC")
            self.assertEqual(validate_package(desc), desc)
            self.assertEqual(json.dumps(desc, sort_keys=True), frozen_pkg)
            result["deviceModel"] = "tampered"
            self.assertNotEqual(result["deviceModel"], "TB322FC")
            self.assertEqual(json.dumps(desc, sort_keys=True), frozen_pkg)
            # metadata only: no signature material is produced or required
            self.assertNotIn("signature", result)
            validate_recovery_manifest(doc, desc, "TB322FC")

    def test_document_must_be_bytes_and_model_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, desc = self._package(tmp)
            doc = _doc_bytes(_base_doc("TB322FC"))
            for bad_doc in (doc.decode("utf-8"), bytearray(doc), memoryview(doc)):
                with self.subTest(doc=type(bad_doc).__name__):
                    with self.assertRaises(PackageError):
                        validate_recovery_manifest(bad_doc, desc, "TB322FC")
            for bad_model in (None, 1, b"TB322FC", "TB710FU"):
                with self.subTest(model=bad_model):
                    with self.assertRaises(PackageError):
                        validate_recovery_manifest(doc, desc, bad_model)

    def test_tampered_or_length_mismatched_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, desc = self._package(tmp)
            doc = _doc_bytes(_base_doc("TB322FC"))
            variants = (doc[:-1], doc + b" ", doc[:5] + bytes([doc[5] ^ 0x01]) + doc[6:])
            for index, bad in enumerate(variants):
                with self.subTest(case=index):
                    with self.assertRaises(PackageError):
                        validate_recovery_manifest(bad, desc, "TB322FC")

    def test_model_without_recovery_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = BASE._make_bundle(Path(tmp))
            desc = builder.build_descriptor(archive, release_id="rel-1")
            doc = _doc_bytes(_base_doc("TB322FC"))
            with self.assertRaises(PackageError):
                validate_recovery_manifest(doc, desc, "TB322FC")

    def test_schema_negatives_rejected(self):
        def bool_schema(doc):
            doc["schemaVersion"] = True

        def float_version(doc):
            doc["version"] = 1.5

        def nan(doc):
            return json.dumps(doc).replace('"%s"' % KERNEL_RELEASE, "NaN").encode("utf-8")

        def invalid_utf8(doc):
            return b'{"schemaVersion":1,\xff}'

        def oversized(doc):
            return json.dumps(doc).encode("utf-8") + b" " * (MAX_RECOVERY_MANIFEST_BYTES + 1)

        def wrong_role(doc):
            doc["artifacts"]["dtb"] = "packages/TB322FC/dtb/normal.dtb"

        def compressed(doc):
            doc["artifacts"]["kernel"] = "common/images/root.img.gz"

        def excess(doc):
            doc["extra"] = "arbitrary"

        for label, mutate in (
            ("bool_schema", bool_schema), ("float_version", float_version),
            ("nan", nan), ("invalid_utf8", invalid_utf8),
            ("oversized", oversized), ("wrong_role", wrong_role),
            ("compressed", compressed), ("excess", excess),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                _, desc = self._package(tmp)
                doc = _base_doc("TB322FC")
                result = mutate(doc)
                raw = _doc_bytes(result if result is not None else doc)
                rebound = _rebind(desc, "TB322FC", raw)
                with self.assertRaises(PackageError):
                    validate_recovery_manifest(raw, rebound, "TB322FC")
