# SPDX-License-Identifier: GPL-3.0-only
"""Internal physical UFS A/B slot activation after inactive boot write.

Deprecated and unreferenced.  The ordinary Linux update, rollback, boot and
confirmation paths use the mirrored dual-slot writer and never select a slot,
mark slot success, or write Android boot-control state.  Kept only so the
native selector contract stays documented; no update path imports it.
"""

import ctypes
import os
import re
import stat
import uuid
from pathlib import Path

from . import (
    boot_health,
    physical_slots,
    recovery_device,
    trial_record,
    trial_store,
)
from .boot_candidate import _require_positive_int, _verify_claim
from .boot_sources import _load_locator, _locate, _rehash_region
from .boot_writer import _inventory
from .deployment_store import _parse_binding

SYS_CLASS_BSG = Path('/sys/class/bsg')
SYS_DEV_CHAR = Path('/sys/dev/char')
DEV_BSG = Path('/dev/bsg')
_DEV_RE = re.compile(r'^([0-9]+):([0-9]+)$')
_SLOTS = {'a': 0, 'b': 1}


class ActivationError(RuntimeError):
    def __init__(self, message, outcome='not_attempted', native_result=None):
        super().__init__(message)
        self.outcome = outcome
        self.native_result = native_result


def _exact_int(value, name):
    if type(value) is not int:
        raise ActivationError(f'{name} must be an int')
    return value


def _open_validated(path, flags, mode_pred, expected_rdev, label):
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ActivationError(f'cannot open {label} {path}: {error}') from error
    try:
        info = os.fstat(fd)
        if not mode_pred(info.st_mode) or info.st_rdev != expected_rdev:
            raise ActivationError(f'{label} {path} identity mismatch')
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return fd


def _extent_id(ext):
    return (
        ext['lun'],
        ext['firstLba'],
        ext['blockCount'],
        ext['blockSize'],
        ext['bytes'],
        bytes(ext['uuid']),
    )


def _bsg_descriptor(anchor, found):
    host = found[0]['hct'][0]
    name = f'ufs-bsg{host}'
    host_dir = f'host{host}'
    class_entry = SYS_CLASS_BSG / name
    try:
        node = class_entry.resolve(strict=True)
    except OSError as error:
        raise ActivationError(f'cannot resolve {class_entry}: {error}') from error
    try:
        rel = node.relative_to(anchor)
    except ValueError as error:
        raise ActivationError(f'{node} is not under {anchor}') from error
    if host_dir not in rel.parts[:-1]:
        raise ActivationError(f'{node} missing {host_dir} ancestor under {anchor}')
    dev_text = physical_slots._sysfs_read(node / 'dev')
    match = _DEV_RE.fullmatch(dev_text)
    if not match:
        raise ActivationError(f'invalid BSG dev major:minor: {dev_text!r}')
    rdev = os.makedev(int(match.group(1), 10), int(match.group(2), 10))
    char_link = SYS_DEV_CHAR / dev_text
    try:
        char_node = char_link.resolve(strict=True)
    except OSError as error:
        raise ActivationError(f'cannot resolve {char_link}: {error}') from error
    if char_node != node:
        raise ActivationError(f'{char_link} does not resolve to {node}')
    dev_path = DEV_BSG / name
    try:
        info = os.lstat(dev_path)
    except OSError as error:
        raise ActivationError(f'cannot lstat {dev_path}: {error}') from error
    if not stat.S_ISCHR(info.st_mode) or info.st_rdev != rdev:
        raise ActivationError(f'{dev_path} is not the expected BSG char device')
    return str(dev_path), str(node), rdev


def _load_selector():
    try:
        lib = ctypes.CDLL(str(physical_slots.LIBRARY_PATH))
        fn = lib.armada_update_ab_select_fds
    except OSError as error:
        raise ActivationError(f'cannot load {physical_slots.LIBRARY_PATH}: {error}') from error
    except AttributeError as error:
        raise ActivationError(
            f'missing armada_update_ab_select_fds in {physical_slots.LIBRARY_PATH}'
        ) from error
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return fn


