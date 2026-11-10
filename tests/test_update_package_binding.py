import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "build_files/make-edl-raw-package.py"
SPEC = importlib.util.spec_from_file_location("make_edl_raw_package", SOURCE)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = producer
SPEC.loader.exec_module(producer)


class UpdatePackageBindingTests(unittest.TestCase):
    COMMIT = "a" * 64
    BOOT_CHECKSUM = "b" * 64

    def make_tree(self, root, boot="boot.0", serial="0", commit=COMMIT):
        deployment = (
            root / "ostree/deploy/default/deploy" / f"{commit}.{serial}"
        )
        deployment.mkdir(parents=True)
        link = root / "ostree" / boot / "default" / self.BOOT_CHECKSUM / "0"
        link.parent.mkdir(parents=True)
        link.symlink_to(deployment, target_is_directory=True)
        pointer = f"ostree=/ostree/{boot}/default/{self.BOOT_CHECKSUM}/0"
        parsed = {
            "normalBootArgs": f"console=ttyS0 {pointer}",
            "diagnosticBootArgs": f"console=ttyS0 {pointer}",
        }
        return deployment, link, parsed

    def test_valid_boot_slots_and_serials(self):
        for boot in ("boot.0", "boot.1"):
            for serial in ("0", "7"):
                with self.subTest(boot=boot, serial=serial):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        deployment, _, parsed = self.make_tree(
                            root, boot=boot, serial=serial
                        )
                        original = dict(parsed)
                        bound = producer._bind_deployment(
                            root, deployment, parsed, self.COMMIT
                        )
                        binding = f"armada.deployment=default/{self.COMMIT}.{serial}"
                        self.assertEqual(parsed, original)
                        self.assertEqual(
                            bound["normalBootArgs"],
                            original["normalBootArgs"] + " " + binding,
                        )
                        self.assertEqual(
                            bound["diagnosticBootArgs"],
                            original["diagnosticBootArgs"] + " " + binding,
                        )
                        for field in ("normalBootArgs", "diagnosticBootArgs"):
                            self.assertEqual(bound[field].split().count(binding), 1)

    def test_rejects_stale_missing_and_non_symlink_pointers(self):
        for case in ("stale", "missing", "file"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                deployment, link, parsed = self.make_tree(root)
                link.unlink()
                if case == "stale":
                    other = root / "ostree/deploy/default/deploy" / ("c" * 64 + ".0")
                    other.mkdir()
                    link.symlink_to(other, target_is_directory=True)
                elif case == "file":
                    link.write_text("not a symlink", encoding="utf-8")
                with self.assertRaises(ValueError):
                    producer._bind_deployment(root, deployment, parsed, self.COMMIT)

    def test_rejects_noncanonical_deployment_names(self):
        cases = (("00", self.COMMIT), ("4294967296", self.COMMIT), ("0", "c" * 64))
        for serial, commit in cases:
            with self.subTest(serial=serial, commit=commit), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                deployment, _, parsed = self.make_tree(root, serial=serial, commit=commit)
                with self.assertRaises(ValueError):
                    producer._bind_deployment(root, deployment, parsed, self.COMMIT)

    def test_rejects_symlink_deployment_and_parent_escape(self):
        for case in ("symlink", "escape"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "target"
                root.mkdir()
                if case == "symlink":
                    deployment, _, parsed = self.make_tree(root)
                    deployment.rmdir()
                    actual = Path(temporary) / "actual"
                    actual.mkdir()
                    deployment.symlink_to(actual, target_is_directory=True)
                else:
                    outside = Path(temporary) / "outside"
                    outside.mkdir()
                    (outside / ("c" * 64 + ".0")).mkdir()
                    deploy_dir = root / "ostree/deploy/default"
                    deploy_dir.mkdir(parents=True)
                    deploy_dir.joinpath("deploy").symlink_to(
                        outside, target_is_directory=True
                    )
                    _, _, parsed = self.make_tree(root)
                    deployment = deploy_dir / "deploy" / f"{self.COMMIT}.0"
                with self.assertRaises(ValueError):
                    producer._bind_deployment(root, deployment, parsed, self.COMMIT)

    def test_rejects_invalid_existing_boot_arguments(self):
        additions = (
            "armada.deployment",
            "armada.deployment=default/old.0",
            "ostree",
            f"ostree=/ostree/boot.0/default/{self.BOOT_CHECKSUM}/0",
        )
        for field in ("normalBootArgs", "diagnosticBootArgs"):
            for addition in additions:
                with self.subTest(field=field, addition=addition):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        deployment, _, parsed = self.make_tree(root)
                        parsed[field] += " " + addition
                        with self.assertRaises(ValueError):
                            producer._bind_deployment(root, deployment, parsed, self.COMMIT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment, _, parsed = self.make_tree(root)
            parsed["diagnosticBootArgs"] = parsed["diagnosticBootArgs"].replace(
                "boot.0", "boot.1"
            )
            with self.assertRaises(ValueError):
                producer._bind_deployment(root, deployment, parsed, self.COMMIT)

    def test_model_boot_args_protects_pointers_and_preserves_console(self):
        for extra in (
            "ostree",
            "ostree=/elsewhere",
            "armada.deployment",
            "armada.deployment=default/other.0",
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                producer._model_boot_args("console=ttyS0", {"extraArgs": [extra]})
        self.assertEqual(
            producer._model_boot_args("console=ttyS0", {"extraArgs": ["loglevel=7"]}),
            "console=ttyS0 loglevel=7",
        )


if __name__ == "__main__":
    unittest.main()
