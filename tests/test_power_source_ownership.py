#!/usr/bin/env python3
"""Guard the power-file ownership split with Freedom Control.

Inputs are the real checkouts and the Containerfile; no manifest is kept here,
so a newly added file cannot slip in unregistered.
"""
from __future__ import annotations

import json
import os
import shlex
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOT_FILES = REPO / "system_files"
CONTAINERFILE = REPO / "Containerfile"
CONTROL_SUBMODULE = REPO / "decky" / "freedom-control"
SOURCE_PREFIX = "decky/freedom-control/system_files"

CANONICAL = (
    "usr/libexec/armada/armada-powerd",
    "usr/lib/armada/devices/lenovo-tb323fu.conf",
    "usr/share/armada/power-profiles-SM8850.conf",
)
# Copies only the standalone control tests need: fixtures, not shipped.
FIXTURES = (
    "usr/lib/armada/armada_perf.py",
    "usr/share/armada/power-profiles.conf",
    "usr/share/armada/game-tweaks.json",
)


def find_control() -> Path:
    if not (CONTROL_SUBMODULE / "system_files").is_dir():
        raise RuntimeError(
            "Freedom Control checkout not found: the resolved COPY source "
            "decky/freedom-control has no system_files directory. Check out "
            "the control submodule or run build_files/resolve-plugin-sources.py."
        )
    return CONTROL_SUBMODULE


def logical_lines(text: str):
    """Join standard backslash continuations into logical instructions.

    Comment and blank physical lines are dropped, including inside a
    continuation, so an instruction split over several lines is parsed as one.
    """
    pending = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = stripped if pending is None else f"{pending} {stripped}"
        trailing = len(pending) - len(pending.rstrip("\\"))
        if trailing % 2:
            pending = pending[:-1]
            continue
        yield pending
        pending = None
    if pending:
        yield pending


def copy_instructions(text: str):
    for line in logical_lines(text):
        instruction = line.split(None, 1)
        if len(instruction) < 2 or instruction[0].upper() != "COPY":
            continue
        arguments = instruction[1].strip()
        while arguments.startswith("--"):
            option = arguments.split(None, 1)
            if len(option) < 2:
                arguments = ""
                break
            arguments = option[1].lstrip()
        fields = _copy_arguments(arguments)
        if len(fields) < 2:
            continue
        yield fields[:-1], fields[-1]


def _copy_arguments(arguments: str):
    """Split COPY arguments in shell or JSON form, options already removed."""
    if arguments.startswith("["):
        try:
            decoded = json.loads(arguments)
        except ValueError:
            decoded = None
        if (
            isinstance(decoded, list)
            and len(decoded) >= 2
            and all(isinstance(field, str) for field in decoded)
        ):
            return decoded
    return shlex.split(arguments)


def tree_files(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }


class PowerSourceOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control_files = find_control() / "system_files"
        cls.copies = list(
            copy_instructions(CONTAINERFILE.read_text(encoding="utf-8"))
        )

    def test_canonical_files_come_only_from_control(self):
        for relative in CANONICAL:
            with self.subTest(relative=relative):
                self.assertTrue(
                    (self.control_files / relative).is_file(),
                    f"{relative} missing from the Freedom Control checkout",
                )
                self.assertFalse(
                    os.path.lexists(ROOT_FILES / relative),
                    f"{relative} must not exist in this repository: nothing "
                    "at that path would ship",
                )
                destination = f"/system_files/{relative}"
                expected = f"{SOURCE_PREFIX}/{relative}"
                sources = [
                    source
                    for copied_sources, dest in self.copies
                    if dest == destination
                    for source in copied_sources
                ]
                self.assertIn(
                    expected,
                    sources,
                    f"Containerfile must COPY {relative} from Freedom Control "
                    "to its root path",
                )
                competing = sorted(set(sources) - {expected})
                self.assertEqual(
                    competing,
                    [],
                    f"Containerfile also COPYs {relative} from {competing} to "
                    "its root path; only Freedom Control may own that file",
                )

    def test_only_named_shared_fixtures_overlap(self):
        overlap = tree_files(self.control_files) & tree_files(ROOT_FILES)
        self.assertEqual(
            overlap - set(FIXTURES),
            set(),
            "control and system_files may share only the standalone-test "
            "fixtures; register any intentional overlap explicitly",
        )
        for relative in FIXTURES:
            self.assertTrue(
                (ROOT_FILES / relative).is_file(),
                f"{relative} is owned by this repository but missing",
            )

    def test_containerfile_never_installs_control_fixtures(self):
        for sources, dest in self.copies:
            for source in sources:
                if source != SOURCE_PREFIX and not source.startswith(
                    SOURCE_PREFIX + "/"
                ):
                    continue
                relative = source[len(SOURCE_PREFIX):].lstrip("/")
                self.assertIn(
                    relative,
                    CANONICAL,
                    f"Containerfile installs non-canonical control file "
                    f"{source} to {dest}; a broad system_files COPY counts",
                )


if __name__ == "__main__":
    unittest.main()
