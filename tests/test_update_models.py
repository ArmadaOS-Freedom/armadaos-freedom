"""Contract tests for the unified model capability table (issue 66).

These tests exercise the exported registry itself, the updater policy tables
that must stay derived from it, and the real device-tree matchers that consume
it.  They deliberately avoid source-text matching: behaviour is checked through
the production call sites.
"""

import dataclasses
import json
import struct
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ARMADA_LIB = ROOT / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import (
    boot_payload,
    models,
    package_contract,
    physical_slots,
    recovery_device,
    system_manager,
)
from updates.recovery_layout import RecoveryLayoutError

MODEL_NAMES = ("TB322FC", "TB323FU", "TB710FU")
FIELD_NAMES = (
    "compatible",
    "soc_compatible",
    "board_id",
    "offline_upgrade",
    "mirrored_boot",
    "gbl_envelope",
)
EXPECTED_CAPABILITIES = {
    "TB322FC": (
        (b"lenovo,elden",),
        (b"qcom,sm8750",),
        1,
        True,
        True,
        False,
    ),
    "TB323FU": (
        (b"lenovo,baldur", b"lenovo,tb323fu"),
        (b"qcom,sm8850", b"qcom,kaanapali"),
        2,
        True,
        True,
        True,
    ),
    "TB710FU": (
        (b"lenovo,tb710fu",),
        (b"qcom,sm8650",),
        None,
        False,
        False,
        False,
    ),
}
EXPECTED_PROFILES = {
    (b"lenovo,elden", (b"qcom,sm8750",), 1),
    (b"lenovo,baldur", (b"qcom,sm8850", b"qcom,kaanapali"), 2),
    (b"lenovo,tb323fu", (b"qcom,sm8850", b"qcom,kaanapali"), 2),
}


