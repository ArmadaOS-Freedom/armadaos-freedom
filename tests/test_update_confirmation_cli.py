# SPDX-License-Identifier: GPL-3.0-only
"""CLI boundary tests for the trial confirmation service entrypoint."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVICE_UNIT = (
    ROOT / "system_files/usr/lib/systemd/system/armada-update-confirm.service"
)
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import trial_confirmation as tc


def _unit_directives(key: str) -> list[str]:
    values = []
    for raw in SERVICE_UNIT.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(key + "="):
            values.append(line.split("=", 1)[1])
    return values


class ConfirmationCliTest(unittest.TestCase):
    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = tc.main()
        return code, out.getvalue(), err.getvalue()

    def test_confirmed_reports_mirrored_boot_policy(self) -> None:
        payload = {
            "state": "confirmed",
            "sequence": 2,
            "transactionId": "11111111-2222-3333-4444-555555555555",
            "bootSlot": "b",
            "targetSlot": "b",
            "deployment": "armada/" + "a" * 64 + ".1",
            "bootPolicy": "dual-boot-no-slot-control",
            "slotControlState": "not_issued",
        }
        with mock.patch.object(tc, "confirm_trial", return_value=dict(payload)):
            code, out, err = self._run_main(["trial_confirmation"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), payload)

    def test_not_pending_is_zero_without_invented_success(self) -> None:
        payload = {"state": "not_pending", "recordState": None}
        with mock.patch.object(tc, "confirm_trial", return_value=dict(payload)):
            code, out, err = self._run_main(["trial_confirmation"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), payload)

    def test_expected_error_reports_stderr_only(self) -> None:
        for exc in (
            OSError("device gone"),
            tc.ConfirmationError("update lock is busy"),
            RuntimeError("generic runtime failure"),
            ValueError("bad value"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(tc, "confirm_trial", side_effect=exc):
                    code, out, err = self._run_main(["trial_confirmation"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                failure = json.loads(err.strip().splitlines()[-1])
                self.assertEqual(failure["state"], "confirmation_failed")
                self.assertIn(type(exc).__name__, failure["error"])

    def test_subprocess_error_reports_stderr_only(self) -> None:
        import subprocess

        exc = subprocess.SubprocessError("toolbox call failed")
        with mock.patch.object(tc, "confirm_trial", side_effect=exc):
            code, out, err = self._run_main(["trial_confirmation"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        failure = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(failure["state"], "confirmation_failed")

    def test_late_wayland_readiness_retries_to_normal_success(self) -> None:
        payload = {"state": "not_pending", "recordState": None}
        not_ready = tc.runtime_health.RuntimeNotReady
        with mock.patch.object(
            tc,
            "confirm_trial",
            side_effect=[
                not_ready("No login sessions reported"),
                not_ready("No eligible seat0 Wayland user session reported"),
                dict(payload),
            ],
        ) as confirm, mock.patch.object(tc.time, "sleep") as sleep:
            code, out, err = self._run_main(["trial_confirmation"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), payload)
        self.assertEqual(confirm.call_count, 3)
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(tc.READINESS_RETRY_DELAY_SECONDS)] * 2,
        )

    def test_fatal_evidence_failure_is_not_retried(self) -> None:
        failures = (
            tc.ConfirmationError("update lock is busy"),
            tc.runtime_health.RuntimeHealthError(
                "Unsafe runtime dir ownership/permissions"
            ),
        )
        for exc in failures:
            with self.subTest(exc=type(exc).__name__), mock.patch.object(
                tc, "confirm_trial", side_effect=exc
            ) as confirm, mock.patch.object(tc.time, "sleep") as sleep:
                code, out, err = self._run_main(["trial_confirmation"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(confirm.call_count, 1)
                sleep.assert_not_called()
                failure = json.loads(err.strip().splitlines()[-1])
                self.assertEqual(failure["state"], "confirmation_failed")
                self.assertIn(type(exc).__name__, failure["error"])

    def test_readiness_retry_is_bounded_and_then_fails(self) -> None:
        not_ready = tc.runtime_health.RuntimeNotReady("No login sessions reported")
        with mock.patch.object(
            tc, "confirm_trial", side_effect=not_ready
        ) as confirm, mock.patch.object(tc.time, "sleep") as sleep:
            code, out, err = self._run_main(["trial_confirmation"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(confirm.call_count, tc.READINESS_RETRY_ATTEMPTS)
        self.assertEqual(
            sleep.call_args_list,
            [mock.call(tc.READINESS_RETRY_DELAY_SECONDS)]
            * (tc.READINESS_RETRY_ATTEMPTS - 1),
        )
        failure = json.loads(err.strip().splitlines()[-1])
        self.assertEqual(failure["state"], "confirmation_failed")

    def test_unexpected_extra_args_rejected_before_confirm(self) -> None:
        confirm = mock.Mock()
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(tc, "confirm_trial", confirm), \
                mock.patch.object(sys, "argv", ["trial_confirmation", "--extra"]), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as ctx:
            tc.main()
        self.assertEqual(ctx.exception.code, 2)
        confirm.assert_not_called()


class ConfirmationServicePolicyTest(unittest.TestCase):
    """The unit must not hold graphical.target while Wayland logs in late."""

    def test_nonblocking_exec_unit_keeps_bounded_restart_policy(self) -> None:
        self.assertEqual(_unit_directives("Type"), ["exec"])
        self.assertEqual(_unit_directives("RemainAfterExit"), ["yes"])
        self.assertEqual(_unit_directives("Restart"), ["on-failure"])
        self.assertEqual(_unit_directives("RestartSec"), ["15s"])
        self.assertEqual(_unit_directives("TimeoutStartSec"), [])
        self.assertEqual(_unit_directives("StartLimitIntervalSec"), ["10min"])
        self.assertEqual(_unit_directives("StartLimitBurst"), ["10"])
        self.assertEqual(
            _unit_directives("ExecStart"),
            ["/usr/bin/python3 -m updates.trial_confirmation"],
        )
        self.assertEqual(_unit_directives("WantedBy"), ["graphical.target"])
        for order in _unit_directives("Before"):
            self.assertNotIn("graphical.target", order.split())


if __name__ == "__main__":
    unittest.main()
