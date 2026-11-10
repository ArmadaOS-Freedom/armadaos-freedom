# tests/export-update-package-interop.py
"""Generate cross-repo interop fixtures: tiny update archives + descriptors.

Runs the actual descriptor CLI twice (single-archive and split-manifest) so the
Windows consumer tests consume what the real producer emits. Outputs are left in
place for check-in; the script refuses to overwrite.
"""
import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _import_fixture():
    spec = importlib.util.spec_from_file_location(
        "test_update_package_builder", TESTS / "test_update_package_builder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="new unique output directory (must not exist)")
    args = parser.parse_args()
    output = args.output
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")
    output.mkdir(parents=True)

    fixture_module = _import_fixture()
    fixture = fixture_module.UpdatePackageBuilderTests()

    models = ("TB322FC", "TB323FU", "TB710FU")
    archive = fixture._make_bundle(output, models=models)
    print(f"archive: {archive}")

    split_dir = output / "split"
    split_dir.mkdir()
    manifest_path, _manifest = fixture_module._split_to_manifest(
        archive, split_dir, part_bytes=4096)
    print(f"parts manifest: {manifest_path}")

    cli = TESTS.parent / "build_files" / "make-universal-update-descriptor.py"
    release_id = "interop-2026.04.0"

    # Single archive: descriptor lands next to the archive, no parts manifest.
    subprocess.run(
        [sys.executable, str(cli), "make",
         "--archive", str(archive),
         "--release-id", release_id,
         "--output", str(archive.with_name(archive.name + ".update.json"))],
        check=True)

    # Split archive: descriptor inside split/ referencing the parts manifest.
    subprocess.run(
        [sys.executable, str(cli), "make",
         "--archive", str(archive),
         "--release-id", release_id,
         "--parts-manifest", str(manifest_path),
         "--output", str(split_dir / (archive.name + ".update.json"))],
        check=True)

    print("outputs (single):", sorted(p.name for p in output.iterdir() if p.is_file()))
    print("outputs (split):", sorted(p.name for p in split_dir.iterdir()))
    print("NOTE: descriptors are unsigned/synthetic; signing, keys, and network "
          "are out of scope here. Crypto is covered by separate tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
