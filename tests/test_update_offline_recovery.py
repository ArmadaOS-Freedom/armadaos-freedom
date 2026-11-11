# SPDX-License-Identifier: GPL-3.0-only
"""Legacy recovery descriptor compatibility for the offline upgrade path."""

from __future__ import annotations

import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import (  # noqa: E402
    offline_prepare, offline_recovery, recovery_layout, trial_record, trial_store,
)

SIZE = 2 * 1024**3
REGION_BYTES = recovery_layout.REGION_BYTES
REGION_OFFSET = recovery_layout.REGION_OFFSET
SLOT_BYTES = recovery_layout.SLOT_BYTES
TAIL = {1: 3 * SLOT_BYTES, 2: 7 * SLOT_BYTES, 3: 9 * SLOT_BYTES}
PART = "11111111-2222-4333-8444-555555555555"
OTHER_PART = "99999999-8888-4777-8666-555544443333"
INST = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BACKUP = b"BACKUP-MARKER-PRESERVED"


def _header(part, inst, board, compressed=1024):
    meta = {
        "kind": "armada-board-recovery-image",
        "schemaVersion": 3,
        "imageBytes": SIZE,
        "headerBytes": REGION_BYTES,
        "payloadOffset": REGION_BYTES,
        "compressedBytes": compressed,
        "installationId": inst,
        "configRegionOffset": REGION_OFFSET,
        "configRegionBytes": REGION_BYTES,
        "configPartitionUuid": part,
        "configBoardId": board,
    }
    return json.dumps(meta, sort_keys=True).encode("ascii").ljust(REGION_BYTES, b" ")


def _descriptor(version, part, inst, board, compressed=1024):
    descriptor = bytearray(SLOT_BYTES)
    descriptor[0:8] = b"KSCFGV3\0"
    struct.pack_into("<IIQQQII", descriptor, 8, version, SLOT_BYTES, SIZE,
                     REGION_BYTES + compressed, REGION_OFFSET, REGION_BYTES,
                     SLOT_BYTES)
    descriptor[48:64] = uuid.UUID(part).bytes
    descriptor[64:80] = uuid.UUID(inst).bytes
    struct.pack_into("<3I", descriptor, 80, SLOT_BYTES, 2 * SLOT_BYTES, board)
    if version == 3:
        struct.pack_into("<III", descriptor, 92, 7 * SLOT_BYTES,
                         8 * SLOT_BYTES, SLOT_BYTES)
    struct.pack_into("<I", descriptor, SLOT_BYTES - 4,
                     zlib.crc32(bytes(descriptor[:SLOT_BYTES - 4])) & 0xFFFFFFFF)
    return bytes(descriptor)


def _region(version, part, inst, board, compressed=1024):
    region = bytearray(REGION_BYTES)
    region[0:SLOT_BYTES] = _descriptor(version, part, inst, board, compressed)
    # Config slots 1/2 and the legacy diagnostic/log slots are allowed to hold
    # data; only the reserved tail must stay zero.
    stop = min(TAIL[version], 7 * SLOT_BYTES)
    pattern = bytes([0x40 + version])
    region[SLOT_BYTES:stop] = pattern * (stop - SLOT_BYTES)
    return bytes(region)


def _record(part, inst):
    return trial_record.TrialRecord(
        sequence=1,
        partition_uuid=uuid.UUID(part).bytes,
        installation_uuid=uuid.UUID(inst).bytes,
        transaction_uuid=uuid.UUID("11112222-3333-4444-8555-666677778888").bytes,
        state=trial_record.PREPARED,
        from_slot=0,
        target_slot=1,
        remaining=3,
        attempt_limit=3,
        old_serial=0,
        new_serial=1,
        os_name="armada",
        old_commit=b"a" * 32,
        new_commit=b"b" * 32,
        old_boot_sha256=b"c" * 32,
        new_boot_sha256=b"d" * 32,
        plan_sha256=b"e" * 32,
    )


class ImageCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "recovery.img"
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, self.fd)
        os.ftruncate(self.fd, SIZE)

    def write_image(self, version, *, part=PART, inst=INST, board=1):
        header = _header(part, inst, board)
        region = _region(version, part, inst, board)
        os.pwrite(self.fd, header, 0)
        os.pwrite(self.fd, BACKUP, REGION_BYTES)
        os.pwrite(self.fd, region, REGION_OFFSET)
        return header, region


