#!/usr/bin/python3
"""Integration tests for updates/image_source.py.

Real OSTree sysroot (from the deployment-store fixture), real ECDSA package
and channel signatures; only the HTTP opener is faked and image_source._pull
is patched so no network bootc import is performed. Pull-command tests mock
subprocess.Popen on the image_source module (shared subprocess object, so all
signing happens before any patching).
"""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, OSTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

from updates import image_source  # noqa: E402
from updates.catalog import CatalogError  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store_fix = _load("imgsrc_store_fixture", ROOT / "tests/update-deployment-store-test.py")
release_fix = _load("imgsrc_release_fixture", ROOT / "tests/test_update_channel_release.py")
channel_fix = _load("imgsrc_channel_fixture", ROOT / "tests/test_update_channel_source.py")
CH_URL = channel_fix.CH_URL
CH_SIG_URL = channel_fix.CH_SIG_URL
PKG_URL = channel_fix.PKG_URL
PKG_SIG_URL = channel_fix.PKG_SIG_URL
PUBLISHED_AT = channel_fix.PUBLISHED_AT


def _install_opener(fake):
    """Patch the channel_source opener injection point."""
    import updates.channel_source as src
    return mock.patch.object(src, "build_opener", lambda *a, **k: fake)


class ImageSourceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store_fix.DeploymentStoreIntegrationTests.setUpClass()
        cls._keydir = tempfile.TemporaryDirectory(prefix="imgsrc-keys-")
        cls.priv_key, cls.pub_key = release_fix._generate_keypair(
            Path(cls._keydir.name), "trusted"
        )

    @classmethod
    def tearDownClass(cls):
        cls._keydir.cleanup()
        store_fix.DeploymentStoreIntegrationTests.tearDownClass()

    def setUp(self):
        self.store = store_fix.DeploymentStoreIntegrationTests(
            "test_two_updates_merges_retains_and_reuses"
        )
        self.store.setUp()
        self.addCleanup(self.store.doCleanups)
        self.tmp = tempfile.TemporaryDirectory(prefix="imgsrc-work-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.model = store_fix.DeploymentStoreIntegrationTests.model

    # -- helpers ----------------------------------------------------------
    def _fixture(self):
        return self.store

    def _pkg(self, commit, index=1, **overrides):
        pkg = release_fix._upgrade_package_dict()
        pkg.update({
            "ociDigest": store_fix.DIGESTS[index],
            "sourceRevision": store_fix.REVISIONS[index],
            "version": store_fix.VERSIONS[index],
            "ostreeCommit": commit,
        })
        pkg.update(overrides)
        return release_fix.validate_package(pkg)

    def _signed(self, pkg):
        raw = json.dumps(pkg, sort_keys=True, separators=(",", ":")).encode()
        return raw, release_fix._sign_bytes(raw, self.priv_key)

    def _channel(self, pkg, raw, sig, **over):
        kwargs = {
            "channel": "stable", "descriptor_url": PKG_URL,
            "signature_url": PKG_SIG_URL, "published_at": PUBLISHED_AT,
            "expected_oci_digest": pkg["ociDigest"], "trusted_key": self.pub_key,
        }
        kwargs.update(over)
        return release_fix.make_channel(raw, sig, **kwargs)

    def _routes(self, raw, sig, channel, pkg_sig=None):
        ch_raw = json.dumps(channel).encode()
        return {
            CH_URL: ch_raw,
            CH_SIG_URL: release_fix._sign_bytes(ch_raw, self.priv_key),
            PKG_URL: raw,
            PKG_SIG_URL: pkg_sig if pkg_sig is not None else sig,
        }

    def _fake_pull(self, commit_hex=None, log=b"pulled ok\n", record=None, source=None):
        def pull(repo, origin, work):
            if record is not None:
                record.append((repo, origin, work))
            source_repo = source or self.store.root / "ostree" / "repo"
            store_fix.run(
                "ostree", "pull-local", f"--repo={repo}", "--untrusted",
                "--disable-verify-bindings", str(source_repo), commit_hex,
            )
            (work / "commit").write_text(commit_hex + "\n", encoding="ascii")
            (work / "pull.log").write_bytes(log)
            return work / "pull.log"
        return pull

    def _fetch(self, routes, work, *, model=None, updater="1.0.0", channel="stable"):
        fake = channel_fix.FakeOpener(routes)
        with _install_opener(fake):
            return image_source.fetch_online_image(
                channel_url=CH_URL, signature_url=CH_SIG_URL,
                expected_channel=channel, model=model or self.model,
                updater_version=updater, sysroot_path=self.store.root,
                work_dir=work, trusted_key=self.pub_key,
            )

    # -- 1: successful import ---------------------------------------------
    def test_successful_import_downloads_and_changes_nothing(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        work = self.base / "work"
        etc_before = store_fix.tree_digest(case.current_path / "etc")
        live_repo = case.root / "ostree" / "repo"
        refs_before = store_fix.run("ostree", "refs", f"--repo={live_repo}")
        calls = []
        with mock.patch.object(image_source, "_pull", side_effect=self._fake_pull(commit, record=calls)):
            result = self._fetch(self._routes(raw, sig, channel), work)
        expected_origin = (
            "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
            f"armadaos-freedom:stable@{pkg['ociDigest']}"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], work / "staging-repo")
        self.assertEqual(calls[0][1], expected_origin)
        self.assertEqual(calls[0][2], work)
        self.assertEqual(result["commit"], commit)
        self.assertEqual(result["package"], pkg)
        self.assertEqual(result["originRef"], expected_origin)
        self.assertEqual(result["repo"], str(case.root / "ostree" / "repo"))
        self.assertEqual(Path(result["pullLog"]), work / "pull.log")
        self.assertEqual(result["phase"], "downloaded")
        self.assertFalse((work / "staging-repo").exists())
        self.assertTrue((work / "import.log").is_file())
        self.assertEqual(store_fix.run("ostree", "refs", f"--repo={live_repo}"), refs_before)
        self.assertEqual(case._bindings(), [case.initial_binding])
        self.assertEqual(store_fix.tree_digest(case.current_path / "etc"), etc_before)
        case._assert_user_data_unchanged()
        self.assertEqual(stat.S_IMODE(os.stat(work).st_mode), 0o700)

    def test_interrupted_stage_retry_imports_previously_absent_commit(self):
        case = self._fixture()
        commit = case._commit_tree("2", 1)
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        routes = self._routes(raw, sig, channel)
        live_repo = case.root / "ostree" / "repo"
        donor = self.base / "donor-repo"
        donor.mkdir()
        store_fix.run("ostree", "init", f"--repo={donor}", "--mode=bare")
        store_fix.run(
            "ostree", "pull-local", f"--repo={donor}", "--untrusted",
            "--disable-verify-bindings", str(live_repo), commit,
        )
        store_fix.run("ostree", "refs", f"--repo={live_repo}", "--delete", "fixture-2-arm64")
        store_fix.run("ostree", "prune", f"--repo={live_repo}", "--refs-only")
        absent = subprocess.run(
            ["ostree", "show", f"--repo={live_repo}", commit],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        self.assertNotEqual(absent.returncode, 0)
        refs_before = store_fix.run("ostree", "refs", f"--repo={live_repo}")
        etc_before = store_fix.tree_digest(case.current_path / "etc")

        failed_work = self.base / "work-interrupted"

        def interrupted(repo, origin, work):
            store_fix.run(
                "ostree", "pull-local", f"--repo={repo}", "--untrusted",
                "--disable-verify-bindings", str(donor), commit,
            )
            raise image_source.ImageSourceError("interrupted")

        with mock.patch.object(image_source, "_pull", side_effect=interrupted):
            with self.assertRaisesRegex(image_source.ImageSourceError, "interrupted"):
                self._fetch(routes, failed_work)
        failed_stage = failed_work / "staging-repo"
        self.assertTrue(failed_stage.is_dir())
        self.assertFalse((failed_work / "commit").exists())
        self.assertFalse((failed_work / "import.log").exists())
        still_absent = subprocess.run(
            ["ostree", "show", f"--repo={live_repo}", commit],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        )
        self.assertNotEqual(still_absent.returncode, 0)
        self.assertEqual(store_fix.run("ostree", "refs", f"--repo={live_repo}"), refs_before)
        self.assertEqual(case._bindings(), [case.initial_binding])
        self.assertEqual(store_fix.tree_digest(case.current_path / "etc"), etc_before)
        case._assert_user_data_unchanged()

        retry_work = self.base / "work-retry"
        with mock.patch.object(
            image_source, "_pull", side_effect=self._fake_pull(commit, source=donor)
        ):
            result = self._fetch(routes, retry_work)
        self.assertEqual(result["commit"], commit)
        present = store_fix.run("ostree", "show", f"--repo={live_repo}", commit)
        self.assertIn(commit, present)
        self.assertTrue((retry_work / "import.log").is_file())
        self.assertFalse((retry_work / "staging-repo").exists())
        self.assertTrue(failed_stage.is_dir())
        self.assertFalse((failed_work / "import.log").exists())
        self.assertEqual(store_fix.run("ostree", "refs", f"--repo={live_repo}"), refs_before)
        self.assertEqual(case._bindings(), [case.initial_binding])
        self.assertEqual(store_fix.tree_digest(case.current_path / "etc"), etc_before)
        case._assert_user_data_unchanged()

    # -- 2: rejections before any pull -------------------------------------
    def test_authentication_failures_never_pull_or_create_work(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        ch_raw = json.dumps(channel).encode()
        withdrawn = self._channel(pkg, raw, sig, withdrawn=True)
        old_pkg = self._pkg(commit, minimumUpdaterVersion="99.0.0")
        old_raw, old_sig = self._signed(old_pkg)
        old_channel = self._channel(old_pkg, old_raw, old_sig)
        cases = {
            "channel-signature": {
                CH_URL: ch_raw,
                CH_SIG_URL: b"garbage\n",
                PKG_URL: raw, PKG_SIG_URL: sig,
            },
            "channel-tampered": {
                CH_URL: ch_raw.replace(b'"stable"', b'"beta"'),
                CH_SIG_URL: release_fix._sign_bytes(ch_raw, self.priv_key),
                PKG_URL: raw, PKG_SIG_URL: sig,
            },
            "package-signature": self._routes(raw, sig, channel, pkg_sig=b"bad\n"),
            "withdrawn": self._routes(raw, sig, withdrawn),
            "model-unsupported": self._routes(raw, sig, channel),
            "minimum-version": self._routes(old_raw, old_sig, old_channel),
        }
        for name, routes in cases.items():
            with self.subTest(name=name):
                work = self.base / f"work-{name}"
                calls = []
                with mock.patch.object(image_source, "_pull", side_effect=self._fake_pull(record=calls)) as pull:
                    kwargs = {}
                    if name == "model-unsupported":
                        kwargs["model"] = "UNKNOWN-MODEL"
                    if name == "minimum-version":
                        kwargs["updater"] = "1.0.0"
                    with self.assertRaises((image_source.ImageSourceError, CatalogError)):
                        self._fetch(routes, work, **kwargs)
                self.assertEqual(pull.call_count, 0)
                self.assertEqual(len(calls), 0)
                self.assertFalse(work.exists())

    # -- 3: pull stdout success is not enough ------------------------------
    def test_untrustworthy_pull_output_rejected(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        routes = self._routes(raw, sig, channel)
        oversized = "a" * 300
        variants = {
            "missing-digest": None,
            "wrong-commit": case.commits[0],
            "oversized": oversized,
            "symlink-commit": "symlink",
        }
        for name, value in variants.items():
            with self.subTest(name=name):
                work = self.base / f"work-{name}"
                def pull(repo, origin, w, value=value):
                    (w / "pull.log").write_bytes(b"success\n")
                    if value == "symlink":
                        os.symlink(commit, w / "commit")
                    elif value is not None:
                        (w / "commit").write_text(value + "\n", encoding="ascii")
                    return w / "pull.log"
                with mock.patch.object(image_source, "_pull", side_effect=pull) as fake_pull:
                    with self.assertRaises(image_source.ImageSourceError):
                        self._fetch(routes, work)
                self.assertEqual(fake_pull.call_count, 1)
                self.assertEqual(case._bindings(), [case.initial_binding])
                case._assert_user_data_unchanged()

    # -- 4: real commit metadata must match signed package ------------------
    def test_signed_metadata_mismatch_against_real_commit_rejected(self):
        case = self._fixture()
        commit = case.commits[1]
        bad_arch = case._commit_tree("imgsrc-arch", 1, architecture="x86_64")
        variants = {
            "wrong-digest": (
                self._pkg(commit, index=1, ociDigest=store_fix.DIGESTS[2]),
                "manifest-digest",
            ),
            "wrong-revision": (
                self._pkg(commit, index=1, sourceRevision="f" * 40),
                "revision label",
            ),
            "wrong-version": (
                self._pkg(commit, index=1, version="9.9.9"),
                "version label",
            ),
            "wrong-architecture": (
                self._pkg(bad_arch, index=1),
                "architecture/os",
            ),
        }
        for name, (pkg, error) in variants.items():
            with self.subTest(name=name):
                raw, sig = self._signed(pkg)
                channel = self._channel(pkg, raw, sig)
                routes = self._routes(raw, sig, channel)
                work = self.base / f"work-{name}"
                pulled = pkg["ostreeCommit"]
                with mock.patch.object(
                    image_source, "_pull", side_effect=self._fake_pull(pulled)
                ) as fake_pull:
                    with self.assertRaisesRegex(image_source.ImageSourceError, error):
                        self._fetch(routes, work)
                self.assertEqual(fake_pull.call_count, 1)
                self.assertFalse((work / "import.log").exists())
                self.assertEqual(case._bindings(), [case.initial_binding])
                case._assert_user_data_unchanged()

    # -- 5: locking and work-dir hygiene -----------------------------------
    def test_sysroot_lock_blocks_pull_and_releases(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        routes = self._routes(raw, sig, channel)
        contender = OSTree.Sysroot.new(Gio.File.new_for_path(str(case.root)))
        ok, locked = contender.try_lock()
        self.assertTrue(ok and locked)
        try:
            work = self.base / "work-locked"
            with mock.patch.object(image_source, "_pull", side_effect=self._fake_pull(commit)) as pull:
                with self.assertRaisesRegex(image_source.ImageSourceError, "busy"):
                    self._fetch(routes, work)
                self.assertEqual(pull.call_count, 0)
        finally:
            contender.unlock()
        work = self.base / "work-after"
        with mock.patch.object(image_source, "_pull", side_effect=self._fake_pull(commit)):
            result = self._fetch(routes, work)
        self.assertEqual(result["commit"], commit)

    def test_failing_pull_releases_lock(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        routes = self._routes(raw, sig, channel)

        def failing(repo, origin, work):
            raise image_source.ImageSourceError("pull exploded")

        work = self.base / "work-fail"
        with mock.patch.object(image_source, "_pull", side_effect=failing):
            with self.assertRaisesRegex(image_source.ImageSourceError, "pull exploded"):
                self._fetch(routes, work)
        sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(case.root)))
        ok, locked = sysroot.try_lock()
        self.assertTrue(ok and locked)
        sysroot.unlock()

    def test_work_dir_requirements(self):
        case = self._fixture()
        commit = case.commits[1]
        pkg = self._pkg(commit)
        raw, sig = self._signed(pkg)
        channel = self._channel(pkg, raw, sig)
        routes = self._routes(raw, sig, channel)
        existing = self.base / "work-existing"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep\n")
        link_target = self.base / "work-target"
        link_target.mkdir()
        symlink = self.base / "work-link"
        os.symlink(link_target, symlink)
        inside = case.root / "ostree" / "repo" / "work-inside"
        inside.parent.mkdir(parents=True, exist_ok=True)
        for name, work in (
            ("existing", existing), ("symlink", symlink),
            ("inside-protected", inside), ("relative", Path("relative-work")),
        ):
            with self.subTest(name=name):
                with mock.patch.object(image_source, "_pull") as pull:
                    with self.assertRaises(image_source.ImageSourceError):
                        self._fetch(routes, work)
                    self.assertEqual(pull.call_count, 0)
        self.assertEqual((existing / "keep.txt").read_text(), "keep\n")
        self.assertTrue(symlink.is_symlink())


class PullCommandTests(unittest.TestCase):
    ORIGIN = ("ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
              "armadaos-freedom:stable@sha256:" + "ab" * 32)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="imgsrc-pull-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.work = self.base / "work"
        self.work.mkdir(mode=0o700)
        self.repo = self.base / "sysroot" / "ostree" / "repo"

    def _pull(self, process=None, popen_error=None):
        with mock.patch.object(image_source.subprocess, "Popen",
                               side_effect=popen_error, return_value=process) as popen:
            result = image_source._pull(self.repo, self.ORIGIN, self.work)
        return result, popen

    def _assert_spawn(self, popen, log):
        self.assertEqual(popen.call_count, 1)
        args, kwargs = popen.call_args
        argv = args[0] if args else kwargs["args"]
        self.assertEqual(argv, [
            "/usr/bin/bootc", "internals", "ostree-ext", "container", "image",
            "pull", "--auth-anonymous", "--ostree-digestfile",
            str(self.work / "commit"), str(self.repo), self.ORIGIN,
        ])
        self.assertEqual(kwargs["env"], image_source._ENV)
        self.assertIs(kwargs["shell"], False)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_success_spawn_contract(self):
        proc = mock.MagicMock()
        proc.wait.return_value = 0
        log, popen = self._pull(process=proc)
        self._assert_spawn(popen, log)
        proc.wait.assert_called_once_with(timeout=image_source._PULL_TIMEOUT)

    def test_nonzero_status_is_error_without_retry(self):
        proc = mock.MagicMock()
        proc.wait.return_value = 3
        with mock.patch.object(image_source.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(image_source.os, "killpg") as killpg:
            with self.assertRaisesRegex(image_source.ImageSourceError, "status 3"):
                image_source._pull(self.repo, self.ORIGIN, self.work)
        log = self.work / "pull.log"
        self.assertTrue(log.is_file())
        self._assert_spawn(popen, log)
        killpg.assert_not_called()

    def test_timeout_kills_group_and_reaps(self):
        for poll_val in (None, 0):
            with self.subTest(poll=poll_val):
                work = self.base / f"work-timeout-{poll_val}"
                work.mkdir(mode=0o700)
                proc = mock.MagicMock()
                proc.pid = 4242
                proc.poll.return_value = poll_val
                proc.wait.side_effect = [subprocess.TimeoutExpired("bootc", 1), 9]
                with mock.patch.object(image_source.subprocess, "Popen", return_value=proc) as popen, \
                     mock.patch.object(image_source.os, "killpg") as killpg:
                    with self.assertRaisesRegex(image_source.ImageSourceError, "timed out"):
                        image_source._pull(self.repo, self.ORIGIN, work)
                self.assertEqual(popen.call_count, 1)
                killpg.assert_called_once_with(4242, signal.SIGKILL)
                self.assertEqual(proc.wait.call_count, 2)
                self.assertEqual(
                    proc.wait.call_args_list[0],
                    mock.call(timeout=image_source._PULL_TIMEOUT),
                )
                self.assertEqual(proc.wait.call_args_list[1], mock.call())

    def test_normal_exit_does_not_kill(self):
        proc = mock.MagicMock()
        proc.pid = 1
        proc.wait.return_value = 0
        with mock.patch.object(image_source.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(image_source.os, "killpg") as killpg:
            image_source._pull(self.repo, self.ORIGIN, self.work)
        self.assertEqual(popen.call_count, 1)
        killpg.assert_not_called()
        proc.poll.assert_not_called()
        proc.wait.assert_called_once_with(timeout=image_source._PULL_TIMEOUT)

    def test_missing_binary_is_error_without_retry(self):
        with mock.patch.object(image_source.subprocess, "Popen", side_effect=OSError(2)) as popen:
            with self.assertRaisesRegex(image_source.ImageSourceError, "cannot start"):
                image_source._pull(self.repo, self.ORIGIN, self.work)
        self.assertEqual(popen.call_count, 1)

    def test_read_commit_validation(self):
        good = "0f" * 32
        (self.work / "commit").write_text(good + "\n", encoding="ascii")
        self.assertEqual(image_source._read_commit(self.work), good)
        (self.work / "commit").unlink()
        (self.work / "commit").write_text("z" * 64 + "\n", encoding="ascii")
        with self.assertRaises(image_source.ImageSourceError):
            image_source._read_commit(self.work)
        (self.work / "commit").unlink()
        os.symlink(good, self.work / "commit")
        with self.assertRaises(image_source.ImageSourceError):
            image_source._read_commit(self.work)
        (self.work / "commit").unlink()
        (self.work / "commit").write_text("a" * 300, encoding="ascii")
        with self.assertRaises(image_source.ImageSourceError):
            image_source._read_commit(self.work)


if __name__ == "__main__":
    unittest.main()