class ModelCapabilityTableTests(unittest.TestCase):
    def test_capability_record_is_a_frozen_dataclass_with_exact_fields(self):
        self.assertTrue(dataclasses.is_dataclass(models.ModelCapabilities))
        self.assertTrue(models.ModelCapabilities.__dataclass_params__.frozen)
        self.assertEqual(
            {field.name for field in dataclasses.fields(models.ModelCapabilities)},
            set(FIELD_NAMES),
        )
        capability = models.MODELS["TB322FC"]
        self.assertIs(type(capability), models.ModelCapabilities)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capability.gbl_envelope = True

    def test_registered_models_are_exactly_the_three_boards(self):
        self.assertEqual(len(models.MODELS), len(MODEL_NAMES))
        self.assertEqual(set(models.MODELS), set(MODEL_NAMES))

    def test_capability_boundaries_and_value_types(self):
        for name in MODEL_NAMES:
            with self.subTest(model=name):
                expected = EXPECTED_CAPABILITIES[name]
                capability = models.MODELS[name]
                self.assertIs(type(capability), models.ModelCapabilities)
                self.assertEqual(capability.compatible, expected[0])
                self.assertEqual(capability.soc_compatible, expected[1])
                self.assertEqual(capability.board_id, expected[2])
                self.assertEqual(capability.offline_upgrade, expected[3])
                self.assertEqual(capability.mirrored_boot, expected[4])
                self.assertEqual(capability.gbl_envelope, expected[5])

                self.assertIs(type(capability.compatible), tuple)
                self.assertTrue(capability.compatible)
                self.assertIs(type(capability.soc_compatible), tuple)
                self.assertTrue(capability.soc_compatible)
                for token in capability.compatible:
                    self.assertIs(type(token), bytes)
                    self.assertTrue(token.startswith(b"lenovo,"))
                for token in capability.soc_compatible:
                    self.assertIs(type(token), bytes)
                    self.assertTrue(token.startswith(b"qcom,"))
                for flag in ("offline_upgrade", "mirrored_boot", "gbl_envelope"):
                    self.assertIs(type(getattr(capability, flag)), bool)
                if capability.board_id is not None:
                    self.assertIs(type(capability.board_id), int)

    def test_every_compatible_alias_is_globally_unique(self):
        owners = {}
        for name, capability in models.MODELS.items():
            for token in capability.compatible + capability.soc_compatible:
                with self.subTest(token=token):
                    self.assertNotIn(token, owners)
                    owners[token] = name
        self.assertEqual(len(owners), 8)

    def test_derived_sets_are_exact_and_follow_the_flags(self):
        self.assertIsInstance(models.AUTHORIZED_MODELS, frozenset)
        self.assertIsInstance(models.OFFLINE_UPGRADE_MODELS, frozenset)
        self.assertIsInstance(models.MIRROR_POLICY_MODELS, frozenset)
        self.assertIsInstance(models.GBL_MODELS, frozenset)
        self.assertEqual(models.AUTHORIZED_MODELS, frozenset(MODEL_NAMES))
        self.assertEqual(
            models.OFFLINE_UPGRADE_MODELS, frozenset(("TB322FC", "TB323FU")))
        self.assertEqual(
            models.MIRROR_POLICY_MODELS, frozenset(("TB322FC", "TB323FU")))
        self.assertEqual(models.GBL_MODELS, frozenset(("TB323FU",)))
        for name, capability in models.MODELS.items():
            with self.subTest(model=name):
                self.assertEqual(
                    capability.offline_upgrade,
                    name in models.OFFLINE_UPGRADE_MODELS)
                self.assertEqual(
                    capability.mirrored_boot,
                    name in models.MIRROR_POLICY_MODELS)
                self.assertEqual(
                    capability.gbl_envelope, name in models.GBL_MODELS)
        self.assertEqual(
            models.GBL_MODELS,
            frozenset(
                name for name, capability in models.MODELS.items()
                if capability.gbl_envelope),
        )

    def test_compatible_and_board_maps_are_exact(self):
        expected_aliases = {}
        for name, capability in models.MODELS.items():
            for alias in capability.compatible:
                expected_aliases[alias] = name
        self.assertIsInstance(models.COMPATIBLE_MODELS, Mapping)
        self.assertEqual(dict(models.COMPATIBLE_MODELS), expected_aliases)

        expected_boards = {}
        for name, capability in models.MODELS.items():
            if capability.board_id is not None:
                expected_boards[capability.board_id] = name
        self.assertIsInstance(models.BOARD_MODELS, Mapping)
        self.assertEqual(dict(models.BOARD_MODELS), expected_boards)

    def test_recovery_profiles_cover_exactly_the_board_models(self):
        self.assertIs(type(models.RECOVERY_PROFILES), tuple)
        self.assertEqual(len(models.RECOVERY_PROFILES), len(EXPECTED_PROFILES))
        for entry in models.RECOVERY_PROFILES:
            with self.subTest(entry=entry):
                self.assertIs(type(entry), tuple)
                self.assertEqual(len(entry), 3)
                alias, socs, board_id = entry
                self.assertIs(type(alias), bytes)
                self.assertTrue(alias.startswith(b"lenovo,"))
                self.assertIs(type(socs), tuple)
                self.assertTrue(socs)
                for soc in socs:
                    self.assertIs(type(soc), bytes)
                    self.assertTrue(soc.startswith(b"qcom,"))
                self.assertIs(type(board_id), int)
        self.assertEqual(set(models.RECOVERY_PROFILES), EXPECTED_PROFILES)

        by_alias = {
            alias: name
            for name, capability in models.MODELS.items()
            for alias in capability.compatible
        }
        for alias, socs, board_id in models.RECOVERY_PROFILES:
            capability = models.MODELS[by_alias[alias]]
            with self.subTest(alias=alias):
                self.assertEqual((socs, board_id),
                                 (capability.soc_compatible, capability.board_id))

    def test_tb710fu_remains_install_only_without_recovery_or_gbl(self):
        capability = models.MODELS["TB710FU"]
        self.assertIsNone(capability.board_id)
        self.assertFalse(capability.offline_upgrade)
        self.assertFalse(capability.mirrored_boot)
        self.assertFalse(capability.gbl_envelope)
        self.assertNotIn("TB710FU", models.OFFLINE_UPGRADE_MODELS)
        self.assertNotIn("TB710FU", models.MIRROR_POLICY_MODELS)
        self.assertNotIn("TB710FU", models.GBL_MODELS)
        aliases = {entry[0] for entry in models.RECOVERY_PROFILES}
        self.assertFalse(aliases & set(capability.compatible))