def activate_prepared_slot(write_report):
    return _activate_prepared_slots(write_report, offline=False)


def activate_offline_slots(write_report):
    return _activate_prepared_slots(write_report, offline=True)


def _activate_prepared_slots(write_report, offline):
    own_fds = []
    attempted = False
    native_rc = None
    try:
        if not isinstance(write_report, dict):
            raise ActivationError('write_report must be a dict')
        if _exact_int(write_report.get('schemaVersion'), 'schemaVersion') != 1:
            raise ActivationError('unsupported write_report schemaVersion')
        expected_phase = 'dual_boots_written' if offline else 'inactive_boot_written'
        if write_report.get('phase') != expected_phase:
            raise ActivationError(f'write_report phase must be {expected_phase}')
        model = write_report.get('model')
        if model not in ('TB322FC', 'TB323FU'):
            raise ActivationError(f'unsupported model: {model!r}')
        from_slot = write_report.get('fromSlot')
        if offline:
            if from_slot not in _SLOTS:
                raise ActivationError('invalid offline fromSlot')
            target_slot = 'ab'[1 - _SLOTS[from_slot]]
            if (
                'targetSlot' in write_report
                and write_report.get('targetSlot') != target_slot
            ):
                raise ActivationError('offline targetSlot contradicts fromSlot')
            if write_report.get('slotOrder') != [target_slot, from_slot]:
                raise ActivationError('offline slotOrder must be [targetSlot, fromSlot]')
        else:
            target_slot = write_report.get('targetSlot')
            if (
                from_slot not in _SLOTS
                or target_slot not in _SLOTS
                or from_slot == target_slot
            ):
                raise ActivationError('invalid fromSlot/targetSlot pair')
        from_idx = _SLOTS[from_slot]
        target_idx = _SLOTS[target_slot]
        initial = write_report.get('physical')
        if not isinstance(initial, dict):
            raise ActivationError('write_report.physical must be a dict')
        inspect_slots = physical_slots.inspect_physical_slots
        read_boot_state = physical_slots._read_boot_state
        if inspect_slots() != initial:
            raise ActivationError('live physical slots report does not match initial report')
        if (
            initial.get('model') != model
            or initial.get('actualSlot') != from_slot
            or initial.get('inactiveSlot') != target_slot
            or initial.get('selectedSlot') != from_slot
        ):
            raise ActivationError('write_report slot/model metadata disagrees with physical report')

        current_readback_sha = None
        source_boot_sha = None
        if offline:
            writes = write_report.get('writes')
            if not isinstance(writes, dict) or set(writes) != {'a', 'b'}:
                raise ActivationError('offline writes must contain exactly slots a and b')
            candidates = {}
            readback_shas = {}
            source_boot_shas = {}
            write_keys = {'sourceBootSha256', 'candidate', 'bytesWritten', 'readbackSha256'}
            for slot in ('a', 'b'):
                evidence = writes[slot]
                if not isinstance(evidence, dict) or set(evidence) != write_keys:
                    raise ActivationError(f'offline writes.{slot} has invalid evidence keys')
                source_sha = evidence.get('sourceBootSha256')
                if (
                    not isinstance(source_sha, str)
                    or re.fullmatch(r'[0-9a-f]{64}', source_sha) is None
                    or source_sha == '0' * 64
                ):
                    raise ActivationError(
                        f'writes.{slot}.sourceBootSha256 must be canonical non-zero SHA-256'
                    )
                slot_candidate = _verify_claim(
                    evidence.get('candidate'), f'writes.{slot}.candidate'
                )
                slot_bytes_written = _require_positive_int(
                    _exact_int(
                        evidence.get('bytesWritten'), f'writes.{slot}.bytesWritten'
                    ),
                    f'writes.{slot}.bytesWritten',
                )
                slot_readback_sha = evidence.get('readbackSha256')
                if (
                    slot_bytes_written != slot_candidate['bytes']
                    or slot_readback_sha != slot_candidate['sha256']
                ):
                    raise ActivationError(
                        f'writes.{slot} bytesWritten/readbackSha256 mismatch candidate'
                    )
                candidates[slot] = slot_candidate
                readback_shas[slot] = slot_readback_sha
                source_boot_shas[slot] = source_sha
            candidate = candidates[target_slot]
            readback_sha = readback_shas[target_slot]
            current_readback_sha = readback_shas[from_slot]
            source_boot_sha = source_boot_shas[from_slot]
        else:
            candidate = _verify_claim(write_report.get('candidate'), 'candidate')
            bytes_written = _require_positive_int(
                _exact_int(write_report.get('bytesWritten'), 'bytesWritten'),
                'bytesWritten',
            )
            readback_sha = write_report.get('readbackSha256')
            if bytes_written != candidate['bytes'] or readback_sha != candidate['sha256']:
                raise ActivationError('write_report bytesWritten/readbackSha256 mismatch candidate')

        tx_str = write_report.get('transactionUuid')
        if not isinstance(tx_str, str):
            raise ActivationError('transactionUuid must be a canonical string')
        try:
            tx_uuid = uuid.UUID(tx_str)
        except ValueError as error:
            raise ActivationError(f'invalid transactionUuid: {tx_str!r}') from error
        if str(tx_uuid) != tx_str or tx_uuid.bytes == b'\x00' * 16:
            raise ActivationError('transactionUuid must be canonical lowercase non-zero UUID')


        new_deployment = write_report.get('deployment')
        old_deployment = initial.get('deployment')
        old_os, old_commit_hex, old_serial = _parse_binding(old_deployment)
        new_os, new_commit_hex, new_serial = _parse_binding(new_deployment)
        if old_os != new_os or (old_commit_hex, old_serial) == (new_commit_hex, new_serial):
            raise ActivationError('deployment bindings must share os_name and differ in target')

        saved_boot_state = read_boot_state()
        recovery = physical_slots.locate_recovery_partition(require_edl=False)
        _, rec_uuid_str, rec_board, rec_rdev = recovery
        if (
            physical_slots._MODEL.get(rec_board) != model
            or rec_uuid_str != initial.get('recoveryPartUuid')
        ):
            raise ActivationError('recovery partition disagrees with initial physical report')
        anchor, found = _inventory(initial, rec_rdev)
        bsg_desc = _bsg_descriptor(anchor, found)
        luns = sorted(found)
        for lun in luns:
            own_fds.append(physical_slots._device_fd(found[lun]['path'], found[lun]['rdev']))
        ro_fds = list(own_fds)

        locator = _load_locator()
        enabled = found[0]['enabled']
        old_label = f'boot_{from_slot}'
        new_label = f'boot_{target_slot}'
        old_ext = _locate(locator, ro_fds, luns, found, enabled, old_label)
        new_ext = _locate(locator, ro_fds, luns, found, enabled, new_label)
        old_ext_id = _extent_id(old_ext)
        new_ext_id = _extent_id(new_ext)
        if new_ext['bytes'] != candidate['bytes']:
            raise ActivationError('target boot partition size does not match candidate bytes')
        if _rehash_region(
            new_ext['fd'], new_ext['firstLba'] * new_ext['blockSize'], new_ext['bytes']
        ) != readback_sha:
            raise ActivationError('target boot partition digest does not match readbackSha256')
        current_boot_hex = _rehash_region(
            old_ext['fd'], old_ext['firstLba'] * old_ext['blockSize'], old_ext['bytes']
        )
        old_boot_hex = current_boot_hex
        if offline:
            current_candidate = candidates[from_slot]
            if old_ext['bytes'] != current_candidate['bytes']:
                raise ActivationError(
                    'current boot partition size does not match current candidate bytes'
                )
            if current_boot_hex != current_readback_sha:
                raise ActivationError(
                    'current boot partition digest does not match current readbackSha256'
                )
            old_boot_hex = source_boot_sha

        with recovery_device.open_recovery(writable=False) as (jfd, juuid, jboard):
            if (juuid, jboard) != (rec_uuid_str, rec_board):
                raise ActivationError('opened recovery device metadata mismatch')
            jinfo = os.fstat(jfd)
            if not stat.S_ISBLK(jinfo.st_mode) or jinfo.st_rdev != rec_rdev:
                raise ActivationError('opened recovery fd rdev mismatch')
            snapshot = trial_store.load(jfd, juuid, jboard)
            record = snapshot.record
            trial_record.encode_record(record)
            if (
                record.state != trial_record.PREPARED
                or record.policy
                != (trial_record.OFFLINE_DUAL if offline else trial_record.ONLINE_SINGLE)
                or record.from_slot != from_idx
                or record.target_slot != target_idx
                or record.transaction_uuid != tx_uuid.bytes
                or record.os_name != new_os
                or record.old_serial != old_serial
                or record.new_serial != new_serial
                or record.old_commit != bytes.fromhex(old_commit_hex)
                or record.new_commit != bytes.fromhex(new_commit_hex)
                or record.new_boot_sha256 != bytes.fromhex(readback_sha)
                or record.old_boot_sha256 != bytes.fromhex(old_boot_hex)
                or (
                    offline
                    and record.other_boot_sha256 != bytes.fromhex(current_readback_sha)
                )
            ):
                raise ActivationError('trial journal record does not match prepared activation')

            rw_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            rw_fds = []
            for lun in luns:
                fd = _open_validated(
                    found[lun]['path'], rw_flags, stat.S_ISBLK, found[lun]['rdev'], f'LUN{lun}'
                )
                own_fds.append(fd)
                rw_fds.append(fd)
            bsg_fd = _open_validated(bsg_desc[0], rw_flags, stat.S_ISCHR, bsg_desc[2], 'BSG')
            own_fds.append(bsg_fd)
            selector = _load_selector()

            def verify_state(expected_idx, pre_invoke=False):
                if pre_invoke and inspect_slots() != initial:
                    raise ActivationError('pre-activation physical slot recheck drifted')
                if read_boot_state() != saved_boot_state:
                    raise ActivationError('boot state drifted during activation')
                if physical_slots.locate_recovery_partition(require_edl=False) != recovery:
                    raise ActivationError('recovery partition drifted during activation')
                cur_anchor, cur_found = _inventory(initial, rec_rdev)
                if _bsg_descriptor(cur_anchor, cur_found) != bsg_desc:
                    raise ActivationError('BSG controller descriptor drifted')
                for lun, rfd, wfd in zip(luns, ro_fds, rw_fds):
                    exp_rdev = cur_found[lun]['rdev']
                    for fd in (rfd, wfd):
                        info = os.fstat(fd)
                        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != exp_rdev:
                            raise ActivationError(f'held LUN{lun} fd drifted')
                binfo = os.fstat(bsg_fd)
                if not stat.S_ISCHR(binfo.st_mode) or binfo.st_rdev != bsg_desc[2]:
                    raise ActivationError('held BSG fd drifted')
                rinfo = os.fstat(jfd)
                if not stat.S_ISBLK(rinfo.st_mode) or rinfo.st_rdev != rec_rdev:
                    raise ActivationError('held recovery fd drifted')
                if trial_store.load(jfd, juuid, jboard) != snapshot:
                    raise ActivationError('trial journal drifted during activation')
                if physical_slots._run_inspection(ro_fds, cur_found) != expected_idx:
                    raise ActivationError('inspected selected slot does not match expected slot')
                cur_enabled = cur_found[0]['enabled']
                cur_old = _locate(locator, ro_fds, luns, cur_found, cur_enabled, old_label)
                cur_new = _locate(locator, ro_fds, luns, cur_found, cur_enabled, new_label)
                if _extent_id(cur_old) != old_ext_id or _extent_id(cur_new) != new_ext_id:
                    raise ActivationError('boot partition extent geometry drifted')
                if _rehash_region(
                    cur_old['fd'], cur_old['firstLba'] * cur_old['blockSize'], cur_old['bytes']
                ) != current_boot_hex:
                    raise ActivationError('active boot partition content drifted')
                if _rehash_region(
                    cur_new['fd'], cur_new['firstLba'] * cur_new['blockSize'], cur_new['bytes']
                ) != readback_sha:
                    raise ActivationError('inactive boot partition content drifted')

            verify_state(from_idx, pre_invoke=True)

            fd_arr = (ctypes.c_int * 6)(*rw_fds)
            bs_arr = (ctypes.c_uint32 * 6)(*(found[l]['blockSize'] for l in luns))
            bc_arr = (ctypes.c_uint64 * 6)(*(found[l]['blockCount'] for l in luns))
            bid_arr = (ctypes.c_uint8 * 6)(*(found[l]['bootId'] for l in luns))
            out = (ctypes.c_uint32 * 3)()
            attempted = True
            native_rc = int(
                selector(
                    fd_arr,
                    bs_arr,
                    bc_arr,
                    bid_arr,
                    ctypes.c_int(bsg_fd),
                    ctypes.c_uint32(target_idx),
                    ctypes.c_uint32(from_idx),
                    out,
                )
            )
            if native_rc == 0:
                if [int(v) for v in out] != [target_idx, 1, 1]:
                    raise ActivationError(
                        f'unexpected selector output {list(out)}',
                        outcome='uncertain',
                        native_result=native_rc,
                    )
                verify_state(target_idx)
                result = {
                    'schemaVersion': 1,
                    'phase': 'offline_slots_activated' if offline else 'slot_activated',
                    'model': model,
                    'fromSlot': from_slot,
                    'targetSlot': target_slot,
                    'selectedSlot': target_slot,
                    'transactionUuid': tx_str,
                    'deployment': new_deployment,
                    'recordSequence': record.sequence,
                    'readbackSha256': readback_sha,
                }
                if offline:
                    result['currentReadbackSha256'] = current_readback_sha
                    result['targetReadbackSha256'] = readback_sha
                return result
            if native_rc == -30:
                raise ActivationError(
                    'native slot selector returned -30 (rollback/state uncertain)',
                    outcome='uncertain',
                    native_result=native_rc,
                )
            try:
                verify_state(from_idx)
            except Exception as verify_error:
                raise ActivationError(
                    f'native selector failed rc={native_rc} and rollback verification failed: '
                    f'{verify_error}',
                    outcome='uncertain',
                    native_result=native_rc,
                ) from verify_error
            raise ActivationError(
                f'native selector failed rc={native_rc}; slot verified unchanged',
                outcome='unchanged',
                native_result=native_rc,
            )
    except ActivationError as error:
        if attempted and error.outcome == 'not_attempted':
            raise ActivationError(
                str(error), outcome='uncertain', native_result=native_rc
            ) from error
        raise
    except Exception as error:
        raise ActivationError(
            f'slot activation failed: {error}',
            outcome='uncertain' if attempted else 'not_attempted',
            native_result=native_rc,
        ) from error
    finally:
        for fd in own_fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _load_success_marker():
    try:
        lib = ctypes.CDLL(str(physical_slots.LIBRARY_PATH))
        fn = lib.armada_update_ab_mark_success_fds
    except OSError as error:
        raise ActivationError(f'cannot load {physical_slots.LIBRARY_PATH}: {error}') from error
    except AttributeError as error:
        raise ActivationError(
            f'missing armada_update_ab_mark_success_fds in {physical_slots.LIBRARY_PATH}'
        ) from error
    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return fn


