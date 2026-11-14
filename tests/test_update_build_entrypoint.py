import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_update_package_builder as fixture


ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = ROOT / "build_files" / "make-universal-update-descriptor.py"
JUSTFILE = ROOT / "Justfile"


class BuildEntrypointDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.fixture = fixture.UpdatePackageBuilderTests()

    def _base_argv(self):
        # Exercise the real descriptor CLI contract directly; the wrapper's
        # selection is covered by test_edl_build_scripts.py so this test does
        # not duplicate the wrapper implementation.
        return [
            "python3", "-B", str(DESCRIPTOR), "make",
            "--archive", "/work/archive/$zip_name",
            "--parts-manifest", "/output/$zip_name.parts.json",
            "--release-id", "$version",
            "--output", "/output/$zip_name.update.json",
        ]

    def _run_descriptor(self, models, extra_args):
        # Exercise only the real descriptor CLI invocation, not the image build.
        directory = Path(tempfile.mkdtemp(dir=self.temporary_directory.name))
        archive = self.fixture._make_bundle(
            directory, models=models,
            mutate=lambda bundle, docs, members: bundle.update(offlineUpgrade=True),
        )
        split_dir = directory / "split"
        split_dir.mkdir()
        _, manifest = fixture._split_to_manifest(
            archive, split_dir, part_bytes=300
        )
        archive_before = archive.read_bytes()

        replacements = []
        for argument in self._base_argv() + list(extra_args):
            argument = argument.replace("$zip_name", archive.name)
            argument = argument.replace("$version", "2026.04.0")
            for prefix, destination in (
                ("/source/", ROOT),
                ("/work/archive/", archive.parent),
                ("/output/", split_dir),
            ):
                if argument.startswith(prefix):
                    argument = str(destination / argument[len(prefix):])
                    break
            replacements.append(argument)

        environment = os.environ.copy()
        python_path = [str(ROOT), str(ROOT / "build_files")]
        if environment.get("PYTHONPATH"):
            python_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
        result = subprocess.run(
            [sys.executable, *replacements[1:]],
            capture_output=True,
            text=True,
            timeout=60,
            env=environment,
        )
        self.assertEqual(
            result.returncode, 0, msg=result.stdout + result.stderr
        )

        output = split_dir / f"{archive.name}.update.json"
        self.assertTrue(output.is_file())
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["archive"]["bytes"], archive.stat().st_size)
        self.assertEqual(document["parts"], manifest["parts"])
        self.assertEqual(document["sourceRevision"], fixture.REV)
        self.assertEqual(document["ostreeCommit"], fixture.COMMIT)
        self.assertEqual(document["ociDigest"], fixture.DIGEST)
        self.assertEqual(archive.read_bytes(), archive_before)
        return document

    def test_full_configuration_descriptor_declares_install_and_upgrade(self):
        # The single universal package covers every registered model. The
        # descriptor advertises upgrade for the Y700 models but the operation
        # gate rejects a TB710FU upgrade before execution.
        models = tuple(json.loads(
            fixture.CONFIG_PATH.read_text(encoding="utf-8"))["profiles"])
        document = self._run_descriptor(models, ("--offline-upgrade",))
        self.assertEqual(document["operations"], ["install", "offline-upgrade"])
        self.assertEqual(document["recoveryManifests"], {})
        self.assertEqual(document["supportedModels"], list(models))
        self.assertEqual(fixture.validate_package(document), document)

        for model in models:
            fixture.require_operation(document, model, "install",
                                      installer_version="0.10.0")
        for model in sorted(fixture.OFFLINE_UPGRADE_MODELS):
            fixture.require_operation(document, model, "offline-upgrade",
                                      updater_version="0.10.0")
        with self.assertRaisesRegex(
                fixture.PackageError, r"(?i)(offline-upgrade|dual-slot|upgrade)"):
            fixture.require_operation(document, "TB710FU", "offline-upgrade",
                                      updater_version="0.10.0")

    def test_explicit_install_only_full_selection_still_valid(self):
        models = ("TB322FC", "TB323FU", "TB710FU")
        document = self._run_descriptor(models, ())
        self.assertEqual(document["operations"], ["install"])
        self.assertEqual(document["supportedModels"], list(models))
        self.assertEqual(fixture.validate_package(document), document)

    def test_compatible_models_descriptor_keeps_upgrade(self):
        # A package limited to the dual-slot models may still advertise upgrade.
        models = ("TB322FC", "TB323FU")
        document = self._run_descriptor(models, ("--offline-upgrade",))
        self.assertEqual(document["operations"], ["install", "offline-upgrade"])
        self.assertEqual(document["recoveryManifests"], {})
        self.assertEqual(document["supportedModels"], list(models))
        self.assertEqual(fixture.validate_package(document), document)

    def test_custom_model_uses_the_explicit_install_only_allowance(self):
        models = ("TB322FC", fixture.CUSTOM_MODEL)
        document = self._run_descriptor(
            models, ("--install-model", fixture.CUSTOM_MODEL)
        )
        self.assertEqual(document["operations"], ["install"])
        self.assertEqual(document["supportedModels"], list(models))
        self.assertEqual(
            fixture.validate_package(
                document, install_models=(fixture.CUSTOM_MODEL,)
            ),
            document,
        )


class BuildRecipeProfileTests(unittest.TestCase):
    """The Justfile builds one universal package with no model selection."""

    @classmethod
    def setUpClass(cls):
        text = JUSTFILE.read_text(encoding="utf-8").replace("\r\n", "\n")
        match = re.search(
            r"^build-edl-package [^\n]*:\n(?P<body>(?:(?:[ \t]+.*)?\n)*)",
            text,
            re.MULTILINE,
        )
        if match is None:
            raise AssertionError("the Justfile must define build-edl-package")
        cls.body = match.group("body")

    def test_recipe_has_no_profile_or_model_selection(self):
        self.assertNotIn("EDL_PACKAGE_PROFILE", self.body)
        self.assertNotIn("--model ", self.body)
        self.assertIn("build_files/build-edl-package.sh", self.body)

    def test_recipe_forwards_no_variant_specific_arguments(self):
        self.assertNotIn("release)", self.body)
        self.assertNotIn("model_args", self.body)
        self.assertIn('--output "${output}"', self.body)


if __name__ == "__main__":
    unittest.main()
