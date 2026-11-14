# SPDX-License-Identifier: GPL-3.0-only
"""Offline dual-boot slot activation and confirmation marking tests."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import dataclasses
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

import test_update_slot_activation as af
import test_update_boot_writer as wf
from updates import boot_health
from updates import slot_activation as activation
from updates import trial_record as rec
from updates import trial_store as store


class OfflineSlotActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        af.SlotActivationTests.setUpClass()
        cls.addClassCleanup(af.SlotActivationTests.doClassCleanups)
        cls.fixture = af.SlotActivationTests()
        cls.shim = af.SlotActivationTests.shim

    def _prepare_offline(self, stack: ExitStack, *, bs: int = 512, current: int = 0,
                         board: int = 1, native_mode: int = 0,
                         write_current: bool = True):
        case = self.fixture._prepare(
            stack, bs=bs, current=current, board=board, native_mode=native_mode)
        from_slot = 'ab'[case.current]
        target_slot = 'ab'[1 - case.current]
        orig_claim = case.trust['sources']['slots'][from_slot]['boot']
        orig_bytes = Path(orig_claim['path']).read_bytes()
        cur = bytearray(orig_bytes)
        cur[129] ^= 0x33
        cur[-2] ^= 0x66
        cur_bytes = bytes(cur)
        cur_sha = hashlib.sha256(cur_bytes).hexdigest()
        orig_sha = hashlib.sha256(orig_bytes).hexdigest()
        root = case.env.images[0].parent.parent
        cur_path = root / f'candidate_boot_{from_slot}.img'
        cur_path.write_bytes(cur_bytes)
        cur_claim = {
            'path': str(cur_path),
            'bytes': len(cur_bytes),
            'sha256': cur_sha,
        }
        if write_current:
            part = case.trust['sources']['partitions'][f'boot_{from_slot}']
            off = part['firstLba'] * case.bs
            fd = os.open(case.env.images[part['lun']], os.O_RDWR | os.O_CLOEXEC)
            try:
                self.assertEqual(os.pwrite(fd, cur_bytes, off), len(cur_bytes))
                os.fsync(fd)
            finally:
                os.close(fd)
        report = {
            'schemaVersion': 1,
            'phase': 'dual_boots_written',
            'model': case.report['model'],
            'fromSlot': from_slot,
            'slotOrder': [target_slot, from_slot],
            'transactionUuid': case.report['transactionUuid'],
            'deployment': case.report['deployment'],
            'physical': case.report['physical'],
            'writes': {
                from_slot: {
                    'sourceBootSha256': orig_sha,
                    'candidate': cur_claim,
                    'bytesWritten': len(cur_bytes),
                    'readbackSha256': cur_sha,
                },
                target_slot: {
                    'sourceBootSha256': case.report['sourceBootSha256'],
                    'candidate': case.report['candidate'],
                    'bytesWritten': case.report['bytesWritten'],
                    'readbackSha256': case.report['readbackSha256'],
                },
            },
        }
        snap = store.load(case.journal.fd, case.journal.part, case.board)
        record = dataclasses.replace(
            snap.record, sequence=2, policy=rec.OFFLINE_DUAL,
            old_boot_sha256=hashlib.sha256(orig_bytes).digest(),
            other_boot_sha256=bytes.fromhex(cur_sha))
        store.save(case.journal.fd, snap, record)
        case.env.opened.clear()
        case.env.closed.clear()
        case.opens.clear()
        case.offline_report = report
        case.current_claim = cur_claim
        case.current_original_sha = orig_sha
        self.assertNotEqual(orig_sha, cur_sha)
        self.assertNotEqual(cur_sha, report['writes'][target_slot]['readbackSha256'])
        return case

    def _boot_extent_bytes(self, case, slot):
        part = case.trust['sources']['partitions'][f'boot_{slot}']
        off = part['firstLba'] * case.bs
        size = part['blockCount'] * case.bs
        img = case.env.images[part['lun']].read_bytes()
        return img[off:off + size]

    def _no_rw_opens(self, case):
        self.assertEqual([
            1 for _, _, fl in case.opens if fl & os.O_ACCMODE == os.O_RDWR], [])

    def _assert_rejected_before_write(self, case, base, error):
        self.assertEqual(error.outcome, 'not_attempted')
        self.assertIsNone(error.native_result)
        self.assertTrue(str(error).strip())
        self.assertEqual(self.shim.fixture_calls(), 0)
        self._no_rw_opens(case)
        self.fixture._assert_disks_and_journal(case, base, gpt_changed=False)
        self.fixture._assert_all_closed(case)

    def test_offline_activation_matrix(self) -> None:
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), ExitStack() as stack:
                        case = self._prepare_offline(
                            stack, bs=bs, current=current, board=board)
                        base = self.fixture._baseline(case)
                        from_slot = 'ab'[current]
                        target_idx = 1 - current
                        target_slot = 'ab'[target_idx]
                        report = case.offline_report
                        res = activation.activate_offline_slots(report)
                        self.assertEqual(res, {
                            'schemaVersion': 1,
                            'phase': 'offline_slots_activated',
                            'model': case.trust['physical']['model'],
                            'fromSlot': from_slot,
                            'targetSlot': target_slot,
                            'selectedSlot': target_slot,
                            'transactionUuid': wf.TX_UUID,
                            'deployment': wf.TARGET_DEP,
                            'recordSequence': 2,
                            'readbackSha256':
                                report['writes'][target_slot]['readbackSha256'],
                            'currentReadbackSha256':
                                report['writes'][from_slot]['readbackSha256'],
                            'targetReadbackSha256':
                                report['writes'][target_slot]['readbackSha256'],
                        })
                        self.assertEqual(self.shim.fixture_set_calls(), 1)
                        self.assertEqual(
                            case.attribute.read_text(encoding='ascii').strip(),
                            str(target_idx + 1))
                        self.assertEqual(
                            af.slots._read_boot_state()[1]['actualSlot'], from_slot)
                        rw = [p for _, p, fl in case.opens
                              if (fl & os.O_ACCMODE) == os.O_RDWR]
                        self.assertEqual(len(rw), 7)
                        self.assertEqual(set(rw), set(case.env.images) | {case.bsg})
                        self.assertEqual(
                            self.fixture._inspect_selected_slot(case), target_idx)
                        self.fixture._assert_disks_and_journal(
                            case, base, gpt_changed=True)
                        self.fixture._assert_all_closed(case)

    def test_offline_report_and_journal_negatives(self) -> None:
        for kind in (
            'offline_rejects_online', 'online_rejects_offline', 'wrongpolicy',
            'wrongoldhash', 'wrongotherhash', 'missingwrites', 'reversedorder',
            'contradictorytarget', 'wrongbytes', 'wrongreadback',
            'zerosource', 'noncanonicalsource', 'changedcandidate',
        ):
            with self.subTest(kind=kind), ExitStack() as stack:
                case = self._prepare_offline(stack)
                report = copy.deepcopy(case.offline_report)
                from_slot = report['fromSlot']
                target_slot = report['slotOrder'][0]
                caller = activation.activate_offline_slots
                if kind == 'offline_rejects_online':
                    report = case.report
                elif kind == 'online_rejects_offline':
                    caller = activation.activate_prepared_slot
                else:
                    if kind in ('wrongpolicy', 'wrongoldhash', 'wrongotherhash'):
                        snap = store.load(
                            case.journal.fd, case.journal.part, case.board)
                        fields = {'sequence': 3}
                        if kind == 'wrongpolicy':
                            fields['policy'] = rec.ONLINE_SINGLE
                            fields['other_boot_sha256'] = b'\x00' * 32
                        elif kind == 'wrongoldhash':
                            fields['old_boot_sha256'] = bytes.fromhex(
                                report['writes'][from_slot]['readbackSha256'])
                        else:
                            fields['other_boot_sha256'] = bytes.fromhex(
                                report['writes'][target_slot]['readbackSha256'])
                        store.save(case.journal.fd, snap, dataclasses.replace(
                            snap.record, **fields))
                    elif kind == 'missingwrites':
                        del report['writes'][from_slot]
                    elif kind == 'reversedorder':
                        report['slotOrder'] = [from_slot, target_slot]
                    elif kind == 'contradictorytarget':
                        report['targetSlot'] = from_slot
                    elif kind == 'wrongbytes':
                        report['writes'][from_slot]['bytesWritten'] += 1
                    elif kind == 'wrongreadback':
                        report['writes'][from_slot]['readbackSha256'] = 'c' * 64
                    elif kind == 'zerosource':
                        report['writes'][target_slot]['sourceBootSha256'] = '0' * 64
                    elif kind == 'noncanonicalsource':
                        report['writes'][target_slot]['sourceBootSha256'] = 'A' * 64
                    elif kind == 'changedcandidate':
                        path = Path(report['writes'][from_slot]['candidate']['path'])
                        tampered = bytearray(path.read_bytes())
                        tampered[64] ^= 0x11
                        path.write_bytes(bytes(tampered))
                base = self.fixture._baseline(case)
                with self.assertRaises(activation.ActivationError) as ctx:
                    caller(report)
                self._assert_rejected_before_write(case, base, ctx.exception)

    def test_offline_current_boot_digest_mismatch(self) -> None:
        with ExitStack() as stack:
            case = self._prepare_offline(stack, write_current=False)
            base = self.fixture._baseline(case)
            with self.assertRaises(activation.ActivationError) as ctx:
                activation.activate_offline_slots(case.offline_report)
            self.assertIn(
                'current boot partition digest does not match',
                str(ctx.exception))
            self._assert_rejected_before_write(case, base, ctx.exception)

    def test_offline_preinvoke_drift_after_bsg_open(self) -> None:
        with ExitStack() as stack:
            case = self._prepare_offline(stack)
            base = self.fixture._baseline(case)
            from_slot = case.offline_report['fromSlot']
            part = case.trust['sources']['partitions'][f'boot_{from_slot}']
            lun = part['lun']
            drift_off = part['firstLba'] * case.bs + 256
            drift_byte = bytes([base.disks[lun][drift_off] ^ 0x5A])
            prev = os.open

            def drift_open(path, flags, *args, **kwargs):
                fd = prev(path, flags, *args, **kwargs)
                if Path(path) == Path(case.bsg) and flags & os.O_RDWR:
                    dfd = prev(case.env.images[lun], os.O_RDWR | os.O_CLOEXEC)
                    try:
                        self.assertEqual(wf.REAL_PWRITE(dfd, drift_byte, drift_off), 1)
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                return fd

            stack.enter_context(mock.patch.object(os, 'open', side_effect=drift_open))
            with self.assertRaises(activation.ActivationError) as ctx:
                activation.activate_offline_slots(case.offline_report)
            self.assertEqual(ctx.exception.outcome, 'not_attempted')
            self.assertIsNone(ctx.exception.native_result)
            self.assertIn(
                'active boot partition content drifted', str(ctx.exception))
            self.assertEqual(self.shim.fixture_calls(), 0)
            expected_disk = bytearray(base.disks[lun])
            expected_disk[drift_off] ^= 0x5A
            base.disks[lun] = bytes(expected_disk)
            self.fixture._assert_disks_and_journal(
                case, base, gpt_changed=False)
            self.fixture._assert_all_closed(case)

    def test_offline_native_undo_and_uncertain_modes(self) -> None:
        for mode in (1, 2):
            with self.subTest(mode=mode), ExitStack() as stack:
                case = self._prepare_offline(stack, native_mode=mode)
                base = self.fixture._baseline(case)
                report = case.offline_report
                from_slot = report['fromSlot']
                target_slot = report['slotOrder'][0]
                with self.assertRaises(activation.ActivationError) as ctx:
                    activation.activate_offline_slots(report)
                err = ctx.exception
                if mode == 1:
                    self.assertEqual(err.outcome, 'unchanged')
                    self.assertNotEqual(err.native_result, 0)
                    self.assertNotEqual(err.native_result, -30)
                    self.assertEqual(
                        case.attribute.read_text(encoding='ascii').strip(), '1')
                    self.fixture._assert_disks_and_journal(
                        case, base, gpt_changed=False)
                else:
                    self.assertEqual(err.outcome, 'uncertain')
                    self.assertEqual(err.native_result, -30)
                    self.fixture._assert_disks_and_journal(
                        case, base, gpt_changed=True)
                self.assertEqual(self.shim.fixture_set_calls(), 2)
                for slot, claim in (
                    (target_slot, report['writes'][target_slot]),
                    (from_slot, report['writes'][from_slot]),
                ):
                    self.assertEqual(
                        self._boot_extent_bytes(case, slot),
                        Path(claim['candidate']['path']).read_bytes())
                self.fixture._assert_all_closed(case)

    def _confirm_setup(self, case):
        actual_idx = case.current
        actual_slot = 'ab'[actual_idx]
        case.env.context['deployment'] = wf.TARGET_DEP
        snap = store.load(case.journal.fd, case.journal.part, case.board)
        record = dataclasses.replace(
            snap.record, sequence=3, state=rec.CONFIRMED, remaining=2)
        store.save(case.journal.fd, snap, record)
        snap = store.load(case.journal.fd, case.journal.part, case.board)
        case.env.opened.clear()
        case.env.closed.clear()
        case.opens.clear()
        return snap.record, actual_idx, actual_slot

    def _evidence_patch(self, record, actual_idx, *, sha=None, tx=None, dep=None):
        expected_deployment = (
            f'{record.os_name}/{record.new_commit.hex()}.{record.new_serial}')
        evidence = boot_health.BootEvidence(
            transaction_uuid=tx if tx is not None else record.transaction_uuid,
            actual_slot=actual_idx,
            deployment=dep if dep is not None else expected_deployment,
            kernel_release='6.1.0-fixture',
            boot_sha256=sha if sha is not None else record.other_boot_sha256,
        )
        return mock.patch.object(
            boot_health, 'collect_boot_evidence', return_value=evidence)

    def test_mark_confirmed_offline_matrix(self) -> None:
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), ExitStack() as stack:
                        case = self._prepare_offline(
                            stack, bs=bs, current=current, board=board)
                        record, actual_idx, actual_slot = self._confirm_setup(case)
                        self.assertEqual(
                            record.old_boot_sha256.hex(), case.current_original_sha)
                        self.assertNotEqual(
                            record.old_boot_sha256, record.other_boot_sha256)
                        base = self.fixture._baseline(case)
                        expected_deployment = (
                            f'{record.os_name}/{record.new_commit.hex()}'
                            f'.{record.new_serial}')
                        with self._evidence_patch(
                            record, actual_idx, sha=record.other_boot_sha256
                        ):
                            res = activation.mark_confirmed_slot(record)
                        self.assertEqual(res, {
                            'schemaVersion': 1,
                            'phase': 'slot_confirmed',
                            'model': case.trust['physical']['model'],
                            'targetSlot': actual_slot,
                            'deployment': expected_deployment,
                            'transactionUuid': wf.TX_UUID,
                            'recordSequence': 3,
                        })
                        self.assertEqual(self.shim.fixture_set_calls(), 0)
                        self.assertEqual(
                            case.attribute.read_text(encoding='ascii').strip(),
                            str(actual_idx + 1))
                        self.assertEqual(
                            self.fixture._inspect_selected_slot(case), actual_idx)
                        self.fixture._assert_disks_and_journal(
                            case, base, gpt_changed=True)
                        self.fixture._assert_all_closed(case)

    def test_mark_confirmed_negatives(self) -> None:
        for kind in ('wronghash', 'pending', 'wrongtx', 'onlinefrom'):
            with self.subTest(kind=kind), ExitStack() as stack:
                case = self._prepare_offline(stack)
                case.env.context['deployment'] = wf.TARGET_DEP
                snap = store.load(case.journal.fd, case.journal.part, case.board)
                valid_other_sha = snap.record.other_boot_sha256
                fields = {
                    'sequence': 3,
                    'state': rec.TRIAL if kind == 'pending' else rec.CONFIRMED,
                    'remaining': 2,
                }
                if kind == 'onlinefrom':
                    fields['policy'] = rec.ONLINE_SINGLE
                    fields['other_boot_sha256'] = b'\x00' * 32
                record = dataclasses.replace(snap.record, **fields)
                store.save(case.journal.fd, snap, record)
                snap = store.load(case.journal.fd, case.journal.part, case.board)
                record = snap.record
                case.env.opened.clear()
                case.env.closed.clear()
                case.opens.clear()
                sha = bytes.fromhex('d' * 64) if kind == 'wronghash' else valid_other_sha
                tx = b'\x99' * 16 if kind == 'wrongtx' else None
                base = self.fixture._baseline(case)
                with self._evidence_patch(record, case.current, sha=sha, tx=tx):
                    with self.assertRaises(activation.ActivationError) as ctx:
                        activation.mark_confirmed_slot(record)
                self._assert_rejected_before_write(case, base, ctx.exception)


if __name__ == '__main__':
    unittest.main()
