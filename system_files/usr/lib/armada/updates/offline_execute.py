# SPDX-License-Identifier: GPL-3.0-only
"""Execute a prepared, recovery-exported offline dual-slot transaction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from . import (
    boot_candidate,
    deployment_options,
    offline_boot_writer,
    offline_prepare,
    package_contract,
    physical_slots,
    signature,
    system_transaction,
    trial_confirmation,
    trial_record,
)

_JSON_MAX = 2 * 1024 * 1024
_SIG_MAX = 16 * 1024
_SLOTS = ('a', 'b')
_ACK_KEYS = frozenset({'schemaVersion', 'operation', 'transactionId', 'planSha256',
                      'recoveryManifestSha256', 'files'})
_ACK_FILE_KEYS = frozenset({'path', 'bytes', 'sha256'})


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _is_sha(value):
    return (type(value) is str and len(value) == 64 and
            all(ch in '0123456789abcdef' for ch in value))


def _is_schema_one(value):
    return type(value) is int and value == 1


def _read_bounded(path, limit, label):
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        raise ValueError('%s must be a non-symlink canonical path' % label)
    try:
        resolved = p.resolve(strict=True)
    except OSError as exc:
        raise ValueError('%s cannot be resolved: %s' % (label, exc)) from exc
    if resolved != p:
        raise ValueError('%s path is not canonical' % label)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(p, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('%s is not a regular file' % label)
        if st.st_size <= 0 or st.st_size > limit:
            raise ValueError('%s size out of bounds' % label)
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, min((st.st_size - total) + 1, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > st.st_size or total > limit:
                raise ValueError('%s grew while reading' % label)
        end_st = os.fstat(fd)
        if (total != st.st_size or end_st.st_size != st.st_size or
                (end_st.st_dev, end_st.st_ino) != (st.st_dev, st.st_ino)):
            raise ValueError('%s changed while reading' % label)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _load_json(work, name, limit=_JSON_MAX):
    data = _read_bounded(work / name, limit, name)
    value = json.loads(data.decode('ascii'))
    if not isinstance(value, dict):
        raise ValueError('%s root is not a JSON object' % name)
    return value, data


def _under(work, path):
    try:
        Path(path).relative_to(work)
        return True
    except ValueError:
        return False


def _claim_entry(work, claim, kind, label):
    verified = boot_candidate._verify_claim(claim, label)
    path = Path(verified['path'])
    if not _under(work, path):
        raise ValueError('%s claim is outside the transaction work directory' % label)
    return (path.relative_to(work).as_posix(), verified['bytes'], verified['sha256'])


def _check_rel_path(rel, label):
    if type(rel) is not str or not rel or rel.startswith('/') or '\\' in rel or ':' in rel:
        raise ValueError('%s path is not safe and relative' % label)
    parts = rel.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('%s path contains invalid segment' % label)


def _check_recovery_ack(recovery_ack, *, tx, plan, manifest):
    if not isinstance(recovery_ack, dict) or set(recovery_ack) != _ACK_KEYS:
        raise ValueError('recovery acknowledgement has invalid key set')
    if (not _is_schema_one(recovery_ack.get('schemaVersion')) or
            recovery_ack.get('operation') != 'offline-upgrade'):
        raise ValueError('recovery acknowledgement schema/operation mismatch')
    if (recovery_ack['transactionId'] != tx or
            recovery_ack['planSha256'] != plan['planSha256'] or
            recovery_ack['recoveryManifestSha256'] != plan['recoveryManifestSha256']):
        raise ValueError('recovery acknowledgement does not bind this transaction')
    files = recovery_ack['files']
    if not isinstance(files, list) or not files:
        raise ValueError('recovery acknowledgement files must be a non-empty list')
    acked = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != _ACK_FILE_KEYS:
            raise ValueError('recovery acknowledgement file entry has invalid keys')
        _check_rel_path(entry.get('path'), 'recovery acknowledgement')
        if (type(entry['bytes']) is not int or entry['bytes'] <= 0 or
                not _is_sha(entry['sha256'])):
            raise ValueError('recovery acknowledgement file entry is malformed')
        key = (entry['path'], entry['bytes'], entry['sha256'])
        if key in acked:
            raise ValueError('recovery acknowledgement repeats a file entry')
        acked.add(key)
    expected = {(f['path'], f['bytes'], f['sha256']) for f in manifest['files']}
    if acked != expected:
        raise ValueError('recovery acknowledgement file set does not match manifest')


def _preflight(work, recovery_ack, updater_version, trusted_key):
    if work.is_symlink() or not work.is_dir() or work.resolve(strict=True) != work:
        raise ValueError('work directory must be canonical, symlinks rejected')
    saved_status, _ = _load_json(work, 'status.json')
    if (not _is_schema_one(saved_status.get('schemaVersion')) or
            saved_status.get('operation') != 'offline-upgrade' or
            saved_status.get('policy') != 'offline-dual' or
            saved_status.get('phase') != 'awaiting_recovery_export' or
            saved_status.get('state') != 'awaiting_recovery_export'):
        raise ValueError('status is not awaiting_recovery_export for offline-dual')
    prepared, _ = _load_json(work, 'prepared.json')
    plan, plan_bytes = _load_json(work, 'plan.json')
    trust, _ = _load_json(work, 'trust.json')
    materials, _ = _load_json(work, 'materials.json')
    manifest, manifest_bytes = _load_json(work, 'recovery-manifest.json')
    package_doc = _read_bounded(work / 'package.json', _JSON_MAX, 'package.json')
    signature_b64 = _read_bounded(work / 'package.sig', _SIG_MAX, 'package.sig')
    if (not _is_schema_one(prepared.get('schemaVersion')) or
            prepared.get('operation') != 'offline-upgrade' or
            prepared.get('policy') != 'offline-dual'):
        raise ValueError('prepared document schema/operation/policy mismatch')
    if (prepared.get('plan') != 'plan.json' or
            prepared.get('trust') != 'trust.json' or
            prepared.get('materials') != 'materials.json' or
            prepared.get('candidates') != {'a': 'candidate-a.json', 'b': 'candidate-b.json'} or
            prepared.get('recoveryManifest') != 'recovery-manifest.json' or
            prepared.get('packageDescriptor') != 'package.json' or
            prepared.get('packageSignature') != 'package.sig'):
        raise ValueError('prepared artifact filenames mismatch')
    if (not _is_schema_one(plan.get('schemaVersion')) or
            plan.get('operation') != 'offline-upgrade' or
            plan.get('policy') != 'offline-dual' or
            not _is_schema_one(manifest.get('schemaVersion')) or
            manifest.get('operation') != 'offline-upgrade'):
        raise ValueError('plan or recovery manifest schema/operation/policy mismatch')
    tx = prepared.get('transactionId')
    try:
        parsed = str(uuid.UUID(tx))
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed != tx or tx.replace('-', '') == '0' * 32:
        raise ValueError('transactionId must be canonical and nonzero')
    if (plan.get('transactionId') != tx or manifest.get('transactionId') != tx or
            saved_status.get('transactionId') != tx):
        raise ValueError('documents do not bind the same transaction')
    if (not _is_sha(prepared.get('planSha256')) or
            _sha256(plan_bytes) != prepared.get('planSha256')):
        raise ValueError('plan digest mismatch')
    if (not _is_sha(prepared.get('recoveryManifestSha256')) or
            _sha256(manifest_bytes) != prepared.get('recoveryManifestSha256') or
            plan.get('recoveryManifestSha256') != prepared['recoveryManifestSha256']):
        raise ValueError('recovery manifest digest mismatch')
    if (not _is_sha(prepared.get('packageDescriptorSha256')) or
            _sha256(package_doc) != prepared.get('packageDescriptorSha256') or
            plan.get('packageDescriptorSha256') != prepared['packageDescriptorSha256']):
        raise ValueError('package descriptor digest mismatch')
    if (saved_status.get('planSha256') != prepared['planSha256'] or
            saved_status.get('recoveryManifestSha256') != prepared['recoveryManifestSha256']):
        raise ValueError('status-declared digests mismatch prepared digests')

    package = package_contract.load_package(
        package_doc, signature_b64, trusted_key=trusted_key)
    initial = (physical_slots.inspect_physical_slots())
    if initial['model'] not in ('TB322FC', 'TB323FU'):
        raise ValueError('unsupported physical model')
    package_contract.require_operation(
        package, initial['model'], 'offline-upgrade', updater_version=updater_version)
    for key in ('releaseId', 'version', 'sourceRevision', 'ociDigest', 'ostreeCommit'):
        if plan.get(key) != package[key]:
            raise ValueError('plan %s does not match authenticated package' % key)
    current_binding = initial['deployment']
    old = system_transaction._binding(current_binding)
    new = system_transaction._binding(plan.get('newDeployment'))
    if (new[0] != old[0] or new[1] != plan['ostreeCommit'] or new[1] == old[1] or
            new == old or plan.get('oldDeployment') != current_binding):
        raise ValueError('deployment bindings do not match the prepared transaction')
    raw_root = plan.get('sysrootPath')
    if type(raw_root) is not str or not raw_root:
        raise ValueError('plan missing canonical sysrootPath')
    root = Path(raw_root)
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir() or
            root.resolve(strict=True) != root):
        raise ValueError('plan sysrootPath must be canonical directory')
    if _under(root / 'ostree', work) or _under(root / 'boot', work):
        raise ValueError('work directory cannot be inside sysroot ostree or boot')
    dep_opts = deployment_options.read_deployment_options(
        sysroot_path=root, current_binding=current_binding,
        target_binding=plan['newDeployment'])
    if (materials.get('transactionUuid') != tx or
            materials.get('model') != initial['model'] or
            materials.get('deployment') != plan['newDeployment'] or
            materials.get('deployment') != dep_opts['deployment'] or
            materials.get('target') != dep_opts['path'] or
            materials.get('rootUuid') != dep_opts['rootUuid'] or
            materials.get('bootUuid') != dep_opts['bootUuid']):
        raise ValueError('prepared deployment or boot materials binding mismatch')
    actual, inactive = initial['actualSlot'], initial['inactiveSlot']
    if actual not in _SLOTS or inactive != ('b' if actual == 'a' else 'a'):
        raise ValueError('physical active/inactive slots invalid')
    if (plan.get('fromSlot') != actual or plan.get('targetSlot') != inactive or
            plan.get('model') != initial['model']):
        raise ValueError('plan slot/model bindings mismatch physical state')

    if (not _is_schema_one(trust.get('schemaVersion')) or
            not physical_slots.same_physical_identity(trust.get('physical'), initial) or
            not physical_slots.same_physical_identity(
                trust.get('sources', {}).get('physical'), initial) or
            not physical_slots.same_physical_identity(manifest.get('physical'), initial) or
            manifest.get('model') != initial['model']):
        raise ValueError('boot trust physical report mismatch')
    sources = trust['sources']
    candidates = {}
    for slot in _SLOTS:
        candidate, _ = _load_json(work, 'candidate-%s.json' % slot)
        original = boot_candidate._verify_claim(sources['slots'][slot]['boot'],
                                                'old boot %s' % slot)
        if not _under(work, Path(original['path'])):
            raise ValueError('original boot claim outside work directory')
        offline_prepare._check_candidate(
            candidate, slot=slot, tx=tx, deployment=plan['newDeployment'],
            model=initial['model'], mode=trust['mode'],
            source_sha=original['sha256'])
        if plan['originalBootSha256'][slot] != original['sha256']:
            raise ValueError('plan original boot digest mismatch for slot %s' % slot)
        candidate_claim = boot_candidate._verify_claim(candidate['candidate'],
                                                       'new boot %s' % slot)
        if not _under(work, Path(candidate_claim['path'])):
            raise ValueError('candidate outside work directory')
        if plan['newBootSha256'][slot] != candidate_claim['sha256']:
            raise ValueError('plan new boot digest mismatch for slot %s' % slot)
        candidates[slot] = candidate

    journal = system_transaction._check_recovery(initial, old)
    layout = journal.layout
    if (manifest.get('installationUuid') != str(uuid.UUID(bytes=layout.installation_uuid)) or
            manifest.get('recoveryPartUuid') != str(uuid.UUID(bytes=layout.partition_uuid)) or
            manifest.get('recoveryPartUuid') != initial['recoveryPartUuid'] or
            manifest.get('board') != layout.board or
            manifest.get('fromSlot') != actual or
            manifest.get('oldDeployment') != current_binding or
            manifest.get('partitions') != sources.get('partitions')):
        raise ValueError('recovery manifest identity mismatch')
    manifest_files = manifest.get('files')
    if not isinstance(manifest_files, list) or not manifest_files:
        raise ValueError('recovery manifest files must be a non-empty list')
    seen = set()
    for entry in manifest_files:
        if not isinstance(entry, dict):
            raise ValueError('malformed recovery manifest file entry')
        rel = entry.get('path')
        _check_rel_path(rel, 'recovery manifest')
        if (type(entry.get('bytes')) is not int or entry['bytes'] <= 0 or
                not _is_sha(entry.get('sha256'))):
            raise ValueError('malformed recovery manifest file digest/size')
        if rel in seen:
            raise ValueError('recovery manifest repeats path %s' % rel)
        seen.add(rel)
        target = work / rel
        if target.is_symlink() or not target.exists():
            raise ValueError('recovery manifest file must be non-symlink: %s' % rel)
        resolved = target.resolve(strict=True)
        if resolved != target or not _under(work, resolved):
            raise ValueError('recovery manifest file is not canonical under work: %s' % rel)
        size, digest = boot_candidate._hash_regular_file(target)
        if size != entry['bytes'] or digest != entry['sha256']:
            raise ValueError('recovery manifest file content mismatch for %s' % rel)
    expected = set()
    for slot in _SLOTS:
        for name, claim in sources['slots'][slot].items():
            expected.add(_claim_entry(work, claim, 'slot', '%s.%s' % (slot, name)))
    for name, claim in sources['extraFiles'].items():
        expected.add(_claim_entry(work, claim, 'extra', name))
    for name, data in (('recovery-header.bin', journal.header),
                       ('recovery-region.bin', journal.region)):
        if data is None or not isinstance(data, bytes) or len(data) != offline_prepare._JOURNAL_BYTES:
            raise ValueError('captured recovery journal has invalid size')
        expected.add((name, len(data), _sha256(data)))
    if expected != {(f['path'], f['bytes'], f['sha256']) for f in manifest['files']}:
        raise ValueError('recovery manifest does not cover captured sources exactly')

    _check_recovery_ack(recovery_ack, tx=tx, plan=prepared, manifest=manifest)
    result = {'initial': initial, 'old': old, 'new': new, 'tx': tx, 'plan': plan,
              'trust': trust, 'candidates': candidates, 'manifest': manifest,
              'journal': journal, 'saved_status': saved_status}
    return result


def _build_record(ctx, plan_sha):
    journal, initial = ctx['journal'], ctx['initial']
    old, new = ctx['old'], ctx['new']
    actual, inactive = initial['actualSlot'], initial['inactiveSlot']
    sequence = 1 if journal.record is None else journal.record.sequence + 1
    record = trial_record.TrialRecord(
        sequence=sequence, partition_uuid=journal.layout.partition_uuid,
        installation_uuid=journal.layout.installation_uuid,
        transaction_uuid=uuid.UUID(ctx['tx']).bytes, state=trial_record.PREPARED,
        from_slot=0 if actual == 'a' else 1, target_slot=0 if inactive == 'a' else 1,
        remaining=3, attempt_limit=3, old_serial=old[2], new_serial=new[2],
        os_name=old[0], old_commit=bytes.fromhex(old[1]),
        new_commit=bytes.fromhex(new[1]),
        old_boot_sha256=bytes.fromhex(ctx['plan']['originalBootSha256'][actual]),
        new_boot_sha256=bytes.fromhex(ctx['plan']['newBootSha256'][inactive]),
        plan_sha256=bytes.fromhex(plan_sha), policy=trial_record.OFFLINE_DUAL,
        other_boot_sha256=bytes.fromhex(ctx['plan']['newBootSha256'][actual]))
    trial_record.encode_record(record)
    return record


def execute_offline_update(*, work_dir, recovery_ack,
                           updater_version='0.8.0',
                           trusted_key=signature.DEFAULT_TRUSTED_KEY):
    return _execute_offline_update(
        work_dir=work_dir, recovery_ack=recovery_ack,
        updater_version=updater_version, trusted_key=trusted_key)


def _execute_offline_update(*, work_dir=None, recovery_ack,
                            updater_version='0.8.0',
                            trusted_key=signature.DEFAULT_TRUSTED_KEY):
    with trial_confirmation._locked():
        work = Path(work_dir) if work_dir is not None else None
        status = None
        started = False
        write_call_started = False
        boots_written = False
        report_validated = False
        try:
            ctx = _preflight(work, recovery_ack, updater_version, trusted_key)
            status = dict(ctx['saved_status'])
            initial, plan = ctx['initial'], ctx['plan']
            actual, inactive = initial['actualSlot'], initial['inactiveSlot']
            record = _build_record(ctx, ctx['saved_status']['planSha256'])
            started = True
            system_transaction._save_json(work, 'recovery-ack.json', recovery_ack)
            status.update(state='executing', phase='writing_boot',
                          bootWriteState='writing',
                          bootPolicy=trial_record.BOOT_POLICY,
                          slotControlState='not_issued', requiresReboot=False)
            system_transaction._save_json(work, 'status.json', status)

            def progress(event):
                status['bootWriteEvent'] = event
                system_transaction._save_json(work, 'status.json', status)

            def commit():
                # The durable PREPARED journal must cross the commit boundary
                # before the writer touches the running boot extent.
                system_transaction._commit_prepared_journal(
                    initial=initial, expected=ctx['journal'], record=record)

            write_call_started = True
            report = offline_boot_writer.write_offline_boots(
                ctx['trust'], ctx['candidates'], commit=commit, progress=progress)
            boots_written = True
            if ( not isinstance(report, dict) or
                    report.get('phase') != 'dual_boots_written' or
                    report.get('model') != initial['model'] or
                    not physical_slots.same_physical_identity(
                        report.get('physical'), initial) or
                    report.get('transactionUuid') != ctx['tx'] or
                    report.get('deployment') != plan['newDeployment'] or
                    report.get('fromSlot') != actual or
                    report.get('slotOrder') != [inactive, actual] or
                    set(report.get('writes', {})) != {'a', 'b'}):
                raise ValueError('offline dual boot write report mismatch')
            for slot in _SLOTS:
                evidence = report['writes'][slot]
                claim = boot_candidate._verify_claim(evidence['candidate'],
                                                     'write evidence %s' % slot)
                if (evidence['sourceBootSha256'] != plan['originalBootSha256'][slot] or
                        evidence['bytesWritten'] != claim['bytes'] or
                        claim['sha256'] != plan['newBootSha256'][slot] or
                        evidence['readbackSha256'] != plan['newBootSha256'][slot]):
                    raise ValueError('offline boot write evidence mismatch for slot %s' % slot)
            report_validated = True
            status['bootWriteState'] = 'written'
            system_transaction._save_json(work, 'write-report.json', report)
            system_transaction._save_json(work, 'status.json', status)

            after_write = (physical_slots.inspect_physical_slots())
            if not physical_slots.same_physical_identity(after_write, initial):
                raise ValueError('physical slots changed after boot writes')
            status['phase'] = 'journal_prepared'
            system_transaction._save_json(work, 'status.json', status)

            status.update(state='awaiting_reboot', phase='awaiting_reboot',
                          requiresReboot=True, bootWriteState='written',
                          slotControlState='not_issued',
                          journalSequence=record.sequence)
            system_transaction._save_json(work, 'status.json', status)
            return status
        except Exception as exc:
            if isinstance(exc, system_transaction.SystemTransactionError):
                raise
            if not started:
                result = {'phase': 'preflight', 'state': 'failed',
                          'bootPolicy': trial_record.BOOT_POLICY,
                          'slotControlState': 'not_issued',
                          'error': str(exc), 'requiresReboot': False}
                raise system_transaction.SystemTransactionError(
                    str(exc), result) from exc
            status['failedPhase'] = status.get('phase')
            status['phase'] = 'failed'
            status['requiresReboot'] = False
            status['slotControlState'] = 'not_issued'
            if not write_call_started:
                status['bootWriteState'] = 'not_started'
                status['state'] = 'failed'
            elif (isinstance(exc, offline_boot_writer.BootWriteError) and
                    not boots_written):
                restore = exc.restore_status
                commit_state = getattr(exc, 'commit_state', 'not_committed')
                status['bootRestoreState'] = restore
                if restore == 'not_needed':
                    status['bootWriteState'] = 'not_started'
                    status['state'] = 'failed'
                elif restore == 'restored':
                    status['bootWriteState'] = 'restored'
                    status['state'] = 'failed'
                elif restore == 'not_restored':
                    # A committed journal only records that the commit
                    # boundary was crossed; it does not prove that both slots
                    # received the image, and this failure happened before the
                    # writer returned its report.  Keep the run in recovery,
                    # record the reported commit state and per-slot results,
                    # and never advertise a restart from partial evidence.
                    status['bootWriteState'] = 'uncertain'
                    status['journalCommitState'] = commit_state
                    status['state'] = 'recovery_required'
                else:
                    status['bootWriteState'] = 'uncertain'
                    status['state'] = 'recovery_required'
                if getattr(exc, 'slot_results', None) is not None:
                    status['slotResults'] = exc.slot_results
            else:
                status['bootWriteState'] = 'written' if report_validated else 'uncertain'
                status['state'] = 'recovery_required'
                if boots_written:
                    status['journalCommitState'] = 'committed'
            status['error'] = '%s: %s' % (type(exc).__name__, exc)
            try:
                system_transaction._save_json(work, 'status.json', status)
            except Exception as save_exc:
                status['persistenceError'] = str(save_exc)
            raise system_transaction.SystemTransactionError(str(exc), status) from exc
