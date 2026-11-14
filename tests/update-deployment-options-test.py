#!/usr/bin/python3
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, OSTree

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_spec = importlib.util.spec_from_file_location(
    "_deployment_store_fixture", _HERE / "update-deployment-store-test.py"
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)

from updates import deployment_options  # noqa: E402

ROOT_UUID = "11111111-1111-4111-8111-111111111111"
BOOT_UUID = "22222222-2222-4222-8222-222222222222"
OTHER_UUID = "33333333-3333-4333-8333-333333333333"
UPPER_UUID = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"

BASE_TOKENS = [
    f"root=UUID={ROOT_UUID}",
    f"boot=UUID={BOOT_UUID}",
    "rootflags=subvol=root",
    "console=tty0",
    "console=ttyS0",
]


class DeploymentOptionsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _fixture.DeploymentStoreIntegrationTests.setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        _fixture.DeploymentStoreIntegrationTests.tearDownClass()

    def setUp(self) -> None:
        self.f = _fixture.DeploymentStoreIntegrationTests(
            "test_two_updates_merges_retains_and_reuses"
        )
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        current = self._dep(self.f.initial_binding)
        options = current.get_bootconfig().get("options")
        ostree_token = next(
            tok for tok in options.split() if tok.startswith("ostree=")
        )
        new_kargs = BASE_TOKENS + [
            f"armada.deployment={self.f.initial_binding}",
            ostree_token,
        ]
        self._set_kargs(self.f.initial_binding, new_kargs)

    def _dep(self, wanted: str):
        for dep in self.f._sysroot().get_deployments():
            if _fixture.binding(dep) == wanted:
                return dep
        raise AssertionError(f"no deployment with binding {wanted}")

    def _set_kargs(self, binding: str, tokens: list[str]) -> None:
        sysroot = self.f._sysroot()
        matches = [
            dep
            for dep in sysroot.get_deployments()
            if _fixture.binding(dep) == binding
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(sysroot.deployment_set_kargs(matches[0], tokens, None))

    def _set_pinned(self, binding: str, value: bool) -> None:
        sysroot = self.f._sysroot()
        matches = [
            dep
            for dep in sysroot.get_deployments()
            if _fixture.binding(dep) == binding
        ]
        self.assertEqual(len(matches), 1)
        self.assertTrue(sysroot.deployment_set_pinned(matches[0], value))
        self.assertEqual(self._dep(binding).is_pinned(), value)

    def _read(self, target_binding: str, current: str | None = None):
        return deployment_options.read_deployment_options(
            sysroot_path=self.f.root,
            current_binding=current or self.f.initial_binding,
            target_binding=target_binding,
        )

    def _target_options(self, binding: str) -> str:
        options = self._dep(binding).get_bootconfig().get("options")
        self.assertIsInstance(options, str)
        return options.strip()

    def _bls_path(self, binding: str) -> Path:
        wanted = self._target_options(binding)
        ostree_tok = next(
            tok for tok in wanted.split() if tok.startswith("ostree=")
        )
        entries = self.f.root / "boot/loader/entries"
        matches: list[Path] = []
        for path in sorted(entries.glob("*.conf")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("options "):
                    opts = line[len("options "):].strip()
                    if opts == wanted or ostree_tok in opts.split():
                        matches.append(path)
                        break
        self.assertEqual(len(matches), 1, f"expected 1 BLS entry for {binding}")
        return matches[0]

    def _rewrite_bls_options(self, path: Path, new_options: str) -> None:
        raw = path.read_text(encoding="utf-8")
        rewritten: list[str] = []
        replaced = 0
        for line in raw.splitlines(keepends=True):
            ending = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            if line.startswith("options "):
                rewritten.append(f"options {new_options}{ending}")
                replaced += 1
            else:
                rewritten.append(line)
        self.assertEqual(replaced, 1)
        path.write_text("".join(rewritten), encoding="utf-8")

    def _snapshot_bootloader(self) -> dict:
        snap: dict[str, bytes] = {}
        boot = self.f.root / "boot"
        for path in sorted(boot.rglob("*")):
            rel = str(path.relative_to(boot))
            if path.is_symlink():
                snap["L:" + rel] = os.readlink(path).encode()
            elif path.is_file():
                snap["F:" + rel] = path.read_bytes()
        return snap

    def _assert_unchanged(self, snap: dict[str, bytes]) -> None:
        self.assertEqual(self._snapshot_bootloader(), snap)
        self.f._assert_user_data_unchanged()

    def test_readonly_read_success_and_pointer_resolution(self) -> None:
        target = self.f._call(1)
        tgt_binding = target["deployment"]
        self._dep(tgt_binding)
        current_path = self.f.current_path
        target_path = Path(target["path"])
        snap = self._snapshot_bootloader()
        trees_before = {
            "cur_usr": _fixture.tree_digest(current_path / "usr"),
            "cur_etc": _fixture.tree_digest(current_path / "etc"),
            "tgt_usr": _fixture.tree_digest(target_path / "usr"),
            "tgt_etc": _fixture.tree_digest(target_path / "etc"),
        }
        result = self._read(tgt_binding)
        self._assert_unchanged(snap)
        self.assertEqual(
            {
                "cur_usr": _fixture.tree_digest(current_path / "usr"),
                "cur_etc": _fixture.tree_digest(current_path / "etc"),
                "tgt_usr": _fixture.tree_digest(target_path / "usr"),
                "tgt_etc": _fixture.tree_digest(target_path / "etc"),
            },
            trees_before,
        )
        options = result["options"]
        self.assertIn("rootflags=subvol=root", options)
        lower = options.index("console=tty0")
        upper = options.index("console=ttyS0")
        self.assertLess(lower, upper)
        self.assertEqual(options.count("console="), 2)
        self.assertEqual(options.count("root="), 1)
        self.assertEqual(options.count("boot="), 1)
        self.assertIn(f"root=UUID={ROOT_UUID}", options)
        self.assertIn(f"boot=UUID={BOOT_UUID}", options)
        self.assertEqual(result["rootUuid"], ROOT_UUID)
        self.assertEqual(result["bootUuid"], BOOT_UUID)
        armada = [
            tok for tok in options.split() if tok.startswith("armada.deployment=")
        ]
        self.assertEqual(armada, [f"armada.deployment={tgt_binding}"])
        self.assertEqual(result["deployment"], tgt_binding)
        self.assertEqual(result["path"], target["path"])
        ostree_tok = next(
            tok for tok in self._target_options(tgt_binding).split()
            if tok.startswith("ostree=")
        )
        pointer = self.f.root / ostree_tok[len("ostree="):].lstrip("/")
        self.assertTrue(pointer.is_symlink())
        self.assertEqual(
            pointer.resolve(strict=True), Path(target["path"])
        )

    def test_two_consecutive_appends_then_reads_and_idempotent_reuse(self) -> None:
        initial_options = self._target_options(self.f.initial_binding)
        first = self.f._call(1)
        second = self.f._call(2, current_binding=first["deployment"])
        self.assertNotEqual(first["deployment"], second["deployment"])
        self.assertEqual(
            self.f._bindings(),
            [self.f.initial_binding, first["deployment"], second["deployment"]],
        )
        for dep_binding in (
            self.f.initial_binding,
            first["deployment"],
            second["deployment"],
        ):
            armada = [
                tok for tok in self._target_options(dep_binding).split()
                if tok.startswith("armada.deployment=")
            ]
            self.assertEqual(armada, [f"armada.deployment={dep_binding}"])
        self.assertEqual(
            [t for t in self._target_options(self.f.initial_binding).split() if not t.startswith("ostree=")],
            [t for t in initial_options.split() if not t.startswith("ostree=")],
        )
        result = self._read(first["deployment"])
        self.assertEqual(result["deployment"], first["deployment"])
        self.assertEqual(
            [
                tok for tok in result["options"].split()
                if tok.startswith("armada.deployment=")
            ],
            [f"armada.deployment={first['deployment']}"],
        )
        snap = self._snapshot_bootloader()
        result2 = self._read(
            second["deployment"], current=first["deployment"]
        )
        self._assert_unchanged(snap)
        self.assertEqual(result2["rootUuid"], result["rootUuid"])
        self.assertEqual(result2["bootUuid"], result["bootUuid"])
        self.assertEqual(
            [
                tok for tok in result2["options"].split()
                if tok.startswith("armada.deployment=")
            ],
            [f"armada.deployment={second['deployment']}"],
        )
        reuse_snap = self._snapshot_bootloader()
        reused = self.f._call(2, current_binding=first["deployment"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["deployment"], second["deployment"])
        self._assert_unchanged(reuse_snap)
        self.assertEqual(
            self.f._bindings(),
            [self.f.initial_binding, first["deployment"], second["deployment"]],
        )
        final = self._read(second["deployment"], current=first["deployment"])
        self.assertEqual(
            [
                tok for tok in final["options"].split()
                if tok.startswith("armada.deployment=")
            ],
            [f"armada.deployment={second['deployment']}"],
        )
        self.assertEqual(
            [t for t in self._target_options(self.f.initial_binding).split() if not t.startswith("ostree=")],
            [t for t in initial_options.split() if not t.startswith("ostree=")],
        )
        for dep_binding in self.f._bindings():
            self.assertTrue(self._dep(dep_binding).is_pinned())
        self.f._assert_user_data_unchanged()

    def test_reuse_repairs_stale_inherited_armada_binding(self) -> None:
        target = self.f._call(1)
        tgt_binding = target["deployment"]
        options = self._target_options(tgt_binding)
        tokens = [
            tok for tok in options.split()
            if not tok.startswith("armada.deployment=")
        ]
        tokens.append(f"armada.deployment={self.f.initial_binding}")
        self._set_kargs(tgt_binding, tokens)
        stale_armada = [
            tok for tok in self._target_options(tgt_binding).split()
            if tok.startswith("armada.deployment=")
        ]
        self.assertEqual(
            stale_armada, [f"armada.deployment={self.f.initial_binding}"]
        )
        repaired = self.f._call(1)
        self.assertTrue(repaired["reused"])
        self.assertEqual(repaired["deployment"], tgt_binding)
        repaired_armada = [
            tok for tok in self._target_options(tgt_binding).split()
            if tok.startswith("armada.deployment=")
        ]
        self.assertEqual(repaired_armada, [f"armada.deployment={tgt_binding}"])
        result = self._read(tgt_binding)
        self.assertEqual(result["deployment"], tgt_binding)
        self.assertEqual(
            [
                tok for tok in result["options"].split()
                if tok.startswith("armada.deployment=")
            ],
            [f"armada.deployment={tgt_binding}"],
        )
        current_armada = [
            tok for tok in self._target_options(self.f.initial_binding).split()
            if tok.startswith("armada.deployment=")
        ]
        self.assertEqual(
            current_armada, [f"armada.deployment={self.f.initial_binding}"]
        )
        self.f._assert_user_data_unchanged()
        self.assertTrue(self._dep(self.f.initial_binding).is_pinned())

    def test_invalid_target_options_fail_readonly(self) -> None:
        target = self.f._call(1)
        tgt_binding = target["deployment"]
        bls_path = self._bls_path(tgt_binding)
        original_bytes = bls_path.read_bytes()
        original_tokens = self._target_options(tgt_binding).split()
        armada_tok = f"armada.deployment={tgt_binding}"
        stale_binding = f"default/{'0' * 64}.0"

        def replace_prefix(prefix: str, replacement: list[str]) -> list[str]:
            out: list[str] = []
            for tok in original_tokens:
                if tok.startswith(prefix):
                    out.extend(replacement)
                else:
                    out.append(tok)
            return out

        cases: dict[str, tuple[list[str], str]] = {
            "root-uuid-mismatch": (
                replace_prefix("root=", [f"root=UUID={OTHER_UUID}"]),
                r"(?i)root.*(mismatch|differ|match)",
            ),
            "duplicate-root": (
                replace_prefix(
                    "root=",
                    [f"root=UUID={ROOT_UUID}", f"root=UUID={ROOT_UUID}"],
                ),
                r"(?i)root",
            ),
            "zero-root-uuid": (
                replace_prefix(
                    "root=",
                    ["root=UUID=00000000-0000-0000-0000-000000000000"],
                ),
                r"(?i)(root|uuid|zero)",
            ),
            "uppercase-root-uuid": (
                replace_prefix("root=", [f"root=UUID={UPPER_UUID}"]),
                r"(?i)(root|uuid)",
            ),
            "same-root-boot": (
                replace_prefix("boot=", [f"boot=UUID={ROOT_UUID}"]),
                r"(?i)(root.*boot|boot.*root|distinct|same|equal)",
            ),
            "conflicting-armada": (
                replace_prefix(
                    "armada.deployment=",
                    [f"armada.deployment={stale_binding}"],
                ),
                r"(?i)armada\.deployment",
            ),
            "duplicate-armada": (
                replace_prefix(
                    "armada.deployment=", [armada_tok, armada_tok]
                ),
                r"(?i)armada\.deployment",
            ),
            "bare-armada": (
                replace_prefix("armada.deployment=", ["armada.deployment"]),
                r"(?i)armada\.deployment",
            ),
            "rd-break": (
                original_tokens + ["rd.break"],
                r"(?i)(rd\.break|forbidden|disallowed|invalid|unsafe)",
            ),
            "init": (
                original_tokens + ["init=/bin/sh"],
                r"target options forbid init",
            ),
        }
        clean_snap = self._snapshot_bootloader()
        for name, (tokens, pattern) in cases.items():
            with self.subTest(name=name):
                try:
                    self._rewrite_bls_options(bls_path, " ".join(tokens))
                    mutated_snap = self._snapshot_bootloader()
                    with self.assertRaisesRegex(
                        deployment_options.DeploymentOptionsError, pattern
                    ):
                        self._read(tgt_binding)
                    self._assert_unchanged(mutated_snap)
                finally:
                    bls_path.write_bytes(original_bytes)
                result = self._read(tgt_binding)
                self.assertEqual(result["deployment"], tgt_binding)
        self._assert_unchanged(clean_snap)

    def test_wrong_ostree_pointer_fails_and_restores(self) -> None:
        target = self.f._call(1)
        tgt_binding = target["deployment"]
        bls_path = self._bls_path(tgt_binding)
        original_bytes = bls_path.read_bytes()
        original_tokens = self._target_options(tgt_binding).split()
        current_options = self._target_options(self.f.initial_binding)
        current_ostree = next(
            tok for tok in current_options.split() if tok.startswith("ostree=")
        )
        swapped = " ".join(
            current_ostree if tok.startswith("ostree=") else tok
            for tok in original_tokens
        )
        clean_snap = self._snapshot_bootloader()
        try:
            self._rewrite_bls_options(bls_path, swapped)
            mutated_snap = self._snapshot_bootloader()
            with self.assertRaisesRegex(
                deployment_options.DeploymentOptionsError,
                r"(?i)(ostree|bindings must each appear once)",
            ):
                self._read(tgt_binding)
            self._assert_unchanged(mutated_snap)
        finally:
            bls_path.write_bytes(original_bytes)
        self.assertEqual(self._read(tgt_binding)["deployment"], tgt_binding)
        self._assert_unchanged(clean_snap)

    def test_unpinned_unknown_and_lock_contention(self) -> None:
        target = self.f._call(1)
        tgt_binding = target["deployment"]
        self._set_pinned(tgt_binding, False)
        with self.assertRaises(deployment_options.DeploymentOptionsError):
            self._read(tgt_binding)
        self._set_pinned(tgt_binding, True)

        with self.assertRaises(deployment_options.DeploymentOptionsError):
            self._read(f"default/{'0' * 64}.0")
        with self.assertRaises(deployment_options.DeploymentOptionsError):
            self._read(self.f.initial_binding)

        contender = OSTree.Sysroot.new(
            Gio.File.new_for_path(str(self.f.root))
        )
        ok, acquired = contender.try_lock()
        self.assertTrue(ok and acquired)
        try:
            with self.assertRaisesRegex(
                deployment_options.DeploymentOptionsError, "busy"
            ):
                self._read(tgt_binding)
        finally:
            contender.unlock()
        result = self._read(tgt_binding)
        self.assertEqual(result["deployment"], tgt_binding)
        self.f._assert_user_data_unchanged()


if __name__ == "__main__":
    unittest.main()
