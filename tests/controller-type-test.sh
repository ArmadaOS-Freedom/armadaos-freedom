#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$ROOT" <<'PY'
import importlib.machinery
import importlib.util
import io
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

root = Path(sys.argv[1])
DEVICE_ENV = root / "system_files/usr/libexec/armada/device-env"
CONTROLLER_TYPE = root / "system_files/usr/libexec/armada/controller-type"
DEVICE_DIR = root / "system_files/usr/lib/armada/devices"
ALL_TARGETS = "deck-uhid,xbox-series,xb360,ds5"


def unquote(value):
    try:
        parts = shlex.split(value)
    except ValueError:
        return value.strip()
    return parts[0] if parts else ""


def load_controller_type():
    loader = importlib.machinery.SourceFileLoader(
        "armada_controller_type", str(CONTROLLER_TYPE)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def run_device_env(model, dt_root):
    env = dict(os.environ)
    env["ARMADA_DEVICE_DIR"] = str(DEVICE_DIR)
    env["ARMADA_DT_ROOT"] = str(dt_root)
    env["ARMADA_MODEL"] = model
    result = subprocess.run(
        ["bash", str(DEVICE_ENV)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value
    return values


def make_fake_device_env(tmpdir, name, lines):
    path = Path(tmpdir) / name
    body = (
        "#!/usr/bin/env bash\n"
        "cat <<'ARMADA_ENV_EOF'\n"
        + "\n".join(lines)
        + "\nARMADA_ENV_EOF\n"
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def make_failing_device_env(tmpdir, name):
    path = Path(tmpdir) / name
    path.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class DeviceEnvProfileTest(unittest.TestCase):
    """profile export: default 1, exact Gen4 detection 0 (real device-env)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dt_root = Path(self.tmp.name) / "dt"
        self.dt_root.mkdir()

    def test_unknown_device_keeps_default_one(self):
        values = run_device_env("Totally Unknown Handheld", self.dt_root)
        self.assertEqual(values["ARMADA_DEVICE_ID"], "unknown")
        self.assertEqual(unquote(values["ARMADA_BUILTIN_GAMEPAD"]), "1")
        self.assertEqual(unquote(values["ARMADA_IP_TARGETS"]), ALL_TARGETS)

    def test_gen4_compatible_token_sets_zero(self):
        (self.dt_root / "compatible").write_bytes(b"lenovo,elden\0")
        values = run_device_env("", self.dt_root)
        self.assertEqual(values["ARMADA_DEVICE_ID"], "lenovo-tb322fc")
        self.assertEqual(unquote(values["ARMADA_BUILTIN_GAMEPAD"]), "0")

    def test_gen4_model_string_sets_zero(self):
        values = run_device_env("Lenovo Legion Y700 Gen4", self.dt_root)
        self.assertEqual(values["ARMADA_DEVICE_ID"], "lenovo-tb322fc")
        self.assertEqual(unquote(values["ARMADA_BUILTIN_GAMEPAD"]), "0")


class ControllerTypeApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_controller_type()
        self.module.CONFIG = Path(self.tmp.name) / "controller.conf"

    def _patch_polling(self):
        polled, slept = [], []
        real_time = self.module.time
        isolated_time = SimpleNamespace(
            monotonic=real_time.monotonic,
            sleep=lambda _s: slept.append(1),
        )
        patches = [
            mock.patch.object(
                self.module, "composite_paths", side_effect=lambda deadline=None: polled.append(1) or []
            ),
            mock.patch.object(
                self.module, "time", isolated_time
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return polled, slept

    def test_patch_polling_does_not_mutate_shared_time_module(self):
        original_sleep = time.sleep
        polled, slept = self._patch_polling()
        self.assertIs(time.sleep, original_sleep)
        self.module.time.sleep(1)
        self.assertEqual(slept, [1])
        self.assertIs(time.sleep, original_sleep)

    def test_apply_capability_zero_is_noop_without_polling(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name,
            "env0",
            [f"ARMADA_IP_TARGETS={ALL_TARGETS}", "ARMADA_BUILTIN_GAMEPAD=0"],
        )
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 0)
        self.assertEqual(polled, [])
        self.assertEqual(slept, [])

    def test_apply_absent_flag_preserves_original_apply(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name, "env_absent", [f"ARMADA_IP_TARGETS={ALL_TARGETS}"]
        )
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 1)
        self.assertEqual(len(polled), 30)
        self.assertEqual(len(slept), 30)

    def test_apply_unknown_flag_preserves_original_apply(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name,
            "env_unknown",
            [f"ARMADA_IP_TARGETS={ALL_TARGETS}", "ARMADA_BUILTIN_GAMEPAD=maybe"],
        )
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 1)
        self.assertEqual(len(polled), 30)
        self.assertEqual(len(slept), 30)

    def test_apply_explicit_flag_one_preserves_original_apply(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name,
            "env_explicit_one",
            [f"ARMADA_IP_TARGETS={ALL_TARGETS}", "ARMADA_BUILTIN_GAMEPAD=1"],
        )
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 1)
        self.assertEqual(len(polled), 30)
        self.assertEqual(len(slept), 30)

    def test_apply_device_env_nonzero_exit_preserves_original_apply(self):
        self.module.DEVICE_ENV = make_failing_device_env(self.tmp.name, "env_nonzero")
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 1)
        self.assertEqual(len(polled), 30)
        self.assertEqual(len(slept), 30)

    def test_apply_missing_device_env_preserves_original_apply(self):
        self.module.DEVICE_ENV = str(Path(self.tmp.name) / "does-not-exist")
        polled, slept = self._patch_polling()
        self.assertEqual(self.module.main(["controller-type", "apply"]), 1)
        self.assertEqual(len(polled), 30)
        self.assertEqual(len(slept), 30)

    def test_set_still_applies_and_saves_even_with_capability_zero(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name,
            "env_set",
            [f"ARMADA_IP_TARGETS={ALL_TARGETS}", "ARMADA_BUILTIN_GAMEPAD=0"],
        )
        config = Path(self.tmp.name) / "controller.conf"
        self.module.CONFIG = config
        applied = []
        with mock.patch.object(
            self.module, "apply_type", side_effect=lambda v, deadline=None: applied.append(v) or True
        ):
            self.assertEqual(self.module.main(["controller-type", "set", "xbox-series"]), 0)
        self.assertEqual(applied, ["xbox-series"])
        self.assertEqual(
            config.read_text(encoding="utf-8").strip(), "controller_type=xbox-series"
        )

    def test_set_rejects_unknown_type(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name, "env_reject", [f"ARMADA_IP_TARGETS={ALL_TARGETS}"]
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(self.module.main(["controller-type", "set", "nope"]), 2)

    def test_get_unaffected_by_capability_zero(self):
        self.module.DEVICE_ENV = make_fake_device_env(
            self.tmp.name,
            "env_get",
            [f"ARMADA_IP_TARGETS={ALL_TARGETS}", "ARMADA_BUILTIN_GAMEPAD=0"],
        )
        self.module.CONFIG = Path(self.tmp.name) / "controller.conf"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(self.module.main(["controller-type", "get"]), 0)
        self.assertEqual(stdout.getvalue().strip(), "deck-uhid")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
PY
