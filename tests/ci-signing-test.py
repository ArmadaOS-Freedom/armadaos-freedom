#!/usr/bin/env python3
"""Command-flow tests for the CI signing / promotion boundary.

These tests extract the *actual* ``run:`` blocks from the checked-in GitHub
Actions workflows, render their ``${{ ... }}`` expressions with a fixed,
harmless test mapping, and execute them with stub ``cosign`` / ``skopeo`` /
``jq`` scripts that record their arguments.  They assert *command flow* (which
tool runs with which arguments, in which order), not cryptography: no key is
generated, no registry is contacted and nothing is built.

Tool isolation does not depend on ``PATH`` or on executable bits.  Each stub is
written as plain data (mode 0644) and dispatched by a ``readonly`` bash
function of the same name whose body pins the absolute bash interpreter and the
absolute stub path as literals, so the stub is read as a script and never needs
the exec bit -- a ``noexec`` temporary mount cannot break dispatch.  The child
environment is scrubbed of ``BASH_ENV`` / ``ENV`` / inherited ``BASH_FUNC_*``
entries and bash runs with ``--noprofile --norc``, so an injected startup
definition cannot substitute a real ``cosign`` / ``skopeo`` / ``jq``.  Before
any workflow script runs, the fixture asserts both ``type -t`` and
``command -v`` resolve each tool to its fixture function.

Expected to be RED against the current workflows until the signing boundary is
implemented; see the case-by-case comments for the approved behaviour each test
pins down.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Approved configuration values.
CANONICAL_IMAGE = "ghcr.io/armadaos-freedom/armadaos-freedom"
CANONICAL_OWNER = "ArmadaOS-Freedom"
CANONICAL_REPO = "armadaos-freedom"
SIGNING_PUBLIC_KEY = "system_files/etc/pki/containers/armadaos-freedom.pub"
LEGACY_PUBLIC_KEY = "system_files/etc/pki/containers/armada.pub"
LEGACY_IMAGE = "ghcr.io/virtudude/armada"

# One fixed, obviously-fake sha256.  The skopeo stub always reports it and the
# step-output digest expressions are rendered to the same value so a combined
# chain of real steps sees a consistent digest.
SHA256_DIGEST = "sha256:" + "deadbeef" * 8

# Literal fixture material -- never real secrets, never a generated key.
PRIVATE_VALUE = "fixture-private-value"
PUBLIC_FILE_CONTENT = "fixture-public-key"


# --------------------------------------------------------------------------
# GitHub expression rendering
# --------------------------------------------------------------------------

class UnrenderedExpression(Exception):
    """Raised when a workflow uses an expression the test mapping does not know."""


def _expr_values():
    """Explicit, harmless substitution table for the known workflow expressions."""
    return {
        "github.repository": f"{CANONICAL_OWNER}/{CANONICAL_REPO}",
        "github.repository_owner": CANONICAL_OWNER,
        "github.event.repository.name": CANONICAL_REPO,
        "github.event.repository.fork": "false",
        "github.actor": "fixture-actor",
        "github.sha": "0" * 40,
        "github.ref": "refs/heads/main",
        "github.workflow": "Build images",
        "runner.temp": "/tmp/fixture-runner-temp",
        "secrets.GITHUB_TOKEN": "fixture-token",
        "secrets.SIGNING_SECRET": PRIVATE_VALUE,
        "secrets.SIGNING_PASSWORD": "fixture-password",
        "secrets.COSIGN_PRIVATE_KEY": PRIVATE_VALUE,
        "env.IMAGE_NAME": CANONICAL_REPO,
        "env.IMAGE_REGISTRY": "ghcr.io/" + CANONICAL_OWNER.lower(),
        "env.ARMADA_CANONICAL_IMAGE": CANONICAL_IMAGE,
        "env.ARMADA_SIGNING_PUBLIC_KEY": SIGNING_PUBLIC_KEY,
        "env.HAVE_SIGNING_SECRET": "true",
        "steps.push.outputs.digest": SHA256_DIGEST,
        "steps.resolve.outputs.digest": SHA256_DIGEST,
        "steps.version.outputs.version": "20261016.deadbee",
        "steps.version.outputs.revision": "0" * 40,
        "steps.version.outputs.created": "2026-10-16T00:00:00Z",
        "steps.metadata.outputs.tags": "testing",
        "steps.channel.outputs.tag": "testing",
        "steps.channel.outputs.channel": "preview",
        "steps.chunkah_output.outputs.ref": "oci:/tmp/fixture-runner-temp/chunked",
        "steps.chunkah_config.outputs.base_image": "quay.io/fedora/fedora-bootc@sha256:" + "0" * 64,
        "steps.chunkah_config.outputs.config": "{}",
        "steps.metadata.outputs.json": "{}",
        "steps.metadata.outputs.labels": "",
        "needs.packages.outputs.build_args": "",
        "inputs.source": "testing",
        "inputs.target": "stable",
        "inputs.version": "",
    }


_EXPR_RE = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def render(text, overrides=None):
    """Replace every ``${{ ... }}`` with its test value; reject leftovers."""
    values = _expr_values()
    if overrides:
        values.update(overrides)

    def replace(match):
        key = match.group(1)
        if key in values:
            return values[key]
        comparison = re.fullmatch(r"(.+?)\s*!=\s*''", key)
        if comparison and comparison.group(1) in values:
            return "false" if values[comparison.group(1)] == "" else "true"
        raise UnrenderedExpression(key)

    return _EXPR_RE.sub(replace, text)


# --------------------------------------------------------------------------
# Minimal, fixed-shape workflow accessor (text based, like build-channel-test.sh)
# --------------------------------------------------------------------------

class Workflow:
    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text()

    def env(self):
        """Parse the top-level ``env:`` block into a dict (values unquoted)."""
        lines = self.text.splitlines()
        result = {}
        for index, line in enumerate(lines):
            if line.rstrip() != "env:":
                continue
            for entry in lines[index + 1:]:
                if entry.strip() == "" or not entry.startswith("  ") or entry.startswith("    "):
                    break
                key, sep, value = entry.strip().partition(":")
                if not sep:
                    break
                result[key.strip()] = value.strip().strip('"').strip("'")
            break
        return result

    def has_step(self, name):
        return f"      - name: {name}\n" in self.text

    def step_offset(self, name):
        return self.text.index(f"      - name: {name}\n")

    def step_env(self, name):
        """Parse the step-level ``env:`` block of a named step into a dict.

        Workflows pass ``${{ ... }}`` values through step ``env:`` instead of
        interpolating them into the shell command, so the fixture must export
        those values before running the step body, exactly like a runner does.
        """
        marker = f"      - name: {name}\n"
        if marker not in self.text:
            raise KeyError(f"workflow has no step named {name!r}")
        step = self.text.split(marker, 1)[1]
        lines = step.split("\n")
        result = {}
        for index, line in enumerate(lines):
            if line.startswith("      - name:"):
                break
            if line != "        env:":
                continue
            for entry in lines[index + 1:]:
                if not entry.startswith("          "):
                    break
                key, sep, value = entry.strip().partition(":")
                if not sep:
                    break
                result[key.strip()] = value.strip().strip('"').strip("'")
            break
        return result

    def step_script(self, name):
        """Return the dedented body of the ``run: |`` block of a named step."""
        marker = f"      - name: {name}\n"
        if marker not in self.text:
            raise KeyError(f"workflow has no step named {name!r}")
        step = self.text.split(marker, 1)[1]
        head = "        run: |\n"
        if head not in step:
            raise KeyError(f"step {name!r} is not a block run step")
        body = step.split(head, 1)[1]
        kept = []
        for line in body.split("\n"):
            if line.strip() == "" or line.startswith("          "):
                kept.append(line)
            else:
                break
        return textwrap.dedent("\n".join(kept))


BUILD = Workflow(ROOT / ".github" / "workflows" / "build.yml")
PROMOTE = Workflow(ROOT / ".github" / "workflows" / "promote.yml")


# --------------------------------------------------------------------------
# Stub tools
# --------------------------------------------------------------------------

# The stubs are dispatched by bash functions through an explicit interpreter,
# so the interpreter path itself is resolved once (symlinks collapsed) and
# baked into the generated function bodies.
STUB_TOOLS = ("cosign", "skopeo", "jq")


def resolve_bash():
    """Return the absolute, symlink-resolved bash interpreter, or ``None``."""
    found = shutil.which("bash")
    return Path(found).resolve() if found else None


BASH = resolve_bash()


COSIGN_STUB = """#!/usr/bin/env bash
set -u
log="${STUB_LOG:?STUB_LOG is required}"
line="cosign"
for arg in "$@"; do line="$line $arg"; done
printf '%s\\n' "$line" >> "$log"

