#!/usr/bin/env python3
"""Guard the Freedom Touch packaging chain in the image build."""
import re
import runpy
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "packages/inputplumber/inputplumber.spec"
PATCH_DIR = REPO_ROOT / "packages/inputplumber/patches"
PROFILE = REPO_ROOT / "system_files/usr/share/inputplumber/devices/05-freedom-touch-controller.yaml"
UDEV = REPO_ROOT / "system_files/usr/lib/udev/rules.d/71-freedom-touch-source.rules"
BASE = REPO_ROOT / "build_files/10-base-packages.sh"
CTRL = REPO_ROOT / "system_files/usr/libexec/armada/controller-type"
GS = REPO_ROOT / "packages/gamescope/build.sh"

TOUCH_PATCHES = [
    "0004-add-dbus-touch-events.patch",
    "0005-freedom-touch-virtual-source.patch",
    "0006-preserve-digital-edges.patch",
]
SOURCE_NAME = "Freedom Touch Controller Source"
PROFILE_NAME = "Freedom Touch Controller"


def read(path):
    return path.read_text(encoding="utf-8")


class PatchFilesExist(unittest.TestCase):
    def test_numbered_touch_patches_present(self):
        for name in TOUCH_PATCHES:
            with self.subTest(patch=name):
                self.assertTrue((PATCH_DIR / name).is_file(), name)


class TouchEventUsesOrderedWriteEvent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(PATCH_DIR / "0004-add-dbus-touch-events.patch")

    def test_send_touch_event_uses_write_event_not_write_send_event(self):
        # Extract the SendTouchEvent method body from the patch:
        # every line of the added method is PLUS-prefixed.
        lines = self.text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.startswith("+") and "async fn send_touch_event" in line:
                start = i
                break
        self.assertIsNotNone(start, "send_touch_event not found in patch0004")
        body = []
        for line in lines[start + 1:]:
            if line.startswith("+"):
                body.append(line[1:])
            elif line.startswith(" "):
                # context line - stop once we pass the method (next doc comment/method)
                if "Directly write to the composite device" in line:
                    break
            else:
                break
        joined = "\n".join(body)
        self.assertIn(
            "write_event(NativeEvent::new(cap, value))",
            joined,
            "SendTouchEvent must reuse CompositeDeviceClient::write_event for FIFO ordering",
        )
        self.assertNotIn(
            "write_send_event(NativeEvent::new(cap, value))",
            joined,
            "SendTouchEvent must not go through the button-delay write_send_event path",
        )


class SpecWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SPEC)

    def test_patches_4_5_6_declared(self):
        for n in ("4", "5", "6"):
            self.assertRegex(
                self.text,
                rf"(?m)^Patch{n}:\s+000{n}-[A-Za-z0-9_.-]+\.patch\s*$",
                f"Patch{n} line missing",
            )

    def test_patch6_filename(self):
        self.assertIn("Patch6:         0006-preserve-digital-edges.patch", self.text)

    def test_autosetup_p1(self):
        self.assertIn("%autosetup -n %{appname}-%{commit} -p1", self.text)


class ProfileOptIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(PROFILE)

    def test_exact_identity(self):
        self.assertIn(f"name: {PROFILE_NAME}", self.text)
        self.assertIn(f"name: {SOURCE_NAME}", self.text)
        self.assertIn('vendor_id: "0000"', self.text)
        self.assertIn('product_id: "4654"', self.text)
        self.assertIn("handler: event*", self.text)
        self.assertIn("matches: []", self.text)
        self.assertIn("single_source: false", self.text)
        self.assertIn("group: gamepad", self.text)

    def test_target_deck_uhid(self):
        self.assertIn("target_devices:", self.text)
        self.assertRegex(self.text, r"(?m)^\s+-\s+deck-uhid\s*$")

    def test_preserve_digital_edges_opt_in(self):
        self.assertRegex(
            self.text,
            r"(?m)^\s+preserve_digital_edges:\s+true\s*$",
        )

    def test_auto_manage_and_no_persist(self):
        self.assertRegex(self.text, r"(?m)^\s+auto_manage:\s+true\s*$")
        self.assertRegex(self.text, r"(?m)^\s+persist:\s+false\s*$")


class UdevRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(UDEV)

    def test_name_scoped(self):
        self.assertIn(f'ATTRS{{name}}=="{SOURCE_NAME}"', self.text)

    def test_root_only_0600(self):
        self.assertIn('OWNER="root"', self.text)
        self.assertIn('GROUP="root"', self.text)
        self.assertIn('MODE="0600"', self.text)
        self.assertIn('TAG-="uaccess"', self.text)

    def test_scope_is_input_events_and_js(self):
        self.assertIn('SUBSYSTEM=="input"', self.text)
        self.assertIn('KERNEL=="event*|js*"', self.text)


class BasePackages(unittest.TestCase):
    def test_python3_evdev_installed(self):
        text = read(BASE)
        stripped = re.sub(r"\\\n", " ", text)
        found = False
        for line in stripped.splitlines():
            if "python3-evdev" not in line:
                continue
            tokens = line.split()
            for i, tok in enumerate(tokens):
                if tok in ("dnf", "dnf5") and "install" in tokens[i + 1:i + 4]:
                    found = True
                    break
            if found:
                break
        self.assertTrue(found, "python3-evdev not in an actual dnf install command")


