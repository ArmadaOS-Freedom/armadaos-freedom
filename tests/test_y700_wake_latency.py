# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ArmadaOS Freedom contributors

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices/lenovo-tb323fu/diagnostics"))
import y700_wake_latency as wake


class WakeLatencyTests(unittest.TestCase):
    def test_identifies_internal_panel_and_power_key(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_sys = Path(directory) / "sys"
            fake_dev = Path(directory) / "dev"
            entries = {
                "power/mem_sleep": "[s2idle] deep",
                "class/input/event0/device/name": "pmic_pwrkey",
                "class/input/event1/device/name": "controller",
                "class/drm/card1-DSI-1/status": "connected",
                "class/drm/card1-DSI-2/status": "disconnected",
                "class/drm/card1-HDMI-1/status": "connected",
                "class/backlight/aw99706-backlight/actual_brightness": "154",
            }
            for relative, value in entries.items():
                path = fake_sys / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="ascii")
            self.assertEqual(wake.find_hardware(fake_sys, fake_dev), (
                fake_dev / "input/event0",
                fake_sys / "class/drm/card1-DSI-1",
                fake_sys / "class/backlight/aw99706-backlight",
            ))
            (fake_sys / "power/mem_sleep").write_text("s2idle [deep]", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "select s2idle"):
                wake.find_hardware(fake_sys, fake_dev)

    def test_only_counts_power_key_down_events(self):
        def packet(code, value, us):
            return wake.INPUT_EVENT.pack(3, us, wake.EV_KEY, code, value)

        records = packet(wake.KEY_POWER, 1, 5) + packet(wake.KEY_POWER, 0, 69) + packet(3, 1, 99)
        self.assertEqual(wake.power_presses(records), [3_000_005_000])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            wake.power_presses(records[:-1])

    @unittest.skipUnless(hasattr(time, "CLOCK_MONOTONIC"), "Linux evdev clock IDs required")
    def test_resumed_reader_ignores_old_key_event_and_records_the_first_screen_sample(self):
        data = wake.INPUT_EVENT.pack(1, 100_000, wake.EV_KEY, wake.KEY_POWER, 1)
        data += wake.INPUT_EVENT.pack(1, 350_000, wake.EV_KEY, wake.KEY_POWER, 1)
        clock = [(1_000_000_000, 200_000_000), (1_200_000_000, 200_000_000),
                 (1_400_000_000, 400_000_000), (1_500_000_000, 400_000_000)]
        readable = [([], [], []), ([7], [], []), ([], [], [])]
        with (patch.dict(sys.modules, {"fcntl": MagicMock()}),
              patch.object(wake.os, "open", return_value=7), patch.object(wake.os, "close"),
              patch.object(wake.os, "read", return_value=data),
              patch.object(wake.select, "select", side_effect=readable),
              patch.object(wake, "clock_pair", side_effect=clock),
              patch.object(wake, "panel_status", return_value=(True, True)),
              patch.object(wake.time, "monotonic_ns", side_effect=[1_410_000_000, 1_510_000_000, 1_520_000_000])):
            result = wake.record("key", "panel", "light", 5, 0.05)
        wake.summarize(result)
        self.assertEqual(result["milestones_ns"]["wake_key_event"], 1_350_000_000)
        self.assertEqual(result["offsets_ms"]["screen_ready"], 60.0)
        self.assertTrue(result["screen_ready_at_first_poll"])

    @unittest.skipUnless(hasattr(time, "CLOCK_MONOTONIC"), "Linux evdev clock IDs required")
    def test_times_out_while_awake_without_reporting_latency(self):
        with (patch.dict(sys.modules, {"fcntl": MagicMock()}),
              patch.object(wake.os, "open", return_value=7), patch.object(wake.os, "close") as close,
              patch.object(wake.select, "select", return_value=([], [], [])),
              patch.object(wake, "clock_pair", side_effect=[(0, 1_000), (2_000_000_000, 1_000)]),
              patch.object(wake, "panel_status") as panel_status):
            result = wake.record("key", "panel", "light", 1, 1)
        wake.summarize(result)
        self.assertNotIn("reference", result)
        self.assertFalse(result["milestones_ns"])
        panel_status.assert_not_called()
        close.assert_called_once_with(7)

    def test_falls_back_to_first_post_resume_sample_without_a_wake_event(self):
        result = {"milestones_ns": {"first_sample_after_resume": 1_000_000_000, "screen_ready": 1_025_000_000}}
        wake.summarize(result)
        self.assertEqual(result["reference"], "first_sample_after_resume")
        self.assertEqual(result["offsets_ms"]["screen_ready"], 25.0)

    def test_journal_uses_receipt_time_and_ignores_unrelated_records(self):
        def row(message, stamp, transport="kernel"):
            return json.dumps({"MESSAGE": message, "__MONOTONIC_TIMESTAMP": stamp,
                               "_SOURCE_MONOTONIC_TIMESTAMP": 55, "_TRANSPORT": transport})

        result = {"started_ns": 10_000, "finished_ns": 30_000, "milestones_ns": {}}
        journal = "\n".join([
            "malformed journal line",
            row("PM: suspend entry (s2idle)", 7),
            row("PM: suspend entry (s2idle)", 11),
            row("state is now CONNECTED_GLOBAL", 13, "syslog"),
            row("PM: suspend exit", 22, "stdout"),
            row("PM: suspend exit", 29),
            row("PM: suspend exit", 45),
        ])
        wake.add_journal_markers(result, journal)
        self.assertEqual(result["milestones_ns"], {"s2idle_entry_logged": 11_000, "suspend_exit_logged": 29_000})


if __name__ == "__main__":
    unittest.main()
