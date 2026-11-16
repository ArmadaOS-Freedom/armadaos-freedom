"""Regression tests for FEX thunk selection in ``armada-game-launch``.

FEX loads ``Config.json`` in layers - global, then user, then the ``AppConfig``
override - and applies every ``ThunksDB`` item as
``DBObject.Enabled = json_getInteger(Item) != 0``.  A layer that merely omits a
key inherits the previous layer's value, and the older shipped base
``Config.json`` enables ``asound`` and ``WaylandClient``.  Both thunks are
unsupported due to upstream
compatibility faults, so a generated per-game config must carry them as an
explicit ``0`` on every launch: not inheriting them from the base config, and
not re-enabling them because legacy tweak settings ask for them.

``Vulkan``, ``GL`` and ``drm`` stay selectable with default-on and
explicit-off semantics, and every other module named by the base config keeps
its key.

The launch script is imported for real; only its base FEX config path and
``XDG_CACHE_HOME`` are redirected, so ``apply_fex`` writes a real JSON config
whose contents the assertions read back.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

SYSTEM_FILES = Path(__file__).resolve().parents[1] / "system_files"
sys.path.insert(0, str(SYSTEM_FILES / "usr/lib/armada"))


def _load_launch():
    path = SYSTEM_FILES / "usr/libexec/armada/armada-game-launch"
    loader = SourceFileLoader("armada_game_launch_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


launch = _load_launch()

THUNK_MODULES = ["Vulkan", "GL", "drm", "asound", "WaylandClient"]
SUPPORTED_THUNKS = {"Vulkan": 1, "GL": 1, "drm": 1}
UNSUPPORTED_THUNKS = ("asound", "WaylandClient")
DISABLED_UNSUPPORTED_THUNKS = {name: 0 for name in UNSUPPORTED_THUNKS}
# What every generated config must end up with: the supported modules at their
# selected state plus an explicit off for the two unsupported ones.
EXPECTED_THUNKS = {**SUPPORTED_THUNKS, **DISABLED_UNSUPPORTED_THUNKS}

# An older bundled Config.json: both removed thunks are still advertised.
BASE_FEX_CONFIG = {
    "Config": {
        "RootFS": "/usr/share/fex-emu/RootFS/Ubuntu_23_04",
        "ThunkGuestLibs": "/usr/lib/fex-emu/GuestThunks",
        "ThunkHostLibs": "/usr/lib/fex-emu/HostThunks",
        "SMCChecks": "mtrack",
        "X87ReducedPrecision": "0",
    },
    "ThunksDB": {name: 1 for name in THUNK_MODULES},
}

# The app-layer PluginLoader.json overrides the global config, so it must name
# both unsupported thunks explicitly as 0: a missing key would inherit the
# enabled value from the older base Config.json.
SHIPPED_PLUGIN_LOADER = SYSTEM_FILES / "usr/share/fex-emu/AppConfig/PluginLoader.json"
FACTORY_TWEAKS = SYSTEM_FILES / "usr/share/armada/game-tweaks.json"

PROFILES = {
    "default": {
        "label": "Default",
        "config": {"TSOEnabled": "1", "Multiblock": "1"},
    },
    "fast": {
        "label": "Fast",
        "config": {"TSOEnabled": "0", "Multiblock": "0"},
    },
}


class ThunkSettingsTests(unittest.TestCase):
    def test_unsupported_thunks_are_explicitly_off_even_when_legacy_settings_enable_them(self):
        settings = {"thunks": {name: True for name in UNSUPPORTED_THUNKS}}

        self.assertEqual(launch.thunk_settings(settings, THUNK_MODULES), EXPECTED_THUNKS)

    def test_supported_thunks_default_on(self):
        self.assertEqual(launch.thunk_settings({}, THUNK_MODULES), EXPECTED_THUNKS)

    def test_supported_thunks_keep_an_explicit_false(self):
        settings = {"thunks": {"GL": False, "drm": False}}

        self.assertEqual(
            launch.thunk_settings(settings, THUNK_MODULES),
            {"Vulkan": 1, "GL": 0, "drm": 0, **DISABLED_UNSUPPORTED_THUNKS},
        )

    def test_unsupported_thunks_are_added_when_the_base_omits_them(self):
        # A base config that never lists them would otherwise inherit the
        # device layer's enabled value, so the keys must be introduced here.
        self.assertEqual(
            launch.thunk_settings({"thunks": {"asound": True}}, {"GL": 1}),
            {"GL": 1, **DISABLED_UNSUPPORTED_THUNKS},
        )

    def test_other_module_keys_are_preserved(self):
        modules = [*THUNK_MODULES, "X11"]

        self.assertEqual(
            launch.thunk_settings({"thunks": {"X11": False}}, modules),
            {**EXPECTED_THUNKS, "X11": 0},
        )


class ApplyFexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.base_config = base / "usr/share/fex-emu/Config.json"
        self.base_config.parent.mkdir(parents=True)
        self.cache = base / "cache"
        self.cache.mkdir()
        patcher = mock.patch.object(launch, "BASE_FEX_CONFIG", self.base_config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_base(BASE_FEX_CONFIG)

    def write_base(self, config):
        self.base_config.write_text(json.dumps(config), encoding="utf-8")

    def generate(self, settings):
        """Run the real apply_fex and read back the config it wrote."""
        env = {}
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}):
            launch.apply_fex(settings, "12345", PROFILES, env)
        target = Path(env["FEX_APP_CONFIG"])
        self.assertEqual(target.parent, self.cache / "armada-fex")
        return json.loads(target.read_text(encoding="utf-8"))

    def test_generated_config_overrides_inherited_unsupported_thunks(self):
        # The base advertises both unsupported thunks as enabled.  FEX would
        # inherit 1 from that layer if the key were missing here.
        generated = self.generate({"fexProfile": "default"})

        self.assertEqual(generated["ThunksDB"], EXPECTED_THUNKS)

    def test_generated_config_overrides_legacy_enabled_settings(self):
        settings = {
            "fexProfile": "default",
            "thunks": {name: True for name in UNSUPPORTED_THUNKS},
        }

        self.assertEqual(self.generate(settings)["ThunksDB"], EXPECTED_THUNKS)

    def test_generated_config_writes_unsupported_zero_keys_for_a_bare_base(self):
        self.write_base({"Config": {"SMCChecks": "mtrack"}})

        generated = self.generate({"fexProfile": "default"})

        self.assertEqual(generated["ThunksDB"], DISABLED_UNSUPPORTED_THUNKS)

    def test_generated_config_keeps_supported_explicit_off(self):
        settings = {"fexProfile": "default", "thunks": {"GL": False, "drm": False}}

        self.assertEqual(
            self.generate(settings)["ThunksDB"],
            {"Vulkan": 1, "GL": 0, "drm": 0, **DISABLED_UNSUPPORTED_THUNKS},
        )

    def test_generated_config_keeps_other_base_modules(self):
        self.write_base({"Config": {"SMCChecks": "mtrack"},
                         "ThunksDB": {**BASE_FEX_CONFIG["ThunksDB"], "X11": 1}})

        generated = self.generate({"fexProfile": "default", "thunks": {"X11": False}})

        self.assertEqual(generated["ThunksDB"], {**EXPECTED_THUNKS, "X11": 0})

    def test_generated_config_drops_legacy_thunks_and_keeps_the_rest(self):
        settings = {
            "fexConfig": {
                "TSOEnabled": "0",
                "Multiblock": "1",
                "RootFS": "/tmp/rootfs",
                "NotAFexSetting": "1",
            },
            "thunks": {name: True for name in UNSUPPORTED_THUNKS},
        }

        generated = self.generate(settings)

        self.assertEqual(generated["ThunksDB"], EXPECTED_THUNKS)
        config = generated["Config"]
        self.assertEqual(config["SMCChecks"], "mtrack")
        self.assertEqual(config["X87ReducedPrecision"], "0")
        self.assertEqual(config["TSOEnabled"], "0")
        self.assertEqual(config["Multiblock"], "1")
        self.assertNotIn("RootFS", config)
        self.assertNotIn("ThunkGuestLibs", config)
        self.assertNotIn("ThunkHostLibs", config)
        self.assertNotIn("NotAFexSetting", config)


class ShippedConfigTests(unittest.TestCase):
    def test_shipped_plugin_loader_disables_unsupported_thunks(self):
        loader = json.loads(SHIPPED_PLUGIN_LOADER.read_text(encoding="utf-8"))

        thunks = loader.get("ThunksDB", {})
        self.assertEqual(thunks.get("asound"), 0)
        self.assertEqual(thunks.get("WaylandClient"), 0)
        self.assertEqual(
            {name: thunks.get(name) for name in SUPPORTED_THUNKS}, SUPPORTED_THUNKS)

    def test_factory_tweaks_still_omit_the_unsupported_entries(self):
        factory = json.loads(FACTORY_TWEAKS.read_text(encoding="utf-8"))

        thunks = factory["global"]["thunks"]
        self.assertEqual(set(thunks), set(SUPPORTED_THUNKS))
        self.assertTrue(all(thunks.values()))


if __name__ == "__main__":
    unittest.main()
