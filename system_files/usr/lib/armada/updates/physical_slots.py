"""Read-only precheck of physical UFS A/B slots. Not an updater/writer."""

import ctypes
import os
import re
import stat
from pathlib import Path

import armada_os
from . import boot_context, offline_context
from .boot_health import BootHealthError, _sysfs_block_node, _ufs_controller
from .recovery_device import locate_recovery_partition

SYS_CLASS_BLOCK = Path('/sys/class/block')
DEV_BLOCK = Path('/dev/block')
LIBRARY_PATH = Path('/usr/libexec/armada/libarmada-update-ab.so')

# Report marker for a legacy controller-only observation.  A legacy report never
# names an actual/inactive slot: only the controller's selected slot is known.
LEGACY_BOOT_CONTEXT = offline_context.LEGACY_BOOT_CONTEXT

_MODEL = {1: 'TB322FC', 2: 'TB323FU'}
_LUN_IDS = frozenset(range(6))
_INT64_MAX = (1 << 63) - 1
_SYSFS_MAX = 64
_DEC_RE = re.compile(r'^[0-9]+$')
_HEX_RE = re.compile(r'^0[xX][0-9a-fA-F]+$')
_DEV_RE = re.compile(r'^([0-9]+):([0-9]+)$')
_HCTL_RE = re.compile(r'^([0-9]+):([0-9]+):([0-9]+):([0-9]+)$')


class PhysicalSlotsError(RuntimeError):
    """Raised when physical slot precheck evidence is invalid or drifting."""


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


def _run_inspection(fds, found):
    """Strict inspection: both slots must be bootable, returns the slot index."""
    current, bootable = _read_slot_flags(fds, found)
    if not bootable['a'] or not bootable['b']:
        raise PhysicalSlotsError(
            f"both slots must be bootable (a={int(bootable['a'])}, b={int(bootable['b'])})"
        )
    return current


def _read_boot_state():
    try:
        snap = armada_os.snapshot()
    except Exception as error:
        # The strict snapshot is the one strict read whose failure type lives
        # outside this module (bootc/libostree raise RuntimeError).  Normalize it
        # here so callers can tell "no witness evidence" apart from an
        # inspection defect; the original failure stays attached as the cause.
        raise PhysicalSlotsError(f'cannot read the boot snapshot: {error}') from error
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


def _read_legacy_boot_state():
    """Read-only libostree binding used only by the offline inspector."""
    try:
        booted, context = offline_context.read_legacy_boot_state()
    except Exception as error:
        raise PhysicalSlotsError(f'invalid legacy boot context: {error}') from error
    return dict(booted), dict(context)


def _read_offline_boot_state():
    """Strict booted state when witness evidence exists, else the legacy binding."""
    try:
        return _read_boot_state()
    except PhysicalSlotsError:
        if offline_context.new_evidence_present():
            raise
    return _read_legacy_boot_state()


def _inspect_slots(reader, phase, *, require_witness=True, require_bootable=True):
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

        def read_state():
            if require_bootable:
                # The strict inspection only returns when both flags are true.
                return _run_inspection(fds, found), {'a': True, 'b': True}
            return _read_slot_flags(fds, found)

        current, bootable_slots = read_state()
        selected_slot = 'ab'[current]
        if not bootable_slots[selected_slot]:
            raise PhysicalSlotsError(
                f'selected slot {selected_slot} is not bootable; repair required')
        if require_witness and selected_slot != context['actualSlot']:
            raise PhysicalSlotsError(
                'actual boot != next selected slot; restart or repair required '
                f'(witness={context["actualSlot"]}, controller reports slot index {current})'
            )
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
        if not require_witness and read_state() != (current, bootable_slots):
            raise PhysicalSlotsError('controller A/B slot state drifted during check')
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
        if require_witness:
            report = {
                'schemaVersion': 1,
                'phase': phase,
                'model': _MODEL[board],
                'actualSlot': context['actualSlot'],
                'inactiveSlot': context['inactiveSlot'],
            }
            report['deployment'] = context['deployment']
            report['selectedSlot'] = selected_slot
        else:
            if context.get('actualSlot') is not None or context.get('inactiveSlot') is not None:
                raise PhysicalSlotsError('legacy boot context must not claim an actual slot')
            if not isinstance(context.get('deployment'), str) or not context['deployment']:
                raise PhysicalSlotsError('legacy boot context has no deployment binding')
            report = {
                'schemaVersion': 1,
                'phase': phase,
                'model': _MODEL[board],
                'actualSlot': None,
                'inactiveSlot': None,
                'selectedSlot': selected_slot,
                'bootContext': LEGACY_BOOT_CONTEXT,
                'deployment': context.get('deployment'),
            }
        report['bootableSlots'] = dict(bootable_slots)
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


def inspect_offline_physical_slots():
    """Physical precheck for offline dual-slot work.

    Systems with firmware witness evidence get exactly the strict report.  A
    legacy controller-only system is inspected only when the witness and the
    ``armada.deployment`` argument are both provably absent, and then the report
    exposes the controller's selected slot without claiming an actual slot.
    """
    try:
        return inspect_physical_slots()
    except PhysicalSlotsError:
        if offline_context.new_evidence_present():
            raise
    return _inspect_slots(
        _read_legacy_boot_state, 'physical_slots_checked', require_witness=False)


def inspect_legacy_identity_slots():
    """Read-only controller-only inventory used to identify a legacy device.

    ``inspect_offline_physical_slots`` answers whether this device may start a
    slot mutation, so it requires both slots to be bootable.  An authenticated
    identity query only needs to know which system is running: a legacy device
    whose inactive slot is not bootable yet (GPT attribute UNBOOTABLE, or an
    image the firmware has not authenticated) is still a healthy running
    system, and refusing to identify it would hide the reason a future upgrade
    cannot start.  The controller's genuine flags are reported unchanged and
    the selected slot must itself be bootable; no actual slot is claimed.

    This path is never used by prepare, execute, preflight or any other caller
    that can write; those keep the strict both-bootable precheck.
    """
    if offline_context.new_evidence_present():
        raise PhysicalSlotsError(
            'witness evidence exists; the legacy identity inventory does not apply')
    return _inspect_slots(
        _read_legacy_boot_state, 'physical_slots_checked',
        require_witness=False, require_bootable=False)


def offline_slot_pair(physical):
    """Return ``(reference, other)`` for an OFFLINE-DUAL write.

    The reference slot is the controller's selected slot; the other slot is
    written first.  A legacy report never turns the selected slot into an actual
    slot, and a strict report must agree with the booted slot.
    """
    if not isinstance(physical, dict):
        raise PhysicalSlotsError('physical report must be a dict')
    legacy = physical.get('bootContext') == LEGACY_BOOT_CONTEXT
    if legacy:
        reference = physical.get('selectedSlot')
        if physical.get('actualSlot') is not None or physical.get('inactiveSlot') is not None:
            raise PhysicalSlotsError('legacy physical report must not claim an actual slot')
    else:
        reference = physical.get('selectedSlot', physical.get('actualSlot'))
        if reference != physical.get('actualSlot'):
            raise PhysicalSlotsError(
                'strict physical report selected slot disagrees with the booted slot')
    if reference not in ('a', 'b'):
        raise PhysicalSlotsError('physical report has no valid selected slot')
    other = 'b' if reference == 'a' else 'a'
    if not legacy and physical.get('inactiveSlot') not in (None, other):
        raise PhysicalSlotsError('strict physical report inactive slot disagrees')
    return reference, other
