# SPDX-License-Identifier: MIT

import hashlib
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


HELPER = (
    Path(__file__).resolve().parents[1]
    / "system_files/usr/libexec/armada/tb323fu-btaddr"
)
ROM_ADDRESS = "00:00:00:00:5a:ad"
MANUAL_ADDRESS = "12:34:56:78:9a:bc"
MACHINE_ID = "0123456789abcdef" * 2

BTMGMT = """#!/bin/sh
[ "$1" = --index ] && [ "$2" = 0 ] || exit 3
printf '%s\\n' "$3" >> "$BT_CALLS"
case "$3" in
    info) printf '\\taddr %s version 13 manufacturer 29 class 0x000000\\n' "$(cat "$BT_RADIO")" ;;
    power) [ "$4" = off ] || exit 3; printf 'Set Powered complete\\n' ;;
    public-addr)
        [ "${BT_REJECT:-0}" = 0 ] || exit 1
        printf '%s\\n' "$4" > "$BT_RADIO"
        printf 'Set Public Address complete\\n'
        ;;
    *) exit 3 ;;
esac
"""


def expected_address(machine_id):
    octets = bytearray(hashlib.sha256(f"tb323fu-bt:{machine_id}".encode()).digest()[:6])
    octets[0] = (octets[0] | 2) & 0xFE
    return octets.hex(":")


class BluetoothAddressTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.override = root / "etc" / "bt-address"
        self.saved = root / "var" / "bt-address"
        self.machine_id = root / "machine-id"
        self.compat = root / "compatible"
        self.radio = root / "radio"
        self.calls = root / "calls"
        self.script = root / "tb323fu-btaddr"

        self.override.parent.mkdir()
        self.compat.write_bytes(b"lenovo,baldur\0qcom,kaanapali\0")
        self.machine_id.write_text(MACHINE_ID + "\n")
        self.radio.write_text(ROM_ADDRESS + "\n")
        self.calls.touch()

        source = HELPER.read_text()
        for name, original, fixture in (
            ("ADDR_FILE", "/etc/tb323fu/bt-address", self.override),
            ("STATE_FILE", "/var/lib/armada/bluetooth/bt-address", self.saved),
            ("MACHINE_ID_FILE", "/etc/machine-id", self.machine_id),
            ("COMPAT", "/sys/firmware/devicetree/base/compatible", self.compat),
        ):
            source = source.replace(
                f"\n{name}={original}\n", f"\n{name}={shlex.quote(str(fixture))}\n"
            )
        self.script.write_text(source)

        for name, contents in (("btmgmt", BTMGMT), ("sleep", "#!/bin/sh\nexit 0\n")):
            stub = root / name
            stub.write_text(contents)
            stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{root}:{os.environ['PATH']}",
            "BT_RADIO": str(self.radio),
            "BT_CALLS": str(self.calls),
        }

    def run_helper(self, **env):
        return subprocess.run(
            ["sh", str(self.script)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=5,
        )

    def assert_success(self, **env):
        result = self.run_helper(**env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_boot_provisions_a_distinct_address_per_install(self):
        for machine_id in (MACHINE_ID, "fedcba9876543210" * 2):
            with self.subTest(machine_id=machine_id):
                self.machine_id.write_text(machine_id + "\n")
                self.assert_success()
                expected = expected_address(machine_id) + "\n"
                self.assertEqual(self.radio.read_text(), expected)
                self.assertEqual(self.saved.read_text(), expected)
                self.assertEqual(stat.S_IMODE(self.saved.stat().st_mode), 0o600)
                self.assertFalse(self.override.exists())
                self.saved.unlink()

    def test_saved_address_survives_new_deployment_without_override(self):
        self.saved.parent.mkdir()
        self.saved.write_text(MANUAL_ADDRESS + "\n")
        self.machine_id.unlink()
        os.utime(self.saved, (1, 1))
        self.assert_success()
        self.assertEqual(self.radio.read_text(), MANUAL_ADDRESS + "\n")
        self.assertEqual(self.saved.stat().st_mtime, 1)

    def test_explicit_address_migrates_when_radio_already_matches(self):
        override = b"  12:34:56:78:9A:BC\r\n"
        self.override.write_bytes(override)
        self.radio.write_text(MANUAL_ADDRESS + "\n")
        self.assert_success()
        self.assertTrue(self.saved.is_file())
        self.assertEqual(self.saved.read_text(), MANUAL_ADDRESS + "\n")
        self.assertEqual(self.override.read_bytes(), override)
        self.assertEqual(self.calls.read_text().splitlines(), ["info"])

    def test_rejected_override_does_not_replace_saved_address(self):
        self.saved.parent.mkdir()
        self.saved.write_text("02:12:34:56:78:90\n")
        self.override.write_text(MANUAL_ADDRESS + "\n")
        self.assertNotEqual(self.run_helper(BT_REJECT="1").returncode, 0)
        self.assertEqual(self.saved.read_text(), "02:12:34:56:78:90\n")

    def test_invalid_explicit_address_does_not_replace_saved_address(self):
        self.saved.parent.mkdir()
        self.saved.write_text(MANUAL_ADDRESS + "\n")
        for address in ("invalid", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            with self.subTest(address=address):
                self.override.write_text(address + "\n")
                self.assertNotEqual(self.run_helper().returncode, 0)
                self.assertEqual(self.saved.read_text(), MANUAL_ADDRESS + "\n")
        self.assertEqual(self.calls.read_text(), "")

    def test_invalid_machine_id_does_not_program_controller(self):
        for value in ("", "0" * 32, "a" * 16 + "\n" + "b" * 16):
            with self.subTest(value=value):
                self.machine_id.write_text(value + "\n")
                self.assertNotEqual(self.run_helper().returncode, 0)
                self.assertFalse(self.saved.exists())
        self.assertEqual(self.calls.read_text(), "")

    def test_other_models_are_untouched(self):
        self.compat.write_bytes(b"lenovo,elden\0lenovo,baldur-test\0")
        self.assert_success()
        self.assertFalse(self.saved.parent.exists())
        self.assertEqual(self.calls.read_text(), "")
