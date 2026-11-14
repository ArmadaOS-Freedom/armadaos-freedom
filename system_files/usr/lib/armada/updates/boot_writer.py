"""Write a prepared candidate to the inactive native boot partition only."""

import hashlib
import os
import stat
import uuid

from . import boot_sources, physical_slots
from .boot_candidate import _require_positive_int, _verify_claim
from .boot_lock import read_boot_lock
from .boot_sources import _load_locator, _locate, _rehash_region
from .boot_trust import _normalize_snapshot
from .deployment_store import _parse_binding

_MAX_BUFFER = 256 * 1024 * 1024
_CHUNK = 1024 * 1024
_MODES = ('aosp-resign', 'gbl-preserve-metadata')
_SLOT_KEYS = {
    'boot': 'boot', 'vendorBoot': 'vendor_boot', 'initBoot': 'init_boot',
    'abl': 'abl', 'xblConfig': 'xbl_config', 'vbmeta': 'vbmeta',
}
_HEX64 = frozenset('0123456789abcdef')


class BootWriteError(RuntimeError):
    def __init__(self, message, restore_status='not_needed'):
        super().__init__(message)
        self.restore_status = restore_status


def _hex64(value):
    if not isinstance(value, str) or len(value) != 64:
        return None
    normalized = value.lower()
    if any(ch not in _HEX64 for ch in normalized):
        return None
    return normalized


def _require_matching_metadata_hash(slot, report_sha256, trusted_sha256):
    """Bind gbl-preserve-metadata candidates to the trusted target metadata.

    Mirrors the offline writer rule: the candidate's metadata digest must be a
    valid 64-hex value and equal the authenticated per-target value before any
    partition writer is opened.
    """
    trusted = _hex64(trusted_sha256)
    if trusted is None:
        raise BootWriteError(f'authenticated metadataSha256 is invalid for slot {slot}')
    candidate = _hex64(report_sha256)
    if candidate is None:
        raise BootWriteError(f'candidate metadataSha256 is missing or invalid for slot {slot}')
    if candidate != trusted:
        raise BootWriteError(f'candidate metadataSha256 mismatch for slot {slot}')


def _projection(found):
    result = []
    for lun in sorted(found):
        item = {key: found[lun][key] for key in
                ('lun', 'path', 'rdev', 'blockSize', 'blockCount', 'bootId')}
        item['path'] = str(item['path'])
        result.append(item)
    return result


def _inventory(initial, recovery_rdev):
    anchor, found = physical_slots._enumerate_luns(recovery_rdev)
    if str(anchor) != initial['ufsController'] or _projection(found) != initial['luns']:
        raise BootWriteError('UFS inventory does not match captured physical report')
    return anchor, found


