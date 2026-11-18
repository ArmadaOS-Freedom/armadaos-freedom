#!/usr/bin/env bash
#
# TB323FU input integration regression for the common Armada image.
#
# Source and build-contract checks only. This file verifies the wiring recorded
# in the repository (Containerfile COPY entries, build steps, udev rule text and
# the Steam session default). These checks complement image and device testing;
# no udev, systemd, GPIO, session bootstrap or device is executed here.
#
# The behavioral USB routing tests live in
# devices/lenovo-tb323fu/tests/test_tb323fu_usb_port.py and are run as-is.

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINERFILE="$ROOT/Containerfile"
BASE_PACKAGES="$ROOT/build_files/10-base-packages.sh"
VENDOR_FILES="$ROOT/build_files/40-vendor-system-files.sh"
STEAM_SESSION="$ROOT/system_files/usr/share/gamescope-session-plus/sessions.d/steam"
TOUCH_RULE="$ROOT/system_files/usr/lib/udev/rules.d/99-armada-tb323fu-virtual-touch.rules"
USB_TEST="$ROOT/devices/lenovo-tb323fu/tests/test_tb323fu_usb_port.py"

TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

assert_eq() {
    local actual="$1" expected="$2" label="$3"

    if [[ "$actual" != "$expected" ]]; then
        printf 'FAIL: %s: expected %q, got %q\n' "$label" "$expected" "$actual" >&2
        exit 1
    fi
}

assert_file() {
    [[ -f "$1" ]] || fail "missing file: $1"
}

assert_line() {
    grep -Fq -- "$2" "$1" || fail "$1 does not contain: $2"
}

# Destination of the first Containerfile COPY whose source argument is $1.
copy_dest() {
    awk -v src="$1" '
        $1 == "COPY" {
            for (i = 2; i < NF; i++) {
                if ($i == src) { print $NF; exit }
            }
        }' "$CONTAINERFILE"
}

# --- common image wiring: the four accepted USB components -------------------
# These were dropped when the device rootfs stopped being copied wholesale.
usb_copies=(
    "devices/lenovo-tb323fu/rootfs/usr/libexec/tb323fu/tb323fu-usb-port /system_files/usr/libexec/tb323fu/tb323fu-usb-port"
    "devices/lenovo-tb323fu/rootfs/etc/udev/rules.d/71-tb323fu-usb-host.rules /system_files/usr/lib/udev/rules.d/71-tb323fu-usb-host.rules"
    "devices/lenovo-tb323fu/kernel/tb323fu-linux-11a25bf8/tb323fu-linux-11a25bf89b65fff48112a91a0b25bc247e7736d8/userspace/platform/udev/70-tb323fu-usb-port.rules /system_files/usr/lib/udev/rules.d/70-tb323fu-usb-port.rules"
    "devices/lenovo-tb323fu/kernel/tb323fu-linux-11a25bf8/tb323fu-linux-11a25bf89b65fff48112a91a0b25bc247e7736d8/userspace/platform/systemd/system/tb323fu-usb-port.service /system_files/usr/lib/systemd/system/tb323fu-usb-port.service"
)

for entry in "${usb_copies[@]}"; do
    read -r src dst <<<"$entry"
    assert_file "$ROOT/$src"
    assert_eq "$(copy_dest "$src")" "$dst" "Containerfile COPY destination for $src"
done

# The helper is made executable after the COPY and the unit is enabled, and the
# helper's gpiod tools must stay installed (weak deps are disabled).
assert_line "$VENDOR_FILES" 'chmod 0755 /usr/libexec/tb323fu/tb323fu-usb-port'
assert_line "$VENDOR_FILES" 'systemctl enable tb323fu-usb-port.service'
assert_line "$BASE_PACKAGES" 'libgpiod-utils'

# system_files is installed wholesale, so a rule placed there ships in the image.
assert_line "$CONTAINERFILE" 'COPY system_files /system_files/'
assert_line "$VENDOR_FILES" 'cp -a /ctx/system_files/. /'
assert_file "$TOUCH_RULE"

# --- virtual NVT touch rule: configuration contract -------------------------
# Text checks only: they prove the rule restricts itself to the virtual event
# node of the exact board, not that a real udev/device accepts it.
assert_line "$TOUCH_RULE" 'ACTION!="add|change"'
assert_line "$TOUCH_RULE" 'SUBSYSTEM!="input"'
assert_line "$TOUCH_RULE" 'KERNEL!="event*"'
assert_line "$TOUCH_RULE" 'DEVPATH!="/devices/virtual/input/input*/event*"'
assert_line "$TOUCH_RULE" 'ATTRS{name}!="NVTCapacitiveTouchScreen"'
assert_line "$TOUCH_RULE" 'ATTRS{id/bustype}!="001c"'
assert_line "$TOUCH_RULE" 'grep -azFxq lenovo,baldur /sys/firmware/devicetree/base/compatible'
assert_line "$TOUCH_RULE" 'ENV{LIBINPUT_CALIBRATION_MATRIX}="0 1 0 -1 0 1"'

# --- Steam session orientation defaults -------------------------------------
# Run the session file's real preamble (everything before the first
# "source /usr/lib/armada/storage-lib") in a clean environment. The rest of the
# script, including storage-lib and bootstrap actions, is never evaluated.
BASH_BIN="$(command -v bash)"
preamble="$TEST_ROOT/steam-preamble.sh"
grep -Fqx -- 'source /usr/lib/armada/storage-lib' "$STEAM_SESSION" \
    || fail 'Steam session is missing expected storage-lib source line'
awk '/^source \/usr\/lib\/armada\/storage-lib/ { exit } { print }' "$STEAM_SESSION" > "$preamble"
assert_file "$preamble"
[[ -s "$preamble" ]] || fail 'extracted Steam preamble is empty'
assert_line "$preamble" 'ARMADA_PANEL_ORIENTATION'

session_orientation() {
    local device="$1" orientation="${2-}" panel="${3-}"
    local -a cmd=(env -i "ARMADA_DEVICE_ID=$device")

    [[ -z "$orientation" ]] || cmd+=("ORIENTATION=$orientation")
    [[ -z "$panel" ]] || cmd+=("ARMADA_PANEL_ORIENTATION=$panel")
    "${cmd[@]}" "$BASH_BIN" --noprofile --norc -c \
        'source "$1"; printf "%s" "${ORIENTATION-<unset>}"' _ "$preamble"
}

assert_eq "$(session_orientation lenovo-tb323fu)" right 'TB323FU default orientation'
assert_eq "$(session_orientation lenovo-tb323fu left)" left 'explicit ORIENTATION retained'
assert_eq "$(session_orientation lenovo-tb323fu '' left)" left 'ARMADA_PANEL_ORIENTATION honored'
assert_eq "$(session_orientation lenovo-tb322fc)" right 'TB322FC default orientation'
assert_eq "$(session_orientation unknown-device)" '<unset>' 'unknown device leaves orientation unset'

# --- existing USB routing behavior ------------------------------------------
command -v python3 >/dev/null || fail 'python3 is required'
assert_file "$USB_TEST"
python3 "$USB_TEST"

printf 'TB323FU input integration checks passed\n'