def mark_confirmed_slot(record):
    own_fds = []
    attempted = False
    native_rc = None
    try:
        trial_record.encode_record(record)
        if record.state != trial_record.CONFIRMED:
            raise ActivationError('record state must be CONFIRMED')
        evidence = boot_health.collect_boot_evidence(record)
        if type(evidence) is not boot_health.BootEvidence:
            raise ActivationError('collect_boot_evidence must return exact BootEvidence')
        target_idx = _exact_int(evidence.actual_slot, 'evidence.actual_slot')
        if target_idx not in (0, 1):
            raise ActivationError('invalid evidence.actual_slot')
        target_slot = 'ab'[target_idx]
        expected_sha256 = trial_record.expected_boot_sha256(record, target_idx)
        if (
            evidence.transaction_uuid != record.transaction_uuid
            or not evidence.kernel_release
            or evidence.boot_sha256 != expected_sha256
        ):
            raise ActivationError('boot evidence does not bind record transaction/slot/sha')
        expected_deployment = f'{record.os_name}/{record.new_commit.hex()}.{record.new_serial}'
        if evidence.deployment != expected_deployment:
            raise ActivationError('boot evidence deployment does not match record binding')

        initial = physical_slots.inspect_physical_slots()
        if (
            initial.get('actualSlot') != target_slot
            or initial.get('selectedSlot') != target_slot
            or initial.get('deployment') != expected_deployment
        ):
            raise ActivationError('live physical report does not match confirmed target')
        model = initial.get('model')
        if model not in ('TB322FC', 'TB323FU'):
            raise ActivationError(f'unsupported model: {model!r}')
        saved_boot_state = physical_slots._read_boot_state()
        recovery = physical_slots.locate_recovery_partition(require_edl=False)
        _, rec_uuid_str, rec_board, rec_rdev = recovery
        if (
            physical_slots._MODEL.get(rec_board) != model
            or rec_uuid_str != initial.get('recoveryPartUuid')
        ):
            raise ActivationError('recovery partition disagrees with physical report')
        anchor, found = _inventory(initial, rec_rdev)
        bsg_desc = _bsg_descriptor(anchor, found)
        luns = sorted(found)
        for lun in luns:
            own_fds.append(physical_slots._device_fd(found[lun]['path'], found[lun]['rdev']))
        ro_fds = list(own_fds)

        locator = _load_locator()
        enabled = found[0]['enabled']
        label = f'boot_{target_slot}'
        ext = _locate(locator, ro_fds, luns, found, enabled, label)
        ext_id = _extent_id(ext)
        if _rehash_region(
            ext['fd'], ext['firstLba'] * ext['blockSize'], ext['bytes']
        ) != expected_sha256.hex():
            raise ActivationError('current boot partition digest does not match record')

        with recovery_device.open_recovery(writable=False) as (jfd, juuid, jboard):
            if (juuid, jboard) != (rec_uuid_str, rec_board):
                raise ActivationError('opened recovery device metadata mismatch')
            jinfo = os.fstat(jfd)
            if not stat.S_ISBLK(jinfo.st_mode) or jinfo.st_rdev != rec_rdev:
                raise ActivationError('opened recovery fd rdev mismatch')
            snapshot = trial_store.load(jfd, juuid, jboard)
            if snapshot.record != record:
                raise ActivationError('trial journal record does not match confirmed record')

            rw_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            rw_fds = []
            for lun in luns:
                fd = _open_validated(
                    found[lun]['path'], rw_flags, stat.S_ISBLK, found[lun]['rdev'], f'LUN{lun}'
                )
                own_fds.append(fd)
                rw_fds.append(fd)
            bsg_fd = _open_validated(bsg_desc[0], rw_flags, stat.S_ISCHR, bsg_desc[2], 'BSG')
            own_fds.append(bsg_fd)
            marker = _load_success_marker()

            def verify_state(pre_invoke=False):
                if physical_slots.inspect_physical_slots() != initial:
                    raise ActivationError('pre-mark physical slot recheck drifted')
                if physical_slots._read_boot_state() != saved_boot_state:
                    raise ActivationError('boot state drifted during mark')
                if physical_slots.locate_recovery_partition(require_edl=False) != recovery:
                    raise ActivationError('recovery partition drifted during mark')
                cur_anchor, cur_found = _inventory(initial, rec_rdev)
                if _bsg_descriptor(cur_anchor, cur_found) != bsg_desc:
                    raise ActivationError('BSG controller descriptor drifted')
                for lun, rfd, wfd in zip(luns, ro_fds, rw_fds):
                    exp_rdev = cur_found[lun]['rdev']
                    for fd in (rfd, wfd):
                        info = os.fstat(fd)
                        if not stat.S_ISBLK(info.st_mode) or info.st_rdev != exp_rdev:
                            raise ActivationError(f'held LUN{lun} fd drifted')
                binfo = os.fstat(bsg_fd)
                if not stat.S_ISCHR(binfo.st_mode) or binfo.st_rdev != bsg_desc[2]:
                    raise ActivationError('held BSG fd drifted')
                rinfo = os.fstat(jfd)
                if not stat.S_ISBLK(rinfo.st_mode) or rinfo.st_rdev != rec_rdev:
                    raise ActivationError('held recovery fd drifted')
                if trial_store.load(jfd, juuid, jboard) != snapshot:
                    raise ActivationError('trial journal drifted during mark')
                cur_enabled = cur_found[0]['enabled']
                cur_ext = _locate(locator, ro_fds, luns, cur_found, cur_enabled, label)
                if _extent_id(cur_ext) != ext_id:
                    raise ActivationError('boot partition extent geometry drifted')
                if _rehash_region(
                    cur_ext['fd'], cur_ext['firstLba'] * cur_ext['blockSize'], cur_ext['bytes']
                ) != expected_sha256.hex():
                    raise ActivationError('current boot partition content drifted')

            verify_state(pre_invoke=True)

            fd_arr = (ctypes.c_int * 6)(*rw_fds)
            bs_arr = (ctypes.c_uint32 * 6)(*(found[l]['blockSize'] for l in luns))
            bc_arr = (ctypes.c_uint64 * 6)(*(found[l]['blockCount'] for l in luns))
            bid_arr = (ctypes.c_uint8 * 6)(*(found[l]['bootId'] for l in luns))
            out = (ctypes.c_uint32 * 3)()
            attempted = True
            native_rc = int(
                marker(
                    fd_arr,
                    bs_arr,
                    bc_arr,
                    bid_arr,
                    ctypes.c_int(bsg_fd),
                    ctypes.c_uint32(target_idx),
                    out,
                )
            )
            if native_rc == 0:
                if [int(v) for v in out] != [target_idx, 1, 1]:
                    raise ActivationError(
                        f'unexpected mark-success output {list(out)}',
                        outcome='uncertain',
                        native_result=native_rc,
                    )
                verify_state()
                return {
                    'schemaVersion': 1,
                    'phase': 'slot_confirmed',
                    'model': model,
                    'targetSlot': target_slot,
                    'deployment': expected_deployment,
                    'transactionUuid': str(uuid.UUID(bytes=bytes(record.transaction_uuid))),
                    'recordSequence': record.sequence,
                }
            if native_rc == -30:
                raise ActivationError(
                    'native mark-success returned -30 (rollback/state uncertain)',
                    outcome='uncertain',
                    native_result=native_rc,
                )
            try:
                verify_state()
            except Exception as verify_error:
                raise ActivationError(
                    f'native mark-success failed rc={native_rc} and rollback verification '
                    f'failed: {verify_error}',
                    outcome='uncertain',
                    native_result=native_rc,
                ) from verify_error
            raise ActivationError(
                f'native mark-success failed rc={native_rc}; state verified unchanged',
                outcome='unchanged',
                native_result=native_rc,
            )
    except ActivationError as error:
        if attempted and error.outcome == 'not_attempted':
            raise ActivationError(
                str(error), outcome='uncertain', native_result=native_rc
            ) from error
        raise
    except Exception as error:
        raise ActivationError(
            f'slot confirmation marking failed: {error}',
            outcome='uncertain' if attempted else 'not_attempted',
            native_result=native_rc,
        ) from error
    finally:
        for fd in own_fds:
            try:
                os.close(fd)
            except OSError:
                pass
