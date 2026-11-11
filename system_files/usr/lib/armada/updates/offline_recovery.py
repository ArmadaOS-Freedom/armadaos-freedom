# SPDX-License-Identifier: GPL-3.0-only
"""Offline-only recovery descriptor compatibility for the trial journal.

Online callers must keep :mod:`trial_store`'s requirement that the settings
descriptor is already version 3.  A controller-only 0.7 system can still carry
a validated version 1 or 2 descriptor, and the offline upgrade must be able to
read that journal and migrate the descriptor once, immediately before the
trial record is written.

This module never changes :mod:`trial_store`'s global rules.  It only:

* reads a validated v1/v2 descriptor for the offline prepare/preflight check
  and returns an explicitly empty snapshot (an existing record is refused);
* converts an expected legacy descriptor to v3 inside the offline execute
  path, under an exclusive lock and after an exact compare-and-swap of the
  header and the whole 64 KiB region.

The migration rewrites only the first 4096 bytes of the config region.  The
recovery header, compressed backup, config slots, old diagnostic slots and the
unused region are never touched, and no slot is activated here.
"""

from __future__ import annotations

import fcntl
import os
import struct
import uuid
import zlib

from . import trial_store
from .recovery_layout import (
    REGION_BYTES,
    REGION_OFFSET,
    SLOT_BYTES,
    RecoveryLayout,
    RecoveryLayoutError,
    parse_layout,
)
from .trial_record import (
    DESCRIPTOR_MIN_VERSION,
    RECORD_BYTES,
    TAIL_OFFSET_A,
    TAIL_OFFSET_B,
)

LEGACY_DESCRIPTOR_VERSIONS = (1, 2)
# Offset 8 carries the descriptor version; the journal geometry lives at
# 92/96/100 only when the descriptor is version 3 or newer.
_VERSION_OFFSET = 8
_GEOMETRY_OFFSET = 92
_GEOMETRY = (7 * SLOT_BYTES, 8 * SLOT_BYTES, SLOT_BYTES)
_CRC_OFFSET = SLOT_BYTES - 4
_JOURNAL_SLOTS = slice(TAIL_OFFSET_A, TAIL_OFFSET_B + RECORD_BYTES)


class OfflineRecoveryError(RuntimeError):
    """A legacy recovery descriptor cannot be read or migrated safely."""


def _partition_text(raw):
    try:
        return str(uuid.UUID(bytes=raw))
    except (TypeError, ValueError) as error:
        raise OfflineRecoveryError('invalid recovery partition uuid') from error


def _read_region(fd):
    header = os.pread(fd, REGION_BYTES, 0)
    region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
    if len(header) != REGION_BYTES or len(region) != REGION_BYTES:
        raise OfflineRecoveryError('short recovery read')
    return header, region


def _parse(header, region, partition_uuid, board):
    try:
        return parse_layout(header, region, partition_uuid, board)
    except RecoveryLayoutError as error:
        raise OfflineRecoveryError(str(error)) from error


def _legacy_snapshot(header, region, layout):
    # parse_layout already requires every byte from the legacy tail onwards to
    # be zero, which covers both journal slots.  State that explicitly so an
    # older record can never be mistaken for an empty legacy journal.
    if any(region[_JOURNAL_SLOTS]):
        raise OfflineRecoveryError('legacy recovery journal is not empty')
    return trial_store.TrialSnapshot(
        header=header,
        region=region,
        layout=layout,
        record=None,
        selected=None,
    )


def load(fd, partition_uuid, board):
    """Read a recovery journal for an offline caller.

    A version 3 descriptor delegates to :func:`trial_store.load` and keeps its
    strict record rules.  A validated version 1 or 2 descriptor is accepted
    only as an empty legacy journal.
    """
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        header, region = _read_region(fd)
        layout = _parse(header, region, partition_uuid, board)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
    if layout.descriptor_version == DESCRIPTOR_MIN_VERSION:
        # Never call trial_store while holding this module's lock: its
        # load/save pair owns and releases the same flock.
        return trial_store.load(fd, partition_uuid, board)
    if layout.descriptor_version not in LEGACY_DESCRIPTOR_VERSIONS:
        raise OfflineRecoveryError(
            'unsupported recovery descriptor version: %s'
            % layout.descriptor_version)
    return _legacy_snapshot(header, region, layout)


