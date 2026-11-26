# SPDX-License-Identifier: GPL-3.0-only
"""Durable dual-copy journal I/O for the Linux update trial record.
Callers must supply a fresh, unlocked fd for an already-verified internal partition.
Functions in this module own flock serialization on the supplied file descriptor.
``migrate_legacy_descriptor`` rewrites only the descriptor sector the caller
already parsed; this module does not perform device selection, partition
migration, payload movement or slot activation.

``save`` is copy-on-write: the next sequence is written into the record copy
that was *not* selected, then fsynced and read back in full.  The previously
selected copy is never touched, so the old record stays selectable until the
new record is durable.  This pins the journal commit boundary only.  It is not
a claim that a write to any individual boot partition is atomic across a power
interruption; callers must rely on the journal plus one complete image per
state, never on intra-partition write atomicity.

The very first record is different: with two zeroed copies there is nothing
older to fall back to, so a torn first write would leave the journal
unreadable forever.  Before writing that record into copy A, ``save`` durably
writes the logical-empty sentinel (one fixed bit in copy B), flushes it and
reads the copy back.  A later ``load`` accepts a torn A plus that exact marker
in B as an empty journal, so the initial write can simply be retried.  An
attempt that finds the exact marker already present does not rewrite it, but
still flushes and re-reads it before copy A is touched: a marker returned from
the page cache proves nothing after an earlier flush failed.  The
sentinel is written only when no record exists, is replaced by the second
record write, and is never consulted while a real record still selects.  An
older reader that predates the marker rejects it as a format error (fail
closed); it never accepts the marker as a trial record.  The anchor therefore
only exists in journals whose first record was written by the current writer:
a journal already initialised by an older writer keeps the old unanchored
behaviour, and an older reader installed over a marker pair refuses to boot
rather than guessing.  Interrupted marker initialisation is also safe: the
write can only land or leave copy B at zero, and a zero copy is either plain
empty (nothing written yet) or still covered by this retry rule once A tears.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
import struct
import uuid
import zlib

from .recovery_layout import (
    REGION_BYTES,
    REGION_OFFSET,
    RecoveryLayout,
    SLOT_BYTES,
    parse_layout,
)
from .trial_record import (
    DESCRIPTOR_MIN_VERSION,
    EMPTY_MARKER,
    EmptyTrialJournal,
    RECORD_BYTES,
    TAIL_OFFSET_A,
    TAIL_OFFSET_B,
    TrialRecord,
    encode_record,
    select_record,
)

_MAX_U64 = (1 << 64) - 1
_ZERO_RECORD = b"\x00" * RECORD_BYTES
LEGACY_DESCRIPTOR_VERSIONS = (1, 2)
_V3_RECORD_SLOT_A = 7 * SLOT_BYTES
_V3_RECORD_SLOT_B = 8 * SLOT_BYTES


class TrialStoreError(RuntimeError):
    """Raised when trial journal storage state or I/O violates the contract."""


@dataclass(frozen=True)
class TrialSnapshot:
    header: bytes
    region: bytes
    layout: RecoveryLayout
    record: TrialRecord | None
    selected: int | None

    @property
    def migration_required(self) -> bool:
        return self.layout.descriptor_version != DESCRIPTOR_MIN_VERSION


def _read_snapshot(fd: int, partition_uuid: str, board: int, *,
                   allow_legacy: bool = False) -> TrialSnapshot:
    header = os.pread(fd, REGION_BYTES, 0)
    region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
    layout = parse_layout(header, region, partition_uuid, board)
    if layout.descriptor_version != DESCRIPTOR_MIN_VERSION:
        # v1/v2 describe a settings region whose reserved tail is required to
        # be zero, so the v3 record slots cannot hold a journal yet.  Only a
        # caller that has promised to migrate before any journal write may
        # read them as an empty journal; every journal operation stays strict.
        if allow_legacy and layout.descriptor_version in LEGACY_DESCRIPTOR_VERSIONS:
            return TrialSnapshot(
                header=header,
                region=region,
                layout=layout,
                record=None,
                selected=None,
            )
        raise TrialStoreError(
            f"unsupported recovery descriptor version: {layout.descriptor_version}"
        )
    slot_a = region[TAIL_OFFSET_A : TAIL_OFFSET_A + RECORD_BYTES]
    slot_b = region[TAIL_OFFSET_B : TAIL_OFFSET_B + RECORD_BYTES]
    try:
        record, selected = select_record(
            slot_a,
            slot_b,
            layout.partition_uuid,
            layout.installation_uuid,
        )
    except EmptyTrialJournal:
        record = None
        selected = None
    return TrialSnapshot(
        header=header,
        region=region,
        layout=layout,
        record=record,
        selected=selected,
    )


def _validate_transition(snapshot: TrialSnapshot, next_record: TrialRecord) -> int:
    if type(snapshot) is not TrialSnapshot or type(snapshot.layout) is not RecoveryLayout:
        raise TrialStoreError("invalid TrialSnapshot")
    if snapshot.layout.descriptor_version != DESCRIPTOR_MIN_VERSION:
        raise TrialStoreError(
            f"unsupported recovery descriptor version: {snapshot.layout.descriptor_version}"
        )
    if (
        next_record.partition_uuid != snapshot.layout.partition_uuid
        or next_record.installation_uuid != snapshot.layout.installation_uuid
    ):
        raise TrialStoreError("next_record identity does not match layout")
    if snapshot.record is None or snapshot.selected is None:
        if snapshot.record is not None or snapshot.selected is not None:
            raise TrialStoreError("inconsistent empty snapshot state")
        if next_record.sequence != 1:
            raise TrialStoreError("initial trial record must have sequence 1")
        return 0
    if snapshot.selected not in (0, 1):
        raise TrialStoreError("invalid selected slot index")
    if snapshot.record.sequence >= _MAX_U64:
        raise TrialStoreError("trial record sequence overflow")
    if next_record.sequence != snapshot.record.sequence + 1:
        raise TrialStoreError("next_record sequence must increment by 1")
    return snapshot.selected ^ 1


def load(fd: int, partition_uuid: str, board: int, *,
         allow_legacy: bool = False) -> TrialSnapshot:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        return _read_snapshot(fd, partition_uuid, board, allow_legacy=allow_legacy)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def migrate_legacy_descriptor(fd: int, partition_uuid: str, board: int) -> dict | None:
    """Upgrade a v1/v2 settings descriptor to the v3 trial-capable layout.

    The migration rewrites only the first 4096-byte descriptor slot.  The
    payload, archive header, settings slots and every byte after the descriptor
    are preserved.  ``parse_layout`` has already proved that the legacy
    reserved tail is zero, which covers both v3 trial record slots, so the new
    descriptor starts with an empty journal.

    The write is a single sector-sized pwrite followed by fsync and a full
    readback.  A short write, a flush failure or any readback difference is
    reported as a migration failure; no caller may continue to journal writes.
    Re-running on a v3 descriptor is a no-op.
    """
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        header = os.pread(fd, REGION_BYTES, 0)
        region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
        layout = parse_layout(header, region, partition_uuid, board)
        if layout.descriptor_version == DESCRIPTOR_MIN_VERSION:
            return None
        if layout.descriptor_version not in LEGACY_DESCRIPTOR_VERSIONS:
            raise TrialStoreError(
                f"unsupported recovery descriptor version: {layout.descriptor_version}"
            )

        old_descriptor = region[:SLOT_BYTES]
        if any(old_descriptor[92:4092]):
            raise TrialStoreError("legacy descriptor has nonzero v3 reserved bytes")

        descriptor = bytearray(old_descriptor)
        struct.pack_into("<I", descriptor, 8, DESCRIPTOR_MIN_VERSION)
        struct.pack_into(
            "<III", descriptor, 92,
            _V3_RECORD_SLOT_A, _V3_RECORD_SLOT_B, SLOT_BYTES,
        )
        descriptor[104:4092] = b"\x00" * (4092 - 104)
        struct.pack_into(
            "<I", descriptor, 4092,
            zlib.crc32(bytes(descriptor[:4092])) & 0xFFFFFFFF,
        )
        expected_region = bytes(descriptor) + region[SLOT_BYTES:]
        if (descriptor[:8] != old_descriptor[:8] or
                descriptor[16:92] != old_descriptor[16:92]):
            raise TrialStoreError("recovery descriptor identity changed during migration")

        written = os.pwrite(fd, bytes(descriptor), REGION_OFFSET)
        if written != SLOT_BYTES:
            raise TrialStoreError(
                f"recovery descriptor migration short write: {written}"
            )
        try:
            os.fsync(fd)
        except OSError as exc:
            raise TrialStoreError(
                "recovery descriptor migration fsync failed"
            ) from exc

        readback_header = os.pread(fd, REGION_BYTES, 0)
        readback_region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
        if readback_header != header:
            raise TrialStoreError("recovery header changed during descriptor migration")
        if readback_region != expected_region:
            raise TrialStoreError(
                "recovery descriptor migration readback verification failed"
            )
        migrated = parse_layout(readback_header, readback_region, partition_uuid, board)
        if (migrated.descriptor_version != DESCRIPTOR_MIN_VERSION or
                migrated.partition_uuid != layout.partition_uuid or
                migrated.installation_uuid != layout.installation_uuid or
                migrated.board != layout.board):
            raise TrialStoreError(
                "recovery descriptor migration produced an invalid v3 descriptor"
            )
        return {
            "fromVersion": layout.descriptor_version,
            "toVersion": DESCRIPTOR_MIN_VERSION,
            "descriptorSha256": hashlib.sha256(bytes(descriptor)).hexdigest(),
            "regionSha256Before": hashlib.sha256(region).hexdigest(),
            "regionSha256After": hashlib.sha256(readback_region).hexdigest(),
            "recordSlotOffsets": [_V3_RECORD_SLOT_A, _V3_RECORD_SLOT_B],
        }
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _ensure_empty_marker(fd: int) -> None:
    """Durably place the logical-empty sentinel in the untouched copy B.

    Called only while the journal holds no record, immediately before the
    first record is written to copy A.  The marker is idempotent: an exact
    marker is accepted without a rewrite, a zeroed copy is written, and any
    other content is refused rather than overwritten.  Both paths then flush
    and read the marker back.  An exact marker that is merely visible in the
    page cache does not prove that an earlier flush succeeded, so a retry
    re-flushes and re-verifies it instead of trusting it as durable.
    """
    offset = REGION_OFFSET + TAIL_OFFSET_B
    current = os.pread(fd, RECORD_BYTES, offset)
    if current != EMPTY_MARKER:
        if current != _ZERO_RECORD:
            raise TrialStoreError(
                "empty journal marker copy is neither blank nor the exact marker"
            )
        written = os.pwrite(fd, EMPTY_MARKER, offset)
        if written != RECORD_BYTES:
            raise TrialStoreError(f"short empty-journal marker write: {written}")
    try:
        os.fsync(fd)
    except OSError as exc:
        raise TrialStoreError(
            "empty-journal marker fsync failed; retry is still safe"
        ) from exc
    if os.pread(fd, RECORD_BYTES, offset) != EMPTY_MARKER:
        raise TrialStoreError("empty-journal marker readback verification failed")


def save(fd: int, expected: TrialSnapshot, next_record: TrialRecord) -> int:
    wire = encode_record(next_record)
    _validate_transition(expected, next_record)
    try:
        partition_uuid = str(uuid.UUID(bytes=expected.layout.partition_uuid))
    except (TypeError, ValueError) as exc:
        raise TrialStoreError("invalid expected layout partition_uuid") from exc
    board = expected.layout.board

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fresh = _read_snapshot(fd, partition_uuid, board)
        if fresh != expected:
            raise TrialStoreError("concurrent modification detected")
        target = _validate_transition(fresh, next_record)
        if fresh.record is None:
            # Anchor the initial write before touching copy A.  The marker is
            # flushed and read back even when an earlier attempt already left
            # the exact bytes behind.  If this or the record write tears,
            # load() still reports an empty journal from the exact marker and
            # the caller can retry sequence 1.
            _ensure_empty_marker(fd)
        offset = REGION_OFFSET + (TAIL_OFFSET_A if target == 0 else TAIL_OFFSET_B)
        other = target ^ 1
        other_offset = REGION_OFFSET + (TAIL_OFFSET_A if other == 0 else TAIL_OFFSET_B)
        previous_copy = os.pread(fd, RECORD_BYTES, other_offset)
        written = os.pwrite(fd, wire, offset)
        if written != RECORD_BYTES:
            raise TrialStoreError(f"short trial record write: {written}")
        try:
            os.fsync(fd)
        except OSError as exc:
            raise TrialStoreError(
                "trial journal fsync failed; durable commit state is ambiguous"
            ) from exc
        readback = os.pread(fd, RECORD_BYTES, offset)
        if readback != wire:
            raise TrialStoreError("trial record readback verification failed")
        if os.pread(fd, RECORD_BYTES, other_offset) != previous_copy:
            raise TrialStoreError("copy-on-write journal copy changed")
        verified = _read_snapshot(fd, partition_uuid, board)
        if verified.selected != target or verified.record != next_record:
            raise TrialStoreError("trial journal durable readback mismatch")
        return target
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