if [ "${1:-}" = "verify" ]; then
  key=""
  prev=""
  for arg in "$@"; do
    if [ "$prev" = "--key" ]; then key="$arg"; fi
    prev="$arg"
  done
  if [ -z "$key" ]; then
    echo "stub-cosign: verify called without --key" >&2
    exit 2
  fi
  if [ ! -s "$key" ]; then
    echo "stub-cosign: public key missing or empty: $key" >&2
    exit 1
  fi
  if [ "${STUB_VERIFY_FAIL:-0}" = "1" ]; then
    echo "stub-cosign: verify failed as configured" >&2
    exit 1
  fi
  printf '[{"critical":{"identity":{"docker-reference":"%s"}}}]\\n' "${STUB_IDENTITY:-}"
fi
exit 0
"""

SKOPEO_STUB = """#!/usr/bin/env bash
set -u
log="${STUB_LOG:?STUB_LOG is required}"
line="skopeo"
for arg in "$@"; do line="$line $arg"; done
printf '%s\\n' "$line" >> "$log"

if [ "${1:-}" = "inspect" ]; then
  for arg in "$@"; do
    if [ "$arg" = "--format" ]; then
      printf '%s\\n' "${STUB_DIGEST:?STUB_DIGEST is required}"
      exit 0
    fi
  done
  printf '{}\\n'
