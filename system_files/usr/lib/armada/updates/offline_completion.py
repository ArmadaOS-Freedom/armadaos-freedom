# SPDX-License-Identifier: GPL-3.0-only
"""Bind an executed offline session to durable trial/finalization evidence.

The session receipt only records what the execute worker wrote.  This module
compares it with the live trial journal and boot witness reported by
trial_status, which already joins the durable finalization receipt.  It is
read-only: status and reconnect paths must never write boot media or receipts.

A different or newer transaction is never accepted as this session's success,
and a missing or contradictory piece of evidence degrades to ``unverified`` or
``mismatch`` instead of ``confirmed``.
"""

from __future__ import annotations

import re
import uuid

_DEPLOYMENT_RE = re.compile(
    r'[A-Za-z0-9][A-Za-z0-9_.-]*/[0-9a-f]{64}\.[0-9]+\Z', re.ASCII)
_SLOTS = ('a', 'b')

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

LEGACY_BOOT_CONTEXT = 'legacy-controller'
LEGACY_PRE_REBOOT = 'legacy-pre-reboot'
LEGACY_EVIDENCE_SCHEMA = 1
# Explicit payload shape of the root-only, read-only legacy pre-reboot
# evidence document produced by offline_jobs.  Every key is required; the
# document never carries an actual/inactive slot.
LEGACY_EVIDENCE_KEYS = frozenset({
    'schemaVersion', 'state', 'bootContext', 'bootId', 'deployment',
    'status', 'journal', 'plan', 'writeReport', 'activationReport',
})
_WRITE_EVIDENCE_KEYS = frozenset({
    'candidate', 'sourceBootSha256', 'bytesWritten', 'readbackSha256',
})
_CANDIDATE_KEYS = frozenset({'path', 'bytes', 'sha256'})


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


def _legacy_claim(entry):
    """Validate one write-report candidate claim without trusting its path."""
    if not isinstance(entry, dict) or set(entry) != _CANDIDATE_KEYS:
        return None
    if type(entry.get('bytes')) is not int or entry['bytes'] <= 0:
        return None
    digest = _sha(entry.get('sha256'))
    if digest is None or digest == '0' * 64:
        return None
    return digest


def _legacy_boot_hashes(plan, key):
    value = plan.get(key) if isinstance(plan, dict) else None
    if not isinstance(value, dict) or set(value) != set(_SLOTS):
        return None
    hashes = {}
    for slot in _SLOTS:
        hashes[slot] = _sha(value[slot])
        if hashes[slot] is None or hashes[slot] == '0' * 64:
            return None
    return hashes


def _legacy_write_evidence(write, identity, physical, plan):
    """Return True only for a complete, plan-verified dual write report."""
    if not isinstance(write, dict) or write.get('phase') != 'dual_boots_written':
        return False
    if (write.get('transactionUuid') != identity['transactionId'] or
            write.get('deployment') != identity['newDeployment'] or
            write.get('fromSlot') != identity['fromSlot'] or
            write.get('slotOrder') != [identity['targetSlot'], identity['fromSlot']] or
            write.get('physical') != physical):
        return False
    original = _legacy_boot_hashes(plan, 'originalBootSha256')
    new = _legacy_boot_hashes(plan, 'newBootSha256')
    if original is None or new is None:
        return False
    writes = write.get('writes')
    if not isinstance(writes, dict) or set(writes) != set(_SLOTS):
        return False
    for slot in _SLOTS:
        evidence = writes[slot]
        if not isinstance(evidence, dict) or set(evidence) != _WRITE_EVIDENCE_KEYS:
            return False
        if evidence.get('sourceBootSha256') != original[slot]:
            return False
        candidate = evidence.get('candidate')
        digest = _legacy_claim(candidate)
        if digest is None or digest != new[slot]:
            return False
        if evidence.get('bytesWritten') != candidate['bytes']:
            return False
        if evidence.get('readbackSha256') != digest:
            return False
    return True


