#!/usr/bin/python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, GLib, OSTree

import update_boot_payload_fixture as payload_fixture
from updates import boot_payload, deployment_store


DIGESTS = [f"sha256:{c * 64}" for c in "abc"]
REVISIONS = [c * 40 for c in "123"]
VERSIONS = ["fixture-1", "fixture-2", "fixture-3"]
ORIGINS = [
    "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
    f"armadaos-freedom:stable@{digest}"
    for digest in DIGESTS
]


def run(*args: str) -> str:
    try:
        return subprocess.run(
            list(args), check=True, capture_output=True, text=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"command failed: {exc.args!r}\n"
            f"stderr:\n{exc.stderr}\nstdout:\n{exc.stdout}"
        ) from exc


def binding(dep) -> str:
    return (
        f"{dep.get_osname()}/{dep.get_csum()}."
        f"{dep.get_deployserial()}"
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root)).encode()
        if path.is_symlink():
            digest.update(b"L" + rel + os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"F" + rel + path.read_bytes())
    return digest.hexdigest()


class DeploymentStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload_fixture.UpdateBootPayloadFixture.setUpClass()
        cls.model = payload_fixture.UpdateBootPayloadFixture.models[0]

    @classmethod
    def tearDownClass(cls) -> None:
        payload_fixture.UpdateBootPayloadFixture.tearDownClass()

    def setUp(self) -> None:
        # OSTree marks deployment directories immutable, which the privileged
        # CI runner cannot remove. Its own test harness exports this flag so
        # cleanup works; it must be visible before any sysroot is created.
        debug_flags = [
            flag
            for flag in os.environ.get("OSTREE_SYSROOT_DEBUG", "").split(":")
            if flag
        ]
        if "mutable-deployments" not in debug_flags:
            debug_flags.append("mutable-deployments")
        debug_env = mock.patch.dict(
            os.environ, {"OSTREE_SYSROOT_DEBUG": ":".join(debug_flags)}
        )
        debug_env.start()
        self.addCleanup(debug_env.stop)
        self.payload = payload_fixture.UpdateBootPayloadFixture()
        self.addCleanup(self.payload.doCleanups)
        tmp = tempfile.TemporaryDirectory(
            prefix="deployment-store-", dir=os.environ.get("TMPDIR")
        )
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sysroot"
        self.root.mkdir()
        run("ostree", "admin", "init-fs", "--modern", str(self.root))
        run(
            "ostree", "config",
            f"--repo={self.root / 'ostree/repo'}",
            "set", "sysroot.bootloader", "none"
        )
        run(
            "ostree", "admin", "os-init",
            f"--sysroot={self.root}", "default"
        )
        (self.root / "boot").mkdir(exist_ok=True)
        self.commits = [
            self._commit_tree(str(index + 1), index)
            for index in range(3)
        ]
        run(
            "ostree", "admin", "deploy",
            f"--sysroot={self.root}", "--os=default", "--no-merge",
            self.commits[0]
        )
        self.initial = self._deployments()[0]
        self.current_path = Path(
            self._sysroot().get_deployment_directory(self.initial).get_path()
        )
        self.initial_binding = binding(self.initial)
        (self.current_path / "etc/custom.conf").write_text(
            "site=retained\n", encoding="utf-8"
        )
        self.markers = {
            self.root / "var/root-marker": b"root var\n",
            self.root / "var/home/game-marker": b"game\n",
            self.root / "ostree/deploy/default/var/home/personal-marker": b"personal\n",
        }
        for path in self.markers:
            if not path.parent.resolve(strict=False).is_relative_to(
                self.root.resolve()
            ):
                raise AssertionError(f"marker escapes sysroot: {path}")
        for path, data in self.markers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def _sysroot(self):
        sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(self.root)))
        self.assertTrue(sysroot.load(None))
        return sysroot

    def _deployments(self):
        return list(self._sysroot().get_deployments() or [])

    def _bindings(self) -> list[str]:
        return [binding(dep) for dep in self._deployments()]

    def _refs(self) -> dict[str, str]:
        ok, repo = self._sysroot().get_repo(None)
        self.assertTrue(ok and repo is not None)
        ok, refs = repo.list_refs(None, None)
        self.assertTrue(ok, "listing repository refs failed")
        return dict(refs)

    def _pending_refs(self) -> list[str]:
        return [
            name for name in self._refs()
            if name.startswith("armada-update/pending/")
        ]

    def _commit_exists(self, checksum: str) -> bool:
        ok, repo = self._sysroot().get_repo(None)
        self.assertTrue(ok and repo is not None)
        try:
            ok, variant, state = repo.load_commit(checksum)
        except GLib.Error:
            return False
        return bool(ok and variant is not None and int(state) == 0)

    def _options(self, wanted: str) -> list[str]:
        dep = next(
            item for item in self._deployments() if binding(item) == wanted
        )
        return (dep.get_bootconfig().get("options") or "").split()

    def _armada_tokens(self, wanted: str) -> list[str]:
        return [
            tok for tok in self._options(wanted)
            if tok.startswith("armada.deployment=")
        ]

    def _pin_state(self) -> dict[str, bool]:
        return {binding(dep): dep.is_pinned() for dep in self._deployments()}

    def _commit_tree(
        self,
        revision: str,
        metadata_index: int,
        *,
        architecture: str = "arm64",
        damage=None,
    ) -> str:
        source = self.payload._make_root()
        for rel in ("usr/lib/os-release", "usr/etc/os-release"):
            path = source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "ID=armada\nNAME=Armada\nVERSION_ID=fixture\n",
                encoding="utf-8",
            )
        (source / "usr/etc/fixture.conf").write_text(
            f"default={revision}\n", encoding="utf-8"
        )
        (source / "usr/share/revision").write_text(
            revision + "\n", encoding="utf-8"
        )
        manifest = boot_payload.create_manifest(source)
        manifest_path = source / "usr/share/armada/update-boot/manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if damage is not None:
            damage(source)
        image = {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": {
                "org.opencontainers.image.revision": REVISIONS[metadata_index],
                "org.opencontainers.image.version": VERSIONS[metadata_index],
            }},
        }
        branch = f"fixture-{revision}-{architecture}"
        run(
            "ostree", "commit", f"--repo={self.root / 'ostree/repo'}",
            f"--branch={branch}", f"--tree=dir={source}",
            "--subject=fixture",
            f"--add-metadata-string=ostree.manifest-digest={DIGESTS[metadata_index]}",
            "--add-metadata-string=ostree.container.image-config="
            + json.dumps(image, separators=(",", ":")),
        )
        return run(
            "ostree", "rev-parse", f"--repo={self.root / 'ostree/repo'}",
            branch
        )

    def _call(self, index: int = 1, **overrides):
        args = {
            "sysroot_path": self.root,
            "current_binding": self.initial_binding,
            "target_commit": self.commits[index],
            "oci_digest": DIGESTS[index],
            "source_revision": REVISIONS[index],
            "version": VERSIONS[index],
            "model": self.model,
            "origin_ref": ORIGINS[index],
        }
        args.update(overrides)
        return deployment_store.append_verified_deployment(**args)

    def _assert_user_data_unchanged(self) -> None:
        for path, expected in self.markers.items():
            self.assertEqual(path.read_bytes(), expected, str(path))

    def _assert_pinned(self, expected_binding: str) -> None:
        matches = [
            dep for dep in self._deployments()
            if binding(dep) == expected_binding
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].is_pinned(), expected_binding)

    def test_two_updates_merges_retains_and_reuses(self) -> None:
        old_hashes = {
            self.initial_binding: (
                tree_digest(self.current_path / "etc"),
                tree_digest(self.current_path / "usr"),
            )
        }
        second = self._call(1)
        self.assertFalse(second["reused"])
        self.assertEqual(second["retainedDeployments"], [self.initial_binding])
        self.assertEqual(second["kernelRelease"], payload_fixture.KVER)
        self.assertEqual(second["model"], self.model)
        second_path = Path(second["path"])
        self.assertEqual(
            second_path,
            self.root / "ostree/deploy/default/deploy" /
            f"{self.commits[1]}.{second['deployment'].rsplit('.', 1)[1]}"
        )
        self.assertEqual(
            (second_path / "etc/custom.conf").read_text(encoding="utf-8"),
            "site=retained\n"
        )
        self.assertEqual(
            (second_path / "etc/fixture.conf").read_text(encoding="utf-8"),
            "default=2\n"
        )
        self.assertEqual(self._bindings(), [self.initial_binding, second["deployment"]])
        self.assertEqual(self._deployments()[0].get_csum(), self.commits[0])
        self._assert_pinned(self.initial_binding)
        self._assert_pinned(second["deployment"])
        self.assertEqual(
            (tree_digest(self.current_path / "etc"),
             tree_digest(self.current_path / "usr")),
            old_hashes[self.initial_binding]
        )
        self._assert_user_data_unchanged()

        old_hashes[second["deployment"]] = (
            tree_digest(second_path / "etc"), tree_digest(second_path / "usr")
        )
        third = self._call(2, current_binding=second["deployment"])
        self.assertFalse(third["reused"])
        self.assertEqual(
            third["retainedDeployments"],
            [self.initial_binding, second["deployment"]]
        )
        third_path = Path(third["path"])
        self.assertEqual(third["kernelRelease"], payload_fixture.KVER)
        self.assertEqual(third["model"], self.model)
        self.assertEqual(
            (third_path / "etc/custom.conf").read_text(encoding="utf-8"),
            "site=retained\n"
        )
        self.assertEqual(
            (third_path / "etc/fixture.conf").read_text(encoding="utf-8"),
            "default=3\n"
        )
        expected = [self.initial_binding, second["deployment"], third["deployment"]]
        self.assertEqual(self._bindings(), expected)
        self.assertEqual(self._deployments()[0].get_csum(), self.commits[0])
        for item in expected:
            self._assert_pinned(item)
        for old_binding, path in (
            (self.initial_binding, self.current_path),
            (second["deployment"], second_path),
        ):
            self.assertEqual(
                (tree_digest(path / "etc"), tree_digest(path / "usr")),
                old_hashes[old_binding]
            )
        self._assert_user_data_unchanged()

        reused = self._call(2, current_binding=second["deployment"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["deployment"], third["deployment"])
        self.assertEqual(reused["path"], third["path"])
        self.assertEqual(reused["retainedDeployments"], expected)
        self.assertEqual(self._bindings(), expected)
        self._assert_user_data_unchanged()

    def test_fresh_first_install_appends_digest_only_target(self) -> None:
        repo = str(self.root / "ostree/repo")
        target = self.commits[1]
        # A fresh first-install deployment predates the per-deployment token,
        # so append has to persist the current self-binding before use.
        self.assertEqual(self._armada_tokens(self.initial_binding), [])

        # The target lives in the repository but is named by no ref: dropping
        # the fixture branch leaves it reachable by checksum only, so a real
        # prune deletes it unless the append keeps it alive.
        run("ostree", "refs", f"--repo={repo}", "--delete", "fixture-2-arm64")
        refs_before = self._refs()
        fixture_refs = {
            name: csum for name, csum in refs_before.items()
            if name.startswith("fixture-")
        }
        self.assertNotIn("fixture-2-arm64", fixture_refs)
        self.assertIn("fixture-1-arm64", fixture_refs)
        self.assertIn("fixture-3-arm64", fixture_refs)
        self.assertNotIn(
            target, refs_before.values(), "target commit is still pinned by a ref"
        )
        self.assertEqual(self._pending_refs(), [])
        self.assertTrue(self._commit_exists(target), "target commit object missing")

        original = deployment_store._persist_self_binding
        pruned = []

        def persist_then_prune(sysroot, dep, own_binding, label):
            changed = original(sysroot, dep, own_binding, label)
            if changed:
                # Real pre-registration boundary: the current deployment now
                # names itself, the target is not deployed yet, and no ref
                # holds the target commit.
                self.assertFalse(
                    [item for item in (sysroot.get_deployments() or [])
                     if item.get_csum() == target],
                    "prune must run before the target is registered",
                )
                run("ostree", "prune", "--refs-only", f"--repo={repo}")
                self.assertTrue(
                    self._commit_exists(target),
                    "target commit did not survive the pre-registration prune",
                )
                pruned.append(label)
            return changed

        with mock.patch.object(
            deployment_store, "_persist_self_binding",
            side_effect=persist_then_prune,
        ):
            result = self._call(1)
        self.assertEqual(len(pruned), 1, "pre-registration prune did not run")
        self.assertFalse(result["reused"])
        self.assertTrue(
            result["deployment"].startswith(f"default/{target}."),
            result["deployment"],
        )
        self.assertEqual(
            self._bindings(), [self.initial_binding, result["deployment"]]
        )
        self.assertEqual(
            self._armada_tokens(self.initial_binding),
            [f"armada.deployment={self.initial_binding}"],
        )
        self.assertEqual(
            self._armada_tokens(result["deployment"]),
            [f"armada.deployment={result['deployment']}"],
        )
        self.assertTrue(self._commit_exists(target), "target commit disappeared")
        self.assertEqual(self._pending_refs(), [])
        # Registering the candidate legitimately rewrites internal ostree/*
        # refs, so only the fixture namespace has to be untouched.
        self.assertEqual(
            {name: csum for name, csum in self._refs().items()
             if name.startswith("fixture-")},
            fixture_refs,
            "append changed or dropped an unrelated fixture ref",
        )
        self._assert_user_data_unchanged()

    def test_five_confirmed_updates_apply_promotion_grace(self) -> None:
        commits = [
            self._commit_tree(label, 1) for label in ("4", "5", "6", "7")
        ]

        def append_and_confirm(index: int, previous: str, **kw):
            item = self._call(index, current_binding=previous, **kw)
            deployment_store.promote_confirmed_deployment(
                sysroot_path=self.root,
                current_binding=item["deployment"],
                previous_binding=previous,
            )
            return item

        first = append_and_confirm(1, self.initial_binding)
        self.assertEqual(
            set(self._bindings()),
            {self.initial_binding, first["deployment"]},
        )
        self._assert_pinned(self.initial_binding)
        self._assert_pinned(first["deployment"])
        self._assert_user_data_unchanged()

        second = append_and_confirm(2, first["deployment"])
        self.assertEqual(
            set(self._bindings()),
            {
                self.initial_binding,
                first["deployment"],
                second["deployment"],
            },
        )
        for item in self._bindings():
            self._assert_pinned(item)
        self._assert_user_data_unchanged()

        # Round three exceeds the retention window, which only releases the
        # pin of the oldest stale updater deployment. The unmarked initial
        # admin deployment is never a candidate.
        third = append_and_confirm(
            1, second["deployment"], target_commit=commits[0]
        )
        pins = self._pin_state()
        self.assertEqual(len(pins), 4)
        self.assertIn(self.initial_binding, pins)
        self.assertTrue(pins[third["deployment"]])
        self.assertTrue(pins[second["deployment"]])
        self.assertIn(first["deployment"], pins)
        self.assertFalse(pins[first["deployment"]])
        self._assert_user_data_unchanged()

        # The same confirmation is not a distinct one: it must not advance
        # the grace clock or delete the released candidate.
        repeated = deployment_store.promote_confirmed_deployment(
            sysroot_path=self.root,
            current_binding=third["deployment"],
            previous_binding=second["deployment"],
        )
        self.assertFalse(repeated["changed"])
        self.assertEqual(repeated["deployment"], third["deployment"])
        self.assertEqual(self._pin_state(), pins)
        self._assert_user_data_unchanged()

        # Round four is distinct, so the still-unpinned eldest is removed and
        # the next stale deployment inherits the released pin.
        fourth = append_and_confirm(
            1, third["deployment"], target_commit=commits[1]
        )
        pins = self._pin_state()
        self.assertEqual(len(pins), 4)
        self.assertIn(self.initial_binding, pins)
        self.assertNotIn(first["deployment"], pins)
        self.assertTrue(pins[fourth["deployment"]])
        self.assertTrue(pins[third["deployment"]])
        self.assertIn(second["deployment"], pins)
        self.assertFalse(pins[second["deployment"]])
        self._assert_user_data_unchanged()

        fifth = append_and_confirm(
            1, fourth["deployment"], target_commit=commits[2]
        )
        pins = self._pin_state()
        self.assertEqual(len(pins), 4)
        self.assertIn(self.initial_binding, pins)
        self.assertNotIn(first["deployment"], pins)
        self.assertNotIn(second["deployment"], pins)
        self.assertTrue(pins[fifth["deployment"]])
        self.assertTrue(pins[fourth["deployment"]])
        self.assertIn(third["deployment"], pins)
        self.assertFalse(pins[third["deployment"]])
        self.assertEqual(
            (Path(fifth["path"]) / "etc/custom.conf").read_text(
                encoding="utf-8"
            ),
            "site=retained\n",
        )
        self._assert_user_data_unchanged()

        # A real admin pin on the released candidate must survive a distinct
        # confirmation. The new round uses a fresh commit, so the pinned
        # deployment is stale rather than the protected current binding.
        self.assertEqual(
            [item for item, pinned in pins.items() if not pinned],
            [third["deployment"]],
        )
        sysroot = self._sysroot()
        released = [
            dep for dep in sysroot.get_deployments()
            if binding(dep) == third["deployment"]
        ]
        self.assertEqual(len(released), 1)
        self.assertTrue(sysroot.deployment_set_pinned(released[0], True))
        self.assertTrue(self._pin_state()[third["deployment"]])
        sixth = append_and_confirm(
            1, fifth["deployment"], target_commit=commits[3]
        )
        self.assertFalse(sixth["reused"])
        self.assertNotEqual(sixth["deployment"], third["deployment"])
        pins = self._pin_state()
        self.assertIn(third["deployment"], pins)
        self.assertTrue(pins[third["deployment"]])
        self._assert_pinned(sixth["deployment"])
        self._assert_user_data_unchanged()

    def test_metadata_identity_and_binding_reject_without_registration(self) -> None:
        bad_arch = self._commit_tree("wrong-arch", 1, architecture="x86_64")
        cases = {
            "architecture": {"target_commit": bad_arch},
            "revision": {"source_revision": "f" * 40},
            "version": {"version": "wrong-version"},
            "digest": {
                "oci_digest": DIGESTS[2],
                "origin_ref": ORIGINS[2],
            },
            "origin": {"origin_ref": ORIGINS[2]},
            "same-commit": {"target_commit": self.commits[0]},
            "uint32": {
                "current_binding":
                    f"default/{self.commits[0]}.4294967296"
            },
            "unknown-current": {
                "current_binding": f"default/{'0' * 64}.0"
            },
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                before = self._bindings()
                old_etc = tree_digest(self.current_path / "etc")
                old_usr = tree_digest(self.current_path / "usr")
                with self.assertRaises(deployment_store.DeploymentError):
                    self._call(1, **overrides)
                self.assertEqual(self._bindings(), before)
                self.assertEqual(tree_digest(self.current_path / "etc"), old_etc)
                self.assertEqual(tree_digest(self.current_path / "usr"), old_usr)
                self._assert_user_data_unchanged()

    def test_unverifiable_candidate_is_not_registered(self) -> None:
        profile = payload_fixture.UpdateBootPayloadFixture.cfg["profiles"][self.model]

        def remove_dtb(source: Path) -> None:
            (source / f"usr/lib/modules/{payload_fixture.KVER}/dtb" /
             profile["dtb"]).unlink()

        damaged = self._commit_tree("missing-dtb", 1, damage=remove_dtb)
        before = self._bindings()
        old_etc = tree_digest(self.current_path / "etc")
        old_usr = tree_digest(self.current_path / "usr")
        with self.assertRaisesRegex(
            deployment_store.DeploymentError, "manifest verification failed"
        ):
            self._call(1, target_commit=damaged)
        self.assertEqual(self._bindings(), before)
        self.assertTrue(self.current_path.is_dir())
        self.assertEqual(tree_digest(self.current_path / "etc"), old_etc)
        self.assertEqual(tree_digest(self.current_path / "usr"), old_usr)
        self._assert_user_data_unchanged()

    def test_real_sysroot_lock_contention_then_success(self) -> None:
        contender = OSTree.Sysroot.new(Gio.File.new_for_path(str(self.root)))
        ok, acquired = contender.try_lock()
        self.assertTrue(ok and acquired)
        before = self._bindings()
        try:
            with self.assertRaisesRegex(deployment_store.DeploymentError, "busy"):
                self._call(1)
            self.assertEqual(self._bindings(), before)
        finally:
            contender.unlock()
        result = self._call(1)
        self.assertFalse(result["reused"])
        self.assertEqual(
            self._bindings(), [self.initial_binding, result["deployment"]]
        )
        self._assert_user_data_unchanged()

    def test_reuse_rejects_tampered_real_origin(self) -> None:
        result = self._call(1)
        before = self._bindings()
        sysroot = self._sysroot()
        candidate = [
            dep for dep in sysroot.get_deployments()
            if binding(dep) == result["deployment"]
        ]
        self.assertEqual(len(candidate), 1)
        origin_file = OSTree.Sysroot.get_deployment_origin_path(
            sysroot.get_deployment_directory(candidate[0])
        )
        origin_path = Path(origin_file.get_path())
        self.assertTrue(origin_path.is_file())
        self.assertNotIn(
            result["path"], str(origin_path.parent),
            "origin must be the real deployment metadata file"
        )
        original = origin_path.read_text(encoding="utf-8")
        self.assertIn(ORIGINS[1], original)
        origin_path.write_text(
            original.replace(ORIGINS[1], ORIGINS[2]), encoding="utf-8"
        )
        old_etc = tree_digest(self.current_path / "etc")
        old_usr = tree_digest(self.current_path / "usr")
        with self.assertRaisesRegex(
            deployment_store.DeploymentError, "existing target origin mismatch"
        ):
            self._call(1)
        self.assertEqual(self._bindings(), before)
        self.assertEqual(tree_digest(self.current_path / "etc"), old_etc)
        self.assertEqual(tree_digest(self.current_path / "usr"), old_usr)
        self._assert_user_data_unchanged()

    def test_promote_confirmed_reorders_pins_and_is_idempotent(self) -> None:
        old_hashes = {
            self.initial_binding: (
                tree_digest(self.current_path / "etc"),
                tree_digest(self.current_path / "usr"),
            )
        }
        second = self._call(1)
        second_path = Path(second["path"])
        old_hashes[second["deployment"]] = (
            tree_digest(second_path / "etc"), tree_digest(second_path / "usr")
        )
        promoted = deployment_store.promote_confirmed_deployment(
            sysroot_path=self.root,
            current_binding=second["deployment"],
            previous_binding=self.initial_binding,
        )
        self.assertEqual(promoted, {
            "deployment": second["deployment"],
            "previousDeployment": self.initial_binding,
            "changed": True,
        })
        self.assertEqual(
            self._bindings(), [second["deployment"], self.initial_binding]
        )
        for item in self._bindings():
            self._assert_pinned(item)
        for item, path in ((self.initial_binding, self.current_path),
                           (second["deployment"], second_path)):
            self.assertEqual(
                (tree_digest(path / "etc"), tree_digest(path / "usr")),
                old_hashes[item]
            )
        self._assert_user_data_unchanged()

        before = self._bindings()
        repeated = deployment_store.promote_confirmed_deployment(
            sysroot_path=self.root,
            current_binding=second["deployment"],
            previous_binding=self.initial_binding,
        )
        self.assertFalse(repeated["changed"])
        self.assertEqual(self._bindings(), before)
        for item, path in ((self.initial_binding, self.current_path),
                           (second["deployment"], second_path)):
            self.assertEqual(
                (tree_digest(path / "etc"), tree_digest(path / "usr")),
                old_hashes[item]
            )
        self._assert_user_data_unchanged()

        third = self._call(2, current_binding=second["deployment"])
        third_path = Path(third["path"])
        old_hashes[third["deployment"]] = (
            tree_digest(third_path / "etc"), tree_digest(third_path / "usr")
        )
        promoted = deployment_store.promote_confirmed_deployment(
            sysroot_path=self.root,
            current_binding=third["deployment"],
            previous_binding=second["deployment"],
        )
        self.assertTrue(promoted["changed"])
        self.assertEqual(self._bindings(), [
            third["deployment"], second["deployment"], self.initial_binding
        ])
        for item, path in ((self.initial_binding, self.current_path),
                           (second["deployment"], second_path),
                           (third["deployment"], third_path)):
            self._assert_pinned(item)
            self.assertEqual(
                (tree_digest(path / "etc"), tree_digest(path / "usr")),
                old_hashes[item]
            )
        self._assert_user_data_unchanged()

    def test_promote_confirmed_lock_contention_then_success(self) -> None:
        second = self._call(1)
        before = self._bindings()
        old_etc = tree_digest(self.current_path / "etc")
        old_usr = tree_digest(self.current_path / "usr")
        contender = OSTree.Sysroot.new(Gio.File.new_for_path(str(self.root)))
        ok, acquired = contender.try_lock()
        self.assertTrue(ok and acquired)
        try:
            with self.assertRaisesRegex(deployment_store.DeploymentError, "busy"):
                deployment_store.promote_confirmed_deployment(
                    sysroot_path=self.root,
                    current_binding=second["deployment"],
                    previous_binding=self.initial_binding,
                )
            self.assertEqual(self._bindings(), before)
            self._assert_user_data_unchanged()
        finally:
            contender.unlock()
        result = deployment_store.promote_confirmed_deployment(
            sysroot_path=self.root,
            current_binding=second["deployment"],
            previous_binding=self.initial_binding,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self._bindings(), [second["deployment"], self.initial_binding]
        )
        self.assertEqual(tree_digest(self.current_path / "etc"), old_etc)
        self.assertEqual(tree_digest(self.current_path / "usr"), old_usr)
        self._assert_user_data_unchanged()

    def test_promote_confirmed_invalid_identity_or_root_is_inert(self) -> None:
        second = self._call(1)
        second_path = Path(second["path"])
        paths = (self.current_path, second_path)
        before = self._bindings()
        hashes = [(tree_digest(p / "etc"), tree_digest(p / "usr")) for p in paths]
        link = self.root.parent / "sysroot-link"
        link.symlink_to(self.root, target_is_directory=True)
        missing = f"default/{'0' * 64}.0"
        cases = [
            {"current_binding": missing},
            {"previous_binding": missing},
            {"previous_binding": self.initial_binding.replace("default/", "other/")},
            {"previous_binding": second["deployment"]},
            {"sysroot_path": self.root / "missing"},
            {"sysroot_path": link},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                args = dict(
                    sysroot_path=self.root,
                    current_binding=second["deployment"],
                    previous_binding=self.initial_binding,
                )
                args.update(overrides)
                with self.assertRaises(deployment_store.DeploymentError):
                    deployment_store.promote_confirmed_deployment(**args)
                self.assertEqual(self._bindings(), before)
                self.assertEqual(
                    [(tree_digest(p / "etc"), tree_digest(p / "usr")) for p in paths],
                    hashes
                )
                self._assert_user_data_unchanged()


if __name__ == "__main__":
    unittest.main()
