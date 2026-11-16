"""Regression tests for FEX thunk selection in ``armada-game-launch``.

Older bundled FEX defaults list ``asound`` and ``WaylandClient`` in
``ThunksDB``, but these thunks are unsupported due to upstream compatibility
faults, so a generated per-game config must never carry them - not from those
defaults and not because legacy tweak settings enable them.  ``Vulkan``, ``GL``
and ``drm`` stay selectable with default-on and explicit-off semantics.

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
    def test_unsupported_thunks_stay_out_even_when_legacy_settings_enable_them(self):
        settings = {"thunks": {name: True for name in UNSUPPORTED_THUNKS}}

        self.assertEqual(launch.thunk_settings(settings, THUNK_MODULES), SUPPORTED_THUNKS)

    def test_supported_thunks_default_on(self):
        self.assertEqual(launch.thunk_settings({}, THUNK_MODULES), SUPPORTED_THUNKS)

    def test_supported_thunks_keep_an_explicit_false(self):
        settings = {"thunks": {"GL": False, "drm": False}}

        self.assertEqual(
            launch.thunk_settings(settings, THUNK_MODULES),
            {"Vulkan": 1, "GL": 0, "drm": 0},
        )


class ApplyFexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.base_config = base / "usr/share/fex-emu/Config.json"
        self.base_config.parent.mkdir(parents=True)
        self.base_config.write_text(json.dumps(BASE_FEX_CONFIG), encoding="utf-8")
        self.cache = base / "cache"
        self.cache.mkdir()
        patcher = mock.patch.object(launch, "BASE_FEX_CONFIG", self.base_config)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        env = {}

        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)}):
            launch.apply_fex(settings, "12345", PROFILES, env)

        target = Path(env["FEX_APP_CONFIG"])
        self.assertEqual(target.parent, self.cache / "armada-fex")
        generated = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(generated["ThunksDB"], SUPPORTED_THUNKS)
        config = generated["Config"]
        self.assertEqual(config["SMCChecks"], "mtrack")
        self.assertEqual(config["X87ReducedPrecision"], "0")
        self.assertEqual(config["TSOEnabled"], "0")
        self.assertEqual(config["Multiblock"], "1")
        self.assertNotIn("RootFS", config)
        self.assertNotIn("ThunkGuestLibs", config)
        self.assertNotIn("ThunkHostLibs", config)
        self.assertNotIn("NotAFexSetting", config)


if __name__ == "__main__":
    unittest.main()