def _legacy_activation_evidence(activation, write, identity, plan):
    """Return True only for a complete, plan-verified activation report."""
    if not isinstance(activation, dict):
        return False
    if activation.get('phase') != 'offline_slots_activated':
        return False
    if (activation.get('transactionUuid') != identity['transactionId'] or
            activation.get('deployment') != identity['newDeployment'] or
            activation.get('fromSlot') != identity['fromSlot'] or
            activation.get('targetSlot') != identity['targetSlot'] or
            activation.get('selectedSlot') != identity['targetSlot']):
        return False
    sequence = activation.get('recordSequence')
    if type(sequence) is not int or sequence < 1:
        return False
    new = _legacy_boot_hashes(plan, 'newBootSha256')
    if new is None:
        return False
    writes = write['writes']
    return (
        activation.get('currentReadbackSha256') == new[identity['fromSlot']] and
        activation.get('targetReadbackSha256') == new[identity['targetSlot']] and
        writes[identity['fromSlot']]['readbackSha256'] ==
        new[identity['fromSlot']] and
        writes[identity['targetSlot']]['readbackSha256'] ==
        new[identity['targetSlot']]
    )


def _classify_legacy_pre_reboot(receipt, evidence, identity):
    outcome = _outcome(receipt)
    if outcome in _TERMINAL_FAILURE_STATES:
        return _result(FAILED, identity, reason=outcome)

    if evidence.get('bootContext') != LEGACY_BOOT_CONTEXT:
        return _unverified(identity, 'legacy_evidence_malformed')
    boot_id = _canonical_uuid(evidence.get('bootId'))
    if boot_id is None:
        return _unverified(identity, 'legacy_boot_id_missing')

    status = evidence.get('status')
    if not isinstance(status, dict):
        return _unverified(identity, 'legacy_status_missing')
    if (status.get('transactionId') != identity['transactionId'] or
            status.get('oldDeployment') != identity['oldDeployment'] or
            status.get('newDeployment') != identity['newDeployment'] or
            status.get('planSha256') != identity['planSha256']):
        return _mismatch(
            identity, 'legacy_status_mismatch',
            'the durable execute status does not describe this session')
    if status.get('state') != 'awaiting_reboot':
        return _unverified(identity, 'legacy_status_mismatch')
    recorded_boot = _canonical_uuid(status.get('bootId'))
    if recorded_boot is None:
        return _unverified(identity, 'legacy_boot_id_missing')
    if recorded_boot != boot_id:
        # The activation happened in an earlier boot; only the post-reboot
        # witness/finalization path may judge this transaction now.
        return _unverified(identity, 'boot_changed')

    journal = evidence.get('journal')
    if not isinstance(journal, dict) or journal.get('state') != 'available':
        error = journal.get('error') if isinstance(journal, dict) else None
        return _unverified(identity, 'trial_unavailable', error)
    if (journal.get('bootContext') != LEGACY_BOOT_CONTEXT or
            journal.get('policy') != 'offline-dual' or
            journal.get('actualSlot') is not None or
            journal.get('inactiveSlot') is not None):
        return _unverified(identity, 'legacy_journal_malformed')
    if (journal.get('transactionId') != identity['transactionId'] or
            journal.get('oldDeployment') != identity['oldDeployment'] or
            journal.get('newDeployment') != identity['newDeployment'] or
            journal.get('fromSlot') != identity['fromSlot'] or
            journal.get('targetSlot') != identity['targetSlot']):
        return _mismatch(
            identity, 'transaction_mismatch',
            'the legacy journal does not describe this session')
    if journal.get('planSha256') != identity['planSha256']:
        return _mismatch(
            identity, 'plan_mismatch',
            'the legacy journal plan digest does not match this session')

    record_state = journal.get('recordState')
    if record_state == CONFIRMED:
        # A controller-only boot cannot provide the new witness; never label a
        # transaction confirmed from the pre-reboot journal alone.
        return _unverified(identity, 'legacy_confirmation_requires_witness')
    if record_state != 'prepared':
        # TRIAL/ROLLED_BACK mean a boot attempt was already consumed; only the
        # post-reboot strict path may judge those.
        return _unverified(
            identity, 'legacy_pre_reboot_requires_prepared_journal')

    deployment = evidence.get('deployment')
    running = journal.get('runningDeployment')
    if deployment == identity['newDeployment']:
        if running != 'new':
            return _mismatch(
                identity, 'running_deployment_mismatch',
                'the legacy journal does not place the new deployment')
        evidence_fields = {
            'evidenceKind': LEGACY_PRE_REBOOT,
            'bootContext': LEGACY_BOOT_CONTEXT,
            'actualSlot': None,
            'bootId': boot_id,
            'recordState': record_state,
            'runningDeployment': 'new',
        }
        return _result(AWAITING_CONFIRMATION, identity, **evidence_fields)
    if deployment != identity['oldDeployment']:
        return _unverified(identity, 'boot_evidence_missing')
    if running != 'old':
        return _mismatch(
            identity, 'running_deployment_mismatch',
            'the legacy journal does not place the old deployment')

    plan = evidence.get('plan')
    if not isinstance(plan, dict):
        return _unverified(identity, 'legacy_plan_missing')
    if (plan.get('transactionId') != identity['transactionId'] or
            plan.get('oldDeployment') != identity['oldDeployment'] or
            plan.get('newDeployment') != identity['newDeployment'] or
            plan.get('fromSlot') != identity['fromSlot'] or
            plan.get('targetSlot') != identity['targetSlot']):
        return _mismatch(
            identity, 'legacy_plan_mismatch',
            'the durable plan does not describe this session')
    write = evidence.get('writeReport')
    activation = evidence.get('activationReport')
    if (status.get('bootWriteState') != 'written' or
            status.get('activationOutcome') != 'activated'):
        return _unverified(identity, 'legacy_write_evidence_missing')
    if not _legacy_write_evidence(write, identity, receipt.get('physical'), plan):
        return _unverified(identity, 'legacy_write_evidence_missing')
    if not _legacy_activation_evidence(activation, write, identity, plan):
        return _unverified(identity, 'legacy_activation_evidence_missing')

    return _result(
        AWAITING_REBOOT, identity,
        evidenceKind=LEGACY_PRE_REBOOT,
        bootContext=LEGACY_BOOT_CONTEXT,
        actualSlot=None,
        bootId=boot_id,
        recordState=record_state,
        runningDeployment='old',
    )