class LoadTest(ImageCase):
    def test_valid_legacy_descriptors_load_read_only_and_empty(self):
        for version in (1, 2):
            with self.subTest(version=version):
                header, region = self.write_image(version)
                with mock.patch.object(offline_recovery.os, "pwrite") as writes:
                    snapshot = offline_recovery.load(self.fd, PART, 1)
                writes.assert_not_called()
                self.assertEqual(snapshot.layout.descriptor_version, version)
                self.assertEqual(snapshot.header, header)
                self.assertEqual(snapshot.region, region)
                self.assertIsNone(snapshot.record)
                self.assertIsNone(snapshot.selected)

    def test_v3_delegates_to_the_strict_store(self):
        self.write_image(3)
        with mock.patch.object(
                offline_recovery.trial_store, "load",
                wraps=trial_store.load) as load:
            snapshot = offline_recovery.load(self.fd, PART, 1)
        load.assert_called_once()
        self.assertEqual(snapshot.layout.descriptor_version, 3)
        self.assertIsNone(snapshot.record)

    def test_corrupt_identity_and_nonzero_legacy_tail_are_rejected(self):
        cases = (
            ("crc", lambda: os.pwrite(self.fd, b"\x01", REGION_OFFSET + SLOT_BYTES - 4)),
            ("identity", None),
            ("board", None),
            ("v1-tail", lambda: os.pwrite(self.fd, b"\x01", REGION_OFFSET + TAIL[1])),
            ("v2-tail", lambda: os.pwrite(self.fd, b"\x01", REGION_OFFSET + TAIL[2])),
        )
        for label, corrupt in cases:
            version = 1 if label == "v1-tail" else 2
            with self.subTest(case=label):
                self.write_image(version)
                if corrupt is not None:
                    corrupt()
                with self.assertRaises(offline_recovery.OfflineRecoveryError):
                    if label == "identity":
                        offline_recovery.load(self.fd, OTHER_PART, 1)
                    elif label == "board":
                        offline_recovery.load(self.fd, PART, 2)
                    else:
                        offline_recovery.load(self.fd, PART, 1)


