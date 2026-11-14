#!/usr/bin/python3
"""Real signed gzip payload expansion and OSTree import integration tests."""
from __future__ import annotations

from copy import deepcopy
import gzip
import importlib.util
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))


def _load_fixture():
    path = ROOT / "tests/update-offline-image-test.py"
    spec = importlib.util.spec_from_file_location("update_offline_payload_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


image_fix = _load_fixture()
release_fix = image_fix.release_fix
store_fix = image_fix.store_fix
from updates import offline_payload
from updates.offline_source import OfflineSourceError


class OfflinePayloadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        image_fix.OfflineImageIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        image_fix.OfflineImageIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.fixture = image_fix.OfflineImageIntegrationTests(
            "test_real_btrfs_import_preserves_repo_metadata_and_excludes_decoys"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.temp = self.fixture.temp
        self.image = self.fixture.image
        self.payload_dir = self.temp / "payload"
        self.payload = self.payload_dir / "images/linux-root.img.gz"
        self.payload.parent.mkdir(parents=True)
        with self.image.open("rb") as source, self.payload.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
                shutil.copyfileobj(source, stream)
        self.package = self.fixture._package(self.image)
        self.entry = next(f for f in self.package["files"] if f.get("role") == "linux-root")
        self.entry.update({
            "path": "images/linux-root.img.gz",
            "bytes": self.payload.stat().st_size,
            "sha256": image_fix.sha256(self.payload),
            "compression": "gzip",
        })
        self.entry.pop("size", None)
        self.assertEqual(self.entry["expandedBytes"], self.image.stat().st_size)
        self.assertEqual(self.entry["expandedSha256"], image_fix.sha256(self.image))
        self.document = self.fixture.fixture._document(self.package)
        self.signature = release_fix._sign_bytes(
            self.document, release_fix.ChannelReleaseTest.priv_key
        )
        self.work_number = 0

    def _work(self, label: str) -> Path:
        self.work_number += 1
        return self.temp / f"payload-work-{self.work_number}-{label}"

    def _invoke(self, *, package=None, signature=None, work=None, **overrides):
        if package is not None:
            document = self.fixture.fixture._document(package)
            signature = release_fix._sign_bytes(
                document, release_fix.ChannelReleaseTest.priv_key
            )
        else:
            document = self.document
        args = {
            "document": document,
            "signature_b64": self.signature if signature is None else signature,
            "payload_directory": self.payload_dir,
            "expected_channel": "stable",
            "model": self.fixture.fixture.fixture.model,
            "updater_version": "1.0.0",
            "sysroot_path": self.fixture.fixture.root,
            "work_dir": self._work("import") if work is None else work,
            "trusted_key": release_fix.ChannelReleaseTest.pub_key,
        }
        args.update(overrides)
        return offline_payload.import_offline_payload(**args)

    def _assert_untouched(self) -> None:
        self.fixture._assert_untouched()

    def test_free_space_preflight_covers_expanded_image_and_repository(self) -> None:
        required = self.entry["expandedBytes"] * 2
        work = self._work("space-boundary-short")
        with mock.patch.object(
            offline_payload.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=required - 1, f_frsize=1),
        ):
            with mock.patch.object(offline_payload, "import_offline_image") as imported:
                with self.assertRaises(OfflineSourceError) as caught:
                    self._invoke(work=work)
        imported.assert_not_called()
        self.assertFalse(work.exists())
        message = str(caught.exception)
        self.assertIn(str(required), message)
        self.assertIn("repository", message)
        self._assert_untouched()

        work = self._work("space-boundary-exact")
        with mock.patch.object(
            offline_payload.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=required, f_frsize=1),
        ):
            with mock.patch.object(
                offline_payload,
                "import_offline_image",
                return_value={"phase": "imported", "commit": "c" * 64},
            ) as imported:
                result = self._invoke(work=work)
        imported.assert_called_once()
        self.assertEqual(result["phase"], "imported")
        self.assertEqual(result["payloadPath"], str(self.payload))
        self.assertEqual(result["expandedImage"], str(work / "linux-root.img"))
        self.assertEqual(image_fix.sha256(self.payload), self.entry["sha256"])
        self._assert_untouched()

    def test_real_gzip_import_preserves_source_and_current_deployment(self) -> None:
        compressed_before = image_fix.sha256(self.payload)
        image_before = image_fix.sha256(self.image)
        bindings = self.fixture.fixture.fixture._bindings()
        current_etc = self.fixture.fixture.fixture.current_path / "etc"
        etc_digest = store_fix.tree_digest(current_etc)
        result = self._invoke()
        self.fixture.fixture.assert_commit_present()
        self.assertEqual(result["commit"], self.fixture.fixture.commit)
        self.assertEqual(result["phase"], "imported")
        self.assertEqual(result["payloadPath"], str(self.payload))
        self.assertTrue(Path(result["expandedImage"]).is_file())
        self.assertEqual(image_fix.sha256(self.payload), compressed_before)
        self.assertEqual(image_fix.sha256(self.image), image_before)
        self.assertEqual(self.entry["bytes"], self.payload.stat().st_size)
        self.assertEqual(self.entry["sha256"], compressed_before)
        self.assertEqual([p for p in self.payload_dir.rglob("*") if p.is_file()], [self.payload])
        self.assertEqual(self.fixture.fixture.fixture._bindings(), bindings)
        self.assertEqual(store_fix.tree_digest(current_etc), etc_digest)
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.fixture.fixture.destination}"),
            self.fixture.fixture.refs_before,
        )
        self.fixture.fixture.fixture._assert_user_data_unchanged()

    def test_authentication_compatibility_and_compressed_metadata_do_no_work(self) -> None:
        no_operation = deepcopy(self.package)
        no_operation["operations"] = ["install"]
        bad_bytes = deepcopy(self.package)
        next(f for f in bad_bytes["files"] if f.get("role") == "linux-root")["bytes"] += 1
        bad_hash = deepcopy(self.package)
        next(f for f in bad_hash["files"] if f.get("role") == "linux-root")["sha256"] = "0" * 64
        cases = [
            {"signature": b"invalid-signature\n"},
            {"package": no_operation},
            {"model": "not-a-supported-model"},
            {"updater_version": "0.1.0"},
            {"package": bad_bytes},
            {"package": bad_hash},
        ]
        for overrides in cases:
            with self.subTest(overrides=list(overrides)):
                work = self._work("rejected")
                with mock.patch.object(offline_payload, "import_offline_image") as imported:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(work=work, **overrides)
                imported.assert_not_called()
                self.assertFalse(work.exists())
                self._assert_untouched()

    def test_signed_truncation_and_expanded_mismatch_never_import(self) -> None:
        original = self.payload.read_bytes()
        self.payload.write_bytes(original[:-1])
        truncated = deepcopy(self.package)
        entry = next(f for f in truncated["files"] if f.get("role") == "linux-root")
        entry.update({"bytes": self.payload.stat().st_size, "sha256": image_fix.sha256(self.payload)})
        with mock.patch.object(offline_payload, "import_offline_image") as imported:
            with self.assertRaises(OfflineSourceError):
                self._invoke(package=truncated, work=self._work("truncated"))
        imported.assert_not_called()
        self._assert_untouched()
        self.payload.write_bytes(original)
        size = self.image.stat().st_size
        for changes in (
            {"expandedSha256": "0" * 64},
            {"expandedBytes": size + 1},
            {"expandedBytes": size - 1},
        ):
            with self.subTest(changes=changes):
                package = deepcopy(self.package)
                next(f for f in package["files"] if f.get("role") == "linux-root").update(changes)
                with mock.patch.object(offline_payload, "import_offline_image") as imported:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(package=package, work=self._work("expanded"))
                imported.assert_not_called()
                self._assert_untouched()

    def test_unsafe_paths_space_and_existing_work_refuse_before_import(self) -> None:
        saved = self.payload.with_suffix(".saved")
        self.payload.rename(saved)
        cases = [self._work("missing")]
        cases.append(self._work("symlink"))
        cases.append(self.payload_dir / "work-overlap")
        existing = self._work("existing")
        existing.mkdir()
        marker = existing / "keep"
        marker.write_text("preserve\n")
        cases.append(existing)
        for index, work in enumerate(cases):
            with self.subTest(work=str(work)):
                if index == 1:
                    self.payload.symlink_to(self.image)
                if index == 2:
                    self.payload.unlink()
                    saved.rename(self.payload)
                with mock.patch.object(offline_payload, "import_offline_image") as imported:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(work=work)
                imported.assert_not_called()
                self._assert_untouched()
        self.assertEqual(marker.read_text(), "preserve\n")
        work = self._work("no-space")
        space = SimpleNamespace(f_bavail=0, f_frsize=4096)
        with mock.patch.object(offline_payload.os, "statvfs", return_value=space):
            with mock.patch.object(offline_payload, "import_offline_image") as imported:
                with self.assertRaises(OfflineSourceError):
                    self._invoke(work=work)
        imported.assert_not_called()
        self.assertFalse(work.exists())
        self._assert_untouched()

    def test_real_gzip_import_from_canonical_private_staging(self) -> None:
        session = self.fixture.fixture.root / ".armada-update/session"
        incoming = session / "incoming"
        images = incoming / "images"
        images.mkdir(parents=True)
        payload = images / "linux-root.img.gz"
        shutil.copy2(self.payload, payload)
        work = session / "transaction"
        compressed_before = image_fix.sha256(payload)
        image_before = image_fix.sha256(self.image)
        bindings = self.fixture.fixture.fixture._bindings()
        etc_digest = store_fix.tree_digest(
            self.fixture.fixture.fixture.current_path / "etc"
        )
        result = self._invoke(payload_directory=incoming, work=work)
        self.fixture.fixture.assert_commit_present()
        self.assertEqual(result["commit"], self.fixture.fixture.commit)
        self.assertEqual(result["phase"], "imported")
        self.assertEqual(result["payloadPath"], str(payload))
        self.assertEqual(image_fix.sha256(payload), compressed_before)
        expanded = Path(result["expandedImage"])
        self.assertTrue(expanded.is_file())
        self.assertTrue(work.resolve() in expanded.resolve().parents)
        self.assertEqual(image_fix.sha256(expanded), self.entry["expandedSha256"])
        self.assertEqual(image_fix.sha256(self.image), image_before)
        self.assertEqual(self.entry["bytes"], payload.stat().st_size)
        self.assertEqual(self.entry["sha256"], compressed_before)
        content = Path(result["sourceRepository"]).parents[2]
        self.assertTrue(work.resolve() in content.resolve().parents)
        self.assertFalse((content / "var").exists())
        self.assertFalse((content / "home").exists())
        self.assertFalse((content / "root/etc/fstab").exists())
        self.assertFalse((content / "root/ostree/deploy").exists())
        for path in content.rglob("*"):
            relative = str(path.relative_to(content))
            self.assertTrue(
                relative in ("root", "root/ostree", "root/ostree/repo")
                or relative.startswith("root/ostree/repo/"), relative
            )
        self.assertEqual(self.fixture.fixture.fixture._bindings(), bindings)
        self.assertEqual(
            store_fix.tree_digest(self.fixture.fixture.fixture.current_path / "etc"),
            etc_digest,
        )
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.fixture.fixture.destination}"),
            self.fixture.fixture.refs_before,
        )
        self.fixture.fixture.fixture._assert_user_data_unchanged()

    def test_gzip_staged_outside_canonical_private_root_rejected(self) -> None:
        for name in ("ostree", "boot", "var", "home", ".armada-update-other"):
            with self.subTest(staging=name):
                incoming = self.fixture.fixture.root / name / "incoming"
                images = incoming / "images"
                images.mkdir(parents=True)
                payload = images / "linux-root.img.gz"
                shutil.copy2(self.payload, payload)
                work = self._work(name)
                with mock.patch.object(
                    offline_payload, "import_offline_image"
                ) as imported:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(payload_directory=incoming, work=work)
                imported.assert_not_called()
                self.assertFalse(work.exists())
                payload.unlink()
                images.rmdir()
                incoming.rmdir()
                self._assert_untouched()
        canonical = self.fixture.fixture.root / ".armada-update/session/incoming"
        canonical.parent.mkdir(parents=True)
        canonical.symlink_to(self.payload_dir, target_is_directory=True)
        try:
            work = self._work("symlinked-private")
            with mock.patch.object(
                offline_payload, "import_offline_image"
            ) as imported:
                with self.assertRaises(OfflineSourceError):
                    self._invoke(payload_directory=canonical, work=work)
            imported.assert_not_called()
            self.assertFalse(work.exists())
            self._assert_untouched()
        finally:
            canonical.unlink()


if __name__ == "__main__":
    unittest.main()
