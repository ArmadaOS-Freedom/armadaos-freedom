# SPDX-License-Identifier: GPL-3.0-only
"""Bind an executed offline session to durable trial/finalization evidence.

The session receipt only records what the execute worker wrote.  This module
compares it with the live trial journal and boot witness reported by
trial_status, which already joins the durable finalization receipt.  It is
read-only: status and reconnect paths must never write boot media or receipts.

A different or newer transaction is never accepted as this session's success,
and a missing or contradictory piece of evidence degrades to ``unverified`` or
``mismatch`` instead of ``confirmed``.  ``execute_completion`` also validates
the final execute status and verified boot write report against the approved
plan; both the detached job manager and the system status reader use it, and
its per-transaction completion pointer lets status find a pending result
without scanning sessions.
"""

from __future__ import annotations

from pathlib import Path
import re
import uuid

from .trial_record import BOOT_POLICY

_DEPLOYMENT_RE = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9_.-]*/[0-9a-f]{64}\.[0-9]+\Z', re.ASCII)
_SESSION_ID_RE = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_SLOTS = ('a', 'b')
_CANDIDATE_KEYS = frozenset({'path', 'bytes', 'sha256'})
_WRITE_KEYS = frozenset(
    {'sourceBootSha256', 'candidate', 'bytesWritten', 'readbackSha256'})

# Private pointers that name the one session holding a pending execute result.
# They live beside the session directories but are keyed by the validated
# canonical transaction id, so a later queued session cannot overwrite an
# earlier pending completion merely by being submitted.
POINTER_DIRECTORY = 'pending'
_POINTER_KEYS = frozenset({'schemaVersion', 'transactionId', 'sessionId'})

_TERMINAL_FAILURE_STATES = frozenset({'failed', 'recovery_required'})
_UNCERTAIN_STATES = frozenset({'execution_uncertain'})
_RESTART_STATES = frozenset({'dispatching', 'dispatched', 'uncertain', 'failed'})

UNVERIFIED = 'unverified'
MISMATCH = 'mismatch'
AWAITING_REBOOT = 'awaiting_reboot'
AWAITING_CONFIRMATION = 'awaiting_confirmation'
FINALIZING = 'finalizing'
CONFIRMED = 'confirmed'
ROLLED_BACK = 'rolled_back'
FAILED = 'failed'


def _canonical_uuid(value):
    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value and parsed.int != 0 else None


def _deployment(value):
    return value if type(value) is str and _DEPLOYMENT_RE.fullmatch(value) else None


def _sha(value):
    if type(value) is not str or len(value) != 64:
        return None
    return value if all(ch in '0123456789abcdef' for ch in value) else None


def _source(receipt):
    """Return the document that carries the executed transaction identity."""
    for name in ('result', 'transaction'):
        value = receipt.get(name)
        if isinstance(value, dict):
            return value
    return receipt


def _identity(source, plan_sha256=None):
    identity = {
        'transactionId': _canonical_uuid(source.get('transactionId')),
        'oldDeployment': _deployment(source.get('oldDeployment')),
        'newDeployment': _deployment(source.get('newDeployment')),
        'fromSlot': source.get('fromSlot'),
        'targetSlot': source.get('targetSlot'),
        'planSha256': _sha(plan_sha256 if plan_sha256 is not None
                           else source.get('planSha256')),
    }
    if (identity['transactionId'] is None or identity['oldDeployment'] is None or
            identity['newDeployment'] is None or
            identity['fromSlot'] not in _SLOTS or
            identity['targetSlot'] not in _SLOTS or
            identity['fromSlot'] == identity['targetSlot'] or
            identity['planSha256'] is None):
        return None
    return identity


def session_identity(receipt):
    """Return the executed session identity recorded on the receipt, if any."""
    if not isinstance(receipt, dict):
        return None
    return _identity(_source(receipt))


