# SPDX-License-Identifier: GPL-3.0-only
"""Private, authenticated offline-update payload and recovery-export storage."""

from __future__ import annotations

import configparser
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from contextlib import contextmanager

import armada_os
from . import (
    boot_candidate,
    boot_context,
    offline_context,
    offline_execute,
    offline_payload,
    offline_prepare,
    package_contract,
    physical_slots,
    recovery_device,
    signature,
    system_transaction,
    trial_record,
)

_DEFAULT_BASE = Path('/var/lib/armada-updates/offline')
_DEFAULT_SYSROOT = Path('/sysroot')
_UPDATER_VERSION = '0.8.0'
_DOCUMENT_MAX = 2 * 1024 * 1024
_SIGNATURE_MAX = 16 * 1024
_JSON_MAX = 2 * 1024 * 1024
_REPOSITORY_CONFIG_MAX = 64 * 1024
# The only on-disk repository format this updater's ``pull-local`` can open.
_REPOSITORY_VERSION = 1
_CHUNK_MAX = 1024 * 1024
_SESSION_ID = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_MODELS = frozenset({'TB322FC', 'TB323FU'})
# BTRFS_IOC_FS_INFO is _IOR(0x94, 31, struct btrfs_ioctl_fs_info_args): the
# filesystem-wide FSID starts at byte 16 of its 1024-byte argument, unlike
# statfs whose f_fsid is XORed with the subvolume root id.
_BTRFS_IOCTL_FS_INFO = 0x8400941F
_BTRFS_FS_INFO_SIZE = 1024
_BTRFS_FSID_OFFSET = 16
_BTRFS_FSID_SIZE = 16
# The OSTree repository keeps free space for itself before a pull-local writes
# into it.  libostree's configured minimum wins; without one this conservative
# share covers releases that keep no reserve of their own.
_REPOSITORY_RESERVE_DEFAULT_PERCENT = 3
# ``min-free-space-size`` is expressed in power-of-two units.
_REPOSITORY_RESERVE_UNITS = (('TB', 1024 ** 4), ('GB', 1024 ** 3),
                             ('MB', 1024 ** 2), ('KB', 1024))
_EXPORT_STATES = frozenset({
    'awaiting_recovery_export', 'executing', 'awaiting_reboot', 'failed',
    'recovery_required',
})
# Receipt states in which a read-only preflight may reuse an existing session's
# payload: nothing is executing, interrupted or failed, so prepare can still
# resume from exactly the bytes this session already stores.
_PREFLIGHT_SESSION_STATES = frozenset({
    'uploading', 'uploaded', 'prepare_queued', 'preparing', 'prepared',
    'up_to_date',
})
# Read-only dependencies every offline worker needs before a session starts.
_REQUIRED_UPDATER_FILES = (
    Path('/usr/libexec/armada/kshim-build-boot.py'),
    physical_slots.LIBRARY_PATH,
)
_REQUIRED_UPDATER_EXECUTABLES = (
    Path('/usr/bin/python3'),
    Path('/usr/libexec/armada/armada-offline-update'),
    boot_candidate._DEFAULT_BOOT_TOOL_PATH,
)
# info() treats exactly this inspector as "not injected" and reads the device.
_DEFAULT_INSPECT = physical_slots.inspect_offline_physical_slots


class OfflineSessionError(RuntimeError):
    """An offline session cannot safely perform the requested operation."""


