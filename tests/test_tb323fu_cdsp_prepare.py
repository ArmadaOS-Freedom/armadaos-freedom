import configparser
import hashlib
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / 'system_files/usr/libexec/tb323fu/tb323fu-cdsp-prepare'
UNIT = REPO / 'system_files/usr/lib/systemd/system/tb323fu-cdsp-prepare.service'
CONTAINERFILE = REPO / 'Containerfile'
VENDOR_SCRIPT = REPO / 'build_files/40-vendor-system-files.sh'
BUNDLE = REPO / 'devices/lenovo-tb323fu/firmware/compute'

NL = bytes([10])
NUL = bytes([0])
MODEL = NUL.join([b'qcom,kaanapali', b'lenovo,baldur', b''])
FW_REL = Path('qcom/kaanapali/lenovo/baldur')
CDSP_FIRMWARE = 'qcom/kaanapali/lenovo/baldur/cdsp.mbn'
MBN = b'cdsp-mbn-fixture-0001'
DTB = b'cdsp-dtb-fixture-0002'
FIRMWARE_NAMES = ('cdsp.mbn', 'cdsp_dtb.mbn')
INSTALL_ROOT = '/usr/share/tb323fu/firmware/compute'
COPY_DEST = '/tb323fu-cdsp/firmware'
SERVICE = 'tb323fu-cdsp-prepare.service'
EXEC_START = '/usr/libexec/tb323fu/tb323fu-cdsp-prepare'
UNIT_DEST = '/usr/lib/systemd/system/' + SERVICE
VENDOR_MARKER = '# TB710FU firmware:'
EM_ARM = 40
EM_HEXAGON = 164
ELF32 = struct.Struct('<16sHHIIIIIHHHHHH')
ELF_MAGIC = bytes([0x7f]) + b'ELF'
# per-file expectation: cdsp.mbn is the Hexagon v81 DSP executable,
# cdsp_dtb.mbn is the ARM data wrapper holding the device tree, not DSP code.
FIRMWARE_ELF = {
    'cdsp.mbn': (EM_HEXAGON, 0x81),
    'cdsp_dtb.mbn': (EM_ARM, 0x00),
}


class CdspPrepareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (HELPER, UNIT, CONTAINERFILE, VENDOR_SCRIPT):
            if not path.is_file():
                raise AssertionError('missing file: ' + str(path))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.compatible = self.root / 'compatible'
        self.compatible.write_bytes(MODEL)
        self.bundle = self.root / 'bundle'
        self.bundle_src = self.bundle / FW_REL
        self.bundle_src.mkdir(parents=True)
        (self.bundle_src / 'cdsp.mbn').write_bytes(MBN)
        (self.bundle_src / 'cdsp_dtb.mbn').write_bytes(DTB)
        self.remoteproc = self.root / 'remoteproc'
        self.cdsp = self._proc('cdsp0', 'cdsp', CDSP_FIRMWARE, 'offline')
        self.adsp = self._proc('adsp0', 'adsp', 'qcom/kaanapali/lenovo/baldur/adsp.mbn', 'running')
        self.param = self.root / 'firmware-class-path'
        self.param.write_bytes(b'')
        self.run_fw = self.root / 'run-fw'

    def tearDown(self):
        self.tmp.cleanup()

    def _proc(self, directory, name, firmware, state):
        path = self.remoteproc / directory
        path.mkdir(parents=True)
        (path / 'name').write_bytes(name.encode() + NL)
        (path / 'firmware').write_bytes(firmware.encode() + NL)
        (path / 'state').write_bytes(state.encode() + NL)
        return path

    def _env(self, **overrides):
        env = {
            'PATH': os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'),
            'TB323FU_DT_COMPATIBLE': str(self.compatible),
            'TB323FU_CDSP_FIRMWARE_DIR': str(self.bundle),
            'TB323FU_REMOTEPROC_DIR': str(self.remoteproc),
            'TB323FU_FIRMWARE_CLASS_PATH': str(self.param),
            'TB323FU_RUN_FIRMWARE': str(self.run_fw),
            'TB323FU_CDSP_WAIT_SECONDS': '0',
        }
        env.update(overrides)
        return env

    def _snapshot(self, directory):
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in sorted(directory.rglob('*'))
            if path.is_file()
        }

    def _run(self, **overrides):
        return subprocess.run(['bash', str(HELPER)], env=self._env(**overrides), capture_output=True, text=True)

    def _assert_no_mutation(self):
        self.assertFalse(self.run_fw.exists())
        self.assertEqual(self.param.read_bytes(), b'')
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'offline' + NL)

    def test_wrong_and_prefix_models_ignored_without_firmware(self):
        for compatible in (
            NUL.join([b'qcom,kaanapali', b'lenovo,baldurx', b'']),
            NUL.join([b'lenovo,baldur-extra', b'']),
            NUL.join([b'qcom,kaanapali', b'']),
            b'',
        ):
            with self.subTest(compatible=compatible):
                self.compatible.write_bytes(compatible)
                for name in FIRMWARE_NAMES:
                    (self.bundle_src / name).unlink(missing_ok=True)
                result = self._run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self._assert_no_mutation()

    def test_matching_model_proceeds(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.run_fw / FW_REL / 'cdsp.mbn').read_bytes(), MBN)

    def test_missing_or_empty_firmware_blocks_install(self):
        for name in FIRMWARE_NAMES:
            path = self.bundle_src / name
            original = path.read_bytes()
            for damage in ('missing', 'empty'):
                with self.subTest(name=name, damage=damage):
                    if damage == 'missing':
                        path.unlink()
                    else:
                        path.write_bytes(b'')
                    result = self._run()
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self._assert_no_mutation()
                    path.write_bytes(original)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mismatched_cdsp_firmware_rejected(self):
        (self.cdsp / 'firmware').write_bytes(b'qcom/other/cdsp.mbn' + NL)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self._assert_no_mutation()

    def test_relative_firmware_path_rejected(self):
        self.param.write_bytes(b'relative/firmware' + NL)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'relative').exists())
        self.assertEqual(self.param.read_bytes(), b'relative/firmware' + NL)
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'offline' + NL)

    def test_invalid_cdsp_state_rejected(self):
        (self.cdsp / 'state').write_bytes(b'suspended' + NL)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'suspended' + NL)
        self.assertFalse(self.run_fw.exists())
        self.assertEqual(self.param.read_bytes(), b'')

    def test_missing_or_multiple_cdsp_rejected(self):
        (self.cdsp / 'name').write_bytes(b'gdsp' + NL)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self._assert_no_mutation()
        (self.cdsp / 'name').write_bytes(b'cdsp' + NL)
        self._proc('cdsp1', 'cdsp', CDSP_FIRMWARE, 'offline')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self._assert_no_mutation()

    def test_copy_failure_cannot_start(self):
        self.run_fw.mkdir()
        (self.run_fw / 'qcom').write_bytes(b'blocker')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'offline' + NL)
        self.assertEqual(self.param.read_bytes(), b'')

    def test_offline_install_starts_cdsp(self):
        adsp_before = self._snapshot(self.adsp)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = self.run_fw / FW_REL
        self.assertEqual((dest / 'cdsp.mbn').read_bytes(), MBN)
        self.assertEqual((dest / 'cdsp_dtb.mbn').read_bytes(), DTB)
        self.assertEqual((self.bundle_src / 'cdsp.mbn').read_bytes(), MBN)
        self.assertEqual((self.bundle_src / 'cdsp_dtb.mbn').read_bytes(), DTB)
        self.assertEqual(self.param.read_bytes(), str(self.run_fw).encode())
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'start' + NL)
        self.assertEqual(self._snapshot(self.adsp), adsp_before)
        self.assertEqual(sorted(p.name for p in self.remoteproc.iterdir()), ['adsp0', 'cdsp0'])
        self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
        for name in FIRMWARE_NAMES:
            self.assertEqual((dest / name).stat().st_mode & 0o777, 0o644)

    def test_late_cdsp_registration_starts_after_wait(self):
        # Remove async cdsp; adsp remains. Helper must wait, then start once
        # the async cdsp directory appears with complete contents.
        shutil.rmtree(self.cdsp)
        adsp_before = self._snapshot(self.adsp)
        staging = self.remoteproc / '.cdsp0.staging'
        staging.mkdir()
        (staging / 'name').write_bytes(b'cdsp' + NL)
        (staging / 'firmware').write_bytes(CDSP_FIRMWARE.encode() + NL)
        (staging / 'state').write_bytes(b'offline' + NL)
        err_path = self.root / 'helper-stderr'
        published = {'done': False}
        with open(err_path, 'wb') as err_file:
            proc = subprocess.Popen(
                ['bash', str(HELPER)],
                env=self._env(TB323FU_CDSP_WAIT_SECONDS='5'),
                stdout=subprocess.PIPE,
                stderr=err_file,
            )
            try:
                deadline = time.monotonic() + 3.0
                observed = False
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        text = err_path.read_text(errors='replace')
                    except OSError:
                        text = ''
                    if 'waiting up to ' in text:
                        observed = True
                        break
                    time.sleep(0.05)
                self.assertTrue(observed,
                                'helper did not emit waiting message; stderr=' +
                                err_path.read_text(errors='replace'))
                # Atomically publish fully-prepared cdsp directory.
                os.rename(str(staging), str(self.remoteproc / 'cdsp0'))
                published['done'] = True
                try:
                    out, _ = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                    self.fail('helper did not exit after late cdsp appeared')
            finally:
                if not published['done'] and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
        self.assertEqual(proc.returncode, 0, err_path.read_text(errors='replace'))
        dest = self.run_fw / FW_REL
        self.assertEqual((dest / 'cdsp.mbn').read_bytes(), MBN)
        self.assertEqual((dest / 'cdsp_dtb.mbn').read_bytes(), DTB)
        self.assertEqual(self.param.read_bytes(), str(self.run_fw).encode())
        self.assertEqual((self.remoteproc / 'cdsp0' / 'state').read_bytes(), b'start' + NL)
        self.assertEqual(self._snapshot(self.adsp), adsp_before)


    def test_duplicate_cdsp_fails_immediately_with_positive_wait(self):
        self._proc('cdsp1', 'cdsp', CDSP_FIRMWARE, 'offline')
        try:
            result = subprocess.run(
                ['bash', str(HELPER)],
                env=self._env(TB323FU_CDSP_WAIT_SECONDS='10'),
                capture_output=True, text=True, timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail('helper waited on duplicate cdsp instead of failing fast')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.run_fw.exists())
        self.assertEqual(self.param.read_bytes(), b'')
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'offline' + NL)
        self.assertEqual((self.remoteproc / 'cdsp1' / 'state').read_bytes(), b'offline' + NL)

    def test_running_cdsp_state_never_written(self):
        (self.cdsp / 'state').write_bytes(b'running' + NL)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'running' + NL)
        for path in self.remoteproc.rglob('state'):
            self.assertNotIn(b'start', path.read_bytes())

    def test_existing_firmware_path_reused(self):
        shared = self.root / 'shared-fw'
        self.param.write_bytes(str(shared).encode() + NL)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((shared / FW_REL / 'cdsp.mbn').read_bytes(), MBN)
        self.assertEqual((shared / FW_REL / 'cdsp_dtb.mbn').read_bytes(), DTB)
        self.assertEqual(self.param.read_bytes(), str(shared).encode() + NL)
        self.assertFalse(self.run_fw.exists())
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'start' + NL)

    def test_firmware_path_with_spaces_preserved(self):
        shared = self.root / 'shared fw dir'
        (shared / 'qcom/adreno').mkdir(parents=True)
        gpu = shared / 'qcom/adreno/a740_sqe.fw'
        gpu.write_bytes(b'gpu-blob')
        touch = shared / 'novatek/nt36532_fw.bin'
        touch.parent.mkdir(parents=True)
        touch.write_bytes(b'touch-blob')
        self.param.write_bytes(str(shared).encode() + NL)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((shared / FW_REL / 'cdsp.mbn').read_bytes(), MBN)
        self.assertEqual((shared / FW_REL / 'cdsp_dtb.mbn').read_bytes(), DTB)
        self.assertEqual(self.param.read_bytes(), str(shared).encode() + NL)
        self.assertEqual(gpu.read_bytes(), b'gpu-blob')
        self.assertEqual(touch.read_bytes(), b'touch-blob')
        created = sorted(p.relative_to(shared).as_posix() for p in shared.rglob('*') if p.is_file())
        expected = sorted([
            'qcom/adreno/a740_sqe.fw',
            'novatek/nt36532_fw.bin',
            (FW_REL / 'cdsp.mbn').as_posix(),
            (FW_REL / 'cdsp_dtb.mbn').as_posix(),
        ])
        self.assertEqual(created, expected)
        self.assertFalse(self.run_fw.exists())
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'start' + NL)

    def test_empty_path_sets_absolute_fallback(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        written = Path(self.param.read_text())
        self.assertTrue(written.is_absolute())
        self.assertEqual(written, self.run_fw)
        self.assertTrue((written / FW_REL / 'cdsp_dtb.mbn').is_file())

    def test_relative_fallback_rejected(self):
        result = self._run(TB323FU_RUN_FIRMWARE='relative-run-fw')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'relative-run-fw').exists())
        self.assertEqual(self.param.read_bytes(), b'')
        self.assertEqual((self.cdsp / 'state').read_bytes(), b'offline' + NL)


