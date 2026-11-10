import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("update_boot_dtb_errors", ROOT / "system_files/usr/libexec/armada/kshim-build-boot.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BootDtbErrors(unittest.TestCase):
    def test_bad_transaction_ids_are_reported_with_dtb_stage(self):
        for value in (42, "bad", "00000000-0000-0000-0000-000000000000", "ABCDEF12-1234-5678-9ABC-DEF012345678"):
            with self.subTest(value=value):
                with self.assertRaises(builder.StageError) as error:
                    builder.build_dtb({}, "", transaction_uuid=value)
                self.assertEqual(error.exception.stage, "dtb")
                self.assertIn("transaction UUID", error.exception.message)
