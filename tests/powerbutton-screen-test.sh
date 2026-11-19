#!/usr/bin/env bash
# Short power press = display toggle. Simulated evtest events, fake gamescopectl
# and fake gamescope sockets; no hardware, no systemd, no real compositor.

set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
POWERBUTTOND="$ROOT/system_files/usr/libexec/armada/powerbuttond"
SERVICE="$ROOT/system_files/usr/lib/systemd/user/armada-powerbuttond.service"
POWERDEVILRC="$ROOT/system_files/etc/xdg/powerdevilrc"
LOGIND="$ROOT/system_files/usr/lib/systemd/logind.conf.d/armada-handheld.conf"

if [[ "${ARMADA_SCREEN_TEST_FIXTURE:-}" == 1 ]]; then
    # The daemon runs without errexit in production; keep the fixture faithful.
    set +e
    fixture_tmp="$ARMADA_SCREEN_TEST_TMP"
    source "$POWERBUTTOND"

    log() { printf '%s\n' "$*" >>"$fixture_tmp/log"; }
    power_device() { printf '/dev/input/fake\n'; }
    armada_find_lid_dev() { return 1; }
    armada_lid_closed() { return 1; }
    steam_uri() { printf '%s\n' "$1" >>"$fixture_tmp/steam-uris"; }
    # Builtins only: no child may survive powerbutton_stop_watchers and keep
    # reading events after a reexec.
    evtest() {
        local event_line
        exec 8<"$ARMADA_SCREEN_TEST_EVENTS"
        while IFS= read -r -u 8 event_line; do
            printf '%s\n' "$event_line"
        done
    }

    powerbutton_main
    exit $?
fi

tmp="$(mktemp -d)"
daemon_pid=
CASE=
cleanup() {
    exec 7>&- 2>/dev/null || true
    if [[ -n "$daemon_pid" ]]; then
        kill "$daemon_pid" 2>/dev/null || true
        wait "$daemon_pid" 2>/dev/null || true
    fi
    rm -rf -- "$tmp"
}
trap cleanup EXIT

# Records the display variables gamescopectl would have used, then fails or
# hangs for sockets the current case marks in $FAIL_DIR/$SLOW_DIR.
mkdir -p "$tmp/bin"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "%s %s %s %s\n" "${WAYLAND_DISPLAY-unset}" "${GAMESCOPE_WAYLAND_DISPLAY-unset}" "${XDG_RUNTIME_DIR-unset}" "$*" >>"$CALL_LOG"' \
    'if [[ -e "$FAIL_DIR/${WAYLAND_DISPLAY-unset}" ]]; then exit 1; fi' \
    'if [[ -e "$FAIL_STATE_DIR/${WAYLAND_DISPLAY-unset}-${2-unset}" ]]; then exit 1; fi' \
    'if [[ -e "$SLOW_DIR/${WAYLAND_DISPLAY-unset}" ]]; then sleep 30; fi' \
    'exit 0' \
    >"$tmp/bin/gamescopectl"
chmod +x "$tmp/bin/gamescopectl"

# A real display socket, two decoys with valid-looking names, the primary
# symlink alias, and a plain file that must not be treated as a socket.
make_sockets() {
    python3 - "$1" <<'PY'
import os, socket, sys
d = sys.argv[1]
for name in ("gamescope-0", "gamescope-1", "gamescope-0-ei", "gamescope-lock"):
    s = socket.socket(socket.AF_UNIX)
    s.bind(os.path.join(d, name))
    s.close()
os.symlink("gamescope-0", os.path.join(d, "gamescope-primary"))
open(os.path.join(d, "gamescope-4"), "w").close()
PY
}

wait_for_file() {
    local file="$1" attempts=200
    while [[ ! -e "$file" ]] && (( attempts > 0 )); do
        sleep 0.01
        attempts=$((attempts - 1))
    done
    [[ -e "$file" ]]
}

wait_for_calls() {
    local want="$1" attempts=500
    while (( attempts > 0 )); do
        (( $(wc -l <"$CASE/calls") >= want )) && return 0
        sleep 0.02
        attempts=$((attempts - 1))
    done
    return 1
}