fi
exit 0
"""

JQ_STUB = """#!/usr/bin/env bash
set -u
log="${STUB_LOG:?STUB_LOG is required}"
printf 'jq\\n' >> "$log"
cat > /dev/null 2>&1 || true
printf 'true\\n'
exit 0
"""


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def tool_calls(commands, tool):
    return [c for c in commands if c and c[0] == tool]


def arg_value(args, flag):
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            return args[index + 1]
    return None


def first_index(commands, predicate):
    for index, command in enumerate(commands):
        if predicate(command):
            return index
    return -1


class Harness(unittest.TestCase):
    workflow = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.workdir = self.tmp / "repo"
        self.workdir.mkdir()
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.log = self.tmp / "commands.log"
        self.log.write_text("")
        if BASH is None or not BASH.is_file() or not os.access(BASH, os.X_OK):
            self.fail(f"ci-signing-test fixture error: no usable absolute bash interpreter (got {BASH!r})")
        self._install_stubs()

    def tearDown(self):
        self._tmp.cleanup()

    # -- setup helpers ----------------------------------------------------

    def _install_stubs(self):
        self.stub_paths = {}
        for name, body in (("cosign", COSIGN_STUB), ("skopeo", SKOPEO_STUB), ("jq", JQ_STUB)):
            path = self.bindir / name
            path.write_text(body)
            # Data mode: an explicit bash reads these files as scripts, so they
            # are never executed directly and must not require an exec bit.
            os.chmod(path, 0o644)
            if not path.is_file() or not os.access(path, os.R_OK):
                self.fail(f"ci-signing-test fixture error: stub missing or unreadable: {path}")
            self.stub_paths[name] = path

    def write_key(self, relative_path, content=PUBLIC_FILE_CONTENT):
        path = self.workdir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    # -- execution --------------------------------------------------------

    def _dispatch_prelude(self):
        """Readonly bash functions that dispatch each tool to its pinned stub.

        The interpreter and stub paths are embedded as ``shlex.quote``d
        literals, so neither a later variable assignment nor a workflow ``env``
        change can redirect dispatch.
        """
        interpreter = shlex.quote(str(BASH))
        lines = []
        for name in sorted(self.stub_paths):
            stub = shlex.quote(str(self.stub_paths[name]))
            lines.append(f'{name}() {{ {interpreter} {stub} "$@"; }}')
            lines.append(f"readonly -f {name}")
        return lines

    def _dispatch_assertions(self):
        """Fail with a fixture-specific error before any workflow step runs."""
        return [
            "for tool in " + " ".join(STUB_TOOLS) + "; do",
            '  if [ "$(type -t "$tool")" != "function" ]; then',
            '    echo "ci-signing-test fixture error: $tool does not resolve to a fixture function (type -t)" >&2',
            "    exit 97",
            "  fi",
            '  if [ "$(command -v "$tool")" != "$tool" ]; then',
            '    echo "ci-signing-test fixture error: $tool does not resolve to the fixture function (command -v)" >&2',
            "    exit 97",
            "  fi",
            "done",
        ]

    def _env(self, overrides, extra_env):
        overrides = dict(overrides or {})
        env = dict(os.environ)
        for key, value in self.workflow.env().items():
            env[key] = render(value, overrides) if "${{" in value else value

        # These step tests start after the runner has applied GITHUB_ENV.
        env["IMAGE_REGISTRY"] = env["IMAGE_REGISTRY"].lower()
        env["IMAGE_NAME"] = env["IMAGE_NAME"].lower()

        for name in ("github_output", "github_env", "summary"):
            (self.tmp / name).write_text("")
        (self.tmp / "home").mkdir(exist_ok=True)

        env.update({
            "STUB_LOG": str(self.log),
            "STUB_DIGEST": SHA256_DIGEST,
            "STUB_IDENTITY": CANONICAL_IMAGE,
            "STUB_VERIFY_FAIL": "0",
            "COSIGN_PASSWORD": "fixture-password",
            "GITHUB_OUTPUT": str(self.tmp / "github_output"),
            "GITHUB_ENV": str(self.tmp / "github_env"),
            "GITHUB_STEP_SUMMARY": str(self.tmp / "summary"),
            "GITHUB_RUN_ID": "4242",
            "GITHUB_RUN_ATTEMPT": "1",
            "HOME": str(self.tmp / "home"),
        })
        if extra_env:
            env.update(extra_env)

        # Dispatch is by bash function, not PATH, so no temporary bin is
        # prepended.  Startup hooks are dropped so nothing in the environment
        # can redefine the fixture functions before the workflow runs.
        for key in [k for k in env if k in ("BASH_ENV", "ENV") or k.startswith("BASH_FUNC_")]:
            del env[key]
        return env

    def run_chain(self, names, overrides=None, extra_env=None):
        """Run one or more real steps in a single bash process.

        The script is preceded by the readonly fixture functions and by
        assertions that each tool resolves to its fixture function; only then
        does the strict ``set -e`` / ``set -o pipefail`` mode apply to the real
        steps.  Concatenating the extracted scripts keeps that strictness
        across steps, so a failing sign or verify aborts before the later
        publish/promotion step can run -- exactly like GitHub's per-step
        shells.
        """
        lines = self._dispatch_prelude()
        lines += self._dispatch_assertions()
        lines.append("set -eo pipefail")
        for name in names:
            lines.append(f"# ---- step: {name} ----")
            for key, value in self.workflow.step_env(name).items():
                rendered = shlex.quote(render(value, overrides))
                # A variable already supplied by the fixture (a simulated
                # secret, for instance) wins: those cases test a missing value,
                # which a rendered step ``env:`` would otherwise restore.
                lines.append(
                    f'if [ -z "${{{key}+x}}" ]; then export {key}={rendered}; fi'
                )
            lines.append(render(self.workflow.step_script(name), overrides))
        script = "\n".join(lines) + "\n"
        return subprocess.run(
            [str(BASH), "--noprofile", "--norc", "-c", script],
            cwd=str(self.workdir),
            env=self._env(overrides, extra_env),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def commands(self):
        return [line.split() for line in self.log.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Config / identity agreement
# --------------------------------------------------------------------------

class ConfigContractTest(unittest.TestCase):
    def test_canonical_image_pinned_in_build(self):
        self.assertEqual(BUILD.env().get("ARMADA_CANONICAL_IMAGE"), CANONICAL_IMAGE)

    def test_canonical_image_pinned_in_promote(self):
        self.assertEqual(PROMOTE.env().get("ARMADA_CANONICAL_IMAGE"), CANONICAL_IMAGE)

    def test_signing_public_key_pinned_in_build(self):
        self.assertEqual(BUILD.env().get("ARMADA_SIGNING_PUBLIC_KEY"), SIGNING_PUBLIC_KEY)

    def test_signing_public_key_pinned_in_promote(self):
        self.assertEqual(PROMOTE.env().get("ARMADA_SIGNING_PUBLIC_KEY"), SIGNING_PUBLIC_KEY)

    def test_derived_repository_matches_declared_canonical(self):
        # IMAGE_REGISTRY / IMAGE_NAME must derive the same repository the
        # canonical constant names, in both workflows.
        for workflow in (BUILD, PROMOTE):
            env = workflow.env()
            derived = f"{render(env['IMAGE_REGISTRY'])}/{render(env['IMAGE_NAME'])}".lower()
            self.assertEqual(derived, env.get("ARMADA_CANONICAL_IMAGE"))
            self.assertEqual(derived, CANONICAL_IMAGE)


# --------------------------------------------------------------------------
# Build: "Check signing key"
# --------------------------------------------------------------------------

class BuildSigningKeyTest(Harness):
    workflow = BUILD

    def run_check(self, private=PRIVATE_VALUE, own_key=True, own_empty=False, legacy_key=False):
        if own_key:
            self.write_key(SIGNING_PUBLIC_KEY, "" if own_empty else PUBLIC_FILE_CONTENT)
        if legacy_key:
            self.write_key(LEGACY_PUBLIC_KEY)
        return self.run_chain(
            ["Check signing key"],
            overrides={"secrets.SIGNING_SECRET": private},
            extra_env={"COSIGN_PRIVATE_KEY": private},
        )

    def test_key_check_runs_after_checkout(self):
        # The own public-key file only exists after checkout, so the check must
        # follow "Check out source".
        self.assertGreater(BUILD.step_offset("Check signing key"),
                           BUILD.step_offset("Check out source"))

    def test_present_private_key_and_own_public_file_passes(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_own_public_key_fails_even_with_legacy_key(self):
        # A non-empty legacy armada.pub must not satisfy the own-key check.
        result = self.run_check(own_key=False, legacy_key=True)
        self.assertNotEqual(result.returncode, 0)

    def test_empty_own_public_key_fails(self):
        result = self.run_check(own_empty=True)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_private_signing_value_fails(self):
        result = self.run_check(private="")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_signing_password_fails(self):
        self.write_key(SIGNING_PUBLIC_KEY)
        result = self.run_chain(
            ["Check signing key"],
            extra_env={"COSIGN_PRIVATE_KEY": PRIVATE_VALUE, "COSIGN_PASSWORD": ""},
        )
        self.assertNotEqual(result.returncode, 0)


# --------------------------------------------------------------------------
# Build: sign -> publish flow
# --------------------------------------------------------------------------

class BuildSigningFlowTest(Harness):
    workflow = BUILD

    def sign_then_publish(self, fork="false", verify_fail="0"):
        self.write_key(SIGNING_PUBLIC_KEY)
        return self.run_chain(
            ["Sign container image", "Publish channel tags"],
            overrides={"github.event.repository.fork": fork},
            extra_env={"COSIGN_PRIVATE_KEY": PRIVATE_VALUE, "STUB_VERIFY_FAIL": verify_fail},
        )

    def test_fork_signing_still_verifies_own_key_with_explicit_identity(self):
        result = self.sign_then_publish(fork="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()

        signs = [c for c in tool_calls(commands, "cosign") if c[1:2] == ["sign"]]
        self.assertEqual(len(signs), 1, "exactly one signing invocation expected")
        sign = signs[0]
        self.assertEqual(arg_value(sign, "--sign-container-identity"), CANONICAL_IMAGE,
                         "signing must pass --sign-container-identity canonical")
        self.assertTrue(any(a.endswith("@" + SHA256_DIGEST) for a in sign),
                        "must sign the digest, not a tag")

        verifies = [c for c in tool_calls(commands, "cosign") if c[1:2] == ["verify"]]
        self.assertTrue(verifies, "signing must always be followed by a verify, fork or not")
        self.assertTrue(all(arg_value(v, "--key") == SIGNING_PUBLIC_KEY for v in verifies),
                        "verify must use the own public key")
        self.assertFalse(any("--check-claims" in c for c in tool_calls(commands, "cosign")),
                         "no legacy identity bridge expected")

        verify_index = first_index(commands, lambda c: c[:2] == ["cosign", "verify"])
        copy_index = first_index(commands, lambda c: c[:2] == ["skopeo", "copy"])
        self.assertNotEqual(copy_index, -1, "channel copy expected after a good verify")
        self.assertLess(verify_index, copy_index, "verify must precede the channel copy")

    def test_failed_verify_prevents_channel_copy(self):
        result = self.sign_then_publish(fork="true", verify_fail="1")
        self.assertNotEqual(result.returncode, 0)
        commands = self.commands()

        verifies = [c for c in tool_calls(commands, "cosign") if c[1:2] == ["verify"]]
        self.assertTrue(verifies, "the fork path must still attempt a verify")
        self.assertTrue(all(arg_value(v, "--key") == SIGNING_PUBLIC_KEY for v in verifies))
        self.assertEqual([c for c in tool_calls(commands, "skopeo") if c[1:2] == ["copy"]], [],
                         "a failed verify must abort before any channel copy")


class BuildRepositoryGuardTest(Harness):
    workflow = BUILD

    def test_prepare_normalizes_organization_name_for_ghcr(self):
        result = self.run_chain(
            ["Prepare environment"],
            extra_env={"IMAGE_REGISTRY": "ghcr.io/ArmadaOS-Freedom",
                       "IMAGE_NAME": "armadaos-freedom"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        exported = (self.tmp / "github_env").read_text().splitlines()
        self.assertIn("IMAGE_REGISTRY=ghcr.io/armadaos-freedom", exported)
        self.assertIn("IMAGE_NAME=armadaos-freedom", exported)

    def test_wrong_repository_fails_before_registry_use(self):
        self.write_key(SIGNING_PUBLIC_KEY)
        self.write_key(LEGACY_PUBLIC_KEY)
        result = self.run_chain(
            ["Prepare environment", "Check signing key", "Sign container image", "Publish channel tags"],
            overrides={"github.repository_owner": "wrong-owner",
                       "github.event.repository.name": "wrong-repo"},
            extra_env={"COSIGN_PRIVATE_KEY": PRIVATE_VALUE},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tool_calls(self.commands(), "skopeo"), [],
                         "a repository/canonical mismatch must fail before any skopeo call")


# --------------------------------------------------------------------------
# Promote: guard, verify-before-copy, channel semantics
# --------------------------------------------------------------------------

class PromoteGuardTest(Harness):
    workflow = PROMOTE

    CHAIN = ["Prepare environment", "Verify source container signature", "Publish release tags"]

    def promote_env(self, extra=None):
        env = {"COSIGN_PRIVATE_KEY": PRIVATE_VALUE}
        if extra:
            env.update(extra)
        return env

    def test_missing_own_key_does_not_inspect_or_copy(self):
        # Legacy key present, own key absent: promote must refuse before touching
        # the registry.
        self.write_key(LEGACY_PUBLIC_KEY)
        result = self.run_chain(self.CHAIN, extra_env=self.promote_env())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tool_calls(self.commands(), "skopeo"), [],
                         "a missing own signing key must fail before any registry access")

    def test_wrong_repository_does_not_inspect_or_copy(self):
        self.write_key(SIGNING_PUBLIC_KEY)
        result = self.run_chain(
            self.CHAIN,
            overrides={"github.repository_owner": "wrong-owner",
                       "github.event.repository.name": "wrong-repo"},
            extra_env=self.promote_env(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tool_calls(self.commands(), "skopeo"), [],
                         "a repository/canonical mismatch must fail before any registry access")

    def test_verify_failure_prevents_copy(self):
        self.write_key(SIGNING_PUBLIC_KEY)
        result = self.run_chain(self.CHAIN, extra_env=self.promote_env({"STUB_VERIFY_FAIL": "1"}))
        self.assertNotEqual(result.returncode, 0)
        commands = self.commands()
        self.assertNotEqual(first_index(commands, lambda c: c[:2] == ["skopeo", "inspect"]), -1,
                            "the source digest should have been resolved before verifying")
        verifies = [c for c in tool_calls(commands, "cosign") if c[1:2] == ["verify"]]
        self.assertTrue(verifies)
        self.assertTrue(all(arg_value(v, "--key") == SIGNING_PUBLIC_KEY for v in verifies),
                        "verify must use the own public key")
        self.assertEqual([c for c in tool_calls(commands, "skopeo") if c[1:2] == ["copy"]], [],
                         "a failed verify must abort before any promotion copy")

    def test_digest_is_verified_before_copy(self):
        self.write_key(SIGNING_PUBLIC_KEY)
        result = self.run_chain(self.CHAIN, extra_env=self.promote_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()

        inspect_index = first_index(commands, lambda c: c[:2] == ["skopeo", "inspect"])
        verify_index = first_index(commands, lambda c: c[:2] == ["cosign", "verify"])
        copy_index = first_index(commands, lambda c: c[:2] == ["skopeo", "copy"])
        self.assertNotEqual(inspect_index, -1)
        self.assertNotEqual(verify_index, -1)
        self.assertNotEqual(copy_index, -1)
        self.assertLess(inspect_index, verify_index, "resolve the digest before verifying")
        self.assertLess(verify_index, copy_index, "verify before copying")

        verifies = [c for c in tool_calls(commands, "cosign") if c[1:2] == ["verify"]]
        self.assertTrue(all(arg_value(v, "--key") == SIGNING_PUBLIC_KEY for v in verifies))
        copies = [c for c in tool_calls(commands, "skopeo") if c[1:2] == ["copy"]]
        self.assertTrue(any(SHA256_DIGEST in a for a in copies[-1]),
                        "the promoted source must be pinned to the verified digest")


class PromoteChannelTest(Harness):
    workflow = PROMOTE

    def copies(self):
        return [c for c in tool_calls(self.commands(), "skopeo") if c[1:2] == ["copy"]]

    def test_stable_adds_latest(self):
        result = self.run_chain(["Publish release tags"],
                                overrides={"inputs.target": "stable", "inputs.version": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        targets = [c[-1] for c in self.copies()]
        self.assertTrue(any(t.endswith(":stable") for t in targets), targets)
        self.assertTrue(any(t.endswith(":latest") for t in targets), targets)

    def test_beta_does_not_add_latest(self):
        result = self.run_chain(["Publish release tags"],
                                overrides={"inputs.target": "beta", "inputs.version": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        targets = [c[-1] for c in self.copies()]
        self.assertTrue(any(t.endswith(":beta") for t in targets), targets)
        self.assertFalse(any(t.endswith(":latest") for t in targets), targets)


if __name__ == "__main__":
    unittest.main()
