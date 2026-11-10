"""Shared validation for Armada recovery headers and settings descriptors."""

from dataclasses import dataclass
import json
import struct
import uuid
import zlib

SIZE = 2 * 1024**3
REGION_BYTES = 65536
REGION_OFFSET = SIZE - REGION_BYTES
SLOT_BYTES = 4096


class RecoveryLayoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryLayout:
    descriptor_version: int
    partition_uuid: bytes
    installation_uuid: bytes
    board: int


def require(condition, message):
    if not condition:
        raise RecoveryLayoutError(message)


def u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


def parse_layout(header: bytes, region: bytes, partition_uuid: str, board: int) -> RecoveryLayout:
    try:
        require(len(header) == REGION_BYTES and len(region) == REGION_BYTES, 'Short settings read')
        meta = json.loads(header.decode('utf-8'))
        part = uuid.UUID(partition_uuid).bytes
        installation = uuid.UUID(meta['installationId']).bytes
        require(meta['kind'] == 'armada-board-recovery-image' and meta['schemaVersion'] == 3,
                'Backup has no provisioned launcher settings region')
        require(meta['imageBytes'] == SIZE and meta['headerBytes'] == REGION_BYTES and
                meta['payloadOffset'] == REGION_BYTES and meta['configRegionOffset'] == REGION_OFFSET and
                meta['configRegionBytes'] == REGION_BYTES and meta['configBoardId'] == board and
                uuid.UUID(meta['configPartitionUuid']).bytes == part,
                'Backup metadata does not match the internal partition')
        require(0 < meta['compressedBytes'] <= REGION_OFFSET - REGION_BYTES, 'Invalid backup payload bounds')
        d = region[:SLOT_BYTES]
        dver = u32(d, 8)
        require(d[:8] == b'KSCFGV3\0' and dver in (1, 2, 3) and u32(d, 12) == SLOT_BYTES and
                zlib.crc32(d[:4092]) == u32(d, 4092), 'Invalid settings descriptor')
        tail = (3 * SLOT_BYTES, 7 * SLOT_BYTES, 9 * SLOT_BYTES)[dver - 1]
        v3_ok = (not any(d[92:4092])) if dver < 3 else (
            u32(d, 92) == 7 * SLOT_BYTES and u32(d, 96) == 8 * SLOT_BYTES and
            u32(d, 100) == SLOT_BYTES and not any(d[104:4092]))
        require(u64(d, 16) == SIZE and u64(d, 24) == REGION_BYTES + meta['compressedBytes'] and
                u64(d, 32) == REGION_OFFSET and u32(d, 40) == REGION_BYTES and u32(d, 44) == SLOT_BYTES and
                u32(d, 80) == SLOT_BYTES and u32(d, 84) == 2 * SLOT_BYTES and u32(d, 88) == board and
                d[48:64] == part and d[64:80] == installation and any(part) and any(installation) and
                v3_ok and not any(region[tail:]), 'Settings descriptor identity or layout mismatch')
        return RecoveryLayout(
            descriptor_version=dver,
            partition_uuid=part,
            installation_uuid=installation,
            board=board,
        )
    except RecoveryLayoutError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, struct.error) as error:
        raise RecoveryLayoutError(str(error)) from error
