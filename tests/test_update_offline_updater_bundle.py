# SPDX-License-Identifier: GPL-3.0-only
"""Offline updater bundle builder: exact layout, determinism, strict inputs."""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / 'build_files' / 'build-offline-updater-bundle.py'
SYSTEM = ROOT / 'system_files'
UPDATES = 'usr/lib/armada/updates'
TRUST_KEY_SOURCE = 'etc/pki/containers/armadaos-freedom.pub'
TRUST_KEY_DESTINATION = 'usr/share/armada/updates/armadaos-freedom.pub'


def load_module(name, path):
    """Load by explicit source loader; extensionless helpers have no suffix."""
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def make_runtime_root(base, version='0.8.0'):
    """Assemble the subset of a rootfs the builder is allowed to read."""
    root = base / 'rootfs'
    (root / 'usr/lib/armada').mkdir(parents=True)
    shutil.copytree(SYSTEM / UPDATES, root / UPDATES)
    shutil.copyfile(SYSTEM / 'usr/lib/armada/armada_os.py',
                    root / 'usr/lib/armada/armada_os.py')
    shutil.copyfile(SYSTEM / 'usr/lib/armada/update-lib',
                    root / 'usr/lib/armada/update-lib')
    # These two live in build_files but are installed into the updates package.
    for name in ('edl_device_config.py', 'edl_boot_requirements.py'):
        shutil.copyfile(ROOT / 'build_files' / name,
                        root / 'usr/lib/armada/updates' / name)
    if version != '0.8.0':
        session = root / 'usr/lib/armada/updates/offline_session.py'
        session.write_text(session.read_text().replace(
            "_UPDATER_VERSION = '0.8.0'", "_UPDATER_VERSION = '%s'" % version))
    entry = SYSTEM / 'usr/libexec/armada/armada-offline-update'
    write(root / 'usr/libexec/armada/armada-offline-update', entry.read_bytes(), 0o755)
    write(root / 'usr/libexec/armada/kshim-build-boot.py', b'#!/usr/bin/python3\n')
    write(root / 'usr/libexec/armada/libarmada-update-ab.so', b'\x7fELF fake ab library\n')
    write(root / 'usr/libexec/armada/boot-tool/armada-boot-tool',
          b'\x7fELF fake boot tool\n', 0o755)
    write(root / 'usr/libexec/armada/boot-tool/runtime/libhostfxr.so', b'fake hostfxr\n')
    # The repository/assembled rootfs keeps its key under /etc; the builder maps
    # it to the merged /usr location and never writes /etc itself.  ``write``
    # creates the etc/pki/containers parents this fixture never made before.
    write(root / TRUST_KEY_SOURCE, (SYSTEM / TRUST_KEY_SOURCE).read_bytes())
    return root


class BundleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_module('offline_updater_bundle_builder', BUILDER_PATH)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = make_runtime_root(self.base)

    def build(self, output=None):
        output = output or (self.base / 'payload.tar.gz')
        return self.builder.build(self.root, output), output

    def test_bundle_layout_and_per_file_hashes(self):
        summary, output = self.build()
        self.assertEqual(summary['version'], '0.8.0')
        self.assertEqual(summary['format'], 'tar.gz')
        self.assertEqual(summary['sha256'],
                         hashlib.sha256(output.read_bytes()).hexdigest())

        with tarfile.open(output) as archive:
            members = {member.name: member for member in archive.getmembers()}
            self.assertIn('manifest.json', members)
            manifest = json.loads(archive.extractfile('manifest.json').read())
            self.assertEqual(manifest['schemaVersion'], 1)
            self.assertEqual(manifest['bundle'], 'armada-offline-updater')
            self.assertEqual(manifest['version'], '0.8.0')
            self.assertEqual(manifest['format'], 'tar.gz')
            self.assertEqual(
                manifest['entry'], 'usr/libexec/armada/armada-offline-update')

            declared = {entry['path']: entry for entry in manifest['files']}
            self.assertEqual(set(declared), set(members) - {'manifest.json'})
            for name, entry in declared.items():
                member = members[name]
                self.assertTrue(member.isreg(), name)
                self.assertFalse(name.startswith('/'), name)
                self.assertNotIn('..', name.split('/'))
                self.assertEqual(member.size, entry['bytes'], name)
                digest = hashlib.sha256(archive.extractfile(name).read()).hexdigest()
                self.assertEqual(digest, entry['sha256'], name)
                self.assertIn(entry['mode'], ('0644', '0755'))

        expected = {
            'usr/libexec/armada/armada-offline-update',
            'usr/libexec/armada/kshim-build-boot.py',
            'usr/libexec/armada/libarmada-update-ab.so',
            'usr/libexec/armada/boot-tool/armada-boot-tool',
            'usr/libexec/armada/boot-tool/runtime/libhostfxr.so',
            'usr/lib/armada/armada_os.py',
            'usr/lib/armada/update-lib',
            'usr/lib/armada/updates/__init__.py',
            'usr/lib/armada/updates/client.py',
            'usr/lib/armada/updates/offline_rpc.py',
            'usr/lib/armada/updates/offline_jobs.py',
            'usr/lib/armada/updates/offline_session.py',
            'usr/lib/armada/updates/offline_prepare.py',
            'usr/lib/armada/updates/offline_execute.py',
            'usr/lib/armada/updates/wayland_probe.py',
            TRUST_KEY_DESTINATION,
        }
        self.assertTrue(expected <= set(declared))
        # systemd-sysext merges /usr and /opt only: a key under /etc would never
        # become visible after the merge and must not be part of the payload.
        for name in declared:
            self.assertFalse(name.startswith('etc/'), name)
        # Modules that pull in the desktop session or GTK are never part of the
        # offline prepare/execute runtime.
        for unrelated in ('dbus_service.py', 'coordinator.py', 'decky_manager.py',
                          'system_manager.py', 'installer_authorization.py'):
            self.assertNotIn('usr/lib/armada/updates/' + unrelated, declared)
        for name in declared:
            self.assertNotIn('private', name.lower())
            self.assertFalse(name.endswith('.key'), name)

    def test_uncompressed_tar_output(self):
        summary, output = self.build(self.base / 'payload.tar')
        self.assertEqual(summary['format'], 'tar')
        with tarfile.open(output) as archive:
            self.assertEqual(json.loads(
                archive.extractfile('manifest.json').read())['format'], 'tar')

    def test_output_is_byte_identical_for_identical_input(self):
        first = self.base / 'a.tar.gz'
        second = self.base / 'b.tar.gz'
        self.builder.build(self.root, first)
        self.builder.build(self.root, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_missing_boot_tool_fails(self):
        shutil.rmtree(self.root / 'usr/libexec/armada/boot-tool')
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('boot-tool', str(caught.exception))
        self.assertFalse((self.base / 'payload.tar.gz').exists())

    def test_missing_consumed_module_fails(self):
        (self.root / 'usr/lib/armada/updates/offline_session.py').unlink()
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('offline_session.py', str(caught.exception))

    def test_missing_path_consumed_script_fails(self):
        (self.root / 'usr/lib/armada/updates/wayland_probe.py').unlink()
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('wayland_probe.py', str(caught.exception))

    def test_missing_support_module_fails(self):
        (self.root / 'usr/lib/armada/armada_os.py').unlink()
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('armada_os.py', str(caught.exception))

    def test_missing_trust_key_fails(self):
        (self.root / TRUST_KEY_SOURCE).unlink()
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn(TRUST_KEY_SOURCE, str(caught.exception))

    def test_trust_key_is_shipped_under_usr_with_host_key_bytes(self):
        _summary, output = self.build()
        with tarfile.open(output) as archive:
            manifest = json.loads(archive.extractfile('manifest.json').read())
            declared = {entry['path']: entry for entry in manifest['files']}
            self.assertIn(TRUST_KEY_DESTINATION, declared)
            self.assertNotIn(TRUST_KEY_SOURCE, declared)
            shipped = archive.extractfile(TRUST_KEY_DESTINATION).read()
        self.assertEqual(shipped, (SYSTEM / TRUST_KEY_SOURCE).read_bytes())

    def test_missing_build_files_support_module_names_its_source(self):
        (self.root / UPDATES / 'edl_device_config.py').unlink()
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        message = str(caught.exception)
        self.assertIn('usr/lib/armada/updates/edl_device_config.py', message)
        self.assertIn('build_files/edl_device_config.py', message)

    def test_absolute_updates_import_in_armada_os_extends_the_closure(self):
        # armada_os imports updates.client at module load time; dropping the
        # import must not silently produce a bundle the installer rejects.
        armada_os = self.root / 'usr/lib/armada/armada_os.py'
        armada_os.write_text(armada_os.read_text().replace(
            'from updates.client import UpdateClient', 'UpdateClient = object'))
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('client', str(caught.exception))

    @unittest.skipUnless(os.name == 'posix', 'isolated interpreter import is POSIX only')
    def test_bundled_entry_point_imports_in_an_isolated_interpreter(self):
        _summary, output = self.build()
        extracted = self.base / 'extracted'
        with tarfile.open(output) as archive:
            archive.extractall(extracted)
        library = extracted / 'usr/lib/armada'
        self.assertTrue((library / 'updates/offline_rpc.py').is_file())
        code = (
            'import sys; sys.path.insert(0, %r); '
            'import updates.offline_rpc, updates.offline_jobs, '
            'updates.offline_session, updates.offline_prepare, '
            'updates.offline_execute, armada_os; '
            'print("ok")' % str(library)
        )
        completed = subprocess.run([sys.executable, '-I', '-c', code],
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('ok', completed.stdout)

    def test_runtime_version_mismatch_fails(self):
        root = make_runtime_root(self.base / 'other', version='0.7.9')
        with self.assertRaises(self.builder.BundleError) as caught:
            self.builder.build(root, self.base / 'other.tar.gz')
        self.assertIn('0.8.0', str(caught.exception))

    @unittest.skipUnless(os.name == 'posix', 'symlinks are POSIX only')
    def test_symlinked_input_is_rejected(self):
        library = self.root / 'usr/libexec/armada/libarmada-update-ab.so'
        library.unlink()
        target = self.base / 'elsewhere.so'
        target.write_bytes(b'\x7fELF\n')
        os.symlink(target, library)
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('libarmada-update-ab.so', str(caught.exception))

    def test_non_executable_entry_is_rejected(self):
        entry = self.root / 'usr/libexec/armada/armada-offline-update'
        entry.chmod(0o644)
        with self.assertRaises(self.builder.BundleError) as caught:
            self.build()
        self.assertIn('not executable', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
