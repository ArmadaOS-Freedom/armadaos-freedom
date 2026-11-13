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
"""Stand-in docker: record argv, then fail with a distinctive status."""

import json
import os
import sys

with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")

sys.exit(%d if sys.argv[1:] == ["info"] else %d)
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


@contextmanager
def build_fixture():
    """Yield a minimal source tree, runtimes, fake docker and an env for it."""
    with tempfile.TemporaryDirectory(dir=_artifact_root()) as raw_root:
        root = Path(raw_root)
        source = root / "source tree"
        build_files = source / "build_files"
        build_files.mkdir(parents=True)

        shutil.copyfile(REAL_WRAPPER, build_files / REAL_WRAPPER.name)
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
            "signature",
            "package_contract",
            "package_media",
            "recovery_manifest",
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

    @staticmethod
    def _contract_models():
        module_root = str(REPO_ROOT / "system_files/usr/lib/armada")
        sys.path.insert(0, module_root)
        try:
            from updates import package_contract
        finally:
            sys.path.remove(module_root)
        return package_contract

    def test_authorized_model_list_matches_the_package_contract(self):
        # The wrapper passes custom profiles to the descriptor as explicit
        # install models, so this list must stay the same closed set the
        # shipped descriptor contract accepts by default.
        contract = self._contract_models()
        self.assertEqual(
            self._wrapper_models("AUTHORIZED_MODELS"),
            set(contract.AUTHORIZED_MODELS),
        )

    def test_offline_upgrade_model_list_matches_the_package_contract(self):
        # The wrapper decides the package-level upgrade claim from this list.
        contract = self._contract_models()
        self.assertEqual(
            self._wrapper_models("OFFLINE_UPGRADE_MODELS"),
            set(contract.OFFLINE_UPGRADE_MODELS),
        )

    def test_tb710fu_is_authorized_for_install_only(self):
        authorized = self._wrapper_models("AUTHORIZED_MODELS")
        upgrade = self._wrapper_models("OFFLINE_UPGRADE_MODELS")
        self.assertIn("TB710FU", authorized)
        self.assertNotIn("TB710FU", upgrade)
        self.assertEqual(upgrade, {"TB322FC", "TB323FU"})
        self.assertLessEqual(upgrade, authorized)

    def test_wrapper_gates_both_upgrade_flags_on_the_upgrade_models(self):
        # Neither the producer bundle flag nor the descriptor flag may depend on
        # the wider install set: TB710FU is installed but never upgraded.
        text = REAL_WRAPPER.read_text(encoding="utf-8").replace("\r\n", "\n")
        for flag in ("producer_args", "descriptor_args"):
            self.assertIn(
                "if ((${#upgrade_blocked_models[@]} == 0)); then\n"
                "    %s+=(--offline-upgrade)" % flag,
                text,
            )

    def test_recipe_release_profile_matches_the_wrapper_upgrade_models(self):
        # The release variant selects exactly the models the wrapper and the
        # shipped contract allow to advertise offline-upgrade. The universal
        # default carries no --model, so every configured profile stays in the
        # install-only artifact.
        text = REAL_JUSTFILE.read_text(encoding="utf-8").replace("\r\n", "\n")
        match = re.search(
            r"^build-edl-package [^\n]*:\n(?P<body>(?:(?:[ \t]+.*)?\n)*)",
            text,
            re.MULTILINE,
        )
        self.assertIsNotNone(match, "the Justfile must define build-edl-package")
        body = match.group("body")
        models = set(
            re.findall(r"--model ([A-Za-z0-9][A-Za-z0-9._-]*)", body)
        )
        self.assertEqual(models, self._wrapper_models("OFFLINE_UPGRADE_MODELS"))
        self.assertEqual(
            models, set(self._contract_models().OFFLINE_UPGRADE_MODELS)
        )
        self.assertIn("universal) model_args=()", body)
        self.assertIn('${model_args[@]+"${model_args[@]}"}', body)

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


if __name__ == "__main__":
    unittest.main()
