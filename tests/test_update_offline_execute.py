# SPDX-License-Identifier: GPL-3.0-only
"""Tests for execution of prepared, authenticated offline dual-slot updates."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_offline_prepare as prepare_fixture  # noqa: E402
from updates import (  # noqa: E402
    offline_boot_writer, offline_execute, slot_activation, system_transaction,
    trial_record,
)


class OfflineExecuteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        prepare_fixture.OfflinePrepareTest.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        prepare_fixture.OfflinePrepareTest.tearDownClass()

    def setUp(self) -> None:
        self.fixture = prepare_fixture.OfflinePrepareTest(
            methodName="test_prepare_success_models_and_slots")

    @staticmethod
    def _json(work: Path, name: str) -> dict:
        return json.loads((work / name).read_text("utf-8"))

    def _ack(self, env) -> dict:
        prepared = self._json(env.work, "prepared.json")
        manifest = self._json(env.work, "recovery-manifest.json")
        return {
            "schemaVersion": 1, "operation": "offline-upgrade",
            "transactionId": prepared["transactionId"],
            "planSha256": prepared["planSha256"],
            "recoveryManifestSha256": prepared["recoveryManifestSha256"],
            "files": [{k: f[k] for k in ("path", "bytes", "sha256")}
                      for f in manifest["files"]],
        }

    def _ready(self, env) -> tuple[dict, bytes, dict]:
        original_capture = env.capture.side_effect

        def capture(**kw):
            result = original_capture(**kw)
            result.update(schemaVersion=1, phase="boot_trust_checked")
            return result

        env.capture.side_effect = capture
        original_materials = env.materials.side_effect

        def materials(**kw):
            result = original_materials(**kw)
            result.update(target=str(kw["output_dir"] / "target"),
                          rootUuid="root-uuid", bootUuid="boot-uuid")
            return result

        env.materials.side_effect = materials
        env.journal.record = None
        initial = env.run()
        initial_bytes = (env.work / "status.json").read_bytes()
        return initial, initial_bytes, self._ack(env)

    @contextmanager
    def _execution(self, env):
        materials = self._json(env.work, "materials.json")
        options = {"deployment": materials["deployment"], "path": materials["target"],
                   "rootUuid": materials["rootUuid"], "bootUuid": materials["bootUuid"]}

        @contextmanager
        def open_recovery(*, writable):
            self.assertTrue(writable)
            yield 73, env.physical["recoveryPartUuid"], env.journal.layout.board

        def load(fd, partuuid, board):
            return SimpleNamespace(layout=env.journal.layout, header=env.journal.header,
                                   region=env.journal.region, record=env.journal.record)

        def save(fd, journal, record):
            env.journal.record = record

        env.spies["trial_save"].side_effect = save
        with ExitStack() as stack:
            mocks = SimpleNamespace(
                options=stack.enter_context(mock.patch.object(
                    offline_execute.deployment_options, "read_deployment_options",
                    return_value=options)),
                opened=stack.enter_context(mock.patch.object(
                    offline_execute.recovery_device, "open_recovery", open_recovery)),
                load=stack.enter_context(mock.patch.object(
                    offline_execute.trial_store, "load", side_effect=load)),
            )
            yield mocks

    def _writer_report(self, env, trust, candidates, progress):
        plan = self._json(env.work, "plan.json")
        writes = {}
        for slot in (plan["targetSlot"], plan["fromSlot"]):
            progress({"phase": "writing", "slot": slot})
            progress({"phase": "verified", "slot": slot})
            self.assertEqual(self._json(env.work, "status.json")["bootWriteEvent"],
                             {"phase": "verified", "slot": slot})
            writes[slot] = {
                "candidate": candidates[slot]["candidate"],
                "sourceBootSha256": plan["originalBootSha256"][slot],
                "bytesWritten": candidates[slot]["candidate"]["bytes"],
                "readbackSha256": plan["newBootSha256"][slot],
            }
        return {"phase": "dual_boots_written", "model": plan["model"],
                "physical": dict(env.physical), "transactionUuid": plan["transactionId"],
                "deployment": plan["newDeployment"], "fromSlot": plan["fromSlot"],
                "slotOrder": [plan["targetSlot"], plan["fromSlot"]], "writes": writes}

    def _activate(self, env, report):
        plan = self._json(env.work, "plan.json")
        return {"phase": "offline_slots_activated", "model": plan["model"],
                "fromSlot": plan["fromSlot"], "targetSlot": plan["targetSlot"],
                "selectedSlot": plan["targetSlot"],
                "transactionUuid": plan["transactionId"],
                "deployment": plan["newDeployment"],
                "recordSequence": env.journal.record.sequence,
                "currentReadbackSha256": plan["newBootSha256"][plan["fromSlot"]],
                "targetReadbackSha256": plan["newBootSha256"][plan["targetSlot"]]}

    def _install_success_spies(self, env) -> None:
        env.spies["write_offline_boots"].side_effect = (
            lambda trust, candidates, progress: self._writer_report(
                env, trust, candidates, progress))
        env.spies["activate_offline_slots"].side_effect = lambda report: self._activate(env, report)

    def _assert_sources_untouched(self, env) -> None:
        self.assertEqual((env.sysroot / "var/home/user-marker.txt").read_bytes(),
                         b"preserve-sysroot\n")
        self.assertEqual((env.payload / "source-marker.bin").read_bytes(), b"preserve-payload\n")
        self.assertEqual((env.temp / "source-package.json").read_bytes(), env.document)
        self.assertEqual((env.temp / "source-package.sig").read_bytes(), env.signature_b64)
        for rel, data in env.captured_originals.items():
            self.assertEqual((env.work / "trust" / rel).read_bytes(), data)
        env.spies["write_inactive_boot"].assert_not_called()

    def _assert_no_execution(self, env, initial_bytes: bytes) -> None:
        self.assertEqual((env.work / "status.json").read_bytes(), initial_bytes)
        self.assertFalse((env.work / "recovery-ack.json").exists())
        env.spies["write_offline_boots"].assert_not_called()
        env.spies["activate_offline_slots"].assert_not_called()
        env.spies["trial_save"].assert_not_called()
        self._assert_sources_untouched(env)

    def test_success_models_slots_record_progress_and_single_execution(self) -> None:
        for model in ("TB322FC", "TB323FU"):
            for actual in ("a", "b"):
                with self.subTest(model=model, actual=actual), self.fixture._case(
                        model=model, actual=actual) as env:
                    initial, _, ack = self._ready(env)
                    self._install_success_spies(env)
                    imports = env.import_payload.call_count
                    with self._execution(env):
                        result = offline_execute.execute_offline_update(
                            work_dir=env.work, recovery_ack=ack, trusted_key=self.fixture.pub_key)
                    self.assertEqual((result["state"], result["phase"]),
                                     ("awaiting_reboot", "awaiting_reboot"))
                    self.assertTrue(result["requiresReboot"])
                    inactive = initial["targetSlot"]
                    record = env.journal.record
                    self.assertEqual(record.from_slot, 0 if actual == "a" else 1)
                    self.assertEqual(record.target_slot, 0 if inactive == "a" else 1)
                    self.assertEqual(record.old_boot_sha256.hex(),
                                     self._json(env.work, "plan.json")["originalBootSha256"][actual])
                    self.assertEqual(record.new_boot_sha256.hex(),
                                     self._json(env.work, "plan.json")["newBootSha256"][inactive])
                    self.assertEqual(record.other_boot_sha256.hex(),
                                     self._json(env.work, "plan.json")["newBootSha256"][actual])
                    self.assertEqual(record.policy, trial_record.OFFLINE_DUAL)
                    self.assertEqual(trial_record.decode_record(
                        trial_record.encode_record(record), record.partition_uuid,
                        record.installation_uuid), record)
                    self.assertEqual(result["bootWriteEvent"]["slot"], actual)
                    self.assertTrue((env.work / "recovery-ack.json").is_file())
                    self.assertTrue((env.work / "write-report.json").is_file())
                    self.assertTrue((env.work / "activation-report.json").is_file())
                    self.assertEqual(env.import_payload.call_count, imports)
                    self._assert_sources_untouched(env)
                    with self._execution(env):
                        with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                            offline_execute.execute_offline_update(
                                work_dir=env.work, recovery_ack=ack,
                                trusted_key=self.fixture.pub_key)
                    self.assertIn("awaiting_recovery_export", str(raised.exception))
                    self.assertEqual(env.spies["write_offline_boots"].call_count, 1)

    def test_acknowledgement_failures_have_no_effects(self) -> None:
        cases = ("missing", "wrong_tx", "wrong_plan", "missing_file", "duplicate", "wrong_hash")
        for case in cases:
            with self.subTest(case=case), self.fixture._case() as env:
                _, initial_bytes, ack = self._ready(env)
                if case == "missing":
                    ack = None
                    message = "recovery acknowledgement has invalid key set"
                else:
                    if case == "wrong_tx":
                        ack["transactionId"] = str(uuid.uuid4())
                        message = "recovery acknowledgement does not bind this transaction"
                    elif case == "wrong_plan":
                        ack["planSha256"] = "0" * 64
                        message = "recovery acknowledgement does not bind this transaction"
                    elif case == "missing_file":
                        ack["files"].pop()
                        message = "recovery acknowledgement file set does not match manifest"
                    elif case == "duplicate":
                        ack["files"][-1] = dict(ack["files"][0])
                        message = "recovery acknowledgement repeats a file entry"
                    else:
                        ack["files"][0]["sha256"] = "0" * 64
                        message = "recovery acknowledgement file set does not match manifest"
                with self._execution(env):
                    with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                        offline_execute.execute_offline_update(
                            work_dir=env.work, recovery_ack=ack, trusted_key=self.fixture.pub_key)
                self.assertEqual(str(raised.exception), message)
                self._assert_no_execution(env, initial_bytes)

    def test_artifact_source_tamper_and_failed_status_refuse_execution(self) -> None:
        for case in ("plan", "prepared", "candidate", "original", "failed_status"):
            with self.subTest(case=case), self.fixture._case() as env:
                _, initial_bytes, ack = self._ready(env)
                if case == "plan":
                    (env.work / "plan.json").write_bytes(b"{}\n")
                elif case == "prepared":
                    path = env.work / "prepared.json"
                    doc = self._json(env.work, "prepared.json")
                    doc["planSha256"] = "0" * 64
                    path.write_text(json.dumps(doc), "utf-8")
                elif case == "candidate":
                    candidate = self._json(env.work, "candidate-a.json")
                    Path(candidate["candidate"]["path"]).write_bytes(b"tampered")
                elif case == "original":
                    (env.work / "trust/slots/a/boot.img").write_bytes(b"tampered")
                    env.captured_originals["slots/a/boot.img"] = b"tampered"
                else:
                    path = env.work / "status.json"
                    doc = self._json(env.work, "status.json")
                    doc["state"] = "failed"
                    path.write_text(json.dumps(doc), "utf-8")
                    initial_bytes = path.read_bytes()
                with self._execution(env):
                    with self.assertRaises(system_transaction.SystemTransactionError):
                        offline_execute.execute_offline_update(
                            work_dir=env.work, recovery_ack=ack, trusted_key=self.fixture.pub_key)
                self._assert_no_execution(env, initial_bytes)

    def test_options_journal_and_physical_drift_fail_preflight(self) -> None:
        cases = ("options", "journal", "physical")
        for case in cases:
            with self.subTest(case=case), self.fixture._case() as env:
                _, initial_bytes, ack = self._ready(env)
                if case == "journal":
                    env.recovery.side_effect = lambda phys, old: SimpleNamespace(
                        layout=env.journal.layout, header=b"X" * 65536,
                        region=env.journal.region, record=None)
                elif case == "physical":
                    drifted = dict(env.physical)
                    drifted["actualSlot"] = env.physical["inactiveSlot"]
                    env.inspect.side_effect = lambda: dict(drifted)
                with self._execution(env) as mocks:
                    if case == "options":
                        mocks.options.side_effect = ValueError("target deployment missing")
                    with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                        offline_execute.execute_offline_update(
                            work_dir=env.work, recovery_ack=ack, trusted_key=self.fixture.pub_key)
                expected = {"options": "target deployment missing",
                            "journal": "recovery manifest does not cover captured sources exactly",
                            "physical": "physical active/inactive slots invalid"}[case]
                self.assertEqual(str(raised.exception), expected)
                self._assert_no_execution(env, initial_bytes)

    def test_boot_writer_error_states(self) -> None:
        cases = (("not_needed", "failed", "not_started"),
                 ("restored", "failed", "restored"),
                 ("failed", "recovery_required", "uncertain"),
                 ("oserror", "recovery_required", "uncertain"))
        for restore, state, write_state in cases:
            with self.subTest(restore=restore), self.fixture._case() as env:
                self._ready(env)
                ack = self._ack(env)
                if restore == "oserror":
                    env.spies["write_offline_boots"].side_effect = OSError("device I/O")
                else:
                    error = offline_boot_writer.BootWriteError("write failed", restore_status=restore)
                    error.slot_results = {"a": "failed"}
                    env.spies["write_offline_boots"].side_effect = error
                with self._execution(env):
                    with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                        offline_execute.execute_offline_update(
                            work_dir=env.work, recovery_ack=ack, trusted_key=self.fixture.pub_key)
                result = raised.exception.result
                self.assertEqual((result["state"], result["bootWriteState"]), (state, write_state))
                self.assertFalse(result["requiresReboot"])
                if restore != "oserror":
                    self.assertEqual(result["slotResults"], {"a": "failed"})
                env.spies["trial_save"].assert_not_called()
                env.spies["activate_offline_slots"].assert_not_called()

    def test_post_write_failures_are_recovery_required(self) -> None:
        for case in ("malformed_report", "report_save", "journal_compare", "journal_save"):
            with self.subTest(case=case), self.fixture._case() as env:
                self._ready(env)
                ack = self._ack(env)
                self._install_success_spies(env)
                if case == "malformed_report":
                    env.spies["write_offline_boots"].side_effect = None
                    env.spies["write_offline_boots"].return_value = {}
                real_save = system_transaction._save_json

                def save_json(directory, name, payload):
                    if case == "report_save" and name == "write-report.json":
                        raise OSError("report disk full")
                    return real_save(directory, name, payload)

                with self._execution(env) as mocks:
                    if case == "journal_compare":
                        mocks.load.side_effect = lambda *args: SimpleNamespace(
                            layout=env.journal.layout, header=b"bad", region=b"bad", record=None)
                    elif case == "journal_save":
                        env.spies["trial_save"].side_effect = OSError("journal disk full")
                    with mock.patch.object(offline_execute.system_transaction, "_save_json",
                                           side_effect=save_json):
                        with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                            offline_execute.execute_offline_update(
                                work_dir=env.work, recovery_ack=ack,
                                trusted_key=self.fixture.pub_key)
                result = raised.exception.result
                self.assertEqual(result["state"], "recovery_required")
                self.assertEqual(result["bootWriteState"],
                                 "uncertain" if case == "malformed_report" else "written")
                self.assertFalse(result["requiresReboot"])
                env.spies["activate_offline_slots"].assert_not_called()

    def test_activation_failures_and_final_status_save_failure(self) -> None:
        cases = ("unchanged", "uncertain", "wrong_report", "final_save")
        for case in cases:
            with self.subTest(case=case), self.fixture._case() as env:
                self._ready(env)
                ack = self._ack(env)
                self._install_success_spies(env)
                if case in ("unchanged", "uncertain"):
                    env.spies["activate_offline_slots"].side_effect = slot_activation.ActivationError(
                        "activation failed", outcome=case)
                elif case == "wrong_report":
                    env.spies["activate_offline_slots"].side_effect = None
                    env.spies["activate_offline_slots"].return_value = {"phase": "wrong"}
                real_save = system_transaction._save_json

                def save_json(directory, name, payload):
                    if case == "final_save" and name == "status.json" and payload.get(
                            "state") == "awaiting_reboot":
                        raise OSError("final status disk full")
                    return real_save(directory, name, payload)

                with self._execution(env):
                    with mock.patch.object(offline_execute.system_transaction, "_save_json",
                                           side_effect=save_json):
                        with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                            offline_execute.execute_offline_update(
                                work_dir=env.work, recovery_ack=ack,
                                trusted_key=self.fixture.pub_key)
                result = raised.exception.result
                self.assertEqual(result["state"], "recovery_required")
                self.assertEqual(result["bootWriteState"], "written")
                self.assertFalse(result["requiresReboot"])
                self.assertNotEqual(result["activationOutcome"], "activated")
                if case in ("unchanged", "uncertain"):
                    self.assertEqual(result["activationOutcome"], case)
                if case == "final_save":
                    self.assertIn("final status disk full", result["error"])
                    self.assertNotIn("persistenceError", result)

    @contextmanager
    def _recovery_ready_case(self, model="TB322FC", actual="a"):
        with self.fixture._recovery_case(model=model, actual=actual) as rc:
            original_capture = rc.env.capture.side_effect

            def capture(**kw):
                result = original_capture(**kw)
                result.update(schemaVersion=1)
                return result

            rc.env.capture.side_effect = capture
            original_materials = rc.env.materials.side_effect

            def materials(**kw):
                result = original_materials(**kw)
                result.update(target=str(kw["output_dir"] / "target"),
                              rootUuid="root-uuid", bootUuid="boot-uuid")
                return result

            rc.env.materials.side_effect = materials
            rc.env.journal.record = None
            rc.run()
            rc.env.work = rc.work
            ack = self._ack(rc.env)
            initial_status = (rc.work / "status.json").read_bytes()
            recovery = {
                "identity": json.loads(json.dumps(rc.session_identity)),
                "temporary_boot_sha256": rc.temp_sha,
                "context": json.loads(json.dumps(rc.context_report)),
            }
            yield rc, ack, recovery, initial_status

    def _assert_recovery_preflight_only(self, rc, initial_status) -> None:
        self.assertEqual((rc.work / "status.json").read_bytes(), initial_status)
        self.assertFalse((rc.work / "recovery-ack.json").exists())
        for name in ("write_offline_boots", "activate_offline_slots", "trial_save"):
            rc.env.spies[name].assert_not_called()
        rc.env.inspect.assert_not_called()
        rc.env.snapshot.assert_not_called()
        rc.assert_untouched()

    def test_recovery_preflight_success_models_and_slots(self) -> None:
        for model in ("TB322FC", "TB323FU"):
            for actual in ("a", "b"):
                with self.subTest(model=model, actual=actual), self._recovery_ready_case(
                        model=model, actual=actual) as (rc, ack, recovery, initial_status):
                    env = rc.env
                    with self._execution(env) as mocks:
                        ctx = offline_execute._preflight(
                            env.work, ack, "0.8.0", self.fixture.pub_key,
                            recovery=recovery)
                    self.assertNotIn("deployment", ctx["initial"])
                    self.assertNotIn("booted", ctx["initial"])
                    self.assertEqual(
                        ctx["old"],
                        offline_execute.system_transaction._binding(
                            recovery["identity"]["installedDeployment"]))
                    self.assertEqual(
                        ctx["tx"],
                        self._json(env.work, "prepared.json")["transactionId"])
                    self.assertEqual(
                        env.work, Path(recovery["context"]["sysrootPath"]) /
                        ".armada-update" / rc.session_id / "transaction")
                    self.assertEqual(
                        mocks.options.call_args.kwargs["current_binding"],
                        recovery["identity"]["installedDeployment"])
                    for slot in ("a", "b"):
                        boot = recovery["identity"]["originalBoots"][slot]
                        self.assertEqual(
                            sha256(rc.original_contents[slot]).hexdigest(),
                            boot["sha256"])
                        self.assertEqual(
                            len(rc.original_contents[slot]), boot["bytes"])
                    self._assert_recovery_preflight_only(rc, initial_status)

    def test_recovery_preflight_rejects_binding_drift(self) -> None:
        cases = ("identity_schema", "session", "entry_slot", "model", "boot_sha",
                 "boot_bytes", "partition_uuid", "temporary_uppercase",
                 "context_channel", "installed_binding", "sysroot",
                 "physical_drift", "booted")
        for case in cases:
            with self.subTest(case=case), self._recovery_ready_case() as (
                    rc, ack, recovery, initial_status):
                identity = recovery["identity"]
                if case == "identity_schema":
                    identity["schemaVersion"] = 3
                elif case == "session":
                    identity["sessionId"] = "2" * 32
                elif case == "entry_slot":
                    identity["entrySlot"] = "b" if identity["entrySlot"] == "a" else "a"
                elif case == "model":
                    identity["model"] = ("TB323FU" if identity["model"] == "TB322FC"
                                         else "TB322FC")
                elif case == "boot_sha":
                    identity["originalBoots"]["a"]["sha256"] = "0" * 64
                elif case == "boot_bytes":
                    identity["originalBoots"]["a"]["bytes"] += 1
                elif case == "partition_uuid":
                    identity["originalBoots"]["a"]["partitionUuid"] = str(uuid.uuid4())
                elif case == "temporary_uppercase":
                    recovery["temporary_boot_sha256"] = rc.temp_sha.upper()
                elif case == "context_channel":
                    recovery["context"]["channel"] = "beta"
                elif case == "installed_binding":
                    recovery["context"]["installed"]["installedDeployment"] = "unexpected"
                elif case == "sysroot":
                    recovery["context"]["sysrootPath"] = str(rc.env.temp)
                elif case == "physical_drift":
                    rc.physical["actualSlot"] = rc.env.physical["inactiveSlot"]
                else:
                    rc.physical["booted"] = rc.physical["actualSlot"]
                with self._execution(rc.env):
                    with self.assertRaises((
                            ValueError,
                            offline_execute.recovery_authorization.RecoveryAuthorizationError,
                            offline_execute.boot_candidate.BootCandidateError)):
                        offline_execute._preflight(
                            rc.env.work, ack, "0.8.0", self.fixture.pub_key,
                            recovery=recovery)
                self._assert_recovery_preflight_only(rc, initial_status)

    def test_recovery_preflight_pins_independent_bindings(self) -> None:
        for case in ("plan_session", "manifest_session", "plan_temp",
                     "file_content", "ack_missing_file", "package_sig"):
            with self.subTest(case=case), self._recovery_ready_case() as (
                    rc, ack, recovery, initial_status):
                work = rc.work
                if case in ("plan_session", "manifest_session", "plan_temp"):
                    prepared = self._json(work, "prepared.json")
                    saved_status = self._json(work, "status.json")
                    if case in ("plan_session", "plan_temp"):
                        doc = self._json(work, "plan.json")
                        if case == "plan_session":
                            doc["recoverySessionId"] = "2" * 32
                        else:
                            doc["temporaryBootSha256"] = "f" * 64
                        raw = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
                        (work / "plan.json").write_bytes(raw)
                        prepared["planSha256"] = sha256(raw).hexdigest()
                        saved_status["planSha256"] = prepared["planSha256"]
                        ack["planSha256"] = prepared["planSha256"]
                    else:
                        doc = self._json(work, "recovery-manifest.json")
                        doc["recoverySessionId"] = "2" * 32
                        raw = (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8")
                        (work / "recovery-manifest.json").write_bytes(raw)
                        prepared["recoveryManifestSha256"] = sha256(raw).hexdigest()
                        plan = self._json(work, "plan.json")
                        plan["recoveryManifestSha256"] = prepared["recoveryManifestSha256"]
                        (work / "plan.json").write_text(
                            json.dumps(plan, sort_keys=True) + "\n", "utf-8")
                        prepared["planSha256"] = sha256(
                            (work / "plan.json").read_bytes()).hexdigest()
                        saved_status.update(
                            planSha256=prepared["planSha256"],
                            recoveryManifestSha256=prepared["recoveryManifestSha256"])
                        ack.update(
                            planSha256=prepared["planSha256"],
                            recoveryManifestSha256=prepared["recoveryManifestSha256"])
                    (work / "prepared.json").write_text(
                        json.dumps(prepared, sort_keys=True) + "\n", "utf-8")
                    (work / "status.json").write_text(
                        json.dumps(saved_status, sort_keys=True) + "\n", "utf-8")
                    initial_status = (work / "status.json").read_bytes()
                elif case == "file_content":
                    (work / "trust/slots/a/boot.img").write_bytes(b"tampered\n")
                elif case == "ack_missing_file":
                    ack["files"].pop()
                else:
                    (work / "package.sig").write_bytes(b"not-a-signature\n")
                with self._execution(rc.env):
                    with self.assertRaises((
                            ValueError,
                            offline_execute.recovery_authorization.RecoveryAuthorizationError,
                            offline_execute.boot_candidate.BootCandidateError)):
                        offline_execute._preflight(
                            rc.env.work, ack, "0.8.0", self.fixture.pub_key,
                            recovery=recovery)
                self._assert_recovery_preflight_only(rc, initial_status)

    @contextmanager
    def _recovery_execution(self, rc, exit_failure=None, writer=None, activator=None):
        with self._execution(rc.env) as mocks, ExitStack() as outer:
            contexts = []
            exits = []
            expected_parent = str(rc.env.temp / "parent")
            original_side_effect = rc.context.side_effect

            def opened(*, expected_binding, expected_physical, parent_dir,
                       expected_selected_slot_after):
                self.assertEqual(expected_binding,
                                 rc.session_identity["installedDeployment"])
                self.assertEqual(expected_physical, dict(rc.physical))
                self.assertEqual(parent_dir, expected_parent)
                self.assertEqual(expected_selected_slot_after,
                                 rc.physical["inactiveSlot"])

                @contextmanager
                def opened_context():
                    context = json.loads(json.dumps(rc.context_report))
                    contexts.append(context)
                    try:
                        yield context
                    except Exception as exc:
                        exits.append(exc)
                        raise
                    else:
                        exits.append(None)
                    finally:
                        contexts.remove(context)
                        if exit_failure is not None:
                            raise type(exit_failure)(*exit_failure.args)

                return opened_context()

            rc.context.side_effect = opened

            def write_recovery(trust, candidates, *, session_identity,
                               temporary_boot_sha256, progress):
                self.assertEqual(len(contexts), 1)
                self.assertEqual(session_identity["sessionId"], rc.session_id)
                self.assertEqual(temporary_boot_sha256, rc.temp_sha)
                if writer is not None:
                    return writer(trust, candidates, progress)
                report = self._writer_report(rc.env, trust, candidates, progress)
                report["physical"] = dict(rc.physical)
                report.update(runtime="recovery", recoverySessionId=rc.session_id,
                              oldDeployment=rc.session_identity["installedDeployment"],
                              temporaryBootSha256=rc.temp_sha)
                return report

            def activate(report, *, session_identity, temporary_boot_sha256):
                self.assertEqual(len(contexts), 1)
                self.assertEqual(rc.env.journal.record.state, trial_record.PREPARED)
                self.assertEqual(session_identity["sessionId"], rc.session_id)
                self.assertEqual(temporary_boot_sha256, rc.temp_sha)
                if activator is not None:
                    return activator(report)
                activated = self._activate(rc.env, report)
                activated.update(runtime="recovery", recoverySessionId=rc.session_id,
                                 oldDeployment=rc.session_identity["installedDeployment"])
                return activated

            write_recovery_mock = outer.enter_context(mock.patch.object(
                offline_execute.offline_boot_writer, "write_recovery_offline_boots",
                side_effect=write_recovery))
            activate_mock = outer.enter_context(mock.patch.object(
                offline_execute.slot_activation, "activate_recovery_offline_slots",
                side_effect=activate))
            try:
                yield SimpleNamespace(contexts=contexts, exits=exits, mocks=mocks,
                                      write_recovery=write_recovery_mock,
                                      activate=activate_mock)
            finally:
                rc.context.side_effect = original_side_effect

    def _run_recovery(self, rc, ack, *, identity="keep", temp="keep"):
        return offline_execute.execute_recovery_offline_update(
            session_identity=(rc.session_identity if identity == "keep" else identity),
            temporary_boot_sha256=(rc.temp_sha if temp == "keep" else temp),
            recovery_ack=ack, parent_dir=str(rc.env.temp / "parent"),
            trusted_key=self.fixture.pub_key)

    def test_recovery_execution_success_models_slots_and_single_run(self) -> None:
        for model in ("TB322FC", "TB323FU"):
            for actual in ("a", "b"):
                with self.subTest(model=model, actual=actual), self._recovery_ready_case(
                        model=model, actual=actual) as (rc, ack, recovery, initial_status):
                    plan = self._json(rc.work, "plan.json")
                    inactive = plan["targetSlot"]
                    with self._recovery_execution(rc) as run:
                        result = self._run_recovery(rc, ack)
                        self.assertEqual(run.contexts, [])
                    self.assertEqual((result["state"], result["phase"]),
                                     ("awaiting_reboot", "awaiting_reboot"))
                    self.assertTrue(result["requiresReboot"])
                    self.assertEqual(result["runtime"], "recovery")
                    self.assertEqual(result["recoverySessionId"], rc.session_id)
                    record = rc.env.journal.record
                    self.assertEqual(record.state, trial_record.PREPARED)
                    self.assertEqual(record.old_boot_sha256.hex(),
                                     plan["originalBootSha256"][actual])
                    self.assertEqual(record.new_boot_sha256.hex(),
                                     plan["newBootSha256"][inactive])
                    self.assertEqual(record.other_boot_sha256.hex(),
                                     plan["newBootSha256"][actual])
                    self.assertEqual(record.policy, trial_record.OFFLINE_DUAL)
                    write_report = self._json(rc.work, "write-report.json")
                    self.assertEqual(write_report["slotOrder"], [inactive, actual])
                    self.assertEqual(write_report["runtime"], "recovery")
                    self.assertEqual(write_report["recoverySessionId"], rc.session_id)
                    self.assertEqual(write_report["temporaryBootSha256"], rc.temp_sha)
                    self.assertEqual(write_report["oldDeployment"],
                                     rc.session_identity["installedDeployment"])
                    activated = self._json(rc.work, "activation-report.json")
                    self.assertEqual(activated["runtime"], "recovery")
                    self.assertEqual(activated["recoverySessionId"], rc.session_id)
                    self.assertEqual(activated["oldDeployment"],
                                     rc.session_identity["installedDeployment"])
                    self.assertNotIn("oldBinding", activated)
                    self.assertNotIn("temporaryBootSha256", activated)
                    for name in ("recovery-ack.json", "write-report.json",
                                 "activation-report.json"):
                        self.assertTrue((rc.work / name).is_file())
                    self.assertEqual(self._json(rc.work, "status.json")["state"],
                                     "awaiting_reboot")
                    self.assertNotEqual((rc.work / "status.json").read_bytes(),
                                        initial_status)
                    self.assertNotIn("booted", rc.physical)
                    self.assertNotIn("deployment", rc.physical)
                    self.assertEqual(rc.env.spies["trial_save"].call_count, 1)
                    rc.env.spies["write_offline_boots"].assert_not_called()
                    rc.env.spies["activate_offline_slots"].assert_not_called()
                    rc.env.inspect.assert_not_called()
                    rc.env.snapshot.assert_not_called()
                    self._assert_sources_untouched(rc.env)
                    with self._recovery_execution(rc) as run:
                        with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                            self._run_recovery(rc, ack)
                    self.assertIn("awaiting_recovery_export", str(raised.exception))
                    self.assertEqual(run.contexts, [])
                    run.write_recovery.assert_not_called()
                    run.activate.assert_not_called()

    def test_recovery_execution_rejection_before_write(self) -> None:
        cases = ("identity_none", "bad_temp", "context_mismatch", "bad_ack")
        for case in cases:
            with self.subTest(case=case), self._recovery_ready_case() as (
                    rc, ack, recovery, initial_status):
                identity = temp = "keep"
                if case == "identity_none":
                    identity = None
                elif case == "bad_temp":
                    temp = "not-a-sha"
                elif case == "context_mismatch":
                    rc.context_report["channel"] = "beta"
                else:
                    ack["files"].pop()
                with self._recovery_execution(rc) as run:
                    with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                        self._run_recovery(rc, ack, identity=identity, temp=temp)
                result = raised.exception.result
                self.assertEqual(result["state"], "failed")
                self.assertFalse(result["requiresReboot"])
                self.assertEqual(run.contexts, [])
                self.assertEqual((rc.work / "status.json").read_bytes(), initial_status)
                self.assertFalse((rc.work / "recovery-ack.json").exists())
                rc.env.spies["trial_save"].assert_not_called()
                rc.env.spies["write_offline_boots"].assert_not_called()
                rc.env.spies["activate_offline_slots"].assert_not_called()
                self._assert_sources_untouched(rc.env)

    def test_recovery_execution_failure_matrix(self) -> None:
        cases = ("writer_restored", "writer_failed", "forged_writer", "cas",
                 "activation_session")
        for case in cases:
            with self.subTest(case=case), self._recovery_ready_case() as (
                    rc, ack, recovery, initial_status):
                def forged(trust, candidates, progress):
                    report = self._writer_report(rc.env, trust, candidates, progress)
                    report.update(physical=dict(rc.physical), runtime="recovery",
                                  recoverySessionId="2" * 32,
                                  oldDeployment=rc.session_identity["installedDeployment"],
                                  temporaryBootSha256=rc.temp_sha)
                    return report

                def failing(trust, candidates, progress):
                    error = offline_boot_writer.BootWriteError(
                        "write failed",
                        restore_status="restored" if case == "writer_restored" else "failed")
                    error.slot_results = {"a": "failed"}
                    raise error

                def wrong_session(report):
                    activated = self._activate(rc.env, report)
                    activated.update(runtime="recovery", recoverySessionId="2" * 32,
                                     oldDeployment=rc.session_identity["installedDeployment"])
                    return activated

                with self._recovery_execution(
                        rc,
                        writer=(forged if case == "forged_writer"
                                else failing if case.startswith("writer_") else None),
                        activator=(wrong_session if case == "activation_session" else None)) as run:
                    if case == "cas":
                        run.mocks.load.side_effect = None
                        run.mocks.load.return_value = SimpleNamespace(
                            layout=rc.env.journal.layout, header=b"bad",
                            region=b"bad", record=None)
                    with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                        self._run_recovery(rc, ack)
                    self.assertEqual(run.contexts, [])
                result = raised.exception.result
                if case == "writer_restored":
                    self.assertEqual(
                        (result["state"], result["bootWriteState"]),
                        ("failed", "restored"))
                    self.assertEqual(result["slotResults"], {"a": "failed"})
                    rc.env.spies["trial_save"].assert_not_called()
                    self.assertEqual(len(run.exits), 1)
                    self.assertIsInstance(run.exits[0],
                                          offline_boot_writer.BootWriteError)
                elif case == "writer_failed":
                    self.assertEqual(
                        (result["state"], result["bootWriteState"]),
                        ("recovery_required", "uncertain"))
                elif case == "forged_writer":
                    self.assertEqual(result["state"], "recovery_required")
                    self.assertEqual(result["bootWriteState"], "uncertain")
                    self.assertIn("recovery offline boot write report binding mismatch",
                                  result["error"])
                elif case == "cas":
                    self.assertEqual(result["state"], "recovery_required")
                    self.assertEqual(result["bootWriteState"], "written")
                    self.assertIn("compare-and-swap", result["error"])
                    rc.env.spies["trial_save"].assert_not_called()
                    run.activate.assert_not_called()
                else:
                    self.assertEqual(result["state"], "recovery_required")
                    self.assertEqual(result["bootWriteState"], "written")
                    self.assertIn("recovery slot activation report binding mismatch",
                                  result["error"])
                self.assertFalse(result["requiresReboot"])
                self.assertNotEqual(result.get("activationOutcome"), "activated")

    def test_recovery_execution_system_transaction_error_closes_context(self) -> None:
        with self._recovery_ready_case() as (rc, ack, recovery, initial_status):
            error = system_transaction.SystemTransactionError(
                "writer failed", {"state": "failed"})

            def failing(trust, candidates, progress):
                raise error

            with self._recovery_execution(rc, writer=failing) as run:
                with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                    self._run_recovery(rc, ack)
                self.assertEqual(run.contexts, [])
            self.assertIs(raised.exception, error)
            self.assertEqual(run.exits, [error])

    def test_recovery_execution_close_failure_semantics(self) -> None:
        with self._recovery_ready_case() as (rc, ack, recovery, initial_status):
            with self._recovery_execution(rc, exit_failure=RuntimeError("close failed")) as run:
                with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                    self._run_recovery(rc, ack)
                self.assertEqual(run.contexts, [])
            self.assertEqual(run.exits, [None])
            result = raised.exception.result
            self.assertEqual(result["state"], "recovery_required")
            self.assertEqual(result["bootWriteState"], "written")
            self.assertFalse(result["requiresReboot"])
            self.assertIn("close failed", result["error"])

        with self._recovery_ready_case() as (rc, ack, recovery, initial_status):
            def failing(trust, candidates, progress):
                error = offline_boot_writer.BootWriteError(
                    "write failed", restore_status="restored")
                error.slot_results = {"a": "failed"}
                raise error

            with self._recovery_execution(rc, exit_failure=RuntimeError("close failed"),
                                          writer=failing) as run:
                with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                    self._run_recovery(rc, ack)
                self.assertEqual(run.contexts, [])
            self.assertEqual(len(run.exits), 1)
            self.assertIsInstance(run.exits[0], offline_boot_writer.BootWriteError)
            result = raised.exception.result
            self.assertIn("write failed", str(raised.exception))
            self.assertEqual(
                (result["state"], result["bootWriteState"]), ("failed", "restored"))
            self.assertEqual(result["cleanupError"], "close failed")


if __name__ == "__main__":
    unittest.main()
