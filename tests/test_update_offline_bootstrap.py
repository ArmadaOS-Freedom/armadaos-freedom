"""Regression tests for the offline updater bundle and bootstrap helper."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "system_files/usr/libexec/armada/armada-offline-bootstrap"
BUILDER_PATH = ROOT / "build_files/build-offline-updater-bundle.py"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bootstrap = _load("offline_bootstrap_test", BOOTSTRAP_PATH)
builder = _load("offline_bundle_builder_test", BUILDER_PATH)


class BundleOutputTests(unittest.TestCase):
    def test_failed_replace_removes_only_this_build_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "offline-updater.tar"
            stale = output.with_name("." + output.name + ".tmp")
            stale.write_bytes(b"owned by another build")
            with mock.patch.object(
                    builder.os, "replace", side_effect=OSError("publish failed")):
                with self.assertRaises(OSError):
                    builder._write_output(output, b"payload", gzipped=False)
            self.assertEqual(stale.read_bytes(), b"owned by another build")
            generated = [
                path for path in output.parent.iterdir()
                if path.name.startswith("." + output.name + ".")
                and path != stale
            ]
            self.assertEqual(generated, [])

    def test_runtime_version_reads_package_constant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / builder.UPDATES_DIR / "offline_session.py"
            package = root / builder.UPDATES_DIR / "__init__.py"
            session.parent.mkdir(parents=True)
            session.write_text("_UPDATER_VERSION = UPDATER_VERSION\n")
            package.write_text("UPDATER_VERSION = '0.8.2'\n")
            self.assertEqual(builder._runtime_version(root), "0.8.2")


@unittest.skipUnless(os.name == "posix", "bootstrap installs a Linux extension")
class BootstrapTests(unittest.TestCase):
    VERSION = "0.8.2"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, relative, data=b"x", mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def _bundle(self, version=None, imported_version=False):
        version = version or self.VERSION
        entries = {}
        for relative in bootstrap.REQUIRED_FILES:
            if relative == bootstrap.ENTRY_PATH:
                data = b"#!/bin/sh\nexit 0\n"
                mode = 0o755
            elif relative == bootstrap.UPDATES_DIR + "/offline_session.py":
                if imported_version:
                    data = b"_UPDATER_VERSION = UPDATER_VERSION\n"
                else:
                    data = ("_UPDATER_VERSION = %r\n" % version).encode()
                mode = 0o644
            else:
                data = ("payload:" + relative + "\n").encode()
                mode = 0o644
            entries[relative] = (data, mode)
        if imported_version:
            entries[bootstrap.UPDATES_DIR + "/__init__.py"] = (
                ("UPDATER_VERSION = %r\n" % version).encode(), 0o644)
        files = [
            {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": "%04o" % mode,
            }
            for relative, (data, mode) in sorted(entries.items())
        ]
        manifest = {
            "schemaVersion": bootstrap.SCHEMA_VERSION,
            "bundle": bootstrap.BUNDLE_NAME,
            "version": version,
            "format": "tar",
            "entry": bootstrap.ENTRY_PATH,
            "files": files,
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            raw_manifest = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw_manifest)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(raw_manifest))
            for relative, (data, mode) in sorted(entries.items()):
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                info.mode = mode
                archive.addfile(info, io.BytesIO(data))
        raw = buffer.getvalue()
        bundle = self.root / "offline-updater.tar"
        bundle.write_bytes(raw)
        return bundle, hashlib.sha256(raw).hexdigest()

    def _prepare_root(self):
        self._write("etc/os-release", b"ID=fedora\nVERSION_ID=44\n")
        sysext = self._write("usr/bin/systemd-sysext", b"#!/bin/sh\n", 0o755)
        return sysext

    def _runner(self, *returncodes):
        results = iter(returncodes)

        def run(*_args, **_kwargs):
            return SimpleNamespace(returncode=next(results))

        return run

    def test_os_release_prefers_etc(self):
        self._write("etc/os-release", b"ID=etc-host\nVERSION_ID=2\n")
        self._write("usr/lib/os-release", b"ID=usr-host\nVERSION_ID=1\n")
        values = bootstrap._read_os_release(self.root)
        self.assertEqual(values["ID"], "etc-host")
        self.assertEqual(values["_path"], "etc/os-release")

    def test_manifest_entry_must_be_executable(self):
        members = {relative: 0 for relative in bootstrap.REQUIRED_FILES}
        files = [
            {
                "path": relative,
                "bytes": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "mode": "0755" if relative == bootstrap.ENTRY_PATH else "0644",
            }
            for relative in sorted(bootstrap.REQUIRED_FILES)
        ]
        manifest = {"files": files}
        bootstrap._validate_manifest(manifest, members)
        for entry in files:
            if entry["path"] == bootstrap.ENTRY_PATH:
                entry["mode"] = "0644"
        with self.assertRaisesRegex(bootstrap.BootstrapError, "executable"):
            bootstrap._validate_manifest(manifest, members)

    def test_archive_scan_enforces_cumulative_payload_limit(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for relative in (
                    bootstrap.UPDATES_DIR + "/offline_rpc.py",
                    bootstrap.UPDATES_DIR + "/offline_jobs.py"):
                data = b"data"
                info = tarfile.TarInfo(relative)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        buffer.seek(0)
        with mock.patch.object(bootstrap, "MAX_BUNDLE_BYTES", 6):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "too large"):
                bootstrap._scan_archive(buffer)

    def test_host_trust_symlink_is_followed_consistently(self):
        target = self._write("keys/host.pub", b"same-key")
        host = self.root / bootstrap.HOST_TRUST_KEY_PATH
        host.parent.mkdir(parents=True, exist_ok=True)
        host.symlink_to(target)
        bundled = self._write(bootstrap.TRUST_KEY_PATH, b"same-key")
        self.assertEqual(
            bootstrap._trust_anchor(self.root),
            (bootstrap.HOST_TRUST_KEY_PATH, "etc"))
        self.assertFalse(bootstrap._trust_files_differ(self.root, self.root))
        specs = {
            bootstrap.TRUST_KEY_PATH: {
                "bytes": bundled.stat().st_size,
                "sha256": hashlib.sha256(bundled.read_bytes()).hexdigest(),
                "mode": "0644",
                "path": bootstrap.TRUST_KEY_PATH,
            }
        }
        self.assertFalse(bootstrap._host_key_mismatch(self.root, specs))
        target.write_bytes(b"different-key")
        self.assertTrue(bootstrap._trust_files_differ(self.root, self.root))
        self.assertTrue(bootstrap._host_key_mismatch(self.root, specs))

    def test_higher_priority_extension_is_effective(self):
        run_extension = (
            self.root / bootstrap.RUN_EXTENSIONS / bootstrap.EXTENSION_NAME)
        etc_extension = (
            self.root / bootstrap.ETC_EXTENSIONS / bootstrap.EXTENSION_NAME)
        for directory in (run_extension, etc_extension):
            release = directory / bootstrap.EXTENSION_RELEASE
            release.parent.mkdir(parents=True, exist_ok=True)
            release.write_text("ID=fedora\n")
        self.assertEqual(
            bootstrap._effective_extension_dir(self.root), etc_extension)
        self.assertEqual(bootstrap._runtime_root(self.root), etc_extension)

    def test_install_rejects_shadowing_extension_path(self):
        self._prepare_root()
        bundle, digest = self._bundle()
        shadow = (
            self.root / bootstrap.ETC_EXTENSIONS / bootstrap.EXTENSION_NAME)
        release = shadow / bootstrap.EXTENSION_RELEASE
        release.parent.mkdir(parents=True, exist_ok=True)
        release.write_text("ID=fedora\n")
        with mock.patch.object(bootstrap, "_is_root", return_value=True):
            with self.assertRaisesRegex(
                    bootstrap.BootstrapError, "shadows"):
                bootstrap.install(
                    bundle, digest, root=self.root,
                    runner=self._runner(0))

    def test_install_verifies_effective_host_tree(self):
        self._prepare_root()
        bundle, digest = self._bundle()
        with mock.patch.object(bootstrap, "_is_root", return_value=True):
            result = bootstrap.install(
                bundle, digest, root=self.root,
                runner=self._runner(0))
        self.assertEqual(result["state"], "installed")
        final = (
            self.root / bootstrap.RUN_EXTENSIONS / bootstrap.EXTENSION_NAME)
        self.assertEqual(bootstrap._effective_extension_dir(self.root), final)
        self.assertEqual(
            bootstrap._read_updater_version(final), self.VERSION)

    def test_install_accepts_imported_version_layout(self):
        self._prepare_root()
        bundle, digest = self._bundle(imported_version=True)
        with mock.patch.object(bootstrap, "_is_root", return_value=True):
            result = bootstrap.install(
                bundle, digest, root=self.root,
                runner=self._runner(0))
        self.assertEqual(result["state"], "installed")
        final = (
            self.root / bootstrap.RUN_EXTENSIONS / bootstrap.EXTENSION_NAME)
        self.assertEqual(
            bootstrap._read_updater_version(final), self.VERSION)
        self.assertTrue(
            (final / bootstrap.UPDATES_DIR / "__init__.py").is_file())

    def test_rollback_refresh_success_keeps_original_activation_error(self):
        self._prepare_root()
        final = (
            self.root / bootstrap.RUN_EXTENSIONS / bootstrap.EXTENSION_NAME)
        self._write(
            str(final.relative_to(self.root)) + "/" +
            bootstrap.UPDATES_DIR + "/offline_session.py",
            b"_UPDATER_VERSION = '0.8.1'\n")
        self._write(
            bootstrap.EXTENSION_RELEASE, b"ID=fedora\n")
        bundle, digest = self._bundle()
        with mock.patch.object(bootstrap, "_is_root", return_value=True):
            with self.assertRaises(bootstrap.BootstrapError) as raised:
                bootstrap.install(
                    bundle, digest, root=self.root,
                    runner=self._runner(1, 0))
        self.assertEqual(raised.exception.code, "sysext_failed")
        self.assertEqual(
            bootstrap._read_updater_version(final), "0.8.1")

    def test_rollback_refresh_failure_reports_recovery_error(self):
        self._prepare_root()
        final = (
            self.root / bootstrap.RUN_EXTENSIONS / bootstrap.EXTENSION_NAME)
        self._write(
            str(final.relative_to(self.root)) + "/" +
            bootstrap.UPDATES_DIR + "/offline_session.py",
            b"_UPDATER_VERSION = '0.8.1'\n")
        self._write(
            bootstrap.EXTENSION_RELEASE, b"ID=fedora\n")
        bundle, digest = self._bundle()
        with mock.patch.object(bootstrap, "_is_root", return_value=True):
            with self.assertRaises(bootstrap.BootstrapError) as raised:
                bootstrap.install(
                    bundle, digest, root=self.root,
                    runner=self._runner(1, 1))
        self.assertEqual(raised.exception.code, "recovery_failed")
        self.assertIn("sysext_failed", str(raised.exception))
        self.assertIn("refresh returned failure", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
