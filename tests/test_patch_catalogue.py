"""Catalogue checks for the kernel and gamescope-session patch packages.

The tests are intentionally limited to the documented contract in each
package's PATCHES.md: an active series entry must have one catalogue record
with a source, every patch file that is not in the series must have a
catalogue record that says so, and the gamescope-session records must name
the files that actually exist.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RECORD_START = re.compile(r"^- `(?P<path>[^`]+)`\s*$")
UPSTREAM_PREFIXES = ("http", "local", "unknown", "not submitted")


def parse_catalogue(path):
    """Return {repo-relative path: {source, upstream, notes}} in file order."""
    records = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = RECORD_START.match(raw)
        if match:
            name = match.group("path")
            if name in records:
                raise AssertionError(f"duplicate catalogue record for {name}")
            current = {"source": "", "upstream": "", "notes": ""}
            records[name] = current
            continue
        if current is None or not raw.startswith("  "):
            continue
        key, separator, value = raw.strip().partition(":")
        if separator and key in current:
            current[key] = value.strip()
    return records


def parse_series(path):
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            entries.append(entry)
    return entries


class KernelCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.package = ROOT / "packages" / "kernel"
        self.patch_dir = self.package / "patches"
        self.records = parse_catalogue(self.package / "PATCHES.md")
        self.active = parse_series(self.patch_dir / "series")

    def test_active_series_entries_are_documented(self):
        for entry in self.active:
            documented = f"patches/{entry}"
            self.assertIn(
                documented,
                self.records,
                f"{entry} is active in series but has no catalogue record",
            )
            self.assertTrue(
                self.records[documented]["source"],
                f"{documented} has no source",
            )

    def test_upstream_values_are_recognisable(self):
        for name, record in self.records.items():
            upstream = record["upstream"]
            if not upstream:
                continue
            self.assertTrue(
                upstream.startswith(UPSTREAM_PREFIXES),
                f"{name} has an unrecognised upstream value: {upstream}",
            )

    def test_inactive_documented_patches_are_marked(self):
        active = set(self.active)
        for path in sorted(self.patch_dir.glob("*.patch")):
            if path.name in active:
                continue
            documented = f"patches/{path.name}"
            self.assertIn(
                documented,
                self.records,
                f"{path.name} is present in patches but has no catalogue record",
            )
            self.assertIn(
                "not enabled",
                self.records[documented]["notes"].lower(),
                f"{documented} is present but not in series and is not marked "
                "as not enabled",
            )


class GamescopeCatalogueTest(unittest.TestCase):
    def setUp(self):
        self.package = ROOT / "packages" / "gamescope-session"
        self.patch_dir = self.package / "patches"
        self.records = parse_catalogue(self.package / "PATCHES.md")

    def test_documented_files_match_the_patch_directory(self):
        actual = {path.name for path in self.patch_dir.glob("*.patch")}
        documented = {
            name.split("/", 1)[1]
            for name in self.records
            if name.startswith("patches/")
        }
        self.assertEqual(
            actual,
            documented,
            "gamescope-session PATCHES.md does not match the patch files",
        )
        for name in documented:
            self.assertTrue(
                self.records[f"patches/{name}"]["source"],
                f"patches/{name} has no source",
            )


if __name__ == "__main__":
    unittest.main()
