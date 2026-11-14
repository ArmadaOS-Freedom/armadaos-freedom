# Testing

## Automatic unit tests

`.github/workflows/unit-tests.yml` runs on pull requests, pushes to `main`, and
manual dispatches. It installs the real dependencies on `ubuntu-24.04` and runs
`tests/run_unit_tests.py`, which discovers every `tests/test_*.py` file with the
standard-library `unittest` loader.

The runner prints the discovered groups and the executed, skipped, failed, and
error counts. It fails on any real test failure or import error. It does not
use a test framework beyond `unittest`.

Packages are installed with `sudo`; the suite itself runs unprivileged.

## The integration group

One discovered test is held back by default, by exact name:

- `test_edl_raw_package.ProducerContractTests.test_filesystem_pair_real_ext4_and_btrfs_images`

It creates a real ext4/btrfs pair through loop devices, so it needs root,
`/dev/loop-control`, mount, and the btrfs tools. The default run lists it with
that prerequisite and leaves it out. In a privileged environment, include it:

```bash
/usr/bin/python3 tests/run_unit_tests.py --integration
```

No other test is excluded, ignored, or converted into a success. Skips that the
tests already declare still report as skips in the output.

## Coverage limits

This job expands automatic public unit coverage; it does not replace the
existing privileged jobs, and those jobs remain as they are.

Shell tests (`tests/*-test.sh`), dashed-script tests that are not named
`test_*.py`, and tests that need a real upstream source tree (the kernel and
MangoHud applied-source tests) are not all covered by this job. Those remain in
their dedicated jobs. This workflow adds no fake upstream build fixtures and
compiles neither kernel nor MangoHud.

Refs #61: the job covers the automatically discoverable unit suite, not every
test in the repository, so the issue stays open.

## Running locally

Install the same dependencies as the workflow, then run from the repository
root:

```bash
/usr/bin/python3 tests/run_unit_tests.py
```
