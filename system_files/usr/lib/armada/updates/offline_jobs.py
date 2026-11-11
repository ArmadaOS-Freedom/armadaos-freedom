# SPDX-License-Identifier: GPL-3.0-only
"""Detached systemd jobs for private offline-update sessions."""

from __future__ import annotations

import hashlib
import subprocess

import armada_os
from . import (
    boot_context,
    offline_completion,
    offline_context,
    offline_execute,
    offline_session,
    system_transaction,
    trial_status,
)

SYSROOT_PATH = '/sysroot'
_DOCUMENT_MAX = 2 * 1024 * 1024
_SIGNATURE_MAX = 16 * 1024
_BUSY = frozenset({
    'prepare_queued', 'execute_queued', 'preparing', 'executing',
    'dispatch_uncertain',
})
# Receipt states whose transaction may already have been executed.  Only these
# are bound to live trial evidence; earlier states cannot have a transaction.
_COMPLETION_STATES = frozenset({
    'awaiting_reboot', 'failed', 'dispatch_uncertain',
})
_IDEMPOTENT = frozenset({
    'prepare_queued', 'preparing', 'prepared', 'up_to_date',
    'execute_queued', 'executing', 'awaiting_reboot', 'failed',
    'dispatch_uncertain',
})


def _read_trial_evidence():
    """Read the live trial/boot evidence without changing anything."""
    try:
        snapshot = armada_os.snapshot()
        booted = snapshot.get('booted') if isinstance(snapshot, dict) else None
        return trial_status.read_trial_status(boot_context.read_boot_context(booted))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        return {'state': 'unavailable',
                'error': str(error) or type(error).__name__}


def _legacy_evidence_unavailable(error):
    return {'schemaVersion': offline_completion.LEGACY_EVIDENCE_SCHEMA,
            'state': 'unavailable',
            'error': (str(error) or type(error).__name__)[:200]}


def _read_legacy_completion_evidence(session):
    """Read the explicit pre-reboot legacy evidence document, read-only.

    This never writes, stages, selects, or synthesizes a slot.  The exact
    booted deployment comes from libostree; the journal and both execute
    reports are the durable documents written by this transaction.
    """
    try:
        transaction = session / 'transaction'
        status, _raw = offline_execute._load_json(transaction, 'status.json')
        plan, _raw = offline_execute._load_json(transaction, 'plan.json')
        write_report, _raw = offline_execute._load_json(
            transaction, 'write-report.json')
        activation_report, _raw = offline_execute._load_json(
            transaction, 'activation-report.json')
        _booted, context = offline_context.read_legacy_boot_state()
        journal = trial_status.read_legacy_trial_status(context)
        return {
            'schemaVersion': offline_completion.LEGACY_EVIDENCE_SCHEMA,
            'state': 'available',
            'bootContext': context.get('bootContext'),
            'bootId': offline_context.read_boot_id(),
            'deployment': context.get('deployment'),
            'status': status,
            'journal': journal,
            'plan': plan,
            'writeReport': write_report,
            'activationReport': activation_report,
        }
    except Exception as error:
        return _legacy_evidence_unavailable(error)


