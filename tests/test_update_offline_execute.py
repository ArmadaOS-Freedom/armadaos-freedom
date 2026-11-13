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

        # Every journal callback must read/write through the descriptor that
        # open_recovery returned, not a path or a stale descriptor.
        def load(fd, partuuid, board):
            self.assertEqual(fd, 73)
            self.assertEqual(partuuid, env.physical["recoveryPartUuid"])
            self.assertEqual(board, env.journal.layout.board)
            return SimpleNamespace(layout=env.journal.layout, header=env.journal.header,
                                   region=env.journal.region, record=env.journal.record)

        def save(fd, journal, record):
            self.assertEqual(fd, 73)
            env.journal.record = record

        env.spies["trial_save"].side_effect = save
        with ExitStack() as stack:
            mocks = SimpleNamespace(
                options=stack.enter_context(mock.patch.object(
                    offline_execute.deployment_options, "read_deployment_options",
                    return_value=options)),
                opened=stack.enter_context(mock.patch.object(
                    system_transaction.recovery_device, "open_recovery", open_recovery)),
                load=stack.enter_context(mock.patch.object(
                    system_transaction.trial_store, "load", side_effect=load)),
            )
            yield mocks

    @staticmethod
    def _commit_failure(exc):
        if exc.commit_state == "not_committed":
            error = offline_boot_writer.BootWriteError(
                "%s; started boot slots restored" % exc, "restored")
        else:
            error = offline_boot_writer.BootWriteError(
                "%s; verified non-running boot image preserved" % exc, "not_restored")
        error.commit_state = exc.commit_state
        return error

    def _writer_report(self, env, trust, candidates, progress, *, commit):
        plan = self._json(env.work, "plan.json")
        order = [plan["targetSlot"], plan["fromSlot"]]
        writes = {}
        for index, slot in enumerate(order):
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
            if index == 0:
                try:
                    commit()
                except offline_boot_writer.JournalCommitError as exc:
                    raise self._commit_failure(exc) from exc
        return {"phase": "dual_boots_written", "model": plan["model"],
                "physical": dict(env.physical), "transactionUuid": plan["transactionId"],
                "deployment": plan["newDeployment"], "fromSlot": plan["fromSlot"],
                "slotOrder": order, "writes": writes}

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
            lambda trust, candidates, *, commit, progress: self._writer_report(
                env, trust, candidates, progress, commit=commit))

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
                    self.assertEqual(result["bootPolicy"], "dual-boot-no-slot-control")
                    self.assertEqual(result["slotControlState"], "not_issued")
                    self.assertNotIn("activationOutcome", result)
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
                    self.assertFalse((env.work / "activation-report.json").exists())
                    env.spies["activate_offline_slots"].assert_not_called()
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
        cases = (("not_needed", "not_committed", "failed", "not_started"),
                 ("restored", "not_committed", "failed", "restored"),
                 ("not_restored", "committed", "recovery_required", "uncertain"),
                 ("failed", "not_committed", "recovery_required", "uncertain"),
                 ("oserror", None, "recovery_required", "uncertain"))
        for restore, commit_state, state, write_state in cases:
            with self.subTest(restore=restore, commit=commit_state), self.fixture._case() as env:
                self._ready(env)
                ack = self._ack(env)
                if restore == "oserror":
                    env.spies["write_offline_boots"].side_effect = OSError("device I/O")
                else:
                    error = offline_boot_writer.BootWriteError("write failed", restore_status=restore)
                    error.commit_state = commit_state
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
                if restore == "not_restored":
                    self.assertEqual(result["journalCommitState"], commit_state)
                env.spies["trial_save"].assert_not_called()
                env.spies["activate_offline_slots"].assert_not_called()

    def test_running_write_failure_after_commit_preserves_alternate(self) -> None:
        with self.fixture._case() as env:
            self._ready(env)
            ack = self._ack(env)
            plan = self._json(env.work, "plan.json")

            def writer(trust, candidates, *, commit, progress):
                progress({"phase": "writing", "slot": plan["targetSlot"]})
                progress({"phase": "verified", "slot": plan["targetSlot"]})
                commit()
                error = offline_boot_writer.BootWriteError(
                    "running write failed; journal commit is committed; "
                    "verified non-running boot image preserved", "not_restored")
                error.commit_state = "committed"
                error.slot_results = {
                    plan["targetSlot"]: {"status": "preserved"},
                    plan["fromSlot"]: {"status": "writing"},
                }
                raise error

            env.spies["write_offline_boots"].side_effect = writer
            with self._execution(env):
                with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                    offline_execute.execute_offline_update(
                        work_dir=env.work, recovery_ack=ack,
                        trusted_key=self.fixture.pub_key)
            result = raised.exception.result
            self.assertEqual(result["state"], "recovery_required")
            # The journal commit crossed its boundary, but the writer never
            # returned a validated report, so both slots are not proven and
            # the run may not advertise a restart.
            self.assertEqual(result["bootWriteState"], "uncertain")
            self.assertEqual(result["journalCommitState"], "committed")
            self.assertEqual(result["slotResults"], {
                plan["targetSlot"]: {"status": "preserved"},
                plan["fromSlot"]: {"status": "writing"},
            })
            self.assertFalse(result["requiresReboot"])
            self.assertEqual(env.spies["trial_save"].call_count, 1)
            self.assertFalse((env.work / "write-report.json").exists())
            env.spies["activate_offline_slots"].assert_not_called()

    def test_post_write_failures_are_recovery_required(self) -> None:
        expected = {
            "malformed_report": ("recovery_required", "uncertain"),
            "report_save": ("recovery_required", "written"),
            "journal_compare": ("recovery_required", "uncertain"),
            "journal_ambiguous": ("recovery_required", "uncertain"),
            "journal_save": ("failed", "restored"),
        }
        for case in expected:
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
                    elif case == "journal_ambiguous":
                        state = {"record": None, "written": False}

                        def ambiguous_save(fd, journal, record):
                            state["record"] = record
                            state["written"] = True
                            raise OSError("journal fsync failed")

                        def written_load(fd, partuuid, board):
                            return SimpleNamespace(
                                layout=env.journal.layout,
                                header=env.journal.header,
                                region=(b"new-region" if state["written"]
                                        else env.journal.region),
                                record=(state["record"] if state["written"]
                                        else env.journal.record))

                        env.spies["trial_save"].side_effect = ambiguous_save
                        mocks.load.side_effect = written_load
                    with mock.patch.object(offline_execute.system_transaction, "_save_json",
                                           side_effect=save_json):
                        with self.assertRaises(system_transaction.SystemTransactionError) as raised:
                            offline_execute.execute_offline_update(
                                work_dir=env.work, recovery_ack=ack,
                                trusted_key=self.fixture.pub_key)
                result = raised.exception.result
                self.assertEqual(
                    (result["state"], result["bootWriteState"]), expected[case])
                self.assertFalse(result["requiresReboot"])
                if case == "journal_compare":
                    self.assertEqual(result["journalCommitState"], "ambiguous")
                elif case == "journal_ambiguous":
                    self.assertEqual(result["journalCommitState"], "ambiguous")
                elif case == "report_save":
                    self.assertEqual(result["journalCommitState"], "committed")
                env.spies["activate_offline_slots"].assert_not_called()

    def test_final_status_save_failure_never_reports_success(self) -> None:
        with self.fixture._case() as env:
            self._ready(env)
            ack = self._ack(env)
            self._install_success_spies(env)
            real_save = system_transaction._save_json

            def save_json(directory, name, payload):
                if name == "status.json" and payload.get("state") == "awaiting_reboot":
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
            self.assertEqual(result["slotControlState"], "not_issued")
            self.assertIn("final status disk full", result["error"])
            self.assertNotIn("persistenceError", result)
            env.spies["activate_offline_slots"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
