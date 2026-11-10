"""Read-only boot source snapshot from physical UFS A/B slots.

This module only performs current-source reads and file snapshots. It is
NOT an AVB trust, readiness, or write authorization step; a completed
snapshot is evidence of captured bytes, not of trustworthiness.
"""

import ctypes
import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path

from . import physical_slots
from .boot_candidate import BootCandidateError, _hash_regular_file, _write_new
from .boot_materials import _validate_output_dir
from .physical_slots import PhysicalSlotsError

_MAX_PARTITION_BYTES = 256 * 1024 * 1024
_FREE_SLACK_BYTES = 16 * 1024 * 1024
_CHUNK = 1024 * 1024
_SCHEMA_VERSION = 1
_PHASE = 'boot_sources_captured'

_REQUIRED_LABELS = (
    'boot_a', 'vendor_boot_a', 'init_boot_a', 'abl_a', 'xbl_config_a', 'vbmeta_a',
    'boot_b', 'vendor_boot_b', 'init_boot_b', 'abl_b', 'xbl_config_b', 'vbmeta_b',
)
_EXTRA_ALLOWED = frozenset((
    'recovery_a', 'recovery_b',
    'vbmeta_system_a', 'vbmeta_system_b',
    'dtbo_a', 'dtbo_b',
    'efisp',
))


class BootSourcesError(RuntimeError):
    """Raised when the read-only boot source snapshot cannot be completed."""


def _uuid_text(uuid_bytes):
    return str(uuid.UUID(bytes=bytes(uuid_bytes)))


def _pread(fd, size, offset):
    while True:
        try:
            return os.pread(fd, size, offset)
        except InterruptedError:
            continue


def _load_locator():
    try:
        lib = ctypes.CDLL(str(physical_slots.LIBRARY_PATH))
        fn = lib.armada_update_ab_partition_fds
    except OSError as error:
        raise BootSourcesError(
            f'cannot load {physical_slots.LIBRARY_PATH}: {error}') from error
    except AttributeError as error:
        raise BootSourcesError(
            f'missing armada_update_ab_partition_fds in {physical_slots.LIBRARY_PATH}') from error
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    return fn


def _locate(fn, fds, luns, found, enabled, label):
    fd_arr = (ctypes.c_int * 6)(*fds)
    bs_arr = (ctypes.c_uint32 * 6)(*(found[l]['blockSize'] for l in luns))
    bc_arr = (ctypes.c_uint64 * 6)(*(found[l]['blockCount'] for l in luns))
    bid_arr = (ctypes.c_uint8 * 6)(*(found[l]['bootId'] for l in luns))
    result = (ctypes.c_uint64 * 3)()
    uuid = (ctypes.c_uint8 * 16)()
    rc = fn(fd_arr, bs_arr, bc_arr, bid_arr, ctypes.c_uint32(enabled),
            label.encode('ascii'), result, uuid)
    if rc != 0:
        raise BootSourcesError(
            f'native locator failed for {label}: rc={rc} result={list(result)}')
    lun, first_lba, block_count = (int(v) for v in result)
    if lun not in physical_slots._LUN_IDS:
        raise BootSourcesError(f'{label}: invalid LUN index {lun}')
    item = found[lun]
    if block_count == 0 or first_lba > item['blockCount'] or \
            block_count > item['blockCount'] - first_lba:
        raise BootSourcesError(f'{label}: extent out of LUN{lun} bounds')
    size = block_count * item['blockSize']
    if size <= 0 or size > _MAX_PARTITION_BYTES:
        raise BootSourcesError(
            f'{label}: partition size {size} outside (0, {_MAX_PARTITION_BYTES}]')
    uuid_bytes = bytes(uuid)
    if uuid_bytes == b'\x00' * 16:
        raise BootSourcesError(f'{label}: partition UUID is all zero')
    return {
        'lun': lun,
        'firstLba': first_lba,
        'blockCount': block_count,
        'blockSize': item['blockSize'],
        'bytes': size,
        'uuid': uuid_bytes,
        'fd': fds[luns.index(lun)],
    }


def _stream_copy(fd, offset, size, dest):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    out_fd = -1
    try:
        out_fd = os.open(dest, flags, 0o600)
        digest = hashlib.sha256()
        done = 0
        while done < size:
            want = min(_CHUNK, size - done)
            got = _pread(fd, want, offset + done)
            if not got:
                raise BootSourcesError(
                    f'short source read copying {dest.name} at {offset + done}')
            written = 0
            while written < len(got):
                try:
                    put = os.write(out_fd, got[written:])
                except InterruptedError:
                    continue
                if put <= 0:
                    raise BootSourcesError(f'short write creating {dest}')
                written += put
            digest.update(got)
            done += len(got)
        os.fsync(out_fd)
        return digest.hexdigest()
    finally:
        if out_fd >= 0:
            try:
                os.close(out_fd)
            except OSError:
                pass