# Bounded wait (<=5s) for an expected daemon log line, so a test never races
# the daemon: it polls until the line appears instead of sleeping a fixed time.
wait_for_log() {
    local pattern="$1" attempts=250
    while (( attempts > 0 )); do
        grep -qF -- "$pattern" "$CASE/log" && return 0
        sleep 0.02
        attempts=$((attempts - 1))
    done
    return 1
}

call_lines() { wc -l <"$CASE/calls"; }

expect_call() {
    local sock="$1" state="$2"
    if ! grep -Fxq "$sock $sock $CASE/runtime drm_sleep_internal_screen $state" "$CASE/calls"; then
        printf 'missing display request: %s -> %s\n' "$sock" "$state" >&2
        cat "$CASE/calls" >&2
        exit 1
    fi
}

start_case() {
    CASE="$tmp/$1"
    mkdir -p "$CASE/runtime" "$CASE/fail" "$CASE/failstate" "$CASE/slow"
    : >"$CASE/calls"
    : >"$CASE/steam-uris"
    : >"$CASE/log"
    make_sockets "$CASE/runtime"
    mkfifo "$CASE/events"
    env \
        PATH="$tmp/bin:$PATH" \
        ARMADA_SCREEN_TEST_FIXTURE=1 \
        ARMADA_SCREEN_TEST_TMP="$CASE" \
        ARMADA_SCREEN_TEST_EVENTS="$CASE/events" \
        ARMADA_POWERBUTTON_PID_FILE="$CASE/pid" \
        ARMADA_LID_CLOSE_MARKER="$CASE/lid-close-ms" \
        ARMADA_USER_RUNTIME_DIR="$CASE/runtime" \
        ARMADA_POWERBUTTON_LONG_MS=400 \
        ARMADA_POWERBUTTON_SCREEN_TIMEOUT_SEC=1 \
        CALL_LOG="$CASE/calls" \
        FAIL_DIR="$CASE/fail" \
        FAIL_STATE_DIR="$CASE/failstate" \
        SLOW_DIR="$CASE/slow" \
        bash "$0" &
    daemon_pid=$!
    # Read-write so writes never block and the daemon never sees a stray writer.
    exec 7<>"$CASE/events"
    wait_for_file "$CASE/pid"
}

# evtest prints "Event: (KEY_POWER), value N"; 0 is release, 2 is autorepeat.
send() { printf '%s\n' "$@" >&7; }

stop_case() {
    exec 7>&-
    wait "$daemon_pid" 2>/dev/null || true
    daemon_pid=
}

# --- two short cycles: off/on on every real socket, nothing else -----------
start_case short-cycles
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 4
sleep 0.2
stop_case
(( $(call_lines) == 4 )) || { printf 'unexpected display requests\n' >&2; cat "$CASE/calls" >&2; exit 1; }
expect_call gamescope-0 1
expect_call gamescope-1 1
expect_call gamescope-0 0
expect_call gamescope-1 0
# -ei input socket, lock file, primary alias and the plain file are skipped.
if grep -qvE '^gamescope-[01] ' "$CASE/calls"; then
    printf 'wrong socket used for a display request\n' >&2
    cat "$CASE/calls" >&2
    exit 1
fi
# A short press must never ask Steam to sleep.
[[ ! -s "$CASE/steam-uris" ]]

# --- long hold without a release: only Steam's power menu -----------------
start_case long-press
send 'Event: (KEY_POWER), value 1'
sleep 0.6
send 'Event: (KEY_POWER), value 2'
send 'Event: (KEY_POWER), value 2'
send 'Event: (KEY_POWER), value 0'
wait_for_file "$CASE/steam-uris"
sleep 0.2
[[ "$(wc -l <"$CASE/steam-uris")" == 1 ]]
[[ "$(<"$CASE/steam-uris")" == longpowerpress ]]
grep -q 'long press -> longpowerpress' "$CASE/log"
(( $(call_lines) == 0 )) || { printf 'long press touched the display\n' >&2; exit 1; }
stop_case
(( $(call_lines) == 0 ))

# --- a release while idle is ignored, then a full cycle still works -------
start_case orphan-release
send 'Event: (KEY_POWER), value 0'
sleep 0.1
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
sleep 0.2
(( $(call_lines) == 2 )) || { printf 'orphan release toggled the display\n' >&2; cat "$CASE/calls" >&2; exit 1; }
expect_call gamescope-0 1
expect_call gamescope-1 1
stop_case
(( $(call_lines) == 4 ))

