import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
UPDATES_PARENT = ROOT / "system_files" / "usr" / "lib" / "armada"
if str(UPDATES_PARENT) not in sys.path:
    sys.path.insert(0, str(UPDATES_PARENT))


import armada_os  # noqa: E402
from updates import boot_context  # noqa: E402
from updates.boot_context import (  # noqa: E402
    CMDLINE_MAX,
    BootContextError,
    decode_boot_context,
    read_boot_context,
)
from updates.system_manager import SystemManager  # noqa: E402

COMMIT_CSUM = "a" * 64
BOOT_CSUM = "b" * 64
OTHER_CSUM = "c" * 64


def make_witness(version=1, source=1, slot=0, flags=0, fmt=">4I"):
    return struct.pack(fmt, version, source, slot, flags)


def make_booted(
    stateroot="armada",
    checksum=COMMIT_CSUM,
    serial=0,
    version="1.2.3",
    incompatible=False,
):
    return {
        "stateroot": stateroot,
        "checksum": checksum,
        "serial": serial,
        "version": version,
        "incompatible": incompatible,
    }


def make_cmdline(
    osname="armada",
    commit=COMMIT_CSUM,
    serial=0,
    bootver=0,
    ostree_osname=None,
    bootcsum=BOOT_CSUM,
    bootidx=0,
    extra="console=ttyMSM0,115200n8 quiet",
):
    ostree_osname = osname if ostree_osname is None else ostree_osname
    return (
        f"{extra} "
        f"armada.deployment={osname}/{commit}.{serial} "
        f"ostree=/ostree/boot.{bootver}/{ostree_osname}/{bootcsum}/{bootidx}"
    ).strip()


