"""Behavioral tests for SystemManager status/check."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

import armada_os
from updates import system_manager
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


if __name__ == "__main__":
    unittest.main()
