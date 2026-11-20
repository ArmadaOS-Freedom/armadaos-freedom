"""Regression tests for the new data-loss-sensitive ARMADA_BOOT disk switch selector."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from updates import (
    boot_disk_generation,
    boot_disk_selector,
    boot_disk_switch,
    deployment_store,
)


_OLD_COMMIT = "a" * 64
_NEW_COMMIT = "b" * 64
_OLD_BINDING = f"default/{_OLD_COMMIT}.0"
_NEW_BINDING = f"default/{_NEW_COMMIT}.0"
_OLD_GEN = "gen-" + "1" * 32
_NEW_GEN = "gen-" + "2" * 32
_OLD_POINTER = f"/ostree/boot.0/default/{_OLD_COMMIT}/0"
_NEW_POINTER = f"/ostree/boot.1/default/{_NEW_COMMIT}/0"
_TID = "11111111-2222-3333-4444-555555555555"
_PLAN_SHA = "c" * 64


def _make_gen_dir(parent: Path, name: str, binding_bytes: bytes) -> Path:
    gen = parent / name
    gen.mkdir(parents=True, exist_ok=False)
    (gen / "binding.bin").write_bytes(binding_bytes)
    # Presence of the other members is irrelevant because verify_generation is patched.
    return gen


def _make_deployment(root: Path, binding: str) -> None:
    commit, serial = binding.split("/", 1)[1].split(".", 1)
    deploy = root / "ostree" / "deploy" / "default" / "deploy" / f"{commit}.{serial}"
    deploy.mkdir(parents=True, exist_ok=True)


class UpdateBootDiskSwitchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="armada-switch-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "sysroot"
        self.root.mkdir()
        # Real filesystem that acts as the ARMADA_BOOT mount for the test.
        self.boot = Path(self._tmp.name) / "boot-mount"
        self.boot.mkdir()
        (self.boot / "armada").mkdir()

        # seed and both gen dirs with actual encoded binding bytes.
        _make_gen_dir(self.boot / "armada", _OLD_GEN,
                      boot_disk_generation.encode_binding(_OLD_POINTER, _OLD_BINDING))
        _make_gen_dir(self.boot / "armada", _NEW_GEN,
                      boot_disk_generation.encode_binding(_NEW_POINTER, _NEW_BINDING))

        # Actual deployment directories for old and new.
        _make_deployment(self.root, _OLD_BINDING)
        _make_deployment(self.root, _NEW_BINDING)

        # Selector symlink starts at the old generation.
        os.symlink(_OLD_GEN, self.boot / "armada" / "boot")

        self.initial = {
            "bootSource": "armada_boot",
            "model": "TB322FC",
            "deployment": _OLD_BINDING,
        }
        self.trusted_key = b"test-key"

        # Patch mount resolution only; verification is stubbed separately.
        self._resolve_patcher = mock.patch.object(
            boot_disk_switch, "_resolve_boot", return_value=self.boot,
        )
        self._resolve_patcher.start()
        self.addCleanup(self._resolve_patcher.stop)

        # Real verify_generation contains signature/manifest checks exercised by
        # integration tests; here we substitute to keep this a focused regression.
        self._verify_patcher = mock.patch.object(
            boot_disk_generation, "verify_generation", return_value=None,
        )
        self.verify_mock = self._verify_patcher.start()
        self.addCleanup(self._verify_patcher.stop)

        # Stub promote_confirmed_deployment; record exact order of arguments.
        self._promote_calls = []
        def _promote(*, sysroot_path, current_binding, previous_binding):
            self._promote_calls.append(
                (sysroot_path, current_binding, previous_binding))
        self._promote_patcher = mock.patch.object(
            deployment_store, "promote_confirmed_deployment", side_effect=_promote,
        )
        self._promote_patcher.start()
        self.addCleanup(self._promote_patcher.stop)

    # ------------------------------------------------------------------ helpers

    def _make_plan(self):
        return {
            "expected": _OLD_GEN,
            "target": _NEW_GEN,
            "currentDeployment": _OLD_BINDING,
            "newDeployment": _NEW_BINDING,
            "model": "TB322FC",
        }

    def _make_receipt(self):
        return {
            "transactionId": _TID,
            "planSha256": _PLAN_SHA,
            "fromSlot": "a",
            "targetSlot": "b",
        }

    def _read_selector(self) -> str:
        return os.readlink(os.fspath(self.boot / "armada" / "boot"))

    def _read_pending(self):
        path = self.boot / "armada" / "pending-switch.json"
        if not path.exists():
            return None
        return json.loads(path.read_bytes())

    # -------------------------------------------------------------------- tests

    def test_activate_writes_pending_and_switches_selector(self):
        plan = self._make_plan()
        receipt = self._make_receipt()

        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )

        pending = self._read_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["expected"], _OLD_GEN)
        self.assertEqual(pending["target"], _NEW_GEN)
        self.assertEqual(pending["currentDeployment"], _OLD_BINDING)
        self.assertEqual(pending["newDeployment"], _NEW_BINDING)
        self.assertEqual(pending["model"], "TB322FC")
        self.assertEqual(pending["receipt"], receipt)
        self.assertEqual(self._read_selector(), _NEW_GEN)

    def test_read_trial_status_transitions(self):
        plan = self._make_plan()
        receipt = self._make_receipt()
        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )

        status = boot_disk_switch.read_trial_status(
            {"deployment": _OLD_BINDING}, root=self.root,
        )
        self.assertEqual(status["state"], "available")
        self.assertEqual(status["recordState"], "prepared")
        self.assertEqual(status["runningDeployment"], "old")
        self.assertFalse(status["bootConfirmed"])
        self.assertEqual(status["oldDeployment"], _OLD_BINDING)
        self.assertEqual(status["newDeployment"], _NEW_BINDING)
        self.assertEqual(status["transactionId"], _TID)
        self.assertEqual(status["planSha256"], _PLAN_SHA)

        # Synthetic witness: caller reports running the new deployment.
        status = boot_disk_switch.read_trial_status(
            {"deployment": _NEW_BINDING}, root=self.root,
        )
        self.assertEqual(status["state"], "available")
        self.assertEqual(status["recordState"], "prepared")
        self.assertEqual(status["runningDeployment"], "new")
        self.assertFalse(status["bootConfirmed"])

    def test_confirm_promotes_and_finalizes(self):
        plan = self._make_plan()
        receipt = self._make_receipt()
        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )

        new_context = dict(self.initial, deployment=_NEW_BINDING)
        promoted = boot_disk_switch.confirm(
            self.root, new_context, trusted_key=self.trusted_key,
        )
        self.assertTrue(promoted)

        # promote_confirmed_deployment hit exactly once with exact order.
        self.assertEqual(
            self._promote_calls,
            [(self.root, _NEW_BINDING, _OLD_BINDING)],
        )

        finished = json.loads(
            (self.boot / "armada" / "finished-switch.json").read_bytes())
        self.assertEqual(finished["receipt"]["transactionId"], _TID)
        self.assertEqual(finished["currentDeployment"], _OLD_BINDING)
        self.assertEqual(finished["newDeployment"], _NEW_BINDING)

        # Pending cleared, completed retained, previous generation retained.
        self.assertFalse((self.boot / "armada" / "pending-switch.json").exists())
        self.assertTrue((self.boot / "armada" / "finished-switch.json").exists())
        self.assertTrue((self.boot / "armada" / _OLD_GEN).exists())
        self.assertEqual(self._read_selector(), _NEW_GEN)

    def test_rollback_plan_retains_old_generation(self):
        # Simulate the state after activate+confirm: new is selected, old exists,
        # and the rollback plan must target the exact old deployment.
        plan = self._make_plan()
        receipt = self._make_receipt()
        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )
        boot_disk_switch.confirm(
            self.root,
            dict(self.initial, deployment=_NEW_BINDING),
            trusted_key=self.trusted_key,
        )

        # Plan rollback from the new deployment back to the old one.
        rollback_initial = dict(self.initial, deployment=_NEW_BINDING)
        rollback = boot_disk_switch.plan_rollback(
            self.root, rollback_initial, _OLD_BINDING,
            trusted_key=self.trusted_key,
        )
        self.assertEqual(rollback["expected"], _NEW_GEN)
        self.assertEqual(rollback["target"], _OLD_GEN)
        self.assertEqual(rollback["currentDeployment"], _NEW_BINDING)
        self.assertEqual(rollback["newDeployment"], _OLD_BINDING)
        self.assertEqual(rollback["model"], "TB322FC")

    def test_activate_rename_succeeded_but_fsync_failed(self):
        """If the rename succeeds but post-rename fsync fails, activate returns
        success after verifying the exact on-disk state."""
        plan = self._make_plan()
        receipt = self._make_receipt()

        original_replace = os.replace
        def _replace(src, dst, *args, **kwargs):
            original_replace(src, dst, *args, **kwargs)
            if os.fspath(dst) == os.fspath(self.boot / "armada" / "boot"):
                raise OSError(5, "simulated post-rename fsync failure")

        with mock.patch.object(boot_disk_selector.os, "replace", side_effect=_replace):
            boot_disk_switch.activate(
                self.root, self.initial, plan,
                trusted_key=self.trusted_key, receipt=receipt,
            )

        self.assertEqual(self._read_selector(), _NEW_GEN)
        pending = self._read_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["target"], _NEW_GEN)
        self.assertEqual(pending["expected"], _OLD_GEN)

    def test_activate_final_readback_error_is_uncertain(self):
        """If the selector was changed but the final readback raises, activate
        must report BootDiskSwitchUncertainError rather than leaking the error."""
        plan = self._make_plan()
        receipt = self._make_receipt()

        real_read = boot_disk_switch._read_boot_active
        def _read_active(boot):
            result = real_read(boot)
            # The final readback happens after activate_generation succeeds;
            # the first few calls are prechecks. Raise once selector is new.
            if result[1] == _NEW_GEN:
                raise OSError("simulated final readback failure")
            return result

        with mock.patch.object(
            boot_disk_switch, "_read_boot_active", side_effect=_read_active,
        ):
            with self.assertRaises(boot_disk_switch.BootDiskSwitchUncertainError):
                boot_disk_switch.activate(
                    self.root, self.initial, plan,
                    trusted_key=self.trusted_key, receipt=receipt,
                )

    def test_activate_interrupted_before_rename(self):
        """If activate_generation raises after we write pending, selector stays old
        and ensure_ready clears only that interrupted pending state."""
        plan = self._make_plan()
        receipt = self._make_receipt()

        with mock.patch.object(
            boot_disk_selector, "activate_generation",
            side_effect=OSError("simulated interrupt before rename"),
        ):
            with self.assertRaises(OSError):
                boot_disk_switch.activate(
                    self.root, self.initial, plan,
                    trusted_key=self.trusted_key, receipt=receipt,
                )

        # Selector should still point at the old generation.
        self.assertEqual(self._read_selector(), _OLD_GEN)
        # Pre-existing behavior leaves the pending record behind.
        self.assertIsNotNone(self._read_pending())

        # ensure_ready resolves the interrupted state without raising because
        # selection remains at expected.
        boot_disk_switch.ensure_ready(self.root, self.initial)
        self.assertIsNone(self._read_pending())
        self.assertEqual(self._read_selector(), _OLD_GEN)

        # And there is no erroneous rollback entry on disk.
        self.assertFalse(
            (self.boot / "armada" / "finished-switch.json").exists())
        self.assertFalse((self.boot / "armada" / "pending-switch.json").exists())


if __name__ == "__main__":
    unittest.main()
