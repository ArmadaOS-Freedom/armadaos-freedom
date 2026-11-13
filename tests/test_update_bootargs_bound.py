# SPDX-License-Identifier: GPL-3.0-only
"""Boundary tests for assembled bootargs against the boot witness limit.

devices/lenovo-tb323fu/boot/kshim-success-source/src/dt/boot_witness.c caps
/chosen bootargs at BOOT_WITNESS_MAX_BOOTARGS = 65536 bytes including the
terminating NUL.  boot_materials must refuse both assembled command lines at
that boundary before any DT helper or candidate is generated.
"""

import hashlib
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import boot_materials, boot_recipe  # noqa: E402

WITNESS_LIMIT = 65536
VERBOSE_TAIL = " ".join(boot_recipe.DIAGNOSTIC_VERBOSE_TAIL)
RD_BREAK_TAIL = "rd.break=pre-mount"
ORIGIN = "armada"
CURRENT_BINDING = "%s/%s.1" % (ORIGIN, "1" * 64)
TARGET_BINDING = "%s/%s.2" % (ORIGIN, "2" * 64)
ROOT_UUID = "11111111-2222-4333-8444-555555555555"
BOOT_UUID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
ARTIFACTS = (
    ("kernel", "kernel.img"),
    ("initramfs", "initramfs.img"),
    ("runtime", "runtime.bin"),
    ("dtb", "source.dtb"),
)