def _migrated_descriptor(region):
    descriptor = bytearray(region[:SLOT_BYTES])
    struct.pack_into('<I', descriptor, _VERSION_OFFSET, DESCRIPTOR_MIN_VERSION)
    struct.pack_into('<III', descriptor, _GEOMETRY_OFFSET, *_GEOMETRY)
    struct.pack_into(
        '<I', descriptor, _CRC_OFFSET,
        zlib.crc32(bytes(descriptor[:_CRC_OFFSET])) & 0xFFFFFFFF)
    return bytes(descriptor)


def migrate_descriptor(fd, expected):
    """Return a snapshot whose descriptor is v3, migrating only a legacy one.

    ``expected`` is the snapshot captured by prepare.  For a version 3
    descriptor the device is re-read (CAS) and never written.  For a validated
    version 1/2 descriptor the whole header and 64 KiB region must match
    exactly; then exactly one 4096-byte descriptor write is flushed, read back
    and re-parsed before the new snapshot is returned.  The caller writes the
    trial record afterwards, outside this lock.
    """
    if (type(expected) is not trial_store.TrialSnapshot or
            type(expected.layout) is not RecoveryLayout):
        raise OfflineRecoveryError('invalid recovery snapshot')
    layout = expected.layout
    partition_uuid = _partition_text(layout.partition_uuid)
    if layout.descriptor_version == DESCRIPTOR_MIN_VERSION:
        current = trial_store.load(fd, partition_uuid, layout.board)
        if current != expected:
            raise OfflineRecoveryError('recovery journal compare-and-swap failed')
        return current
    if layout.descriptor_version not in LEGACY_DESCRIPTOR_VERSIONS:
        raise OfflineRecoveryError(
            'unsupported recovery descriptor version: %s'
            % layout.descriptor_version)
    if expected.record is not None or expected.selected is not None:
        raise OfflineRecoveryError('legacy recovery journal is not empty')

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        header, region = _read_region(fd)
        if header != expected.header or region != expected.region:
            raise OfflineRecoveryError('recovery journal compare-and-swap failed')
        current = _parse(header, region, partition_uuid, layout.board)
        if (current.descriptor_version != layout.descriptor_version or
                current.partition_uuid != layout.partition_uuid or
                current.installation_uuid != layout.installation_uuid or
                current.board != layout.board):
            raise OfflineRecoveryError('recovery descriptor identity mismatch')
        if any(region[_JOURNAL_SLOTS]):
            raise OfflineRecoveryError('legacy recovery journal is not empty')

        descriptor = _migrated_descriptor(region)
        written = os.pwrite(fd, descriptor, REGION_OFFSET)
        if written != SLOT_BYTES:
            raise OfflineRecoveryError(
                'short recovery descriptor write: %s' % written)
        os.fsync(fd)

        new_header = os.pread(fd, REGION_BYTES, 0)
        new_region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
        if len(new_header) != REGION_BYTES or len(new_region) != REGION_BYTES:
            raise OfflineRecoveryError('short recovery readback')
        if new_header != header:
            raise OfflineRecoveryError(
                'recovery header changed during descriptor migration')
        if new_region != descriptor + region[SLOT_BYTES:]:
            raise OfflineRecoveryError('recovery descriptor readback mismatch')

        migrated = _parse(new_header, new_region, partition_uuid, layout.board)
        if (migrated.descriptor_version != DESCRIPTOR_MIN_VERSION or
                migrated.partition_uuid != current.partition_uuid or
                migrated.installation_uuid != current.installation_uuid or
                migrated.board != current.board):
            raise OfflineRecoveryError('migrated recovery descriptor is invalid')
        if any(new_region[_JOURNAL_SLOTS]):
            raise OfflineRecoveryError('migrated recovery journal is not empty')
        return trial_store.TrialSnapshot(
            header=new_header,
            region=new_region,
            layout=migrated,
            record=None,
            selected=None,
        )
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
