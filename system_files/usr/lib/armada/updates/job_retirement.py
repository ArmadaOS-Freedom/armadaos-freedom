# SPDX-License-Identifier: GPL-3.0-only
"""Bounded retirement of large system-apply artifacts after finalization.

A successful apply or rollback job leaves its directory under the apply state
root holding the downloaded image, captured boot trust, prepared boot
materials and mirrored boot candidates -- several gigabytes that no status,
receipt or recovery path reads once the trial journal is CONFIRMED and its
finalization receipt is durable.  This module removes exactly those known
subtrees and nothing else.

The job is located without scanning any directory: the private ``current.json``
pointer names one job id, and that job's own ``status.json`` must repeat the
finalized transaction id, the approved plan digest and a completed mirrored
boot write.  The job directory, the pointer and the compact ``status.json``,
``plan.json``, ``write-report.json`` and candidate claim documents stay in
place so status, receipts and recovery keep resolving.

Artifacts are only unlinked when the state root, the job directory and the
artifact directory are canonical root-owned directories that nobody else may
modify.  Symlinks are never followed: a symlinked job or artifact is left
alone, and links found inside a proven artifact tree are unlinked without
touching their targets.  Repeating the call after a successful retirement is a
no-op, so a retried confirmation stays safe.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from . import boot_context
from .trial_record import BOOT_POLICY

APPLY_STATE_ROOT = Path('/var/lib/armada/updates/system')
POINTER_NAME = 'current.json'
STATUS_NAME = 'status.json'
ARTIFACT_NAMES = (
    'candidate-a', 'candidate-b', 'download', 'materials', 'trust')

_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_TREE_DEPTH = 8


class JobRetirementError(RuntimeError):
    """Internal guard: an artifact tree exceeds the bounded depth."""


def _canonical_uuid(value):
    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value and parsed.int != 0 else None


def _sha256(value):
    if type(value) is not str or len(value) != 64:
        return False
    return all(ch in '0123456789abcdef' for ch in value)


def _owned_by_root(info):
    return info.st_uid == 0


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: %s' % key)
        result[key] = value
    return result


def _document(path):
    raw = boot_context._read_regular(path, _MAX_DOCUMENT_BYTES)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicate_keys)


def _trusted_root(root):
    """Return whether *root* is the canonical, private apply state root."""
    try:
        if not root.is_absolute() or root != root.resolve(strict=True):
            return False
        info = os.lstat(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and _owned_by_root(info)
        and not info.st_mode & 0o022
    )


def _pointer_job(root):
    """Return the job id named by the private pointer, or ``None``."""
    try:
        pointer = _document(root / POINTER_NAME)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(pointer, dict) or set(pointer) != {'schemaVersion', 'jobId'}:
        return None
    if type(pointer['schemaVersion']) is not int or pointer['schemaVersion'] != 1:
        return None
    return _canonical_uuid(pointer['jobId'])


def _status_is_finalized(status, job, transaction_id, plan_sha256):
    """Require the job's own receipt to be this transaction's finished write."""
    return (
        isinstance(status, dict)
        and type(status.get('schemaVersion')) is int
        and status['schemaVersion'] == 1
        and status.get('component') == 'system'
        and status.get('workDir') == str(job)
        and status.get('transactionId') == transaction_id
        and status.get('planSha256') == plan_sha256
        and status.get('state') == 'awaiting_reboot'
        and status.get('phase') == 'awaiting_reboot'
        and status.get('requiresReboot') is True
        and status.get('bootWriteState') == 'written'
        and status.get('slotControlState') == 'not_issued'
        and status.get('bootPolicy') == BOOT_POLICY
    )


def _job_directory(transaction_id, plan_sha256, state_root):
    """Return the pointer-named job only if it provably owns the transaction."""
    transaction_id = _canonical_uuid(transaction_id)
    if transaction_id is None or not _sha256(plan_sha256):
        return None
    if state_root is None:
        root = APPLY_STATE_ROOT
    elif isinstance(state_root, Path):
        root = state_root
    else:
        return None
    if not _trusted_root(root):
        return None
    job_id = _pointer_job(root)
    if job_id is None:
        return None
    job = root / job_id
    if job.parent != root:
        return None
    try:
        if job.resolve(strict=True) != job:
            return None
        info = os.lstat(job)
    except (OSError, RuntimeError):
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or not _owned_by_root(info)
        or info.st_mode & 0o022
    ):
        return None
    try:
        status = _document(job / STATUS_NAME)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not _status_is_finalized(status, job, transaction_id, plan_sha256):
        return None
    return job


def _remove_tree(path, depth=0):
    """Unlink one proven artifact tree without following any symlink."""
    if depth > _MAX_TREE_DEPTH:
        raise JobRetirementError('artifact tree exceeds the bounded depth')
    with os.scandir(os.fspath(path)) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                _remove_tree(Path(entry.path), depth + 1)
            else:
                os.unlink(entry.path)
    os.rmdir(os.fspath(path))


def retire_finalized_job(transaction_id, *, plan_sha256, state_root=None):
    """Remove the large artifacts of the job that wrote *transaction_id*.

    Returns ``True`` when at least one artifact tree was removed.  ``False``
    covers every unproven case -- unknown pointer, foreign transaction, active
    or failed job, symlinked path, unreadable document -- and leaves the job
    untouched.  Individual unlink failures are swallowed so a finalization
    that is already durable is never reported as incomplete.
    """
    job = _job_directory(transaction_id, plan_sha256, state_root)
    if job is None:
        return False
    retired = False
    for name in ARTIFACT_NAMES:
        path = job / name
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or not _owned_by_root(info)
            or info.st_mode & 0o022
        ):
            continue
        try:
            _remove_tree(path)
        except (OSError, JobRetirementError):
            continue
        retired = True
    return retired
