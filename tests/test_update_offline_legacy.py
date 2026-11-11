# SPDX-License-Identifier: GPL-3.0-only
"""Legacy (0.7) boot-context binding and OFFLINE-DUAL slot semantics."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zlib
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as release_fixture  # noqa: E402
import test_update_physical_slots as ab_fixture  # noqa: E402
from updates import (  # noqa: E402
    channel_selection, offline_context, offline_rpc, offline_session,
    physical_slots, system_transaction, trial_store,
)
from updates.package_contract import validate_package  # noqa: E402

OSNAME = "default"
CHECKSUM = "a" * 64
BOOTSUM = "b" * 64
SERIAL = 2
BOOT_SERIAL = 7
ROOT_UUID = "11111111-1111-4111-8111-111111111111"
BOOT_UUID = "22222222-2222-4222-8222-222222222222"
PART_UUID = "33333333-3333-4333-8333-333333333333"
LEGACY_DEPLOYMENT = "%s/%s.%d" % (OSNAME, CHECKSUM, SERIAL)
OPTIONS = (
    "root=UUID=%s boot=UUID=%s ostree=/ostree/boot.1/%s/%s/%d rw"
    % (ROOT_UUID, BOOT_UUID, OSNAME, BOOTSUM, BOOT_SERIAL)
)
LEGACY_REPORT = {
    "schemaVersion": 1,
    "phase": "physical_slots_checked",
    "model": "TB322FC",
    "actualSlot": None,
    "inactiveSlot": None,
    "selectedSlot": "b",
    "bootContext": "legacy-controller",
    "deployment": LEGACY_DEPLOYMENT,
    "bootableSlots": {"a": True, "b": True},
    "recoveryPartUuid": PART_UUID,
    "ufsController": "/sys/devices/ufs",
    "luns": [],
}
# The live legacy device that blocks the upgrade: the controller selected A
# (bootable) and B keeps a valid Android header but its GPT attribute is
# UNBOOTABLE, so the native inspector reports bootable_b false.  Identity may
# describe that device; a slot mutation may not proceed.
UNBOOTABLE_REPORT = dict(
    LEGACY_REPORT, selectedSlot="a", bootableSlots={"a": True, "b": False})


def _evidence(**overrides):
    body = {
        "osname": OSNAME,
        "checksum": CHECKSUM,
        "serial": SERIAL,
        "bootserial": BOOT_SERIAL,
        "bootcsum": BOOTSUM,
        "options": OPTIONS,
        "originRef": None,
        "staged": None,
        "rollback_queued": False,
        "booted": {"stateroot": OSNAME, "checksum": CHECKSUM, "serial": SERIAL,
                   "incompatible": False},
    }
    body.update(overrides)
    return body


class _FakeBootConfig:
    def __init__(self, options):
        self._options = options

    def get(self, key):
        if key != "options":
            raise KeyError(key)
        return self._options


class _FakeOrigin:
    def __init__(self, ref):
        self._ref = ref

    def get_string(self, group, key):
        if self._ref is None:
            raise KeyError(key)
        return self._ref


class _FakeDeployment:
    def __init__(self, *, options=OPTIONS, origin=None, osname=OSNAME,
                 checksum=CHECKSUM, serial=SERIAL, bootserial=BOOT_SERIAL,
                 bootcsum=BOOTSUM):
        self._options = options
        self._origin = origin
        self._osname = osname
        self._checksum = checksum
        self._serial = serial
        self._bootserial = bootserial
        self._bootcsum = bootcsum

    def get_osname(self):
        return self._osname

    def get_csum(self):
        return self._checksum

    def get_deployserial(self):
        return self._serial

    def get_bootserial(self):
        return self._bootserial

    def get_bootcsum(self):
        return self._bootcsum

    def get_bootconfig(self):
        return _FakeBootConfig(self._options)

    def get_origin(self):
        return None if self._origin is None else _FakeOrigin(self._origin)


class _FakeSysroot:
    def __init__(self, deployment, staged=None, deployments=None):
        self.deployment = deployment
        self.staged = staged
        self.deployments = deployments

    def load(self, cancellable):
        return True

    def get_staged_deployment(self):
        return self.staged

    def get_booted_deployment(self):
        return self.deployment

    def get_deployments(self):
        if self.deployments is not None:
            return list(self.deployments)
        return [] if self.deployment is None else [self.deployment]


def _fake_gi(sysroot, explicit=None):
    """Fake libostree: only an explicit path is allowed to use Sysroot.new."""
    gio = SimpleNamespace(File=SimpleNamespace(
        new_for_path=lambda path: SimpleNamespace(path=path)))
    glib = SimpleNamespace(Error=Exception)

    def new(gfile):
        if explicit is None:
            raise AssertionError('booted identity must use Sysroot.new_default')
        return explicit

    ostree = SimpleNamespace(
        Sysroot=SimpleNamespace(new_default=lambda: sysroot, new=new),
        DeploymentUnlockedState=SimpleNamespace(NONE=0))
    return gio, glib, ostree


class LegacyBootContextTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.sysroot = self.tmp / "sysroot"
        self.sysroot.mkdir()
        # Absent by default so the host's own selection never decides a test.
        self.state = self.tmp / "update-channel"

    def _cmdline(self, text=OPTIONS):
        path = self.tmp / "cmdline"
        path.write_text(text, encoding="utf-8")
        return path

    def _os_release(self, text="ID=armada\nVERSION_ID=42\n"):
        path = self.tmp / "os-release"
        path.write_text(text, encoding="utf-8")
        return path

    def _state(self, **kwargs):
        mounts = kwargs.pop("mounts", {offline_context.ROOT_MOUNT: ROOT_UUID,
                                       offline_context.BOOT_MOUNT: BOOT_UUID})
        cmdline_path = kwargs.pop("cmdline_path", None)
        if cmdline_path is None:
            cmdline_path = self._cmdline()
        # Resolve the default only when the caller did not supply a foreign
        # os-release; otherwise writing the default would clobber it.
        os_release_path = kwargs.pop("os_release_path", None)
        if os_release_path is None:
            os_release_path = self._os_release()
        reject_pending = kwargs.pop("reject_pending", True)
        return offline_context.read_legacy_boot_state(
            sysroot_path=self.sysroot,
            cmdline_path=cmdline_path,
            os_release_path=os_release_path,
            mount_lookup=lambda mount: mounts[mount],
            reject_pending=reject_pending)

    def test_missing_evidence_binds_libostree_without_an_actual_slot(self):
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence()):
            booted, context = self._state()
        self.assertEqual(booted, {"stateroot": OSNAME, "checksum": CHECKSUM,
                                  "serial": SERIAL, "incompatible": False})
        self.assertEqual(context, {
            "state": "available", "actualSlot": None, "inactiveSlot": None,
            "bootContext": "legacy-controller", "deployment": LEGACY_DEPLOYMENT,
        })

    def test_ostree_reader_uses_the_default_runtime_sysroot(self):
        deployment = _FakeDeployment(
            origin="ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
                   "armadaos-freedom:beta@sha256:" + "d" * 64)
        sysroot = _FakeSysroot(deployment)
        with mock.patch.object(offline_context, "_gi",
                               return_value=_fake_gi(sysroot)):
            evidence = offline_context._read_ostree_booted()
        self.assertEqual(evidence["osname"], OSNAME)
        self.assertEqual(evidence["checksum"], CHECKSUM)
        self.assertEqual(evidence["serial"], SERIAL)
        self.assertEqual(evidence["bootserial"], BOOT_SERIAL)
        self.assertEqual(evidence["bootcsum"], BOOTSUM)
        self.assertEqual(evidence["options"], OPTIONS)
        self.assertIn(":beta@sha256:", evidence["originRef"])
        self.assertNotIn("actualSlot", evidence)

    def test_ostree_reader_never_uses_sysroot_or_the_deployment_list(self):
        deployment = _FakeDeployment()
        # The real composefs runtime: /sysroot loads but reports no booted
        # deployment, while its deployment list is non-empty.
        sysroot_mount = _FakeSysroot(None, deployments=[_FakeDeployment()])
        runtime = _FakeSysroot(deployment)
        with mock.patch.object(
                offline_context, "_gi",
                return_value=_fake_gi(runtime, explicit=sysroot_mount)):
            self.assertEqual(
                offline_context._read_ostree_booted()["serial"], SERIAL)
            # The runtime root is the default sysroot, never Sysroot.new('/').
            self.assertEqual(
                offline_context._read_ostree_booted("/")["serial"], SERIAL)
            # An explicit /sysroot-style sysroot that reports no booted
            # deployment is refused; no booted deployment is inferred from its
            # deployment list.
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "no booted deployment"):
                offline_context._read_ostree_booted(self.sysroot)

    def test_ostree_reader_is_stable_when_a_deployment_is_appended(self):
        # Prepare appends this transaction's own deployment; the booted
        # identity must not move to the first entry of the deployment list.
        deployment = _FakeDeployment()
        runtime = _FakeSysroot(deployment, deployments=[deployment])
        with mock.patch.object(offline_context, "_gi",
                               return_value=_fake_gi(runtime)):
            before = offline_context._read_ostree_booted()
            runtime.deployments = [_FakeDeployment(options=OPTIONS.replace(
                "/%d " % BOOT_SERIAL, "/9 ")), deployment]
            after = offline_context._read_ostree_booted()
        self.assertEqual(before, after)
        self.assertEqual(after["serial"], SERIAL)
        self.assertEqual(after["bootserial"], BOOT_SERIAL)

    def test_ostree_reader_reports_a_staged_deployment_with_its_identity(self):
        # A staged deployment is a pending-update blocker, not an identity
        # defect: the reader still returns the booted deployment and reports
        # the staged one as data.
        sysroot = _FakeSysroot(
            _FakeDeployment(), staged=_FakeDeployment(checksum="c" * 64, serial=1))
        with mock.patch.object(
                offline_context, "_gi",
                return_value=_fake_gi(sysroot)):
            evidence = offline_context._read_ostree_booted()
        self.assertEqual(evidence["serial"], SERIAL)
        self.assertEqual(evidence["staged"], {
            "stateroot": OSNAME, "checksum": "c" * 64, "serial": 1,
            "incompatible": False})
        self.assertFalse(evidence["rollback_queued"])

    def test_staged_identity_binds_while_mutation_readers_refuse(self):
        # The identity query tolerates staging; the strict reader used by
        # preflight/prepare/execute/boot writer/activation still refuses it.
        sysroot = _FakeSysroot(_FakeDeployment(), staged=_FakeDeployment())
        with mock.patch.object(
                offline_context, "_gi",
                return_value=_fake_gi(sysroot, explicit=sysroot)):
            booted, context = self._state(reject_pending=False)
            self.assertEqual(booted["serial"], SERIAL)
            self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)
            self.assertIsNone(context["actualSlot"])
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "staged"):
                self._state()

    def test_queued_rollback_binds_for_identity_but_refuses_mutations(self):
        # A rollback queued after prepare is pending even though nothing is
        # staged.  The identity query reports it; every mutation caller refuses
        # it exactly like a staged deployment.
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence(rollback_queued=True)):
            booted, context = self._state(reject_pending=False)
            self.assertEqual(booted["serial"], SERIAL)
            self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)
            self.assertIsNone(context["actualSlot"])
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "rollback"):
                self._state()

    def test_cmdline_index_binds_the_boot_serial_not_the_deploy_serial(self):
        # The fixture intentionally uses serial=2 (deployment) and bootserial=7
        # (final ostree pointer index); comparing the deploy serial would fail.
        context = offline_context.bind_legacy_evidence(
            _evidence(), OPTIONS, ROOT_UUID, BOOT_UUID)
        self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "boot serial"):
            offline_context.bind_legacy_evidence(
                _evidence(bootserial=SERIAL), OPTIONS, ROOT_UUID, BOOT_UUID)

    def test_bootconfig_cmdline_and_mount_mismatches_fail(self):
        cases = {
            "ostree-checksum": dict(
                cmdline=OPTIONS.replace(BOOTSUM, "c" * 64)),
            "ostree-index": dict(
                cmdline=OPTIONS.replace("/%d " % BOOT_SERIAL,
                                        "/%d " % (BOOT_SERIAL + 1))),
            "boot-serial": dict(evidence=_evidence(bootserial=BOOT_SERIAL + 1)),
            "option-pointer": dict(
                evidence=_evidence(options=OPTIONS.replace(BOOTSUM, "e" * 64))),
            "missing-root": dict(
                cmdline=OPTIONS.replace("root=UUID=%s " % ROOT_UUID, "")),
            "root-mismatch": dict(
                mounts={offline_context.ROOT_MOUNT: BOOT_UUID,
                        offline_context.BOOT_MOUNT: BOOT_UUID}),
            "duplicate-ostree": dict(
                cmdline=OPTIONS + " ostree=/ostree/boot.1/%s/%s/%d"
                % (OSNAME, BOOTSUM, SERIAL)),
            "new-token": dict(
                cmdline=OPTIONS + " armada.deployment=%s" % LEGACY_DEPLOYMENT),
        }
        for label, case in cases.items():
            with self.subTest(label=label):
                evidence = case.get("evidence", _evidence())
                cmdline = case.get("cmdline", OPTIONS)
                mounts = case.get("mounts", {
                    offline_context.ROOT_MOUNT: ROOT_UUID,
                    offline_context.BOOT_MOUNT: BOOT_UUID})
                with self.assertRaises(offline_context.OfflineContextError):
                    offline_context.bind_legacy_evidence(
                        evidence, cmdline, mounts[offline_context.ROOT_MOUNT],
                        mounts[offline_context.BOOT_MOUNT],
                        sysroot_path=self.sysroot)

    def _pointer_options(self, *, bootver=1, index=BOOT_SERIAL, bootcsum=BOOTSUM,
                         root_uuid=ROOT_UUID, boot_uuid=BOOT_UUID):
        return ("root=UUID=%s boot=UUID=%s ostree=/ostree/boot.%d/%s/%s/%d rw"
                % (root_uuid, boot_uuid, bootver, OSNAME, bootcsum, index))

    def _boot_entry(self, *, bootver=1, index=BOOT_SERIAL, bootcsum=BOOTSUM,
                    target_checksum=CHECKSUM, target_serial=SERIAL):
        """Create the BLS symlink a rewritten boot entry would leave behind."""
        deploy = (self.sysroot / "ostree" / "deploy" / OSNAME / "deploy"
                  / ("%s.%d" % (target_checksum, target_serial)))
        deploy.mkdir(parents=True, exist_ok=True)
        entry_dir = (self.sysroot / "ostree" / ("boot.%d.0" % bootver)
                     / OSNAME / bootcsum)
        entry_dir.mkdir(parents=True, exist_ok=True)
        version_link = self.sysroot / "ostree" / ("boot.%d" % bootver)
        if not version_link.is_symlink() and not version_link.exists():
            version_link.symlink_to("boot.%d.0" % bootver)
        (entry_dir / str(index)).symlink_to(os.path.relpath(deploy, entry_dir))
        return deploy

    def test_rewritten_bls_pointer_after_append_still_binds(self):
        # append_verified_deployment/write_deployments may rewrite the BLS entry
        # (boot version and/or index) while /proc/cmdline keeps the pointer the
        # running boot used.  Both rewrites still name the booted deployment.
        cases = (
            ("boot-version", 0, BOOT_SERIAL, 1, BOOT_SERIAL),
            ("entry-index", 1, BOOT_SERIAL, 1, BOOT_SERIAL + 2),
        )
        for label, cmd_version, cmd_index, opt_version, opt_index in cases:
            with self.subTest(label=label):
                self._boot_entry(bootver=opt_version, index=opt_index)
                cmdline = self._pointer_options(bootver=cmd_version, index=cmd_index)
                options = self._pointer_options(bootver=opt_version, index=opt_index)
                context = offline_context.bind_legacy_evidence(
                    _evidence(options=options, bootserial=opt_index),
                    cmdline, ROOT_UUID, BOOT_UUID, sysroot_path=self.sysroot)
                self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)
                self.assertIsNone(context["actualSlot"])

    def test_rewritten_bls_pointer_to_another_deployment_is_rejected(self):
        # Same osname and boot serial, but the current pointer names a different
        # deployment: identity is never inferred from the index or the list.
        self._boot_entry(bootver=1, index=BOOT_SERIAL + 2,
                         target_checksum="c" * 64, target_serial=1)
        options = self._pointer_options(bootver=1, index=BOOT_SERIAL + 2)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "not the booted deployment"):
            offline_context.bind_legacy_evidence(
                _evidence(options=options, bootserial=BOOT_SERIAL + 2),
                self._pointer_options(bootver=0), ROOT_UUID, BOOT_UUID,
                sysroot_path=self.sysroot)

    def test_rewritten_bls_pointer_with_wrong_identity_is_rejected(self):
        self._boot_entry(bootver=1, index=BOOT_SERIAL + 2)
        wrong_checksum = self._pointer_options(
            bootver=1, index=BOOT_SERIAL + 2, bootcsum="c" * 64)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "checksum"):
            offline_context.bind_legacy_evidence(
                _evidence(options=wrong_checksum, bootserial=BOOT_SERIAL + 2),
                self._pointer_options(bootver=0), ROOT_UUID, BOOT_UUID,
                sysroot_path=self.sysroot)
        options = self._pointer_options(bootver=1, index=BOOT_SERIAL + 2)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "/sysroot mount UUID"):
            offline_context.bind_legacy_evidence(
                _evidence(options=options, bootserial=BOOT_SERIAL + 2),
                self._pointer_options(bootver=0), BOOT_UUID, BOOT_UUID,
                sysroot_path=self.sysroot)

    def test_rewritten_bls_pointer_must_resolve_inside_the_sysroot(self):
        # A missing entry (or one leaving the sysroot) is refused instead of
        # falling back to the boot serial or the deployment list.
        options = self._pointer_options(bootver=1, index=BOOT_SERIAL + 2)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "not a symlink"):
            offline_context.bind_legacy_evidence(
                _evidence(options=options, bootserial=BOOT_SERIAL + 2),
                self._pointer_options(bootver=0), ROOT_UUID, BOOT_UUID,
                sysroot_path=self.sysroot)

    def test_legacy_reader_binds_a_rewritten_pointer_after_append(self):
        # The strict reader forwards its sysroot to the pointer check, so the
        # final physical inspection still binds after prepare rewrote the BLS.
        self._boot_entry(bootver=1, index=BOOT_SERIAL + 2)
        options = self._pointer_options(bootver=1, index=BOOT_SERIAL + 2)
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence(options=options,
                                                      bootserial=BOOT_SERIAL + 2)):
            booted, context = self._state(
                cmdline_path=self._cmdline(self._pointer_options(bootver=1)))
        self.assertEqual(booted["serial"], SERIAL)
        self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)

    def test_unrecognized_os_release_is_rejected(self):
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence()):
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "recognized Armada"):
                self._state(os_release_path=self._os_release("ID=fedora\n"))

    def test_os_release_is_read_from_the_authoritative_regular_file(self):
        # The image's /etc/os-release is a symlink to ../usr/lib/os-release; the
        # helper reads the regular usr file so O_NOFOLLOW never rejects it.
        self.assertEqual(offline_context.OS_RELEASE_PATH,
                         Path("/usr/lib/os-release"))
        usr = self.tmp / "usr/lib/os-release"
        usr.parent.mkdir(parents=True)
        usr.write_text("ID=armada\nVERSION_ID=42\n", encoding="utf-8")
        etc = self.tmp / "etc/os-release"
        etc.parent.mkdir(parents=True)
        etc.symlink_to("../usr/lib/os-release")
        self.assertEqual(offline_context._armada_os_release(usr)["ID"], "armada")
        with self.assertRaises(offline_context.OfflineContextError):
            offline_context._armada_os_release(etc)

    def test_boot_id_is_canonical(self):
        path = self.tmp / "boot_id"
        path.write_text("0e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b\n",
                        encoding="ascii")
        self.assertEqual(offline_context.read_boot_id(path),
                         "0e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b")
        for text in ("not-a-uuid\n", "00000000-0000-0000-0000-000000000000\n"):
            with self.subTest(text=text):
                path.write_text(text, encoding="ascii")
                with self.assertRaises(offline_context.OfflineContextError):
                    offline_context.read_boot_id(path)

    def test_new_evidence_presence_fails_closed(self):
        witness = self.tmp / "witness"
        plain = self.tmp / "plain-cmdline"
        plain.write_text("quiet root=/dev/ram0", encoding="utf-8")
        token = self.tmp / "token-cmdline"
        token.write_text("quiet armada.deployment=" + LEGACY_DEPLOYMENT,
                         encoding="utf-8")
        self.assertTrue(offline_context.new_evidence_present(
            witness_path=witness, cmdline_path=token))
        self.assertFalse(offline_context.new_evidence_present(
            witness_path=witness, cmdline_path=plain))
        witness.write_bytes(b"x" * 16)
        self.assertTrue(offline_context.new_evidence_present(
            witness_path=witness, cmdline_path=plain))
        self.assertTrue(offline_context.new_evidence_present(
            witness_path=witness, cmdline_path=self.tmp / "missing-cmdline"))

    def test_malformed_new_evidence_never_falls_back_to_libostree(self):
        with mock.patch.object(offline_context.armada_os, "snapshot",
                               side_effect=RuntimeError("bootc status failed")), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=True), \
                mock.patch.object(offline_context, "_read_ostree_booted") as reader:
            with self.assertRaises(RuntimeError):
                offline_context.snapshot(sysroot_path=self.sysroot)
        reader.assert_not_called()

    def test_legacy_snapshot_records_an_inferred_channel(self):
        with mock.patch.object(offline_context.armada_os, "snapshot",
                               side_effect=RuntimeError("bootc status failed")), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=False), \
                mock.patch.object(offline_context, "_read_ostree_booted",
                                  return_value=_evidence(originRef=None)):
            snap = offline_context.snapshot(
                sysroot_path=self.sysroot, state_path=self.state)
        self.assertEqual(snap["repository"], offline_context.armada_os.FREEDOM)
        self.assertEqual(snap["channel"], "stable")
        self.assertTrue(snap["channelInferred"])
        self.assertIsNone(snap["staged"])
        self.assertEqual(snap["bootContext"], "legacy-controller")

    def test_legacy_snapshot_uses_a_recognized_channel_when_available(self):
        origin = ("ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
                  "armadaos-freedom:testing@sha256:" + "d" * 64)
        with mock.patch.object(offline_context.armada_os, "snapshot",
                               side_effect=RuntimeError("bootc status failed")), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=False), \
                mock.patch.object(offline_context, "_read_ostree_booted",
                                  return_value=_evidence(originRef=origin)):
            snap = offline_context.snapshot(
                sysroot_path=self.sysroot, state_path=self.state)
        self.assertEqual(snap["repository"], offline_context.armada_os.FREEDOM)
        self.assertEqual(snap["channel"], "preview")
        self.assertFalse(snap["channelInferred"])

    def test_unknown_container_origin_is_not_guessed(self):
        with self.assertRaises(offline_context.OfflineContextError):
            offline_context.legacy_channel(
                "docker://example.invalid/os:stable", state_path=self.state)

    def _legacy_snapshot(self, sysroot):
        with mock.patch.object(offline_context.armada_os, "snapshot",
                               side_effect=RuntimeError("bootc status failed")), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=False), \
                mock.patch.object(offline_context, "_gi",
                                  return_value=_fake_gi(sysroot)):
            return offline_context.snapshot(state_path=self.state)

    def test_legacy_snapshot_reports_the_booted_default_as_idle(self):
        booted = _FakeDeployment()
        snap = self._legacy_snapshot(
            _FakeSysroot(booted, deployments=[booted]))
        self.assertIsNone(snap["staged"])
        self.assertFalse(snap["rollback_queued"])
        self.assertEqual(snap["booted"]["checksum"], CHECKSUM)

    def test_legacy_snapshot_derives_a_queued_rollback_from_the_default(self):
        # No staged deployment, but the first (default) deployment is a
        # different osname/checksum/serial: a rollback is queued for the next
        # boot, so prepare may not start.
        booted = _FakeDeployment()
        default = _FakeDeployment(checksum="c" * 64, serial=1)
        snap = self._legacy_snapshot(
            _FakeSysroot(booted, deployments=[default, booted]))
        self.assertIsNone(snap["staged"])
        self.assertTrue(snap["rollback_queued"])

    def test_legacy_snapshot_does_not_flag_a_prepared_append_target(self):
        # Prepare appends its pinned target behind the default deployment.
        # Only the first entry decides the next boot, so the appended target is
        # never mistaken for a rollback.
        booted = _FakeDeployment()
        target = _FakeDeployment(checksum="d" * 64, serial=3)
        snap = self._legacy_snapshot(
            _FakeSysroot(booted, deployments=[booted, target]))
        self.assertIsNone(snap["staged"])
        self.assertFalse(snap["rollback_queued"])
        self.assertEqual(snap["booted"]["checksum"], CHECKSUM)
        self.assertEqual(snap["booted"]["serial"], SERIAL)

    def test_legacy_snapshot_reports_staging_not_a_rollback(self):
        # A pending staged deployment is reported as staged, never as a queued
        # rollback, even when the first listed deployment differs from booted.
        booted = _FakeDeployment()
        staged = _FakeDeployment(checksum="e" * 64, serial=4)
        snap = self._legacy_snapshot(
            _FakeSysroot(booted, staged=staged, deployments=[staged, booted]))
        self.assertEqual(snap["staged"], {
            "stateroot": OSNAME, "checksum": "e" * 64, "serial": 4,
            "incompatible": False})
        self.assertFalse(snap["rollback_queued"])

    def test_legacy_snapshot_refuses_a_missing_default_ordering(self):
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "default deployment order"):
            self._legacy_snapshot(
                _FakeSysroot(_FakeDeployment(), deployments=[]))
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "default deployment checksum"):
            self._legacy_snapshot(_FakeSysroot(
                _FakeDeployment(), deployments=[_FakeDeployment(checksum="z")]))


class LegacyChannelStateTests(unittest.TestCase):
    """The persisted update-channel selection wins over the image origin."""

    ORIGIN_STABLE = ("ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
                     "armadaos-freedom:stable@sha256:" + "d" * 64)
    RAW_ORIGIN = "armada/stable"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.sysroot = self.tmp / "sysroot"
        self.sysroot.mkdir()
        self.state = self.tmp / "var/lib/armada/update-channel"
        self.state.parent.mkdir(parents=True)

    def _write_state(self, content, mode=0o644):
        self.state.write_bytes(content)
        self.state.chmod(mode)

    @contextmanager
    def _root_owned(self):
        # read_installed_channel trusts root and the distro account; emulate
        # root ownership when the tests do not run as root.
        if os.geteuid() == 0:
            yield
            return
        original = os.fstat
        owner = os.geteuid()

        def root_owned(fd):
            values = list(original(fd))
            if values[4] == owner:
                values[4] = 0
            return os.stat_result(values)

        with mock.patch.object(channel_selection.os, "fstat", side_effect=root_owned):
            yield

    ARMADA_UID = 4321

    @contextmanager
    def _armada_owned(self):
        # The tmpfiles stamp declares the channel file for the distro account;
        # emulate that owner and its passwd entry without a real chown.
        original = os.fstat

        def armada_owned(fd):
            values = list(original(fd))
            values[4] = self.ARMADA_UID
            return os.stat_result(values)

        with mock.patch.object(
                channel_selection.os, "fstat", side_effect=armada_owned), \
                mock.patch.object(
                    channel_selection.pwd, "getpwnam",
                    return_value=mock.Mock(pw_uid=self.ARMADA_UID)):
            yield

    def _snapshot(self, origin_ref, ownership=None):
        if ownership is None:
            ownership = self._root_owned()
        with mock.patch.object(offline_context.armada_os, "snapshot",
                               side_effect=RuntimeError("bootc status failed")), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=False), \
                mock.patch.object(offline_context, "_read_ostree_booted",
                                  return_value=_evidence(originRef=origin_ref)), \
                ownership:
            return offline_context.snapshot(
                sysroot_path=self.sysroot, state_path=self.state)

    def test_armada_owned_state_reports_the_stored_channel(self):
        # The current install leaves the selection owned by the distro account;
        # the legacy snapshot must read it instead of failing as unsafe.
        self._write_state(b"beta\n")
        snap = self._snapshot(self.ORIGIN_STABLE, self._armada_owned())
        self.assertEqual(snap["repository"], offline_context.armada_os.FREEDOM)
        self.assertEqual(snap["channel"], "beta")
        self.assertFalse(snap["channelInferred"])
        self.assertEqual(system_transaction._snapshot_channel(snap), "beta")

    def test_armada_owned_state_selects_a_raw_local_origin(self):
        self._write_state(b"testing\n")
        snap = self._snapshot(self.RAW_ORIGIN, self._armada_owned())
        self.assertEqual(snap["channel"], "preview")
        self.assertFalse(snap["channelInferred"])
        self.assertEqual(system_transaction._snapshot_channel(snap), "testing")

    def test_persisted_beta_overrides_a_stable_origin(self):
        self._write_state(b"beta\n")
        snap = self._snapshot(self.ORIGIN_STABLE)
        self.assertEqual(snap["repository"], offline_context.armada_os.FREEDOM)
        self.assertEqual(snap["channel"], "beta")
        self.assertFalse(snap["channelInferred"])
        # Submission identity compares exactly this derived value.
        self.assertEqual(system_transaction._snapshot_channel(snap), "beta")

    def test_persisted_testing_selects_a_raw_local_origin(self):
        # A raw local commit origin names no channel, so it is never forced
        # through the strict OCI parser; the persisted selection still wins.
        self._write_state(b"testing\n")
        snap = self._snapshot(self.RAW_ORIGIN)
        self.assertEqual(snap["repository"], offline_context.armada_os.FREEDOM)
        self.assertEqual(snap["channel"], "preview")
        self.assertFalse(snap["channelInferred"])
        self.assertEqual(system_transaction._snapshot_channel(snap), "testing")

    def test_absent_state_keeps_the_origin_tag_and_stable_inference(self):
        tagged = self._snapshot(self.ORIGIN_STABLE)
        self.assertEqual(tagged["channel"], "stable")
        self.assertFalse(tagged["channelInferred"])
        inferred = self._snapshot(None)
        self.assertEqual(inferred["channel"], "stable")
        self.assertTrue(inferred["channelInferred"])
        self.assertFalse(self.state.exists())

    def test_invalid_or_unsafe_state_is_never_ignored(self):
        self._write_state(b"feature\n")
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "channel state"):
            self._snapshot(self.ORIGIN_STABLE)
        self._write_state(b"beta\n", 0o666)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "channel state"):
            self._snapshot(self.ORIGIN_STABLE)
        # A raw local origin must fail too instead of silently inferring stable.
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "channel state"):
            self._snapshot(self.RAW_ORIGIN)
        self.state.unlink()
        target = self.tmp / "target"
        target.write_bytes(b"beta\n")
        self.state.symlink_to(target)
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "channel state"):
            self._snapshot(self.ORIGIN_STABLE)


class MountEvidenceTests(unittest.TestCase):
    """mountinfo must be parsed through the `` - `` separator, not major:minor."""

    SYSROOT_UUID = "44444444-4444-4444-8444-444444444444"
    BOOT_UUID = "55555555-5555-4555-8555-555555555555"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def _mountinfo(self, *lines):
        path = self.tmp / "mountinfo"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_btrfs_uses_the_source_block_device_not_the_anonymous_mount_number(self):
        info = self._mountinfo(
            "36 1 0:38 /@ /sysroot rw,relatime shared:1 - btrfs /dev/sda16 rw,ssd")
        with mock.patch.object(offline_context, "_resolve_block_source",
                               return_value=0x0810) as resolve:
            self.assertEqual(
                offline_context._mount_device("/sysroot", info), 0x0810)
        resolve.assert_called_once_with("/dev/sda16", "/sysroot")

    def test_ext4_regular_mount_must_also_match_its_device_number(self):
        info = self._mountinfo(
            "37 1 8:17 / /boot rw,relatime shared:2 - ext4 /dev/sda17 rw")
        with mock.patch.object(offline_context, "_resolve_block_source",
                               return_value=os.makedev(8, 17)):
            self.assertEqual(
                offline_context._mount_device("/boot", info), os.makedev(8, 17))
        with mock.patch.object(offline_context, "_resolve_block_source",
                               return_value=os.makedev(8, 18)):
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "does not match"):
                offline_context._mount_device("/boot", info)

    def test_ambiguous_malformed_and_unrelated_mounts_are_rejected(self):
        ambiguous = self._mountinfo(
            "36 1 0:38 /@ /sysroot rw - btrfs /dev/sda16 rw",
            "38 1 0:38 /@other /sysroot rw - btrfs /dev/sda16 rw")
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "exactly one mount"):
            offline_context._mount_device("/sysroot", ambiguous)
        malformed = self._mountinfo("36 1 0:38 /@ /sysroot rw,relatime")
        with self.assertRaisesRegex(offline_context.OfflineContextError,
                                    "separator"):
            offline_context._mount_device("/sysroot", malformed)
        with self.assertRaises(offline_context.OfflineContextError):
            offline_context._resolve_block_source("overlay", "/sysroot")
        if Path("/dev/null").exists():
            with self.assertRaisesRegex(offline_context.OfflineContextError,
                                        "not a block device"):
                offline_context._resolve_block_source("/dev/null", "/sysroot")

    def test_legacy_binding_reads_btrfs_sysroot_and_ext4_boot(self):
        info = self._mountinfo(
            "36 1 0:38 /@ /sysroot ro - btrfs /dev/sda16 ro",
            "37 1 8:17 / /boot ro - ext4 /dev/sda17 ro")
        by_uuid = self.tmp / "by-uuid"
        by_uuid.mkdir()
        target = self.tmp / "target"
        target.write_bytes(b"x")
        for name in (ROOT_UUID, BOOT_UUID):
            (by_uuid / name).symlink_to(target)
        rdevs = {ROOT_UUID: os.makedev(8, 16),
                 BOOT_UUID: os.makedev(8, 17)}

        def resolve(source, _label):
            return {"/dev/sda16": rdevs[ROOT_UUID],
                    "/dev/sda17": rdevs[BOOT_UUID]}[source]

        def entry_rdev(entry):
            return rdevs.get(entry.name)

        cmdline = self.tmp / "cmdline"
        cmdline.write_text(OPTIONS, encoding="utf-8")
        release = self.tmp / "os-release"
        release.write_text("ID=armada\nVERSION_ID=42\n", encoding="utf-8")
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence()), \
                mock.patch.object(offline_context, "_resolve_block_source",
                                  side_effect=resolve), \
                mock.patch.object(offline_context, "_uuid_entry_rdev",
                                  side_effect=entry_rdev):
            booted, context = offline_context.read_legacy_boot_state(
                cmdline_path=cmdline, os_release_path=release,
                mount_lookup=lambda mount: offline_context.read_mount_uuid(
                    mount, mountinfo_path=info, by_uuid_path=by_uuid))
        self.assertEqual(booted["serial"], SERIAL)
        self.assertEqual(context["deployment"], LEGACY_DEPLOYMENT)
        self.assertIsNone(context["actualSlot"])
        self.assertEqual(context["bootContext"], "legacy-controller")

    def test_uuid_lookup_compares_against_the_source_device(self):
        info = self._mountinfo(
            "36 1 0:38 /@ /sysroot rw,relatime shared:1 - btrfs /dev/sda16 rw")
        by_uuid = self.tmp / "by-uuid"
        by_uuid.mkdir()
        target = self.tmp / "target"
        target.write_bytes(b"x")
        (by_uuid / self.SYSROOT_UUID).symlink_to(target)
        (by_uuid / self.BOOT_UUID).symlink_to(target)

        def entry_rdev(entry):
            return 0x0810 if entry.name == self.SYSROOT_UUID else 0x0811

        with mock.patch.object(offline_context, "_resolve_block_source",
                               return_value=0x0810), \
                mock.patch.object(offline_context, "_uuid_entry_rdev",
                                  side_effect=entry_rdev):
            self.assertEqual(
                offline_context.read_mount_uuid(
                    "/sysroot", mountinfo_path=info, by_uuid_path=by_uuid),
                self.SYSROOT_UUID)
        with mock.patch.object(offline_context, "_resolve_block_source",
                               return_value=0x0810), \
                mock.patch.object(offline_context, "_uuid_entry_rdev",
                                  return_value=0x0811):
            with self.assertRaises(offline_context.OfflineContextError):
                offline_context.read_mount_uuid(
                    "/sysroot", mountinfo_path=info, by_uuid_path=by_uuid)


class SlotSemanticsTests(unittest.TestCase):
    def test_offline_slot_pair_uses_the_selected_slot_as_reference(self):
        for selected in ("a", "b"):
            with self.subTest(selected=selected):
                report = dict(LEGACY_REPORT, selectedSlot=selected)
                other = "b" if selected == "a" else "a"
                self.assertEqual(physical_slots.offline_slot_pair(report),
                                 (selected, other))

    def test_legacy_report_may_not_claim_an_actual_slot(self):
        with self.assertRaises(physical_slots.PhysicalSlotsError):
            physical_slots.offline_slot_pair(dict(LEGACY_REPORT, actualSlot="b"))

    def test_strict_report_must_agree_with_the_selected_slot(self):
        strict = {"model": "TB322FC", "actualSlot": "a", "inactiveSlot": "b",
                  "selectedSlot": "a", "deployment": LEGACY_DEPLOYMENT,
                  "recoveryPartUuid": PART_UUID}
        self.assertEqual(physical_slots.offline_slot_pair(strict), ("a", "b"))
        # Fixtures without selectedSlot keep their existing strict meaning.
        self.assertEqual(
            physical_slots.offline_slot_pair(
                {"actualSlot": "b", "inactiveSlot": "a"}), ("b", "a"))
        with self.assertRaises(physical_slots.PhysicalSlotsError):
            physical_slots.offline_slot_pair(dict(strict, selectedSlot="b"))

    def test_online_inspector_still_rejects_a_missing_witness(self):
        error = physical_slots.PhysicalSlotsError("invalid boot context: no witness")
        with mock.patch.object(physical_slots, "_read_boot_state", side_effect=error):
            with self.assertRaises(physical_slots.PhysicalSlotsError):
                physical_slots.inspect_physical_slots()

    def test_offline_inspector_does_not_fall_back_when_evidence_exists(self):
        error = physical_slots.PhysicalSlotsError("invalid boot context: bad witness")
        with mock.patch.object(physical_slots, "inspect_physical_slots",
                               side_effect=error), \
                mock.patch.object(offline_context, "new_evidence_present",
                                  return_value=True):
            with self.assertRaises(physical_slots.PhysicalSlotsError):
                physical_slots.inspect_offline_physical_slots()

    def test_native_unbootable_inactive_slot_keeps_mutation_strict(self):
        # Same native answer as the live device: selected A, B unbootable.
        with mock.patch.object(physical_slots, "_read_slot_flags",
                               return_value=(0, {"a": True, "b": False})):
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "both slots must be bootable"):
                physical_slots._run_inspection([0] * 6, {})

    def test_legacy_inventory_is_refused_when_witness_evidence_exists(self):
        with mock.patch.object(physical_slots.offline_context,
                               "new_evidence_present", return_value=True), \
                mock.patch.object(physical_slots, "_inspect_slots") as inspect:
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "witness"):
                physical_slots.inspect_legacy_identity_slots()
        inspect.assert_not_called()


class SnapshotFailureFallbackTests(unittest.TestCase):
    """A strict snapshot RuntimeError must not bypass the legacy fallback gate."""

    MESSAGE = ("bootc status failed: Missing ostree.manifest-digest metadata "
               "on merge commit")
    LEGACY_STATE = (
        {"stateroot": OSNAME, "checksum": CHECKSUM, "serial": SERIAL,
         "incompatible": False},
        {"state": "available", "actualSlot": None, "inactiveSlot": None,
         "bootContext": "legacy-controller", "deployment": LEGACY_DEPLOYMENT},
    )

    def _snapshot_failure(self):
        # The live 0.7 failure: armada_os.snapshot() raises a plain RuntimeError
        # from bootc status instead of a PhysicalSlotsError.
        return mock.patch.object(physical_slots.armada_os, "snapshot",
                                 side_effect=RuntimeError(self.MESSAGE))

    def _inspector(self, calls, report):
        def fake_inspect(reader, phase, *, require_witness=True):
            calls.append((reader, phase, require_witness))
            reader()
            return dict(report)

        return mock.patch.object(physical_slots, "_inspect_slots",
                                 side_effect=fake_inspect)

    def test_snapshot_failure_is_normalized_and_keeps_its_cause(self):
        with self._snapshot_failure():
            with self.assertRaises(physical_slots.PhysicalSlotsError) as caught:
                physical_slots._read_boot_state()
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertIn("manifest-digest", str(caught.exception))

    def test_offline_inspector_falls_back_when_no_new_evidence_exists(self):
        calls = []
        with self._snapshot_failure(), \
                self._inspector(calls, LEGACY_REPORT), \
                mock.patch.object(physical_slots.offline_context,
                                  "new_evidence_present", return_value=False), \
                mock.patch.object(physical_slots, "_read_legacy_boot_state",
                                  return_value=self.LEGACY_STATE) as legacy:
            result = physical_slots.inspect_offline_physical_slots()
        self.assertEqual(result, LEGACY_REPORT)
        self.assertIs(calls[0][0], physical_slots._read_boot_state)
        self.assertIs(calls[0][2], True)
        self.assertIs(calls[1][0], legacy)
        self.assertIs(calls[1][2], False)
        legacy.assert_called_once_with()

    def test_offline_inspector_rejects_the_same_failure_with_new_evidence(self):
        calls = []
        with self._snapshot_failure(), \
                self._inspector(calls, LEGACY_REPORT), \
                mock.patch.object(physical_slots.offline_context,
                                  "new_evidence_present", return_value=True), \
                mock.patch.object(physical_slots,
                                  "_read_legacy_boot_state") as legacy:
            with self.assertRaises(physical_slots.PhysicalSlotsError):
                physical_slots.inspect_offline_physical_slots()
        legacy.assert_not_called()
        self.assertEqual(len(calls), 1)


class LegacyStagedMutationTests(unittest.TestCase):
    """A pending transaction never hides the device but still blocks writes."""

    STATE = ({"stateroot": OSNAME, "checksum": CHECKSUM, "serial": SERIAL,
              "incompatible": False},
             {"state": "available", "actualSlot": None, "inactiveSlot": None,
              "bootContext": "legacy-controller", "deployment": LEGACY_DEPLOYMENT})

    def test_identity_inspector_uses_the_pending_tolerant_reader(self):
        calls = []

        def fake_inspect(reader, phase, *, require_witness=True,
                         require_bootable=True):
            calls.append((reader, phase, require_witness, require_bootable))
            reader()
            return dict(LEGACY_REPORT)

        with mock.patch.object(physical_slots, "_inspect_slots",
                               side_effect=fake_inspect), \
                mock.patch.object(physical_slots.offline_context,
                                  "new_evidence_present", return_value=False), \
                mock.patch.object(physical_slots, "_read_legacy_boot_state",
                                  return_value=self.STATE) as legacy:
            report = physical_slots.inspect_legacy_identity_slots()
        self.assertEqual(report, LEGACY_REPORT)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][2])
        self.assertFalse(calls[0][3])
        legacy.assert_called_once_with(reject_pending=False)

    def test_mutation_reader_keeps_the_staged_rejection(self):
        staged = offline_context.OfflineContextError(
            "a staged OSTree deployment refuses a legacy offline upgrade")
        with mock.patch.object(physical_slots.offline_context,
                               "read_legacy_boot_state",
                               side_effect=staged) as reader:
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "staged"):
                physical_slots._read_legacy_boot_state()
        reader.assert_called_once_with(reject_pending=True)

    def test_mutation_reader_also_rejects_a_queued_rollback(self):
        pending = offline_context.OfflineContextError(
            "a pending OSTree transaction (rollback) refuses a legacy offline upgrade")
        with mock.patch.object(physical_slots.offline_context,
                               "read_legacy_boot_state",
                               side_effect=pending) as reader:
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "rollback"):
                physical_slots._read_legacy_boot_state()
        reader.assert_called_once_with(reject_pending=True)

    def test_mutation_fallback_never_passes_the_identity_only_flag(self):
        # _read_offline_boot_state is a mutation fallback, so it must keep the
        # strict default; only inspect_legacy_identity_slots may relax it.
        error = physical_slots.PhysicalSlotsError("invalid boot context: no witness")
        with mock.patch.object(physical_slots, "_read_boot_state",
                               side_effect=error), \
                mock.patch.object(physical_slots.offline_context,
                                  "new_evidence_present", return_value=False), \
                mock.patch.object(physical_slots, "_read_legacy_boot_state",
                                  return_value=self.STATE) as legacy:
            physical_slots._read_offline_boot_state()
        legacy.assert_called_once_with()


def _set_gpt_unbootable(image, block_size, entry):
    """Set GPT attribute bit 55 on one fixture entry and repair both CRCs.

    This reproduces the live legacy device: boot_b (or boot_a) keeps its valid
    Android header, so the native inspection still succeeds, but the A/B
    attribute marks the slot UNBOOTABLE and ``bootable[s]`` becomes false.
    """
    with image.open("r+b") as stream:
        data = bytearray(stream.read())
        table_blocks = (16 * 128 + block_size - 1) // block_size
        arrays = (2, 256 - 1 - table_blocks)
        for lba in arrays:
            at = lba * block_size + entry * 128
            attr = int.from_bytes(data[at + 48:at + 56], "little")
            data[at + 48:at + 56] = (attr | (1 << 55)).to_bytes(8, "little")
        for lba in arrays:
            start = lba * block_size
            digest = zlib.crc32(bytes(data[start:start + 16 * 128]))
            for header in (1, 256 - 1):
                at = header * block_size + 88
                data[at:at + 4] = digest.to_bytes(4, "little")
        for header in (1, 256 - 1):
            start = header * block_size
            length = int.from_bytes(data[start + 12:start + 16], "little")
            data[start + 16:start + 20] = b"\0" * 4
            crc = zlib.crc32(bytes(data[start:start + length]))
            data[start + 16:start + 20] = crc.to_bytes(4, "little")
        stream.seek(0)
        stream.write(data)


class LegacyUnbootableSlotFixtureTests(unittest.TestCase):
    """Identity on the compiled six-LUN GPT fixture with an unbootable slot."""

    @classmethod
    def setUpClass(cls):
        # Reuse the compiled GPT/Boot-LUN fixture rather than duplicate it; its
        # own test methods are not inherited, so nothing runs twice.
        ab_fixture.PhysicalSlotsTests.setUpClass()
        cls.fixture = ab_fixture.PhysicalSlotsTests(
            "test_valid_matrix_report_native_geometry_and_read_only")

    @classmethod
    def tearDownClass(cls):
        ab_fixture.PhysicalSlotsTests.doClassCleanups()

    def _legacy_state(self):
        return mock.patch.object(
            physical_slots, "_read_legacy_boot_state",
            return_value=SnapshotFailureFallbackTests.LEGACY_STATE)

    def test_identity_reports_genuine_flags_while_mutation_stays_strict(self):
        with self.fixture.environment(512, 0, 1) as env:
            _set_gpt_unbootable(env.images[4], 512, entry=1)  # boot_b
            before = self.fixture.digests(env)
            with mock.patch.object(physical_slots.offline_context,
                                   "new_evidence_present", return_value=False), \
                    self._legacy_state():
                report = physical_slots.inspect_legacy_identity_slots()
                with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                            "both slots must be bootable"):
                    physical_slots.inspect_physical_slots()
                with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                            "both slots must be bootable"):
                    physical_slots.inspect_offline_physical_slots()
            # The inventory is read-only: no image byte and no fd is left open.
            self.fixture.assert_closed_unchanged(env, before)
        self.assertEqual(report["bootableSlots"], {"a": True, "b": False})
        self.assertEqual(report["selectedSlot"], "a")
        self.assertIsNone(report["actualSlot"])
        self.assertIsNone(report["inactiveSlot"])
        self.assertEqual(report["bootContext"], "legacy-controller")
        self.assertEqual(report["deployment"], LEGACY_DEPLOYMENT)

    def test_identity_refuses_an_unbootable_selected_slot(self):
        with self.fixture.environment(512, 0, 1) as env:
            _set_gpt_unbootable(env.images[4], 512, entry=0)  # boot_a
            before = self.fixture.digests(env)
            with mock.patch.object(physical_slots.offline_context,
                                   "new_evidence_present", return_value=False), \
                    self._legacy_state():
                with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                            "selected slot a is not bootable"):
                    physical_slots.inspect_legacy_identity_slots()
            self.fixture.assert_closed_unchanged(env, before)


class OfflineIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name) / "offline"
        patcher = mock.patch.object(offline_session.os, "geteuid", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _legacy_device(self, report):
        """Bind the legacy fallback to a controller-only physical report."""
        for target, attribute, value in (
            (offline_session.armada_os, "snapshot", mock.Mock(
                side_effect=RuntimeError("bootc status failed"))),
            (offline_context, "new_evidence_present", mock.Mock(return_value=False)),
            (offline_context, "_read_ostree_booted",
             mock.Mock(return_value=_evidence())),
            (physical_slots, "inspect_legacy_identity_slots",
             mock.Mock(return_value=dict(report))),
        ):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_legacy_identity_and_info_report_the_selected_slot_only(self):
        self._legacy_device(LEGACY_REPORT)
        identity = offline_session.read_offline_device_identity()
        self.assertEqual(identity, {
            "model": "TB322FC", "actualSlot": None, "inactiveSlot": None,
            "selectedSlot": "b", "bootContext": "legacy-controller",
            "deployment": LEGACY_DEPLOYMENT, "recoveryPartUuid": PART_UUID,
            "bootableSlots": {"a": True, "b": True},
        })
        info = offline_session.OfflineSessionStore(base=self.base).info()
        self.assertIsNone(info["actualSlot"])
        self.assertIsNone(info["inactiveSlot"])
        self.assertEqual(info["selectedSlot"], "b")
        self.assertEqual(info["bootContext"], "legacy-controller")
        self.assertEqual(info["deployment"], LEGACY_DEPLOYMENT)
        self.assertEqual(info["bootableSlots"], {"a": True, "b": True})
        self.assertEqual(info["channel"], "stable")

    def test_info_identifies_a_device_with_an_unbootable_inactive_slot(self):
        # The live legacy state: selected A is bootable, B's Android header is
        # valid but its GPT attribute is UNBOOTABLE.  An authenticated info
        # connection must still describe this device and report the real flag
        # instead of inventing "both bootable"; the upgrade blocker is reported,
        # not hidden by rejecting the connection.
        self._legacy_device(UNBOOTABLE_REPORT)
        identity = offline_session.read_offline_device_identity()
        self.assertEqual(identity["bootableSlots"], {"a": True, "b": False})
        self.assertIsNone(identity["actualSlot"])
        self.assertIsNone(identity["inactiveSlot"])
        self.assertEqual(identity["selectedSlot"], "a")
        info = offline_session.OfflineSessionStore(base=self.base).info()
        self.assertEqual(info["bootableSlots"], {"a": True, "b": False})
        self.assertEqual(info["selectedSlot"], "a")
        self.assertIsNone(info["actualSlot"])
        self.assertEqual(info["deployment"], LEGACY_DEPLOYMENT)
        self.assertEqual(info["channel"], "stable")
        self.assertFalse(self.base.exists())

    def test_legacy_identity_never_fabricates_bootable_slots(self):
        for bootable in ({"a": True}, {"a": 1, "b": 0},
                         {"a": True, "b": True, "c": True}):
            with self.subTest(bootable=bootable):
                with self.assertRaises(offline_session.OfflineSessionError):
                    offline_session._legacy_identity(
                        dict(LEGACY_REPORT, bootableSlots=bootable))
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    "selected slot"):
            offline_session._legacy_identity(dict(
                UNBOOTABLE_REPORT, selectedSlot="b"))

    def test_legacy_report_that_claims_an_actual_slot_is_refused(self):
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    "actual slot"):
            offline_session._legacy_identity(dict(LEGACY_REPORT, actualSlot="a"))

    def test_info_reports_a_staged_deployment_as_data(self):
        # info must still identify the device; the staged deployment is the
        # upgrade blocker and is exposed instead of failing the connection.
        self._legacy_device(LEGACY_REPORT)
        staged = {"stateroot": OSNAME, "checksum": "c" * 64, "serial": 1,
                  "incompatible": False}
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence(staged=staged)):
            info = offline_session.OfflineSessionStore(base=self.base).info()
        self.assertEqual(info["staged"], staged)
        self.assertFalse(info["rollbackQueued"])
        self.assertEqual(info["deployment"], LEGACY_DEPLOYMENT)
        self.assertIsNone(info["actualSlot"])
        self.assertFalse(self.base.exists())

    def test_info_reports_a_queued_rollback_as_data(self):
        self._legacy_device(LEGACY_REPORT)
        with mock.patch.object(offline_context, "_read_ostree_booted",
                               return_value=_evidence(rollback_queued=True)):
            info = offline_session.OfflineSessionStore(base=self.base).info()
        self.assertIsNone(info["staged"])
        self.assertTrue(info["rollbackQueued"])
        self.assertEqual(info["deployment"], LEGACY_DEPLOYMENT)
        self.assertFalse(self.base.exists())


def _signed_package(private_key, payload: bytes):
    package = release_fixture._upgrade_package_dict()
    package["minimumInstallerVersion"] = "0.8.0"
    package["minimumUpdaterVersion"] = "0.8.0"
    for record in package["files"]:
        if record["role"] == "linux-root":
            record["bytes"] = len(payload)
            record["sha256"] = sha256(payload).hexdigest()
            record["expandedBytes"] = len(payload) * 2
            record["expandedSha256"] = sha256(payload + b"x").hexdigest()
    document = json.dumps(validate_package(package), sort_keys=True,
                          separators=(",", ":")).encode()
    return document, release_fixture._sign_bytes(document, private_key), payload


class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmpdir.name)
        cls.private, cls.public = release_fixture._generate_keypair(cls.tmp, "preflight")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name).resolve()
        self.sysroot = self.root / "sysroot"
        self.sysroot.mkdir()
        self.binary = self.root / "tool"
        self.binary.write_bytes(b"#!/bin/sh\n")
        self.binary.chmod(0o755)
        self.base = self.root / "offline"
        for target, attribute, value in (
            (offline_session.os, "geteuid", mock.Mock(return_value=0)),
        ):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            offline_session.armada_os, "snapshot", return_value={
                "channel": "stable",
                "repository": offline_session.armada_os.REPOSITORIES[0],
            })
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            offline_session, "_REQUIRED_UPDATER_FILES", (self.binary,))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            offline_session, "_REQUIRED_UPDATER_EXECUTABLES", (self.binary,))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Preflight now reads the bound recovery journal with the same
        # read-only check prepare uses.  Provide valid reads instead of
        # letting the host look for a real block device.
        self.board = 1
        self.legacy_journal = SimpleNamespace(
            layout=SimpleNamespace(
                descriptor_version=1,
                partition_uuid=bytes.fromhex(PART_UUID.replace("-", "")),
                installation_uuid=b"I" * 16,
                board=1),
            header=b"H" * 65536, region=b"R" * 65536,
            record=None, selected=None)
        self.strict_journal = SimpleNamespace(
            layout=SimpleNamespace(
                descriptor_version=3,
                partition_uuid=bytes.fromhex(PART_UUID.replace("-", "")),
                installation_uuid=b"I" * 16,
                board=2),
            header=b"H" * 65536, region=b"R" * 65536,
            record=None, selected=None)

        @contextmanager
        def _open_recovery(*, writable):
            self.assertFalse(writable)
            yield 0, PART_UUID, self.board

        patcher = mock.patch.object(
            offline_session.offline_prepare.recovery_device, "open_recovery",
            _open_recovery)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            trial_store, "load", return_value=self.strict_journal)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            offline_session.offline_prepare.offline_recovery, "load",
            return_value=self.legacy_journal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store(self, inspector):
        return offline_session.OfflineSessionStore(
            base=self.base, trusted_key=self.public, inspect=inspector,
            sysroot_path=self.sysroot)

    def test_preflight_is_read_only_and_reports_no_slot_claim(self):
        document, signature, payload = _signed_package(self.private, os.urandom(4096))
        store = self._store(lambda: dict(LEGACY_REPORT))
        with mock.patch.object(
                offline_session.offline_prepare.offline_recovery.os, "pwrite") as writes:
            result = store.preflight(document, signature)
        self.assertEqual(result["phase"], "preflight_passed")
        self.assertEqual(result["bootWriteState"], "not_started")
        self.assertIsNone(result["actualSlot"])
        self.assertEqual(result["selectedSlot"], "b")
        self.assertEqual(result["bootContext"], "legacy-controller")
        self.assertEqual(result["currentDeployment"], LEGACY_DEPLOYMENT)
        self.assertEqual(result["recoveryPartUuid"], PART_UUID)
        self.assertEqual(result["recoveryDescriptorVersion"], 1)
        self.assertTrue(result["recoveryMigrationRequired"])
        self.assertEqual(result["model"], "TB322FC")
        self.assertFalse(self.base.exists())
        self.assertFalse((self.sysroot / "ostree").exists())
        writes.assert_not_called()

    def test_preflight_keeps_the_strict_identity(self):
        document, signature, _payload = _signed_package(self.private, os.urandom(4096))
        self.board = 2
        strict = {"model": "TB323FU", "actualSlot": "a", "inactiveSlot": "b",
                  "selectedSlot": "a", "deployment": LEGACY_DEPLOYMENT,
                  "recoveryPartUuid": PART_UUID}
        result = self._store(lambda: dict(strict)).preflight(document, signature)
        self.assertEqual(result["actualSlot"], "a")
        self.assertEqual(result["selectedSlot"], "a")
        self.assertEqual(result["bootContext"], "witness")
        self.assertEqual(result["recoveryDescriptorVersion"], 3)
        self.assertFalse(result["recoveryMigrationRequired"])

    def test_preflight_rejects_an_unbootable_inactive_slot(self):
        # info accepts this device, but preflight asks the strict mutation
        # precheck and must still refuse: the write path may not start.
        document, signature, _payload = _signed_package(
            self.private, os.urandom(4096))
        strict = mock.Mock(side_effect=physical_slots.PhysicalSlotsError(
            "both slots must be bootable (a=1, b=0)"))
        with mock.patch.object(
                offline_session.offline_prepare.offline_recovery.os,
                "pwrite") as writes:
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "both slots must be bootable"):
                self._store(strict).preflight(document, signature)
        strict.assert_called_once_with()
        writes.assert_not_called()
        self.assertFalse(self.base.exists())
        self.assertFalse((self.sysroot / "ostree").exists())

    def test_preflight_rejects_a_staged_deployment(self):
        # info accepts this device, but preflight asks the strict mutation
        # precheck, which keeps the staged-deployment rejection.
        document, signature, _payload = _signed_package(
            self.private, os.urandom(4096))
        store = offline_session.OfflineSessionStore(
            base=self.base, trusted_key=self.public, sysroot_path=self.sysroot)
        staged = physical_slots.PhysicalSlotsError(
            "invalid legacy boot context: a staged OSTree deployment refuses "
            "a legacy offline upgrade")
        with mock.patch.object(
                physical_slots, "inspect_physical_slots",
                side_effect=physical_slots.PhysicalSlotsError("no witness")), \
                mock.patch.object(physical_slots.offline_context,
                                  "new_evidence_present", return_value=False), \
                mock.patch.object(physical_slots, "_inspect_slots",
                                  side_effect=staged), \
                mock.patch.object(
                    offline_session.offline_prepare.offline_recovery.os,
                    "pwrite") as writes:
            with self.assertRaisesRegex(physical_slots.PhysicalSlotsError,
                                        "staged"):
                store.preflight(document, signature)
        writes.assert_not_called()
        self.assertFalse(self.base.exists())
        self.assertFalse((self.sysroot / "ostree").exists())

    def test_preflight_rejects_a_queued_rollback_before_any_work(self):
        # The injected inspector cannot prove pending state, so preflight reads
        # the snapshot: a rollback queued after prepare must fail before
        # package, dependency or recovery work instead of reporting ready.
        document, signature, _payload = _signed_package(
            self.private, os.urandom(4096))
        snapshot = {"channel": "stable", "staged": None, "rollback_queued": True}
        with mock.patch.object(
                offline_session.offline_context, "snapshot",
                return_value=snapshot), \
                mock.patch.object(
                    offline_session.package_contract, "load_package") as load, \
                mock.patch.object(
                    offline_session.OfflineSessionStore,
                    "_check_updater_dependencies") as dependencies, \
                mock.patch.object(
                    offline_session.offline_prepare, "_check_recovery") as recovery, \
                mock.patch.object(
                    offline_session.offline_prepare.offline_recovery.os,
                    "pwrite") as writes:
            with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                        "pending"):
                self._store(lambda: dict(LEGACY_REPORT)).preflight(
                    document, signature)
        load.assert_not_called()
        dependencies.assert_not_called()
        recovery.assert_not_called()
        writes.assert_not_called()
        self.assertFalse(self.base.exists())
        self.assertFalse((self.sysroot / "ostree").exists())

    def test_preflight_rejects_a_pending_modern_snapshot(self):
        # Modern systems keep the same strict policy: a staged deployment or
        # queued rollback blocks preflight regardless of boot context.
        document, signature, _payload = _signed_package(
            self.private, os.urandom(4096))
        self.board = 2
        strict = {"model": "TB323FU", "actualSlot": "a", "inactiveSlot": "b",
                  "selectedSlot": "a", "deployment": LEGACY_DEPLOYMENT,
                  "recoveryPartUuid": PART_UUID}
        snapshot = {"channel": "stable", "staged": {
            "stateroot": OSNAME, "checksum": "c" * 64, "serial": 1},
            "rollback_queued": False}
        with mock.patch.object(
                offline_session.offline_context, "snapshot",
                return_value=snapshot), \
                mock.patch.object(
                    offline_session.package_contract, "load_package") as load:
            with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                        "pending"):
                self._store(lambda: dict(strict)).preflight(document, signature)
        load.assert_not_called()
        self.assertFalse(self.base.exists())

    def test_preflight_rejects_a_bad_signature_or_missing_dependency(self):
        document, signature, _payload = _signed_package(self.private, os.urandom(4096))
        store = self._store(lambda: dict(LEGACY_REPORT))
        with self.assertRaises(ValueError):
            store.preflight(document, b"not-a-signature")
        self.binary.unlink()
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    "dependency"):
            store.preflight(document, signature)

    def test_dependency_check_accepts_the_fedora_python3_symlink(self):
        bindir = self.root / "usr/bin"
        bindir.mkdir(parents=True)
        target = bindir / "python3.13"
        target.write_bytes(b"#!/bin/sh\n")
        target.chmod(0o755)
        link = bindir / "python3"
        link.symlink_to("python3.13")
        with mock.patch.object(
                offline_session, "_REQUIRED_UPDATER_FILES", (self.binary,)), \
                mock.patch.object(
                    offline_session, "_REQUIRED_UPDATER_EXECUTABLES",
                    (link, self.binary)):
            offline_session.OfflineSessionStore._check_updater_dependencies()

        target.chmod(0o644)
        with mock.patch.object(
                offline_session, "_REQUIRED_UPDATER_FILES", (self.binary,)), \
                mock.patch.object(
                    offline_session, "_REQUIRED_UPDATER_EXECUTABLES",
                    (link, self.binary)):
            with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                        "executable"):
                offline_session.OfflineSessionStore._check_updater_dependencies()

        directory_link = bindir / "python3-dir"
        directory_link.symlink_to(bindir, target_is_directory=True)
        with mock.patch.object(
                offline_session, "_REQUIRED_UPDATER_FILES", (self.binary,)), \
                mock.patch.object(
                    offline_session, "_REQUIRED_UPDATER_EXECUTABLES",
                    (directory_link, self.binary)):
            with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                        "missing updater dependency"):
                offline_session.OfflineSessionStore._check_updater_dependencies()

    def test_preflight_rpc_forwards_without_creating_a_session(self):
        manager = mock.MagicMock()
        manager.store.preflight.return_value = {"phase": "preflight_passed"}
        request = {
            "protocolVersion": 1, "id": 1, "method": "preflight",
            "params": {"document": base64.b64encode(b"doc").decode("ascii"),
                       "signature": base64.b64encode(b"sig").decode("ascii")},
        }
        writer = io.BytesIO()
        rc = offline_rpc.serve(io.BytesIO((json.dumps(request) + "\n").encode()),
                               writer, manager)
        self.assertEqual(rc, 0)
        response = json.loads(writer.getvalue().decode())
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], {"phase": "preflight_passed"})
        manager.store.preflight.assert_called_once_with(b"doc", b"sig")
        manager.store.begin.assert_not_called()

    def test_preflight_rpc_rejects_extra_parameters_and_restricted_endpoints(self):
        manager = mock.MagicMock()
        malformed = [
            {"protocolVersion": 1, "id": 1, "method": "preflight",
             "params": {"document": "ZG9j", "signature": "c2ln", "sessionId": "x"}},
            {"protocolVersion": 1, "id": 2, "method": "preflight",
             "params": {"document": "ZG9j"}},
        ]
        payload = b"".join((json.dumps(item) + "\n").encode() for item in malformed)
        self.assertEqual(offline_rpc.serve(io.BytesIO(payload), io.BytesIO(), manager), 0)
        self.assertEqual(manager.mock_calls, [])

        valid = {"protocolVersion": 1, "id": 3, "method": "preflight",
                 "params": {"document": "ZG9j", "signature": "c2ln"}}
        writer = io.BytesIO()
        offline_rpc.serve(
            io.BytesIO((json.dumps(valid) + "\n").encode()), writer, manager,
            allowed_methods=frozenset({"info"}))
        response = json.loads(writer.getvalue().decode())
        self.assertEqual(response["error"]["code"], "operation_not_available")
        self.assertEqual(manager.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
