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

A confirmed offline session keeps the same problem one level deeper: the
uploaded signed payload under its private ``incoming`` directory plus a fixed
set of bulky build files below ``transaction/materials`` and
``transaction/candidate-a``/``candidate-b`` stay on disk after the trial
journal is CONFIRMED and the finalization receipt is durable, while every
status, receipt and recovery export path only reads the compact documents
beside them.  ``retire_confirmed_offline_session`` follows the transaction's
own pending pointer -- never a directory scan -- and re-proves the receipt,
plan, prepared documents, write report and recovery manifest against the
confirmed record before unlinking exactly those named regular files.  A
receipt that still says ``executing`` is accepted only because the execute
worker saves ``status.json`` after the write report; a power cut before the
receipt update leaves that state behind while every document below it is
already terminal and matches the confirmed journal.  No tree is walked or
removed: directories, logs, candidate and material reports, acknowledgements,
receipts and every recovery export stay in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from . import boot_context
from .trial_record import BOOT_POLICY, CONFIRMED, OFFLINE_DUAL

APPLY_STATE_ROOT = Path('/var/lib/armada/updates/system')
POINTER_NAME = 'current.json'
STATUS_NAME = 'status.json'
ARTIFACT_NAMES = (
    'candidate-a', 'candidate-b', 'download', 'materials', 'trust')

_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_TREE_DEPTH = 8

# Private offline-session layout: the pending pointer names one session, and
# only the fixed bulky regular files of that session are dead once confirmed.
# Every other entry, including every directory, is retained.
_OFFLINE_POINTER_MAX = 8 * 1024
_OFFLINE_SLOTS = ('a', 'b')
# An execute worker writes the terminal status and write report before it
# records its result on the receipt.  ``awaiting_reboot`` is the normal
# recorded outcome; ``executing`` is that same session after a power cut
# between the status save and the receipt update.  Both are still only proof
# together with the complete documents checked by ``_offline_proof``.
_OFFLINE_EXECUTED_STATES = frozenset({'awaiting_reboot', 'executing'})
_OFFLINE_MATERIAL_FILES = (
    'kernel.img', 'initramfs.img', 'runtime.bin', 'source.dtb',
    'dtb-normal.img', 'dtb-diagnostic.img')
_OFFLINE_CANDIDATE_FILES = (
    'inspect-slot/base.bin', 'build-raw/PackedKernel',
    'repack-envelope/candidate.img')


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


