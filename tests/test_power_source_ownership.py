#!/usr/bin/env python3
"""Guard the power-file ownership split with Freedom Control.

Inputs are the real checkouts and the Containerfile; no manifest is kept here,
so a newly added file cannot slip in unregistered.
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex
import unittest
from collections import namedtuple
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


Copy = namedtuple("Copy", "stage from_stage sources dest")
CTX_STAGE = "ctx"

# The one tolerated variable COPY source.  It seeds the boot-tool builder from
# a resolved tree this guard never compares, so it cannot hide a control file.
KNOWN_VARIABLE_SOURCE = "${BOOT_TOOL_SOURCE}/"
KNOWN_VARIABLE_STAGE = "boot-tool-builder"


def _known_variable_source(stage, from_stage, source, dest):
    """Whether a variable source is the single tolerated boot-tool seed."""
    return (
        source == KNOWN_VARIABLE_SOURCE
        and from_stage is None
        and dest == "./"
        and stage is not None
        and stage.casefold() == KNOWN_VARIABLE_STAGE
    )


def stage_instruction(arguments: str):
    """Return the stage alias from FROM arguments, or the image name."""
    fields = arguments.split()
    for index, field in enumerate(fields[:-1]):
        if field.upper() == "AS":
            return fields[index + 1]
    return fields[-1] if fields else None


def copy_options(arguments: str):
    """Strip COPY options, returning the --from stage and remaining fields."""
    from_stage = None
    while arguments.startswith("--"):
        option = arguments.split(None, 1)
        if len(option) < 2:
            return from_stage, ""
        flag, remainder = option
        arguments = remainder.lstrip()
        name, has_value, value = flag.partition("=")
        if name.upper() == "--FROM":
            if has_value:
                from_stage = value
            else:
                from_stage, _, arguments = arguments.partition(" ")
                arguments = arguments.lstrip()
    return from_stage, arguments


def copy_instructions(text: str):
    """Yield COPY instructions with the stage they belong to.

    A COPY's sources are build-context paths only when --from is absent;
    otherwise they name paths inside another stage or image.

    Sources and destinations must be explicit.  Variables are not expanded
    here, so an unresolved "$" would make the path this guard compares differ
    from the path Docker copies; only the known boot-tool seed is tolerated and
    every other variable is rejected.  A wildcard source could likewise slip
    past the ownership checks, so any source containing *, ? or [ is rejected.
    A --from reference must name a stage, because a numeric index silently
    points at whichever stage happens to come first.
    """
    stage = None
    for line in logical_lines(text):
        instruction = line.split(None, 1)
        keyword = instruction[0].upper()
        if keyword == "FROM":
            stage = (
                stage_instruction(instruction[1].strip())
                if len(instruction) > 1
                else None
            )
            continue
        if keyword != "COPY" or len(instruction) < 2:
            continue
        from_stage, arguments = copy_options(instruction[1].strip())
        if from_stage is not None and from_stage.isdigit():
            raise ValueError(
                "ownership guard requires COPY --from to name a stage; "
                f"numeric stage index {from_stage} is unsupported: use the "
                "stage alias instead"
            )
        fields = _copy_arguments(arguments)
        if len(fields) < 2:
            continue
        sources, dest = fields[:-1], fields[-1]
        for source in sources:
            if "$" in source and not _known_variable_source(
                stage, from_stage, source, dest
            ):
                raise ValueError(
                    "ownership guard requires explicit COPY source paths; "
                    f"unresolved variable in COPY source {source}: the guard "
                    "does not expand it, so use a literal build-context path "
                    f"(only {KNOWN_VARIABLE_SOURCE} from stage "
                    f"{KNOWN_VARIABLE_STAGE} is supported)"
                )
            if any(character in source for character in "*?["):
                raise ValueError(
                    "ownership guard requires explicit COPY source paths; "
                    f"wildcard COPY sources are unsupported: {source}"
                )
        if "$" in dest:
            raise ValueError(
                "ownership guard requires explicit COPY destination paths; "
                f"unresolved variable in COPY destination {dest}: use a "
                "literal path"
            )
        yield Copy(stage, from_stage, tuple(sources), dest)


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


def normalize(path: str) -> str:
    """Collapse . and .. lexically; the filesystem is never touched."""
    return posixpath.normpath(path)


def context_identity(source: str) -> str:
    """Return the build-context path a literal source addresses, or its root.

    Docker resolves build-context sources against the context root, discarding
    a leading slash and any parent traversal.  Pinning each source at that root
    makes /decky/x and ../decky/x the same path as decky/x, so the checks below
    see the file Docker would actually copy.  This is lexical, not realpath.
    """
    return normalize("/" + source).lstrip("/") or "."


def in_ctx(copied: Copy) -> bool:
    return copied.stage is not None and copied.stage.casefold() == CTX_STAGE


def context_source(copied: Copy, source: str):
    """Return the build-context path a COPY source addresses, or None.

    A COPY without --from already names a build-context path.  The ctx stage
    holds a copy of the build context, so its sources address that same tree
    from that stage root; any other stage has its own filesystem and its paths
    say nothing about this repository.
    """
    if copied.from_stage is None or copied.from_stage.casefold() == CTX_STAGE:
        return context_identity(source)
    return None


def source_origin(copied: Copy) -> str:
    """Name the stage a COPY reads from, for diagnostics."""
    return (
        "the build context"
        if copied.from_stage is None
        else f"--from {copied.from_stage}"
    )


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
                destination = normalize(f"/system_files/{relative}")
                expected = context_identity(f"{SOURCE_PREFIX}/{relative}")
                installed = [
                    (context_identity(source), copied.from_stage)
                    for copied in self.copies
                    if in_ctx(copied) and normalize(copied.dest) == destination
                    for source in copied.sources
                ]
                self.assertIn(
                    expected,
                    [
                        source
                        for source, from_stage in installed
                        if from_stage is None
                    ],
                    f"Containerfile must COPY {relative} from the build "
                    "context, sourced from Freedom Control, inside the ctx "
                    "stage",
                )
                competing = sorted(
                    {
                        source
                        if from_stage is None
                        else f"--from {from_stage} {source}"
                        for source, from_stage in installed
                        if not (from_stage is None and source == expected)
                    }
                )
                self.assertEqual(
                    competing,
                    [],
                    f"Containerfile also writes {relative} from {competing} "
                    "to its root path in the ctx stage; only Freedom Control "
                    "may own that file",
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
        for copied in self.copies:
            for source in copied.sources:
                normalized = context_source(copied, source)
                if normalized is None:
                    continue
                if normalized != SOURCE_PREFIX and not normalized.startswith(
                    SOURCE_PREFIX + "/"
                ):
                    continue
                relative = normalized[len(SOURCE_PREFIX):].lstrip("/")
                self.assertIn(
                    relative,
                    CANONICAL,
                    f"Containerfile installs non-canonical control file "
                    f"{source} from {source_origin(copied)} to {copied.dest} "
                    f"in stage {copied.stage}; a broad system_files COPY "
                    "counts",
                )

        # A directory COPY from an ancestor of the control tree copies its
        # contents, so a fixture can land on its shipped path without the
        # source naming system_files at all (e.g. decky/freedom-control to /,
        # including read back out of the ctx stage).
        for copied in self.copies:
            for source in copied.sources:
                directory = context_source(copied, source)
                if directory is None:
                    continue
                if directory != "." and not SOURCE_PREFIX.startswith(
                    directory + "/"
                ):
                    continue
                for relative in FIXTURES:
                    projected = normalize(
                        posixpath.join(
                            copied.dest,
                            posixpath.relpath(
                                f"{SOURCE_PREFIX}/{relative}", directory
                            ),
                        )
                    )
                    self.assertNotEqual(
                        projected,
                        normalize(f"/system_files/{relative}"),
                        f"Containerfile COPY {source} {copied.dest} from "
                        f"{source_origin(copied)} into stage {copied.stage} "
                        f"overwrites the shipped fixture {relative} at "
                        f"{projected}",
                    )


if __name__ == "__main__":
    unittest.main()
