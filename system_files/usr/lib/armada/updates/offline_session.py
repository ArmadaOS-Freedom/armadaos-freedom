# SPDX-License-Identifier: GPL-3.0-only
"""Private, authenticated offline-update payload and recovery-export storage."""

from __future__ import annotations

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
    boot_context,
    offline_execute,
    offline_payload,
    package_contract,
    physical_slots,
    recovery_device,
    signature,
    system_transaction,
    trial_record,
)

_DEFAULT_BASE = Path('/var/lib/armada-updates/offline')
_UPDATER_VERSION = '0.8.0'
_DOCUMENT_MAX = 2 * 1024 * 1024
_SIGNATURE_MAX = 16 * 1024
_JSON_MAX = 2 * 1024 * 1024
_CHUNK_MAX = 1024 * 1024
_SESSION_ID = re.compile(r'[0-9a-f]{32}\Z', re.ASCII)
_MODELS = frozenset({'TB322FC', 'TB323FU'})
_EXPORT_STATES = frozenset({
    'awaiting_recovery_export', 'executing', 'awaiting_reboot', 'failed',
    'recovery_required',
})
# info() treats exactly this inspector as "not injected" and reads the device.
_DEFAULT_INSPECT = physical_slots.inspect_physical_slots


class OfflineSessionError(RuntimeError):
    """An offline session cannot safely perform the requested operation."""


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


def read_device_identity():
    """Read the running system's identity for read-only queries.

    ``inspect_physical_slots`` is the mutation precheck: it opens and
    cross-checks every LUN and boot extent.  A read-only query does not need
    that full precheck, so it uses the same verified evidence the boot precheck
    trusts without duplicating it: the booted deployment record, the firmware
    witness and cmdline binding, and the located internal recovery partition.
    Nothing is cached from a previous session and no slot is inferred from the
    controller's selection.
    """
    snapshot = armada_os.snapshot()
    if not isinstance(snapshot, dict):
        raise OfflineSessionError('device snapshot is not an object')
    booted = snapshot.get('booted')
    if not isinstance(booted, dict):
        raise OfflineSessionError('device snapshot has no booted deployment')
    try:
        context = boot_context.read_boot_context(booted)
    except Exception as exc:
        raise OfflineSessionError('cannot verify the booted slot: %s' % exc) from exc
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


class OfflineSessionStore:
    """Transport-independent storage; authorization belongs to the stdio helper."""

    def __init__(self, base=_DEFAULT_BASE, trusted_key=signature.DEFAULT_TRUSTED_KEY,
                 inspect=physical_slots.inspect_physical_slots, info_reader=None):
        if os.geteuid() != 0:
            raise PermissionError('offline session storage requires euid 0')
        self.base = Path(base)
        if not self.base.is_absolute() or self.base.is_symlink():
            raise ValueError('offline session base must be an absolute non-symlink path')
        self.trusted_key = Path(trusted_key)
        self._inspect = inspect
        self._info_reader = info_reader
        self.updater_version = _UPDATER_VERSION

    def _physical_info(self):
        physical = self._inspect()
        if not isinstance(physical, dict) or physical.get('model') not in _MODELS:
            raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
        return physical

    def _require_identity(self, physical):
        if not isinstance(physical, dict) or physical.get('model') not in _MODELS:
            raise OfflineSessionError('physical offline upgrades support only TB322FC/TB323FU')
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
        """Identity for read-only info; mutation paths keep the full precheck."""
        if self._info_reader is not None:
            return self._require_identity(self._info_reader())
        if self._inspect is _DEFAULT_INSPECT:
            return self._require_identity(read_device_identity())
        # A caller that supplied its own inspector is describing the device for
        # this store, so info keeps reading that fixture instead of the host.
        return self._require_identity(self._inspect())

    def _channel_info(self):
        return system_transaction._snapshot_channel(armada_os.snapshot())

    def _new_session_id(self):
        return uuid.uuid4().hex

    def info(self):
        physical = self._info_physical()
        return {
            'schemaVersion': 1,
            'updaterVersion': self.updater_version,
            'bootPolicy': trial_record.BOOT_POLICY,
            'model': physical['model'],
            'actualSlot': physical['actualSlot'],
            'inactiveSlot': physical['inactiveSlot'],
            'deployment': physical['deployment'],
            'recoveryPartUuid': physical['recoveryPartUuid'],
            'channel': self._channel_info(),
        }

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
