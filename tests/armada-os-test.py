#!/usr/bin/env python3
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
import armada_os as maintenance
from updates.client import BUSY_ERROR
REAL_SNAPSHOT = maintenance.snapshot


def fixture():
    def entry(version, checksum):
        return {"version": version, "image": "ghcr.io/armadaos-freedom/armadaos-freedom:stable", "checksum": checksum,
                "serial": 0, "stateroot": "default", "incompatible": False}
    return {"booted": entry("current", "a" * 64), "rollback": entry("previous", "b" * 64),
            "staged": None, "rollback_queued": False, "channel": "stable",
            "repository": "ghcr.io/armadaos-freedom/armadaos-freedom", "channels": ["stable", "beta", "preview"]}


def staged_entry(image, version="queued"):
    return {"version": version, "image": image, "checksum": "c" * 64, "serial": 0,
            "stateroot": "default", "incompatible": False}


def raw_state(staged=True):
    def entry(version, checksum):
        return {"image": {"version": version, "image": {"image": "ghcr.io/armadaos-freedom/armadaos-freedom:stable"}},
                "ostree": {"checksum": checksum, "deploySerial": 0, "stateroot": "default"}}
    return {"status": {"booted": entry("current", "a" * 64), "rollback": entry("previous", "b" * 64),
                       "staged": entry("queued", "c" * 64) if staged else None, "rollbackQueued": False}}


def default_service_result():
    return {"component": "system", "state": "installed", "requiresReboot": False}


