"""Regression tests for the new data-loss-sensitive ARMADA_BOOT disk switch selector."""

import json
import os
import shutil
import subprocess
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
_INSTALLED_RECOVERY_WANTS_PATH = boot_disk_switch.RECOVERY_WANTS_PATH


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

        # Real per-test recovery paths and a valid mocked systemd unit symlink so
        # the production capability check runs for real against temp files.
        self.recovery_version = Path(self._tmp.name) / "bootloader" / "entries" / "armada-boot.conf"
        self.recovery_service = Path(self._tmp.name) / "systemd" / "armada-boot-check.service"
        self.recovery_wants = (
            Path(self._tmp.name) / "systemd" / "multi-user.target.wants"
            / "armada-boot-check.service")
        self.recovery_version.parent.mkdir(parents=True)
        self.recovery_version.write_bytes(b"\x00\x00\x00\x01")
        self.recovery_service.parent.mkdir(parents=True, exist_ok=True)
        self.recovery_service.write_bytes(b"# unit")
        self.recovery_wants.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.fspath(self.recovery_service), os.fspath(self.recovery_wants))

        for name, value in (
            ("BOOT_RECOVERY_VERSION_PATH", self.recovery_version),
            ("RECOVERY_SERVICE_PATH", self.recovery_service),
            ("RECOVERY_WANTS_PATH", self.recovery_wants),
        ):
            patcher = mock.patch.object(boot_disk_switch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # Patch mount resolution only; verification is stubbed separately.
        self._resolve_patcher = mock.patch.object(
            boot_disk_switch, "_resolve_boot", return_value=self.boot,
        )
        self._resolve_patcher.start()
        self.addCleanup(self._resolve_patcher.stop)

        # Patch checkpointing so tests can assert exact call order and inject
        # failures without touching real transaction state.
        self._checkpoint_patcher = mock.patch.object(
            boot_disk_switch, "_checkpoint_boot",
        )
        self.checkpoint_mock = self._checkpoint_patcher.start()
        self.addCleanup(self._checkpoint_patcher.stop)

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
            "fromSlot": None,
            "targetSlot": None,
        }

    def _read_selector(self) -> str:
        return os.readlink(os.fspath(self.boot / "armada" / "boot"))

    def _read_pending(self):
        path = self.boot / "armada" / "pending-switch.json"
        if not path.exists():
            return None
        return json.loads(path.read_bytes())

    # -------------------------------------------------------------------- tests

    def test_systemctl_enable_real_unit_in_fake_root_creates_recovery_wants(self):
        """Real systemctl --root enables the recovery unit inside a fake root."""
        if shutil.which("systemctl") is None:
            self.skipTest("systemctl not available")
        fake_root = Path(self._tmp.name) / "fake-root"
        fake_root.mkdir()
        unit_src = (Path(__file__).resolve().parents[1]
                    / "system_files/usr/lib/systemd/system/armada-linux-recovery.service")
        unit_rel = Path("usr/lib/systemd/system/armada-linux-recovery.service")
        unit_dst = fake_root / unit_rel
        unit_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(unit_src, unit_dst)
        subprocess.run(
            ["systemctl", "--root", os.fspath(fake_root), "enable",
             "armada-linux-recovery.service"],
            check=True, capture_output=True,
        )
        wants_rel = _INSTALLED_RECOVERY_WANTS_PATH.relative_to("/")
        link = fake_root / wants_rel
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(os.fspath(link)),
                         "/usr/lib/systemd/system/armada-linux-recovery.service")

    def test_write_switch_durably_fsync_failure_cleans_tmp(self):
        """os.fsync failure propagates and leaves no pending or tmp artifact."""
        payload = {"schema": 1, "transactionId": _TID}
        with mock.patch.object(boot_disk_switch.os, "fsync",
                               side_effect=OSError("injected fsync failure")):
            with self.assertRaises(OSError):
                boot_disk_switch._write_switch_durably(
                    self.boot, boot_disk_switch._PENDING_NAME, dict(payload))
        armada = self.boot / "armada"
        self.assertFalse((armada / boot_disk_switch._PENDING_NAME).exists())
        self.assertEqual(
            [p.name for p in armada.iterdir()
             if p.name.startswith(".pending.") and p.name.endswith(".tmp")],
            [],
        )

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
        self.assertEqual(
            self.checkpoint_mock.call_args_list,
            [
                mock.call(self.boot, dry_run=True),
                mock.call(self.boot),
                mock.call(self.boot),
            ],
        )

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
            [(self.root, new_context["deployment"], self.initial["deployment"])],
        )

        finished = json.loads(
            (self.boot / "armada" / "finished-switch.json").read_bytes())
        self.assertEqual(finished["receipt"]["transactionId"], _TID)
        self.assertEqual(finished["currentDeployment"], _OLD_BINDING)
        self.assertEqual(finished["newDeployment"], _NEW_BINDING)
        self.assertEqual(finished["receipt"]["fromSlot"], None)
        self.assertEqual(finished["receipt"]["targetSlot"], None)

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

    def test_activate_dry_run_checkpoint_error_leaves_selector_old(self):
        """A checkpoint failure during the dry run must abort with a wrapped
        BootDiskSwitchError before the selector is switched."""
        plan = self._make_plan()
        receipt = self._make_receipt()

        self.checkpoint_mock.side_effect = OSError("simulated dry-run failure")

        with self.assertRaisesRegex(
            boot_disk_switch.BootDiskSwitchError, "checkpoint",
        ):
            boot_disk_switch.activate(
                self.root, self.initial, plan,
                trusted_key=self.trusted_key, receipt=receipt,
            )

        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertIsNone(self._read_pending())

    def test_activate_post_switch_checkpoint_error_is_uncertain(self):
        """If the post-switch checkpoint fails, activate must report
        BootDiskSwitchUncertainError with the selector switched and pending kept."""
        plan = self._make_plan()
        receipt = self._make_receipt()

        self.checkpoint_mock.side_effect = [None, None, OSError("simulated post failure")]

        with self.assertRaises(boot_disk_switch.BootDiskSwitchUncertainError):
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

    def test_recover_pending_broken_new_boot_restores_old(self):
        """A broken new boot recovers to the exact old generation without promoting."""
        plan = self._make_plan()
        receipt = self._make_receipt()
        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )
        self.assertEqual(self._read_selector(), _NEW_GEN)
        recovery = self.boot / "armada" / "recovery"
        self.assertTrue(recovery.is_symlink())
        self.assertEqual(os.readlink(recovery), _OLD_GEN)

        old_bytes = (self.boot / "armada" / _OLD_GEN / "binding.bin").read_bytes()
        new_bytes = (self.boot / "armada" / _NEW_GEN / "binding.bin").read_bytes()
        # Byte sentinels for preserved data areas beyond the generations.
        for name in ("userdata", "home", "boot_a", "boot_b", "slot-metadata"):
            (self.boot / "armada" / name).write_bytes(b"sentinel-" + name.encode())

        self.verify_mock.reset_mock()
        self._promote_calls.clear()
        recovered = boot_disk_switch.recover_pending(
            self.root, self.initial, trusted_key=self.trusted_key,
        )
        self.assertTrue(recovered)
        self.assertEqual(self._promote_calls, [])
        self.verify_mock.assert_called_once_with(
            self.boot / "armada" / _OLD_GEN, "TB322FC", trusted_key=self.trusted_key)
        self.assertFalse(recovery.exists() or recovery.is_symlink())
        self.assertIsNone(self._read_pending())
        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertTrue((self.boot / "armada" / _OLD_GEN).is_dir())
        self.assertTrue((self.boot / "armada" / _NEW_GEN).is_dir())
        self.assertEqual((self.boot / "armada" / _OLD_GEN / "binding.bin").read_bytes(), old_bytes)
        self.assertEqual((self.boot / "armada" / _NEW_GEN / "binding.bin").read_bytes(), new_bytes)
        for name in ("userdata", "home", "boot_a", "boot_b", "slot-metadata"):
            self.assertEqual(
                (self.boot / "armada" / name).read_bytes(),
                b"sentinel-" + name.encode(),
            )
        self.assertFalse((self.boot / "armada" / _NEW_GEN / "kernel.img").exists())

    def test_recover_pending_retry_old_selector_pointer_absent(self):
        """Retry with old selector + pointer already absent but pending still present."""
        plan = self._make_plan()
        receipt = self._make_receipt()
        boot_disk_switch.activate(
            self.root, self.initial, plan,
            trusted_key=self.trusted_key, receipt=receipt,
        )
        # Simulate partial recovery: selector restored, recovery pointer already gone.
        os.unlink(os.fspath(self.boot / "armada" / "recovery"))
        selector = self.boot / "armada" / "boot"
        os.unlink(os.fspath(selector))
        os.symlink(_OLD_GEN, selector)
        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertTrue((self.boot / "armada" / "pending-switch.json").exists())
        self._promote_calls.clear()
        self.verify_mock.reset_mock()
        recovered = boot_disk_switch.recover_pending(
            self.root, self.initial, trusted_key=self.trusted_key,
        )
        self.assertTrue(recovered)
        self.assertEqual(self._promote_calls, [])
        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertFalse((self.boot / "armada" / "recovery").exists())
        self.assertIsNone(self._read_pending())
        self.assertEqual(self.verify_mock.call_count, 1)
        self.assertEqual(self.verify_mock.call_args.args[0].name, _OLD_GEN)

    def test_recover_pending_rmtree_failure_keeps_failed_generation_then_ensure_ready_cleans(self):
        """A failed rmtree during recovery preserves the failed generation until ensure_ready."""
        boot_disk_switch.activate(
            self.root, self.initial, self._make_plan(),
            trusted_key=self.trusted_key, receipt=self._make_receipt(),
            reclaim_failed_target=True,
        )
        self.assertEqual(self._read_selector(), _NEW_GEN)
        self.assertIsNotNone(self._read_pending())
        self.assertTrue((self.boot / "armada" / _NEW_GEN).is_dir())

        real_rmtree = boot_disk_switch.shutil.rmtree
        calls = []

        def _failing_rmtree(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("simulated rmtree failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(
            boot_disk_switch.shutil, "rmtree", side_effect=_failing_rmtree,
        ):
            with self.assertRaisesRegex(
                boot_disk_switch.BootDiskSwitchError,
                "cannot remove failed target",
            ):
                boot_disk_switch.recover_pending(
                    self.root, self.initial, trusted_key=self.trusted_key,
                )

        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertIsNotNone(self._read_pending())
        self.assertTrue((self.boot / "armada" / _OLD_GEN).is_dir())
        self.assertTrue((self.boot / "armada" / _NEW_GEN).is_dir())

        boot_disk_switch.ensure_ready(self.root, self.initial)

        self.assertIsNone(self._read_pending())
        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertTrue((self.boot / "armada" / _OLD_GEN).is_dir())
        self.assertFalse((self.boot / "armada" / _NEW_GEN).exists())

    def test_recover_pending_awaiting_recovery_export_keeps_failed_target(self):
        """A pending awaiting_recovery_export transaction keeps the failed target."""
        from updates import offline_session  # local import; no new top-level imports.

        boot_disk_switch.activate(
            self.root, self.initial, self._make_plan(),
            trusted_key=self.trusted_key, receipt=self._make_receipt(),
            reclaim_failed_target=True,
        )
        self.assertEqual(self._read_selector(), _NEW_GEN)

        base = Path(self._tmp.name) / "offline-sessions"
        txn = base / ("d" * 32) / "transaction"
        txn.mkdir(parents=True)
        status = {"phase": "awaiting_recovery_export", "state": "awaiting_recovery_export"}
        plan = {"armadaBootSwitch": self._make_plan()}
        (txn / "status.json").write_text(json.dumps(status), encoding="utf-8")
        (txn / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

        with mock.patch.object(offline_session, "_DEFAULT_BASE", base):
            recovered = boot_disk_switch.recover_pending(
                self.root, self.initial, trusted_key=self.trusted_key,
            )

        self.assertTrue(recovered)
        self.assertIsNone(self._read_pending())
        self.assertEqual(self._read_selector(), _OLD_GEN)
        # Another plan could still execute against the failed target, so it is kept.
        self.assertTrue((self.boot / "armada" / _NEW_GEN).is_dir())
        self.assertTrue((self.boot / "armada" / _OLD_GEN).is_dir())

    def test_ensure_root_binding_retries_ostree_fsync(self):
        """First call fails fsyncing ostree parent; retry synchronizes both dirs."""
        ostree_dir = self.root / "ostree"
        armada_dir = ostree_dir / "armada"
        if armada_dir.exists():
            armada_dir.rmdir()

        real_fsync_dir = boot_disk_selector._io.fsync_dir
        calls = []
        state = {"failed": False}

        def _fsync_dir(path):
            calls.append(Path(path))
            if not state["failed"] and Path(path) == ostree_dir:
                state["failed"] = True
                raise OSError(5, "simulated ostree fsync failure")
            return real_fsync_dir(path)

        with mock.patch.object(boot_disk_selector._io, "fsync_dir", side_effect=_fsync_dir):
            with self.assertRaises(OSError):
                boot_disk_selector.ensure_root_binding(self.root, _OLD_BINDING)

            self.assertTrue(state["failed"])
            self.assertTrue(armada_dir.is_dir())

            calls.clear()
            result = boot_disk_selector.ensure_root_binding(self.root, _OLD_BINDING)

        self.assertEqual(result, f"/ostree/armada/{_OLD_BINDING.split('/', 1)[1]}")
        self.assertIn(armada_dir, calls)
        self.assertIn(ostree_dir, calls)
        self.assertLess(calls.index(armada_dir), calls.index(ostree_dir))

    def test_activate_missing_capability_leaves_old_selector(self):
        """Missing recovery capability aborts before any pending or pointer changes."""
        plan = self._make_plan()
        receipt = self._make_receipt()
        os.unlink(os.fspath(self.recovery_wants))
        with self.assertRaises(boot_disk_switch.BootDiskSwitchError):
            boot_disk_switch.activate(
                self.root, self.initial, plan,
                trusted_key=self.trusted_key, receipt=receipt,
            )
        self.assertEqual(self._read_selector(), _OLD_GEN)
        self.assertIsNone(self._read_pending())
        self.assertFalse((self.boot / "armada" / "recovery").exists())


if __name__ == "__main__":
    unittest.main()