def _manifest():
    entries = {}
    text = (BUNDLE / 'SHA256SUMS').read_text(encoding='utf-8')
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, path = line.split('  ', 1)
        if len(digest) != 64:
            raise AssertionError('bad digest length: ' + line)
        entries[path] = digest
    return entries


def _command_lines(text):
    """Active logical command lines, shell continuations joined."""
    result = []
    pending = ''
    for raw in text.splitlines():
        if raw.rstrip().endswith('\\'):
            pending += raw.rstrip()[:-1] + ' '
            continue
        line = (pending + raw).strip()
        pending = ''
        if line and not line.startswith('#'):
            result.append(line)
    if pending.strip():
        result.append(pending.strip())
    return result


def _tokens(line):
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return []


def _copy_sources(text):
    """(build-context source, absolute destination) for every active COPY line."""
    copies = []
    for line in _command_lines(text):
        tokens = _tokens(line)
        if not tokens or tokens[0].upper() != 'COPY':
            continue
        args = [token for token in tokens[1:] if not token.startswith('--')]
        if len(args) < 2 or not args[-1].startswith('/'):
            continue
        for source in args[:-1]:
            if not source.startswith('/'):
                copies.append((source, args[-1]))
    return copies


def _installed_source(copies, path):
    """Build-context source that the COPY contract installs at `path`, or None."""
    for source, destination in copies:
        prefix = destination.rstrip('/')
        if not path.startswith(prefix + '/'):
            continue
        rest = path[len(prefix) + 1:]
        resolved = (REPO / source).resolve()
        if source.endswith('/') or resolved.is_dir():
            candidate = (resolved / rest).resolve() if rest else resolved
        elif rest:
            continue
        else:
            candidate = resolved
        if candidate.exists():
            return candidate
    return None


