# SPDX-License-Identifier: GPL-3.0-only
"""Bounded retirement tests for system apply job artifacts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import job_retirement as jr  # noqa: E402

TX = "11111111-2222-4333-8444-555555555555"
OTHER_TX = "99999999-8888-4777-8666-555555555555"
PLAN = "e" * 64
OTHER_PLAN = "d" * 64
JOB = "abcdef12-1234-5678-9abc-def012345678"
OTHER_JOB = "12345678-1234-4678-9abc-def012345678"
MODEL = "TB322FC"
COMPACT_DOCUMENTS = (
    "status.json", "plan.json", "write-report.json",
    "candidate-a.json", "candidate-b.json",
)


class JobRetirementTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_root = self.tmp / "state"
        self.state_root.mkdir(mode=0o700)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        # Fixture paths belong to the invoking user; report them as root owned
        # so the ownership checks are exercised on a non-root test runner.
        fixture_owner = os.getuid()
        real_lstat = os.lstat
        real_path_lstat = Path.lstat

        def is_fixture(candidate):
            try:
                return Path(os.fsdecode(candidate)).is_relative_to(self.tmp)
            except (TypeError, ValueError):
                return False

        def as_root_owned(info):
            values = list(info)
            values[4] = 0
            return os.stat_result(values)

        def fixture_lstat(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            if info.st_uid == fixture_owner and is_fixture(candidate):
                return as_root_owned(info)
            return info

        def fixture_path_lstat(candidate):
            info = real_path_lstat(candidate)
            if info.st_uid == fixture_owner and is_fixture(candidate):
                return as_root_owned(info)
            return info

        for patcher in (
            mock.patch.object(os, "lstat", new=fixture_lstat),
            mock.patch.object(Path, "lstat", new=fixture_path_lstat),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed_job(self, *, job_id=JOB, pointer_job=None, with_pointer=True,
                 status=None, artifacts=True):
        if with_pointer:
            (self.state_root / jr.POINTER_NAME).write_text(json.dumps({
                "schemaVersion": 1,
                "jobId": pointer_job or job_id,
            }))
        job = self.state_root / job_id
        job.mkdir(mode=0o700)
        body = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
            "phase": "awaiting_reboot",
            "requiresReboot": True,
            "bootWriteState": "written",
            "slotControlState": "not_issued",
            "bootPolicy": "dual-boot-no-slot-control",
            "workDir": str(job),
            "transactionId": TX,
            "planSha256": PLAN,
            "model": MODEL,
        }
        body.update(status or {})
        for name in COMPACT_DOCUMENTS[1:]:
            (job / name).write_text("{}")
        (job / jr.STATUS_NAME).write_text(json.dumps(body))
        if artifacts:
            for name in jr.ARTIFACT_NAMES:
                directory = job / name
                directory.mkdir(mode=0o755)
                (directory / "blob.bin").write_bytes(b"x" * 32)
                nested = directory / "nested"
                nested.mkdir()
                (nested / "part.bin").write_bytes(b"y" * 16)
        return job

    def retire(self, transaction_id=TX, plan_sha256=PLAN):
        return jr.retire_finalized_job(
            transaction_id, plan_sha256=plan_sha256, state_root=self.state_root)

    def seed_failed_job(self, *, job_id=JOB, pointer_job=None,
                        with_pointer=True, status=None):
        body = {
            "state": "failed",
            "phase": "preparing_candidates",
            "requiresReboot": False,
            "bootWriteState": "not_started",
        }
        body.update(status or {})
        return self.seed_job(
            job_id=job_id, pointer_job=pointer_job,
            with_pointer=with_pointer, status=body)

    def retire_failed(self, transaction_id=TX):
        return jr.retire_failed_prewrite_job(
            transaction_id, state_root=self.state_root)

    def test_removes_artifacts_and_keeps_compact_documents(self):
        job = self.seed_job()
        before = {name: (job / name).read_bytes() for name in COMPACT_DOCUMENTS}
        pointer = (self.state_root / jr.POINTER_NAME).read_bytes()

        self.assertTrue(self.retire())

        for name in jr.ARTIFACT_NAMES:
            self.assertFalse((job / name).exists(), name)
            self.assertFalse((job / name).is_symlink(), name)
        for name in COMPACT_DOCUMENTS:
            self.assertEqual((job / name).read_bytes(), before[name], name)
        self.assertEqual(
            (self.state_root / jr.POINTER_NAME).read_bytes(), pointer)
        self.assertTrue(job.is_dir())

    def test_repeated_retirement_is_idempotent(self):
        job = self.seed_job()
        self.assertTrue(self.retire())
        self.assertFalse(self.retire())
        self.assertTrue(job.is_dir())
        for name in COMPACT_DOCUMENTS:
            self.assertTrue((job / name).is_file(), name)

    def test_missing_artifacts_are_not_an_error(self):
        job = self.seed_job(artifacts=False)
        self.assertFalse(self.retire())
        self.assertTrue(job.is_dir())

    def test_non_finalized_job_states_are_retained(self):
        cases = (
            ("failed", {"state": "failed", "phase": "boot_write",
                        "requiresReboot": False,
                        "bootWriteState": "restored"}),
            ("uncertain", {"state": "uncertain", "phase": "boot_write",
                           "requiresReboot": False,
                           "bootWriteState": "uncertain"}),
            ("recovery required", {"state": "recovery_required",
                                   "requiresReboot": False}),
            ("preparing", {"state": "preparing", "phase": "downloading",
                           "requiresReboot": False,
                           "bootWriteState": "not_started"}),
            ("not written", {"bootWriteState": "writing"}),
            ("reboot not required", {"requiresReboot": False}),
            ("slot control uncertain", {"slotControlState": "uncertain"}),
            ("foreign policy", {"bootPolicy": "legacy-single-slot"}),
            ("phase mismatch", {"phase": "complete"}),
        )
        for name, status in cases:
            with self.subTest(case=name):
                self.doCleanups()
                self.setUp()
                job = self.seed_job(status=status)
                self.assertFalse(self.retire())
                for artifact in jr.ARTIFACT_NAMES:
                    self.assertTrue((job / artifact).is_dir(), artifact)

    def test_identity_mismatches_are_retained(self):
        cases = (
            ("other transaction", {"transactionId": OTHER_TX}),
            ("other plan", {"planSha256": OTHER_PLAN}),
            ("other work dir", {"workDir": str(self.outside)}),
            ("missing transaction", {"transactionId": None}),
        )
        for name, status in cases:
            with self.subTest(case=name):
                self.doCleanups()
                self.setUp()
                job = self.seed_job(status=status)
                self.assertFalse(self.retire())
                self.assertTrue((job / "download").is_dir())

        self.doCleanups()
        self.setUp()
        self.seed_job()
        self.assertFalse(self.retire(plan_sha256=OTHER_PLAN))
        self.assertFalse(self.retire(transaction_id=OTHER_TX))
        self.assertFalse(self.retire(transaction_id="not-a-uuid"))
        self.assertFalse(self.retire(plan_sha256="not-a-digest"))

    def test_pointer_must_name_the_finalized_job(self):
        job = self.seed_job(pointer_job=OTHER_JOB)
        self.assertFalse(self.retire())
        self.assertTrue((job / "download").is_dir())

        self.doCleanups()
        self.setUp()
        self.seed_job(with_pointer=False)
        self.assertFalse(self.retire())
        self.assertTrue((self.state_root / JOB / "download").is_dir())

    def test_symlinked_job_directory_is_not_followed(self):
        job = self.seed_job()
        real = self.outside / "job-real"
        job.rename(real)
        job.symlink_to(real)
        self.assertFalse(self.retire())
        for name in jr.ARTIFACT_NAMES:
            self.assertTrue((real / name).is_dir(), name)

    def test_symlinked_artifact_directory_is_left_alone(self):
        job = self.seed_job()
        real = self.outside / "download-real"
        (job / "download").rename(real)
        (job / "download").symlink_to(real)
        self.assertTrue(self.retire())
        self.assertTrue((job / "download").is_symlink())
        self.assertTrue((real / "blob.bin").is_file())
        self.assertFalse((job / "trust").exists())

    def test_symlink_inside_artifact_is_unlinked_without_target_damage(self):
        job = self.seed_job()
        precious = self.outside / "precious.bin"
        precious.write_bytes(b"keep")
        (job / "download" / "link").symlink_to(precious)
        self.assertTrue(self.retire())
        self.assertFalse((job / "download").exists())
        self.assertEqual(precious.read_bytes(), b"keep")

    def test_untrusted_state_root_is_refused(self):
        job = self.seed_job()
        os.chmod(self.state_root, 0o777)
        try:
            self.assertFalse(self.retire())
        finally:
            os.chmod(self.state_root, 0o700)
        self.assertTrue((job / "download").is_dir())

        real = self.outside / "state-real"
        self.state_root.rename(real)
        self.state_root.symlink_to(real)
        self.assertFalse(self.retire())
        self.assertTrue((real / JOB / "download").is_dir())

    def test_symlinked_status_document_is_refused(self):
        job = self.seed_job()
        real = self.outside / "status.json"
        real.write_bytes((job / jr.STATUS_NAME).read_bytes())
        (job / jr.STATUS_NAME).unlink()
        (job / jr.STATUS_NAME).symlink_to(real)
        self.assertFalse(self.retire())
        self.assertTrue((job / "download").is_dir())

    def test_malformed_pointer_or_status_is_ignored(self):
        job = self.seed_job()
        pointer = self.state_root / jr.POINTER_NAME
        valid = pointer.read_text()
        for raw in (
            "{",
            json.dumps({"schemaVersion": True, "jobId": JOB}),
            json.dumps({"schemaVersion": 1, "jobId": JOB, "extra": 1}),
            json.dumps({"schemaVersion": 1, "jobId": "not-a-uuid"}),
            json.dumps({
                "schemaVersion": 1,
                "jobId": "00000000-0000-0000-0000-000000000000",
            }),
        ):
            pointer.write_text(raw)
            self.assertFalse(self.retire())
            self.assertTrue((job / "download").is_dir())
        pointer.write_text(valid)

        status = job / jr.STATUS_NAME
        valid_status = status.read_text()
        for raw in ("{", "[]", json.dumps({"schemaVersion": 2})):
            status.write_text(raw)
            self.assertFalse(self.retire())
            self.assertTrue((job / "download").is_dir())
        status.write_text(valid_status)

    def test_failed_prewrite_job_removes_only_artifact_trees(self):
        job = self.seed_failed_job()
        before = {name: (job / name).read_bytes() for name in COMPACT_DOCUMENTS}
        pointer = (self.state_root / jr.POINTER_NAME).read_bytes()

        self.assertTrue(self.retire_failed())

        for name in jr.ARTIFACT_NAMES:
            self.assertFalse((job / name).exists(), name)
            self.assertFalse((job / name).is_symlink(), name)
        for name in COMPACT_DOCUMENTS:
            self.assertEqual((job / name).read_bytes(), before[name], name)
        self.assertEqual(
            (self.state_root / jr.POINTER_NAME).read_bytes(), pointer)
        self.assertTrue(job.is_dir())
        self.assertFalse(self.retire_failed())

    def test_failed_prewrite_job_requires_a_prewrite_status(self):
        cases = (
            ("writing", {"phase": "writing_boot",
                         "bootWriteState": "writing"}),
            ("written", {"phase": "writing_boot",
                         "bootWriteState": "written"}),
            ("uncertain", {"phase": "writing_boot",
                           "bootWriteState": "uncertain"}),
            ("restored", {"phase": "writing_boot",
                          "bootWriteState": "restored"}),
            ("restored not needed", {"phase": "writing_boot",
                                     "bootWriteState": "not_started"}),
            ("recovery required", {"state": "recovery_required",
                                   "phase": "writing_boot"}),
            ("reboot required", {"requiresReboot": True}),
            ("active", {"state": "preparing", "phase": "downloading"}),
            ("finalized", {"state": "awaiting_reboot",
                           "phase": "awaiting_reboot",
                           "requiresReboot": True,
                           "bootWriteState": "written"}),
        )
        for name, status in cases:
            with self.subTest(case=name):
                self.doCleanups()
                self.setUp()
                job = self.seed_failed_job(status=status)
                self.assertFalse(self.retire_failed())
                for artifact in jr.ARTIFACT_NAMES:
                    self.assertTrue((job / artifact).is_dir(), artifact)

    def test_failed_prewrite_identity_and_pointer_must_match(self):
        job = self.seed_failed_job(status={"transactionId": OTHER_TX})
        self.assertFalse(self.retire_failed())
        self.assertTrue((job / "download").is_dir())

        self.doCleanups()
        self.setUp()
        job = self.seed_failed_job(status={"workDir": str(self.outside)})
        self.assertFalse(self.retire_failed())
        self.assertTrue((job / "download").is_dir())

        self.doCleanups()
        self.setUp()
        job = self.seed_failed_job(pointer_job=OTHER_JOB)
        self.assertFalse(self.retire_failed())
        self.assertTrue((job / "download").is_dir())

        self.doCleanups()
        self.setUp()
        self.seed_failed_job(with_pointer=False)
        self.assertFalse(self.retire_failed())
        self.assertTrue((self.state_root / JOB / "download").is_dir())

        self.doCleanups()
        self.setUp()
        job = self.seed_failed_job()
        self.assertFalse(self.retire_failed(transaction_id=OTHER_TX))
        self.assertFalse(self.retire_failed(transaction_id="not-a-uuid"))
        self.assertTrue((job / "download").is_dir())

    def test_failed_prewrite_malformed_status_is_ignored(self):
        job = self.seed_failed_job()
        status = job / jr.STATUS_NAME
        valid = status.read_text()
        for raw in ("{", "[]", json.dumps({"schemaVersion": 2})):
            status.write_text(raw)
            self.assertFalse(self.retire_failed())
            self.assertTrue((job / "download").is_dir())
        status.write_text(valid)

    def test_failed_prewrite_untrusted_paths_are_refused(self):
        job = self.seed_failed_job()
        os.chmod(self.state_root, 0o777)
        try:
            self.assertFalse(self.retire_failed())
        finally:
            os.chmod(self.state_root, 0o700)
        self.assertTrue((job / "download").is_dir())

        with mock.patch.object(jr, "_owned_by_root", return_value=False):
            self.assertFalse(self.retire_failed())
        self.assertTrue((job / "download").is_dir())

        real = self.outside / "job-real"
        job.rename(real)
        job.symlink_to(real)
        self.assertFalse(self.retire_failed())
        for name in jr.ARTIFACT_NAMES:
            self.assertTrue((real / name).is_dir(), name)

        self.doCleanups()
        self.setUp()
        self.seed_failed_job()
        real = self.outside / "state-real"
        self.state_root.rename(real)
        self.state_root.symlink_to(real)
        self.assertFalse(self.retire_failed())
        self.assertTrue((real / JOB / "download").is_dir())

    def test_failed_prewrite_symlinks_are_unlinked_without_target_damage(self):
        job = self.seed_failed_job()
        precious = self.outside / "precious.bin"
        precious.write_bytes(b"keep")
        (job / "download" / "link").symlink_to(precious)
        self.assertTrue(self.retire_failed())
        self.assertFalse((job / "download").exists())
        self.assertEqual(precious.read_bytes(), b"keep")
        for name in COMPACT_DOCUMENTS:
            self.assertTrue((job / name).is_file(), name)

    def test_confirmation_retires_both_artifact_stores_best_effort(self):
        from updates import trial_confirmation as tc

        record = SimpleNamespace(
            transaction_uuid=uuid.UUID(TX).bytes,
            plan_sha256=bytes.fromhex(PLAN))
        online = mock.patch.object(
            jr, "retire_finalized_job", side_effect=OSError("online"))
        offline = mock.patch.object(
            jr, "retire_confirmed_offline_session",
            side_effect=RuntimeError("offline"))
        with online as online_mock, offline as offline_mock:
            tc._retire_job_artifacts(record)
        online_mock.assert_called_once_with(TX, plan_sha256=PLAN)
        offline_mock.assert_called_once_with(record)

    def test_offline_retirement_needs_a_confirmed_record(self):
        self.assertFalse(jr.retire_confirmed_offline_session(
            SimpleNamespace(state=1), base=self.tmp))


if __name__ == "__main__":
    unittest.main()