def _read_private(claim):
    claim = _verify_claim(claim, 'boot write input')
    if claim['bytes'] > _MAX_BUFFER:
        raise BootWriteError('boot write input exceeds 256 MiB')
    fd = os.open(claim['path'], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != claim['bytes']:
            raise BootWriteError('boot write input is not the claimed regular file')
        chunks = []
        remaining = claim['bytes']
        while remaining:
            chunk = os.read(fd, min(_CHUNK, remaining))
            if not chunk:
                raise BootWriteError('short read loading private boot buffer')
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
        if len(data) != claim['bytes'] or hashlib.sha256(data).hexdigest() != claim['sha256']:
            raise BootWriteError('private boot buffer failed digest verification')
        return data
    finally:
        os.close(fd)


def _pwrite_all(fd, data, offset):
    done = 0
    while done < len(data):
        try:
            put = os.pwrite(fd, data[done:done + _CHUNK], offset + done)
        except InterruptedError:
            raise BootWriteError('interrupted device write; I/O outcome is uncertain')
        if put <= 0 or put > min(_CHUNK, len(data) - done):
            raise BootWriteError('invalid partial device write; I/O outcome is uncertain')
        done += put


def _check_context(initial, lock_state, recovery):
    if not physical_slots.same_physical_identity(
            physical_slots.inspect_physical_slots(), initial):
        raise BootWriteError('physical slot report drifted')
    if read_boot_lock() != lock_state:
        raise BootWriteError('boot-lock state drifted')
    if physical_slots.locate_recovery_partition(require_edl=False) != recovery:
        raise BootWriteError('recovery partition identity drifted')
    _inventory(initial, recovery[3])


def write_inactive_boot(trust_report, candidate_report):
    """Write only the captured inactive boot extent; caller holds the global lock."""
    fds = []
    rw_fd = -1
    write_started = False
    original = None
    target_info = None
    try:
        if not isinstance(trust_report, dict) \
                or type(trust_report.get('schemaVersion')) is not int \
                or trust_report.get('schemaVersion') != 1 \
                or trust_report.get('phase') != 'boot_trust_checked':
            raise BootWriteError('invalid boot trust report header')
        if not isinstance(candidate_report, dict) \
                or type(candidate_report.get('schemaVersion')) is not int \
                or candidate_report.get('schemaVersion') != 1 \
                or candidate_report.get('phase') != 'candidate_prepared':
            raise BootWriteError('invalid candidate report header')
        initial = trust_report['physical']
        actual, inactive = initial['actualSlot'], initial['inactiveSlot']
        if actual not in ('a', 'b') or inactive != ('b' if actual == 'a' else 'a'):
            raise BootWriteError('invalid physical active/inactive slots')
        target = candidate_report['slot']
        mode = candidate_report['mode']
        if target != inactive or initial['model'] not in ('TB322FC', 'TB323FU') \
                or candidate_report['model'] != initial['model'] \
                or mode not in _MODES or mode != trust_report['mode']:
            raise BootWriteError('candidate target, model, or mode mismatch')
        if mode == 'aosp-resign' and candidate_report.get('payloadDigestMatchesSignedMetadata') is not True:
            raise BootWriteError('candidate payload digest does not match signed metadata')
        extras = ['recovery_a', 'recovery_b', 'vbmeta_system_a', 'vbmeta_system_b']
        if initial['model'] == 'TB323FU':
            extras.append('efisp')
        snapshot = _normalize_snapshot(trust_report['sources'], initial, extras)
        trusted = trust_report['trust'][target]
        if mode == 'gbl-preserve-metadata':
            _require_matching_metadata_hash(
                target, candidate_report.get('metadataSha256'),
                trusted.get('metadataSha256'))
        material = trusted['material']
        _require_positive_int(candidate_report['maxRawBytes'], 'candidate.maxRawBytes')
        if candidate_report['maxRawBytes'] > trusted['maxRawBytes']:
            raise BootWriteError('candidate raw-byte budget exceeds trusted target budget')
        for key in ('baseImageSha256',):
            if candidate_report[key].lower() != material[key].lower():
                raise BootWriteError(f'candidate {key} mismatch')
        for key in ('rootPublicKeySha256', 'bootPublicKeySha256'):
            if candidate_report[key].lower() != trusted[key].lower():
                raise BootWriteError(f'candidate {key} mismatch')
        transaction = str(uuid.UUID(candidate_report['transactionUuid']))
        if transaction != candidate_report['transactionUuid'] or int(transaction.replace('-', ''), 16) == 0:
            raise BootWriteError('transactionUuid must be canonical and nonzero')
        deployment = candidate_report['deployment']
        if not isinstance(deployment, str) or tuple(_parse_binding(deployment)) == tuple(_parse_binding(initial['deployment'])):
            raise BootWriteError('candidate deployment is invalid or not inactive')
        boot_claim = snapshot['slots'][target]['boot']
        if candidate_report['sourceBootSha256'].lower() != boot_claim['sha256'].lower():
            raise BootWriteError('candidate source digest is not captured inactive boot')
        candidate = _verify_claim(candidate_report['candidate'], 'candidate')
        original_claim = _verify_claim(boot_claim, 'original inactive boot')
        if (not physical_slots.same_physical_identity(
                physical_slots.inspect_physical_slots(), initial)
                or read_boot_lock() != trust_report['lockState']):
            raise BootWriteError('physical slot or lock state changed since trust capture')
        recovery = physical_slots.locate_recovery_partition(require_edl=False)
        dev, part_uuid, board, recovery_rdev = recovery
        if physical_slots._MODEL.get(board) != initial['model'] or part_uuid != initial['recoveryPartUuid']:
            raise BootWriteError('recovery board, model, or UUID mismatch')
        _, found = _inventory(initial, recovery_rdev)
        luns = sorted(found)
        for lun in luns:
            fds.append(physical_slots._device_fd(found[lun]['path'], found[lun]['rdev']))
        locator = _load_locator()
        enabled = found[luns[0]]['enabled']
        claims = {f'{name}_{slot}': snapshot['slots'][slot][key]
                  for slot in ('a', 'b') for key, name in _SLOT_KEYS.items()}
        claims.update(snapshot['extraFiles'])

        def verify_extents():
            extents = {}
            for label, claim in claims.items():
                info = _locate(locator, fds, luns, found, enabled, label)
                captured = snapshot['partitions'][label]
                identity = (info['lun'], info['firstLba'], info['blockCount'],
                            boot_sources._uuid_text(info['uuid']))
                expected = (captured['lun'], captured['firstLba'], captured['blockCount'],
                            captured['partitionUuid'])
                if identity != expected or info['bytes'] != claim['bytes']:
                    raise BootWriteError(f'native extent mismatch for {label}')
                expected_sha = candidate['sha256'] if write_started and label == f'boot_{target}' else claim['sha256']
                if _rehash_region(info['fd'], info['firstLba'] * info['blockSize'],
                                  info['bytes']) != expected_sha:
                    raise BootWriteError(f'live source digest mismatch for {label}')
                extents[label] = info
            return extents

        target_info = verify_extents()[f'boot_{target}']
        if original_claim['bytes'] != target_info['bytes'] or candidate['bytes'] != target_info['bytes']:
            raise BootWriteError('candidate or original size differs from live boot extent')
        candidate_data = _read_private(candidate)
        original = _read_private(original_claim)
        lun = target_info['lun']
        rw_fd = os.open(found[lun]['path'], os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        info = os.fstat(rw_fd)
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != found[lun]['rdev']:
            raise BootWriteError('target LUN read-write fd identity mismatch')
        _check_context(initial, trust_report['lockState'], recovery)
        target_info = verify_extents()[f'boot_{target}']
        offset = target_info['firstLba'] * target_info['blockSize']
        write_started = True
        _pwrite_all(rw_fd, candidate_data, offset)
        os.fsync(rw_fd)
        readback = _rehash_region(rw_fd, offset, len(candidate_data))
        if readback != candidate['sha256']:
            raise BootWriteError('inactive boot readback digest mismatch')
        verify_extents()
        _check_context(initial, trust_report['lockState'], recovery)
        return {
            'schemaVersion': 1, 'phase': 'inactive_boot_written',
            'model': initial['model'], 'fromSlot': actual, 'targetSlot': target,
            'transactionUuid': transaction, 'deployment': deployment,
            'candidate': candidate, 'sourceBootSha256': original_claim['sha256'],
            'bytesWritten': len(candidate_data), 'readbackSha256': readback,
            'physical': initial,
        }
    except Exception as error:
        if not write_started:
            if isinstance(error, BootWriteError) and error.restore_status == 'not_needed':
                raise
            raise BootWriteError(f'inactive boot write failed before writing: {error}') from error
        try:
            _pwrite_all(rw_fd, original, target_info['firstLba'] * target_info['blockSize'])
            os.fsync(rw_fd)
            restored = _rehash_region(rw_fd, target_info['firstLba'] * target_info['blockSize'], len(original))
            if restored != hashlib.sha256(original).hexdigest():
                raise BootWriteError('restore readback digest mismatch')
        except Exception as restore_error:
            raise BootWriteError(
                f'{error}; inactive boot restore failed ({restore_error}); device writes are uncertain',
                'failed') from error
        raise BootWriteError(f'{error}; inactive boot restored', 'restored') from error
    finally:
        if rw_fd >= 0:
            try:
                os.close(rw_fd)
            except OSError:
                pass
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
