# SPDX-License-Identifier: GPL-3.0-only
"""Prepare an authenticated offline dual-slot update without writing boot state."""

from __future__ import annotations

import hashlib
from pathlib import Path
import uuid

import armada_os
from . import (
    boot_candidate,
    boot_materials,
    boot_trust,
    deployment_store,
    image_source,
    offline_context,
    offline_payload,
    offline_recovery,
    package_contract,
    physical_slots,
    recovery_device,
    signature,
    system_transaction,
    trial_confirmation,
)

_CHANNELS = frozenset({'stable', 'beta', 'testing'})
_JOURNAL_BYTES = 64 * 1024


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _check_recovery(initial, old):
    """Journal precheck for OFFLINE-DUAL.

    Strict systems keep the online rules untouched.  A legacy system may
    only start from a validated v1/v2 descriptor with an empty journal; the
    descriptor itself is migrated later, in execute, immediately before the
    record write.  An existing 0.8 record names slot proof that a
    controller-only boot cannot supply, so it is refused instead of guessed.
    """
    if initial.get('bootContext') != physical_slots.LEGACY_BOOT_CONTEXT:
        return system_transaction._check_recovery(initial, old)
    try:
        with recovery_device.open_recovery(writable=False) as (fd, part_uuid, board):
            if part_uuid != initial['recoveryPartUuid']:
                raise ValueError('recovery partition changed')
            if physical_slots._MODEL.get(board) != initial['model']:
                raise ValueError('recovery board changed')
            snapshot = offline_recovery.load(fd, part_uuid, board)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f'cannot read the legacy recovery journal: {error}') from error
    if snapshot.record is not None:
        raise ValueError(
            'legacy boot context cannot bind the existing trial record to an actual slot')
    return snapshot


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


def prepare_offline_update(*, document, signature_b64, payload_directory,
                           expected_channel, sysroot_path, work_dir,
                           updater_version='0.8.0',
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
                            updater_version='0.8.0',
                            boot_tool_path=boot_candidate._DEFAULT_BOOT_TOOL_PATH,
                            dtb_helper_path=Path('/usr/libexec/armada/kshim-build-boot.py'),
                            trusted_key=signature.DEFAULT_TRUSTED_KEY):
    status = {'component': 'system', 'operation': 'offline-upgrade',
              'policy': 'offline-dual', 'schemaVersion': 1,
              'phase': 'preparing', 'state': 'preparing',
              'bootWriteState': 'not_started', 'requiresReboot': False}
    work = None
    created = False
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
            inspect_slots = physical_slots.inspect_offline_physical_slots

            initial = inspect_slots()
            if initial['model'] not in ('TB322FC', 'TB323FU'):
                raise ValueError('unsupported physical model')
            # Booted identity comes from the default runtime sysroot; `root`
            # is the image sysroot this transaction writes, not the libostree
            # sysroot that reports the booted deployment.
            snapshot = offline_context.snapshot()
            if (snapshot['repository'] not in armada_os.REPOSITORIES or
                    snapshot['staged'] or snapshot['rollback_queued']):
                raise ValueError('legacy OSTree transaction conflict')
            if system_transaction._snapshot_channel(snapshot) != expected_channel:
                raise ValueError('requested channel differs from configured channel')
            # OFFLINE-DUAL reference/other pair: the controller's selected slot is
            # the reference and the other slot is written first.  A legacy report
            # never claims an actual/inactive slot, so the pair is recorded
            # explicitly rather than reusing witness names.
            reference_slot, other_slot = physical_slots.offline_slot_pair(initial)
            current_binding = initial['deployment']
            old = system_transaction._binding(current_binding)
            journal = _check_recovery(initial, old)

            package = package_contract.load_package(
                document, signature_b64, trusted_key=trusted_key)
            package_contract.require_operation(
                package, initial['model'], 'offline-upgrade',
                updater_version=updater_version)
            status.update(model=initial['model'], fromSlot=reference_slot,
                          targetSlot=other_slot,
                          oldDeployment=current_binding)
            if initial.get('bootContext') == physical_slots.LEGACY_BOOT_CONTEXT:
                status.update(bootContext=initial['bootContext'],
                              selectedSlot=initial['selectedSlot'])
            if snapshot.get('channelInferred'):
                status['channelInferred'] = True
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
            if inspect_slots() != initial:
                raise ValueError('physical slots changed after offline import')

            status['phase'] = 'capturing_boot_trust'
            system_transaction._save_json(work, 'status.json', status)
            capture_kwargs = {}
            if initial.get('bootContext') == physical_slots.LEGACY_BOOT_CONTEXT:
                # Only the offline path may inspect a controller-only system;
                # the online default inspector stays strict.
                capture_kwargs['inspector'] = inspect_slots
            trust = boot_trust.capture_boot_trust(
                output_dir=work / 'trust', boot_tool_path=boot_tool_path,
                **capture_kwargs)
            if (trust['physical'] != initial or
                    trust['sources']['physical'] != initial):
                raise ValueError('boot trust physical report mismatch')

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

            sources = trust['sources']
            manifest = {
                'schemaVersion': 1, 'operation': 'offline-upgrade',
                'transactionId': tx, 'model': initial['model'],
                'installationUuid': str(uuid.UUID(bytes=journal.layout.installation_uuid)),
                'recoveryPartUuid': str(uuid.UUID(bytes=journal.layout.partition_uuid)),
                'board': journal.layout.board,
                'oldDeployment': current_binding,
                'fromSlot': reference_slot, 'physical': initial,
                'bootContext': initial.get('bootContext'),
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
                'fromSlot': reference_slot, 'targetSlot': other_slot,
                'selectedSlot': reference_slot,
                'bootContext': initial.get('bootContext'),
                'oldDeployment': current_binding,
                'newDeployment': target_deployment,
                'originalBootSha256': old_sha, 'newBootSha256': new_sha,
                'recoveryManifestSha256': manifest_sha,
                'packageDescriptorSha256': document_sha,
            }
            if snapshot.get('channelInferred'):
                plan['channelInferred'] = True
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

            if inspect_slots() != initial:
                raise ValueError('physical slots changed before preparation completed')
            final_journal = _check_recovery(initial, old)
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
            if created:
                try:
                    system_transaction._save_json(work, 'status.json', status)
                except Exception as save_exc:
                    status['persistenceError'] = str(save_exc)
            raise system_transaction.SystemTransactionError(str(exc), status) from exc
