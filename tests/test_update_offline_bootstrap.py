# SPDX-License-Identifier: GPL-3.0-only
"""Offline updater bootstrap: probe contract and safe extension install."""

import ast
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / 'system_files/usr/libexec/armada/armada-offline-bootstrap'
BUILDER_PATH = ROOT / 'build_files' / 'build-offline-updater-bundle.py'
SYSTEM = ROOT / 'system_files'
UPDATES = 'usr/lib/armada/updates'
EXTENSION = 'zzzzzz-armada-offline-updater'
TRUST_KEY_SOURCE = 'etc/pki/containers/armadaos-freedom.pub'
TRUST_KEY_PACKAGED = 'usr/share/armada/updates/armadaos-freedom.pub'
UPDATE_SERVICE = 'usr/share/dbus-1/system-services/org.armada.Updates.service'


def load_module(name, path):
    """Load an extensionless helper with an explicit source loader."""
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


def copy(source, target):
    """Copy a file and keep its mode, so manifest claims stay comparable."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(source.stat().st_mode & 0o777)
    return target


def set_version(root, version):
    session = root / UPDATES / 'offline_session.py'
    session.write_text(session.read_text().replace(
        "_UPDATER_VERSION = '0.8.0'", "_UPDATER_VERSION = '%s'" % version))


def populate_runtime(root, version='0.8.0', key='etc'):
    """Install the same files the bundle builder expects to find.

    ``key='etc'`` models the bundle source (repository /etc key); ``key='usr'``
    models the merged runtime an installed extension provides.
    """
    (root / 'usr/lib/armada').mkdir(parents=True, exist_ok=True)
    shutil.copytree(SYSTEM / 'usr/lib/armada/updates', root / UPDATES,
                    dirs_exist_ok=True)
    for name in ('armada_os.py', 'update-lib'):
        copy(SYSTEM / 'usr/lib/armada' / name, root / 'usr/lib/armada' / name)
    for name in ('edl_device_config.py', 'edl_boot_requirements.py'):
        copy(ROOT / 'build_files' / name, root / UPDATES / name)
    if version != '0.8.0':
        set_version(root, version)
    entry = (SYSTEM / 'usr/libexec/armada/armada-offline-update').read_bytes()
    write(root / 'usr/libexec/armada/armada-offline-update', entry, 0o755)
    write(root / 'usr/libexec/armada/kshim-build-boot.py', b'#!/usr/bin/python3\n')
    write(root / 'usr/libexec/armada/libarmada-update-ab.so', b'\x7fELF fake ab library\n')
    write(root / 'usr/libexec/armada/boot-tool/armada-boot-tool',
          b'\x7fELF fake boot tool\n', 0o755)
    write(root / 'usr/libexec/armada/boot-tool/runtime/libhostfxr.so', b'fake hostfxr\n')
    if key == 'etc':
        copy(SYSTEM / TRUST_KEY_SOURCE, root / TRUST_KEY_SOURCE)
    elif key == 'usr':
        copy(SYSTEM / TRUST_KEY_SOURCE, root / TRUST_KEY_PACKAGED)
    return root


def make_target(base, version='0.7.0', runtime=False, etc_key=None, packaged_key=None):
    """A pre-0.8 installation: no updater entry point, no updater modules.

    The real pre-0.8 tablet has no /etc/pki/containers key at all, so no trust
    key is written unless a test asks for one.
    """
    root = base / 'target'
    write(root / 'usr/lib/os-release',
          ('NAME="Armada"\nID=armada\nID_LIKE="fedora"\nVERSION_ID=41\n'
           'IMAGE_VERSION=%s\nVERSION=%s\n' % (version, version)).encode())
    write(root / 'usr/lib/armada/version', (version + '\n').encode())
    write(root / 'usr/bin/systemd-sysext', b'#!/bin/sh\nexit 0\n', 0o755)
    write(root / 'sys/firmware/devicetree/base/compatible',
          b'lenovo,baldur\x00qcom,kaanapali\x00')
    (root / 'run/extensions').mkdir(parents=True)
    if etc_key is not None:
        write(root / TRUST_KEY_SOURCE, etc_key)
    if packaged_key is not None:
        write(root / TRUST_KEY_PACKAGED, packaged_key)
    if runtime:
        populate_runtime(root, version)
    return root


def populate_from_bundle(root, bundle):
    """Install the bundle's manifest contents verbatim.

    The builder manifest is the authority on both bytes and modes.  Reusing a
    checked-out runtime tree can silently drift (for example a mode the fixture
    copies but the manifest normalises), which would prove nothing about the
    verifier.  This keeps the reuse fixture exactly comparable.
    """
    with tarfile.open(bundle) as archive:
        manifest = json.loads(archive.extractfile('manifest.json').read())
        for entry in manifest['files']:
            write(root / entry['path'], archive.extractfile(entry['path']).read(),
                  int(entry['mode'], 8))
    return root


class FakeRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return types.SimpleNamespace(returncode=self.returncode, stdout=b'', stderr=b'')


def write_archive(path, extra, manifest=None):
    document = manifest or {
        'schemaVersion': 1,
        'bundle': 'armada-offline-updater',
        'version': '0.8.0',
        'format': 'tar.gz',
        'entry': 'usr/libexec/armada/armada-offline-update',
        'files': [{'path': 'usr/libexec/armada/armada-offline-update',
                   'bytes': 1, 'sha256': '0' * 64, 'mode': '0755'}],
    }
    with tarfile.open(path, 'w:gz') as archive:
        data = json.dumps(document).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        for member in extra:
            if member[1] is None:
                info = tarfile.TarInfo(member[0])
                info.type = tarfile.SYMTYPE
                info.linkname = member[2]
                archive.addfile(info)
                continue
            info = tarfile.TarInfo(member[0])
            info.size = len(member[1])
            info.mode = member[2]
            archive.addfile(info, io.BytesIO(member[1]))
    return path


def base_manifest(**overrides):
    document = {
        'schemaVersion': 1,
        'bundle': 'armada-offline-updater',
        'version': '0.8.0',
        'format': 'tar.gz',
        'entry': 'usr/libexec/armada/armada-offline-update',
        'files': [{'path': 'usr/libexec/armada/armada-offline-update',
                   'bytes': 1, 'sha256': '0' * 64, 'mode': '0755'}],
    }
    document.update(overrides)
    return document


def rewrite_manifest(source, target, mutate):
    """Copy a bundle, replacing manifest.json after ``mutate(document)``."""
    with tarfile.open(source) as archive:
        members = [(member, archive.extractfile(member).read())
                   for member in archive]
    with tarfile.open(target, 'w:gz') as archive:
        for member, data in members:
            if member.name == 'manifest.json':
                document = json.loads(data.decode('utf-8'))
                mutate(document)
                data = json.dumps(document).encode('utf-8')
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            info.mode = member.mode
            archive.addfile(info, io.BytesIO(data))
    return target


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_module('armada_offline_bootstrap', BOOTSTRAP_PATH)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = make_target(self.base)
        self.enter = mock.patch.object(self.bootstrap, '_is_root', return_value=True)
        self.enter.start()
        self.addCleanup(self.enter.stop)
        self.arch = mock.patch.object(self.bootstrap, '_architecture',
                                      return_value='aarch64')
        self.arch.start()
        self.addCleanup(self.arch.stop)

    def probe(self, runner=None):
        return self.bootstrap.probe(root=str(self.root), runner=runner)

    def test_old_installation_without_updater_is_still_deployable(self):
        result = self.probe()
        self.assertTrue(result['ok'])
        self.assertTrue(result['connection']['ready'])
        self.assertTrue(result['upgrade']['eligible'])
        self.assertEqual(result['system']['model'], 'TB323FU')
        self.assertEqual(result['system']['version'], '0.7.0')
        self.assertEqual(result['system']['osId'], 'armada')
        self.assertEqual(result['system']['osVersionId'], '41')
        self.assertFalse(result['updater']['entryPresent'])
        self.assertIsNone(result['updater']['version'])
        self.assertFalse(result['updater']['capable'])
        self.assertIn('usr/libexec/armada/armada-offline-update',
                      result['updater']['missing'])
        self.assertFalse(result['upgrade']['tokensEvaluated'])

    def test_probe_reports_missing_components_from_connection(self):
        (self.root / 'usr/bin/systemd-sysext').unlink()
        result = self.probe()
        self.assertFalse(result['connection']['ready'])
        self.assertIn('missing-systemd-sysext', result['connection']['reasons'])
        # The pre-0.8 tablet has no /etc key and the bundle is not installed yet.
        self.assertFalse(result['trust']['present'])
        self.assertIsNone(result['trust']['source'])
        self.assertIn('missing-trust-anchor', result['trust']['reasons'])
        # The bundle supplies the key, so a host without one stays eligible.
        self.assertTrue(result['upgrade']['eligible'])

    def test_probe_uses_the_packaged_key_when_etc_has_none(self):
        write(self.root / TRUST_KEY_PACKAGED, b'public key\n')
        result = self.probe()
        self.assertTrue(result['upgrade']['eligible'])
        self.assertTrue(result['trust']['present'])
        self.assertEqual(result['trust']['source'], 'packaged')
        self.assertFalse(result['trust']['conflict'])
        self.assertTrue(result['trust']['keyPath'].endswith(TRUST_KEY_PACKAGED))

    def test_probe_prefers_the_host_key_and_reports_a_conflict(self):
        write(self.root / TRUST_KEY_PACKAGED, b'bundled key\n')
        write(self.root / TRUST_KEY_SOURCE, b'host key\n')
        result = self.probe()
        self.assertEqual(result['trust']['source'], 'etc')
        self.assertTrue(result['trust']['conflict'])
        self.assertTrue(result['trust']['keyPath'].endswith(TRUST_KEY_SOURCE))

        write(self.root / TRUST_KEY_SOURCE, b'bundled key\n')
        result = self.probe()
        self.assertEqual(result['trust']['source'], 'etc')
        self.assertFalse(result['trust']['conflict'])

    def test_probe_reports_the_current_service_separately(self):
        result = self.probe()
        self.assertFalse(result['service']['present'])
        self.assertIn('missing-update-service', result['service']['reasons'])
        # A missing service is not a platform limitation.
        self.assertTrue(result['upgrade']['eligible'])
        self.assertNotIn('missing-update-service', result['upgrade']['reasons'])
        self.assertNotIn('missing-update-service', result['connection']['reasons'])

        write(self.root / UPDATE_SERVICE, b'[D-BUS Service]\n')
        result = self.probe()
        self.assertTrue(result['service']['present'])
        self.assertEqual(result['service']['reasons'], [])

    def test_probe_flags_unsupported_or_unknown_platform(self):
        write(self.root / 'sys/firmware/devicetree/base/compatible',
              b'lenovo,unknown\x00qcom,unknown\x00')
        result = self.probe()
        self.assertIsNone(result['system']['model'])
        self.assertIn('unsupported-model', result['upgrade']['reasons'])

        (self.root / 'sys/firmware/devicetree/base/compatible').unlink()
        result = self.probe()
        self.assertIn('missing-devicetree-compatible', result['upgrade']['reasons'])

    def test_probe_reports_unsupported_architecture(self):
        with mock.patch.object(self.bootstrap, '_architecture', return_value='x86_64'):
            result = self.probe()
        self.assertFalse(result['upgrade']['eligible'])
        self.assertIn('unsupported-architecture', result['upgrade']['reasons'])

    def test_probe_never_executes_commands(self):
        def bomb(*_args, **_kwargs):
            raise AssertionError('probe must not run external commands')

        result = self.probe(runner=bomb)
        self.assertTrue(result['ok'])

    def test_helper_has_no_updater_boot_or_partition_operations(self):
        # Filenames such as offline_prepare.py are legitimate CORE_MODULES
        # entries.  Check what the helper actually imports and runs instead of
        # banning words that also appear in a manifest filename.
        source = BOOTSTRAP_PATH.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(BOOTSTRAP_PATH))
        allowed_imports = {
            '__future__', 'hashlib', 'hmac', 'json', 'os', 'pathlib', 'platform',
            're', 'shutil', 'stat', 'subprocess', 'sys', 'tarfile', 'tempfile',
            'uuid',
        }
        forbidden_imports = {
            'updates', 'armada_os', 'boot_writer', 'slot_activation',
            'trial_store', 'system_transaction', 'offline_prepare',
            'offline_execute', 'offline_recovery',
        }
        imported = set()
        runner_calls = []
        direct_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split('.')[0])
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in (
                        'runner', 'run', 'call', 'check_call', 'check_output',
                        'Popen'):
                    runner_calls.append(node)
                elif (isinstance(func, ast.Attribute) and
                        isinstance(func.value, ast.Name) and
                        func.value.id in ('os', 'subprocess')):
                    direct_calls.append((func.value.id, func.attr))
        self.assertEqual(imported - allowed_imports, set())
        self.assertFalse(imported & forbidden_imports)

        # The only external command is the injected runner's systemd-sysext
        # refresh; no direct os/subprocess execution exists.
        forbidden_methods = {
            'system', 'popen', 'fork', 'execv', 'execve', 'execl', 'execle',
            'spawn', 'spawnl', 'spawnv', 'spawnvp',
        }
        for module, method in direct_calls:
            self.assertNotIn(method, forbidden_methods)
            self.assertNotEqual(module, 'subprocess',
                                'helper must use the injected runner')
        self.assertEqual(len(runner_calls), 1)
        command = runner_calls[0]
        self.assertIsInstance(command.func, ast.Name)
        self.assertEqual(command.func.id, 'runner')
        argv = command.args[0]
        self.assertIsInstance(argv, ast.List)
        self.assertEqual(len(argv.elts), 2)
        literals = [element.value for element in argv.elts
                    if isinstance(element, ast.Constant)]
        self.assertEqual(literals, ['refresh'])
        for forbidden in ('systemctl', 'sgdisk', 'parted', 'mkfs', 'wipefs',
                          'systemd-run', 'bootc', 'ostree'):
            self.assertNotIn(forbidden, literals)

    def test_probe_reports_installed_runtime_without_querying_it(self):
        populate_runtime(self.root, key='usr')
        write(self.root / ('usr/lib/extension-release.d/extension-release.'
                           + EXTENSION),
              b'ID=armada\n')
        result = self.probe()
        self.assertEqual(result['updater']['version'], '0.8.0')
        self.assertEqual(result['updater']['source'], 'extension')
        self.assertTrue(result['updater']['capable'])
        self.assertEqual(result['updater']['missing'], [])

    def test_native_runtime_is_capable_with_only_the_etc_policy_key(self):
        populate_runtime(self.root, key='etc')
        result = self.probe()
        self.assertEqual(result['updater']['source'], 'native')
        self.assertEqual(result['trust']['source'], 'etc')
        self.assertTrue(result['updater']['capable'])
        self.assertEqual(result['updater']['missing'], [])


class ManifestCapabilityTest(unittest.TestCase):
    """The helper accepts old bundles and rejects malformed capability claims."""

    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_module('armada_offline_bootstrap_capabilities',
                                    BOOTSTRAP_PATH)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()

    def scan(self, manifest, extra=()):
        path = write_archive(self.base / 'bundle.tar.gz', list(extra), manifest)
        with path.open('rb') as stream:
            return self.bootstrap._scan_archive(stream)

    def test_bundle_without_capabilities_is_still_accepted(self):
        manifest, _members = self.scan(base_manifest())
        self.assertNotIn('capabilities', manifest)

    def test_declared_capabilities_are_accepted_including_unknown_ones(self):
        manifest, _members = self.scan(base_manifest(
            capabilities=['preflight-session-v1', 'some-future-capability']))
        self.assertEqual(manifest['capabilities'],
                         ['preflight-session-v1', 'some-future-capability'])

    def test_malformed_capabilities_are_rejected(self):
        cases = (
            'preflight-session-v1',
            None,
            [1],
            ['Preflight Session'],
            ['preflight-session-v1', 'preflight-session-v1'],
            ['a' * (self.bootstrap.MAX_CAPABILITY_LENGTH + 1)],
            ['cap-%d' % index
             for index in range(self.bootstrap.MAX_CAPABILITIES + 1)],
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(self.bootstrap.BootstrapError) as caught:
                    self.scan(base_manifest(capabilities=value))
                self.assertEqual(caught.exception.code, 'manifest_invalid')

    def test_oversized_manifest_is_rejected(self):
        path = self.base / 'oversized.tar.gz'
        data = b' ' * (self.bootstrap.MAX_MANIFEST_BYTES + 1)
        with tarfile.open(path, 'w:gz') as archive:
            info = tarfile.TarInfo('manifest.json')
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with path.open('rb') as stream:
            with self.assertRaises(self.bootstrap.BootstrapError) as caught:
                self.bootstrap._scan_archive(stream)
        self.assertEqual(caught.exception.code, 'manifest_invalid')

    def test_duplicate_manifest_is_rejected(self):
        data = json.dumps(base_manifest()).encode()
        path = self.base / 'duplicate.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for _index in range(2):
                info = tarfile.TarInfo('manifest.json')
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with path.open('rb') as stream:
            with self.assertRaises(self.bootstrap.BootstrapError) as caught:
                self.bootstrap._scan_archive(stream)
        self.assertEqual(caught.exception.code, 'archive_member_rejected')


class ExtensionReleaseTest(unittest.TestCase):
    """extension-release values are serialised, never interpolated blindly."""

    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_module('armada_offline_bootstrap_release', BOOTSTRAP_PATH)

    def test_serialises_plain_values(self):
        data = self.bootstrap._extension_release(
            {'ID': 'armada', 'VERSION_ID': '41', 'SYSEXT_LEVEL': '41'})
        self.assertEqual(
            data,
            b'# managed by armada-offline-bootstrap\n'
            b'ID=armada\nVERSION_ID=41\nSYSEXT_LEVEL=41\n')

    def test_optional_keys_are_omitted_when_absent(self):
        data = self.bootstrap._extension_release({'ID': 'armada'})
        self.assertEqual(data, b'# managed by armada-offline-bootstrap\nID=armada\n')

    def test_rejects_values_that_would_escape_the_assignment(self):
        for value in ('armada\nID=evil', 'armada"', "armada'", 'armada extra',
                      'armada=evil', '$ID', 'armada\\n'):
            with self.assertRaises(self.bootstrap.BootstrapError) as caught:
                self.bootstrap._extension_release({'ID': value})
            self.assertEqual(caught.exception.code, 'input_invalid', value)
            self.assertIn('ID', str(caught.exception))

        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.bootstrap._extension_release({'ID': 'armada', 'VERSION_ID': '41\nID=x'})
        self.assertEqual(caught.exception.code, 'input_invalid')
        self.assertIn('VERSION_ID', str(caught.exception))

    def test_requires_an_id(self):
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.bootstrap._extension_release({})
        self.assertEqual(caught.exception.code, 'input_missing')


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'extension install writes root-owned files')
class InstallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_module('armada_offline_bootstrap_install', BOOTSTRAP_PATH)
        cls.builder = load_module('offline_updater_bundle_builder_install', BUILDER_PATH)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        source = populate_runtime(self.base / 'bundle-src')
        self.bundle = self.base / 'updater.tar.gz'
        summary = self.builder.build(source, self.bundle)
        self.digest = summary['sha256']
        self.root = make_target(self.base)
        self.runner = FakeRunner()
        self.final = self.root / 'run/extensions' / EXTENSION

    def install(self, bundle=None, digest=None, runner=None):
        return self.bootstrap.install(
            str(bundle or self.bundle), digest or self.digest,
            root=str(self.root), runner=runner or self.runner)

    def leftovers(self):
        """Temporary directories this invocation may have created."""
        return sorted(path.name for path in
                      (self.root / 'run/extensions').glob('.%s.*' % EXTENSION))

    def test_install_deploys_extension_without_touching_other_extensions(self):
        other = self.root / 'run/extensions/other.raw'
        write(other / 'keep.txt', b'other extension\n')

        result = self.install()

        self.assertTrue(result['ok'])
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(result['version'], '0.8.0')
        self.assertEqual(result['extension'], EXTENSION)
        self.assertEqual(result['path'], str(self.final))
        self.assertFalse(result['replaced'])
        self.assertTrue(result['sysextRefreshed'])

        entry = self.final / 'usr/libexec/armada/armada-offline-update'
        self.assertTrue(entry.is_file())
        self.assertEqual(entry.stat().st_mode & 0o777, 0o755)
        for name in ('offline_rpc.py', 'offline_jobs.py', 'offline_session.py',
                     'offline_prepare.py', 'offline_execute.py', 'wayland_probe.py'):
            self.assertTrue((self.final / UPDATES / name).is_file(), name)
        # The extension is merged into /usr: the trust key must live there and
        # nothing may be written under /etc.
        self.assertTrue((self.final / TRUST_KEY_PACKAGED).is_file())
        self.assertFalse((self.final / 'etc').exists())
        release = self.final / ('usr/lib/extension-release.d/extension-release.'
                                + EXTENSION)
        self.assertIn(b'ID=armada', release.read_bytes())
        self.assertIn(b'VERSION_ID=41', release.read_bytes())

        self.assertEqual((other / 'keep.txt').read_bytes(), b'other extension\n')
        self.assertEqual(self.leftovers(), [])

        sysext = str(self.root / 'usr/bin/systemd-sysext')
        self.assertEqual(self.runner.calls, [[sysext, 'refresh']])

    def test_install_accepts_a_bundle_without_capabilities(self):
        # The helper stays compatible with bundles built before the capability
        # list existed; the installer is the side that enforces the contract.
        legacy = rewrite_manifest(
            self.bundle, self.base / 'legacy.tar.gz',
            lambda document: document.pop('capabilities'))
        digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
        result = self.install(bundle=legacy, digest=digest)
        self.assertEqual(result['state'], 'installed')

    def test_install_rejects_wrong_bundle_digest(self):
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install(digest='0' * 64)
        self.assertEqual(caught.exception.code, 'hash_mismatch')
        self.assertFalse(self.final.exists())

    def test_install_rejects_traversal_member(self):
        malicious = write_archive(self.base / 'bad.tar.gz',
                                  [('../escape.txt', b'owned', 0o644)])
        digest = hashlib.sha256(malicious.read_bytes()).hexdigest()
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install(bundle=malicious, digest=digest)
        self.assertEqual(caught.exception.code, 'archive_path_rejected')
        self.assertFalse((self.base / 'escape.txt').exists())
        self.assertFalse(self.final.exists())

    def test_install_rejects_symlink_member(self):
        malicious = write_archive(self.base / 'link.tar.gz', [
            ('usr/libexec/armada/armada-offline-update', None, '/etc/passwd'),
        ])
        digest = hashlib.sha256(malicious.read_bytes()).hexdigest()
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install(bundle=malicious, digest=digest)
        self.assertEqual(caught.exception.code, 'archive_member_rejected')
        self.assertFalse(self.final.exists())

    def test_install_rejects_manifest_digest_mismatch(self):
        tampered = self.base / 'tampered.tar.gz'
        with tarfile.open(self.bundle) as source, tarfile.open(tampered, 'w:gz') as target:
            for member in source:
                data = source.extractfile(member).read()
                if member.name == UPDATES + '/offline_rpc.py':
                    data = data[:-1] + (b'\x00' if data[-1:] != b'\x00' else b'\x01')
                info = tarfile.TarInfo(member.name)
                info.size = len(data)
                info.mode = member.mode
                target.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(tampered.read_bytes()).hexdigest()
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install(bundle=tampered, digest=digest)
        self.assertEqual(caught.exception.code, 'manifest_mismatch')
        self.assertFalse(self.final.exists())

    def test_failed_refresh_keeps_the_previous_extension(self):
        write(self.final / 'keep.txt', b'previous runtime\n')
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install(runner=FakeRunner(returncode=1))
        self.assertEqual(caught.exception.code, 'sysext_failed')
        self.assertEqual((self.final / 'keep.txt').read_bytes(), b'previous runtime\n')
        self.assertEqual(self.leftovers(), [])

    def test_install_preserves_unknown_stage_and_backup_directories(self):
        extensions = self.root / 'run/extensions'
        preexisting_stage = write(extensions / ('.%s.stage' % EXTENSION) / 'keep.txt',
                                  b'not ours\n')
        preexisting_backup = write(extensions / ('.%s.old' % EXTENSION) / 'keep.txt',
                                   b'not ours either\n')

        result = self.install()

        self.assertEqual(result['state'], 'installed')
        self.assertTrue(preexisting_stage.is_file())
        self.assertTrue(preexisting_backup.is_file())
        # The invocation cleans up only the directories it created itself.
        self.assertEqual(self.leftovers(),
                         sorted([preexisting_stage.parent.name,
                                 preexisting_backup.parent.name]))

    def test_install_reports_a_trust_conflict_instead_of_switching(self):
        host_key = write(self.root / TRUST_KEY_SOURCE, b'host key that differs\n')
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, 'trust_conflict')
        self.assertEqual(host_key.read_bytes(), b'host key that differs\n')
        self.assertFalse(self.final.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.runner.calls, [])

    def test_install_keeps_an_identical_host_trust_key(self):
        bundled = (SYSTEM / TRUST_KEY_SOURCE).read_bytes()
        host_key = write(self.root / TRUST_KEY_SOURCE, bundled)
        result = self.install()
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(host_key.read_bytes(), bundled)
        self.assertFalse((self.final / 'etc').exists())

    def test_install_rejects_a_malformed_os_release_before_staging(self):
        write(self.root / 'usr/lib/os-release',
              b'ID=armada\nVERSION_ID=41\nSYSEXT_LEVEL="41 41"\n')
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, 'input_invalid')
        self.assertFalse(self.final.exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.runner.calls, [])

    def test_install_reuses_a_same_version_runtime(self):
        populate_from_bundle(self.root, self.bundle)
        result = self.install()
        self.assertEqual(result['state'], 'reused')
        self.assertTrue(result['verified'])
        self.assertFalse(result['sysextRefreshed'])
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.final.exists())

    def test_install_does_not_reuse_stale_same_version_contents(self):
        # Same version, different bytes: the claim in the manifest wins.
        populate_runtime(self.final, '0.8.0', key='usr')
        write(self.final / UPDATES / 'offline_rpc.py', b'# tampered\n')
        write(self.root / ('usr/lib/extension-release.d/extension-release.'
                           + EXTENSION),
              b'ID=armada\n')

        result = self.install()

        self.assertEqual(result['state'], 'installed')
        self.assertTrue(result['replaced'])
        self.assertNotEqual((self.final / UPDATES / 'offline_rpc.py').read_bytes(),
                            b'# tampered\n')
        self.assertTrue((self.final / TRUST_KEY_PACKAGED).is_file())

    def test_install_refuses_a_newer_installed_runtime(self):
        populate_runtime(self.root, '0.9.0')
        with self.assertRaises(self.bootstrap.BootstrapError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, 'newer_runtime_present')
        self.assertFalse(self.final.exists())
        self.assertTrue((self.root / UPDATES / 'offline_session.py').is_file())

    def test_install_requires_root(self):
        with mock.patch.object(self.bootstrap, '_is_root', return_value=False):
            with self.assertRaises(self.bootstrap.BootstrapError) as caught:
                self.install()
        self.assertEqual(caught.exception.code, 'not_root')

    def test_install_only_ever_runs_systemd_sysext(self):
        self.install()
        self.assertTrue(self.runner.calls)
        sysext = str(self.root / 'usr/bin/systemd-sysext')
        for call in self.runner.calls:
            self.assertEqual(call, [sysext, 'refresh'])


if __name__ == '__main__':
    unittest.main()
