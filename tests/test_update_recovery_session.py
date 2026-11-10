"""Focused offline tests for schema 2 recovery session identity binding.

These tests exercise the real recovery_authorization._session parser and the
real resolve_session_recovery_boot_sources resolver with synthetic fixtures.
No production module is modified; only inspect_recovery_slots (a hardware
touch) is replaced with a deterministic stand-in.
"""

import copy
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                      / "system_files/usr/lib/armada"))

from updates import boot_trust, recovery_authorization, recovery_boot_sources  # noqa: E402
from updates.recovery_boot_sources import (  # noqa: E402
    RecoveryBootSourcesError,
    resolve_session_recovery_boot_sources,
)

RECOVERY_PART_UUID = "aaaa3210-1234-4abc-8def-000000003210"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class RecoverySessionIdentityTests(unittest.TestCase):

    def setUp(self):
        # Reuse the _build fixture from the sources test module without
        # exposing its TestCase globally or collecting it twice.
        path = Path(__file__).with_name("test_update_recovery_boot_sources.py")
        spec = importlib.util.spec_from_file_location("_rbs_session_fixture",
                                                     path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.fixture = module.RecoveryBootSourcesTests("_build")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base

    def _case(self, actual):
        snapshot, originals, expected, temporary = self.fixture._build(actual)
        physical = snapshot["physical"]
        physical["recoveryPartUuid"] = RECOVERY_PART_UUID
        identity = {
            "schemaVersion": 2,
            "sessionId": "ab" * 16,
            "model": "TB322FC",
            "recoveryPartUuid": RECOVERY_PART_UUID,
            "installedDeployment": "armada/" + ("a" * 64) + ".7",
            "entrySlot": actual,
            "originalBoots": {
                slot: {
                    "partitionUuid":
                        snapshot["partitions"][f"boot_{slot}"]["partitionUuid"],
                    "bytes": originals[slot]["bytes"],
                    "sha256": originals[slot]["sha256"],
                } for slot in ("a", "b")},
        }
        return {"snapshot": snapshot, "originals": originals,
                "expected": expected, "temporary": temporary,
                "physical": physical, "identity": identity,
                "out": self.base / f"out-{actual}"}

    def _resolve(self, state, identity=None):
        identity = copy.deepcopy(
            state["identity"] if identity is None else identity)
        snapshot = state["snapshot"]
        originals = state["originals"]
        before = copy.deepcopy((snapshot, originals, identity,
                                state["physical"]))
        files_before = sorted(
            str(p) for p in self.base.rglob("*"))
        with patch.object(recovery_boot_sources.physical_slots,
                          "inspect_recovery_slots",
                          side_effect=[copy.deepcopy(state["physical"]),
                                       copy.deepcopy(state["physical"])]) \
                as inspect_mock:
            result = resolve_session_recovery_boot_sources(
                identity, snapshot, originals,
                temporary_boot_sha256=state["temporary"])
        self.assertEqual(inspect_mock.call_count, 2)
        self.assertEqual((snapshot, originals, identity, state["physical"]),
                         before)
        self.assertEqual(sorted(str(p) for p in self.base.rglob("*")),
                         files_before)
        self.assertFalse(state["out"].exists())
        return result, identity

    def _reject(self, state, identity=None, pattern=None):
        identity = copy.deepcopy(
            state["identity"] if identity is None else identity)
        with patch.object(recovery_boot_sources.physical_slots,
                          "inspect_recovery_slots",
                          return_value=copy.deepcopy(state["physical"])):
            with self.assertRaisesRegex(RecoveryBootSourcesError,
                                        pattern or ".*"):
                resolve_session_recovery_boot_sources(
                    identity, state["snapshot"], state["originals"],
                    temporary_boot_sha256=state["temporary"])
        self.assertFalse(state["out"].exists())

    def _parse(self, identity):
        return recovery_authorization._session(
            json.dumps(identity).encode("utf-8"))

    def test_v1_identity_parser_accepted_but_resolver_rejects(self):
        state = self._case("a")
        identity = copy.deepcopy(state["identity"])
        v1 = {key: identity[key] for key in
              ("schemaVersion", "sessionId", "model", "recoveryPartUuid",
               "installedDeployment")}
        v1["schemaVersion"] = 1
        self.assertEqual(self._parse(v1), v1)
        self._reject(state, identity=v1,
                     pattern="identity schema 2")

    def test_happy_path_a_and_b(self):
        for actual in ("a", "b"):
            with self.subTest(actual=actual):
                state = self._case(actual)
                original_identity = copy.deepcopy(state["identity"])
                result, identity = self._resolve(state)
                inactive = "b" if actual == "a" else "a"
                self.assertEqual(result["phase"],
                                 "recovery_boot_sources_resolved")
                self.assertEqual(result["schemaVersion"], 1)
                self.assertEqual(result["recoverySessionId"],
                                 identity["sessionId"])
                self.assertEqual(identity, original_identity)
                for slot in ("a", "b"):
                    self.assertEqual(result["originalBoots"][slot]["sha256"],
                                     original_identity["originalBoots"][slot]
                                     ["sha256"])
                self.assertEqual(result["liveBoots"][actual]["sha256"],
                                 state["temporary"])
                self.assertEqual(result["liveBoots"][inactive]["sha256"],
                                 original_identity["originalBoots"][inactive]
                                 ["sha256"])
                self.assertNotEqual(result["liveBoots"][actual]["sha256"],
                                    result["originalBoots"][actual]["sha256"])
                # The retained session id is the original pinned value and is
                # distinct from every hash/uuid echoed in the report.
                echoed = {result["liveBoots"][s]["sha256"] for s in ("a", "b")}
                echoed |= {result["originalBoots"][s]["sha256"]
                           for s in ("a", "b")}
                self.assertNotIn(result["recoverySessionId"], echoed)

    def test_identity_mismatch_failures(self):
        state = self._case("a")
        cases = {
            "model": lambda i: i.update(model="TB710FU"),
            "recovery_uuid": lambda i: i.update(
                recoveryPartUuid="bbbb3210-1234-4abc-8def-000000003210"),
            "entry_slot": lambda i: i.update(entrySlot="b"),
            "boot_uuid": lambda i: i["originalBoots"]["a"].update(
                partitionUuid="cccc3210-1234-4abc-8def-000000003210"),
            "boot_bytes": lambda i: i["originalBoots"]["a"].update(
                bytes=i["originalBoots"]["a"]["bytes"] + 1),
            "boot_digest": lambda i: i["originalBoots"]["a"].update(
                sha256="0" * 64),
        }
        patterns = {
            "model": "model does not match",
            "recovery_uuid": "recovery partition UUID does not match",
            "entry_slot": "actualSlot does not match",
            "boot_uuid": "boot partition UUID for slot a",
            "boot_bytes": "bytes do not match the pinned identity",
            "boot_digest": "original boot a hash does not equal",
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                identity = copy.deepcopy(state["identity"])
                mutate(identity)
                self._reject(state, identity=identity,
                             pattern=patterns[name])

    def test_parser_rejections(self):
        state = self._case("a")
        base = state["identity"]

        def variant(**changes):
            identity = copy.deepcopy(base)
            for key, value in changes.items():
                if value is None:
                    identity.pop(key, None)
                else:
                    identity[key] = value
            return identity

        def boot_variant(slot, **changes):
            identity = copy.deepcopy(base)
            for key, value in changes.items():
                if value is None:
                    identity["originalBoots"][slot].pop(key, None)
                else:
                    identity["originalBoots"][slot][key] = value
            return identity

        cases = [
            ("missing 'a' boot slot",
             variant(originalBoots={"b": base["originalBoots"]["b"]})),
            ("missing 'b' boot slot",
             variant(originalBoots={"a": base["originalBoots"]["a"]})),
            ("extra 'c' boot slot",
             variant(originalBoots=dict(base["originalBoots"],
                                        c=base["originalBoots"]["a"]))),
            ("missing root key", variant(sessionId=None)),
            ("extra root key", {**copy.deepcopy(base), "extra": 1}),
            ("missing originalBoot key",
             boot_variant("a", partitionUuid=None)),
            ("extra originalBoot key",
             {**copy.deepcopy(base), "originalBoots": {
                 "a": {**base["originalBoots"]["a"], "extra": 1},
                 "b": base["originalBoots"]["b"]}}),
            ("bool schema", variant(schemaVersion=True)),
            ("float schema", variant(schemaVersion=2.0)),
            ("bool bytes", boot_variant("a", bytes=True)),
            ("float bytes", boot_variant("a", bytes=4096.0)),
            ("zero bytes", boot_variant("a", bytes=0)),
            ("oversized bytes", boot_variant("a", bytes=256 * 1024 * 1024 + 1)),
            ("missing originalBoot bytes key",
             boot_variant("a", bytes=None)),
            ("missing originalBoot sha key",
             boot_variant("a", sha256=None)),
            ("zero recovery uuid", variant(recoveryPartUuid=ZERO_UUID)),
            ("uppercase recovery uuid",
             variant(recoveryPartUuid=RECOVERY_PART_UUID.upper())),
            ("zero boot uuid", boot_variant("a", partitionUuid=ZERO_UUID)),
            ("uppercase boot uuid", boot_variant(
                "a", partitionUuid=base["originalBoots"]["a"]
                ["partitionUuid"].upper())),
            ("duplicate boot uuids", variant(originalBoots={
                "a": copy.deepcopy(base["originalBoots"]["a"]),
                "b": {**copy.deepcopy(base["originalBoots"]["b"]),
                      "partitionUuid": base["originalBoots"]["a"]
                      ["partitionUuid"]}})),
            ("uppercase sha", boot_variant(
                "a", sha256=base["originalBoots"]["a"]["sha256"].upper())),
            ("entry invalid", variant(entrySlot="c")),
            ("serial out of range",
             variant(installedDeployment="armada/" + ("a" * 64)
                     + ".4294967296")),
        ]
        for name, identity in cases:
            with self.subTest(case=name):
                with self.assertRaises(recovery_authorization.
                                       RecoveryAuthorizationError):
                    self._parse(identity)
        # The untouched identity itself parses.
        self.assertEqual(self._parse(copy.deepcopy(base)), base)

    def test_no_output_writes_and_no_output_parameters(self):
        state = self._case("a")
        result, _ = self._resolve(state)
        self.assertNotIn("output_dir",
                         inspect.signature(
                             resolve_session_recovery_boot_sources).parameters)
        self.assertFalse(state["out"].exists())
        self.assertIn("recoverySessionId", result)

    def test_legacy_raw_expected_hash_api_cannot_capture_trust(self):
        state = self._case("a")
        out = self.base / "legacy-out"
        with patch.object(boot_trust, "capture_recovery_boot_sources",
                          side_effect=AssertionError(
                              "permissive bypass attempted")):
            with self.assertRaises(TypeError):
                boot_trust.capture_recovery_boot_trust(
                    output_dir=out,
                    boot_tool_path=self.base / "tool",
                    original_boots=state["originals"],
                    expected_original_sha256=copy.deepcopy(
                        state["expected"]),
                    temporary_boot_sha256=state["temporary"])
        self.assertFalse(out.exists())

    def test_interop_with_real_csharp_identity_if_available(self):
        path = os.environ.get("ARMADA_SESSION_IDENTITY_FILE")
        if not path:
            self.skipTest("ARMADA_SESSION_IDENTITY_FILE not set; "
                          "no artifact synthesized")
        artifact = Path(path)
        if not artifact.is_file():
            self.fail(f"no C# identity artifact at {path}; "
                      "no artifact synthesized")
        identity = json.loads(artifact.read_bytes().decode("utf-8"))
        parsed = recovery_authorization._session(
            json.dumps(identity, sort_keys=True).encode("utf-8"))
        self.assertEqual(parsed["schemaVersion"], 2)
        for key in ("entrySlot", "originalBoots", "recoveryPartUuid",
                    "model", "sessionId", "installedDeployment"):
            self.assertIn(key, parsed)
        for slot in ("a", "b"):
            self.assertEqual(set(parsed["originalBoots"][slot]),
                             {"partitionUuid", "bytes", "sha256"})


if __name__ == "__main__":
    unittest.main()
