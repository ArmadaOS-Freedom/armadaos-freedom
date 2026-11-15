#!/usr/bin/python3
"""Run the shell and dashed-script test suites under tests/.

Discovery is every ``tests/*-test.sh`` and ``tests/*-test.py`` file, sorted.
The regular unit group runs on an ordinary host. The privileged group runs in
a disposable root container; its members are listed in
``tests/script-test-groups.json`` together with the tests that need a
manager-supplied argument contract or a separate upstream source tree.

The same file may supply per-test arguments and environment entries, so a test
that requires an option or a container marker is still run by this runner
instead of being demoted to a skip.

``TEST_SCRIPT_ARTIFACT_ROOT`` (or ``TEST_ARTIFACT_ROOT``) must name a writable
directory; every test execution gets a fresh short temporary directory created
directly under it with ``tempfile.mkdtemp``. Earlier directories are left in
place, so repeated runs against the same artifact root never share scratch
space. The short name matters: several tests create AF_UNIX sockets inside
``TMPDIR`` and the 108-byte sun_path limit is exceeded by an artifact path that
repeats the full test filename.

Exit status 77 means "skipped: precondition unavailable" and is reported as a
skip. Any other non-zero status is a failure. Exceptions, import errors and
missing dependencies are never turned into skips or passes.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
GROUPS_FILE = TESTS_DIR / "script-test-groups.json"
SKIP_STATUS = 77
ARTIFACT_ENV = ("TEST_SCRIPT_ARTIFACT_ROOT", "TEST_ARTIFACT_ROOT")


def discover():
    files = sorted(TESTS_DIR.glob("*-test.sh")) + sorted(TESTS_DIR.glob("*-test.py"))
    return sorted(files, key=lambda path: path.name)


def load_groups():
    data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    privileged = {str(item) for item in data.get("privileged", [])}
    manager_only = {str(key): str(value) for key, value in
                    data.get("manager_only", {}).items()}
    arguments = {str(key): [str(value) for value in values]
                 for key, values in data.get("arguments", {}).items()}
    environment = {str(key): {str(name): str(value) for name, value in values.items()}
                   for key, values in data.get("environment", {}).items()}
    overlap = privileged & set(manager_only)
    if overlap:
        raise SystemExit(
            "error: files classified twice: " + ", ".join(sorted(overlap)))
    for relative in sorted(privileged | set(manager_only)):
        if not (REPO_ROOT / relative).is_file():
            raise SystemExit(f"error: classified test is missing: {relative}")
    for relative in sorted(set(arguments) | set(environment)):
        if not (REPO_ROOT / relative).is_file():
            raise SystemExit(f"error: configured test is missing: {relative}")
        if relative in manager_only:
            raise SystemExit(
                f"error: {relative} is manager-only and cannot take arguments "
                "or environment")
    return privileged, manager_only, arguments, environment


def classify(files, privileged):
    classified = {}
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        classified[relative] = "privileged" if relative in privileged else "unit"
    return classified


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run the shell and dashed-script tests under tests/.")
    parser.add_argument(
        "--group", choices=("all", "unit", "privileged"), default="all",
        help="which group to execute (default: all)")
    parser.add_argument(
        "--list", action="store_true",
        help="print the discovered files and their group without running them")
    return parser.parse_args(argv)


def artifact_root():
    for name in ARTIFACT_ENV:
        value = os.environ.get(name)
        if value:
            path = Path(value)
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SystemExit(
                    f"error: {name}={value!r} is not a usable directory: {exc}")
            return path
    raise SystemExit(
        "error: set TEST_SCRIPT_ARTIFACT_ROOT (or TEST_ARTIFACT_ROOT) to a "
        "writable directory; the runner creates its short per-test temporary "
        "directories there")


def command_for(path, work, arguments=()):
    # The only placeholder is the literal "{work}": it names this test's own
    # short temporary directory, so a configured argument can point a test at
    # scratch space without the runner setting up a shell or a generic template.
    arguments = [argument.replace("{work}", str(work)) for argument in arguments]
    if path.suffix == ".sh":
        return ["bash", str(path), *arguments]
    return [sys.executable, "-B", str(path), *arguments]


def main(argv=None):
    args = parse_args(argv)
    files = discover()
    if not files:
        print("error: no tests/*-test.sh or tests/*-test.py files found",
              file=sys.stderr)
        return 1

    privileged, manager_only, arguments, environment = load_groups()
    classified = classify(files, privileged)

    listed = [
        path for path in files
        if args.group == "all" or classified[path.relative_to(REPO_ROOT).as_posix()] == args.group
    ]

    if args.list:
        print(f"Discovered {len(files)} script tests")
        for path in files:
            relative = path.relative_to(REPO_ROOT).as_posix()
            note = ""
            if relative in manager_only:
                note = "  [manager-only: " + manager_only[relative] + "]"
            print(f"  {classified[relative]:10s} {relative}{note}")
        return 0

    artifacts = artifact_root()
    os.chdir(REPO_ROOT)
    passed = skipped = 0
    failed = []
    not_run = []

    for index, path in enumerate(listed, start=1):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in manager_only:
            not_run.append((relative, manager_only[relative]))
            print(f"NOT RUN: {relative} - {manager_only[relative]}")
            continue

        # A fresh short directory directly under the artifact root keeps
        # AF_UNIX paths inside sun_path and keeps consecutive runs from
        # reusing a work directory that already holds the previous run's
        # fixtures. Earlier directories are deliberately preserved.
        work = Path(tempfile.mkdtemp(prefix=f"{index:02d}", dir=artifacts))
        extra = arguments.get(relative, ())
        env = dict(os.environ)
        env.update(environment.get(relative, {}))
        env["TMPDIR"] = str(work)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPYCACHEPREFIX"] = str(work / "pycache")
        command = command_for(path, work, extra)
        # command_for expands {work} in the trailing arguments; show what will run.
        suffix = " " + " ".join(command[-len(extra):]) if extra else ""
        print(f"Running {classified[relative]} test: {relative}{suffix}")
        result = subprocess.run(
            command, cwd=REPO_ROOT, env=env)
        if result.returncode == 0:
            passed += 1
            print(f"PASS: {relative}")
        elif result.returncode == SKIP_STATUS:
            skipped += 1
            print(f"Skipped test: {relative} (exit 77)")
        else:
            failed.append((relative, result.returncode))
            print(f"FAIL: {relative} exited {result.returncode}", file=sys.stderr)

    print(
        f"Executed {len(listed) - len(not_run)} script tests: {passed} passed, "
        f"{skipped} skipped, {len(failed)} failed, {len(not_run)} not run")
    for relative, reason in not_run:
        print(f"  not run: {relative} - {reason}")
    for relative, status in failed:
        print(f"  failed: {relative} (exit {status})", file=sys.stderr)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