def _record_bounds(record):
    """Return the confirmed identity the live journal record proves, or ``None``."""
    try:
        if (type(record.transaction_uuid) is not bytes or
                type(record.plan_sha256) is not bytes or
                type(record.installation_uuid) is not bytes or
                type(record.partition_uuid) is not bytes or
                getattr(record, 'state', None) != CONFIRMED or
                getattr(record, 'policy', None) != OFFLINE_DUAL or
                record.from_slot not in (0, 1) or
                record.target_slot not in (0, 1) or
                record.from_slot == record.target_slot):
            return None
        return {
            'transactionId': str(uuid.UUID(bytes=record.transaction_uuid)),
            'planSha256': record.plan_sha256.hex(),
            'oldDeployment': '%s/%s.%d' % (
                record.os_name, record.old_commit.hex(), record.old_serial),
            'newDeployment': '%s/%s.%d' % (
                record.os_name, record.new_commit.hex(), record.new_serial),
            'fromSlot': 'ab'[record.from_slot],
            'targetSlot': 'ab'[record.target_slot],
            'installationUuid': str(uuid.UUID(bytes=record.installation_uuid)),
            'recoveryPartUuid': str(uuid.UUID(bytes=record.partition_uuid)),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def _manifest_paths(manifest):
    """Return the validated recovery-export paths, or ``None``."""
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        return None
    paths = []
    for entry in files:
        path = entry.get('path') if isinstance(entry, dict) else None
        if (type(path) is not str or not path or path.startswith('/') or
                '\\' in path or ':' in path or
                any(part in ('', '.', '..') for part in path.split('/')) or
                path in paths):
            return None
        paths.append(path)
    return set(paths)


def _offline_proof(session, receipt, bounds):
    """Return the exact files that may be unlinked once the session is proven.

    ``offline_completion.execute_completion`` already validates the prepared,
    plan, acknowledgement, final status and verified write report chain.  This
    only adds the live-record binding, the manifest digest bound to that plan,
    the recovery identity and the recovery-export exclusion.  The receipt must
    name this execute operation and either its recorded terminal state or the
    ``executing`` state a power cut leaves before the receipt is updated; the
    documents alone decide whether that state is complete and matches the
    confirmed record.  The materials and candidate reports are not parsed:
    their bulky file names are fixed.
    """
    from . import offline_completion, offline_execute, offline_session

    transaction = session / 'transaction'
    try:
        offline_session._check_dir(transaction)
        if (receipt.get('state') not in _OFFLINE_EXECUTED_STATES or
                receipt.get('jobOperation') != 'execute'):
            return None
        model = receipt.get('model')
        if type(model) is not str or not model:
            return None
        prepared, _ = offline_execute._load_json(transaction, 'prepared.json')
        plan, plan_raw = offline_execute._load_json(transaction, 'plan.json')
        status, _ = offline_execute._load_json(transaction, 'status.json')
        report, _ = offline_execute._load_json(transaction, 'write-report.json')
        manifest, manifest_raw = offline_execute._load_json(
            transaction, 'recovery-manifest.json')
        ack, _ = offline_execute._load_json(session, 'ack.json')
    except (OSError, ValueError, RuntimeError):
        return None

    identity = offline_completion.execute_completion(
        prepared, plan, hashlib.sha256(plan_raw).hexdigest(),
        status, report, ack, model)
    if identity is None:
        return None
    for key in ('transactionId', 'planSha256', 'oldDeployment', 'newDeployment',
                'fromSlot', 'targetSlot'):
        if identity.get(key) != bounds[key]:
            return None

    manifest_sha256 = prepared.get('recoveryManifestSha256')
    if (not _sha256(manifest_sha256) or
            hashlib.sha256(manifest_raw).hexdigest() != manifest_sha256 or
            plan.get('recoveryManifestSha256') != manifest_sha256):
        return None
    if (manifest.get('transactionId') != bounds['transactionId'] or
            manifest.get('model') != model or
            manifest.get('oldDeployment') != bounds['oldDeployment'] or
            manifest.get('fromSlot') != bounds['fromSlot'] or
            manifest.get('installationUuid') != bounds['installationUuid'] or
            manifest.get('recoveryPartUuid') != bounds['recoveryPartUuid']):
        return None
    members = _manifest_paths(manifest)
    if members is None:
        return None

    targets = []
    for name in _OFFLINE_MATERIAL_FILES:
        relative = 'materials/' + name
        if relative not in members:
            targets.append(('transaction/' + relative, None))
    for slot in _OFFLINE_SLOTS:
        for name in _OFFLINE_CANDIDATE_FILES:
            relative = 'candidate-%s/%s' % (slot, name)
            if relative not in members:
                targets.append(('transaction/' + relative, None))
    return targets


def _payload_target(store, session, receipt):
    """Return the descriptor-bound payload's path and size below *session*."""
    try:
        signed = store._signed_payload(session)
        if signed is None:
            return None
        path, size, digest = signed
        payload = receipt.get('payload')
        if (not isinstance(payload, dict) or payload.get('path') != path or
                payload.get('bytes') != size or payload.get('sha256') != digest):
            return None
        source = store._payload_path(session, receipt)
        relative = source.relative_to(session)
    except (OSError, ValueError, RuntimeError):
        return None
    if not relative.parts or relative.parts[0] != 'incoming' or '..' in relative.parts:
        return None
    return relative.as_posix(), size


def _fd_mount_id(fd):
    """Return the Linux mount id of an open descriptor, or ``None``."""
    try:
        with open('/proc/self/fdinfo/%d' % fd, encoding='ascii') as stream:
            for line in stream:
                if line.startswith('mnt_id:'):
                    return int(line.split(':', 1)[1])
    except (OSError, ValueError, UnicodeError):
        return None
    return None


def _unlink_below(root_fd, mount_id, relative, expected_size):
    """Unlink one exact file below *root_fd* without crossing a mount."""
    parts = relative.split('/')
    if not parts or any(part in ('', '.', '..') for part in parts):
        return False
    current = None
    try:
        current = os.dup(root_fd)
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if (not stat.S_ISDIR(info.st_mode) or not _owned_by_root(info) or
                    info.st_mode & 0o022 or _fd_mount_id(current) != mount_id):
                return False
        name = parts[-1]
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
            getattr(os, 'O_NONBLOCK', 0), dir_fd=current)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or not _owned_by_root(info) or
                    info.st_mode & 0o022 or _fd_mount_id(descriptor) != mount_id or
                    (expected_size is not None and info.st_size != expected_size)):
                return False
        finally:
            os.close(descriptor)
        os.unlink(name, dir_fd=current)
        return True
    except OSError:
        return False
    finally:
        if current is not None:
            os.close(current)


