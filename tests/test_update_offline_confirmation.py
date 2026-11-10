# SPDX-License-Identifier: GPL-3.0-only
"""Focused offline-confirmation integration tests.

Composes the supplied TrialConfirmationTest and TrialStatusTests fixtures
instead of duplicating their suites."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import os
from pathlib import Path
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_trial_confirmation as conf_mod
import test_update_trial_status as status_mod
from updates import (
    boot_health,
    system_transaction,
    trial_confirmation as tc,
    trial_record as rec,
    trial_status,
    trial_store as store,
)

OTHER_HASH = bytes.fromhex("77" * 32)
BOOT_BYTES = 4194304


def _offline(r: rec.TrialRecord, **kw) -> rec.TrialRecord:
    return replace(
        r, policy=rec.OFFLINE_DUAL,
        other_boot_sha256=OTHER_HASH if "other_boot_sha256" not in kw else kw.pop("other_boot_sha256"),
        **kw,
    )


def _new_binding(r: rec.TrialRecord) -> str:
    return f"{r.os_name}/{r.new_commit.hex()}.{r.new_serial}"


class OfflineConfirmationTest(unittest.TestCase):
    def test_expected_boot_sha256_matrix(self) -> None:
        base = status_mod.make_record(state=rec.TRIAL, from_slot=0, target_slot=1)
        self.assertNotIn(OTHER_HASH, (base.new_boot_sha256, base.old_boot_sha256))
        cases = [
            (rec.ONLINE_SINGLE, rec.TRIAL, 1, base.new_boot_sha256),
            (rec.ONLINE_SINGLE, rec.CONFIRMED, 1, base.new_boot_sha256),
            (rec.OFFLINE_DUAL, rec.TRIAL, 1, base.new_boot_sha256),
            (rec.OFFLINE_DUAL, rec.CONFIRMED, 1, base.new_boot_sha256),
            (rec.OFFLINE_DUAL, rec.CONFIRMED, 0, OTHER_HASH),
        ]
        for policy, state, slot, want in cases:
            with self.subTest(policy=policy, state=state, slot=slot):
                r = _offline(base, state=state)
                if policy == rec.ONLINE_SINGLE:
                    r = replace(r, policy=policy, other_boot_sha256=b"\x00" * 32)
                self.assertEqual(rec.expected_boot_sha256(r, slot), want)
                self.assertNotEqual(want, r.old_boot_sha256)
        good = _offline(base, state=rec.CONFIRMED)
        for bad in (2, -1, True, "a", 0.0, None):
            with self.subTest(bad_slot=bad):
                with self.assertRaises(rec.TrialRecordError):
                    rec.expected_boot_sha256(good, bad)
        for state, remaining in ((rec.PREPARED, 3), (rec.ROLLED_BACK, 2)):
            with self.subTest(bad_state=state):
                r = replace(base, state=state, remaining=remaining)
                with self.assertRaises(rec.TrialRecordError):
                    rec.expected_boot_sha256(r, 1)
        with self.assertRaises(rec.TrialRecordError):
            rec.expected_boot_sha256(replace(base, target_slot=0), 0)

    def _collect(self, record, live_ctx, marker, boot_hash):
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(
                boot_health, "_read_live_evidence",
                return_value=({"booted": 1}, live_ctx, marker)))
            vp = st.enter_context(mock.patch.object(
                boot_health, "_verify_boot_payload",
                return_value=("6.12.0-armada", BOOT_BYTES)))
            st.enter_context(mock.patch.object(
                boot_health.recovery_device, "locate_recovery_partition",
                return_value=(0, 0, 0, 9)))
            loc = st.enter_context(mock.patch.object(
                boot_health, "_locate_boot_partition",
                return_value=(Path("/dev/boot_a"), 5)))
            hsh = st.enter_context(mock.patch.object(
                boot_health, "_hash_boot_partition", return_value=boot_hash))
            return boot_health.collect_boot_evidence(record), vp, loc, hsh

    def test_health_collect_offline_confirmed_from(self) -> None:
        r = _offline(status_mod.make_record(state=rec.CONFIRMED, from_slot=0, target_slot=1))
        ctx = {"actualSlot": "a", "deployment": _new_binding(r)}
        ev, vp, loc, hsh = self._collect(r, ctx, r.transaction_uuid, OTHER_HASH)
        self.assertEqual(
            (ev.boot_sha256, ev.actual_slot, ev.deployment),
            (OTHER_HASH, 0, _new_binding(r)),
        )
        loc.assert_called_once_with("a", 9, BOOT_BYTES)
        hsh.assert_called_once_with(Path("/dev/boot_a"), 5, BOOT_BYTES)

        with self.assertRaises(boot_health.BootHealthError):
            self._collect(r, ctx, r.transaction_uuid, r.new_boot_sha256)
        with self.assertRaises(boot_health.BootHealthError):
            self._collect(r, ctx, b"z" * 16, OTHER_HASH)

        pending = _offline(status_mod.make_record(state=rec.TRIAL, from_slot=0, target_slot=1))
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(
                boot_health, "_read_live_evidence",
                return_value=({"booted": 1}, ctx, pending.transaction_uuid)))
            vp2 = st.enter_context(mock.patch.object(
                boot_health, "_verify_boot_payload",
                return_value=("6.12.0-armada", BOOT_BYTES)))
            loc2 = st.enter_context(mock.patch.object(
                boot_health, "_locate_boot_partition",
                return_value=(Path("/dev/boot_a"), 5)))
            hsh2 = st.enter_context(mock.patch.object(
                boot_health, "_hash_boot_partition", return_value=OTHER_HASH))
            with self.assertRaises(boot_health.BootHealthError):
                boot_health.collect_boot_evidence(pending)
        vp2.assert_not_called()
        loc2.assert_not_called()
        hsh2.assert_not_called()
        online = status_mod.make_record(state=rec.CONFIRMED, from_slot=0, target_slot=1)
        with self.assertRaises(boot_health.BootHealthError):
            self._collect(online, ctx, online.transaction_uuid, online.new_boot_sha256)

    def _fixture(self) -> conf_mod.TrialConfirmationTest:
        fx = conf_mod.TrialConfirmationTest(
            methodName="test_sequence_overflow_fails_before_writable_open")
        fx.setUp()
        self.addCleanup(fx.doCleanups)
        return fx

    def _seed(self, fx, record) -> None:
        snap = store.load(fx.fx.fd, fx.fx.part, 1)
        store.save(fx.fx.fd, snap, record)
        fx.boot_ev = fx._valid_boot(record)

    def test_durable_journal_confirmation_then_confirmed_from(self) -> None:
        fx = self._fixture()
        trial = _offline(fx._trial_sample(from_slot=0, target_slot=1))
        self._seed(fx, trial)
        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pw:
            res = tc.confirm_trial()
        self.assertEqual(res["state"], "confirmed")
        self.assertEqual(pw.call_count, 1)
        confirmed = replace(trial, sequence=2, state=rec.CONFIRMED)
        got = store.load(fx.fx.fd, fx.fx.part, 1).record
        self.assertEqual(got, confirmed)
        self.assertEqual(got.other_boot_sha256, OTHER_HASH)
        fx.receipt_mock.assert_called_once()
        self.assertEqual(fx.receipt_mock.call_args.args[0], confirmed)
        fx.receipt_mock.reset_mock()

        fx.boot_ev = boot_health.BootEvidence(
            transaction_uuid=trial.transaction_uuid, actual_slot=0,
            deployment=_new_binding(trial), kernel_release="6.12.0-armada",
            boot_sha256=OTHER_HASH,
        )

        def mark_from_actual(record):
            return {
                "schemaVersion": 1, "phase": "slot_confirmed",
                "targetSlot": "ab"[fx.boot_ev.actual_slot],
                "deployment": fx.boot_ev.deployment,
                "transactionUuid": str(uuid.UUID(bytes=record.transaction_uuid)),
                "recordSequence": record.sequence,
            }

        def mark_wrong_slot(record):
            out = mark_from_actual(record)
            out["targetSlot"] = "ab"[1 - fx.boot_ev.actual_slot]
            return out

        fx.mark_mock.side_effect = mark_wrong_slot
        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pw:
            with self.assertRaises(tc.ConfirmationError):
                tc.confirm_trial()
        pw.assert_not_called()
        self.assertEqual(store.load(fx.fx.fd, fx.fx.part, 1).record, confirmed)
        self.receipt_mock_none(fx)

        fx.mark_mock.side_effect = mark_from_actual
        with mock.patch.object(os, "pwrite", wraps=os.pwrite) as pw:
            res = tc.confirm_trial()
        self.assertEqual(res, {"state": "not_pending", "recordState": rec.CONFIRMED})
        pw.assert_not_called()
        self.assertEqual(store.load(fx.fx.fd, fx.fx.part, 1).record, confirmed)
        fx.promote_mock.assert_called_with(**fx._promotion_kwargs(trial))
        kwargs = fx.promote_mock.call_args.kwargs
        self.assertEqual(
            kwargs["previous_binding"],
            f"armada/{trial.old_commit.hex()}.{trial.old_serial}",
        )
        self.assertEqual(kwargs["current_binding"], _new_binding(trial))

    def receipt_mock_none(self, fx) -> None:
        fx.receipt_mock.assert_not_called()

    def test_unconfirmed_from_rejected_before_writable_journal(self) -> None:
        fx = self._fixture()
        trial = _offline(fx._trial_sample(from_slot=0, target_slot=1))
        self._seed(fx, trial)
        fx.boot_ev = boot_health.BootEvidence(
            transaction_uuid=trial.transaction_uuid, actual_slot=0,
            deployment=_new_binding(trial), kernel_release="6.12.0-armada",
            boot_sha256=OTHER_HASH,
        )
        with self.assertRaises(tc.ConfirmationError):
            tc.confirm_trial()
        self.assertEqual(fx.open_calls, [False])
        self.assertEqual(store.load(fx.fx.fd, fx.fx.part, 1).record, trial)
        fx.mark_mock.assert_not_called()
        fx.promote_mock.assert_not_called()
        fx.receipt_mock.assert_not_called()

    def test_status_offline_confirmed(self) -> None:
        st = status_mod.TrialStatusTests(
            methodName="test_record_state_mappings_and_canonical_fields")
        st.setUp()
        self.addCleanup(st.doCleanups)
        st.marker_path.write_bytes(status_mod.TXN_UUID)
        confirmed = _offline(status_mod.make_record(state=rec.CONFIRMED, from_slot=0, target_slot=1))
        ctx_from_new = status_mod.make_context(0, status_mod.NEW_COMMIT, 2, boot_ver=0)
        ctx_target_new = status_mod.make_context(1, status_mod.NEW_COMMIT, 2, boot_ver=0)
        with st.patch_journal(record=confirmed):
            for ctx in (ctx_from_new, ctx_target_new):
                res = trial_status.read_trial_status(ctx, st.marker_path)
                self.assertEqual(res["runningDeployment"], "new")
                self.assertTrue(res["bootConfirmed"])
            bad = trial_status.read_trial_status(ctx_from_new, st.tmp / "missing")
            self.assertIsNone(bad["runningDeployment"])
            self.assertFalse(bad["bootConfirmed"])
            self.assertTrue(bad.get("bootEvidenceError"))
            old_ctx = status_mod.make_context(0, status_mod.OLD_COMMIT, 1, boot_ver=0)
            old = trial_status.read_trial_status(old_ctx, st.marker_path)
            self.assertIsNone(old["runningDeployment"])
            self.assertFalse(old["bootConfirmed"])
        pending = _offline(status_mod.make_record(state=rec.TRIAL, from_slot=0, target_slot=1))
        with st.patch_journal(record=pending):
            res = trial_status.read_trial_status(ctx_from_new, st.marker_path)
            self.assertIsNone(res["runningDeployment"])
            self.assertFalse(res["bootConfirmed"])
            self.assertNotIn("finalization", res)
        last = _offline(status_mod.make_record(state=rec.TRIAL, from_slot=0, target_slot=1, remaining=0))
        with st.patch_journal(record=last):
            res = trial_status.read_trial_status(ctx_target_new, st.marker_path)
            self.assertEqual(res["recordState"], "trial")
            self.assertEqual(res["remainingAttempts"], 0)
            self.assertEqual(res["runningDeployment"], "new")
            self.assertFalse(res["bootConfirmed"])
            self.assertNotIn("recoveryRequired", res)
            self.assertNotIn("finalization", res)

    def test_next_online_check_recovery(self) -> None:
        fx = self._fixture()
        confirmed = _offline(fx._trial_sample(state=rec.CONFIRMED, from_slot=0, target_slot=1))
        self._seed(fx, confirmed)

        @contextlib.contextmanager
        def fake_open(*, writable=False):
            self.assertFalse(writable)
            yield (fx.fx.fd, fx.fx.part, 1)

        initial = {"recoveryPartUuid": fx.fx.part, "model": "ModelX", "actualSlot": "a"}
        new_dep = ("armada", confirmed.new_commit.hex(), confirmed.new_serial)
        old_dep = ("armada", confirmed.old_commit.hex(), confirmed.old_serial)

        def run(model):
            with mock.patch.object(system_transaction.recovery_device, "open_recovery", fake_open), \
                 mock.patch.dict(system_transaction.physical_slots._MODEL, {1: model}):
                system_transaction._check_recovery(dict(initial), new_dep)

        run("ModelX")
        initial["actualSlot"] = "b"
        run("ModelX")
        initial["actualSlot"] = "a"
        with mock.patch.object(system_transaction.recovery_device, "open_recovery", fake_open), \
             mock.patch.dict(system_transaction.physical_slots._MODEL, {1: "ModelX"}):
            with self.assertRaises(ValueError):
                system_transaction._check_recovery(dict(initial), old_dep)
            pending = replace(confirmed, sequence=2, state=rec.TRIAL, remaining=1)
            store.save(fx.fx.fd, store.load(fx.fx.fd, fx.fx.part, 1), pending)
            with self.assertRaises(ValueError):
                system_transaction._check_recovery(dict(initial), new_dep)
        with self.assertRaises(ValueError):
            run("OtherModel")

        online = fx._trial_sample(sequence=3, state=rec.CONFIRMED, from_slot=0, target_slot=1)
        store.save(fx.fx.fd, store.load(fx.fx.fd, fx.fx.part, 1), online)
        with mock.patch.object(system_transaction.recovery_device, "open_recovery", fake_open), \
             mock.patch.dict(system_transaction.physical_slots._MODEL, {1: "ModelX"}):
            system_transaction._check_recovery(
                {**initial, "actualSlot": "b"}, new_dep)
            with self.assertRaises(ValueError):
                system_transaction._check_recovery(
                    {**initial, "actualSlot": "a"}, new_dep)


if __name__ == "__main__":
    unittest.main()
