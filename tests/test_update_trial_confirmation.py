# SPDX-License-Identifier: GPL-3.0-only
"""Integration tests for Linux update trial confirmation."""

from __future__ import annotations

import contextlib
import errno
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_trial_store as store_test_mod
from updates import boot_health, deployment_store, recovery_layout, runtime_health, trial_confirmation as tc, trial_record as rec, trial_store as store


class TrialConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = store_test_mod.TrialStoreTest(methodName="test_roundtrip_region_bounds")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

        self.lock_path = self.fx.path.parent / "armada-update.lock"
        self.open_calls: list[bool] = []
        self.reopen_uuid = self.fx.part
        self.reopen_board = 1

        @contextlib.contextmanager
        def fake_open_recovery(*, writable: bool = False):
            self.open_calls.append(writable)
            flags = os.O_RDWR if writable else os.O_RDONLY
            fd = os.open(self.fx.path, flags)
            try:
                yield (fd, self.reopen_uuid if writable else self.fx.part, self.reopen_board if writable else 1)
            finally:
                os.close(fd)

        self.boot_ev = self._valid_boot(self._trial_sample())
        self.runtime_ev = self._valid_runtime()
        self.boot_mock = mock.Mock(side_effect=lambda r: self.boot_ev)
        self.runtime_mock = mock.Mock(side_effect=lambda: self.runtime_ev)
        self.sleep_mock = mock.Mock()
        self.sysroot_path = self.fx.path.parent / "sysroot"
        self.promote_mock = mock.Mock(side_effect=self._promotion_result)
        self.receipt_mock = mock.Mock()

        for p in (
            mock.patch.object(tc, "UPDATE_LOCK_PATH", self.lock_path),
            mock.patch.object(tc, "SYSROOT_PATH", self.sysroot_path),
            mock.patch.object(tc, "open_recovery", fake_open_recovery),
            mock.patch.object(tc, "collect_boot_evidence", self.boot_mock),
            mock.patch.object(tc, "collect_runtime_evidence", self.runtime_mock),
            mock.patch.object(tc.deployment_store, "promote_confirmed_deployment", self.promote_mock),
            mock.patch.object(tc.finalization_receipt, "write", self.receipt_mock),
            mock.patch.object(tc.time, "sleep", self.sleep_mock),
            mock.patch.object(
                tc.job_retirement, "APPLY_STATE_ROOT",
                self.fx.path.parent / "no-apply-state"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _promotion_result(self, **kw) -> dict:
        return {
            "deployment": kw["current_binding"],
            "previousDeployment": kw["previous_binding"],
            "changed": False,
        }

    def _promotion_kwargs(self, r: rec.TrialRecord) -> dict:
        return dict(
            sysroot_path=self.sysroot_path,
            current_binding=f"{r.os_name}/{r.new_commit.hex()}.{r.new_serial}",
            previous_binding=f"{r.os_name}/{r.old_commit.hex()}.{r.old_serial}",
        )

    def _trial_sample(self, sequence: int = 1, **kw) -> rec.TrialRecord:
        defaults = dict(state=rec.TRIAL, remaining=2, attempt_limit=3)
        defaults.update(kw)
        return self.fx.sample(sequence=sequence, **defaults)

    def _valid_boot(self, r: rec.TrialRecord) -> boot_health.BootEvidence:
        return boot_health.BootEvidence(
            transaction_uuid=r.transaction_uuid,
            actual_slot=r.target_slot,
            deployment=f"{r.os_name}/{r.new_commit.hex()}.{r.new_serial}",
            kernel_release="6.12.0-armada",
            boot_sha256=r.new_boot_sha256,
        )

    def _valid_runtime(self) -> runtime_health.RuntimeEvidence:
        return runtime_health.RuntimeEvidence(
            boot_id="boot-1",
            session_id="2",
            uid=1000,
            display=":0",
            services=runtime_health.REQUIRED_UNITS,
        )

    def _seed_trial(self, **kw) -> rec.TrialRecord:
        snap = store.load(self.fx.fd, self.fx.part, 1)
        r = self._trial_sample(**kw)
        store.save(self.fx.fd, snap, r)
        self.boot_ev = self._valid_boot(r)
        return r

    def test_confirm_both_directions_exact_bounds_and_idempotent(self) -> None:
        for from_slot, target_slot, slot_name in ((0, 1, "b"), (1, 0, "a")):
            with self.subTest(target=slot_name):
                self.doCleanups()
                self.setUp()
                seeded = self._seed_trial(from_slot=from_slot, target_slot=target_slot)
                before_hdr = os.pread(self.fx.fd, store_test_mod.HEADER_BYTES, 0)
                before_arc = os.pread(self.fx.fd, 64, store_test_mod.HEADER_BYTES)
                before_reg = os.pread(self.fx.fd, recovery_layout.REGION_BYTES, recovery_layout.REGION_OFFSET)
                confirmed = replace(seeded, sequence=2, state=rec.CONFIRMED)

                def check_saved_before_promotion(**kw):
                    self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)
                    return self._promotion_result(**kw)

                self.promote_mock.side_effect = check_saved_before_promotion
                with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock:
                    res = tc.confirm_trial()
                self.assertEqual(pwrite_mock.call_count, 1)
                pwrite_args = pwrite_mock.call_args.args
                self.assertEqual(pwrite_args[2], recovery_layout.REGION_OFFSET + rec.TAIL_OFFSET_B)
                self.assertEqual(len(pwrite_args[1]), rec.RECORD_BYTES)
                self.assertEqual(res, {
                    "state": "confirmed",
                    "sequence": 2,
                    "transactionId": str(uuid.UUID(bytes=seeded.transaction_uuid)),
                    "bootSlot": slot_name,
                    "targetSlot": slot_name,
                    "deployment": f"{seeded.os_name}/{seeded.new_commit.hex()}.{seeded.new_serial}",
                    "bootPolicy": rec.BOOT_POLICY,
                    "slotControlState": "not_issued",
                })
                self.assertEqual(self.open_calls, [False, True])
                self.sleep_mock.assert_called_once_with(30)
                self.promote_mock.assert_called_once_with(**self._promotion_kwargs(seeded))

                after_hdr = os.pread(self.fx.fd, store_test_mod.HEADER_BYTES, 0)
                after_arc = os.pread(self.fx.fd, 64, store_test_mod.HEADER_BYTES)
                after_reg = os.pread(self.fx.fd, recovery_layout.REGION_BYTES, recovery_layout.REGION_OFFSET)
                self.assertEqual(before_hdr, after_hdr)
                self.assertEqual(before_arc, after_arc)
                self.assertEqual(before_reg[:rec.TAIL_OFFSET_B], after_reg[:rec.TAIL_OFFSET_B])
                self.assertEqual(
                    before_reg[rec.TAIL_OFFSET_B + rec.RECORD_BYTES:],
                    after_reg[rec.TAIL_OFFSET_B + rec.RECORD_BYTES:],
                )
                self.assertNotEqual(
                    before_reg[rec.TAIL_OFFSET_B:rec.TAIL_OFFSET_B + rec.RECORD_BYTES],
                    after_reg[rec.TAIL_OFFSET_B:rec.TAIL_OFFSET_B + rec.RECORD_BYTES],
                )

                snap = store.load(self.fx.fd, self.fx.part, 1)
                self.assertEqual(snap.selected, 1)
                self.assertEqual(snap.record, confirmed)

                self.open_calls.clear()
                with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock:
                    res2 = tc.confirm_trial()
                self.assertEqual(res2, {"state": "not_pending", "recordState": rec.CONFIRMED})
                self.assertEqual(self.open_calls, [False])
                pwrite_mock.assert_not_called()
                self.sleep_mock.assert_called_once_with(30)
                self.assertEqual(self.promote_mock.call_count, 2)
                self.promote_mock.assert_called_with(**self._promotion_kwargs(seeded))
                self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)

    def test_non_pending_states_skip_probes_and_writable_open(self) -> None:
        self.assertEqual(tc.confirm_trial(), {"state": "not_pending", "recordState": None})
        self.promote_mock.assert_not_called()
        for state in (rec.PREPARED, rec.ROLLED_BACK):
            snap = store.load(self.fx.fd, self.fx.part, 1)
            seq = 1 if snap.record is None else snap.record.sequence + 1
            rem = 3 if state == rec.PREPARED else (0 if state == rec.ROLLED_BACK else 2)
            store.save(self.fx.fd, snap, self._trial_sample(sequence=seq, state=state, remaining=rem))
            self.open_calls.clear()
            self.boot_mock.reset_mock()
            self.runtime_mock.reset_mock()
            self.assertEqual(tc.confirm_trial(), {"state": "not_pending", "recordState": state})
            self.assertEqual(self.open_calls, [False])
            self.boot_mock.assert_not_called()
            self.runtime_mock.assert_not_called()
            self.promote_mock.assert_not_called()

    def test_confirmed_state_rechecks_boot_and_promotes_without_probes(self) -> None:
        r = self._seed_trial(state=rec.CONFIRMED)
        self.assertEqual(
            tc.confirm_trial(), {"state": "not_pending", "recordState": rec.CONFIRMED}
        )
        self.assertEqual(self.open_calls, [False])
        self.boot_mock.assert_called_once()
        self.runtime_mock.assert_not_called()
        self.sleep_mock.assert_not_called()
        self.promote_mock.assert_called_once_with(**self._promotion_kwargs(r))

    def test_evidence_validation_failures_prevent_writable_open(self) -> None:
        r = self._seed_trial()
        vb = self._valid_boot(r)
        vr = self._valid_runtime()
        bad_boots = [
            "not-boot-evidence",
            replace(vb, actual_slot=0),
            replace(vb, deployment=f"os/{r.new_serial}"),
            replace(vb, transaction_uuid=b"x" * 16),
            replace(vb, boot_sha256=b"x" * 32),
            replace(vb, kernel_release=""),
        ]
        for b in bad_boots:
            self.open_calls.clear()
            self.boot_mock.side_effect = lambda _r, b=b: b
            with self.assertRaises(tc.ConfirmationError):
                tc.confirm_trial()
            self.assertEqual(self.open_calls, [False])

        self.boot_mock.side_effect = lambda _r: vb
        bad_runtimes = [
            "not-runtime-evidence",
            replace(vr, boot_id=""),
            replace(vr, session_id=""),
            replace(vr, display=""),
            replace(vr, uid=0),
            replace(vr, uid=True),
            replace(vr, services=("dbus.service",)),
        ]
        for rt in bad_runtimes:
            self.open_calls.clear()
            self.runtime_mock.side_effect = lambda rt=rt: rt
            with self.assertRaises(tc.ConfirmationError):
                tc.confirm_trial()
            self.assertEqual(self.open_calls, [False])

    def test_stability_window_drift_rejected_without_writable_open(self) -> None:
        r = self._seed_trial()
        vb = self._valid_boot(r)
        vr = self._valid_runtime()
        cases = [
            ([vb, replace(vb, kernel_release="6.12.1")], [vr, vr]),
            ([vb, vb], [vr, replace(vr, boot_id="boot-2")]),
            ([vb, vb], [vr, replace(vr, session_id="3")]),
            ([vb, vb], [vr, replace(vr, display=":1")]),
        ]
        for b_seq, r_seq in cases:
            self.open_calls.clear()
            self.sleep_mock.reset_mock()
            self.boot_mock.side_effect = list(b_seq)
            self.runtime_mock.side_effect = list(r_seq)
            with self.assertRaises(tc.ConfirmationError):
                tc.confirm_trial()
            self.sleep_mock.assert_called_once_with(30)
            self.assertEqual(self.open_calls, [False])
            self.promote_mock.assert_not_called()

    def test_concurrent_journal_change_or_reopen_identity_mismatch_rejected(self) -> None:
        r = self._seed_trial()
        snap = store.load(self.fx.fd, self.fx.part, 1)
        concurrent = replace(r, sequence=2, remaining=1)

        def sleep_and_modify(_secs: int) -> None:
            store.save(self.fx.fd, snap, concurrent)

        self.sleep_mock.side_effect = sleep_and_modify
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, concurrent)

        self.sleep_mock.side_effect = None
        self._seed_trial(sequence=3)
        self.reopen_uuid = "99999999-2222-4333-8444-555555555555"
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.reopen_uuid = self.fx.part
        self.reopen_board = 2
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()

    def test_sequence_overflow_fails_before_writable_open(self) -> None:
        r = self._trial_sample(sequence=(1 << 64) - 1)
        os.pwrite(self.fx.fd, rec.encode_record(r), recovery_layout.REGION_OFFSET + rec.TAIL_OFFSET_A)
        self.boot_ev = self._valid_boot(r)
        with self.assertRaises(rec.TrialRecordError):
            tc.confirm_trial()
        self.assertEqual(self.open_calls, [False])

    def test_early_wayland_readiness_retries_outside_lock_and_rechecks_evidence(self) -> None:
        self._seed_trial()
        runtime_valid = self._valid_runtime()
        self.runtime_mock.side_effect = [
            runtime_health.RuntimeNotReady("No login sessions reported"),
            runtime_health.RuntimeNotReady(
                "No candidate Wayland sockets in /run/user/1000"
            ),
            runtime_valid,
            runtime_valid,
        ]
        retry_sleeps = []

        def sleep_and_assert_unlocked(seconds: int) -> None:
            if seconds != tc.READINESS_RETRY_DELAY_SECONDS:
                return
            retry_sleeps.append(seconds)
            lfd = os.open(self.lock_path, os.O_RDWR)
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lfd, fcntl.LOCK_UN)
            finally:
                os.close(lfd)

        self.sleep_mock.side_effect = sleep_and_assert_unlocked
        self.assertEqual(tc.confirm_with_readiness_retry()["state"], "confirmed")
        self.assertEqual(
            retry_sleeps, [tc.READINESS_RETRY_DELAY_SECONDS] * 2
        )
        # Both not-ready attempts abort after their single pre-stability boot
        # check, while the final successful attempt checks boot twice (before
        # and after the stability window), so boot is collected four times.
        self.assertEqual(self.boot_mock.call_count, 4)
        self.assertEqual(self.runtime_mock.call_count, 4)
        self.promote_mock.assert_called_once()

    def test_fatal_runtime_evidence_failure_is_not_retried(self) -> None:
        self._seed_trial()
        self.runtime_mock.side_effect = runtime_health.RuntimeHealthError(
            "Unsafe runtime dir ownership/permissions on /run/user/1000"
        )
        with self.assertRaises(runtime_health.RuntimeHealthError):
            tc.confirm_with_readiness_retry()
        self.assertEqual(self.boot_mock.call_count, 1)
        self.assertEqual(self.runtime_mock.call_count, 1)
        self.sleep_mock.assert_not_called()
        self.promote_mock.assert_not_called()

    def test_lock_contention_and_symlink_rejected_before_partition_open(self) -> None:
        lfd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lfd)
        fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.assertEqual(self.open_calls, [])
        fcntl.flock(lfd, fcntl.LOCK_UN)

        self.lock_path.unlink()
        target = self.fx.path.parent / "real.lock"
        target.touch()
        self.lock_path.symlink_to(target)
        with self.assertRaises(OSError) as raised:
            tc.confirm_trial()
        self.assertEqual(raised.exception.errno, errno.ELOOP)
        self.assertEqual(self.open_calls, [])

    def test_io_or_verification_failure_propagates_and_releases_lock(self) -> None:
        self._seed_trial()
        with mock.patch.object(store.os, "fsync", side_effect=OSError("fsync boom")), mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock:
            with self.assertRaises(store.TrialStoreError):
                tc.confirm_trial()
        self.assertEqual(pwrite_mock.call_count, 1)
        self.assertEqual(self.open_calls, [False, True])

        lfd = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lfd, fcntl.LOCK_UN)
        finally:
            os.close(lfd)

        self.doCleanups()
        self.setUp()
        self._seed_trial()
        real_pread = os.pread
        read_offset = recovery_layout.REGION_OFFSET + rec.TAIL_OFFSET_B

        def flaky_pread(fd: int, n: int, offset: int) -> bytes:
            data = real_pread(fd, n, offset)
            if n == rec.RECORD_BYTES and offset == read_offset:
                return b"\x00" * len(data)
            return data

        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock, mock.patch.object(os, "pread", side_effect=flaky_pread):
            with self.assertRaises(store.TrialStoreError):
                tc.confirm_trial()
        self.assertEqual(pwrite_mock.call_count, 1)
        self.assertEqual(self.open_calls, [False, True])

        lfd = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lfd, fcntl.LOCK_UN)
        finally:
            os.close(lfd)

    def test_mirrored_origin_confirm_accepts_either_physical_slot(self) -> None:
        for actual in (0, 1):
            with self.subTest(actual=actual):
                self.doCleanups()
                self.setUp()
                r = self._seed_trial(
                    state=rec.TRIAL, remaining=2, policy=rec.OFFLINE_DUAL,
                    other_boot_sha256=b"9" * 32)
                self.boot_ev = replace(
                    self._valid_boot(r),
                    actual_slot=actual,
                    boot_sha256=(r.new_boot_sha256 if actual == r.target_slot
                                 else r.other_boot_sha256),
                )
                self.assertEqual(tc.confirm_trial()["state"], "confirmed")
                confirmed = store.load(self.fx.fd, self.fx.part, 1).record
                self.assertEqual(confirmed.state, rec.CONFIRMED)
                self.promote_mock.assert_called_once_with(**self._promotion_kwargs(r))

    def test_mirrored_origin_hash_mismatch_is_rejected(self) -> None:
        r = self._seed_trial(
            state=rec.TRIAL, remaining=2, policy=rec.OFFLINE_DUAL,
            other_boot_sha256=b"9" * 32)
        self.boot_ev = replace(
            self._valid_boot(r), actual_slot=r.from_slot, boot_sha256=b"8" * 32)
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.assertEqual(
            store.load(self.fx.fd, self.fx.part, 1).record, r)
        self.promote_mock.assert_not_called()
        self.receipt_mock.assert_not_called()
        self.assertEqual(self.open_calls, [False])

    def test_journal_promotion_receipt_order(self) -> None:
        r = self._seed_trial()
        events = []
        real_pwrite = os.pwrite
        self.promote_mock.side_effect = lambda **kw: events.append("promotion") or self._promotion_result(**kw)
        self.receipt_mock.side_effect = lambda record: events.append("receipt")
        with mock.patch.object(os, "pwrite", side_effect=lambda *a: events.append("journal") or real_pwrite(*a)):
            tc.confirm_trial()
        self.assertEqual(events, ["journal", "promotion", "receipt"])
        self.receipt_mock.assert_called_once_with(replace(r, sequence=2, state=rec.CONFIRMED))

    def test_promotion_failure_never_writes_receipt(self) -> None:
        self._seed_trial()
        self.promote_mock.side_effect = OSError("failure")
        with self.assertRaises(Exception):
            tc.confirm_trial()
        self.receipt_mock.assert_not_called()

    def _seed_apply_job(self, record, *, transaction_id=None, status=None):
        state_root = self.fx.path.parent / "apply-state"
        state_root.mkdir(mode=0o700)
        job = state_root / str(uuid.uuid4())
        job.mkdir(mode=0o700)
        (state_root / "current.json").write_text(json.dumps(
            {"schemaVersion": 1, "jobId": job.name}))
        body = {
            "schemaVersion": 1,
            "component": "system",
            "state": "awaiting_reboot",
            "phase": "awaiting_reboot",
            "requiresReboot": True,
            "bootWriteState": "written",
            "slotControlState": "not_issued",
            "bootPolicy": rec.BOOT_POLICY,
            "workDir": str(job),
            "transactionId": (
                transaction_id
                if transaction_id is not None
                else str(uuid.UUID(bytes=bytes(record.transaction_uuid)))),
            "planSha256": bytes(record.plan_sha256).hex(),
        }
        body.update(status or {})
        (job / "status.json").write_text(json.dumps(body))
        for name in ("download", "trust", "materials",
                     "candidate-a", "candidate-b"):
            (job / name).mkdir(mode=0o700)
            (job / name / "blob.bin").write_bytes(b"x" * 32)
        return state_root, job

    @staticmethod
    def _retirement_context(state_root):
        return (
            mock.patch.object(
                tc.job_retirement, "APPLY_STATE_ROOT", state_root),
            mock.patch.object(
                tc.job_retirement, "_owned_by_root", return_value=True),
        )

    def test_confirmation_retires_only_the_finalized_jobs_artifacts(self) -> None:
        record = self._seed_trial()
        state_root, job = self._seed_apply_job(record)
        root_patch, owner_patch = self._retirement_context(state_root)
        with root_patch, owner_patch:
            result = tc.confirm_trial()
        self.assertEqual(result["state"], "confirmed")
        for name in ("download", "trust", "materials",
                     "candidate-a", "candidate-b"):
            self.assertFalse((job / name).exists(), name)
        self.assertTrue((job / "status.json").is_file())
        self.assertTrue((state_root / "current.json").is_file())
        self.assertTrue(job.is_dir())

    def test_confirmed_retry_also_retires_artifacts(self) -> None:
        record = self._seed_trial(state=rec.CONFIRMED)
        state_root, job = self._seed_apply_job(record)
        root_patch, owner_patch = self._retirement_context(state_root)
        with root_patch, owner_patch:
            self.assertEqual(
                tc.confirm_trial(),
                {"state": "not_pending", "recordState": rec.CONFIRMED},
            )
        self.assertFalse((job / "download").exists())

    def test_unconfirmed_and_foreign_job_artifacts_are_retained(self) -> None:
        for name, seed in (
            ("other transaction",
             lambda record: {"transaction_id": str(uuid.uuid4())}),
            ("failed job",
             lambda record: {"status": {
                 "state": "failed", "requiresReboot": False,
                 "bootWriteState": "restored"}}),
            ("uncertain job",
             lambda record: {"status": {
                 "state": "uncertain", "requiresReboot": False,
                 "bootWriteState": "uncertain"}}),
        ):
            with self.subTest(case=name):
                self.doCleanups()
                self.setUp()
                record = self._seed_trial()
                state_root, job = self._seed_apply_job(record, **seed(record))
                root_patch, owner_patch = self._retirement_context(state_root)
                with root_patch, owner_patch:
                    self.assertEqual(
                        tc.confirm_trial()["state"], "confirmed")
                for artifact in ("download", "trust", "materials",
                                 "candidate-a", "candidate-b"):
                    self.assertTrue((job / artifact).is_dir(), artifact)

    def test_durable_receipt_is_required_before_retirement(self) -> None:
        record = self._seed_trial()
        state_root, job = self._seed_apply_job(record)
        self.receipt_mock.side_effect = OSError("receipt unavailable")
        root_patch, owner_patch = self._retirement_context(state_root)
        with root_patch, owner_patch, self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.assertTrue((job / "download").is_dir())

    def test_retirement_failure_leaves_confirmed_outcome_intact(self) -> None:
        for error in (OSError("busy"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                self.doCleanups()
                self.setUp()
                record = self._seed_trial()
                state_root, job = self._seed_apply_job(record)
                root_patch, owner_patch = self._retirement_context(state_root)
                with root_patch, owner_patch, mock.patch.object(
                    tc.job_retirement, "retire_finalized_job",
                    side_effect=error,
                ):
                    self.assertEqual(
                        tc.confirm_trial()["state"], "confirmed")
                self.assertTrue((job / "download").is_dir())

    def test_retirement_runs_after_the_receipt_is_written(self) -> None:
        self._seed_trial()
        events = []
        self.promote_mock.side_effect = (
            lambda **kw: events.append("promotion")
            or self._promotion_result(**kw))
        self.receipt_mock.side_effect = lambda record: events.append("receipt")
        with mock.patch.object(
            tc.job_retirement, "retire_finalized_job",
            side_effect=lambda *a, **k: events.append("retire"),
        ):
            tc.confirm_trial()
        self.assertEqual(events, ["promotion", "receipt", "retire"])

    def test_promotion_failure_preserves_confirmed_journal_then_retries(self) -> None:
        r = self._seed_trial()
        error = deployment_store.DeploymentError("sysroot busy")
        self.promote_mock.side_effect = error
        with self.assertRaises(tc.ConfirmationError) as raised:
            tc.confirm_trial()
        self.assertIs(raised.exception.__cause__, error)
        confirmed = replace(r, sequence=2, state=rec.CONFIRMED)
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)
        self.promote_mock.assert_called_once_with(**self._promotion_kwargs(r))
        self.assertEqual(self.boot_mock.call_count, 2)
        self.assertEqual(self.runtime_mock.call_count, 2)
        self.sleep_mock.assert_called_once_with(30)

        self.promote_mock.side_effect = self._promotion_result
        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock:
            self.assertEqual(
                tc.confirm_trial(),
                {"state": "not_pending", "recordState": rec.CONFIRMED},
            )
        pwrite_mock.assert_not_called()
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)
        self.assertEqual(self.promote_mock.call_count, 2)
        self.sleep_mock.assert_called_once_with(30)
        self.assertEqual(self.open_calls, [False, True, False])

    def test_receipt_failure_preserves_journal_and_retry_does_not_rewrite(self) -> None:
        r = self._seed_trial()
        self.receipt_mock.side_effect = OSError("receipt unavailable")
        with self.assertRaises(tc.ConfirmationError) as cm:
            tc.confirm_trial()
        self.assertIn("receipt unavailable", str(cm.exception))
        confirmed = replace(r, sequence=2, state=rec.CONFIRMED)
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)
        self.receipt_mock.side_effect = None
        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pwrite_mock:
            tc.confirm_trial()
        pwrite_mock.assert_not_called()
        self.assertEqual(self.receipt_mock.call_count, 2)
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, confirmed)

    def test_confirmed_bad_boot_rejects_before_promotion(self) -> None:
        r = self._seed_trial(state=rec.CONFIRMED)
        self.boot_mock.side_effect = lambda _r: replace(
            self.boot_ev, deployment="default/invalid.0"
        )
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.promote_mock.assert_not_called()
        self.runtime_mock.assert_not_called()
        self.sleep_mock.assert_not_called()
        self.assertEqual(store.load(self.fx.fd, self.fx.part, 1).record, r)

    def test_invalid_promotion_result_leaves_confirmed_journal_safe(self) -> None:
        r = self._seed_trial()
        self.promote_mock.side_effect = lambda **kw: {
            "deployment": kw["current_binding"],
            "previousDeployment": kw["previous_binding"],
            "changed": "yes",
        }
        with self.assertRaises(tc.ConfirmationError) as raised:
            tc.confirm_trial()
        self.assertNotIn("confirmed", str(raised.exception).lower())
        self.assertEqual(
            store.load(self.fx.fd, self.fx.part, 1).record,
            replace(r, sequence=2, state=rec.CONFIRMED),
        )
        self.promote_mock.assert_called_once_with(**self._promotion_kwargs(r))


if __name__ == "__main__":
    unittest.main()
