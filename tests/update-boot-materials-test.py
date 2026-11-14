#!/usr/bin/python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
import uuid
from unittest import mock

import test_update_boot_payload as payload_fixture
from updates import boot_materials, boot_payload, boot_recipe
from updates.deployment_options import read_deployment_options


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "system_files/usr/libexec/armada/kshim-build-boot.py"
ROOT_UUID = "11111111-1111-4111-8111-111111111111"
BOOT_UUID = "22222222-2222-4222-8222-222222222222"
ARTIFACT_KEYS = {
    "kernel", "initramfs", "runtime", "dtbNormal", "dtbDiagnostic"
}

_spec = importlib.util.spec_from_file_location(
    "update_deployment_store_fixture",
    Path(__file__).with_name("update-deployment-store-test.py"),
)
_store = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_store)


def run(*args: str) -> str:
    try:
        return subprocess.run(
            list(args), check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"command failed: {exc.args!r}\n"
            f"stderr:\n{exc.stderr}\nstdout:\n{exc.stdout}"
        ) from exc


class BootMaterialsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _store.DeploymentStoreIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        _store.DeploymentStoreIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.f = _store.DeploymentStoreIntegrationTests(
            "test_two_updates_merges_retains_and_reuses"
        )
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        sysroot = self.f._sysroot()
        deployments = [
            dep for dep in sysroot.get_deployments()
            if _store.binding(dep) == self.f.initial_binding
        ]
        self.assertEqual(len(deployments), 1)
        dep = deployments[0]
        options = dep.get_bootconfig().get("options") or ""
        tokens = options.split() + [
            f"root=UUID={ROOT_UUID}",
            f"boot=UUID={BOOT_UUID}",
            f"armada.deployment={self.f.initial_binding}",
        ]
        self.assertTrue(sysroot.deployment_set_kargs(dep, tokens, None))
        self.target_commit = self.f._commit_tree(
            "materials", 1, damage=self._real_dtbs()
        )
        self.result = self.f._call(1, target_commit=self.target_commit)
        self.target_binding = self.result["deployment"]
        self.target_path = Path(self.result["path"])
        self.assertTrue(HELPER.is_file())

    def _real_dtbs(self, bad_model=None):
        def damage(source: Path) -> None:
            dtb_dir = (
                source / "usr/lib/modules" / payload_fixture.KVER / "dtb"
            )
            for model, profile in self.f.payload.cfg["profiles"].items():
                required = (
                    ["armada,invalid-compatible-for-materials-test"]
                    if model == bad_model else list(profile["required"])
                )
                compatible = ", ".join(json.dumps(item) for item in required)
                dts = (
                    "/dts-v1/; / { compatible = " + compatible + "; "
                    "chosen { bootargs=\"fixture\"; "
                    "linux,initrd-start = <0x10>; "
                    "linux,initrd-end = <0x20>; "
                    "armada,update-transaction = "
                    "[99 88 77 66 55 44 33 22 11 0f 0e 0d 0c 0b 0a 09]; }; "
                    "test-node { marker = \"keep\"; }; };"
                )
                subprocess.run(
                    ["dtc", "-I", "dts", "-O", "dtb", "-o",
                     str(dtb_dir / profile["dtb"]), "-"],
                    input=dts, text=True, check=True, capture_output=True,
                )
            manifest = boot_payload.create_manifest(source)
            (source / "usr/share/armada/update-boot/manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
        return damage

    def _prepare(self, model: str, output: Path, helper=HELPER,
                 transaction_uuid=None):
        return boot_materials.prepare_boot_materials(
            sysroot_path=self.f.root,
            current_binding=self.f.initial_binding,
            target_binding=self.target_binding,
            model=model,
            output_dir=output,
            dtb_helper_path=helper,
            transaction_uuid=transaction_uuid,
        )

    def _source_snapshot(self):
        return {
            "current-etc": _store.tree_digest(self.f.current_path / "etc"),
            "current-usr": _store.tree_digest(self.f.current_path / "usr"),
            "target-etc": _store.tree_digest(self.target_path / "etc"),
            "target-usr": _store.tree_digest(self.target_path / "usr"),
            "boot": _store.tree_digest(self.f.root / "boot"),
        }

    def _assert_sources_unchanged(self, before) -> None:
        self.assertEqual(self._source_snapshot(), before)
        self.f._assert_user_data_unchanged()

    def test_all_models_real_helper_artifacts_and_source_immutability(self):
        before = self._source_snapshot()
        cwd = os.getcwd()
        runtime_hashes = set()
        for model, profile in self.f.payload.cfg["profiles"].items():
            with self.subTest(model=model):
                out = self.f.root.parent / f"materials-{model}"
                report = self._prepare(model, out)
                self.assertEqual(report["phase"], "materials_prepared")
                self.assertEqual(set(report["artifacts"]), ARTIFACT_KEYS)
                self.assertEqual(
                    json.loads((out / "materials.json").read_text()), report
                )
                options = read_deployment_options(
                    sysroot_path=self.f.root,
                    current_binding=self.f.initial_binding,
                    target_binding=self.target_binding,
                )
                normal = boot_recipe.model_boot_args(options["options"], profile)
                self.assertEqual(report["normalBootArgs"], normal)
                self.assertEqual(
                    report["diagnosticBootArgs"],
                    boot_recipe.diagnostic_boot_args(normal, profile),
                )
                self.assertEqual(report["rootUuid"], ROOT_UUID)
                self.assertEqual(report["bootUuid"], BOOT_UUID)
                for key, record in report["artifacts"].items():
                    data = Path(record["path"]).read_bytes()
                    self.assertEqual(record["bytes"], len(data), key)
                    self.assertEqual(
                        record["sha256"], hashlib.sha256(data).hexdigest(), key
                    )
                runtime_hashes.add(report["artifacts"]["runtime"]["sha256"])
                for key, stage in (("dtbNormal", "os"),
                                   ("dtbDiagnostic", "maintenance")):
                    dtb = report["artifacts"][key]["path"]
                    args = report["normalBootArgs"] if key == "dtbNormal" else report["diagnosticBootArgs"]
                    self.assertEqual(
                        run("fdtget", "-t", "s", dtb, "/chosen", "bootargs"),
                        args,
                    )
                    self.assertEqual(
                        run("fdtget", dtb, "/chosen", "armada,boot-stage"),
                        stage,
                    )
                    self.assertEqual(
                        run("fdtget", dtb, "/chosen", "armada,root-medium"),
                        "internal",
                    )
                    self.assertEqual(
                        run("fdtget", "-t", "x", dtb, "/chosen", "kshim,initrd-entry"),
                        "5",
                    )
                    for prop in ("linux,initrd-start", "linux,initrd-end"):
                        stale = subprocess.run(
                            ["fdtget", dtb, "/chosen", prop],
                            capture_output=True, text=True,
                        )
                        self.assertNotEqual(stale.returncode, 0)
                    self.assertEqual(
                        run("fdtget", "-t", "s", dtb, "/test-node", "marker"),
                        "keep",
                    )
                    self.assertEqual(
                        set(run("fdtget", "-t", "s", dtb, "/", "compatible").split()),
                        set(profile["required"]),
                    )
                self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
                self.assertEqual(
                    stat.S_IMODE((out / "materials.json").stat().st_mode), 0o600
                )
        self.assertEqual(os.getcwd(), cwd)
        self.assertEqual(len(runtime_hashes), len(self.f.payload.cfg["profiles"]))
        self._assert_sources_unchanged(before)

    def test_invalid_outputs_and_untrusted_helper_are_rejected(self):
        before = self._source_snapshot()
        existing = self.f.root.parent / "materials-existing"
        existing.mkdir()
        sentinel = existing / "sentinel"
        sentinel.write_text("retained", encoding="utf-8")
        linked = self.f.root.parent / "materials-link"
        linked.symlink_to(existing, target_is_directory=True)
        cases = [
            ("unknown-model", self.f.root.parent / "materials-unknown", HELPER,
             "not-a-configured-model"),
            ("existing", existing, HELPER, self.f.model),
            ("symlink", linked, HELPER, self.f.model),
            ("current-root", self.f.current_path / "new-materials", HELPER,
             self.f.model),
            ("target-root", self.target_path / "new-materials", HELPER,
             self.f.model),
            ("candidate-helper", self.f.root.parent / "materials-helper",
             self.target_path / "usr/lib/os-release", self.f.model),
        ]
        for name, output, helper, model in cases:
            with self.subTest(name=name):
                with self.assertRaises(boot_materials.BootMaterialsError):
                    self._prepare(model, output, helper)
                self.assertFalse((output / "materials.json").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "retained")
        self._assert_sources_unchanged(before)

    def test_bad_compatible_fails_real_dtb_verification(self):
        model = self.f.model
        bad_commit = self.f._commit_tree(
            "bad-compatible", 1, damage=self._real_dtbs(bad_model=model)
        )
        bad = self.f._call(1, target_commit=bad_commit)
        self.target_binding = bad["deployment"]
        self.target_path = Path(bad["path"])
        before = self._source_snapshot()
        out = self.f.root.parent / "materials-bad-compatible"
        with self.assertRaisesRegex(
            boot_materials.BootMaterialsError, "compatible-required"
        ):
            self._prepare(model, out)
        self.assertTrue(out.is_dir())
        self.assertFalse((out / "materials.json").exists())
        self._assert_sources_unchanged(before)

    def test_zero_write_is_an_error_without_report(self):
        before = self._source_snapshot()
        out = self.f.root.parent / "materials-zero-write"
        with mock.patch.object(boot_materials.os, "write", return_value=0):
            with self.assertRaises(boot_materials.BootMaterialsError):
                self._prepare(self.f.model, out)
        self.assertFalse((out / "materials.json").exists())
        self._assert_sources_unchanged(before)

    def _commit_with_unmarked_runtime(self, model):
        def damage(source: Path) -> None:
            self._real_dtbs()(source)
            runtime = (
                source
                / "usr/share/armada/update-boot/runtimes"
                / f"{model}.bin"
            )
            data = bytearray(runtime.read_bytes())
            marker = boot_payload.MIRROR_RUNTIME_MARKER
            offset = data.find(marker)
            self.assertGreaterEqual(offset, 0)
            data[offset:offset + len(marker)] = b"\x00" * len(marker)
            runtime.write_bytes(bytes(data))
            manifest_path = (
                source / "usr/share/armada/update-boot/manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["models"][model]["runtime"]["sha256"] = (
                hashlib.sha256(data).hexdigest()
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return self.f._commit_tree("unmarked-runtime", 1, damage=damage)

    def test_unmarked_mirror_runtime_is_rejected(self):
        model = self.f.model
        self.assertIn(model, boot_payload.MIRROR_POLICY_MODELS)
        bad_commit = self._commit_with_unmarked_runtime(model)
        bad = self.f._call(1, target_commit=bad_commit)
        self.target_binding = bad["deployment"]
        self.target_path = Path(bad["path"])
        before = self._source_snapshot()
        out = self.f.root.parent / "materials-unmarked-runtime"
        with self.assertRaisesRegex(
            boot_materials.BootMaterialsError, "ARMADA_LINUX_MIRROR_V1"
        ):
            self._prepare(model, out)
        self.assertFalse((out / "materials.json").exists())
        self._assert_sources_unchanged(before)

    def test_unmarked_tb710fu_runtime_is_not_gated(self):
        model = "TB710FU"
        if model not in self.f.payload.cfg["profiles"]:
            self.skipTest("TB710FU profile is not configured")
        bad_commit = self._commit_with_unmarked_runtime(model)
        bad = self.f._call(1, target_commit=bad_commit)
        self.target_binding = bad["deployment"]
        self.target_path = Path(bad["path"])
        before = self._source_snapshot()
        out = self.f.root.parent / "materials-unmarked-tb710fu"
        report = self._prepare(model, out)
        self.assertEqual(report["phase"], "materials_prepared")
        runtime = Path(report["artifacts"]["runtime"]["path"]).read_bytes()
        self.assertNotIn(boot_payload.MIRROR_RUNTIME_MARKER, runtime)
        self._assert_sources_unchanged(before)

    def test_transaction_uuid_written_verified_and_reported(self):
        before = self._source_snapshot()
        transaction_uuid = uuid.UUID("01020a0b-0c0d-4e0f-8001-020304050607")
        for model in self.f.payload.cfg["profiles"]:
            with self.subTest(model=model):
                out = self.f.root.parent / f"materials-tx-{model}"
                report = self._prepare(
                    model, out, transaction_uuid=str(transaction_uuid)
                )
                persisted = json.loads((out / "materials.json").read_text())
                self.assertEqual(persisted, report)
                self.assertEqual(report["transactionUuid"],
                                 str(transaction_uuid))
                for key in ("dtbNormal", "dtbDiagnostic"):
                    dtb = report["artifacts"][key]["path"]
                    raw = run("fdtget", "-t", "bx", dtb, "/chosen",
                              "armada,update-transaction")
                    got = bytes(int(token, 16) for token in raw.split())
                    self.assertEqual(len(got), 16)
                    self.assertEqual(got, transaction_uuid.bytes)
                    self.assertEqual(
                        run("fdtget", "-t", "s", dtb, "/test-node", "marker"),
                        "keep",
                    )
                plain = self.f.root.parent / f"materials-notx-{model}"
                plain_report = self._prepare(model, plain)
                plain_persisted = json.loads(
                    (plain / "materials.json").read_text()
                )
                self.assertEqual(plain_persisted, plain_report)
                self.assertNotIn("transactionUuid", plain_report)
                for key in ("dtbNormal", "dtbDiagnostic"):
                    dtb = plain_report["artifacts"][key]["path"]
                    props = run("fdtget", "-p", dtb, "/chosen")
                    self.assertNotIn(
                        "armada,update-transaction", props.split()
                    )
        self._assert_sources_unchanged(before)

    def test_invalid_transaction_uuid_rejected_before_output(self):
        before = self._source_snapshot()
        invalid_uuids = [
            "malformed",
            "00000000-0000-0000-0000-000000000000",
            "01020A0B-0C0D-4E0F-8001-020304050607",
            "01020a0b0c0d4e0f8001020304050607",
            True,
            1,
            [],
        ]
        for bad in invalid_uuids:
            with self.subTest(bad=bad):
                out = self.f.root.parent / "materials-bad-tx"
                with self.assertRaises(boot_materials.BootMaterialsError):
                    self._prepare(self.f.model, out, transaction_uuid=bad)
                self.assertFalse(out.exists())
        self._assert_sources_unchanged(before)


if __name__ == "__main__":
    unittest.main()
