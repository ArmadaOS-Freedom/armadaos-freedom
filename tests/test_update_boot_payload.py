from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "system_files/usr/lib/armada"))

import updates
from updates import boot_payload
BUILD_FILES = REPO / "build_files"
KVER = "6.6-fixture"
FW_SHA = hashlib.sha256(b"firmware").hexdigest()


class UpdateBootPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = str(BUILD_FILES)
        cls.added_path = cls.build_dir not in updates.__path__
        if cls.added_path:
            updates.__path__.append(cls.build_dir)
        cfg = json.loads((BUILD_FILES / "edl-devices.json").read_text(encoding="utf-8"))
        for prof in cfg["profiles"].values():
            prof["bootRequirements"] = {
                "earlyFirmware": ["test/fw.bin"],
                "earlyDrivers": ["test_core"],
                "firmwareSha256": {"test/fw.bin": FW_SHA},
            }
        cls.cfg = cfg
        cls.models = list(cfg["profiles"])
        cls._case_idx = 0

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "added_path", False) and cls.build_dir in updates.__path__:
            updates.__path__.remove(cls.build_dir)

    def _make_root(self) -> Path:
        type(self)._case_idx += 1
        td = tempfile.TemporaryDirectory(prefix=f"ubp-{self._case_idx}-")
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "root"
        fw = root / "usr/lib/firmware/test/fw.bin"
        fw.parent.mkdir(parents=True, exist_ok=True)
        fw.write_bytes(b"firmware")
        mod = root / f"usr/lib/modules/{KVER}"
        (mod / "dtb").mkdir(parents=True, exist_ok=True)
        (mod / "modules.builtin").write_text("kernel/test_core.ko\n", encoding="utf-8")
        (mod / "vmlinuz").write_bytes(b"\x00" * 56 + b"ARM\x64" + b"\x00" * 4)
        (mod / "initramfs.img").write_bytes(b"fixture")
        ub = root / "usr/share/armada/update-boot"
        rt_dir = ub / "runtimes"
        rt_dir.mkdir(parents=True, exist_ok=True)
        (ub / "edl-devices.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        dtb_bytes = struct.pack(">II", 0xD00DFEED, 40) + b"\x00" * 32
        for idx, (model, prof) in enumerate(self.cfg["profiles"].items()):
            dtb = mod / "dtb" / prof["dtb"]
            dtb.parent.mkdir(parents=True, exist_ok=True)
            dtb.write_bytes(dtb_bytes)
            runtime = bytearray(bytes([idx + 1]) * 4194304)
            marker = boot_payload.MIRROR_RUNTIME_MARKER
            runtime[:len(marker)] = marker
            (rt_dir / f"{model}.bin").write_bytes(bytes(runtime))
        return root

    def test_roundtrip_and_zero_writes(self) -> None:
        root = self._make_root()
        snap = lambda: {
            p.relative_to(root): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*")
            if p.is_file()
        }
        before = snap()
        manifest = boot_payload.create_manifest(root)
        self.assertEqual(set(manifest["models"]), set(self.models))
        rt_hashes = set()
        for model in self.models:
            res = boot_payload.verify_manifest(root, model, copy.deepcopy(manifest))
            self.assertEqual(res["runtime"]["path"], f"usr/share/armada/update-boot/runtimes/{model}.bin")
            rt_hashes.add(res["runtime"]["sha256"])
        self.assertEqual(len(rt_hashes), 3)
        self.assertEqual(before, snap())

    def _unmark_runtime(self, root: Path, model: str, document: dict) -> bytes:
        """Remove the marker in place and keep the manifest record truthful."""
        path = root / f"usr/share/armada/update-boot/runtimes/{model}.bin"
        data = bytearray(path.read_bytes())
        marker = boot_payload.MIRROR_RUNTIME_MARKER
        offset = data.find(marker)
        self.assertGreaterEqual(offset, 0)
        data[offset:offset + len(marker)] = b"\x00" * len(marker)
        path.write_bytes(bytes(data))
        document["models"][model]["runtime"]["sha256"] = (
            hashlib.sha256(data).hexdigest()
        )
        return bytes(data)

    def test_mirror_marker_gate_is_policy_specific(self) -> None:
        for model in ("TB322FC", "TB323FU"):
            if model not in self.models:
                continue
            with self.subTest(model=model):
                root = self._make_root()
                document = copy.deepcopy(boot_payload.create_manifest(root))
                self._unmark_runtime(root, model, document)

                # The new-system target gate refuses the unmarked runtime.
                with self.assertRaisesRegex(
                    ValueError, "ARMADA_LINUX_MIRROR_V1"
                ):
                    boot_payload.verify_manifest(
                        root, model, document, require_runtime_marker=True
                    )

                # Read-only/legacy manifest inspection still works; only the
                # accepted new-system runtime boundary has the extra gate.
                verified = boot_payload.verify_manifest(root, model, document)
                self.assertEqual(
                    verified["runtime"]["sha256"],
                    document["models"][model]["runtime"]["sha256"],
                )

        if "TB710FU" in self.models:
            root = self._make_root()
            document = copy.deepcopy(boot_payload.create_manifest(root))
            self._unmark_runtime(root, "TB710FU", document)
            verified = boot_payload.verify_manifest(
                root, "TB710FU", document, require_runtime_marker=True
            )
            self.assertEqual(
                verified["runtime"]["sha256"],
                document["models"]["TB710FU"]["runtime"]["sha256"],
            )

    def test_mutated_files_and_symlinks_rejected(self) -> None:
        m0 = self.models[0]
        dtb_rel = self.cfg["profiles"][m0]["dtb"]

        def move_runtimes_outside(r: Path) -> None:
            rt = r / "usr/share/armada/update-boot/runtimes"
            out = r.parent / "outside"
            rt.rename(out)
            rt.symlink_to(out)

        cases = [
            lambda r: (r / f"usr/share/armada/update-boot/runtimes/{m0}.bin").unlink(),
            lambda r: (r / f"usr/lib/modules/{KVER}/dtb/{dtb_rel}").unlink(),
            lambda r: (r / "usr/lib/firmware/test/fw.bin").unlink(),
            lambda r: (r / f"usr/lib/modules/{KVER}/modules.builtin").unlink(),
            lambda r: (r / f"usr/lib/modules/{KVER}/vmlinuz").write_bytes(b"\x00" * 64),
            lambda r: (r / f"usr/lib/modules/{KVER}/dtb/{dtb_rel}").write_bytes(
                struct.pack(">II", 0xD00DFEED, 64) + b"\x00" * 32
            ),
            lambda r: (r / f"usr/share/armada/update-boot/runtimes/{m0}.bin").write_bytes(b"x" * 1024),
            lambda r: (r / "usr/lib/modules/6.7-extra").mkdir(),
            lambda r: (
                (r / f"usr/share/armada/update-boot/runtimes/{m0}.bin").unlink(),
                (r / f"usr/share/armada/update-boot/runtimes/{m0}.bin").symlink_to(r.parent),
            ),
            move_runtimes_outside,
        ]
        for mutate in cases:
            root = self._make_root()
            manifest = boot_payload.create_manifest(root)
            mutate(root)
            with self.assertRaises((ValueError, OSError)):
                boot_payload.create_manifest(root)
            with self.assertRaises((ValueError, OSError)):
                boot_payload.verify_manifest(root, m0, manifest)

    def test_manifest_document_validation(self) -> None:
        root = self._make_root()
        manifest = boot_payload.create_manifest(root)
        m0 = self.models[0]
        bad_schema = copy.deepcopy(manifest)
        bad_schema["schemaVersion"] = True
        bad_hash = copy.deepcopy(manifest)
        bad_hash["kernel"]["sha256"] = "0" * 64
        extra_model = copy.deepcopy(manifest)
        extra_model["models"]["extra"] = copy.deepcopy(manifest["models"][m0])
        for doc, model in [
            (bad_schema, m0),
            (bad_hash, m0),
            (extra_model, m0),
            (manifest, "unknown-model"),
        ]:
            with self.assertRaises(ValueError):
                boot_payload.verify_manifest(root, model, doc)

    def _setup_56_case(self) -> tuple[Path, Path]:
        root = self._make_root()
        ctx = root.parent / "ctx"
        (ctx / "build_files").mkdir(parents=True)
        (ctx / "armada-runtimes").mkdir(parents=True)
        for name in ("edl_device_config.py", "edl_boot_requirements.py"):
            shutil.copy2(BUILD_FILES / name, ctx / "build_files" / name)
        (ctx / "build_files/edl-devices.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        sums = []
        for model in self.models:
            src = root / f"usr/share/armada/update-boot/runtimes/{model}.bin"
            dst = ctx / "armada-runtimes" / f"{model}.bin"
            shutil.copy2(src, dst)
            sums.append(f"{hashlib.sha256(dst.read_bytes()).hexdigest()}  {model}.bin\n")
            src.unlink()
        (ctx / "armada-runtimes/SHA256SUMS").write_text("".join(sums), encoding="utf-8")
        upd = root / "usr/lib/armada/updates"
        upd.mkdir(parents=True, exist_ok=True)
        for name in ("__init__.py", "boot_payload.py"):
            shutil.copy2(REPO / "system_files/usr/lib/armada/updates" / name, upd / name)
        (root / "var/home/user/file").parent.mkdir(parents=True, exist_ok=True)
        (root / "var/home/user/file").write_text("keep", encoding="utf-8")
        return root, ctx

    def test_56_install_script_and_wiring(self) -> None:
        root, ctx = self._setup_56_case()
        script = BUILD_FILES / "56-install-update-boot.sh"
        subprocess.run(["bash", str(script), "--root", str(root), "--context", str(ctx)], check=True)
        manifest_path = root / "usr/share/armada/update-boot/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["models"]), set(self.models))
        self.assertEqual((root / "var/home/user/file").read_text(encoding="utf-8"), "keep")
        for name in ("edl_device_config.py", "edl_boot_requirements.py"):
            inst = root / "usr/lib/armada/updates" / name
            self.assertEqual(inst.read_bytes(), (BUILD_FILES / name).read_bytes())
            self.assertEqual(inst.stat().st_mode & 0o777, 0o644)
        env = dict(os.environ, PYTHONPATH=str(root / "usr/lib/armada"))
        out = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import json, sys; from updates import boot_payload; "
                "print(json.dumps(boot_payload.create_manifest(sys.argv[1]), sort_keys=True))",
                str(root),
            ],
            env=env,
            text=True,
        )
        self.assertEqual(json.loads(out), manifest)

        root_bad, ctx_bad = self._setup_56_case()
        (ctx_bad / "armada-runtimes" / f"{self.models[0]}.bin").write_bytes(b"corrupt" * 600000)
        res = subprocess.run(["bash", str(script), "--root", str(root_bad), "--context", str(ctx_bad)], capture_output=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse((root_bad / "usr/share/armada/update-boot/manifest.json").exists())

        cfile = (REPO / "Containerfile").read_text(encoding="utf-8")
        self.assertIn("COPY --from=update-runtime /runtime-build/runtimes/ /armada-runtimes/", cfile)
        self.assertNotIn("56-install-update-boot.sh", cfile)
        bscript = (BUILD_FILES / "build.sh").read_text(encoding="utf-8")
        self.assertLess(bscript.index("55-"), bscript.index("56-"))
        self.assertLess(bscript.index("56-"), bscript.index("60-"))

    def _load_wrapper(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "edl_wrapper_under_test", BUILD_FILES / "make-edl-raw-package.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["edl_wrapper_under_test"] = module
        self.addCleanup(sys.modules.pop, "edl_wrapper_under_test", None)
        spec.loader.exec_module(module)
        return module

    def _prepare_case(self, models):
        root = self._make_root()
        manifest = boot_payload.create_manifest(root)
        ub = root / "usr/share/armada/update-boot"
        (ub / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        upd = root / "usr/lib/armada/updates"
        upd.mkdir(parents=True, exist_ok=True)
        for name in ("__init__.py", "boot_payload.py"):
            (upd / name).write_text(
                "raise RuntimeError('deployment python must never execute')\n",
                encoding="utf-8",
            )
        tmp = Path(tempfile.mkdtemp(prefix="ubp-ext-", dir=str(root.parent)))
        self.addCleanup(shutil.rmtree, tmp, True)
        mod = root / f"usr/lib/modules/{KVER}"
        kernel = tmp / "vmlinuz"
        shutil.copy2(mod / "vmlinuz", kernel)
        initramfs = tmp / "initramfs.img"
        shutil.copy2(mod / "initramfs.img", initramfs)
        dtbs = {}
        for model in models:
            dst = tmp / f"{model}.dtb"
            shutil.copy2(mod / "dtb" / self.cfg["profiles"][model]["dtb"], dst)
            dtbs[model] = dst
        external = {}
        for model in self.models:
            ext = tmp / f"ext-{model}.bin"
            shutil.copy2(ub / "runtimes" / f"{model}.bin", ext)
            rec = manifest["models"][model]["runtime"]
            external[model] = {
                "path": str(ext),
                "bytes": rec["bytes"],
                "sha256": rec["sha256"],
            }
        return {
            "root": root,
            "manifest": manifest,
            "ub": ub,
            "kernel": kernel,
            "initramfs": initramfs,
            "dtbs": dtbs,
            "external": external,
        }

    def _call_verified(self, wrapper, case, models, external=None):
        return wrapper._verified_deployment_runtimes(
            REPO,
            case["root"],
            self.cfg,
            tuple(models),
            {} if external is None else external,
            str(case["kernel"]),
            str(case["initramfs"]),
            {m: str(p) for m, p in case["dtbs"].items()},
        )

    def test_verified_runtimes_are_embedded(self):
        wrapper = self._load_wrapper()
        for models in (list(self.models), list(self.models[:2])):
            case = self._prepare_case(models)
            same = {m: case["external"][m] for m in models}
            for ext in ({}, same):
                verified = self._call_verified(wrapper, case, models, ext)
                self.assertEqual(set(verified), set(models))
                for model in models:
                    expected = (
                        case["root"]
                        / "usr/share/armada/update-boot/runtimes"
                        / f"{model}.bin"
                    ).resolve()
                    self.assertEqual(verified[model]["path"], str(expected))
                    self.assertNotEqual(
                        verified[model]["path"], case["external"][model]["path"]
                    )
                    self.assertEqual(verified[model]["bytes"], 4194304)
                    self.assertEqual(
                        verified[model]["sha256"],
                        case["manifest"]["models"][model]["runtime"]["sha256"],
                    )

    def _unmark_case_runtime(self, case, model):
        path = (
            case["root"]
            / "usr/share/armada/update-boot/runtimes"
            / f"{model}.bin"
        )
        data = bytearray(path.read_bytes())
        marker = boot_payload.MIRROR_RUNTIME_MARKER
        offset = data.find(marker)
        self.assertGreaterEqual(offset, 0)
        data[offset:offset + len(marker)] = b"\x00" * len(marker)
        path.write_bytes(bytes(data))
        digest = hashlib.sha256(data).hexdigest()
        case["manifest"]["models"][model]["runtime"]["sha256"] = digest
        (case["ub"] / "manifest.json").write_text(
            json.dumps(case["manifest"]), encoding="utf-8"
        )
        return digest

    def test_package_builder_requires_mirror_marker(self):
        wrapper = self._load_wrapper()
        for model in ("TB322FC", "TB323FU"):
            if model not in self.models:
                continue
            with self.subTest(model=model):
                case = self._prepare_case([model])
                self._unmark_case_runtime(case, model)
                with self.assertRaisesRegex(
                    ValueError, "ARMADA_LINUX_MIRROR_V1"
                ):
                    self._call_verified(wrapper, case, [model], {})

        if "TB710FU" in self.models:
            case = self._prepare_case(["TB710FU"])
            digest = self._unmark_case_runtime(case, "TB710FU")
            verified = self._call_verified(wrapper, case, ["TB710FU"], {})
            self.assertEqual(verified["TB710FU"]["sha256"], digest)

    def test_external_runtime_crosscheck_rejections(self):
        wrapper = self._load_wrapper()
        models = list(self.models)
        case = self._prepare_case(models)
        missing = {m: case["external"][m] for m in models[:2]}
        extra = dict(case["external"])
        extra["extra"] = dict(case["external"][models[0]])
        stale = {m: dict(r) for m, r in case["external"].items()}
        stale_path = Path(stale[models[0]]["path"])
        blob = bytearray(stale_path.read_bytes())
        blob[0] ^= 0xFF
        stale_path.write_bytes(bytes(blob))
        swapped = dict(case["external"])
        swapped[models[0]] = case["external"][models[1]]
        for ext in (missing, extra, stale, swapped):
            with self.assertRaises(ValueError):
                self._call_verified(wrapper, case, models, ext)

    def test_embedded_drift_and_boot_asset_mismatch(self):
        wrapper = self._load_wrapper()
        model = self.models[0]

        def fresh():
            return self._prepare_case([model])

        case = fresh()
        drifted = copy.deepcopy(self.cfg)
        drifted["crosscheckNote"] = "drift"
        with self.assertRaises(ValueError):
            wrapper._verified_deployment_runtimes(
                REPO, case["root"], drifted, (model,), {},
                str(case["kernel"]), str(case["initramfs"]),
                {model: str(case["dtbs"][model])},
            )
        case = fresh()
        case["kernel"].write_bytes(b"\x00" * 56 + b"ARM\x64" + b"\x00" * 8)
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, [model], {})
        case = fresh()
        with case["initramfs"].open("ab") as fh:
            fh.write(b"x")
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, [model], {})
        case = fresh()
        dtb = case["dtbs"][model]
        blob = bytearray(dtb.read_bytes())
        blob.append(0xFF)
        dtb.write_bytes(bytes(blob))
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, [model], {})

    def test_manifest_and_payload_rejections(self):
        wrapper = self._load_wrapper()
        subset = list(self.models[:2])
        case = self._prepare_case(subset)
        (case["ub"] / "manifest.json").unlink()
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})
        case = self._prepare_case(subset)
        (case["ub"] / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})
        case = self._prepare_case(subset)
        doc = (case["ub"] / "manifest.json").read_text(encoding="utf-8")
        (case["ub"] / "manifest.json").write_text(
            doc + " " * (1 << 20), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})
        case = self._prepare_case(subset)
        tampered = copy.deepcopy(case["manifest"])
        tampered["kernel"]["sha256"] = "0" * 64
        (case["ub"] / "manifest.json").write_text(
            json.dumps(tampered), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})
        case = self._prepare_case(subset)
        (case["ub"] / "runtimes" / f"{self.models[2]}.bin").unlink()
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})
        case = self._prepare_case(subset)
        (case["root"] / "usr/lib/firmware/test/fw.bin").unlink()
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})

    def test_symlinks_and_namespace_hygiene(self):
        import types as types_mod

        wrapper = self._load_wrapper()
        sentinel = types_mod.ModuleType("_armada_trusted_updates_keep")
        sys.modules[sentinel.__name__] = sentinel
        self.addCleanup(sys.modules.pop, sentinel.__name__, None)
        before = set(sys.modules)
        subset = list(self.models[:1])
        case = self._prepare_case(subset)
        self._call_verified(wrapper, case, subset, {})
        self.assertIs(sys.modules.get(sentinel.__name__), sentinel)
        bad_ext = {
            self.models[0]: {
                "path": "/nonexistent-runtime.bin",
                "bytes": 4194304,
                "sha256": "0" * 64,
            }
        }
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, bad_ext)
        leaked = [
            n for n in sys.modules
            if n not in before and n.startswith("_armada_trusted_updates_")
        ]
        self.assertEqual(leaked, [])

        case = self._prepare_case(subset)
        manifest_path = case["ub"] / "manifest.json"
        outside = case["root"].parent / "outside-manifest.json"
        shutil.copy2(manifest_path, outside)
        manifest_path.unlink()
        manifest_path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})

        case = self._prepare_case(subset)
        ub = case["ub"]
        moved = case["root"].parent / "outside-ub"
        ub.rename(moved)
        ub.symlink_to(moved)
        with self.assertRaises(ValueError):
            self._call_verified(wrapper, case, subset, {})

    def test_loader_failure_isolated(self):
        wrapper = self._load_wrapper()
        src = Path(tempfile.mkdtemp(prefix="ubp-src-"))
        self.addCleanup(shutil.rmtree, src, True)
        upd = src / "system_files/usr/lib/armada/updates"
        bld = src / "build_files"
        upd.mkdir(parents=True)
        bld.mkdir(parents=True)
        (upd / "__init__.py").write_text(
            "raise RuntimeError('trusted loader failure')\n", encoding="utf-8"
        )
        shutil.copy2(
            REPO / "system_files/usr/lib/armada/updates/boot_payload.py",
            upd / "boot_payload.py",
        )
        for name in ("edl_device_config.py", "edl_boot_requirements.py"):
            shutil.copy2(BUILD_FILES / name, bld / name)
        subset = list(self.models[:1])
        case = self._prepare_case(subset)
        before = set(sys.modules)
        with self.assertRaises(RuntimeError):
            wrapper._verified_deployment_runtimes(
                src, case["root"], self.cfg, tuple(subset), {},
                str(case["kernel"]), str(case["initramfs"]),
                {subset[0]: str(case["dtbs"][subset[0]])},
            )
        self.assertEqual(
            [
                n for n in sys.modules
                if n not in before and n.startswith("_armada_trusted_updates_")
            ],
            [],
        )
        (upd / "boot_payload.py").unlink()
        with self.assertRaises(ValueError):
            wrapper._verified_deployment_runtimes(
                src, case["root"], self.cfg, tuple(subset), {},
                str(case["kernel"]), str(case["initramfs"]),
                {subset[0]: str(case["dtbs"][subset[0]])},
            )
