import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import test_update_physical_slots as fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] /
                 'system_files/usr/lib/armada'))
from updates import boot_sources as sources
from updates import physical_slots as slots


REAL_OPEN = os.open
REAL_CLOSE = os.close
REAL_PREAD = os.pread
REAL_PWRITE = os.pwrite
REAL_WRITE = os.write
REAL_LSEEK = os.lseek
REAL_FSTAT = os.fstat
REAL_FSYNC = os.fsync

REQUIRED = (
    'boot_a', 'vendor_boot_a', 'init_boot_a', 'abl_a', 'xbl_config_a',
    'vbmeta_a', 'boot_b', 'vendor_boot_b', 'init_boot_b', 'abl_b',
    'xbl_config_b', 'vbmeta_b',
)
GEOMETRY = {
    'boot_a': (4, 8), 'boot_b': (4, 16),
    'vendor_boot_a': (4, 24), 'vendor_boot_b': (4, 32),
    'vbmeta_a': (4, 40), 'vbmeta_b': (4, 48),
    'init_boot_a': (4, 56), 'init_boot_b': (4, 64),
    'abl_a': (1, 24), 'abl_b': (2, 24),
    'xbl_config_a': (1, 16), 'xbl_config_b': (2, 16),
    'efisp': (5, 8),
}
GPT_INDEX = {
    'boot_a': 0, 'boot_b': 1,
    'vendor_boot_a': 2, 'vendor_boot_b': 3,
    'vbmeta_a': 4, 'vbmeta_b': 5,
    'init_boot_a': 6, 'init_boot_b': 7,
    'xbl_config_a': 1, 'abl_a': 2,
    'xbl_config_b': 1, 'abl_b': 2,
    'efisp': 0,
}


class BootSourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.PhysicalSlotsTests.setUpClass()
        cls.addClassCleanup(fixture.PhysicalSlotsTests.doClassCleanups)

    def setUp(self):
        # Composition keeps PhysicalSlotsTests' own unittest methods out of this class.
        self.fixture = fixture.PhysicalSlotsTests()

    @staticmethod
    def output_for(env):
        out = (env.images[0].parent.parent / 'capture').resolve()
        if out.exists() or out.is_symlink():
            raise AssertionError('capture output unexpectedly already exists')
        return out

    def assert_no_manifest(self, out):
        self.assertFalse((out / 'source-snapshot.json').exists())

    def test_matrix_native_capture_manifest_bytes_uuids_and_read_only_opens(self):
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), \
                            self.fixture.environment(bs, current, board) as env:
                        before = self.fixture.digests(env)
                        expected = slots.inspect_physical_slots()
                        out = self.output_for(env)
                        source_opens = []
                        previous_open = slots.os.open

                        def recording_open(path, *args, **kwargs):
                            if Path(path) in env.images:
                                source_opens.append((Path(path), args[0] if args else 0))
                            return previous_open(path, *args, **kwargs)

                        with mock.patch.object(slots.os, 'open', side_effect=recording_open):
                            returned = sources.capture_boot_sources(
                                expected, out, extra_labels=('efisp',))
                        self.assertEqual(
                            set(returned),
                            {'schemaVersion', 'phase', 'physical', 'slots',
                             'extraFiles', 'partitions'})
                        self.assertEqual(returned['schemaVersion'], 1)
                        self.assertEqual(returned['phase'], 'boot_sources_captured')
                        self.assertEqual(returned['physical'], expected)
                        self.assertEqual(
                            json.loads((out / 'source-snapshot.json').read_text('ascii')),
                            returned)
                        slot_keys = {
                            'boot', 'vendorBoot', 'initBoot', 'abl', 'xblConfig', 'vbmeta'
                        }
                        self.assertEqual(set(returned['slots']), {'a', 'b'})
                        for slot in ('a', 'b'):
                            self.assertEqual(set(returned['slots'][slot]), slot_keys)
                        self.assertEqual(set(returned['extraFiles']), {'efisp'})
                        labels = set(REQUIRED) | {'efisp'}
                        self.assertEqual(set(returned['partitions']), labels)
                        self.assertEqual(len(returned['partitions']), 13)
                        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
                        self.assertEqual(len(list(out.glob('*.img'))), 13)

                        for label in labels:
                            lun, first = GEOMETRY[label]
                            extent = returned['partitions'][label]
                            self.assertEqual(
                                set(extent),
                                {'lun', 'firstLba', 'blockCount', 'partitionUuid'})
                            self.assertEqual(extent['lun'], lun)
                            self.assertEqual(extent['firstLba'], first)
                            self.assertEqual(extent['blockCount'], 8)
                            image = env.images[lun].read_bytes()
                            raw_uuid_offset = 2 * bs + GPT_INDEX[label] * 128 + 16
                            raw_uuid = image[raw_uuid_offset:raw_uuid_offset + 16]
                            self.assertEqual(len(raw_uuid), 16)
                            import uuid
                            self.assertEqual(
                                extent['partitionUuid'],
                                str(uuid.UUID(bytes_le=raw_uuid)))
                            offset = extent['firstLba'] * bs
                            size = extent['blockCount'] * bs
                            payload = image[offset:offset + size]
                            dest = out / f'{label}.img'
                            self.assertEqual(dest.read_bytes(), payload)
                            self.assertEqual(dest.stat().st_size, size)
                            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
                            if label == 'efisp':
                                entry = returned['extraFiles'][label]
                            else:
                                name, slot = label.rsplit('_', 1)
                                name = {
                                    'vendor_boot': 'vendorBoot',
                                    'init_boot': 'initBoot',
                                    'xbl_config': 'xblConfig',
                                }.get(name, name)
                                entry = returned['slots'][slot][name]
                            self.assertEqual(set(entry), {'path', 'bytes', 'sha256'})
                            self.assertEqual(entry['path'], str(dest))
                            self.assertEqual(entry['bytes'], size)
                            self.assertEqual(
                                entry['sha256'], hashlib.sha256(payload).hexdigest())

                        self.assertEqual(len(source_opens), 18)  # three six-LUN inspections
                        for path, flags in source_opens:
                            self.assertIn(path, env.images)
                            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR))
                        self.fixture.assert_closed_unchanged(env, before)

    def test_android_telemetry_difference_is_not_capture_drift(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            report = slots.inspect_physical_slots()
            expected = json.loads(json.dumps(report))
            expected['selectedSlot'] = (
                'b' if report['selectedSlot'] == 'a' else 'a')
            expected['bootableSlots'] = {
                'a': report['bootableSlots']['a'],
                'b': not report['bootableSlots']['b'],
            }
            out = self.output_for(env)
            returned = sources.capture_boot_sources(
                expected, out, extra_labels=('efisp',))
            self.assertTrue(
                slots.same_physical_identity(returned['physical'], expected))
            self.fixture.assert_closed_unchanged(env, before)

    def test_argument_report_and_board_failures_do_not_create_success(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            out = self.output_for(env)
            invalid = [
                ('userdata',), ('rawdump',), ('metadata',), 'efisp', 7,
                [None], [[]], [{'unhashable'}], ('efisp', 'efisp'),
            ]
            for extras in invalid:
                with self.subTest(extras=repr(extras)), \
                        mock.patch.object(slots, 'inspect_physical_slots') as inspection:
                    with self.assertRaises(sources.BootSourcesError):
                        sources.capture_boot_sources({}, out, extra_labels=extras)
                    inspection.assert_not_called()
                    self.assert_no_manifest(out)
            report = slots.inspect_physical_slots()
            mismatched = json.loads(json.dumps(report))
            mismatched['deployment'] += '.drifted'
            with self.assertRaisesRegex(sources.BootSourcesError, 'drifted'):
                sources.capture_boot_sources(mismatched, out, extra_labels=('efisp',))
            self.assert_no_manifest(out)
            self.fixture.assert_closed_unchanged(env, before)
        with self.fixture.environment(board=3) as env:
            before = self.fixture.digests(env)
            with self.assertRaisesRegex(sources.BootSourcesError, 'unsupported board id 3'):
                sources.capture_boot_sources({}, self.output_for(env))
            self.assertEqual(env.opened, [])
            self.fixture.assert_closed_unchanged(env, before)

    def test_missing_allowed_extra_uses_native_failure_and_cleans_up(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            expected = slots.inspect_physical_slots()
            out = self.output_for(env)
            with self.assertRaisesRegex(
                    sources.BootSourcesError, 'native locator failed for recovery_a'):
                sources.capture_boot_sources(
                    expected, out, extra_labels=('recovery_a',))
            self.assert_no_manifest(out)
            self.fixture.assert_closed_unchanged(env, before)

    def test_space_existing_output_and_symlink_are_refused_without_overwrite(self):
        for rejection in ('space', 'existing', 'symlink'):
            with self.subTest(rejection=rejection), self.fixture.environment() as env:
                before = self.fixture.digests(env)
                expected = slots.inspect_physical_slots()
                out = self.output_for(env)
                sentinel = None
                if rejection == 'existing':
                    out.mkdir()
                    sentinel = out / 'sentinel'
                    sentinel.write_bytes(b'keep-existing')
                elif rejection == 'symlink':
                    target = out.parent / 'elsewhere'
                    target.mkdir()
                    sentinel = target / 'sentinel'
                    sentinel.write_bytes(b'keep-symlink-target')
                    out.symlink_to(target, target_is_directory=True)
                with mock.patch.object(
                        sources.shutil, 'disk_usage',
                        return_value=SimpleNamespace(free=0)) if rejection == 'space' \
                        else nullcontext():
                    with self.assertRaises(sources.BootSourcesError):
                        sources.capture_boot_sources(expected, out)
                self.assert_no_manifest(out)
                if sentinel is not None:
                    self.assertIn(sentinel.read_bytes(),
                                  (b'keep-existing', b'keep-symlink-target'))
                self.fixture.assert_closed_unchanged(env, before)

    def test_eintr_and_short_io_capture_full_bytes_and_preserve_source_positions(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            expected = slots.inspect_physical_slots()
            out = self.output_for(env)
            pread_calls = 0
            write_calls = 0
            active_source_fds = {}
            observed_offsets = []
            real_device_fd = slots._device_fd
            previous_close = slots.os.close

            def short_pread(fd, size, offset):
                nonlocal pread_calls
                pread_calls += 1
                if pread_calls == 1:
                    raise InterruptedError()
                return REAL_PREAD(fd, min(size, 97), offset)

            def short_write(fd, data):
                nonlocal write_calls
                write_calls += 1
                if write_calls == 1:
                    raise InterruptedError()
                return REAL_WRITE(fd, data[:71])

            def positioned_device_fd(path, rdev):
                fd = real_device_fd(path, rdev)
                REAL_LSEEK(fd, 7, os.SEEK_SET)
                active_source_fds[fd] = True
                return fd

            def checked_close(fd):
                if fd in active_source_fds:
                    observed_offsets.append(REAL_LSEEK(fd, 0, os.SEEK_CUR))
                    active_source_fds.pop(fd)
                return previous_close(fd)

            with mock.patch.object(sources.os, 'pread', side_effect=short_pread), \
                    mock.patch.object(sources.os, 'write', side_effect=short_write), \
                    mock.patch.object(slots, '_device_fd', side_effect=positioned_device_fd), \
                    mock.patch.object(slots.os, 'close', side_effect=checked_close):
                manifest = sources.capture_boot_sources(expected, out)
            self.assertGreater(pread_calls, 2)
            self.assertGreater(write_calls, 2)
            self.assertEqual(observed_offsets, [7] * 18)
            self.assertEqual(active_source_fds, {})
            for label, extent in manifest['partitions'].items():
                payload = env.images[extent['lun']].read_bytes()
                start = extent['firstLba'] * 512
                end = start + extent['blockCount'] * 512
                self.assertEqual((out / f'{label}.img').read_bytes(), payload[start:end])
            self.fixture.assert_closed_unchanged(env, before)

    def test_short_source_eof_leaves_no_success_manifest(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            expected = slots.inspect_physical_slots()
            out = self.output_for(env)
            with mock.patch.object(sources.os, 'pread', return_value=b''):
                with self.assertRaisesRegex(sources.BootSourcesError, 'short source read'):
                    sources.capture_boot_sources(expected, out)
            self.assert_no_manifest(out)
            self.fixture.assert_closed_unchanged(env, before)

    def test_post_copy_source_drift_is_rejected_with_only_deliberate_mutation(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            expected = slots.inspect_physical_slots()
            out = self.output_for(env)
            original_image = env.images[4].read_bytes()
            real_copy = sources._stream_copy
            mutated = False

            def copy_then_mutate(fd, offset, size, dest):
                nonlocal mutated
                result = real_copy(fd, offset, size, dest)
                if dest.name == 'boot_a.img' and not mutated:
                    mutated = True
                    mutation_offset = 8 * 512 + 128
                    replacement = bytes([original_image[mutation_offset] ^ 0x5a])
                    fd_source = REAL_OPEN(env.images[4], os.O_WRONLY)
                    try:
                        self.assertEqual(
                            REAL_PWRITE(fd_source, replacement, mutation_offset), 1)
                        REAL_FSYNC(fd_source)
                    finally:
                        REAL_CLOSE(fd_source)
                return result

            with mock.patch.object(sources, '_stream_copy', side_effect=copy_then_mutate):
                with self.assertRaisesRegex(
                        sources.BootSourcesError, 'source region for boot_a changed'):
                    sources.capture_boot_sources(expected, out)
            self.assertTrue(mutated)
            self.assert_no_manifest(out)
            final_image = env.images[4].read_bytes()
            differences = [i for i, pair in enumerate(zip(original_image, final_image))
                           if pair[0] != pair[1]]
            self.assertEqual(differences, [8 * 512 + 128])
            for lun in (0, 1, 2, 3, 5):
                self.assertEqual(
                    hashlib.sha256(env.images[lun].read_bytes()).hexdigest(), before[lun])
            self.fixture.assert_closed_unchanged(
                env, before[:4] + [hashlib.sha256(final_image).hexdigest()] + before[5:])

    def test_directory_fsync_failure_removes_manifest_and_preserves_images(self):
        with self.fixture.environment() as env:
            before = self.fixture.digests(env)
            expected = slots.inspect_physical_slots()
            out = self.output_for(env)

            def fail_directory_fsync(fd):
                if stat.S_ISDIR(REAL_FSTAT(fd).st_mode):
                    raise OSError('injected directory fsync failure')
                return REAL_FSYNC(fd)

            with mock.patch.object(sources.os, 'fsync', side_effect=fail_directory_fsync):
                with self.assertRaisesRegex(sources.BootSourcesError, 'cannot sync'):
                    sources.capture_boot_sources(expected, out, extra_labels=('efisp',))
            self.assert_no_manifest(out)
            self.assertEqual(len(list(out.glob('*.img'))), 13)
            for label in set(REQUIRED) | {'efisp'}:
                lun, first = GEOMETRY[label]
                payload = env.images[lun].read_bytes()[first * 512:(first + 8) * 512]
                self.assertEqual((out / f'{label}.img').read_bytes(), payload)
            self.fixture.assert_closed_unchanged(env, before)


if __name__ == '__main__':
    unittest.main()
