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
import zlib
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
            env.bs = bs
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

    def mark_android_slot_unbootable(self, env, slot_index):
        """Set GPT attribute bit 55 on boot_a/boot_b and refresh both GPT CRCs."""
        bs = env.bs
        entries = 16
        array_blocks = (entries * 128 + bs - 1) // bs
        image = env.images[4]
        data = bytearray(image.read_bytes())
        for header_lba, array_lba in ((1, 2), (BLOCKS - 1, BLOCKS - 1 - array_blocks)):
            entry_offset = array_lba * bs + slot_index * 128
            attrs = int.from_bytes(data[entry_offset + 48:entry_offset + 56], 'little')
            data[entry_offset + 48:entry_offset + 56] = (
                attrs | (1 << 55)).to_bytes(8, 'little')
            table = data[array_lba * bs:array_lba * bs + entries * 128]
            data[header_lba * bs + 88:header_lba * bs + 92] = (
                zlib.crc32(table).to_bytes(4, 'little'))
            header = header_lba * bs
            data[header + 16:header + 20] = b'\x00' * 4
            data[header + 16:header + 20] = (
                zlib.crc32(data[header:header + 92]).to_bytes(4, 'little'))
        image.write_bytes(data)

    def test_identity_comparison_excludes_only_android_telemetry(self):
        base = {
            'model': 'TB322FC', 'actualSlot': 'a', 'inactiveSlot': 'b',
            'deployment': 'armada/' + ('a' * 64) + '.0',
            'recoveryPartUuid': '12345678-1234-5678-9abc-123456789abc',
            'ufsController': '/sys/devices/controller.ufs',
            'luns': [{'lun': 0}], 'selectedSlot': 'a',
            'bootableSlots': {'a': True, 'b': True},
        }
        telemetry = dict(
            base, selectedSlot='b', bootableSlots={'a': True, 'b': False})
        self.assertTrue(slots.same_physical_identity(base, telemetry))
        legacy = {key: value for key, value in base.items()
                  if key not in ('selectedSlot', 'bootableSlots')}
        self.assertTrue(slots.same_physical_identity(base, legacy))
        for field, value in (
                ('actualSlot', 'b'), ('inactiveSlot', 'a'),
                ('deployment', 'armada/' + ('b' * 64) + '.0'),
                ('recoveryPartUuid', 'changed'),
                ('ufsController', 'changed'), ('luns', [{'lun': 1}])):
            with self.subTest(field=field):
                changed = dict(base, **{field: value})
                self.assertFalse(slots.same_physical_identity(base, changed))

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

    def test_native_gpt_crc_and_misc_ota_rejections(self):
        for name, lun, offset, data in [
            ('gpt-crc', 4, 512 + 16, None),
            ('misc-ota', 0, 8 * 512 + 2048, b'\x01'),
            ('ab-pending-merging', 0, 8 * 512 + 32768,
             b'\x02\xb0\x0a\x74\x56\x03\x00' + b'\x00' * 57),
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
                with self.assertRaisesRegex(slots.PhysicalSlotsError, 'inspection failed'):
                    slots.inspect_physical_slots()
                self.assert_closed_unchanged(env, before)

    def test_selected_slot_differing_from_the_boot_witness_is_telemetry(self):
        for selected, booted in ((0, 'b'), (1, 'a')):
            with self.subTest(selected='ab'[selected], booted=booted), \
                    self.environment(current=selected) as env:
                env.context_mock.return_value = {
                    'actualSlot': booted,
                    'inactiveSlot': 'a' if booted == 'b' else 'b',
                    'deployment': 'test',
                }
                before = self.digests(env)
                report = slots.inspect_physical_slots()
                self.assertEqual(report['selectedSlot'], 'ab'[selected])
                self.assertEqual(report['actualSlot'], booted)
                self.assertEqual(report['inactiveSlot'],
                                 'a' if booted == 'b' else 'b')
                self.assertEqual(report['bootableSlots'], {'a': True, 'b': True})
                self.assert_closed_unchanged(env, before)

    def test_each_real_android_bootable_bit_is_reported_not_gated(self):
        for bs in (512, 4096):
            for selected in (0, 1):
                for slot_index in (0, 1):
                    with self.subTest(
                            bs=bs, selected='ab'[selected], slot='ab'[slot_index]), \
                            self.environment(bs, selected) as env:
                        self.mark_android_slot_unbootable(env, slot_index)
                        before = self.digests(env)
                        report = slots.inspect_physical_slots()
                        expected = {'a': True, 'b': True}
                        expected['ab'[slot_index]] = False
                        self.assertEqual(report['bootableSlots'], expected)
                        self.assertEqual(report['selectedSlot'], 'ab'[selected])
                        self.assertEqual(report['actualSlot'], 'ab'[selected])
                        self.assertEqual(report['inactiveSlot'], 'ba'[selected])
                        self.assert_closed_unchanged(env, before)

    def test_empty_secondary_boot_chain_is_reported_not_fabricated(self):
        # The probe reports the real Android bit instead of fabricating true.
        # Signed boot-material trust still refuses this empty source later;
        # only the slot probe stops using bootability as its trust gate.
        with self.environment() as env:
            with env.images[4].open('r+b') as stream:
                stream.seek(64 * env.bs)
                stream.write(b'\0' * 8)
            before = self.digests(env)
            report = slots.inspect_physical_slots()
            self.assertEqual(report['bootableSlots'], {'a': True, 'b': False})
            self.assert_closed_unchanged(env, before)

    def test_all_zero_gpt_input_is_still_refused(self):
        with self.environment() as env:
            env.images[4].write_bytes(b'\0' * env.images[4].stat().st_size)
            before = self.digests(env)
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'inspection failed'):
                slots.inspect_physical_slots()
            self.assert_closed_unchanged(env, before)

    def test_all_zero_bootability_is_still_refused(self):
        for bs in (512, 4096):
            with self.subTest(bs=bs), self.environment(bs) as env:
                self.mark_android_slot_unbootable(env, 0)
                self.mark_android_slot_unbootable(env, 1)
                before = self.digests(env)
                with self.assertRaisesRegex(slots.PhysicalSlotsError,
                                            'no bootable slot'):
                    slots.inspect_physical_slots()
                self.assert_closed_unchanged(env, before)

    def test_post_native_attribute_drift(self):
        for attribute in ('enabled', 'boot-id'):
            with self.subTest(attribute=attribute), self.environment() as env:
                real_read = slots._read_slot_state

                def read_then_mutate(fds, found):
                    result = real_read(fds, found)
                    if attribute == 'enabled':
                        self.write(env.controller / 'attributes/boot_lun_enabled', 2)
                    else:
                        self.write(env.nodes[1].parent.parent /
                                   'unit_descriptor/boot_lun_id', 0)
                    return result

                with mock.patch.object(slots, '_read_slot_state',
                                       side_effect=read_then_mutate):
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


    def test_normal_path_still_refuses_without_booted_context(self):
        with self.environment() as env:
            env.context_mock.side_effect = RuntimeError('no snapshot booted record')
            with self.assertRaisesRegex(slots.PhysicalSlotsError, 'invalid boot context'):
                slots.inspect_physical_slots()


if __name__ == '__main__':
    unittest.main()
