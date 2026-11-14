import copy
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'system_files/usr/lib/armada', ROOT / 'tests'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import test_update_physical_slots as physical_fixture
from updates import boot_sources as sources
from updates import boot_trust as trust_mod
from updates import boot_writer as writer
from updates import physical_slots as slots

REAL_OPEN = physical_fixture.REAL_OPEN
REAL_FSTAT = physical_fixture.REAL_FSTAT
REAL_PWRITE = os.pwrite
REAL_PREAD = os.pread
REAL_FSYNC = os.fsync
ACTIVE_DEP = f"armada/{'a' * 64}.0"
TARGET_DEP = f"armada/{'b' * 64}.0"
TX_UUID = '12345678-1234-5678-9abc-def012345678'


class BootWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        physical_fixture.PhysicalSlotsTests.setUpClass()
        cls.addClassCleanup(physical_fixture.PhysicalSlotsTests.doClassCleanups)
        base = Path(physical_fixture.PhysicalSlotsTests.tmp.name)
        kshim = physical_fixture.PhysicalSlotsTests.kshim
        cls.writer_helper = base / 'update_boot_writer_fixture'
        subprocess.run([
            'cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L',
            '-D_FILE_OFFSET_BITS=64', '-Wall', '-Wextra', '-Werror',
            '-I', str(kshim / 'src/include'), '-I', str(kshim / 'tests'),
            str(ROOT / 'tests/update_boot_writer_fixture.c'),
            str(kshim / 'src/core/storage_ab.c'),
            str(kshim / 'src/core/storage_block.c'),
            '-o', str(cls.writer_helper),
        ], check=True)

    def setUp(self):
        self.fixture = physical_fixture.PhysicalSlotsTests()
        self.fixture.helper = self.writer_helper

    @staticmethod
    def _assert_all_closed(env):
        assert set(env.opened).issubset(set(env.closed))
        for fd in set(env.opened):
            try:
                REAL_FSTAT(fd)
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                raise AssertionError(f'fd {fd} remained open')

    def _prepare(self, stack, *, bs=512, current=0, board=1,
                 mode='aosp-resign', payload_match=True):
        env = stack.enter_context(
            self.fixture.environment(bs=bs, current=current, board=board))
        env.context['deployment'] = ACTIVE_DEP
        stack.enter_context(
            mock.patch.object(writer, 'read_boot_lock', return_value='unlocked'))
        work = env.images[0].parent.parent
        cap_dir = work / 'captured'
        extras = ['recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b']
        if board == 2:
            extras.append('efisp')
        physical = slots.inspect_physical_slots()
        snapshot = sources.capture_boot_sources(physical, cap_dir, extra_labels=extras)
        self._assert_all_closed(env)
        env.opened.clear()
        env.closed.clear()

        target = physical['inactiveSlot']
        orig_claim = snapshot['slots'][target]['boot']
        orig_bytes = Path(orig_claim['path']).read_bytes()
        cand_arr = bytearray(orig_bytes)
        cand_arr[128] ^= 0x5A
        cand_arr[-1] ^= 0xA5
        cand_bytes = bytes(cand_arr)
        cand_path = work / f'candidate_boot_{target}.img'
        cand_path.write_bytes(cand_bytes)
        cand_sha = hashlib.sha256(cand_bytes).hexdigest()
        cand_claim = {
            'path': str(cand_path),
            'bytes': len(cand_bytes),
            'sha256': cand_sha,
        }
        trust_report = {
            'schemaVersion': 1,
            'phase': 'boot_trust_checked',
            'physical': copy.deepcopy(physical),
            'lockState': 'unlocked',
            'mode': mode,
            'sources': copy.deepcopy(snapshot),
            'trust': {
                target: {
                    'maxRawBytes': 32768,
                    'material': {'baseImageSha256': '1' * 64},
                    'rootPublicKeySha256': '2' * 64,
                    'bootPublicKeySha256': '3' * 64,
                    'metadataSha256': '4' * 64,
                },
            },
        }
        candidate_report = {
            'schemaVersion': 1,
            'phase': 'candidate_prepared',
            'slot': target,
            'model': physical['model'],
            'mode': mode,
            'transactionUuid': TX_UUID,
            'deployment': TARGET_DEP,
            'maxRawBytes': 32768,
            'candidate': copy.deepcopy(cand_claim),
            'sourceBootSha256': orig_claim['sha256'],
            'baseImageSha256': '1' * 64,
            'rootPublicKeySha256': '2' * 64,
            'bootPublicKeySha256': '3' * 64,
            'metadataSha256': '4' * 64,
            'payloadDigestMatchesSignedMetadata': payload_match,
        }
        part = snapshot['partitions'][f'boot_{target}']
        extent = (part['lun'], part['firstLba'] * bs, len(cand_bytes))
        captured_files = {
            p: p.read_bytes() for p in cap_dir.rglob('*') if p.is_file()
        }
        return (
            env, trust_report, candidate_report,
            [p.read_bytes() for p in env.images],
            captured_files, orig_bytes, cand_bytes, extent,
        )

    def test_matrix_writes_only_inactive_boot_extent(self):
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), \
                            contextlib_stack() as stack:
                        (env, trust, cand, disk_before, cap_before,
                         orig_bytes, cand_bytes, extent) = self._prepare(
                            stack, bs=bs, current=current, board=board)
                        lun, offset, size = extent
                        by_image = set(env.images)
                        open_calls = []

                        def spy_open(path, flags, *args, **kwargs):
                            fd = REAL_OPEN(path, flags, *args, **kwargs)
                            p = Path(path)
                            if p in by_image:
                                env.opened.append(fd)
                                open_calls.append((p, flags))
                            return fd

                        stack.enter_context(
                            mock.patch.object(slots.os, 'open', side_effect=spy_open))
                        res = writer.write_inactive_boot(trust, cand)
                        self._assert_all_closed(env)

                        rw_calls = [
                            (p, fl) for p, fl in open_calls
                            if (fl & os.O_ACCMODE) != os.O_RDONLY
                        ]
                        self.assertEqual(len(rw_calls), 1)
                        self.assertEqual(rw_calls[0][0], env.images[4])
                        self.assertEqual(rw_calls[0][1] & os.O_ACCMODE, os.O_RDWR)
                        self.assertEqual(lun, 4)

                        expected_disks = list(disk_before)
                        lun4 = bytearray(expected_disks[4])
                        self.assertEqual(bytes(lun4[offset:offset + size]), orig_bytes)
                        lun4[offset:offset + size] = cand_bytes
                        expected_disks[4] = bytes(lun4)
                        self.assertEqual(
                            [p.read_bytes() for p in env.images], expected_disks)
                        self.assertEqual(
                            {p: p.read_bytes() for p in cap_before}, cap_before)
                        self.assertEqual(slots.inspect_physical_slots(), trust['physical'])
                        self._assert_all_closed(env)
                        self.assertEqual(res, {
                            'schemaVersion': 1,
                            'phase': 'inactive_boot_written',
                            'model': 'TB322FC' if board == 1 else 'TB323FU',
                            'fromSlot': 'ab'[current],
                            'targetSlot': 'ba'[current],
                            'transactionUuid': TX_UUID,
                            'deployment': TARGET_DEP,
                            'candidate': cand['candidate'],
                            'sourceBootSha256': hashlib.sha256(orig_bytes).hexdigest(),
                            'bytesWritten': len(cand_bytes),
                            'readbackSha256': cand['candidate']['sha256'],
                            'physical': trust['physical'],
                        })

    def test_mode_payload_digest_contract(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, cand_bytes, extent) = self._prepare(
                stack, mode='gbl-preserve-metadata', payload_match=False)
            res = writer.write_inactive_boot(trust, cand)
            self._assert_all_closed(env)
            self.assertEqual(res['readbackSha256'], cand['candidate']['sha256'])
            lun, offset, size = extent
            self.assertEqual(
                env.images[lun].read_bytes()[offset:offset + size], cand_bytes)

        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(
                stack, mode='aosp-resign', payload_match=False)
            pwrite_spy = stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(writer.BootWriteError, 'payload digest') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            pwrite_spy.assert_not_called()
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)

    def test_gbl_metadata_hash_bound_to_trusted_target_before_write(self):
        cases = [
            ('candidate_missing',
             lambda t, c: c.pop('metadataSha256'),
             'candidate metadataSha256 is missing or invalid'),
            ('candidate_non_hex',
             lambda t, c: c.__setitem__('metadataSha256', 'z' * 64),
             'candidate metadataSha256 is missing or invalid'),
            ('candidate_short',
             lambda t, c: c.__setitem__('metadataSha256', 'f' * 63),
             'candidate metadataSha256 is missing or invalid'),
            ('candidate_mismatch',
             lambda t, c: c.__setitem__('metadataSha256', 'f' * 64),
             'candidate metadataSha256 mismatch'),
            ('trusted_missing',
             lambda t, c: t['trust'][t['physical']['inactiveSlot']].pop('metadataSha256'),
             'authenticated metadataSha256 is invalid'),
            ('trusted_non_hex',
             lambda t, c: t['trust'][t['physical']['inactiveSlot']].__setitem__(
                 'metadataSha256', 'g' * 64),
             'authenticated metadataSha256 is invalid'),
        ]
        for name, mutate, pattern in cases:
            with self.subTest(case=name), contextlib_stack() as stack:
                (env, trust, cand, disk_before, _, _, _, _) = self._prepare(
                    stack, mode='gbl-preserve-metadata')
                mutate(trust, cand)
                pwrite_spy = stack.enter_context(
                    mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))
                with self.assertRaisesRegex(writer.BootWriteError, pattern) as ctx:
                    writer.write_inactive_boot(trust, cand)
                self.assertEqual(ctx.exception.restore_status, 'not_needed')
                pwrite_spy.assert_not_called()
                self.assertEqual([p.read_bytes() for p in env.images], disk_before)
                self._assert_all_closed(env)

        # aosp-resign keeps its existing rule: metadataSha256 is not consulted.
        with contextlib_stack() as stack:
            (env, trust, cand, _, _, _, cand_bytes, extent) = self._prepare(
                stack, mode='aosp-resign')
            trust['trust'][trust['physical']['inactiveSlot']]['metadataSha256'] = 'a' * 64
            cand['metadataSha256'] = 'b' * 64
            res = writer.write_inactive_boot(trust, cand)
            self._assert_all_closed(env)
            lun, offset, size = extent
            self.assertEqual(res['readbackSha256'], cand['candidate']['sha256'])
            self.assertEqual(
                env.images[lun].read_bytes()[offset:offset + size], cand_bytes)

    def test_prewrite_binding_and_schema_rejections(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            pwrite_spy = stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))

            cases = [
                (lambda t, c: c.__setitem__('slot', t['physical']['actualSlot']),
                 'target, model, or mode'),
                (lambda t, c: c.__setitem__('deployment', ACTIVE_DEP),
                 'deployment is invalid or not inactive'),
                (lambda t, c: c.__setitem__('sourceBootSha256', 'f' * 64),
                 'source digest is not captured inactive boot'),
                (lambda t, c: c.__setitem__('rootPublicKeySha256', '9' * 64),
                 'rootPublicKeySha256 mismatch'),
                (lambda t, c: c.__setitem__('maxRawBytes', 65536),
                 'raw-byte budget exceeds'),
                (lambda t, c: c.__setitem__(
                    'transactionUuid', '00000000-0000-0000-0000-000000000000'),
                 'transactionUuid must be canonical and nonzero'),
                (lambda t, c: t.__setitem__('schemaVersion', True),
                 'invalid boot trust report header'),
                (lambda t, c: c.__setitem__('schemaVersion', True),
                 'invalid candidate report header'),
            ]
            for mutate, pattern in cases:
                with self.subTest(pattern=pattern):
                    t_copy = copy.deepcopy(trust)
                    c_copy = copy.deepcopy(cand)
                    mutate(t_copy, c_copy)
                    with self.assertRaisesRegex(writer.BootWriteError, pattern) as ctx:
                        writer.write_inactive_boot(t_copy, c_copy)
                    self.assertEqual(ctx.exception.restore_status, 'not_needed')
                    pwrite_spy.assert_not_called()
                    self.assertEqual([p.read_bytes() for p in env.images], disk_before)
                    self._assert_all_closed(env)

    def test_late_context_drift_on_rw_open_aborts_before_pwrite(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            by_image = set(env.images)

            def drift_on_rw_open(path, flags, *args, **kwargs):
                fd = REAL_OPEN(path, flags, *args, **kwargs)
                if Path(path) in by_image:
                    env.opened.append(fd)
                    if (flags & os.O_ACCMODE) == os.O_RDWR:
                        env.context['deployment'] = f"armada/{'c' * 64}.0"
                return fd

            stack.enter_context(
                mock.patch.object(slots.os, 'open', side_effect=drift_on_rw_open))
            pwrite_spy = stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(
                    writer.BootWriteError, 'physical slot report drifted') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            pwrite_spy.assert_not_called()
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)

    def test_short_positive_pwrite_advances_within_inactive_extent(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, cand_bytes, extent) = self._prepare(stack)
            lun, offset, size = extent
            write_spans = []

            def short_pwrite(fd, data, pos):
                write_spans.append((pos, min(97, len(data))))
                return REAL_PWRITE(fd, data[:97], pos)

            stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', side_effect=short_pwrite))
            res = writer.write_inactive_boot(trust, cand)
            self._assert_all_closed(env)
            self.assertGreater(len(write_spans), len(cand_bytes) // 97)
            for pos, length in write_spans:
                self.assertGreaterEqual(pos, offset)
                self.assertLessEqual(pos + length, offset + size)
            self.assertEqual(res['bytesWritten'], size)
            self.assertEqual(
                env.images[lun].read_bytes()[offset:offset + size], cand_bytes)

    def test_partial_write_then_oserror_restores_original(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, orig_bytes, cand_bytes, extent) = (
                self._prepare(stack))
            lun, offset, size = extent
            half = size // 2
            self.assertGreater(half, 128)
            calls = []
            mid_disk = []

            def flaky_pwrite(fd, data, pos):
                idx = len(calls)
                calls.append((pos, len(data)))
                if idx == 0:
                    written = REAL_PWRITE(fd, data[:half], pos)
                    mid_disk.append(env.images[lun].read_bytes()[offset:offset + size])
                    return written
                if idx == 1:
                    raise OSError(errno.EIO, 'simulated mid-write failure')
                return REAL_PWRITE(fd, data, pos)

            stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', side_effect=flaky_pwrite))
            with self.assertRaisesRegex(
                    writer.BootWriteError, 'inactive boot restored') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual(len(mid_disk), 1)
            self.assertNotEqual(mid_disk[0], orig_bytes)
            self.assertEqual(mid_disk[0][:half], cand_bytes[:half])
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)

    def test_fsync_failure_restores_and_persistent_pwrite_failure_reports_failed(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            fsync_calls = []

            def fail_first_fsync(fd):
                fsync_calls.append(fd)
                if len(fsync_calls) == 1:
                    raise OSError(errno.EIO, 'fsync boom')
                return REAL_FSYNC(fd)

            stack.enter_context(
                mock.patch.object(writer.os, 'fsync', side_effect=fail_first_fsync))
            with self.assertRaisesRegex(
                    writer.BootWriteError, 'fsync boom; inactive boot restored') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)

        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, cand_bytes, extent) = self._prepare(stack)
            lun, offset, size = extent
            half = size // 2
            calls = []

            def persistent_fail_pwrite(fd, data, pos):
                calls.append(pos)
                if len(calls) == 1:
                    return REAL_PWRITE(fd, data[:half], pos)
                raise OSError(errno.EIO, 'persistent write failure')

            stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', side_effect=persistent_fail_pwrite))
            with self.assertRaisesRegex(
                    writer.BootWriteError, 'inactive boot restore failed') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'failed')
            self._assert_all_closed(env)
            for idx, img in enumerate(env.images):
                if idx != lun:
                    self.assertEqual(img.read_bytes(), disk_before[idx])
            lun_after = env.images[lun].read_bytes()
            self.assertEqual(lun_after[:offset], disk_before[lun][:offset])
            self.assertEqual(lun_after[offset + size:], disk_before[lun][offset + size:])
            self.assertEqual(lun_after[offset:offset + half], cand_bytes[:half])

    def test_corrupted_first_write_detected_by_real_readback_and_restored(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            calls = []

            def corrupting_pwrite(fd, data, pos):
                calls.append(pos)
                if len(calls) == 1:
                    bad = bytearray(data)
                    bad[200] ^= 0xFF
                    return REAL_PWRITE(fd, bytes(bad), pos)
                return REAL_PWRITE(fd, data, pos)

            stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', side_effect=corrupting_pwrite))
            with self.assertRaisesRegex(
                    writer.BootWriteError,
                    'inactive boot readback digest mismatch; inactive boot restored') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)

    def test_live_source_and_candidate_file_digest_drift_rejected_before_write(self):
        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            active = trust['physical']['actualSlot']
            part = trust['sources']['partitions'][f'vendor_boot_{active}']
            v_lun = part['lun']
            v_off = part['firstLba'] * 512
            mutated_lun = bytearray(disk_before[v_lun])
            mutated_lun[v_off + 128] ^= 0x33
            env.images[v_lun].write_bytes(bytes(mutated_lun))
            expected_disks = list(disk_before)
            expected_disks[v_lun] = bytes(mutated_lun)

            pwrite_spy = stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(
                    writer.BootWriteError,
                    f'live source digest mismatch for vendor_boot_{active}') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            pwrite_spy.assert_not_called()
            self.assertEqual([p.read_bytes() for p in env.images], expected_disks)
            self._assert_all_closed(env)

        with contextlib_stack() as stack:
            (env, trust, cand, disk_before, _, _, _, _) = self._prepare(stack)
            cand_path = Path(cand['candidate']['path'])
            tampered = bytearray(cand_path.read_bytes())
            tampered[64] ^= 0x11
            cand_path.write_bytes(bytes(tampered))
            pwrite_spy = stack.enter_context(
                mock.patch.object(writer.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(
                    writer.BootWriteError, 'candidate content mismatch') as ctx:
                writer.write_inactive_boot(trust, cand)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            pwrite_spy.assert_not_called()
            self.assertEqual([p.read_bytes() for p in env.images], disk_before)
            self._assert_all_closed(env)


def contextlib_stack():
    return physical_fixture.ExitStack()


if __name__ == '__main__':
    unittest.main()