def evaluate_legacy_pre_reboot(receipt, evidence, identity=None):
    """Evaluate a legacy session before its reboot without inventing a slot.

    ``evidence`` is the explicit :data:`LEGACY_EVIDENCE_KEYS` document read by
    offline_jobs.  Restart eligibility requires the *same* kernel boot that
    performed the activation, the exact old booted deployment, a durable
    PREPARED journal bound to this transaction, and complete verified write and
    activation reports.  It never returns ``confirmed``: that needs the new
    witness and finalization evidence after the reboot.
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
    outcome = _outcome(receipt)
    if outcome in _TERMINAL_FAILURE_STATES:
        result = _result(FAILED, identity, reason=outcome)
    elif not isinstance(evidence, dict) or evidence.get('state') != 'available':
        error = evidence.get('error') if isinstance(evidence, dict) else None
        result = _unverified(identity, 'legacy_evidence_unavailable', error)
    elif (evidence.get('schemaVersion') != LEGACY_EVIDENCE_SCHEMA or
            frozenset(evidence) != LEGACY_EVIDENCE_KEYS):
        result = _unverified(identity, 'legacy_evidence_malformed')
    else:
        result = _classify_legacy_pre_reboot(receipt, evidence, identity)
    result['restartState'] = _restart_state(receipt, identity['transactionId'])
    return result
