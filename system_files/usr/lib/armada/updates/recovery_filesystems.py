"""Read-only discovery of existing ARMADA_BOOT, ARMADA_ROOT, and ARMADA_BACKUP on UFS LUN0.

This module performs read-only GPT, sysfs, block identity, and low-level superblock
verification for RAM recovery. It does not mount, write, repair, or select deployments.
"""

import ctypes
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

from . import physical_slots
from .boot_health import BootHealthError, _sysfs_block_node
from .boot_sources import BootSourcesError, _load_locator
from .physical_slots import (
    PhysicalSlotsError,
    _DEV_RE,
    _device_fd,
    _enumerate_luns,
    _sysfs_int,
    _sysfs_read,
)
from .recovery_device import locate_recovery_partition

_SCHEMA_VERSION = 1
_PHASE = 'recovery_filesystems_checked'
_EXPORT_MAX_BYTES = 16 * 1024
_EXPORT_KEY_RE = re.compile(r'^[A-Z0-9_]+$')

_BOOT_LABEL = 'ARMADA_BOOT'
_ROOT_LABEL = 'ARMADA_ROOT'
_BACKUP_LABEL = 'ARMADA_BACKUP'

_BOOT_BYTES = 1024 * 1024 * 1024
_MIN_ROOT_BYTES = 63 * 1024 * 1024 * 1024
_BACKUP_BYTES = 2 * 1024 * 1024 * 1024

_ROLE_SPECS = (
    ('boot', _BOOT_LABEL, 'ext4'),
    ('root', _ROOT_LABEL, 'btrfs'),
    ('backup', _BACKUP_LABEL, None),
)


class RecoveryFilesystemsError(RuntimeError):
    """Raised when recovery filesystem discovery or verification fails."""


def _canonical_uuid(value, description):
    if not isinstance(value, str):
        raise RecoveryFilesystemsError(f'{description} is not a string')
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise RecoveryFilesystemsError(f'invalid {description}: {value!r}') from error
    text = str(parsed)
    if value != text:
        raise RecoveryFilesystemsError(f'non-canonical {description}: {value!r}')
    if parsed.bytes == b'\x00' * 16:
        raise RecoveryFilesystemsError(f'{description} must not be all-zero')
    return text


def _uuid_from_bytes(raw_bytes, description):
    raw = bytes(raw_bytes)
    if len(raw) != 16:
        raise RecoveryFilesystemsError(f'{description} must be 16 bytes')
    if raw == b'\x00' * 16:
        raise RecoveryFilesystemsError(f'{description} is all zero')
    return _canonical_uuid(str(uuid.UUID(bytes=raw)), description)


def _validate_live_physical(live, expected_physical):
    if not isinstance(live, dict):
        raise RecoveryFilesystemsError('live physical report is not a dict')
    if live.get('schemaVersion') != 1 or live.get('phase') != 'recovery_slots_checked':
        raise RecoveryFilesystemsError('unexpected live physical slot report phase or schema')
    if 'deployment' in live or 'booted' in live:
        raise RecoveryFilesystemsError('recovery slot report must not contain deployment or booted state')
    if expected_physical is not None:
        if not isinstance(expected_physical, dict):
            raise RecoveryFilesystemsError('expected_physical must be a dict or None')
        if 'deployment' in expected_physical or 'booted' in expected_physical:
            raise RecoveryFilesystemsError(
                'expected_physical must not contain forged deployment or booted state'
            )
        if expected_physical != live:
            raise RecoveryFilesystemsError('expected_physical does not match live recovery slot report')


def _reconstruct_luns(found):
    return [
        {
            'lun': found[l]['lun'],
            'path': str(found[l]['path']),
            'rdev': found[l]['rdev'],
            'blockSize': found[l]['blockSize'],
            'blockCount': found[l]['blockCount'],
            'bootId': found[l]['bootId'],
        }
        for l in sorted(found)
    ]


def _verify_inventory_matches_physical(anchor, found, part_uuid, live):
    if str(anchor) != live.get('ufsController'):
        raise RecoveryFilesystemsError('UFS controller does not match physical report')
    if part_uuid != live.get('recoveryPartUuid'):
        raise RecoveryFilesystemsError('recovery PARTUUID does not match physical report')
    if _reconstruct_luns(found) != live.get('luns'):
        raise RecoveryFilesystemsError('UFS LUN inventory does not match physical report')
    luns = sorted(found)
    if luns != list(range(6)):
        raise RecoveryFilesystemsError(f'expected LUN0..5, got {luns}')
    enabled_set = {found[l]['enabled'] for l in luns}
    if len(enabled_set) != 1:
        raise RecoveryFilesystemsError('inconsistent boot_lun_enabled across LUN0..5')
    return next(iter(enabled_set))


