#!/usr/bin/python3
"""Run the automatic unit suite from tests/test_*.py.

Discovery is plain unittest over every tests/test_*.py file. Exactly one test
is classified as an integration test because it needs a privileged container:
the real ext4/btrfs loop-mount pair. It is reported by its exact name and
stays out of the default run; --integration includes it.

Existing skips are left alone: they still report as skipped, never as passes,
and a real test failure or import error still fails the run.
"""

import argparse
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# Only this exact test needs root plus loop/mount support. Nothing else is
# filtered, and no error is downgraded to a skip or a success.
INTEGRATION_TESTS = {
    "test_edl_raw_package.ProducerContractTests."
    "test_filesystem_pair_real_ext4_and_btrfs_images": (
        "requires root, /dev/loop-control and mount/btrfs/loop support"
    ),
}


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Run the automatic unit tests.")
    parser.add_argument(
        "--integration",
        action="store_true",
        help="also run tests that need root and loop/mount privileges",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    os.chdir(REPO_ROOT)
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(TESTS_DIR),
        pattern="test_*.py",
        top_level_dir=str(TESTS_DIR),
    )
    tests = list(flatten(suite))
    if not tests:
        print("error: no tests found under tests/test_*.py", file=sys.stderr)
        return 1

    integration = [test for test in tests if test.id() in INTEGRATION_TESTS]
    found = {test.id() for test in integration}
    for test_id in INTEGRATION_TESTS:
        if test_id not in found:
            print(
                f"warning: integration test {test_id} was not discovered; "
                "no exclusion applies to it",
                file=sys.stderr,
            )

    selected = [test for test in tests if test.id() not in INTEGRATION_TESTS]
    if args.integration:
        selected.extend(integration)

    print(f"Discovered {len(tests)} tests under tests/test_*.py")
    print(f"  unit group: {len(tests) - len(integration)} tests")
    if integration:
        state = "included" if args.integration else "excluded; pass --integration"
        print(f"  integration group: {len(integration)} test ({state})")
        for test in integration:
            print(f"    {test.id()} - {INTEGRATION_TESTS[test.id()]}")
    else:
        print("  integration group: 0 tests")

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    print(
        f"Executed {result.testsRun} tests: {len(result.skipped)} skipped, "
        f"{len(result.failures)} failed, {len(result.errors)} errors"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
