#!/usr/bin/env bash

# Regression tests for the shipped apply-input-calibration script.
#
# Scope note: these cases cover the production contract that exists in this
# tree -- legacy trigger-reference migration, backend selection, and the
# parameter writes into the kernel module parameter directories. The previous
# revision of this file also asserted an ``armada_control.calibration`` plugin
# module and daemon-side calibration actions; neither implementation is part of
# the pinned Armada Control, so those assertions were removed rather than
# replaced with a fake calibration model.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

python3 -B - "$ROOT" "$WORK" <<'PYEOF'
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
work = Path(sys.argv[2])

spec = importlib.util.spec_from_loader(
    "apply_input_calibration_test",
    importlib.machinery.SourceFileLoader(
        "apply_input_calibration_test",
        str(root / "system_files/usr/libexec/armada/apply-input-calibration"),
    ),
)
apply_calibration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_calibration)

BACKENDS = ("mangmi", "rsinput", "retroid")
created = 0


def backend_dir(name):
    """Fresh module parameter directory holding every shipped parameter."""
    global created
    created += 1
    path = work / f"{created:02d}-{name}"
    path.mkdir()
    for param in apply_calibration.PARAM_NAMES:
        (path / param).write_text("0", encoding="utf-8")
    return path


def install_backends():
    backends = {name: backend_dir(name) for name in BACKENDS}
    apply_calibration.CALIBRATION_BACKENDS = dict(backends)
    return backends


def write_config(payload):
    path = work / "input-calibration.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    apply_calibration.CONFIG = path
    return path


def param(directory, name):
    path = directory / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def untouched(directories):
    """No directory received a single non-default parameter or update marker."""
    for name, directory in directories.items():
        for entry in (*apply_calibration.PARAM_NAMES, "update_params"):
            value = param(directory, entry)
            assert value in (None, "0"), (name, entry, value)


# --- backend selection and parameter writes -------------------------------
backends = install_backends()
write_config('{"axis_leftx_center": 17}\n')
apply_calibration.main()
assert param(backends["rsinput"], "axis_leftx_center") == "17"
assert param(backends["rsinput"], "update_params") == "1"
assert param(backends["retroid"], "axis_leftx_center") == "0"
assert param(backends["retroid"], "update_params") is None
assert param(backends["mangmi"], "axis_leftx_center") == "0"
assert param(backends["mangmi"], "update_params") is None

backends = install_backends()
write_config('{"backend":"retroid","axis_leftx_center":23}\n')
apply_calibration.main()
assert param(backends["retroid"], "axis_leftx_center") == "23"
assert param(backends["retroid"], "update_params") == "1"
assert param(backends["rsinput"], "axis_leftx_center") == "0"
assert param(backends["rsinput"], "update_params") is None

# An unknown backend must not reach any module directory.
backends = install_backends()
write_config('{"backend":"unknown","axis_leftx_center":5}\n')
apply_calibration.main()
untouched(backends)

# A missing config file is a no-op.
backends = install_backends()
apply_calibration.CONFIG = work / "absent-input-calibration.json"
apply_calibration.CONFIG.unlink(missing_ok=True)
apply_calibration.main()
untouched(backends)

# --- legacy trigger-reference conversion ----------------------------------
def shaped_trigger(raw, reference, params):
    value = reference - raw
    if value < params["trigger_left_deadzone"]:
        return 0
    return value - params["trigger_left_antideadzone"]


# Rest at raw 1352, full pull at raw 452.
legacy = {"backend": "retroid", "trigger_left_max": 900, "trigger_left_deadzone": 27,
          "trigger_left_antideadzone": 0}
migrated = apply_calibration.migrate(legacy, "retroid")
assert legacy["trigger_left_max"] == 900, "migrate mutated its input"
assert migrated["trigger_left_max"] == 1552
assert migrated["trigger_left_deadzone"] == 679
assert migrated["trigger_left_antideadzone"] == 652
assert migrated["trigger_left_max"] - migrated["trigger_left_antideadzone"] == 900
for raw in range(0, 2000, 7):
    assert shaped_trigger(raw, 1552, migrated) == shaped_trigger(
        raw, legacy["trigger_left_max"], legacy
    ), raw
assert shaped_trigger(1352, 1552, migrated) == 0
assert shaped_trigger(452, 1552, migrated) == 448

# Version 2 files and backends without a legacy reference stay untouched.
assert apply_calibration.migrate({**legacy, "version": 2}, "retroid")["trigger_left_max"] == 900
assert apply_calibration.migrate({**legacy, "backend": "rsinput"}, "rsinput")["trigger_left_max"] == 900
legacy_mangmi = apply_calibration.migrate(
    {"trigger_right_max": 1552, "trigger_right_deadzone": 0, "trigger_right_antideadzone": 0}, "mangmi"
)
assert legacy_mangmi["trigger_right_max"] == 1758
assert legacy_mangmi["trigger_right_deadzone"] == 206
assert legacy_mangmi["trigger_right_antideadzone"] == 206

# The script writes the converted values, not the legacy ones.
backends = install_backends()
write_config(legacy)
apply_calibration.main()
assert param(backends["retroid"], "trigger_left_max") == "1552"
assert param(backends["retroid"], "trigger_left_deadzone") == "679"
assert param(backends["retroid"], "trigger_left_antideadzone") == "652"
assert param(backends["retroid"], "update_params") == "1"
assert param(backends["rsinput"], "trigger_left_max") == "0"
PYEOF

echo "Input calibration tests passed"
