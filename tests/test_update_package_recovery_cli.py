"""Subprocess coverage for the universal update descriptor CLI."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import test_update_package_recovery as fixtures  # noqa: E402

CLI = ROOT / "build_files/make-universal-update-descriptor.py"
MODEL_SETS = (("TB322FC", "TB323FU"), ("TB322FC", "TB323FU", "TB710FU"))


class UpdateDescriptorCliTests(unittest.TestCase):
    def _make(self, archive, output, offline=False):
        command = [
            sys.executable, "-B", str(CLI), "make",
            "--archive", str(archive), "--release-id", "rel-1",
            "--output", str(output),
        ]
        if offline:
            command.append("--offline-upgrade")
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)

    def test_full_recovery_default_install_and_explicit_upgrade(self):
        for models in MODEL_SETS:
            with self.subTest(models=models), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                archive = fixtures.BASE._make_bundle(
                    directory, models=models,
                    mutate=fixtures.make_recovery_mutate(models),
                )
                default_output = directory / "default.json"
                result = self._make(archive, default_output)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(default_output.read_text(encoding="utf-8"))["operations"],
                    ["install"],
                )
                upgrade_output = directory / "upgrade.json"
                result = self._make(archive, upgrade_output, offline=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(upgrade_output.read_text(encoding="utf-8"))["operations"],
                    ["install", "offline-upgrade"],
                )
                self.assertIn("not signed", result.stdout)

    def test_normal_upgrade_without_recovery_manifests(self):
        def capable(bundle, model_docs, members):
            bundle["offlineUpgrade"] = True

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            archive = fixtures.BASE._make_bundle(directory, models=MODEL_SETS[0],
                                                 mutate=capable)
            output = directory / "descriptor.json"
            result = self._make(archive, output, offline=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["operations"], ["install", "offline-upgrade"])
            self.assertEqual(document["recoveryManifests"], {})

    def test_legacy_archive_rejects_explicit_upgrade(self):
        for models in MODEL_SETS:
            with self.subTest(models=models), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                archive = fixtures.BASE._make_bundle(directory, models=models)
                output = directory / "descriptor.json"
                result = self._make(archive, output, offline=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
