# SPDX-License-Identifier: GPL-3.0-only
"""Bounded retirement tests for confirmed offline update sessions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import job_retirement as jr  # noqa: E402
from updates import offline_session, trial_record  # noqa: E402

ROOT_REQUIRED = unittest.skipUnless(
    getattr(os, "geteuid", lambda: -1)() == 0,
    "offline session storage requires euid 0 (root-owned private directories)")

TX = "11111111-2222-4333-8444-555555555555"
OTHER_TX = "99999999-8888-4777-8666-555555555555"
SESSION = "1" * 32
OTHER_SESSION = "2" * 32
MODEL = "TB322FC"
OS_NAME = "armada"
OLD_COMMIT = bytes.fromhex("11" * 32)
NEW_COMMIT = bytes.fromhex("22" * 32)
INSTALLATION = "33333333-3333-4333-8333-333333333333"
PARTITION = "44444444-4444-4444-8444-444444444444"
PAYLOAD_PATH = "images/linux-root.img"
PAYLOAD = b"compressed-linux-root-payload"
OLD_DEPLOYMENT = "%s/%s.%d" % (OS_NAME, OLD_COMMIT.hex(), 1)
NEW_DEPLOYMENT = "%s/%s.%d" % (OS_NAME, NEW_COMMIT.hex(), 2)
PHYSICAL = {
    "model": MODEL,
    "actualSlot": "a",
    "inactiveSlot": "b",
    "deployment": OLD_DEPLOYMENT,
    "recoveryPartUuid": PARTITION,
}
COMPACT_DOCUMENTS = (
    "status.json", "plan.json", "prepared.json", "write-report.json",
    "recovery-manifest.json", "recovery-ack.json", "materials.json",
    "candidate-a.json", "candidate-b.json", "trust.json",
    "package.json", "package.sig",
)
SESSION_DOCUMENTS = ("receipt.json", "package.json", "package.sig", "ack.json")
RECOVERY_FILES = (
    "recovery-header.bin", "recovery-region.bin",
    "trust/sources/slots/a/boot", "trust/sources/slots/b/boot",
)
MATERIAL_FILES = (
    "kernel.img", "initramfs.img", "runtime.bin", "source.dtb",
    "dtb-normal.img", "dtb-diagnostic.img",
)
CANDIDATE_FILES = (
    "inspect-slot/base.bin", "build-raw/PackedKernel",
    "repack-envelope/candidate.img",
)
BULKY_FILES = tuple("materials/" + name for name in MATERIAL_FILES) + tuple(
    "candidate-%s/%s" % (slot, name)
    for slot in ("a", "b") for name in CANDIDATE_FILES)
KEPT_FILES = (
    "materials/materials.json",
    "materials/dtb-work/normal.dts",
    "materials/dtb-logs/normal.log",
    "materials/dtb-tmp/scratch",
    "candidate-a/candidate.json",
    "candidate-a/inspect-slot/result.json",
    "candidate-a/inspect-slot.stdout.log",
    "candidate-a/build-raw/result.json",
    "candidate-a/repack-envelope/result.json",
    "candidate-a/tmp/scratch",
    "candidate-b/candidate.json",
    "candidate-b/inspect-slot/result.json",
    "candidate-b/inspect-slot.stdout.log",
    "candidate-b/build-raw/result.json",
    "candidate-b/repack-envelope/result.json",
    "candidate-b/tmp/scratch",
)
TREES = (
    "materials", "materials/dtb-work", "materials/dtb-logs", "materials/dtb-tmp",
    "candidate-a", "candidate-a/inspect-slot", "candidate-a/build-raw",
    "candidate-a/repack-envelope", "candidate-a/tmp",
    "candidate-b", "candidate-b/inspect-slot", "candidate-b/build-raw",
    "candidate-b/repack-envelope", "candidate-b/tmp",
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def make_record(**overrides):
    values = {
        "sequence": 2,
        "partition_uuid": uuid.UUID(PARTITION).bytes,
        "installation_uuid": uuid.UUID(INSTALLATION).bytes,
        "transaction_uuid": uuid.UUID(TX).bytes,
        "state": trial_record.CONFIRMED,
        "from_slot": 0,
        "target_slot": 1,
        "remaining": 3,
        "attempt_limit": 3,
        "old_serial": 1,
        "new_serial": 2,
        "os_name": OS_NAME,
        "old_commit": OLD_COMMIT,
        "new_commit": NEW_COMMIT,
        "old_boot_sha256": bytes.fromhex("33" * 32),
        "new_boot_sha256": bytes.fromhex("44" * 32),
        "plan_sha256": bytes.fromhex("55" * 32),
        "policy": trial_record.OFFLINE_DUAL,
        "other_boot_sha256": bytes.fromhex("66" * 32),
    }
    values.update(overrides)
    return trial_record.TrialRecord(**values)


@ROOT_REQUIRED
class OfflineRetirementTest(unittest.TestCase):
    manifest_extra = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.base = self.tmp / "offline"
        self.base.mkdir(mode=0o700)
        self.session = self.base / SESSION
        self.session.mkdir(mode=0o700)
        self.transaction = self.session / "transaction"
        self.transaction.mkdir(mode=0o700)
        self.record = self._seed(self.manifest_extra)

    def fresh(self, *, manifest_extra=None):
        self.doCleanups()
        self.manifest_extra = manifest_extra
        self.setUp()

    @staticmethod
    def _write_json(directory, name, value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
        (directory / name).write_bytes(raw)
        return raw

    def _seed(self, manifest_extra=None):
        self.payload = self.session / "incoming" / "images" / "linux-root.img"
        (self.session / "incoming" / "images").mkdir(mode=0o700, parents=True)
        self.payload.write_bytes(PAYLOAD)

        package = b'{"schemaVersion":1,"files":[]}'
        signature = b"signature\n"
        for directory in (self.session, self.transaction):
            (directory / "package.json").write_bytes(package)
            (directory / "package.sig").write_bytes(signature)

        candidates = {}
        new_sha = {}
        for slot in ("a", "b"):
            root = self.transaction / ("candidate-" + slot)
            root.mkdir(mode=0o700)
            for name in ("inspect-slot", "build-raw", "repack-envelope", "tmp"):
                (root / name).mkdir(mode=0o700)
            (root / "inspect-slot" / "base.bin").write_bytes(
                ("base-" + slot).encode("ascii"))
            (root / "inspect-slot" / "result.json").write_bytes(b"{}")
            (root / "inspect-slot.stdout.log").write_bytes(b"inspect\n")
            (root / "build-raw" / "PackedKernel").write_bytes(
                ("packed-" + slot).encode("ascii"))
            (root / "build-raw" / "result.json").write_bytes(b"{}")
            data = ("candidate-" + slot).encode("ascii")
            (root / "repack-envelope" / "candidate.img").write_bytes(data)
            (root / "repack-envelope" / "result.json").write_bytes(b"{}")
            (root / "candidate.json").write_bytes(b"{}")
            (root / "tmp" / "scratch").write_bytes(b"tmp")
            candidates[slot] = root / "repack-envelope" / "candidate.img"
            new_sha[slot] = _sha(data)

        materials = self.transaction / "materials"
        materials.mkdir(mode=0o700)
        for name in ("dtb-work", "dtb-logs", "dtb-tmp"):
            (materials / name).mkdir(mode=0o700)
        (materials / "dtb-work" / "normal.dts").write_bytes(b"/dts-v1/;")
        (materials / "dtb-logs" / "normal.log").write_bytes(b"dtb log\n")
        (materials / "dtb-tmp" / "scratch").write_bytes(b"tmp")
        artifacts = {}
        for key, name in (
            ("kernel", "kernel.img"),
            ("initramfs", "initramfs.img"),
            ("runtime", "runtime.bin"),
            ("dtbNormal", "dtb-normal.img"),
            ("dtbDiagnostic", "dtb-diagnostic.img"),
        ):
            path = materials / name
            data = ("material-" + key).encode("ascii")
            path.write_bytes(data)
            artifacts[key] = {
                "path": str(path), "bytes": len(data), "sha256": _sha(data)}
        (materials / "source.dtb").write_bytes(b"source-dtb")
        self._write_json(materials, "materials.json", {
            "schemaVersion": 1, "phase": "materials_prepared", "artifacts": artifacts})

        sources = self.transaction / "trust" / "sources" / "slots"
        recovery_entries = []
        for slot in ("a", "b"):
            path = sources / slot / "boot"
            path.parent.mkdir(mode=0o700, parents=True)
            data = ("old-boot-" + slot).encode("ascii")
            path.write_bytes(data)
            recovery_entries.append({
                "path": "trust/sources/slots/%s/boot" % slot,
                "bytes": len(data), "sha256": _sha(data),
                "kind": "slot", "label": "%s.boot" % slot})
        for name in ("recovery-header.bin", "recovery-region.bin"):
            data = ("journal-" + name).encode("ascii")
            (self.transaction / name).write_bytes(data)
            recovery_entries.append({
                "path": name, "bytes": len(data), "sha256": _sha(data),
                "kind": "journal", "label": name})
        if manifest_extra is not None:
            data = (self.transaction / manifest_extra).read_bytes()
            recovery_entries.append({
                "path": manifest_extra, "bytes": len(data),
                "sha256": _sha(data), "kind": "extra", "label": manifest_extra})

        manifest = {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "transactionId": TX, "model": MODEL,
            "installationUuid": INSTALLATION, "recoveryPartUuid": PARTITION,
            "board": 1, "oldDeployment": OLD_DEPLOYMENT, "fromSlot": "a",
            "physical": dict(PHYSICAL), "partitions": {},
            "files": recovery_entries,
        }
        manifest_raw = self._write_json(
            self.transaction, "recovery-manifest.json", manifest)
        manifest_sha = _sha(manifest_raw)

        ack = {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "transactionId": TX,
            "planSha256": "", "recoveryManifestSha256": manifest_sha,
            "files": [
                {"path": entry["path"], "bytes": entry["bytes"],
                 "sha256": entry["sha256"]}
                for entry in recovery_entries],
        }

        original_sha = {
            "a": _sha(b"old-boot-a"), "b": _sha(b"old-boot-b")}
        plan = {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "policy": "offline-dual", "transactionId": TX, "model": MODEL,
            "channel": "stable", "sysrootPath": "/sysroot",
            "releaseId": "release-1", "version": "1.0.0",
            "sourceRevision": "revision", "ociDigest": "sha256:" + "0" * 64,
            "ostreeCommit": NEW_COMMIT.hex(), "fromSlot": "a", "targetSlot": "b",
            "oldDeployment": OLD_DEPLOYMENT, "newDeployment": NEW_DEPLOYMENT,
            "originalBootSha256": original_sha, "newBootSha256": new_sha,
            "recoveryManifestSha256": manifest_sha,
            "packageDescriptorSha256": _sha(package),
        }
        plan_raw = self._write_json(self.transaction, "plan.json", plan)
        plan_sha = _sha(plan_raw)
        ack["planSha256"] = plan_sha
        self._write_json(self.session, "ack.json", ack)
        self._write_json(self.transaction, "recovery-ack.json", ack)

        prepared = {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "policy": "offline-dual", "transactionId": TX,
            "plan": "plan.json", "planSha256": plan_sha,
            "trust": "trust.json", "materials": "materials.json",
            "candidates": {"a": "candidate-a.json", "b": "candidate-b.json"},
            "recoveryManifest": "recovery-manifest.json",
            "recoveryManifestSha256": manifest_sha,
            "packageDescriptor": "package.json",
            "packageSignature": "package.sig",
            "packageDescriptorSha256": _sha(package),
        }
        self._write_json(self.transaction, "prepared.json", prepared)

        status = {
            "schemaVersion": 1, "component": "system",
            "operation": "offline-upgrade", "policy": "offline-dual",
            "model": MODEL, "transactionId": TX, "planSha256": plan_sha,
            "fromSlot": "a", "targetSlot": "b",
            "oldDeployment": OLD_DEPLOYMENT, "newDeployment": NEW_DEPLOYMENT,
            "state": "awaiting_reboot", "phase": "awaiting_reboot",
            "requiresReboot": True, "bootWriteState": "written",
            "slotControlState": "not_issued",
            "bootPolicy": "dual-boot-no-slot-control",
            "recoveryManifestSha256": manifest_sha,
            "recoveryManifestBytes": len(manifest_raw),
        }
        self._write_json(self.transaction, "status.json", status)

        report = {
            "schemaVersion": 1, "phase": "dual_boots_written", "model": MODEL,
            "transactionUuid": TX, "deployment": NEW_DEPLOYMENT,
            "fromSlot": "a", "slotOrder": ["b", "a"],
            "physical": dict(PHYSICAL),
            "writes": {
                slot: {
                    "sourceBootSha256": original_sha[slot],
                    "candidate": {
                        "path": str(candidates[slot]),
                        "bytes": candidates[slot].stat().st_size,
                        "sha256": new_sha[slot]},
                    "bytesWritten": candidates[slot].stat().st_size,
                    "readbackSha256": new_sha[slot],
                } for slot in ("a", "b")},
        }
        self._write_json(self.transaction, "write-report.json", report)

        self._write_json(self.transaction, "trust.json", {
            "schemaVersion": 1, "physical": dict(PHYSICAL)})
        materials_document = {
            "schemaVersion": 1, "phase": "materials_prepared", "model": MODEL,
            "deployment": NEW_DEPLOYMENT, "transactionUuid": TX,
            "artifacts": artifacts,
        }
        self._write_json(self.transaction, "materials.json", materials_document)
        for slot in ("a", "b"):
            self._write_json(self.transaction, "candidate-%s.json" % slot, {
                "schemaVersion": 1, "phase": "candidate_prepared",
                "slot": slot, "model": MODEL, "deployment": NEW_DEPLOYMENT,
                "transactionUuid": TX,
                "candidate": {
                    "path": str(candidates[slot]),
                    "bytes": candidates[slot].stat().st_size,
                    "sha256": new_sha[slot]}})

        receipt = {
            "schemaVersion": 1, "sessionId": SESSION,
            "state": "awaiting_reboot", "jobOperation": "execute",
            "model": MODEL, "channel": "stable",
            "physical": dict(PHYSICAL),
            "payload": {"path": PAYLOAD_PATH, "bytes": len(PAYLOAD),
                        "sha256": _sha(PAYLOAD)},
            "receivedBytes": len(PAYLOAD), "transactionDir": "transaction",
        }
        self._write_json(self.session, "receipt.json", receipt)
        pending = self.base / "pending"
        pending.mkdir(mode=0o700)
        self._write_json(pending, TX + ".json", {
            "schemaVersion": 1, "transactionId": TX, "sessionId": SESSION})

        other = self.base / OTHER_SESSION
        other.mkdir(mode=0o700)
        (other / "keep.bin").write_bytes(b"another session")
        self._write_json(other, "receipt.json", {
            "schemaVersion": 1, "sessionId": OTHER_SESSION, "state": "uploading"})

        self.package = {"files": [{
            "role": "linux-root", "path": PAYLOAD_PATH,
            "bytes": len(PAYLOAD), "sha256": _sha(PAYLOAD),
            "compression": "gzip", "expandedBytes": len(PAYLOAD),
            "expandedSha256": _sha(PAYLOAD)}]}
        return make_record(plan_sha256=bytes.fromhex(plan_sha))

    def rewrite_manifest(self, **changes):
        manifest = json.loads(
            (self.transaction / "recovery-manifest.json").read_text())
        manifest.update(changes)
        manifest_raw = self._write_json(
            self.transaction, "recovery-manifest.json", manifest)
        manifest_sha = _sha(manifest_raw)

        plan = json.loads((self.transaction / "plan.json").read_text())
        plan["recoveryManifestSha256"] = manifest_sha
        plan_sha = _sha(self._write_json(self.transaction, "plan.json", plan))

        prepared = json.loads((self.transaction / "prepared.json").read_text())
        prepared.update(planSha256=plan_sha, recoveryManifestSha256=manifest_sha)
        self._write_json(self.transaction, "prepared.json", prepared)

        status = json.loads((self.transaction / "status.json").read_text())
        status.update(planSha256=plan_sha, recoveryManifestSha256=manifest_sha,
                      recoveryManifestBytes=len(manifest_raw))
        self._write_json(self.transaction, "status.json", status)

        ack = json.loads((self.session / "ack.json").read_text())
        ack.update(planSha256=plan_sha, recoveryManifestSha256=manifest_sha)
        self._write_json(self.session, "ack.json", ack)
        self._write_json(self.transaction, "recovery-ack.json", ack)
        self.record = make_record(plan_sha256=bytes.fromhex(plan_sha))

    def retire(self, record=None, package=None):
        with mock.patch.object(
                offline_session.package_contract, "load_package",
                return_value=self.package if package is None else package):
            return jr.retire_confirmed_offline_session(
                self.record if record is None else record, base=self.base)

    def preserve(self):
        snapshot = {}
        for name in COMPACT_DOCUMENTS + RECOVERY_FILES + KEPT_FILES:
            snapshot["transaction/" + name] = (
                self.transaction / name).read_bytes()
        for name in SESSION_DOCUMENTS:
            snapshot["session/" + name] = (self.session / name).read_bytes()
        snapshot["pointer"] = (self.base / "pending" / (TX + ".json")).read_bytes()
        snapshot["other"] = (
            self.base / OTHER_SESSION / "keep.bin").read_bytes()
        return snapshot

    def assert_retained(self):
        for name in BULKY_FILES:
            self.assertTrue((self.transaction / name).is_file(), name)
        self.assertTrue(self.payload.is_file())
        for name in TREES:
            self.assertTrue((self.transaction / name).is_dir(), name)
        self.assertTrue((self.session / "incoming" / "images").is_dir())

    def assert_preserved(self, snapshot):
        for name, data in snapshot.items():
            if name == "pointer":
                self.assertEqual(
                    (self.base / "pending" / (TX + ".json")).read_bytes(), data)
            elif name == "other":
                self.assertEqual(
                    (self.base / OTHER_SESSION / "keep.bin").read_bytes(), data)
            else:
                root, relative = name.split("/", 1)
                base = self.transaction if root == "transaction" else self.session
                self.assertEqual((base / relative).read_bytes(), data, name)

    def test_removes_only_the_agreed_bulky_files(self):
        snapshot = self.preserve()

        self.assertTrue(self.retire())

        for name in BULKY_FILES:
            self.assertFalse((self.transaction / name).exists(), name)
        self.assertFalse(self.payload.exists())
        for name in TREES:
            self.assertTrue((self.transaction / name).is_dir(), name)
        for name in KEPT_FILES:
            self.assertTrue((self.transaction / name).is_file(), name)
        self.assertTrue((self.session / "incoming" / "images").is_dir())
        self.assertTrue(self.session.is_dir())
        self.assert_preserved(snapshot)

    def test_repeated_retirement_is_idempotent(self):
        snapshot = self.preserve()
        self.assertTrue(self.retire())
        self.assertFalse(self.retire())
        self.assertTrue(self.session.is_dir())
        for name in TREES:
            self.assertTrue((self.transaction / name).is_dir(), name)
        self.assert_preserved(snapshot)

    def test_unconfirmed_or_foreign_journal_is_retained(self):
        for name, record in (
            ("trial", make_record(state=trial_record.TRIAL)),
            ("online", make_record(policy=trial_record.ONLINE_SINGLE)),
        ):
            with self.subTest(case=name):
                self.fresh()
                self.assertFalse(self.retire(record=record))
                self.assert_retained()

    def test_record_identity_mismatches_are_refused(self):
        cases = (
            ("other transaction",
             {"transaction_uuid": uuid.UUID(OTHER_TX).bytes}),
            ("other plan", {"plan_sha256": bytes.fromhex("77" * 32)}),
            ("other deployment", {"new_serial": 3}),
            ("other installation",
             {"installation_uuid": uuid.UUID(
                 "55555555-5555-4555-8555-555555555555").bytes}),
            ("other partition",
             {"partition_uuid": uuid.UUID(
                 "66666666-6666-4666-8666-666666666666").bytes}),
            ("other slots", {"from_slot": 1, "target_slot": 0}),
        )
        for name, changes in cases:
            with self.subTest(case=name):
                self.fresh()
                record = replace(self.record, **changes)
                self.assertFalse(self.retire(record=record))
                self.assert_retained()

    def test_receipt_and_pointer_must_describe_this_execute(self):
        for state in ("execute_queued", "preparing", "dispatch_uncertain",
                      "failed", "prepared"):
            with self.subTest(receipt=state):
                self.fresh()
                receipt = json.loads(
                    (self.session / "receipt.json").read_text())
                receipt["state"] = state
                self._write_json(self.session, "receipt.json", receipt)
                self.assertFalse(self.retire())
                self.assert_retained()
        self.fresh()
        receipt = json.loads((self.session / "receipt.json").read_text())
        receipt.update(state="executing", jobOperation="prepare")
        self._write_json(self.session, "receipt.json", receipt)
        self.assertFalse(self.retire())
        self.assert_retained()
        for pointer in (
            {"schemaVersion": 1, "transactionId": OTHER_TX, "sessionId": SESSION},
            {"schemaVersion": 1, "transactionId": TX, "sessionId": SESSION,
             "extra": 1},
            {"schemaVersion": 1, "transactionId": TX, "sessionId": OTHER_SESSION},
        ):
            with self.subTest(pointer=pointer):
                self.fresh()
                self._write_json(
                    self.base / "pending", TX + ".json", pointer)
                self.assertFalse(self.retire())
                self.assert_retained()

    def _set_receipt_state(self, state):
        receipt = json.loads((self.session / "receipt.json").read_text())
        receipt["state"] = state
        self._write_json(self.session, "receipt.json", receipt)

    def test_stale_executing_receipt_after_physical_confirmation_is_retired(self):
        # A power cut between the transaction status save and the receipt
        # update leaves the receipt at executing even though the confirmed
        # journal makes the terminal documents final.
        self._set_receipt_state("executing")
        snapshot = self.preserve()

        self.assertTrue(self.retire())

        for name in BULKY_FILES:
            self.assertFalse((self.transaction / name).exists(), name)
        self.assertFalse(self.payload.exists())
        for name in TREES:
            self.assertTrue((self.transaction / name).is_dir(), name)
        for name in KEPT_FILES:
            self.assertTrue((self.transaction / name).is_file(), name)
        # Retirement never rewrites the receipt; the jobs path repairs it.
        self.assert_preserved(snapshot)

    def test_stale_executing_receipt_with_incomplete_evidence_is_retained(self):
        def unfinished_status(transaction):
            status = json.loads((transaction / "status.json").read_text())
            status.update(state="writing_boot", phase="writing_boot",
                          bootWriteState="writing", requiresReboot=False)
            self._write_json(transaction, "status.json", status)

        cases = (
            ("missing write report",
             lambda transaction: (transaction / "write-report.json").unlink()),
            ("unfinished status", unfinished_status),
            ("missing acknowledgement",
             lambda transaction: (self.session / "ack.json").unlink()),
        )
        for name, tamper in cases:
            with self.subTest(case=name):
                self.fresh()
                self._set_receipt_state("executing")
                tamper(self.transaction)
                self.assertFalse(self.retire())
                self.assert_retained()

    def test_manifest_digest_and_bound_identity_are_required(self):
        self.fresh()
        manifest = json.loads(
            (self.transaction / "recovery-manifest.json").read_text())
        manifest["board"] = 2
        self._write_json(self.transaction, "recovery-manifest.json", manifest)
        self.assertFalse(self.retire())
        self.assert_retained()

        for name, value in (
            ("installation", "55555555-5555-4555-8555-555555555555"),
            ("partition", "66666666-6666-4666-8666-666666666666"),
        ):
            with self.subTest(identity=name):
                self.fresh()
                key = ("installationUuid" if name == "installation"
                       else "recoveryPartUuid")
                self.rewrite_manifest(**{key: value})
                self.assertFalse(self.retire())
                self.assert_retained()

    def test_corrupt_execute_evidence_is_refused(self):
        cases = (
            ("status transaction", "status.json",
             lambda doc: doc.update(transactionId=OTHER_TX)),
            ("prepared plan", "prepared.json",
             lambda doc: doc.update(planSha256="0" * 64)),
            ("write report readback", "write-report.json",
             lambda doc: doc["writes"]["a"].update(readbackSha256="0" * 64)),
        )
        for name, document, mutate in cases:
            with self.subTest(case=name):
                self.fresh()
                value = json.loads(
                    (self.transaction / document).read_text())
                mutate(value)
                self._write_json(self.transaction, document, value)
                self.assertFalse(self.retire())
                self.assert_retained()

    def test_recovery_manifest_members_are_never_removed(self):
        self.fresh(manifest_extra="materials/kernel.img")

        self.assertTrue(self.retire())

        self.assertTrue((self.transaction / "materials" / "kernel.img").is_file())
        for name in MATERIAL_FILES[1:]:
            self.assertFalse((self.transaction / "materials" / name).exists(), name)
        for name in BULKY_FILES:
            if not name.startswith("materials/"):
                self.assertFalse((self.transaction / name).exists(), name)
        self.assertFalse(self.payload.exists())
        for name in TREES:
            self.assertTrue((self.transaction / name).is_dir(), name)

    def test_payload_is_retained_when_its_binding_does_not_match(self):
        self.fresh()
        self.payload.write_bytes(PAYLOAD[:-1])
        self.assertTrue(self.retire())
        self.assertTrue(self.payload.is_file())
        self.assertFalse((self.transaction / "materials").joinpath(
            "kernel.img").exists())

        self.fresh()
        other = {"files": [{
            "role": "linux-root", "path": PAYLOAD_PATH,
            "bytes": len(PAYLOAD), "sha256": "0" * 64,
            "compression": "gzip", "expandedBytes": len(PAYLOAD),
            "expandedSha256": _sha(PAYLOAD)}]}
        self.assertTrue(self.retire(package=other))
        self.assertTrue(self.payload.is_file())
        self.assertFalse((self.transaction / "materials").joinpath(
            "kernel.img").exists())

    def test_mount_identity_mismatch_or_absence_skips_removal(self):
        def other_mount(fd):
            info = os.fstat(fd)
            return 1 if stat.S_ISDIR(info.st_mode) else 2

        with mock.patch.object(jr, "_fd_mount_id", side_effect=other_mount):
            self.assertFalse(self.retire())
        self.assert_retained()

        self.fresh()
        with mock.patch.object(jr, "_fd_mount_id", return_value=None):
            self.assertFalse(self.retire())
        self.assert_retained()

    def test_missing_or_untrusted_storage_is_a_noop(self):
        self.assertFalse(jr.retire_confirmed_offline_session(
            self.record, base=self.tmp / "missing"))
        self.assert_retained()

        os.chmod(self.base, 0o777)
        try:
            self.assertFalse(self.retire())
        finally:
            os.chmod(self.base, 0o700)
        self.assert_retained()


if __name__ == "__main__":
    unittest.main()