class GamescopePatches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(GS)

    def test_auto_consumes_patches(self):
        self.assertIn("/work/patches", self.text)
        self.assertIn("[0-9][0-9][0-9][0-9]-*.patch", self.text)
        self.assertIn("find", self.text)

    def test_freedom_touch_patch_files_present(self):
        gamescope_patch_dir = REPO_ROOT / "packages/gamescope/patches"
        for name in (
            "0044-steamcompmgr-freedom-touch-overlay.patch",
            "0045-steamcompmgr-freedom-touch-placeholder.patch",
        ):
            with self.subTest(patch=name):
                self.assertTrue((gamescope_patch_dir / name).is_file(), name)


class ControllerTypeSkip(unittest.TestCase):
    def setUp(self):
        self.ns = runpy.run_path(str(CTRL), run_name="touch_packaging_test")
        self.fn = self.ns["apply_type"]

    def _run(self, paths, names, extras):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(
            self.fn.__globals__,
            {
                "composite_paths": lambda: list(paths),
                "composite_name": names.get,
                "extra_targets": lambda _: [],
            },
        ), mock.patch.object(self.ns["subprocess"], "run", side_effect=fake_run):
            ok = self.fn("ds5")
        return ok, calls

    def test_skips_exact_virtual_source(self):
        names = {
            "/org/shadowblip/InputPlumber/CompositeDevice0": PROFILE_NAME,
            "/org/shadowblip/InputPlumber/CompositeDevice1": "G9",
        }
        ok, calls = self._run(list(names), names, {})
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertIn("/org/shadowblip/InputPlumber/CompositeDevice1", cmd)
        self.assertNotIn("/org/shadowblip/InputPlumber/CompositeDevice0", cmd)
        self.assertNotIn(PROFILE_NAME, cmd)
        # busctl call: SERVICE path IFACE method sig count ...
        self.assertEqual(cmd[0], "busctl")
        self.assertEqual(cmd[1], "--system")
        self.assertEqual(cmd[2], "call")
        self.assertEqual(cmd[4], "/org/shadowblip/InputPlumber/CompositeDevice1")
        self.assertEqual(cmd[6], "SetTargetDevices")
        self.assertEqual(cmd[7], "as")
        self.assertEqual(cmd[8], "1")
        self.assertEqual(cmd[9], "ds5")

    def test_virtual_only_returns_false_no_calls(self):
        names = {"/org/shadowblip/InputPlumber/CompositeDevice0": PROFILE_NAME}
        ok, calls = self._run(list(names), names, {})
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_skips_only_exact_virtual_name(self):
        names = {
            "/org/shadowblip/InputPlumber/CompositeDevice0": "Freedom Touch Controller Extra",
            "/org/shadowblip/InputPlumber/CompositeDevice1": PROFILE_NAME,
            "/org/shadowblip/InputPlumber/CompositeDevice2": "G9",
        }
        ok, calls = self._run(list(names), names, {})
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        targets = [c[4] for c in calls]
        self.assertIn("/org/shadowblip/InputPlumber/CompositeDevice0", targets)
        self.assertIn("/org/shadowblip/InputPlumber/CompositeDevice2", targets)
        self.assertNotIn("/org/shadowblip/InputPlumber/CompositeDevice1", targets)

    def test_unknown_names_skip_all_returns_false_no_calls(self):
        names = {
            "/org/shadowblip/InputPlumber/CompositeDevice0": "",
            "/org/shadowblip/InputPlumber/CompositeDevice1": None,
        }
        ok, calls = self._run(list(names), names, {})
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_mixed_unknown_plugin_physical_only_physical_targeted(self):
        names = {
            "/org/shadowblip/InputPlumber/CompositeDevice0": "",
            "/org/shadowblip/InputPlumber/CompositeDevice1": PROFILE_NAME,
            "/org/shadowblip/InputPlumber/CompositeDevice2": "G9",
        }
        ok, calls = self._run(list(names), names, {})
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[4], "/org/shadowblip/InputPlumber/CompositeDevice2")
        self.assertEqual(cmd[6], "SetTargetDevices")
        self.assertEqual(cmd[9], "ds5")

    def test_composite_name_timeout_does_not_retarget(self):
        paths = ["/org/shadowblip/InputPlumber/CompositeDevice0"]
        run_calls = []

        def fake_run(cmd, *args, **kwargs):
            if "get-property" in cmd and cmd[-1] == "Name":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 2))
            run_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(
            self.fn.__globals__,
            {
                "composite_paths": lambda: list(paths),
                "extra_targets": lambda _: [],
            },
        ), mock.patch.object(self.ns["subprocess"], "run", side_effect=fake_run):
            ok = self.fn("ds5")
        self.assertFalse(ok)
        self.assertFalse(any("SetTargetDevices" in c for c in run_calls))

    def test_composite_name_failure_does_not_retarget(self):
        paths = ["/org/shadowblip/InputPlumber/CompositeDevice0"]
        run_calls = []

        def fake_run(cmd, *args, **kwargs):
            if "get-property" in cmd and cmd[-1] == "Name":
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="")
            run_calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(
            self.fn.__globals__,
            {
                "composite_paths": lambda: list(paths),
                "extra_targets": lambda _: [],
            },
        ), mock.patch.object(self.ns["subprocess"], "run", side_effect=fake_run):
            ok = self.fn("ds5")
        self.assertFalse(ok)
        self.assertFalse(any("SetTargetDevices" in c for c in run_calls))

    def test_existing_adapter_slot_codes_preserved(self):
        text = read(CTRL)
        for code in ("deck-uhid", "xbox-series", "xb360", "ds5"):
            with self.subTest(code=code):
                self.assertIn(f'"{code}"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
