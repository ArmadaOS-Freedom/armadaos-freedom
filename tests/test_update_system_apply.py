"""Boundary tests for the isolated system_apply worker."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

from test_update_system_manager import (
    FULL_CHANNELS,
    STAGED_COMMIT,
    TARGET_COMMIT,
    record,
    snapshot,
    write_config,
)
from updates import system_apply, system_manager
from updates.system_transaction import SystemTransactionError

VALID_JOB = "abcdef12-1234-5678-9abc-def012345678"
ZERO_JOB = "00000000-0000-0000-0000-000000000000"
COMPATIBLE_TB322FC = b"lenovo,elden\x00"
COMPATIBLE_TB710FU = b"lenovo,tb710fu\x00"


class SystemApplyWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.config_path = write_config(FULL_CHANNELS, self.base)
        self.compatible_path = self.base / "compatible"
        self.compatible_path.write_bytes(COMPATIBLE_TB322FC)
        self.sysroot_path = self.base / "sysroot"
        self.sysroot_path.mkdir(mode=0o755)
        self.state_dir = self.base / "state" / "updates" / "system"

        for attr, value in (
            ("CONFIG_PATH", self.config_path),
            ("COMPATIBLE_PATH", self.compatible_path),
            ("SYSROOT_PATH", self.sysroot_path),
            ("STATE_DIR", self.state_dir),
        ):
            patcher = patch.object(system_apply, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_execute_success_passes_selected_configuration_and_paths(self):
        expected = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
            "phase": "awaiting_reboot",
            "channel": "beta",
            "targetVersion": "2.0.0",
            "pendingDeployment": TARGET_COMMIT,
            "requiresReboot": True,
            "bootWriteState": "written",
            "activationOutcome": "activated",
        }
        with patch.object(system_apply, "os", wraps=os) as os_mock, \
             patch("armada_os.snapshot", return_value=snapshot(channel="beta")), \
             patch.object(system_apply, "run_online_transaction", return_value=expected) as run_tx:
            os_mock.geteuid.return_value = 0
            result = system_apply.execute("beta", VALID_JOB)

        self.assertEqual(result, expected)
        self.assertTrue(self.state_dir.is_dir())
        self.assertFalse((self.state_dir / VALID_JOB).exists())
        run_tx.assert_called_once_with(
            channel_url=FULL_CHANNELS["beta"]["url"],
            signature_url=FULL_CHANNELS["beta"]["signatureUrl"],
            expected_channel="beta",
            sysroot_path=self.sysroot_path,
            work_dir=self.state_dir / VALID_JOB,
            updater_version=system_manager.UPDATER_VERSION,
        )

    def test_execute_rollback_success_passes_checksum_and_paths_without_channel_config(self):
        expected = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
            "phase": "awaiting_reboot",
            "channel": "stable",
            "targetVersion": "1.0.0",
            "pendingDeployment": TARGET_COMMIT,
            "requiresReboot": True,
            "bootWriteState": "written",
            "activationOutcome": "activated",
        }
        missing_config = self.base / "missing_channels.json"
        with patch.object(system_apply, "CONFIG_PATH", missing_config), \
             patch.object(system_apply, "os", wraps=os) as os_mock, \
             patch("armada_os.snapshot", return_value=snapshot()), \
             patch.object(system_manager, "_configuration") as cfg_mock, \
             patch.object(system_apply, "run_online_transaction") as online_tx, \
             patch.object(system_apply, "run_rollback_transaction", return_value=expected) as rollback_tx:
            os_mock.geteuid.return_value = 0
            result = system_apply.execute(None, VALID_JOB, rollback_checksum=TARGET_COMMIT)

        self.assertEqual(result, expected)
        self.assertTrue(self.state_dir.is_dir())
        self.assertFalse((self.state_dir / VALID_JOB).exists())
        cfg_mock.assert_not_called()
        online_tx.assert_not_called()
        rollback_tx.assert_called_once_with(
            expected_checksum=TARGET_COMMIT,
            sysroot_path=self.sysroot_path,
            work_dir=self.state_dir / VALID_JOB,
        )

    def test_transaction_error_returns_detailed_failed_report(self):
        detailed = {
            "schemaVersion": 1,
            "component": "system",
            "state": "failed",
            "phase": "commit",
            "model": "TB322FC",
            "channel": "stable",
            "currentVersion": "1.0.0",
            "targetVersion": "2.0.0",
            "currentCommit": "a" * 64,
            "pendingDeployment": TARGET_COMMIT,
            "requiresReboot": False,
            "bootWriteState": "written",
            "activationOutcome": "uncertain",
            "error": "post-write finalize failed",
        }
        err = SystemTransactionError("post-write finalize failed", detailed)
        with patch("armada_os.snapshot", return_value=snapshot()), \
             patch.object(system_apply, "run_online_transaction", side_effect=err):
            result = system_apply.execute("stable", VALID_JOB)

        self.assertEqual(result, detailed)
        self.assertEqual(result["bootWriteState"], "written")
        self.assertEqual(result["activationOutcome"], "uncertain")

    def test_nonroot_rejects_before_snapshot(self):
        with patch.object(system_apply.os, "geteuid", return_value=1000), \
             patch("armada_os.snapshot") as snap, \
             patch.object(system_apply, "run_online_transaction") as run_tx:
            result = system_apply.execute("stable", VALID_JOB)

        snap.assert_not_called()
        run_tx.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["phase"], "preflight")
        self.assertIn("root", result["error"])

    def test_invalid_inputs_reject_before_snapshot_and_transaction(self):
        cases = [
            ("unknown_channel", "archive", VALID_JOB),
            ("empty_channel", "", VALID_JOB),
            ("uppercase_uuid", "stable", VALID_JOB.upper()),
            ("unhyphenated_uuid", "stable", VALID_JOB.replace("-", "")),
            ("not_a_uuid", "stable", "../escape"),
            ("zero_uuid", "stable", ZERO_JOB),
            ("non_string_uuid", "stable", 12345),
        ]
        for name, channel, job_id in cases:
            with self.subTest(case=name), \
                 patch("armada_os.snapshot") as snap, \
                 patch.object(system_apply, "run_online_transaction") as run_tx:
                result = system_apply.execute(channel, job_id)
                snap.assert_not_called()
                run_tx.assert_not_called()
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["phase"], "preflight")

        bad_checksums = [
            ("non_string", 12345),
            ("empty", ""),
            ("short", "a" * 63),
            ("long", "a" * 65),
            ("uppercase", TARGET_COMMIT.upper()),
            ("non_hex", "g" * 64),
        ]
        for name, bad_checksum in bad_checksums:
            with self.subTest(rollback_checksum=name), \
                 patch.object(system_apply.os, "geteuid", return_value=0), \
                 patch("armada_os.snapshot") as snap, \
                 patch.object(system_apply, "run_rollback_transaction") as rollback_tx:
                result = system_apply.execute(
                    None, VALID_JOB, rollback_checksum=bad_checksum
                )
                snap.assert_not_called()
                rollback_tx.assert_not_called()
                self.assertFalse(self.state_dir.exists())
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["phase"], "preflight")
                self.assertIn("checksum", result["error"])

    def test_preflight_deployment_and_config_rejections(self):
        (self.base / "partial_cfg").mkdir(exist_ok=True)
        partial_config = write_config(
            {"stable": FULL_CHANNELS["stable"]}, self.base / "partial_cfg"
        )
        cases = [
            ("unsupported_tb710fu", COMPATIBLE_TB710FU, self.config_path, snapshot(), "stable", "TB710FU"),
            ("channel_drift", COMPATIBLE_TB322FC, self.config_path, snapshot(channel="beta"), "stable", "differs"),
            ("missing_selected_config", COMPATIBLE_TB322FC, partial_config, snapshot(channel="testing"), "testing", "testing"),
            ("staged_exists", COMPATIBLE_TB322FC, self.config_path, snapshot(staged=record(commit=STAGED_COMMIT)), "stable", "staged"),
            ("rollback_queued", COMPATIBLE_TB322FC, self.config_path, snapshot(rollback_queued=True), "stable", "rollback"),
        ]
        for name, compat, cfg, snap_data, req_channel, err_match in cases:
            with self.subTest(case=name):
                self.compatible_path.write_bytes(compat)
                with patch.object(system_apply, "CONFIG_PATH", cfg), \
                     patch("armada_os.snapshot", return_value=snap_data), \
                     patch.object(system_apply, "run_online_transaction") as run_tx:
                    result = system_apply.execute(req_channel, VALID_JOB)
                run_tx.assert_not_called()
                self.assertFalse(self.state_dir.exists())
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["phase"], "preflight")
                self.assertIn(err_match, result["error"])

    def test_rollback_preflight_and_nonroot_rejections(self):
        with patch.object(system_apply.os, "geteuid", return_value=1000), \
             patch("armada_os.snapshot") as snap, \
             patch.object(system_apply, "run_rollback_transaction") as rollback_tx:
            nonroot = system_apply.execute(
                None, VALID_JOB, rollback_checksum=TARGET_COMMIT
            )
        snap.assert_not_called()
        rollback_tx.assert_not_called()
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(nonroot["state"], "failed")
        self.assertEqual(nonroot["phase"], "preflight")
        self.assertIn("root", nonroot["error"])

        cases = [
            ("unsupported_tb710fu", COMPATIBLE_TB710FU, snapshot(), "TB710FU"),
            ("staged_exists", COMPATIBLE_TB322FC, snapshot(staged=record(commit=STAGED_COMMIT)), "staged"),
            ("rollback_queued", COMPATIBLE_TB322FC, snapshot(rollback_queued=True), "rollback"),
        ]
        for name, compat, snap_data, err_match in cases:
            with self.subTest(case=name):
                self.compatible_path.write_bytes(compat)
                with patch.object(system_apply.os, "geteuid", return_value=0), \
                     patch("armada_os.snapshot", return_value=snap_data), \
                     patch.object(system_apply, "run_rollback_transaction") as rollback_tx:
                    result = system_apply.execute(
                        None, VALID_JOB, rollback_checksum=TARGET_COMMIT
                    )
                rollback_tx.assert_not_called()
                self.assertFalse(self.state_dir.exists())
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["phase"], "preflight")
                self.assertIn(err_match, result["error"])

    def test_existing_job_directory_preserved_and_rejected(self):
        job_dir = self.state_dir / VALID_JOB
        job_dir.mkdir(parents=True, mode=0o700)
        marker = job_dir / "existing.json"
        marker.write_text("{\"keep\": true}")

        with patch("armada_os.snapshot", return_value=snapshot()), \
             patch.object(system_apply, "run_online_transaction") as run_tx:
            result = system_apply.execute("stable", VALID_JOB)

        run_tx.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("already exists", result["error"])
        self.assertEqual(marker.read_text(), "{\"keep\": true}")

    def test_state_root_symlink_and_writable_rejected(self):
        real_state = self.base / "real_state"
        real_state.mkdir(mode=0o700)
        symlink_state = self.base / "symlink_state"
        symlink_state.symlink_to(real_state)

        writable_state = self.base / "writable_state"
        writable_state.mkdir(mode=0o777)
        os.chmod(writable_state, 0o777)

        for name, bad_root, match in (
            ("symlink_root", symlink_state, "Non-canonical state path"),
            ("writable_root", writable_state, "writable"),
        ):
            with self.subTest(case=name), \
                 patch.object(system_apply, "STATE_DIR", bad_root), \
                 patch("armada_os.snapshot", return_value=snapshot()), \
                 patch.object(system_apply, "run_online_transaction") as run_tx:
                result = system_apply.execute("stable", VALID_JOB)
                run_tx.assert_not_called()
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["phase"], "preflight")
                self.assertIn(match, result["error"])

    def test_snapshot_subprocess_error_returns_structured_preflight_failure(self):
        err = subprocess.CalledProcessError(1, ["ostree", "admin", "status"], stderr="boom")
        with patch("armada_os.snapshot", side_effect=err), \
             patch.object(system_apply, "run_online_transaction") as run_tx:
            result = system_apply.execute("stable", VALID_JOB)

        run_tx.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["phase"], "preflight")
        self.assertEqual(result["bootWriteState"], "not_started")
        self.assertEqual(result["activationOutcome"], "not_attempted")
        self.assertIn("ostree", result["error"])

    def test_main_exit_codes_json_output_and_strict_arguments(self):
        for state, expected_code in (
            ("awaiting_reboot", 0),
            ("up_to_date", 0),
            ("failed", 1),
        ):
            with self.subTest(state=state):
                payload = {"schemaVersion": 1, "component": "system", "state": state}
                out = io.StringIO()
                with patch.object(sys, "argv", ["system_apply", "--channel", "stable", "--job-id", VALID_JOB]), \
                     patch.object(system_apply, "execute", return_value=payload) as exec_mock, \
                     redirect_stdout(out):
                    code = system_apply.main()
                exec_mock.assert_called_once_with(
                    "stable", VALID_JOB, rollback_checksum=None
                )
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(out.getvalue()), payload)

        rollback_payload = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
        }
        rollback_out = io.StringIO()
        with patch.object(
            sys,
            "argv",
            ["system_apply", "--rollback-checksum", TARGET_COMMIT, "--job-id", VALID_JOB],
        ), patch.object(
            system_apply, "execute", return_value=rollback_payload
        ) as exec_mock, redirect_stdout(rollback_out):
            code = system_apply.main()
        exec_mock.assert_called_once_with(
            None, VALID_JOB, rollback_checksum=TARGET_COMMIT
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(rollback_out.getvalue()), rollback_payload)

        for bad_args in (
            ["system_apply", "--channel", "stable", "--rollback-checksum", TARGET_COMMIT, "--job-id", VALID_JOB],
            ["system_apply", "--channel", "stable", "--job-id", VALID_JOB, "--url", "https://evil/x"],
            ["system_apply", "--channel", "stable", "--job-id", VALID_JOB, "--sysroot", "/tmp/x"],
            ["system_apply", "--channel", "stable", "--job-id", VALID_JOB, "--work-dir", "/tmp/x"],
        ):
            with self.subTest(argv=bad_args), \
                 patch.object(sys, "argv", bad_args), \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    system_apply.main()
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
