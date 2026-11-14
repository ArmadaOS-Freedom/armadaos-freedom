# SPDX-License-Identifier: GPL-3.0-only
"""Session fixtures for the detached offline update job tests.

``OfflineJobFixtureMixin`` only builds sessions and the transaction documents a
worker would leave behind; the tests themselves live in
``test_update_offline_jobs``.  The module is deliberately not named ``test_*``
and the class deliberately does not derive from ``unittest.TestCase`` so that
unittest discovery never collects it on its own.
"""
from __future__ import annotations

import hashlib
import json
import os
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

ROOT_REQUIRED = unittest.skipUnless(
    getattr(os, 'geteuid', lambda: -1)() == 0,
    'offline session storage requires root-owned private directories')

PHYSICAL = {
    'model': 'TB322FC', 'actualSlot': 'a', 'inactiveSlot': 'b',
    'deployment': 'armada/' + ('a' * 64) + '.0',
    'recoveryPartUuid': '12345678-1234-5678-9abc-123456789abc',
}
PAYLOAD = b'data'
TX = '11111111-2222-4333-8444-555555555555'
OTHER_TX = '99999999-8888-4777-8666-555555555555'
INSTALLATION_UUID = '33333333-4444-4555-8666-777777777777'
NEW_DEPLOYMENT = 'armada/' + ('b' * 64) + '.1'
ORIGINAL_BOOT = {'a': 'e' * 64, 'b': 'f' * 64}
NEW_BOOT = {'a': '1' * 64, 'b': '2' * 64}