class UpdaterPolicyImportTests(unittest.TestCase):
    """Public updater tables must stay aliases of the registry."""

    def test_system_manager_alias_table_matches(self):
        self.assertEqual(dict(system_manager.MODELS),
                         dict(models.COMPATIBLE_MODELS))

    def test_package_contract_model_sets_match(self):
        self.assertEqual(package_contract.AUTHORIZED_MODELS,
                         models.AUTHORIZED_MODELS)
        self.assertEqual(package_contract.OFFLINE_UPGRADE_MODELS,
                         models.OFFLINE_UPGRADE_MODELS)

    def test_boot_payload_mirror_policy_matches(self):
        self.assertEqual(boot_payload.MIRROR_POLICY_MODELS,
                         models.MIRROR_POLICY_MODELS)
        for name, capability in models.MODELS.items():
            with self.subTest(model=name):
                self.assertEqual(
                    boot_payload.runtime_marker_required(name),
                    capability.mirrored_boot)

    def test_physical_slot_board_map_matches(self):
        self.assertEqual(dict(physical_slots._MODEL),
                         dict(models.BOARD_MODELS))

    def test_system_manager_resolves_every_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            compatible = Path(directory) / "compatible"
            for alias, expected in models.COMPATIBLE_MODELS.items():
                with self.subTest(alias=alias):
                    compatible.write_bytes(alias + b"\0")
                    self.assertEqual(system_manager._model(compatible), expected)

    def test_system_manager_treats_both_tb323fu_aliases_as_one_model(self):
        with tempfile.TemporaryDirectory() as directory:
            compatible = Path(directory) / "compatible"
            compatible.write_bytes(
                b"lenovo,baldur\0lenovo,tb323fu\0qcom,sm8850\0")
            self.assertEqual(system_manager._model(compatible), "TB323FU")


class EdlProfileAliasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            (ROOT / "build_files" / "edl-devices.json").read_text(
                encoding="utf-8"))

    def test_all_registry_models_have_a_profile(self):
        self.assertEqual(set(self.config["profiles"]), set(models.MODELS))

    def test_profile_identity_and_alias_sets_are_consistent(self):
        for name, profile in self.config["profiles"].items():
            capability = models.MODELS[name]
            accepted = set(capability.compatible) | set(capability.soc_compatible)
            required = {token.encode("ascii") for token in profile["required"]}
            forbidden = {token.encode("ascii") for token in profile["forbidden"]}
            device = profile["compatible"]["device"]
            soc = profile["compatible"]["soc"].lower()
            with self.subTest(model=name):
                self.assertIn(f"lenovo,{device}".encode("ascii"),
                              capability.compatible)
                self.assertIn(f"qcom,{soc}".encode("ascii"),
                              capability.soc_compatible)
                self.assertLessEqual(required, accepted)
                self.assertFalse(forbidden & accepted)


PART_UUID = "12345678-1234-5678-9abc-def012345678"


