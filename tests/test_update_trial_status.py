"""Behavioral tests for read-only update trial status integration."""

from contextlib import contextmanager
import copy
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import armada_os
import test_update_system_manager as sm_test
from updates import (
    boot_context,
    finalization_receipt,
    offline_completion,
    recovery_device,
    system_manager,
    trial_record,
    trial_status,
    trial_store,
)

PART_UUID = bytes.fromhex("10" * 16)
INST_UUID = bytes.fromhex("20" * 16)
TXN_UUID = bytes.fromhex("30" * 16)
OLD_COMMIT_BYTES = bytes.fromhex("a1" * 32)
NEW_COMMIT_BYTES = bytes.fromhex("b2" * 32)
OLD_COMMIT = OLD_COMMIT_BYTES.hex()
NEW_COMMIT = NEW_COMMIT_BYTES.hex()
BOOT_CSUM = "c3" * 32


def make_context(slot=0, commit=OLD_COMMIT, serial=1, boot_ver=1):
    raw_slot = struct.pack(">4I", 1, 1, slot, 0)
    cmdline = (
        f"armada.deployment=armada/{commit}.{serial} "
        f"ostree=/ostree/boot.{boot_ver}/armada/{BOOT_CSUM}/0\n"
    )
    booted = {"stateroot": "armada", "checksum": commit, "serial": serial}
    return boot_context.decode_boot_context(raw_slot, cmdline, booted)


def make_record(
    state=trial_record.TRIAL,
    from_slot=0,
    target_slot=1,
    remaining=None,
    attempt_limit=3,
    old_serial=1,
    new_serial=2,
    policy=trial_record.ONLINE_SINGLE,
):
    if remaining is None:
        remaining = attempt_limit if state == trial_record.PREPARED else attempt_limit - 1
    other_boot_sha256 = (
        bytes.fromhex("17" * 32)
        if policy == trial_record.OFFLINE_DUAL
        else b"\x00" * 32
    )
    rec = trial_record.TrialRecord(
        sequence=9,
        partition_uuid=PART_UUID,
        installation_uuid=INST_UUID,
        transaction_uuid=TXN_UUID,
        state=state,
        from_slot=from_slot,
        target_slot=target_slot,
        remaining=remaining,
        attempt_limit=attempt_limit,
        old_serial=old_serial,
        new_serial=new_serial,
        os_name="armada",
        old_commit=OLD_COMMIT_BYTES,
        new_commit=NEW_COMMIT_BYTES,
        old_boot_sha256=bytes.fromhex("d4" * 32),
        new_boot_sha256=bytes.fromhex("e5" * 32),
        plan_sha256=bytes.fromhex("f6" * 32),
        policy=policy,
        other_boot_sha256=other_boot_sha256,
    )
    trial_record.encode_record(rec)
    return rec


class TrialStatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.marker_path = self.tmp / "armada,update-transaction"
        self.board = 1
        for target in (
            patch.object(trial_store, "save", side_effect=AssertionError("save called")),
            patch.object(os, "pwrite", side_effect=AssertionError("pwrite called")),
        ):
            target.start()
            self.addCleanup(target.stop)

    @contextmanager
    def patch_journal(self, record=None, open_error=None, load_error=None):
        @contextmanager
        def fake_open(*, writable=False):
            self.assertFalse(writable)
            if open_error is not None:
                raise open_error
            yield (99, str(uuid.UUID(bytes=PART_UUID)), self.board)

        def fake_load(fd, part_uuid, board):
            self.assertEqual((fd, part_uuid, board), (99, str(uuid.UUID(bytes=PART_UUID)), self.board))
            if load_error is not None:
                raise load_error
            return SimpleNamespace(record=record)

        with patch.object(recovery_device, "open_recovery", side_effect=fake_open) as m_open, \
             patch.object(trial_store, "load", side_effect=fake_load) as m_load:
            yield m_open, m_load

    def test_invalid_or_unavailable_context_opens_nothing(self):
        unavailable_context = {
            "state": "unavailable",
            "error": "boot context unavailable",
            "actualSlot": None,
            "deployment": None,
        }
        for ctx in (
            None,
            "bad",
            unavailable_context,
            {**make_context(), "actualSlot": "c"},
            {**make_context(), "deployment": ""},
        ):
            with self.patch_journal() as (m_open, m_load):
                res = trial_status.read_trial_status(ctx, self.marker_path)
            self.assertEqual(res["state"], "unavailable")
            self.assertTrue(res["error"])
            m_open.assert_not_called()
            m_load.assert_not_called()

    def test_empty_and_storage_or_codec_errors(self):
        ctx = make_context()
        with self.patch_journal(record=None):
            self.assertEqual(trial_status.read_trial_status(ctx, self.marker_path), {"state": "empty"})
        for err in (
            OSError("io fail"),
             RuntimeError("layout fail"),
            trial_record.TrialRecordError("corrupt crc"),
            subprocess.CalledProcessError(1, ["lsblk"]),
        ):
            with self.patch_journal(open_error=err):
                res = trial_status.read_trial_status(ctx, self.marker_path)
                self.assertEqual(res["state"], "unavailable")
            with self.patch_journal(load_error=err):
                res = trial_status.read_trial_status(ctx, self.marker_path)
                self.assertEqual(res["state"], "unavailable")

    def test_old_deployment_matching_and_marker_independence(self):
        rec = make_record(state=trial_record.PREPARED, from_slot=0, target_slot=1)
        with self.patch_journal(record=rec):
            res = trial_status.read_trial_status(make_context(0, OLD_COMMIT, 1, boot_ver=0), self.marker_path)
            self.assertEqual(res["runningDeployment"], "old")
            self.assertFalse(res["bootConfirmed"])
            self.assertNotIn("bootEvidenceError", res)
            for bad_ctx in (
                make_context(1, OLD_COMMIT, 1),
                make_context(0, NEW_COMMIT, 1),
                make_context(0, OLD_COMMIT, 9),
            ):
                mismatch = trial_status.read_trial_status(bad_ctx, self.marker_path)
                self.assertIsNone(mismatch["runningDeployment"])
                self.assertFalse(mismatch["bootConfirmed"])

    @staticmethod
    def _identity(trial):
        return {
            "transactionId": trial["transactionId"],
            "oldDeployment": trial["oldDeployment"],
            "newDeployment": trial["newDeployment"],
            "fromSlot": trial["fromSlot"],
            "targetSlot": trial["targetSlot"],
            "planSha256": trial["planSha256"],
        }

    def test_offline_prepared_still_on_old_deployment_is_evidence(self):
        """An offline dual transaction written but not rebooted runs old Linux.

        Before this was reported the offline session could never be bound to
        live evidence, so the completion verdict stayed unverified and the
        restart action was never offered.
        """
        rec = make_record(state=trial_record.PREPARED, from_slot=0, target_slot=1,
                          policy=trial_record.OFFLINE_DUAL)
        with self.patch_journal(record=rec):
            res = trial_status.read_trial_status(
                make_context(0, OLD_COMMIT, 1, boot_ver=0), self.marker_path)
        self.assertEqual(res["policy"], "offline-dual")
        self.assertEqual(res["runningDeployment"], "old")
        self.assertFalse(res["bootConfirmed"])
        self.assertNotIn("bootEvidenceError", res)

        # Only the live slot and deployment matching the journal's own from-slot
        # binding describe the old deployment; the slot selected for writing is
        # not evidence for it.
        for bad_ctx in (
            make_context(1, OLD_COMMIT, 1),
            make_context(0, OLD_COMMIT, 9),
        ):
            with self.patch_journal(record=rec):
                bad = trial_status.read_trial_status(bad_ctx, self.marker_path)
            self.assertIsNone(bad["runningDeployment"])
            self.assertFalse(bad["bootConfirmed"])

        # With mirrored writes the new deployment is also valid evidence when
        # observed on the from-slot after the reboot.
        self.marker_path.write_bytes(TXN_UUID)
        with self.patch_journal(record=rec):
            mirrored = trial_status.read_trial_status(
                make_context(0, NEW_COMMIT, 2), self.marker_path)
        self.assertEqual(mirrored["runningDeployment"], "new")

    def test_offline_trial_running_old_is_not_a_restart_candidate(self):
        # OFFLINE_DUAL never records ROLLED_BACK, and a TRIAL record that is
        # back on the old deployment means the boot attempt already happened;
        # that belongs to normal trial reconciliation, not to a restart offer.
        rec = make_record(state=trial_record.TRIAL, from_slot=0, target_slot=1,
                          policy=trial_record.OFFLINE_DUAL)
        with self.patch_journal(record=rec):
            res = trial_status.read_trial_status(make_context(0, OLD_COMMIT, 1), self.marker_path)
        self.assertIsNone(res["runningDeployment"])
        self.assertFalse(res["bootConfirmed"])

    def test_offline_prepared_old_reaches_awaiting_reboot_verdict(self):
        """Integration: real reader output feeds the real completion evaluator."""
        rec = make_record(state=trial_record.PREPARED, from_slot=0, target_slot=1,
                          policy=trial_record.OFFLINE_DUAL)
        with self.patch_journal(record=rec):
            trial = trial_status.read_trial_status(
                make_context(0, OLD_COMMIT, 1, boot_ver=0), self.marker_path)
        receipt = {"state": "awaiting_reboot"}
        verdict = offline_completion.evaluate(receipt, trial, self._identity(trial))
        self.assertEqual(verdict["state"], "awaiting_reboot")
        self.assertTrue(verdict["restartEligible"])
        self.assertIsNone(verdict["restartState"])
        self.assertEqual(verdict["transactionId"], str(uuid.UUID(bytes=TXN_UUID)))
        self.assertEqual(verdict["deployment"], f"armada/{NEW_COMMIT}.2")

        # A different live transaction is never accepted as this session's.
        other = copy.deepcopy(trial)
        other["transactionId"] = str(uuid.uuid4())
        mismatch = offline_completion.evaluate(receipt, other, self._identity(trial))
        self.assertEqual(mismatch["state"], "mismatch")
        self.assertFalse(mismatch["restartEligible"])

    def test_offline_confirmed_both_slot_rule_and_finalization(self):
        rec = make_record(state=trial_record.CONFIRMED, from_slot=0, target_slot=1,
                          remaining=2, policy=trial_record.OFFLINE_DUAL)
        self.marker_path.write_bytes(TXN_UUID)
        for slot in (1, 0):
            with self.patch_journal(record=rec), \
                 patch.object(finalization_receipt, "read",
                              return_value={"state": "complete"}) as m_read:
                trial = trial_status.read_trial_status(
                    make_context(slot, NEW_COMMIT, 2, boot_ver=0), self.marker_path)
            m_read.assert_called_once_with(rec)
            self.assertEqual(trial["runningDeployment"], "new")
            self.assertTrue(trial["bootConfirmed"])
            verdict = offline_completion.evaluate(
                {"state": "awaiting_reboot"}, trial, self._identity(trial))
            self.assertEqual(verdict["state"], "confirmed")

        with self.patch_journal(record=rec), \
             patch.object(finalization_receipt, "read",
                          return_value={"state": "pending", "error": "not yet"}):
            trial = trial_status.read_trial_status(
                make_context(1, NEW_COMMIT, 2, boot_ver=0), self.marker_path)
        verdict = offline_completion.evaluate(
            {"state": "awaiting_reboot"}, trial, self._identity(trial))
        self.assertEqual(verdict["state"], "finalizing")

        # Mirrored writes put the same new deployment in both physical slots,
        # so a boot observed on the from-slot with the new deployment is valid
        # evidence for the trial exactly like the target slot.
        prepared = make_record(state=trial_record.PREPARED, from_slot=0, target_slot=1,
                               policy=trial_record.OFFLINE_DUAL)
        with self.patch_journal(record=prepared):
            trial = trial_status.read_trial_status(
                make_context(0, NEW_COMMIT, 2, boot_ver=0), self.marker_path)
        self.assertEqual(trial["runningDeployment"], "new")
        self.assertFalse(trial["bootConfirmed"])

    def test_target_deployment_marker_validation_and_states(self):
        self.marker_path.write_bytes(TXN_UUID)
        ctx_new = make_context(1, NEW_COMMIT, 2, boot_ver=0)
        with self.patch_journal(record=make_record(state=trial_record.TRIAL)):
            res = trial_status.read_trial_status(ctx_new, self.marker_path)
            self.assertEqual(res["runningDeployment"], "new")
            self.assertFalse(res["bootConfirmed"])
            self.assertNotIn("bootEvidenceError", res)
        with self.patch_journal(record=make_record(state=trial_record.CONFIRMED)):
            res = trial_status.read_trial_status(ctx_new, self.marker_path)
            self.assertEqual(res["runningDeployment"], "new")
            self.assertTrue(res["bootConfirmed"])
            for wrong_ctx in (make_context(1, NEW_COMMIT, 7), make_context(0, NEW_COMMIT, 2)):
                bad = trial_status.read_trial_status(wrong_ctx, self.marker_path)
                self.assertIsNone(bad["runningDeployment"])
                self.assertFalse(bad["bootConfirmed"])

        link_path = self.tmp / "marker_link"
        link_path.symlink_to(self.marker_path)
        for path, payload in (
            (self.tmp / "missing", None),
            (self.marker_path, b"short"),
            (self.marker_path, b"\xff" * 16),
            (link_path, TXN_UUID),
        ):
            if payload is not None and path == self.marker_path:
                self.marker_path.write_bytes(payload)
            with self.patch_journal(record=make_record(state=trial_record.CONFIRMED)):
                bad = trial_status.read_trial_status(ctx_new, path)
            self.assertIsNone(bad["runningDeployment"])
            self.assertFalse(bad["bootConfirmed"])
            self.assertTrue(bad.get("bootEvidenceError"))

    def test_record_state_mappings_and_canonical_fields(self):
        cases = [
            (trial_record.PREPARED, "prepared", 0, 1, "a", "b", 3),
            (trial_record.TRIAL, "trial", 1, 0, "b", "a", 2),
            (trial_record.CONFIRMED, "confirmed", 0, 1, "a", "b", 1),
            (trial_record.ROLLED_BACK, "rolled_back", 1, 0, "b", "a", 0),
        ]
        for code, name, f_slot, t_slot, f_let, t_let, rem in cases:
            rec = make_record(state=code, from_slot=f_slot, target_slot=t_slot, remaining=rem)
            with self.patch_journal(record=rec):
                res = trial_status.read_trial_status(make_context(f_slot, OLD_COMMIT, 1), self.marker_path)
            self.assertEqual(res["recordState"], name)
            self.assertEqual((res["fromSlot"], res["targetSlot"]), (f_let, t_let))
            self.assertEqual(res["transactionId"], str(uuid.UUID(bytes=TXN_UUID)))
            self.assertEqual(res["oldDeployment"], f"armada/{OLD_COMMIT}.1")
            self.assertEqual(res["newDeployment"], f"armada/{NEW_COMMIT}.2")

    def test_system_manager_status_and_check_integration(self):
        dt = self.tmp / "compatible"
        dt.write_bytes(b"lenovo,elden\0lenovo,tb322fc\0")
        cfg = sm_test.write_config(sm_test.FULL_CHANNELS, self.tmp)
        mgr = system_manager.SystemManager(config_path=cfg, compatible_path=dt)
        snap = sm_test.snapshot(
            booted=sm_test.record(version="2.0.0", commit=NEW_COMMIT),
            channel="stable",
        )
        entry = sm_test.channel_entry(version="2.0.0", commit=NEW_COMMIT)
        ctx_new = make_context(1, NEW_COMMIT, 2)
        self.marker_path.write_bytes(TXN_UUID)

        def run_mgr(rec, marker=self.marker_path, load_err=None):
            orig_read = trial_status.read_trial_status
            with self.patch_journal(record=rec, load_error=load_err), \
                 patch.object(armada_os, "snapshot", side_effect=lambda: copy.deepcopy(snap)), \
                 patch.object(mgr, "_boot_context", return_value=copy.deepcopy(ctx_new)), \
                 patch.object(system_manager, "fetch_update", return_value={"channel": entry}), \
                 patch.object(system_manager, "read_trial_status", side_effect=lambda c: orig_read(c, marker)):
                return mgr.status(), mgr.check()

        for st in (trial_record.PREPARED, trial_record.TRIAL):
            status_res, check_res = run_mgr(make_record(state=st))
            self.assertEqual(status_res["state"], "awaiting_confirmation")
            self.assertEqual(check_res["state"], "awaiting_confirmation")
            self.assertFalse(status_res["requiresReboot"])
            self.assertFalse(status_res["trial"]["bootConfirmed"])

        with patch.object(
            finalization_receipt,
            "read",
            return_value={"state": "complete"},
        ):
            status_ok, check_ok = run_mgr(make_record(state=trial_record.CONFIRMED))
        self.assertEqual(status_ok["state"], "installed")
        self.assertEqual(check_ok["state"], "up_to_date")
        self.assertTrue(status_ok["trial"]["bootConfirmed"])
        self.assertTrue(check_ok["trial"]["bootConfirmed"])

        status_unavail, check_unavail = run_mgr(None, load_err=trial_record.TrialRecordError("bad journal"))
        self.assertEqual(status_unavail["trial"]["state"], "unavailable")
        self.assertFalse(status_unavail["trial"].get("bootConfirmed", False))
        self.assertNotEqual(status_unavail["state"], "awaiting_confirmation")
        self.assertEqual(check_unavail["trial"]["state"], "unavailable")

    def test_finalization_receipt_read_only_for_confirmed_new_deployment(self):
        self.marker_path.write_bytes(TXN_UUID)
        ctx_new = make_context(1, NEW_COMMIT, 2, boot_ver=0)
        ctx_old = make_context(0, OLD_COMMIT, 1, boot_ver=0)
        confirmed_rec = make_record(state=trial_record.CONFIRMED)

        for payload in (
            {"state": "complete", "transactionId": str(uuid.UUID(bytes=TXN_UUID))},
            {"state": "pending", "error": "Finalization receipt is missing"},
        ):
            with self.patch_journal(record=confirmed_rec), \
                 patch.object(finalization_receipt, "read", return_value=payload) as m_read:
                res = trial_status.read_trial_status(ctx_new, self.marker_path)
            m_read.assert_called_once_with(confirmed_rec)
            self.assertTrue(res["bootConfirmed"])
            self.assertEqual(res["finalization"], payload)

        for rec, ctx in (
            (make_record(state=trial_record.PREPARED), ctx_new),
            (make_record(state=trial_record.TRIAL), ctx_new),
            (make_record(state=trial_record.ROLLED_BACK), ctx_old),
            (confirmed_rec, ctx_old),
        ):
            with self.patch_journal(record=rec), \
                 patch.object(finalization_receipt, "read") as m_read:
                res = trial_status.read_trial_status(ctx, self.marker_path)
            m_read.assert_not_called()
            self.assertFalse(res["bootConfirmed"])
            self.assertNotIn("finalization", res)


if __name__ == "__main__":
    unittest.main()
