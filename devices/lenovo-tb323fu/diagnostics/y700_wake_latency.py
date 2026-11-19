#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ArmadaOS Freedom contributors
"""Record software-visible wake milestones for one TB323FU s2idle cycle."""

import argparse
import json
import math
import os
import select
import struct
import subprocess
import sys
import time
from pathlib import Path


INPUT_EVENT = struct.Struct("@llHHi")
EVIOCSCLOCKID = 0x400445A0
EV_KEY = 1
KEY_POWER = 116
MIN_SUSPEND_NS = 100_000_000
JOURNAL_MARKERS = {
    "PM: suspend entry (s2idle)": "s2idle_entry_logged",
    "PM: suspend exit": "suspend_exit_logged",
}


def read_value(path):
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def find_hardware(sys_root=Path("/sys"), dev_root=Path("/dev")):
    modes = (read_value(sys_root / "power/mem_sleep") or "").split()
    if "[s2idle]" not in modes:
        raise RuntimeError("select s2idle before starting this diagnostic")
    inputs = sorted((sys_root / "class/input").glob("event*"))
    keys = [entry for entry in inputs if read_value(entry / "device/name") == "pmic_pwrkey"]
    panels = [panel for panel in sorted((sys_root / "class/drm").glob("card*-DSI-*"))
              if read_value(panel / "status") == "connected"]
    lights = sorted((sys_root / "class/backlight").glob("*"))
    if len(keys) != 1 or len(panels) != 1 or len(lights) != 1:
        raise RuntimeError("could not uniquely identify the TB323FU power key, internal DSI panel and backlight")
    return dev_root / "input" / keys[0].name, panels[0], lights[0]


def clock_pair():
    # Bracket the boottime read so preemption does not look like a suspend.
    for _ in range(3):
        before = time.monotonic_ns()
        boottime = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        after = time.monotonic_ns()
        if after - before <= 5_000_000:
            return after, boottime - (before + after) // 2
    raise RuntimeError("could not sample the system clocks within 5 ms")


def power_presses(data):
    if len(data) % INPUT_EVENT.size:
        raise RuntimeError("incomplete power-key input event")
    return [seconds * 1_000_000_000 + micros * 1000
            for seconds, micros, kind, code, value in INPUT_EVENT.iter_unpack(data)
            if (kind, code, value) == (EV_KEY, KEY_POWER, 1)]


def panel_status(panel, light):
    drm_on = read_value(panel / "dpms") == "On" and read_value(panel / "enabled") == "enabled"
    brightness = read_value(light / "actual_brightness") or "0"
    backlight_on = read_value(light / "bl_power") == "0" and brightness.isdigit() and int(brightness) > 0
    return drm_on, backlight_on


def record(key, panel, light, timeout, after_wake):
    import fcntl

    fd = os.open(key, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
        started_ns, initial_offset = clock_pair()
        last_awake_ns = started_ns
        result = {"started_unix": int(time.time()), "started_ns": started_ns, "milestones_ns": {}}
        marks = result["milestones_ns"]
        print("Ready. Enter s2idle from the Steam power menu, then press the power key to wake.", flush=True)

        while True:
            resumed = "first_sample_after_resume" in marks
            ready, _, _ = select.select([fd], [], [], 0.04 if resumed else 0.25)
            now, offset = clock_pair()

            if not resumed and offset - initial_offset >= MIN_SUSPEND_NS:
                marks["first_sample_after_resume"] = now
                result["suspended_seconds"] = round((offset - initial_offset) / 1e9, 3)
                resumed = True

            if ready:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    data = None
                if data == b"":
                    raise RuntimeError("power-key input device disconnected")
                if data and resumed and "wake_key_event" not in marks:
                    wake_press = next((stamp for stamp in power_presses(data)
                                       if last_awake_ns <= stamp <= now), None)
                    if wake_press is not None:
                        marks["wake_key_event"] = wake_press

            if not resumed:
                if (now - started_ns) / 1e9 >= timeout:
                    break
                last_awake_ns = now
                continue

            drm_on, backlight_on = panel_status(panel, light)
            observed_ns = time.monotonic_ns()
            if drm_on:
                marks.setdefault("drm_ready", observed_ns)
            if backlight_on:
                marks.setdefault("backlight_ready", observed_ns)
            if drm_on and backlight_on and "screen_ready" not in marks:
                marks["screen_ready"] = observed_ns
                result["screen_ready_at_first_poll"] = now == marks["first_sample_after_resume"]
            if (now - marks["first_sample_after_resume"]) / 1e9 >= after_wake:
                break
        result["finished_ns"] = time.monotonic_ns()
        return result
    finally:
        os.close(fd)


def add_journal_markers(result, journal):
    marks = result["milestones_ns"]
    for line in journal.splitlines():
        try:
            entry = json.loads(line)
            # Kernel printk's source clock can differ from evdev's CLOCK_MONOTONIC.
            stamp = int(entry["__MONOTONIC_TIMESTAMP"]) * 1000
        except (KeyError, TypeError, ValueError):
            continue
        if entry.get("_TRANSPORT") != "kernel" or not result["started_ns"] <= stamp <= result["finished_ns"]:
            continue
        name = JOURNAL_MARKERS.get(str(entry.get("MESSAGE", "")).strip())
        if name is not None:
            marks.setdefault(name, stamp)


def collect_journal(result):
    try:
        journal = subprocess.run(
            ["journalctl", "-b", "-k", "--since", f"@{result['started_unix']}", "--no-pager",
             "-o", "json", "--grep", "PM: suspend (entry|exit)"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        result["journal_error"] = str(exc)
        return
    add_journal_markers(result, journal)


def summarize(result):
    marks = result["milestones_ns"]
    if "first_sample_after_resume" not in marks:
        return
    reference = "wake_key_event" if "wake_key_event" in marks else "first_sample_after_resume"
    result["reference"] = reference
    result["offsets_ms"] = {
        name: round((stamp - marks[reference]) / 1e6, 1)
        for name, stamp in marks.items() if name != "s2idle_entry_logged"
    }


def positive_seconds(text):
    seconds = float(text)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("expected a finite positive number of seconds")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=positive_seconds, default=180,
                        help="awake seconds to wait for the first suspend (default: 180)")
    parser.add_argument("--after-wake", type=positive_seconds, default=15,
                        help="seconds to observe after resume (default: 15)")
    parser.add_argument("--output", type=Path, default=Path.home() / ".local/state/armada-wake-latency/latest.json",
                        help="path to the JSON report")
    args = parser.parse_args(argv)

    try:
        result = record(*find_hardware(), args.timeout, args.after_wake)
        collect_journal(result)
        summarize(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        print(f"Wake measurement failed: {exc}", file=sys.stderr)
        return 1

    if "reference" not in result:
        print("No suspend and resume observed before the timeout.")
    else:
        reference = "evdev wake-key timestamp" if result["reference"] == "wake_key_event" else "first post-resume sample"
        print(f"Milliseconds relative to the {reference}:")
        for name, value in result["offsets_ms"].items():
            print(f"  {name}: {value:+.1f}")
        if result.get("screen_ready_at_first_poll"):
            print("The display and backlight already reported ready at the first post-resume sample.")
        elif "screen_ready" not in result["milestones_ns"]:
            print("The display and backlight did not both report ready during capture.")
        print("Software timestamps cannot capture the entire physical key-to-visible-screen interval.")
    print(f"Report saved to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
