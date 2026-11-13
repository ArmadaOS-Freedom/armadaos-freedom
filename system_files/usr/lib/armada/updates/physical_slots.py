"""Read-only precheck of physical UFS A/B slots. Not an updater/writer."""

import ctypes
import os
import re
import stat
from pathlib import Path

import armada_os
from . import boot_context
from .boot_health import BootHealthError, _sysfs_block_node, _ufs_controller
from .recovery_device import locate_recovery_partition

SYS_CLASS_BLOCK = Path('/sys/class/block')
DEV_BLOCK = Path('/dev/block')
LIBRARY_PATH = Path('/usr/libexec/armada/libarmada-update-ab.so')

_MODEL = {1: 'TB322FC', 2: 'TB323FU'}
_LUN_IDS = frozenset(range(6))
_INT64_MAX = (1 << 63) - 1
_SYSFS_MAX = 64
_DEC_RE = re.compile(r'^[0-9]+$')
_HEX_RE = re.compile(r'^0[xX][0-9a-fA-F]+$')
_DEV_RE = re.compile(r'^([0-9]+):([0-9]+)$')
_HCTL_RE = re.compile(r'^([0-9]+):([0-9]+):([0-9]+):([0-9]+)$')
# Android-owned state reported for telemetry.  It may change while Linux
# mirrors both slots and must not be treated as update identity drift.
_TELEMETRY_KEYS = frozenset({'selectedSlot', 'bootableSlots'})


class PhysicalSlotsError(RuntimeError):
    """Raised when physical slot precheck evidence is invalid or drifting."""


def physical_identity(report):
    """Project a slot report to the fields that must stay stable per update."""
    if not isinstance(report, dict):
        return report
    return {
        key: value for key, value in report.items()
        if key not in _TELEMETRY_KEYS
    }


def same_physical_identity(left, right):
    """Compare reports without treating Android-owned telemetry as drift.

    Reports written before the telemetry fields existed compare equal to
    reports that carry them, so cross-version receipts do not manufacture a
    physical-slot conflict.
    """
    return physical_identity(left) == physical_identity(right)


