import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_update_package_builder as fixture


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "build_files" / "build-edl-package.sh"
JUSTFILE = ROOT / "Justfile"


class BuildEntrypointDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.fixture = fixture.UpdatePackageBuilderTests()

    def _base_argv(self):
        script_text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        match = re.search(
            r"^descriptor_args=\(\n(?P<body>.*?)\n\)$", script_text, re.S | re.M
        )
        self.assertIsNotNone(match, "the wrapper must build the descriptor argv")
        argv = shlex.split(match.group("body"))
        self.assertIn("/source/build_files/make-universal-update-descriptor.py", argv)
        self.assertIn('"${descriptor_args[@]}"', script_text)
        self.assertIn("descriptor_args+=(--offline-upgrade)", script_text)
        self.assertIn('descriptor_args+=(--install-model "$model_id")', script_text)
        return argv

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

    def test_default_selection_descriptor_is_install_only(self):
        # The default selection includes TB710FU, so the wrapper passes no
        # --offline-upgrade and the descriptor must claim installation only.
        models = ("TB322FC", "TB323FU", "TB710FU")
        document = self._run_descriptor(models, ())
        self.assertEqual(document["operations"], ["install"])
        self.assertEqual(document["recoveryManifests"], {})
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
    """The Justfile recipe keeps its universal default and one closed release set."""

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

        module_root = str(ROOT / "system_files/usr/lib/armada")
        sys.path.insert(0, module_root)
        try:
            from updates import package_contract
        finally:
            sys.path.remove(module_root)
        cls.contract = package_contract

    def test_default_profile_is_universal_and_forwards_no_model(self):
        # CI relies on this: unset means every registered profile, unchanged.
        self.assertIn('profile="${EDL_PACKAGE_PROFILE:-universal}"', self.body)
        self.assertIn("universal) model_args=()", self.body)
        # The only model selectors belong to the closed release case.
        self.assertEqual(
            set(re.findall(r"--model ([A-Za-z0-9][A-Za-z0-9._-]*)", self.body)),
            {"TB322FC", "TB323FU"},
        )

    def test_release_profile_selects_exactly_the_upgrade_models(self):
        models = set(
            re.findall(r"--model ([A-Za-z0-9][A-Za-z0-9._-]*)", self.body)
        )
        self.assertIn(
            "release) model_args=(--model TB322FC --model TB323FU) ;;",
            self.body,
        )
        self.assertEqual(models, self.contract.OFFLINE_UPGRADE_MODELS)
        self.assertEqual(models, {"TB322FC", "TB323FU"})

    def test_unknown_profile_is_rejected_before_the_wrapper(self):
        self.assertIn("*)", self.body)
        self.assertIn("expected universal or release", self.body)

    def test_model_selection_is_quoted_for_the_wrapper(self):
        self.assertIn('${model_args[@]+"${model_args[@]}"}', self.body)


if __name__ == "__main__":
    unittest.main()
