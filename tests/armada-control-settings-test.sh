#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# device-env reads defaults.conf and the selected profile from one directory.
# Assemble the shipped profiles and overlay the canonical FreedomControl
# profile, which the image takes from the plugin source rather than the base
# device tree.
DEVICES_DIR="$WORK/devices"
mkdir -p "$DEVICES_DIR"
cp "$ROOT"/system_files/usr/lib/armada/devices/*.conf "$DEVICES_DIR"/
cp "$ROOT"/decky/freedom-control/system_files/usr/lib/armada/devices/lenovo-tb323fu.conf \
    "$DEVICES_DIR"/

python3 - "$ROOT" "$WORK" <<'PYEOF'
import importlib.machinery
import importlib.util
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
work = pathlib.Path(sys.argv[2])
lib = root / "system_files/usr/lib/armada"
sys.path.insert(0, str(lib))
import armada_perf  # noqa: F401

control_path = root / "system_files/usr/libexec/armada/armada-control"
loader = importlib.machinery.SourceFileLoader("armada_control", str(control_path))
spec = importlib.util.spec_from_loader("armada_control", loader)
control = importlib.util.module_from_spec(spec)
loader.exec_module(control)

control.SLEEP_CONFIG = work / "sleep.conf"
control.NM_IGNORE_SLEEP = work / "ignore-sleep"
control.STEAMOS_MANAGER_STATE = work / "steamos-manager-state.toml"

# Missing or raw upstream state is Mobile; both explicit armada choices map.
assert control.get_desktop_mode() == "mobile"
control.STEAMOS_MANAGER_STATE.write_text('desktop_session = "plasma.desktop"\n')
assert control.get_desktop_mode() == "mobile"
control.STEAMOS_MANAGER_STATE.write_text('other_setting = "keep"\n')
assert control.get_desktop_mode() == "mobile"
control.STEAMOS_MANAGER_STATE.write_text('desktop_session = "armada-plasma.desktop"\n')
assert control.get_desktop_mode() == "desktop"
control.STEAMOS_MANAGER_STATE.write_text('desktop_session = "armada-plasma-mobile.desktop"\n')
assert control.get_desktop_mode() == "mobile"
control.MEM_SLEEP_PATH = work / "mem_sleep"
control.MEM_SLEEP_PATH.write_text("[s2idle] deep\n")
control.BOTTOM_SCREEN_BRIGHTNESS_PATH = work / "bottom-screen-brightness"
control.BACKLIGHT_ROOT = work / "backlight"
control.DRM_ROOT = work / "drm"
secondary_connector = control.DRM_ROOT / "card0-DSI-1"
secondary_connector.mkdir(parents=True)
(secondary_connector / "enabled").write_text("disabled\n")
secondary_backlight = control.BACKLIGHT_ROOT / "secondary"
secondary_backlight.mkdir(parents=True)
(secondary_backlight / "brightness").write_text("128\n")
(secondary_backlight / "max_brightness").write_text("255\n")


class Result:
    def __init__(self, returncode):
        self.returncode = returncode


unit_state = {"enabled": False}
bottom_starts = []
systemctl_calls = []


def fake_session_systemctl(*args, check=True, timeout=30):
    systemctl_calls.append(args)
    if args[:2] == ("is-enabled", "--quiet"):
        return Result(0 if unit_state["enabled"] else 1)
    if args[0] == "enable":
        unit_state["enabled"] = True
    elif args[0] == "disable":
        unit_state["enabled"] = False
    return Result(0)


control.session_systemctl = fake_session_systemctl
control.start_bottom_screen = lambda: bottom_starts.append(True)
control.device_env = lambda: {
    "ARMADA_SECONDARY_CONNECTOR": "DSI-1",
    "ARMADA_SECONDARY_BACKLIGHT": "secondary",
    "ARMADA_SECONDARY_TOUCHSCREEN": "bottom_touchscreen",
}
assert control.action_get_bottom_screen_enabled({}) == {"enabled": False}
assert control.action_set_bottom_screen_enabled({"enabled": True}) == {"enabled": True}
assert ("enable", control.BOTTOM_SCREEN_SERVICE) in systemctl_calls
assert bottom_starts == [True]
assert control.action_set_bottom_screen_enabled({"enabled": False}) == {"enabled": False}
assert ("disable", "--now", control.BOTTOM_SCREEN_SERVICE) in systemctl_calls

assert bottom_starts == [True]

control.device_env = lambda: {}
try:
    control.action_set_bottom_screen_enabled({"enabled": True})
except RuntimeError:
    pass
else:
    raise AssertionError("unsupported bottom screen was enabled")

try:
    control.action_set_bottom_screen_enabled({"enabled": "yes"})
except ValueError:
    pass
else:
    raise AssertionError("invalid bottom-screen state was accepted")

control.device_env = lambda: {
    "ARMADA_SECONDARY_BACKLIGHT": "secondary",
    "ARMADA_SECONDARY_CONNECTOR": "DSI-1",
}
assert control.action_get_bottom_screen_brightness({}) == {
    "supported": True,
    "brightness": 50,
    "active": False,
}
(secondary_connector / "enabled").write_text("enabled\n")
assert control.action_get_bottom_screen_brightness({})["active"] is True
(secondary_connector / "enabled").write_text("disabled\n")
assert control.action_set_bottom_screen_brightness({"brightness": 40}) == {"brightness": 40}
assert (secondary_backlight / "brightness").read_text() == "102\n"
assert control.BOTTOM_SCREEN_BRIGHTNESS_PATH.read_text() == "40\n"

# Driver and session changes can reset the backlight; the saved preference wins.
(secondary_backlight / "brightness").write_text("255\n")
control.restore_bottom_screen_brightness()
assert (secondary_backlight / "brightness").read_text() == "102\n"

# Missing and malformed preferences leave the hardware's current value alone.
control.BOTTOM_SCREEN_BRIGHTNESS_PATH.unlink()
(secondary_backlight / "brightness").write_text("128\n")
control.restore_bottom_screen_brightness()
assert (secondary_backlight / "brightness").read_text() == "128\n"
control.BOTTOM_SCREEN_BRIGHTNESS_PATH.write_text("invalid\n")
control.restore_bottom_screen_brightness()
assert (secondary_backlight / "brightness").read_text() == "128\n"

secondary_backlight_4096 = control.BACKLIGHT_ROOT / "secondary-4096"
secondary_backlight_4096.mkdir()
(secondary_backlight_4096 / "brightness").write_text("4096\n")
(secondary_backlight_4096 / "max_brightness").write_text("4096\n")
control.device_env = lambda: {"ARMADA_SECONDARY_BACKLIGHT": "secondary-4096"}
assert control.action_set_bottom_screen_brightness({"brightness": 25}) == {"brightness": 25}
assert (secondary_backlight_4096 / "brightness").read_text() == "1024\n"
assert control.BOTTOM_SCREEN_BRIGHTNESS_PATH.read_text() == "25\n"

for invalid in (True, "40", -1, 101):
    try:
        control.action_set_bottom_screen_brightness({"brightness": invalid})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid bottom-screen brightness was accepted")

control.device_env = lambda: {}
assert control.action_get_bottom_screen_brightness({}) == {
    "supported": False,
    "brightness": 0,
    "active": False,
}
try:
    control.action_set_bottom_screen_brightness({"brightness": 50})
except RuntimeError:
    pass
else:
    raise AssertionError("unsupported bottom-screen brightness was changed")

plugin_lib = root / "decky/freedom-control/decky/armada-control/py_modules"
sys.path.insert(0, str(plugin_lib))
from armada_control import system as plugin_system

def fake_plugin_call(action, **payload):
    if action == "get_bottom_screen_brightness":
        return {"supported": True, "brightness": 50, "active": True}
    if action == "set_bottom_screen_brightness":
        return {"brightness": int(payload["brightness"])}
    return {"enabled": action == "get_bottom_screen_enabled" or bool(payload.get("enabled"))}


plugin_system.call = fake_plugin_call
assert plugin_system.bottom_screen_enabled()
assert plugin_system.set_bottom_screen_enabled(True)
assert plugin_system.bottom_screen_brightness() == 50
assert plugin_system.bottom_screen_active()
assert plugin_system.set_bottom_screen_brightness(40) == 40

plugin_system.MEM_SLEEP_PATH = control.MEM_SLEEP_PATH
assert plugin_system.sleep_modes() == [
    {"data": "s2idle", "label": "Native"},
    {"data": "fake", "label": "Fake"},
]

# fan_supported() probes the single powerd FanMode property via busctl; the
# mock keeps the test off real hardware.
FAN_BUSCTL = [
    "/usr/bin/busctl",
    "--system",
    "get-property",
    "org.armada.Power",
    "/org/armada/Power",
    "org.armada.Power1",
    "FanMode",
]
fan_calls = []


class FanResult:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


fan_result = [FanResult(0, 's "managed"\n')]


def fake_fan_run_cmd(cmd, timeout=5, capture=True):
    fan_calls.append((cmd, timeout))
    return fan_result[0]


plugin_system.run_cmd = fake_fan_run_cmd

# Managed fan: one bounded busctl call, exact argument vector.
assert plugin_system.fan_supported() is True
assert fan_calls == [(FAN_BUSCTL, 2)]

# Fanless device: powerd reports the FanMode property as unavailable.
fan_result[0] = FanResult(0, 's "unavailable"\n')
assert plugin_system.fan_supported() is False

# Nonzero exit and a failed launch both mean "no support", not an error.
fan_result[0] = FanResult(1, 's "managed"\n')
assert plugin_system.fan_supported() is False
fan_result[0] = None
assert plugin_system.fan_supported() is False

# Malformed payloads (garbage line, lone signature, unterminated quote, empty)
# are treated as no support rather than true.
for payload in ('error: not found\n', "s", 's "managed', ""):
    fan_result[0] = FanResult(0, payload)
    assert plugin_system.fan_supported() is False

# Every probe is exactly one busctl call with the two-second bound.
assert len(fan_calls) == 8
assert all(call == (FAN_BUSCTL, 2) for call in fan_calls)

control.SLEEP_CONFIG.write_text("future_sleep_setting=keep\n")
control.NM_IGNORE_SLEEP.touch()
assert control.action_set_sleep_mode({"value": "s2idle"}) == {"value": "s2idle"}
assert control.MEM_SLEEP_PATH.read_text() == "s2idle\n"
assert control.SLEEP_CONFIG.read_text() == (
    "future_sleep_setting=keep\nsuspend_mode=s2idle\n"
)
assert not control.NM_IGNORE_SLEEP.exists()

assert control.action_set_sleep_mode({"value": "fake"}) == {"value": "fake"}
assert control.SLEEP_CONFIG.read_text() == (
    "future_sleep_setting=keep\nsuspend_mode=fake\n"
)
assert control.NM_IGNORE_SLEEP.exists()

control.MEM_SLEEP_PATH.write_text("[s2idle] deep\n")
try:
    control.action_set_sleep_mode({"value": "deep"})
except RuntimeError:
    pass
else:
    raise AssertionError("retired deep sleep setting was accepted")

control.MEM_SLEEP_PATH.write_text("[deep]\n")
assert plugin_system.sleep_modes() == [{"data": "fake", "label": "Fake"}]
try:
    control.action_set_sleep_mode({"value": "s2idle"})
except RuntimeError:
    pass
else:
    raise AssertionError("unavailable s2idle sleep setting was accepted")
PYEOF

grep -Fq 'After=armada-device-quirks.service inputplumber.service armada-powerd.service' \
    "$ROOT/system_files/usr/lib/systemd/system/armada-control.service"

DEVICE_ENV="$ROOT/system_files/usr/libexec/armada/device-env"
DEVICE_QUIRKS="$ROOT/system_files/usr/libexec/armada/device-quirks"
DISPATCH="$ROOT/system_files/usr/libexec/armada/suspend-dispatch"

device_env() {
    env ARMADA_DEVICE_DIR="$DEVICES_DIR" \
        ARMADA_MODEL="$1" ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
        ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
        "$DEVICE_ENV" | grep -x "ARMADA_SUSPEND_MODE=$2" >/dev/null
}

env ARMADA_DEVICE_DIR="$DEVICES_DIR" \
    ARMADA_MODEL="AYN Thor" ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
    ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
    "$ROOT/system_files/usr/libexec/armada/device-env" |
    grep -Fx 'ARMADA_SECONDARY_BACKLIGHT=ae94000.dsi.0' >/dev/null
env ARMADA_DEVICE_DIR="$DEVICES_DIR" \
    ARMADA_MODEL="AYANEO Pocket DS" ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
    ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
    "$ROOT/system_files/usr/libexec/armada/device-env" |
    grep -Fx 'ARMADA_SECONDARY_BACKLIGHT=sy7758-backlight' >/dev/null

# The real profile resolves the TB323FU model to the ID the dispatcher gates on.
env ARMADA_DEVICE_DIR="$DEVICES_DIR" \
    ARMADA_MODEL="Lenovo Legion Tab Y700 (TB323FU)" \
    ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
    ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
    "$ROOT/system_files/usr/libexec/armada/device-env" |
    grep -Fx 'ARMADA_DEVICE_ID=lenovo-tb323fu' >/dev/null

printf '[s2idle] deep\n' >"$WORK/mem_sleep"
printf 'suspend_mode=s2idle\n' >"$WORK/sleep.conf"
device_env "AYN Odin 2" s2idle

printf 'suspend_mode=deep\n' >"$WORK/sleep.conf"
device_env "AYN Odin 2" s2idle

printf 'suspend_mode = s2idle\nsuspend_mode = fake\n' >"$WORK/sleep.conf"
device_env "AYN Odin 2" fake

printf 'suspend_mode = fake' >"$WORK/sleep.conf"
device_env "AYN Odin 2" fake

rm -f "$WORK/sleep.conf"
device_env "AYN Odin 2" s2idle
device_env "Retroid Pocket 5" s2idle
device_env "AYN Odin 3" s2idle

printf '[deep]\n' >"$WORK/mem_sleep"
printf 'suspend_mode=s2idle\n' >"$WORK/sleep.conf"
device_env "Retroid Pocket 5" fake

printf 'suspend_mode=deep\n' >"$WORK/sleep.conf"
device_env "AYN Odin 2" fake

: >"$WORK/mem_sleep"
rm -f "$WORK/sleep.conf"
device_env "Retroid Pocket 5" fake

# Boot-time quirks reapply the saved mode and NetworkManager policy.
printf '[s2idle] deep\n' >"$WORK/mem_sleep"
printf 'future_sleep_setting=keep\nsuspend_mode=deep\n' >"$WORK/sleep.conf"
touch "$WORK/ignore-sleep"
env ARMADA_DEVICE_ENV="$DEVICE_ENV" \
    ARMADA_DEVICE_DIR="$DEVICES_DIR" \
    ARMADA_MODEL="AYN Odin 2" ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
    ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
    ARMADA_NM_IGNORE_SLEEP="$WORK/ignore-sleep" \
    "$DEVICE_QUIRKS" >/dev/null
grep -x 's2idle' "$WORK/mem_sleep" >/dev/null
[[ "$(cat "$WORK/sleep.conf")" == "future_sleep_setting=keep
suspend_mode=s2idle" ]]
[[ ! -e "$WORK/ignore-sleep" ]]

printf '[s2idle] deep\n' >"$WORK/mem_sleep"
printf 'suspend_mode=fake\n' >"$WORK/sleep.conf"
env ARMADA_DEVICE_ENV="$DEVICE_ENV" \
    ARMADA_DEVICE_DIR="$DEVICES_DIR" \
    ARMADA_MODEL="AYN Odin 2" ARMADA_SLEEP_CONFIG="$WORK/sleep.conf" \
    ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
    ARMADA_NM_IGNORE_SLEEP="$WORK/ignore-sleep" \
    "$DEVICE_QUIRKS" >/dev/null
grep -Fx '[s2idle] deep' "$WORK/mem_sleep" >/dev/null
[[ -e "$WORK/ignore-sleep" ]]

printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "ARMADA_SUSPEND_MODE=%s\\n" "$TEST_SLEEP_MODE"' \
    '[[ -z "${TEST_DEVICE_ID:-}" ]] || printf "ARMADA_DEVICE_ID=%s\\n" "$TEST_DEVICE_ID"' \
    >"$WORK/device-env"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "%s\\n" "$*" >"$TEST_SLEEP_CALL"' \
    'printf "%s\\n" "${SYSTEMD_SLEEP_FREEZE_USER_SESSIONS-<unset>}" >"$TEST_SLEEP_FREEZE_ENV"' \
    >"$WORK/systemd-sleep"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "%s\\n" "$*" >"$TEST_FAKE_SLEEP_CALL"' \
    'printf "%s\\n" "${SYSTEMD_SLEEP_FREEZE_USER_SESSIONS-<unset>}" >"$TEST_FAKE_SLEEP_FREEZE_ENV"' \
    >"$WORK/fake-suspend"
chmod +x "$WORK/device-env" "$WORK/systemd-sleep" "$WORK/fake-suspend"

dispatch() {
    rm -f "$WORK/sleep-call" "$WORK/fake-sleep-call" \
        "$WORK/sleep-freeze-env" "$WORK/fake-sleep-freeze-env"
    env TEST_SLEEP_MODE="$1" TEST_DEVICE_ID="${2:-}" \
        TEST_SLEEP_CALL="$WORK/sleep-call" \
        TEST_FAKE_SLEEP_CALL="$WORK/fake-sleep-call" \
        TEST_SLEEP_FREEZE_ENV="$WORK/sleep-freeze-env" \
        TEST_FAKE_SLEEP_FREEZE_ENV="$WORK/fake-sleep-freeze-env" \
        ARMADA_DEVICE_ENV="$WORK/device-env" \
        ARMADA_FAKE_SUSPEND="$WORK/fake-suspend" \
        ARMADA_MEM_SLEEP_PATH="$WORK/mem_sleep" \
        ARMADA_SYSTEMD_SLEEP="$WORK/systemd-sleep" \
        "$DISPATCH" 2>/dev/null
}

printf '[s2idle] deep\n' >"$WORK/mem_sleep"
dispatch s2idle
grep -x 's2idle' "$WORK/mem_sleep" >/dev/null
grep -x 'suspend' "$WORK/sleep-call" >/dev/null

printf '[s2idle] deep\n' >"$WORK/mem_sleep"
dispatch deep
grep -x 'sleep' "$WORK/fake-sleep-call" >/dev/null
[[ ! -e "$WORK/sleep-call" ]]

printf '[deep]\n' >"$WORK/mem_sleep"
dispatch s2idle
grep -x 'sleep' "$WORK/fake-sleep-call" >/dev/null
[[ ! -e "$WORK/sleep-call" ]]

# Only a real TB323FU suspend turns off the user-session freeze, and it does so
# even when the caller explicitly asked for freezing.
printf '[s2idle] deep\n' >"$WORK/mem_sleep"
export SYSTEMD_SLEEP_FREEZE_USER_SESSIONS=1
dispatch s2idle lenovo-tb323fu
grep -x 'suspend' "$WORK/sleep-call" >/dev/null
grep -Fx '0' "$WORK/sleep-freeze-env" >/dev/null

# Other or unknown devices keep the caller's explicit value as-is.
dispatch s2idle ayn-odin-2
grep -x 'suspend' "$WORK/sleep-call" >/dev/null
grep -Fx '1' "$WORK/sleep-freeze-env" >/dev/null

dispatch s2idle unknown-device
grep -x 'suspend' "$WORK/sleep-call" >/dev/null
grep -Fx '1' "$WORK/sleep-freeze-env" >/dev/null

# A non-TB323FU device acquires nothing when the caller sets nothing.
unset SYSTEMD_SLEEP_FREEZE_USER_SESSIONS
dispatch s2idle ayn-odin-2
grep -Fx '<unset>' "$WORK/sleep-freeze-env" >/dev/null

# Fake mode and the unsupported-mode fallback never acquire the override.
dispatch fake lenovo-tb323fu
grep -x 'sleep' "$WORK/fake-sleep-call" >/dev/null
grep -Fx '<unset>' "$WORK/fake-sleep-freeze-env" >/dev/null

printf '[deep]\n' >"$WORK/mem_sleep"
dispatch s2idle lenovo-tb323fu
grep -x 'sleep' "$WORK/fake-sleep-call" >/dev/null
grep -Fx '<unset>' "$WORK/fake-sleep-freeze-env" >/dev/null
[[ ! -e "$WORK/sleep-call" ]]

# A device-env that resolves nothing must not reach real suspend.
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' >"$WORK/device-env"
chmod +x "$WORK/device-env"
printf '[s2idle] deep\n' >"$WORK/mem_sleep"
dispatch s2idle
grep -x 'sleep' "$WORK/fake-sleep-call" >/dev/null
[[ ! -e "$WORK/sleep-call" ]]

echo "Armada Control settings tests passed"