class ComputeBundleTest(unittest.TestCase):
    def test_manifest_covers_the_shipped_files(self):
        entries = _manifest()
        expected = {FW_REL.as_posix() + '/' + name for name in FIRMWARE_NAMES}
        self.assertEqual(set(entries), expected)
        for path, digest in entries.items():
            data = (BUNDLE / path).read_bytes()
            self.assertTrue(data, path)
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, path)

    def test_firmware_matches_expected_elf_architecture(self):
        for name, (machine, flags) in FIRMWARE_ELF.items():
            with self.subTest(name=name):
                data = (BUNDLE / FW_REL / name).read_bytes()
                self.assertGreaterEqual(len(data), ELF32.size, name)
                fields = ELF32.unpack(data[:ELF32.size])
                ident, _etype, elf_machine, _version, _entry, _phoff, _shoff, elf_flags = fields[:8]
                self.assertEqual(ident[:4], ELF_MAGIC, name)
                self.assertEqual(ident[4], 1, name)
                self.assertEqual(ident[5], 1, name)
                self.assertEqual(elf_machine, machine, name)
                self.assertEqual(elf_flags, flags, name)

    def test_manifest_check_rejects_corrupted_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / 'compute'
            shutil.copytree(BUNDLE, copied)
            command = ['bash', '-c', 'cd "$1" && sha256sum -c --strict SHA256SUMS', 'check', str(copied)]
            good = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            target = copied / FW_REL / 'cdsp.mbn'
            blob = bytearray(target.read_bytes())
            blob[0] ^= 0xff
            target.write_bytes(bytes(blob))
            bad = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)


