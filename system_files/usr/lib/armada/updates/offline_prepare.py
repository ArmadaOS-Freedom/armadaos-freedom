# SPDX-License-Identifier: GPL-3.0-only
"""Prepare an authenticated offline dual-slot update without writing boot state."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import uuid

import armada_os
from . import (
    UPDATER_VERSION,
    _io,
    boot_candidate,
    boot_disk_switch,
    boot_materials,
    boot_sources,
    boot_trust,
    deployment_store,
    image_source,
    models,
    offline_payload,
    package_contract,
    physical_slots,
    recovery_device,
    signature,
    system_transaction,
    trial_confirmation,
    trial_record,
    trial_store,
)

_CHANNELS = frozenset({'stable', 'beta', 'testing'})
_JOURNAL_BYTES = 64 * 1024


def _sha256(data):
    return _io.sha256_hex(data)


def _remove_owned_tree(path):
    """Remove one private tree without following any symlink it contains."""
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            info = os.lstat(child)
            if stat.S_ISDIR(info.st_mode):
                _remove_owned_tree(child)
            else:
                os.unlink(child)
    os.rmdir(path)


def _discard_import_workspace(work):
    """Remove the transient import staging tree owned by this preparation.

    ``import_offline_payload`` expands the full-size ``linux-root.img`` and
    extracts the whole OSTree repository under ``<transaction>/import``.  Both
    are reusable work products only until the imported deployment is committed
    and its binding re-verified, after which they are dead weight.

    Only that exact leaf is considered.  A symlink is never followed
    (``lstat`` reports the link itself), and a directory that is not owned by
    this process or is writable by group/other is left untouched, so nothing
    outside the owned staging tree -- transaction documents, recovery exports,
    trust material or candidates -- can be removed.  No other path is scanned.
    """
    staging = work / 'import'
    try:
        info = os.lstat(staging)
    except OSError:
        return
    if (not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.geteuid() or info.st_mode & 0o022):
        return
    _remove_owned_tree(staging)


def _check_candidate(candidate, *, slot, tx, deployment, model, mode, source_sha):
    if (candidate.get('phase') != 'candidate_prepared' or
            candidate.get('slot') != slot or candidate.get('model') != model or
            candidate.get('transactionUuid') != tx or
            candidate.get('deployment') != deployment or
            candidate.get('mode') != mode or
            candidate.get('sourceBootSha256') != source_sha):
        raise ValueError('boot candidate binding mismatch for slot %s' % slot)
    boot_candidate._verify_claim(candidate.get('candidate'), 'new boot')


def _recovery_files(work, sources, journal):
    entries = []
    seen = {}

    def add(claim, kind, label):
        verified = boot_candidate._verify_claim(claim, label)
        path = Path(verified['path'])
        try:
            relative = path.relative_to(work).as_posix()
        except ValueError as exc:
            raise ValueError('%s is outside the transaction work directory' % label) from exc
        identity = (verified['bytes'], verified['sha256'])
        if str(path) in seen:
            if seen[str(path)] != identity:
                raise ValueError('conflicting recovery export claims for %s' % path)
            return
        seen[str(path)] = identity
        entries.append({'path': relative, 'bytes': verified['bytes'],
                        'sha256': verified['sha256'], 'kind': kind, 'label': label})

    for slot in ('a', 'b'):
        for name, claim in sources['slots'][slot].items():
            add(claim, 'slot', '%s.%s' % (slot, name))
    for name, claim in sources['extraFiles'].items():
        add(claim, 'extra', name)

    for name, data in (('recovery-header.bin', journal.header),
                       ('recovery-region.bin', journal.region)):
        if not isinstance(data, bytes) or len(data) != _JOURNAL_BYTES:
            raise ValueError('captured recovery journal has invalid size')
        path = work / name
        boot_materials._write_new(path, data)
        entries.append({'path': name, 'bytes': len(data), 'sha256': _sha256(data),
                        'kind': 'journal', 'label': name})
    return entries


def _read_recovery_for_disk(initial):
    with recovery_device.open_recovery(writable=False) as (fd, partuuid, board):
        if (partuuid != initial['recoveryPartUuid'] or
                physical_slots._MODEL.get(board) != initial['model']):
            raise ValueError('recovery identity changed')
        return trial_store.load(fd, partuuid, board)


def prepare_offline_update(*, document, signature_b64, payload_directory,
                           expected_channel, sysroot_path, work_dir,
                           updater_version=UPDATER_VERSION,
                           boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                           dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py'),
                           trusted_key=signature.DEFAULT_TRUSTED_KEY):
    return _prepare_offline_update(
        document=document, signature_b64=signature_b64,
        payload_directory=payload_directory, expected_channel=expected_channel,
        sysroot_path=sysroot_path, work_dir=work_dir,
        updater_version=updater_version, boot_tool_path=boot_tool_path,
        dtb_helper_path=dtb_helper_path, trusted_key=trusted_key)


def _prepare_offline_update(*, document, signature_b64, payload_directory=None,
                            expected_channel, sysroot_path=None, work_dir=None,
                            updater_version=UPDATER_VERSION,
                            boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                            dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py'),
                            trusted_key=signature.DEFAULT_TRUSTED_KEY):
    status = {'component': 'system', 'operation': 'offline-upgrade',
              'policy': 'offline-dual', 'schemaVersion': 1,
              'bootPolicy': trial_record.BOOT_POLICY,
              'phase': 'preparing', 'state': 'preparing',
              'bootWriteState': 'not_started', 'slotControlState': 'not_issued',
              'requiresReboot': False}
    work = None
    created = False
    created_generation = None
    with trial_confirmation._locked():
        try:
            if type(expected_channel) is not str or expected_channel not in _CHANNELS:
                raise ValueError('invalid expected_channel')
            if not isinstance(sysroot_path, (str, Path)) or not isinstance(work_dir, (str, Path)):
                raise ValueError('sysroot_path and work_dir must be paths')
            root, requested_work = Path(sysroot_path), Path(work_dir)
            if not root.is_absolute() or not requested_work.is_absolute():
                raise ValueError('sysroot_path and work_dir must be absolute')
            if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
                raise ValueError('sysroot path must be canonical, symlinks rejected')
            inspect_slots = physical_slots.inspect_physical_slots

            initial = inspect_slots()
            if initial['model'] not in models.OFFLINE_UPGRADE_MODELS:
                raise ValueError('unsupported physical model')
            disk = initial.get('bootSource') == 'armada_boot'
            if disk:
                boot_disk_switch.ensure_ready(root, initial)
                status['bootSource'] = 'armada_boot'
            snapshot = armada_os.snapshot()
            if (snapshot['repository'] not in armada_os.REPOSITORIES or
                    snapshot['staged'] or snapshot['rollback_queued']):
                raise ValueError('legacy OSTree transaction conflict')
            if system_transaction._snapshot_channel(snapshot) != expected_channel:
                raise ValueError('requested channel differs from configured channel')
            current_binding = initial['deployment']
            old = system_transaction._binding(current_binding)
            journal = (_read_recovery_for_disk(initial) if disk else
                       system_transaction._check_recovery(initial, old))

            package = package_contract.load_package(
                document, signature_b64, trusted_key=trusted_key)
            package_contract.require_operation(
                package, initial['model'], 'offline-upgrade',
                updater_version=updater_version)
            has_capsule = initial['model'] in package.get('armadaBootCapsules', {})
            if disk and not has_capsule:
                raise ValueError('signed package lacks ARMADA_BOOT materials for model')
            if has_capsule and not disk:
                raise ValueError('this package requires an existing ARMADA_BOOT installation')
            status.update(model=initial['model'], fromSlot=initial['actualSlot'],
                          targetSlot=initial['inactiveSlot'],
                          oldDeployment=current_binding)
            if package['ostreeCommit'] == old[1]:
                status.update(state='up_to_date', phase='complete')
                return status

            boot_materials._validate_output_dir(
                requested_work, [root / 'ostree', root / 'boot'])
            root, work = image_source._paths(root, requested_work)
            created = True
            tx = str(uuid.uuid4())
            status['transactionId'] = tx
            system_transaction._save_json(work, 'status.json', status)
            boot_materials._write_new(work / 'package.json', document)
            boot_materials._write_new(work / 'package.sig', signature_b64)

            status['phase'] = 'importing'
            system_transaction._save_json(work, 'status.json', status)
            imported = offline_payload.import_offline_payload(
                document=document, signature_b64=signature_b64,
                payload_directory=payload_directory, expected_channel=expected_channel,
                model=initial['model'], updater_version=updater_version,
                sysroot_path=root, work_dir=work / 'import', trusted_key=trusted_key)
            if (imported.get('package') != package or
                    imported.get('commit') != package['ostreeCommit']):
                raise ValueError('offline import does not match authenticated package')
            if not physical_slots.same_physical_identity(inspect_slots(), initial):
                raise ValueError('physical slots changed after offline import')

            status['phase'] = 'capturing_boot_trust'
            system_transaction._save_json(work, 'status.json', status)
            if disk:
                labels = ('recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b')
                if initial['model'] in models.GBL_MODELS:
                    labels += ('efisp',)
                sources = boot_sources.capture_boot_sources(initial, work / 'trust', extra_labels=labels)
                if not physical_slots.same_physical_identity(sources['physical'], initial):
                    raise ValueError('boot sources physical report mismatch')
                trust = {'schemaVersion': 1, 'physical': initial, 'sources': sources}
            else:
                trust = boot_trust.capture_boot_trust(
                    output_dir=work / 'trust', boot_tool_path=boot_tool_path)
                if (not physical_slots.same_physical_identity(trust['physical'], initial) or
                        not physical_slots.same_physical_identity(
                            trust['sources']['physical'], initial)):
                    raise ValueError('boot trust physical report mismatch')
                sources = trust['sources']

            status['phase'] = 'deploying'
            system_transaction._save_json(work, 'status.json', status)
            deployed = deployment_store.append_verified_deployment(
                sysroot_path=root, current_binding=current_binding,
                target_commit=imported['commit'], oci_digest=package['ociDigest'],
                source_revision=package['sourceRevision'], version=package['version'],
                model=initial['model'], origin_ref=imported['originRef'])
            target_deployment = deployed['deployment']
            new = system_transaction._binding(target_deployment)
            if (deployed['model'] != initial['model'] or new[0] != old[0] or
                    new[1] != imported['commit'] or new == old):
                raise ValueError('deployment does not match authenticated package')
            status['newDeployment'] = target_deployment

            # The expanded image and extracted repository are no longer needed
            # once the deployment is committed and its binding is verified.
            # Reclaim only this step's owned staging tree; every transaction
            # document and recovery export stays in place for execute/export.
            try:
                _discard_import_workspace(work)
            except OSError:
                pass

            switch_plan = None
            if disk:
                status['phase'] = 'preparing_armada_boot'
                system_transaction._save_json(work, 'status.json', status)
                def _generation_created(name, path):
                    nonlocal created_generation
                    created_generation = path

                switch_plan = boot_disk_switch.prepare(
                    root, initial, target_deployment, package, trusted_key=trusted_key,
                    on_stage=_generation_created)
                old_sha = {
                    slot: boot_candidate._verify_claim(sources['slots'][slot]['boot'], 'old boot')['sha256']
                    for slot in ('a', 'b')
                }
                new_sha = dict(old_sha)
                system_transaction._save_json(work, 'trust.json', trust)
            else:
                status['phase'] = 'preparing_materials'
                system_transaction._save_json(work, 'status.json', status)
                materials = boot_materials.prepare_boot_materials(
                    sysroot_path=root, current_binding=current_binding,
                    target_binding=target_deployment, model=initial['model'],
                    output_dir=work / 'materials', dtb_helper_path=dtb_helper_path,
                    transaction_uuid=tx)
                if (materials['transactionUuid'] != tx or
                        materials['deployment'] != target_deployment or
                        materials['model'] != initial['model']):
                    raise ValueError('boot materials binding mismatch')

                status['phase'] = 'preparing_candidates'
                system_transaction._save_json(work, 'status.json', status)
                candidates = {}
                old_sha = {}
                new_sha = {}
                for slot in ('a', 'b'):
                    status['preparingSlot'] = slot
                    system_transaction._save_json(work, 'status.json', status)
                    original = boot_candidate._verify_claim(
                        trust['sources']['slots'][slot]['boot'], 'old boot')
                    old_sha[slot] = original['sha256']
                    candidate = boot_candidate.prepare_boot_candidate(
                        slot=slot, slot_files=trust['sources']['slots'][slot],
                        materials=materials, mode=trust['mode'],
                        max_raw_bytes=trust['trust'][slot]['maxRawBytes'],
                        output_dir=work / ('candidate-' + slot),
                        boot_tool_path=boot_tool_path)
                    _check_candidate(candidate, slot=slot, tx=tx,
                                     deployment=target_deployment, model=initial['model'],
                                     mode=trust['mode'], source_sha=original['sha256'])
                    candidates[slot] = candidate
                    new_sha[slot] = candidate['candidate']['sha256']

                system_transaction._save_json(work, 'trust.json', trust)
                system_transaction._save_json(work, 'materials.json', materials)
                for slot in ('a', 'b'):
                    system_transaction._save_json(
                        work, 'candidate-%s.json' % slot, candidates[slot])

            manifest = {
                'schemaVersion': 1, 'operation': 'offline-upgrade',
                'transactionId': tx, 'model': initial['model'],
                'installationUuid': str(uuid.UUID(bytes=journal.layout.installation_uuid)),
                'recoveryPartUuid': str(uuid.UUID(bytes=journal.layout.partition_uuid)),
                'board': journal.layout.board,
                'oldDeployment': current_binding,
                'fromSlot': initial['actualSlot'], 'physical': initial,
                'partitions': sources['partitions'],
                'files': _recovery_files(work, sources, journal),
            }
            if manifest['recoveryPartUuid'] != initial['recoveryPartUuid']:
                raise ValueError('recovery identity mismatch')
            manifest_bytes = system_transaction._save_json(
                work, 'recovery-manifest.json', manifest)
            manifest_sha = _sha256(manifest_bytes)
            document_sha = _sha256(document)
            plan = {
                'schemaVersion': 1, 'operation': 'offline-upgrade',
                'policy': 'offline-dual', 'transactionId': tx,
                'model': initial['model'], 'channel': expected_channel,
                'sysrootPath': str(root),
                'releaseId': package['releaseId'], 'version': package['version'],
                'sourceRevision': package['sourceRevision'],
                'ociDigest': package['ociDigest'], 'ostreeCommit': imported['commit'],
                'fromSlot': initial['actualSlot'], 'targetSlot': initial['inactiveSlot'],
                'oldDeployment': current_binding,
                'newDeployment': target_deployment,
                'originalBootSha256': old_sha, 'newBootSha256': new_sha,
                'recoveryManifestSha256': manifest_sha,
                'packageDescriptorSha256': document_sha,
            }
            if switch_plan is not None:
                plan['armadaBootSwitch'] = switch_plan
            plan_sha = _sha256(system_transaction._save_json(work, 'plan.json', plan))
            prepared = {
                'schemaVersion': 1, 'operation': 'offline-upgrade',
                'policy': 'offline-dual', 'transactionId': tx,
                'plan': 'plan.json', 'planSha256': plan_sha,
                'trust': 'trust.json', 'materials': 'materials.json',
                'candidates': {'a': 'candidate-a.json', 'b': 'candidate-b.json'},
                'recoveryManifest': 'recovery-manifest.json',
                'recoveryManifestSha256': manifest_sha,
                'packageDescriptor': 'package.json', 'packageSignature': 'package.sig',
                'packageDescriptorSha256': document_sha,
            }
            system_transaction._save_json(work, 'prepared.json', prepared)

            if not physical_slots.same_physical_identity(inspect_slots(), initial):
                raise ValueError('physical slots changed before preparation completed')
            final_journal = (_read_recovery_for_disk(initial) if disk else
                             system_transaction._check_recovery(initial, old))
            if (final_journal.header != journal.header or
                    final_journal.region != journal.region):
                raise ValueError('recovery journal changed during preparation')
            status.update(
                phase='awaiting_recovery_export', state='awaiting_recovery_export',
                recoveryManifest='recovery-manifest.json',
                recoveryManifestSha256=manifest_sha,
                recoveryManifestBytes=len(manifest_bytes), planSha256=plan_sha)
            system_transaction._save_json(work, 'status.json', status)
            return status
        except Exception as exc:
            status.update(failedPhase=status.get('phase'), phase='failed',
                          state='failed', error=str(exc))
            persisted = False
            if created:
                try:
                    system_transaction._save_json(work, 'status.json', status)
                    persisted = True
                except Exception as save_exc:
                    status['persistenceError'] = str(save_exc)
            if created_generation is not None and (persisted or not created):
                try:
                    if created_generation.is_dir():
                        shutil.rmtree(created_generation)
                        _io.fsync_dir(created_generation.parent, nofollow=True)
                except OSError as cleanup_exc:
                    status.setdefault('supplementalErrors', []).append(
                        'generation cleanup failed: %s' % (cleanup_exc,))
            raise system_transaction.SystemTransactionError(str(exc), status) from exc
