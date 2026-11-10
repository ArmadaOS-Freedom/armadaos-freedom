"""Behavioral tests for SystemManager apply worker bridge."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from updates import system_manager
from test_update_system_manager import (
    BOOTED_COMMIT,
    FULL_CHANNELS,
    TARGET_COMMIT,
    snapshot,
    write_config,
)

FIXED_UUID = "abcdef12-1234-5678-9abc-def012345678"


class SystemManagerApplyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_root = self.root / "state"
        self.state_root.mkdir()
        self.config_path = write_config(FULL_CHANNELS, self.root)
        self.compatible_path = self.root / "compatible"
        self.compatible_path.write_bytes(b"lenovo,elden\0")
        self.work_dir = self.state_root / FIXED_UUID

    def make_manager(self):
        return system_manager.SystemManager(
            config_path=self.config_path,
            compatible_path=self.compatible_path,
            witness_path=self.root / "missing-witness",
            cmdline_path=self.root / "missing-cmdline",
        )

    def worker_report(self, **overrides):
        report = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
            "phase": "complete",
            "requiresReboot": True,
            "bootWriteState": "written",
            "activationOutcome": "activated",
            "targetVersion": "2.0.0",
            "targetCommit": TARGET_COMMIT,
            "workDir": str(self.work_dir),
        }
        report.update(overrides)
        return report

    def test_apply_success_spawns_worker_and_merges_report(self):
        manager = self.make_manager()
        report = self.worker_report()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(report), stderr=""
        )
        with patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
        ), patch("os.geteuid", return_value=0), patch(
            "armada_os.snapshot", return_value=snapshot(channel="beta")
        ), patch(
            "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
        ), patch(
            "subprocess.run", return_value=completed
        ) as run_mock:
            result = manager.apply()

        run_mock.assert_called_once_with(
            [
                "/usr/bin/python3",
                "-m",
                "updates.system_apply",
                "--channel",
                "beta",
                "--job-id",
                FIXED_UUID,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        _, kwargs = run_mock.call_args
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("timeout", kwargs)
        self.assertIsNone(manager._active_work)
        self.assertEqual(result["state"], "awaiting_reboot")
        self.assertEqual(result["phase"], "complete")
        self.assertTrue(result["requiresReboot"])
        self.assertEqual(result["model"], "TB322FC")
        self.assertEqual(result["channel"], "beta")
        self.assertEqual(result["currentVersion"], "1.0.0")
        self.assertEqual(result["currentCommit"], BOOTED_COMMIT)
        self.assertEqual(result["targetVersion"], "2.0.0")
        self.assertEqual(result["targetCommit"], TARGET_COMMIT)
        self.assertEqual(result["bootWriteState"], "written")
        self.assertEqual(result["activationOutcome"], "activated")
        self.assertEqual(result["workDir"], str(self.work_dir))

    def test_apply_preserves_worker_failed_and_uncertain_evidence(self):
        manager = self.make_manager()
        failed_report = self.worker_report(
            state="failed",
            phase="boot_write",
            requiresReboot=False,
            bootWriteState="failed_restored",
            bootRestoreState="restored",
            activationOutcome="not_activated",
            nativeResult={"stage": "boot_write", "rc": 5},
            error="slot write failed",
        )
        uncertain_report = self.worker_report(
            state="uncertain",
            phase="activate",
            requiresReboot=False,
            bootWriteState="written",
            bootRestoreState="restore_failed",
            activationOutcome="uncertain",
            nativeResult={"stage": "activate", "rc": 9},
            error="restore uncertain",
        )
        for expected_state, payload in (
            ("failed", failed_report),
            ("uncertain", uncertain_report),
        ):
            completed = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=json.dumps(payload), stderr=""
            )
            with patch.object(
                system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
            ), patch("os.geteuid", return_value=0), patch(
                "armada_os.snapshot", return_value=snapshot()
            ), patch(
                "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
            ), patch(
                "subprocess.run", return_value=completed
            ):
                result = manager.apply()
            self.assertEqual(result["state"], expected_state)
            self.assertFalse(result["requiresReboot"])
            self.assertEqual(
                result["bootRestoreState"], payload["bootRestoreState"]
            )
            self.assertEqual(result["nativeResult"], payload["nativeResult"])
            self.assertEqual(result["error"], payload["error"])

    def test_apply_outcome_rejects_malformed_or_inconsistent_results(self):
        base = {
            "schemaVersion": 1,
            "component": "system",
            "model": "TB322FC",
            "channel": "stable",
            "currentVersion": "1.0.0",
            "currentCommit": BOOTED_COMMIT,
        }
        cases = [
            (None, 0),
            (b"{}", 0),
            ("", 0),
            ("{not-json", 0),
            ("[]", 0),
            (
                '{"schemaVersion":1,"schemaVersion":1,"component":"system",'
                '"state":"up_to_date","phase":"check","requiresReboot":false}',
                0,
            ),
            (
                json.dumps(
                    self.worker_report(
                        state="up_to_date",
                        requiresReboot=False,
                        pad="x" * (system_manager.MAX_CONFIG_BYTES + 10),
                    )
                ),
                0,
            ),
            (json.dumps(self.worker_report(schemaVersion=True)), 0),
            (json.dumps(self.worker_report(component="decky")), 0),
            (json.dumps(self.worker_report(requiresReboot=1)), 0),
            (
                json.dumps(
                    self.worker_report(
                        state="failed",
                        requiresReboot=False,
                        bootWriteState="not_started",
                        activationOutcome="not_attempted",
                    )
                ),
                0,
            ),
            (json.dumps(self.worker_report()), 1),
            (json.dumps(self.worker_report(requiresReboot=False)), 0),
            (json.dumps(self.worker_report(bootWriteState="partial")), 0),
            (
                json.dumps(
                    self.worker_report(
                        state="up_to_date", requiresReboot=True
                    )
                ),
                0,
            ),
            (
                json.dumps(
                    self.worker_report(workDir=str(self.root / "other"))
                ),
                0,
            ),
        ]
        for stdout, rc in cases:
            with self.subTest(stdout=repr(stdout)[:60], rc=rc):
                result = system_manager._apply_outcome(
                    stdout, rc, base, self.work_dir
                )
                self.assertEqual(result["state"], "uncertain")
                self.assertEqual(result["phase"], "worker_result")
                self.assertFalse(result["requiresReboot"])
                self.assertEqual(result["bootWriteState"], "uncertain")
                self.assertEqual(result["activationOutcome"], "uncertain")
                self.assertEqual(result["workDir"], str(self.work_dir))

    def test_apply_preflight_and_spawn_failures(self):
        manager = self.make_manager()
        with patch("os.geteuid", return_value=1000), patch(
            "armada_os.snapshot"
        ) as snap_mock, patch("subprocess.run") as run_mock:
            nonroot = manager.apply()
        snap_mock.assert_not_called()
        run_mock.assert_not_called()
        self.assertEqual(nonroot["state"], "failed")
        self.assertEqual(nonroot["phase"], "apply")
        self.assertFalse(nonroot["requiresReboot"])
        self.assertEqual(nonroot["bootWriteState"], "not_started")
        self.assertEqual(nonroot["activationOutcome"], "not_attempted")

        with patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
        ), patch("os.geteuid", return_value=0), patch(
            "armada_os.snapshot", return_value=snapshot()
        ), patch(
            "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
        ), patch(
            "subprocess.run", side_effect=FileNotFoundError("no python3")
        ):
            missing_exec = manager.apply()
        self.assertEqual(missing_exec["state"], "failed")
        self.assertEqual(missing_exec["phase"], "apply")
        self.assertFalse(missing_exec["requiresReboot"])
        self.assertEqual(missing_exec["bootWriteState"], "not_started")
        self.assertEqual(missing_exec["activationOutcome"], "not_attempted")
        self.assertEqual(missing_exec["currentVersion"], "1.0.0")
        self.assertIsNone(manager._active_work)

        with patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
        ), patch("os.geteuid", return_value=0), patch(
            "armada_os.snapshot", return_value=snapshot()
        ), patch(
            "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
        ), patch(
            "subprocess.run",
            side_effect=subprocess.SubprocessError("spawn failure"),
        ):
            subproc_err = manager.apply()
        self.assertEqual(subproc_err["state"], "uncertain")
        self.assertEqual(subproc_err["phase"], "worker_spawn")
        self.assertFalse(subproc_err["requiresReboot"])
        self.assertEqual(subproc_err["bootWriteState"], "uncertain")
        self.assertEqual(subproc_err["activationOutcome"], "uncertain")
        self.assertEqual(subproc_err["workDir"], str(self.work_dir))
        self.assertIsNone(manager._active_work)

    def test_progress_reads_valid_active_status_and_clears_after_apply(self):
        manager = self.make_manager()
        self.assertIsNone(manager.progress())
        observed = []

        def fake_run(*_args, **_kwargs):
            self.work_dir.mkdir(parents=True, exist_ok=True)
            status_file = self.work_dir / "status.json"
            observed.append(manager.progress())
            status_file.write_text("{bad json")
            observed.append(manager.progress())
            status_file.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "component": "system",
                        "phase": "pulling",
                        "workDir": str(self.root / "wrong"),
                    }
                )
            )
            observed.append(manager.progress())
            valid_progress = {
                "schemaVersion": 1,
                "component": "system",
                "state": "running",
                "phase": "boot_write",
                "workDir": str(self.work_dir),
                "bootWriteState": "writing",
            }
            status_file.write_text(json.dumps(valid_progress))
            observed.append(manager.progress())
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(
                    self.worker_report(
                        state="up_to_date",
                        requiresReboot=False,
                        bootWriteState="not_started",
                        activationOutcome="not_needed",
                    )
                ),
                stderr="",
            )

        with patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
        ), patch("os.geteuid", return_value=0), patch(
            "armada_os.snapshot", return_value=snapshot()
        ), patch(
            "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
        ), patch(
            "subprocess.run", side_effect=fake_run
        ):
            result = manager.apply()

        self.assertEqual(result["state"], "up_to_date")
        self.assertEqual(observed[:3], [None, None, None])
        self.assertEqual(observed[3]["phase"], "boot_write")
        self.assertEqual(observed[3]["workDir"], str(self.work_dir))
        self.assertIsNone(manager.progress())

    def test_main_cli_apply_exit_codes_and_arg_rejection(self):
        for state, expected_rc in (
            ("awaiting_reboot", 0),
            ("up_to_date", 0),
            ("failed", 1),
            ("uncertain", 1),
        ):
            out = io.StringIO()
            with patch.object(sys, "argv", ["system_manager", "apply"]), patch.object(
                system_manager.SystemManager,
                "apply",
                return_value={
                    "schemaVersion": 1,
                    "component": "system",
                    "state": state,
                },
            ), patch(
                "sys.stdout", out
            ):
                rc = system_manager.main()
            self.assertEqual(rc, expected_rc)
            self.assertEqual(json.loads(out.getvalue())["state"], state)

        with patch.object(
            sys,
            "argv",
            ["system_manager", "apply", "https://evil.example/update"],
        ), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                system_manager.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_rollback_success_spawns_worker_tracks_active_work_and_clears(self):
        manager = self.make_manager()
        report = self.worker_report(
            targetVersion="1.0.0",
            targetCommit=TARGET_COMMIT,
        )
        observed_active = []

        def fake_run(argv, **kwargs):
            observed_active.append(manager._active_work)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=json.dumps(report), stderr=""
            )

        with patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
        ), patch("os.geteuid", return_value=0), patch(
            "armada_os.snapshot", return_value=snapshot(channel="beta")
        ), patch(
            "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
        ), patch(
            "subprocess.run", side_effect=fake_run
        ) as run_mock:
            result = manager.rollback(TARGET_COMMIT)

        run_mock.assert_called_once_with(
            [
                "/usr/bin/python3",
                "-m",
                "updates.system_apply",
                "--rollback-checksum",
                TARGET_COMMIT,
                "--job-id",
                FIXED_UUID,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        sent_argv = run_mock.call_args[0][0]
        self.assertNotIn("--channel", sent_argv)
        self.assertEqual(observed_active, [self.work_dir])
        self.assertIsNone(manager._active_work)
        self.assertEqual(result["state"], "awaiting_reboot")
        self.assertTrue(result["requiresReboot"])
        self.assertEqual(result["bootWriteState"], "written")
        self.assertEqual(result["activationOutcome"], "activated")
        self.assertEqual(result["targetCommit"], TARGET_COMMIT)
        self.assertEqual(result["workDir"], str(self.work_dir))

    def test_rollback_rejects_invalid_checksum_before_snapshot_or_worker(self):
        manager = self.make_manager()
        for bad_checksum in (None, 123, "", "a" * 63, TARGET_COMMIT.upper(), "g" * 64):
            with self.subTest(checksum=bad_checksum), patch(
                "os.geteuid", return_value=0
            ), patch("armada_os.snapshot") as snap_mock, patch(
                "subprocess.run"
            ) as run_mock:
                result = manager.rollback(bad_checksum)
            snap_mock.assert_not_called()
            run_mock.assert_not_called()
            self.assertIsNone(manager._active_work)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["phase"], "rollback")
            self.assertFalse(result["requiresReboot"])
            self.assertEqual(result["bootWriteState"], "not_started")
            self.assertEqual(result["activationOutcome"], "not_attempted")
            self.assertIn("checksum", result["error"])

    def test_rollback_worker_non_success_and_invalid_outcomes_never_succeed(self):
        manager = self.make_manager()
        uncertain_payload = self.worker_report(
            state="uncertain",
            phase="activate",
            requiresReboot=False,
            bootWriteState="written",
            activationOutcome="uncertain",
            error="rollback slot switch uncertain",
        )
        for rc, stdout, expected_phase in (
            (1, json.dumps(uncertain_payload), "activate"),
            (1, json.dumps(self.worker_report()), "worker_result"),
            (0, "{malformed", "worker_result"),
        ):
            completed = subprocess.CompletedProcess(
                args=[], returncode=rc, stdout=stdout, stderr=""
            )
            with self.subTest(rc=rc, phase=expected_phase), patch.object(
                system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root
            ), patch("os.geteuid", return_value=0), patch(
                "armada_os.snapshot", return_value=snapshot()
            ), patch(
                "uuid.uuid4", return_value=uuid.UUID(FIXED_UUID)
            ), patch(
                "subprocess.run", return_value=completed
            ):
                result = manager.rollback(TARGET_COMMIT)
            self.assertIsNone(manager._active_work)
            self.assertEqual(result["state"], "uncertain")
            self.assertEqual(result["phase"], expected_phase)
            self.assertFalse(result["requiresReboot"])
            self.assertEqual(result["activationOutcome"], "uncertain")


if __name__ == "__main__":
    unittest.main()
