"""Tests for updates.boot_lock."""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_ROOT = os.path.join(ROOT, "system_files", "usr", "lib", "armada")
KSHIM = os.path.join(
    ROOT, "devices", "lenovo-tb323fu", "boot", "kshim-success-source")
TEST_C = os.path.join(KSHIM, "tests", "boot_lock_test.c")
BOOT_WITNESS_C = os.path.join(KSHIM, "src", "dt", "boot_witness.c")
INCLUDE = os.path.join(KSHIM, "src", "include")

sys.path.insert(0, SYSTEM_ROOT)

from updates.boot_lock import (  # noqa: E402
    BootLockError,
    decode_boot_lock,
    read_boot_lock,
)


class DecodeBootLockTests(unittest.TestCase):
    def _cells(self, version=1, source=1, state=1, flags=0):
        return struct.pack(">4I", version, source, state, flags)

    def test_locked_unlocked(self):
        self.assertEqual(decode_boot_lock(self._cells(state=1)), "locked")
        self.assertEqual(decode_boot_lock(self._cells(state=2)), "unlocked")

    def test_rejects_bad_length(self):
        for raw in (b"", b"\x00" * 15, b"\x00" * 17, b"\x00" * 32,
                    bytearray(15), "not bytes at all", None):
            with self.assertRaises(BootLockError):
                decode_boot_lock(raw)

    def test_rejects_bad_wire(self):
        for kw in ({"version": 0}, {"version": 2}, {"source": 0},
                   {"source": 2}, {"state": 0}, {"state": 3},
                   {"flags": 1}, {"flags": 0xFFFFFFFF}):
            with self.assertRaises(BootLockError):
                decode_boot_lock(self._cells(**kw))

    def test_rejects_wrong_byte_order_interpretation(self):
        # little-endian packing of the same cells must be rejected
        raw = struct.pack("<4I", 1, 1, 1, 0)
        with self.assertRaises(BootLockError):
            decode_boot_lock(raw)


class ReadBootLockFileTests(unittest.TestCase):
    def _write(self, tmpdir, name, data):
        path = os.path.join(tmpdir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_good_witness(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "lock", struct.pack(">4I", 1, 1, 2, 0))
            self.assertEqual(read_boot_lock(path), "unlocked")

    def test_missing_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(
                read_boot_lock(os.path.join(tmpdir, "absent")), "unknown")

    def test_oversize_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "big", b"\x00" * 17)
            with self.assertRaises(BootLockError):
                read_boot_lock(path)

    def test_malformed_contents_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "bad", struct.pack(">4I", 1, 1, 9, 0))
            with self.assertRaises(BootLockError):
                read_boot_lock(path)

    def test_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(BootLockError):
                read_boot_lock(tmpdir)

    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write(tmpdir, "real", struct.pack(">4I", 1, 1, 1, 0))
            link = os.path.join(tmpdir, "link")
            os.symlink(path, link)
            with self.assertRaises(BootLockError):
                read_boot_lock(link)


class FirmwareWitnessFixtureTests(unittest.TestCase):
    binary = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.binary = os.path.join(cls._tmp.name, "boot_lock_fixture")
        cmd = [
            "gcc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
            "-no-pie", "-I", INCLUDE, TEST_C, BOOT_WITNESS_C,
            "-lfdt", "-o", cls.binary,
        ]
        comp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if comp.returncode != 0:
            raise RuntimeError(f"compile failed:\n{comp.stderr}")
        proc = subprocess.run(
            [cls.binary], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"test binary failed:\n{proc.stderr}")
        lines = proc.stdout.splitlines()
        assert len(lines) == 2, lines
        cls.witnesses = [bytes.fromhex(line) for line in lines]

    def test_locked_witness_bytes(self):
        raw = self.witnesses[0]
        self.assertEqual(len(raw), 16)
        self.assertEqual(decode_boot_lock(raw), "locked")
        self.assertEqual(struct.unpack(">4I", raw), (1, 1, 1, 0))

    def test_unlocked_witness_bytes(self):
        raw = self.witnesses[1]
        self.assertEqual(len(raw), 16)
        self.assertEqual(decode_boot_lock(raw), "unlocked")
        self.assertEqual(struct.unpack(">4I", raw), (1, 1, 2, 0))


if __name__ == "__main__":
    unittest.main()
