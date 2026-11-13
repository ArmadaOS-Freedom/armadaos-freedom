import copy
import errno
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'system_files/usr/lib/armada', ROOT / 'tests'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import test_update_boot_writer as boot_fixture
from updates import boot_context
from updates import boot_writer
from updates import offline_boot_writer as offline
from updates import physical_slots as slots

REAL_PWRITE = boot_fixture.REAL_PWRITE
RECOVERY_SESSION_ID = '12345678123456781234567812345678'
RECOVERY_PART_UUID = '12345678-1234-5678-9abc-def012345679'


class OfflineBootWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        boot_fixture.BootWriterTests.setUpClass()
        cls.addClassCleanup(boot_fixture.BootWriterTests.doClassCleanups)

    def setUp(self):
        self.base = boot_fixture.BootWriterTests()
        self.base.setUp()

    def assert_closed(self, env):
        self.base._assert_all_closed(env)

    def assert_unchanged(self, env, disks, captures):
        self.assertEqual([p.read_bytes() for p in env.images], disks)
        self.assertEqual({p: p.read_bytes() for p in captures}, captures)
        self.assert_closed(env)

    def _prepare(self, stack, *, bs=512, current=0, board=1,
                 mode='aosp-resign', payload_match=True):
        env = stack.enter_context(self.base.fixture.environment(
            bs=bs, current=current, board=board))
        env.context['deployment'] = boot_fixture.ACTIVE_DEP
        stack.enter_context(mock.patch.object(
            boot_writer, 'read_boot_lock', return_value='unlocked'))
        stack.enter_context(mock.patch.object(
            offline, 'read_boot_lock', return_value='unlocked'))
        work = env.images[0].parent.parent
        cap_dir = work / 'captured'
        extras = ['recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b']
        if board == 2:
            extras.append('efisp')
        physical = slots.inspect_physical_slots()
        from updates import boot_sources as sources
        snapshot = sources.capture_boot_sources(physical, cap_dir, extra_labels=extras)
        self.assert_closed(env)
        env.opened.clear()
        env.closed.clear()
        inactive, actual = physical['inactiveSlot'], physical['actualSlot']
        originals, candidate_bytes, extents = {}, {}, {}
        candidates = {}
        trust_entry = {
            'maxRawBytes': 32768,
            'material': {'baseImageSha256': '1' * 64},
            'rootPublicKeySha256': '2' * 64,
            'bootPublicKeySha256': '3' * 64,
        }
        trust = {
            'schemaVersion': 1, 'phase': 'boot_trust_checked',
            'physical': copy.deepcopy(physical), 'lockState': 'unlocked',
            'mode': mode, 'sources': copy.deepcopy(snapshot),
            'trust': {inactive: copy.deepcopy(trust_entry)},
        }
        trust['trust'][actual] = copy.deepcopy(trust['trust'][inactive])
        for slot in (inactive, actual):
            trust['trust'][slot]['metadataSha256'] = hashlib.sha256(
                f'authenticated-metadata-{slot}'.encode()).hexdigest()
        for slot in (inactive, actual):
            original = snapshot['slots'][slot]['boot']
            originals[slot] = Path(original['path']).read_bytes()
            changed = bytearray(originals[slot])
            if slot == inactive:
                changed[128] ^= 0x5A
                changed[-1] ^= 0xA5
            else:
                changed[129] ^= 0xC3
                changed[-2] ^= 0x3C
            candidate_bytes[slot] = bytes(changed)
            path = work / f'candidate_boot_{slot}.img'
            path.write_bytes(candidate_bytes[slot])
            claim = {'path': str(path), 'bytes': len(changed),
                     'sha256': hashlib.sha256(candidate_bytes[slot]).hexdigest()}
            candidates[slot] = {
                'schemaVersion': 1, 'phase': 'candidate_prepared', 'slot': slot,
                'model': physical['model'], 'mode': mode,
                'transactionUuid': boot_fixture.TX_UUID,
                'deployment': boot_fixture.TARGET_DEP, 'maxRawBytes': 32768,
                'candidate': claim, 'sourceBootSha256': original['sha256'],
                'baseImageSha256': '1' * 64, 'rootPublicKeySha256': '2' * 64,
                'bootPublicKeySha256': '3' * 64,
                'metadataSha256': trust['trust'][slot]['metadataSha256'],
                'payloadDigestMatchesSignedMetadata': payload_match,
            }
            part = snapshot['partitions'][f'boot_{slot}']
            extents[slot] = (part['lun'], part['firstLba'] * bs, len(changed))
        captures = {p: p.read_bytes() for p in cap_dir.rglob('*') if p.is_file()}
        return (env, trust, candidates, [p.read_bytes() for p in env.images],
                captures, originals, candidate_bytes, extents)

    def test_matrix_exact_reports_events_and_only_two_boot_extents(self):
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), \
                            boot_fixture.contextlib_stack() as stack:
                        env, trust, cands, before, caps, originals, payloads, extents = \
                            self._prepare(stack, bs=bs, current=current, board=board)
                        events = []
                        result = offline.write_offline_boots(
                            trust, cands, commit=lambda: None, progress=events.append)
                        order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
                        writes = {slot: {
                            'sourceBootSha256': hashlib.sha256(originals[slot]).hexdigest(),
                            'candidate': copy.deepcopy(cands[slot]['candidate']),
                            'bytesWritten': len(payloads[slot]),
                            'readbackSha256': cands[slot]['candidate']['sha256'],
                        } for slot in order}
                        self.assertEqual(result, {
                            'schemaVersion': 1, 'phase': 'dual_boots_written',
                            'model': 'TB322FC' if board == 1 else 'TB323FU',
                            'fromSlot': 'ab'[current], 'slotOrder': order,
                            'transactionUuid': boot_fixture.TX_UUID,
                            'deployment': boot_fixture.TARGET_DEP,
                            'physical': trust['physical'], 'writes': writes,
                        })
                        expected_events = []
                        for slot in order:
                            common = {'slot': slot, 'slotOrder': order,
                                      'transactionUuid': boot_fixture.TX_UUID,
                                      'deployment': boot_fixture.TARGET_DEP}
                            expected_events.extend([
                                {'phase': 'writing', **common,
                                 'sourceBootSha256': writes[slot]['sourceBootSha256'],
                                 'candidate': cands[slot]['candidate']},
                                {'phase': 'verified', **common, 'evidence': writes[slot]},
                            ])
                        self.assertEqual(events, expected_events)
                        expected = list(before)
                        for slot in order:
                            lun, offset, size = extents[slot]
                            image = bytearray(expected[lun])
                            self.assertEqual(image[offset:offset + size], originals[slot])
                            image[offset:offset + size] = payloads[slot]
                            expected[lun] = bytes(image)
                        self.assertEqual([p.read_bytes() for p in env.images], expected)
                        self.assertEqual({p: p.read_bytes() for p in caps}, caps)
                        self.assertEqual(slots.inspect_physical_slots(), trust['physical'])
                        self.assert_closed(env)

    def test_payload_digest_mode_contract(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, _, caps, _, _, _ = self._prepare(
                stack, mode='gbl-preserve-metadata', payload_match=False)
            result = offline.write_offline_boots(trust, cands, commit=lambda: None)
            self.assertEqual(result['phase'], 'dual_boots_written')
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(
                stack, payload_match=False)
            spy = stack.enter_context(mock.patch.object(offline.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'payload digest') as ctx:
                offline.write_offline_boots(trust, cands, commit=lambda: None)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            spy.assert_not_called()
            self.assert_unchanged(env, before, caps)

    def test_gbl_metadata_must_match_authenticated_source_before_any_write(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(
                stack, mode='gbl-preserve-metadata')
            first, second = 'a', 'b'
            self.assertNotEqual(
                trust['trust'][first]['metadataSha256'],
                trust['trust'][second]['metadataSha256'])
            cases = [
                ('missing candidate metadata',
                 lambda c, t: c[second].pop('metadataSha256')),
                ('non-hex candidate metadata',
                 lambda c, t: c[second].__setitem__('metadataSha256', 'z' * 64)),
                ('short candidate metadata',
                 lambda c, t: c[second].__setitem__('metadataSha256', 'f' * 63)),
                ('candidate mismatch on second slot',
                 lambda c, t: c[second].__setitem__(
                     'metadataSha256', t['trust'][first]['metadataSha256'])),
                ('cross-slot metadata swap',
                 lambda c, t: (
                     c[first].__setitem__(
                         'metadataSha256', t['trust'][second]['metadataSha256']),
                     c[second].__setitem__(
                         'metadataSha256', t['trust'][first]['metadataSha256']))),
                ('missing trusted metadata',
                 lambda c, t: t['trust'][second].pop('metadataSha256')),
                ('malformed trusted metadata',
                 lambda c, t: t['trust'][second].__setitem__('metadataSha256', 'g' * 64)),
            ]
            for label, mutate in cases:
                with self.subTest(label):
                    changed, changed_trust = copy.deepcopy(cands), copy.deepcopy(trust)
                    mutate(changed, changed_trust)
                    with mock.patch.object(
                            offline.os, 'pwrite', wraps=REAL_PWRITE) as spy:
                        with self.assertRaisesRegex(
                                boot_writer.BootWriteError, 'metadataSha256') as ctx:
                            offline.write_offline_boots(
                                changed_trust, changed, commit=lambda: None)
                        self.assertEqual(ctx.exception.restore_status, 'not_needed')
                        spy.assert_not_called()
                    self.assert_unchanged(env, before, caps)

    def test_aosp_resign_new_metadata_is_intentionally_not_compared(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, _, caps, _, _, _ = self._prepare(stack)
            for slot in ('a', 'b'):
                trust['trust'][slot]['metadataSha256'] = 'a' * 64
                cands[slot]['metadataSha256'] = 'b' * 64
            result = offline.write_offline_boots(trust, cands, commit=lambda: None)
            self.assertEqual(result['phase'], 'dual_boots_written')
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_current_candidate_rejections_happen_before_any_write(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(stack)
            current = trust['physical']['actualSlot']
            cases = [
                (lambda c: c.__setitem__('slot', trust['physical']['inactiveSlot']), 'slot, model, or mode'),
                (lambda c: c.__setitem__('model', 'TB323FU'), 'slot, model, or mode'),
                (lambda c: c.__setitem__('baseImageSha256', 'f' * 64), 'baseImageSha256 mismatch'),
                (lambda c: c.__setitem__('rootPublicKeySha256', 'f' * 64), 'rootPublicKeySha256 mismatch'),
                (lambda c: c.__setitem__('bootPublicKeySha256', 'f' * 64), 'bootPublicKeySha256 mismatch'),
                (lambda c: c.__setitem__('sourceBootSha256', 'f' * 64), 'source digest'),
                (lambda c: c['candidate'].__setitem__('sha256', 'f' * 64),
                 f'candidate {trust["physical"]["actualSlot"]} content mismatch'),
                (lambda c: c.__setitem__('transactionUuid', '22345678-1234-5678-9abc-def012345678'), 'disagree'),
                (lambda c: c.__setitem__('deployment', f"armada/{'c' * 64}.0"), 'disagree'),
            ]
            for mutate, pattern in cases:
                with self.subTest(pattern=pattern):
                    changed = copy.deepcopy(cands)
                    mutate(changed[current])
                    with mock.patch.object(
                            offline.os, 'pwrite', wraps=REAL_PWRITE) as spy:
                        with self.assertRaisesRegex(boot_writer.BootWriteError, pattern) as ctx:
                            offline.write_offline_boots(trust, changed, commit=lambda: None)
                        self.assertEqual(ctx.exception.restore_status, 'not_needed')
                        spy.assert_not_called()
                    self.assert_unchanged(env, before, caps)

    def test_commit_hook_is_required_and_runs_between_verified_slots(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            spy = stack.enter_context(mock.patch.object(
                offline.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(
                    boot_writer.BootWriteError,
                    'inter-slot durable journal commit hook is required') as ctx:
                offline.write_offline_boots(trust, cands)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            spy.assert_not_called()
            self.assert_unchanged(env, before, caps)

        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            state = {'calls': 0, 'before_commit': None}
            writes = []
            def track(fd, data, pos):
                writes.append((bytes(data), pos))
                return REAL_PWRITE(fd, data, pos)
            def commit():
                state['calls'] += 1
                state['before_commit'] = list(writes)
                first_lun, first_offset, first_size = extents[order[0]]
                second_lun, second_offset, second_size = extents[order[1]]
                self.assertEqual(
                    env.images[first_lun].read_bytes()[
                        first_offset:first_offset + first_size],
                    payloads[order[0]])
                self.assertEqual(
                    env.images[second_lun].read_bytes()[
                        second_offset:second_offset + second_size],
                    originals[order[1]])
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=track))
            result = offline.write_offline_boots(trust, cands, commit=commit)
            self.assertEqual(result['phase'], 'dual_boots_written')
            self.assertEqual(state['calls'], 1)
            self.assertEqual(len(state['before_commit']), 1)
            self.assertEqual(state['before_commit'][0][0], payloads[order[0]])
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_uncommitted_commit_failure_restores_nonrunning_image(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            def commit():
                raise offline.JournalCommitError(
                    'journal compare-and-swap lost', commit_state='not_committed')
            with self.assertRaisesRegex(
                    boot_writer.BootWriteError, 'restored') as ctx:
                offline.write_offline_boots(trust, cands, commit=commit)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual(ctx.exception.commit_state, 'not_committed')
            self.assertEqual(ctx.exception.slot_results[order[0]]['status'], 'restored')
            self.assertEqual(ctx.exception.slot_results[order[1]]['status'], 'not_started')
            self.assert_unchanged(env, before, caps)

    def test_ambiguous_commit_failure_preserves_nonrunning_image(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            def commit():
                raise offline.JournalCommitError(
                    'journal fsync failed', commit_state='ambiguous')
            with self.assertRaisesRegex(
                    boot_writer.BootWriteError, 'ambiguous') as ctx:
                offline.write_offline_boots(trust, cands, commit=commit)
            self.assertEqual(ctx.exception.restore_status, 'not_restored')
            self.assertEqual(ctx.exception.commit_state, 'ambiguous')
            self.assertEqual(ctx.exception.slot_results[order[0]]['status'], 'preserved')
            self.assertEqual(ctx.exception.slot_results[order[1]]['status'], 'not_started')
            expected = list(before)
            lun, offset, size = extents[order[0]]
            expected[lun] = expected[lun][:offset] + payloads[order[0]] + expected[lun][offset + size:]
            self.assertEqual([p.read_bytes() for p in env.images], expected)
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_running_write_failure_after_commit_preserves_nonrunning_image(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            calls = []
            def faulty(fd, data, pos):
                calls.append((bytes(data), pos))
                if len(calls) == 2:
                    raise OSError(errno.EIO, 'running-slot failure')
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=faulty))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'preserved') as ctx:
                offline.write_offline_boots(trust, cands, commit=lambda: None)
            self.assertEqual(ctx.exception.restore_status, 'not_restored')
            self.assertEqual(ctx.exception.commit_state, 'committed')
            self.assertEqual(ctx.exception.slot_results[order[0]]['status'], 'preserved')
            self.assertEqual(ctx.exception.slot_results[order[1]]['status'], 'writing')
            self.assertEqual(len(calls), 2)
            expected = list(before)
            lun, offset, size = extents[order[0]]
            expected[lun] = expected[lun][:offset] + payloads[order[0]] + expected[lun][offset + size:]
            self.assertEqual([p.read_bytes() for p in env.images], expected)
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_running_readback_failure_after_commit_preserves_nonrunning_image(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            faulted = bytearray(payloads[order[1]])
            faulted[200] ^= 0xFF
            faulted = bytes(faulted)
            calls = []
            def corrupt(fd, data, pos):
                calls.append((pos, bytes(data)))
                if len(calls) == 2:
                    return REAL_PWRITE(fd, faulted, pos)
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=corrupt))
            with self.assertRaisesRegex(
                    boot_writer.BootWriteError,
                    f'boot_{order[1]} readback digest mismatch') as ctx:
                offline.write_offline_boots(trust, cands, commit=lambda: None)
            self.assertEqual(ctx.exception.restore_status, 'not_restored')
            self.assertEqual(ctx.exception.commit_state, 'committed')
            self.assertEqual(ctx.exception.slot_results[order[0]]['status'], 'preserved')
            self.assertEqual(ctx.exception.slot_results[order[1]]['status'], 'writing')
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1], payloads[order[0]])
            self.assertEqual(calls[1], (extents[order[1]][1], payloads[order[1]]))
            expected = list(before)
            for slot, data in ((order[0], payloads[order[0]]),
                               (order[1], faulted)):
                lun, offset, size = extents[slot]
                self.assertEqual(len(data), size)
                self.assertEqual(expected[lun][offset:offset + size], originals[slot])
                expected[lun] = (expected[lun][:offset] + data +
                                 expected[lun][offset + size:])
            boot_luns = {extents[slot][0] for slot in order}
            for lun, image in enumerate(before):
                if lun not in boot_luns:
                    self.assertEqual(env.images[lun].read_bytes(), image)
            self.assertEqual([p.read_bytes() for p in env.images], expected)
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_progress_failure_after_both_verified_preserves_images(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            def stop_after_second(event):
                if event['phase'] == 'verified' and event['slot'] == order[1]:
                    raise OSError('progress stop after both verified')
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'preserved') as ctx:
                offline.write_offline_boots(
                    trust, cands, commit=lambda: None, progress=stop_after_second)
            self.assertEqual(ctx.exception.restore_status, 'not_restored')
            self.assertEqual(ctx.exception.commit_state, 'committed')
            expected = list(before)
            for slot in order:
                lun, offset, size = extents[slot]
                expected[lun] = (expected[lun][:offset] + payloads[slot] +
                                 expected[lun][offset + size:])
            self.assertEqual([p.read_bytes() for p in env.images], expected)
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_progress_failure_boundaries_and_source_backup_integrity(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(stack)
            spy = stack.enter_context(mock.patch.object(offline.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'before writing') as ctx:
                offline.write_offline_boots(
                    trust, cands, commit=lambda: None,
                    progress=lambda event: (_ for _ in ()).throw(OSError('stop')))
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            spy.assert_not_called()
            self.assert_unchanged(env, before, caps)
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, _ = self._prepare(stack)
            inactive = trust['physical']['inactiveSlot']
            current = trust['physical']['actualSlot']
            writes = []
            def track(fd, data, pos):
                writes.append(bytes(data))
                return REAL_PWRITE(fd, data, pos)
            def stop_after_first(event):
                if event['phase'] == 'verified' and event['slot'] == inactive:
                    raise OSError('progress stop')
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=track))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'restored') as ctx:
                offline.write_offline_boots(
                    trust, cands, commit=lambda: None, progress=stop_after_first)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual(writes, [payloads[inactive], originals[inactive]])
            self.assertNotIn(payloads[current], writes)
            self.assert_unchanged(env, before, caps)


if __name__ == '__main__':
    unittest.main()