class RecoveryAliasResolutionTests(unittest.TestCase):
    """Run the real locator; only device-tree/sysfs/subprocess reads are faked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.dt = self.base / "dt"
        self.chosen = self.dt / "chosen"
        self.chosen.mkdir(parents=True)
        self._write_settings(1)

        self.dev = self.base / "dev_sda19"
        self.dev.write_bytes(b"")
        self.sys_block = self.base / "sys" / "dev" / "block"
        self.sys_block.mkdir(parents=True)
        node = self.base / "sys" / "devices" / "soc" / "1.ufs" / "sda" / "sda19"
        node.mkdir(parents=True)
        (node.parent / "queue").mkdir(parents=True)
        (node / "partition").write_text("19\n")
        (node / "size").write_text("4194304\n")
        (node.parent / "queue" / "logical_block_size").write_text("4096\n")
        (self.sys_block / "0:0").symlink_to(node)

    def _write_settings(self, board):
        values = [0] * 16
        values[0] = 1
        values[1] = board
        values[5] = 1
        values[13] = 1
        (self.chosen / "kshim,settings-v1").write_bytes(
            struct.pack(">16I", *values))

    def _write_compatible(self, *tokens):
        (self.dt / "compatible").write_bytes(b"\0".join(tokens) + b"\0")

    def _path_router(self, raw):
        text = str(raw)
        if text == "/proc/device-tree":
            return self.dt
        if text.startswith("/sys/dev/block/"):
            return self.sys_block / text[len("/sys/dev/block/"):]
        return Path(raw)

    def _locate(self):
        with (
            mock.patch.object(recovery_device, "Path",
                              side_effect=self._path_router),
            mock.patch.object(recovery_device.stat, "S_ISBLK",
                              return_value=True),
            mock.patch.object(
                recovery_device.subprocess, "run",
                return_value=SimpleNamespace(stdout=f"{self.dev}\n")),
            mock.patch.object(
                recovery_device.subprocess, "check_output",
                return_value=f"{PART_UUID}\n"),
        ):
            return recovery_device.locate_recovery_partition(require_edl=False)

    def test_registered_recovery_profiles_resolve_to_their_board(self):
        for alias, socs, board in models.RECOVERY_PROFILES:
            for soc in socs:
                with self.subTest(alias=alias, soc=soc):
                    self._write_compatible(alias, soc)
                    self._write_settings(board)
                    self.assertEqual(
                        self._locate(),
                        (self.dev.resolve(), PART_UUID, board, 0),
                    )

    def test_dual_aliases_for_the_same_board_are_accepted_once(self):
        self._write_compatible(
            b"lenovo,tb323fu", b"qcom,kaanapali",
            b"lenovo,baldur", b"qcom,sm8850")
        self._write_settings(2)
        self.assertEqual(
            self._locate(), (self.dev.resolve(), PART_UUID, 2, 0))

    def test_mismatched_soc_is_rejected(self):
        cases = {
            "tb323fu with sm8750": (b"lenovo,tb323fu", b"qcom,sm8750"),
            "elden with sm8850": (b"lenovo,elden", b"qcom,sm8850"),
            "soc without a known alias": (b"qcom,sm8850",),
        }
        for name, tokens in cases.items():
            with self.subTest(case=name):
                self._write_compatible(*tokens)
                with self.assertRaises(RecoveryLayoutError):
                    self._locate()

    def test_dual_alias_with_a_mismatched_soc_is_rejected(self):
        self._write_compatible(
            b"lenovo,baldur", b"lenovo,tb323fu", b"qcom,sm8750")
        with self.assertRaises(RecoveryLayoutError):
            self._locate()

    def test_two_different_boards_are_rejected_as_ambiguous(self):
        self._write_compatible(
            b"lenovo,elden", b"qcom,sm8750",
            b"lenovo,baldur", b"qcom,sm8850")
        with self.assertRaises(RecoveryLayoutError):
            self._locate()

    def test_unregistered_model_alias_is_rejected(self):
        self._write_compatible(b"lenovo,tb710fu", b"qcom,sm8650")
        with self.assertRaises(RecoveryLayoutError):
            self._locate()


if __name__ == "__main__":
    unittest.main()