class MigrationTest(ImageCase):
    def test_legacy_conversion_preserves_every_byte_outside_descriptor(self):
        for version in (1, 2):
            with self.subTest(version=version):
                header, region = self.write_image(version)
                expected = offline_recovery.load(self.fd, PART, 1)
                with mock.patch.object(
                        offline_recovery.os, "pwrite",
                        wraps=os.pwrite) as writes:
                    migrated = offline_recovery.migrate_descriptor(
                        self.fd, expected)
                writes.assert_called_once()
                _fd, payload, offset = writes.call_args.args
                self.assertEqual(offset, REGION_OFFSET)
                self.assertEqual(len(payload), SLOT_BYTES)

                descriptor = migrated.region[:SLOT_BYTES]
                self.assertEqual(migrated.layout.descriptor_version, 3)
                self.assertEqual(struct.unpack_from("<I", descriptor, 8)[0], 3)
                self.assertEqual(
                    struct.unpack_from("<III", descriptor, 92),
                    (7 * SLOT_BYTES, 8 * SLOT_BYTES, SLOT_BYTES))
                self.assertEqual(
                    struct.unpack_from("<I", descriptor, SLOT_BYTES - 4)[0],
                    zlib.crc32(descriptor[:SLOT_BYTES - 4]) & 0xFFFFFFFF)

                # Header, compressed backup, config slots, old logs and the
                # unused region are byte-identical; the journal is empty.
                self.assertEqual(os.pread(self.fd, REGION_BYTES, 0), header)
                self.assertEqual(
                    os.pread(self.fd, len(BACKUP), REGION_BYTES), BACKUP)
                self.assertEqual(migrated.region[SLOT_BYTES:], region[SLOT_BYTES:])
                self.assertEqual(migrated.region[TAIL[3]:], b"\x00" * (REGION_BYTES - TAIL[3]))
                # The migrated image passes the strict reader unchanged.
                strict = trial_store.load(self.fd, PART, 1)
                self.assertEqual(strict.layout.descriptor_version, 3)
                self.assertIsNone(strict.record)

    def test_v3_is_never_written(self):
        self.write_image(3)
        expected = offline_recovery.load(self.fd, PART, 1)
        before = os.pread(self.fd, REGION_BYTES, REGION_OFFSET)
        with mock.patch.object(offline_recovery.os, "pwrite") as writes:
            migrated = offline_recovery.migrate_descriptor(self.fd, expected)
        writes.assert_not_called()
        self.assertEqual(migrated, expected)
        self.assertEqual(
            os.pread(self.fd, REGION_BYTES, REGION_OFFSET), before)

    def test_cas_identity_or_nonzero_journal_rejects_without_write(self):
        # A changed region byte outside the descriptor breaks the exact CAS.
        self.write_image(2)
        expected = offline_recovery.load(self.fd, PART, 1)
        os.pwrite(self.fd, b"\x7f", REGION_OFFSET + SLOT_BYTES)
        with mock.patch.object(offline_recovery.os, "pwrite") as writes:
            with self.assertRaises(offline_recovery.OfflineRecoveryError):
                offline_recovery.migrate_descriptor(self.fd, expected)
        writes.assert_not_called()

        # A different identity can never match the expected snapshot.
        header, region = self.write_image(2)
        expected = offline_recovery.load(self.fd, PART, 1)
        foreign = trial_store.TrialSnapshot(
            header=expected.header, region=expected.region,
            layout=recovery_layout.RecoveryLayout(
                2, uuid.UUID(OTHER_PART).bytes,
                expected.layout.installation_uuid, 1),
            record=None, selected=None)
        with mock.patch.object(offline_recovery.os, "pwrite") as writes:
            with self.assertRaises(offline_recovery.OfflineRecoveryError):
                offline_recovery.migrate_descriptor(self.fd, foreign)
        writes.assert_not_called()

        # A non-empty unused journal slot is not an empty legacy journal.
        header, region = self.write_image(2)
        expected = offline_recovery.load(self.fd, PART, 1)
        os.pwrite(self.fd, b"\x01", REGION_OFFSET + TAIL[2])
        with mock.patch.object(offline_recovery.os, "pwrite") as writes:
            with self.assertRaises(offline_recovery.OfflineRecoveryError):
                offline_recovery.migrate_descriptor(self.fd, expected)
        writes.assert_not_called()

    def test_v3_with_a_record_is_returned_unchanged_without_write(self):
        self.write_image(3)
        os.pwrite(self.fd, trial_record.encode_record(_record(PART, INST)),
                  REGION_OFFSET + trial_record.TAIL_OFFSET_A)
        expected = offline_recovery.load(self.fd, PART, 1)
        self.assertIsNotNone(expected.record)
        with mock.patch.object(offline_recovery.os, "pwrite") as writes:
            migrated = offline_recovery.migrate_descriptor(self.fd, expected)
        writes.assert_not_called()
        self.assertEqual(migrated, expected)

    def test_short_write_and_readback_error_reject(self):
        self.write_image(2)
        expected = offline_recovery.load(self.fd, PART, 1)
        with mock.patch.object(
                offline_recovery.os, "pwrite", return_value=SLOT_BYTES // 2) as writes:
            with self.assertRaisesRegex(offline_recovery.OfflineRecoveryError,
                                        "short recovery descriptor write"):
                offline_recovery.migrate_descriptor(self.fd, expected)
        writes.assert_called_once()

        real_pread = os.pread
        calls = {"count": 0}

        def short_pread(fd, size, offset):
            calls["count"] += 1
            if calls["count"] == 4:  # the region readback after the write
                return b""
            return real_pread(fd, size, offset)

        self.write_image(2)
        expected = offline_recovery.load(self.fd, PART, 1)
        with mock.patch.object(offline_recovery.os, "pread",
                               side_effect=short_pread):
            with self.assertRaisesRegex(offline_recovery.OfflineRecoveryError,
                                        "short recovery readback"):
                offline_recovery.migrate_descriptor(self.fd, expected)

    def test_module_never_activates_or_imports_updaters_boot_writers(self):
        tree = ast.parse(Path(offline_recovery.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
        self.assertFalse(imported & {"slot_activation", "boot_writer",
                                     "trial_confirmation", "subprocess",
                                     "armada_os"})


class CallerTest(ImageCase):
    def test_prepare_check_reads_legacy_without_writing(self):
        self.write_image(1)
        initial = {
            "bootContext": "legacy-controller",
            "recoveryPartUuid": PART,
            "model": "TB322FC",
        }

        @contextmanager
        def open_recovery(*, writable):
            self.assertFalse(writable)
            fd = os.open(self.path, os.O_RDONLY)
            try:
                yield fd, PART, 1
            finally:
                os.close(fd)

        with mock.patch.object(
                offline_prepare.recovery_device, "open_recovery", open_recovery), \
                mock.patch.object(offline_recovery.os, "pwrite") as writes:
            snapshot = offline_prepare._check_recovery(
                initial, ("armada", "a" * 64, 0))
        writes.assert_not_called()
        self.assertEqual(snapshot.layout.descriptor_version, 1)
        self.assertIsNone(snapshot.record)

    def test_prepare_check_still_refuses_an_existing_record(self):
        self.write_image(3)
        os.pwrite(self.fd, trial_record.encode_record(_record(PART, INST)),
                  REGION_OFFSET + trial_record.TAIL_OFFSET_A)
        initial = {
            "bootContext": "legacy-controller",
            "recoveryPartUuid": PART,
            "model": "TB322FC",
        }

        @contextmanager
        def open_recovery(*, writable):
            self.assertFalse(writable)
            fd = os.open(self.path, os.O_RDONLY)
            try:
                yield fd, PART, 1
            finally:
                os.close(fd)

        with mock.patch.object(
                offline_prepare.recovery_device, "open_recovery", open_recovery):
            with self.assertRaisesRegex(ValueError,
                                        "cannot bind the existing trial record"):
                offline_prepare._check_recovery(
                    initial, ("armada", "a" * 64, 0))


if __name__ == "__main__":
    unittest.main()
