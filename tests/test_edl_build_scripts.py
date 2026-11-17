#!/usr/bin/env python3
"""Behaviour tests for the build-edl-package.sh preflight.

Every case runs the real wrapper as a bash process against a throw-away source
fixture. The fixture holds copies of the checked-in wrapper, the devices
helper and the devices config; one purely synthetic profile (THIRD-DEVICE) is
appended to that copy of the config, so the default selection covers every
profile the copied config happens to define plus that synthetic one. The exact
configured set is read back from the copied document and carried on the
fixture, so no case depends on a hardcoded model list. Only the source files
the preflight stats are created, and tests supplying explicit runtimes use empty
placeholder files outside the source tree, so no image, container, archive,
mount or device work is started.

A minimal fake ``docker`` sits first on the fixture PATH. It appends its argv
as one JSON line per call and then exits with a distinctive nonzero status.
That lets a case prove exactly which docker calls were made: the valid
preflight cases must reach ``docker info`` and stop at the daemon check, and
every rejected case must fail before docker is ever looked up.

The argument-recording cases opt into a second fake-docker mode that also
creates the few files the wrapper checks for in the mounted work and output
directories. Those cases run a copy of the wrapper whose only edit is the
physical loop-device precondition, replaced with two fixed dummy loop paths;
all model selection, upgrade decisions and producer/descriptor argument
construction stay byte-identical to the checked-in wrapper.

Standard library only; run with ``python3 -m unittest`` or directly.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_FILES = REPO_ROOT / "build_files"
REAL_WRAPPER = BUILD_FILES / "build-edl-package.sh"
REAL_HELPER = BUILD_FILES / "edl_device_config.py"
REAL_DEVICES_CONFIG = BUILD_FILES / "edl-devices.json"
REAL_JUSTFILE = REPO_ROOT / "Justfile"
REAL_RUNTIME_BUILDER = BUILD_FILES / "build-edl-runtime.sh"

# An arbitrary ID the copied config is extended with, never shipped by it.
THIRD_MODEL = "THIRD-DEVICE"
UNKNOWN_MODEL = "NO-SUCH-DEVICE"

OCI_INPUT = "ghcr.io/armadaos-freedom/armadaos-freedom:stable"
MANIFEST_DIGEST = "sha256:" + "0123456789abcdef" * 4
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
PACKAGE_VERSION = "20261024"
BOOT_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
ROOT_UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

DOCKER_INFO_SENTINEL = 73
DOCKER_OTHER_SENTINEL = 74
TIMEOUT_SECONDS = 120

PREFLIGHT_REACHED = "Docker daemon is not reachable"

FAKE_DOCKER_SOURCE = r'''#!/usr/bin/env python3
"""Stand-in docker: record argv, then fail or emulate mounted effects."""

import json
import os
import sys
from pathlib import Path

ARGS = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(ARGS) + "\n")

ZIP_NAME = os.environ.get("FAKE_DOCKER_ZIP", "")
PIPELINE = os.environ.get("FAKE_DOCKER_PIPELINE") == "1"


def mounted(target):
    """Return the host path bound to *target*, if the call binds it."""
    for index, value in enumerate(ARGS[:-1]):
        if value != "-v":
            continue
        spec = ARGS[index + 1]
        for suffix in (":" + target + ":ro", ":" + target):
            if spec.endswith(suffix):
                return Path(spec[: -len(suffix)])
    return None


def invokes(name):
    return any(Path(argument).name == name for argument in ARGS)


if ARGS == ["info"]:
    sys.exit(0 if PIPELINE else %d)
if not PIPELINE:
    sys.exit(%d)

# Only the mounted effects the wrapper stat-checks are emulated; no image,
# container, mount, loop or archive work happens.
if ARGS[:1] == ["build"] or ARGS[:1] in (["rm"], ["image"]):
    sys.exit(0)

work = mounted("/work")
output = mounted("/output")

if invokes("skopeo"):
    if work is not None:
        (work / "oci").mkdir(parents=True, exist_ok=True)
elif invokes("make-edl-raw-package.py"):
    if work is not None:
        archive = work / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / ZIP_NAME).write_bytes(b"fake producer archive")
elif invokes("split-package-archive.py"):
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        (output / (ZIP_NAME + ".parts.json")).write_text(
            json.dumps({"parts": [{
                "fileName": ZIP_NAME + ".001",
                "bytes": 1,
                "sha256": "0" * 64,
            }]}),
            encoding="utf-8",
        )
elif invokes("make-universal-update-descriptor.py"):
    if output is not None:
        (output / (ZIP_NAME + ".update.json")).write_text(
            "{}\n", encoding="utf-8"
        )

sys.exit(0)
''' % (DOCKER_INFO_SENTINEL, DOCKER_OTHER_SENTINEL)


@dataclass(frozen=True)
class Fixture:
    source: Path
    wrapper: Path
    work: Path
    output: Path
    runtime_dir: Path
    docker_log: Path
    registered_models: tuple
    env: dict


def _artifact_root():
    configured = os.environ.get("TEST_ARTIFACT_ROOT")
    return configured or None


def _third_profile(base_profile):
    """A complete, valid profile copying the shape of a configured one."""
    profile = copy.deepcopy(base_profile)
    profile.update(
        {
            "displayName": "Synthetic Third Device",
            "compatible": {"soc": "SYNTH9000", "device": "third-device"},
            "dtb": "synthetic/third-device.dtb",
            "required": ["synthetic,third-device", "synthetic,syn9000"],
            "forbidden": ["synthetic,foreign-board", "synthetic,other-soc"],
            "extraArgs": [],
            "runtimeConfig": "configs/third_device_defconfig",
            "runtimeCmakeArgs": ["-DSYNTHETIC_THIRD_DEVICE=ON"],
            "bootPartitionBytes": 100663296,
            "diagnosticMode": "verbose",
        }
    )
    return profile


def _contract_models():
    module_root = str(REPO_ROOT / "system_files/usr/lib/armada")
    sys.path.insert(0, module_root)
    try:
        from updates import package_contract
    finally:
        sys.path.remove(module_root)
    return package_contract


def _instrument_loop_precondition(text):
    """Replace only the physical loop precondition of the wrapper copy.

    The checked-in wrapper needs two real unused /dev/loopN block devices and
    /dev/loop-control. A test fixture has none, so the complete loop-scan
    section is replaced with two fixed dummy paths. Everything outside that
    bounded section, including all upgrade decisions and both argument arrays,
    is left byte-identical.
    """
    start = text.index("# --- loop devices")
    end = text.index("# --- raw producer")
    head, section, tail = text[:start], text[start:end], text[end:]
    if "[[ -c /dev/loop-control ]]" not in section:
        raise AssertionError("the wrapper loop section must hold the loop precondition")
    replacement = (
        'loop_a="/dev/loop0"\n'
        'loop_b="/dev/loop1"\n'
        'note "using $loop_a and $loop_b for the file-backed images"\n'
        "\n"
    )
    instrumented = head + replacement + tail
    # The edit inserts one span and deletes exactly the original loop section.
    if instrumented[: len(head)] != head:
        raise AssertionError("the loop instrumentation changed the wrapper head")
    if instrumented[len(head) + len(replacement):] != tail:
        raise AssertionError("the loop instrumentation changed the wrapper tail")
    # Prove the edit is bounded to the loop section.
    for needle in (
        "producer_args+=(--offline-upgrade)",
        "descriptor_args+=(--offline-upgrade)",
        'descriptor_args+=(--install-model "$model_id")',
        'producer_args+=(--model "$model_id")',
    ):
        if instrumented.count(needle) != text.count(needle):
            raise AssertionError("the loop instrumentation changed %r" % needle)
    return instrumented


@contextmanager
def build_fixture(full_build=False):
    """Yield a minimal source tree, runtimes, fake docker and an env for it."""
    with tempfile.TemporaryDirectory(dir=_artifact_root()) as raw_root:
        root = Path(raw_root)
        source = root / "source tree"
        build_files = source / "build_files"
        build_files.mkdir(parents=True)

        wrapper_text = REAL_WRAPPER.read_text(encoding="utf-8")
        if full_build:
            wrapper_text = _instrument_loop_precondition(wrapper_text)
        with open(
            build_files / REAL_WRAPPER.name, "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(wrapper_text)
        shutil.copyfile(REAL_HELPER, build_files / REAL_HELPER.name)

        with open(REAL_DEVICES_CONFIG, encoding="utf-8") as handle:
            document = json.load(handle)
        profiles = document["profiles"]
        registered_models = tuple(profiles)
        profiles[THIRD_MODEL] = _third_profile(profiles[registered_models[0]])
        (build_files / REAL_DEVICES_CONFIG.name).write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

        for name in (
            "make-edl-raw-package.py",
            "split-package-archive.py",
            "make-universal-update-descriptor.py",
            "universal_package.py",
            "edl_boot_requirements.py",
            "Containerfile.edl-tools",
        ):
            (build_files / name).write_text(
                "# placeholder: the preflight only stats this file\n", encoding="utf-8"
            )

        for name in (
            "__init__",
            "_io",
            "signature",
            "package_contract",
            "package_media",
            "recovery_manifest",
            "models",
            "boot_payload",
        ):
            module = source / ("system_files/usr/lib/armada/updates/" + name + ".py")
            module.parent.mkdir(parents=True, exist_ok=True)
            module.write_text(
                "# placeholder: the preflight only stats this file\n", encoding="utf-8"
            )

        protocol_test = source / "tests/update-oci-pull-test.py"
        protocol_test.parent.mkdir(parents=True)
        protocol_test.write_text(
            "# placeholder: the preflight only stats this file\n", encoding="utf-8"
        )

        installer = source / "system_files/usr/libexec/armada/armada-installer"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

        # The preflight only checks that the checkout marker exists.
        (source / ".git").mkdir()

        runtime_dir = root / "runtime files"
        runtime_dir.mkdir()

        bin_dir = root / "fake bin"
        bin_dir.mkdir()
        docker_log = root / "docker-calls.jsonl"
        fake_docker = bin_dir / "docker"
        fake_docker.write_text(FAKE_DOCKER_SOURCE, encoding="utf-8")
        fake_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = os.pathsep.join((str(bin_dir), env.get("PATH", "/usr/bin:/bin")))
        env["FAKE_DOCKER_LOG"] = str(docker_log)
        if full_build:
            env["FAKE_DOCKER_PIPELINE"] = "1"
            env["FAKE_DOCKER_ZIP"] = "ArmadaOS-Freedom-%s.zip" % PACKAGE_VERSION

        yield Fixture(
            source=source,
            wrapper=build_files / REAL_WRAPPER.name,
            work=root / "work dir",
            output=root / "output dir",
            runtime_dir=runtime_dir,
            docker_log=docker_log,
            registered_models=registered_models,
            env=env,
        )


def runtime_path(fixture, model):
    """Create (if needed) and return the placeholder runtime for *model*."""
    path = fixture.runtime_dir / (model + ".bin")
    path.touch()
    return path


def base_arguments(fixture, models=(), runtime_models=()):
    arguments = [
        "--oci", OCI_INPUT,
        "--manifest-digest", MANIFEST_DIGEST,
        "--source-commit", SOURCE_COMMIT,
        "--version", PACKAGE_VERSION,
        "--source", str(fixture.source),
        "--work", str(fixture.work),
        "--output", str(fixture.output),
        "--boot-uuid", BOOT_UUID,
        "--root-uuid", ROOT_UUID,
    ]
    for model in models:
        arguments += ["--model", model]
    for model in runtime_models:
        arguments += ["--runtime", "%s=%s" % (model, runtime_path(fixture, model))]
    return arguments


def run_wrapper(fixture, arguments):
    return subprocess.run(
        ["bash", str(fixture.wrapper), *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=fixture.env,
    )


def read_docker_log(fixture):
    try:
        content = fixture.docker_log.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in content.splitlines() if line.strip()]


def _diagnostics(result):
    return (
        "wrapper exit status: %d\n"
        "--- stdout ---\n%s\n"
        "--- stderr ---\n%s" % (result.returncode, result.stdout, result.stderr)
    )


class WrapperPreflightTests(unittest.TestCase):
    @staticmethod
    def _wrapper_models(name):
        match = re.search(
            r'^readonly %s="([^"]*)"$' % name,
            REAL_WRAPPER.read_text(encoding="utf-8").replace("\r\n", "\n"),
            re.MULTILINE,
        )
        if match is None:
            raise AssertionError("the wrapper must define %s" % name)
        return set(match.group(1).split())

    def test_authorized_model_list_matches_the_package_contract(self):
        # The wrapper passes custom profiles to the descriptor as explicit
        # install models, so this list must stay the same closed set the
        # shipped descriptor contract accepts by default.
        contract = _contract_models()
        self.assertEqual(
            self._wrapper_models("AUTHORIZED_MODELS"),
            set(contract.AUTHORIZED_MODELS),
        )

    def test_offline_upgrade_model_list_matches_the_package_contract(self):
        # The wrapper decides the package-level upgrade claim from this list.
        contract = _contract_models()
        self.assertEqual(
            self._wrapper_models("OFFLINE_UPGRADE_MODELS"),
            set(contract.OFFLINE_UPGRADE_MODELS),
        )

    def test_runtime_marker_models_match_the_mirror_policy(self):
        # build-edl-runtime.sh refuses a runtime without the mirrored-policy
        # marker for exactly the models the updater gates on it.
        text = REAL_RUNTIME_BUILDER.read_text(encoding="utf-8")
        match = re.search(
            r'^\s*if \[\[ (?P<cond>[^\n]*) \]\]; then\n'
            r'\s*grep -aqF \'ARMADA_LINUX_MIRROR_V1\'',
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "the runtime builder must gate the marker")
        marker_models = set(re.findall(r'"\$model" == ([A-Za-z0-9_-]+)',
                                       match.group("cond")))
        module_root = str(REPO_ROOT / "system_files/usr/lib/armada")
        sys.path.insert(0, module_root)
        try:
            from updates import models
        finally:
            sys.path.remove(module_root)
        self.assertEqual(marker_models, set(models.MIRROR_POLICY_MODELS))

    def test_tb710fu_is_authorized_for_install_and_upgrade(self):
        authorized = self._wrapper_models("AUTHORIZED_MODELS")
        upgrade = self._wrapper_models("OFFLINE_UPGRADE_MODELS")
        self.assertIn("TB710FU", authorized)
        self.assertIn("TB710FU", upgrade)
        self.assertEqual(upgrade, {"TB322FC", "TB323FU", "TB710FU"})
        self.assertLessEqual(upgrade, authorized)
        # The synthetic profile stays outside both shipped lists.
        self.assertNotIn(THIRD_MODEL, authorized)
        self.assertNotIn(THIRD_MODEL, upgrade)

    def test_wrapper_does_not_suppress_upgrade_for_mixed_shipped_models(self):
        # A shipped model outside the upgrade list must not disable upgrade for
        # the dual-slot models; the installer rejects that model's own upgrade
        # request.
        text = REAL_WRAPPER.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("upgrade_blocked_models", text)
        for flag in ("producer_args", "descriptor_args"):
            self.assertIn("%s+=(--offline-upgrade)" % flag, text)
        # Custom models stay explicit install-only identifiers.
        self.assertIn('descriptor_args+=(--install-model "$model_id")', text)

    def test_recipe_is_one_universal_package(self):
        # One artifact contains every registered profile: no profile switch
        # and no model selector may remain in the recipe.
        text = REAL_JUSTFILE.read_text(encoding="utf-8").replace("\r\n", "\n")
        match = re.search(
            r"^build-edl-package [^\n]*:\n(?P<body>(?:(?:[ \t]+.*)?\n)*)",
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "the Justfile must define build-edl-package")
        body = match.group("body")
        self.assertNotIn("EDL_PACKAGE_PROFILE", body)
        self.assertNotIn("--model ", body)
        self.assertNotIn("model_args", body)

    def _assert_stopped_at_docker_preflight(self, fixture, result):
        self.assertEqual(1, result.returncode, _diagnostics(result))
        calls = read_docker_log(fixture)
        self.assertEqual(
            [["info"]],
            calls,
            "the wrapper must stop at the docker daemon check\n" + _diagnostics(result),
        )
        self.assertIn(PREFLIGHT_REACHED, result.stderr, _diagnostics(result))

    def _assert_rejected_before_docker(self, fixture, result, expected):
        self.assertEqual(1, result.returncode, _diagnostics(result))
        self.assertIn(expected, result.stderr, _diagnostics(result))
        calls = read_docker_log(fixture)
        self.assertEqual(
            [],
            calls,
            "docker must not be called for rejected input\n" + _diagnostics(result),
        )

    def test_default_selection_accepts_the_synthetic_profile(self):
        # No --model: every configured profile plus THIRD-DEVICE is selected and
        # every selected runtime is supplied, so only the daemon check stops us.
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture,
                base_arguments(
                    fixture,
                    runtime_models=fixture.registered_models + (THIRD_MODEL,),
                ),
            )
            self._assert_stopped_at_docker_preflight(fixture, result)

    def test_subset_selection_accepts_only_the_synthetic_profile(self):
        # A real subset: one model, one runtime, same preflight result.
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture,
                base_arguments(fixture, models=(THIRD_MODEL,), runtime_models=(THIRD_MODEL,)),
            )
            self._assert_stopped_at_docker_preflight(fixture, result)

    def test_default_selection_uses_oci_embedded_runtimes(self):
        # With no --runtime, all selected models use the OCI-embedded runtimes.
        with build_fixture() as fixture:
            result = run_wrapper(fixture, base_arguments(fixture))
            self._assert_stopped_at_docker_preflight(fixture, result)

    def test_subset_selection_uses_oci_embedded_runtimes(self):
        # The default runtime path also applies to an explicitly selected subset.
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture, base_arguments(fixture, models=(THIRD_MODEL,))
            )
            self._assert_stopped_at_docker_preflight(fixture, result)

    def test_unknown_model_is_rejected_by_the_helper(self):
        # The devices helper rejects the model before runtime cross-checks.
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture,
                base_arguments(fixture, models=(UNKNOWN_MODEL,), runtime_models=(THIRD_MODEL,)),
            )
            self._assert_rejected_before_docker(fixture, result, "unknown model")

    def test_missing_runtime_for_a_selected_model_is_rejected(self):
        # Default selection means every configured profile plus the synthetic
        # one, so the configured runtimes alone are not enough.
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture, base_arguments(fixture, runtime_models=fixture.registered_models)
            )
            self._assert_rejected_before_docker(
                fixture, result, "exactly once for every selected model"
            )

    def test_runtime_for_an_unselected_model_is_rejected(self):
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture,
                base_arguments(
                    fixture,
                    models=(THIRD_MODEL,),
                    runtime_models=(THIRD_MODEL, fixture.registered_models[0]),
                ),
            )
            self._assert_rejected_before_docker(
                fixture, result, "not selected by the devices config"
            )

    def test_missing_update_descriptor_cli_is_rejected(self):
        # The descriptor CLI is a required source file: without it the wrapper
        # must fail in the preflight, before docker is ever looked up.
        with build_fixture() as fixture:
            (fixture.source / "build_files/make-universal-update-descriptor.py").unlink()
            result = run_wrapper(
                fixture,
                base_arguments(
                    fixture,
                    runtime_models=fixture.registered_models + (THIRD_MODEL,),
                ),
            )
            self._assert_rejected_before_docker(
                fixture,
                result,
                "source is missing build_files/make-universal-update-descriptor.py",
            )

    def test_missing_recovery_manifest_is_rejected(self):
        # Recovery manifest validation is required by the descriptor builder.
        with build_fixture() as fixture:
            (
                fixture.source
                / "system_files/usr/lib/armada/updates/recovery_manifest.py"
            ).unlink()
            result = run_wrapper(fixture, base_arguments(fixture))
            self._assert_rejected_before_docker(
                fixture,
                result,
                "source is missing system_files/usr/lib/armada/updates/recovery_manifest.py",
            )

    def test_missing_boot_payload_is_rejected(self):
        # The trusted boot payload is required even when runtimes come from OCI.
        with build_fixture() as fixture:
            (
                fixture.source
                / "system_files/usr/lib/armada/updates/boot_payload.py"
            ).unlink()
            result = run_wrapper(fixture, base_arguments(fixture))
            self._assert_rejected_before_docker(
                fixture,
                result,
                "source is missing system_files/usr/lib/armada/updates/boot_payload.py",
            )

    def test_missing_oci_protocol_test_is_rejected(self):
        # The real protocol test must be present before any Docker work starts.
        with build_fixture() as fixture:
            (fixture.source / "tests/update-oci-pull-test.py").unlink()
            result = run_wrapper(fixture, base_arguments(fixture))
            self._assert_rejected_before_docker(
                fixture,
                result,
                "source is missing tests/update-oci-pull-test.py",
            )

    def test_duplicate_runtime_for_a_selected_model_is_rejected(self):
        with build_fixture() as fixture:
            result = run_wrapper(
                fixture,
                base_arguments(
                    fixture,
                    models=(THIRD_MODEL,),
                    runtime_models=(THIRD_MODEL, THIRD_MODEL),
                ),
            )
            self._assert_rejected_before_docker(fixture, result, "duplicate runtime for")


def _invoked(argv, script):
    return any(Path(argument).name == script for argument in argv)


def _values(argv, option):
    return [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == option
    ]


class WrapperArgumentTests(unittest.TestCase):
    """Record the real producer and descriptor argv the wrapper builds."""

    def _run_full_build(self, models):
        contract = _contract_models()
        authorized = set(contract.AUTHORIZED_MODELS)
        upgrade_models = set(contract.OFFLINE_UPGRADE_MODELS)
        install_only = [model for model in models if model not in authorized]
        expected_upgrade = (
            any(model in upgrade_models for model in models) and not install_only
        )

        with build_fixture(full_build=True) as fixture:
            result = run_wrapper(fixture, base_arguments(fixture, models=models))
            self.assertEqual(0, result.returncode, _diagnostics(result))

            calls = read_docker_log(fixture)
            producer = [
                argv for argv in calls if _invoked(argv, "make-edl-raw-package.py")
            ]
            descriptor = [
                argv
                for argv in calls
                if _invoked(argv, "make-universal-update-descriptor.py")
            ]
            self.assertEqual(1, len(producer), _diagnostics(result))
            self.assertEqual(1, len(descriptor), _diagnostics(result))
            producer, descriptor = producer[0], descriptor[0]

            selected = _values(producer, "--model")
            self.assertEqual(set(models), set(selected), _diagnostics(result))
            self.assertEqual([], _values(descriptor, "--model"), _diagnostics(result))

            # The producer records the package capability and the descriptor
            # declares it. Both must agree with the selected-model eligibility
            # the package contract defines, not with a fixed expectation.
            self.assertEqual(
                expected_upgrade, "--offline-upgrade" in producer,
                _diagnostics(result),
            )
            self.assertEqual(
                expected_upgrade, "--offline-upgrade" in descriptor,
                _diagnostics(result),
            )
            self.assertEqual(
                set(install_only),
                set(_values(descriptor, "--install-model")),
                _diagnostics(result),
            )

            # A package-level upgrade claim and an explicit install allowance
            # are mutually exclusive in the descriptor.
            if "--offline-upgrade" in descriptor:
                self.assertEqual([], install_only)
            return producer, descriptor

    def test_y700_alone_builds_an_upgrade_package(self):
        producer, descriptor = self._run_full_build(("TB322FC",))
        self.assertIn("--offline-upgrade", producer)
        self.assertIn("--offline-upgrade", descriptor)

    def test_y700_with_tb710fu_builds_one_upgrade_package(self):
        producer, descriptor = self._run_full_build(("TB322FC", "TB710FU"))
        self.assertIn("--offline-upgrade", producer)
        self.assertIn("--offline-upgrade", descriptor)
        self.assertIn("TB710FU", _values(producer, "--model"))
        self.assertEqual([], _values(descriptor, "--install-model"))

    def test_tb710fu_alone_builds_an_upgrade_package(self):
        producer, descriptor = self._run_full_build(("TB710FU",))
        self.assertIn("--offline-upgrade", producer)
        self.assertIn("--offline-upgrade", descriptor)
        self.assertEqual([], _values(descriptor, "--install-model"))

    def test_custom_profile_alone_builds_an_install_only_package(self):
        producer, descriptor = self._run_full_build((THIRD_MODEL,))
        self.assertNotIn("--offline-upgrade", producer)
        self.assertNotIn("--offline-upgrade", descriptor)
        self.assertEqual([THIRD_MODEL], _values(descriptor, "--install-model"))

    def test_custom_profile_is_an_explicit_install_model(self):
        producer, descriptor = self._run_full_build(("TB322FC", THIRD_MODEL))
        self.assertNotIn("--offline-upgrade", producer)
        self.assertNotIn("--offline-upgrade", descriptor)
        self.assertEqual([THIRD_MODEL], _values(descriptor, "--install-model"))
        self.assertIn(THIRD_MODEL, _values(producer, "--model"))


if __name__ == "__main__":
    unittest.main()
