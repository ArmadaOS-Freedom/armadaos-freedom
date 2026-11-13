# SPDX-License-Identifier: GPL-3.0-only
"""Detached systemd jobs for private offline-update sessions."""

from __future__ import annotations

import hashlib
import os
import subprocess

import armada_os
from . import (
    boot_context,
    offline_completion,
    offline_execute,
    offline_session,
    physical_slots,
    system_transaction,
    trial_confirmation,
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
# Receipt states in which a detached execute may have run to completion but
# died before recording its result on the receipt.
_UNRECORDED_EXECUTE_STATES = frozenset({
    'execute_queued', 'executing', 'dispatch_uncertain',
})
# Receipt states in which a detached prepare may have written the terminal
# transaction documents but died before recording its result on the receipt.
_UNRECORDED_PREPARE_STATES = frozenset({
    'prepare_queued', 'preparing', 'dispatch_uncertain',
})
# The only prepare result that leaves a durable, non-booting transaction the
# client may still resume; ``up_to_date`` leaves no durable output at all.
_PREPARED_STATE = 'awaiting_recovery_export'
# Documents only an execute worker writes.  Their presence means the prepared
# transaction has already moved on and must never be adopted as freshly
# prepared.
_EXECUTE_ARTIFACTS = ('recovery-ack.json', 'write-report.json')


def _is_sha256(value):
    return (type(value) is str and len(value) == 64 and
            all(ch in '0123456789abcdef' for ch in value))


def _read_trial_evidence():
    """Read the live trial/boot evidence without changing anything."""
    try:
        snapshot = armada_os.snapshot()
        booted = snapshot.get('booted') if isinstance(snapshot, dict) else None
        return trial_status.read_trial_status(boot_context.read_boot_context(booted))
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        return {'state': 'unavailable',
                'error': str(error) or type(error).__name__}


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
        if not physical_slots.same_physical_identity(
                self.store._physical_info(), receipt.get('physical')):
            raise offline_session.OfflineSessionError(
                'physical identity changed since session submission')
        channel = system_transaction._snapshot_channel(armada_os.snapshot())
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

    def _publish_completion_pointer(self, transaction_id, session_id):
        """Publish the private pointer for this transaction's pending result.

        The pointer is keyed by the validated canonical transaction id instead
        of a single shared name, so a second session submitted while an earlier
        execute is still pending cannot replace the pointer of the older one.
        It is written atomically before the receipt is marked execute_queued:
        read-only status follows the pointer for the live trial's transaction
        and re-verifies the session's own documents before reporting anything.
        """
        document = offline_completion.completion_pointer_document(
            transaction_id, session_id)
        pointer = offline_completion.completion_pointer(
            self.store.base, transaction_id)
        if document is None or pointer is None:
            raise offline_session.OfflineSessionError(
                'transaction identity is not a canonical transaction id')
        offline_session._check_dir(self.store.base)
        created = False
        try:
            os.mkdir(pointer.parent, 0o700)
            created = True
        except FileExistsError:
            pass
        offline_session._check_dir(pointer.parent)
        if created:
            offline_session._fsync_dir(self.store.base)
        system_transaction._save_json(pointer.parent, pointer.name, document)

    def _job_active(self, operation, session_id):
        """Return whether the detached unit for this operation still runs."""
        completed = self._runner(
            ['systemctl', 'is-active', self._unit(operation, session_id)],
            capture_output=True, check=False, timeout=10)
        if completed.returncode == 0:
            return True
        if completed.returncode not in (3, 4):
            raise offline_session.OfflineSessionError(
                'systemctl is-active failed with status %s' % completed.returncode)
        return False

    def _job_owned(self, operation, session_id):
        """Return whether the transient unit of the recorded name exists.

        ``systemd-run --unit=`` is the only thing that creates the unit and
        ``--collect`` drops it again once it exits, so ``LoadState`` is the
        unit ownership marker of the submission: only ``not-found`` is
        positive proof that the queued launch never happened.  A failed or
        unrecognized answer is reported as ``None`` (unknown) so callers
        never restart work they cannot prove never started.
        """
        try:
            completed = self._runner(
                ['systemctl', 'show', '--property=LoadState',
                 self._unit(operation, session_id)],
                capture_output=True, check=False, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None
        if getattr(completed, 'returncode', None) != 0:
            return None
        stdout = getattr(completed, 'stdout', b'') or b''
        if isinstance(stdout, bytes):
            stdout = stdout.decode('ascii', 'replace')
        for line in stdout.splitlines():
            if line.startswith('LoadState='):
                value = line.split('=', 1)[1].strip()
                if not value:
                    return None
                return value != 'not-found'
        return None

    @staticmethod
    def _durable_completion(session, receipt):
        """Return this session's durable execute result, or None.

        Only a private session whose own approved plan, verified write report
        and final transaction status agree with the live trial journal is
        accepted.  Missing or contradictory evidence is refused rather than
        repaired, so an interrupted worker can never be turned into a success
        claim it did not earn.
        """
        transaction = session / 'transaction'
        if transaction.is_symlink() or not transaction.is_dir():
            return None
        try:
            prepared, _raw = offline_execute._load_json(
                transaction, 'prepared.json')
            plan, plan_raw = offline_execute._load_json(transaction, 'plan.json')
            status, _raw = offline_execute._load_json(transaction, 'status.json')
            report, _raw = offline_execute._load_json(
                transaction, 'write-report.json')
            ack, _raw = offline_execute._load_json(session, 'ack.json')
        except (OSError, ValueError, RuntimeError):
            return None
        identity = offline_completion.execute_completion(
            prepared, plan, hashlib.sha256(plan_raw).hexdigest(),
            status, report, ack, receipt.get('model'))
        if identity is None:
            return None
        verdict = offline_completion.evaluate(
            {'state': 'awaiting_reboot', 'result': status},
            _read_trial_evidence(), identity)
        if (verdict.get('state') != offline_completion.AWAITING_REBOOT or
                verdict.get('restartEligible') is not True):
            return None
        return status

    @staticmethod
    def _durable_prepare(session, receipt):
        """Return this session's durable prepared status, or None.

        A prepare worker writes ``status.json`` last, after every other
        transaction document, so only a status that is still awaiting the
        recovery export proves the prepare step completed.  Earlier phases,
        missing, partial or contradictory documents, foreign identities and
        any later execute output are refused rather than repaired: nothing is
        fabricated and no deployment step is repeated.

        The binding is re-derived from the private documents instead of being
        trusted from the receipt, and is matched against the signed session
        document, the approved plan, the recovery manifest and the physical
        identity recorded when the session was submitted.
        """
        transaction = session / 'transaction'
        if transaction.is_symlink() or not transaction.is_dir():
            return None
        for name in _EXECUTE_ARTIFACTS:
            path = transaction / name
            if path.exists() or path.is_symlink():
                return None
        try:
            status, _status_raw = offline_execute._load_json(
                transaction, 'status.json')
            prepared, _prepared_raw = offline_execute._load_json(
                transaction, 'prepared.json')
            plan, plan_raw = offline_execute._load_json(transaction, 'plan.json')
            manifest, manifest_raw = offline_execute._load_json(
                transaction, 'recovery-manifest.json')
            package_document = offline_execute._read_bounded(
                transaction / 'package.json', _DOCUMENT_MAX,
                'transaction package document')
            package_signature = offline_execute._read_bounded(
                transaction / 'package.sig', _SIGNATURE_MAX,
                'transaction package signature')
            session_document = offline_execute._read_bounded(
                session / 'package.json', _DOCUMENT_MAX,
                'session package document')
            session_signature = offline_execute._read_bounded(
                session / 'package.sig', _SIGNATURE_MAX,
                'session package signature')
        except (OSError, ValueError, RuntimeError):
            return None

        if (type(status.get('schemaVersion')) is not int or
                status.get('schemaVersion') != 1 or
                status.get('component') != 'system' or
                status.get('operation') != 'offline-upgrade' or
                status.get('policy') != 'offline-dual' or
                status.get('bootPolicy') != system_transaction.BOOT_POLICY or
                status.get('phase') != _PREPARED_STATE or
                status.get('state') != _PREPARED_STATE or
                status.get('bootWriteState') != 'not_started' or
                status.get('slotControlState') != 'not_issued' or
                status.get('requiresReboot') is not False):
            return None
        if (type(plan.get('schemaVersion')) is not int or
                plan.get('schemaVersion') != 1 or
                plan.get('operation') != 'offline-upgrade' or
                plan.get('policy') != 'offline-dual' or
                type(manifest.get('schemaVersion')) is not int or
                manifest.get('schemaVersion') != 1 or
                manifest.get('operation') != 'offline-upgrade'):
            return None

        identity = offline_completion.document_identity(
            prepared, plan, hashlib.sha256(plan_raw).hexdigest())
        if identity is None:
            return None
        physical = receipt.get('physical')
        if (not isinstance(physical, dict) or
                plan.get('model') != receipt.get('model') or
                plan.get('channel') != receipt.get('channel') or
                plan.get('oldDeployment') != physical.get('deployment') or
                plan.get('fromSlot') != physical.get('actualSlot') or
                plan.get('targetSlot') != physical.get('inactiveSlot')):
            return None
        if (status.get('transactionId') != identity['transactionId'] or
                status.get('model') != plan.get('model') or
                status.get('fromSlot') != identity['fromSlot'] or
                status.get('targetSlot') != identity['targetSlot'] or
                status.get('oldDeployment') != identity['oldDeployment'] or
                status.get('newDeployment') != identity['newDeployment']):
            return None

        manifest_sha = prepared.get('recoveryManifestSha256')
        package_sha = prepared.get('packageDescriptorSha256')
        if (not _is_sha256(manifest_sha) or
                hashlib.sha256(manifest_raw).hexdigest() != manifest_sha or
                plan.get('recoveryManifestSha256') != manifest_sha or
                status.get('recoveryManifestSha256') != manifest_sha or
                status.get('planSha256') != identity['planSha256'] or
                type(status.get('recoveryManifestBytes')) is not int or
                status['recoveryManifestBytes'] != len(manifest_raw)):
            return None
        if (not _is_sha256(package_sha) or
                hashlib.sha256(session_document).hexdigest() != package_sha or
                plan.get('packageDescriptorSha256') != package_sha or
                package_document != session_document or
                package_signature != session_signature):
            return None

        if (manifest.get('transactionId') != identity['transactionId'] or
                manifest.get('model') != plan.get('model') or
                manifest.get('oldDeployment') != identity['oldDeployment'] or
                manifest.get('fromSlot') != identity['fromSlot'] or
                manifest.get('recoveryPartUuid') != physical.get('recoveryPartUuid') or
                not physical_slots.same_physical_identity(
                    manifest.get('physical'), physical) or
                offline_completion._canonical_uuid(
                    manifest.get('installationUuid')) is None or
                type(manifest.get('board')) is not int):
            return None
        return status

    def _adopt_interrupted(self, session, receipt):
        """Adopt a durable terminal result that its dead worker never recorded.

        The receipt object is updated in place and True is returned when new
        durable evidence was found; the caller owns persistence so the same
        decision is reusable under an already-held session lock.  Nothing is
        fabricated: partial, foreign or contradictory documents are refused
        rather than repaired, and no worker is ever relaunched here.
        """
        state = receipt.get('state')
        operation = receipt.get('jobOperation')
        if operation == 'prepare' and state in _UNRECORDED_PREPARE_STATES:
            result = self._durable_prepare(session, receipt)
            final = 'prepared'
        elif operation == 'execute' and state in _UNRECORDED_EXECUTE_STATES:
            result = self._durable_completion(session, receipt)
            final = 'awaiting_reboot'
        else:
            return False
        if result is None:
            return False
        receipt['state'] = final
        receipt['result'] = result
        receipt.pop('error', None)
        return True

    def _reconcile_interrupted(self, session_id):
        """Persist one adopted durable result; never launch a worker."""
        with self.store.session_lock(session_id) as session:
            receipt = self.store.read_receipt(session)
            if not self._adopt_interrupted(session, receipt):
                return False
            try:
                self.store.update_receipt(session, receipt)
            except OSError:
                return False
            return True

    def _never_started(self, session, operation, session_id):
        """Return True only with positive proof that the queued job never ran.

        A worker's first action is the atomic claim of its queued receipt under
        the session lock (``prepare_queued`` -> ``preparing`` or
        ``execute_queued`` -> ``executing``), so a receipt that is still queued
        has not been claimed.  The transient systemd unit named by the
        submission is the second marker: while it is loaded the worker may not
        have reached that claim yet.  Partial output of either worker, or an
        unreadable systemd answer, is treated as started and never restarted.
        """
        transaction = session / 'transaction'
        if operation == 'prepare':
            # offline_prepare creates the transaction work directory as its
            # first step, so its absence proves that no prepare ran.
            if transaction.exists() or transaction.is_symlink():
                return False
        else:
            if transaction.is_symlink() or not transaction.is_dir():
                return False
            # An execute worker writes these before it reports anything.
            for name in _EXECUTE_ARTIFACTS:
                path = transaction / name
                if path.exists() or path.is_symlink():
                    return False
            try:
                status, _raw = offline_execute._load_json(
                    transaction, 'status.json')
            except (OSError, ValueError, RuntimeError):
                return False
            if status.get('state') != _PREPARED_STATE:
                return False
        return self._job_owned(operation, session_id) is False

    def _resume_queued(self, session, receipt, operation, session_id):
        """Adopt a durable result, or allow one relaunch of a queued job.

        Returns True only when a queued submission may be dispatched again.
        A worker that really ran but died before recording its result leaves
        terminal documents (adopted instead), partial output (never
        restarted), or a loaded unit (unknown; never restarted).
        """
        if self._adopt_interrupted(session, receipt):
            self.store.update_receipt(session, receipt)
            return False
        return self._never_started(session, operation, session_id)

    def _launch_job(self, session, operation, session_id, queued):
        """Dispatch a queued job, keeping an already-owned unit a success.

        A parallel client can create the same transient unit between this
        request's ownership check and its ``systemd-run``.  That collision is
        idempotent: the named unit already owns the work and the worker will
        claim the queued receipt, so it must not be recorded as a failed
        dispatch.  Only a launch with no owned unit becomes uncertain.
        """
        try:
            self._launch(operation, session_id)
        except (subprocess.SubprocessError, OSError) as exc:
            current = self.store.read_receipt(session)
            if (current.get('state') != queued or
                    current.get('jobOperation') != operation):
                return
            if self._job_owned(operation, session_id) is True:
                return
            self.store.update_receipt(
                session, self._dispatch_failed(session, current, exc))

    def submit_prepare(self, sessionId):
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            state = receipt.get('state')
            if state == 'uploaded':
                self._recheck_submission_identity(receipt)
                receipt.update(
                    state='prepare_queued', jobOperation='prepare',
                    jobUnit=self._unit('prepare', sessionId))
                receipt.pop('error', None)
                self.store.update_receipt(session, receipt)
                self._launch_job(session, 'prepare', sessionId, 'prepare_queued')
            elif state == 'prepare_queued':
                if receipt.get('jobOperation') != 'prepare':
                    raise offline_session.OfflineSessionError(
                        'session receipt has an unrecognized state')
                if self._resume_queued(session, receipt, 'prepare', sessionId):
                    self._launch_job(
                        session, 'prepare', sessionId, 'prepare_queued')
            elif state == 'uploading':
                raise offline_session.OfflineSessionError(
                    'session payload has not been sealed')
            elif (state not in _IDEMPOTENT or
                    receipt.get('jobOperation') not in ('prepare', 'execute')):
                raise offline_session.OfflineSessionError(
                    'session receipt has an unrecognized state')
        return self.store.status(sessionId)

    def submit_execute(self, sessionId, recovery_ack):
        with self.store.session_lock(sessionId) as session:
            receipt = self.store.read_receipt(session)
            state = receipt.get('state')
            if state == 'prepared':
                prepared_status, _raw = offline_execute._load_json(
                    session / 'transaction', 'status.json')
                if prepared_status.get('state') != _PREPARED_STATE:
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
                self._publish_completion_pointer(
                    prepared['transactionId'], sessionId)
                receipt.update(
                    state='execute_queued', jobOperation='execute',
                    jobUnit=self._unit('execute', sessionId))
                receipt.pop('error', None)
                self.store.update_receipt(session, receipt)
                self._launch_job(session, 'execute', sessionId, 'execute_queued')
            else:
                if receipt.get('jobOperation') != 'execute':
                    raise offline_session.OfflineSessionError(
                        'session is not prepared for execution')
                # The acknowledgement saved at submission is the only one an
                # execute worker may use; a reconnect repeats it verbatim.
                saved, _raw = offline_execute._load_json(session, 'ack.json')
                if recovery_ack != saved:
                    raise ValueError('recovery acknowledgement differs from saved acknowledgement')
                if state == 'execute_queued':
                    if self._resume_queued(
                            session, receipt, 'execute', sessionId):
                        self._launch_job(
                            session, 'execute', sessionId, 'execute_queued')
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

    def _restore_contended_submission(self, session_id, operation, busy, queued):
        """Undo a worker claim that lost the shared update lock.

        Prepare and execute take the update lock as their first action, so this
        specific error proves the transaction was not touched.  The receipt
        goes back to the exact queued state a new explicit submission requires;
        nothing is dispatched, recorded as failed, or relaunched here.
        """
        with self.store.session_lock(session_id) as session:
            receipt = self.store.read_receipt(session)
            if (receipt.get('state') != busy or
                    receipt.get('jobOperation') != operation):
                raise offline_session.OfflineSessionError(
                    'worker session state changed unexpectedly')
            receipt['state'] = queued
            receipt.pop('error', None)
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
        except trial_confirmation.UpdateLockBusyError:
            # Contention only.  Any other error may follow a mutation and must
            # stay terminal, so it is handled below and never replayed.
            self._restore_contended_submission(sessionId, operation, busy, queued)
            return self.store.status(sessionId)
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

    def _attach_completion(self, session_id, receipt):
        """Attach the read-only completion verdict for an executed session."""
        if receipt.get('state') not in _COMPLETION_STATES:
            return receipt
        try:
            identity = self._session_identity(self.store.resolve_session(session_id))
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
        receipt['completion'] = offline_completion.evaluate(
            receipt, _read_trial_evidence(), identity)
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
        current = self.store.status(sessionId)
        if current.get('state') in _UNRECORDED_EXECUTE_STATES:
            # A dead execute worker may have completed the transaction without
            # recording it.  Adopt that durable result once and continue with
            # the normal checks; never while the worker still runs.
            if (current.get('jobOperation') != 'execute' or
                    self._job_active('execute', sessionId) or
                    not self._reconcile_interrupted(sessionId)):
                raise offline_session.OfflineSessionError(
                    'session is not awaiting a restart')
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
            completion = offline_completion.evaluate(
                receipt, _read_trial_evidence(), identity)
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
        if self._job_active(operation, sessionId):
            receipt['jobActive'] = True
            return receipt
        # Re-read after the query: a worker may have committed a terminal result.
        receipt = self.store.status(sessionId)
        receipt['jobActive'] = False
        if receipt.get('state') in _BUSY:
            if self._reconcile_interrupted(sessionId):
                # The durable completion is read back from the receipt instead
                # of being fabricated in this response.
                receipt = self.store.status(sessionId)
                receipt['jobActive'] = False
            else:
                receipt['jobInterrupted'] = True
                receipt['attentionRequired'] = True
        return self._attach_completion(sessionId, receipt)
