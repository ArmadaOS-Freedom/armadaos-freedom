# SPDX-License-Identifier: GPL-3.0-only
"""Durable dual-copy journal I/O for the Linux update trial record.
Callers must supply a fresh, unlocked fd for an already-verified internal partition.
Functions in this module own flock serialization on the supplied file descriptor.
Does not perform device selection, partition migration, or slot activation."""

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
    EmptyTrialJournal,
    RECORD_BYTES,
    TAIL_OFFSET_A,
    TAIL_OFFSET_B,
    TrialRecord,
    encode_record,
    select_record,
)

_MAX_U64 = (1 << 64) - 1


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
        offset = REGION_OFFSET + (TAIL_OFFSET_A if target == 0 else TAIL_OFFSET_B)
        written = os.pwrite(fd, wire, offset)
        if written != RECORD_BYTES:
            raise TrialStoreError(f"short trial record write: {written}")
        os.fsync(fd)
        readback = os.pread(fd, RECORD_BYTES, offset)
        if readback != wire:
            raise TrialStoreError("trial record readback verification failed")
        return target
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