class _WitnessEvidenceError(OfflineSessionError):
    """Firmware witness evidence could not be read at all."""


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('short private-file write')
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_dir(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise OfflineSessionError('private directory must be absolute and non-symlink')
    try:
        resolved = path.resolve(strict=True)
        st = path.stat()
    except OSError as exc:
        raise OfflineSessionError('private directory cannot be inspected: %s' % exc) from exc
    if resolved != path or not stat.S_ISDIR(st.st_mode):
        raise OfflineSessionError('private directory is not canonical')
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise OfflineSessionError('private directory has unsafe ownership or permissions')
    return path


def _regular(fd, label):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        raise OfflineSessionError('%s is not a safe root-owned regular file' % label)
    return st


def _repository_config(path):
    """Return the canonical OSTree repository config text, or ``None`` if absent.

    Strictly read-only: the file is inspected without following links and read
    through the same bounded helper the protected session documents use.  A
    missing config returns ``None``; callers that need an openable repository
    refuse it, while the reserve reader alone would keep libostree's default.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OfflineSessionError(
            'cannot inspect the OSTree repository config: %s' % exc) from exc
    try:
        return offline_execute._read_bounded(
            path, _REPOSITORY_CONFIG_MAX,
            'OSTree repository config').decode('ascii')
    except (ValueError, UnicodeDecodeError) as exc:
        raise OfflineSessionError(
            'cannot read the OSTree repository config: %s' % exc) from exc


def _require_repository_version(text):
    """Refuse a repository config that ``ostree pull-local`` cannot open.

    Only the format this updater writes is supported, so a config that is
    missing, has no ``[core]`` section, or names another or malformed version
    is refused instead of being planned for.  Nothing is written; the text
    already passed through the bounded ``_repository_config`` reader.
    """
    if text is None:
        raise OfflineSessionError('OSTree repository config is missing')
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise OfflineSessionError(
            'OSTree repository config is malformed: %s' % exc) from exc
    version = parser.get('core', 'repo_version', fallback=None)
    if version is None:
        raise OfflineSessionError(
            'OSTree repository config has no [core] repo_version')
    digits = version.strip()
    if not digits.isdigit() or int(digits) != _REPOSITORY_VERSION:
        raise OfflineSessionError(
            'OSTree repository repo_version is unsupported: %r' % version)


def _reserve_size_bytes(value):
    """Return the bytes named by a libostree ``min-free-space-size`` value."""
    text = value.strip().upper()
    for suffix, factor in _REPOSITORY_RESERVE_UNITS:
        if text.endswith(suffix):
            digits = text[:-len(suffix)]
            break
    else:
        digits, factor = text, 1
    if not digits.isdigit():
        raise OfflineSessionError(
            'OSTree repository reserve size is malformed: %r' % value)
    return int(digits) * factor


def _repository_reserve_minimum(text):
    """Return ``('size', bytes)`` or ``('percent', n)`` for the repository config.

    ``min-free-space-size`` takes precedence over ``min-free-space-percent``,
    the way older libostree resolves them.  Newer releases instead take the
    lesser of the two, so this precedence can overstate the reserve but never
    understate it: the budget stays deliberately conservative rather than an
    exact model of current libostree.  A zero size disables only the absolute
    form, leaving the percent or the conservative default in force.  Neither
    key being present keeps that default.  A value this module cannot honour is
    refused rather than ignored, because ignoring it would understate the
    requirement.
    """
    if text is None:
        return ('percent', _REPOSITORY_RESERVE_DEFAULT_PERCENT)
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise OfflineSessionError(
            'OSTree repository config is malformed: %s' % exc) from exc
    if not parser.has_section('core'):
        return ('percent', _REPOSITORY_RESERVE_DEFAULT_PERCENT)
    size = parser.get('core', 'min-free-space-size', fallback=None)
    if size is not None:
        reserve = _reserve_size_bytes(size)
        if reserve:
            return ('size', reserve)
    percent = parser.get('core', 'min-free-space-percent', fallback=None)
    if percent is not None:
        digits = percent.strip()
        if not digits.isdigit() or int(digits) > 99:
            raise OfflineSessionError(
                'OSTree repository reserve percent is malformed: %r' % percent)
        return ('percent', int(digits))
    return ('percent', _REPOSITORY_RESERVE_DEFAULT_PERCENT)


def _reserve_bytes(minimum, vfs):
    """Return the reserve in bytes for the filesystem described by ``vfs``."""
    kind, value = minimum
    if kind == 'size':
        return value
    return value * vfs.f_blocks * vfs.f_frsize // 100


def _strict_evidence():
    """Read and bind the firmware witness evidence; the strict rules are unchanged."""
    try:
        snapshot = armada_os.snapshot()
    except Exception as exc:
        raise _WitnessEvidenceError('cannot verify the booted slot: %s' % exc) from exc
    if not isinstance(snapshot, dict):
        raise _WitnessEvidenceError('device snapshot is not an object')
    booted = snapshot.get('booted')
    if not isinstance(booted, dict):
        raise _WitnessEvidenceError('device snapshot has no booted deployment')
    try:
        context = boot_context.read_boot_context(booted)
    except Exception as exc:
        raise _WitnessEvidenceError('cannot verify the booted slot: %s' % exc) from exc
    if not isinstance(context, dict):
        raise OfflineSessionError('boot context is not an object')
    stateroot = booted.get('stateroot')
    checksum = booted.get('checksum')
    serial = booted.get('serial')
    if (not isinstance(stateroot, str) or not isinstance(checksum, str) or
            type(serial) is not int):
        raise OfflineSessionError('booted deployment record is incomplete')
    deployment = '%s/%s.%d' % (stateroot, checksum, serial)
    if context.get('deployment') != deployment:
        raise OfflineSessionError(
            'boot context does not describe the booted deployment')
    actual = context.get('actualSlot')
    inactive = context.get('inactiveSlot')
    if actual not in ('a', 'b') or inactive != ('b' if actual == 'a' else 'a'):
        raise OfflineSessionError('boot context reports invalid slots')
    return deployment, actual, inactive


def _recovery_identity(deployment, actual, inactive):
    try:
        _dev, part_uuid, board, _rdev = recovery_device.locate_recovery_partition(
            require_edl=False)
    except Exception as exc:
        raise OfflineSessionError(
            'cannot verify the recovery partition: %s' % exc) from exc
    # The board-id table belongs to the slot precheck; do not duplicate it.
    model = physical_slots._MODEL.get(board) if type(board) is int else None
    if model not in _MODELS:
        raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
    if not isinstance(part_uuid, str) or not part_uuid:
        raise OfflineSessionError('recovery partition has no verified PARTUUID')
    return {
        'model': model,
        'actualSlot': actual,
        'inactiveSlot': inactive,
        'deployment': deployment,
        'recoveryPartUuid': part_uuid,
    }


def _legacy_bootable_slots(physical):
    """Validate the controller flags a legacy identity may report.

    Only ``info`` uses the permissive legacy inventory, so an inactive slot may
    be unbootable here.  The flags are never rewritten: an absent or
    non-boolean value is a defect, not "both bootable".
    """
    bootable = physical.get('bootableSlots')
    if not isinstance(bootable, dict) or set(bootable) != {'a', 'b'}:
        raise OfflineSessionError('legacy device identity has no verified bootableSlots')
    if any(type(bootable[slot]) is not bool for slot in ('a', 'b')):
        raise OfflineSessionError('legacy device identity reports non-boolean bootableSlots')
    if not bootable[physical['selectedSlot']]:
        raise OfflineSessionError(
            'legacy device identity reports the selected slot as not bootable')
    return {'a': bootable['a'], 'b': bootable['b']}


def _legacy_identity(physical):
    """Describe a controller-only legacy device without inventing an actual slot."""
    if not isinstance(physical, dict):
        raise OfflineSessionError('legacy physical report is not an object')
    if physical.get('bootContext') != physical_slots.LEGACY_BOOT_CONTEXT:
        raise OfflineSessionError('legacy physical report is not controller-only')
    if physical.get('actualSlot') is not None or physical.get('inactiveSlot') is not None:
        raise OfflineSessionError('legacy physical report must not claim an actual slot')
    if physical.get('model') not in _MODELS:
        raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
    selected = physical.get('selectedSlot')
    if selected not in ('a', 'b'):
        raise OfflineSessionError('legacy physical report has no selected slot')
    deployment = physical.get('deployment')
    if not isinstance(deployment, str) or not deployment:
        raise OfflineSessionError('legacy device identity has no verified deployment')
    part_uuid = physical.get('recoveryPartUuid')
    if not isinstance(part_uuid, str) or not part_uuid:
        raise OfflineSessionError('recovery partition has no verified PARTUUID')
    return {
        'model': physical['model'],
        'actualSlot': None,
        'inactiveSlot': None,
        'selectedSlot': selected,
        'bootContext': physical_slots.LEGACY_BOOT_CONTEXT,
        'deployment': deployment,
        'recoveryPartUuid': part_uuid,
        'bootableSlots': _legacy_bootable_slots(physical),
    }


def read_device_identity():
    """Read the running system's identity for read-only queries.

    ``inspect_physical_slots`` deliberately fails while the controller's selected
    slot differs from the slot the machine booted from.  That is the expected
    state between a successful activation of the inactive slot and the reboot, so
    queries made in that window must not reuse the strict mutation precheck.  The
    identity here comes from the same verified evidence the boot precheck trusts:
    the booted deployment record, the firmware witness and cmdline binding, and
    the located internal recovery partition.  Nothing is cached from a previous
    session and no slot is inferred from the controller's selection.
    """
    return _recovery_identity(*_strict_evidence())


def read_offline_device_identity():
    """Identity used by the offline service.

    A system with witness evidence keeps the strict identity above.  A legacy
    0.7 system is inspected only when neither the witness nor an
    ``armada.deployment`` argument exists, and then only the controller's
    selected slot is reported.  That inventory is read-only and permissive
    about the inactive slot: info must identify a running device even when the
    inactive slot is not bootable yet, because that is a future upgrade
    blocker, not an identity defect.  It is also the only inspector that
    tolerates a staged OSTree deployment; mutation callers keep the strict
    both-bootable and staged-deployment precheck.
    """
    try:
        evidence = _strict_evidence()
    except _WitnessEvidenceError:
        if offline_context.new_evidence_present():
            raise
        try:
            physical = physical_slots.inspect_legacy_identity_slots()
        except Exception as exc:
            raise OfflineSessionError('cannot verify the booted slot: %s' % exc) from exc
        return _legacy_identity(physical)
    return _recovery_identity(*evidence)


class OfflineSessionStore:
    """Transport-independent storage; authorization belongs to the stdio helper."""

    def __init__(self, base=_DEFAULT_BASE, trusted_key=signature.DEFAULT_TRUSTED_KEY,
                 inspect=_DEFAULT_INSPECT, info_reader=None, snapshot=None,
                 sysroot_path=_DEFAULT_SYSROOT):
        if os.geteuid() != 0:
            raise PermissionError('offline session storage requires euid 0')
        self.base = Path(base)
        if not self.base.is_absolute() or self.base.is_symlink():
            raise ValueError('offline session base must be an absolute non-symlink path')
        self.trusted_key = Path(trusted_key)
        self._inspect = inspect
        self._info_reader = info_reader
        self._snapshot = snapshot
        self.sysroot_path = Path(sysroot_path)
        self.updater_version = _UPDATER_VERSION

    def _snapshot_state(self):
        if self._snapshot is not None:
            return self._snapshot()
        # The booted deployment lives in the default runtime sysroot; the
        # configured /sysroot path is only a mount for image work, not the
        # libostree sysroot that owns the booted deployment.
        return offline_context.snapshot()

    def _physical_info(self):
        physical = self._inspect()
        if not isinstance(physical, dict) or physical.get('model') not in _MODELS:
            raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
        return physical

    def _require_identity(self, physical):
        if not isinstance(physical, dict) or physical.get('model') not in _MODELS:
            raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
        if physical.get('bootContext') == physical_slots.LEGACY_BOOT_CONTEXT:
            if physical.get('actualSlot') is not None or physical.get('inactiveSlot') is not None:
                raise OfflineSessionError('legacy device identity must not claim an actual slot')
            for field in ('model', 'deployment', 'recoveryPartUuid', 'selectedSlot'):
                value = physical.get(field)
                if not isinstance(value, str) or not value:
                    raise OfflineSessionError('device identity has no verified %s' % field)
            if physical['selectedSlot'] not in ('a', 'b'):
                raise OfflineSessionError('device identity reports an invalid selected slot')
            _legacy_bootable_slots(physical)
            return physical
        for field in ('model', 'actualSlot', 'inactiveSlot', 'deployment',
                      'recoveryPartUuid'):
            value = physical.get(field)
            if not isinstance(value, str) or not value:
                raise OfflineSessionError('device identity has no verified %s' % field)
        actual = physical['actualSlot']
        if actual not in ('a', 'b') or physical['inactiveSlot'] != (
                'b' if actual == 'a' else 'a'):
            raise OfflineSessionError('device identity reports invalid slots')
        return physical

    def _info_physical(self):
        """Identity for read-only info; mutation paths keep the strict precheck."""
        if self._info_reader is not None:
            return self._require_identity(self._info_reader())
        if self._inspect is _DEFAULT_INSPECT:
            return self._require_identity(read_offline_device_identity())
        # A caller that supplied its own inspector is describing the device for
        # this store, so info keeps reading that fixture instead of the host.
        return self._require_identity(self._inspect())

    def _channel_info(self):
        return system_transaction._snapshot_channel(self._snapshot_state())

    def _new_session_id(self):
        return uuid.uuid4().hex

    def info(self):
        physical = self._info_physical()
        snapshot = self._snapshot_state()
        result = {
            'schemaVersion': 1,
            'updaterVersion': self.updater_version,
            'model': physical['model'],
            'deployment': physical['deployment'],
            'recoveryPartUuid': physical['recoveryPartUuid'],
            'channel': system_transaction._snapshot_channel(snapshot),
        }
        if physical.get('bootContext') == physical_slots.LEGACY_BOOT_CONTEXT:
            # Pending OSTree state is reported as data instead of hiding the
            # device: info must identify a connected legacy system even when a
            # staged deployment or a queued rollback blocks a later upgrade.
            result.update(actualSlot=None, inactiveSlot=None,
                          selectedSlot=physical['selectedSlot'],
                          bootContext=physical['bootContext'],
                          bootableSlots=dict(physical['bootableSlots']),
                          staged=snapshot.get('staged'),
                          rollbackQueued=bool(snapshot.get('rollback_queued')))
        else:
            result.update(actualSlot=physical['actualSlot'],
                          inactiveSlot=physical['inactiveSlot'])
        return result

    def _canonical_sysroot(self):
        path = Path(self.sysroot_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise OfflineSessionError('sysroot must be an absolute non-symlink directory')
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise OfflineSessionError('cannot resolve the sysroot: %s' % exc) from exc
        if resolved != path:
            raise OfflineSessionError('sysroot path is not canonical')
        return path

    @staticmethod
    def _check_updater_dependencies():
        required = [(Path(path), False) for path in _REQUIRED_UPDATER_FILES]
        required += [(Path(path), True) for path in _REQUIRED_UPDATER_EXECUTABLES]
        for path, executable in required:
            try:
                entry = os.lstat(path)
            except OSError as exc:
                raise OfflineSessionError(
                    'missing updater dependency: %s' % path) from exc
            if stat.S_ISLNK(entry.st_mode):
                # /usr/bin/python3 is a normal root-owned distro symlink on
                # Fedora.  Resolve trusted executable symlinks to their regular
                # target instead of rejecting Python; a dangling or non-regular
                # target still fails.  Uploaded payload and archive-member
                # symlinks keep their own strict O_NOFOLLOW protections.
                try:
                    checked = path.resolve(strict=True)
                    target = os.stat(checked)
                except OSError as exc:
                    raise OfflineSessionError(
                        'missing updater dependency: %s' % path) from exc
                if not stat.S_ISREG(target.st_mode):
                    raise OfflineSessionError(
                        'missing updater dependency: %s' % path)
            elif stat.S_ISREG(entry.st_mode):
                checked = path
            else:
                raise OfflineSessionError('missing updater dependency: %s' % path)
            if executable and not os.access(checked, os.X_OK):
                raise OfflineSessionError(
                    'missing executable updater dependency: %s' % path)

    def _preflight_session_bytes(self, session, document, signature_b64,
                                 physical, channel, payload_path, size, digest):
        """Return the payload bytes already stored by exactly this session.

        Strictly read-only: no session lock (which would create ``.lock``), no
        receipt update, no job and no write of any kind.  Only the protected
        directory named by the authenticated ``sessionId`` is inspected, so a
        caller can never credit bytes of another session, package or device.
        """
        try:
            stored_document = offline_execute._read_bounded(
                session / 'package.json', _DOCUMENT_MAX, 'session package document')
            stored_signature = offline_execute._read_bounded(
                session / 'package.sig', _SIGNATURE_MAX, 'session package signature')
            receipt = self.read_receipt(session)
        except ValueError as exc:
            raise OfflineSessionError(
                'cannot read the protected session: %s' % exc) from exc
        # The session must have been created for these exact signed bytes; an
        # identical-looking descriptor for another package is a different one.
        if stored_document != document:
            raise OfflineSessionError(
                'session belongs to a different package document')
        if stored_signature != signature_b64:
            raise OfflineSessionError(
                'session belongs to a different package signature')
        # Bind the receipt's device, bridge and slot context to the device that
        # answers now, exactly as the prepare submission does.
        if receipt.get('physical') != physical:
            raise OfflineSessionError('session belongs to a different device state')
        if receipt.get('model') != physical.get('model'):
            raise OfflineSessionError('session belongs to a different device model')
        if receipt.get('channel') != channel:
            raise OfflineSessionError('session was created for a different channel')
        if receipt.get('jobInterrupted') or receipt.get('attentionRequired'):
            raise OfflineSessionError(
                'interrupted session cannot be preflighted read-only')
        state = receipt.get('state')
        if state not in _PREFLIGHT_SESSION_STATES:
            raise OfflineSessionError(
                'session state %r cannot be preflighted read-only' % (state,))
        payload = receipt.get('payload')
        if not isinstance(payload, dict):
            raise OfflineSessionError('session receipt has no payload record')
        # The stored path, declared size and hash must be the signed descriptor's
        # own values; the receipt alone never authorizes a different payload.
        if (payload.get('path') != payload_path or
                payload.get('bytes') != size or
                payload.get('sha256') != digest):
            raise OfflineSessionError(
                'session payload does not match the signed descriptor')
        return self._received(session, receipt)

    def _prepared_expansion_evidence(self, session, document, expanded_bytes):
        """Return True only when this session proved its prepare completed.

        The session receipt is never enough: the protected transaction's own
        ``prepared.json`` and ``status.json`` must bind the completed prepare to
        exactly these signed bytes, and the expanded image must still be a
        regular file of the declared size.  ``awaiting_recovery_export`` is only
        written once the expansion, the ``btrfs restore`` extraction and the
        ``ostree pull-local`` import all succeeded, so those allocations are
        already paid for.  Anything missing or foreign stays charged in full,
        and the current device binding comes from the receipt checked before
        this call, never from the receipt alone.
        """
        transaction = session / 'transaction'
        if transaction.is_symlink() or not transaction.is_dir():
            return False
        try:
            prepared = json.loads(offline_execute._read_bounded(
                transaction / 'prepared.json', _JSON_MAX,
                'prepared transaction').decode('ascii'))
            status = json.loads(offline_execute._read_bounded(
                transaction / 'status.json', _JSON_MAX,
                'transaction status').decode('ascii'))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        if not isinstance(prepared, dict) or not isinstance(status, dict):
            return False
        if (prepared.get('schemaVersion') != 1 or
                prepared.get('operation') != 'offline-upgrade' or
                prepared.get('packageDescriptor') != 'package.json'):
            return False
        transaction_id = prepared.get('transactionId')
        if (type(transaction_id) is not str or not transaction_id or
                status.get('transactionId') != transaction_id):
            return False
        # Only the state that still owns the completed preparation counts; an
        # executed, failed or interrupted transaction is refused earlier.
        if status.get('state') != 'awaiting_recovery_export':
            return False
        if prepared.get('packageDescriptorSha256') != hashlib.sha256(document).hexdigest():
            return False
        import_dir = transaction / 'import'
        if import_dir.is_symlink() or not import_dir.is_dir():
            return False
        try:
            entry = os.lstat(import_dir / 'linux-root.img')
        except OSError:
            return False
        return stat.S_ISREG(entry.st_mode) and entry.st_size == expanded_bytes

    @staticmethod
    def _allocation_dir(path, label):
        """Nearest existing canonical directory holding *path*, read-only.

        A session or transaction leaf is created only by prepare, so its
        filesystem is attributed to the nearest ancestor that exists now.
        Every path segment is inspected without following links: a symlinked or
        non-directory component is refused instead of being resolved, and
        nothing is created, locked or written here.
        """
        path = Path(path)
        if not path.is_absolute():
            raise OfflineSessionError('%s must be an absolute path' % label)
        current = Path(path.anchor)
        for part in path.parts[1:]:
            candidate = current / part
            try:
                entry = os.lstat(candidate)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise OfflineSessionError(
                    'cannot inspect %s: %s' % (label, exc)) from exc
            if stat.S_ISLNK(entry.st_mode):
                raise OfflineSessionError('%s has a symlinked path segment' % label)
            if not stat.S_ISDIR(entry.st_mode):
                raise OfflineSessionError(
                    '%s has a non-directory path segment' % label)
            current = candidate
        try:
            if current.resolve(strict=True) != current:
                raise OfflineSessionError('%s is not canonical' % label)
        except OSError as exc:
            raise OfflineSessionError(
                'cannot resolve %s: %s' % (label, exc)) from exc
        return current

    @staticmethod
    def _filesystem_identity(directory, label):
        """Return a grouping key for the filesystem that holds *directory*.

        Every Btrfs subvolume reports its own ``st_dev`` although all of them
        draw on one pool, and ``statfs`` XORs the subvolume root id into
        ``f_fsid``.  Only BTRFS_IOC_FS_INFO exposes the filesystem-wide FSID, so
        it is read through a read-only directory descriptor.  A non-Btrfs
        filesystem answers ENOTTY and keeps its block-device identity; any
        other failure is refused instead of being treated as a separate
        filesystem.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(directory, flags)
        except OSError as exc:
            raise OfflineSessionError(
                'cannot open %s storage: %s' % (label, exc)) from exc
        try:
            info = bytearray(_BTRFS_FS_INFO_SIZE)
            try:
                fcntl.ioctl(fd, _BTRFS_IOCTL_FS_INFO, info)
            except OSError as exc:
                if exc.errno != errno.ENOTTY:
                    raise OfflineSessionError(
                        'cannot identify the %s filesystem: %s'
                        % (label, exc)) from exc
                try:
                    return ('device', os.stat(directory).st_dev)
                except OSError as stat_exc:
                    raise OfflineSessionError(
                        'cannot inspect %s storage: %s'
                        % (label, stat_exc)) from stat_exc
            fsid = bytes(info[_BTRFS_FSID_OFFSET:
                              _BTRFS_FSID_OFFSET + _BTRFS_FSID_SIZE])
            if not any(fsid):
                raise OfflineSessionError(
                    '%s storage reports an empty filesystem FSID' % label)
            return ('btrfs', fsid)
        finally:
            os.close(fd)

    @staticmethod
    def _require_free_space(destinations, reserve=None):
        """Check each write destination's own filesystem, never a proxy.

        ``destinations`` are ``(directory, bytes, label)`` triples for the
        storage this preflight is about to consume.  Requirements that land on
        one filesystem are summed once, so a shared pool is never
        double-charged, while distinct filesystems are checked independently.
        Each path in a group must also report enough of its own available space
        for the summed requirement, so a subvolume quota is never overruled by
        the pool total.

        ``reserve`` is an optional ``(directory, minimum)`` pair describing the
        space the OSTree repository at ``directory`` keeps for itself.  It is
        charged once, on that directory's own filesystem.
        """
        groups = {}
        for directory, required, label in destinations:
            if required <= 0:
                continue
            identity = OfflineSessionStore._filesystem_identity(directory, label)
            group = groups.setdefault(identity, [[], 0])
            group[0].append(directory)
            group[1] += required
        reserve_identity = None
        reserve_bytes = 0
        if reserve is not None:
            directory, minimum = reserve
            reserve_identity = OfflineSessionStore._filesystem_identity(
                directory, 'repository reserve')
            try:
                vfs = os.statvfs(directory)
            except OSError as exc:
                raise OfflineSessionError(
                    'cannot check free space on %s: %s'
                    % (directory, exc)) from exc
            reserve_bytes = _reserve_bytes(minimum, vfs)
            group = groups.setdefault(reserve_identity, [[], 0])
            group[0].append(directory)
        for identity, (paths, required) in groups.items():
            if identity == reserve_identity:
                required += reserve_bytes
            available = []
            for directory in paths:
                try:
                    vfs = os.statvfs(directory)
                except OSError as exc:
                    raise OfflineSessionError(
                        'cannot check free space on %s: %s'
                        % (directory, exc)) from exc
                available.append(vfs.f_bavail * vfs.f_frsize)
            if required and (not available or min(available) < required):
                raise OfflineSessionError(
                    'insufficient free space for the payload, its expansion '
                    'and the repository import')

    def _check_repository(self, repository, config_text):
        """Refuse an existing repository that ``ostree pull-local`` cannot open.

        The empty ``ostree/repo`` a fresh sysroot may leave, or a repository
        written by an unsupported libostree, would fail the actual prepare
        after every allocation was promised.  Only the two things the import
        needs are proven here, read-only through the existing path-safety
        helpers: the required ``objects`` directory and a bounded config
        naming the supported format.  Nothing is created or rewritten.
        """
        objects = self._allocation_dir(
            repository / 'objects', 'repository objects')
        missing = repository / 'objects'
        if objects != missing:
            raise OfflineSessionError(
                'repository objects directory is missing: %s' % missing)
        _require_repository_version(config_text)

    def preflight(self, document: bytes, signature_b64: bytes, sessionId=None):
        """Read-only readiness check: no session, no staging, no device write.

        The result shape is fixed: ``schemaVersion``, ``phase``,
        ``bootWriteState``, ``model``, ``bootContext``, ``actualSlot``,
        ``selectedSlot``, ``currentDeployment``, ``recoveryPartUuid``,
        ``recoveryDescriptorVersion``, ``recoveryMigrationRequired``,
        ``channel`` and optional ``channelInferred``.  When the caller names an
        existing session, ``sessionId`` is echoed as well.

        ``sessionId`` is optional and only ever selects one existing protected
        session by name.  With it, the receipt, its package document/signature
        and the regular payload file already stored there are bound to the
        authenticated package and the current device, and only those validated
        bytes are credited against the free-space requirement.  Nothing is
        created, written, locked or started; the first upgrade of a device
        keeps the full compressed, expanded and repository requirement.

        Space is checked on every filesystem that actually receives data: the
        session's protected directory for uploads, the transaction storage that
        holds the expanded image, the staging tree ``btrfs restore`` extracts
        the package repository into, and the ``sysroot/ostree/repo`` that
        ``ostree pull-local`` fills.  The repository must already exist and be
        openable: an ``objects`` directory plus a bounded ``config`` declaring
        ``[core] repo_version=1``; preflight never creates or rewrites it.
        Allocations on one filesystem are summed once, keyed by the Btrfs pool
        FSID so subvolumes with different ``st_dev`` are not split, and each
        path must still report enough of its own available space.  The repository destination also keeps the
        repository's own reserve, charged once, and distinct filesystems are
        checked independently.
        The authenticated ``expandedBytes`` is the planning allowance for the
        expansion, the extraction and the import; it is an estimate rather than
        a guarantee, because a compressed Btrfs image can hold more logical data
        than its expanded size.  A session whose protected transaction
        documents prove a completed preparation pays nothing again for the
        expansion, extraction or import it already finished.

        A controller-only legacy system reports ``actualSlot: null`` plus its
        selected slot; no slot is ever inferred from the controller's
        selection.  The bound recovery journal is read with the same rules
        prepare and execute use, so a legacy v1/v2 descriptor is reported as
        needing migration rather than being ignored; nothing is written here.
        """
        if type(document) is not bytes or not 0 < len(document) <= _DOCUMENT_MAX:
            raise ValueError('document must be nonempty bytes of at most 2 MiB')
        if type(signature_b64) is not bytes or not 0 < len(signature_b64) <= _SIGNATURE_MAX:
            raise ValueError('signature must be nonempty bytes of at most 16 KiB')
        if sessionId is not None and (
                type(sessionId) is not str or not _SESSION_ID.fullmatch(sessionId)):
            raise ValueError(
                'sessionId must be exactly 32 lowercase hexadecimal characters')
        physical = self._require_identity(self._physical_info())
        snapshot = self._snapshot_state()
        # A pending OSTree transaction blocks prepare, execute, the boot
        # writer and activation.  The injected/mocked inspector cannot prove
        # the device is idle, and the modern snapshot carries the same fields,
        # so preflight rejects the snapshot before package, dependency or
        # recovery work instead of claiming readiness prepare would refuse.
        if isinstance(snapshot, dict):
            if snapshot.get('staged') or snapshot.get('rollback_queued'):
                raise OfflineSessionError(
                    'a pending OSTree transaction refuses an offline upgrade')
            # prepare refuses a repository outside the trusted pair, so
            # reporting readiness before this check would promise an upgrade
            # that prepare must reject.  Both checks precede all expensive work.
            if snapshot.get('repository') not in armada_os.REPOSITORIES:
                raise OfflineSessionError(
                    'configured OSTree repository is not supported')
        # ``ostree pull-local`` writes into the canonical repository that
        # already exists; the nearest existing ancestor is only an allocation
        # target for a planned session or transaction, never for the
        # repository.  Rejecting it here keeps preflight read-only and stops a
        # missing repository from being charged to the sysroot instead.
        root = self._canonical_sysroot()
        repository = root / 'ostree' / 'repo'
        repository_dir = self._allocation_dir(
            repository, 'repository destination')
        if repository_dir != repository:
            raise OfflineSessionError(
                'repository destination does not exist: %s' % repository)
        # An existing directory is not yet a repository: prove its layout and
        # format before the package, storage and journal work claims readiness.
        # The same bounded config text is reused for the reserve below.
        repository_config = _repository_config(repository / 'config')
        self._check_repository(repository, repository_config)
        channel = system_transaction._snapshot_channel(snapshot)
        package = package_contract.load_package(
            document, signature_b64, trusted_key=self.trusted_key)
        package_contract.require_operation(
            package, physical['model'], 'offline-upgrade',
            updater_version=self.updater_version)
        parts, size, digest, expanded_size, _expanded_sha = \
            offline_payload._select_payload_entry(package)
        stored_bytes = 0
        prepared = False
        if sessionId is not None:
            session = self.resolve_session(sessionId)
            stored_bytes = self._preflight_session_bytes(
                session, document, signature_b64, physical, channel,
                '/'.join(parts), size, digest)
            prepared = self._prepared_expansion_evidence(
                session, document, expanded_size)
            # An existing session keeps its bytes and its expansion in its own
            # protected directory, so that is the storage a resume consumes.
            upload_dir = self._allocation_dir(session, 'session storage')
            transaction = session / 'transaction'
        else:
            # The session does not exist yet: attribute every allocation to the
            # nearest existing ancestor of the base and never create it from a
            # read-only preflight.
            upload_dir = self._allocation_dir(self.base, 'session base')
            transaction = self.base
        # btrfs restore writes the extracted package repository below
        # ``transaction/import/import/content`` and ostree pull-local then
        # copies it into ``sysroot/ostree/repo``, so both trees need their own
        # capacity.  The authenticated expandedBytes is the planning allowance
        # for each; it is an estimate, not a guarantee, because a compressed
        # Btrfs image can hold more logical data than its expanded size.
        expansion_dir = self._allocation_dir(transaction, 'transaction storage')
        extraction_dir = self._allocation_dir(
            transaction / 'import' / 'import', 'repository staging storage')
        allocations = [(upload_dir, size - stored_bytes, 'payload upload')]
        reserve = None
        if not prepared:
            allocations += [
                (expansion_dir, expanded_size, 'payload expansion'),
                (extraction_dir, expanded_size, 'repository extraction'),
                (repository_dir, expanded_size, 'repository import'),
            ]
            # The reserve is the repository's own; it is read from the
            # canonical config and never written back.
            reserve = (repository_dir, _repository_reserve_minimum(
                repository_config))
        self._require_free_space(allocations, reserve=reserve)
        self._check_updater_dependencies()
        # Read the journal and its descriptor version with the same read-only
        # check prepare uses.  Reporting readiness before this would claim a
        # legacy upgrade can start when the descriptor still blocks it.
        journal = offline_prepare._check_recovery(
            physical, system_transaction._binding(physical['deployment']))
        descriptor_version = journal.layout.descriptor_version
        result = {
            'schemaVersion': 1,
            'phase': 'preflight_passed',
            'bootWriteState': 'not_started',
            'model': physical['model'],
            'bootContext': physical.get('bootContext',
                                        offline_context.WITNESS_BOOT_CONTEXT),
            'actualSlot': physical.get('actualSlot'),
            'selectedSlot': physical.get('selectedSlot', physical.get('actualSlot')),
            'currentDeployment': physical['deployment'],
            'recoveryPartUuid': physical['recoveryPartUuid'],
            'recoveryDescriptorVersion': descriptor_version,
            'recoveryMigrationRequired': (
                descriptor_version != trial_record.DESCRIPTOR_MIN_VERSION),
            'channel': channel,
        }
        if isinstance(snapshot, dict) and snapshot.get('channelInferred'):
            result['channelInferred'] = True
        if sessionId is not None:
            result['sessionId'] = sessionId
        return result

    def _ensure_base(self):
        if self.base.exists() or self.base.is_symlink():
            return _check_dir(self.base)
        missing = []
        current = self.base
        while not current.exists() and not current.is_symlink():
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise OfflineSessionError('offline session base has no existing ancestor')
            current = parent
        _check_dir(current)
        for path in reversed(missing):
            os.mkdir(path, 0o700)
            _check_dir(path)
            _fsync_dir(path.parent)
        return _check_dir(self.base)

    def resolve_session(self, session_id):
        if type(session_id) is not str or not _SESSION_ID.fullmatch(session_id):
            raise ValueError('sessionId must be exactly 32 lowercase hexadecimal characters')
        _check_dir(self.base)
        return _check_dir(self.base / session_id)

    def transaction_dir(self, session_id):
        session = self.resolve_session(session_id)
        transaction = session / 'transaction'
        if transaction.exists() or transaction.is_symlink():
            _check_dir(transaction)
        return transaction

    @contextmanager
    def session_lock(self, session_id):
        session = self.resolve_session(session_id)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(session / '.lock', flags, 0o600)
        try:
            _regular(fd, 'session lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield session
        finally:
            os.close(fd)

    def read_receipt(self, session):
        session = self.resolve_session(session) if isinstance(session, str) else Path(session)
        raw = offline_execute._read_bounded(session / 'receipt.json', _JSON_MAX, 'receipt')
        receipt = json.loads(raw.decode('ascii'))
        if (not isinstance(receipt, dict) or receipt.get('schemaVersion') != 1 or
                receipt.get('sessionId') != session.name):
            raise OfflineSessionError('invalid session receipt')
        return receipt

    def update_receipt(self, session, receipt):
        session = self.resolve_session(session) if isinstance(session, str) else Path(session)
        if receipt.get('sessionId') != session.name:
            raise ValueError('receipt session binding mismatch')
        system_transaction._save_json(session, 'receipt.json', receipt)
        return receipt

    def begin(self, document: bytes, signature_b64: bytes):
        if type(document) is not bytes or not 0 < len(document) <= _DOCUMENT_MAX:
            raise ValueError('document must be nonempty bytes of at most 2 MiB')
        if type(signature_b64) is not bytes or not 0 < len(signature_b64) <= _SIGNATURE_MAX:
            raise ValueError('signature must be nonempty bytes of at most 16 KiB')
        physical = self._physical_info()
        channel = self._channel_info()
        package = package_contract.load_package(
            document, signature_b64, trusted_key=self.trusted_key)
        package_contract.require_operation(
            package, physical['model'], 'offline-upgrade',
            updater_version=self.updater_version)
        parts, size, digest, _expanded_size, _expanded_sha = \
            offline_payload._select_payload_entry(package)
        base = self._ensure_base()
        free = os.statvfs(base).f_bavail * os.statvfs(base).f_frsize
        if free < size:
            raise OfflineSessionError('insufficient free space for compressed payload')

        session_id = self._new_session_id()
        session = base / session_id
        os.mkdir(session, 0o700)
        _check_dir(session)
        _write_new(session / 'package.json', document)
        _write_new(session / 'package.sig', signature_b64)
        current = session / 'incoming'
        os.mkdir(current, 0o700)
        _fsync_dir(session)
        for part in parts[:-1]:
            parent = current
            current = current / part
            os.mkdir(current, 0o700)
            _fsync_dir(parent)
        _check_dir(current)
        payload_path = '/'.join(parts)
        receipt = {
            'schemaVersion': 1, 'sessionId': session_id, 'state': 'uploading',
            'model': physical['model'], 'channel': channel, 'physical': physical,
            'payload': {'path': payload_path, 'bytes': size, 'sha256': digest},
            'receivedBytes': 0, 'transactionDir': 'transaction',
        }
        system_transaction._save_json(session, 'receipt.json', receipt)
        _fsync_dir(session)
        _fsync_dir(base)
        return receipt

    def _payload_path(self, session, receipt):
        raw = receipt['payload']['path']
        if type(raw) is not str or not raw or '\\' in raw:
            raise OfflineSessionError('invalid payload path in receipt')
        parts = raw.split('/')
        if any(part in ('', '.', '..') for part in parts):
            raise OfflineSessionError('invalid payload path in receipt')
        path = session / 'incoming'
        _check_dir(path)
        for part in parts[:-1]:
            path = path / part
            _check_dir(path)
        return path / parts[-1]

    def _received(self, session, receipt):
        path = self._payload_path(session, receipt)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                         getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return 0
        try:
            size = _regular(fd, 'payload').st_size
        finally:
            os.close(fd)
        if size > receipt['payload']['bytes']:
            raise OfflineSessionError('stored payload exceeds signed payload size')
        return size

    def status(self, sessionId):
        with self.session_lock(sessionId) as session:
            receipt = self.read_receipt(session)
            if receipt['state'] == 'uploading':
                receipt['receivedBytes'] = self._received(session, receipt)
            transaction = session / 'transaction'
            if transaction.exists():
                _check_dir(transaction)
                status_path = transaction / 'status.json'
                if status_path.exists():
                    raw = offline_execute._read_bounded(status_path, _JSON_MAX, 'transaction status')
                    receipt['transaction'] = json.loads(raw.decode('ascii'))
                else:
                    receipt['transaction'] = {'state': 'queued', 'statusAvailable': False}
            return receipt

    def upload(self, sessionId, offset: int, data: bytes):
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be a nonnegative integer')
        if type(data) is not bytes or not 0 < len(data) <= _CHUNK_MAX:
            raise ValueError('data must contain 1 byte through 1 MiB')
        with self.session_lock(sessionId) as session:
            receipt = self.read_receipt(session)
            if receipt.get('state') != 'uploading':
                raise OfflineSessionError('session is not accepting uploads')
            expected = receipt['payload']['bytes']
            if offset + len(data) > expected:
                raise OfflineSessionError('upload exceeds signed payload size')
            path = self._payload_path(session, receipt)
            flags = (os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC |
                     getattr(os, 'O_NONBLOCK', 0))
            fd = os.open(path, flags, 0o600)
            try:
                st = _regular(fd, 'payload')
                if st.st_size != offset:
                    raise OfflineSessionError('upload offset conflicts with stored payload')
                view = memoryview(data)
                position = offset
                while view:
                    count = os.pwrite(fd, view, position)
                    if count <= 0:
                        raise OSError('short payload write')
                    view = view[count:]
                    position += count
                os.fsync(fd)
                _fsync_dir(path.parent)
                return {'receivedBytes': os.fstat(fd).st_size}
            finally:
                os.close(fd)

    def seal(self, sessionId):
        with self.session_lock(sessionId) as session:
            receipt = self.read_receipt(session)
            if receipt.get('state') == 'uploaded':
                return receipt
            if receipt.get('state') != 'uploading':
                raise OfflineSessionError('session cannot be sealed in its current state')
            expected = receipt['payload']
            path = self._payload_path(session, receipt)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                         getattr(os, 'O_NONBLOCK', 0))
            try:
                st = _regular(fd, 'payload')
                if st.st_size != expected['bytes']:
                    raise OfflineSessionError('payload size does not match signed descriptor')
                hasher = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(fd, min(_CHUNK_MAX, expected['bytes'] + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected['bytes']:
                        raise OfflineSessionError('payload grew while sealing')
                    hasher.update(chunk)
                if total != expected['bytes'] or hasher.hexdigest() != expected['sha256']:
                    raise OfflineSessionError('payload sha256/size mismatch')
            finally:
                os.close(fd)
            receipt.update(state='uploaded', receivedBytes=expected['bytes'])
            return self.update_receipt(session, receipt)

    def _export_entry(self, transaction, path):
        if path in ('plan.json', 'recovery-manifest.json'):
            return {'path': path, 'bytes': None}
        raw = offline_execute._read_bounded(
            transaction / 'recovery-manifest.json', _JSON_MAX, 'recovery manifest')
        manifest = json.loads(raw.decode('ascii'))
        files = manifest.get('files') if isinstance(manifest, dict) else None
        if not isinstance(files, list):
            raise OfflineSessionError('recovery manifest has no validated file list')
        matches = [entry for entry in files if isinstance(entry, dict) and entry.get('path') == path]
        if len(matches) != 1:
            raise OfflineSessionError('path is not approved for recovery export')
        entry = matches[0]
        if (type(entry.get('bytes')) is not int or entry['bytes'] <= 0 or
                type(entry.get('sha256')) is not str or len(entry['sha256']) != 64):
            raise OfflineSessionError('recovery manifest entry is invalid')
        return entry

    def export(self, sessionId, path, offset, length):
        if type(path) is not str or not path:
            raise ValueError('path must be a nonempty string')
        if type(offset) is not int or offset < 0 or type(length) is not int or not 0 < length <= _CHUNK_MAX:
            raise ValueError('invalid export offset or length')
        if path.startswith('/') or '\\' in path:
            raise ValueError('invalid recovery export path')
        parts = path.split('/')
        if any(part in ('', '.', '..') for part in parts):
            raise ValueError('invalid recovery export path')
        with self.session_lock(sessionId) as session:
            transaction = session / 'transaction'
            _check_dir(transaction)
            raw = offline_execute._read_bounded(
                transaction / 'status.json', _JSON_MAX, 'transaction status')
            tx_status = json.loads(raw.decode('ascii'))
            if tx_status.get('state') not in _EXPORT_STATES:
                raise OfflineSessionError('transaction is not ready for recovery export')
            entry = self._export_entry(transaction, path)
            directory = transaction
            for part in parts[:-1]:
                directory = directory / part
                _check_dir(directory)
            source = directory / parts[-1]
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, 'O_NONBLOCK', 0)
            fd = os.open(source, flags)
            try:
                st = _regular(fd, 'recovery export source')
                if entry['bytes'] is None:
                    if st.st_size > _JSON_MAX:
                        raise OfflineSessionError('recovery export metadata exceeds 2 MiB cap')
                elif st.st_size != entry['bytes']:
                    raise OfflineSessionError('recovery export length changed')
                if offset > st.st_size:
                    raise ValueError('offset exceeds recovery export size')
                remaining = min(length, st.st_size - offset)
                chunks = []
                position = offset
                while remaining > 0:
                    chunk = os.pread(fd, min(_CHUNK_MAX, remaining), position)
                    if not chunk:
                        raise OfflineSessionError('recovery export source truncated during read')
                    chunks.append(chunk)
                    position += len(chunk)
                    remaining -= len(chunk)
                final = os.fstat(fd)
                if (final.st_dev, final.st_ino, final.st_size) != (st.st_dev, st.st_ino, st.st_size):
                    raise OfflineSessionError('recovery export source changed during read')
                return b''.join(chunks)
            finally:
                os.close(fd)