class BootContextTests(unittest.TestCase):
    def test_valid_combinations_across_physical_slots_and_ostree_bootversions(self):
        for witness_slot, actual, inactive in ((0, "a", "b"), (1, "b", "a")):
            for bootver in (0, 1):
                with self.subTest(witness_slot=witness_slot, bootver=bootver):
                    witness = make_witness(slot=witness_slot)
                    cmdline = make_cmdline(
                        osname="freedom_os.1-0",
                        commit=COMMIT_CSUM,
                        serial=7,
                        bootver=bootver,
                        bootcsum=BOOT_CSUM,
                        bootidx=3,
                    )
                    booted = make_booted(
                        stateroot="freedom_os.1-0",
                        checksum=COMMIT_CSUM,
                        serial=7,
                    )
                    self.assertNotEqual(COMMIT_CSUM, BOOT_CSUM)
                    ctx = decode_boot_context(witness, cmdline, booted)
                    self.assertEqual(
                        ctx,
                        {
                            "state": "available",
                            "actualSlot": actual,
                            "inactiveSlot": inactive,
                            "deployment": f"freedom_os.1-0/{COMMIT_CSUM}.7",
                        },
                    )

    def test_lookalike_tokens_and_whitespace_are_ignored(self):
        cmdline = (
            "\tarmada.deployment_extra=ignored "
            "my.armada.deployment=ignored "
            "ostree_extra=/ostree/boot.0/armada/" + OTHER_CSUM + "/0\n"
            "foo.ostree=/ostree/boot.1/armada/" + OTHER_CSUM + "/0\r\f\v "
            + make_cmdline(serial=42, bootver=1, bootidx=9)
        )
        ctx = decode_boot_context(
            bytearray(make_witness(slot=1)),
            cmdline,
            make_booted(serial=42),
        )
        self.assertEqual(ctx["actualSlot"], "b")
        self.assertEqual(ctx["inactiveSlot"], "a")
        self.assertEqual(ctx["deployment"], f"armada/{COMMIT_CSUM}.42")

    def test_invalid_witness_rejected(self):
        cases = [
            ("none", None),
            ("str", "0" * 16),
            ("short", b"\x00" * 15),
            ("long", b"\x00" * 17),
            ("version_0", make_witness(version=0)),
            ("version_2", make_witness(version=2)),
            ("source_0", make_witness(source=0)),
            ("source_2", make_witness(source=2)),
            ("slot_2", make_witness(slot=2)),
            ("slot_max", make_witness(slot=0xFFFFFFFF)),
            ("flags_nonzero", make_witness(flags=1)),
            ("little_endian", make_witness(slot=0, fmt="<4I")),
        ]
        cmdline = make_cmdline()
        booted = make_booted()
        for name, witness in cases:
            with self.subTest(case=name):
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, cmdline, booted)

    def test_cmdline_type_nul_unicode_and_size_limits(self):
        witness = make_witness()
        booted = make_booted()
        base = make_cmdline()
        pad_len = CMDLINE_MAX - len(base.encode("utf-8")) - 1
        exact_cmdline = base + " " + ("x" * pad_len)
        self.assertEqual(len(exact_cmdline.encode("utf-8")), CMDLINE_MAX)
        self.assertEqual(
            decode_boot_context(witness, exact_cmdline, booted)["state"],
            "available",
        )

        invalid_cases = [
            ("bytes_cmdline", b"armada.deployment=..."),
            ("none_cmdline", None),
            ("nul_inside", base + "\x00"),
            ("nul_token", base + " quiet\x00"),
            ("surrogate", base + " \ud800"),
            ("oversize_ascii", exact_cmdline + "x"),
            ("oversize_multibyte", base + " " + ("é" * (CMDLINE_MAX // 2))),
        ]
        for name, bad_cmdline in invalid_cases:
            with self.subTest(case=name):
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, bad_cmdline, booted)

    def test_bare_missing_and_duplicate_binding_tokens_rejected(self):
        witness = make_witness()
        booted = make_booted()
        dep = f"armada.deployment=armada/{COMMIT_CSUM}.0"
        ost = f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/0"
        cases = [
            ("missing_both", "quiet splash"),
            ("missing_deployment", ost),
            ("missing_ostree", dep),
            ("bare_deployment", f"armada.deployment { dep } { ost }"),
            ("bare_ostree", f"ostree { dep } { ost }"),
            ("dup_equal_deployment", f"{dep} {dep} {ost}"),
            (
                "dup_diff_deployment",
                f"{dep} armada.deployment=armada/{OTHER_CSUM}.0 {ost}",
            ),
            ("dup_equal_ostree", f"{dep} {ost} {ost}"),
            (
                "dup_diff_ostree",
                f"{dep} {ost} ostree=/ostree/boot.1/armada/{OTHER_CSUM}/0",
            ),
        ]
        for name, cmdline in cases:
            with self.subTest(case=name):
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, cmdline, booted)

    def test_malformed_deployment_binding_rejected(self):
        witness = make_witness()
        ost = f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/0"
        huge_digits = "9" * 5000
        cases = [
            ("empty_value", "armada.deployment="),
            ("traversal_dotdot", f"armada.deployment=../{COMMIT_CSUM}.0"),
            ("traversal_nested", f"armada.deployment=a/b/{COMMIT_CSUM}.0"),
            ("leading_dash_osname", f"armada.deployment=-armada/{COMMIT_CSUM}.0"),
            ("leading_dot_osname", f"armada.deployment=.armada/{COMMIT_CSUM}.0"),
            ("long_osname", f"armada.deployment={'a' * 65}/{COMMIT_CSUM}.0"),
            ("bad_char_osname", f"armada.deployment=armada$1/{COMMIT_CSUM}.0"),
            ("uppercase_checksum", f"armada.deployment=armada/{'A' * 64}.0"),
            ("short_checksum", f"armada.deployment=armada/{'a' * 63}.0"),
            ("leading_zero_serial", f"armada.deployment=armada/{COMMIT_CSUM}.01"),
            ("negative_serial", f"armada.deployment=armada/{COMMIT_CSUM}.-1"),
            ("uint32_overflow_serial", f"armada.deployment=armada/{COMMIT_CSUM}.4294967296"),
            (" eleven_digit_serial", f"armada.deployment=armada/{COMMIT_CSUM}.10000000000"),
            ("pathological_4300_digit_serial", f"armada.deployment=armada/{COMMIT_CSUM}.{huge_digits}"),
        ]
        for name, dep in cases:
            with self.subTest(case=name):
                cmdline = f"{dep} {ost}"
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, cmdline, make_booted())

    def test_malformed_ostree_binding_rejected(self):
        witness = make_witness()
        dep = f"armada.deployment=armada/{COMMIT_CSUM}.0"
        huge_digits = "9" * 5000
        cases = [
            ("empty_value", "ostree="),
            ("relative_path", f"ostree=ostree/boot.0/armada/{BOOT_CSUM}/0"),
            ("bootver_2", f"ostree=/ostree/boot.2/armada/{BOOT_CSUM}/0"),
            ("bootver_missing", f"ostree=/ostree/boot/armada/{BOOT_CSUM}/0"),
            ("osname_mismatch", f"ostree=/ostree/boot.0/other/{BOOT_CSUM}/0"),
            ("extra_slash", f"ostree=/ostree/boot.0/armada/sub/{BOOT_CSUM}/0"),
            ("uppercase_csum", f"ostree=/ostree/boot.0/armada/{'B' * 64}/0"),
            ("short_csum", f"ostree=/ostree/boot.0/armada/{'b' * 63}/0"),
            ("leading_zero_index", f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/01"),
            ("negative_index", f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/-1"),
            ("uint32_overflow_index", f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/4294967296"),
            ("pathological_4300_digit_index", f"ostree=/ostree/boot.0/armada/{BOOT_CSUM}/{huge_digits}"),
        ]
        for name, ost in cases:
            with self.subTest(case=name):
                cmdline = f"{dep} {ost}"
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, cmdline, make_booted())

    def test_booted_record_mismatches_and_invalid_types_rejected(self):
        witness = make_witness()
        cmdline = make_cmdline(osname="armada", commit=COMMIT_CSUM, serial=1)
        cases = [
            ("none_booted", None),
            ("list_booted", []),
            ("incompatible_true", make_booted(serial=1, incompatible=True)),
            ("stateroot_mismatch", make_booted(stateroot="other", serial=1)),
            ("stateroot_none", make_booted(stateroot=None, serial=1)),
            ("checksum_mismatch", make_booted(checksum=OTHER_CSUM, serial=1)),
            ("serial_mismatch", make_booted(serial=2)),
            ("serial_bool_true", make_booted(serial=True)),
            ("serial_bool_false", make_booted(serial=False)),
            ("serial_negative", make_booted(serial=-1)),
            ("serial_overflow", make_booted(serial=0x100000000)),
            ("serial_str", make_booted(serial="1")),
        ]
        for name, booted in cases:
            with self.subTest(case=name):
                with self.assertRaises(BootContextError):
                    decode_boot_context(witness, cmdline, booted)

    def test_read_boot_context_with_tempfiles_and_error_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            witness_path = tmp / "witness"
            cmdline_path = tmp / "cmdline"
            witness_path.write_bytes(make_witness(slot=1))
            cmdline_path.write_text(
                make_cmdline(serial=5, bootver=0) + "\n", encoding="utf-8"
            )
            booted = make_booted(serial=5)

            result = read_boot_context(booted, witness_path, cmdline_path)
            self.assertEqual(
                result,
                {
                    "state": "available",
                    "actualSlot": "b",
                    "inactiveSlot": "a",
                    "deployment": f"armada/{COMMIT_CSUM}.5",
                },
            )

            # Missing file
            with self.assertRaises(BootContextError):
                read_boot_context(booted, tmp / "missing", cmdline_path)

            # Directory instead of regular file
            dir_path = tmp / "subdir"
            dir_path.mkdir()
            with self.assertRaises(BootContextError):
                read_boot_context(booted, dir_path, cmdline_path)

            # Symlink rejected via O_NOFOLLOW
            symlink_path = tmp / "symlink"
            symlink_path.symlink_to(witness_path)
            with self.assertRaises(BootContextError):
                read_boot_context(booted, symlink_path, cmdline_path)

            # FIFO rejected as non-regular without blocking
            if hasattr(os, "mkfifo"):
                fifo_path = tmp / "fifo"
                os.mkfifo(fifo_path)
                with self.assertRaises(BootContextError):
                    read_boot_context(booted, fifo_path, cmdline_path)

            # Oversize cmdline file
            oversize_path = tmp / "oversize"
            oversize_path.write_bytes(b"a" * (CMDLINE_MAX + 1))
            with self.assertRaises(BootContextError):
                read_boot_context(booted, witness_path, oversize_path)

            # Invalid UTF-8 in cmdline file
            bad_utf8_path = tmp / "bad_utf8"
            bad_utf8_path.write_bytes(b"\xff\xfe" + make_cmdline(serial=5).encode("utf-8"))
            with self.assertRaises(BootContextError):
                read_boot_context(booted, witness_path, bad_utf8_path)

            # NUL byte in cmdline file
            nul_path = tmp / "nul_cmdline"
            nul_path.write_bytes(make_cmdline(serial=5).encode("utf-8") + b"\x00")
            with self.assertRaises(BootContextError):
                read_boot_context(booted, witness_path, nul_path)

    def test_read_regular_accumulates_short_reads_and_closes_fd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            witness_path = tmp / "witness"
            cmdline_path = tmp / "cmdline"
            witness_path.write_bytes(make_witness(slot=0))
            cmdline_text = make_cmdline(serial=3, bootver=1)
            cmdline_path.write_text(cmdline_text, encoding="utf-8")

            real_open = os.open
            real_read = os.read
            real_close = os.close
            opened_fds = []
            closed_fds = []
            read_requests = []

            def wrapped_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                opened_fds.append(fd)
                return fd

            def wrapped_read(fd, n):
                read_requests.append(n)
                return real_read(fd, min(n, 5))

            def wrapped_close(fd):
                closed_fds.append(fd)
                return real_close(fd)

            with patch("updates.boot_context.os.open", side_effect=wrapped_open), \
                 patch("updates.boot_context.os.read", side_effect=wrapped_read), \
                 patch("updates.boot_context.os.close", side_effect=wrapped_close):
                ctx = read_boot_context(make_booted(serial=3), witness_path, cmdline_path)

            self.assertEqual(ctx["actualSlot"], "a")
            self.assertEqual(ctx["deployment"], f"armada/{COMMIT_CSUM}.3")
            self.assertEqual(opened_fds, closed_fds)
            self.assertGreater(len(read_requests), 4)

    def test_read_regular_enforces_limit_when_stat_size_is_zero_like_proc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            proc_like = tmp / "proc_cmdline"
            proc_like.write_bytes(b"x" * 40)

            real_fstat = os.fstat

            def fake_zero_size_fstat(fd):
                st = real_fstat(fd)
                return os.stat_result((
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    0,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                ))

            with patch("updates.boot_context.os.fstat", side_effect=fake_zero_size_fstat):
                with self.assertRaises(BootContextError):
                    boot_context._read_regular(proc_like, 32)

    def test_system_manager_status_and_check_with_available_and_unavailable_boot_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            compat_path = tmp / "compatible"
            compat_path.write_bytes(b"lenovo,elden\x00")
            config_path = tmp / "channels.json"
            config_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "channels": {
                        "stable": {
                            "url": "https://example.invalid/stable.json",
                            "signatureUrl": "https://example.invalid/stable.sig",
                        }
                    },
                }),
                encoding="utf-8",
            )
            witness_path = tmp / "witness"
            cmdline_path = tmp / "cmdline"
            witness_path.write_bytes(make_witness(slot=0))
            cmdline_path.write_text(make_cmdline(serial=0, bootver=1), encoding="utf-8")

            snapshot = {
                "channel": "stable",
                "repository": armada_os.REPOSITORIES[0],
                "booted": make_booted(serial=0, version="2025.01"),
                "staged": None,
                "rollback_queued": False,
            }
            target_commit = "d" * 64
            catalog_update = {
                "channel": {
                    "version": "2025.02",
                    "ostreeCommit": target_commit,
                    "releaseId": "rel-2025-02",
                    "ociDigest": "sha256:" + ("e" * 64),
                }
            }

            manager = SystemManager(
                config_path=config_path,
                compatible_path=compat_path,
                witness_path=witness_path,
                cmdline_path=cmdline_path,
            )
            with patch.object(armada_os, "snapshot", return_value=snapshot), \
                 patch("updates.system_manager.fetch_update", return_value=catalog_update):
                status_ok = manager.status()
                check_ok = manager.check()

            self.assertEqual(status_ok["state"], "installed")
            self.assertEqual(
                status_ok["bootEvidence"],
                {
                    "state": "available",
                    "actualSlot": "a",
                    "inactiveSlot": "b",
                    "deployment": f"armada/{COMMIT_CSUM}.0",
                },
            )
            self.assertEqual(check_ok["state"], "update_available")
            self.assertEqual(check_ok["bootEvidence"], status_ok["bootEvidence"])

            # Missing witness makes bootEvidence unavailable but does NOT fail status or check
            manager_missing = SystemManager(
                config_path=config_path,
                compatible_path=compat_path,
                witness_path=tmp / "missing_witness",
                cmdline_path=cmdline_path,
            )
            with patch.object(armada_os, "snapshot", return_value=snapshot), \
                 patch("updates.system_manager.fetch_update", return_value=catalog_update):
                status_unavail = manager_missing.status()
                check_unavail = manager_missing.check()

            self.assertEqual(status_unavail["state"], "installed")
            self.assertEqual(status_unavail["bootEvidence"]["state"], "unavailable")
            self.assertIsNone(status_unavail["bootEvidence"]["actualSlot"])
            self.assertIsNone(status_unavail["bootEvidence"]["inactiveSlot"])
            self.assertIsNone(status_unavail["bootEvidence"]["deployment"])
            self.assertTrue(status_unavail["bootEvidence"]["error"])
            self.assertEqual(check_unavail["state"], "update_available")
            self.assertEqual(check_unavail["bootEvidence"]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
