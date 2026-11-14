# SPDX-License-Identifier: GPL-3.0-only
"""Recovery cases for interrupted offline update jobs.

``OfflineJobRecoveryCases`` holds the execute-side adoption, stale-receipt and
pending-pointer tests that used to sit in ``test_update_offline_jobs``.  The
module is not named ``test_*`` and the class is not a ``unittest.TestCase``, so
unittest still collects every test exactly once, through the concrete
``OfflineJobTests`` class that mixes this one in.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))
sys.path.insert(0, str(ROOT / 'tests'))
from offline_job_fixtures import (
    NEW_BOOT, ORIGINAL_BOOT, OTHER_TX, TX,
)
from updates import offline_session, system_transaction


class OfflineJobRecoveryCases:
    """Execute-side recovery behaviour for an interrupted or stalled job."""

    def test_interrupted_execute_adopts_verified_completion_once(self):
        sid = self.interrupted_execute()
        self.trial.return_value = self.evidence(sid)
        mgr = self.manager()
        launches = sum(1 for call in self.runner.call_args_list
                       if call.args[0][0] == 'systemd-run')

        first = mgr.status(sid)
        self.assertEqual(first['state'], 'awaiting_reboot')
        self.assertEqual(first['result']['state'], 'awaiting_reboot')
        self.assertNotIn('jobInterrupted', first)
        self.assertNotIn('attentionRequired', first)
        self.assertEqual(first['completion']['state'], 'awaiting_reboot')
        self.assertEqual(first['completion']['transactionId'], TX)
        self.assertTrue(first['completion']['restartEligible'])

        recorded = self.receipt_bytes(sid)
        second = mgr.status(sid)
        self.assertEqual(second['state'], 'awaiting_reboot')
        self.assertEqual(self.receipt_bytes(sid), recorded)
        self.assertEqual(
            sum(1 for call in self.runner.call_args_list
                if call.args[0][0] == 'systemd-run'), launches)
        self.reboot.assert_not_called()

        result = mgr.request_restart(sid)
        self.assertEqual(result['state'], 'awaiting_reboot')
        self.assertEqual(result['restart']['state'], 'dispatched')
        self.assertEqual(self.reboot.call_count, 1)
        again = mgr.request_restart(sid)
        self.assertEqual(again['restart']['state'], 'dispatched')
        self.assertEqual(self.reboot.call_count, 1)

    def test_interrupted_dispatch_uncertain_adopts_completion(self):
        sid = self.interrupted_execute(receipt_state='dispatch_uncertain')
        self.trial.return_value = self.evidence(sid)
        result = self.manager().status(sid)
        self.assertEqual(result['state'], 'awaiting_reboot')
        self.assertEqual(result['completion']['state'], 'awaiting_reboot')
        self.reboot.assert_not_called()

    def test_request_restart_adopts_completion_without_a_status_first(self):
        sid = self.interrupted_execute()
        self.trial.return_value = self.evidence(sid)
        result = self.manager().request_restart(sid)
        self.assertEqual(result['restart']['state'], 'dispatched')
        self.assertEqual(self.reboot.call_count, 1)

    def test_interrupted_execute_refuses_foreign_or_missing_journal(self):
        sid = self.interrupted_execute()
        for trial in (self.evidence(sid, transactionId=OTHER_TX),
                      self.evidence(sid, planSha256='d' * 64),
                      {'state': 'unavailable', 'error': 'no journal'}):
            with self.subTest(trial=trial.get('transactionId')):
                self.trial.return_value = trial
                receipt = self.manager().status(sid)
                self.assertEqual(receipt['state'], 'executing')
                self.assertTrue(receipt['jobInterrupted'])
                self.assertTrue(receipt['attentionRequired'])
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.manager().request_restart(sid)
        self.reboot.assert_not_called()

    def test_interrupted_execute_refuses_unverified_durable_evidence(self):
        cases = (
            ('uncertain boot write', {'bootWriteState': 'uncertain'}),
            ('slot control issued', {'slotControlState': 'issued'}),
            ('no restart requirement', {'requiresReboot': False}),
            ('failed transaction', {'state': 'failed', 'phase': 'failed',
                                    'requiresReboot': False}),
            ('other plan', {'planSha256': 'd' * 64}),
            ('other transaction', {'transactionId': OTHER_TX}),
            ('other model', {'model': 'TB323FU'}),
        )
        for name, overrides in cases:
            with self.subTest(case=name):
                sid = self.interrupted_execute(**overrides)
                self.trial.return_value = self.evidence(sid)
                result = self.manager().status(sid)
                self.assertEqual(result['state'], 'executing')
                self.assertTrue(result['jobInterrupted'])
                self.assertTrue(result['attentionRequired'])
                self.reboot.assert_not_called()

    def test_interrupted_execute_refuses_missing_write_report_or_unapproved_plan(self):
        for case in ('write-report', 'ack'):
            with self.subTest(case=case):
                sid = self.interrupted_execute()
                with self.store.session_lock(sid) as session:
                    if case == 'write-report':
                        (session / 'transaction/write-report.json').unlink()
                    else:
                        ack = json.loads((session / 'ack.json').read_text())
                        ack['planSha256'] = 'd' * 64
                        system_transaction._save_json(session, 'ack.json', ack)
                self.trial.return_value = self.evidence(sid)
                result = self.manager().status(sid)
                self.assertEqual(result['state'], 'executing')
                self.assertTrue(result['jobInterrupted'])
                self.reboot.assert_not_called()

    def test_interrupted_execute_refuses_unverified_write_evidence(self):
        def slot(slot, **changes):
            report = self.write_report()
            report['writes'][slot].update(changes)
            return report

        def report(**changes):
            return dict(self.write_report(), **changes)

        cases = (
            ('empty slot entries', report(writes={'a': {}, 'b': {}})),
            ('missing writes', {k: v for k, v in self.write_report().items()
                                if k != 'writes'}),
            ('writes as a list', report(writes=[])),
            ('running slot written first', report(slotOrder=['a', 'b'])),
            ('short write', slot('b', bytesWritten=1)),
            ('zero write', slot('b', bytesWritten=0)),
            ('readback mismatch', slot('b', readbackSha256='3' * 64)),
            ('source mismatch', slot('b', sourceBootSha256=NEW_BOOT['b'])),
            ('candidate digest mismatch',
             slot('b', candidate={'path': '/work/b.img', 'bytes': 4096,
                                  'sha256': ORIGINAL_BOOT['b']})),
            ('candidate replaced',
             report(writes=dict(
                 self.write_report()['writes'], b={'candidate': {}}))),
            ('missing first slot',
             report(writes={'a': self.write_report()['writes']['a']})),
        )
        for name, write_report in cases:
            with self.subTest(case=name):
                sid = self.interrupted_execute(report=write_report)
                self.trial.return_value = self.evidence(sid)
                result = self.manager().status(sid)
                self.assertEqual(result['state'], 'executing')
                self.assertTrue(result['jobInterrupted'])
                self.assertTrue(result['attentionRequired'])
                self.reboot.assert_not_called()

    def test_stale_executing_receipt_is_repaired_after_physical_confirmation(self):
        # The worker saved status.json and the write report, then the power
        # cut before the receipt was updated.  The device has since booted the
        # new deployment and finalization is complete.
        sid = self.interrupted_execute()
        self.trial.return_value = self.confirmed_evidence(sid)
        mgr = self.manager()
        launches = len(self.launches())

        receipt = mgr.status(sid)
        self.assertEqual(receipt['state'], 'awaiting_reboot')
        self.assertEqual(receipt['result']['state'], 'awaiting_reboot')
        self.assertNotIn('jobInterrupted', receipt)
        self.assertNotIn('attentionRequired', receipt)
        self.assertEqual(receipt['completion']['state'], 'confirmed')
        self.assertEqual(receipt['completion']['transactionId'], TX)
        self.assertFalse(receipt['completion']['restartEligible'])
        self.assertEqual(len(self.launches()), launches)
        self.reboot.assert_not_called()

        recorded = self.receipt_bytes(sid)
        with self.store.session_lock(sid) as session:
            persisted = self.store.read_receipt(session)
        self.assertEqual(persisted['state'], 'awaiting_reboot')
        self.assertEqual(persisted['result']['state'], 'awaiting_reboot')
        self.assertNotIn('restart', persisted)

        again = mgr.status(sid)
        self.assertEqual(again['completion']['state'], 'confirmed')
        self.assertEqual(self.receipt_bytes(sid), recorded)
        self.assertEqual(len(self.launches()), launches)

        # A confirmed transaction is never restarted by the repair path.
        with self.assertRaises(offline_session.OfflineSessionError):
            mgr.request_restart(sid)
        self.reboot.assert_not_called()
        self.assertEqual(self.receipt_bytes(sid), recorded)

    def test_stale_executing_receipt_refuses_unfinalized_or_foreign_record(self):
        cases = (
            ('finalization pending',
             {'finalization': {'state': 'pending', 'error': 'journal busy'}}),
            ('other transaction', {'transactionId': OTHER_TX}),
            ('running old deployment', {'runningDeployment': 'old'}),
            ('boot evidence missing',
             {'runningDeployment': None, 'bootConfirmed': False}),
        )
        for name, overrides in cases:
            with self.subTest(case=name):
                sid = self.interrupted_execute()
                self.trial.return_value = self.confirmed_evidence(sid, **overrides)
                before = self.receipt_bytes(sid)

                receipt = self.manager().status(sid)
                self.assertEqual(receipt['state'], 'executing')
                self.assertTrue(receipt['jobInterrupted'])
                self.assertTrue(receipt['attentionRequired'])
                self.assertEqual(self.receipt_bytes(sid), before)
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.manager().request_restart(sid)
                self.assertEqual(self.receipt_bytes(sid), before)
                self.reboot.assert_not_called()

    def test_stale_executing_receipt_requires_complete_durable_evidence(self):
        sid = self.interrupted_execute()
        self.trial.return_value = self.confirmed_evidence(sid)
        with self.store.session_lock(sid) as session:
            (session / 'transaction/write-report.json').unlink()
        before = self.receipt_bytes(sid)

        receipt = self.manager().status(sid)
        self.assertEqual(receipt['state'], 'executing')
        self.assertTrue(receipt['jobInterrupted'])
        self.assertTrue(receipt['attentionRequired'])
        self.assertEqual(self.receipt_bytes(sid), before)
        self.reboot.assert_not_called()

    def test_stale_executing_receipt_is_not_repaired_while_worker_is_active(self):
        sid = self.interrupted_execute()
        self.trial.return_value = self.confirmed_evidence(sid)
        self.runner.return_value.returncode = 0
        before = self.receipt_bytes(sid)

        receipt = self.manager().status(sid)
        self.assertEqual(receipt['state'], 'executing')
        self.assertTrue(receipt['jobActive'])
        self.assertNotIn('jobInterrupted', receipt)
        self.assertEqual(self.receipt_bytes(sid), before)
        self.reboot.assert_not_called()

    def test_completion_pointer_is_keyed_by_transaction(self):
        first = self.submitted_session(TX)
        second = self.submitted_session(OTHER_TX)
        pending = self.store.base / 'pending'
        for transaction, session_id in ((TX, first), (OTHER_TX, second)):
            pointer = json.loads((pending / (transaction + '.json')).read_text())
            self.assertEqual(pointer, {'schemaVersion': 1,
                                       'transactionId': transaction,
                                       'sessionId': session_id})
        # A second queued session publishes its own pointer and cannot displace
        # the one that still describes the first pending transaction.
        self.assertEqual(
            sorted(path.name for path in pending.iterdir()),
            sorted([TX + '.json', OTHER_TX + '.json']))
        self.assertFalse((self.store.base / 'current.json').exists())

    def test_pointer_is_published_before_execute_is_queued(self):
        sid = self.prepared_session()
        ack = self.ack(sid)
        real = system_transaction._save_json

        def failing_pointer(directory, name, value):
            if name == TX + '.json':
                raise OSError('pointer storage is unavailable')
            return real(directory, name, value)

        with mock.patch('updates.offline_jobs.system_transaction._save_json',
                        side_effect=failing_pointer):
            with self.assertRaises(OSError):
                self.manager().submit_execute(sid, ack)
        with self.store.session_lock(sid) as session:
            self.assertEqual(self.store.read_receipt(session)['state'], 'prepared')
        self.runner.assert_not_called()

    def test_interrupted_execute_never_adopts_while_worker_is_active(self):
        sid = self.interrupted_execute()
        self.trial.return_value = self.evidence(sid)
        self.runner.return_value.returncode = 0
        result = self.manager().status(sid)
        self.assertEqual(result['state'], 'executing')
        self.assertTrue(result['jobActive'])
        self.assertNotIn('jobInterrupted', result)
        with self.assertRaises(offline_session.OfflineSessionError):
            self.manager().request_restart(sid)
        self.reboot.assert_not_called()
