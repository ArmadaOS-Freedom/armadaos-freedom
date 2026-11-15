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
    "gesture_control", str(ROOT / "system_files/usr/libexec/armada/armada-control"),
)
spec = importlib.util.spec_from_loader(loader.name, loader)
control = importlib.util.module_from_spec(spec)
loader.exec_module(control)
PLUGIN_ROOT = Path(os.environ.get(
    "ARMADA_TEST_PLUGIN_ROOT",
    str(ROOT / "decky/freedom-control/decky/armada-control/py_modules"),
))
sys.path.insert(0, str(PLUGIN_ROOT))
from armada_control import system as plugin_system


class SwipeGesturesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = Path(directory.name) / "swipe-gestures.conf"
        self.config = config
        self.live = True
        self.in_game_live = True
        self.output = None
        self.commands = []
        for target, replacement in (
            ("SWIPE_GESTURES_CONFIG", config),
            ("session_user_command", lambda *args: ["session-user", *args]),
            ("session_systemctl", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)),
        ):
            mock = patch.object(control, target, replacement)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(control.subprocess, "run", self.run_gamescopectl)
        mock.start()
        self.addCleanup(mock.stop)

    def run_gamescopectl(self, command, **kwargs):
        self.commands.append(command)
        self.assertIn("GAMESCOPE_WAYLAND_DISPLAY=gamescope-primary", command)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertTrue(kwargs["check"])
        if command[-1] in ("0", "1"):
            name = command[-2]
            self.assertIn(name, ("enable_touch_gestures", "enable_touch_gestures_in_game"))
            value = command[-1] == "1"
            if name == "enable_touch_gestures":
                self.live = value
            else:
                self.in_game_live = value
            output = ""
        else:
            self.assertEqual(command[-1], "help")
            output = self.output if self.output is not None else (
                "enable_touch_gestures: Enable/Disable the usage of touch gestures\n"
                "enable_touch_gestures_in_game: Enable/Disable touch gestures inside games\n"
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr=output)

    def config_state(self):
        try:
            text = self.config.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        state = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                state[key.strip()] = value.strip()
        return state

    def gamescope_values(self, name):
        return [command[-1] for command in self.commands if command[-1] in ("0", "1") and command[-2] == name]

    def test_default_and_restart_persistence(self):
        self.assertEqual(control.action_get_swipe_gestures_enabled({}), {"enabled": True})
        for enabled in (False, True):
            self.assertEqual(control.action_set_swipe_gestures_enabled({"enabled": enabled}), {"enabled": enabled})
            self.assertEqual(self.live, enabled)
            self.assertEqual(control.action_get_swipe_gestures_enabled({}), {"enabled": enabled})
            self.assertEqual(
                self.config_state(),
                {"enabled": str(int(enabled)), "in_game": "1"},
            )
            self.assertEqual(self.session_value(), "" if enabled else "1")
            self.assertTrue(self.in_game_live)
            self.assertEqual(self.gamescope_values("enable_touch_gestures_in_game"), [])

    def session_environment(self, inherited_disable="", inherited_child=""):
        source = (ROOT / "system_files/usr/share/gamescope-session-plus/sessions.d/steam").read_text()
        block = source.split('_armada_swipe_gestures_config=', 1)[1].split(
            'unset _armada_swipe_gestures_config', 1
        )[0]
        script = (
            '_armada_swipe_gestures_config=' + block
            + '\nprintf "%s|%s" "${DISABLE_TOUCH_GESTURES:-}" "${GAMESCOPE_TOUCH_GESTURES_IN_GAME:-}"'
        )
        result = REAL_RUN(
            ["bash", "-eu", "-c", script], text=True, capture_output=True, check=True,
            env={
                **os.environ,
                "ARMADA_SWIPE_GESTURES_CONFIG": str(self.config),
                "DISABLE_TOUCH_GESTURES": inherited_disable,
                "GAMESCOPE_TOUCH_GESTURES_IN_GAME": inherited_child,
            },
        )
        disable, child = result.stdout.split("|", 1)
        return disable, child

    def session_value(self, inherited=""):
        return self.session_environment(inherited)[0]

    def child_value(self, inherited=""):
        return self.session_environment(inherited_child=inherited)[1]

    def test_missing_config_preserves_session_default(self):
        self.assertEqual(self.session_value(), "")
        self.assertEqual(self.session_value("1"), "1")

    def test_explicit_enable_clears_inherited_disable(self):
        self.config.write_text("enabled=1\n")
        self.assertEqual(self.session_value("1"), "")

    def test_config_parsers_agree(self):
        for text, enabled in (
            ("# comment\nunknown=1\n", True),
            ("enabled=no\n", True),
            (" enabled = 0 \n", False),
            ("enabled=0\nenabled=1", True),
            ("enabled=1\nenabled=0", False),
        ):
            self.config.write_text(text)
            self.assertEqual(control.action_get_swipe_gestures_enabled({})["enabled"], enabled)
            self.assertEqual(self.session_value(), "" if enabled else "1")

    def test_plugin_getter_defaults_on_service_failure(self):
        for error in (RuntimeError("unknown action"), OSError("socket unavailable"), KeyError("enabled")):
            with patch.object(plugin_system, "call", side_effect=error):
                self.assertTrue(plugin_system.swipe_gestures_enabled())
        with patch.object(plugin_system, "call", return_value={"enabled": False}):
            self.assertFalse(plugin_system.swipe_gestures_enabled())

    def test_non_utf8_config_defaults_and_can_be_repaired(self):
        self.config.write_bytes(b"\xff\n")
        self.assertTrue(control.action_get_swipe_gestures_enabled({})["enabled"])
        control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertEqual(self.config_state(), {"enabled": "0", "in_game": "1"})

    def test_unreadable_config_does_not_prevent_replacement(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("unreadable")):
            self.assertTrue(control.action_get_swipe_gestures_enabled({})["enabled"])
            control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertEqual(self.config_state(), {"enabled": "0", "in_game": "1"})

    def test_desktop_only_saves_preference(self):
        with patch.object(control, "session_systemctl", return_value=subprocess.CompletedProcess([], 3)):
            control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertEqual(self.commands, [])
        self.assertFalse(control.action_get_swipe_gestures_enabled({})["enabled"])

    def test_invalid_values_do_not_change_anything(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.assertRaises(ValueError):
                control.action_set_swipe_gestures_enabled({"enabled": value})
        self.assertFalse(self.config.exists())
        self.assertEqual(self.commands, [])

    def test_unknown_convar_does_not_save(self):
        self.output = "Command not found.\n"
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertFalse(self.config.exists())

    def test_set_failure_does_not_save(self):
        def fail_set(command, **kwargs):
            if command[-1] == "help":
                return self.run_gamescopectl(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)

        with patch.object(control.subprocess, "run", side_effect=fail_set):
            with self.assertRaises(subprocess.CalledProcessError):
                control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertFalse(self.config.exists())

    def test_write_failure_restores_live_value(self):
        with patch.object(control, "atomic_write", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                control.action_set_swipe_gestures_enabled({"enabled": False})
        self.assertTrue(self.live)
        self.assertTrue(self.in_game_live)
        self.assertFalse(self.config.exists())

    def test_connection_failure_preserves_saved_preference(self):
        self.config.write_text("enabled=0\n")
        with patch.object(control.subprocess, "run", side_effect=subprocess.TimeoutExpired("gamescopectl", 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                control.action_set_swipe_gestures_enabled({"enabled": True})
        self.assertEqual(self.config.read_text(), "enabled=0\n")

    def test_suspend_with_enabled_preference_restores_live_and_keeps_saved(self):
        self.config.write_text("enabled=1\n")
        saved = self.config.read_bytes()
        self.assertEqual(control.action_suspend_swipe_gestures({"suspended": True}), {"suspended": True})
        self.assertFalse(self.live)
        self.assertEqual(control.action_suspend_swipe_gestures({"suspended": False}), {"suspended": False})
        self.assertTrue(self.live)
        self.assertEqual(self.config.read_bytes(), saved)

    def test_suspend_with_disabled_preference_stays_disabled_without_runtime(self):
        self.config.write_text("enabled=0\n")
        self.live = False
        saved = self.config.read_bytes()
        self.assertEqual(control.action_suspend_swipe_gestures({"suspended": True}), {"suspended": True})
        self.assertEqual(control.action_suspend_swipe_gestures({"suspended": False}), {"suspended": False})
        self.assertEqual(self.commands, [])
        self.assertFalse(self.live)
        self.assertEqual(self.config.read_bytes(), saved)

    def test_invalid_suspend_values_do_not_mutate(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.assertRaises(ValueError):
                control.action_suspend_swipe_gestures({"suspended": value})
        self.assertEqual(self.commands, [])
        self.assertTrue(self.live)
        self.assertFalse(self.config.exists())

    def test_plugin_suspend_passes_bool_and_returns_response(self):
        with patch.object(plugin_system, "call", return_value={"suspended": True}) as mock_call:
            self.assertTrue(plugin_system.suspend_swipe_gestures(True))
            mock_call.assert_called_once_with("suspend_swipe_gestures", suspended=True)
        with patch.object(plugin_system, "call", return_value={"suspended": False}) as mock_call:
            self.assertFalse(plugin_system.suspend_swipe_gestures(False))
            mock_call.assert_called_once_with("suspend_swipe_gestures", suspended=False)

    def test_in_game_default_on_and_restart_persistence(self):
        self.assertEqual(control.action_get_swipe_gestures_in_game_enabled({}), {"enabled": True})
        for enabled in (False, True):
            self.assertEqual(
                control.action_set_swipe_gestures_in_game_enabled({"enabled": enabled}),
                {"enabled": enabled},
            )
            self.assertEqual(self.in_game_live, enabled)
            self.assertTrue(self.live)
            self.assertEqual(control.action_get_swipe_gestures_in_game_enabled({}), {"enabled": enabled})
            self.assertEqual(self.config_state(), {"enabled": "1", "in_game": str(int(enabled))})

    def test_reads_both_states(self):
        for text, master, child in (
            ("enabled=1\nin_game=0\n", True, False),
            ("enabled=0\nin_game=1\n", False, True),
            ("in_game=0\nenabled=1\n", True, False),
            ("enabled=yes\nin_game=yes\n", True, True),
        ):
            self.config.write_text(text)
            self.assertEqual(control.action_get_swipe_gestures_enabled({}), {"enabled": master})
            self.assertEqual(
                control.action_get_swipe_gestures_in_game_enabled({}), {"enabled": child}
            )

    def test_setters_preserve_each_other(self):
        self.config.write_text("enabled=0\nin_game=0\n")
        self.live = False
        self.in_game_live = False
        control.action_set_swipe_gestures_enabled({"enabled": True})
        self.assertEqual(self.live, True)
        self.assertEqual(self.in_game_live, False)
        self.assertEqual(self.config_state(), {"enabled": "1", "in_game": "0"})
        control.action_set_swipe_gestures_in_game_enabled({"enabled": True})
        self.assertEqual(self.live, True)
        self.assertEqual(self.in_game_live, True)
        self.assertEqual(self.config_state(), {"enabled": "1", "in_game": "1"})

    def test_in_game_strict_bool(self):
        for value in (None, 0, 1, "false", [], {}):
            with self.assertRaises(ValueError):
                control.action_set_swipe_gestures_in_game_enabled({"enabled": value})
        self.assertFalse(self.config.exists())
        self.assertEqual(self.commands, [])

    def test_in_game_runtime_uses_exact_convar(self):
        control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertEqual(self.gamescope_values("enable_touch_gestures_in_game"), ["0"])
        self.assertEqual(self.gamescope_values("enable_touch_gestures"), [])
        self.assertEqual(self.in_game_live, False)
        self.assertTrue(self.live)

    def test_in_game_missing_capability_does_not_save(self):
        self.output = "enable_touch_gestures: Enable/Disable the usage of touch gestures\n"
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertFalse(self.config.exists())
        self.assertEqual(self.gamescope_values("enable_touch_gestures_in_game"), [])
        self.assertTrue(self.in_game_live)

    def test_in_game_set_failure_does_not_save(self):
        def fail_set(command, **kwargs):
            if command[-1] == "help":
                return self.run_gamescopectl(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)

        with patch.object(control.subprocess, "run", side_effect=fail_set):
            with self.assertRaises(subprocess.CalledProcessError):
                control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertFalse(self.config.exists())
        self.assertTrue(self.in_game_live)

    def test_in_game_write_failure_restores_live_value(self):
        with patch.object(control, "atomic_write", side_effect=OSError("read-only")):
            with self.assertRaises(OSError):
                control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertTrue(self.in_game_live)
        self.assertFalse(self.config.exists())

    def test_in_game_desktop_only_saves_preference(self):
        with patch.object(control, "session_systemctl", return_value=subprocess.CompletedProcess([], 3)):
            control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertEqual(self.commands, [])
        self.assertTrue(self.in_game_live)
        self.assertEqual(self.config_state(), {"enabled": "1", "in_game": "0"})

    def test_in_game_connection_failure_preserves_saved_preference(self):
        self.config.write_text("enabled=0\nin_game=1\n")
        with patch.object(control.subprocess, "run", side_effect=subprocess.TimeoutExpired("gamescopectl", 5)):
            with self.assertRaises(subprocess.TimeoutExpired):
                control.action_set_swipe_gestures_in_game_enabled({"enabled": False})
        self.assertEqual(self.config.read_text(), "enabled=0\nin_game=1\n")

    def test_suspend_controls_master_only_and_preserves_child(self):
        self.config.write_text("enabled=1\nin_game=0\n")
        saved = self.config.read_bytes()
        child_live = self.in_game_live
        control.action_suspend_swipe_gestures({"suspended": True})
        self.assertFalse(self.live)
        self.assertEqual(self.in_game_live, child_live)
        control.action_suspend_swipe_gestures({"suspended": False})
        self.assertTrue(self.live)
        self.assertEqual(self.in_game_live, child_live)
        self.assertEqual(self.gamescope_values("enable_touch_gestures_in_game"), [])
        self.assertEqual(self.config.read_bytes(), saved)

    def test_session_restores_in_game_child(self):
        self.assertEqual(self.child_value(), "1")
        for text, child, disable in (
            ("in_game=0\n", "0", ""),
            ("in_game=1\n", "1", ""),
            ("in_game=no\n", "1", ""),
            ("enabled=0\nin_game=0\n", "0", "1"),
            ("in_game=0\nenabled=0\n", "0", "1"),
            ("enabled=1\nin_game=1\n", "1", ""),
        ):
            self.config.write_text(text)
            self.assertEqual(self.child_value(), child)
            self.assertEqual(self.session_value(), disable)

    def test_session_child_default_overrides_inherited_value(self):
        self.assertEqual(self.child_value("0"), "1")
        self.config.write_text("in_game=0\n")
        self.assertEqual(self.child_value("1"), "0")

    def test_plugin_config_exposes_in_game_setting(self):
        plugin_config = importlib.import_module("armada_control.config")
        with patch.multiple(
            plugin_config,
            load_fex_contract=lambda: {},
            device_env=lambda: {},
            bottom_screen_brightness=lambda: None,
            parse_power=lambda: {"fan": {}},
            factory_power_defaults=lambda: {},
            load_tweaks=lambda: {},
            installed_games=lambda: [],
            fex_profile_labels=lambda _contract: [],
            turnip_drivers=lambda: [],
            load_env_presets=lambda: [],
            perf_info=lambda: {},
            rgb_supported=lambda: False,
            os_version=lambda: "",
            bottom_screen_enabled=lambda: False,
            bottom_screen_active=lambda: False,
            fan_supported=lambda: False,
            ssh_enabled=lambda: False,
            swipe_gestures_enabled=lambda: True,
            mtp_enabled=lambda: False,
            desktop_mode=lambda: "desktop",
            desktop_modes=lambda: [],
            sleep_modes=lambda: [],
            # build_config reads the USB network state unconditionally; unmocked
            # it would reach for the live privileged socket.
            usb_network_state=lambda: {"enabled": False, "supported": False},
        ), patch.object(
            plugin_config, "swipe_gestures_in_game_enabled",
            return_value=False, create=True,
        ):
            config = plugin_config.build_config(False)
        self.assertFalse(config["swipeGesturesInGameEnabled"])

    def test_plugin_in_game_getter_defaults_off_on_service_failure(self):
        for error in (RuntimeError("unknown action"), OSError("socket unavailable"), KeyError("enabled")):
            with patch.object(plugin_system, "call", side_effect=error):
                self.assertFalse(plugin_system.swipe_gestures_in_game_enabled())
        with patch.object(plugin_system, "call", return_value={"enabled": True}):
            self.assertTrue(plugin_system.swipe_gestures_in_game_enabled())

    def test_plugin_in_game_setter_is_strict_passthrough(self):
        with patch.object(plugin_system, "call", return_value={"enabled": True}) as mock_call:
            self.assertTrue(plugin_system.set_swipe_gestures_in_game_enabled(True))
            mock_call.assert_called_once_with(
                "set_swipe_gestures_in_game_enabled", enabled=True
            )
        with patch.object(plugin_system, "call", return_value={"enabled": False}) as mock_call:
            self.assertFalse(plugin_system.set_swipe_gestures_in_game_enabled(False))
            mock_call.assert_called_once_with(
                "set_swipe_gestures_in_game_enabled", enabled=False
            )


if __name__ == "__main__":
    unittest.main()
