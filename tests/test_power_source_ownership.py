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
# Stages whose root becomes the shipped power tree.  armada-rootfs and armada
# are the image stages, so a COPY here writes the runtime tree directly;
# update-boot-tool is a scratch provider whose root is exported into the image's
# /system_files, so a protected path written there lands on the shipped file
# just the same.  Protected files must arrive through the checked ctx stage
# instead, whatever source such a COPY names.
SHIPPED_ROOT_STAGES = ("armada-rootfs", "armada", "update-boot-tool")


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
# The ctx stage assembles the shipped tree under this single root.  Two imports
# seed it whole, so their destination is a strict ancestor of every protected
# path; only these exact sources may do that, because the guard cannot see which
# files a directory COPY carries.
CTX_TREE = "/system_files"
CTX_BASE_IMPORTS = (
    (None, "system_files"),
    ("update-boot-tool", "/"),
)

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
    A --from reference must name a stage: a numeric index is resolved by
    position, so what it refers to depends on the order of the stages, and
    this guard requires a named reference it can compare directly.

    A destination in the ctx stage must be absolute.  The guard does not
    evaluate WORKDIR, so it needs an absolute destination to compare and
    project the protected paths below; it cannot infer which directory a
    relative destination would land in.  Other, unrelated builder stages keep
    relative destinations, which are meaningful against their own root.

    A destination in a shipped-root stage must be absolute too, and it must not
    equal or contain a protected power path: those files are assembled through
    the checked ctx stage, never overlaid directly on the shipped tree.  The
    check is on the destination alone, so it holds whatever source such a COPY
    names, including one staged elsewhere.  update-boot-tool is covered as well
    because its root is exported into the image's /system_files, so a protected
    path there overwrites the shipped file exactly the same.  Relative
    destinations in these stages are unsupported for the same WORKDIR reason.
    The tolerated builder forms (a ./ destination and the boot-tool variable
    seed) are unaffected.

    In the ctx stage the destination must not be a directory ancestor of a
    protected path either, since the guard cannot see what a directory COPY
    brings with it.  Two imports seed the whole tree and are allowed by exact
    source: this repository's own system_files from the build context, and the
    provider's exported root.  A ctx COPY that targets a fixture path directly
    must likewise come from this repository's own system_files, so a stage or
    alias source cannot install content the guard cannot trace; the canonical
    files keep their separate Control ownership check.

    A ctx destination that normalizes onto a protected file must also be
    spelled as that file.  Normalizing drops a trailing slash, so
    /system_files/usr/lib/armada/x/ would compare equal to the protected file x
    while Docker still treats it as a directory and lands the source inside it,
    leaving the required file path unwritten.  The raw destination must name a
    file leaf, so such a COPY is rejected before the path comparison.
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
        copied = Copy(stage, from_stage, tuple(sources), dest)
        landing = normalize(dest)
        if stage is not None and stage.casefold() == CTX_STAGE:
            if not posixpath.isabs(dest):
                raise ValueError(
                    "ownership guard requires an absolute COPY destination in "
                    f"the ctx stage; {dest} is relative, and the guard does not "
                    "evaluate WORKDIR, so it cannot compare or project "
                    "protected paths against a relative destination: use an "
                    "absolute path such as /decky"
                )
            covered = [
                normalize(f"{CTX_TREE}/{relative}")
                for relative in CANONICAL + FIXTURES
                if landing == "/"
                or normalize(f"{CTX_TREE}/{relative}").startswith(landing + "/")
            ]
            if covered and not _ctx_base_import(copied):
                raise ValueError(
                    "ownership guard forbids a ctx COPY onto directory "
                    f"{landing} from {source_origin(copied)}: it would overlay "
                    f"protected power path {covered[0]} with files the guard "
                    "cannot check.  Only this repository's system_files and the "
                    "provider's exported root may seed the whole tree; otherwise "
                    "COPY the protected files themselves from the owning tree"
                )
            # A protected file must be addressed as a file.  normalize() drops
            # the trailing slash, so a directory spelling would otherwise match
            # the leaf while Docker lands the source inside it: basename is ''
            # for a trailing slash and '.'/'..' for a dot leaf.
            is_protected_file = landing in {
                normalize(f"{CTX_TREE}/{relative}")
                for relative in CANONICAL + FIXTURES
            }
            if is_protected_file and posixpath.basename(dest) in ("", ".", ".."):
                raise ValueError(
                    "ownership guard requires a file-form COPY destination for "
                    f"protected file {landing}: {dest} names a directory, so "
                    "Docker would place the source inside it instead of at the "
                    "protected file path; drop the trailing slash and name the "
                    "file"
                )
            for relative in FIXTURES:
                if landing != normalize(f"{CTX_TREE}/{relative}"):
                    continue
                expected = f"system_files/{relative}"
                owned = copied.from_stage is None and tuple(
                    context_identity(source) for source in copied.sources
                ) == (expected,)
                if not owned:
                    raise ValueError(
                        "ownership guard forbids installing the fixture "
                        f"{relative} in the ctx stage from "
                        f"{source_origin(copied)}: only this repository's own "
                        f"{expected} may land there, so the root-owned file "
                        "stays the shipped one"
                    )
        if stage is not None and stage.casefold() in SHIPPED_ROOT_STAGES:
            if not posixpath.isabs(dest):
                raise ValueError(
                    "ownership guard requires an absolute COPY destination in "
                    f"stage {stage}; {dest} is relative and the guard does not "
                    "evaluate WORKDIR, so it cannot tell which path would be "
                    "overlaid"
                )
            for relative in CANONICAL + FIXTURES:
                protected = normalize("/" + relative)
                if (
                    landing == "/"
                    or landing == protected
                    or protected.startswith(landing + "/")
                ):
                    raise ValueError(
                        "ownership guard forbids overlaying protected power "
                        f"path {protected} in stage {stage}, whose root becomes "
                        f"the shipped tree: COPY destination {dest} covers it, "
                        "so the file must be assembled through the checked ctx "
                        "stage instead"
                    )
        yield copied


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
    """Collapse . and .. lexically; the filesystem is never touched.

    posixpath keeps exactly '//' as a distinct POSIX spelling, but a Linux
    container path has a single root, so //system_files and /system_files are
    the same destination.  Collapse the repeated leading slash; relative paths
    and the single-root case are unchanged.
    """
    normalized = posixpath.normpath(path)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


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


