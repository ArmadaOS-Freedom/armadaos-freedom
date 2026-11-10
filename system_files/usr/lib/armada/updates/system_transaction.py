# SPDX-License-Identifier: GPL-3.0-only
"""Orchestrate one locked, inactive-slot online system transaction."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

import armada_os
from . import (
    boot_candidate,
    boot_materials,
    boot_trust,
    boot_writer,
    deployment_store,
    image_source,
    physical_slots,
    recovery_device,
    signature,
    slot_activation,
    trial_confirmation,
    trial_record,
    trial_store,
)

_CHANNELS = frozenset({'stable', 'beta', 'testing'})
_TERMINAL = frozenset({trial_record.CONFIRMED, trial_record.ROLLED_BACK})


class SystemTransactionError(RuntimeError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def _save_json(directory, name, value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    temporary = directory / ('.%s.%s.tmp' % (name, uuid.uuid4().hex))
    fd = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('short JSON write')
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, directory / name)
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return data
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _binding(value):
    return deployment_store._parse_binding(value)


def _snapshot_channel(snapshot):
    channel = snapshot.get('channel') or 'stable'
    if channel == 'preview':
        channel = 'testing'
    if channel not in _CHANNELS:
        raise ValueError('unsupported channel')
    return channel


def _validate_rollback_target(snapshot, expected_checksum, old, journal_record):
    if (not isinstance(expected_checksum, str) or len(expected_checksum) != 64 or
            any(ch not in '0123456789abcdef' for ch in expected_checksum)):
        raise ValueError('rollback checksum must be 64 lowercase hex characters')
    rollback = snapshot.get('rollback')
    if not isinstance(rollback, dict) or rollback.get('incompatible') is not False:
        raise ValueError('rollback deployment is not compatible')
    if rollback.get('checksum') != expected_checksum:
        raise ValueError('rollback checksum does not match current rollback target')
    serial = rollback.get('serial')
    if isinstance(serial, bool) or not isinstance(serial, int) or serial < 0 or serial > 0xFFFFFFFF:
        raise ValueError('rollback deployment serial is invalid')
    stateroot = rollback.get('stateroot')
    if not isinstance(stateroot, str) or not stateroot:
        raise ValueError('rollback deployment stateroot is invalid')
    target_deployment = '%s/%s.%d' % (stateroot, expected_checksum, serial)
    target = _binding(target_deployment)
    if target[0] != old[0] or target[1] == old[1]:
        raise ValueError('rollback deployment must be a distinct commit in the active OS')
    if journal_record is None or journal_record.state != trial_record.CONFIRMED:
        raise ValueError('previous bound deployment unavailable')
    journal_old = (journal_record.os_name, journal_record.old_commit.hex(), journal_record.old_serial)
    if target != journal_old:
        raise ValueError('previous bound deployment unavailable')
    version = rollback.get('version')
    return (target_deployment, target,
            version if isinstance(version, str) and version else None)


def _recheck_rollback_snapshot(expected_checksum, target):
    snapshot = armada_os.snapshot()
    if (snapshot['repository'] not in armada_os.REPOSITORIES or
            snapshot['staged'] or snapshot['rollback_queued']):
        raise ValueError('legacy OSTree transaction conflict')
    rollback = snapshot.get('rollback')
    if (not isinstance(rollback, dict) or rollback.get('incompatible') is not False or
            rollback.get('checksum') != expected_checksum or
            type(rollback.get('serial')) is not int or
            rollback.get('serial') != target[2] or
            rollback.get('stateroot') != target[0]):
        raise ValueError('rollback target changed before boot write')


def _check_recovery(initial, old):
    with recovery_device.open_recovery(writable=False) as (fd, partuuid, board):
        if partuuid != initial['recoveryPartUuid']:
            raise ValueError('recovery partition changed')
        if physical_slots._MODEL.get(board) != initial['model']:
            raise ValueError('recovery board changed')
        snapshot = trial_store.load(fd, partuuid, board)
    record = snapshot.record
    if record is not None:
        if record.state not in _TERMINAL:
            raise ValueError('pending trial journal refuses overwrite')
        if record.state == trial_record.CONFIRMED:
            expected = {(record.target_slot, record.os_name, record.new_commit.hex(), record.new_serial)}
            if record.policy == trial_record.OFFLINE_DUAL:
                expected.add((record.from_slot, record.os_name, record.new_commit.hex(), record.new_serial))
        else:
            expected = {(record.from_slot, record.os_name, record.old_commit.hex(), record.old_serial)}
        actual = (0 if initial['actualSlot'] == 'a' else 1, old[0], old[1], old[2])
        if actual not in expected:
            raise ValueError('terminal trial record does not match physical deployment')
    return snapshot


def _run_transaction(*, mode, sysroot_path, work_dir,
                     channel_url=None, signature_url=None, expected_channel=None,
                     expected_checksum=None, updater_version='0.8.0',
                     boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                     dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py'),
                     trusted_key=signature.DEFAULT_TRUSTED_KEY):
    if mode not in ('online', 'rollback'):
        raise ValueError('unsupported transaction mode')
    with trial_confirmation._locked():
        work = Path(work_dir)
        created = False
        activation_started = False
        status = {'schemaVersion': 1, 'component': 'system', 'state': 'failed',
                  'phase': 'preflight', 'requiresReboot': False,
                  'bootWriteState': 'not_started', 'activationOutcome': 'not_attempted',
                  'workDir': str(work)}
        if mode == 'rollback':
            status['operation'] = 'rollback'
        try:
            if mode == 'online' and expected_channel not in _CHANNELS:
                raise ValueError('unsupported channel')
            if work.exists() or not work.parent.is_dir():
                raise ValueError('work leaf must not exist and parent must exist')
            raw_root = Path(sysroot_path)
            if not raw_root.is_absolute():
                raise ValueError('sysroot path must be absolute')
            root = raw_root.resolve(strict=True)
            if raw_root != root:
                raise ValueError('sysroot path must be canonical, symlinks rejected')
            if not root.is_dir():
                raise ValueError('sysroot is not a directory')
            boot_materials._validate_output_dir(work, [root / 'ostree', root / 'boot'])
            initial = physical_slots.inspect_physical_slots()
            if initial['model'] not in ('TB322FC', 'TB323FU'):
                raise ValueError('unsupported physical model')
            snapshot = armada_os.snapshot()
            if (snapshot['repository'] not in armada_os.REPOSITORIES or
                    snapshot['staged'] or snapshot['rollback_queued']):
                raise ValueError('legacy OSTree transaction conflict')
            snapshot_channel = _snapshot_channel(snapshot)
            if mode == 'online' and snapshot_channel != expected_channel:
                raise ValueError('requested channel differs from configured channel')
            old = _binding(initial['deployment'])
            journal = _check_recovery(initial, old)
            target_deployment = None
            new = None
            target_version = None
            if mode == 'rollback':
                target_deployment, new, target_version = _validate_rollback_target(
                    snapshot, expected_checksum, old, journal.record)
            if journal.record is not None and journal.record.state == trial_record.CONFIRMED:
                boot = trial_confirmation._check_boot_evidence(
                    trial_confirmation.collect_boot_evidence(journal.record), journal.record
                )
                trial_confirmation._finalize_deployment(journal.record, boot, sysroot_path=root)
                if physical_slots.inspect_physical_slots() != initial:
                    raise ValueError('physical slots changed after confirmed trial finalization')
            tx = str(uuid.uuid4())
            status.update(transactionId=tx, model=initial['model'],
                          fromSlot=initial['actualSlot'], targetSlot=initial['inactiveSlot'],
                          oldDeployment=initial['deployment'], phase='preparing', state='preparing')
            if mode == 'rollback':
                status['newDeployment'] = target_deployment
                if target_version is not None:
                    status['targetVersion'] = target_version
            work.mkdir(mode=0o700)
            created = True
            _save_json(work, 'status.json', status)
            _save_json(work.parent, 'current.json', {
                'schemaVersion': 1, 'jobId': work.name})

            if mode == 'online':
                status['phase'] = 'downloading'
                _save_json(work, 'status.json', status)
                downloaded = image_source.fetch_online_image(
                    channel_url=channel_url, signature_url=signature_url,
                    expected_channel=expected_channel, model=initial['model'],
                    updater_version=updater_version, sysroot_path=sysroot_path,
                    work_dir=work / 'download', trusted_key=trusted_key)
                if downloaded['commit'] == old[1]:
                    status.update(state='up_to_date', phase='complete', requiresReboot=False)
                    _save_json(work, 'status.json', status)
                    return status
                if physical_slots.inspect_physical_slots() != initial:
                    raise ValueError('physical slots changed after download')

            status['phase'] = 'capturing_boot_trust'
            _save_json(work, 'status.json', status)
            trust = boot_trust.capture_boot_trust(output_dir=work / 'trust',
                                                  boot_tool_path=boot_tool_path)
            if trust['physical'] != initial:
                raise ValueError('boot trust physical report mismatch')

            if mode == 'online':
                status['phase'] = 'deploying'
                _save_json(work, 'status.json', status)
                package = downloaded['package']
                deployed = deployment_store.append_verified_deployment(
                    sysroot_path=sysroot_path, current_binding=initial['deployment'],
                    target_commit=downloaded['commit'], oci_digest=package['ociDigest'],
                    source_revision=package['sourceRevision'], version=package['version'],
                    model=initial['model'], origin_ref=downloaded['originRef'])
                target_deployment = deployed['deployment']
                new = _binding(target_deployment)
                if deployed['model'] != initial['model'] or new[0] != old[0] or new[1] != downloaded['commit']:
                    raise ValueError('deployment does not match authenticated package')
                status['newDeployment'] = target_deployment

            status['phase'] = 'preparing_materials'
            _save_json(work, 'status.json', status)
            materials = boot_materials.prepare_boot_materials(
                sysroot_path=sysroot_path, current_binding=initial['deployment'],
                target_binding=target_deployment, model=initial['model'],
                output_dir=work / 'materials', dtb_helper_path=dtb_helper_path,
                transaction_uuid=tx)
            if (materials['transactionUuid'] != tx or
                    materials['deployment'] != target_deployment or
                    materials['model'] != initial['model']):
                raise ValueError('boot materials binding mismatch')

            status['phase'] = 'preparing_candidate'
            _save_json(work, 'status.json', status)
            target = initial['inactiveSlot']
            candidate = boot_candidate.prepare_boot_candidate(
                slot=target, slot_files=trust['sources']['slots'][target], materials=materials,
                mode=trust['mode'], max_raw_bytes=trust['trust'][target]['maxRawBytes'],
                output_dir=work / 'candidate', boot_tool_path=boot_tool_path)
            if (candidate['phase'] != 'candidate_prepared' or candidate['slot'] != target or
                    candidate['transactionUuid'] != tx or
                    candidate['deployment'] != target_deployment):
                raise ValueError('boot candidate binding mismatch')
            old_claim = boot_candidate._verify_claim(
                trust['sources']['slots'][initial['actualSlot']]['boot'], 'old boot')
            new_claim = boot_candidate._verify_claim(candidate['candidate'], 'new boot')
            if mode == 'online':
                plan = {'schemaVersion': 1, 'transactionId': tx, 'model': initial['model'],
                        'channel': expected_channel, 'releaseId': package['releaseId'],
                        'version': package['version'], 'sourceRevision': package['sourceRevision'],
                        'ociDigest': package['ociDigest'], 'ostreeCommit': downloaded['commit'],
                        'fromSlot': initial['actualSlot'], 'targetSlot': target,
                        'oldDeployment': initial['deployment'], 'newDeployment': target_deployment,
                        'oldBootSha256': old_claim['sha256'], 'newBootSha256': new_claim['sha256']}
            else:
                plan = {'schemaVersion': 1, 'operation': 'rollback', 'transactionId': tx,
                        'model': initial['model'], 'channel': snapshot_channel,
                        'ostreeCommit': new[1],
                        'fromSlot': initial['actualSlot'], 'targetSlot': target,
                        'oldDeployment': initial['deployment'], 'newDeployment': target_deployment,
                        'oldBootSha256': old_claim['sha256'], 'newBootSha256': new_claim['sha256']}
                if target_version is not None:
                    plan['targetVersion'] = target_version
            plan_bytes = _save_json(work, 'plan.json', plan)
            plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
            status['planSha256'] = plan_sha256

            if mode == 'rollback':
                _recheck_rollback_snapshot(expected_checksum, new)
            status.update(phase='writing_boot', bootWriteState='writing')
            _save_json(work, 'status.json', status)
            report = boot_writer.write_inactive_boot(trust, candidate)
            status['bootWriteState'] = 'written'
            if (report['phase'] != 'inactive_boot_written' or report['model'] != initial['model'] or
                    report['physical'] != initial or
                    report['bytesWritten'] != new_claim['bytes'] or
                    report['fromSlot'] != initial['actualSlot'] or report['targetSlot'] != target or
                    report['transactionUuid'] != tx or report['deployment'] != target_deployment or
                    report['candidate']['sha256'] != plan['newBootSha256'] or
                    report['readbackSha256'] != plan['newBootSha256']):
                raise ValueError('inactive boot report mismatch')
            _save_json(work, 'write-report.json', report)
            status['phase'] = 'preparing_trial'
            _save_json(work, 'status.json', status)

            sequence = 1 if journal.record is None else journal.record.sequence + 1
            record = trial_record.TrialRecord(
                sequence=sequence, partition_uuid=journal.layout.partition_uuid,
                installation_uuid=journal.layout.installation_uuid,
                transaction_uuid=uuid.UUID(tx).bytes, state=trial_record.PREPARED,
                from_slot=0 if initial['actualSlot'] == 'a' else 1,
                target_slot=0 if target == 'a' else 1, remaining=3, attempt_limit=3,
                old_serial=old[2], new_serial=new[2], os_name=old[0],
                old_commit=bytes.fromhex(old[1]), new_commit=bytes.fromhex(new[1]),
                old_boot_sha256=bytes.fromhex(plan['oldBootSha256']),
                new_boot_sha256=bytes.fromhex(plan['newBootSha256']),
                plan_sha256=bytes.fromhex(plan_sha256))
            trial_record.encode_record(record)
            if physical_slots.inspect_physical_slots() != initial:
                raise ValueError('physical slots changed before journal write')
            with recovery_device.open_recovery(writable=True) as (fd, partuuid, board):
                if partuuid != initial['recoveryPartUuid'] or physical_slots._MODEL.get(board) != initial['model']:
                    raise ValueError('recovery identity changed')
                if trial_store.load(fd, partuuid, board) != journal:
                    raise ValueError('recovery journal compare-and-swap failed')
                trial_store.save(fd, journal, record)
                if trial_store.load(fd, partuuid, board).record != record:
                    raise ValueError('prepared journal readback mismatch')
            status['phase'] = 'journal_prepared'
            _save_json(work, 'status.json', status)

            status.update(phase='activating', activationOutcome='attempting')
            _save_json(work, 'status.json', status)
            activation_started = True
            activated = slot_activation.activate_prepared_slot(report)
            if (activated['phase'] != 'slot_activated' or activated['model'] != initial['model'] or
                    activated['fromSlot'] != initial['actualSlot'] or activated['targetSlot'] != target or
                    activated['selectedSlot'] != target or activated['transactionUuid'] != tx or
                    activated['deployment'] != target_deployment or
                    activated['recordSequence'] != sequence or
                    activated['readbackSha256'] != plan['newBootSha256']):
                raise ValueError('slot activation report mismatch')
            status.update(state='awaiting_reboot', phase='awaiting_reboot', requiresReboot=True,
                          activationOutcome='activated', bootWriteState='written')
            _save_json(work, 'status.json', status)
            return status
        except Exception as exc:
            if isinstance(exc, SystemTransactionError):
                raise
            status['state'] = 'failed'
            status['requiresReboot'] = False
            status['activationOutcome'] = 'not_attempted'
            if isinstance(exc, boot_writer.BootWriteError):
                restore = exc.restore_status
                status['bootRestoreState'] = restore
                if restore == 'not_needed':
                    status['bootWriteState'] = 'not_started'
                elif restore == 'restored':
                    status['bootWriteState'] = 'restored'
                else:
                    status['bootWriteState'] = 'uncertain'
                    status['state'] = 'uncertain'
            if isinstance(exc, slot_activation.ActivationError):
                status['activationOutcome'] = exc.outcome
                if getattr(exc, 'native_result', None) is not None:
                    status['nativeResult'] = exc.native_result
                if exc.outcome == 'uncertain':
                    status['state'] = 'uncertain'
            elif activation_started:
                status['activationOutcome'] = 'uncertain'
                status['state'] = 'uncertain'
            status['error'] = '%s: %s' % (type(exc).__name__, exc)
            if created:
                try:
                    _save_json(work, 'status.json', status)
                except Exception as save_exc:
                    status['persistenceError'] = '%s: %s' % (type(save_exc).__name__, save_exc)
            raise SystemTransactionError(str(exc), status) from exc


def run_online_transaction(*, channel_url, signature_url, expected_channel,
                           sysroot_path, work_dir, updater_version='0.8.0',
                           boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                           dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py'),
                           trusted_key=signature.DEFAULT_TRUSTED_KEY):
    return _run_transaction(
        mode='online', channel_url=channel_url, signature_url=signature_url,
        expected_channel=expected_channel, sysroot_path=sysroot_path,
        work_dir=work_dir, updater_version=updater_version,
        boot_tool_path=boot_tool_path, dtb_helper_path=dtb_helper_path,
        trusted_key=trusted_key)


def run_rollback_transaction(*, expected_checksum, sysroot_path, work_dir,
                             boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                             dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py')):
    return _run_transaction(
        mode='rollback', expected_checksum=expected_checksum,
        sysroot_path=sysroot_path, work_dir=work_dir,
        boot_tool_path=boot_tool_path, dtb_helper_path=dtb_helper_path)
