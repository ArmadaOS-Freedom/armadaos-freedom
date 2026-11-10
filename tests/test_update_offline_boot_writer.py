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
from updates import recovery_boot_sources

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
                        result = offline.write_offline_boots(trust, cands, progress=events.append)
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
            result = offline.write_offline_boots(trust, cands)
            self.assertEqual(result['phase'], 'dual_boots_written')
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(
                stack, payload_match=False)
            spy = stack.enter_context(mock.patch.object(offline.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'payload digest') as ctx:
                offline.write_offline_boots(trust, cands)
            self.assertEqual(ctx.exception.restore_status, 'not_needed')
            spy.assert_not_called()
            self.assert_unchanged(env, before, caps)

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
                            offline.write_offline_boots(trust, changed)
                        self.assertEqual(ctx.exception.restore_status, 'not_needed')
                        spy.assert_not_called()
                    self.assert_unchanged(env, before, caps)

    def test_partial_second_write_restores_both_in_reverse_order(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
            calls = []
            def faulty(fd, data, pos):
                calls.append((bytes(data), pos))
                if len(calls) == 2:
                    return REAL_PWRITE(fd, data[:len(data) // 2], pos)
                if len(calls) == 3:
                    raise OSError(errno.EIO, 'second-slot failure')
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=faulty))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'restored') as ctx:
                offline.write_offline_boots(trust, cands)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual([calls[i][0] for i in (0, 1, 3, 4)],
                             [payloads[order[0]], payloads[order[1]],
                              originals[order[1]], originals[order[0]]])
            for slot in order:
                evidence = ctx.exception.slot_results[slot]
                self.assertEqual(evidence['status'], 'restored')
                self.assertEqual(evidence['restoredSha256'], hashlib.sha256(originals[slot]).hexdigest())
                self.assertEqual(evidence['candidate'], cands[slot]['candidate'])
            self.assert_unchanged(env, before, caps)

    def test_corrupt_second_write_real_readback_restores_both(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(stack)
            calls = []
            def corrupt(fd, data, pos):
                calls.append(pos)
                if len(calls) == 2:
                    damaged = bytearray(data)
                    damaged[200] ^= 0xFF
                    return REAL_PWRITE(fd, bytes(damaged), pos)
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=corrupt))
            current = trust['physical']['actualSlot']
            with self.assertRaisesRegex(boot_writer.BootWriteError,
                                        f'boot_{current} readback digest mismatch') as ctx:
                offline.write_offline_boots(trust, cands)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assert_unchanged(env, before, caps)

    def test_current_restore_failure_still_restores_inactive(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, originals, payloads, extents = self._prepare(stack)
            current = trust['physical']['actualSlot']
            inactive = trust['physical']['inactiveSlot']
            def verified_then_fail(event):
                if event['phase'] == 'verified' and event['slot'] == current:
                    raise OSError(errno.EIO, 'after both verified')
            current_offset = extents[current][1]
            def fail_current_restore(fd, data, pos):
                if pos == current_offset and bytes(data) == originals[current]:
                    raise OSError(errno.EIO, 'current restore failed')
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=fail_current_restore))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'uncertain') as ctx:
                offline.write_offline_boots(trust, cands, progress=verified_then_fail)
            self.assertEqual(ctx.exception.restore_status, 'failed')
            results = ctx.exception.slot_results
            self.assertEqual(results[current]['status'], 'restore_failed')
            self.assertNotIn('restoredSha256', results[current])
            self.assertEqual(results[inactive]['status'], 'restored')
            expected = list(before)
            lun, offset, size = extents[current]
            expected[lun] = expected[lun][:offset] + payloads[current] + expected[lun][offset + size:]
            self.assertEqual([p.read_bytes() for p in env.images], expected)
            self.assertEqual({p: p.read_bytes() for p in caps}, caps)
            self.assert_closed(env)

    def test_progress_failure_boundaries_and_source_backup_integrity(self):
        with boot_fixture.contextlib_stack() as stack:
            env, trust, cands, before, caps, _, _, _ = self._prepare(stack)
            spy = stack.enter_context(mock.patch.object(offline.os, 'pwrite', wraps=REAL_PWRITE))
            with self.assertRaisesRegex(boot_writer.BootWriteError, 'before writing') as ctx:
                offline.write_offline_boots(trust, cands, progress=lambda event: (_ for _ in ()).throw(OSError('stop')))
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
                offline.write_offline_boots(trust, cands, progress=stop_after_first)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assertEqual(writes, [payloads[inactive], originals[inactive]])
            self.assertNotIn(payloads[current], writes)
            self.assert_unchanged(env, before, caps)

    def _prepare_recovery(self, stack, *, bs=512, current=0, board=1,
                          mode='aosp-resign', payload_match=True):
        env, trust, cands, before, caps, originals, payloads, extents = self._prepare(
            stack, bs=bs, current=current, board=board, mode=mode,
            payload_match=payload_match)
        actual = trust['physical']['actualSlot']
        inactive = trust['physical']['inactiveSlot']

        # Recovery session bindings: env.recovery tuple with canonical nonzero UUID.
        recovery = (env.recovery[0], RECOVERY_PART_UUID, env.recovery[2], env.recovery[3])
        stack.enter_context(mock.patch.object(
            slots, 'locate_recovery_partition', return_value=recovery))

        # The recovery main path must never take the normal inspection route.
        def _forbidden(*args, **kwargs):
            raise AssertionError('normal boot inspection path used in recovery mode')

        normal_inspect = stack.enter_context(mock.patch.object(
            slots, 'inspect_physical_slots', side_effect=_forbidden))
        normal_snapshot = stack.enter_context(mock.patch.object(
            slots.armada_os, 'snapshot', side_effect=_forbidden))

        # Real binary witness parser for regular-slot RAM reads.
        stack.enter_context(mock.patch.object(
            boot_context, '_read_regular',
            side_effect=lambda *args, **kwargs: self.base.fixture.witness_bytes(current)))

        physical = slots.inspect_recovery_slots()
        self.assertNotIn('deployment', physical)
        self.assertNotIn('booted', physical)

        work = env.images[0].parent.parent
        temporary = bytearray(originals[actual])
        temporary[130] ^= 0x96
        temporary = bytes(temporary)
        temp_path = work / f'temporary_boot_{actual}.img'
        temp_path.write_bytes(temporary)
        fd = os.open(env.images[extents[actual][0]], os.O_RDWR)
        try:
            self.assertEqual(os.pwrite(fd, temporary, extents[actual][1]), len(temporary))
            os.fsync(fd)
        finally:
            os.close(fd)
        temp_claim = {
            'path': str(temp_path), 'bytes': len(temporary),
            'sha256': hashlib.sha256(temporary).hexdigest(),
        }
        temp_sha = temp_claim['sha256']

        live_snapshot = copy.deepcopy(trust['sources'])
        live_snapshot['phase'] = 'recovery_boot_sources_captured'
        live_snapshot['physical'] = copy.deepcopy(physical)
        live_snapshot['slots'][actual]['boot'] = copy.deepcopy(temp_claim)
        # inactive slot keeps its original captured boot claim

        original_boots = {
            slot: copy.deepcopy(trust['sources']['slots'][slot]['boot'])
            for slot in ('a', 'b')
        }
        identity = {
            'schemaVersion': 2,
            'sessionId': RECOVERY_SESSION_ID,
            'model': physical['model'],
            'recoveryPartUuid': RECOVERY_PART_UUID,
            'installedDeployment': boot_fixture.ACTIVE_DEP,
            'entrySlot': actual,
            'originalBoots': {
                slot: {
                    'partitionUuid': live_snapshot['partitions'][f'boot_{slot}']['partitionUuid'],
                    'bytes': original_boots[slot]['bytes'],
                    'sha256': original_boots[slot]['sha256'],
                }
                for slot in ('a', 'b')
            },
        }
        resolved = recovery_boot_sources.resolve_session_recovery_boot_sources(
            identity, live_snapshot, original_boots, temporary_boot_sha256=temp_sha)

        trust['phase'] = 'recovery_boot_trust_checked'
        trust['physical'] = copy.deepcopy(physical)
        trust['sources'] = copy.deepcopy(resolved)
        caps[temp_path] = temporary
        self.assert_closed(env)
        env.opened.clear()
        env.closed.clear()
        return (env, trust, cands, before, caps, originals, payloads, extents,
                identity, temp_sha, normal_inspect, normal_snapshot)

    def test_recovery_matrix_exact_reports_events_and_only_two_boot_extents(self):
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), \
                            boot_fixture.contextlib_stack() as stack:
                        (env, trust, cands, before, caps, originals, payloads, extents,
                         identity, temp_sha, normal_inspect, normal_snapshot) = \
                            self._prepare_recovery(stack, bs=bs, current=current, board=board)
                        actual = trust['physical']['actualSlot']
                        inactive = trust['physical']['inactiveSlot']
                        order = [inactive, actual]
                        source_sha = {
                            slot: hashlib.sha256(originals[slot]).hexdigest()
                            for slot in (inactive, actual)
                        }
                        trust_before = copy.deepcopy(trust)
                        cands_before = copy.deepcopy(cands)
                        events = []
                        result = offline.write_recovery_offline_boots(
                            trust, cands, session_identity=identity,
                            temporary_boot_sha256=temp_sha, progress=events.append)
                        writes = {slot: {
                            'sourceBootSha256': source_sha[slot],
                            'candidate': copy.deepcopy(cands[slot]['candidate']),
                            'bytesWritten': len(payloads[slot]),
                            'readbackSha256': cands[slot]['candidate']['sha256'],
                        } for slot in order}
                        self.assertEqual(result, {
                            'schemaVersion': 1, 'phase': 'dual_boots_written',
                            'model': 'TB322FC' if board == 1 else 'TB323FU',
                            'fromSlot': actual, 'slotOrder': order,
                            'transactionUuid': boot_fixture.TX_UUID,
                            'deployment': boot_fixture.TARGET_DEP,
                            'physical': trust['physical'], 'writes': writes,
                            'runtime': 'recovery',
                            'recoverySessionId': identity['sessionId'],
                            'oldDeployment': boot_fixture.ACTIVE_DEP,
                            'temporaryBootSha256': temp_sha,
                        })
                        self.assertNotIn('deployment', result['physical'])
                        self.assertNotIn('booted', result['physical'])
                        expected_events = []
                        for slot in order:
                            common = {'slot': slot, 'slotOrder': order,
                                      'transactionUuid': boot_fixture.TX_UUID,
                                      'deployment': boot_fixture.TARGET_DEP}
                            expected_events.extend([
                                {'phase': 'writing', **common,
                                 'sourceBootSha256': source_sha[slot],
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
                        self.assertEqual(trust, trust_before)
                        self.assertEqual(cands, cands_before)
                        self.assertEqual(slots.inspect_recovery_slots(), trust['physical'])
                        normal_inspect.assert_not_called()
                        normal_snapshot.assert_not_called()
                        self.assert_closed(env)

    def test_recovery_identity_temporary_and_live_rejections(self):
        with boot_fixture.contextlib_stack() as stack:
            (env, trust, cands, before, caps, originals, payloads, extents,
             identity, temp_sha, normal_inspect, normal_snapshot) = self._prepare_recovery(stack)
            actual = trust['physical']['actualSlot']
            def live_change():
                fd = os.open(env.images[extents[actual][0]], os.O_RDWR)
                try:
                    offset = extents[actual][1]
                    REAL_PWRITE(fd, bytes([os.pread(fd, 1, offset)[0] ^ 1]), offset)
                finally:
                    os.close(fd)
            cases = [
                lambda t, c, i: i['originalBoots'][actual].__setitem__('sha256', 'f' * 64),
                lambda t, c, i: None,
                lambda t, c, i: t.__setitem__('phase', 'boot_trust_checked'),
                lambda t, c, i: t['sources'].__setitem__('schemaVersion', True),
                lambda t, c, i: live_change(),
                lambda t, c, i: i.pop('originalBoots'),
            ]
            for index, mutate in enumerate(cases):
                with self.subTest(case=index):
                    changed_trust = copy.deepcopy(trust)
                    changed_cands = copy.deepcopy(cands)
                    changed_identity = copy.deepcopy(identity)
                    mutate(changed_trust, changed_cands, changed_identity)
                    expected_sha = 'f' * 64 if index == 1 else temp_sha
                    baseline = [p.read_bytes() for p in env.images]
                    with mock.patch.object(offline.os, 'pwrite', wraps=REAL_PWRITE) as spy:
                        with self.assertRaises(boot_writer.BootWriteError) as ctx:
                            offline.write_recovery_offline_boots(
                                changed_trust, changed_cands, session_identity=changed_identity,
                                temporary_boot_sha256=expected_sha)
                        self.assertEqual(ctx.exception.restore_status, 'not_needed')
                        spy.assert_not_called()
                    self.assert_unchanged(env, baseline, caps)
            normal_inspect.assert_not_called()
            normal_snapshot.assert_not_called()

    def test_recovery_partial_second_write_restores_true_originals(self):
        for current in (0, 1):
            with self.subTest(current=current), boot_fixture.contextlib_stack() as stack:
                (env, trust, cands, before, caps, originals, payloads, extents,
                 identity, temp_sha, normal_inspect, normal_snapshot) = \
                    self._prepare_recovery(stack, current=current)
                order = [trust['physical']['inactiveSlot'], trust['physical']['actualSlot']]
                calls = []
                def faulty(fd, data, pos):
                    calls.append(bytes(data))
                    if len(calls) == 2:
                        return REAL_PWRITE(fd, data[:len(data) // 2], pos)
                    if len(calls) == 3:
                        raise OSError(errno.EIO, 'second-slot failure')
                    return REAL_PWRITE(fd, data, pos)
                stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=faulty))
                with self.assertRaisesRegex(boot_writer.BootWriteError, 'restored') as ctx:
                    offline.write_recovery_offline_boots(
                        trust, cands, session_identity=identity, temporary_boot_sha256=temp_sha)
                self.assertEqual(ctx.exception.restore_status, 'restored')
                self.assertEqual([calls[i] for i in (0, 1, 3, 4)],
                                 [payloads[order[0]], payloads[order[1]],
                                  originals[order[1]], originals[order[0]]])
                for slot in order:
                    evidence = ctx.exception.slot_results[slot]
                    original_sha = hashlib.sha256(originals[slot]).hexdigest()
                    self.assertEqual(evidence['status'], 'restored')
                    self.assertEqual(evidence['sourceBootSha256'], original_sha)
                    self.assertEqual(evidence['restoredSha256'], original_sha)
                self.assert_unchanged(env, before, caps)

    def test_recovery_real_readback_corruption_restores_true_originals(self):
        with boot_fixture.contextlib_stack() as stack:
            (env, trust, cands, before, caps, originals, payloads, extents,
             identity, temp_sha, normal_inspect, normal_snapshot) = \
                self._prepare_recovery(stack, current=1)
            calls = []
            def corrupt(fd, data, pos):
                calls.append(pos)
                if len(calls) == 2:
                    damaged = bytearray(data)
                    damaged[200] ^= 0xFF
                    return REAL_PWRITE(fd, bytes(damaged), pos)
                return REAL_PWRITE(fd, data, pos)
            stack.enter_context(mock.patch.object(offline.os, 'pwrite', side_effect=corrupt))
            current_slot = trust['physical']['actualSlot']
            with self.assertRaisesRegex(boot_writer.BootWriteError,
                                        f'boot_{current_slot} readback digest mismatch') as ctx:
                offline.write_recovery_offline_boots(
                    trust, cands, session_identity=identity, temporary_boot_sha256=temp_sha)
            self.assertEqual(ctx.exception.restore_status, 'restored')
            self.assert_unchanged(env, before, caps)


if __name__ == '__main__':
    unittest.main()