class OfflineJobManager:
    """Submit authorized session operations without accepting client commands."""

    def __init__(self, store=None, *, runner=subprocess.run, reboot=None):
        self.store = store if store is not None else offline_session.OfflineSessionStore()
        self._runner = runner
        # Exactly one dedicated dispatcher: the restart RPC must never reuse the
        # detached-job runner, and it must stay replaceable for tests.
        self._reboot = reboot if reboot is not None else subprocess.run

    @staticmethod
    def _unit(operation, session_id):
        return 'armada-offline-%s-%s' % (operation, session_id)

    def _recheck_submission_identity(self, receipt):
        if self.store._physical_info() != receipt.get('physical'):
            raise offline_session.OfflineSessionError(
                'physical identity changed since session submission')
        channel = system_transaction._snapshot_channel(self.store._snapshot_state())
        if channel != receipt.get('channel'):
            raise offline_session.OfflineSessionError(
                'configured channel changed since session submission')

    def _launch(self, operation, session_id):
        argv = [
            'systemd-run', '--quiet', '--collect',
            '--unit=' + self._unit(operation, session_id),
            '--property=Type=exec', '--property=TimeoutStopSec=15min', '--',
            '/usr/bin/python3', '-I', '/usr/libexec/armada/armada-offline-update',
            '--worker', operation, session_id,
        ]
        self._runner(argv, capture_output=True, check=True, timeout=30)

    @staticmethod
    def _dispatch_failed(session, receipt, exc):
        # Never copy unbounded child stderr/stdout into persistent session state.
        receipt['state'] = 'dispatch_uncertain'
        receipt['error'] = ('detached job dispatch failed or timed out: %s' % exc)[:500]
        return receipt

    def submit_prepare(self, sessionId):
        launch = False
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            state = receipt.get('state')
            if state != 'uploaded':
                if state == 'uploading':
                    raise offline_session.OfflineSessionError(
                        'session payload has not been sealed')
                if (state not in _IDEMPOTENT or
                        receipt.get('jobOperation') not in ('prepare', 'execute')):
                    raise offline_session.OfflineSessionError(
                        'session receipt has an unrecognized state')
            else:
                self._recheck_submission_identity(receipt)
                receipt.update(
                    state='prepare_queued', jobOperation='prepare',
                    jobUnit=self._unit('prepare', sessionId))
                receipt.pop('error', None)
                self.store.update_receipt(session, receipt)
                launch = True
            if launch:
                try:
                    self._launch('prepare', sessionId)
                except (subprocess.SubprocessError, OSError) as exc:
                    current = self.store.read_receipt(session)
                    if (current.get('state') == 'prepare_queued' and
                            current.get('jobOperation') == 'prepare'):
                        self.store.update_receipt(
                            session, self._dispatch_failed(session, current, exc))
        return self.store.status(sessionId)

    def submit_execute(self, sessionId, recovery_ack):
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            state = receipt.get('state')
            if state != 'prepared':
                if receipt.get('jobOperation') != 'execute':
                    raise offline_session.OfflineSessionError(
                        'session is not prepared for execution')
                saved, _raw = offline_execute._load_json(session, 'ack.json')
                if recovery_ack != saved:
                    raise ValueError('recovery acknowledgement differs from saved acknowledgement')
            else:
                prepared_status, _raw = offline_execute._load_json(
                    session / 'transaction', 'status.json')
                if prepared_status.get('state') != 'awaiting_recovery_export':
                    raise offline_session.OfflineSessionError(
                        'transaction is not awaiting recovery export')
                prepared, _raw = offline_execute._load_json(
                    session / 'transaction', 'prepared.json')
                manifest, _raw = offline_execute._load_json(
                    session / 'transaction', 'recovery-manifest.json')
                offline_execute._check_recovery_ack(
                    recovery_ack, tx=prepared['transactionId'],
                    plan=prepared, manifest=manifest)
                system_transaction._save_json(session, 'ack.json', recovery_ack)
                receipt.update(
                    state='execute_queued', jobOperation='execute',
                    jobUnit=self._unit('execute', sessionId))
                receipt.pop('error', None)
                self.store.update_receipt(session, receipt)
                try:
                    self._launch('execute', sessionId)
                except (subprocess.SubprocessError, OSError) as exc:
                    current = self.store.read_receipt(session)
                    if (current.get('state') == 'execute_queued' and
                            current.get('jobOperation') == 'execute'):
                        self.store.update_receipt(
                            session, self._dispatch_failed(session, current, exc))
        return self.store.status(sessionId)

    def _prepare(self, session, receipt):
        self._recheck_submission_identity(receipt)
        document = offline_execute._read_bounded(
            session / 'package.json', _DOCUMENT_MAX, 'package document')
        signature_b64 = offline_execute._read_bounded(
            session / 'package.sig', _SIGNATURE_MAX, 'package signature')
        from . import offline_prepare
        return offline_prepare.prepare_offline_update(
            document=document, signature_b64=signature_b64,
            payload_directory=session / 'incoming',
            expected_channel=receipt['channel'], sysroot_path=SYSROOT_PATH,
            work_dir=session / 'transaction',
            updater_version=self.store.updater_version,
            trusted_key=self.store.trusted_key)

    def _execute(self, session):
        recovery_ack, _raw = offline_execute._load_json(session, 'ack.json')
        return offline_execute.execute_offline_update(
            work_dir=session / 'transaction', recovery_ack=recovery_ack,
            updater_version=self.store.updater_version,
            trusted_key=self.store.trusted_key)

    def _record_result(self, session_id, operation, busy_state, state, result):
        with self.store.session_lock(session_id) as session:
            receipt = self.store.read_receipt(session)
            if (receipt.get('state') != busy_state or
                    receipt.get('jobOperation') != operation):
                raise offline_session.OfflineSessionError(
                    'worker session state changed unexpectedly')
            receipt['state'] = state
            receipt['result'] = result
            self.store.update_receipt(session, receipt)

    def run_job(self, operation, sessionId):
        if operation not in ('prepare', 'execute'):
            raise ValueError('invalid offline job operation')
        busy = 'preparing' if operation == 'prepare' else 'executing'
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            queued = 'prepare_queued' if operation == 'prepare' else 'execute_queued'
            if (receipt.get('state') not in (queued, 'dispatch_uncertain') or
                    receipt.get('jobOperation') != operation):
                raise offline_session.OfflineSessionError(
                    'stale or duplicate offline worker invocation')
            receipt['state'] = busy
            self.store.update_receipt(session, receipt)
        try:
            if operation == 'prepare':
                result = self._prepare(session, receipt)
                states = {'awaiting_recovery_export': 'prepared', 'up_to_date': 'up_to_date'}
            else:
                result = self._execute(session)
                states = {'awaiting_reboot': 'awaiting_reboot'}
            if not isinstance(result, dict) or result.get('state') not in states:
                raise ValueError('offline worker returned an unexpected result')
            final = states[result['state']]
        except system_transaction.SystemTransactionError as exc:
            final, result = 'failed', exc.result
        except Exception as exc:
            final = 'failed'
            result = {
                'state': 'failed' if operation == 'prepare' else 'execution_uncertain',
                'error': str(exc)[:1000],
            }
        self._record_result(sessionId, operation, busy, final, result)
        return self.store.status(sessionId)

    @staticmethod
    def _session_identity(session):
        """Read the executed transaction binding from immutable session documents."""
        transaction = session / 'transaction'
        if not transaction.is_dir():
            return None
        try:
            prepared, _prepared_raw = offline_execute._load_json(
                transaction, 'prepared.json')
            plan, plan_raw = offline_execute._load_json(transaction, 'plan.json')
        except (OSError, ValueError):
            return None
        return offline_completion.document_identity(
            prepared, plan, hashlib.sha256(plan_raw).hexdigest())

    @staticmethod
    def _completion(session, receipt, identity):
        """Evaluate this session with the strict or the legacy reader.

        A legacy controller-only receipt has no witness and no actual slot, so
        it is judged by the narrow pre-reboot evidence document.  Every other
        receipt keeps the unchanged strict trial/witness path.
        """
        physical = receipt.get('physical')
        if (isinstance(physical, dict) and
                physical.get('bootContext') == offline_completion.LEGACY_BOOT_CONTEXT):
            verdict = offline_completion.evaluate_legacy_pre_reboot(
                receipt, _read_legacy_completion_evidence(session), identity)
            # The narrow verdict only covers the boot that performed the
            # activation.  After a reboot (or when the legacy reader is no
            # longer applicable) the strict witness/finalization path decides;
            # a confirmed result can never come from the legacy document.
            if verdict.get('reason') in ('boot_changed',
                                         'legacy_evidence_unavailable'):
                strict = offline_completion.evaluate(
                    receipt, _read_trial_evidence(), identity)
                if (strict.get('state') != offline_completion.UNVERIFIED or
                        strict.get('reason') != 'trial_unavailable'):
                    return strict
            return verdict
        return offline_completion.evaluate(
            receipt, _read_trial_evidence(), identity)

    def _attach_completion(self, session_id, receipt):
        """Attach the read-only completion verdict for an executed session."""
        if receipt.get('state') not in _COMPLETION_STATES:
            return receipt
        try:
            session = self.store.resolve_session(session_id)
            identity = self._session_identity(session)
        except (OSError, ValueError, RuntimeError) as error:
            receipt['completion'] = {
                'schemaVersion': 1,
                'state': offline_completion.UNVERIFIED,
                'transactionId': None,
                'reason': 'session_documents_unavailable',
                'error': str(error)[:200],
                'restartEligible': False,
                'restartState': None,
            }
            return receipt
        receipt['completion'] = self._completion(session, receipt, identity)
        return receipt

    @staticmethod
    def _restart_result(session_id, identity, restart):
        return {
            'schemaVersion': 1,
            'sessionId': session_id,
            'state': 'awaiting_reboot',
            'transactionId': identity['transactionId'],
            'deployment': identity['newDeployment'],
            'restart': {
                'schemaVersion': 1,
                'state': restart['state'],
                'transactionId': identity['transactionId'],
            },
        }

    def request_restart(self, sessionId):
        """Persist one restart request, verify eligibility, then reboot once.

        The receipt is made durable *before* the reboot is scheduled so that a
        lost connection is later readable as a pending restart rather than as a
        failed upgrade.  Any stored request that may already have reached the
        firmware (dispatching, dispatched, uncertain) is answered from the
        persisted record and never reboots again; only an explicitly rejected
        request (failed) can be retried by the user, after the transaction is
        re-checked for eligibility.
        """
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            if receipt.get('state') != 'awaiting_reboot':
                raise offline_session.OfflineSessionError(
                    'session is not awaiting a restart')
            identity = self._session_identity(session)
            if identity is None:
                raise offline_session.OfflineSessionError(
                    'session transaction identity is unavailable')
            saved = receipt.get('restart')
            if (isinstance(saved, dict)
                    and saved.get('transactionId') == identity['transactionId']
                    and saved.get('state') in ('dispatching', 'dispatched', 'uncertain')):
                # A request may already have reached the firmware.  Report the
                # persisted pending result and never issue a second reboot, not
                # even when the connection was lost before the outcome was known.
                return self._restart_result(sessionId, identity, saved)
            # Only a request the system explicitly rejected may be retried, and
            # that retry still has to pass the eligibility check below.
            completion = self._completion(session, receipt, identity)
            if (completion.get('state') != offline_completion.AWAITING_REBOOT or
                    completion.get('restartEligible') is not True):
                raise offline_session.OfflineSessionError(
                    'this transaction is not eligible for restart (%s)' %
                    (completion.get('state'),))
            receipt['restart'] = {
                'schemaVersion': 1,
                'state': 'dispatching',
                'transactionId': identity['transactionId'],
            }
            self.store.update_receipt(session, receipt)

        try:
            completed = self._reboot(
                ['/usr/bin/systemctl', 'reboot'],
                capture_output=True, check=False, timeout=30)
        except (subprocess.SubprocessError, OSError):
            state = 'uncertain'
        else:
            state = ('dispatched'
                     if getattr(completed, 'returncode', None) == 0 else 'failed')

        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            restart = receipt.get('restart')
            if (not isinstance(restart, dict) or
                    restart.get('transactionId') != identity['transactionId'] or
                    restart.get('state') != 'dispatching'):
                raise offline_session.OfflineSessionError(
                    'session restart state changed unexpectedly')
            restart['state'] = state
            self.store.update_receipt(session, receipt)
        if state == 'failed':
            raise offline_session.OfflineSessionError(
                'the restart request was rejected by the system')
        return self._restart_result(sessionId, identity, restart)

    def status(self, sessionId):
        receipt = self.store.status(sessionId)
        if receipt.get('state') not in _BUSY:
            return self._attach_completion(sessionId, receipt)
        operation = receipt.get('jobOperation')
        if operation not in ('prepare', 'execute'):
            raise offline_session.OfflineSessionError('busy session has invalid job operation')
        unit = self._unit(operation, sessionId)
        completed = self._runner(
            ['systemctl', 'is-active', unit],
            capture_output=True, check=False, timeout=10)
        if completed.returncode == 0:
            receipt['jobActive'] = True
            return receipt
        if completed.returncode not in (3, 4):
            raise offline_session.OfflineSessionError(
                'systemctl is-active failed with status %s' % completed.returncode)
        # Re-read after the query: a worker may have committed a terminal result.
        receipt = self.store.status(sessionId)
        receipt['jobActive'] = False
        if receipt.get('state') in _BUSY:
            receipt['jobInterrupted'] = True
            receipt['attentionRequired'] = True
        return self._attach_completion(sessionId, receipt)
