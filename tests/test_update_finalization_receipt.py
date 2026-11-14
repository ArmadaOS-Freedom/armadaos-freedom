# SPDX-License-Identifier: GPL-3.0-only
"""Tests for durable trial finalization receipts."""

from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_trial_status as status_test
from updates import finalization_receipt as receipt
from updates.trial_record import CONFIRMED, TRIAL


class DocumentTests(unittest.TestCase):
    def test_document_binds_record_and_rejects_nonconfirmed(self):
        record = status_test.make_record(state=CONFIRMED)
        doc = receipt._document(record)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["recordSequence"], record.sequence)
        self.assertEqual(doc["recordSha256"], __import__("hashlib").sha256(
            __import__("updates.trial_record", fromlist=["encode_record"]).encode_record(record)
        ).hexdigest())
        self.assertEqual(doc["bootPolicy"], "dual-boot-no-slot-control")
        self.assertEqual(doc["firmwareSlotControl"], "not_issued")
        for changed in (
            replace(record, sequence=record.sequence + 1),
            replace(record, new_serial=record.new_serial + 1),
        ):
            with self.subTest(sequence=changed.sequence):
                self.assertNotEqual(doc, receipt._document(changed))
        with self.assertRaises(ValueError):
            receipt._document(replace(record, state=TRIAL))

    def test_writer_denies_nonroot_before_touching_state(self):
        with mock.patch.object(receipt.os, "geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                receipt.write(status_test.make_record(state=CONFIRMED))


@unittest.skipUnless(os.geteuid() == 0, "requires root filesystem ownership")
class ReceiptFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "receipts" / "finalized.json"
        patcher = mock.patch.object(receipt, "STATE_PATH", self.target)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = status_test.make_record(state=CONFIRMED)

    def test_roundtrip_canonical_hash_and_mode(self):
        doc = receipt.write(self.record)
        raw = self.target.read_bytes()
        self.assertEqual(raw, (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode())
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(receipt.read(self.record), {"state": "complete"})

    def test_every_bound_record_change_is_pending(self):
        receipt.write(self.record)
        changes = {
            "sequence": {"sequence": 10},
            "transactionUUID": {"transaction_uuid": bytes.fromhex("31" * 16)},
            "partitionUUID": {"partition_uuid": bytes.fromhex("11" * 16)},
            "installationUUID": {"installation_uuid": bytes.fromhex("21" * 16)},
            "from_slot": {"from_slot": 1, "target_slot": 0},
            "old_commit": {"old_commit": bytes.fromhex("a2" * 32)},
            "new_commit": {"new_commit": bytes.fromhex("b3" * 32)},
            "old_serial": {"old_serial": 2},
            "new_serial": {"new_serial": 3},
            "os_name": {"os_name": "other"},
            "old_boot": {"old_boot_sha256": bytes.fromhex("d5" * 32)},
            "new_boot": {"new_boot_sha256": bytes.fromhex("e6" * 32)},
            "plan": {"plan_sha256": bytes.fromhex("f7" * 32)},
        }
        for name, kwargs in changes.items():
            with self.subTest(field=name):
                self.assertEqual(receipt.read(replace(self.record, **kwargs))["state"], "pending")

    def test_malformed_documents_are_pending(self):
        self.target.parent.mkdir(mode=0o755)
        good = json.dumps(receipt._document(self.record), sort_keys=True, separators=(",", ":"))
        cases = {
            "missing": "{}",
            "invalid": "{",
            "duplicate": good.replace('"schemaVersion":1', '"schemaVersion":1,"schemaVersion":1'),
            "extra": good[:-1] + ',"extra":1}',
            "booleanSequence": good.replace('"recordSequence":9', '"recordSequence":true'),
            "booleanSchema": good.replace('"schemaVersion":1', '"schemaVersion":true'),
            "wrongType": good.replace('"firmwareSlotControl":"not_issued"', '"firmwareSlotControl":1'),
            "array": "[]",
            "oversize": " " * 8193,
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.target.write_text(text)
                self.assertEqual(receipt.read(self.record)["state"], "pending")

    def test_symlinks_and_unsafe_modes_rejected_without_chmod(self):
        self.target.parent.mkdir(mode=0o755)
        real = Path(self.tmp.name) / "real"
        real.mkdir(mode=0o755)
        real_target = real / "finalized.json"

        # Seed a genuinely valid receipt through its real path and prove the
        # same bytes read complete there.
        with mock.patch.object(receipt, "STATE_PATH", real_target):
            receipt.write(self.record)
            self.assertEqual(receipt.read(self.record), {"state": "complete"})

        # A file symlink alias to that valid receipt must still be pending.
        self.target.symlink_to(real_target)
        self.assertEqual(receipt.read(self.record)["state"], "pending")
        self.target.unlink()

        # A directory symlink alias to the valid receipt's directory is also
        # pending even though the file reached through it is valid.
        link = Path(self.tmp.name) / "link"
        link.symlink_to(real, target_is_directory=True)
        with mock.patch.object(receipt, "STATE_PATH", link / "finalized.json"):
            self.assertEqual(receipt.read(self.record)["state"], "pending")

        unsafe = Path(self.tmp.name) / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with mock.patch.object(receipt, "STATE_PATH", unsafe / "finalized.json"):
            with self.assertRaises(ValueError):
                receipt.write(self.record)
            self.assertEqual(stat.S_IMODE(unsafe.stat().st_mode), 0o777)
        receipt.write(self.record)
        self.target.chmod(0o666)
        self.assertEqual(receipt.read(self.record)["state"], "pending")

    def test_replace_failure_retains_receipt_and_cleans_temp(self):
        receipt.write(self.record)
        before = self.target.read_bytes()
        with mock.patch.object(receipt.os, "replace", side_effect=OSError("replace")):
            with self.assertRaises(OSError):
                receipt.write(self.record)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_directory_open_failure_before_replace_preserves_previous_receipt(self):
        receipt.write(self.record)
        before = self.target.read_bytes()
        real_open = os.open
        directory_flag = getattr(os, "O_DIRECTORY", 0)

        def open_dir_fails(path, flags, *args, **kwargs):
            if directory_flag and flags & directory_flag:
                raise OSError(errno.EACCES, "simulated directory open failure")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(receipt.os, "open", side_effect=open_dir_fails):
            with self.assertRaises(OSError):
                receipt.write(self.record)

        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        self.assertEqual(receipt.read(self.record), {"state": "complete"})

    def test_flush_fsync_replace_and_directory_fsync_order(self):
        events = []
        real_fsync = os.fsync
        real_replace = os.replace

        def fsync(fd):
            events.append("fsync")
            return real_fsync(fd)

        def replace_file(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        with mock.patch.object(receipt.os, "fsync", side_effect=fsync), mock.patch.object(receipt.os, "replace", side_effect=replace_file):
            receipt.write(self.record)
        # The temp file is flushed first, then every directory entry leading to
        # the receipt, then the rename, then the immediate parent again so the
        # new entry itself is durable.
        self.assertEqual(events[0], "fsync")
        self.assertEqual(events[-1], "fsync")
        self.assertEqual(events.count("replace"), 1)
        replace_at = events.index("replace")
        self.assertGreaterEqual(replace_at, 2)
        self.assertTrue(all(event == "fsync" for event in events if event != "replace"))
        self.assertEqual(events[replace_at - 1], "fsync")

    def test_ancestor_inspection_failure_is_not_swallowed(self):
        self.target.parent.mkdir(mode=0o755)
        real_lstat = os.lstat
        blocked = self.target.parent.parent

        def lstat(path, *args, **kwargs):
            if Path(path) == blocked:
                raise OSError(errno.EIO, "simulated ancestor lstat failure")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(receipt.os, "lstat", side_effect=lstat):
            with self.assertRaises(OSError):
                receipt._durable_ancestors(self.target.parent)

    def test_filesystem_root_entry_is_flushed_when_on_same_device(self):
        self.target.parent.mkdir(mode=0o755)
        real_fsync = os.fsync
        flushed = []

        def record(fd):
            info = os.fstat(fd)
            flushed.append((info.st_dev, info.st_ino))
            return real_fsync(fd)

        with mock.patch.object(receipt.os, "fsync", side_effect=record):
            receipt._durable_ancestors(self.target.parent)

        root = Path(self.target.anchor)
        root_info = os.lstat(root)
        parent_info = os.lstat(self.target.parent)
        # Outermost first, ending at the receipt's own parent.
        self.assertEqual(flushed[-1], (parent_info.st_dev, parent_info.st_ino))
        if root_info.st_dev == parent_info.st_dev:
            # The top-level directory's entry lives in the filesystem root, so
            # the root must be part of the flushed chain too.
            self.assertEqual(flushed[0], (root_info.st_dev, root_info.st_ino))

    def test_directory_fsync_failure_invalidates_replaced_receipt(self):
        self.target.parent.mkdir(mode=0o755)
        unrelated = self.target.parent / "unrelated.json"
        unrelated.write_text("other writer\n")
        real_fsync = os.fsync
        real_replace = os.replace
        renamed = {"done": False}

        def replace_file(src, dst):
            result = real_replace(src, dst)
            renamed["done"] = True
            return result

        def fail_directory_fsync(fd):
            # Fail only the flush that follows the rename, so this exercises
            # the "receipt visible but directory entry not durable" path.
            if renamed["done"] and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "simulated directory flush failure")
            return real_fsync(fd)

        with mock.patch.object(
            receipt.os, "replace", side_effect=replace_file
        ), mock.patch.object(
            receipt.os, "fsync", side_effect=fail_directory_fsync
        ):
            with self.assertRaises(OSError):
                receipt.write(self.record)

        # The rename landed but the directory entry is not durable, so the
        # receipt it installed must not stay readable as complete evidence.
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(unrelated.read_text(), "other writer\n")
        self.assertEqual(
            sorted(path.name for path in self.target.parent.iterdir()),
            ["unrelated.json"])
        self.assertEqual(receipt.read(self.record)["state"], "pending")

        # A retry installs and flushes a fresh complete receipt.
        doc = receipt.write(self.record)
        self.assertEqual(receipt.read(self.record), {"state": "complete"})
        self.assertEqual(
            self.target.read_bytes(),
            (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode())

    def test_partially_synced_created_parents_are_flushed_again_on_retry(self):
        real_fsync = os.fsync
        failed = {"done": False}

        def fail_created_parent_fsync(fd):
            # Fail the flush of the directory this call created, not a
            # pre-existing ancestor, so the retry story below is exact.
            info = os.fstat(fd)
            parent = os.lstat(self.target.parent)
            if (
                not failed["done"]
                and stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino)
            ):
                failed["done"] = True
                raise OSError(errno.EIO, "simulated parent flush failure")
            return real_fsync(fd)

        with mock.patch.object(receipt.os, "fsync", side_effect=fail_created_parent_fsync):
            with self.assertRaises(OSError):
                receipt.write(self.record)

        # The directories were created but their entries were not flushed, so
        # no receipt may be visible and the failure must not be stranded.
        self.assertTrue(failed["done"])
        self.assertTrue(self.target.parent.is_dir())
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(receipt.read(self.record)["state"], "pending")

        # A retry finds the directories already present; it must flush them
        # again instead of skipping straight to the rename.
        flushed = []

        def count_directory_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                flushed.append(fd)
            return real_fsync(fd)

        with mock.patch.object(receipt.os, "fsync", side_effect=count_directory_fsync):
            doc = receipt.write(self.record)

        self.assertGreaterEqual(len(flushed), 2)
        self.assertEqual(receipt.read(self.record), {"state": "complete"})
        self.assertEqual(
            self.target.read_bytes(),
            (json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode())

    def test_invalidation_never_removes_a_foreign_file(self):
        self.target.parent.mkdir(mode=0o755)
        self.target.write_text("other writer\n")
        st = self.target.stat()
        receipt._discard_installed(self.target, (st.st_dev, st.st_ino + 1))
        self.assertEqual(self.target.read_text(), "other writer\n")
        receipt._discard_installed(self.target, (st.st_dev, st.st_ino))
        self.assertFalse(os.path.lexists(self.target))


if __name__ == "__main__":
    unittest.main()