def document_identity(prepared, plan, plan_sha256=None):
    """Return the executed session identity recorded by transaction documents.

    The final execute status does not repeat the deployment binding, so this
    reads it from the durable ``prepared.json``/``plan.json`` pair while
    checking that the prepared plan digest still matches the plan bytes.
    """
    if not isinstance(prepared, dict) or not isinstance(plan, dict):
        return None
    if plan_sha256 is not None and plan_sha256 != prepared.get('planSha256'):
        return None
    identity = _identity({
        'transactionId': prepared.get('transactionId'),
        'planSha256': prepared.get('planSha256'),
        'oldDeployment': plan.get('oldDeployment'),
        'newDeployment': plan.get('newDeployment'),
        'fromSlot': plan.get('fromSlot'),
        'targetSlot': plan.get('targetSlot'),
    })
    if identity is None or plan.get('transactionId') != identity['transactionId']:
        return None
    return identity


def completion_pointer(root, transaction_id):
    """Return the private pointer path for one canonical transaction id.

    ``None`` means the transaction id is not canonical, so no file name could
    be derived from it safely.
    """
    transaction_id = _canonical_uuid(transaction_id)
    if transaction_id is None:
        return None
    return Path(root) / POINTER_DIRECTORY / (transaction_id + '.json')


def completion_pointer_document(transaction_id, session_id):
    """Return the pointer document binding a transaction to a session.

    ``None`` means either identifier is unusable, so nothing is published.
    """
    transaction_id = _canonical_uuid(transaction_id)
    if (transaction_id is None or type(session_id) is not str or
            _SESSION_ID_RE.fullmatch(session_id) is None):
        return None
    return {
        'schemaVersion': 1,
        'transactionId': transaction_id,
        'sessionId': session_id,
    }


def pointer_session(pointer, transaction_id):
    """Return the session a pointer selects for this transaction, or ``None``.

    The stored key must repeat the canonical transaction id it is filed under;
    a pointer that names another transaction, session or schema is refused
    rather than followed.
    """
    transaction_id = _canonical_uuid(transaction_id)
    if transaction_id is None or not isinstance(pointer, dict):
        return None
    if set(pointer) != _POINTER_KEYS:
        return None
    if type(pointer['schemaVersion']) is not int or pointer['schemaVersion'] != 1:
        return None
    if pointer['transactionId'] != transaction_id:
        return None
    session_id = pointer['sessionId']
    if type(session_id) is not str or _SESSION_ID_RE.fullmatch(session_id) is None:
        return None
    return session_id


def _slot_digests(plan, name):
    """Return the canonical per-slot digest map from an approved plan."""
    value = plan.get(name)
    if not isinstance(value, dict) or set(value) != set(_SLOTS):
        return None
    digests = {}
    for slot in _SLOTS:
        digest = _sha(value[slot])
        if digest is None:
            return None
        digests[slot] = digest
    return digests


def _status_evidence(status, identity, model):
    """Return whether the final status proves the dual boot write completed."""
    if not isinstance(status, dict):
        return False
    return (
        type(status.get('schemaVersion')) is int and
        status['schemaVersion'] == 1 and
        status.get('component') == 'system' and
        status.get('operation') == 'offline-upgrade' and
        status.get('policy') == 'offline-dual' and
        status.get('model') == model and
        status.get('transactionId') == identity['transactionId'] and
        status.get('planSha256') == identity['planSha256'] and
        status.get('fromSlot') == identity['fromSlot'] and
        status.get('targetSlot') == identity['targetSlot'] and
        status.get('oldDeployment') == identity['oldDeployment'] and
        status.get('newDeployment') == identity['newDeployment'] and
        status.get('state') == AWAITING_REBOOT and
        status.get('phase') == AWAITING_REBOOT and
        status.get('requiresReboot') is True and
        status.get('bootWriteState') == 'written' and
        status.get('slotControlState') == 'not_issued' and
        status.get('bootPolicy') == BOOT_POLICY)


def _physical_evidence(value, identity, model):
    """Accept an absent physical report but never a contradictory one."""
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value.get('model') != model:
        return False
    if value.get('actualSlot') != identity['fromSlot']:
        return False
    if value.get('inactiveSlot') != identity['targetSlot']:
        return False
    deployment = value.get('deployment')
    return deployment is None or deployment == identity['oldDeployment']