class BootargsBoundUnitTest(unittest.TestCase):
    def test_exact_limit_including_nul_is_accepted_for_both_lines(self):
        boot_materials._check_bootargs_bound(
            "n" * (WITNESS_LIMIT - 1), "d" * (WITNESS_LIMIT - 1))

    def test_one_byte_over_is_refused_for_each_line(self):
        for label, normal, diagnostic in (
            ("normal bootargs", "n" * WITNESS_LIMIT, "d"),
            ("diagnostic bootargs", "n", "d" * WITNESS_LIMIT),
        ):
            with self.subTest(label=label):
                with self.assertRaises(boot_materials.BootMaterialsError) as raised:
                    boot_materials._check_bootargs_bound(normal, diagnostic)
                message = str(raised.exception)
                self.assertIn(label, message)
                self.assertIn(str(WITNESS_LIMIT + 1), message)
                self.assertIn(str(WITNESS_LIMIT), message)

    def test_non_str_and_non_ascii_values_are_refused(self):
        for value, expected in (
            (123, "must be str, got int"),
            (b"quiet", "must be str, got bytes"),
            ("caf\u00e9", "non-ASCII"),
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(boot_materials.BootMaterialsError) as raised:
                    boot_materials._check_bootargs_bound(value, "console=ttyS0")
                self.assertIn("normal bootargs", str(raised.exception))
                self.assertIn(expected, str(raised.exception))


class BootargsBoundMaterialsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.sysroot = self.base / "sysroot"
        self.sysroot.mkdir(mode=0o700)
        self.target = (
            self.sysroot / "ostree" / "deploy" / ORIGIN / "deploy"
            / (TARGET_BINDING.split("/", 1)[1]))
        self.target.mkdir(parents=True, mode=0o700)
        self.out = self.base / "materials"
        self.load_helper = None
        self.helper_path = self.base / "kshim-build-boot.py"
        self.helper_path.write_text("# test double\n")

        self.records = {}
        self.sources = {}
        self.target_bytes = {}
        for key, name in ARTIFACTS:
            relative = "usr/lib/armada/%s" % name
            data = ("source-" + key).encode("ascii")
            path = self.target / name
            path.write_bytes(data)
            self.sources[relative] = path
            self.target_bytes[name] = data
            self.records[key] = {
                "path": relative,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

    def _prepare(self, *, mode, options):
        profile = {
            "dtb": "lenovo-tb323fu.dtb",
            "extraArgs": [],
            "diagnosticMode": mode,
            "required": ["lenovo,tb323fu"],
            "forbidden": ["lenovo,elden"],
        }
        helper = types.SimpleNamespace(
            StageError=type("StageError", (Exception,), {}),
            require_tool=lambda name: None)

        def fake_call(_helper, _out, _args, _stage, tag, transaction_uuid=None):
            return ("dtb-image-" + tag).encode("ascii")

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                boot_materials, "read_deployment_options",
                side_effect=lambda **kw: {
                    "deployment": TARGET_BINDING,
                    "path": self.target,
                    "options": options,
                    "rootUuid": ROOT_UUID,
                    "bootUuid": BOOT_UUID,
                }))
            stack.enter_context(mock.patch.object(
                boot_materials, "_read_regular", return_value=b"{}"))
            stack.enter_context(mock.patch.object(
                boot_materials, "verify_manifest",
                side_effect=lambda *a, **kw: dict(
                    {"kernelRelease": "6.12.0-armada", "profile": dict(profile)},
                    **{key: dict(value) for key, value in self.records.items()})))
            stack.enter_context(mock.patch.object(
                boot_materials, "_resolve_root",
                return_value=(self.target, self.target)))
            stack.enter_context(mock.patch.object(
                boot_materials, "_resolve_regular_file",
                side_effect=lambda root, usr, relative: (self.sources[relative], None)))
            load_helper = stack.enter_context(mock.patch.object(
                boot_materials, "_load_helper", return_value=helper))
            self.load_helper = load_helper
            stack.enter_context(mock.patch.object(
                boot_materials, "_call_helper", side_effect=fake_call))
            result = boot_materials.prepare_boot_materials(
                sysroot_path=self.sysroot,
                current_binding=CURRENT_BINDING,
                target_binding=TARGET_BINDING,
                model="TB323FU",
                output_dir=self.out,
                dtb_helper_path=self.helper_path,
            )
        return result

    def _assert_no_dt_work(self):
        self.load_helper.assert_not_called()
        self.assertFalse(self.out.joinpath("dtb-normal.img").exists())
        self.assertFalse(self.out.joinpath("dtb-diagnostic.img").exists())
        self.assertFalse(self.out.joinpath("materials.json").exists())
        for name, data in self.target_bytes.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)

    def _assert_staged_only(self, report):
        for entry in report["artifacts"].values():
            self.assertTrue(Path(entry["path"]).is_relative_to(self.out))
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            sorted(["kernel.img", "initramfs.img", "runtime.bin", "source.dtb",
                    "dtb-work", "dtb-logs", "dtb-tmp",
                    "dtb-normal.img", "dtb-diagnostic.img", "materials.json"]))
        for name, data in self.target_bytes.items():
            self.assertEqual((self.target / name).read_bytes(), data, name)

    def test_verbose_diagnostic_exactly_at_limit_is_staged(self):
        normal_chars = WITNESS_LIMIT - 2 - len(VERBOSE_TAIL)
        report = self._prepare(mode="verbose", options="x" * normal_chars)

        self.assertEqual(len(report["normalBootArgs"]), normal_chars)
        self.assertEqual(len(report["diagnosticBootArgs"]), WITNESS_LIMIT - 1)
        self.assertEqual(report["diagnosticBootArgs"],
                         report["normalBootArgs"] + " " + VERBOSE_TAIL)
        self.load_helper.assert_called_once()
        self._assert_staged_only(report)

    def test_verbose_diagnostic_one_byte_over_is_refused_before_dt_work(self):
        normal_chars = WITNESS_LIMIT - 1 - len(VERBOSE_TAIL)
        with self.assertRaises(boot_materials.BootMaterialsError) as raised:
            self._prepare(mode="verbose", options="x" * normal_chars)

        message = str(raised.exception)
        self.assertIn("diagnostic bootargs", message)
        self.assertIn(str(WITNESS_LIMIT + 1), message)
        self.assertIn(str(WITNESS_LIMIT), message)
        self._assert_no_dt_work()

    def test_rd_break_normal_exactly_at_limit_blames_the_longer_diagnostic(self):
        with self.assertRaises(boot_materials.BootMaterialsError) as raised:
            self._prepare(mode="rd-break", options="x" * (WITNESS_LIMIT - 1))

        message = str(raised.exception)
        self.assertIn("diagnostic bootargs", message)
        self.assertNotIn("normal bootargs", message)
        self.assertIn(str(WITNESS_LIMIT + 1 + len(RD_BREAK_TAIL)), message)
        self._assert_no_dt_work()

    def test_rd_break_diagnostic_exactly_at_limit_is_staged(self):
        normal_chars = WITNESS_LIMIT - 2 - len(RD_BREAK_TAIL)
        report = self._prepare(mode="rd-break", options="x" * normal_chars)

        self.assertEqual(len(report["normalBootArgs"]), normal_chars)
        self.assertEqual(len(report["diagnosticBootArgs"]), WITNESS_LIMIT - 1)
        self.assertEqual(report["diagnosticBootArgs"],
                         report["normalBootArgs"] + " " + RD_BREAK_TAIL)
        self.load_helper.assert_called_once()
        self._assert_staged_only(report)

    def test_rd_break_diagnostic_one_byte_over_is_refused(self):
        normal_chars = WITNESS_LIMIT - 1 - len(RD_BREAK_TAIL)
        with self.assertRaises(boot_materials.BootMaterialsError) as raised:
            self._prepare(mode="rd-break", options="x" * normal_chars)
        self.assertIn("diagnostic bootargs", str(raised.exception))
        self.assertIn(str(WITNESS_LIMIT + 1), str(raised.exception))
        self._assert_no_dt_work()

    def test_rd_break_normal_one_byte_over_is_refused(self):
        with self.assertRaises(boot_materials.BootMaterialsError) as raised:
            self._prepare(mode="rd-break", options="x" * WITNESS_LIMIT)
        self.assertIn("normal bootargs", str(raised.exception))
        self.assertIn(str(WITNESS_LIMIT + 1), str(raised.exception))
        self._assert_no_dt_work()


if __name__ == "__main__":
    unittest.main()