def _sysfs_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise PhysicalSlotsError(f'cannot open {path}: {error}') from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PhysicalSlotsError(f'{path} is not a regular file')
        chunks = []
        total = 0
        while total <= _SYSFS_MAX:
            chunk = os.read(fd, _SYSFS_MAX + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b''.join(chunks)
    except OSError as error:
        raise PhysicalSlotsError(f'cannot read {path}: {error}') from error
    finally:
        os.close(fd)
    if len(data) > _SYSFS_MAX:
        raise PhysicalSlotsError(f'{path} exceeds {_SYSFS_MAX}-byte limit')
    try:
        return data.decode('ascii').strip()
    except UnicodeError as error:
        raise PhysicalSlotsError(f'{path} is not ASCII') from error


def _sysfs_int(path):
    text = _sysfs_read(path)
    if _DEC_RE.fullmatch(text):
        return int(text, 10)
    if _HEX_RE.fullmatch(text):
        return int(text, 16)
    raise PhysicalSlotsError(f'{path} is not an unsigned decimal or 0x-hex integer: {text!r}')


def _device_fd(path, expected_rdev):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise PhysicalSlotsError(f'cannot open {path}: {error}') from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != expected_rdev:
            raise PhysicalSlotsError(f'{path} is not the expected block device')
    except Exception:
        os.close(fd)
        raise
    return fd


def _geometry(node):
    block_size = _sysfs_int(node / 'queue/logical_block_size')
    if block_size not in (512, 4096):
        raise PhysicalSlotsError(f'unsupported logical block size: {block_size}')
    sectors = _sysfs_int(node / 'size')
    sectors_per_block = block_size // 512
    if sectors <= 0 or sectors * 512 > _INT64_MAX or sectors % sectors_per_block != 0:
        raise PhysicalSlotsError(f'invalid size for {node}')
    block_count = sectors // sectors_per_block
    if block_count < 10:
        raise PhysicalSlotsError(f'block count too small for {node}: {block_count}')
    return block_size, block_count


def _boot_lun_id(lun_node, controller):
    boot_id = _sysfs_int(lun_node.parent.parent / 'unit_descriptor/boot_lun_id')
    if boot_id not in (0, 1, 2):
        raise PhysicalSlotsError(f'invalid boot_lun_id: {boot_id}')
    enabled = _sysfs_int(controller / 'attributes/boot_lun_enabled')
    if enabled not in (1, 2):
        raise PhysicalSlotsError(f'invalid boot_lun_enabled: {enabled}')
    return boot_id, enabled


def _enumerate_luns(recovery_rdev):
    try:
        anchor = _ufs_controller(_sysfs_block_node(recovery_rdev))
        entries = list(SYS_CLASS_BLOCK.iterdir())
    except (OSError, BootHealthError) as error:
        raise PhysicalSlotsError(f'cannot enumerate block devices: {error}') from error
    found = {}
    for entry in entries:
        try:
            node = entry.resolve(strict=True)
        except OSError as error:
            raise PhysicalSlotsError(f'cannot resolve {entry}: {error}') from error
        if (node / 'partition').exists():
            continue
        try:
            controller = _ufs_controller(node)
        except BootHealthError:
            continue
        if controller != anchor:
            continue
        if node.parent.name != 'block':
            raise PhysicalSlotsError(f'unexpected sysfs layout at {node}')
        hctl = node.parent.parent.name
        hctl_match = _HCTL_RE.fullmatch(hctl)
        if not hctl_match:
            raise PhysicalSlotsError(f'malformed H:C:T:L name: {hctl}')
        host, channel, target, lun = (int(part, 10) for part in hctl_match.groups())
        if lun not in _LUN_IDS:
            continue
        if lun in found:
            raise PhysicalSlotsError(f'duplicate LUN{lun}')
        dev = _sysfs_read(node / 'dev')
        dev_match = _DEV_RE.fullmatch(dev)
        if not dev_match:
            raise PhysicalSlotsError(f'invalid dev major:minor: {dev!r}')
        major, minor = int(dev_match.group(1), 10), int(dev_match.group(2), 10)
        rdev = os.makedev(major, minor)
        symlink_path = DEV_BLOCK / dev
        try:
            device_path = symlink_path.resolve(strict=True)
            info = os.lstat(device_path)
        except OSError as error:
            raise PhysicalSlotsError(f'cannot resolve or stat {symlink_path}: {error}') from error
        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != rdev:
            raise PhysicalSlotsError(f'{device_path} identity mismatch')
        try:
            mapped_node = _sysfs_block_node(rdev)
        except BootHealthError as error:
            raise PhysicalSlotsError(f'sysfs block lookup failed for LUN{lun}: {error}') from error
        if mapped_node != node:
            raise PhysicalSlotsError(f'sysfs mapping mismatch for LUN{lun}')
        block_size, block_count = _geometry(node)
        boot_id, enabled = _boot_lun_id(node, anchor)
        found[lun] = {
            'lun': lun,
            'path': device_path,
            'node': node,
            'rdev': rdev,
            'major': major,
            'minor': minor,
            'blockSize': block_size,
            'blockCount': block_count,
            'bootId': boot_id,
            'enabled': enabled,
            'hct': (host, channel, target),
        }
    if set(found) != _LUN_IDS:
        raise PhysicalSlotsError(f'expected exactly LUN0..5, found {sorted(found)}')
    if len({item['hct'] for item in found.values()}) != 1:
        raise PhysicalSlotsError('LUNs do not share a single H:C:T')
    if len({item['rdev'] for item in found.values()}) != 6:
        raise PhysicalSlotsError('LUN device numbers are not unique')
    if len({item['enabled'] for item in found.values()}) != 1:
        raise PhysicalSlotsError('boot_lun_enabled samples are not uniform across LUN0..5')
    return anchor, found


def _read_slot_flags(fds, found):
    if len(fds) != 6:
        raise PhysicalSlotsError(f'expected 6 LUN file descriptors, got {len(fds)}')
    luns = sorted(found)
    enabled_values = {found[l]['enabled'] for l in luns}
    if len(enabled_values) != 1:
        raise PhysicalSlotsError('inconsistent boot_lun_enabled across LUN0..5')
    enabled = next(iter(enabled_values))
    try:
        lib = ctypes.CDLL(str(LIBRARY_PATH))
        fn = lib.armada_update_ab_inspect_fds
    except OSError as error:
        raise PhysicalSlotsError(f'cannot load {LIBRARY_PATH}: {error}') from error
    except AttributeError as error:
        raise PhysicalSlotsError(f'missing armada_update_ab_inspect_fds in {LIBRARY_PATH}') from error
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    fd_arr = (ctypes.c_int * 6)(*fds)
    bs_arr = (ctypes.c_uint32 * 6)(*(found[l]['blockSize'] for l in luns))
    bc_arr = (ctypes.c_uint64 * 6)(*(found[l]['blockCount'] for l in luns))
    bid_arr = (ctypes.c_uint8 * 6)(*(found[l]['bootId'] for l in luns))
    out = (ctypes.c_uint32 * 3)()
    rc = fn(fd_arr, bs_arr, bc_arr, bid_arr, ctypes.c_uint32(enabled), out)
    if rc != 0:
        raise PhysicalSlotsError(f'libarmada-update-ab inspection failed rc={rc} out={list(out)}')
    current, boot_a, boot_b = (int(v) for v in out)
    if current not in (0, 1):
        raise PhysicalSlotsError(f'invalid current-slot index {current}')
    for name, value in (('bootable_a', boot_a), ('bootable_b', boot_b)):
        if value not in (0, 1):
            raise PhysicalSlotsError(f'{name} flag is not strictly boolean: {value}')
    return current, {'a': bool(boot_a), 'b': bool(boot_b)}


def _read_slot_state(fds, found):
    """Read the controller selection and both real Android bootable bits.

    Those bits are evidence, not a Linux precondition: mirrored updates write
    both physical slots and never issue slot selection, success marking or
    boot-LUN changes.  The one witness this probe still refuses is the
    degenerate all-zero result, where the native inspection reports that
    neither Android slot is bootable at all.
    """
    current, bootable = _read_slot_flags(fds, found)
    if not bootable['a'] and not bootable['b']:
        raise PhysicalSlotsError(
            "native inspection reports no bootable slot "
            f"(a={int(bootable['a'])}, b={int(bootable['b'])}); "
            "refusing an all-zero bootability witness"
        )
    return current, bootable


def _run_inspection(fds, found):
    """Legacy slot-control helper: both slots must be bootable.

    Only the deprecated, unreferenced ``slot_activation`` path still calls
    this.  The mirrored update path uses :func:`_read_slot_state` and treats
    the bootable bits as telemetry.
    """
    current, bootable = _read_slot_state(fds, found)
    if not bootable['a'] or not bootable['b']:
        raise PhysicalSlotsError(
            f"both slots must be bootable (a={int(bootable['a'])}, b={int(bootable['b'])})"
        )
    return current


def _read_boot_state():
    snap = armada_os.snapshot()
    if not isinstance(snap, dict):
        raise PhysicalSlotsError('armada_os.snapshot() did not return a dict')
    booted = snap.get('booted')
    if not isinstance(booted, dict):
        raise PhysicalSlotsError('snapshot booted entry is not a dict')
    try:
        context = boot_context.read_boot_context(booted)
    except Exception as error:
        raise PhysicalSlotsError(f'invalid boot context: {error}') from error
    return dict(booted), dict(context)


def _inspect_slots(reader, phase):
    saved_booted, context = reader()
    saved_context = dict(context)
    try:
        dev, part_uuid, board, recovery_rdev = locate_recovery_partition(require_edl=False)
    except Exception as error:
        raise PhysicalSlotsError(f'cannot locate recovery partition: {error}') from error
    if board not in _MODEL:
        raise PhysicalSlotsError(f'unsupported board id {board}')
    anchor, found = _enumerate_luns(recovery_rdev)
    fds = []
    try:
        for lun in sorted(found):
            item = found[lun]
            fds.append(_device_fd(item['path'], item['rdev']))
        current, bootable = _read_slot_state(fds, found)
        selected_slot = 'ab'[current]
        # The controller-selected slot and the Android bootable bits are
        # Android-owned telemetry.  A pending switch must not block the
        # mirrored update: the running slot still comes from the verified boot
        # witness, and both slots are written from their own signed material.
        for lun, fd in zip(sorted(found), fds):
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != found[lun]['rdev']:
                raise PhysicalSlotsError(f'held fd identity drift on LUN{lun}')
        re_anchor, re_found = _enumerate_luns(recovery_rdev)
        if (re_anchor, re_found) != (anchor, found):
            raise PhysicalSlotsError('UFS LUN inventory or attributes drifted during check')
        try:
            re_recovery = locate_recovery_partition(require_edl=False)
        except Exception as error:
            raise PhysicalSlotsError(f'cannot re-verify recovery partition: {error}') from error
        if re_recovery != (dev, part_uuid, board, recovery_rdev):
            raise PhysicalSlotsError('recovery partition identity drifted during check')
        re_booted, re_context = reader()
        if re_booted != saved_booted or re_context != saved_context:
            raise PhysicalSlotsError('boot context drifted during check')
        luns = [
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
        report = {
            'schemaVersion': 1,
            'phase': phase,
            'model': _MODEL[board],
            'actualSlot': context['actualSlot'],
            'inactiveSlot': context['inactiveSlot'],
        }
        report['deployment'] = context['deployment']
        report['selectedSlot'] = selected_slot
        report['bootableSlots'] = dict(bootable)
        report['recoveryPartUuid'] = part_uuid
        report['ufsController'] = str(anchor)
        report['luns'] = luns
        return report
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def inspect_physical_slots():
    """Perform the read-only physical A/B slot precheck and return its report."""
    return _inspect_slots(_read_boot_state, 'physical_slots_checked')
