import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_update_package_builder as fixture


ROOT = Path(__file__).resolve().parents[1]


class BuildEntrypointDescriptorTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.fixture = fixture.UpdatePackageBuilderTests()

    def test_descriptor_invocation_matches_real_cli(self):
        # Exercise only the real descriptor CLI invocation, not the image build.
        directory = Path(self.temporary_directory.name)
        models = ("TB322FC", "TB323FU", "TB710FU")
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

        script = ROOT / "build_files" / "build-edl-package.sh"
        marker = "/source/build_files/make-universal-update-descriptor.py"
        lines = [
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if "run_container" in line and marker in line
        ]
        self.assertEqual(len(lines), 1)
        tokens = shlex.split(lines[0])
        argv = tokens[tokens.index("python3"):]

        replacements = []
        for argument in argv:
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
        package = fixture.validate_package(json.loads(output.read_text(encoding="utf-8")))
        self.assertEqual(package["operations"], ["install", "offline-upgrade"])
        self.assertEqual(package["recoveryManifests"], {})
        self.assertEqual(package["supportedModels"], list(models))
        self.assertEqual(package["sourceRevision"], fixture.REV)
        self.assertEqual(package["ostreeCommit"], fixture.COMMIT)
        self.assertEqual(package["ociDigest"], fixture.DIGEST)
        self.assertEqual(package["parts"], manifest["parts"])
        self.assertEqual(archive.read_bytes(), archive_before)


if __name__ == "__main__":
    unittest.main()
