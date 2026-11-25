# SPDX-License-Identifier: GPL-3.0-only
"""Check the producer/consumer handoff without a running systemd-sysext."""
import hashlib
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_source(name, path):
    loader = SourceFileLoader(name, str(ROOT / path))
    spec = spec_from_loader(name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


producer = load_source('bundle_builder', 'build_files/build-offline-updater-bundle.py')
bootstrap = load_source('offline_bootstrap', 'system_files/usr/libexec/armada/armada-offline-bootstrap')


class OfflineBootstrapTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / 'root'
        self.bundle = Path(scratch.name) / 'updater.tar'
        for relative, data in (
            ('usr/lib/os-release', b'ID=armadaos\nVERSION_ID=1\n'),
            ('usr/bin/systemd-sysext', b'fake\n'),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755)
        root = mock.patch.object(bootstrap, '_is_root', return_value=True)
        root.start()
        self.addCleanup(root.stop)

    def entries(self, version='1.0.0'):
        sources = {
            name: (name.encode(), '0755' if name in bootstrap.REQUIRED_EXECUTABLES else '0644')
            for name in sorted(bootstrap.REQUIRED_FILES)
        }
        sources[bootstrap.UPDATER_INIT_PATH] = (f'UPDATER_VERSION = "{version}"\n'.encode(), '0644')
        return sources

    def bundle_bytes(self, entries, version='1.0.0'):
        manifest = producer._manifest_bytes(entries, 'tar', (), version)
        return producer._archive_bytes(entries, manifest)

    def publish(self, entries, version='1.0.0'):
        raw = self.bundle_bytes(entries, version)
        self.bundle.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def refresh(self, args, **kwargs):
        final = bootstrap._extension_dir(self.root)
        shutil.copytree(final / 'usr', self.root / 'usr', dirs_exist_ok=True)
        return subprocess.CompletedProcess(args, 0)

    def test_refuses_nonexecutable_entry_from_other_producer(self):
        for path in bootstrap.REQUIRED_EXECUTABLES:
            with self.subTest(path=path):
                entries = self.entries()
                entries[path] = (b'executable', '0644')
                digest = self.publish(entries)
                with self.assertRaisesRegex(bootstrap.BootstrapError, 'must have mode 0755'):
                    bootstrap.install(self.bundle, digest, root=self.root, runner=self.refresh)

    def test_reuse_matches_active_files_and_repair_refreshes_when_they_differ(self):
        digest = self.publish(self.entries())
        runner = mock.Mock(side_effect=self.refresh)
        installed = bootstrap.install(self.bundle, digest, root=self.root, runner=runner)
        self.assertEqual(installed['state'], 'installed')
        reused = bootstrap.install(self.bundle, digest, root=self.root, runner=runner)
        self.assertEqual(reused['state'], 'reused')
        runner.assert_called_once()

        entry = self.root / bootstrap.ENTRY_PATH
        entry.write_bytes(b'stale active runtime')
        repaired = bootstrap.install(self.bundle, digest, root=self.root, runner=runner)
        self.assertEqual(repaired['state'], 'installed')
        self.assertEqual(entry.read_bytes(), self.entries()[bootstrap.ENTRY_PATH][0])
        self.assertEqual(runner.call_count, 2)

    def test_active_verification_failure_restores_previous_runtime(self):
        old_digest = self.publish(self.entries())
        bootstrap.install(self.bundle, old_digest, root=self.root, runner=self.refresh)
        digest = self.publish(self.entries('1.1.0'), '1.1.0')
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
        ])
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'merged updater version'):
            bootstrap.install(self.bundle, digest, root=self.root, runner=runner)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(bootstrap._read_updater_version(bootstrap._extension_dir(self.root)), '1.0.0')

    def test_reports_both_install_and_rollback_refresh_failure(self):
        digest = self.publish(self.entries())
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1))
        with self.assertRaisesRegex(bootstrap.BootstrapError, 'refresh failed.*rollback refresh failed') as error:
            bootstrap.install(self.bundle, digest, root=self.root, runner=runner)
        self.assertEqual(error.exception.code, 'rollback_failed')
        self.assertEqual(runner.call_count, 2)

    def test_producer_hashes_the_bytes_it_archives(self):
        path = self.root / 'usr/bin/systemd-sysext'
        captured = producer._read_entries({'tool': path})
        path.write_bytes(b'replaced after capture')
        manifest = producer._manifest_bytes(captured, 'tar', (), '1.0.0')
        self.assertEqual(json.loads(manifest)['files'][0]['sha256'], hashlib.sha256(b'fake\n').hexdigest())
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(producer._archive_bytes(captured, manifest))) as archive:
            self.assertEqual(archive.extractfile('tool').read(), b'fake\n')

    def test_producer_rejects_oversized_output_without_replacing_existing_archive(self):
        self.bundle.write_bytes(b'previous')
        with mock.patch.object(producer, '_MAX_BUNDLE_BYTES', 10):
            with self.assertRaisesRegex(producer.BundleError, 'archive is too large'):
                producer._write_output(self.bundle, b'uncompressible', gzipped=True)
        self.assertEqual(self.bundle.read_bytes(), b'previous')

    def test_producer_rejects_version_declarations_the_bootstrap_cannot_read(self):
        with self.assertRaisesRegex(producer.BundleError, 'double-quoted'):
            producer._runtime_version(b"UPDATER_VERSION = '1.0.0'\n")


if __name__ == '__main__':
    unittest.main()
