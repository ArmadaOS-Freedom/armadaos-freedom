# SPDX-License-Identifier: GPL-3.0-only
"""Durable dual-copy journal I/O for the Linux update trial record.
Callers must supply a fresh, unlocked fd for an already-verified internal partition.
Functions in this module own flock serialization on the supplied file descriptor.
Does not perform device selection, partition migration, or slot activation.

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
import os
import uuid

from .recovery_layout import (
    REGION_BYTES,
    REGION_OFFSET,
    RecoveryLayout,
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


class TrialStoreError(RuntimeError):
    """Raised when trial journal storage state or I/O violates the contract."""


@dataclass(frozen=True)
class TrialSnapshot:
    header: bytes
    region: bytes
    layout: RecoveryLayout
    record: TrialRecord | None
    selected: int | None


def _read_snapshot(fd: int, partition_uuid: str, board: int) -> TrialSnapshot:
    header = os.pread(fd, REGION_BYTES, 0)
    region = os.pread(fd, REGION_BYTES, REGION_OFFSET)
    layout = parse_layout(header, region, partition_uuid, board)
    if layout.descriptor_version != DESCRIPTOR_MIN_VERSION:
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


def load(fd: int, partition_uuid: str, board: int) -> TrialSnapshot:
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        return _read_snapshot(fd, partition_uuid, board)
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