def _write_evidence(writes, new_digests, old_digests):
    """Return whether every slot carries verified write evidence for its plan."""
    if not isinstance(writes, dict) or set(writes) != set(_SLOTS):
        return False
    for slot in _SLOTS:
        entry = writes[slot]
        if not isinstance(entry, dict) or set(entry) != _WRITE_KEYS:
            return False
        candidate = entry['candidate']
        if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_KEYS:
            return False
        path = candidate['path']
        if type(path) is not str or not path:
            return False
        size = candidate['bytes']
        if type(size) is not int or size <= 0:
            return False
        if type(entry['bytesWritten']) is not int or entry['bytesWritten'] != size:
            return False
        if _sha(candidate['sha256']) != new_digests[slot]:
            return False
        if _sha(entry['readbackSha256']) != new_digests[slot]:
            return False
        if _sha(entry['sourceBootSha256']) != old_digests[slot]:
            return False
    return True


def execute_completion(prepared, plan, plan_sha256, status, report, ack,
                       model=None):
    """Validate durable evidence of a written but unrebooted transaction.

    The approved plan decides which boot images belong in each slot: both
    ``originalBootSha256`` and ``newBootSha256`` must map exactly ``a`` and
    ``b`` to canonical digests.  The write report must have written the
    inactive target slot first and the running slot second, and each slot's
    verified candidate, readback and source digests must equal the plan.  The
    same checks are shared by the detached job manager and the system status
    reader, so neither can accept a report that merely lists slot names.

    Returns the transaction identity on success and ``None`` on any missing,
    malformed or contradictory document.
    """
    identity = document_identity(prepared, plan, plan_sha256)
    if identity is None or not isinstance(plan, dict) or not isinstance(ack, dict):
        return None
    # The recovery acknowledgement binds the approved plan digest.
    if (ack.get('transactionId') != identity['transactionId'] or
            ack.get('planSha256') != identity['planSha256']):
        return None
    plan_model = plan.get('model')
    if type(plan_model) is not str or not plan_model:
        return None
    if model is not None and plan_model != model:
        return None
    new_digests = _slot_digests(plan, 'newBootSha256')
    old_digests = _slot_digests(plan, 'originalBootSha256')
    if new_digests is None or old_digests is None:
        return None
    if not _status_evidence(status, identity, plan_model):
        return None
    if not isinstance(report, dict):
        return None
    if (report.get('phase') != 'dual_boots_written' or
            report.get('model') != plan_model or
            report.get('transactionUuid') != identity['transactionId'] or
            report.get('deployment') != identity['newDeployment'] or
            report.get('fromSlot') != identity['fromSlot'] or
            report.get('slotOrder') != [identity['targetSlot'],
                                        identity['fromSlot']] or
            not _physical_evidence(report.get('physical'), identity, plan_model)):
        return None
    if not _write_evidence(report.get('writes'), new_digests, old_digests):
        return None
    return identity


def _outcome(receipt):
    if not isinstance(receipt, dict):
        return None
    value = _source(receipt).get('state')
    if type(value) is not str:
        value = receipt.get('state')
    return value if type(value) is str else None


def _restart_state(receipt, transaction_id):
    if not isinstance(receipt, dict) or transaction_id is None:
        return None
    restart = receipt.get('restart')
    if (not isinstance(restart, dict) or restart.get('state') not in _RESTART_STATES or
            restart.get('transactionId') != transaction_id):
        return None
    return restart['state']


def _result(state, identity, **extra):
    result = {
        'schemaVersion': 1,
        'state': state,
        'transactionId': identity['transactionId'],
        'deployment': identity['newDeployment'],
        'oldDeployment': identity['oldDeployment'],
        'restartEligible': state == AWAITING_REBOOT,
    }
    result.update(extra)
    return result


def _unverified(identity, reason, error=None):
    extra = {'reason': reason}
    if error:
        extra['error'] = str(error)[:200]
    return _result(UNVERIFIED, identity, **extra)