class OfflineJobFixtureMixin:
    """Build offline sessions and the documents their workers leave behind."""

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
                    'files': [{k: f[k] for k in ('path', 'bytes', 'sha256')}
                              for f in manifest['files']]}

    def write_prepare_documents(self, session, work_dir, transaction_id=TX,
                                plan=None, status=None, manifest=None,
                                prepared=None):
        """Write the terminal documents a prepare worker leaves behind.

        The shape follows offline_prepare.py: the manifest is written first,
        then the plan that binds its digest, then prepared.json, and status.json
        last as the durable completion marker.
        """
        work_dir.mkdir()
        document = (session / 'package.json').read_bytes()
        signature = (session / 'package.sig').read_bytes()
        document_sha = hashlib.sha256(document).hexdigest()
        manifest_body = {
            'schemaVersion': 1, 'operation': 'offline-upgrade',
            'transactionId': transaction_id, 'model': PHYSICAL['model'],
            'installationUuid': INSTALLATION_UUID,
            'recoveryPartUuid': PHYSICAL['recoveryPartUuid'], 'board': 1,
            'oldDeployment': PHYSICAL['deployment'], 'fromSlot': 'a',
            'physical': dict(PHYSICAL),
            'partitions': {'a': 'partition-a', 'b': 'partition-b'},
            'files': [{'path': 'original.img', 'bytes': len(PAYLOAD),
                       'sha256': hashlib.sha256(PAYLOAD).hexdigest(),
                       'kind': 'slot', 'label': 'a.boot'}],
        }
        manifest_body.update(manifest or {})
        manifest_raw = system_transaction._save_json(
            work_dir, 'recovery-manifest.json', manifest_body)
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        plan_body = {
            'schemaVersion': 1, 'operation': 'offline-upgrade',
            'policy': 'offline-dual', 'transactionId': transaction_id,
            'model': PHYSICAL['model'], 'channel': 'stable',
            'sysrootPath': '/sysroot', 'releaseId': 'release-1',
            'version': '1.0.0', 'sourceRevision': 'c' * 40,
            'ociDigest': 'sha256:' + ('d' * 64), 'ostreeCommit': 'b' * 64,
            'fromSlot': 'a', 'targetSlot': 'b',
            'oldDeployment': PHYSICAL['deployment'],
            'newDeployment': NEW_DEPLOYMENT,
            'originalBootSha256': dict(ORIGINAL_BOOT),
            'newBootSha256': dict(NEW_BOOT),
            'recoveryManifestSha256': manifest_sha,
            'packageDescriptorSha256': document_sha,
        }
        plan_body.update(plan or {})
        plan_raw = system_transaction._save_json(work_dir, 'plan.json', plan_body)
        plan_sha = hashlib.sha256(plan_raw).hexdigest()
        (work_dir / 'package.json').write_bytes(document)
        (work_dir / 'package.sig').write_bytes(signature)
        prepared_body = {
            'schemaVersion': 1, 'operation': 'offline-upgrade',
            'policy': 'offline-dual', 'transactionId': transaction_id,
            'plan': 'plan.json', 'planSha256': plan_sha,
            'trust': 'trust.json', 'materials': 'materials.json',
            'candidates': {'a': 'candidate-a.json', 'b': 'candidate-b.json'},
            'recoveryManifest': 'recovery-manifest.json',
            'recoveryManifestSha256': manifest_sha,
            'packageDescriptor': 'package.json',
            'packageSignature': 'package.sig',
            'packageDescriptorSha256': document_sha,
        }
        prepared_body.update(prepared or {})
        system_transaction._save_json(work_dir, 'prepared.json', prepared_body)
        status_body = {
            'schemaVersion': 1, 'component': 'system',
            'operation': 'offline-upgrade', 'policy': 'offline-dual',
            'bootPolicy': system_transaction.BOOT_POLICY,
            'transactionId': transaction_id, 'model': PHYSICAL['model'],
            'fromSlot': 'a', 'targetSlot': 'b',
            'oldDeployment': PHYSICAL['deployment'],
            'newDeployment': NEW_DEPLOYMENT,
            'phase': 'awaiting_recovery_export',
            'state': 'awaiting_recovery_export',
            'bootWriteState': 'not_started',
            'slotControlState': 'not_issued', 'requiresReboot': False,
            'recoveryManifest': 'recovery-manifest.json',
            'recoveryManifestSha256': manifest_sha,
            'recoveryManifestBytes': len(manifest_raw),
            'planSha256': plan_sha,
        }
        status_body.update(status or {})
        system_transaction._save_json(work_dir, 'status.json', status_body)
        return status_body

    def write_executed_documents(self, work_dir, transaction_id):
        """Write the transaction documents a completed prepare leaves behind."""
        work_dir.mkdir()
        plan = {'schemaVersion': 1, 'operation': 'offline-upgrade',
                'policy': 'offline-dual', 'transactionId': transaction_id,
                'model': PHYSICAL['model'],
                'oldDeployment': PHYSICAL['deployment'],
                'newDeployment': NEW_DEPLOYMENT,
                'fromSlot': 'a', 'targetSlot': 'b',
                'originalBootSha256': dict(ORIGINAL_BOOT),
                'newBootSha256': dict(NEW_BOOT)}
        manifest = {'files': [{'path': 'original.img', 'bytes': len(PAYLOAD),
                               'sha256': hashlib.sha256(PAYLOAD).hexdigest()}]}
        plan_raw = system_transaction._save_json(work_dir, 'plan.json', plan)
        manifest_raw = system_transaction._save_json(
            work_dir, 'recovery-manifest.json', manifest)
        prepared = {'transactionId': transaction_id,
                    'planSha256': hashlib.sha256(plan_raw).hexdigest(),
                    'recoveryManifestSha256': hashlib.sha256(
                        manifest_raw).hexdigest()}
        system_transaction._save_json(work_dir, 'prepared.json', prepared)
        status = {'state': 'awaiting_recovery_export',
                  'transactionId': transaction_id}
        system_transaction._save_json(work_dir, 'status.json', status)
        (work_dir / 'original.img').write_bytes(PAYLOAD)
        return status

    def prepared_session(self, transaction_id=TX):
        """Return a session whose prepare job completed for one transaction."""
        sid = self.session()
        with mock.patch(
                'updates.offline_prepare.prepare_offline_update',
                side_effect=lambda work_dir, **_kwargs: (
                    self.write_executed_documents(work_dir, transaction_id))):
            mgr = self.manager()
            mgr.submit_prepare(sid)
            mgr.run_job('prepare', sid)
        self.runner.reset_mock()
        return sid

    def submitted_session(self, transaction_id=TX):
        """Return a session whose execute job was submitted and queued."""
        sid = self.prepared_session(transaction_id)
        self.manager().submit_execute(sid, self.ack(sid))
        return sid

    def executed_session(self):
        """Return a session whose execute worker reported awaiting_reboot."""
        sid = self.submitted_session()
        with mock.patch('updates.offline_execute.execute_offline_update',
                        return_value={'state': 'awaiting_reboot',
                                      'bootWriteState': 'written',
                                      'requiresReboot': True}):
            self.assertEqual(
                self.manager().run_job('execute', sid)['state'], 'awaiting_reboot')
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

    def confirmed_evidence(self, sid, **overrides):
        """Trial evidence for a transaction already booted and finalized."""
        confirmed = {'recordState': 'confirmed', 'runningDeployment': 'new',
                     'bootConfirmed': True,
                     'finalization': {'state': 'complete'}}
        confirmed.update(overrides)
        return self.evidence(sid, **confirmed)

    def receipt_bytes(self, sid):
        with self.store.session_lock(sid) as session:
            return (session / 'receipt.json').read_bytes()

    @staticmethod
    def write_report():
        """Return the verified dual-slot write report a worker leaves behind."""
        return {
            'phase': 'dual_boots_written', 'model': PHYSICAL['model'],
            'transactionUuid': TX, 'deployment': NEW_DEPLOYMENT,
            'fromSlot': 'a', 'slotOrder': ['b', 'a'],
            'writes': {
                slot: {
                    'sourceBootSha256': ORIGINAL_BOOT[slot],
                    'candidate': {'path': '/work/candidate-%s.img' % slot,
                                  'bytes': 4096,
                                  'sha256': NEW_BOOT[slot]},
                    'bytesWritten': 4096,
                    'readbackSha256': NEW_BOOT[slot],
                } for slot in ('a', 'b')
            },
        }

    def completed_status(self, sid, report=None, **overrides):
        """Write the durable execute result a worker leaves before it reports."""
        with self.store.session_lock(sid) as session:
            transaction = session / 'transaction'
            prepared = json.loads((transaction / 'prepared.json').read_text())
            status = {
                'schemaVersion': 1, 'component': 'system',
                'operation': 'offline-upgrade', 'policy': 'offline-dual',
                'bootPolicy': 'dual-boot-no-slot-control',
                'transactionId': TX, 'model': PHYSICAL['model'],
                'fromSlot': 'a', 'targetSlot': 'b',
                'oldDeployment': PHYSICAL['deployment'],
                'newDeployment': NEW_DEPLOYMENT,
                'planSha256': prepared['planSha256'],
                'state': 'awaiting_reboot', 'phase': 'awaiting_reboot',
                'requiresReboot': True, 'bootWriteState': 'written',
                'slotControlState': 'not_issued',
            }
            status.update(overrides)
            system_transaction._save_json(transaction, 'status.json', status)
            write_report = self.write_report() if report is None else report
            system_transaction._save_json(
                transaction, 'write-report.json', write_report)
            return status

    def interrupted_execute(self, receipt_state='executing', report=None,
                            **overrides):
        """Return a session whose execute worker died before recording its result."""
        sid = self.executed_session()
        self.completed_status(sid, report=report, **overrides)
        with self.store.session_lock(sid) as session:
            receipt = self.store.read_receipt(session)
            receipt['state'] = receipt_state
            receipt.pop('result', None)
            self.store.update_receipt(session, receipt)
        self.runner.return_value.returncode = 3
        return sid

    @staticmethod
    def dispatch_runner(*, load_state='not-found', launch_ok=True):
        """Simulate systemd-run and systemctl for the queued-resume paths."""
        def run(argv, **_kwargs):
            if argv[0] == 'systemd-run':
                if not launch_ok:
                    raise subprocess.TimeoutExpired('systemd-run', 30)
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout=b'', stderr=b'')
            if argv[:2] == ['systemctl', 'show']:
                if load_state is None:
                    return subprocess.CompletedProcess(
                        args=argv, returncode=1, stdout=b'', stderr=b'')
                return subprocess.CompletedProcess(
                    args=argv, returncode=0,
                    stdout=('LoadState=%s\n' % load_state).encode(), stderr=b'')
            raise AssertionError('unexpected command: %r' % (argv,))
        return run

    def launches(self):
        return [call for call in self.runner.call_args_list
                if call.args[0][0] == 'systemd-run']

    def queued_prepare_session(self):
        """Persist prepare_queued exactly like a submit that died before launch."""
        sid = self.session()
        with mock.patch.object(offline_jobs.OfflineJobManager, '_launch',
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager().submit_prepare(sid)
        self.runner.reset_mock()
        return sid

    def queued_execute_session(self):
        """Persist execute_queued with its ack, but never launch the worker."""
        sid = self.prepared_session()
        with mock.patch.object(offline_jobs.OfflineJobManager, '_launch',
                               side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.manager().submit_execute(sid, self.ack(sid))
        self.runner.reset_mock()
        return sid