class BuildIntegrationTest(unittest.TestCase):
    """Exercises the changed CDSP firmware packaging path only, not a full OS build."""

    @classmethod
    def setUpClass(cls):
        for path in (CONTAINERFILE, VENDOR_SCRIPT, HELPER, UNIT):
            if not path.is_file():
                raise AssertionError('missing file: ' + str(path))
        cls.copies = _copy_sources(CONTAINERFILE.read_text(encoding='utf-8'))
        lines = _command_lines(VENDOR_SCRIPT.read_text(encoding='utf-8'))
        cls.commands = [tokens for tokens in (_tokens(line) for line in lines) if tokens]

    def test_containerfile_places_bundle_helper_and_unit(self):
        for name in FIRMWARE_NAMES:
            installed = COPY_DEST + '/' + FW_REL.as_posix() + '/' + name
            with self.subTest(installed=installed):
                self.assertEqual(_installed_source(self.copies, installed),
                                 (BUNDLE / FW_REL / name).resolve())
        self.assertEqual(_installed_source(self.copies, '/system_files' + EXEC_START), HELPER.resolve())
        self.assertEqual(_installed_source(self.copies, '/system_files' + UNIT_DEST), UNIT.resolve())
        self.assertIn(['cp', '-a', '/ctx/system_files/.', '/'], self.commands)
        for path, digest in _manifest().items():
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256((BUNDLE / path).read_bytes()).hexdigest(), digest)

    def _vendor_block(self, bundle, image):
        lines = VENDOR_SCRIPT.read_text(encoding='utf-8').splitlines()
        start = next((i for i, line in enumerate(lines) if line.strip().startswith('cdsp_bundle=')), None)
        self.assertIsNotNone(start, 'cdsp_bundle= assignment missing from the vendor script')
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(VENDOR_MARKER)), len(lines))
        block = lines[start:end]
        targets = []
        for index, line in enumerate(block):
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
            if not match:
                continue
            try:
                value = shlex.split(match.group(2), comments=True)[0]
            except (IndexError, ValueError):
                continue
            if value.startswith('/'):
                targets.append((index, match.group(1)))
        self.assertEqual(len(targets), 2, 'block must hold the bundle source and image root')
        (bundle_index, bundle_name), (image_index, image_name) = targets
        block[bundle_index] = bundle_name + '=' + shlex.quote(str(bundle))
        block[image_index] = image_name + '=' + shlex.quote(str(image / INSTALL_ROOT.lstrip('/')))
        return '\n'.join(block) + '\n'

    def _run_vendor_block(self, image, bundle):
        block = self._vendor_block(bundle, image)
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', block],
                              cwd=str(image.parent), capture_output=True, text=True, timeout=300)

    def _installed_files(self, image):
        target = image / INSTALL_ROOT.lstrip('/')
        if not target.is_dir():
            return []
        return [path for path in target.rglob('*') if path.is_file()]

    def test_vendor_block_installs_verified_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'bundle'
            shutil.copytree(BUNDLE, bundle)
            image = root / 'image'
            good = self._run_vendor_block(image, bundle)
            self.assertEqual(good.returncode, 0, good.stderr)
            files = self._installed_files(image)
            self.assertEqual(sorted(path.name for path in files),
                             ['SHA256SUMS', 'cdsp.mbn', 'cdsp_dtb.mbn'])
            expected = {'SHA256SUMS': bundle / 'SHA256SUMS'}
            expected.update({name: bundle / FW_REL / name for name in FIRMWARE_NAMES})
            for path in files:
                with self.subTest(name=path.name):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o644, path)
                    self.assertEqual(path.read_bytes(), expected[path.name].read_bytes())
            target = bundle / FW_REL / 'cdsp.mbn'
            corrupted = bytearray(target.read_bytes())
            corrupted[len(corrupted) // 2] ^= 0xff
            target.write_bytes(bytes(corrupted))
            bad_image = root / 'image-corrupt'
            bad = self._run_vendor_block(bad_image, bundle)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(self._installed_files(bad_image), [])

    def test_vendor_script_enables_unit_and_sets_helper_mode(self):
        enables = [tokens for tokens in self.commands if tokens[:1] == ['systemctl'] and 'enable' in tokens]
        self.assertTrue(any(SERVICE in tokens for tokens in enables), 'unit is not enabled')
        modes = [tokens for tokens in self.commands if tokens[:1] == ['chmod'] and EXEC_START in tokens]
        self.assertTrue(any('0755' in tokens[1:-1] or '755' in tokens[1:-1] for tokens in modes),
                        'helper is not installed 0755')

    def test_unit_contract(self):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(UNIT.read_text(encoding='utf-8'))
        unit, service, install = parser['Unit'], parser['Service'], parser['Install']
        self.assertEqual(service['ExecStart'].split()[0], EXEC_START)
        self.assertEqual(service['Type'], 'oneshot')
        self.assertEqual(service['RemainAfterExit'], 'yes')
        self.assertNotIn('ExecStop', service)
        after = unit['After'].split()
        self.assertTrue(any('udev' in item for item in after), after)
        self.assertTrue(any('audio' in item for item in after), after)
        self.assertTrue(any('display-manager' in item for item in unit['Before'].split()))
        self.assertEqual(install['WantedBy'].split(), ['multi-user.target'])

    def test_helper_defaults_to_installed_bundle(self):
        text = HELPER.read_text(encoding='utf-8')
        self.assertIn(INSTALL_ROOT, text)
        self.assertIn('TB323FU_CDSP_FIRMWARE_DIR', text)
        self.assertNotIn('framewing', text.lower())


if __name__ == '__main__':
    unittest.main()
