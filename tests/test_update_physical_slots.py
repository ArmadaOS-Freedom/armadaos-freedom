import ctypes
import hashlib
import json
import os
from pathlib import Path
import errno
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                 'system_files/usr/lib/armada'))
from updates import physical_slots as slots


REAL_LSTAT = os.lstat
REAL_FSTAT = os.fstat
REAL_OPEN = os.open
REAL_CLOSE = os.close
REAL_READ = os.read
BLOCKS = 256


def changed_stat(info, *, mode=None, size=None, rdev=None):
    attrs = {name: getattr(info, name) for name in dir(info)
             if name.startswith('st_')}
    if mode is not None:
        attrs['st_mode'] = mode
    if size is not None:
        attrs['st_size'] = size
    if rdev is not None:
        attrs['st_rdev'] = rdev
    return SimpleNamespace(**attrs)


class PhysicalSlotsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.kshim = cls.root / 'devices/lenovo-tb323fu/boot/kshim-success-source'
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        cls.helper = base / 'update_ab_fixture'
        cls.library = base / 'libarmada-update-ab.so'
        common = [
            'cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L',
            '-D_FILE_OFFSET_BITS=64', '-Wall', '-Wextra', '-Werror',
            '-I', str(cls.kshim / 'src/include'),
        ]
        core = [str(cls.kshim / 'src/core/storage_ab.c'),
                str(cls.kshim / 'src/core/storage_block.c')]
        subprocess.run(common + ['-I', str(cls.kshim / 'tests'),
                       str(cls.root / 'tests/update_ab_fixture.c')] + core +
                       ['-o', str(cls.helper)], check=True)
        subprocess.run(common + ['-fPIC', '-shared',
                       str(cls.root / 'build_files/update_ab_inspect.c'),
                       str(cls.root / 'build_files/update_ufs_boot.c')] + core +
                       ['-o', str(cls.library)], check=True)
        if not cls.library.is_file():
            raise AssertionError(f'missing library: {cls.library}')

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding='ascii')

    @contextmanager
    def environment(self, bs=512, current=0, board=1, other=False):
        with tempfile.TemporaryDirectory(dir=self.tmp.name) as directory, ExitStack() as stack:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()
            subprocess.run([str(self.helper), str(images), str(bs), str(current)], check=True)
            env = SimpleNamespace(images=[images / f'lun{i}.img' for i in range(6)])
            env.sysfs = root / 'sys'
            env.class_block = env.sysfs / 'class/block'
            env.dev_block = root / 'dev/block'
            env.controller = env.sysfs / 'devices/controller.ufs'
            env.nodes = []
            env.rdevs = []
            env.mapping = {}
            self.write(env.controller / 'attributes/boot_lun_enabled', current + 1)
            for lun in range(6):
                controller = env.controller
                if other and lun == 5:
                    controller = env.sysfs / 'devices/other.ufs'
                node = (controller / 'host0/target0:0:0' /
                        f'0:0:0:{lun}/block/sd{chr(97 + lun)}')
                self.write(node / 'dev', f'8:{16 * lun}')
                self.write(node / 'queue/logical_block_size', bs)
                self.write(node / 'size', BLOCKS * bs // 512)
                self.write(node.parent.parent / 'unit_descriptor/boot_lun_id',
                           (0, 1, 2, 0, 0, 0)[lun])
                rdev = os.makedev(8, 16 * lun)
                env.nodes.append(node)
                env.rdevs.append(rdev)
                env.mapping[rdev] = node
                env.class_block.mkdir(parents=True, exist_ok=True)
                (env.class_block / node.name).symlink_to(node)
                env.dev_block.mkdir(parents=True, exist_ok=True)
                (env.dev_block / f'8:{16 * lun}').symlink_to(env.images[lun])
            recovery_rdev = os.makedev(8, 100)
            recovery_node = env.nodes[0] / 'sda1'
            self.write(recovery_node / 'partition', '1')
            (env.class_block / 'sda1').symlink_to(recovery_node)
            env.mapping[recovery_rdev] = recovery_node
            env.recovery = ('recovery', 'recovery-uuid', board, recovery_rdev)
            env.context = {'actualSlot': 'ab'[current],
                           'inactiveSlot': 'ba'[current], 'deployment': 'test'}
            identities = {}
            for lun, image in enumerate(env.images):
                info = REAL_LSTAT(image)
                identities[(info.st_dev, info.st_ino)] = env.rdevs[lun]
            by_path = dict(zip(env.images, env.rdevs))
            env.opened, env.closed = [], []

            def fake_lstat(path, *args, **kwargs):
                info = REAL_LSTAT(path, *args, **kwargs)
                rdev = by_path.get(Path(path))
                if rdev is None:
                    return info
                return changed_stat(info, mode=stat.S_IFBLK | 0o600, rdev=rdev)

            def fake_fstat(fd):
                info = REAL_FSTAT(fd)
                rdev = identities.get((info.st_dev, info.st_ino))
                if rdev is None:
                    return info
                return changed_stat(info, mode=stat.S_IFBLK | 0o600, rdev=rdev)

            def tracked_open(path, *args, **kwargs):
                fd = REAL_OPEN(path, *args, **kwargs)
                if Path(path) in by_path:
                    env.opened.append(fd)
                return fd

            def tracked_close(fd):
                env.closed.append(fd)
                return REAL_CLOSE(fd)

            env.fake_fstat = fake_fstat
            for target, attribute, replacement in [
                (slots, 'SYS_CLASS_BLOCK', env.class_block),
                (slots, 'DEV_BLOCK', env.dev_block),
                (slots, 'LIBRARY_PATH', self.library),
            ]:
                stack.enter_context(mock.patch.object(target, attribute, replacement))
            stack.enter_context(mock.patch.object(slots.os, 'lstat', side_effect=fake_lstat))
            stack.enter_context(mock.patch.object(slots.os, 'fstat', side_effect=fake_fstat))
            stack.enter_context(mock.patch.object(slots.os, 'open', side_effect=tracked_open))
            stack.enter_context(mock.patch.object(slots.os, 'close', side_effect=tracked_close))
            stack.enter_context(mock.patch.object(slots, '_sysfs_block_node',
                                                  side_effect=lambda r: env.mapping[r]))
            stack.enter_context(mock.patch.object(slots, 'locate_recovery_partition',
                                                  return_value=env.recovery))
            stack.enter_context(mock.patch.object(slots.armada_os, 'snapshot',
                                                  return_value={'booted': {'fixture': True}}))
            env.context_mock = stack.enter_context(mock.patch.object(
                slots.boot_context, 'read_boot_context', return_value=env.context))
            yield env

    def digests(self, env):
        return [hashlib.sha256(path.read_bytes()).hexdigest() for path in env.images]

    def assert_closed_unchanged(self, env, before):
        self.assertEqual(self.digests(env), before)
        self.assertTrue(set(env.opened).issubset(set(env.closed)))
        for fd in env.opened:
            with self.assertRaises(OSError) as ctx:
                REAL_FSTAT(fd)
            self.assertEqual(ctx.exception.errno, errno.EBADF)

    def test_valid_matrix_report_native_geometry_and_read_only(self):
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), \
                            self.environment(bs, current, board) as env:
                        before = self.digests(env)
                        report = slots.inspect_physical_slots()
                        json.dumps(report)
                        self.assertEqual(report['model'], {1: 'TB322FC', 2: 'TB323FU'}[board])
                        self.assertEqual(report['selectedSlot'], 'ab'[current])
                        self.assertEqual(report['actualSlot'], 'ab'[current])
                        self.assertEqual(report['inactiveSlot'], 'ba'[current])
                        self.assertEqual(report['bootableSlots'], {'a': True, 'b': True})
                        self.assertEqual([x['blockSize'] for x in report['luns']], [bs] * 6)
                        self.assertEqual(len(env.opened), 6)
                        self.assert_closed_unchanged(env, before)

    def test_unsupported_board_fails_before_lun_open(self):
        with self.environment(board=3) as env:
            before = self.digests(env)
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'unsupported board id 3'):
                slots.inspect_physical_slots()
            self.assertEqual(env.opened, [])
            self.assertEqual(self.digests(env), before)

    def test_inventory_rejections(self):
        for mutation in ('missing', 'duplicate', 'invalid-dev', 'wrong-target', 'other'):
            with self.subTest(mutation=mutation), self.environment(other=mutation == 'other') as env:
                if mutation == 'missing':
                    (env.class_block / 'sde').unlink()
                elif mutation == 'duplicate':
                    (env.class_block / 'sdg').symlink_to(env.nodes[5])
                elif mutation == 'invalid-dev':
                    self.write(env.nodes[2] / 'dev', '8:32junk')
                elif mutation == 'wrong-target':
                    (env.dev_block / '8:80').unlink()
                    (env.dev_block / '8:80').symlink_to(env.images[4])
                before = self.digests(env)
                with self.assertRaises(slots.PhysicalSlotsError):
                    slots.inspect_physical_slots()
                self.assertEqual(env.opened, [])
                self.assertEqual(self.digests(env), before)

    def test_native_gpt_crc_misc_ota_and_empty_init_boot_rejections(self):
        for name, lun, offset, data in [
            ('gpt-crc', 4, 512 + 16, None),
            ('misc-ota', 0, 8 * 512 + 2048, b'\x01'),
            ('ab-pending-merging', 0, 8 * 512 + 32768,
             b'\x02\xb0\x0a\x74\x56\x03\x00' + b'\x00' * 57),
            ('empty-init-boot-b', 4, 64 * 512, b'\0' * 8),
        ]:
            with self.subTest(name=name), self.environment() as env:
                with env.images[lun].open('r+b') as stream:
                    stream.seek(offset)
                    if data is None:
                        old = stream.read(1)
                        stream.seek(offset)
                        data = bytes([old[0] ^ 1])
                    stream.write(data)
                before = self.digests(env)
                with self.assertRaisesRegex(slots.PhysicalSlotsError,
                                            'inspection failed|both slots must be bootable'):
                    slots.inspect_physical_slots()
                self.assert_closed_unchanged(env, before)

    def test_boot_witness_and_post_native_attribute_drift(self):
        with self.environment() as env:
            env.context_mock.return_value = dict(env.context, actualSlot='b', inactiveSlot='a')
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'actual boot != next selected'):
                slots.inspect_physical_slots()
        for attribute in ('enabled', 'boot-id'):
            with self.subTest(attribute=attribute), self.environment() as env:
                real_run = slots._run_inspection

                def run_then_mutate(fds, found):
                    result = real_run(fds, found)
                    if attribute == 'enabled':
                        self.write(env.controller / 'attributes/boot_lun_enabled', 2)
                    else:
                        self.write(env.nodes[1].parent.parent /
                                   'unit_descriptor/boot_lun_id', 0)
                    return result

                with mock.patch.object(slots, '_run_inspection', side_effect=run_then_mutate):
                    with self.assertRaisesRegex(slots.PhysicalSlotsError, 'drifted during check'):
                        slots.inspect_physical_slots()

    def test_context_drift_and_third_open_failure_close_prior_fds(self):
        with self.environment() as env:
            env.context_mock.side_effect = [env.context,
                                            dict(env.context, deployment='changed')]
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'boot context drifted'):
                slots.inspect_physical_slots()
        with self.environment() as env:
            before = self.digests(env)
            normal_open = os.open

            def fail_third(path, *args, **kwargs):
                if Path(path) == env.images[2]:
                    raise OSError('injected third-open failure')
                return normal_open(path, *args, **kwargs)

            with mock.patch.object(slots.os, 'open', side_effect=fail_third):
                with self.assertRaisesRegex(slots.PhysicalSlotsError, 'cannot open'):
                    slots.inspect_physical_slots()
            self.assertEqual(len(env.opened), 2)
            self.assert_closed_unchanged(env, before)

    def test_sysfs_integer_geometry_limits_and_bounded_short_reads(self):
        with self.environment() as env:
            size = env.nodes[0] / 'size'
            for text, expected in [('0x10', 16), ('00017', 17)]:
                self.write(size, text)
                self.assertEqual(slots._sysfs_int(size), expected)
            for text in ('1_0', '+1', '-1', '1' * 65):
                self.write(size, text)
                with self.assertRaises(slots.PhysicalSlotsError):
                    slots._sysfs_int(size)
            self.write(size, 0)
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'invalid size'):
                slots._geometry(env.nodes[0])
            self.write(env.nodes[0] / 'queue/logical_block_size', 513)
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'unsupported logical'):
                slots._geometry(env.nodes[0])
            self.write(size, 2048)

            def short_read(fd, count):
                return REAL_READ(fd, min(count, 2))

            def short_content_fstat(fd):
                info = env.fake_fstat(fd)
                try:
                    path = Path(f'/proc/self/fd/{fd}').resolve()
                except OSError:
                    return info
                return changed_stat(info, size=4096) if path == size else info

            with mock.patch.object(slots.os, 'read', side_effect=short_read), \
                    mock.patch.object(slots.os, 'fstat', side_effect=short_content_fstat):
                self.assertEqual(slots._sysfs_read(size), '2048')

    def witness_bytes(self, slot):
        return struct.pack('>4I', 1, 1, slot, 0)

    def test_raw_witness_decoder(self):
        self.assertEqual(slots.boot_context._decode_witness(self.witness_bytes(1)), 1)
        for bad in (struct.pack('>4I', 2, 1, 0, 0),
                    struct.pack('>4I', 1, 1, 0, 1),
                    struct.pack('>4I', 1, 1, 2, 0), b''):
            with self.assertRaises(slots.boot_context.BootContextError):
                slots.boot_context._decode_witness(bad)

    def test_recovery_slots_valid_matrix_without_snapshot_or_deployment(self):
        for current in (0, 1):
            for bs in (512, 4096):
                with self.subTest(current=current, bs=bs), \
                        self.environment(bs, current) as env:
                    before = self.digests(env)
                    with mock.patch.object(slots.boot_context, '_read_regular',
                                           return_value=self.witness_bytes(current)), \
                            mock.patch.object(slots.armada_os, 'snapshot',
                                              side_effect=AssertionError('no snapshot in RAM')):
                        report = slots.inspect_recovery_slots()
                    json.dumps(report)
                    self.assertEqual(report['phase'], 'recovery_slots_checked')
                    self.assertEqual(report['actualSlot'], 'ab'[current])
                    self.assertEqual(report['inactiveSlot'], 'ba'[current])
                    self.assertEqual(report['selectedSlot'], 'ab'[current])
                    self.assertEqual(report['bootableSlots'], {'a': True, 'b': True})
                    self.assertEqual([x['blockSize'] for x in report['luns']], [bs] * 6)
                    self.assertNotIn('deployment', report)
                    self.assertNotIn('booted', report)
                    self.assertFalse(env.context_mock.called)
                    self.assertEqual(len(env.opened), 6)
                    self.assert_closed_unchanged(env, before)

    def test_recovery_missing_or_corrupt_witness_rejected_before_open(self):
        cases = [('missing', OSError('witness gone')),
                 ('short', b'\x00' * 15),
                 ('bad-header', struct.pack('>4I', 2, 1, 0, 0))]
        for name, witness in cases:
            with self.subTest(name=name), self.environment() as env:
                before = self.digests(env)

                def fake_read(path, limit):
                    if isinstance(witness, OSError):
                        raise witness
                    return witness

                with mock.patch.object(slots.boot_context, '_read_regular',
                                       side_effect=fake_read), \
                        mock.patch.object(slots.armada_os, 'snapshot',
                                          side_effect=AssertionError('no snapshot in RAM')):
                    with self.assertRaisesRegex(slots.PhysicalSlotsError,
                                                'invalid boot context'):
                        slots.inspect_recovery_slots()
                self.assertEqual(env.opened, [])
                self.assertEqual(self.digests(env), before)

    def test_recovery_witness_drift_after_native_rejected_fds_closed(self):
        with self.environment() as env:
            before = self.digests(env)
            fake_read = mock.Mock(return_value=self.witness_bytes(0))
            real_run = slots._read_slot_flags

            def flip_witness(fds, found):
                result = real_run(fds, found)
                fake_read.return_value = self.witness_bytes(1)
                return result

            with mock.patch.object(slots, '_read_slot_flags', side_effect=flip_witness), \
                    mock.patch.object(slots.boot_context, '_read_regular',
                                      side_effect=fake_read), \
                    mock.patch.object(slots.armada_os, 'snapshot',
                                      side_effect=AssertionError('no snapshot in RAM')):
                with self.assertRaisesRegex(slots.PhysicalSlotsError,
                                            'boot context drifted'):
                    slots.inspect_recovery_slots()
            self.assert_closed_unchanged(env, before)

    def test_recovery_reports_unbootable_slot_after_normal_rejection(self):
        with self.environment() as env:
            with env.images[4].open('r+b') as stream:
                stream.seek(64 * 512)
                stream.write(b'\0' * 8)
            before = self.digests(env)
            with self.assertRaisesRegex(slots.PhysicalSlotsError,
                                        'both slots must be bootable'):
                slots.inspect_physical_slots()
            self.assert_closed_unchanged(env, before)
            env.opened.clear()
            env.closed.clear()
            env.context_mock.reset_mock()
            with mock.patch.object(slots.boot_context, '_read_regular',
                                   return_value=self.witness_bytes(0)), \
                    mock.patch.object(slots.armada_os, 'snapshot',
                                      side_effect=AssertionError('no snapshot in RAM')):
                report = slots.inspect_recovery_slots()
            self.assertEqual(report['actualSlot'], 'a')
            self.assertEqual(report['selectedSlot'], 'a')
            self.assertEqual(report['bootableSlots'], {'a': True, 'b': False})
            self.assertFalse(env.context_mock.called)
            self.assert_closed_unchanged(env, before)

    def test_recovery_reports_actual_b_while_ufs_selects_a(self):
        with self.environment() as env:
            env.context_mock.return_value = dict(env.context, actualSlot='b', inactiveSlot='a')
            before = self.digests(env)
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'actual boot != next selected'):
                slots.inspect_physical_slots()
            self.assert_closed_unchanged(env, before)
            env.opened.clear()
            env.closed.clear()
            env.context_mock.reset_mock()
            with mock.patch.object(slots.boot_context, '_read_regular',
                                   return_value=self.witness_bytes(1)), \
                    mock.patch.object(slots.armada_os, 'snapshot',
                                      side_effect=AssertionError('no snapshot in RAM')):
                report = slots.inspect_recovery_slots()
            self.assertEqual(report['actualSlot'], 'b')
            self.assertEqual(report['selectedSlot'], 'a')
            self.assertEqual(report['inactiveSlot'], 'a')
            self.assertEqual(report['bootableSlots'], {'a': True, 'b': True})
            self.assertFalse(env.context_mock.called)
            self.assert_closed_unchanged(env, before)

    def test_normal_path_still_refuses_without_booted_context(self):
        with self.environment() as env:
            env.context_mock.side_effect = RuntimeError('no snapshot booted record')
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'invalid boot context'):
                slots.inspect_physical_slots()


if __name__ == '__main__':
    unittest.main()