def _ctx_base_import(copied: Copy) -> bool:
    """Whether a ctx COPY is one of the two allowed whole-tree seeds.

    Both land on the ctx tree root, a strict ancestor of every protected path,
    so they cannot be allowed by destination alone: the source must be exactly
    this repository's system_files, read from the build context, or the
    provider's root overlay read from update-boot-tool.  A single source is
    required so a second argument cannot slip past the comparison.
    """
    if normalize(copied.dest) != CTX_TREE or len(copied.sources) != 1:
        return False
    origin = None if copied.from_stage is None else copied.from_stage.casefold()
    identity = (
        context_identity(copied.sources[0])
        if origin is None
        else normalize(copied.sources[0])
    )
    return any(
        origin == stage and identity == source
        for stage, source in CTX_BASE_IMPORTS
    )


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
        # including read back out of the ctx stage).  Projecting that onto the
        # protected paths needs an absolute destination: the guard does not
        # resolve WORKDIR, so a relative destination is rejected as input it
        # cannot evaluate.  That is a requirement on the guard's input, not a
        # Docker restriction.
        for copied in self.copies:
            for source in copied.sources:
                directory = context_source(copied, source)
                if directory is None:
                    continue
                if directory != "." and not SOURCE_PREFIX.startswith(
                    directory + "/"
                ):
                    continue
                self.assertTrue(
                    posixpath.isabs(copied.dest),
                    f"Control ancestor COPY {source} {copied.dest} from "
                    f"{source_origin(copied)} into stage {copied.stage} "
                    "requires an absolute destination to project the Control "
                    "contents onto the stage root",
                )
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


class ParserPolicyRegressionTests(unittest.TestCase):
    """API regressions for COPY policy; literal snippets, no fixtures on disk."""

    # Every one of these was silently accepted before the ownership gates.
    REJECTED = (
        (
            "ctx-staged control file overlaid on shipped tree",
            "FROM ubuntu AS ctx\n"
            "COPY decky/freedom-control /control-staging\n"
            "FROM ubuntu AS armada-rootfs\n"
            "COPY --from=ctx /control-staging/system_files/usr/lib/armada/"
            "armada_perf.py /usr/lib/armada/armada_perf.py\n",
        ),
        (
            "direct rooted copy of the shipped daemon",
            "FROM ubuntu AS armada-rootfs\n"
            "COPY tests/infinity-scheduler-test.py "
            "/usr/libexec/armada/armada-powerd\n",
        ),
        (
            "unknown directory seeded over a protected subtree",
            "FROM ubuntu AS ctx\n"
            "COPY --from=boot-tool-builder /replacement/ "
            "/system_files/usr/libexec/armada/\n",
        ),
        (
            "canonical control daemon addressed as a directory",
            "FROM ubuntu AS ctx\n"
            "COPY decky/freedom-control/system_files/usr/libexec/armada/"
            "armada-powerd /system_files/usr/libexec/armada/armada-powerd/\n",
        ),
        (
            "fixture installed in ctx from a foreign stage",
            "FROM ubuntu AS ctx\n"
            "COPY --from=some-stage /tmp/armada_perf.py "
            "/system_files/usr/lib/armada/armada_perf.py\n",
        ),
        (
            "boot-tool scratch root writing a protected file",
            "FROM ubuntu AS update-boot-tool\n"
            "COPY build/random.py /usr/lib/armada/armada_perf.py\n",
        ),
        (
            "runtime copy into a protected directory",
            "FROM ubuntu AS armada-rootfs\n"
            "COPY build/armada/ /usr/lib/armada/\n",
        ),
    )

    ACCEPTED = (
        (
            "both ctx tree seeds plus a benign Control staging copy",
            "FROM ubuntu AS ctx\n"
            "COPY system_files /system_files\n"
            "COPY --from=update-boot-tool / /system_files\n"
            "COPY decky/freedom-control /control-staging\n",
        ),
        (
            "unrelated runtime path and the owned fixture install",
            "FROM ubuntu AS armada-rootfs\n"
            "COPY build/readme.md /opt/guard-example/README.md\n"
            "FROM ubuntu AS ctx\n"
            "COPY system_files/usr/lib/armada/armada_perf.py "
            "/system_files/usr/lib/armada/armada_perf.py\n",
        ),
    )

    def test_copy_policy_rejections_and_acceptances(self):
        for label, text in self.REJECTED:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    list(copy_instructions(text))
        for label, text in self.ACCEPTED:
            with self.subTest(label=label):
                self.assertTrue(list(copy_instructions(text)))


if __name__ == "__main__":
    unittest.main()
