#!/usr/bin/env bash
# Uses a fake evtest producer; no systemd or hardware required.

set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${ARMADA_POWERBUTTON_TEST_FIXTURE:-}" == 1 ]]; then
    tmp="$ARMADA_POWERBUTTON_TEST_TMP"
    source "$ROOT/system_files/usr/libexec/armada/powerbuttond"

    log() { :; }
    power_device() { printf '/dev/input/fake\n'; }
    armada_find_lid_dev() { return 1; }
    armada_lid_closed() { return 1; }
    steam_uri() { printf '%s\n' "$1" >"$tmp/fresh-consumed"; }
    gamescope_sockets() { printf 'gamescope-0\n'; }
    gamescope_screen() { printf '%s %s\n' "$1" "$2" >>"$tmp/screen-calls"; }
    evtest() {
        exec 9<>"$tmp/block"
        if [[ ! -e "$tmp/old-started" ]]; then
            printf '%s\n' "$BASHPID" >"$tmp/old-producer-pid"
            touch "$tmp/old-started"
            printf 'old-event\n'
        else
            printf '%s\n' "$BASHPID" >"$tmp/fresh-producer-pid"
            touch "$tmp/fresh-started"
            printf 'Event: (KEY_POWER), value 1\n'
            printf 'Event: (KEY_POWER), value 0\n'
        fi
        IFS= read -r -u 9 _
    }

    powerbutton_main
    exit $?
fi

tmp="$(mktemp -d)"
daemon_pid=
cleanup() {
    [[ -n "$daemon_pid" ]] && kill "$daemon_pid" 2>/dev/null || true
    [[ -n "$daemon_pid" ]] && wait "$daemon_pid" 2>/dev/null || true
    rm -rf -- "$tmp"
}
trap cleanup EXIT
mkfifo "$tmp/block"

wait_for_file() {
    local file=$1
    local attempts=100
    while [[ ! -e "$file" ]] && (( attempts > 0 )); do
        sleep 0.01
        attempts=$((attempts - 1))
    done
    [[ -e "$file" ]]
}

ARMADA_POWERBUTTON_TEST_FIXTURE=1 \
ARMADA_POWERBUTTON_TEST_TMP="$tmp" \
ARMADA_POWERBUTTON_PID_FILE="$tmp/daemon-pid" \
    bash "$0" &
daemon_pid=$!

wait_for_file "$tmp/daemon-pid"
wait_for_file "$tmp/old-started"
[[ "$(<"$tmp/daemon-pid")" == "$daemon_pid" ]]
old_producer_pid="$(<"$tmp/old-producer-pid")"

kill -USR1 "$daemon_pid"

wait_for_file "$tmp/fresh-started"
wait_for_file "$tmp/screen-calls"
[[ "$(<"$tmp/daemon-pid")" == "$daemon_pid" ]]
# A short press only toggles the display; Steam's sleep URI must not be sent.
[[ ! -e "$tmp/fresh-consumed" ]]
grep -Fxq 'gamescope-0 1' "$tmp/screen-calls"
fresh_producer_pid="$(<"$tmp/fresh-producer-pid")"
[[ "$fresh_producer_pid" != "$old_producer_pid" ]]
if kill -0 "$old_producer_pid" 2>/dev/null; then
    printf 'old power-button stream survived resume\n' >&2
    exit 1
fi

kill "$daemon_pid"
wait "$daemon_pid" 2>/dev/null || true
daemon_pid=

# Standalone defaults must agree with the root hook's runtime-directory path.
(
    unset ARMADA_POWERBUTTON_PID_FILE ARMADA_LID_CLOSE_MARKER ARMADA_USER_RUNTIME_DIR
    XDG_RUNTIME_DIR="$tmp/default-runtime"
    source "$ROOT/system_files/usr/libexec/armada/powerbuttond"
    [[ "$PID_FILE" == "$tmp/default-runtime/armada-powerbuttond/pid" ]]
    [[ "$LID_CLOSE_MARKER" == "$tmp/default-runtime/armada-powerbuttond/lid-close-ms" ]]
)

# Killing a pipeline parent does not terminate its children.
source "$ROOT/system_files/usr/libexec/armada/powerbuttond"
LID_CLOSE_MARKER="$tmp/lid-close-ms"
powerbutton_mark_lid_close
[[ "$(<"$LID_CLOSE_MARKER")" =~ ^[0-9]+$ ]]
rm -f -- "$LID_CLOSE_MARKER"
(
    sleep 100 | while read -r _; do :; done
) &
lidpid=$!
lid_children=()
attempts=100
while (( attempts > 0 )); do
    mapfile -t lid_children < <(pgrep -P "$lidpid" || true)
    ((${#lid_children[@]} > 0)) && break
    sleep 0.01
    attempts=$((attempts - 1))
done
((${#lid_children[@]} > 0))
lid_parent_pid=$lidpid
powerbutton_stop_watchers
wait_for_pid_exit() {
    local pid=$1 attempts=100
    while kill -0 "$pid" 2>/dev/null && (( attempts > 0 )); do
        sleep 0.01
        attempts=$((attempts - 1))
    done
    ! kill -0 "$pid" 2>/dev/null
}
if ! wait_for_pid_exit "$lid_parent_pid"; then
    printf 'lid watcher parent survived cleanup\n' >&2
    exit 1
fi
for child_pid in "${lid_children[@]}"; do
    if ! wait_for_pid_exit "$child_pid"; then
        printf 'lid watcher child survived cleanup: %s\n' "$child_pid" >&2
        exit 1
    fi
done

# Check the sleep opt-in without touching Steam, systemd or the device clock.
(
    SHORT_ACTION=sleep
    last_suspend_offset_ms=3100
    resume_block_until_ms=0
    press_log="$tmp/opt-in-actions"
    powerbutton_clock_pair() { printf '%s %s\n' "$sample_ms" "$sample_offset_ms"; }
    log() { :; }
    steam_uri() { printf '%s\n' "$1" >>"$press_log"; }
    powerbutton_screen_toggle() { printf 'display\n' >>"$press_log"; }

    sample_ms=5000 sample_offset_ms=3100
    powerbutton_short_press
    grep -Fxq shortpowerpress "$press_log"
    rm -- "$press_log"

    sample_ms=7000 sample_offset_ms=4100
    powerbutton_ignore_resume_press
    [[ "$resume_block_until_ms" = 8000 ]]
    sample_ms=7500
    powerbutton_short_press
    [[ ! -e "$press_log" ]]
    sample_ms=8000
    powerbutton_short_press
    grep -Fxq shortpowerpress "$press_log"

    # Exercise a deadline inherited from reexec with no clock jump.
    rm -- "$press_log"
    resume_block_until_ms=9500
    sample_ms=9000
    powerbutton_short_press
    [[ ! -e "$press_log" ]]
    SHORT_ACTION=display\n    powerbutton_short_press
    grep -Fxq display "$press_log"
)

printf 'powerbutton resume, cleanup and short-action test passed\n'