def _locate_fixed_extent(fn, fds, luns, found, enabled, label):
    fd_arr = (ctypes.c_int * 6)(*fds)
    bs_arr = (ctypes.c_uint32 * 6)(*(found[l]['blockSize'] for l in luns))
    bc_arr = (ctypes.c_uint64 * 6)(*(found[l]['blockCount'] for l in luns))
    bid_arr = (ctypes.c_uint8 * 6)(*(found[l]['bootId'] for l in luns))
    result = (ctypes.c_uint64 * 3)()
    raw_uuid = (ctypes.c_uint8 * 16)()
    rc = fn(
        fd_arr,
        bs_arr,
        bc_arr,
        bid_arr,
        ctypes.c_uint32(enabled),
        label.encode('ascii'),
        result,
        raw_uuid,
    )
    if rc != 0:
        raise RecoveryFilesystemsError(
            f'native locator failed for {label}: rc={rc} result={list(result)}'
        )
    lun, first_lba, block_count = (int(v) for v in result)
    if lun != 0:
        raise RecoveryFilesystemsError(f'{label}: expected LUN0, got LUN{lun}')
    lun0 = found[0]
    block_size = lun0['blockSize']
    total_blocks = lun0['blockCount']
    if block_size not in (512, 4096) or block_size % 512 != 0:
        raise RecoveryFilesystemsError(f'{label}: invalid LUN0 logical block size {block_size}')
    if first_lba <= 0 or block_count <= 0:
        raise RecoveryFilesystemsError(f'{label}: non-positive extent ({first_lba}, {block_count})')
    if first_lba > total_blocks or block_count > total_blocks - first_lba:
        raise RecoveryFilesystemsError(f'{label}: extent out of LUN0 bounds')
    start_bytes = first_lba * block_size
    size_bytes = block_count * block_size
    if size_bytes <= 0:
        raise RecoveryFilesystemsError(f'{label}: non-positive byte size {size_bytes}')
    part_uuid = _uuid_from_bytes(raw_uuid, f'{label} PARTUUID')
    return {
        'label': label,
        'lun': 0,
        'firstLba': first_lba,
        'blockCount': block_count,
        'blockSize': block_size,
        'startBytes': start_bytes,
        'bytes': size_bytes,
        'partUuid': part_uuid,
    }


def _validate_layout(extents, recovery_part_uuid):
    boot_ext = extents['boot']
    root_ext = extents['root']
    backup_ext = extents['backup']

    if boot_ext['bytes'] != _BOOT_BYTES:
        raise RecoveryFilesystemsError(
            f'ARMADA_BOOT must be {_BOOT_BYTES} bytes, got {boot_ext["bytes"]}'
        )
    if root_ext['bytes'] < _MIN_ROOT_BYTES:
        raise RecoveryFilesystemsError(
            f'ARMADA_ROOT must be >= {_MIN_ROOT_BYTES} bytes, got {root_ext["bytes"]}'
        )
    if backup_ext['bytes'] != _BACKUP_BYTES:
        raise RecoveryFilesystemsError(
            f'ARMADA_BACKUP must be {_BACKUP_BYTES} bytes, got {backup_ext["bytes"]}'
        )

    if boot_ext['firstLba'] + boot_ext['blockCount'] != root_ext['firstLba']:
        raise RecoveryFilesystemsError('ARMADA_ROOT must immediately follow ARMADA_BOOT')
    if root_ext['firstLba'] + root_ext['blockCount'] != backup_ext['firstLba']:
        raise RecoveryFilesystemsError('ARMADA_BACKUP must immediately follow ARMADA_ROOT')

    part_uuids = {boot_ext['partUuid'], root_ext['partUuid'], backup_ext['partUuid']}
    if len(part_uuids) != 3:
        raise RecoveryFilesystemsError('boot, root, and backup PARTUUIDs must be distinct')
    if backup_ext['partUuid'] != recovery_part_uuid:
        raise RecoveryFilesystemsError(
            'ARMADA_BACKUP PARTUUID does not match locate_recovery_partition'
        )


