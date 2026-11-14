#!/usr/bin/python3
"""Real file-backed Btrfs image-to-OSTree import integration tests.

The signed linux-root expanded size/hash describe the actual image.  Compressed
byte/hash fields remain base-fixture values because import_offline_image accepts
an already-expanded image and does not perform package archive expansion.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import OSTree  # noqa: F401

from updates import offline_image
from updates.offline_source import OfflineSourceError
import test_update_channel_release as release_fix


def _load_fixture():
    path = ROOT / "tests/update-offline-source-test.py"
    spec = importlib.util.spec_from_file_location("update_offline_image_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


source_fix = _load_fixture()
store_fix = source_fix.store_fix


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OfflineImageIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for command in ("/usr/bin/btrfs", "/usr/bin/mkfs.btrfs", "/usr/bin/openssl"):
            if not Path(command).is_file():
                raise RuntimeError(f"required integration-test executable missing: {command}")
        subprocess.run(["/usr/bin/btrfs", "--version"], check=True)
        subprocess.run(["/usr/bin/openssl", "version"], check=True)
        source_fix.OfflineSourceIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        source_fix.OfflineSourceIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.fixture = source_fix.OfflineSourceIntegrationTests(
            "test_real_import_preserves_source_sysroot_and_user_data"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.temp = self.fixture.temp
        self.image = self.temp / "linux-root.btrfs"
        top = self.temp / "image-root"
        repo = top / "root/ostree/repo"
        repo.parent.mkdir(parents=True)
        store_fix.run("cp", "-a", str(self.fixture.donor), str(repo))
        marker = repo / "fixture-xattr"
        marker.write_bytes(b"repository metadata survives Btrfs restore\n")
        os.setxattr(marker, "user.armada-image-test", b"preserved")
        (repo / "fixture-link").symlink_to("fixture-xattr")
        (top / "root/ostree/deploy/data").mkdir(parents=True)
        (top / "root/ostree/deploy/data/decoy").write_text("not repository data\n")
        (top / "root/etc").mkdir()
        (top / "root/etc/fstab").write_text("decoy fstab\n")
        (top / "var").mkdir()
        (top / "var/decoy").write_text("not restored\n")
        (top / "home").mkdir()
        (top / "home/decoy").write_text("not restored\n")
        with self.image.open("xb") as stream:
            stream.truncate(128 * 1024 * 1024)
        store_fix.run(
            "/usr/bin/mkfs.btrfs", "-f", "-s", "4096",
            "--rootdir", str(top), "--subvol", "root",
            "--subvol", "var", "--subvol", "home", str(self.image),
        )
        self.package = self._package(self.image)
        self.document = self.fixture._document(self.package)
        self.signature = release_fix._sign_bytes(
            self.document, release_fix.ChannelReleaseTest.priv_key
        )
        self.work_number = 0

    def _package(self, image: Path, **changes) -> dict:
        package = deepcopy(self.fixture.package)
        entries = [f for f in package["files"] if f.get("role") == "linux-root"]
        self.assertEqual(len(entries), 1)
        entries[0].update({
            "expandedBytes": image.stat().st_size,
            "expandedSha256": sha256(image),
        })
        package.update(changes)
        return package

    def _work(self, label: str) -> Path:
        self.work_number += 1
        return self.temp / f"image-work-{self.work_number}-{label}"

    def _invoke(self, *, image=None, package=None, signature=None, work=None):
        if package is not None:
            document = self.fixture._document(package)
            signature = release_fix._sign_bytes(
                document, release_fix.ChannelReleaseTest.priv_key
            )
        else:
            document = self.document
        return offline_image.import_offline_image(
            document=document,
            signature_b64=self.signature if signature is None else signature,
            root_image=self.image if image is None else image,
            expected_channel="stable", model=self.fixture.fixture.model,
            updater_version="1.0.0", sysroot_path=self.fixture.root,
            work_dir=self._work("import") if work is None else work,
            trusted_key=release_fix.ChannelReleaseTest.pub_key,
        )

    def _assert_untouched(self) -> None:
        self.fixture.assert_commit_absent()
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.fixture.destination}"),
            self.fixture.refs_before,
        )
        self.fixture.fixture._assert_user_data_unchanged()

    def test_real_btrfs_import_preserves_repo_metadata_and_excludes_decoys(self) -> None:
        before = sha256(self.image)
        donor_digest = store_fix.tree_digest(self.fixture.donor)
        bindings = self.fixture.fixture._bindings()
        etc_digest = store_fix.tree_digest(self.fixture.fixture.current_path / "etc")
        with mock.patch.object(
            offline_image, "_run_command", wraps=offline_image._run_command
        ) as run_command:
            result = self._invoke()
        command = run_command.call_args.args[0]
        self.assertEqual(command[:5], ["/usr/bin/btrfs", "restore", "-r", "5", "-x"])
        self.assertNotIn("-i", command)
        self.assertNotIn("-o", command)
        self.fixture.assert_commit_present()
        self.assertEqual(result["commit"], self.fixture.commit)
        self.assertEqual(result["phase"], "imported")
        self.assertTrue(Path(result["extractionLog"]).is_file())
        staged = Path(result["sourceRepository"])
        self.assertEqual(sha256(self.image), before)
        self.assertEqual(store_fix.tree_digest(self.fixture.donor), donor_digest)
        self.assertEqual(self.fixture.fixture._bindings(), bindings)
        self.assertEqual(
            store_fix.tree_digest(self.fixture.fixture.current_path / "etc"), etc_digest
        )
        self.fixture.fixture._assert_user_data_unchanged()
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.fixture.destination}"),
            self.fixture.refs_before,
        )
        content = staged.parents[2]
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
        restored = staged / "fixture-xattr"
        self.assertEqual(os.getxattr(restored, "user.armada-image-test"), b"preserved")
        self.assertTrue((staged / "fixture-link").is_symlink())
        self.assertEqual(os.readlink(staged / "fixture-link"), "fixture-xattr")

    def test_signed_wrong_image_size_or_hash_never_runs_btrfs(self) -> None:
        for changes in ({"expandedBytes": self.image.stat().st_size + 1},
                        {"expandedSha256": "0" * 64}):
            with self.subTest(changes=changes):
                package = self._package(self.image)
                entry = next(f for f in package["files"] if f.get("role") == "linux-root")
                entry.update(changes)
                with mock.patch.object(offline_image, "_run_command") as run_command:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(package=package)
                run_command.assert_not_called()
                self._assert_untouched()

    def test_invalid_signature_does_no_work(self) -> None:
        work = self._work("bad-signature")
        with self.assertRaises(OfflineSourceError):
            self._invoke(signature=b"invalid-signature\n", work=work)
        self.assertFalse(work.exists())
        self._assert_untouched()

    def test_corrupted_signed_image_fails_real_extraction(self) -> None:
        blank = self.temp / "not-btrfs.img"
        with blank.open("xb") as stream:
            stream.truncate(128 * 1024 * 1024)
        package = self._package(blank)
        entry = next(f for f in package["files"] if f.get("role") == "linux-root")
        self.assertEqual(sha256(blank), entry["expandedSha256"])
        work = self._work("corrupt")
        with self.assertRaises(OfflineSourceError):
            self._invoke(image=blank, package=package, work=work)
        self.assertTrue((work / "extract.log").is_file())
        self.assertGreater((work / "extract.log").stat().st_size, 0)
        self._assert_untouched()

    def test_signed_wrong_commit_aborts_after_extraction(self) -> None:
        package = self._package(self.image, ostreeCommit="f" * 64)
        bindings = self.fixture.fixture._bindings()
        with mock.patch.object(
            offline_image, "_run_command", wraps=offline_image._run_command
        ) as run_command:
            with self.assertRaises(OfflineSourceError):
                self._invoke(package=package)
        run_command.assert_called_once()
        self.assertEqual(self.fixture.fixture._bindings(), bindings)
        self._assert_untouched()

    def test_symlinked_image_ancestor_and_work_overlap_reject(self) -> None:
        image_link = self.temp / "image-link"
        image_link.symlink_to(self.image)
        parent_link = self.temp / "parent-link"
        parent_link.symlink_to(self.temp, target_is_directory=True)
        cases = [
            (image_link, self._work("image-symlink")),
            (parent_link / self.image.name, self._work("ancestor-symlink")),
            (self.image, self.image),
        ]
        for image, work in cases:
            with self.subTest(image=str(image), work=str(work)):
                with mock.patch.object(offline_image, "_run_command") as run_command:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(image=image, work=work)
                run_command.assert_not_called()
                self._assert_untouched()

    def test_image_inside_sysroot_outside_canonical_private_root_rejected(self) -> None:
        for name in (".armada-update-other", "boot"):
            with self.subTest(staging=name):
                incoming = self.fixture.root / name / "incoming"
                incoming.mkdir(parents=True)
                staged = incoming / "linux-root.btrfs"
                shutil.copy2(self.image, staged)
                work = self._work(name)
                with mock.patch.object(offline_image, "_run_command") as run_command:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(image=staged, work=work)
                run_command.assert_not_called()
                self.assertFalse(work.exists())
                staged.unlink()
                incoming.rmdir()
                self._assert_untouched()


if __name__ == "__main__":
    unittest.main()
