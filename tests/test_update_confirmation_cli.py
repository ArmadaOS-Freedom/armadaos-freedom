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
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import trial_confirmation as tc


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


if __name__ == "__main__":
    unittest.main()
