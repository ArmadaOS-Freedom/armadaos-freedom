"""Focused recovery trust tests with synthetic CLI evidence, not real AVB."""

import copy
import errno
import hashlib
import importlib.util
import os
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files/usr/lib/armada"))

from updates import boot_trust, recovery_boot_sources  # noqa: E402


def _claim(path, data):
    path.write_bytes(data)
    return {"path": str(path), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


class RecoveryBootTrustTests(unittest.TestCase):

    def setUp(self):
        # Keep the reusable TestCase out of module globals and collection.
        path = Path(__file__).with_name("test_update_recovery_boot_sources.py")
        spec = importlib.util.spec_from_file_location("_rbs_fixture", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fixture = module.RecoveryBootSourcesTests("_build")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _case(self, model, actual):
        snapshot, originals, expected, temporary = self.fixture._build(actual)
        physical = snapshot["physical"]
        physical["model"] = model
        physical["recoveryPartUuid"] = "aaaa3210-1234-4abc-8def-000000003210"
        directory = Path(originals["a"]["path"]).parent
        for label in ("recovery_b", "vbmeta_system_a", "vbmeta_system_b"):
            snapshot["extraFiles"][label] = _claim(
                directory / f"{label}.img", f"extra-{label}".encode() * 20)
            snapshot["partitions"][label] = {
                "lun": 2, "firstLba": 4096 + len(snapshot["partitions"]) * 16,
                "blockCount": 4,
                "partitionUuid": (
                    f"{len(snapshot['partitions']):08x}-1234-4abc-8def-"
                    f"{len(snapshot['partitions']):012x}"),
            }
        if model == "TB322FC":
            del snapshot["extraFiles"]["efisp"]
            del snapshot["partitions"]["efisp"]
        tool = directory / "fake-boot-tool"
        tool.write_text("synthetic CLI fixture\n")
        tool.chmod(0o700)
        identity = {"schemaVersion": 2, "sessionId": "ab" * 16,
                    "model": model,
                    "recoveryPartUuid": physical["recoveryPartUuid"],
                    "installedDeployment": "armada/" + "a" * 64 + ".7",
                    "entrySlot": actual,
                    "originalBoots": {
                        slot: {"partitionUuid":
                               snapshot["partitions"][f"boot_{slot}"]["partitionUuid"],
                               "bytes": originals[slot]["bytes"],
                               "sha256": expected[slot]}
                        for slot in ("a", "b")}}
        return {"snapshot": snapshot, "originals": originals,
                "expected": expected, "temporary": temporary,
                "physical": physical, "model": model, "out": directory / "out",
                "tool": tool, "identity": identity}

    @staticmethod
    def _response(request, model):
        trust = {"model": model, "lockState": "Locked",
                 "mode": boot_trust._MODE_REPORTS[
                     "aosp-resign" if model == "TB322FC"
                     else "gbl-preserve-metadata"],
                 "gblRegion": None if model == "TB322FC" else "PRC",
                 "efispSha256": (
                     None if model == "TB322FC"
                     else request["efisp"]["sha256"])}
        for slot in ("a", "b"):
            hashes, lengths = {}, {}
            for cli_key, source_key in boot_trust._SOURCE_KEY_MAP.items():
                hashes[source_key] = request[slot][cli_key]["sha256"]
                lengths[source_key] = request[slot][cli_key]["length"]
            trust[slot] = {
                "slot": slot, "maxRawBytes": 4096,
                "rootPublicKeySha256": "1" * 64,
                "bootPublicKeySha256": "2" * 64,
                "metadataSha256": "3" * 64,
                "payloadDigestMatchesSignedMetadata": True,
                "ablRootPublicKeySha256": "4" * 64,
                "material": {"slot": slot, "isKshim": True, "pageSize": 4096,
                             "sourceSha256": hashes, "sourceLengths": lengths,
                             "loaderBudget": {
                                 "profile": "test", "maxRawBytes": 1048576,
                                 "knownLoaderMatch": True,
                                 "evidence": "synthetic", "formula": "test"}}}
        return {"trust": trust}

    def _run(self, state, *, recovery=True, originals=None, expected=None,
             temporary=None, identity=None, alter_response=None,
             stage_mutation=None, physical_drift=False, lock_drift=False):
        snapshot = state["snapshot"]
        physical = state["physical"]
        upload_bytes = ({
            slot: Path((originals if originals is not None else state["originals"])[slot]["path"]).read_bytes()
            for slot in ("a", "b")
        } if recovery else {})
        if not recovery:
            snapshot["phase"] = "boot_sources_captured"
            snapshot["physical"] = physical
        expected_labels = ["recovery_a", "recovery_b", "vbmeta_system_a",
                           "vbmeta_system_b"]
        if state["model"] == "TB323FU":
            expected_labels.append("efisp")

        def capture(_initial, _output, *, extra_labels):
            self.assertEqual(list(extra_labels), expected_labels)
            return snapshot

        def stage(_tool, command, request, *_args):
            self.assertEqual(command, "inspect-trust")
            state["request"] = request
            result = self._response(request, state["model"])
            if alter_response:
                alter_response(result)
            if stage_mutation:
                stage_mutation()
            return None, result

        second = copy.deepcopy(physical) if physical_drift else physical
        if physical_drift:
            second["model"] = "TB323FU" if physical["model"] == "TB322FC" else "TB322FC"
        inspections = [physical, second]
        with ExitStack() as stack:
            inspector = stack.enter_context(patch.object(
                boot_trust, "inspect_recovery_slots" if recovery
                else "inspect_physical_slots", side_effect=inspections))
            fresh = stack.enter_context(patch.object(
                recovery_boot_sources.physical_slots, "inspect_recovery_slots",
                return_value=physical))
            lock = stack.enter_context(patch.object(
                boot_trust, "read_boot_lock",
                side_effect=["locked", "unlocked" if lock_drift else "locked"]))
            stack.enter_context(patch.object(
                boot_trust,
                "capture_recovery_boot_sources" if recovery else "capture_boot_sources",
                side_effect=capture))
            cli = stack.enter_context(patch.object(boot_trust, "_run_stage",
                                                   side_effect=stage))
            state["stage"] = cli
            resolver = None
            if not recovery:
                resolver = stack.enter_context(patch.object(
                    boot_trust, "resolve_session_recovery_boot_sources"))
            kwargs = {"output_dir": state["out"], "boot_tool_path": state["tool"]}
            if recovery:
                session_identity = copy.deepcopy(
                    identity if identity is not None else state["identity"])
                if expected is not None:
                    for slot in ("a", "b"):
                        session_identity["originalBoots"][slot]["sha256"] = expected[slot]
                kwargs.update(
                    original_boots=(originals if originals is not None
                                    else state["originals"]),
                    session_identity=session_identity,
                    temporary_boot_sha256=(temporary if temporary is not None
                                            else state["temporary"]))
                report = boot_trust.capture_recovery_boot_trust(**kwargs)
            else:
                report = boot_trust.capture_boot_trust(**kwargs)
                resolver.assert_not_called()
        self.assertEqual(inspector.call_count, 2)
        self.assertEqual(lock.call_count, 2)
        if recovery:
            self.assertEqual(fresh.call_count, 2)
            for slot in ("a", "b"):
                staged_path = state["out"] / "originals" / f"boot_{slot}.img"
                expected_staged = dict(state["originals"][slot], path=str(staged_path))
                staged = report["sources"]["originalBoots"][slot]
                self.assertEqual(staged, expected_staged)
                self.assertEqual(report["sources"]["slots"][slot]["boot"], staged)
                request_boot = state["request"][slot]["boot"]
                self.assertEqual(request_boot["sha256"], state["expected"][slot])
                self.assertEqual(request_boot["path"], str(staged_path))
                self.assertEqual(request_boot["length"], staged["bytes"])
                self.assertNotEqual(request_boot["sha256"], state["temporary"])
                self.assertEqual(staged["bytes"], len(upload_bytes[slot]))
                self.assertEqual(staged_path.read_bytes(), upload_bytes[slot])
                self.assertEqual(hashlib.sha256(upload_bytes[slot]).hexdigest(), staged["sha256"])
                self.assertEqual(report["sources"]["liveBoots"][slot],
                                 snapshot["slots"][slot]["boot"])
            originals_dir = state["out"] / "originals"
            self.assertEqual(originals_dir.stat().st_mode & 0o777, 0o700)
            for path in originals_dir.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(report["phase"], "recovery_boot_trust_checked")
            self.assertEqual(set(report["sources"]["liveBoots"]), {"a", "b"})
            self.assertEqual(json.loads((state["out"] / "boot-trust.json").read_text()), report)
        else:
            self.assertEqual(report["phase"], "boot_trust_checked")
            self.assertEqual(report["sources"]["phase"], "boot_sources_captured")
        return report

    def _reject(self, state, **kwargs):
        with self.assertRaises(boot_trust.BootTrustError):
            self._run(state, **kwargs)
        self.assertFalse((state["out"] / "boot-trust.json").exists())

    def test_recovery_both_slots_and_models(self):
        for model in ("TB322FC", "TB323FU"):
            for actual in ("a", "b"):
                with self.subTest(model=model, actual=actual):
                    state = self._case(model, actual)
                    report = self._run(state)
                    self.assertEqual(report["physical"], state["physical"])
                    json.loads((state["out"] / "boot-trust.json").read_text())

    def test_resolver_rejects_wrong_expected_swapped_originals_and_temp(self):
        for kind in ("expected", "swapped", "temporary"):
            state = self._case("TB323FU", "a")
            kwargs = {}
            if kind == "expected":
                bad = dict(state["expected"], a="0" * 64)
                kwargs["expected"] = bad
            elif kind == "swapped":
                kwargs["originals"] = {"a": state["originals"]["b"],
                                       "b": state["originals"]["a"]}
            else:
                kwargs["temporary"] = "0" * 64
            self._reject(state, **kwargs)
            state["stage"].assert_not_called()

    def test_identity_schema_and_mismatch_rejected_before_cli(self):
        reference = self._case("TB323FU", "a")
        legacy = {key: value for key, value in reference["identity"].items()
                  if key not in ("originalBoots", "entrySlot")}
        legacy["schemaVersion"] = 1
        self.assertEqual(set(legacy),
                         {"schemaVersion", "sessionId", "model",
                          "recoveryPartUuid", "installedDeployment"})
        cases = {
            "v1 identity": legacy,
            "model mismatch": dict(reference["identity"], model="TB322FC"),
            "recoveryPartUuid mismatch": dict(
                reference["identity"],
                recoveryPartUuid="bbbb3210-1234-4abc-8def-000000003210"),
            "entrySlot mismatch": dict(reference["identity"], entrySlot="b"),
        }
        for label, identity in cases.items():
            with self.subTest(case=label):
                state = self._case("TB323FU", "a")
                with self.assertRaises(boot_trust.BootTrustError):
                    self._run(state, identity=identity)
                state["stage"].assert_not_called()
                self.assertFalse((state["out"] / "boot-trust.json").exists())

    def test_actual_trust_validator_rejects_altered_authenticated_hash(self):
        state = self._case("TB322FC", "b")

        def alter(result):
            result["trust"]["a"]["material"]["sourceSha256"]["current_boot"] = "0" * 64

        self._reject(state, alter_response=alter)

    def test_final_rehash_rejects_original_and_live_boot_changes(self):
        for target in ("original", "live"):
            state = self._case("TB323FU", "a")
            claim = (state["originals"]["a"] if target == "original"
                     else state["snapshot"]["slots"]["a"]["boot"])

            def mutate():
                path = Path(state["request"]["a"]["boot"]["path"]
                            if target == "original" else claim["path"])
                data = bytearray(path.read_bytes())
                data[0] ^= 1
                path.write_bytes(data)

            self._reject(state, stage_mutation=mutate)

    def test_staged_originals_survive_upload_removal_without_caller_mutation(self):
        for unlink in (False, True):
            with self.subTest(unlink=unlink):
                state = self._case("TB323FU", "a")
                callers = {key: copy.deepcopy(state[key])
                           for key in ("originals", "physical", "identity")}

                def remove_uploads():
                    for claim in state["originals"].values():
                        Path(claim["path"]).unlink()

                report = self._run(
                    state, stage_mutation=remove_uploads if unlink else None)
                self.assertEqual(report["phase"], "recovery_boot_trust_checked")
                for key, before in callers.items():
                    self.assertEqual(state[key], before)

    def test_copy_boundary_source_mutation_rejected_before_cli(self):
        state = self._case("TB323FU", "a")
        real_copy = boot_trust._copy_verified
        copied = []

        def mutating_copy(src_fd, out_path, expected_bytes):
            copied.append(out_path.name)
            path = Path(state["originals"]["a"]["path"])
            data = bytearray(path.read_bytes())
            data[0] ^= 1
            path.write_bytes(data)
            return real_copy(src_fd, out_path, expected_bytes)

        with patch.object(boot_trust, "_copy_verified", side_effect=mutating_copy):
            self._reject(state)
        self.assertEqual(copied, ["boot_a.img"])
        state["stage"].assert_not_called()

    def test_copy_io_error_closes_source_and_skips_cli(self):
        state = self._case("TB323FU", "a")
        source_fds = []

        def fail_copy(src_fd, _out_path, _expected_bytes):
            source_fds.append(src_fd)
            raise OSError("synthetic copy failure")

        with patch.object(boot_trust, "_copy_verified", side_effect=fail_copy):
            self._reject(state)
        self.assertEqual(len(source_fds), 1)
        state["stage"].assert_not_called()
        with self.assertRaises(OSError) as caught:
            os.fstat(source_fds[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_final_physical_or_lock_drift_rejects_report(self):
        for drift in ("physical_drift", "lock_drift"):
            self._reject(self._case("TB322FC", "a"), **{drift: True})

    def test_normal_capture_regression(self):
        state = self._case("TB322FC", "a")
        state["physical"] = {"schemaVersion": 1, "phase": "physical_slots_checked",
                             "model": "TB322FC"}
        report = self._run(state, recovery=False)
        self.assertEqual(report["physical"], state["physical"])


if __name__ == "__main__":
    unittest.main()
