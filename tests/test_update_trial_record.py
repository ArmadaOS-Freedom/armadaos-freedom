# SPDX-License-Identifier: GPL-3.0-only
import ctypes
from dataclasses import asdict, replace
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
from updates import trial_record as tr

U8X16 = ctypes.c_uint8 * 16
U8X32 = ctypes.c_uint8 * 32


class CRecord(ctypes.Structure):
    _fields_ = [
        ("sequence", ctypes.c_uint64),
        ("partition_uuid", U8X16), ("installation_uuid", U8X16),
        ("transaction_uuid", U8X16),
        ("state", ctypes.c_uint32), ("from_slot", ctypes.c_uint32),
        ("target_slot", ctypes.c_uint32), ("remaining", ctypes.c_uint32),
        ("attempt_limit", ctypes.c_uint32), ("old_serial", ctypes.c_uint32),
        ("new_serial", ctypes.c_uint32), ("os_name", ctypes.c_char * 65),
        ("old_commit", U8X32), ("new_commit", U8X32),
        ("old_boot_sha256", U8X32), ("new_boot_sha256", U8X32),
        ("plan_sha256", U8X32),
        ("policy", ctypes.c_uint32), ("other_boot_sha256", U8X32),
    ]


PART = bytes(range(16))
INST = bytes(range(16, 32))


def sample():
    return tr.TrialRecord(
        sequence=0x0102030405060708, partition_uuid=PART,
        installation_uuid=INST, transaction_uuid=bytes(range(32, 48)),
        state=tr.PREPARED, from_slot=0, target_slot=1, remaining=7,
        attempt_limit=7, old_serial=0xFFFFFFFF, new_serial=7,
        os_name="armada-1.2_test", old_commit=bytes(range(32)),
        new_commit=bytes(range(1, 33)), old_boot_sha256=bytes(range(2, 34)),
        new_boot_sha256=bytes(range(3, 35)), plan_sha256=bytes(range(4, 36)))


def python_record(c):
    return tr.TrialRecord(
        c.sequence, bytes(c.partition_uuid), bytes(c.installation_uuid),
        bytes(c.transaction_uuid), c.state, c.from_slot, c.target_slot,
        c.remaining, c.attempt_limit, c.old_serial, c.new_serial,
        c.os_name.decode("ascii"), bytes(c.old_commit), bytes(c.new_commit),
        bytes(c.old_boot_sha256), bytes(c.new_boot_sha256), bytes(c.plan_sha256),
        c.policy, bytes(c.other_boot_sha256))


def c_record(r):
    return CRecord(
        r.sequence, U8X16.from_buffer_copy(r.partition_uuid),
        U8X16.from_buffer_copy(r.installation_uuid),
        U8X16.from_buffer_copy(r.transaction_uuid), r.state, r.from_slot,
        r.target_slot, r.remaining, r.attempt_limit, r.old_serial,
        r.new_serial, r.os_name.encode("ascii"),
        U8X32.from_buffer_copy(r.old_commit),
        U8X32.from_buffer_copy(r.new_commit),
        U8X32.from_buffer_copy(r.old_boot_sha256),
        U8X32.from_buffer_copy(r.new_boot_sha256),
        U8X32.from_buffer_copy(r.plan_sha256), r.policy,
        U8X32.from_buffer_copy(r.other_boot_sha256))


def legacy_v1_encode(r):
    os_name = r.os_name.encode("ascii")
    body = struct.pack(
        "<8sIIQ16s16s16s7I65s3s32s32s32s32s32s3764s",
        tr.MAGIC, tr.VERSION, 4096, r.sequence, r.partition_uuid,
        r.installation_uuid, r.transaction_uuid, r.state, r.from_slot,
        r.target_slot, r.remaining, r.attempt_limit, r.old_serial,
        r.new_serial, os_name + bytes(65 - len(os_name)), bytes(3),
        r.old_commit, r.new_commit, r.old_boot_sha256, r.new_boot_sha256,
        r.plan_sha256, bytes(3764))
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


# storage_block.h: the two selector outcomes this test distinguishes.
KSHIM_STORAGE_FORMAT = -3
KSHIM_STORAGE_EMPTY = -8


def resealed(wire, offset, fmt, value):
    data = bytearray(wire)
    struct.pack_into(fmt, data, offset, value)
    struct.pack_into("<I", data, 4092, zlib.crc32(data[:4092]) & 0xFFFFFFFF)
    return bytes(data)


class UpdateTrialRecordTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = ROOT / "devices/lenovo-tb323fu/boot/kshim-success-source/src"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        library = Path(cls.tmp.name) / "libupdate_trial.so"
        subprocess.run([
            "gcc", "-shared", "-fPIC", "-std=c11", "-Wall", "-Wextra",
            "-Werror", "-I", str(src / "include"),
            str(src / "core/update_trial.c"), str(src / "core/storage_block.c"),
            "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.kshim_update_trial_encode.argtypes = [
            ctypes.POINTER(CRecord), ctypes.c_void_p, ctypes.c_size_t]
        cls.lib.kshim_update_trial_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(CRecord)]
        cls.lib.kshim_update_trial_select.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(CRecord), ctypes.POINTER(ctypes.c_uint)]
        for name in ("encode", "decode", "select"):
            getattr(cls.lib, "kshim_update_trial_" + name).restype = ctypes.c_int
        cls.lib.kshim_update_trial_empty_marker.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t]
        cls.lib.kshim_update_trial_empty_marker.restype = ctypes.c_int
        cls.lib.kshim_update_trial_is_empty_marker.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t]
        cls.lib.kshim_update_trial_is_empty_marker.restype = ctypes.c_int

    def c_encode(self, record):
        source, output = c_record(record), ctypes.create_string_buffer(4096)
        rc = self.lib.kshim_update_trial_encode(
            ctypes.byref(source), output, len(output))
        self.assertEqual(rc, 0)
        return output.raw

    def c_decode(self, wire):
        output = CRecord()
        rc = self.lib.kshim_update_trial_decode(
            wire, len(wire), PART, INST, ctypes.byref(output))
        return rc, output

    def assert_both_decode(self, record):
        wire = tr.encode_record(record)
        self.assertEqual(self.c_encode(record), wire)
        rc, decoded = self.c_decode(wire)
        self.assertEqual(rc, 0)
        self.assertEqual(asdict(python_record(decoded)), asdict(record))
        self.assertEqual(tr.decode_record(self.c_encode(record), PART, INST), record)

    def assert_both_reject(self, wire):
        with self.assertRaises(tr.TrialRecordError):
            tr.decode_record(wire, PART, INST)
        self.assertNotEqual(self.c_decode(wire)[0], 0)

    def c_select(self, a, b):
        output, selected = CRecord(), ctypes.c_uint(99)
        rc = self.lib.kshim_update_trial_select(
            a, b, len(a), PART, INST, ctypes.byref(output), ctypes.byref(selected))
        return rc, selected.value

    def assert_select_empty(self, a, b):
        """Both implementations must call this pair a durable empty journal."""
        self.assertEqual(self.c_select(a, b), (KSHIM_STORAGE_EMPTY, 99))
        with self.assertRaises(tr.EmptyTrialJournal):
            tr.select_record(a, b, PART, INST)

    def assert_select_invalid(self, a, b):
        """Both implementations must call this pair invalid, never empty."""
        self.assertEqual(self.c_select(a, b), (KSHIM_STORAGE_FORMAT, 99))
        with self.assertRaises(tr.TrialRecordError) as ctx:
            tr.select_record(a, b, PART, INST)
        self.assertNotIsInstance(ctx.exception, tr.EmptyTrialJournal)

    def assert_select(self, a, b, index=None, error=None):
        output, selected = CRecord(), ctypes.c_uint(99)
        rc = self.lib.kshim_update_trial_select(
            a, b, len(a), PART, INST, ctypes.byref(output), ctypes.byref(selected))
        if error is not None:
            self.assertNotEqual(rc, 0)
            with self.assertRaises(error):
                tr.select_record(a, b, PART, INST)
            return
        self.assertEqual(rc, 0)
        record, py_index = tr.select_record(a, b, PART, INST)
        self.assertEqual((selected.value, py_index), (index, index))
        self.assertEqual(python_record(output), record)

    def test_cross_language_codec_and_state_policy(self):
        base = sample()
        self.assert_both_decode(base)
        cases = [(tr.TRIAL, 0), (tr.TRIAL, 6), (tr.CONFIRMED, 0),
                 (tr.CONFIRMED, 6), (tr.ROLLED_BACK, 0), (tr.ROLLED_BACK, 7)]
        for state, remaining in cases:
            with self.subTest(state=state, remaining=remaining):
                self.assert_both_decode(replace(base, state=state, remaining=remaining))
        longest = replace(base, os_name="A" * 64)
        self.assert_both_decode(longest)
        offline = replace(
            base, policy=tr.OFFLINE_DUAL,
            other_boot_sha256=bytes(range(36, 68)))
        self.assert_both_decode(offline)
        self.assertEqual(tr.encode_record(base), legacy_v1_encode(base))

    def test_independently_resealed_mutations_rejected(self):
        wire = tr.encode_record(sample())
        mutations = {
            "reserved_165": resealed(wire, 165, "<B", 1),
            "reserved_328": resealed(wire, 328, "<B", 1),
            "reserved_332": resealed(wire, 332, "<B", 1),
            "bad_padding": resealed(wire, 100 + len(sample().os_name) + 1, "<B", 1),
            "wrong_magic": resealed(wire, 0, "<B", ord("X")),
            "wrong_version": resealed(wire, 8, "<I", 2),
            "zero_uuid": resealed(wire, 56, "<16s", bytes(16)),
            "zero_hash": resealed(wire, 168, "<32s", bytes(32)),
            "same_slot": resealed(wire, 80, "<I", 0),
            "bad_state": resealed(wire, 72, "<I", 5),
            "prepared_policy": resealed(wire, 84, "<I", 6),
            "wrong_identity": resealed(wire, 40, "<16s", bytes(range(48, 64))),
        }
        for name, changed in mutations.items():
            with self.subTest(name=name):
                self.assert_both_reject(changed)
        bad_crc = bytearray(wire)
        bad_crc[-1] ^= 1
        self.assert_both_reject(bytes(bad_crc))
        with self.assertRaises(tr.EmptyTrialJournal):
            tr.decode_record(bytes(4096), PART, INST)
        self.assertNotEqual(self.c_decode(bytes(4096))[0], 0)

        offline = tr.encode_record(replace(
            sample(), policy=tr.OFFLINE_DUAL,
            other_boot_sha256=bytes(range(36, 68))))
        offline_mutations = {
            "unknown_policy": resealed(offline, 328, "<I", 2),
            "zero_other_hash": resealed(offline, 332, "<32s", bytes(32)),
            "offline_rolled_back": resealed(offline, 72, "<I", tr.ROLLED_BACK),
            "offline_reserved": resealed(offline, 364, "<B", 1),
            "unknown_version": resealed(offline, 8, "<I", 3),
        }
        for name, changed in offline_mutations.items():
            with self.subTest(name=name):
                self.assert_both_reject(changed)

    def test_selection_outcomes(self):
        old = tr.encode_record(sample())
        new = tr.encode_record(replace(sample(), sequence=sample().sequence + 1))
        self.assert_select(new, old, 0)
        self.assert_select(old, new, 1)
        self.assert_select(old, old, 0)
        conflict = tr.encode_record(replace(sample(), new_serial=8))
        self.assert_select(old, conflict, error=tr.TrialRecordError)
        torn = bytearray(old)
        torn[-1] ^= 1
        self.assert_select(old, bytes(torn), 0)
        self.assert_select(bytes(torn), old, 1)
        self.assert_select(old, bytes(4096), 0)
        self.assert_select(bytes(4096), old, 1)
        offline = tr.encode_record(replace(
            sample(), sequence=sample().sequence + 2,
            policy=tr.OFFLINE_DUAL,
            other_boot_sha256=bytes(range(36, 68))))
        self.assert_select(old, offline, 1)
        self.assert_select(offline, old, 0)
        self.assert_select_empty(bytes(4096), bytes(4096))
        self.assert_select_invalid(bytes(torn), bytes(4096))

    def test_logical_empty_marker_parity(self):
        zero = bytes(4096)
        marker = tr.EMPTY_MARKER
        self.assertEqual(len(marker), 4096)
        buf = ctypes.create_string_buffer(4096)
        self.assertEqual(self.lib.kshim_update_trial_empty_marker(buf, 4096), 0)
        self.assertEqual(buf.raw, marker)
        self.assertEqual(self.lib.kshim_update_trial_is_empty_marker(marker, 4096), 1)
        self.assertEqual(self.lib.kshim_update_trial_is_empty_marker(zero, 4096), 0)
        self.assert_both_reject(marker)
        self.assert_select_empty(zero, marker)
        with self.assertRaises(tr.LogicalEmptyTrialJournal):
            tr.select_record(zero, marker, PART, INST)
        torn = bytearray(tr.encode_record(sample()))
        torn[300] ^= 1
        self.assert_select_empty(bytes(torn), marker)
        self.assert_select_invalid(marker, zero)
        # The sentinel belongs to copy B only: a marker pair is a writer fault,
        # never an empty journal, so a real record cannot be masked that way.
        self.assert_select_invalid(marker, marker)
        old = tr.encode_record(sample())
        self.assert_select(old, marker, 0)
        # A complete CRC-valid image next to the marker is never empty, even
        # when only its identity is foreign or its version is unsupported.
        foreign = resealed(old, 40, "<16s", bytes(range(48, 64)))
        self.assert_select_invalid(foreign, marker)
        unknown = resealed(old, 8, "<I", 3)
        self.assert_select_invalid(unknown, marker)

    def test_python_strict_arguments(self):
        base = sample()
        invalid = [
            ("sequence", True), ("sequence", 1.0), ("sequence", 0),
            ("sequence", 1 << 64), ("state", False), ("remaining", -1),
            ("remaining", 8), ("attempt_limit", 0), ("attempt_limit", 8),
            ("old_serial", -1), ("old_serial", 1 << 32),
            ("new_serial", 1.0), ("from_slot", 2), ("target_slot", True),
            ("os_name", ""), ("os_name", "A" * 65), ("os_name", "-bad"),
            ("os_name", "bad/name"), ("os_name", "caf\u00e9"), ("os_name", b"ok"),
        ]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                with self.assertRaises(tr.TrialRecordError):
                    tr.encode_record(replace(base, **{field: value}))


if __name__ == "__main__":
    unittest.main()