class FakeUpdateClient:
    def __init__(self, test_case):
        self._test = test_case

    def snapshot(self):
        return self._test.client_snapshot()

    def run(self, component, operation, channel=None, checksum=None, on_progress=None, timeout=None):
        return self._test.client_run(
            component, operation, channel, checksum=checksum, on_progress=on_progress, timeout=timeout)


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.state = fixture()
        self.service_busy = False
        self.service_result = default_service_result()
        self.busy_races = []
        self.status_error = None
        patch.object(maintenance, "snapshot", side_effect=self.snapshot).start()
        self.command = patch.object(maintenance, "command", side_effect=self.fake_command).start()
        self.run = patch.object(maintenance.subprocess, "run", side_effect=self.fake_run).start()
        self.client_snapshot = Mock(side_effect=self.fake_client_snapshot)
        self.client_run = Mock(side_effect=self.fake_client_run)
        patch.object(maintenance, "UpdateClient", side_effect=lambda *args, **kwargs: FakeUpdateClient(self)).start()

    def snapshot(self):
        return copy.deepcopy(self.state)

    def fake_client_snapshot(self):
        return {
            "schemaVersion": 1,
            "components": {
                "system": {
                    "busy": self.service_busy,
                    "result": copy.deepcopy(self.service_result),
                }
            },
        }

    def prepare_rollback(self):
        self.service_result = {
            "component": "system",
            "state": "installed",
            "requiresReboot": False,
            "model": "TB322FC",
            "lastUpdateState": "confirmed",
            "trial": {
                "recordState": "confirmed",
                "runningDeployment": "new",
                "oldDeployment": f"default/{'b' * 64}.0",
            },
        }

    def fake_client_run(self, component, operation, channel=None, checksum=None, on_progress=None, timeout=None):
        if component == "system" and operation == "status":
            if self.busy_races:
                busy = self.busy_races.pop(0)
                if busy:
                    self.service_busy = True
                    self.service_result = {
                        "component": "system",
                        "state": "running",
                        "phase": "check",
                    }
                raise RuntimeError(f"GDBus.Error:{BUSY_ERROR}: pending job")
            if self.status_error is not None:
                raise self.status_error
            return copy.deepcopy(self.service_result)
        if component == "system" and operation == "select_channel":
            self.state["channel"] = "preview" if channel == "testing" else channel
            return copy.deepcopy(self.service_result)
        if component == "system" and operation == "rollback":
            target = self.state["rollback"]
            binding = f"{target['stateroot']}/{checksum}.{target['serial']}"
            self.service_result = {
                "component": "system",
                "state": "awaiting_reboot",
                "requiresReboot": True,
                "bootWriteState": "written",
                "bootPolicy": "dual-boot-no-slot-control",
                "slotControlState": "not_issued",
                "newDeployment": binding,
                "trial": {"newDeployment": binding},
            }
            return copy.deepcopy(self.service_result)
        raise AssertionError(f"Unexpected service call: {(component, operation, channel, checksum)}")

    def fake_command(self, arguments, **kwargs):
        if arguments[0] == maintenance.SELECT:
            self.state["channel"] = arguments[1]
        return ""

    def fake_run(self, arguments, **kwargs):
        if arguments == [maintenance.UPDATE]:
            self.state["staged"] = None
            self.service_result = {
                "component": "system",
                "state": "awaiting_reboot",
                "requiresReboot": True,
                "trial": {"newDeployment": "next"},
            }
            return subprocess.CompletedProcess(arguments, 0)
        raise AssertionError(f"Unexpected subprocess call: {arguments}")

    def perform(self, operation):
        checksum = (self.state["rollback"] or {}).get("checksum") if operation == "rollback" else None
        return maintenance.execute(operation, checksum)

    def test_versions_available_without_a_network_check(self):
        state = maintenance.status()
        self.assertEqual(state["booted"]["version"], "current")
        self.assertEqual(state["rollback"]["version"], "previous")
        self.command.assert_not_called()
        self.client_run.assert_called_once_with(
            "system", "status", None, checksum=None, on_progress=None, timeout=120)

    def test_bootc_deployment_metadata(self):
        self.command.side_effect = [json.dumps(raw_state()), "ghcr.io/armadaos-freedom/armadaos-freedom testing\n"]
        state = REAL_SNAPSHOT()
        self.assertEqual(state["booted"]["version"], "current")
        self.assertEqual(state["rollback"]["version"], "previous")
        self.assertEqual(state["staged"]["version"], "queued")
        self.assertEqual(state["repository"], "ghcr.io/armadaos-freedom/armadaos-freedom")
        self.assertEqual(state["channels"], ["stable", "beta", "preview"])
        self.assertEqual(state["channel"], "preview")
        self.assertEqual(self.command.call_count, 2)

    def test_snapshot_offers_no_channels_for_old_or_unknown_targets(self):
        for target in ("ghcr.io/armada-os/armada stable\n",
                       "ghcr.io/virtudude/armada beta\n",
                       "example.invalid/whatever stable\n"):
            with self.subTest(target=target):
                self.command.side_effect = [json.dumps(raw_state(staged=False)), target]
                self.assertEqual(REAL_SNAPSHOT()["channels"], [])

    def test_snapshot_preserves_channels_for_pretransfer_freedom(self):
        self.command.side_effect = [
            json.dumps(raw_state(staged=False)),
            "ghcr.io/mydanyi/armadaos-freedom testing\n",
        ]
        state = REAL_SNAPSHOT()
        self.assertEqual(state["channel"], "preview")
        self.assertEqual(state["channels"], ["stable", "beta", "preview"])

    def test_snapshot_target_resolution_failure_leaves_state_empty(self):
        self.command.side_effect = [json.dumps(raw_state(staged=False)), RuntimeError("resolver failed")]
        state = REAL_SNAPSHOT()
        self.assertEqual(state["channel"], "")
        self.assertEqual(state["repository"], "")
        self.assertEqual(state["channels"], [])

    def test_no_previous_version_refuses_rollback(self):
        self.state["rollback"] = None
        with self.assertRaisesRegex(RuntimeError, "No compatible previous"):
            self.perform("rollback")
        self.run.assert_not_called()

    def test_stale_confirmation_refuses_changes(self):
        checksum = self.state["rollback"]["checksum"]
        self.state["rollback"]["checksum"] = "c" * 64
        with self.assertRaisesRegex(RuntimeError, "changed"):
            maintenance.execute("rollback", checksum)
        self.run.assert_not_called()

    def test_rollback_rejects_and_preserves_staged_deployment(self):
        self.prepare_rollback()
        self.state["staged"] = {**self.state["booted"], "version": "queued"}
        self.assertFalse(maintenance.status()["rollback_allowed"])
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.perform("rollback")
        self.assertEqual(self.state["staged"]["version"], "queued")
        self.run.assert_not_called()

    def test_already_queued_rollback_bypasses_without_new_service_call(self):
        binding = f"default/{'b' * 64}.0"
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "newDeployment": binding,
            "trial": {"newDeployment": binding},
        }
        result = self.perform("rollback")
        self.assertEqual(result["message"], "Rollback ready. Restart to apply.")
        self.assertEqual(
            [call.args[:2] for call in self.client_run.call_args_list],
            [("system", "status")],
        )
        self.run.assert_not_called()
        self.assertTrue(maintenance.status()["reboot_ready"])

    def test_channel_change_uses_update_service_and_invalid_channels_are_rejected(self):
        maintenance.select_channel("beta")
        self.assertEqual(self.state["channel"], "beta")
        self.assertEqual(self.client_run.call_args.args[:3], ("system", "select_channel", "beta"))
        maintenance.select_channel("preview")
        self.assertEqual(self.state["channel"], "preview")
        self.assertEqual(self.client_run.call_args.args[:3], ("system", "select_channel", "testing"))
        self.command.assert_not_called()
        with self.assertRaises(ValueError):
            maintenance.select_channel("example.com/image")
        self.state["channels"] = []
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            maintenance.select_channel("stable")

    def test_channel_change_failure_reports_service_error(self):
        self.client_run.side_effect = lambda *args, **kwargs: {
            "component": "system",
            "state": "failed",
            "error": "switch denied",
        }
        with self.assertRaisesRegex(RuntimeError, "Update channel selection failed: switch denied"):
            maintenance.select_channel("beta")

    def test_update_uses_shared_hook(self):
        result = self.perform("update")
        self.assertEqual(result["message"], "Update installed. Restart to apply.")
        state = maintenance.status()
        self.assertTrue(state["reboot_ready"])
        self.assertEqual(state["update_target"], "next")
        self.assertEqual(self.run.call_args.args[0], [maintenance.UPDATE])

    def test_update_without_a_new_deployment_reports_result(self):
        self.run.side_effect = lambda arguments, **kwargs: subprocess.CompletedProcess(arguments, 0)
        result = self.perform("update")
        self.assertIn("No new image", result["message"])
        self.assertFalse(maintenance.status()["reboot_ready"])

    def test_apply_pending_update_returns_without_rerunning_hook(self):
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "trial": {"newDeployment": "next"},
        }
        result = self.perform("update")
        self.assertEqual(result["message"], "Update installed. Restart to apply.")
        self.run.assert_not_called()

    def test_restart_uses_live_pending_state(self):
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            maintenance.restart()
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "trial": {"newDeployment": "next"},
        }
        maintenance.restart()
        self.assertEqual(self.command.call_args.args[0], ["/usr/bin/systemctl", "reboot"])
        self.service_result = {
            "component": "system",
            "state": "installed",
            "requiresReboot": False,
            "lastUpdateState": "confirmed",
            "trial": {"newDeployment": "next"},
        }
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            maintenance.restart()
        self.assertEqual(self.command.call_count, 1)

    def test_reboot_ready_requires_a_migrated_staged_source(self):
        self.state["channel"] = "stable"
        for image in ("ghcr.io/armada-os/armada:stable", "ghcr.io/virtudude/armada:stable",
                      "example.invalid/whatever:stable"):
            with self.subTest(image=image):
                self.state["staged"] = staged_entry(image)
                state = maintenance.status()
                self.assertEqual(state["staged"]["version"], "queued")
                self.assertFalse(state["reboot_ready"])

    def test_staged_deployments_do_not_make_reboot_ready_without_service_confirmation(self):
        for channel, image in (
            ("stable", "ghcr.io/armadaos-freedom/armadaos-freedom:stable"),
            ("preview", "ghcr.io/armadaos-freedom/armadaos-freedom:testing"),
            ("stable", "ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:stable"),
        ):
            with self.subTest(channel=channel, image=image):
                self.state["channel"] = channel
                self.state["staged"] = staged_entry(image)
                state = maintenance.status()
                self.assertEqual(state["staged"]["image"], image)
                self.assertFalse(state["reboot_ready"])

    def test_service_confirmed_awaiting_reboot_is_ready_without_bootc_staged_entry(self):
        self.state["staged"] = None
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "trial": {"newDeployment": "next"},
        }
        state = maintenance.status()
        self.assertIsNone(state["staged"])
        self.assertTrue(state["reboot_ready"])
        self.assertEqual(state["update_target"], "next")
        self.assertEqual(state["update_message"], "Update installed. Restart to apply.")

    def test_awaiting_reboot_without_requires_reboot_is_incomplete_and_disables_restart(self):
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": False,
            "trial": {"newDeployment": "next"},
        }
        state = maintenance.status()
        self.assertFalse(state["reboot_ready"])
        self.assertEqual(state["update_target"], "next")
        self.assertNotEqual(state["update_message"], "Update installed. Restart to apply.")
        self.assertIn("incomplete", state["update_message"].lower())
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            maintenance.restart()
        self.command.assert_not_called()

    def test_reboot_ready_rejects_a_same_freedom_wrong_channel_source(self):
        self.state["channel"] = "stable"
        for image in ("ghcr.io/armadaos-freedom/armadaos-freedom:beta",
                      "ghcr.io/armadaos-freedom/armadaos-freedom:testing",
                      "ghcr.io/armadaos-freedom/armadaos-freedom:preview"):
            with self.subTest(image=image):
                self.state["staged"] = staged_entry(image)
                state = maintenance.status()
                self.assertEqual(state["staged"]["version"], "queued")
                self.assertEqual(state["staged"]["image"], image)
                self.assertFalse(state["reboot_ready"])

    def test_legacy_queued_rollback_is_not_verified_for_any_staged_source(self):
        self.prepare_rollback()
        self.state["rollback_queued"] = True
        for image in ("ghcr.io/armada-os/armada:stable", "example.invalid/whatever:stable"):
            with self.subTest(image=image):
                self.state["staged"] = staged_entry(image)
                state = maintenance.status()
                self.assertFalse(state["reboot_ready"])
                self.assertFalse(state["rollback_allowed"])
                self.assertNotEqual(state["update_message"], "Update confirmed.")
                self.assertIn("unverified", state["update_message"].lower())

    def test_restart_rejects_an_old_upstream_staged_source(self):
        self.state["staged"] = staged_entry("ghcr.io/armada-os/armada:stable")
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            maintenance.restart()

    def test_update_rejects_a_service_pending_conflict(self):
        self.service_result = {
            "component": "system",
            "state": "pending_conflict",
            "requiresReboot": False,
            "error": "foreign deployment queued",
        }
        with self.assertRaisesRegex(RuntimeError, "(?i)pending.*foreign deployment queued"):
            self.perform("update")
        self.run.assert_not_called()

    def test_queued_rollback_blocks_update_but_allows_channel_change(self):
        self.state["rollback_queued"] = True
        maintenance.select_channel("beta")
        self.assertEqual(self.state["channel"], "beta")
        with self.assertRaisesRegex(RuntimeError, "(?i)unverified.*rollback"):
            self.perform("update")
        self.run.assert_not_called()

    def test_busy_service_does_not_refresh_update_or_restart(self):
        self.service_busy = True
        self.service_result = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "phase": "applying",
            "trial": {"newDeployment": "next"},
        }
        state = maintenance.status()
        self.assertTrue(state["update_busy"])
        self.assertFalse(state["update_allowed"])
        self.assertFalse(state["reboot_ready"])
        self.assertEqual(state["update_message"], "Update operation is running (applying).")
        self.client_run.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.perform("update")
        with self.assertRaisesRegex(RuntimeError, "no pending"):
            maintenance.restart()
        self.client_run.assert_not_called()
        self.run.assert_not_called()
        self.command.assert_not_called()

    def test_service_unavailable_fails_without_legacy_fallback(self):
        self.client_snapshot.side_effect = RuntimeError("cannot read update status: service offline")
        with self.assertRaisesRegex(RuntimeError, "service offline"):
            maintenance.status()
        with self.assertRaisesRegex(RuntimeError, "service offline"):
            self.perform("update")
        self.run.assert_not_called()

    def test_busy_refresh_rereads_snapshot_and_reports_running_job(self):
        self.busy_races = [True]
        state = maintenance.status()
        self.assertTrue(state["update_busy"])
        self.assertEqual(state["update"]["state"], "running")
        self.assertEqual(state["update_message"], "Update operation is running (check).")
        self.assertEqual(self.client_snapshot.call_count, 2)
        self.assertEqual(self.client_run.call_count, 1)

    def test_busy_refresh_rereads_snapshot_and_uses_completed_result(self):
        self.service_result = {
            "component": "system",
            "state": "up_to_date",
            "requiresReboot": False,
        }
        self.busy_races = [False]
        state = maintenance.status()
        self.assertFalse(state["update_busy"])
        self.assertEqual(state["update"]["state"], "up_to_date")
        self.assertEqual(self.client_snapshot.call_count, 2)
        self.assertEqual(self.client_run.call_count, 1)

    def test_non_busy_refresh_errors_propagate_without_a_reread(self):
        self.status_error = RuntimeError("status probe failed")
        with self.assertRaisesRegex(RuntimeError, "status probe failed"):
            maintenance.status()
        self.assertEqual(self.client_snapshot.call_count, 1)
        self.assertEqual(self.client_run.call_count, 1)

    def test_malformed_service_status_is_rejected(self):
        for record in (
            {"busy": "no", "result": default_service_result()},
            {"busy": False, "result": {"component": "decky", "state": "installed"}},
            None,
        ):
            with self.subTest(record=record):
                self.client_snapshot.side_effect = lambda record=record: {
                    "schemaVersion": 1,
                    "components": {"system": record},
                }
                with self.assertRaisesRegex(RuntimeError, "invalid system status"):
                    maintenance.status()
        self.client_snapshot.side_effect = self.fake_client_snapshot
        self.client_run.side_effect = lambda *args, **kwargs: {"component": "decky", "state": "installed"}
        with self.assertRaisesRegex(RuntimeError, "invalid system result"):
            maintenance.status()

    def test_finalizing_confirmed_and_rolled_back_messages_clear_completed_targets(self):
        self.service_result = {
            "component": "system",
            "state": "awaiting_confirmation",
            "phase": "status",
            "lastUpdateState": "finalizing",
            "trial": {"newDeployment": "next"},
        }
        state = maintenance.status()
        self.assertEqual(state["update_message"], "Update is finalizing.")
        self.assertEqual(state["update_target"], "next")

        self.service_result = {
            "component": "system",
            "state": "installed",
            "phase": "status",
            "lastUpdateState": "confirmed",
            "targetVersion": "stale-target",
            "newDeployment": "stale-new",
            "trial": {"newDeployment": "confirmed-deployment"},
        }
        state = maintenance.status()
        self.assertEqual(state["update_message"], "Update confirmed.")
        self.assertIsNone(state["update_target"])

        self.service_result = {
            "component": "system",
            "state": "up_to_date",
            "phase": "status",
            "lastUpdateState": "rolled_back",
            "trial": {"newDeployment": "rolled-back-deployment"},
        }
        state = maintenance.status()
        self.assertEqual(state["update_message"], "Update rolled back.")
        self.assertIsNone(state["update_target"])

    def test_unexpected_terminal_update_state_propagates_details(self):
        for terminal_state, phase, error in (
            ("failed", "applying", "digest mismatch"),
            ("uncertain", "verifying", "slot state unreadable"),
        ):
            with self.subTest(state=terminal_state):
                self.service_result = default_service_result()

                def fail_update(arguments, terminal_state=terminal_state, phase=phase, error=error, **kwargs):
                    self.service_result = {
                        "component": "system",
                        "state": terminal_state,
                        "requiresReboot": False,
                        "phase": phase,
                        "error": error,
                    }
                    return subprocess.CompletedProcess(arguments, 0)

                self.run.side_effect = fail_update
                with self.assertRaisesRegex(RuntimeError, f"Update did not reach a ready state: {phase}: {error}"):
                    self.perform("update")

    def test_command_failure_is_reported_to_the_caller(self):
        self.run.side_effect = subprocess.CalledProcessError(7, maintenance.UPDATE)
        with self.assertRaises(subprocess.CalledProcessError):
            self.perform("update")

    def test_rollback_executes_via_update_service_and_marks_ready(self):
        self.prepare_rollback()
        self.assertTrue(maintenance.status()["rollback_allowed"])
        self.client_run.reset_mock()
        result = self.perform("rollback")
        self.assertEqual(result["message"], "Rollback ready. Restart to apply.")
        self.assertEqual(
            [(c.args[0], c.args[1], c.kwargs.get("checksum")) for c in self.client_run.call_args_list],
            [("system", "status", None), ("system", "rollback", "b" * 64)],
        )
        self.run.assert_not_called()
        self.command.assert_not_called()
        self.assertTrue(maintenance.status()["reboot_ready"])

    def test_rollback_rejects_invalid_or_incomplete_service_results(self):
        good = {
            "component": "system",
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "bootWriteState": "written",
            "bootPolicy": "dual-boot-no-slot-control",
            "slotControlState": "not_issued",
            "newDeployment": f"default/{'b' * 64}.0",
        }
        for bad in (
            None,
            ["unexpected"],
            {**good, "bootWriteState": "pending"},
            {**good, "bootPolicy": "legacy-single-slot"},
            {**good, "slotControlState": "issued"},
            {**good, "requiresReboot": False},
            {**good, "newDeployment": f"default/{'a' * 64}.0"},
            {"component": "system", "state": "failed", "phase": "rollback", "error": "write failed"},
            {"component": "system", "state": "uncertain", "phase": "verify", "error": "slot ambiguous"},
        ):
            with self.subTest(bad=bad):
                self.prepare_rollback()

                def responder(component, operation, channel=None, checksum=None, bad=bad, **kwargs):
                    if operation == "status":
                        return copy.deepcopy(self.service_result)
                    if operation == "rollback":
                        return copy.deepcopy(bad)
                    raise AssertionError(f"Unexpected call: {operation}")

                self.client_run.side_effect = responder
                with self.assertRaisesRegex(RuntimeError, "verified ready state"):
                    self.perform("rollback")
                self.run.assert_not_called()
                self.command.assert_not_called()
                self.assertFalse(maintenance.status()["reboot_ready"])

    def test_rollback_availability_guards_and_busy_blocks_before_writes(self):
        self.prepare_rollback()
        self.assertEqual(
            maintenance._rollback_binding(self.state["rollback"]),
            f"default/{'b' * 64}.0",
        )
        for invalid_serial in (True, False, 1.5, -1, 0x100000000, "0"):
            with self.subTest(serial=invalid_serial):
                entry = {**self.state["rollback"], "serial": invalid_serial}
                self.assertIsNone(maintenance._rollback_binding(entry))

        for mutator in (
            lambda: self.service_result.pop("trial"),
            lambda: self.service_result.update({"model": "UNSUPPORTED"}),
            lambda: self.service_result.update({"state": "awaiting_confirmation", "lastUpdateState": "finalizing"}),
            lambda: self.service_result["trial"].update({"oldDeployment": f"default/{'d' * 64}.0"}),
            lambda: self.state["rollback"].update({"serial": True}),
            lambda: self.service_result.update({"state": "failed"}),
            lambda: self.service_result.update({"state": "uncertain"}),
        ):
            self.state = fixture()
            self.prepare_rollback()
            mutator()
            self.assertFalse(maintenance.status()["rollback_allowed"])

        self.state = fixture()
        self.prepare_rollback()
        self.service_busy = True
        self.client_run.reset_mock()
        self.assertFalse(maintenance.status()["rollback_allowed"])
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.perform("rollback")
        self.client_run.assert_not_called()
        self.run.assert_not_called()

    def test_rollback_refuses_when_other_deployment_is_pending_restart(self):
        self.prepare_rollback()
        self.service_result.update({
            "state": "awaiting_reboot",
            "requiresReboot": True,
            "newDeployment": f"default/{'c' * 64}.0",
            "trial": {**self.service_result["trial"], "newDeployment": f"default/{'c' * 64}.0"},
        })
        self.client_run.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "Another OS change is already awaiting restart"):
            self.perform("rollback")
        self.assertEqual(
            [call.args[:2] for call in self.client_run.call_args_list],
            [("system", "status")],
        )
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
