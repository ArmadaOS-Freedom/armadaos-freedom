"""Behavioral tests for SystemManager status/check."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

import armada_os
from updates import offline_completion, system_manager, system_transaction
from updates.catalog import CatalogError

CANONICAL_REPO = armada_os.REPOSITORIES[0]
BOOTED_COMMIT = "a" * 64
STAGED_COMMIT = "b" * 64
TARGET_COMMIT = "c" * 64


def record(version="1.0.0", commit=BOOTED_COMMIT, incompatible=False):
    return {
        "version": version,
        "checksum": commit,
        "incompatible": incompatible,
    }


def snapshot(**overrides):
    base = {
        "booted": record(),
        "staged": None,
        "rollback_queued": False,
        "channel": "",
        "repository": CANONICAL_REPO,
    }
    base.update(overrides)
    return base


def channel_entry(version="2.0.0", commit=TARGET_COMMIT,
                  release="2.0-1", digest="sha256:" + "d" * 64):
    return {
        "version": version,
        "ostreeCommit": commit,
        "releaseId": release,
        "ociDigest": digest,
    }


def write_config(channels, directory):
    path = Path(directory) / "channels.json"
    path.write_text(json.dumps({"schemaVersion": 1, "channels": channels}))
    return path


FULL_CHANNELS = {
    "stable": {"url": "https://updates/stable.json",
               "signatureUrl": "https://updates/stable.sig"},
    "beta": {"url": "https://updates/beta.json",
             "signatureUrl": "https://updates/beta.sig"},
    "testing": {"url": "https://updates/testing.json",
                "signatureUrl": "https://updates/testing.sig"},
}


class SystemManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dt = self.tmp / "compatible"
        self.dt.write_bytes(b"lenovo,elden\0lenovo,tb322fc\0")
        self.config = write_config(FULL_CHANNELS, self.tmp)
        self.manager = system_manager.SystemManager(
            config_path=self.config, compatible_path=self.dt)
        self.fetch = MagicMock(return_value={"channel": channel_entry()})

    def patch_snapshot(self, snap):
        original = copy.deepcopy(snap)
        def guard():
            return copy.deepcopy(snap)
        return patch.object(armada_os, "snapshot", side_effect=guard), original, snap

    def run_check(self, snap):
        guard, original, snap_ref = self.patch_snapshot(snap)
        with guard, patch.object(system_manager, "fetch_update", self.fetch):
            result = self.manager.check()
        self.assertEqual(snap_ref, original)
        return result

    def run_status(self, snap):
        guard, original, snap_ref = self.patch_snapshot(snap)
        with guard, patch.object(system_manager, "fetch_update", self.fetch):
            result = self.manager.status()
        self.assertEqual(snap_ref, original)
        return result

    def assert_fetch(self, url, sig, channel):
        self.fetch.assert_called_once_with(
            url, sig, expected_channel=channel, model="TB322FC",
            updater_version=system_manager.UPDATER_VERSION)

    def test_models_and_aliases(self):
        tokens = {
            b"lenovo,elden": "TB322FC",
            b"lenovo,baldur": "TB323FU",
            b"lenovo,tb323fu": "TB323FU",
            b"lenovo,tb710fu": "TB710FU",
        }
        for token, model in tokens.items():
            self.dt.write_bytes(token + b"\0")
            result = self.run_status(snapshot())
            self.assertEqual((result["state"], result["model"]),
                             ("installed", model))

    def test_unknown_and_conflicting_models_no_fetch(self):
        for content in (b"lenovo,other\0", b"lenovo,elden\0lenovo,baldur\0"):
            self.dt.write_bytes(content)
            result = self.run_check(snapshot())
            self.assertEqual(result["state"], "unsupported")
            self.fetch.assert_not_called()

    def test_status_reads_no_config_or_http(self):
        self.config.unlink()
        result = self.run_status(snapshot())
        self.assertEqual(result["state"], "installed")
        self.fetch.assert_not_called()

    def test_current_version_from_booted_not_staged(self):
        snap = snapshot(staged=record(version="9.9.9", commit=STAGED_COMMIT))
        result = self.run_check(snap)
        self.assertEqual(result["currentVersion"], "1.0.0")
        self.assertEqual(result["pendingDeployment"]["version"], "9.9.9")

    def test_missing_config_not_configured(self):
        self.config.unlink()
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "not_configured")
        self.fetch.assert_not_called()

    def test_selected_channel_missing_no_fallback(self):
        self.config = write_config(
            {"testing": FULL_CHANNELS["testing"]}, self.tmp)
        self.manager.config_path = self.config
        snap = snapshot()  # empty local channel => stable
        result = self.run_check(snap)
        self.assertEqual(result["state"], "not_configured")
        self.fetch.assert_not_called()

    def test_empty_local_defaults_to_configured_stable(self):
        result = self.run_check(snapshot())
        self.assertEqual(result["channel"], "stable")
        self.fetch.assert_called_once_with(
            "https://updates/stable.json",
            "https://updates/stable.sig",
            expected_channel="stable",
            model="TB322FC",
            updater_version="0.8.0")

    def test_preview_maps_to_testing_with_fetch_args(self):
        snap = snapshot(channel="preview")
        result = self.run_check(snap)
        self.assertEqual(result["channel"], "testing")
        self.assert_fetch("https://updates/testing.json",
                          "https://updates/testing.sig", "testing")
        self.assertEqual(result["state"], "update_available")

    def test_distinct_urls_per_channel(self):
        self.config = write_config({"beta": FULL_CHANNELS["beta"]}, self.tmp)
        self.manager.config_path = self.config
        snap = snapshot(channel="beta")
        result = self.run_check(snap)
        self.assertEqual(result["state"], "update_available")
        self.assert_fetch("https://updates/beta.json",
                          "https://updates/beta.sig", "beta")

    def test_invalid_json(self):
        self.config.write_text("{not json")
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")

    def test_duplicate_properties(self):
        self.config.write_text(
            '{"schemaVersion": 1, "schemaVersion": 1, "channels": {}}')
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")

    def test_schema_bool_and_float_rejected(self):
        for version in (True, 1.0):
            self.config.write_text(json.dumps(
                {"schemaVersion": version, "channels": FULL_CHANNELS}))
            result = self.run_check(snapshot())
            self.assertEqual(result["state"], "check_failed")

    def test_unknown_channel_in_config(self):
        self.config.write_text(json.dumps(
            {"schemaVersion": 1, "channels": {"nightly": FULL_CHANNELS["stable"]}}))
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")

    def test_bad_selected_config_fields_no_fetch(self):
        self.config = write_config(
            {"stable": {"url": "https://updates/stable.json"}}, self.tmp)
        self.manager.config_path = self.config
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")
        self.fetch.assert_not_called()

    def test_missing_catalog_fields(self):
        entry = channel_entry()
        del entry["ociDigest"]
        self.fetch.return_value = {"channel": entry}
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")
        self.fetch.reset_mock()
        entry = channel_entry()
        del entry["releaseId"]
        self.fetch.return_value = {"channel": entry}
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")

    def test_fetch_catalog_error_preserves_current(self):
        self.fetch.side_effect = CatalogError("signature invalid")
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "check_failed")
        self.assertEqual(result["currentVersion"], "1.0.0")
        self.assertNotEqual(result["state"], "up_to_date")

    def test_target_equal_up_to_date(self):
        self.fetch.return_value = {"channel": channel_entry(commit=BOOTED_COMMIT)}
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "up_to_date")

    def test_update_available(self):
        result = self.run_check(snapshot())
        self.assertEqual(result["state"], "update_available")

    def test_staged_matches_target(self):
        snap = snapshot(staged=record(version="2.0.0", commit=TARGET_COMMIT))
        result = self.run_check(snap)
        self.assertEqual(result["state"], "staged")

    def test_mismatched_pending_conflict_even_if_booted_matches(self):
        snap = snapshot(staged=record(commit=STAGED_COMMIT))
        self.fetch.return_value = {"channel": channel_entry(commit=BOOTED_COMMIT)}
        result = self.run_check(snap)
        self.assertEqual(result["state"], "pending_conflict")

    def test_rollback_queued_conflict(self):
        snap = snapshot(rollback_queued=True)
        result = self.run_check(snap)
        self.assertEqual(result["state"], "pending_conflict")

    def test_incompatible_pending_conflict(self):
        snap = snapshot(staged=record(commit=TARGET_COMMIT, incompatible=True))
        result = self.run_check(snap)
        self.assertEqual(result["state"], "pending_conflict")

    def test_unknown_repository_no_fetch(self):
        snap = snapshot(repository="https://elsewhere/repo")
        result = self.run_check(snap)
        self.assertEqual(result["state"], "check_failed")
        self.fetch.assert_not_called()

    def test_target_fields_exact(self):
        expected_digest = "sha256:" + "e" * 64
        self.fetch.return_value = {"channel": channel_entry(
            version="3.1.4", commit=TARGET_COMMIT,
            release="3.1.4-2", digest=expected_digest)}
        result = self.run_check(snapshot())
        self.assertEqual(result["targetRelease"], "3.1.4-2")
        self.assertEqual(result["targetDigest"], expected_digest)
        self.assertEqual(result["targetCommit"], TARGET_COMMIT)

    def test_requires_reboot_always_false(self):
        for snap in (snapshot(),
                     snapshot(staged=record(commit=TARGET_COMMIT)),
                     snapshot(staged=record(commit=STAGED_COMMIT)),
                     snapshot(rollback_queued=True)):
            result = self.run_check(snap)
            self.assertFalse(result["requiresReboot"])
        self.assertFalse(self.run_status(snapshot())["requiresReboot"])

    def test_no_mutating_armada_calls(self):
        with patch.object(armada_os, "execute") as execute, \
             patch.object(armada_os, "restart") as restart, \
             patch.object(armada_os, "select_channel") as select:
            self.run_check(snapshot())
            self.run_status(snapshot())
            execute.assert_not_called()
            restart.assert_not_called()
            select.assert_not_called()


OFFLINE_TX = "11111111-2222-4333-8444-555555555555"
OFFLINE_OTHER_TX = "99999999-8888-4777-8666-555555555555"
OFFLINE_SESSION_ID = "0123456789abcdef0123456789abcdef"
OFFLINE_OLD = "armada/" + "a" * 64 + ".0"
OFFLINE_NEW = "armada/" + "b" * 64 + ".1"
OFFLINE_MODEL = "TB322FC"
OFFLINE_ORIGINAL = {"a": "e" * 64, "b": "f" * 64}
OFFLINE_NEW_BOOT = {"a": "1" * 64, "b": "2" * 64}


@unittest.skipUnless(
    getattr(os, "geteuid", lambda: -1)() == 0,
    "private offline session storage requires root-owned directories")
class OfflineCompletionStatusTests(unittest.TestCase):
    """Status must adopt only a private, journal-matched offline completion."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.compatible = self.root / "compatible"
        self.compatible.write_bytes(b"lenovo,elden\0")
        self.offline_root = self.root / "offline"
        self.offline_root.mkdir(mode=0o700)
        self.pending = self.offline_root / offline_completion.POINTER_DIRECTORY
        self.pending.mkdir(mode=0o700)
        self.session = self.offline_root / OFFLINE_SESSION_ID
        self.session.mkdir(mode=0o700)
        self.transaction = self.session / "transaction"
        self.transaction.mkdir(mode=0o700)
        self.digest = ""
        self.write_documents()
        patcher = patch.object(
            system_manager, "OFFLINE_SESSION_ROOT", self.offline_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = system_manager.SystemManager(
            config_path=self.root / "channels.json",
            compatible_path=self.compatible,
            witness_path=self.root / "missing-witness",
            cmdline_path=self.root / "missing-cmdline",
        )

    def write_documents(self, status_overrides=None, receipt_state="executing",
                        report_overrides=None):
        plan = {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "policy": "offline-dual", "transactionId": OFFLINE_TX,
            "model": OFFLINE_MODEL, "fromSlot": "a", "targetSlot": "b",
            "oldDeployment": OFFLINE_OLD, "newDeployment": OFFLINE_NEW,
            "originalBootSha256": dict(OFFLINE_ORIGINAL),
            "newBootSha256": dict(OFFLINE_NEW_BOOT),
        }
        raw = system_transaction._save_json(
            self.transaction, "plan.json", plan)
        digest = hashlib.sha256(raw).hexdigest()
        system_transaction._save_json(self.transaction, "prepared.json", {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "policy": "offline-dual", "transactionId": OFFLINE_TX,
            "plan": "plan.json", "planSha256": digest,
        })
        status = {
            "schemaVersion": 1, "component": "system",
            "operation": "offline-upgrade", "policy": "offline-dual",
            "bootPolicy": "dual-boot-no-slot-control",
            "transactionId": OFFLINE_TX, "model": OFFLINE_MODEL,
            "fromSlot": "a", "targetSlot": "b",
            "oldDeployment": OFFLINE_OLD, "newDeployment": OFFLINE_NEW,
            "planSha256": digest, "state": "awaiting_reboot",
            "phase": "awaiting_reboot", "requiresReboot": True,
            "bootWriteState": "written", "slotControlState": "not_issued",
        }
        status.update(status_overrides or {})
        system_transaction._save_json(self.transaction, "status.json", status)
        report = {
            "phase": "dual_boots_written", "model": OFFLINE_MODEL,
            "transactionUuid": OFFLINE_TX, "deployment": OFFLINE_NEW,
            "fromSlot": "a", "slotOrder": ["b", "a"],
            "writes": {
                slot: {
                    "sourceBootSha256": OFFLINE_ORIGINAL[slot],
                    "candidate": {"path": "/work/candidate-%s.img" % slot,
                                  "bytes": 4096,
                                  "sha256": OFFLINE_NEW_BOOT[slot]},
                    "bytesWritten": 4096,
                    "readbackSha256": OFFLINE_NEW_BOOT[slot],
                } for slot in ("a", "b")
            },
        }
        report.update(report_overrides or {})
        system_transaction._save_json(
            self.transaction, "write-report.json", report)
        system_transaction._save_json(self.session, "receipt.json", {
            "schemaVersion": 1, "sessionId": OFFLINE_SESSION_ID,
            "state": receipt_state, "model": OFFLINE_MODEL,
        })
        system_transaction._save_json(self.session, "ack.json", {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "transactionId": OFFLINE_TX, "planSha256": digest,
        })
        system_transaction._save_json(self.pending, OFFLINE_TX + ".json", {
            "schemaVersion": 1, "transactionId": OFFLINE_TX,
            "sessionId": OFFLINE_SESSION_ID,
        })
        self.digest = digest
        return status

    def trial(self, **overrides):
        body = {
            "state": "available", "recordState": "prepared",
            "runningDeployment": "old", "transactionId": OFFLINE_TX,
            "fromSlot": "a", "targetSlot": "b",
            "oldDeployment": OFFLINE_OLD, "newDeployment": OFFLINE_NEW,
            "planSha256": self.digest, "bootConfirmed": False,
        }
        body.update(overrides)
        return body

    def status(self, trial=None):
        with patch("armada_os.snapshot", return_value=snapshot()), patch.object(
            system_manager, "read_trial_status",
            return_value=self.trial() if trial is None else trial,
        ):
            return self.manager.status()

    def test_valid_offline_completion_reports_awaiting_reboot(self):
        result = self.status()
        self.assertEqual(result["state"], "awaiting_reboot")
        self.assertTrue(result["requiresReboot"])
        self.assertEqual(result["pendingTransactionId"], OFFLINE_TX)
        self.assertEqual(result["pendingFromSlot"], "a")
        self.assertEqual(result["pendingTargetSlot"], "b")

    def test_recorded_and_unrecorded_receipt_states_are_both_accepted(self):
        for state in ("execute_queued", "executing", "dispatch_uncertain",
                      "awaiting_reboot"):
            with self.subTest(state=state):
                self.write_documents(receipt_state=state)
                self.assertEqual(self.status()["state"], "awaiting_reboot")

    def test_foreign_journal_is_never_reported_as_awaiting_reboot(self):
        cases = (
            ({"transactionId": OFFLINE_OTHER_TX}, "pending_conflict"),
            ({"planSha256": "d" * 64}, "pending_conflict"),
            ({"fromSlot": "b", "targetSlot": "a"}, "pending_conflict"),
            ({"recordState": "trial"}, "pending_conflict"),
            ({"recordState": "prepared", "runningDeployment": "new"},
             "awaiting_confirmation"),
            ({"runningDeployment": None}, "pending_conflict"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                result = self.status(self.trial(**overrides))
                self.assertNotEqual(result["state"], "awaiting_reboot")
                self.assertEqual(result["state"], expected)
                self.assertFalse(result["requiresReboot"])

    def test_unverified_or_tampered_documents_are_refused(self):
        cases = (
            ("uncertain boot write", {"bootWriteState": "uncertain"}),
            ("slot control issued", {"slotControlState": "issued"}),
            ("no restart requirement", {"requiresReboot": False}),
            ("failed transaction", {"state": "recovery_required",
                                    "phase": "failed",
                                    "requiresReboot": False}),
            ("other deployment", {"newDeployment": "armada/" + "c" * 64 + ".2"}),
            ("other plan digest", {"planSha256": "d" * 64}),
            ("other model", {"model": "TB323FU"}),
        )
        for name, overrides in cases:
            with self.subTest(case=name):
                self.write_documents(status_overrides=overrides)
                result = self.status()
                self.assertEqual(result["state"], "pending_conflict")
                self.assertFalse(result["requiresReboot"])

    def test_write_evidence_must_match_the_plan(self):
        writes = {
            slot: {
                "sourceBootSha256": OFFLINE_ORIGINAL[slot],
                "candidate": {"path": "/work/%s.img" % slot, "bytes": 4096,
                              "sha256": OFFLINE_NEW_BOOT[slot]},
                "bytesWritten": 4096,
                "readbackSha256": OFFLINE_NEW_BOOT[slot],
            } for slot in ("a", "b")
        }

        def slot(slot, **changes):
            return dict(writes, **{slot: dict(writes[slot], **changes)})

        cases = (
            ("empty slot entries", {"writes": {"a": {}, "b": {}}}),
            ("missing slot", {"writes": {"a": writes["a"]}}),
            ("writes as a list", {"writes": []}),
            ("missing writes", {"writes": None}),
            ("running slot written first", {"slotOrder": ["a", "b"]}),
            ("source digest mismatch",
             {"writes": slot("b", sourceBootSha256=OFFLINE_NEW_BOOT["b"])}),
            ("readback digest mismatch",
             {"writes": slot("a", readbackSha256="3" * 64)}),
            ("short write", {"writes": slot("a", bytesWritten=1)}),
            ("candidate bytes mismatch",
             {"writes": slot("b", candidate={
                 "path": "/work/b.img", "bytes": 1,
                 "sha256": OFFLINE_NEW_BOOT["b"]})}),
            ("physical slot conflict",
             {"physical": {"model": OFFLINE_MODEL, "actualSlot": "b",
                           "inactiveSlot": "a"}}),
        )
        for name, overrides in cases:
            with self.subTest(case=name):
                self.write_documents(report_overrides=overrides)
                result = self.status()
                self.assertEqual(result["state"], "pending_conflict")
                self.assertFalse(result["requiresReboot"])

    def test_matching_physical_evidence_is_accepted(self):
        self.write_documents(report_overrides={
            "physical": {"model": OFFLINE_MODEL, "actualSlot": "a",
                         "inactiveSlot": "b", "deployment": OFFLINE_OLD}})
        self.assertEqual(self.status()["state"], "awaiting_reboot")

    def test_pointer_is_required_instead_of_scanning_sessions(self):
        (self.pending / (OFFLINE_TX + ".json")).unlink()
        self.assertEqual(self.status()["state"], "pending_conflict")

    def test_pointer_cannot_select_an_arbitrary_or_foreign_session(self):
        for pointer in (
            {"schemaVersion": 1, "sessionId": OFFLINE_SESSION_ID},
            {"schemaVersion": 1, "transactionId": OFFLINE_OTHER_TX,
             "sessionId": OFFLINE_SESSION_ID},
            {"schemaVersion": 1, "transactionId": OFFLINE_TX,
             "sessionId": "../escape"},
            {"schemaVersion": 1, "transactionId": OFFLINE_TX,
             "sessionId": None},
            {"schemaVersion": True, "transactionId": OFFLINE_TX,
             "sessionId": OFFLINE_SESSION_ID},
            {"schemaVersion": 1, "transactionId": OFFLINE_TX,
             "sessionId": OFFLINE_SESSION_ID, "jobId": OFFLINE_TX},
        ):
            with self.subTest(pointer=pointer):
                system_transaction._save_json(
                    self.pending, OFFLINE_TX + ".json", pointer)
                self.assertEqual(self.status()["state"], "pending_conflict")

    def test_pointer_for_another_transaction_is_never_adopted(self):
        system_transaction._save_json(self.pending, OFFLINE_OTHER_TX + ".json", {
            "schemaVersion": 1, "transactionId": OFFLINE_OTHER_TX,
            "sessionId": OFFLINE_SESSION_ID,
        })
        result = self.status(self.trial(transactionId=OFFLINE_OTHER_TX))
        self.assertEqual(result["state"], "pending_conflict")
        self.assertFalse(result["requiresReboot"])

    def test_missing_pointer_directory_is_a_conflict_not_a_failure(self):
        (self.pending / (OFFLINE_TX + ".json")).unlink()
        self.pending.rmdir()
        result = self.status()
        self.assertEqual(result["state"], "pending_conflict")
        self.assertFalse(result["requiresReboot"])

    def test_session_documents_must_be_root_private(self):
        os.chmod(self.session, 0o777)
        self.addCleanup(os.chmod, self.session, 0o700)
        self.assertEqual(self.status()["state"], "pending_conflict")

    def test_terminal_receipt_state_is_not_revived(self):
        self.write_documents(receipt_state="failed")
        self.assertEqual(self.status()["state"], "pending_conflict")

    def test_symlinked_documents_are_refused(self):
        outside = self.root / "outside-plan.json"
        outside.write_bytes((self.transaction / "plan.json").read_bytes())
        (self.transaction / "plan.json").unlink()
        os.symlink(outside, self.transaction / "plan.json")
        self.assertEqual(self.status()["state"], "pending_conflict")


if __name__ == "__main__":
    unittest.main()