# --- off failure: restore every socket that was asked, then retry off -----
start_case off-failure
touch "$CASE/failstate/gamescope-1-1"
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 4
sleep 0.2
# gamescope-1 refused off, but a refused request may still have blanked it, so
# its restore is sent as well.
(( $(call_lines) == 4 )) || { printf 'failed off was not fully rolled back\n' >&2; cat "$CASE/calls" >&2; exit 1; }
expect_call gamescope-0 1
expect_call gamescope-1 1
expect_call gamescope-0 0
expect_call gamescope-1 0
grep -q 'drm_sleep_internal_screen 1 failed on gamescope-1' "$CASE/log"
grep -q 'display off failed; displays restored' "$CASE/log"
if grep -q 'display stays on' "$CASE/log"; then
    printf 'off failure claimed the display stays on\n' >&2
    exit 1
fi
[[ ! -s "$CASE/steam-uris" ]]

# The restore was confirmed, so the daemon still believes the display is on.
rm -f "$CASE/failstate/gamescope-1-1"
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 6
sleep 0.2
(( $(call_lines) == 6 )) || { printf 'retry did not turn the display off\n' >&2; cat "$CASE/calls" >&2; exit 1; }
[[ "$(tail -n 1 "$CASE/calls")" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 1" ]]
stop_case
(( $(call_lines) == 8 ))

# --- refused off plus refused restore: keep the off state ----------------
start_case rollback-failure
touch "$CASE/failstate/gamescope-1-1" "$CASE/failstate/gamescope-1-0"
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 4
sleep 0.2
(( $(call_lines) == 4 )) || { printf 'rollback did not run\n' >&2; cat "$CASE/calls" >&2; exit 1; }
grep -q 'failed to restore display gamescope-1; keeping display off for retry' "$CASE/log"
grep -q 'display off failed; restore unconfirmed, next press retries on' "$CASE/log"
[[ ! -s "$CASE/steam-uris" ]]

# That panel may still be dark, so the next press asks for on, not off again.
rm -f "$CASE/failstate/gamescope-1-1" "$CASE/failstate/gamescope-1-0"
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 6
sleep 0.2
(( $(call_lines) == 6 )) || { printf 'retained off state was not retried\n' >&2; cat "$CASE/calls" >&2; exit 1; }
expect_call gamescope-0 0
expect_call gamescope-1 0
[[ "$(tail -n 1 "$CASE/calls")" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 0" ]]
stop_case
(( $(call_lines) == 6 ))

# --- a gamescope that never answers is a failure, not an endless wait -----
start_case on-timeout
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
touch "$CASE/slow/gamescope-1"
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 4
wait_for_log 'drm_sleep_internal_screen 0 failed on gamescope-1' \
    || { printf 'timed-out on was not reported\n' >&2; cat "$CASE/calls" >&2; exit 1; }
(( $(call_lines) == 4 )) || { printf 'timed-out on was not reported\n' >&2; cat "$CASE/calls" >&2; exit 1; }
grep -q 'display on failed; keeping display off for retry' "$CASE/log"
rm -f "$CASE/slow/gamescope-1"
# The retained off state is retried by the cleanup path.
stop_case
(( $(call_lines) == 6 )) || { printf 'cleanup did not retry the display on\n' >&2; cat "$CASE/calls" >&2; exit 1; }
[[ "$(tail -n 1 "$CASE/calls")" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 0" ]]

# --- exit restores the display before the daemon goes away ---------------
start_case exit-restore
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
stop_case
(( $(call_lines) == 4 )) || { printf 'exit did not restore the display\n' >&2; cat "$CASE/calls" >&2; exit 1; }
mapfile -t exit_calls <"$CASE/calls"
[[ "${exit_calls[2]}" == "gamescope-0 gamescope-0 $CASE/runtime drm_sleep_internal_screen 0" ]]
[[ "${exit_calls[3]}" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 0" ]]

# --- restart restores the display, and launching never blanks it ---------
start_case restart-restore
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
kill -USR1 "$daemon_pid"
wait_for_calls 4
sleep 0.3
(( $(call_lines) == 4 )) || { printf 'restart restored the display more than once\n' >&2; cat "$CASE/calls" >&2; exit 1; }
mapfile -t restart_calls <"$CASE/calls"
[[ "${restart_calls[2]}" == "gamescope-0 gamescope-0 $CASE/runtime drm_sleep_internal_screen 0" ]]
[[ "${restart_calls[3]}" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 0" ]]
[[ "$(<"$CASE/pid")" == "$daemon_pid" ]]
# The reexecuted daemon owns no off state but still toggles.
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 6
sleep 0.2
(( $(call_lines) == 6 )) || { printf 'restarted daemon stopped toggling\n' >&2; cat "$CASE/calls" >&2; exit 1; }
stop_case
(( $(call_lines) == 8 ))

# --- a failed restore keeps the daemon, its off state, and its watchers ---
start_case reexec-restore-fail
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 2
touch "$CASE/slow/gamescope-1"
kill -USR1 "$daemon_pid"
wait_for_calls 4
wait_for_log 'display restore failed before reexec; keeping daemon and off state' \
    || { printf 'reexec did not report the failed restore\n' >&2; cat "$CASE/calls" >&2; exit 1; }
(( $(call_lines) == 4 )) || { printf 'reexec did not try to restore\n' >&2; cat "$CASE/calls" >&2; exit 1; }
# Watchers are only stopped once the restore succeeded, and no exec happened.
(( $(grep -c 'watching /dev/input/fake' "$CASE/log") == 1 )) || { printf 'daemon reexecuted after a failed restore\n' >&2; exit 1; }
kill -0 "$daemon_pid"
rm -f "$CASE/slow/gamescope-1"
# The daemon still owns the off state: the next press turns the display on.
send 'Event: (KEY_POWER), value 1'
sleep 0.15
send 'Event: (KEY_POWER), value 0'
wait_for_calls 6
sleep 0.2
(( $(call_lines) == 6 )) || { printf 'surviving daemon lost the off state\n' >&2; cat "$CASE/calls" >&2; exit 1; }
[[ "$(tail -n 1 "$CASE/calls")" == "gamescope-1 gamescope-1 $CASE/runtime drm_sleep_internal_screen 0" ]]
stop_case
(( $(call_lines) == 6 ))

# --- packaged KDE defaults for both desktop shells -----------------------
group_has() {
    awk -v group="$1" -v key="$2" '
        /^\[/ { in_group = ($0 == group); next }
        in_group && $0 == key { found = 1 }
        END { exit !found }
    ' "$POWERDEVILRC"
}
group_has '[AC][SuspendAndShutdown]' 'PowerButtonAction=128'
group_has '[Battery][SuspendAndShutdown]' 'PowerButtonAction=128'
group_has '[LowBattery][SuspendAndShutdown]' 'PowerButtonAction=128'
(( $(grep -c '^PowerButtonAction=' "$POWERDEVILRC") == 3 ))
# Only real setting lines matter: commented-out examples are not immutable.
if grep -vE '^[[:space:]]*([#;]|$)' "$POWERDEVILRC" | grep -qF '[$i]'; then
    printf 'powerdevilrc must not mark keys immutable\n' >&2
    exit 1
fi
if grep -qE '^(PowerDownAction|LidAction|AutoSuspendAction)=' "$POWERDEVILRC"; then
    printf 'powerdevilrc must not change other power actions\n' >&2
    exit 1
fi

# --- logind hands the power key to the graphical session ----------------
grep -Fxq 'HandlePowerKey=ignore' "$LOGIND"
grep -Fxq 'HandleSuspendKey=suspend' "$LOGIND"
grep -Fxq 'HandleHibernateKey=ignore' "$LOGIND"
grep -Fxq 'HandleLidSwitch=suspend' "$LOGIND"
grep -Fxq 'HandleLidSwitchExternalPower=suspend' "$LOGIND"
grep -Fxq 'HandleLidSwitchDocked=ignore' "$LOGIND"

# --- the daemon itself: display-only toggle, no sleep fallback ----------
grep -Fq 'powerbutton_screen_toggle' "$POWERBUTTOND"
# Steam sleep is requested on lid close or through the short-action opt-in.
(( $(grep -c 'steam_uri shortpowerpress' "$POWERBUTTOND") == 2 ))
if grep -Fq 'bl_power' "$POWERBUTTOND"; then
    printf 'the button handler must not poke backlight sysfs\n' >&2
    exit 1
fi
grep -Fxq 'Restart=on-failure' "$SERVICE"

bash -n "$POWERBUTTOND"
printf 'power-button display toggle tests passed\n'