def _mismatch(identity, reason, error=None):
    extra = {'reason': reason}
    if error:
        extra['error'] = str(error)[:200]
    return _result(MISMATCH, identity, **extra)


def _classify(receipt, trial, identity):
    outcome = _outcome(receipt)
    if outcome in _TERMINAL_FAILURE_STATES:
        return _result(FAILED, identity, reason=outcome)

    if not isinstance(trial, dict) or trial.get('state') != 'available':
        error = trial.get('error') if isinstance(trial, dict) else None
        return _unverified(identity, 'trial_unavailable', error)

    if (trial.get('transactionId') != identity['transactionId'] or
            trial.get('oldDeployment') != identity['oldDeployment'] or
            trial.get('newDeployment') != identity['newDeployment'] or
            trial.get('fromSlot') != identity['fromSlot'] or
            trial.get('targetSlot') != identity['targetSlot']):
        return _mismatch(
            identity, 'transaction_mismatch',
            'the live trial journal does not describe this session')
    if trial.get('planSha256') != identity['planSha256']:
        return _mismatch(
            identity, 'plan_mismatch',
            'the live trial plan digest does not match this session')

    record_state = trial.get('recordState')
    running = trial.get('runningDeployment')
    boot_confirmed = trial.get('bootConfirmed') is True
    evidence = {
        'recordState': record_state,
        'runningDeployment': running,
        'bootConfirmed': boot_confirmed,
    }

    if record_state == CONFIRMED:
        if running is None:
            # The journal is confirmed but the boot witness could not place the
            # running deployment; that is missing evidence, not a conflict.
            return _unverified(
                identity, 'boot_evidence_missing',
                trial.get('bootEvidenceError'))
        if running != 'new':
            return _mismatch(
                identity, 'running_deployment_mismatch',
                'the confirmed trial is not running the new deployment')
        if not boot_confirmed:
            return _unverified(
                identity, 'boot_evidence_missing',
                trial.get('bootEvidenceError'))
        finalization = trial.get('finalization')
        if isinstance(finalization, dict) and finalization.get('state') == 'complete':
            return _result(CONFIRMED, identity, **evidence)
        error = (finalization.get('error')
                 if isinstance(finalization, dict) else None)
        return _result(
            FINALIZING, identity, **evidence,
            error=(error if isinstance(error, str) and error
                   else 'finalization receipt is not complete'))

    if record_state == ROLLED_BACK:
        if running == 'old':
            return _result(ROLLED_BACK, identity, **evidence)
        if running is None:
            return _unverified(
                identity, 'boot_evidence_missing',
                trial.get('bootEvidenceError'))
        return _mismatch(
            identity, 'running_deployment_mismatch',
            'the rolled-back trial is not running the old deployment')

    if record_state in ('prepared', 'trial'):
        if running == 'new':
            return _result(AWAITING_CONFIRMATION, identity, **evidence)
        if running == 'old' and record_state == 'prepared':
            result = _result(AWAITING_REBOOT, identity, **evidence)
            if outcome in _UNCERTAIN_STATES:
                # The worker never confirmed the activation, so a restart is
                # never offered automatically for this session.
                result['restartEligible'] = False
                result['reason'] = 'execution_uncertain'
            return result
        return _unverified(
            identity, 'boot_evidence_missing',
            trial.get('bootEvidenceError'))

    return _unverified(identity, 'unsupported_record_state')


def evaluate(receipt, trial, identity=None):
    """Classify a session result using actual trial and boot evidence.

    ``trial`` is the read-only dictionary returned by
    trial_status.read_trial_status.  ``identity`` comes from the session
    transaction documents when the receipt does not repeat the binding.
    """
    identity = identity if identity is not None else session_identity(receipt)
    if identity is None:
        return {
            'schemaVersion': 1,
            'state': UNVERIFIED,
            'transactionId': None,
            'reason': 'session_identity_missing',
            'restartEligible': False,
            'restartState': None,
        }
    result = _classify(receipt, trial, identity)
    result['restartState'] = _restart_state(receipt, identity['transactionId'])
    return result