def _enumerate_lun0_partitions(lun0_node):
    try:
        resolved_lun0 = lun0_node.resolve(strict=True)
        entries = sorted(resolved_lun0.iterdir(), key=lambda entry: entry.name)
    except OSError as error:
        raise RecoveryFilesystemsError(f'cannot iterate LUN0 sysfs node {lun0_node}: {error}') from error

    children = []
    seen_nodes = set()
    for entry in entries:
        try:
            if not (entry / 'partition').exists():
                continue
            if entry.is_symlink():
                raise RecoveryFilesystemsError(f'unexpected symlink child in LUN0 node: {entry}')
            if not entry.is_dir():
                continue
            node = entry.resolve(strict=True)
        except OSError as error:
            raise RecoveryFilesystemsError(f'cannot inspect LUN0 child {entry}: {error}') from error
        if node != entry or node.parent != resolved_lun0:
            raise RecoveryFilesystemsError(f'unexpected sysfs partition parentage at {entry}')
        if node in seen_nodes:
            raise RecoveryFilesystemsError(f'duplicate LUN0 partition node {node}')
        seen_nodes.add(node)
        part_no = _sysfs_int(node / 'partition')
        if part_no <= 0:
            raise RecoveryFilesystemsError(f'invalid partition number at {node}: {part_no}')
        start_512 = _sysfs_int(node / 'start')
        size_512 = _sysfs_int(node / 'size')
        dev_text = _sysfs_read(node / 'dev')
        dev_match = _DEV_RE.fullmatch(dev_text)
        if not dev_match:
            raise RecoveryFilesystemsError(f'invalid dev major:minor at {node}: {dev_text!r}')
        major, minor = int(dev_match.group(1), 10), int(dev_match.group(2), 10)
        rdev = os.makedev(major, minor)
        children.append({
            'node': node,
            'start512': start_512,
            'size512': size_512,
            'dev': dev_text,
            'rdev': rdev,
        })
    return resolved_lun0, children


def _resolve_partition_node(extent, resolved_lun0, children):
    if extent['startBytes'] % 512 != 0 or extent['bytes'] % 512 != 0:
        raise RecoveryFilesystemsError(f"{extent['label']}: byte extent not aligned to 512-byte sectors")
    want_start = extent['startBytes'] // 512
    want_size = extent['bytes'] // 512
    matches = [
        child for child in children
        if child['start512'] == want_start and child['size512'] == want_size
    ]
    if len(matches) != 1:
        raise RecoveryFilesystemsError(
            f"{extent['label']}: expected 1 sysfs partition matching ({want_start}, {want_size}), "
            f'found {len(matches)}'
        )
    match = matches[0]
    node = match['node']
    rdev = match['rdev']
    dev_text = match['dev']

    try:
        mapped_node = _sysfs_block_node(rdev)
    except BootHealthError as error:
        raise RecoveryFilesystemsError(
            f"{extent['label']}: sysfs block lookup failed for {dev_text}: {error}"
        ) from error
    try:
        resolved_mapped = mapped_node.resolve(strict=True)
    except OSError as error:
        raise RecoveryFilesystemsError(
            f"{extent['label']}: cannot resolve mapped sysfs node {mapped_node}: {error}"
        ) from error
    if resolved_mapped != node or resolved_mapped.parent != resolved_lun0:
        raise RecoveryFilesystemsError(f"{extent['label']}: sysfs block node mapping mismatch")

    symlink_path = physical_slots.DEV_BLOCK / dev_text
    try:
        if not symlink_path.is_symlink():
            raise RecoveryFilesystemsError(f'{symlink_path} is not a symlink')
        device_path = symlink_path.resolve(strict=True)
        info = os.lstat(device_path)
    except OSError as error:
        raise RecoveryFilesystemsError(f'cannot resolve or stat {symlink_path}: {error}') from error
    if not stat.S_ISBLK(info.st_mode) or info.st_rdev != rdev:
        raise RecoveryFilesystemsError(f'{device_path} block device identity mismatch')

    return {
        'node': node,
        'dev': dev_text,
        'rdev': rdev,
        'path': device_path,
        'start512': want_start,
        'size512': want_size,
    }