def _rehash_region(fd, offset, size):
    digest = hashlib.sha256()
    done = 0
    while done < size:
        chunk = _pread(fd, min(_CHUNK, size - done), offset + done)
        if not chunk:
            raise BootSourcesError(f'short read rehashing region at {offset + done}')
        digest.update(chunk)
        done += len(chunk)
    return digest.hexdigest()


def capture_boot_sources(expected_physical, output_dir, *, extra_labels=()):
    """Snapshot all boot source partitions read-only into output_dir.

    Caller must hold /run/lock/armada-update.lock for the full transaction.
    Returns the manifest dict; raises BootSourcesError on any failure.
    """
    return _capture_boot_sources(
        expected_physical, output_dir, extra_labels,
        inspector=physical_slots.inspect_physical_slots, phase=_PHASE)


def _capture_boot_sources(expected_physical, output_dir, extra_labels, *,
                          inspector, phase):
    if not isinstance(expected_physical, dict):
        raise BootSourcesError('expected_physical must be a dict')
    if not isinstance(extra_labels, (tuple, list)):
        raise BootSourcesError('extra_labels must be a tuple or list of strings')
    extras = tuple(extra_labels)
    for label in extras:
        if not isinstance(label, str) or label not in _EXTRA_ALLOWED:
            raise BootSourcesError(f'extra label not permitted: {label}')
    if len(set(extras)) != len(extras):
        raise BootSourcesError('duplicate extra labels requested')
    labels = list(_REQUIRED_LABELS) + list(extras)

    try:
        initial = inspector()
    except PhysicalSlotsError as error:
        raise BootSourcesError(f'physical slot precheck failed: {error}') from error
    if initial != expected_physical:
        raise BootSourcesError('physical slot report drifted from expected report')

    try:
        recovery = physical_slots.locate_recovery_partition(require_edl=False)
    except Exception as error:
        raise BootSourcesError(
            f'cannot locate recovery partition: {error}') from error
    dev, part_uuid, board, recovery_rdev = recovery
    expected_model = physical_slots._MODEL.get(board)
    if expected_model is None or initial.get('model') != expected_model:
        raise BootSourcesError(f'unsupported or mismatched board id {board}')
    if initial.get('recoveryPartUuid') != part_uuid:
        raise BootSourcesError('recovery UUID mismatch with physical report')

    try:
        anchor, found = physical_slots._enumerate_luns(recovery_rdev)
    except PhysicalSlotsError as error:
        raise BootSourcesError(f'cannot enumerate LUNs: {error}') from error
    if str(anchor) != initial.get('ufsController'):
        raise BootSourcesError('UFS controller anchor mismatch with physical report')
    projection = [
        {key: found[l][key] for key in
         ('lun', 'path', 'rdev', 'blockSize', 'blockCount', 'bootId')}
        for l in sorted(found)
    ]
    for entry in projection:
        entry['path'] = str(entry['path'])
    if projection != initial.get('luns'):
        raise BootSourcesError('LUN inventory does not match physical report')

    luns = sorted(found)
    enabled = found[luns[0]]['enabled']
    fds = []
    try:
        for lun in luns:
            fds.append(
                physical_slots._device_fd(found[lun]['path'], found[lun]['rdev']))
        locator = _load_locator()

        extents = {}
        seen_extents = set()
        for label in labels:
            info = _locate(locator, fds, luns, found, enabled, label)
            key = (info['lun'], info['firstLba'], info['blockCount'])
            if key in seen_extents:
                raise BootSourcesError(f'duplicate extent for {label}')
            seen_extents.add(key)
            extents[label] = info

        total = sum(info['bytes'] for info in extents.values())
        try:
            out = _validate_output_dir(output_dir, [])
        except Exception as error:
            raise BootSourcesError(
                f'invalid output directory {output_dir}: {error}') from error
        free = shutil.disk_usage(str(out.parent)).free
        if free < total + _FREE_SLACK_BYTES:
            raise BootSourcesError(
                f'insufficient free space: {free} < {total + _FREE_SLACK_BYTES}')
        try:
            os.mkdir(out, 0o700)
        except OSError as error:
            raise BootSourcesError(f'cannot create {out}: {error}') from error

        files = {}
        for label in labels:
            info = extents[label]
            dest = out / f'{label}.img'
            offset = info['firstLba'] * info['blockSize']
            sha = _stream_copy(info['fd'], offset, info['bytes'], dest)
            files[label] = {'dest': dest, 'sha256': sha, 'bytes': info['bytes']}

        # Verification pass: output files, source regions, native requery,
        # held fd identity, LUN inventory, and boot context.
        for label in labels:
            info = extents[label]
            entry = files[label]
            try:
                length, sha = _hash_regular_file(entry['dest'])
            except BootCandidateError as error:
                raise BootSourcesError(
                    f'rehash of {entry["dest"].name} failed: {error}') from error
            if length != info['bytes'] or sha != entry['sha256']:
                raise BootSourcesError(
                    f'output file {entry["dest"].name} failed rehash verification')
            offset = info['firstLba'] * info['blockSize']
            source_sha = _rehash_region(info['fd'], offset, info['bytes'])
            if source_sha != entry['sha256']:
                raise BootSourcesError(
                    f'source region for {label} changed during snapshot')
            re_info = _locate(locator, fds, luns, found, enabled, label)
            if (re_info['lun'], re_info['firstLba'], re_info['blockCount'],
                    re_info['uuid']) != (info['lun'], info['firstLba'],
                                         info['blockCount'], info['uuid']):
                raise BootSourcesError(
                    f'native locator drift for {label}')

        for lun, fd in zip(luns, fds):
            stat_info = os.fstat(fd)
            if not stat.S_ISBLK(stat_info.st_mode) or \
                    stat_info.st_rdev != found[lun]['rdev']:
                raise BootSourcesError(f'held fd identity drift on LUN{lun}')

        try:
            re_anchor, re_found = physical_slots._enumerate_luns(recovery_rdev)
        except PhysicalSlotsError as error:
            raise BootSourcesError(f're-enumeration failed: {error}') from error
        if (re_anchor, re_found) != (anchor, found):
            raise BootSourcesError('UFS LUN inventory drifted during snapshot')
        try:
            final = inspector()
        except PhysicalSlotsError as error:
            raise BootSourcesError(
                f'final physical slot precheck failed: {error}') from error
        if final != initial:
            raise BootSourcesError('physical slot report drifted during snapshot')
        try:
            re_recovery = physical_slots.locate_recovery_partition(require_edl=False)
        except Exception as error:
            raise BootSourcesError(
                f'cannot re-verify recovery partition: {error}') from error
        if re_recovery != recovery:
            raise BootSourcesError('recovery partition identity drifted')

        slots = {'a': {}, 'b': {}}
        extra_files = {}
        partitions = {}
        for label in labels:
            info = extents[label]
            entry = {
                'path': str(files[label]['dest']),
                'bytes': info['bytes'],
                'sha256': files[label]['sha256'],
            }
            if label in _EXTRA_ALLOWED:
                extra_files[label] = entry
            else:
                name, slot = label.rsplit('_', 1)
                slots[slot][name] = entry
            partitions[label] = {
                'lun': info['lun'],
                'firstLba': info['firstLba'],
                'blockCount': info['blockCount'],
                'partitionUuid': _uuid_text(info['uuid']),
            }
        for slot in ('a', 'b'):
            slots[slot]['xblConfig'] = slots[slot].pop('xbl_config')
            slots[slot]['vendorBoot'] = slots[slot].pop('vendor_boot')
            slots[slot]['initBoot'] = slots[slot].pop('init_boot')

        manifest = {
            'schemaVersion': _SCHEMA_VERSION,
            'phase': phase,
            'physical': initial,
            'slots': slots,
            'extraFiles': extra_files,
            'partitions': partitions,
        }
        manifest_path = out / 'source-snapshot.json'
        payload = json.dumps(
            manifest, sort_keys=True, separators=(',', ':')).encode('ascii')
        try:
            _write_new(manifest_path, payload)
        except Exception as error:
            raise BootSourcesError(
                f'cannot write {manifest_path}: {error}') from error
        dir_fd = -1
        try:
            dir_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fsync(dir_fd)
        except OSError as error:
            try:
                os.unlink(manifest_path)
            except OSError:
                pass
            raise BootSourcesError(f'cannot sync {out}: {error}') from error
        finally:
            if dir_fd >= 0:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
        return manifest
    except (PhysicalSlotsError, OSError) as error:
        raise BootSourcesError(f'{error}') from error
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