def _retire_offline_files(session, targets):
    """Unlink the proven files of one session; return whether any name went."""
    try:
        info = os.lstat(session)
        if (not stat.S_ISDIR(info.st_mode) or not _owned_by_root(info) or
                info.st_mode & 0o022):
            return False
        session_fd = os.open(
            session, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        mount_id = _fd_mount_id(session_fd)
        if mount_id is None:
            return False
        retired = False
        for relative, size in targets:
            if _unlink_below(session_fd, mount_id, relative, size):
                retired = True
        return retired
    finally:
        os.close(session_fd)


def retire_confirmed_offline_session(record, *, base=None):
    """Remove the dead payload and fixed bulky files of a confirmed session.

    The session is resolved from its own per-transaction pending pointer and
    locked with the store's session lock; no directory is scanned.  ``True``
    means at least one file was removed.  ``False`` covers every unproven case
    -- absent pointer, foreign or duplicate identity, an unconfirmed session,
    a receipt that is not a complete execute, a symlinked or untrusted path, a
    corrupt document, a recovery manifest member or an unresolvable mount
    identity -- and leaves everything untouched.  Cleanup failures are
    swallowed: the durable finalization receipt already proved the transaction
    complete, so a space saving must never turn a confirmed update into a
    failure.
    """
    bounds = _record_bounds(record)
    if bounds is None:
        return False
    try:
        from . import offline_completion, offline_execute, offline_session
    except ImportError:
        return False
    try:
        if base is None:
            store = offline_session.OfflineSessionStore()
        else:
            store = offline_session.OfflineSessionStore(base=base)
    except (OSError, ValueError, RuntimeError):
        return False
    try:
        base_path = offline_session._check_dir(Path(store.base))
        pointer_path = offline_completion.completion_pointer(
            base_path, bounds['transactionId'])
        if (pointer_path is None or
                pointer_path.parent !=
                base_path / offline_completion.POINTER_DIRECTORY):
            return False
        offline_session._check_dir(pointer_path.parent)
        raw = offline_execute._read_bounded(
            pointer_path, _OFFLINE_POINTER_MAX, 'completion pointer')
        pointer = json.loads(
            raw.decode('ascii'), object_pairs_hook=_no_duplicate_keys)
        session_id = offline_completion.pointer_session(
            pointer, bounds['transactionId'])
        if session_id is None:
            return False
        with store.session_lock(session_id) as session:
            receipt = store.read_receipt(session)
            targets = _offline_proof(session, receipt, bounds)
            if targets is None:
                return False
            payload = _payload_target(store, session, receipt)
            if payload is not None:
                targets.append(payload)
            return _retire_offline_files(session, targets)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
