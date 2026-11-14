# SPDX-License-Identifier: GPL-3.0-only
"""Root-only behavioral tests for detached offline update jobs.

Private session storage is created with 0700 root-owned directories and is
opened through ``fcntl``/``O_NOFOLLOW``, so the whole suite is meaningful only
when it runs as root.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))
sys.path.insert(0, str(ROOT / 'tests'))
from updates import offline_session, system_transaction, trial_confirmation
# Session builders and the execute-side recovery cases live in sibling mixin
# modules so that each file stays reviewable.  Neither module is a test module
# for discovery purposes: see the class defined below for the only concrete
# TestCase.
from offline_job_fixtures import (
    NEW_DEPLOYMENT, OTHER_TX, PHYSICAL, ROOT_REQUIRED, TX,
    OfflineJobFixtureMixin,
)
from offline_job_recovery_cases import OfflineJobRecoveryCases


@ROOT_REQUIRED
class OfflineJobTests(OfflineJobRecoveryCases, OfflineJobFixtureMixin,
                      unittest.TestCase):
    def interrupted_prepare(self, receipt_state='preparing', documents=None,
                            tamper=None):
        """Return a session whose prepare worker died before recording its result."""
        sid = self.session()
        self.manager().submit_prepare(sid)
        changes = documents or {}

        def prepare(work_dir, **_kwargs):
            return self.write_prepare_documents(
                work_dir.parent, work_dir, **changes)

        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=prepare):
            self.manager().run_job('prepare', sid)
        with self.store.session_lock(sid) as session:
            if tamper is not None:
                tamper(session / 'transaction')
            receipt = self.store.read_receipt(session)
            receipt['state'] = receipt_state
            receipt.pop('result', None)
            self.store.update_receipt(session, receipt)
        self.runner.reset_mock()
        self.runner.return_value.returncode = 3
        return sid

    def test_interrupted_prepare_adopts_terminal_transaction_once(self):
        sid = self.interrupted_prepare()
        mgr = self.manager()

        first = mgr.status(sid)
        self.assertEqual(first['state'], 'prepared')
        self.assertEqual(first['result']['state'], 'awaiting_recovery_export')
        self.assertEqual(first['result']['transactionId'], TX)
        self.assertEqual(first['transaction']['state'], 'awaiting_recovery_export')
        self.assertNotIn('jobInterrupted', first)
        self.assertNotIn('attentionRequired', first)
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.runner.call_args.args[0][0], 'systemctl')

        recorded = self.receipt_bytes(sid)
        second = mgr.status(sid)
        self.assertEqual(second['state'], 'prepared')
        self.assertEqual(self.receipt_bytes(sid), recorded)
        self.assertEqual(self.runner.call_count, 1)
        self.reboot.assert_not_called()

        # The client continues through the existing explicit export/ack gate.
        ack = self.ack(sid)
        with self.assertRaises(ValueError):
            mgr.submit_execute(sid, dict(ack, transactionId=OTHER_TX))
        queued = mgr.submit_execute(sid, ack)
        self.assertEqual(queued['state'], 'execute_queued')
        self.assertEqual(queued['jobOperation'], 'execute')

    def test_interrupted_prepare_dispatch_uncertain_adopts_terminal_documents(self):
        sid = self.interrupted_prepare(receipt_state='dispatch_uncertain')
        receipt = self.manager().status(sid)
        self.assertEqual(receipt['state'], 'prepared')
        self.assertEqual(receipt['result']['state'], 'awaiting_recovery_export')
        self.reboot.assert_not_called()

    def test_interrupted_prepare_refuses_incomplete_foreign_or_executed_documents(self):
        def rewrite(name, extra):
            def tamper(transaction):
                path = transaction / name
                path.write_bytes(path.read_bytes() + extra)
            return tamper

        cases = (
            ('still preparing',
             {'status': {'state': 'preparing', 'phase': 'preparing'}}, None),
            ('later execute phase',
             {'status': {'state': 'executing', 'phase': 'writing_boot',
                         'bootWriteState': 'writing'}}, None),
            ('completed execute output',
             {'status': {'state': 'awaiting_reboot',
                         'phase': 'awaiting_reboot',
                         'bootWriteState': 'written',
                         'requiresReboot': True}}, None),
            ('slot control issued',
             {'status': {'slotControlState': 'issued'}}, None),
            ('foreign transaction',
             {'status': {'transactionId': OTHER_TX},
              'plan': {'transactionId': OTHER_TX},
              'manifest': {'transactionId': OTHER_TX}}, None),
            ('foreign model', {'plan': {'model': 'TB323FU'}}, None),
            ('other channel', {'plan': {'channel': 'beta'}}, None),
            ('other old deployment',
             {'plan': {'oldDeployment': 'armada/' + ('c' * 64) + '.0'}}, None),
            ('other slots', {'plan': {'fromSlot': 'b'}}, None),
            ('foreign recovery identity',
             {'manifest': {'recoveryPartUuid':
                           '87654321-4321-4321-8321-123456789abc'}}, None),
            ('manifest byte count',
             {'status': {'recoveryManifestBytes': 1}}, None),
            ('package digest',
             {'prepared': {'packageDescriptorSha256': 'd' * 64}}, None),
            ('tampered plan digest', {}, rewrite('plan.json', b' ')),
            ('tampered manifest digest',
             {}, rewrite('recovery-manifest.json', b' ')),
            ('missing status', {},
             lambda transaction: (transaction / 'status.json').unlink()),
            ('missing manifest', {},
             lambda transaction: (transaction / 'recovery-manifest.json').unlink()),
            ('execute artifacts present', {},
             lambda transaction: (transaction / 'write-report.json').write_bytes(b'{}')),
        )
        for name, documents, tamper in cases:
            with self.subTest(case=name):
                sid = self.interrupted_prepare(documents=documents, tamper=tamper)
                before = self.receipt_bytes(sid)
                receipt = self.manager().status(sid)
                self.assertEqual(receipt['state'], 'preparing')
                self.assertTrue(receipt['jobInterrupted'])
                self.assertTrue(receipt['attentionRequired'])
                self.assertEqual(self.receipt_bytes(sid), before)
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.manager().submit_execute(sid, {})
                self.reboot.assert_not_called()

    def test_interrupted_prepare_never_adopts_while_worker_is_active(self):
        sid = self.interrupted_prepare()
        self.runner.return_value.returncode = 0
        receipt = self.manager().status(sid)
        self.assertEqual(receipt['state'], 'preparing')
        self.assertTrue(receipt['jobActive'])
        self.assertNotIn('jobInterrupted', receipt)
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().submit_execute(sid, {})
        self.reboot.assert_not_called()

    def test_status_binds_completion_without_writing_or_rebooting(self):
        sid = self.executed_session()
        self.trial.return_value = self.evidence(sid)
        before = self.receipt_bytes(sid)

        first = self.manager().status(sid)
        second = self.manager().status(sid)

        for result in (first, second):
            self.assertEqual(result['state'], 'awaiting_reboot')
            completion = result['completion']
            self.assertEqual(completion['state'], 'awaiting_reboot')
            self.assertEqual(completion['transactionId'], TX)
            self.assertEqual(completion['deployment'], NEW_DEPLOYMENT)
            self.assertTrue(completion['restartEligible'])
            self.assertIsNone(completion['restartState'])
        self.assertEqual(self.receipt_bytes(sid), before)
        self.reboot.assert_not_called()

    def test_status_mismatch_is_never_reported_as_success(self):
        sid = self.executed_session()
        # The live trial describes a different (e.g. newer) transaction.
        self.trial.return_value = self.evidence(
            sid, transactionId=OTHER_TX, recordState='confirmed',
            runningDeployment='new', bootConfirmed=True,
            finalization={'state': 'complete'})
        completion = self.manager().status(sid)['completion']
        self.assertEqual(completion['state'], 'mismatch')
        self.assertEqual(completion['reason'], 'transaction_mismatch')
        self.assertFalse(completion['restartEligible'])
        self.assertEqual(completion['transactionId'], TX)
        self.reboot.assert_not_called()

    def test_status_confirmed_requires_complete_finalization_and_boot(self):
        sid = self.executed_session()
        before = self.receipt_bytes(sid)
        self.trial.return_value = self.evidence(
            sid, recordState='confirmed', runningDeployment='new',
            bootConfirmed=True, finalization={'state': 'pending',
                                              'error': 'journal busy'})
        pending = self.manager().status(sid)
        self.assertEqual(pending['completion']['state'], 'finalizing')
        self.assertFalse(pending['completion']['restartEligible'])

        self.trial.return_value = self.evidence(
            sid, recordState='confirmed', runningDeployment='new',
            bootConfirmed=True, finalization={'state': 'complete'})
        confirmed = self.manager().status(sid)
        self.assertEqual(confirmed['completion']['state'], 'confirmed')
        self.assertFalse(confirmed['completion']['restartEligible'])
        self.assertEqual(confirmed['state'], 'awaiting_reboot')
        self.assertEqual(self.receipt_bytes(sid), before)
        self.reboot.assert_not_called()

    def test_request_restart_persists_before_dispatch_and_reboots_once(self):
        sid = self.executed_session()
        self.trial.return_value = self.evidence(sid)
        seen = {}

        def reboot(argv, **kwargs):
            with self.store.session_lock(sid) as session:
                seen['restart'] = json.loads(
                    (session / 'receipt.json').read_text()).get('restart')
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=b'', stderr=b'')

        self.reboot.side_effect = reboot
        result = self.manager().request_restart(sid)
        self.assertEqual(result['state'], 'awaiting_reboot')
        self.assertEqual(result['sessionId'], sid)
        self.assertEqual(result['transactionId'], TX)
        self.assertEqual(result['deployment'], NEW_DEPLOYMENT)
        self.assertEqual(result['restart']['state'], 'dispatched')
        self.assertEqual(result['restart']['transactionId'], TX)
        self.assertEqual(seen['restart'],
                         {'schemaVersion': 1, 'state': 'dispatching',
                          'transactionId': TX})
        self.assertEqual(self.reboot.call_args.args[0],
                         ['/usr/bin/systemctl', 'reboot'])

        again = self.manager().request_restart(sid)
        self.assertEqual(again['restart']['state'], 'dispatched')
        self.assertEqual(self.reboot.call_count, 1)

        status = self.manager().status(sid)
        self.assertEqual(status['completion']['restartState'], 'dispatched')
        # Eligibility still describes the transaction (it is at
        # awaiting_reboot); the persisted dispatch state is what stops a second
        # reboot, both here and in the installer's action gate.
        self.assertTrue(status['completion']['restartEligible'])

    def test_request_restart_during_dispatch_returns_pending_without_rebooting(self):
        sid = self.executed_session()
        self.trial.return_value = self.evidence(sid)
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            receipt['restart'] = {'schemaVersion': 1, 'state': 'dispatching',
                                  'transactionId': TX}
            self.store.update_receipt(session, receipt)

        # Another client's request is inside the reboot window: answer with the
        # persisted pending state instead of raising or rebooting again.
        result = self.manager().request_restart(sid)
        self.assertEqual(result['state'], 'awaiting_reboot')
        self.assertEqual(result['restart']['state'], 'dispatching')
        self.assertEqual(result['restart']['transactionId'], TX)
        self.reboot.assert_not_called()

    def test_request_restart_uncertain_is_not_reissued(self):
        sid = self.executed_session()
        self.trial.return_value = self.evidence(sid)
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            receipt['restart'] = {'schemaVersion': 1, 'state': 'uncertain',
                                  'transactionId': TX}
            self.store.update_receipt(session, receipt)

        # The request may already have reached the firmware; it is pending, not
        # a failure and not an invitation to send a second reboot.
        for _ in range(2):
            result = self.manager().request_restart(sid)
            self.assertEqual(result['restart']['state'], 'uncertain')
        self.reboot.assert_not_called()
        self.assertEqual(
            self.manager().status(sid)['completion']['restartState'], 'uncertain')

    def test_request_restart_after_failed_rechecks_eligibility(self):
        sid = self.executed_session()
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            receipt['restart'] = {'schemaVersion': 1, 'state': 'failed',
                                  'transactionId': TX}
            self.store.update_receipt(session, receipt)

        # An explicitly rejected request may be retried by the user, but only
        # while the transaction is still eligible.
        self.trial.return_value = self.evidence(
            sid, recordState='prepared', runningDeployment='new')
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().request_restart(sid)
        self.reboot.assert_not_called()
        with self.store.session_lock(sid) as session:
            self.assertEqual(
                json.loads((session / 'receipt.json').read_text())
                ['restart']['state'], 'failed')

        self.trial.return_value = self.evidence(sid)
        result = self.manager().request_restart(sid)
        self.assertEqual(result['restart']['state'], 'dispatched')
        self.assertEqual(self.reboot.call_count, 1)

    def test_request_restart_refused_when_transaction_is_not_eligible(self):
        sid = self.executed_session()
        # Already running the new deployment: waiting for confirmation, not a
        # reboot.
        self.trial.return_value = self.evidence(
            sid, recordState='prepared', runningDeployment='new')
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().request_restart(sid)
        self.reboot.assert_not_called()
        with self.store.session_lock(sid) as session:
            self.assertNotIn('restart',
                             json.loads((session / 'receipt.json').read_text()))

        # Unfinished sessions have no executed transaction at all.
        fresh = self.session()
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().request_restart(fresh)
        self.reboot.assert_not_called()

    def test_request_restart_failure_is_persisted_not_success(self):
        sid = self.executed_session()
        self.trial.return_value = self.evidence(sid)
        self.reboot.return_value.returncode = 1
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().request_restart(sid)
        self.assertEqual(self.reboot.call_count, 1)
        self.assertEqual(
            self.manager().status(sid)['completion']['restartState'], 'failed')

    def test_prepare_once_reconnect_and_bindings(self):
        sid = self.session()
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared) as prepare:
            first = self.manager().submit_prepare(sid)
            self.assertEqual(first['state'], 'prepare_queued')
            self.assertEqual(self.manager().submit_prepare(sid)['state'], 'prepare_queued')
            launches = [call for call in self.runner.call_args_list
                        if call.args[0][0] == 'systemd-run']
            self.assertEqual(len(launches), 1)
            argv = launches[0].args[0]
            self.assertEqual(argv[:3], ['systemd-run', '--quiet', '--collect'])
            self.assertIn('--property=Type=exec', argv)
            self.assertIn('/usr/libexec/armada/armada-offline-update', argv)
            self.assertEqual(argv[-3:], ['--worker', 'prepare', sid])
            self.assertEqual(launches[0].kwargs,
                             {'capture_output': True, 'check': True, 'timeout': 30})
            result = self.manager().run_job('prepare', sid)
            self.assertEqual(result['state'], 'prepared')
            self.assertEqual(result['transaction']['transactionId'], TX)
            kwargs = prepare.call_args.kwargs
            self.assertEqual(kwargs['document'], self.document)
            self.assertEqual(kwargs['signature_b64'], self.signature)
            self.assertEqual(kwargs['payload_directory'],
                             self.store.base / sid / 'incoming')
            self.assertEqual(kwargs['sysroot_path'], '/sysroot')
            self.assertEqual(kwargs['trusted_key'], self.public)
            self.assertEqual(kwargs['updater_version'], '0.8.0')
            with self.assertRaises(offline_session.OfflineSessionError):
                self.manager().run_job('prepare', sid)
            prepare.assert_called_once()

    def test_ack_idempotence_execute_reconnect_and_receipts(self):
        sid = self.session()
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared):
            mgr = self.manager()
            mgr.submit_prepare(sid)
            mgr.run_job('prepare', sid)
        ack = self.ack(sid)
        with self.store.session_lock(sid) as session:
            (session / 'transaction/status.json').write_text('{"state":"stale"}')
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().submit_execute(sid, ack)
        self.assertFalse((self.store.base / sid / 'ack.json').exists())
        with self.store.session_lock(sid) as session:
            system_transaction._save_json(session / 'transaction', 'status.json',
                                          {'state': 'awaiting_recovery_export'})
        for bad in ({}, dict(ack, transactionId='wrong')):
            with self.assertRaises(ValueError):
                self.manager().submit_execute(sid, bad)
        queued = self.manager().submit_execute(sid, ack)
        self.assertEqual(queued['state'], 'execute_queued')
        self.assertEqual(self.manager().submit_execute(sid, ack)['state'], 'execute_queued')
        self.assertEqual(
            sum(1 for call in self.runner.call_args_list
                if call.args[0][0] == 'systemd-run'), 2)
        with self.assertRaises(ValueError):
            self.manager().submit_execute(sid, dict(ack, transactionId='changed'))
        expected = {'state': 'awaiting_reboot', 'bootWriteState': 'written',
                    'requiresReboot': True}
        with mock.patch('updates.offline_execute.execute_offline_update',
                        return_value=expected) as execute:
            result = self.manager().run_job('execute', sid)
            self.assertEqual(result['result'], expected)
            self.assertEqual(result['payload']['path'], 'original.img')
            self.assertEqual(execute.call_args.kwargs['recovery_ack'], ack)
            self.assertEqual(execute.call_args.kwargs['trusted_key'], self.public)
            with self.assertRaises(offline_session.OfflineSessionError):
                self.manager().run_job('execute', sid)
            execute.assert_called_once()

    def test_android_telemetry_drift_does_not_block_submission(self):
        sid = self.session()
        self.inspect.return_value = dict(
            PHYSICAL, selectedSlot='b',
            bootableSlots={'a': True, 'b': False})
        result = self.manager().submit_prepare(sid)
        self.assertEqual(result['state'], 'prepare_queued')
        self.runner.assert_called_once()

    def test_identity_drift_submission_and_worker(self):
        for kind in ('physical', 'channel'):
            with self.subTest(kind=kind):
                sid = self.session()
                if kind == 'physical':
                    self.inspect.return_value = dict(
                        PHYSICAL, actualSlot='b', inactiveSlot='a')
                else:
                    self.snapshot.return_value = {'channel': 'beta'}
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.manager().submit_prepare(sid)
                self.runner.assert_not_called()
                self.inspect.return_value = dict(PHYSICAL)
                self.snapshot.return_value = {'channel': 'stable'}
                self.runner.reset_mock()
        sid = self.session()
        self.manager().submit_prepare(sid)
        self.inspect.return_value = dict(PHYSICAL, actualSlot='b',
                                         inactiveSlot='a')
        with mock.patch('updates.offline_prepare.prepare_offline_update') as prepare:
            result = self.manager().run_job('prepare', sid)
            self.assertEqual(result['state'], 'failed')
            prepare.assert_not_called()

    def test_dispatch_uncertain_and_status_never_launches(self):
        sid = self.session()
        self.runner.side_effect = subprocess.TimeoutExpired('systemd-run', 30)
        self.assertEqual(self.manager().submit_prepare(sid)['state'], 'dispatch_uncertain')
        self.runner.side_effect = None
        self.manager().submit_prepare(sid)
        self.assertEqual(
            sum(1 for call in self.runner.call_args_list
                if call.args[0][0] == 'systemd-run'), 1)
        for code, interrupted in ((3, True), (4, True), (0, False)):
            self.runner.return_value.returncode = code
            status = self.manager().status(sid)
            self.assertEqual(status.get('jobInterrupted', False), interrupted)
            self.assertEqual(status.get('attentionRequired', False), interrupted)
            self.assertEqual(self.runner.call_args.args[0][0], 'systemctl')
        self.runner.return_value.returncode = 3
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared) as prepare:
            self.assertEqual(self.manager().run_job('prepare', sid)['state'], 'prepared')
            prepare.assert_called_once()

    def test_execute_errors_preserve_uncertainty(self):
        sid = self.session()
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared):
            self.manager().submit_prepare(sid)
            self.manager().run_job('prepare', sid)
        self.manager().submit_execute(sid, self.ack(sid))
        exact = {'state': 'recovery_required', 'bootWriteState': 'uncertain',
                 'requiresReboot': False}
        error = system_transaction.SystemTransactionError('uncertain', exact)
        with mock.patch('updates.offline_execute.execute_offline_update', side_effect=error):
            result = self.manager().run_job('execute', sid)
            self.assertEqual(result['state'], 'failed')
            self.assertEqual(result['result'], exact)
        sid2 = self.session()
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared):
            self.manager().submit_prepare(sid2)
            self.manager().run_job('prepare', sid2)
        self.manager().submit_execute(sid2, self.ack(sid2))
        with mock.patch('updates.offline_execute.execute_offline_update',
                        side_effect=RuntimeError('boom')):
            result = self.manager().run_job('execute', sid2)
            self.assertEqual(result['result']['state'], 'execution_uncertain')
            self.assertNotIn('bootWriteState', result['result'])

    def test_prepare_lock_contention_restores_queue_for_explicit_retry(self):
        sid = self.session()
        self.manager().submit_prepare(sid)
        launches = len(self.launches())
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=trial_confirmation.UpdateLockBusyError(
                            'update lock is busy')):
            result = self.manager().run_job('prepare', sid)
        self.assertEqual(result['state'], 'prepare_queued')
        self.assertEqual(result['jobOperation'], 'prepare')
        self.assertNotIn('error', result)
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            self.assertEqual(receipt['state'], 'prepare_queued')
            self.assertNotIn('error', receipt)
            self.assertNotIn('result', receipt)
        # The locked-out worker must not have created the transaction.
        self.assertFalse((self.store.base / sid / 'transaction').exists())

        # Polling status never dispatches the contended job by itself.
        self.runner.return_value.returncode = 3
        status = self.manager().status(sid)
        self.assertEqual(status['state'], 'prepare_queued')
        self.assertEqual(len(self.launches()), launches)

        # Only the explicit resubmission of the same session relaunches it.
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        retried = self.manager().submit_prepare(sid)
        self.assertEqual(retried['state'], 'prepare_queued')
        self.assertEqual(len(self.launches()), launches + 1)
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared) as prepare:
            self.assertEqual(self.manager().run_job('prepare', sid)['state'],
                             'prepared')
            prepare.assert_called_once()

    def test_execute_lock_contention_restores_queue_for_explicit_retry(self):
        sid = self.submitted_session()
        ack = self.ack(sid)
        pointer = self.store.base / 'pending' / (TX + '.json')
        pointer_before = pointer.read_bytes()
        launches = len(self.launches())
        # The prepared session's receipt already carries the preparation result.
        # Contention must preserve that evidence verbatim instead of replacing
        # it with an execution result of its own.
        with self.store.session_lock(sid) as session:
            result_before = self.store.read_receipt(session).get('result')
        with mock.patch('updates.offline_execute.execute_offline_update',
                        side_effect=trial_confirmation.UpdateLockBusyError(
                            'update lock is busy')):
            result = self.manager().run_job('execute', sid)
        self.assertEqual(result['state'], 'execute_queued')
        self.assertEqual(result['jobOperation'], 'execute')
        self.assertNotIn('error', result)
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            self.assertEqual(receipt['state'], 'execute_queued')
            self.assertNotIn('error', receipt)
            self.assertEqual(receipt.get('result'), result_before)
            self.assertFalse(
                (session / 'transaction/write-report.json').exists())
        self.assertEqual(pointer.read_bytes(), pointer_before)

        self.runner.return_value.returncode = 3
        status = self.manager().status(sid)
        self.assertEqual(status['state'], 'execute_queued')
        self.assertEqual(len(self.launches()), launches)

        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        retried = self.manager().submit_execute(sid, ack)
        self.assertEqual(retried['state'], 'execute_queued')
        self.assertEqual(len(self.launches()), launches + 1)
        expected = {'state': 'awaiting_reboot', 'bootWriteState': 'written',
                    'requiresReboot': True}
        with mock.patch('updates.offline_execute.execute_offline_update',
                        return_value=expected):
            result = self.manager().run_job('execute', sid)
        self.assertEqual(result['state'], 'awaiting_reboot')

    def test_only_update_lock_contention_restores_the_queue(self):
        # Any other confirmation failure may follow a mutation and must stay
        # terminal instead of being turned back into a queued submission.
        sid = self.session()
        self.manager().submit_prepare(sid)
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=trial_confirmation.ConfirmationError(
                            'update lock is busy')):
            result = self.manager().run_job('prepare', sid)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['state'], 'failed')

        sid = self.session()
        self.manager().submit_prepare(sid)
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=RuntimeError('prepare failed after mutation')):
            failed = self.manager().run_job('prepare', sid)
        self.assertEqual(failed['state'], 'failed')
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        launches = len(self.launches())
        self.assertEqual(self.manager().submit_prepare(sid)['state'], 'failed')
        self.assertEqual(len(self.launches()), launches)

    def test_terminal_prepare_and_invalid_invocations(self):
        for returned, expected in (({'state': 'up_to_date'}, 'up_to_date'),
                                   ({'state': 'nonsense'}, 'failed')):
            sid = self.session()
            self.manager().submit_prepare(sid)
            with mock.patch('updates.offline_prepare.prepare_offline_update',
                            return_value=returned):
                self.assertEqual(self.manager().run_job('prepare', sid)['state'], expected)
            if expected == 'up_to_date':
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.manager().submit_execute(sid, {})
        sid = self.session()
        mgr = self.manager()
        with self.assertRaises(ValueError):
            mgr.run_job('bad', sid)
        with self.assertRaises(ValueError):
            mgr.run_job('prepare', 'bad')
        with self.assertRaises(offline_session.OfflineSessionError):
            mgr.run_job('prepare', sid)
        with self.assertRaises(offline_session.OfflineSessionError):
            mgr.submit_execute(sid, {})


    def test_never_started_prepare_is_relaunched_and_parallel_repeat_is_idempotent(self):
        sid = self.queued_prepare_session()
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        first = self.manager().submit_prepare(sid)
        self.assertEqual(first['state'], 'prepare_queued')
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.launches()[0].args[0][-3:],
                         ['--worker', 'prepare', sid])

        # The transient unit now exists: a parallel repeat must leave it alone
        # and must never overwrite the queued receipt with a failure.
        self.runner.side_effect = self.dispatch_runner(load_state='loaded')
        second = self.manager().submit_prepare(sid)
        self.assertEqual(second['state'], 'prepare_queued')
        self.assertEqual(len(self.launches()), 1)
        self.assertNotIn('error', second)
        with self.store.session_lock(sid) as session:
            self.assertNotIn('error', self.store.read_receipt(session))

    def test_never_started_execute_is_relaunched_with_persisted_ack(self):
        sid = self.queued_execute_session()
        with self.store.session_lock(sid) as session:
            ack_before = (session / 'ack.json').read_bytes()
            self.assertEqual(self.store.read_receipt(session)['state'],
                             'execute_queued')
        pointer = self.store.base / 'pending' / (TX + '.json')
        pointer_before = pointer.read_bytes()

        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        result = self.manager().submit_execute(sid, self.ack(sid))
        self.assertEqual(result['state'], 'execute_queued')
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.launches()[0].args[0][-3:],
                         ['--worker', 'execute', sid])
        with self.store.session_lock(sid) as session:
            self.assertEqual((session / 'ack.json').read_bytes(), ack_before)
        self.assertEqual(pointer.read_bytes(), pointer_before)
        with mock.patch('updates.offline_execute.execute_offline_update',
                        return_value={'state': 'awaiting_reboot'}) as execute:
            self.manager().run_job('execute', sid)
        self.assertEqual(execute.call_args.kwargs['recovery_ack'],
                         json.loads(ack_before))

    def test_queued_with_durable_result_is_adopted_without_dispatch(self):
        sid = self.interrupted_prepare(receipt_state='prepare_queued')
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        result = self.manager().submit_prepare(sid)
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual(result['result']['state'], 'awaiting_recovery_export')
        self.assertFalse(self.launches())

        sid = self.interrupted_execute(receipt_state='execute_queued')
        self.runner.reset_mock()
        self.trial.return_value = self.evidence(sid)
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        result = self.manager().submit_execute(sid, self.ack(sid))
        self.assertEqual(result['state'], 'awaiting_reboot')
        self.assertFalse(self.launches())
        self.reboot.assert_not_called()

    def test_partial_unknown_or_uncertain_queued_jobs_are_never_relaunched(self):
        # A prepare that already created its transaction directory has started.
        sid = self.queued_prepare_session()
        with self.store.session_lock(sid) as session:
            (session / 'transaction').mkdir()
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        before = self.receipt_bytes(sid)
        receipt = self.manager().submit_prepare(sid)
        self.assertEqual(receipt['state'], 'prepare_queued')
        self.assertFalse(self.launches())
        self.assertEqual(self.receipt_bytes(sid), before)

        # An execute that already wrote its first artifact has started.
        sid = self.queued_execute_session()
        with self.store.session_lock(sid) as session:
            (session / 'transaction/recovery-ack.json').write_bytes(b'{}')
        self.runner.reset_mock()
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        before = self.receipt_bytes(sid)
        receipt = self.manager().submit_execute(sid, self.ack(sid))
        self.assertEqual(receipt['state'], 'execute_queued')
        self.assertFalse(self.launches())
        self.assertEqual(self.receipt_bytes(sid), before)

        # A still-loaded unit may just not have claimed the receipt yet.
        sid = self.queued_prepare_session()
        self.runner.side_effect = self.dispatch_runner(load_state='loaded')
        self.assertEqual(self.manager().submit_prepare(sid)['state'],
                         'prepare_queued')
        self.assertFalse(self.launches())

        # An unreadable systemd answer is unknown, never "absent".
        sid = self.queued_prepare_session()
        self.runner.side_effect = self.dispatch_runner(load_state=None)
        self.assertEqual(self.manager().submit_prepare(sid)['state'],
                         'prepare_queued')
        self.assertFalse(self.launches())

        # dispatch_uncertain is never retried.
        sid = self.queued_prepare_session()
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            receipt['state'] = 'dispatch_uncertain'
            self.store.update_receipt(session, receipt)
        self.runner.side_effect = self.dispatch_runner(load_state='not-found')
        self.assertEqual(self.manager().submit_prepare(sid)['state'],
                         'dispatch_uncertain')
        self.assertFalse(self.launches())

    def test_duplicate_dispatch_of_a_running_unit_is_not_uncertain(self):
        sid = self.session()
        self.runner.side_effect = self.dispatch_runner(
            load_state='loaded', launch_ok=False)
        result = self.manager().submit_prepare(sid)
        self.assertEqual(result['state'], 'prepare_queued')
        self.assertNotIn('error', result)
        with self.store.session_lock(sid) as session:
            self.assertNotIn('error', self.store.read_receipt(session))

        # A launch that fails with no owned unit is still uncertain.
        sid = self.session()
        self.runner.side_effect = self.dispatch_runner(
            load_state='not-found', launch_ok=False)
        self.assertEqual(self.manager().submit_prepare(sid)['state'],
                         'dispatch_uncertain')

    def test_status_never_relaunches_a_never_started_queue(self):
        sid = self.queued_prepare_session()
        self.runner.return_value = subprocess.CompletedProcess(
            args=[], returncode=3, stdout=b'inactive\n', stderr=b'')
        before = self.receipt_bytes(sid)
        result = self.manager().status(sid)
        self.assertEqual(result['state'], 'prepare_queued')
        self.assertTrue(result['jobInterrupted'])
        self.assertTrue(result['attentionRequired'])
        self.assertFalse(self.launches())
        self.assertEqual(self.receipt_bytes(sid), before)
        self.reboot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
