# SPDX-License-Identifier: GPL-3.0-only
"""Pure wire codec for the Linux update trial journal record.

Matches the C kshim_update_trial wire contract (4096-byte little-endian
record). CRC32 provides an integrity check only, not authentication. Callers
must verify storage descriptor version/capabilities and manage I/O durability;
this module never reads, writes, or activates storage.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import struct
import zlib

MAGIC = b"ARMUPD1\x00"
VERSION = 1
OFFLINE_VERSION = 2
RECORD_BYTES = 4096
DESCRIPTOR_MIN_VERSION = 3
TAIL_OFFSET_A = 28672
TAIL_OFFSET_B = 32768
ATTEMPT_MAX = 7
OS_NAME_MAX = 64

ONLINE_SINGLE = 0
OFFLINE_DUAL = 1

# The one logical Linux deployment is mirrored into both physical boot slots.
# Slot selection, retry counters, priority and boot-control writes stay owned
# by Android; Linux only observes which slot the bootloader picked.
BOOT_POLICY = "dual-boot-no-slot-control"

PREPARED = 1
TRIAL = 2
CONFIRMED = 3
ROLLED_BACK = 4

_ZERO_HASH = b"\x00" * 32
_ZERO_RECORD = b"\x00" * RECORD_BYTES
_ZERO_RESERVED_1 = b"\x00" * 3
_ZERO_RESERVED_2 = b"\x00" * 3764
_ZERO_RESERVED_2_V2 = b"\x00" * 3728
_OS_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)
_WIRE_STRUCT = struct.Struct("<8sIIQ16s16s16s7I65s3s32s32s32s32s32sI32s3728sI")


class TrialRecordError(ValueError):
    """Raised when a trial journal record or argument violates the contract."""


class EmptyTrialJournal(TrialRecordError):
    """Raised when a trial journal slot or pair is completely zeroed."""


@dataclass(frozen=True)
class TrialRecord:
    sequence: int
    partition_uuid: bytes
    installation_uuid: bytes
    transaction_uuid: bytes
    state: int
    from_slot: int
    target_slot: int
    remaining: int
    attempt_limit: int
    old_serial: int
    new_serial: int
    os_name: str
    old_commit: bytes
    new_commit: bytes
    old_boot_sha256: bytes
    new_boot_sha256: bytes
    plan_sha256: bytes
    policy: int = ONLINE_SINGLE
    other_boot_sha256: bytes = _ZERO_HASH


def _check_uint(value: object, name: str, lo: int, hi: int) -> int:
    if type(value) is not int or value < lo or value > hi:
        raise TrialRecordError(f"invalid {name}")
    return value


def _check_bytes(value: object, name: str, length: int, *, nonzero: bool) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise TrialRecordError(f"invalid {name}")
    if nonzero and value == (b"\x00" * length):
        raise TrialRecordError(f"{name} must be nonzero")
    return value


def _validate_record(record: TrialRecord) -> bytes:
    if type(record) is not TrialRecord:
        raise TrialRecordError("expected TrialRecord")
    _check_uint(record.sequence, "sequence", 1, (1 << 64) - 1)
    _check_bytes(record.partition_uuid, "partition_uuid", 16, nonzero=True)
    _check_bytes(record.installation_uuid, "installation_uuid", 16, nonzero=True)
    _check_bytes(record.transaction_uuid, "transaction_uuid", 16, nonzero=True)
    state = _check_uint(record.state, "state", PREPARED, ROLLED_BACK)
    from_slot = _check_uint(record.from_slot, "from_slot", 0, 1)
    target_slot = _check_uint(record.target_slot, "target_slot", 0, 1)
    if target_slot == from_slot:
        raise TrialRecordError("target_slot must differ from from_slot")
    limit = _check_uint(record.attempt_limit, "attempt_limit", 1, ATTEMPT_MAX)
    remaining = _check_uint(record.remaining, "remaining", 0, limit)
    if state == PREPARED and remaining != limit:
        raise TrialRecordError("PREPARED requires remaining == attempt_limit")
    if state in (TRIAL, CONFIRMED) and remaining == limit:
        raise TrialRecordError("TRIAL and CONFIRMED require remaining < attempt_limit")
    _check_uint(record.old_serial, "old_serial", 0, (1 << 32) - 1)
    _check_uint(record.new_serial, "new_serial", 0, (1 << 32) - 1)
    if type(record.os_name) is not str or _OS_NAME_RE.fullmatch(record.os_name) is None:
        raise TrialRecordError("invalid os_name")
    os_raw = record.os_name.encode("ascii")
    os_padded = os_raw + (b"\x00" * (OS_NAME_MAX + 1 - len(os_raw)))
    _check_bytes(record.old_commit, "old_commit", 32, nonzero=True)
    _check_bytes(record.new_commit, "new_commit", 32, nonzero=True)
    _check_bytes(record.old_boot_sha256, "old_boot_sha256", 32, nonzero=True)
    _check_bytes(record.new_boot_sha256, "new_boot_sha256", 32, nonzero=True)
    _check_bytes(record.plan_sha256, "plan_sha256", 32, nonzero=True)
    policy = _check_uint(record.policy, "policy", ONLINE_SINGLE, OFFLINE_DUAL)
    _check_bytes(
        record.other_boot_sha256, "other_boot_sha256", 32,
        nonzero=policy == OFFLINE_DUAL,
    )
    if policy == ONLINE_SINGLE and record.other_boot_sha256 != _ZERO_HASH:
        raise TrialRecordError("online other_boot_sha256 must be zero")
    if policy == OFFLINE_DUAL and state == ROLLED_BACK:
        raise TrialRecordError("OFFLINE_DUAL does not permit ROLLED_BACK")
    return os_padded


def expected_boot_sha256(record: TrialRecord, actual_slot: int) -> bytes:
    encode_record(record)
    _check_uint(actual_slot, "actual_slot", 0, 1)
    if record.state not in (TRIAL, CONFIRMED):
        raise TrialRecordError("expected boot hash requires TRIAL or CONFIRMED")
    if actual_slot == record.target_slot:
        return record.new_boot_sha256
    if actual_slot == record.from_slot and record.policy == OFFLINE_DUAL:
        # Mirrored writes put the same logical deployment in both physical
        # slots, each rebuilt from that slot's own material.  from_slot and
        # target_slot only index the hashes and the write order, so a boot
        # observed on either slot is valid evidence for this deployment.
        return record.other_boot_sha256
    raise TrialRecordError("actual slot is not valid for the trial record")


def encode_record(record: TrialRecord) -> bytes:
    os_padded = _validate_record(record)
    version = OFFLINE_VERSION if record.policy == OFFLINE_DUAL else VERSION
    body = _WIRE_STRUCT.pack(
        MAGIC,
        version,
        RECORD_BYTES,
        record.sequence,
        record.partition_uuid,
        record.installation_uuid,
        record.transaction_uuid,
        record.state,
        record.from_slot,
        record.target_slot,
        record.remaining,
        record.attempt_limit,
        record.old_serial,
        record.new_serial,
        os_padded,
        _ZERO_RESERVED_1,
        record.old_commit,
        record.new_commit,
        record.old_boot_sha256,
        record.new_boot_sha256,
        record.plan_sha256,
        record.policy,
        record.other_boot_sha256,
        _ZERO_RESERVED_2_V2,
        0,
    )[: RECORD_BYTES - 4]
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + struct.pack("<I", crc)


def decode_record(
    data: bytes,
    partition_uuid: bytes,
    installation_uuid: bytes,
) -> TrialRecord:
    _check_bytes(data, "data", RECORD_BYTES, nonzero=False)
    _check_bytes(partition_uuid, "partition_uuid", 16, nonzero=False)
    _check_bytes(installation_uuid, "installation_uuid", 16, nonzero=False)
    if data == _ZERO_RECORD:
        raise EmptyTrialJournal("trial journal record is empty")
    (
        magic,
        version,
        record_bytes,
        sequence,
        rec_part_uuid,
        rec_inst_uuid,
        transaction_uuid,
        state,
        from_slot,
        target_slot,
        remaining,
        attempt_limit,
        old_serial,
        new_serial,
        os_padded,
        reserved_1,
        old_commit,
        new_commit,
        old_boot_sha256,
        new_boot_sha256,
        plan_sha256,
        policy,
        other_boot_sha256,
        reserved_2,
        stored_crc,
    ) = _WIRE_STRUCT.unpack(data)
    calc_crc = zlib.crc32(data[: RECORD_BYTES - 4]) & 0xFFFFFFFF
    if stored_crc != calc_crc:
        raise TrialRecordError("trial record CRC mismatch")
    if magic != MAGIC or version not in (VERSION, OFFLINE_VERSION) or record_bytes != RECORD_BYTES:
        raise TrialRecordError("invalid trial record header")
    if ((version == VERSION and policy != ONLINE_SINGLE)
            or (version == OFFLINE_VERSION and policy != OFFLINE_DUAL)):
        raise TrialRecordError("trial record version/policy mismatch")
    if reserved_1 != _ZERO_RESERVED_1 or reserved_2 != _ZERO_RESERVED_2_V2:
        raise TrialRecordError("nonzero reserved bytes")
    nul = os_padded.find(0)
    if nul <= 0 or any(os_padded[nul:]):
        raise TrialRecordError("malformed os_name field")
    try:
        os_name = os_padded[:nul].decode("ascii")
    except UnicodeDecodeError as exc:
        raise TrialRecordError("non-ASCII os_name") from exc
    record = TrialRecord(
        sequence=sequence,
        partition_uuid=rec_part_uuid,
        installation_uuid=rec_inst_uuid,
        transaction_uuid=transaction_uuid,
        state=state,
        from_slot=from_slot,
        target_slot=target_slot,
        remaining=remaining,
        attempt_limit=attempt_limit,
        old_serial=old_serial,
        new_serial=new_serial,
        os_name=os_name,
        old_commit=old_commit,
        new_commit=new_commit,
        old_boot_sha256=old_boot_sha256,
        new_boot_sha256=new_boot_sha256,
        plan_sha256=plan_sha256,
        policy=policy,
        other_boot_sha256=other_boot_sha256,
    )
    _validate_record(record)
    if rec_part_uuid != partition_uuid or rec_inst_uuid != installation_uuid:
        raise TrialRecordError("trial record identity mismatch")
    return record


def select_record(
    a: bytes,
    b: bytes,
    partition_uuid: bytes,
    installation_uuid: bytes,
) -> tuple[TrialRecord, int]:
    _check_bytes(a, "a", RECORD_BYTES, nonzero=False)
    _check_bytes(b, "b", RECORD_BYTES, nonzero=False)
    _check_bytes(partition_uuid, "partition_uuid", 16, nonzero=False)
    _check_bytes(installation_uuid, "installation_uuid", 16, nonzero=False)

    rec_a: TrialRecord | None = None
    rec_b: TrialRecord | None = None
    empty_a = False
    empty_b = False

    try:
        rec_a = decode_record(a, partition_uuid, installation_uuid)
    except EmptyTrialJournal:
        empty_a = True
    except TrialRecordError:
        rec_a = None

    try:
        rec_b = decode_record(b, partition_uuid, installation_uuid)
    except EmptyTrialJournal:
        empty_b = True
    except TrialRecordError:
        rec_b = None

    if rec_a is not None and rec_b is not None:
        if rec_a.sequence > rec_b.sequence:
            return rec_a, 0
        if rec_b.sequence > rec_a.sequence:
            return rec_b, 1
        if a == b:
            return rec_a, 0
        raise TrialRecordError("conflicting trial records with equal sequence")
    if rec_a is not None:
        return rec_a, 0
    if rec_b is not None:
        return rec_b, 1
    if empty_a and empty_b:
        raise EmptyTrialJournal("both trial journal slots are empty")
    raise TrialRecordError("no valid trial journal record")
