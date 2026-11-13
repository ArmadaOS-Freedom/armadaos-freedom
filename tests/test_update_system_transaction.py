# SPDX-License-Identifier: GPL-3.0-only
"""System transaction orchestration tests for the mirrored deployment policy."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import dataclasses
import errno
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'system_files/usr/lib/armada', ROOT / 'tests'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import armada_os
import test_update_boot_writer as wf
import test_update_slot_activation as af
import test_update_trial_store as jf
from updates import (
    boot_candidate,
    boot_health,
    boot_materials,
    boot_sources,
    boot_trust,
    deployment_store,
    image_source,
    offline_boot_writer,
    physical_slots,
    runtime_health,
    system_transaction as tx_mod,
    trial_confirmation,
    trial_record as rec,
    trial_store as store,
)

PACKAGE = {
    'releaseId': '2026.04',
    'version': '1.2.3',
    'sourceRevision': 'c' * 40,
    'ociDigest': 'sha256:' + 'd' * 64,
}
SLOTS = ('a', 'b')


class SystemTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        af.SlotActivationTests.setUpClass()
        cls.addClassCleanup(af.SlotActivationTests.doClassCleanups)

    def _prepare(self, stack: ExitStack, *, bs: int = 512, current: int = 0,
                 board: int = 1, commit: str = 'b' * 64):
        act_test = af.SlotActivationTests()
        case = act_test._prepare(stack, bs=bs, current=current, board=board)

        target = 'ab'[1 - current]
        part = case.trust['sources']['partitions'][f'boot_{target}']
        orig_boot = Path(case.trust['sources']['slots'][target]['boot']['path']).read_bytes()
        fd = wf.REAL_OPEN(case.env.images[part['lun']], os.O_RDWR)
        try:
            os.pwrite(fd, orig_boot, part['firstLba'] * bs)
        finally:
            os.close(fd)

        zeros = b'\x00' * rec.RECORD_BYTES
        os.pwrite(case.journal.fd, zeros, jf.REGION_OFFSET + rec.TAIL_OFFSET_A)
        os.pwrite(case.journal.fd, zeros, jf.REGION_OFFSET + rec.TAIL_OFFSET_B)
        self.assertIsNone(store.load(case.journal.fd, case.journal.part, board).record)
        self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)

        root = case.env.images[0].parent.parent
        sysroot = (root / 'sysroot').resolve()
        (sysroot / 'ostree').mkdir(parents=True, exist_ok=True)
        (sysroot / 'boot').mkdir(parents=True, exist_ok=True)
        lock_path = root / 'update.lock'
        work_dir = root / 'tx_work'

        trust = copy.deepcopy(case.trust)
        donor = trust['trust'][target]
        for slot in SLOTS:
            trust['trust'][slot] = copy.deepcopy(donor)

        # ``boot_writer`` and ``offline_boot_writer`` each bind the boot-lock
        # reader in their own module namespace.  The boot-writer fixture above
        # only patches its own copy, so the mirrored write path would otherwise
        # read the real witness and reject every transaction fixture.
        stack.enter_context(mock.patch.object(
            offline_boot_writer, 'read_boot_lock',
            return_value=trust['lockState']))

        candidates = {}
        for slot in SLOTS:
            source = Path(trust['sources']['slots'][slot]['boot']['path']).read_bytes()
            buf = bytearray(source)
            buf[256] ^= 0x10 + ord(slot)
            buf[-3] ^= 0x20 + ord(slot)
            path = root / ('candidate_boot_%s.img' % slot)
            path.write_bytes(buf)
            candidates[slot] = {
                'schemaVersion': 1, 'phase': 'candidate_prepared', 'slot': slot,
                'model': trust['physical']['model'], 'mode': trust['mode'],
                'transactionUuid': wf.TX_UUID, 'deployment': wf.TARGET_DEP,
                'maxRawBytes': 32768,
                'candidate': {'path': str(path), 'bytes': len(buf),
                              'sha256': hashlib.sha256(buf).hexdigest()},
                'sourceBootSha256': trust['sources']['slots'][slot]['boot']['sha256'],
                'baseImageSha256': '1' * 64,
                'rootPublicKeySha256': '2' * 64,
                'bootPublicKeySha256': '3' * 64,
                'payloadDigestMatchesSignedMetadata': True,
            }

        snapshot = dict(armada_os.snapshot())
        snapshot.update({
            'repository': next(iter(armada_os.REPOSITORIES)),
            'staged': None,
            'rollback_queued': False,
        })
        stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snapshot))
        stack.enter_context(mock.patch.object(trial_confirmation, 'UPDATE_LOCK_PATH', lock_path))
        stack.enter_context(mock.patch.object(image_source, 'fetch_online_image', return_value={
            'package': copy.deepcopy(PACKAGE),
            'commit': commit,
            'originRef': 'armada/stable',
        }))
        stack.enter_context(mock.patch.object(
            boot_trust, 'capture_boot_trust', side_effect=lambda **kw: copy.deepcopy(trust)))
        stack.enter_context(mock.patch.object(
            deployment_store, 'append_verified_deployment',
            side_effect=lambda **kw: {'model': kw['model'], 'deployment': wf.TARGET_DEP}))
        stack.enter_context(mock.patch.object(
            deployment_store, 'ensure_deployment_binding',
            side_effect=lambda **kw: {'deployment': kw['binding'], 'changed': False}))
        stack.enter_context(mock.patch.object(
            boot_materials, 'prepare_boot_materials',
            side_effect=lambda **kw: {
                'model': kw['model'],
                'deployment': kw['target_binding'],
                'transactionUuid': kw['transaction_uuid'],
            }))

        def fake_candidate(**kw):
            built = copy.deepcopy(candidates[kw['slot']])
            built['transactionUuid'] = kw['materials']['transactionUuid']
            built['deployment'] = kw['materials']['deployment']
            built['sourceBootSha256'] = kw['slot_files']['boot']['sha256']
            return built

        stack.enter_context(mock.patch.object(
            boot_candidate, 'prepare_boot_candidate', side_effect=fake_candidate))

        case.env.opened.clear()
        case.env.closed.clear()
        case.opens.clear()
        return act_test, case, trust, candidates, sysroot, work_dir

    @staticmethod
    def _run(sysroot: Path, work_dir: Path, channel: str = 'stable'):
        return tx_mod.run_online_transaction(
            channel_url='https://example.invalid/channel.json',
            signature_url='https://example.invalid/channel.json.sig',
            expected_channel=channel,
            sysroot_path=sysroot,
            work_dir=work_dir,
        )

    @staticmethod
    def _boot_bytes(case, slot, bs):
        part = case.trust['sources']['partitions'][f'boot_{slot}']
        return case.env.images[part['lun']].read_bytes()[
            part['firstLba'] * bs:(part['firstLba'] + part['blockCount']) * bs]

    def test_online_transaction_mirrors_both_slots(self) -> None:
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), ExitStack() as stack:
                        act_test, case, trust, candidates, sysroot, work = self._prepare(
                            stack, bs=bs, current=current, board=board)
                        unchanged_before = {
                            index: image.read_bytes()
                            for index, image in enumerate(case.env.images)
                            if index != 4
                        }
                        status = self._run(sysroot, work)
                        self.assertEqual(status, json.loads((work / 'status.json').read_bytes()))
                        self.assertEqual(status['state'], 'awaiting_reboot')
                        self.assertEqual(status['phase'], 'awaiting_reboot')
                        self.assertTrue(status['requiresReboot'])
                        self.assertEqual(status['bootWriteState'], 'written')
                        self.assertEqual(status['bootPolicy'], 'dual-boot-no-slot-control')
                        self.assertEqual(status['slotControlState'], 'not_issued')
                        self.assertNotIn('activationOutcome', status)

                        plan = json.loads((work / 'plan.json').read_bytes())
                        self.assertEqual(plan['bootPolicy'], 'dual-boot-no-slot-control')
                        self.assertEqual(plan['fromSlot'], 'ab'[current])
                        self.assertEqual(plan['targetSlot'], 'ab'[1 - current])

                        # No Android slot selection or boot-control write: only
                        # the boot partition LUN may change, and no selector,
                        # misc/BCB or other raw Android state was touched.
                        self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
                        for index, image in enumerate(case.env.images):
                            if index != 4:
                                self.assertEqual(image.read_bytes(), unchanged_before[index])
                        for slot in SLOTS:
                            expected = Path(candidates[slot]['candidate']['path']).read_bytes()
                            self.assertEqual(self._boot_bytes(case, slot, bs), expected)
                            self.assertEqual(plan['mirroredBootSha256'][slot],
                                             candidates[slot]['candidate']['sha256'])

                        snap = store.load(case.journal.fd, case.journal.part, board)
                        record = snap.record
                        self.assertIsNotNone(record)
                        self.assertEqual(record.state, rec.PREPARED)
                        self.assertEqual(record.policy, rec.OFFLINE_DUAL)
                        self.assertEqual(record.from_slot, current)
                        self.assertEqual(record.target_slot, 1 - current)
                        self.assertEqual(record.old_boot_sha256,
                                         bytes.fromhex(plan['originalBootSha256']['ab'[current]]))
                        self.assertEqual(record.new_boot_sha256,
                                         bytes.fromhex(plan['mirroredBootSha256']['ab'[1 - current]]))
                        self.assertEqual(record.other_boot_sha256,
                                         bytes.fromhex(plan['mirroredBootSha256']['ab'[current]]))
                        self.assertEqual(record.old_commit, bytes.fromhex('a' * 64))
                        self.assertEqual(record.new_commit, bytes.fromhex('b' * 64))
                        self.assertEqual(record.transaction_uuid,
                                         uuid.UUID(status['transactionId']).bytes)
                        act_test._assert_all_closed(case)

    def test_online_transaction_supplies_current_commit(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            fetch = stack.enter_context(mock.patch.object(
                image_source, 'fetch_online_image',
                return_value={
                    'package': copy.deepcopy(PACKAGE), 'commit': 'b' * 64,
                    'originRef': 'armada/stable', 'phase': 'downloaded'}))
            status = self._run(sysroot, work)
            self.assertEqual(status['state'], 'awaiting_reboot')
            self.assertEqual(fetch.call_count, 1)
            kwargs = fetch.call_args.kwargs
            self.assertEqual(kwargs['current_commit'], 'a' * 64)
            self.assertEqual(kwargs['expected_channel'], 'stable')
            self.assertEqual(kwargs['sysroot_path'], sysroot)
            self.assertEqual(kwargs['work_dir'], work / 'download')

    def test_online_transaction_no_op_and_preflight_failures(self) -> None:
        with self.subTest('up_to_date'), ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(
                stack, commit='a' * 64)
            with mock.patch.object(boot_trust, 'capture_boot_trust') as cap_mock:
                status = self._run(sysroot, work)
                cap_mock.assert_not_called()
            self.assertEqual(status['state'], 'up_to_date')
            self.assertEqual(status['phase'], 'complete')
            self.assertFalse(status['requiresReboot'])
            self.assertEqual(status['bootWriteState'], 'not_started')
            self.assertFalse((work / 'download').exists())
            act_test._assert_all_closed(case)

        with self.subTest('channel_mismatch'), ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            snapshot = dict(armada_os.snapshot())
            snapshot.update({
                'repository': next(iter(armada_os.REPOSITORIES)),
                'channel': 'beta', 'staged': None, 'rollback_queued': False,
            })
            stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snapshot))
            fetch = stack.enter_context(mock.patch.object(image_source, 'fetch_online_image'))
            with self.assertRaisesRegex(
                    tx_mod.SystemTransactionError,
                    'requested channel differs from configured channel') as ctx:
                self._run(sysroot, work)
            fetch.assert_not_called()
            self.assertFalse(work.exists())
            self.assertEqual(ctx.exception.result['bootWriteState'], 'not_started')

        with self.subTest('downloader_error'), ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            stack.enter_context(mock.patch.object(
                image_source, 'fetch_online_image', side_effect=RuntimeError('net down')))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'not_started')
            self.assertEqual(json.loads((work / 'status.json').read_bytes()), res)

    def test_boot_write_failure_never_reports_ready(self) -> None:
        for restore, expected_state, expected_write in (
            ('not_needed', 'failed', 'not_started'),
            ('restored', 'failed', 'restored'),
            ('failed', 'uncertain', 'uncertain'),
        ):
            with self.subTest(restore=restore), ExitStack() as stack:
                act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
                cause = offline_boot_writer.BootWriteError('write boom', restore)
                stack.enter_context(mock.patch.object(
                    offline_boot_writer, 'write_offline_boots', side_effect=cause))
                with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                    self._run(sysroot, work)
                self.assertIs(ctx.exception.__cause__, cause)
                res = ctx.exception.result
                self.assertEqual(res['state'], expected_state)
                self.assertFalse(res['requiresReboot'])
                self.assertEqual(res['bootWriteState'], expected_write)
                self.assertEqual(res['bootRestoreState'], restore)
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)

    def test_partial_mirrored_write_restores_before_commit(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            before = [image.read_bytes() for image in case.env.images]
            first = 'ab'[1 - case.current]
            part = case.trust['sources']['partitions']['boot_%s' % first]
            offset = part['firstLba'] * case.bs
            original = self._boot_bytes(case, first, case.bs)
            candidate = Path(candidates[first]['candidate']['path']).read_bytes()
            # A proper prefix that carries the candidate's own differing bytes,
            # so a landed write is observable and distinct from the original.
            prefix = candidate[:512]
            self.assertGreater(len(candidate), len(prefix))
            self.assertNotEqual(prefix, original[:len(prefix)])

            real_pwrite = offline_boot_writer._pwrite_all
            seen = {}
            calls = {'n': 0}

            def failing_pwrite(fd, data, offset):
                calls['n'] += 1
                if calls['n'] == 1:
                    self.assertEqual(data[:len(prefix)], prefix)
                    landed = data[:len(prefix)]
                    seen['before'] = os.pread(fd, len(landed), offset)
                    real_pwrite(fd, landed, offset)
                    seen['after'] = os.pread(fd, len(landed), offset)
                    seen.update(offset=offset, written=landed)
                    raise offline_boot_writer.BootWriteError(
                        'simulated device failure')
                return real_pwrite(fd, data, offset)

            stack.enter_context(mock.patch.object(
                offline_boot_writer, '_pwrite_all', side_effect=failing_pwrite))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'restored')
            self.assertEqual(res['bootRestoreState'], 'restored')
            self.assertEqual(res['slotResults'][first]['status'], 'restored')

            # The partial write reached the synthetic partition and was read
            # back before the failure was reported; the failure path then put
            # the original bytes back through the real helper.
            self.assertEqual(seen['offset'], offset)
            self.assertEqual(seen['written'], prefix)
            self.assertEqual(seen['before'], original[:len(prefix)])
            self.assertEqual(seen['after'], prefix)
            self.assertNotEqual(seen['after'], seen['before'])
            self.assertGreaterEqual(calls['n'], 2)
            self.assertEqual(
                res['slotResults'][first]['restoredSha256'],
                hashlib.sha256(original).hexdigest())
            self.assertEqual(self._boot_bytes(case, first, case.bs), original)
            self.assertEqual([image.read_bytes() for image in case.env.images], before)
            self.assertIsNone(store.load(
                case.journal.fd, case.journal.part, case.board).record)

    def test_journal_failure_before_write_restores_nonrunning_slot(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            inactive_before = self._boot_bytes(case, inactive, 512)
            stack.enter_context(mock.patch.object(
                store, 'save', side_effect=OSError('journal io')))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'restored')
            self.assertEqual(res['bootRestoreState'], 'restored')
            self.assertEqual(res['slotResults'][inactive]['status'], 'restored')
            self.assertEqual(self._boot_bytes(case, inactive, 512), inactive_before)
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertIsNone(store.load(
                case.journal.fd, case.journal.part, case.board).record)

    def test_journal_marker_fsync_failure_is_ambiguous(self) -> None:
        # The very first journal fsync flushes the logical-empty marker before
        # copy A is touched, so no record can be durable yet.
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            real_fsync = os.fsync
            journal_ino = os.fstat(case.journal.fd).st_ino

            def fail_journal_fsync(fd):
                if os.fstat(fd).st_ino == journal_ino:
                    raise OSError(errno.EIO, 'simulated journal fsync failure')
                return real_fsync(fd)

            stack.enter_context(mock.patch.object(
                store.os, 'fsync', side_effect=fail_journal_fsync))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'uncertain')
            self.assertEqual(res['journalCommitState'], 'ambiguous')
            self.assertEqual(res['slotResults'][inactive]['status'], 'preserved')
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertEqual(
                self._boot_bytes(case, inactive, 512),
                Path(candidates[inactive]['candidate']['path']).read_bytes())
            # Copy A was never written, so the journal still loads as empty
            # and the whole initial write stays retryable.
            self.assertIsNone(store.load(
                case.journal.fd, case.journal.part, case.board).record)
            self.assertEqual(
                os.pread(case.journal.fd, rec.RECORD_BYTES,
                         store.REGION_OFFSET + rec.TAIL_OFFSET_A),
                bytes(rec.RECORD_BYTES),
            )
            self.assertEqual(
                os.pread(case.journal.fd, rec.RECORD_BYTES,
                         store.REGION_OFFSET + rec.TAIL_OFFSET_B),
                rec.EMPTY_MARKER,
            )

    def test_journal_record_fsync_failure_after_marker_is_ambiguous(self) -> None:
        # The marker fsync succeeds and only the record fsync fails, so the
        # PREPARED bytes are visible in copy A while their durability is
        # unproven.  That is ambiguous, never a committed or restorable state.
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            real_fsync = os.fsync
            journal_ino = os.fstat(case.journal.fd).st_ino
            calls = {'n': 0}

            def fail_second_journal_fsync(fd):
                if os.fstat(fd).st_ino == journal_ino:
                    calls['n'] += 1
                    if calls['n'] == 2:
                        raise OSError(errno.EIO, 'simulated record fsync failure')
                return real_fsync(fd)

            stack.enter_context(mock.patch.object(
                store.os, 'fsync', side_effect=fail_second_journal_fsync))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(calls['n'], 2)
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'uncertain')
            self.assertEqual(res['journalCommitState'], 'ambiguous')
            self.assertEqual(res['slotResults'][inactive]['status'], 'preserved')
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertEqual(
                self._boot_bytes(case, inactive, 512),
                Path(candidates[inactive]['candidate']['path']).read_bytes())

            # Copy A holds the complete PREPARED record and copy B the durable
            # marker; the running boot extent was never opened for writing.
            snap = store.load(case.journal.fd, case.journal.part, case.board)
            self.assertEqual(snap.selected, 0)
            self.assertEqual(snap.record.state, rec.PREPARED)
            self.assertEqual(snap.record.sequence, 1)
            self.assertEqual(
                os.pread(case.journal.fd, rec.RECORD_BYTES,
                         store.REGION_OFFSET + rec.TAIL_OFFSET_A),
                rec.encode_record(snap.record),
            )
            self.assertEqual(
                os.pread(case.journal.fd, rec.RECORD_BYTES,
                         store.REGION_OFFSET + rec.TAIL_OFFSET_B),
                rec.EMPTY_MARKER,
            )
            act_test._assert_all_closed(case)

    def test_journal_cas_mismatch_is_ambiguous_and_preserves_alternate(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            real_load = store.load
            calls = {'n': 0}

            def load(fd, partuuid, board):
                calls['n'] += 1
                snapshot = real_load(fd, partuuid, board)
                if calls['n'] == 2:
                    return dataclasses.replace(
                        snapshot, record=dataclasses.replace(
                            case.journal.sample(sequence=1),
                            partition_uuid=snapshot.layout.partition_uuid,
                            installation_uuid=snapshot.layout.installation_uuid))
                return snapshot

            stack.enter_context(mock.patch.object(store, 'load', side_effect=load))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'uncertain')
            self.assertEqual(res['journalCommitState'], 'ambiguous')
            self.assertEqual(res['slotResults'][inactive]['status'], 'preserved')
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertEqual(
                self._boot_bytes(case, inactive, 512),
                Path(candidates[inactive]['candidate']['path']).read_bytes())

    def test_running_write_failure_after_commit_preserves_alternate(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            running_part = case.trust['sources']['partitions']['boot_%s' % running]
            inactive_part = case.trust['sources']['partitions']['boot_%s' % inactive]
            running_offset = running_part['firstLba'] * 512
            inactive_offset = inactive_part['firstLba'] * 512
            candidate = Path(candidates[running]['candidate']['path']).read_bytes()
            real_pwrite = offline_boot_writer._pwrite_all
            attempts = []

            def failing_pwrite(fd, data, offset):
                attempts.append((offset, bytes(data)))
                # The writer retries a running-extent failure exactly once
                # after the durable journal commit.  Fail both the initial
                # attempt and that sole retry so the failure stays permanent.
                if len(attempts) in (2, 3):
                    raise offline_boot_writer.BootWriteError(
                        'simulated running-slot failure')
                return real_pwrite(fd, data, offset)

            stack.enter_context(mock.patch.object(
                offline_boot_writer, '_pwrite_all', side_effect=failing_pwrite))
            with self.assertRaisesRegex(
                    tx_mod.SystemTransactionError,
                    'failed twice after the durable journal commit') as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'written')
            self.assertEqual(res['journalCommitState'], 'committed')
            self.assertEqual(res['slotControlState'], 'not_issued')
            self.assertEqual(res['slotResults'][inactive]['status'], 'preserved')
            self.assertEqual(res['slotResults'][inactive]['readbackSha256'],
                             candidates[inactive]['candidate']['sha256'])
            self.assertEqual(res['slotResults'][running]['status'], 'writing')
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertEqual(
                self._boot_bytes(case, inactive, 512),
                Path(candidates[inactive]['candidate']['path']).read_bytes())

            # One write to the alternate extent, then at most two attempts on
            # the running extent: the initial one and its single retry, with
            # identical bytes at the same offset.
            self.assertEqual(len(attempts), 3)
            self.assertEqual(attempts[0][0], inactive_offset)
            self.assertEqual([attempt[0] for attempt in attempts[1:]],
                             [running_offset, running_offset])
            self.assertEqual(attempts[1][1], candidate)
            self.assertEqual(attempts[2], attempts[1])

            # No Android slot selection, success marking or restart request.
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            record = store.load(
                case.journal.fd, case.journal.part, case.board).record
            self.assertEqual(record.state, rec.PREPARED)

    def test_final_status_failure_after_commit_never_suggests_reboot(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
            real_save = tx_mod._save_json

            def save_json(directory, name, value):
                if name == 'status.json' and value.get('state') == 'awaiting_reboot':
                    raise OSError('final status disk full')
                return real_save(directory, name, value)

            stack.enter_context(mock.patch.object(
                tx_mod, '_save_json', side_effect=save_json))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'written')
            self.assertEqual(res['journalCommitState'], 'committed')
            self.assertEqual(res['slotControlState'], 'not_issued')
            self.assertNotIn('persistenceError', res)
            for slot in SLOTS:
                self.assertEqual(
                    self._boot_bytes(case, slot, 512),
                    Path(candidates[slot]['candidate']['path']).read_bytes())
            self.assertEqual(
                store.load(case.journal.fd, case.journal.part, case.board).record.state,
                rec.PREPARED)
            act_test._assert_all_closed(case)

    def test_confirmation_accepts_either_physical_origin(self) -> None:
        # The transaction origin and the physical slot that actually booted
        # are independent.  Each pair is confirmed first with the other slot's
        # mirrored digest (must be rejected without a journal write) and then
        # with its own digest (must confirm).
        for origin in (0, 1):
            for actual in (0, 1):
                with self.subTest(origin=origin, actual=actual), ExitStack() as stack:
                    act_test, case, trust, candidates, sysroot, work = self._prepare(
                        stack, current=origin)
                    status = self._run(sysroot, work)
                    self.assertEqual(status['state'], 'awaiting_reboot')
                    plan = json.loads((work / 'plan.json').read_bytes())
                    self.assertEqual(plan['fromSlot'], 'ab'[origin])
                    self.assertEqual(plan['targetSlot'], 'ab'[1 - origin])
                    snap = store.load(case.journal.fd, case.journal.part, case.board)
                    record = snap.record
                    trial = dataclasses.replace(
                        record, sequence=record.sequence + 1, state=rec.TRIAL,
                        remaining=record.remaining - 1)
                    store.save(case.journal.fd, snap, trial)

                    self.assertEqual(trial.from_slot, origin)
                    self.assertEqual(trial.target_slot, 1 - origin)
                    correct = rec.expected_boot_sha256(trial, actual)
                    if actual == trial.target_slot:
                        self.assertEqual(correct, bytes(trial.new_boot_sha256))
                        swapped = bytes(trial.other_boot_sha256)
                    else:
                        self.assertEqual(actual, trial.from_slot)
                        self.assertEqual(correct, bytes(trial.other_boot_sha256))
                        swapped = bytes(trial.new_boot_sha256)
                    self.assertNotEqual(correct, swapped)

                    def make_evidence(digest, booted=actual):
                        return boot_health.BootEvidence(
                            transaction_uuid=bytes(trial.transaction_uuid),
                            actual_slot=booted, deployment=wf.TARGET_DEP,
                            kernel_release='6.12.0-armada',
                            boot_sha256=digest)

                    evidence = {'value': make_evidence(correct)}
                    rt_ev = runtime_health.RuntimeEvidence(
                        boot_id='boot-1', session_id='session-1', uid=1000,
                        display=':0', services=runtime_health.REQUIRED_UNITS)
                    stack.enter_context(mock.patch.object(
                        trial_confirmation, 'collect_boot_evidence',
                        side_effect=lambda _record: evidence['value']))
                    stack.enter_context(mock.patch.object(
                        trial_confirmation, 'collect_runtime_evidence', return_value=rt_ev))
                    stack.enter_context(mock.patch.object(
                        trial_confirmation.time, 'sleep', return_value=None))
                    stack.enter_context(mock.patch.object(
                        trial_confirmation, 'SYSROOT_PATH', sysroot))
                    stack.enter_context(mock.patch.object(
                        deployment_store, 'promote_confirmed_deployment',
                        side_effect=lambda **kw: {
                            'deployment': kw['current_binding'],
                            'previousDeployment': kw['previous_binding'],
                            'changed': False,
                        }))
                    stack.enter_context(mock.patch.object(
                        trial_confirmation.finalization_receipt, 'write',
                        return_value={}))

                    evidence['value'] = make_evidence(swapped)
                    with self.assertRaisesRegex(
                            trial_confirmation.ConfirmationError,
                            'boot_sha256'):
                        trial_confirmation.confirm_trial()
                    pending = store.load(
                        case.journal.fd, case.journal.part, case.board).record
                    self.assertEqual(pending, trial)
                    self.assertEqual(pending.state, rec.TRIAL)

                    evidence['value'] = make_evidence(correct)
                    conf = trial_confirmation.confirm_trial()
                    self.assertEqual(conf['state'], 'confirmed')
                    self.assertEqual(conf['bootSlot'], 'ab'[actual])
                    self.assertEqual(conf['targetSlot'], 'ab'[trial.target_slot])
                    self.assertEqual(conf['transactionId'], status['transactionId'])
                    self.assertEqual(conf['deployment'], wf.TARGET_DEP)
                    self.assertEqual(conf['bootPolicy'], 'dual-boot-no-slot-control')
                    self.assertEqual(conf['slotControlState'], 'not_issued')
                    self.assertNotIn('firmwareSlotSuccess', conf)
                    confirmed = store.load(
                        case.journal.fd, case.journal.part, case.board).record
                    self.assertEqual(confirmed.state, rec.CONFIRMED)
                    self.assertEqual(confirmed.sequence, trial.sequence + 1)
                    self.assertEqual(confirmed.from_slot, origin)
                    self.assertEqual(confirmed.target_slot, 1 - origin)
                    self.assertEqual(bytes(confirmed.transaction_uuid),
                                     bytes(trial.transaction_uuid))
                    self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)

    def _prepare_rollback(self, stack):
        act_test, case, trust, candidates, sysroot, work = self._prepare(stack)
        case.env.context['deployment'] = wf.TARGET_DEP
        physical = physical_slots.inspect_physical_slots()
        self.assertEqual(physical['deployment'], wf.TARGET_DEP)
        trust = copy.deepcopy(trust)
        trust['physical'] = physical
        trust['sources']['physical'] = physical
        stack.enter_context(mock.patch.object(
            boot_trust, 'capture_boot_trust',
            side_effect=lambda **kw: copy.deepcopy(trust)))

        snap = store.load(case.journal.fd, case.journal.part, case.board)
        confirmed = rec.TrialRecord(
            sequence=1, partition_uuid=snap.layout.partition_uuid,
            installation_uuid=snap.layout.installation_uuid,
            transaction_uuid=uuid.uuid4().bytes, state=rec.CONFIRMED,
            from_slot=case.current, target_slot=1 - case.current,
            remaining=2, attempt_limit=3, old_serial=0, new_serial=0,
            os_name='armada', old_commit=bytes.fromhex('a' * 64),
            new_commit=bytes.fromhex('b' * 64),
            old_boot_sha256=bytes.fromhex('4' * 64),
            new_boot_sha256=bytes.fromhex('5' * 64),
            plan_sha256=bytes.fromhex('6' * 64),
            policy=rec.OFFLINE_DUAL,
            other_boot_sha256=bytes.fromhex('7' * 64))
        store.save(case.journal.fd, snap, confirmed)
        evidence = boot_health.BootEvidence(
            transaction_uuid=bytes(confirmed.transaction_uuid),
            actual_slot=case.current,
            deployment='armada/%s.0' % ('b' * 64),
            kernel_release='6.12.0-armada',
            boot_sha256=(bytes(confirmed.other_boot_sha256)
                         if case.current == confirmed.from_slot
                         else bytes(confirmed.new_boot_sha256)))
        stack.enter_context(mock.patch.object(
            trial_confirmation, 'collect_boot_evidence', return_value=evidence))
        stack.enter_context(mock.patch.object(
            trial_confirmation, '_finalize_deployment', return_value={}))

        snapshot = dict(armada_os.snapshot())
        snapshot['rollback'] = {
            'incompatible': False, 'checksum': 'a' * 64, 'serial': 0,
            'stateroot': 'armada', 'version': '1.2.2',
        }
        stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snapshot))
        return act_test, case, trust, candidates, sysroot, work

    def test_rollback_repacks_both_slots_from_retained_deployment(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare_rollback(stack)
            fetch = stack.enter_context(mock.patch.object(image_source, 'fetch_online_image'))

            status = tx_mod.run_rollback_transaction(
                expected_checksum='a' * 64, sysroot_path=sysroot, work_dir=work)
            fetch.assert_not_called()
            self.assertEqual(status['state'], 'awaiting_reboot')
            self.assertEqual(status['bootPolicy'], 'dual-boot-no-slot-control')
            self.assertEqual(status['newDeployment'], 'armada/%s.0' % ('a' * 64))
            plan = json.loads((work / 'plan.json').read_bytes())
            self.assertEqual(plan['operation'], 'rollback')
            self.assertEqual(plan['bootPolicy'], 'dual-boot-no-slot-control')
            for slot in SLOTS:
                expected = Path(candidates[slot]['candidate']['path']).read_bytes()
                self.assertEqual(self._boot_bytes(case, slot, 512), expected)
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            record = store.load(case.journal.fd, case.journal.part, case.board).record
            self.assertEqual(record.state, rec.PREPARED)
            self.assertEqual(record.policy, rec.OFFLINE_DUAL)
            self.assertEqual(record.new_commit, bytes.fromhex('a' * 64))
            act_test._assert_all_closed(case)

    def test_rollback_running_write_failure_after_commit_preserves_alternate(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare_rollback(stack)
            running = 'ab'[case.current]
            inactive = 'ab'[1 - case.current]
            running_before = self._boot_bytes(case, running, 512)
            running_part = case.trust['sources']['partitions']['boot_%s' % running]
            inactive_part = case.trust['sources']['partitions']['boot_%s' % inactive]
            running_offset = running_part['firstLba'] * 512
            inactive_offset = inactive_part['firstLba'] * 512
            candidate = Path(candidates[running]['candidate']['path']).read_bytes()
            real_pwrite = offline_boot_writer._pwrite_all
            attempts = []

            def failing_pwrite(fd, data, offset):
                attempts.append((offset, bytes(data)))
                # Same bounded retry as the online path: fail the initial
                # running-extent attempt and its sole retry.
                if len(attempts) in (2, 3):
                    raise offline_boot_writer.BootWriteError(
                        'simulated rollback running-slot failure')
                return real_pwrite(fd, data, offset)

            stack.enter_context(mock.patch.object(
                offline_boot_writer, '_pwrite_all', side_effect=failing_pwrite))
            with self.assertRaisesRegex(
                    tx_mod.SystemTransactionError,
                    'failed twice after the durable journal commit') as ctx:
                tx_mod.run_rollback_transaction(
                    expected_checksum='a' * 64, sysroot_path=sysroot, work_dir=work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'recovery_required')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'written')
            self.assertEqual(res['journalCommitState'], 'committed')
            self.assertEqual(res['slotControlState'], 'not_issued')
            self.assertEqual(res['slotResults'][inactive]['status'], 'preserved')
            self.assertEqual(res['slotResults'][inactive]['readbackSha256'],
                             candidates[inactive]['candidate']['sha256'])
            self.assertEqual(res['slotResults'][running]['status'], 'writing')
            self.assertEqual(self._boot_bytes(case, running, 512), running_before)
            self.assertEqual(
                self._boot_bytes(case, inactive, 512),
                Path(candidates[inactive]['candidate']['path']).read_bytes())

            # One write to the alternate extent, then at most two attempts on
            # the running extent: the initial one and its single retry, with
            # identical bytes at the same offset.
            self.assertEqual(len(attempts), 3)
            self.assertEqual(attempts[0][0], inactive_offset)
            self.assertEqual([attempt[0] for attempt in attempts[1:]],
                             [running_offset, running_offset])
            self.assertEqual(attempts[1][1], candidate)
            self.assertEqual(attempts[2], attempts[1])

            # No Android slot selection, success marking or restart request.
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            record = store.load(case.journal.fd, case.journal.part, case.board).record
            self.assertEqual(record.state, rec.PREPARED)
            self.assertEqual(record.old_commit, bytes.fromhex('b' * 64))
            self.assertEqual(record.new_commit, bytes.fromhex('a' * 64))
            act_test._assert_all_closed(case)

    @staticmethod
    def _confirm_record(case):
        snap = store.load(case.journal.fd, case.journal.part, case.board)
        # The boot hook advances PREPARED into its first trial attempt by
        # decrementing the attempt counter, then confirmation records CONFIRMED
        # with that same remaining count.  Each step consumes the next journal
        # sequence, exactly as the native store requires.
        trial = dataclasses.replace(
            snap.record, sequence=snap.record.sequence + 1, state=rec.TRIAL,
            remaining=snap.record.remaining - 1)
        store.save(case.journal.fd, snap, trial)
        snap = store.load(case.journal.fd, case.journal.part, case.board)
        confirmed = dataclasses.replace(
            snap.record, sequence=snap.record.sequence + 1, state=rec.CONFIRMED)
        store.save(case.journal.fd, snap, confirmed)
        return confirmed

    @staticmethod
    def _live_trust(case, base_trust, root, tag):
        physical = physical_slots.inspect_physical_slots()
        extras = ['recovery_a', 'recovery_b',
                  'vbmeta_system_a', 'vbmeta_system_b']
        if physical['model'] == 'TB323FU':
            extras.append('efisp')
        sources = boot_sources.capture_boot_sources(
            physical, root / tag, extra_labels=extras)
        trust = copy.deepcopy(base_trust)
        trust['physical'] = physical
        trust['sources'] = sources
        trust['lockState'] = 'unlocked'
        return trust

    def test_consecutive_updates_then_explicit_rollback_mirror_without_selector(self) -> None:
        with ExitStack() as stack:
            act_test, case, initial_trust, candidates, sysroot, work = self._prepare(
                stack, board=1, current=0, bs=512)
            state = {'trust': copy.deepcopy(initial_trust)}
            stack.enter_context(mock.patch.object(
                boot_trust, 'capture_boot_trust',
                side_effect=lambda **kw: copy.deepcopy(state['trust'])))
            stack.enter_context(mock.patch.object(
                trial_confirmation, '_finalize_deployment', return_value={}))

            # Each deployment must repack its own candidate bytes from the
            # boot images that are live at that step, with a claim that
            # matches the written file exactly.  Reusing one static fixture
            # per slot would let a stale image pass as the new deployment.
            initial_boot = {slot: self._boot_bytes(case, slot, 512) for slot in SLOTS}
            builds = {}

            def build_candidate(**kw):
                slot = kw['slot']
                deployment = kw['materials']['deployment']
                source = self._boot_bytes(case, slot, 512)
                buf = bytearray(source)
                seed = hashlib.sha256(
                    ('%s/%s' % (deployment, slot)).encode('ascii')).digest()
                # The fixture boot image has a fixed size and the native slot
                # inspection derives the bootability witness from its header
                # magic.  Only the tail payload may vary: XORing the leading
                # bytes would leave the next inspection reporting no bootable
                # slot, even though the boot write itself succeeded.
                self.assertEqual(bytes(buf[:8]), b'ANDROID!')
                self.assertGreaterEqual(len(buf), len(seed))
                tail = len(buf) - len(seed)
                for index in range(len(seed)):
                    buf[tail + index] ^= seed[index]
                self.assertEqual(bytes(buf[:tail]), source[:tail])
                self.assertNotEqual(bytes(buf), source)
                digest = hashlib.sha256(bytes(buf)).hexdigest()
                path = sysroot.parent / (
                    'candidate_%s_%s.img' % (
                        hashlib.sha256(
                            ('%s@%s' % (deployment, kw['slot_files']['boot']['sha256']))
                            .encode('ascii')).hexdigest()[:12],
                        slot))
                path.write_bytes(bytes(buf))
                report = copy.deepcopy(candidates[slot])
                report.update({
                    'deployment': deployment,
                    'transactionUuid': kw['materials']['transactionUuid'],
                    'sourceBootSha256': kw['slot_files']['boot']['sha256'],
                    'candidate': {'path': str(path), 'bytes': len(buf),
                                  'sha256': digest},
                })
                builds[(deployment, slot)] = {'bytes': bytes(buf), 'sha256': digest}
                return report

            stack.enter_context(mock.patch.object(
                boot_candidate, 'prepare_boot_candidate', side_effect=build_candidate))

            def evidence(record):
                actual = 0 if case.env.context['actualSlot'] == 'a' else 1
                return boot_health.BootEvidence(
                    transaction_uuid=bytes(record.transaction_uuid),
                    actual_slot=actual,
                    deployment=case.env.context['deployment'],
                    kernel_release='6.12.0-armada',
                    boot_sha256=rec.expected_boot_sha256(record, actual))

            stack.enter_context(mock.patch.object(
                trial_confirmation, 'collect_boot_evidence',
                side_effect=evidence))

            def reboot(record):
                index = record.target_slot
                context = case.env.context
                context['actualSlot'] = 'ab'[index]
                context['inactiveSlot'] = 'ba'[index]
                context['deployment'] = 'armada/%s.%d' % (
                    record.new_commit.hex(), record.new_serial)
                state['trust'] = self._live_trust(
                    case, state['trust'], sysroot.parent,
                    'consecutive_capture_%d' % record.sequence)

            def run_update(commit, deployment, work_dir):
                stack.enter_context(mock.patch.object(
                    image_source, 'fetch_online_image',
                    return_value={
                        'package': copy.deepcopy(PACKAGE),
                        'commit': commit,
                        'originRef': 'armada/stable',
                    }))
                stack.enter_context(mock.patch.object(
                    deployment_store, 'append_verified_deployment',
                    side_effect=lambda **kw: {
                        'model': kw['model'], 'deployment': deployment}))
                return self._run(sysroot, work_dir)

            first = run_update('b' * 64, wf.TARGET_DEP, work)
            self.assertEqual(first['state'], 'awaiting_reboot')
            self.assertEqual(first['bootWriteState'], 'written')
            first_plan = json.loads((work / 'plan.json').read_bytes())
            first_expected = {slot: builds[(wf.TARGET_DEP, slot)] for slot in SLOTS}
            for slot in SLOTS:
                self.assertEqual(first_plan['originalBootSha256'][slot],
                                 hashlib.sha256(initial_boot[slot]).hexdigest())
                self.assertEqual(first_plan['mirroredBootSha256'][slot],
                                 first_expected[slot]['sha256'])
                self.assertEqual(self._boot_bytes(case, slot, 512),
                                 first_expected[slot]['bytes'])
            reboot(self._confirm_record(case))

            second_deployment = 'armada/%s.0' % ('c' * 64)
            second_work = work.parent / 'tx_work_second'
            second = run_update('c' * 64, second_deployment, second_work)
            self.assertEqual(second['state'], 'awaiting_reboot')
            self.assertEqual(second['bootWriteState'], 'written')
            second_plan = json.loads((second_work / 'plan.json').read_bytes())
            self.assertEqual(second_plan['fromSlot'], 'b')
            self.assertEqual(second_plan['targetSlot'], 'a')
            second_expected = {slot: builds[(second_deployment, slot)] for slot in SLOTS}
            for slot in SLOTS:
                self.assertEqual(second_plan['originalBootSha256'][slot],
                                 first_expected[slot]['sha256'])
                self.assertEqual(second_plan['mirroredBootSha256'][slot],
                                 second_expected[slot]['sha256'])
                self.assertEqual(self._boot_bytes(case, slot, 512),
                                 second_expected[slot]['bytes'])
                self.assertNotEqual(second_expected[slot]['bytes'],
                                    first_expected[slot]['bytes'])
            reboot(self._confirm_record(case))

            repo_snapshot = dict(armada_os.snapshot())
            repo_snapshot.update({
                'staged': None, 'rollback_queued': False,
                'rollback': {
                    'incompatible': False, 'checksum': 'b' * 64,
                    'serial': 0, 'stateroot': 'armada', 'version': '1.2.2',
                },
            })
            stack.enter_context(mock.patch.object(
                armada_os, 'snapshot', return_value=repo_snapshot))
            rollback_work = work.parent / 'tx_work_rollback'
            fetch = stack.enter_context(mock.patch.object(
                image_source, 'fetch_online_image'))
            final = tx_mod.run_rollback_transaction(
                expected_checksum='b' * 64, sysroot_path=sysroot,
                work_dir=rollback_work)
            fetch.assert_not_called()

            self.assertEqual(final['state'], 'awaiting_reboot')
            self.assertEqual(final['phase'], 'awaiting_reboot')
            self.assertTrue(final['requiresReboot'])
            self.assertEqual(final['bootPolicy'], 'dual-boot-no-slot-control')
            self.assertEqual(final['bootWriteState'], 'written')
            self.assertEqual(final['slotControlState'], 'not_issued')

            rollback_plan = json.loads(
                (rollback_work / 'plan.json').read_bytes())
            self.assertEqual(rollback_plan['operation'], 'rollback')
            self.assertEqual(rollback_plan['bootPolicy'],
                             'dual-boot-no-slot-control')
            self.assertEqual(rollback_plan['oldDeployment'],
                             'armada/%s.0' % ('c' * 64))
            self.assertEqual(rollback_plan['newDeployment'],
                             'armada/%s.0' % ('b' * 64))

            # Every consecutive transaction mirrors both physical boot slots
            # and never asks the Android slot-control shim to select a slot.
            # The rollback repacks the retained deployment from the images
            # that are live now, so its mirrored bytes differ from the
            # second update even though both target the same slots.
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            rollback_expected = {slot: builds[(wf.TARGET_DEP, slot)] for slot in SLOTS}
            for slot in SLOTS:
                self.assertEqual(rollback_plan['originalBootSha256'][slot],
                                 second_expected[slot]['sha256'])
                self.assertEqual(rollback_plan['mirroredBootSha256'][slot],
                                 rollback_expected[slot]['sha256'])
                self.assertEqual(self._boot_bytes(case, slot, 512),
                                 rollback_expected[slot]['bytes'])
                self.assertNotEqual(rollback_expected[slot]['bytes'],
                                    second_expected[slot]['bytes'])

            record = store.load(
                case.journal.fd, case.journal.part, case.board).record
            self.assertEqual(record.state, rec.PREPARED)
            self.assertEqual(record.policy, rec.OFFLINE_DUAL)
            self.assertEqual(record.old_commit, bytes.fromhex('c' * 64))
            self.assertEqual(record.new_commit, bytes.fromhex('b' * 64))
            act_test._assert_all_closed(case)

    def test_rollback_ensures_retained_binding_before_boot_writes(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare_rollback(stack)
            target = 'armada/%s.0' % ('a' * 64)
            ensure = stack.enter_context(mock.patch.object(
                deployment_store, 'ensure_deployment_binding',
                return_value={'deployment': target, 'changed': True}))
            write = stack.enter_context(mock.patch.object(
                offline_boot_writer, 'write_offline_boots',
                side_effect=RuntimeError('stop before boot write')))

            with self.assertRaises(tx_mod.SystemTransactionError):
                tx_mod.run_rollback_transaction(
                    expected_checksum='a' * 64, sysroot_path=sysroot, work_dir=work)

            ensure.assert_called_once_with(sysroot_path=sysroot, binding=target)
            write.assert_called_once()

    def test_rollback_binding_failure_stops_before_boot_writes(self) -> None:
        with ExitStack() as stack:
            act_test, case, trust, candidates, sysroot, work = self._prepare_rollback(stack)
            stack.enter_context(mock.patch.object(
                deployment_store, 'ensure_deployment_binding',
                side_effect=deployment_store.DeploymentError(
                    'retained target binding unavailable')))
            write = stack.enter_context(mock.patch.object(
                offline_boot_writer, 'write_offline_boots'))

            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                tx_mod.run_rollback_transaction(
                    expected_checksum='a' * 64, sysroot_path=sysroot, work_dir=work)

            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertEqual(res['bootWriteState'], 'not_started')
            self.assertFalse(res['requiresReboot'])
            write.assert_not_called()
            self.assertEqual(store.load(
                case.journal.fd, case.journal.part, case.board).record.state,
                rec.CONFIRMED)
            act_test._assert_all_closed(case)


if __name__ == '__main__':
    unittest.main()
