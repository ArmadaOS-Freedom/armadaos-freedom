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
