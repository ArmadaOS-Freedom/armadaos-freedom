"""Write prepared boot candidates to both native slots for offline upgrades."""

import copy
import hashlib
import os
import stat
import uuid

from . import boot_sources, physical_slots
from .boot_candidate import _require_positive_int, _verify_claim
from .boot_lock import read_boot_lock
from .boot_sources import _load_locator, _locate, _rehash_region
from .boot_trust import _normalize_snapshot
from .boot_writer import (
    BootWriteError,
    _MODES,
    _SLOT_KEYS,
    _check_context,
    _inventory,
    _pwrite_all,
    _read_private,
)
from .deployment_store import _parse_binding

_SLOTS = ('a', 'b')
_HEX64 = frozenset('0123456789abcdef')


def _hex64(value):
    if not isinstance(value, str) or len(value) != 64:
        return None
    normalized = value.lower()
    if any(ch not in _HEX64 for ch in normalized):
        return None
    return normalized


def _require_matching_metadata_hash(slot, report_sha256, trusted_sha256):
    trusted = _hex64(trusted_sha256)
    if trusted is None:
        raise BootWriteError(f'authenticated metadataSha256 is invalid for slot {slot}')
    candidate = _hex64(report_sha256)
    if candidate is None:
        raise BootWriteError(f'candidate metadataSha256 is missing or invalid for slot {slot}')
    if candidate != trusted:
        raise BootWriteError(f'candidate metadataSha256 mismatch for slot {slot}')


def _error_with_results(message, restore_status, slot_results):
    err = BootWriteError(message, restore_status)
    err.slot_results = copy.deepcopy(slot_results)
    return err


def _validate_candidate_report(slot, report, trust_report, snapshot, initial, *, current_binding):
    if not isinstance(report, dict) \
            or type(report.get('schemaVersion')) is not int \
            or report.get('schemaVersion') != 1 \
            or report.get('phase') != 'candidate_prepared':
        raise BootWriteError(f'invalid candidate report header for slot {slot}')
    mode = report['mode']
    if report['slot'] != slot \
            or report['model'] != initial['model'] \
            or mode not in _MODES \
            or mode != trust_report['mode']:
        raise BootWriteError(f'candidate slot, model, or mode mismatch for slot {slot}')
    if mode == 'aosp-resign' and report.get('payloadDigestMatchesSignedMetadata') is not True:
        raise BootWriteError(f'candidate payload digest does not match signed metadata for slot {slot}')
    trusted = trust_report['trust'][slot]
    if mode == 'gbl-preserve-metadata':
        _require_matching_metadata_hash(
            slot, report.get('metadataSha256'), trusted.get('metadataSha256'))
    material = trusted['material']
    _require_positive_int(report['maxRawBytes'], f'candidates.{slot}.maxRawBytes')
    if report['maxRawBytes'] > trusted['maxRawBytes']:
        raise BootWriteError(f'candidate raw-byte budget exceeds trusted budget for slot {slot}')
    for key in ('baseImageSha256',):
        if report[key].lower() != material[key].lower():
            raise BootWriteError(f'candidate {key} mismatch for slot {slot}')
    for key in ('rootPublicKeySha256', 'bootPublicKeySha256'):
        if report[key].lower() != trusted[key].lower():
            raise BootWriteError(f'candidate {key} mismatch for slot {slot}')
    transaction = str(uuid.UUID(report['transactionUuid']))
    if transaction != report['transactionUuid'] or int(transaction.replace('-', ''), 16) == 0:
        raise BootWriteError('transactionUuid must be canonical and nonzero')
    deployment = report['deployment']
    if not isinstance(deployment, str) \
            or tuple(_parse_binding(deployment)) == tuple(_parse_binding(current_binding)):
        raise BootWriteError('candidate deployment is invalid or matches currently booted deployment')
    boot_claim = snapshot['slots'][slot]['boot']
    if report['sourceBootSha256'].lower() != boot_claim['sha256'].lower():
        raise BootWriteError(f'candidate source digest is not captured boot_{slot}')
    candidate_claim = _verify_claim(report['candidate'], f'candidate {slot}')
    original_claim = _verify_claim(boot_claim, f'original boot_{slot}')
    return {
        'transactionUuid': transaction,
        'deployment': deployment,
        'mode': mode,
        'candidateClaim': candidate_claim,
        'originalClaim': original_claim,
    }


def write_offline_boots(trust_report, candidates, *, progress=None):
    """Write inactive then current native boot extents; caller holds transaction/global lock."""
    return _write_offline_boots(trust_report, candidates, progress=progress)


