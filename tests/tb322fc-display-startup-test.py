#!/usr/bin/env python3
"""Run the real device/profile and session startup shell with no live session.

The fixture executes the Armada quirk additions from the package patch, the
Steam session's preamble, and the saved-orientation helper in their startup
order. It never sources storage/bootstrap code or starts Gamescope. Assertions
cover the launcher environment contract, not physical DPU scanout.
"""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEVICE_ENV = ROOT / "system_files/usr/libexec/armada/device-env"
DEVICE_DIR = ROOT / "system_files/usr/lib/armada/devices"
SESSION = ROOT / "system_files/usr/share/gamescope-session-plus/sessions.d/steam"
OVERRIDE = ROOT / "system_files/usr/lib/armada/display-orientation.sh"
QUIRKS_PATCH = (
    ROOT / "packages/gamescope-session/patches/0004-source-armada-specific-quirks.patch"
)


class Tb322fcDisplayStartupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.config = self.work / "display-orientation.conf"
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash, "bash is required")

        # These are the production shell additions, including the conditional
        # device-env and FORCE_COMPOSITION_ROTATION handling, not a reimplementation.
        added = "\n".join(
            line[1:]
            for line in QUIRKS_PATCH.read_text().splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertEqual(added.count("/usr/libexec/armada/device-env"), 2)
        # Redirect just the absolute helper path into the checked-out fixture.
        # Invoking bash avoids depending on checkout executable bits on Windows.
        helper = self.work / "device-env"
        helper.write_text('#!/usr/bin/env bash\nexec bash "$ARMADA_REAL_DEVICE_ENV"\n')
        helper.chmod(0o755)
        self.quirks = self.work / "quirks.sh"
        self.quirks.write_text(added.replace("/usr/libexec/armada/device-env", shlex.quote(str(helper))))
        boundary = "source /usr/lib/armada/storage-lib"
        session = SESSION.read_text()
        self.assertEqual(session.count(boundary), 1)
        self.preamble = self.work / "steam-preamble.sh"
        self.preamble.write_text(session.split(boundary, 1)[0])

    def startup(self, model="Lenovo Legion Y700 Gen4", inherited=None, saved=None):
        if saved is not None:
            self.config.write_text(saved)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.work),
            "ARMADA_MODEL": model,
            "ARMADA_DEVICE_DIR": str(DEVICE_DIR),
            "ARMADA_REAL_DEVICE_ENV": str(DEVICE_ENV),
            "ARMADA_MEM_SLEEP_PATH": str(self.work / "missing-mem-sleep"),
            "ARMADA_SLEEP_CONFIG": str(self.work / "missing-sleep.conf"),
            "ARMADA_DISPLAY_ORIENTATION_CONFIG": str(self.config),
        }
        environment.update(inherited or {})
        script = (
            '. "$1"\n'
            '. "$2"\n'
            '. "$3"\n'
            'printf "device=%s\\n" "$ARMADA_DEVICE_ID"\n'
            'printf "panel=%s\\n" "$ARMADA_PANEL_ORIENTATION"\n'
            'printf "shader=%s\\n" "$ARMADA_GAMESCOPE_USE_ROTATION_SHADER"\n'
            'printf "orientation=%s\\n" "${ORIENTATION-<unset>}"\n'
            'printf "force_composition=%s\\n" "${FORCE_COMPOSITION_ROTATION-<unset>}"\n'
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-eu", "-c", script,
             "fixture", str(self.quirks), str(self.preamble), str(OVERRIDE)],
            env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_gen4_default_is_right_without_forced_shader(self):
        self.assertEqual(self.startup(), {
            "device": "lenovo-tb322fc", "panel": "right", "shader": "0",
            "orientation": "right", "force_composition": "<unset>",
        })

    def test_explicit_session_direction_is_preserved(self):
        self.assertEqual(self.startup(inherited={"ORIENTATION": "left"})["orientation"], "left")

    def test_explicit_software_recovery_is_preserved(self):
        state = self.startup(inherited={"ORIENTATION": "normal", "FORCE_COMPOSITION_ROTATION": "1"})
        self.assertEqual(state["orientation"], "normal")
        self.assertEqual(state["force_composition"], "1")

    def test_saved_user_direction_wins_after_session_defaults(self):
        state = self.startup(inherited={"ORIENTATION": "left"}, saved="orientation=upsidedown\n")
        self.assertEqual(state["orientation"], "upsidedown")
        self.assertEqual(state["force_composition"], "<unset>")

    def test_empty_saved_direction_restores_fixed_default(self):
        self.assertEqual(self.startup(saved="orientation=\n")["orientation"], "right")

    def test_other_device_keeps_existing_orientation_and_shader_policy(self):
        state = self.startup(model="AYANEO Pocket DMG")
        self.assertEqual(state["panel"], "left")
        self.assertEqual(state["orientation"], "<unset>")
        self.assertEqual(state["force_composition"], "<unset>")

    def test_device_that_requires_shader_still_forces_composition(self):
        state = self.startup(model="AYANEO Pocket S 2K")
        self.assertEqual(state["shader"], "1")
        self.assertEqual(state["force_composition"], "1")

    def test_gen5_session_defaults_and_explicit_choice_are_unchanged(self):
        for inherited, expected in (({}, "right"), ({"ORIENTATION": "left"}, "left")):
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                           "ARMADA_DEVICE_ID": "lenovo-tb323fu", **inherited}
            result = subprocess.run(
                [self.bash, "-eu", "-c", '. "$1"; printf "%s" "$ORIENTATION"',
                 "fixture", str(self.preamble)],
                env=environment, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
