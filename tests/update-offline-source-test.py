#!/usr/bin/python3
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import selectors
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, GLib, OSTree

import test_update_channel_release as release_fix
from updates import offline_source
from updates.image_source import ImageSourceError
from updates.offline_source import OfflineSourceError


def _load_store_fixture():
    path = ROOT / "tests/update-deployment-store-test.py"
    spec = importlib.util.spec_from_file_location("update_offline_store_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store_fix = _load_store_fixture()


class OfflineSourceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        store_fix.DeploymentStoreIntegrationTests.setUpClass()
        release_fix.ChannelReleaseTest.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        release_fix.ChannelReleaseTest.tearDownClass()
        store_fix.DeploymentStoreIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.fixture = store_fix.DeploymentStoreIntegrationTests(
            "test_two_updates_merges_retains_and_reuses"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.destination = self.root / "ostree/repo"
        self.commit = self.fixture.commits[1]
        tmp = tempfile.TemporaryDirectory(
            prefix="offline-source-", dir=os.environ.get("TMPDIR")
        )
        self.addCleanup(tmp.cleanup)
        self.temp = Path(tmp.name)
        self.donor = self.temp / "donor"
        store_fix.run("ostree", "init", "--mode=bare", f"--repo={self.donor}")
        store_fix.run(
            "/usr/bin/ostree", "pull-local", f"--repo={self.donor}",
            str(self.destination), self.commit,
        )
        store_fix.run(
            "ostree", "refs", f"--repo={self.destination}",
            "--delete", "fixture-2-arm64",
        )
        store_fix.run("ostree", "prune", "--refs-only", f"--repo={self.destination}")
        self.refs_before = store_fix.run("ostree", "refs", f"--repo={self.destination}")
        self.assert_commit_absent()
        self.package = deepcopy(release_fix.ChannelReleaseTest.pkg_dict)
        self.package.update({
            "ostreeCommit": self.commit,
            "ociDigest": store_fix.DIGESTS[1],
            "sourceRevision": store_fix.REVISIONS[1],
            "version": store_fix.VERSIONS[1],
        })
        self.document = self._document(self.package)
        self.signature = release_fix._sign_bytes(
            self.document, release_fix.ChannelReleaseTest.priv_key
        )
        self.work_number = 0

    def _document(self, package: dict) -> bytes:
        return json.dumps(package, sort_keys=True, separators=(",", ":")).encode()

    def _work(self, label: str) -> Path:
        self.work_number += 1
        return self.temp / f"work-{self.work_number}-{label}"

    def _invoke(self, *, document=None, signature=None, package=None, **overrides):
        if package is not None:
            document = self._document(package)
            signature = release_fix._sign_bytes(
                document, release_fix.ChannelReleaseTest.priv_key
            )
        args = {
            "document": self.document if document is None else document,
            "signature_b64": self.signature if signature is None else signature,
            "source_repo": self.donor,
            "expected_channel": "stable",
            "model": self.fixture.model,
            "updater_version": "1.0.0",
            "sysroot_path": self.root,
            "work_dir": self._work("import"),
            "trusted_key": release_fix.ChannelReleaseTest.pub_key,
        }
        args.update(overrides)
        return offline_source.import_offline_repository(**args)

    def _repo(self, path: Path):
        repo = OSTree.Repo.new(Gio.File.new_for_path(str(path)))
        self.assertTrue(repo.open(None))
        return repo

    def assert_commit_absent(self) -> None:
        try:
            self._repo(self.destination).load_commit(self.commit)
        except GLib.Error:
            pass
        else:
            self.fail(f"commit {self.commit} unexpectedly loadable")
        shown = subprocess.run(
            ["ostree", "show", f"--repo={self.destination}", self.commit],
            capture_output=True,
        )
        self.assertNotEqual(shown.returncode, 0)

    def assert_commit_present(self) -> None:
        ok, variant, state = self._repo(self.destination).load_commit(self.commit)
        self.assertTrue(ok)
        self.assertIsNotNone(variant)
        self.assertEqual(int(state), 0)
        store_fix.run("ostree", "show", f"--repo={self.destination}", self.commit)

    def assert_rejected_without_work(self, work: Path, **overrides) -> None:
        with self.assertRaises(OfflineSourceError):
            self._invoke(work_dir=work, **overrides)
        self.assertFalse(work.exists())
        self.assert_commit_absent()

    def test_real_import_preserves_source_sysroot_and_user_data(self) -> None:
        source_digest = store_fix.tree_digest(self.donor)
        bindings = self.fixture._bindings()
        etc_digest = store_fix.tree_digest(self.fixture.current_path / "etc")
        work = self._work("import")
        result = self._invoke(work_dir=work)
        self.assert_commit_present()
        self.assertEqual(result["phase"], "imported")
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(result["package"], self.package)
        self.assertEqual(result["repo"], str(self.destination))
        self.assertEqual(
            result["originRef"],
            "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
            f"armadaos-freedom:stable@{store_fix.DIGESTS[1]}",
        )
        log = Path(result["importLog"])
        self.assertTrue(log.is_file())
        self.assertEqual(log.parent, work)
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(store_fix.tree_digest(self.donor), source_digest)
        self.assertEqual(self.fixture._bindings(), bindings)
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.destination}"),
            self.refs_before,
        )
        self.assertEqual(
            store_fix.tree_digest(self.fixture.current_path / "etc"), etc_digest
        )
        self.assertEqual(
            (self.fixture.current_path / "etc/custom.conf").read_text(),
            "site=retained\n",
        )
        self.fixture._assert_user_data_unchanged()

    def test_authentication_and_compatibility_reject_before_work(self) -> None:
        unsupported = "not-a-supported-model"
        no_operation = deepcopy(self.package)
        no_operation["operations"] = ["install"]
        cases = [
            {"signature": b"invalid-signature\n"},
            {"model": unsupported},
            {"package": no_operation},
            {"updater_version": "0.1.0"},
        ]
        for overrides in cases:
            with self.subTest(overrides=list(overrides)):
                self.assert_rejected_without_work(self._work("rejected"), **overrides)

    def test_signed_mismatched_metadata_rejects_before_import(self) -> None:
        changes = [
            ("ociDigest", "sha256:" + "f" * 64),
            ("sourceRevision", "f" * 40),
            ("version", "not-the-source-version"),
            ("architecture", "x86_64"),
        ]
        for field, value in changes:
            with self.subTest(field=field):
                package = deepcopy(self.package)
                package[field] = value
                bindings = self.fixture._bindings()
                etc_digest = store_fix.tree_digest(self.fixture.current_path / "etc")
                with mock.patch.object(
                    offline_source, "_run_command",
                    wraps=offline_source._run_command,
                ) as run_command:
                    with self.assertRaises(OfflineSourceError):
                        self._invoke(
                            work_dir=self._work("metadata"), package=package
                        )
                run_command.assert_not_called()
                self.assert_commit_absent()
                self.assertEqual(self.fixture._bindings(), bindings)
                self.assertEqual(
                    store_fix.tree_digest(self.fixture.current_path / "etc"), etc_digest
                )

    def test_unsafe_source_and_work_paths_reject(self) -> None:
        inside = self.root / "media/source"
        inside.parent.mkdir()
        inside.mkdir()
        linked = self.temp / "linked-source-parent"
        linked.symlink_to(self.donor.parent, target_is_directory=True)
        cases = [
            (self.destination, self._work("same")),
            (inside, self._work("inside")),
            (linked / self.donor.name, self._work("symlink")),
            (self.donor, self.donor / "work"),
            (self.donor, self.root / "ostree/work"),
            (self.donor, self.root / "boot/work"),
        ]
        for source, work in cases:
            with self.subTest(source=str(source), work=str(work)):
                self.assert_rejected_without_work(work, source_repo=source)

    def test_empty_sysroot_fails(self) -> None:
        empty = self.temp / "empty-sysroot"
        empty.mkdir()
        store_fix.run("ostree", "admin", "init-fs", "--modern", str(empty))
        store_fix.run(
            "ostree", "config", f"--repo={empty / 'ostree/repo'}",
            "set", "sysroot.bootloader", "none",
        )
        store_fix.run("ostree", "admin", "os-init", f"--sysroot={empty}", "default")
        (empty / "boot").mkdir(exist_ok=True)
        with self.assertRaisesRegex(OfflineSourceError, "existing deployments"):
            self._invoke(sysroot_path=empty)
        self.assert_commit_absent()

    def test_other_process_lock_contention_then_fresh_work_succeeds(self) -> None:
        script = (
            "import sys,time; import gi; gi.require_version('OSTree','1.0'); "
            "from gi.repository import Gio,OSTree; "
            "s=OSTree.Sysroot.new(Gio.File.new_for_path(sys.argv[1])); "
            "ok,locked=s.try_lock(); print(ok and locked,flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            selector = selectors.SelectSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(
                selector.select(timeout=30),
                "lock holder did not report readiness in time",
            )
            self.assertEqual(process.stdout.readline().strip(), "True")
            with self.assertRaisesRegex(OfflineSourceError, "busy"):
                self._invoke()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
        result = self._invoke(work_dir=self._work("after-lock"))
        self.assertEqual(result["phase"], "imported")
        self.assert_commit_present()

    def test_command_failure_is_wrapped_and_fresh_retry_imports(self) -> None:
        failed_work = self._work("failed-command")
        with mock.patch.object(
            offline_source, "_run_command",
            side_effect=ImageSourceError("simulated pull-local failure"),
        ):
            with self.assertRaisesRegex(OfflineSourceError, "simulated pull-local"):
                self._invoke(work_dir=failed_work)
        self.assertFalse((failed_work / "import.log").exists())
        self.assert_commit_absent()
        result = self._invoke(work_dir=self._work("actual-retry"))
        self.assertEqual(result["phase"], "imported")
        self.assertTrue(Path(result["importLog"]).is_file())
        self.assert_commit_present()

    def test_corrupted_donor_file_object_is_rejected(self) -> None:
        listing = store_fix.run(
            "ostree", "ls", f"--repo={self.donor}", self.commit,
            "--checksum", "/usr/share/revision",
        )
        checksum = listing.split()[4]
        victim = self.donor / "objects" / checksum[:2] / f"{checksum[2:]}.file"
        self.assertTrue(victim.is_file())
        self.assertFalse(
            (self.destination / "objects" / checksum[:2] / f"{checksum[2:]}.file").exists(),
            "victim object already present in destination; pull-local would skip it",
        )
        original = victim.stat()
        xattrs = {
            name: os.getxattr(victim, name, follow_symlinks=False)
            for name in os.listxattr(victim, follow_symlinks=False)
        }
        replacement = victim.with_name(victim.name + ".replacement")
        replacement.write_bytes(b"corrupted target-specific donor object\n")
        os.chmod(replacement, stat.S_IMODE(original.st_mode))
        os.chown(replacement, original.st_uid, original.st_gid)
        for name, value in xattrs.items():
            os.setxattr(replacement, name, value, follow_symlinks=False)
        os.replace(replacement, victim)
        bindings = self.fixture._bindings()
        with self.assertRaises(OfflineSourceError):
            self._invoke()
        self.assertEqual(self.fixture._bindings(), bindings)
        self.fixture._assert_user_data_unchanged()
        self.assertEqual(
            store_fix.run("ostree", "refs", f"--repo={self.destination}"),
            self.refs_before,
        )


if __name__ == "__main__":
    unittest.main()
