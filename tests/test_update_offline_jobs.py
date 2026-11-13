# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))
sys.path.insert(0, str(ROOT / 'tests'))
from updates import offline_jobs, offline_session, system_transaction
from test_update_channel_release import (
    _generate_keypair, _sign_bytes, _upgrade_package_dict,
)

PHYSICAL = {
    'model': 'TB322FC', 'actualSlot': 'a', 'inactiveSlot': 'b',
    'deployment': 'armada/' + ('a' * 64) + '.0',
    'recoveryPartUuid': '12345678-1234-5678-9abc-123456789abc',
}
PAYLOAD = b'data'
TX = '11111111-2222-4333-8444-555555555555'
OTHER_TX = '99999999-8888-4777-8666-555555555555'
NEW_DEPLOYMENT = 'armada/' + ('b' * 64) + '.1'


class OfflineJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        private, public = _generate_keypair(root, 'offline')
        package = _upgrade_package_dict()
        package['minimumInstallerVersion'] = '0.8.0'
        package['minimumUpdaterVersion'] = '0.8.0'
        entry = next(e for e in package['files'] if e['role'] == 'linux-root')
        entry.update(path='original.img', bytes=len(PAYLOAD),
                     sha256=hashlib.sha256(PAYLOAD).hexdigest())
        self.document = json.dumps(package, sort_keys=True,
                                   separators=(',', ':')).encode()
        self.signature = _sign_bytes(self.document, private)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'', stderr=b''))
        self.inspect = mock.Mock(return_value=dict(PHYSICAL))
        self.store = offline_session.OfflineSessionStore(
            base=root / 'private', trusted_key=public, inspect=self.inspect)
        self.public = public
        patcher = mock.patch('armada_os.snapshot',
                             return_value={'channel': 'stable'})
        self.snapshot = patcher.start()
        self.addCleanup(patcher.stop)
        # Live trial/boot evidence is a device read; each test states what the
        # tablet would report instead of depending on the host filesystem.
        patcher = mock.patch('updates.offline_jobs._read_trial_evidence',
                             return_value={'state': 'unavailable',
                                           'error': 'test'})
        self.trial = patcher.start()
        self.addCleanup(patcher.stop)
        self.reboot = mock.Mock(return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b'', stderr=b''))

    def manager(self):
        return offline_jobs.OfflineJobManager(
            self.store, runner=self.runner, reboot=self.reboot)

    def session(self):
        receipt = self.store.begin(self.document, self.signature)
        sid = receipt['sessionId']
        self.store.upload(sid, 0, PAYLOAD)
        self.store.seal(sid)
        return sid

    @staticmethod
    def prepared(work_dir, **_kwargs):
        work_dir.mkdir()
        plan = {'transactionId': TX, 'operation': 'offline-upgrade'}
        manifest = {'files': [{'path': 'original.img', 'bytes': len(PAYLOAD),
                               'sha256': hashlib.sha256(PAYLOAD).hexdigest()}]}
        plan_raw = system_transaction._save_json(work_dir, 'plan.json', plan)
        manifest_raw = system_transaction._save_json(
            work_dir, 'recovery-manifest.json', manifest)
        prepared = {'transactionId': TX,
                    'planSha256': hashlib.sha256(plan_raw).hexdigest(),
                    'recoveryManifestSha256': hashlib.sha256(
                        manifest_raw).hexdigest()}
        system_transaction._save_json(work_dir, 'prepared.json', prepared)
        status = {'state': 'awaiting_recovery_export', 'transactionId': TX}
        system_transaction._save_json(work_dir, 'status.json', status)
        (work_dir / 'original.img').write_bytes(PAYLOAD)
        return status

    def ack(self, sid):
        with self.store.session_lock(sid) as session:
            tx = session / 'transaction'
            prepared = json.loads((tx / 'prepared.json').read_text())
            manifest = json.loads((tx / 'recovery-manifest.json').read_text())
            return {'schemaVersion': 1, 'operation': 'offline-upgrade',
                    'transactionId': prepared['transactionId'],
                    'planSha256': prepared['planSha256'],
                    'recoveryManifestSha256': prepared['recoveryManifestSha256'],
                    'files': manifest['files']}

    def prepared_executed(self, work_dir, **_kwargs):
        """Like ``prepared`` but the plan carries the executed slot binding."""
        work_dir.mkdir()
        plan = {'transactionId': TX, 'operation': 'offline-upgrade',
                'oldDeployment': PHYSICAL['deployment'],
                'newDeployment': NEW_DEPLOYMENT,
                'fromSlot': 'a', 'targetSlot': 'b'}
        manifest = {'files': [{'path': 'original.img', 'bytes': len(PAYLOAD),
                               'sha256': hashlib.sha256(PAYLOAD).hexdigest()}]}
        plan_raw = system_transaction._save_json(work_dir, 'plan.json', plan)
        manifest_raw = system_transaction._save_json(
            work_dir, 'recovery-manifest.json', manifest)
        prepared = {'transactionId': TX,
                    'planSha256': hashlib.sha256(plan_raw).hexdigest(),
                    'recoveryManifestSha256': hashlib.sha256(
                        manifest_raw).hexdigest()}
        system_transaction._save_json(work_dir, 'prepared.json', prepared)
        status = {'state': 'awaiting_recovery_export', 'transactionId': TX}
        system_transaction._save_json(work_dir, 'status.json', status)
        (work_dir / 'original.img').write_bytes(PAYLOAD)
        return status

    def executed_session(self):
        """Return a session whose execute worker reported awaiting_reboot."""
        sid = self.session()
        with mock.patch('updates.offline_prepare.prepare_offline_update',
                        side_effect=self.prepared_executed):
            mgr = self.manager()
            mgr.submit_prepare(sid)
            mgr.run_job('prepare', sid)
        mgr.submit_execute(sid, self.ack(sid))
        with mock.patch('updates.offline_execute.execute_offline_update',
                        return_value={'state': 'awaiting_reboot',
                                      'bootWriteState': 'written',
                                      'requiresReboot': True}):
            self.assertEqual(
                mgr.run_job('execute', sid)['state'], 'awaiting_reboot')
        return sid

    def evidence(self, sid, **overrides):
        """Trial evidence describing this session's transaction."""
        with self.store.session_lock(sid) as session:
            prepared = json.loads(
                (session / 'transaction/prepared.json').read_text())
        body = {'state': 'available', 'transactionId': TX,
                'oldDeployment': PHYSICAL['deployment'],
                'newDeployment': NEW_DEPLOYMENT,
                'fromSlot': 'a', 'targetSlot': 'b',
                'planSha256': prepared['planSha256'],
                'recordState': 'prepared', 'runningDeployment': 'old',
                'bootConfirmed': False,
                'finalization': {'state': 'pending', 'error': 'not booted in'}}
        body.update(overrides)
        return body

    def receipt_bytes(self, sid):
        with self.store.session_lock(sid) as session:
            return (session / 'receipt.json').read_bytes()

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
            self.assertEqual(self.runner.call_count, 1)
            argv = self.runner.call_args.args[0]
            self.assertEqual(argv[:3], ['systemd-run', '--quiet', '--collect'])
            self.assertIn('--property=Type=exec', argv)
            self.assertIn('/usr/libexec/armada/armada-offline-update', argv)
            self.assertEqual(argv[-3:], ['--worker', 'prepare', sid])
            self.runner.assert_called_once_with(
                argv, capture_output=True, check=True, timeout=30)
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
        self.assertEqual(self.runner.call_count, 2)
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
        self.runner.assert_called_once()
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


if __name__ == '__main__':
    unittest.main()
