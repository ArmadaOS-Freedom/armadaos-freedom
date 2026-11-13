# SPDX-License-Identifier: GPL-3.0-only
"""Durable trial journal I/O tests."""

from __future__ import annotations

import json
from dataclasses import replace
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import trial_store as store
from updates import trial_record as rec
from updates import recovery_layout as layout

SIZE = 2 * 1024 * 1024 * 1024
HEADER_BYTES = 65536
REGION_OFFSET = layout.REGION_OFFSET
REGION_BYTES = layout.REGION_BYTES


class TrialStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "recovery.img"
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        os.ftruncate(fd, SIZE)
        self.fd = fd
        self.path = path
        self.part = "11111111-2222-4333-8444-555555555555"
        self.inst = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

        meta = {
            "kind": "armada-board-recovery-image",
            "schemaVersion": 3,
            "imageBytes": SIZE,
            "headerBytes": HEADER_BYTES,
            "payloadOffset": HEADER_BYTES,
            "compressedBytes": 1024,
            "installationId": self.inst,
            "configRegionOffset": REGION_OFFSET,
            "configRegionBytes": REGION_BYTES,
            "configPartitionUuid": self.part,
            "configBoardId": 1,
        }
        header = json.dumps(meta).encode().ljust(HEADER_BYTES, b" ")

        d = bytearray(4096)
        d[0:8] = b"KSCFGV3\0"
        struct.pack_into("<IIQQQII", d, 8, 3, 4096, SIZE, 66560, REGION_OFFSET, 65536, 4096)
        d[48:64] = uuid_bytes(self.part)
        d[64:80] = uuid_bytes(self.inst)
        struct.pack_into("<6I", d, 80, 4096, 8192, 1, 28672, 32768, 4096)
        struct.pack_into("<I", d, 4092, zlib.crc32(bytes(d[:4092])) & 0xFFFFFFFF)

        region = bytearray(REGION_BYTES)
        region[0:4096] = d
        region[4096:28672] = b"L" * (28672 - 4096)

        os.pwrite(fd, header, 0)
        os.pwrite(fd, b"archive-marker", HEADER_BYTES)
        os.pwrite(fd, bytes(region), REGION_OFFSET)

    def sample(self, sequence=1, **kw) -> rec.TrialRecord:
        fields = dict(
            partition_uuid=uuid_bytes(self.part),
            installation_uuid=uuid_bytes(self.inst),
            transaction_uuid=uuid_bytes("11112222-3333-4444-8555-666677778888"),
            sequence=sequence,
            state=rec.PREPARED,
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
        fields.update(kw)
        return rec.TrialRecord(**fields)

    def test_roundtrip_region_bounds(self):
        snap = store.load(self.fd, self.part, 1)
        self.assertIsNone(snap.record)
        self.assertIsNone(snap.selected)
        before_header = os.pread(self.fd, HEADER_BYTES, 0)
        before_archive = os.pread(self.fd, 14, HEADER_BYTES)
        before = os.pread(self.fd, REGION_BYTES, REGION_OFFSET)

        with mock.patch("os.pwrite", wraps=os.pwrite) as pw:
            target = store.save(self.fd, snap, self.sample())
        pw.assert_called_once()
        self.assertEqual(pw.call_args.args[2], REGION_OFFSET + rec.TAIL_OFFSET_A)
        self.assertEqual(len(pw.call_args.args[1]), rec.RECORD_BYTES)
        self.assertEqual(target, 0)

        after = os.pread(self.fd, REGION_BYTES, REGION_OFFSET)
        keep = rec.TAIL_OFFSET_A + rec.RECORD_BYTES
        self.assertEqual(after[: rec.TAIL_OFFSET_A], before[: rec.TAIL_OFFSET_A])
        self.assertEqual(after[keep:], before[keep:])
        self.assertEqual(os.pread(self.fd, HEADER_BYTES, 0), before_header)
        self.assertEqual(os.pread(self.fd, 14, HEADER_BYTES), before_archive)

        snap2 = store.load(self.fd, self.part, 1)
        self.assertEqual(snap2.record, self.sample())
        self.assertEqual(snap2.selected, 0)

        nxt = replace(self.sample(sequence=2, state=rec.TRIAL, remaining=2))
        target2 = store.save(self.fd, snap2, nxt)
        self.assertEqual(target2, 1)
        snap3 = store.load(self.fd, self.part, 1)
        self.assertEqual(snap3.record, nxt)
        self.assertEqual(snap3.selected, 1)

    def test_stale_snapshot_and_invalid_next_no_write(self):
        snap = store.load(self.fd, self.part, 1)
        with mock.patch("os.pwrite") as pw:
            with self.assertRaises(store.TrialStoreError):
                store.save(self.fd, snap, self.sample(sequence=2))
            wrong = self.sample(partition_uuid=uuid_bytes("99999999-9999-4999-8999-999999999999"))
            with self.assertRaises(store.TrialStoreError):
                store.save(self.fd, snap, wrong)
            pw.assert_not_called()

        os.pwrite(self.fd, b"X", REGION_OFFSET + 4096)
        with mock.patch("os.pwrite") as pw:
            with self.assertRaises(store.TrialStoreError):
                store.save(self.fd, snap, self.sample())
            pw.assert_not_called()

    def test_corrupt_and_conflicting_journals(self):
        os.pwrite(self.fd, b"X" * 4096, REGION_OFFSET + rec.TAIL_OFFSET_A)
        with self.assertRaises(rec.TrialRecordError):
            store.load(self.fd, self.part, 1)

        os.pwrite(self.fd, rec.encode_record(self.sample()), REGION_OFFSET + rec.TAIL_OFFSET_A)
        os.pwrite(self.fd, b"X" * 4096, REGION_OFFSET + rec.TAIL_OFFSET_B)
        snap = store.load(self.fd, self.part, 1)
        self.assertEqual(snap.selected, 0)
        self.assertEqual(snap.record, self.sample())

        conflicting = replace(self.sample(sequence=1), plan_sha256=b"z" * 32)
        os.pwrite(self.fd, rec.encode_record(conflicting), REGION_OFFSET + rec.TAIL_OFFSET_B)
        with self.assertRaises(rec.TrialRecordError):
            store.load(self.fd, self.part, 1)


    def test_write_failures_preserve_previous_and_unlock(self):
        snapshot = store.load(self.fd, self.part, 1)
        store.save(self.fd, snapshot, self.sample())
        real_pwrite, real_pread = os.pwrite, os.pread
        real_fsync = os.fsync
        os.pwrite(self.fd, b'\x00' * rec.RECORD_BYTES,
                  store.REGION_OFFSET + rec.TAIL_OFFSET_B)
        slot_a_before = os.pread(
            self.fd, rec.RECORD_BYTES, store.REGION_OFFSET + rec.TAIL_OFFSET_A)

        faults = []

        def partial_pwrite(fd, data, offset):
            return real_pwrite(fd, data[:2048], offset)

        def flush_fsync(fd):
            raise OSError('flush failed')

        def readback_pread(fd, size, offset):
            if size == rec.RECORD_BYTES:
                return b'X' * size
            return real_pread(fd, size, offset)

        faults.append(('partial', partial_pwrite, None,
                       (store.TrialStoreError, OSError)))
        faults.append(('flush', real_pwrite, flush_fsync, store.TrialStoreError))
        faults.append(('readback', real_pwrite, None,
                       store.TrialStoreError))

        for fault, pwrite_mock, fsync_mock, expected in faults:
            with self.subTest(fault=fault):
                os.pwrite(self.fd, b'\x00' * rec.RECORD_BYTES,
                          store.REGION_OFFSET + rec.TAIL_OFFSET_B)
                snapshot = store.load(self.fd, self.part, 1)
                next_record = replace(
                    self.sample(), sequence=2, state=rec.TRIAL, remaining=2)
                with mock.patch.object(store.os, 'pwrite',
                                       side_effect=pwrite_mock):
                    if fsync_mock is not None:
                        fsync_patch = mock.patch.object(
                            store.os, 'fsync', side_effect=fsync_mock)
                    else:
                        fsync_patch = mock.patch.object(
                            store.os, 'fsync', side_effect=real_fsync)
                    with fsync_patch:
                        if fault == 'readback':
                            pread_patch = mock.patch.object(
                                store.os, 'pread', side_effect=readback_pread)
                        else:
                            pread_patch = mock.patch.object(
                                store.os, 'pread', side_effect=real_pread)
                        with pread_patch:
                            with self.assertRaises(expected):
                                store.save(self.fd, snapshot, next_record)
                slot_a_after = os.pread(
                    self.fd, rec.RECORD_BYTES,
                    store.REGION_OFFSET + rec.TAIL_OFFSET_A)
                self.assertEqual(slot_a_before, slot_a_after)

                other = os.open(self.path, os.O_RDWR)
                try:
                    import fcntl
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(other, fcntl.LOCK_UN)
                finally:
                    os.close(other)

    def test_readonly_and_competing_lock(self):
        snapshot = store.load(self.fd, self.part, 1)
        readonly_fd = os.open(self.path, os.O_RDONLY)
        try:
            with self.assertRaises(OSError):
                store.save(readonly_fd, snapshot, self.sample())
        finally:
            os.close(readonly_fd)

        competing = os.open(self.path, os.O_RDWR)
        import fcntl
        try:
            fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaises(BlockingIOError):
                    store.load(self.fd, self.part, 1)
                with self.assertRaises(BlockingIOError):
                    store.save(self.fd, snapshot, self.sample())
            finally:
                fcntl.flock(competing, fcntl.LOCK_UN)
        finally:
            os.close(competing)

        fresh = store.load(self.fd, self.part, 1)
        self.assertIsNone(fresh.record)

    def test_layout_versions_and_shortread(self):
        region_offset = 2 * 1024 * 1024 * 1024 - 65536
        pristine = os.pread(self.fd, 65536, region_offset)
        for ver, tail in ((1, 12288), (2, 28672)):
            buf = bytearray(pristine)
            struct.pack_into('<I', buf, 8, ver)
            buf[92:4092] = b'\x00' * 4000
            struct.pack_into('<I', buf, 4092, zlib.crc32(buf[:4092]) & 0xffffffff)
            buf[tail:] = b'\x00' * (65536 - tail)
            os.pwrite(self.fd, bytes(buf), region_offset)
            with self.assertRaises(store.TrialStoreError):
                store.load(self.fd, self.part, 1)
            os.pwrite(self.fd, pristine, region_offset)

        for idx in (4092, 36864):
            buf = bytearray(pristine)
            buf[idx] ^= 1
            os.pwrite(self.fd, bytes(buf), region_offset)
            with self.assertRaises(layout.RecoveryLayoutError):
                store.load(self.fd, self.part, 1)
        os.pwrite(self.fd, pristine, region_offset)

        with self.assertRaises(layout.RecoveryLayoutError):
            store.load(self.fd, '11111111-2222-4333-8444-555555555556', 1)

        with mock.patch.object(store.os, 'pread', return_value=b''):
            with self.assertRaises(layout.RecoveryLayoutError):
                store.load(self.fd, self.part, 1)

    def test_save_pins_copy_on_write_previous_copy(self):
        snap = store.load(self.fd, self.part, 1)
        self.assertIsNone(snap.selected)
        untouched_b = os.pread(
            self.fd, rec.RECORD_BYTES, store.REGION_OFFSET + rec.TAIL_OFFSET_B)
        self.assertEqual(store.save(self.fd, snap, self.sample()), 0)
        self.assertEqual(
            os.pread(self.fd, rec.RECORD_BYTES,
                     store.REGION_OFFSET + rec.TAIL_OFFSET_A),
            rec.encode_record(self.sample()))
        self.assertEqual(
            os.pread(self.fd, rec.RECORD_BYTES,
                     store.REGION_OFFSET + rec.TAIL_OFFSET_B),
            untouched_b)

        snap2 = store.load(self.fd, self.part, 1)
        self.assertEqual(snap2.selected, 0)
        other_offset = store.REGION_OFFSET + rec.TAIL_OFFSET_A
        real_pread = os.pread
        reads = {'n': 0}

        def corrupt_other_copy(fd, size, offset):
            if size == rec.RECORD_BYTES and offset == other_offset:
                reads['n'] += 1
                if reads['n'] == 2:
                    return b'Z' * size
            return real_pread(fd, size, offset)

        nxt = self.sample(sequence=2, state=rec.TRIAL, remaining=2)
        with mock.patch.object(store.os, 'pread', side_effect=corrupt_other_copy):
            with self.assertRaisesRegex(store.TrialStoreError, 'copy-on-write'):
                store.save(self.fd, snap2, nxt)

    def test_save_requires_durable_full_journal_readback(self):
        snap = store.load(self.fd, self.part, 1)
        real_read = store._read_snapshot
        calls = {'n': 0}

        def read_snapshot(fd, partition_uuid, board):
            calls['n'] += 1
            value = real_read(fd, partition_uuid, board)
            if calls['n'] == 2:
                return replace(value, record=None)
            return value

        with mock.patch.object(store, '_read_snapshot', side_effect=read_snapshot):
            with self.assertRaisesRegex(store.TrialStoreError, 'durable readback'):
                store.save(self.fd, snap, self.sample())

    def test_max_sequence(self):
        region_offset = 2 * 1024 * 1024 * 1024 - 65536
        max_rec = self.sample(sequence=2**64 - 1)
        os.pwrite(self.fd, rec.encode_record(max_rec), region_offset + 28672)
        snap = store.load(self.fd, self.part, 1)
        with mock.patch.object(store.os, 'pwrite') as m_pwrite:
            with self.assertRaises(store.TrialStoreError):
                store.save(self.fd, snap, max_rec)
            m_pwrite.assert_not_called()


def uuid_bytes(text: str) -> bytes:
    import uuid as _uuid

    return _uuid.UUID(text).bytes


if __name__ == "__main__":
    unittest.main()
