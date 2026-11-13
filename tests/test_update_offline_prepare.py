# SPDX-License-Identifier: GPL-3.0-only
"""Tests for authenticated offline dual-slot update preparation."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import armada_os  # noqa: E402
import test_update_channel_release as release_fixture  # noqa: E402
from updates import boot_materials, offline_prepare, system_transaction  # noqa: E402
import updates.boot_writer  # noqa: E402
import updates.offline_boot_writer  # noqa: E402
import updates.slot_activation  # noqa: E402
import updates.trial_store  # noqa: E402

OLD_COMMIT = "a" * 64
NEW_COMMIT = "b" * 64
OLD_DEPLOYMENT = f"armada/{OLD_COMMIT}.0"
NEW_DEPLOYMENT = f"armada/{NEW_COMMIT}.0"
RECOVERY_UUID = uuid.UUID("11111111-2222-4333-8444-555555555555")
INSTALL_UUID = uuid.UUID("66666666-7777-4888-9999-aaaaaaaaaaaa")
PART_UUID = uuid.UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")
ORIGIN_REF = "armada/stable/arm64"
SLOT_KEYS = ("boot", "vendorBoot", "initBoot", "abl", "xblConfig", "vbmeta")


def _claim(path: Path, data: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    boot_materials._write_new(path, data)
    return {"path": str(path), "bytes": len(data), "sha256": sha256(data).hexdigest()}


class OfflinePrepareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._key_dir = tempfile.TemporaryDirectory()
        key_path = Path(cls._key_dir.name)
        cls.priv_key, cls.pub_key = release_fixture._generate_keypair(key_path, "test")
        cls.other_priv, cls.other_pub = release_fixture._generate_keypair(key_path, "other")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._key_dir.cleanup()

    def _signed_package(self, *, commit: str = NEW_COMMIT, min_ver: str = "0.8.0",
                        operations: list[str] | None = None,
                        priv_key: Path | None = None) -> tuple[bytes, bytes, dict]:
        pkg = release_fixture._upgrade_package_dict()
        pkg["ostreeCommit"] = commit
        if operations is not None:
            pkg["operations"] = operations
        pkg["minimumInstallerVersion"] = "0.8.0"
        pkg["minimumUpdaterVersion"] = min_ver
        doc = json.dumps(pkg, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        sig = release_fixture._sign_bytes(doc, priv_key or self.priv_key)
        return doc, sig, pkg

    @contextmanager
    def _case(self, model: str = "TB322FC", actual: str = "a",
              document: bytes | None = None, signature_b64: bytes | None = None,
              commit: str = NEW_COMMIT):
        with tempfile.TemporaryDirectory() as tmp_str, ExitStack() as stack:
            temp = Path(tmp_str).resolve()
            sysroot = temp / "sysroot"
            for sub in ("ostree/repo", "boot", "var/home"):
                (sysroot / sub).mkdir(parents=True, exist_ok=True)
            sysroot_marker = sysroot / "var/home/user-marker.txt"
            sysroot_marker.write_bytes(b"preserve-sysroot\n")

            payload = temp / "payload"
            payload.mkdir()
            payload_marker = payload / "source-marker.bin"
            payload_marker.write_bytes(b"preserve-payload\n")

            work = temp / "tx"
            lock_path = temp / "lock/update.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)

            if document is None or signature_b64 is None:
                doc, sig, _ = self._signed_package(commit=commit)
                document = doc if document is None else document
                signature_b64 = sig if signature_b64 is None else signature_b64

            doc_file = temp / "source-package.json"
            sig_file = temp / "source-package.sig"
            doc_file.write_bytes(document)
            sig_file.write_bytes(signature_b64)

            inactive = "b" if actual == "a" else "a"
            board = 1 if model == "TB322FC" else 2
            physical = {
                "model": model,
                "actualSlot": actual,
                "inactiveSlot": inactive,
                "deployment": OLD_DEPLOYMENT,
                "recoveryPartUuid": str(RECOVERY_UUID),
            }
            header_bytes = b"H" * 65536
            region_bytes = b"R" * 65536
            journal = SimpleNamespace(
                layout=SimpleNamespace(
                    partition_uuid=RECOVERY_UUID.bytes,
                    installation_uuid=INSTALL_UUID.bytes,
                    board=board,
                ),
                header=header_bytes,
                region=region_bytes,
            )
            captured_originals: dict[str, bytes] = {}
            extra_override: dict[str, dict] = {}

            def _capture_side_effect(*, output_dir: Path, boot_tool_path: Path) -> dict:
                slots = {"a": {}, "b": {}}
                for s in ("a", "b"):
                    for key in SLOT_KEYS:
                        rel = f"slots/{s}/{key}.img"
                        content = f"orig-{model}-{s}-{key}".encode("utf-8") * 64
                        captured_originals[rel] = content
                        slots[s][key] = _claim(output_dir / rel, content)
                extra_names = ["recovery_a", "recovery_b", "vbmeta_system_a", "vbmeta_system_b"]
                if board == 2:
                    extra_names.append("efisp")
                extras = {}
                for name in extra_names:
                    rel = f"extra/{name}.img"
                    content = f"extra-{model}-{name}".encode("utf-8") * 64
                    captured_originals[rel] = content
                    extras[name] = _claim(output_dir / rel, content)
                extras.update(extra_override)
                partitions = {
                    "boot_a": {"lun": 4, "firstLba": 100, "blockCount": 200, "partitionUuid": str(PART_UUID)},
                    "boot_b": {"lun": 4, "firstLba": 300, "blockCount": 200, "partitionUuid": str(RECOVERY_UUID)},
                }
                return {
                    "physical": dict(physical),
                    "mode": "aosp-resign",
                    "trust": {"a": {"maxRawBytes": 67108864}, "b": {"maxRawBytes": 67108864}},
                    "sources": {
                        "physical": dict(physical),
                        "slots": slots,
                        "extraFiles": extras,
                        "partitions": partitions,
                    },
                }

            def _import_side_effect(**kw) -> dict:
                return {
                    "package": json.loads(kw["document"].decode("utf-8")),
                    "commit": NEW_COMMIT,
                    "originRef": ORIGIN_REF,
                }

            def _deploy_side_effect(**kw) -> dict:
                return {"model": kw["model"], "deployment": NEW_DEPLOYMENT}

            def _materials_side_effect(**kw) -> dict:
                out = kw["output_dir"]
                out.mkdir(parents=True, exist_ok=True)
                return {
                    "transactionUuid": kw["transaction_uuid"],
                    "deployment": kw["target_binding"],
                    "model": kw["model"],
                }

            def _candidate_side_effect(**kw) -> dict:
                slot = kw["slot"]
                mats = kw["materials"]
                out = kw["output_dir"]
                data = f"new-candidate-{mats['model']}-{slot}-{mats['transactionUuid']}".encode("utf-8") * 64
                claim = _claim(out / f"boot-{slot}.img", data)
                return {
                    "phase": "candidate_prepared",
                    "slot": slot,
                    "model": mats["model"],
                    "transactionUuid": mats["transactionUuid"],
                    "deployment": mats["deployment"],
                    "mode": kw["mode"],
                    "sourceBootSha256": kw["slot_files"]["boot"]["sha256"],
                    "candidate": claim,
                }

            stack.enter_context(mock.patch.object(
                offline_prepare.trial_confirmation, "UPDATE_LOCK_PATH", lock_path))
            m_inspect = stack.enter_context(mock.patch.object(
                offline_prepare.physical_slots, "inspect_physical_slots",
                side_effect=lambda: dict(physical)))
            m_snapshot = stack.enter_context(mock.patch.object(
                offline_prepare.armada_os, "snapshot",
                return_value={
                    "repository": next(iter(armada_os.REPOSITORIES)),
                    "channel": "stable",
                    "staged": None,
                    "rollback_queued": False,
                }))
            m_recovery = stack.enter_context(mock.patch.object(
                offline_prepare.system_transaction, "_check_recovery",
                side_effect=lambda phys, old: journal))
            m_import = stack.enter_context(mock.patch.object(
                offline_prepare.offline_payload, "import_offline_payload",
                side_effect=_import_side_effect))
            m_capture = stack.enter_context(mock.patch.object(
                offline_prepare.boot_trust, "capture_boot_trust",
                side_effect=_capture_side_effect))
            m_deploy = stack.enter_context(mock.patch.object(
                offline_prepare.deployment_store, "append_verified_deployment",
                side_effect=_deploy_side_effect))
            m_materials = stack.enter_context(mock.patch.object(
                offline_prepare.boot_materials, "prepare_boot_materials",
                side_effect=_materials_side_effect))
            m_candidate = stack.enter_context(mock.patch.object(
                offline_prepare.boot_candidate, "prepare_boot_candidate",
                side_effect=_candidate_side_effect))

            spies = {
                "write_offline_boots": stack.enter_context(mock.patch.object(
                    updates.offline_boot_writer, "write_offline_boots")),
                "write_inactive_boot": stack.enter_context(mock.patch.object(
                    updates.boot_writer, "write_inactive_boot")),
                "activate_offline_slots": stack.enter_context(mock.patch.object(
                    updates.slot_activation, "activate_offline_slots")),
                "trial_save": stack.enter_context(mock.patch.object(
                    updates.trial_store, "save")),
            }

            def _run(**overrides) -> dict:
                kwargs = {
                    "document": document,
                    "signature_b64": signature_b64,
                    "payload_directory": payload,
                    "expected_channel": "stable",
                    "sysroot_path": sysroot,
                    "work_dir": work,
                    "trusted_key": self.pub_key,
                }
                kwargs.update(overrides)
                return offline_prepare.prepare_offline_update(**kwargs)

            def _assert_untouched() -> None:
                self.assertEqual(sysroot_marker.read_bytes(), b"preserve-sysroot\n")
                self.assertEqual(payload_marker.read_bytes(), b"preserve-payload\n")
                self.assertEqual(doc_file.read_bytes(), document)
                self.assertEqual(sig_file.read_bytes(), signature_b64)
                for rel, expected_bytes in captured_originals.items():
                    self.assertEqual((work / "trust" / rel).read_bytes(), expected_bytes)
                for spy in spies.values():
                    spy.assert_not_called()

            yield SimpleNamespace(
                temp=temp, sysroot=sysroot, payload=payload, work=work,
                document=document, signature_b64=signature_b64,
                physical=physical, journal=journal,
                captured_originals=captured_originals, extra_override=extra_override,
                inspect=m_inspect, snapshot=m_snapshot, recovery=m_recovery,
                import_payload=m_import, capture=m_capture, deploy=m_deploy,
                materials=m_materials, candidate=m_candidate,
                default_candidate=_candidate_side_effect,
                spies=spies, run=_run, assert_untouched=_assert_untouched,
            )

    def test_prepare_success_models_and_slots(self) -> None:
        for model in ("TB322FC", "TB323FU"):
            for actual in ("a", "b"):
                with self.subTest(model=model, actual=actual), self._case(model=model, actual=actual) as env:
                    status = env.run()
                    self.assertEqual(status["schemaVersion"], 1)
                    self.assertEqual(status["phase"], "awaiting_recovery_export")
                    self.assertEqual(status["state"], "awaiting_recovery_export")
                    self.assertEqual(status["bootWriteState"], "not_started")
                    self.assertFalse(status["requiresReboot"])
                    self.assertEqual(status["model"], model)
                    self.assertEqual(status["fromSlot"], actual)
                    self.assertEqual(status["targetSlot"], "b" if actual == "a" else "a")
                    self.assertEqual(status["oldDeployment"], OLD_DEPLOYMENT)
                    self.assertEqual(status["newDeployment"], NEW_DEPLOYMENT)
                    tx_uuid = uuid.UUID(status["transactionId"])
                    self.assertEqual(str(tx_uuid), status["transactionId"])

                    self.assertEqual((env.work / "package.json").read_bytes(), env.document)
                    self.assertEqual((env.work / "package.sig").read_bytes(), env.signature_b64)

                    manifest_raw = (env.work / "recovery-manifest.json").read_bytes()
                    manifest_sha = sha256(manifest_raw).hexdigest()
                    manifest = json.loads(manifest_raw.decode("utf-8"))
                    self.assertEqual(manifest["installationUuid"], str(INSTALL_UUID))
                    self.assertEqual(manifest["recoveryPartUuid"], str(RECOVERY_UUID))
                    self.assertEqual(manifest["board"], 1 if model == "TB322FC" else 2)
                    self.assertEqual(len(manifest["files"]), 18 if model == "TB322FC" else 19)
                    for entry in manifest["files"]:
                        fpath = env.work / entry["path"]
                        self.assertTrue(fpath.is_relative_to(env.work))
                        raw = fpath.read_bytes()
                        self.assertEqual(len(raw), entry["bytes"])
                        self.assertEqual(sha256(raw).hexdigest(), entry["sha256"])
                    self.assertEqual((env.work / "recovery-header.bin").read_bytes(), b"H" * 65536)
                    self.assertEqual((env.work / "recovery-region.bin").read_bytes(), b"R" * 65536)

                    plan_raw = (env.work / "plan.json").read_bytes()
                    plan_sha = sha256(plan_raw).hexdigest()
                    plan = json.loads(plan_raw.decode("utf-8"))
                    prepared = json.loads((env.work / "prepared.json").read_text("utf-8"))
                    saved_status = json.loads((env.work / "status.json").read_text("utf-8"))
                    self.assertEqual(saved_status, status)
                    self.assertEqual(status["planSha256"], plan_sha)
                    self.assertEqual(prepared["planSha256"], plan_sha)
                    self.assertEqual(status["recoveryManifestSha256"], manifest_sha)
                    self.assertEqual(plan["recoveryManifestSha256"], manifest_sha)
                    self.assertEqual(prepared["recoveryManifestSha256"], manifest_sha)

                    self.assertEqual([c.kwargs["slot"] for c in env.candidate.call_args_list], ["a", "b"])
                    trust_data = json.loads((env.work / "trust.json").read_text("utf-8"))
                    for s in ("a", "b"):
                        call_kw = next(c.kwargs for c in env.candidate.call_args_list if c.kwargs["slot"] == s)
                        self.assertEqual(call_kw["slot_files"], trust_data["sources"]["slots"][s])
                        cand_json = json.loads((env.work / f"candidate-{s}.json").read_text("utf-8"))
                        orig_sha = sha256(env.captured_originals[f"slots/{s}/boot.img"]).hexdigest()
                        self.assertEqual(plan["originalBootSha256"][s], orig_sha)
                        self.assertEqual(plan["newBootSha256"][s], cand_json["candidate"]["sha256"])
                        self.assertNotEqual(plan["originalBootSha256"][s], plan["newBootSha256"][s])
                    env.assert_untouched()

    def test_tb710fu_physical_model_is_rejected_before_package_use(self) -> None:
        # TB710FU installs but has no dual-slot backend, so preparation refuses
        # it even when the package descriptor is otherwise well formed.
        with self._case(model="TB710FU") as env:
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertEqual(str(ctx.exception), "unsupported physical model")
            env.assert_untouched()

    def test_package_authentication_and_up_to_date_gates(self) -> None:
        bad_doc, bad_sig, _ = self._signed_package(priv_key=self.other_priv)
        unsup_doc, unsup_sig, _ = self._signed_package(operations=["install"])
        ver_doc, ver_sig, _ = self._signed_package(min_ver="9.0.0")
        for label, doc, sig, err_part in (
            ("bad_sig", bad_doc, bad_sig, "signature"),
            ("unsupported_op", unsup_doc, unsup_sig, "operation is not declared by this package"),
            ("min_version", ver_doc, ver_sig, "version is older than minimumupdaterversion"),
        ):
            with self.subTest(case=label), self._case(document=doc, signature_b64=sig) as env:
                with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                    env.run()
                self.assertIn(err_part, str(ctx.exception).lower())
                self.assertFalse(env.work.exists())
                env.import_payload.assert_not_called()
                env.capture.assert_not_called()
                env.deploy.assert_not_called()
                env.materials.assert_not_called()
                env.candidate.assert_not_called()
                env.assert_untouched()

        same_doc, same_sig, _ = self._signed_package(commit=OLD_COMMIT)
        with self.subTest(case="same_commit"), self._case(document=same_doc, signature_b64=same_sig) as env:
            status = env.run()
            self.assertEqual(status["state"], "up_to_date")
            self.assertEqual(status["phase"], "complete")
            self.assertEqual(status["oldDeployment"], OLD_DEPLOYMENT)
            self.assertFalse(env.work.exists())
            env.import_payload.assert_not_called()
            env.capture.assert_not_called()
            env.deploy.assert_not_called()
            env.materials.assert_not_called()
            env.candidate.assert_not_called()
            env.assert_untouched()

    def test_mid_transaction_binding_and_drift_failures(self) -> None:
        with self.subTest(case="bad_import_commit"), self._case() as env:
            env.import_payload.side_effect = lambda **kw: {
                "package": json.loads(kw["document"].decode("utf-8")),
                "commit": "c" * 64,
                "originRef": ORIGIN_REF,
            }
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertIn("offline import does not match authenticated package", str(ctx.exception))
            self.assertEqual(ctx.exception.result["failedPhase"], "importing")
            env.capture.assert_not_called()
            env.assert_untouched()

        with self.subTest(case="bad_candidate_b_tx"), self._case() as env:
            def _cand_b_bad(**kw):
                res = env.default_candidate(**kw)
                if kw["slot"] == "b":
                    res["transactionUuid"] = str(uuid.uuid4())
                return res

            env.candidate.side_effect = _cand_b_bad
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertEqual(str(ctx.exception), "boot candidate binding mismatch for slot b")
            self.assertEqual(ctx.exception.result["failedPhase"], "preparing_candidates")
            self.assertEqual([c.kwargs["slot"] for c in env.candidate.call_args_list], ["a", "b"])
            env.assert_untouched()

        with self.subTest(case="journal_drift"), self._case() as env:
            calls = 0

            def _journal_drift(phys, old):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return env.journal
                return SimpleNamespace(layout=env.journal.layout, header=b"X" * 65536, region=env.journal.region)

            env.recovery.side_effect = _journal_drift
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertEqual(str(ctx.exception), "recovery journal changed during preparation")
            env.assert_untouched()

        with self.subTest(case="physical_third_inspection_drift"), self._case() as env:
            p_calls = 0

            def _phys_drift():
                nonlocal p_calls
                p_calls += 1
                if p_calls < 3:
                    return dict(env.physical)
                drifted = dict(env.physical)
                drifted["actualSlot"] = "b"
                return drifted

            env.inspect.side_effect = _phys_drift
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertEqual(str(ctx.exception), "physical slots changed before preparation completed")
            self.assertEqual(p_calls, 3)
            env.assert_untouched()

    def test_path_and_export_containment_guards(self) -> None:
        for sub in ("ostree/new", "boot/new"):
            with self.subTest(protected=sub), self._case() as env:
                with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                    env.run(work_dir=env.sysroot / sub)
                self.assertIn("inside deployment root", str(ctx.exception).lower())
                env.import_payload.assert_not_called()
                env.assert_untouched()

        with self.subTest(case="existing_work_marker"), self._case() as env:
            env.work.mkdir(parents=True)
            marker = env.work / "existing.txt"
            marker.write_bytes(b"keep-me")
            with self.assertRaises(system_transaction.SystemTransactionError):
                env.run()
            self.assertEqual(marker.read_bytes(), b"keep-me")
            env.import_payload.assert_not_called()
            env.assert_untouched()

        with self.subTest(case="symlink_sysroot"), self._case() as env:
            link_root = env.temp / "sysroot-link"
            link_root.symlink_to(env.sysroot)
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run(sysroot_path=link_root)
            self.assertIn("symlink", str(ctx.exception).lower())
            self.assertFalse(env.work.exists())
            env.import_payload.assert_not_called()
            env.assert_untouched()

        with self.subTest(case="extra_outside_work"), self._case() as env:
            outside = env.temp / "outside-recovery.img"
            env.extra_override["recovery_a"] = _claim(outside, b"O" * 512)
            with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                env.run()
            self.assertIn("outside the transaction", str(ctx.exception))
            env.assert_untouched()

    def test_failed_status_persistence_error_recorded(self) -> None:
        with self._case() as env:
            env.candidate.side_effect = RuntimeError("candidate tool failed")
            real_save = system_transaction._save_json

            def _save_wrapper(directory: Path, name: str, payload: dict) -> bytes:
                if name == "status.json" and payload.get("state") == "failed":
                    raise OSError("disk full")
                return real_save(directory, name, payload)

            with mock.patch.object(offline_prepare.system_transaction, "_save_json", side_effect=_save_wrapper):
                with self.assertRaises(system_transaction.SystemTransactionError) as ctx:
                    env.run()
            res = ctx.exception.result
            self.assertEqual(res["error"], "candidate tool failed")
            self.assertEqual(res["failedPhase"], "preparing_candidates")
            self.assertEqual(res["state"], "failed")
            self.assertIn("disk full", res["persistenceError"])
            env.assert_untouched()


if __name__ == "__main__":
    unittest.main()
