# SPDX-License-Identifier: GPL-3.0-only
"""System transaction orchestration integration tests."""

from __future__ import annotations

from contextlib import ExitStack
import copy
import dataclasses
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
import test_update_physical_slots as pf
import test_update_slot_activation as af
import test_update_trial_store as jf
from updates import (
    boot_candidate,
    boot_health,
    boot_materials,
    boot_sources,
    boot_trust,
    boot_writer as writer,
    deployment_store,
    image_source,
    physical_slots,
    runtime_health,
    slot_activation as activation,
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


class SystemTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        af.SlotActivationTests.setUpClass()
        cls.addClassCleanup(af.SlotActivationTests.doClassCleanups)

    def _prepare(self, stack: ExitStack, *, bs: int = 512, current: int = 0,
                 board: int = 1, commit: str = 'b' * 64):
        act_test = af.SlotActivationTests()
        captured = {}
        real_write = writer.write_inactive_boot

        def wrap_write(trust, candidate):
            captured['candidate'] = copy.deepcopy(candidate)
            return real_write(trust, candidate)

        with mock.patch.object(writer, 'write_inactive_boot', side_effect=wrap_write):
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

        self.assertEqual(af.SlotActivationTests.shim.fixture_calls(), 0)
        self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)

        root = case.env.images[0].parent.parent
        sysroot = (root / 'sysroot').resolve()
        (sysroot / 'ostree').mkdir(parents=True, exist_ok=True)
        (sysroot / 'boot').mkdir(parents=True, exist_ok=True)
        lock_path = root / 'update.lock'
        work_dir = root / 'tx_work'

        cap_dir = root / 'captured'
        source_files = {p: p.read_bytes() for p in cap_dir.rglob('*') if p.is_file()}
        base = act_test._baseline(case)

        stack.enter_context(mock.patch.object(trial_confirmation, 'UPDATE_LOCK_PATH', lock_path))
        snapshot = dict(armada_os.snapshot())
        snapshot.update({
            'repository': next(iter(armada_os.REPOSITORIES)),
            'staged': None,
            'rollback_queued': False,
        })
        stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snapshot))
        stack.enter_context(mock.patch.object(image_source, 'fetch_online_image', return_value={
            'package': copy.deepcopy(PACKAGE),
            'commit': commit,
            'originRef': 'armada/stable',
        }))
        stack.enter_context(mock.patch.object(
            boot_trust, 'capture_boot_trust', side_effect=lambda **kw: copy.deepcopy(case.trust)))
        stack.enter_context(mock.patch.object(
            deployment_store, 'append_verified_deployment',
            side_effect=lambda **kw: {'model': kw['model'], 'deployment': wf.TARGET_DEP}))
        stack.enter_context(mock.patch.object(
            boot_materials, 'prepare_boot_materials',
            side_effect=lambda **kw: {
                'model': kw['model'],
                'deployment': kw['target_binding'],
                'transactionUuid': kw['transaction_uuid'],
            }))

        def fake_candidate(**kw):
            cand = copy.deepcopy(captured['candidate'])
            cand['transactionUuid'] = kw['materials']['transactionUuid']
            cand['deployment'] = kw['materials']['deployment']
            return cand

        stack.enter_context(mock.patch.object(
            boot_candidate, 'prepare_boot_candidate', side_effect=fake_candidate))

        case.env.opened.clear()
        case.env.closed.clear()
        case.opens.clear()
        return act_test, case, base, source_files, sysroot, work_dir, captured['candidate']

    @staticmethod
    def _run(sysroot: Path, work_dir: Path, channel: str = 'stable'):
        return tx_mod.run_online_transaction(
            channel_url='https://example.invalid/channel.json',
            signature_url='https://example.invalid/channel.json.sig',
            expected_channel=channel,
            sysroot_path=sysroot,
            work_dir=work_dir,
        )

    def _assert_disks_and_journal_after_tx(
        self, case, base, source_files: dict[Path, bytes],
        cand_bytes: bytes, *, boot_written: bool, gpt_changed: bool, journal_changed: bool
    ) -> None:
        target = 'ab'[1 - case.current]
        part = case.trust['sources']['partitions'][f'boot_{target}']
        b_lun = part['lun']
        b_start_byte = part['firstLba'] * case.bs
        b_end_byte = b_start_byte + len(cand_bytes)

        for lun, (old, path) in enumerate(zip(base.disks, case.env.images)):
            now = path.read_bytes()
            expected = bytearray(old)
            if boot_written and lun == b_lun:
                expected[b_start_byte:b_end_byte] = cand_bytes
            if not gpt_changed:
                self.assertEqual(now, bytes(expected))
                continue
            bs = case.bs
            ab = (2048 + bs - 1) // bs
            p_end = (2 + ab) * bs
            b_start = (pf.BLOCKS - 1 - ab) * bs
            self.assertEqual(now[:bs], bytes(expected[:bs]))
            self.assertEqual(now[p_end:b_start], bytes(expected[p_end:b_start]))

        self.assertEqual(os.pread(case.journal.fd, jf.HEADER_BYTES, 0), base.header)
        self.assertEqual(os.pread(case.journal.fd, 64, jf.HEADER_BYTES), base.archive)
        region = os.pread(case.journal.fd, jf.REGION_BYTES, jf.REGION_OFFSET)
        if not journal_changed:
            self.assertEqual(region, base.region)
        else:
            self.assertEqual(region[:rec.TAIL_OFFSET_A], base.region[:rec.TAIL_OFFSET_A])
            end_b = rec.TAIL_OFFSET_B + rec.RECORD_BYTES
            self.assertEqual(region[end_b:], base.region[end_b:])

        for p, orig in source_files.items():
            self.assertEqual(p.read_bytes(), orig)

    def test_online_transaction_rejects_channel_mismatch(self) -> None:
        with ExitStack() as stack:
            act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
            fetch = stack.enter_context(mock.patch.object(image_source, 'fetch_online_image'))
            snapshot = dict(armada_os.snapshot())
            snapshot.update({
                'repository': next(iter(armada_os.REPOSITORIES)),
                'channel': 'beta',
                'staged': None,
                'rollback_queued': False,
            })
            stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snapshot))
            with self.assertRaisesRegex(
                    tx_mod.SystemTransactionError,
                    'requested channel differs from configured channel') as ctx:
                self._run(sysroot, work)
            fetch.assert_not_called()
            self.assertFalse(work.exists())
            result = ctx.exception.result
            self.assertEqual(result['bootWriteState'], 'not_started')
            self.assertEqual(result['activationOutcome'], 'not_attempted')
            self._assert_disks_and_journal_after_tx(
                case, base, sources, Path(cand['candidate']['path']).read_bytes(),
                boot_written=False, gpt_changed=False, journal_changed=False)
            act_test._assert_all_closed(case)

    def test_online_transaction_success_matrix(self) -> None:
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), ExitStack() as stack:
                        act_test, case, base, sources, sysroot, work, cand = self._prepare(
                            stack, bs=bs, current=current, board=board)
                        status = self._run(sysroot, work)
                        persisted = json.loads((work / 'status.json').read_bytes())
                        self.assertEqual(status, persisted)
                        self.assertEqual(status['state'], 'awaiting_reboot')
                        self.assertEqual(json.loads((work.parent / 'current.json').read_bytes()), {'schemaVersion': 1, 'jobId': work.name})
                        self.assertEqual(status['phase'], 'awaiting_reboot')
                        self.assertTrue(status['requiresReboot'])
                        self.assertEqual(status['bootWriteState'], 'written')
                        self.assertEqual(status['activationOutcome'], 'activated')

                        plan_bytes = (work / 'plan.json').read_bytes()
                        plan = json.loads(plan_bytes)
                        self.assertEqual(plan['channel'], 'stable')
                        plan_sha = hashlib.sha256(plan_bytes).hexdigest()
                        self.assertEqual(status['planSha256'], plan_sha)

                        snap = store.load(case.journal.fd, case.journal.part, board)
                        self.assertIsNotNone(snap.record)
                        r = snap.record
                        self.assertEqual(r.state, rec.PREPARED)
                        self.assertEqual(r.from_slot, current)
                        self.assertEqual(r.target_slot, 1 - current)
                        self.assertEqual(r.os_name, 'armada')
                        self.assertEqual(r.old_commit, bytes.fromhex('a' * 64))
                        self.assertEqual(r.new_commit, bytes.fromhex('b' * 64))
                        self.assertEqual(r.old_boot_sha256, bytes.fromhex(plan['oldBootSha256']))
                        self.assertEqual(r.new_boot_sha256, bytes.fromhex(plan['newBootSha256']))
                        self.assertEqual(r.plan_sha256, bytes.fromhex(plan_sha))
                        self.assertEqual(r.transaction_uuid, uuid.UUID(status['transactionId']).bytes)

                        self.assertGreater(af.SlotActivationTests.shim.fixture_calls(), 0)
                        self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 1)
                        self.assertEqual(act_test._inspect_selected_slot(case), 1 - current)

                        cand_bytes = Path(cand['candidate']['path']).read_bytes()
                        self._assert_disks_and_journal_after_tx(
                            case, base, sources, cand_bytes,
                            boot_written=True, gpt_changed=True, journal_changed=True)
                        act_test._assert_all_closed(case)

    def test_online_transaction_failure_modes(self) -> None:
        with self.subTest('up_to_date'), ExitStack() as stack:
            act_test, case, base, sources, sysroot, work, cand = self._prepare(
                stack, commit='a' * 64)
            with mock.patch.object(boot_trust, 'capture_boot_trust') as cap_mock:
                status = self._run(sysroot, work)
                cap_mock.assert_not_called()
            self.assertEqual(status['state'], 'up_to_date')
            self.assertEqual(status['phase'], 'complete')
            self.assertFalse(status['requiresReboot'])
            self.assertEqual(status['bootWriteState'], 'not_started')
            self.assertEqual(status['activationOutcome'], 'not_attempted')
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            self._assert_disks_and_journal_after_tx(
                case, base, sources, b'', boot_written=False, gpt_changed=False, journal_changed=False)
            act_test._assert_all_closed(case)

        with self.subTest('downloader_error'), ExitStack() as stack:
            act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
            stack.enter_context(mock.patch.object(
                image_source, 'fetch_online_image', side_effect=RuntimeError('net down')))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'not_started')
            self.assertEqual(res['activationOutcome'], 'not_attempted')
            self.assertEqual(json.loads((work / 'status.json').read_bytes()), res)

        for r_status, exp_state, exp_write in (
            ('not_needed', 'failed', 'not_started'),
            ('restored', 'failed', 'restored'),
            ('failed', 'uncertain', 'uncertain'),
        ):
            with self.subTest(boot_write_error=r_status), ExitStack() as stack:
                act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
                cause = writer.BootWriteError('write boom', restore_status=r_status)
                stack.enter_context(mock.patch.object(
                    writer, 'write_inactive_boot', side_effect=cause))
                with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                    self._run(sysroot, work)
                self.assertIs(ctx.exception.__cause__, cause)
                res = ctx.exception.result
                self.assertEqual(res['state'], exp_state)
                self.assertFalse(res['requiresReboot'])
                self.assertEqual(res['bootWriteState'], exp_write)
                self.assertEqual(res['bootRestoreState'], r_status)
                self.assertEqual(res['activationOutcome'], 'not_attempted')
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)

        with self.subTest('journal_save_failure'), ExitStack() as stack:
            act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
            stack.enter_context(mock.patch.object(
                store, 'save', side_effect=OSError('journal io')))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'failed')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'written')
            self.assertEqual(res['activationOutcome'], 'not_attempted')
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 0)
            self.assertEqual(act_test._inspect_selected_slot(case), case.current)
            cand_bytes = Path(cand['candidate']['path']).read_bytes()
            self._assert_disks_and_journal_after_tx(
                case, base, sources, cand_bytes,
                boot_written=True, gpt_changed=False, journal_changed=False)

        for outcome, native_rc, exp_state in (
            ('not_attempted', None, 'failed'),
            ('unchanged', -5, 'failed'),
            ('uncertain', -5, 'uncertain'),
        ):
            with self.subTest(activation_error=outcome), ExitStack() as stack:
                act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
                err = activation.ActivationError('act err', outcome, native_rc)
                stack.enter_context(mock.patch.object(
                    activation, 'activate_prepared_slot', side_effect=err))
                with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                    self._run(sysroot, work)
                res = ctx.exception.result
                self.assertEqual(res['state'], exp_state)
                self.assertFalse(res['requiresReboot'])
                self.assertEqual(res['bootWriteState'], 'written')
                self.assertEqual(res['activationOutcome'], outcome)
                if native_rc is not None:
                    self.assertEqual(res['nativeResult'], native_rc)
                else:
                    self.assertNotIn('nativeResult', res)

        with self.subTest('final_status_save_failure'), ExitStack() as stack:
            act_test, case, base, sources, sysroot, work, cand = self._prepare(stack)
            real_save = tx_mod._save_json
            awaiting_saves = 0

            def flaky_save(directory, name, value):
                nonlocal awaiting_saves
                if name == 'status.json' and value.get('state') == 'awaiting_reboot':
                    awaiting_saves += 1
                    raise OSError('disk full')
                return real_save(directory, name, value)

            stack.enter_context(mock.patch.object(tx_mod, '_save_json', side_effect=flaky_save))
            with self.assertRaises(tx_mod.SystemTransactionError) as ctx:
                self._run(sysroot, work)
            self.assertEqual(awaiting_saves, 1)
            self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 1)
            res = ctx.exception.result
            self.assertEqual(res['state'], 'uncertain')
            self.assertFalse(res['requiresReboot'])
            self.assertEqual(res['bootWriteState'], 'written')
            self.assertEqual(res['activationOutcome'], 'uncertain')
            self.assertNotIn('persistenceError', res)
            self.assertEqual(json.loads((work / 'status.json').read_bytes()), res)

    @unittest.skipUnless(os.geteuid() == 0, "Finalization receipt requires root ownership")
    def test_consecutive_updates_across_confirmation_and_slot_return(self) -> None:
        cases = (
            (rollback, board, current, bs)
            for rollback in (False, True)
            for board, current, bs in ((1, 0, 512), (1, 1, 4096), (2, 0, 4096), (2, 1, 512))
        )
        for rollback, board, current, bs in cases:
            with self.subTest(rollback=rollback, board=board, current=current, bs=bs), ExitStack() as stack:
                act_test, case, base, sources, sysroot, work, cand = self._prepare(
                    stack, bs=bs, current=current, board=board)
                root = work.parent

                status1 = self._run(sysroot, work)
                self.assertEqual(status1['state'], 'awaiting_reboot')
                snap1 = store.load(case.journal.fd, case.journal.part, board)
                self.assertIsNotNone(snap1.record)
                r1 = snap1.record
                self.assertEqual(r1.sequence, 1)
                self.assertEqual(r1.state, rec.PREPARED)
                self.assertEqual(r1.from_slot, current)
                self.assertEqual(r1.target_slot, 1 - current)
                self.assertEqual(r1.new_commit, bytes.fromhex('b' * 64))
                self.assertEqual(r1.transaction_uuid, uuid.UUID(status1['transactionId']).bytes)
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 1)
                self.assertEqual(act_test._inspect_selected_slot(case), 1 - current)
                cand1_bytes = Path(cand['candidate']['path']).read_bytes()

                first_target = 'ab'[1 - current]
                second_target = 'ab'[current]
                case.env.context.update(
                    actualSlot=first_target,
                    inactiveSlot=second_target,
                    deployment=wf.TARGET_DEP,
                )
                disks_before_block = [p.read_bytes() for p in case.env.images]
                region_before_block = os.pread(case.journal.fd, jf.REGION_BYTES, jf.REGION_OFFSET)
                with self.assertRaises(tx_mod.SystemTransactionError) as blocked:
                    self._run(sysroot, root / 'blocked-work')
                self.assertEqual(blocked.exception.result['state'], 'failed')
                self.assertIn('pending trial journal refuses overwrite', str(blocked.exception))
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 1)
                self.assertEqual([p.read_bytes() for p in case.env.images], disks_before_block)
                self.assertEqual(
                    os.pread(case.journal.fd, jf.REGION_BYTES, jf.REGION_OFFSET),
                    region_before_block,
                )

                store.save(
                    case.journal.fd,
                    snap1,
                    dataclasses.replace(
                        r1,
                        sequence=2,
                        state=rec.TRIAL,
                        attempt_limit=3,
                        remaining=2,
                    ),
                )
                trial_snap = store.load(case.journal.fd, case.journal.part, board)
                self.assertEqual(trial_snap.record.sequence, 2)
                self.assertEqual(trial_snap.record.state, rec.TRIAL)

                boot_ev = boot_health.BootEvidence(
                    transaction_uuid=bytes(r1.transaction_uuid),
                    actual_slot=1 - current,
                    deployment=wf.TARGET_DEP,
                    kernel_release='6.12.0-armada',
                    boot_sha256=bytes(r1.new_boot_sha256),
                )
                rt_ev = runtime_health.RuntimeEvidence(
                    boot_id='boot-1',
                    session_id='session-1',
                    uid=1000,
                    display=':0',
                    services=runtime_health.REQUIRED_UNITS,
                )
                stack.enter_context(mock.patch.object(
                    trial_confirmation, 'collect_boot_evidence', return_value=boot_ev))
                stack.enter_context(mock.patch.object(
                    activation.boot_health, 'collect_boot_evidence', return_value=boot_ev))
                stack.enter_context(mock.patch.object(
                    trial_confirmation, 'collect_runtime_evidence', return_value=rt_ev))
                stack.enter_context(mock.patch.object(
                    trial_confirmation.time, 'sleep', return_value=None))
                stack.enter_context(mock.patch.object(trial_confirmation, 'SYSROOT_PATH', sysroot))
                stack.enter_context(mock.patch.object(trial_confirmation.finalization_receipt, 'STATE_PATH', sysroot.parent / 'finalized.json'))
                stack.enter_context(mock.patch.object(
                    deployment_store,
                    'promote_confirmed_deployment',
                    side_effect=lambda **kw: {
                        'deployment': kw['current_binding'],
                        'previousDeployment': kw['previous_binding'],
                        'changed': False,
                    },
                ))
                conf = trial_confirmation.confirm_trial()
                self.assertEqual(conf['state'], 'confirmed')
                self.assertEqual(conf['firmwareSlotSuccess'], 'confirmed')
                snap_conf = store.load(case.journal.fd, case.journal.part, board)
                self.assertIsNotNone(snap_conf.record)
                self.assertEqual(snap_conf.record.state, rec.CONFIRMED)
                self.assertEqual(snap_conf.record.sequence, 3)
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 1)

                first_part = case.trust['sources']['partitions'][f'boot_{first_target}']
                lun4_bytes = case.env.images[4].read_bytes()
                first_entry_off = 2 * bs + (1 - current) * 128
                first_attrs = int.from_bytes(lun4_bytes[first_entry_off + 48:first_entry_off + 56], 'little')
                self.assertNotEqual(first_attrs & (1 << 54), 0)
                self.assertEqual((first_attrs >> 51) & 0x7, 0)

                self._assert_disks_and_journal_after_tx(
                    case, base, sources, cand1_bytes,
                    boot_written=True, gpt_changed=True, journal_changed=True)

                fresh_phys = physical_slots.inspect_physical_slots()
                cap2_dir = root / 'captured-second'
                extra_labels = ['recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b']
                if board == 2:
                    extra_labels.append('efisp')
                fresh_sources = boot_sources.capture_boot_sources(
                    fresh_phys, cap2_dir, extra_labels=extra_labels)
                sources2 = dict(sources)
                sources2.update({p: p.read_bytes() for p in cap2_dir.rglob('*') if p.is_file()})

                trust2 = copy.deepcopy(case.trust)
                trust2['physical'] = fresh_phys
                trust2['sources'] = fresh_sources
                trust2['trust'] = {second_target: copy.deepcopy(case.trust['trust'][first_target])}
                case.trust = trust2

                second_src_boot = Path(fresh_sources['slots'][second_target]['boot']['path']).read_bytes()
                second_src_sha = fresh_sources['slots'][second_target]['boot']['sha256']
                cand2_buf = bytearray(second_src_boot)
                cand2_buf[128] ^= 0x33
                cand2_buf[-1] ^= 0xCC
                cand2_bytes = bytes(cand2_buf)
                cand2_path = root / 'second-candidate.img'
                cand2_path.write_bytes(cand2_bytes)
                cand2_sha = hashlib.sha256(cand2_bytes).hexdigest()

                cand2_template = copy.deepcopy(cand)
                cand2_template['slot'] = second_target
                cand2_template['sourceBootSha256'] = second_src_sha
                cand2_template['candidate'].update({
                    'path': str(cand2_path),
                    'bytes': len(cand2_bytes),
                    'sha256': cand2_sha,
                })

                def fake_candidate2(**kw):
                    out = copy.deepcopy(cand2_template)
                    out['transactionUuid'] = kw['materials']['transactionUuid']
                    out['deployment'] = kw['materials']['deployment']
                    return out

                expected_commit = r1.old_commit.hex() if rollback else ('c' * 64)
                expected_serial = r1.old_serial if rollback else 0
                target_binding = f'{r1.os_name}/{expected_commit}.{expected_serial}'
                if rollback:
                    snap2_os = dict(armada_os.snapshot())
                    snap2_os['rollback'] = {
                        'incompatible': False,
                        'checksum': expected_commit,
                        'serial': expected_serial,
                        'stateroot': r1.os_name,
                        'version': '1.2.2',
                    }
                    stack.enter_context(mock.patch.object(armada_os, 'snapshot', return_value=snap2_os))
                pkg2 = copy.deepcopy(PACKAGE)
                pkg2.update({
                    'version': '1.2.4',
                    'releaseId': '2026.05',
                    'ociDigest': 'sha256:' + 'e' * 64,
                })
                fetch2 = stack.enter_context(mock.patch.object(
                    image_source, 'fetch_online_image', return_value={
                        'package': pkg2,
                        'commit': 'c' * 64,
                        'originRef': 'armada/stable',
                    }))
                append2 = stack.enter_context(mock.patch.object(
                    deployment_store, 'append_verified_deployment',
                    side_effect=lambda **kw: {'model': kw['model'], 'deployment': target_binding}))
                mat2 = stack.enter_context(mock.patch.object(
                    boot_materials, 'prepare_boot_materials',
                    side_effect=lambda **kw: {
                        'model': kw['model'],
                        'deployment': kw['target_binding'],
                        'transactionUuid': kw['transaction_uuid'],
                    }))
                stack.enter_context(mock.patch.object(
                    boot_candidate, 'prepare_boot_candidate', side_effect=fake_candidate2))

                case.current = 1 - current
                base2 = act_test._baseline(case)
                act_test._assert_all_closed(case)
                case.env.opened.clear()
                case.env.closed.clear()
                case.opens.clear()

                work2 = root / 'tx_work_second'
                if rollback:
                    status2 = tx_mod.run_rollback_transaction(
                        expected_checksum=expected_commit,
                        sysroot_path=sysroot,
                        work_dir=work2,
                    )
                    fetch2.assert_not_called()
                    append2.assert_not_called()
                else:
                    status2 = self._run(sysroot, work2)
                    fetch2.assert_called_once()
                    append2.assert_called_once()
                mat2.assert_called_once()
                self.assertEqual(mat2.call_args.kwargs['target_binding'], target_binding)
                self.assertEqual(mat2.call_args.kwargs['transaction_uuid'], status2['transactionId'])
                self.assertEqual(status2['state'], 'awaiting_reboot')
                self.assertEqual(status2['fromSlot'], first_target)
                self.assertEqual(status2['targetSlot'], second_target)
                self.assertNotEqual(status2['transactionId'], status1['transactionId'])
                self.assertEqual(af.SlotActivationTests.shim.fixture_set_calls(), 2)
                self.assertEqual(act_test._inspect_selected_slot(case), current)

                plan2_bytes = (work2 / 'plan.json').read_bytes()
                plan2 = json.loads(plan2_bytes)
                if rollback:
                    self.assertEqual(plan2.get('operation'), 'rollback')
                plan2_sha = hashlib.sha256(plan2_bytes).hexdigest()
                snap2 = store.load(case.journal.fd, case.journal.part, board)
                self.assertIsNotNone(snap2.record)
                r2 = snap2.record
                self.assertEqual(r2.sequence, 4)
                self.assertEqual(r2.state, rec.PREPARED)
                self.assertEqual(r2.from_slot, 1 - current)
                self.assertEqual(r2.target_slot, current)
                self.assertEqual(r2.old_commit, bytes.fromhex('b' * 64))
                self.assertEqual(r2.new_commit, bytes.fromhex(expected_commit))
                self.assertEqual(r2.new_serial, expected_serial)
                self.assertEqual(r2.old_boot_sha256, bytes(r1.new_boot_sha256))
                self.assertEqual(r2.new_boot_sha256, bytes.fromhex(cand2_sha))
                self.assertEqual(r2.plan_sha256, bytes.fromhex(plan2_sha))
                self.assertEqual(r2.transaction_uuid, uuid.UUID(status2['transactionId']).bytes)

                self._assert_disks_and_journal_after_tx(
                    case, base2, sources2, cand2_bytes,
                    boot_written=True, gpt_changed=True, journal_changed=True)

                first_boot_disk = case.env.images[first_part['lun']].read_bytes()
                first_start = first_part['firstLba'] * bs
                self.assertEqual(
                    first_boot_disk[first_start:first_start + len(cand1_bytes)],
                    cand1_bytes,
                )
                lun4_after = case.env.images[4].read_bytes()
                first_attrs_after = int.from_bytes(
                    lun4_after[first_entry_off + 48:first_entry_off + 56], 'little')
                self.assertNotEqual(first_attrs_after & (1 << 54), 0)
                self.assertEqual((first_attrs_after >> 51) & 0x7, 0)
                act_test._assert_all_closed(case)


if __name__ == '__main__':
    unittest.main()