def _write_offline_boots(trust_report, candidates, *, progress):
    ro_fds = []
    rw_fds = {}
    started_slots = []
    slot_results = {}
    expected_boot_sha = {}
    plan = {}
    target_extents = {}
    try:
        if progress is not None and not callable(progress):
            raise BootWriteError('progress must be callable or None')
        expected_phase = 'boot_trust_checked'
        if not isinstance(trust_report, dict) \
                or type(trust_report.get('schemaVersion')) is not int \
                or trust_report.get('schemaVersion') != 1 \
                or trust_report.get('phase') != expected_phase:
            raise BootWriteError('invalid boot trust report header')
        if not isinstance(candidates, dict) or set(candidates.keys()) != {'a', 'b'}:
            raise BootWriteError('candidates must map exact keys a and b')
        initial = trust_report['physical']
        actual, inactive = initial['actualSlot'], initial['inactiveSlot']
        if actual not in _SLOTS or inactive != ('b' if actual == 'a' else 'a'):
            raise BootWriteError('invalid physical active/inactive slots')
        if initial['model'] not in ('TB322FC', 'TB323FU'):
            raise BootWriteError('unsupported physical model for offline dual-boot write')
        extras = ['recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b']
        if initial['model'] == 'TB323FU':
            extras.append('efisp')
        snapshot = _normalize_snapshot(trust_report['sources'], initial, extras)
        current_binding = initial['deployment']
        if not isinstance(trust_report.get('trust'), dict) \
                or not {'a', 'b'}.issubset(trust_report['trust'].keys()):
            raise BootWriteError('boot trust report missing slot trust entries')

        for slot in _SLOTS:
            plan[slot] = _validate_candidate_report(
                slot, candidates[slot], trust_report, snapshot, initial,
                current_binding=current_binding)
            expected_boot_sha[slot] = plan[slot]['originalClaim']['sha256']
            slot_results[slot] = {
                'slot': slot,
                'status': 'not_started',
                'sourceBootSha256': plan[slot]['originalClaim']['sha256'],
                'candidate': copy.deepcopy(plan[slot]['candidateClaim']),
            }

        if plan['a']['mode'] != plan['b']['mode'] \
                or plan['a']['transactionUuid'] != plan['b']['transactionUuid'] \
                or tuple(_parse_binding(plan['a']['deployment'])) != tuple(_parse_binding(plan['b']['deployment'])) \
                or plan['a']['deployment'] != plan['b']['deployment']:
            raise BootWriteError('candidate reports disagree on mode, transactionUuid, or deployment')
        transaction = plan['a']['transactionUuid']
        deployment = plan['a']['deployment']
        slot_order = [inactive, actual]

        fresh_physical = physical_slots.inspect_physical_slots()
        if (not physical_slots.same_physical_identity(fresh_physical, initial)
                or read_boot_lock() != trust_report['lockState']):
            raise BootWriteError('physical slot or lock state changed since trust capture')
        recovery = physical_slots.locate_recovery_partition(require_edl=False)
        _, part_uuid, board, recovery_rdev = recovery
        if physical_slots._MODEL.get(board) != initial['model'] \
                or part_uuid != initial['recoveryPartUuid']:
            raise BootWriteError('recovery board, model, or UUID mismatch')
        _, found = _inventory(initial, recovery_rdev)

        def check_context():
            _check_context(initial, trust_report['lockState'], recovery)

        luns = sorted(found)
        for lun in luns:
            ro_fds.append(physical_slots._device_fd(found[lun]['path'], found[lun]['rdev']))
        locator = _load_locator()
        enabled = found[luns[0]]['enabled']
        claims = {f'{name}_{slot}': snapshot['slots'][slot][key]
                  for slot in _SLOTS for key, name in _SLOT_KEYS.items()}
        claims.update(snapshot['extraFiles'])

        def verify_extents():
            extents = {}
            for label, claim in claims.items():
                info = _locate(locator, ro_fds, luns, found, enabled, label)
                captured = snapshot['partitions'][label]
                identity = (info['lun'], info['firstLba'], info['blockCount'],
                            boot_sources._uuid_text(info['uuid']))
                expected = (captured['lun'], captured['firstLba'], captured['blockCount'],
                            captured['partitionUuid'])
                if identity != expected or info['bytes'] != claim['bytes']:
                    raise BootWriteError(f'native extent mismatch for {label}')
                if label == 'boot_a':
                    exp_sha = expected_boot_sha['a']
                elif label == 'boot_b':
                    exp_sha = expected_boot_sha['b']
                else:
                    exp_sha = claim['sha256']
                if _rehash_region(info['fd'], info['firstLba'] * info['blockSize'],
                                  info['bytes']) != exp_sha:
                    raise BootWriteError(f'live source digest mismatch for {label}')
                extents[label] = info
            return extents

        extents = verify_extents()
        for slot in _SLOTS:
            t_info = extents[f'boot_{slot}']
            if plan[slot]['originalClaim']['bytes'] != t_info['bytes'] \
                    or plan[slot]['candidateClaim']['bytes'] != t_info['bytes']:
                raise BootWriteError(f'candidate or original size differs from live boot_{slot} extent')
            plan[slot]['candidateData'] = _read_private(plan[slot]['candidateClaim'])
            plan[slot]['originalData'] = _read_private(plan[slot]['originalClaim'])
            target_extents[slot] = t_info

        for slot in slot_order:
            lun = target_extents[slot]['lun']
            if lun not in rw_fds:
                fd = os.open(
                    found[lun]['path'],
                    os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                )
                rw_fds[lun] = fd
                st = os.fstat(fd)
                if not stat.S_ISBLK(st.st_mode) or st.st_rdev != found[lun]['rdev']:
                    raise BootWriteError(f'target LUN {lun} read-write fd identity mismatch')

        writes = {}
        for slot in slot_order:
            check_context()
            t_info = verify_extents()[f'boot_{slot}']
            target_extents[slot] = t_info
            offset = t_info['firstLba'] * t_info['blockSize']
            cand_claim = plan[slot]['candidateClaim']
            cand_data = plan[slot]['candidateData']
            orig_claim = plan[slot]['originalClaim']
            rw_fd = rw_fds[t_info['lun']]

            if progress is not None:
                progress({
                    'phase': 'writing',
                    'slot': slot,
                    'slotOrder': list(slot_order),
                    'transactionUuid': transaction,
                    'deployment': deployment,
                    'sourceBootSha256': orig_claim['sha256'],
                    'candidate': copy.deepcopy(cand_claim),
                })

            started_slots.append(slot)
            slot_results[slot]['status'] = 'writing'
            _pwrite_all(rw_fd, cand_data, offset)
            os.fsync(rw_fd)
            readback = _rehash_region(rw_fd, offset, len(cand_data))
            if readback != cand_claim['sha256']:
                raise BootWriteError(f'boot_{slot} readback digest mismatch')
            expected_boot_sha[slot] = cand_claim['sha256']
            verify_extents()
            check_context()

            evidence = {
                'sourceBootSha256': orig_claim['sha256'],
                'candidate': copy.deepcopy(cand_claim),
                'bytesWritten': len(cand_data),
                'readbackSha256': readback,
            }
            writes[slot] = evidence
            slot_results[slot] = {
                'slot': slot,
                'status': 'verified',
                **copy.deepcopy(evidence),
            }
            if progress is not None:
                progress({
                    'phase': 'verified',
                    'slot': slot,
                    'slotOrder': list(slot_order),
                    'transactionUuid': transaction,
                    'deployment': deployment,
                    'evidence': copy.deepcopy(evidence),
                })

        result = {
            'schemaVersion': 1,
            'phase': 'dual_boots_written',
            'model': initial['model'],
            'fromSlot': actual,
            'slotOrder': slot_order,
            'transactionUuid': transaction,
            'deployment': deployment,
            'physical': initial,
            'writes': writes,
        }
        return result
    except Exception as error:
        if not started_slots:
            if isinstance(error, BootWriteError) and error.restore_status == 'not_needed':
                error.slot_results = copy.deepcopy(slot_results)
                raise
            raise _error_with_results(
                f'offline dual-boot write failed before writing: {error}',
                'not_needed',
                slot_results,
            ) from error

        restore_failures = []
        for slot in reversed(started_slots):
            t_info = target_extents[slot]
            offset = t_info['firstLba'] * t_info['blockSize']
            orig_data = plan[slot]['originalData']
            orig_sha = hashlib.sha256(orig_data).hexdigest()
            rw_fd = rw_fds[t_info['lun']]
            try:
                _pwrite_all(rw_fd, orig_data, offset)
                os.fsync(rw_fd)
                restored_sha = _rehash_region(rw_fd, offset, len(orig_data))
                if restored_sha != orig_sha:
                    raise BootWriteError(f'boot_{slot} restore readback digest mismatch')
                slot_results[slot]['status'] = 'restored'
                slot_results[slot]['restoredSha256'] = restored_sha
            except Exception as restore_error:
                slot_results[slot]['status'] = 'restore_failed'
                slot_results[slot]['restoreError'] = str(restore_error)
                restore_failures.append(f'{slot}: {restore_error}')

        if restore_failures:
            detail = '; '.join(restore_failures)
            raise _error_with_results(
                f'{error}; offline boot restore failed ({detail}); device writes are uncertain',
                'failed',
                slot_results,
            ) from error
        raise _error_with_results(
            f'{error}; started boot slots restored',
            'restored',
            slot_results,
        ) from error
    finally:
        for fd in rw_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        for fd in ro_fds:
            try:
                os.close(fd)
            except OSError:
                pass
