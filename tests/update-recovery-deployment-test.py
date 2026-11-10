#!/usr/bin/python3
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "_deployment_store_fixture", _HERE / "update-deployment-store-test.py"
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)

from updates import recovery_deployment  # noqa: E402

ROOT_UUID = "11111111-1111-4111-8111-111111111111"
BOOT_UUID = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"
BASE_TOKENS = [
    f"root=UUID={ROOT_UUID}",
    f"boot=UUID={BOOT_UUID}",
    "rootflags=subvol=root",
    "console=tty0",
]


class RecoveryDeploymentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fixture.DeploymentStoreIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        _fixture.DeploymentStoreIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.f = _fixture.DeploymentStoreIntegrationTests(
            "test_two_updates_merges_retains_and_reuses"
        )
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        options = self._dep(self.f.initial_binding).get_bootconfig().get("options")
        ostree_token = next(
            tok for tok in options.split() if tok.startswith("ostree=")
        )
        self._set_kargs(
            self.f.initial_binding,
            BASE_TOKENS
            + [f"armada.deployment={self.f.initial_binding}", ostree_token],
        )

    def _dep(self, wanted: str):
        for dep in self.f._sysroot().get_deployments():
            if _fixture.binding(dep) == wanted:
                return dep
        raise AssertionError(f"no deployment with binding {wanted}")

    def _set_kargs(self, wanted: str, tokens: list[str]) -> None:
        sysroot = self.f._sysroot()
        matches = [
            dep for dep in sysroot.get_deployments()
            if _fixture.binding(dep) == wanted
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(sysroot.deployment_set_kargs(matches[0], tokens, None))

    def _read(self, **overrides):
        args = {
            "sysroot_path": self.f.root,
            "expected_binding": self.f.initial_binding,
            "model": self.f.model,
            "expected_root_uuid": ROOT_UUID,
            "expected_boot_uuid": BOOT_UUID,
        }
        args.update(overrides)
        return recovery_deployment.read_recovery_deployment(**args)

    def _snapshot(self) -> dict:
        snap = {"bindings": self.f._bindings(), "boot": {}, "trees": {}}
        boot = self.f.root / "boot"
        for path in sorted(boot.rglob("*")):
            rel = str(path.relative_to(boot))
            if path.is_symlink():
                snap["boot"]["L:" + rel] = os.readlink(path)
            elif path.is_file():
                snap["boot"]["F:" + rel] = path.read_bytes()
        for dep in self.f._sysroot().get_deployments():
            directory = Path(
                self.f._sysroot().get_deployment_directory(dep).get_path()
            )
            snap["trees"][_fixture.binding(dep)] = (
                _fixture.tree_digest(directory / "etc"),
                _fixture.tree_digest(directory / "usr"),
            )
        return snap

    def _assert_unchanged(self, snap: dict) -> None:
        self.assertEqual(self._snapshot(), snap)
        self.f._assert_user_data_unchanged()

    def _bls_path(self, wanted: str) -> Path:
        options = self._dep(wanted).get_bootconfig().get("options").strip()
        ostree_token = next(
            tok for tok in options.split() if tok.startswith("ostree=")
        )
        matches = []
        for path in sorted((self.f.root / "boot/loader/entries").glob("*.conf")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("options ") and ostree_token in line.split():
                    matches.append(path)
                    break
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_installed_current_read_without_booted_context_or_pinning(self) -> None:
        self.assertFalse(self._dep(self.f.initial_binding).is_pinned())
        before = self._snapshot()
        result = self._read()
        self._assert_unchanged(before)
        self.assertEqual(result, {
            "schemaVersion": 1,
            "phase": "recovery_deployment_checked",
            "model": self.f.model,
            "installedDeployment": self.f.initial_binding,
            "path": str(self.f.current_path),
            "options": self._dep(
                self.f.initial_binding
            ).get_bootconfig().get("options").strip(),
            "rootUuid": ROOT_UUID,
            "bootUuid": BOOT_UUID,
            "originRef": None,
            "kernelRelease": _fixture.payload_fixture.KVER,
        })
        for forbidden in ("booted", "deployment", "currentSlot", "actualSlot"):
            self.assertNotIn(forbidden, result)
        self.assertFalse(self._dep(self.f.initial_binding).is_pinned())

    def test_second_registered_deployment_is_selected_explicitly(self) -> None:
        target = self.f._call(1)
        self.assertEqual(self.f._bindings()[0], self.f.initial_binding)
        self.assertNotEqual(target["deployment"], self.f.initial_binding)
        before = self._snapshot()
        result = self._read(expected_binding=target["deployment"])
        self._assert_unchanged(before)
        self.assertEqual(result["installedDeployment"], target["deployment"])
        self.assertEqual(result["path"], target["path"])
        self.assertEqual(result["originRef"], _fixture.ORIGINS[1])
        self.assertEqual(result["kernelRelease"], _fixture.payload_fixture.KVER)
        self.assertIn(f"armada.deployment={target['deployment']}", result["options"])
        self.assertNotEqual(result["path"], str(self.f.current_path))

    def test_unknown_mismatched_and_malformed_evidence_is_inert(self) -> None:
        link = self.f.root.parent / "recovery-sysroot-link"
        link.symlink_to(self.f.root, target_is_directory=True)
        cases = {
            "unknown-binding": {"expected_binding": f"default/{'0' * 64}.0"},
            "root-mismatch": {"expected_root_uuid": OTHER_UUID},
            "boot-mismatch": {"expected_boot_uuid": OTHER_UUID},
            "same-uuids": {"expected_boot_uuid": ROOT_UUID},
            "zero-uuid": {
                "expected_root_uuid": "00000000-0000-0000-0000-000000000000"
            },
            "uppercase-uuid": {
                "expected_root_uuid": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
            },
            "malformed-binding": {"expected_binding": "not-a-binding"},
            "relative-path": {"sysroot_path": "relative/sysroot"},
            "symlink-path": {"sysroot_path": link},
            "missing-path": {"sysroot_path": self.f.root / "missing"},
        }
        before = self._snapshot()
        for name, overrides in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(recovery_deployment.RecoveryDeploymentError):
                    self._read(**overrides)
                self._assert_unchanged(before)

    def test_invalid_missing_manifest_and_unknown_model_are_rejected(self) -> None:
        manifest = (
            self.f.current_path
            / "usr/share/armada/update-boot/manifest.json"
        )
        original = manifest.read_bytes()
        clean = self._snapshot()
        try:
            for name, content in (
                ("invalid-json", b"not json\n"),
                ("missing", None),
            ):
                with self.subTest(name=name):
                    if content is None:
                        manifest.unlink()
                    else:
                        manifest.write_bytes(content)
                    mutated = self._snapshot()
                    with self.assertRaises(recovery_deployment.RecoveryDeploymentError):
                        self._read()
                    self._assert_unchanged(mutated)
                    manifest.write_bytes(original)
            with self.subTest(name="unknown-model"):
                before = self._snapshot()
                with self.assertRaises(recovery_deployment.RecoveryDeploymentError):
                    self._read(model="unknown-fixture-model")
                self._assert_unchanged(before)
        finally:
            manifest.write_bytes(original)
        self._assert_unchanged(clean)

    def test_bls_pointer_mismatch_is_rejected_without_rewrite(self) -> None:
        target = self.f._call(1)
        bls = self._bls_path(self.f.initial_binding)
        original = bls.read_bytes()
        wrong_pointer = next(
            tok for tok in self._dep(
                target["deployment"]
            ).get_bootconfig().get("options").split()
            if tok.startswith("ostree=")
        )
        tokens = [
            wrong_pointer if tok.startswith("ostree=") else tok
            for tok in self._dep(
                self.f.initial_binding
            ).get_bootconfig().get("options").split()
        ]
        lines = []
        for line in original.decode("utf-8").splitlines():
            lines.append(
                "options " + " ".join(tokens)
                if line.startswith("options ") else line
            )
        try:
            bls.write_text("\n".join(lines) + "\n", encoding="utf-8")
            Gio, GLib, OSTree = recovery_deployment._gi()
            fresh = OSTree.Sysroot.new(
                Gio.File.new_for_path(str(self.f.root))
            )
            self.assertTrue(fresh.load(None))
            fresh_bindings = [
                recovery_deployment._binding_of(dep)
                for dep in fresh.get_deployments()
            ]
            self.assertNotIn(self.f.initial_binding, fresh_bindings)
            current = next(
                dep for dep in fresh.get_deployments()
                if recovery_deployment._binding_of(dep)
                == target["deployment"]
            )
            self.assertIn(
                wrong_pointer,
                current.get_bootconfig().get("options").split(),
            )
            mutated = self._snapshot()
            with self.assertRaisesRegex(
                recovery_deployment.RecoveryDeploymentError,
                "expected deployment binding not found exactly once",
            ):
                self._read()
            self._assert_unchanged(mutated)
        finally:
            bls.write_bytes(original)
        self.assertEqual(
            self._read()["installedDeployment"], self.f.initial_binding
        )

    def test_bls_read_drift_during_read_is_rejected(self) -> None:
        bls = self._bls_path(self.f.initial_binding)
        original = bls.read_bytes()
        original_load = recovery_deployment._load_manifest
        mutated = None

        def load_then_drift(path, model):
            result = original_load(path, model)
            if mutated is not None:
                bls.write_bytes(mutated)
            return result

        recovery_deployment._load_manifest = load_then_drift
        self.addCleanup(
            setattr, recovery_deployment, "_load_manifest", original_load
        )
        lines = []
        for line in original.decode("utf-8").splitlines():
            if line.startswith("options "):
                tokens = [
                    "console=ttyS0" if tok.startswith("console=") else tok
                    for tok in line[len("options "):].split()
                ]
                lines.append("options " + " ".join(tokens))
            else:
                lines.append(line)
        mutated = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            with self.assertRaisesRegex(
                recovery_deployment.RecoveryDeploymentError,
                "BLS options changed during read",
            ):
                self._read()
            self.assertEqual(bls.read_bytes(), mutated)
            self.f._assert_user_data_unchanged()
        finally:
            bls.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
