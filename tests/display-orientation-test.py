#!/usr/bin/env python3
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REAL_RUN = subprocess.run
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
loader = importlib.machinery.SourceFileLoader(
    "orientation_control", str(ROOT / "system_files/usr/libexec/armada/armada-control"),
)
spec = importlib.util.spec_from_loader(loader.name, loader)
control = importlib.util.module_from_spec(spec)
loader.exec_module(control)
DEFAULT_CONFIG = control.DISPLAY_ORIENTATION_CONFIG
HELPER = ROOT / "system_files/usr/lib/armada/display-orientation.sh"
ORIENTATIONS = ("normal", "left", "right", "upsidedown")
PRESERVED_ENV = (
    "TOUCH_MODE",
    "FORCE_COMPOSITION_ROTATION",
    "DISABLE_TOUCH_GESTURES",
    "GAMESCOPE_TOUCH_GESTURES_IN_GAME",
    "ARMADA_PANEL_ORIENTATION",
    "HOME_POWER_KEYS",
)
HELPER_SCRIPT = (
    '. "$1"\n'
    'printf "ORIENTATION=%s\\n" "${ORIENTATION-<unset>}"\n'
    'printf "TOUCH_MODE=%s\\n" "${TOUCH_MODE-<unset>}"\n'
    'printf "FORCE_COMPOSITION_ROTATION=%s\\n" "${FORCE_COMPOSITION_ROTATION-<unset>}"\n'
    'printf "DISABLE_TOUCH_GESTURES=%s\\n" "${DISABLE_TOUCH_GESTURES-<unset>}"\n'
    'printf "GAMESCOPE_TOUCH_GESTURES_IN_GAME=%s\\n" "${GAMESCOPE_TOUCH_GESTURES_IN_GAME-<unset>}"\n'
    'printf "ARMADA_PANEL_ORIENTATION=%s\\n" "${ARMADA_PANEL_ORIENTATION-<unset>}"\n'
    'printf "HOME_POWER_KEYS=%s\\n" "${HOME_POWER_KEYS-<unset>}"\n'
)


class DisplayOrientationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "display-orientation.conf"
        for target, replacement in (
            ("DISPLAY_ORIENTATION_CONFIG", self.config),
            ("device_env", lambda: {}),
        ):
            mock = patch.object(control, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)

    def helper_environment(self, config, orientation, unset_orientation):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "ARMADA_DISPLAY_ORIENTATION_CONFIG": str(config),
            "TOUCH_MODE": "4",
            "FORCE_COMPOSITION_ROTATION": "1",
            "DISABLE_TOUCH_GESTURES": "1",
            "GAMESCOPE_TOUCH_GESTURES_IN_GAME": "0",
            "ARMADA_PANEL_ORIENTATION": "right",
            "HOME_POWER_KEYS": "preserved",
        }
        if not unset_orientation:
            environment["ORIENTATION"] = orientation
        return environment

    def run_helper(self, config=None, orientation="", unset_orientation=False):
        config = self.config if config is None else config
        result = REAL_RUN(
            ["bash", "-eu", "-c", HELPER_SCRIPT, "bash", str(HELPER)],
            text=True,
            capture_output=True,
            env=self.helper_environment(config, orientation, unset_orientation),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                state[key] = value
        return state

    def test_config_path_is_the_dedicated_file(self):
        self.assertEqual(str(DEFAULT_CONFIG), "/etc/armada/display-orientation.conf")
        self.assertIn("get_display_orientation", control.ACTIONS)
        self.assertIn("set_display_orientation", control.ACTIONS)

    def test_missing_config_reports_disabled_state(self):
        self.assertEqual(
            control.action_get_display_orientation({}),
            {"supported": True, "orientation": "", "defaultOrientation": "normal",
             "autoSupported": False},
        )

    def test_default_orientation_follows_valid_device_profile(self):
        for panel in ORIENTATIONS:
            with patch.object(control, "device_env", return_value={"ARMADA_PANEL_ORIENTATION": panel}):
                self.assertEqual(
                    control.action_get_display_orientation({})["defaultOrientation"], panel
                )
        for panel in ("", "landscape", "LEFT", "normal extra"):
            with patch.object(control, "device_env", return_value={"ARMADA_PANEL_ORIENTATION": panel}):
                self.assertEqual(
                    control.action_get_display_orientation({})["defaultOrientation"], "normal"
                )

    def test_y700_defaults_report_landscape_right(self):
        # Mocked device_env only: this exercises the profile values, it does not
        # verify the physical defaults of either model.
        gen4 = {"ARMADA_DEVICE_ID": "lenovo-tb322fc", "ARMADA_PANEL_ORIENTATION": "right"}
        gen5 = {"ARMADA_DEVICE_ID": "lenovo-tb323fu", "ARMADA_PANEL_ORIENTATION": "right"}
        with patch.object(control, "device_env", return_value=gen4):
            self.assertEqual(
                control.action_get_display_orientation({})["defaultOrientation"], "right"
            )
        with patch.object(control, "device_env", return_value=gen5):
            self.assertEqual(
                control.action_get_display_orientation({})["defaultOrientation"], "right"
            )
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "")

    def test_device_env_failure_still_returns_a_valid_default(self):
        for error in (
            OSError("device-env missing"),
            subprocess.CalledProcessError(1, ["device-env"]),
            subprocess.TimeoutExpired("device-env", 5),
        ):
            with patch.object(control, "device_env", side_effect=error):
                self.assertEqual(
                    control.action_get_display_orientation({}),
                    {"supported": True, "orientation": "", "defaultOrientation": "normal",
             "autoSupported": False},
                )

    def test_set_roundtrips_every_orientation(self):
        for orientation in ORIENTATIONS + ("",):
            expected = {
                "supported": True,
                "orientation": orientation,
                "defaultOrientation": "normal",
                "autoSupported": False,
            }
            self.assertEqual(
                control.action_set_display_orientation({"orientation": orientation}), expected
            )
            self.assertEqual(
                self.config.read_text(encoding="utf-8"), f"orientation={orientation}\n"
            )
            self.assertEqual(control.action_get_display_orientation({}), expected)

    def test_auto_is_offered_only_where_the_profile_enables_it(self):
        enabled = {"ARMADA_GAMEMODE_AUTOROTATE": "1", "ARMADA_PANEL_ORIENTATION": "normal"}
        with patch.object(control, "device_env", return_value=enabled):
            state = control.action_set_display_orientation({"orientation": "auto"})
            self.assertEqual(state["orientation"], "auto")
            self.assertTrue(state["autoSupported"])
            self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=auto\n")
            self.assertEqual(control.action_get_display_orientation({})["orientation"], "auto")
        self.config.write_text("orientation=left\n", encoding="utf-8")
        for env in ({}, {"ARMADA_GAMEMODE_AUTOROTATE": "0"}):
            with patch.object(control, "device_env", return_value=env):
                self.assertFalse(control.action_get_display_orientation({})["autoSupported"])
                with self.assertRaises(ValueError):
                    control.action_set_display_orientation({"orientation": "auto"})
        with patch.object(control, "device_env", side_effect=OSError("device-env missing")):
            with self.assertRaises(ValueError):
                control.action_set_display_orientation({"orientation": "auto"})
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=left\n")

    def test_helper_starts_auto_at_the_inherited_orientation(self):
        # "auto" is no fixed override: gamescope starts at the device default and
        # armada-gamemode-autorotate follows the sensor from there.
        self.config.write_text("orientation=auto\n", encoding="utf-8")
        self.assertEqual(self.run_helper(orientation="right")["ORIENTATION"], "right")
        self.assertEqual(self.run_helper(unset_orientation=True)["ORIENTATION"], "<unset>")
        self.config.write_text("orientation=auto\norientation=left\n", encoding="utf-8")
        self.assertEqual(self.run_helper(orientation="right")["ORIENTATION"], "left")

    def test_empty_and_malformed_values_read_as_disabled(self):
        for text in (
            "orientation=\n",
            "orientation=   \n",
            "orientation=sideways\n",
            "orientation=left right\n",
            "enabled=1\n",
            "",
        ):
            self.config.write_text(text, encoding="utf-8")
            self.assertEqual(control.action_get_display_orientation({})["orientation"], "")

    def test_whitespace_and_later_assignment_wins(self):
        self.config.write_text(" orientation = left \nother=1\norientation=right\n")
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "right")

    def test_malformed_assignment_clears_an_earlier_value(self):
        self.config.write_text("orientation=left\norientation=$(reboot)\n")
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "")

    def test_invalid_payload_does_not_write(self):
        self.config.write_text("orientation=left\n", encoding="utf-8")
        for payload in (
            {},
            {"orientation": None},
            {"orientation": True},
            {"orientation": 0},
            {"orientation": 1},
            {"orientation": 1.0},
            {"orientation": []},
            {"orientation": ["left"]},
            {"orientation": {}},
            {"orientation": "Left"},
            {"orientation": " normal"},
            {"orientation": "normal "},
            {"orientation": "normal\n"},
        ):
            with self.assertRaises(ValueError):
                control.action_set_display_orientation(payload)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=left\n")

    def test_failed_atomic_write_keeps_the_previous_config(self):
        self.config.write_text("orientation=left\n", encoding="utf-8")
        with patch.object(control, "atomic_write", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                control.action_set_display_orientation({"orientation": "right"})
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=left\n")
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "left")

    def test_clear_through_rpc_restores_the_inherited_direction(self):
        control.action_set_display_orientation({"orientation": "right"})
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "right")
        self.assertEqual(
            control.action_set_display_orientation({"orientation": ""}),
            {"supported": True, "orientation": "", "defaultOrientation": "normal",
             "autoSupported": False},
        )
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=\n")
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "")
        expected = {
            "TOUCH_MODE": "4",
            "FORCE_COMPOSITION_ROTATION": "1",
            "DISABLE_TOUCH_GESTURES": "1",
            "GAMESCOPE_TOUCH_GESTURES_IN_GAME": "0",
            "ARMADA_PANEL_ORIENTATION": "right",
            "HOME_POWER_KEYS": "preserved",
        }
        for inherited in ("left", "right", "normal"):
            state = self.run_helper(orientation=inherited)
            self.assertEqual(state["ORIENTATION"], inherited)
            for name in PRESERVED_ENV:
                self.assertEqual(state[name], expected[name])

    def test_failed_write_on_clear_keeps_the_previous_config(self):
        self.config.write_text("orientation=right\n", encoding="utf-8")
        with patch.object(control, "atomic_write", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                control.action_set_display_orientation({"orientation": ""})
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=right\n")
        self.assertEqual(control.action_get_display_orientation({})["orientation"], "right")

    def test_set_does_not_touch_live_session_state(self):
        with patch.object(
            control.subprocess, "run", side_effect=AssertionError("live command")
        ), patch.object(
            control.subprocess, "check_output", side_effect=AssertionError("live command")
        ):
            control.action_set_display_orientation({"orientation": "right"})
        self.assertEqual(self.config.read_text(encoding="utf-8"), "orientation=right\n")

    def test_config_read_permission_error_is_reported(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                control.action_get_display_orientation({})

    def test_helper_without_config_is_safe_under_eu(self):
        missing = self.root / "missing.conf"
        self.assertEqual(self.run_helper(missing)["ORIENTATION"], "")
        self.assertEqual(
            self.run_helper(missing, unset_orientation=True)["ORIENTATION"], "<unset>"
        )
        self.assertEqual(self.run_helper(missing, orientation="left")["ORIENTATION"], "left")

    def test_helper_keeps_inherited_orientation_when_disabled(self):
        texts = ("", "orientation=\n", "orientation=   \n", "orientation=bogus\n", "other=1\n")
        for inherited in ("left", "right"):
            for text in texts:
                self.config.write_text(text, encoding="utf-8")
                self.assertEqual(self.run_helper(orientation=inherited)["ORIENTATION"], inherited)
                self.assertEqual(
                    self.run_helper(unset_orientation=True)["ORIENTATION"], "<unset>"
                )

    def test_helper_applies_every_valid_orientation(self):
        for orientation in ORIENTATIONS:
            self.config.write_text(f"orientation={orientation}\n", encoding="utf-8")
            self.assertEqual(self.run_helper(orientation="normal")["ORIENTATION"], orientation)

    def test_helper_parsers_agree_with_the_control_service(self):
        for text in (
            "orientation=normal\n",
            " orientation = left \n",
            "orientation=right\norientation=upsidedown\n",
            "orientation=upsidedown\norientation=bogus\n",
            "# orientation=left\n",
            "enabled=1\n",
            "orientation=\n",
        ):
            self.config.write_text(text, encoding="utf-8")
            expected = control.action_get_display_orientation({})["orientation"] or "normal"
            self.assertEqual(self.run_helper(orientation="normal")["ORIENTATION"], expected)

    def test_helper_never_evaluates_config_values(self):
        marker = self.root / "marker"
        for payload in (
            f"orientation=$(touch {marker})",
            f"orientation=`touch {marker}`",
            f"orientation=left; touch {marker}",
            f"orientation=left && touch {marker}",
            f"orientation=${{HOME}}",
        ):
            self.config.write_text(payload + "\n", encoding="utf-8")
            self.assertEqual(control.action_get_display_orientation({})["orientation"], "")
            self.assertEqual(self.run_helper(orientation="normal")["ORIENTATION"], "normal")
            self.assertFalse(marker.exists())

    def test_helper_leaves_unrelated_session_environment_alone(self):
        self.config.write_text("orientation=left\n", encoding="utf-8")
        state = self.run_helper(orientation="right")
        self.assertEqual(state["ORIENTATION"], "left")
        for name in PRESERVED_ENV:
            expected = {
                "TOUCH_MODE": "4",
                "FORCE_COMPOSITION_ROTATION": "1",
                "DISABLE_TOUCH_GESTURES": "1",
                "GAMESCOPE_TOUCH_GESTURES_IN_GAME": "0",
                "ARMADA_PANEL_ORIENTATION": "right",
                "HOME_POWER_KEYS": "preserved",
            }[name]
            self.assertEqual(state[name], expected)

    def test_helper_exports_the_override_for_gamescope(self):
        self.config.write_text("orientation=right\n", encoding="utf-8")
        environment = self.helper_environment(self.config, "", True)
        result = REAL_RUN(
            ["bash", "-eu", "-c", '. "$1"; printenv ORIENTATION', "bash", str(HELPER)],
            text=True,
            capture_output=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "right\n")


if __name__ == "__main__":
    unittest.main()