def _parse_blkid_export(output, label):
    if not isinstance(output, str):
        raise RecoveryFilesystemsError(f'{label}: blkid output is not text')
    raw_len = len(output.encode('utf-8'))
    if raw_len <= 0 or raw_len > _EXPORT_MAX_BYTES:
        raise RecoveryFilesystemsError(
            f'{label}: blkid export length {raw_len} outside (0, {_EXPORT_MAX_BYTES}]'
        )
    parsed = {}
    for line in output.splitlines():
        if not line:
            continue
        if '=' not in line:
            raise RecoveryFilesystemsError(f'{label}: malformed blkid export line')
        key, value = line.split('=', 1)
        if not _EXPORT_KEY_RE.fullmatch(key):
            raise RecoveryFilesystemsError(f'{label}: invalid blkid export key {key!r}')
        if key in parsed:
            raise RecoveryFilesystemsError(f'{label}: duplicate blkid export key {key!r}')
        parsed[key] = value
    return parsed


def _probe_filesystem(fd, label, expected_type):
    proc_fd_path = f'/proc/self/fd/{fd}'
    try:
        proc = subprocess.run(
            ['blkid', '-p', '-o', 'export', '--', proc_fd_path],
            pass_fds=(fd,),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RecoveryFilesystemsError(f'{label}: low-level blkid probe failed: {error}') from error

    fields = _parse_blkid_export(proc.stdout, label)
    usage = fields.get('USAGE')
    if usage is not None and usage != 'filesystem':
        raise RecoveryFilesystemsError(f'{label}: unexpected USAGE {usage!r}')
    fs_type = fields.get('TYPE')
    if fs_type != expected_type:
        raise RecoveryFilesystemsError(
            f'{label}: expected filesystem TYPE {expected_type!r}, got {fs_type!r}'
        )
    fs_uuid = _canonical_uuid(fields.get('UUID'), f'{label} filesystem UUID')
    return fs_type, fs_uuid


def _reverify_partition_sysfs(part_info, resolved_lun0):
    node = part_info['node']
    try:
        resolved_node = node.resolve(strict=True)
    except OSError as error:
        raise RecoveryFilesystemsError(f'partition sysfs node disappeared: {node}: {error}') from error
    if resolved_node != node or resolved_node.parent != resolved_lun0:
        raise RecoveryFilesystemsError(f'partition sysfs parentage drifted at {node}')
    if _sysfs_int(node / 'start') != part_info['start512']:
        raise RecoveryFilesystemsError(f'partition start drifted at {node}')
    if _sysfs_int(node / 'size') != part_info['size512']:
        raise RecoveryFilesystemsError(f'partition size drifted at {node}')
    if _sysfs_read(node / 'dev') != part_info['dev']:
        raise RecoveryFilesystemsError(f'partition dev drifted at {node}')
    try:
        mapped = _sysfs_block_node(part_info['rdev']).resolve(strict=True)
    except (OSError, BootHealthError) as error:
        raise RecoveryFilesystemsError(f'partition block node lookup drifted at {node}: {error}') from error
    if mapped != node:
        raise RecoveryFilesystemsError(f'partition block mapping drifted at {node}')
    symlink_path = physical_slots.DEV_BLOCK / part_info['dev']
    try:
        resolved_dev = symlink_path.resolve(strict=True)
        info = os.lstat(resolved_dev)
    except OSError as error:
        raise RecoveryFilesystemsError(f'device node drifted for {symlink_path}: {error}') from error
    if resolved_dev != part_info['path'] or not stat.S_ISBLK(info.st_mode) or info.st_rdev != part_info['rdev']:
        raise RecoveryFilesystemsError(f'device path identity drifted for {part_info["path"]}')


def inspect_recovery_filesystems(expected_physical=None):
    """Inspect and verify ARMADA_BOOT, ARMADA_ROOT, and ARMADA_BACKUP on UFS LUN0."""
    lun_fds = []
    part_fds = {}
    try:
        live_physical = physical_slots.inspect_recovery_slots()
        _validate_live_physical(live_physical, expected_physical)

        recovery_tuple = locate_recovery_partition(require_edl=False)
        rec_dev, rec_part_uuid, _board, rec_rdev = recovery_tuple
        rec_part_uuid = _canonical_uuid(rec_part_uuid, 'recovery PARTUUID')
        try:
            rec_dev_resolved = Path(rec_dev).resolve(strict=True)
        except OSError as error:
            raise RecoveryFilesystemsError(f'cannot resolve recovery device {rec_dev}: {error}') from error

        anchor, found = _enumerate_luns(rec_rdev)
        enabled = _verify_inventory_matches_physical(anchor, found, rec_part_uuid, live_physical)

        luns = sorted(found)
        for lun in luns:
            item = found[lun]
            lun_fds.append(_device_fd(item['path'], item['rdev']))

        locator = _load_locator()
        extents = {}
        for role, label, _expected_fs in _ROLE_SPECS:
            extents[role] = _locate_fixed_extent(locator, lun_fds, luns, found, enabled, label)

        _validate_layout(extents, rec_part_uuid)

        resolved_lun0, children = _enumerate_lun0_partitions(found[0]['node'])
        resolved_parts = {}
        for role, _label, _expected_fs in _ROLE_SPECS:
            resolved_parts[role] = _resolve_partition_node(extents[role], resolved_lun0, children)

        part_rdevs = {
            resolved_parts['boot']['rdev'],
            resolved_parts['root']['rdev'],
            resolved_parts['backup']['rdev'],
        }
        if len(part_rdevs) != 3:
            raise RecoveryFilesystemsError('boot, root, and backup must have distinct rdevs')
        if part_rdevs & {found[l]['rdev'] for l in luns}:
            raise RecoveryFilesystemsError('partition rdev collides with whole-LUN rdev')
        if resolved_parts['backup']['rdev'] != rec_rdev:
            raise RecoveryFilesystemsError(
                'ARMADA_BACKUP rdev does not match locate_recovery_partition'
            )
        if resolved_parts['backup']['path'] != rec_dev_resolved:
            raise RecoveryFilesystemsError(
                'ARMADA_BACKUP device path does not match locate_recovery_partition'
            )

        for role, _label, _expected_fs in _ROLE_SPECS:
            pinfo = resolved_parts[role]
            part_fds[role] = _device_fd(pinfo['path'], pinfo['rdev'])

        fs_results = {}
        for role, label, expected_fs in _ROLE_SPECS:
            if expected_fs is None:
                continue
            fs_type, fs_uuid = _probe_filesystem(part_fds[role], label, expected_fs)
            fs_results[role] = {'fsType': fs_type, 'uuid': fs_uuid}

        if fs_results['boot']['uuid'] == fs_results['root']['uuid']:
            raise RecoveryFilesystemsError('ARMADA_BOOT and ARMADA_ROOT filesystem UUIDs must be distinct')

        for lun, fd in zip(luns, lun_fds):
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != found[lun]['rdev']:
                raise RecoveryFilesystemsError(f'held fd identity drift on LUN{lun}')

        for role, fd in part_fds.items():
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != resolved_parts[role]['rdev']:
                raise RecoveryFilesystemsError(f'held fd identity drift on {role} partition')
            _reverify_partition_sysfs(resolved_parts[role], resolved_lun0)

        re_resolved_lun0, re_children = _enumerate_lun0_partitions(found[0]['node'])
        if (re_resolved_lun0, re_children) != (resolved_lun0, children):
            raise RecoveryFilesystemsError('LUN0 partition sysfs inventory drifted during check')

        re_anchor, re_found = _enumerate_luns(rec_rdev)
        if (re_anchor, re_found) != (anchor, found):
            raise RecoveryFilesystemsError('UFS LUN inventory drifted during check')

        re_recovery = locate_recovery_partition(require_edl=False)
        if re_recovery != recovery_tuple:
            raise RecoveryFilesystemsError('recovery partition locator drifted during check')

        re_physical = physical_slots.inspect_recovery_slots()
        if re_physical != live_physical:
            raise RecoveryFilesystemsError('physical recovery slot report drifted during check')

        def _build_entry(role, include_fs):
            ext = extents[role]
            pinfo = resolved_parts[role]
            entry = {
                'path': str(pinfo['path']),
                'rdev': pinfo['rdev'],
                'lun': 0,
                'startBytes': ext['startBytes'],
                'bytes': ext['bytes'],
                'partUuid': ext['partUuid'],
            }
            if include_fs:
                entry['fsType'] = fs_results[role]['fsType']
                entry['uuid'] = fs_results[role]['uuid']
            return entry

        return {
            'schemaVersion': _SCHEMA_VERSION,
            'phase': _PHASE,
            'physical': live_physical,
            'boot': _build_entry('boot', True),
            'root': _build_entry('root', True),
            'backup': _build_entry('backup', False),
        }
    except RecoveryFilesystemsError:
        raise
    except Exception as error:
        raise RecoveryFilesystemsError(f'recovery filesystem check failed: {error}') from error
    finally:
        for fd in part_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        for fd in lun_fds:
            try:
                os.close(fd)
            except OSError:
                pass
