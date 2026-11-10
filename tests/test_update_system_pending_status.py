"""Offline regression tests for the pending-trial status patch."""

import contextlib
import copy
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

import armada_os
from updates import system_manager
import test_update_system_manager as sm_test

MODEL = "TB322FC"


def make_trial(record_state="prepared", running="old", state="available"):
    return {
        "state": state,
        "recordState": record_state,
        "runningDeployment": running,
        "transactionId": str(uuid.uuid4()),
        "fromSlot": "a",
        "targetSlot": "b",
        "oldDeployment": "armada/" + "a" * 64 + ".0",
        "newDeployment": "armada/" + "b" * 64 + ".0",
        "planSha256": "c" * 64,
    }


def make_receipt(work, trial, **overrides):
    receipt = {
        "schemaVersion": 1,
        "component": "system",
        "state": "awaiting_reboot",
        "phase": "awaiting_reboot",
        "requiresReboot": True,
        "bootWriteState": "written",
        "activationOutcome": "activated",
        "workDir": str(work),
        "model": MODEL,
        "transactionId": trial["transactionId"],
        "fromSlot": trial["fromSlot"],
        "targetSlot": trial["targetSlot"],
        "oldDeployment": trial["oldDeployment"],
        "newDeployment": trial["newDeployment"],
        "planSha256": trial["planSha256"],
    }
    receipt.update(overrides)
    return receipt


def write_state(root, job_id, receipt):
    root.mkdir(0o755)
    work = root / job_id
    work.mkdir(0o755)
    (work / "status.json").write_text(json.dumps(receipt))
    (root / "current.json").write_text(
        json.dumps({"schemaVersion": 1, "jobId": job_id}))
    return work


def fingerprint(path):
    info = path.lstat()
    return (path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_ino)


class PendingStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.dt = self.tmp / "compatible"
        self.dt.write_bytes(b"lenovo,elden\0lenovo,tb322fc\0")
        self.config = sm_test.write_config(sm_test.FULL_CHANNELS, self.tmp)
        self.state_root = self.tmp / "state"
        patcher = patch.object(
            system_manager, "SYSTEM_APPLY_STATE_ROOT", self.state_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.snap = sm_test.snapshot()
        self.boot = {"state": "available", "actualSlot": "a",
                     "inactiveSlot": "b", "deployment": None}
        # Receipt validation requires the state root to be owned by uid 0.
        # Report fixture paths owned by the invoking user as root owned while
        # preserving every other stat field, so nonroot runs still exercise
        # the receipt content checks instead of failing on ownership alone.
        fixture_owner = os.getuid()
        original_lstat = os.lstat
        original_path_lstat = Path.lstat
        original_fstat = os.fstat

        def is_fixture_path(candidate):
            try:
                return Path(os.fsdecode(candidate)).is_relative_to(self.tmp)
            except TypeError:
                return False

        def as_root_owned(info):
            values = list(info)
            values[4] = 0
            return os.stat_result(values)

        def fixture_lstat(candidate, *args, **kwargs):
            info = original_lstat(candidate, *args, **kwargs)
            if info.st_uid == fixture_owner and is_fixture_path(candidate):
                return as_root_owned(info)
            return info

        def fixture_path_lstat(candidate):
            info = original_path_lstat(candidate)
            if info.st_uid == fixture_owner and is_fixture_path(candidate):
                return as_root_owned(info)
            return info

        def fixture_fstat(fd):
            info = original_fstat(fd)
            if info.st_uid != fixture_owner:
                return info
            try:
                target = os.readlink("/proc/self/fd/%d" % fd)
            except OSError:
                return info
            if not is_fixture_path(target):
                return info
            return as_root_owned(info)

        for patcher in (
            patch.object(os, "lstat", new=fixture_lstat),
            patch.object(Path, "lstat", new=fixture_path_lstat),
            patch.object(os, "fstat", new=fixture_fstat),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def read(self, trial, op="status", fetch=None, configured=False):
        def boom(*args, **kwargs):
            raise AssertionError("network/config must not be touched")

        if fetch is None:
            fetch = boom
        manager = system_manager.SystemManager(
            config_path=self.config, compatible_path=self.dt)

        def snap_guard():
            return copy.deepcopy(self.snap)

        def trial_guard(*args, **kwargs):
            return copy.deepcopy(trial)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(
                armada_os, "snapshot", side_effect=snap_guard))
            stack.enter_context(patch.object(
                system_manager, "read_trial_status", side_effect=trial_guard))
            stack.enter_context(patch.object(
                manager, "_boot_context", return_value=self.boot))
            stack.enter_context(patch.object(
                system_manager, "fetch_update", side_effect=fetch))
            if not configured:
                stack.enter_context(patch.object(
                    system_manager, "_configuration", side_effect=boom))
            return getattr(manager, op)()

    def stage_valid(self, trial=None):
        trial = trial or make_trial()
        job_id = str(uuid.uuid4())
        work = write_state(self.state_root, job_id,
                           make_receipt(self.state_root / job_id, trial))
        return trial, job_id, work

    def test_valid_receipt_reports_awaiting_reboot(self):
        trial, _, work = self.stage_valid()
        pointer = self.state_root / "current.json"
        receipt = work / "status.json"
        before = (fingerprint(pointer), fingerprint(receipt),
                  stat.S_IMODE(self.state_root.lstat().st_mode))
        # Each read uses a freshly constructed manager (restart semantics).
        results = [self.read(trial, op) for op in ("status", "check")]
        results.append(self.read(trial))
        after = (fingerprint(pointer), fingerprint(receipt),
                 stat.S_IMODE(self.state_root.lstat().st_mode))
        self.assertEqual(before, after)
        for result in results:
            self.assertEqual(result["state"], "awaiting_reboot")
            self.assertTrue(result["requiresReboot"])
            self.assertEqual(result["trial"], trial)
            self.assertEqual(result["pendingTransactionId"],
                             trial["transactionId"])
            self.assertEqual(result["pendingFromSlot"], "a")
            self.assertEqual(result["pendingTargetSlot"], "b")

    def test_receipt_mismatches(self):
        trial, _, work = self.stage_valid()
        status_path = work / "status.json"
        base = json.loads(status_path.read_text())
        mutations = (
            ("transactionId", str(uuid.uuid4())),
            ("fromSlot", "b"),
            ("targetSlot", "a"),
            ("oldDeployment", "armada/" + "d" * 64 + ".0"),
            ("newDeployment", "armada/" + "e" * 64 + ".0"),
            ("planSha256", "f" * 64),
            ("model", "OTHER"),
            ("workDir", str(self.tmp / "other")),
            ("schemaVersion", True),
            ("schemaVersion", 2),
            ("component", "apps"),
            ("state", "completed"),
            ("phase", "completed"),
            ("requiresReboot", False),
            ("requiresReboot", 1),
            ("bootWriteState", "pending"),
            ("activationOutcome", "failed"),
        )
        for key, value in mutations:
            altered = copy.deepcopy(base)
            altered[key] = value
            status_path.write_text(json.dumps(altered))
            for op in ("status", "check"):
                res = self.read(trial, op)
                self.assertEqual(res["state"], "pending_conflict", (key, op))
                self.assertFalse(res["requiresReboot"], (key, op))

    def test_missing_malformed_and_pointer(self):
        def assert_conflict():
            for op in ("status", "check"):
                res = self.read(trial, op)
                self.assertEqual(res["state"], "pending_conflict")
                self.assertFalse(res["requiresReboot"])

        trial = make_trial()
        assert_conflict()
        self.assertFalse(self.state_root.exists())
        trial, job_id, work = self.stage_valid(trial)
        pointer = self.state_root / "current.json"
        status_path = work / "status.json"
        valid_pointer = pointer.read_text()
        pointer.unlink()
        assert_conflict()
        for raw in (
            "{",
            json.dumps({"schemaVersion": True, "jobId": job_id}),
            json.dumps({"schemaVersion": 1, "jobId": job_id, "extra": 1}),
            json.dumps({"schemaVersion": 1, "jobId": "not-a-uuid"}),
            json.dumps({"schemaVersion": 1, "jobId": "../" + job_id}),
            json.dumps({"schemaVersion": 1, "jobId": job_id.upper()}),
            json.dumps({"schemaVersion": 1, "jobId": str(uuid.UUID(int=0))}),
        ):
            pointer.write_text(raw)
            assert_conflict()
        pointer.write_text(valid_pointer)
        status_path.write_text("{")
        assert_conflict()
        status_path.unlink()
        assert_conflict()

    def test_path_restrictions(self):
        trial, _, work = self.stage_valid()
        ext = self.tmp / "external"
        ext.mkdir(0o755)
        status_path = work / "status.json"
        ext_status = ext / "status.json"
        status_path.rename(ext_status)
        status_path.symlink_to(ext_status)
        try:
            res = self.read(trial, "status")
            self.assertEqual(res["state"], "pending_conflict")
            self.assertFalse(res["requiresReboot"])
        finally:
            status_path.unlink()
            ext_status.rename(status_path)
        ext_work = ext / work.name
        work.rename(ext_work)
        work.symlink_to(ext_work)
        try:
            res = self.read(trial, "check")
            self.assertEqual(res["state"], "pending_conflict")
            self.assertFalse(res["requiresReboot"])
        finally:
            work.unlink()
            ext_work.rename(work)
        orig_mode = stat.S_IMODE(self.state_root.lstat().st_mode)
        self.state_root.chmod(0o777)
        try:
            res = self.read(trial, "status")
            self.assertEqual(res["state"], "pending_conflict")
            self.assertFalse(res["requiresReboot"])
        finally:
            self.state_root.chmod(orig_mode)

    def test_foreign_owned_receipt_root_is_rejected(self):
        trial, _, _ = self.stage_valid()
        real_lstat = os.lstat

        def foreign_owner(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            if Path(os.fsdecode(candidate)) == self.state_root:
                values = list(info)
                values[4] = 12345
                return os.stat_result(values)
            return info

        with patch.object(os, "lstat", new=foreign_owner):
            for op in ("status", "check"):
                res = self.read(trial, op)
                self.assertEqual(res["state"], "pending_conflict", op)
                self.assertFalse(res["requiresReboot"], op)

    def test_pending_new_and_old(self):
        for rec_state in ("prepared", "trial"):
            trial_new = make_trial(record_state=rec_state, running="new")
            for op in ("status", "check"):
                res = self.read(trial_new, op)
                self.assertEqual(res["state"], "awaiting_confirmation")
                self.assertFalse(res["requiresReboot"])
        self.assertFalse(self.state_root.exists())
        base_trial, _, _ = self.stage_valid()
        for bad in (
            dict(base_trial, recordState="trial", runningDeployment="old"),
            dict(base_trial, runningDeployment=None),
        ):
            for op in ("status", "check"):
                res = self.read(bad, op)
                self.assertEqual(res["state"], "pending_conflict")
                self.assertFalse(res["requiresReboot"])

    def test_confirmed_and_rolled_back_finalization_states(self):
        complete_trial = dict(
            make_trial(record_state="confirmed", running="new"),
            finalization={"state": "complete"},
        )
        status_res = self.read(complete_trial, "status")
        self.assertEqual(status_res["state"], "installed")
        self.assertEqual(status_res["lastUpdateState"], "confirmed")
        self.assertFalse(status_res["requiresReboot"])
        check_res = self.read(
            complete_trial,
            "check",
            fetch=lambda *a, **k: {"channel": sm_test.channel_entry()},
            configured=True,
        )
        self.assertEqual(check_res["state"], "update_available")
        self.assertEqual(check_res["lastUpdateState"], "confirmed")
        self.assertFalse(check_res["requiresReboot"])

        for finalization, expected_err in (
            (None, "Update finalization is not complete"),
            ({"state": "pending", "error": "receipt missing"}, "receipt missing"),
            ({"state": "pending"}, "Update finalization is not complete"),
        ):
            pending_trial = make_trial(record_state="confirmed", running="new")
            if finalization is not None:
                pending_trial["finalization"] = finalization
            for op in ("status", "check"):
                res = self.read(pending_trial, op)
                self.assertEqual(res["state"], "awaiting_confirmation")
                self.assertEqual(res["lastUpdateState"], "finalizing")
                self.assertEqual(res["error"], expected_err)
                self.assertFalse(res["requiresReboot"])

        for bad_running in ("old", None):
            bad_confirmed = make_trial(record_state="confirmed", running=bad_running)
            for op in ("status", "check"):
                res = self.read(bad_confirmed, op)
                self.assertEqual(res["state"], "pending_conflict")
                self.assertFalse(res["requiresReboot"])
                self.assertTrue(res.get("error"))

        rolled_back_ok = make_trial(record_state="rolled_back", running="old")
        rb_status = self.read(rolled_back_ok, "status")
        self.assertEqual(rb_status["state"], "installed")
        self.assertEqual(rb_status["lastUpdateState"], "rolled_back")
        self.assertFalse(rb_status["requiresReboot"])
        rb_check = self.read(
            rolled_back_ok,
            "check",
            fetch=lambda *a, **k: {"channel": sm_test.channel_entry()},
            configured=True,
        )
        self.assertEqual(rb_check["state"], "update_available")
        self.assertEqual(rb_check["lastUpdateState"], "rolled_back")
        self.assertFalse(rb_check["requiresReboot"])

        for bad_running in ("new", None):
            bad_rb = make_trial(record_state="rolled_back", running=bad_running)
            for op in ("status", "check"):
                res = self.read(bad_rb, op)
                self.assertEqual(res["state"], "pending_conflict")
                self.assertFalse(res["requiresReboot"])
                self.assertTrue(res.get("error"))


if __name__ == "__main__":
    unittest.main()
