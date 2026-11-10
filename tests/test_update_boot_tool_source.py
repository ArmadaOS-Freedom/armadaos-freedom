#!/usr/bin/env python3
"""Unit tests for build_files/prepare-boot-tool-source.py (stdlib only)."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "build_files" / "prepare-boot-tool-source.py"


def load_module():
    spec = importlib.util.spec_from_file_location("prepare_boot_tool_source", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROPS_TEMPLATE = """<Project>
  <PropertyGroup>
    <Version>{ver}</Version>
  </PropertyGroup>
</Project>
"""

CSPROJ_TEMPLATE = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""

FIXTURE_PEM = b"-----BEGIN FAKE PUBLIC KEY-----\nfixture-marker-bytes\n-----END FAKE PUBLIC KEY-----\n"
FIXTURE_KEY_SHA = hashlib.sha256(FIXTURE_PEM).hexdigest()


def make_fixture(root: Path, *, with_boottool_csproj=True, extra=None):
    """Create minimal valid source tree matching module constants."""
    mod = load_module()
    boottool_csproj = "src/FreedomInstaller.BootTool/FreedomInstaller.BootTool.csproj"
    for cs in mod.REQUIRED_CSPROJ:
        if not with_boottool_csproj and cs == boottool_csproj:
            continue
        p = root / cs
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CSPROJ_TEMPLATE, encoding="utf-8")
    if not with_boottool_csproj:
        # wrong alternative csproj while Core csproj remains present
        p = root / "src" / "FreedomInstaller.BootTool" / "BootTool.x.csproj"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CSPROJ_TEMPLATE, encoding="utf-8")
    for rel in mod.ROOT_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((PROPS_TEMPLATE.format(ver="0.8.0") if rel.endswith(".props")
                      else "fixture license\n"), encoding="utf-8")
    for rel in mod.MAINTENANCE + mod.DOCS + mod.TOOLS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"fixture content {rel}\n", encoding="utf-8")
    key = root / mod.TESTKEY
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(FIXTURE_PEM)
    for d in mod.RECURSIVE_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
        # differing content per file path
        (root / d / "code.cs").write_text(f"// code for {d}\n", encoding="utf-8")
        (root / d / "res").mkdir(exist_ok=True)
        (root / d / "res" / "r.txt").write_text(f"res {d}\n", encoding="utf-8")
    if extra:
        extra(root)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bt-src-test-")
        self.tmp = Path(self._tmp.name)
        self.mod = load_module()
        patcher = mock.patch.object(self.mod, "TESTKEY_SHA256", FIXTURE_KEY_SHA)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def export(self, source, dest, mode="local", revision=None):
        return self.mod.export_source(source, dest, mode=mode, revision=revision)


class TestValidLocalExport(Base):
    def test_valid_local_export(self):
        src = self.tmp / "src" / "repo"
        make_fixture(src, extra=lambda r: (
            (r / "src" / "FreedomInstaller.Core" / "bin").mkdir(),
            (r / "src" / "FreedomInstaller.Core" / "bin" / "x.dll").write_text("junk", encoding="utf-8"),
            (r / "src" / "FreedomInstaller.Core" / "obj").mkdir(),
            (r / "src" / "FreedomInstaller.Core" / "obj" / "x.obj").write_text("junk", encoding="utf-8"),
            (r / "src" / "FreedomInstaller.Core" / "res" / ".git").mkdir(parents=True),
            (r / "src" / "FreedomInstaller.Core" / "res" / ".git" / "HEAD").write_text("junk", encoding="utf-8"),
            (r / "src" / "FreedomInstaller.Core" / "res" / "obj").mkdir(parents=True),
            (r / "src" / "FreedomInstaller.Core" / "res" / "obj" / "nested.obj").write_text("junk", encoding="utf-8"),
            (r / ".git").mkdir(),
            (r / ".git" / "config").write_text("junk", encoding="utf-8"),
            (r / "unknownroot.txt").write_text("no", encoding="utf-8"),
            (r / "privatefile.pem").write_text("no", encoding="utf-8"),
            (r / "tools" / "boot-tool-inputs" / "gbl-6.2.192-mod2-prc.efi").write_bytes(b"MZ\x90\x00"),
            (r / "cache" / "efi").mkdir(parents=True),
            (r / "cache" / "efi" / "c.bin").write_bytes(b"\x00\x01"),
        ))
        dest = self.tmp / "out"
        dest.mkdir()
        report = self.export(src, dest)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(report["sourceMode"], "local")
        self.assertIsNone(report["revision"])
        self.assertEqual(report["version"], "0.8.0")
        paths = [f["path"] for f in report["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("src/FreedomInstaller.BootTool/FreedomInstaller.BootTool.csproj", paths)
        self.assertIn("src/FreedomInstaller.Core/code.cs", paths)
        self.assertIn("src/FreedomInstaller.BootTool/res/r.txt", paths)
        self.assertIn(self.mod.TESTKEY, paths)
        for rel in self.mod.ROOT_FILES + self.mod.MAINTENANCE + self.mod.DOCS + self.mod.TOOLS:
            self.assertIn(rel, paths)
        self.assertNotIn("tools/boot-tool-inputs/gbl-6.2.192-mod2-prc.efi", paths)
        self.assertFalse((dest / "tools" / "boot-tool-inputs" / "gbl-6.2.192-mod2-prc.efi").exists())
        for bad in ("bin", "obj", "/.git/", "unknownroot.txt", "privatefile.pem", "cache/efi/c.bin"):
            self.assertFalse(any(bad in f"/{p}/" for p in paths), bad)
        # actual hash/bytes checks against on-disk and corresponding source files
        for f in report["files"]:
            p = dest / f["path"]
            sp = src / f["path"]
            self.assertTrue(p.is_file(), f["path"])
            data = p.read_bytes()
            self.assertEqual(data, sp.read_bytes(), f["path"])
            self.assertEqual(len(data), f["bytes"])
            self.assertEqual(p.stat().st_size, f["bytes"])
            h = hashlib.sha256(data).hexdigest()
            self.assertEqual(h, f["sha256"])
        # differing contents preserved
        self.assertNotEqual(
            (dest / "src/FreedomInstaller.Core/code.cs").read_text(),
            (dest / "src/FreedomInstaller.BootTool/code.cs").read_text())

    @unittest.skipIf(os.name != "posix", "POSIX executable mode bits required")
    def test_executable_mode_preserved(self):
        mod = self.mod
        def extra(r):
            p = r / "src" / "FreedomInstaller.Core" / "run.sh"
            p.write_text("#!/bin/sh\n", encoding="utf-8")
            p.chmod(0o755)
        src = self.tmp / "src"
        make_fixture(src, extra=extra)
        dest = self.tmp / "out"
        dest.mkdir()
        self.export(src, dest)
        out = dest / "src" / "FreedomInstaller.Core" / "run.sh"
        self.assertTrue(os.stat(out).st_mode & 0o111)


class TestMissingAndKey(Base):
    def test_missing_boottool_csproj_rejected(self):
        src = self.tmp / "src"
        make_fixture(src, with_boottool_csproj=False)
        dest = self.tmp / "out"
        dest.mkdir()
        with self.assertRaises(ValueError):
            self.export(src, dest)

    def test_missing_publisher_required_rejected(self):
        mod = self.mod
        src = self.tmp / "src"
        make_fixture(src)
        (src / "tools" / "publish-boot-tool.py").unlink()
        dest = self.tmp / "out"
        dest.mkdir()
        with self.assertRaises(ValueError):
            self.export(src, dest)

    def test_wrong_key_rejected(self):
        mod = self.mod
        src = self.tmp / "src"
        make_fixture(src)
        (src / mod.TESTKEY).write_bytes(b"-----BEGIN OTHER KEY-----\nwrong\n")
        dest = self.tmp / "out"
        dest.mkdir()
        with self.assertRaises(ValueError) as cm:
            self.export(src, dest)
        self.assertIn("sha mismatch", str(cm.exception))


class TestSymlinks(Base):
    def _mk(self):
        src = self.tmp / "src"
        make_fixture(src)
        return src

    def _try(self, src):
        dest = self.tmp / "out"
        dest.mkdir()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.export(src, dest)

    def test_fixed_file_symlink(self):
        src = self._mk()
        target = self.tmp / "external.txt"
        target.write_text("external", encoding="utf-8")
        (src / "LICENSE").unlink()
        (src / "LICENSE").symlink_to(target)
        self._try(src)

    def test_fixed_parent_dir_symlink(self):
        src = self._mk()
        target = self.tmp / "extdir"
        shutil.move(str(src / "docs"), str(target))
        (src / "docs").symlink_to(target)
        self._try(src)

    def test_recursive_file_symlink(self):
        src = self._mk()
        target = self.tmp / "secret.txt"
        target.write_text("secret", encoding="utf-8")
        (src / "src" / "FreedomInstaller.Core").mkdir(exist_ok=True)
        (src / "src" / "FreedomInstaller.Core" / "link.cs").symlink_to(target)
        self._try(src)

    def test_recursive_dir_symlink(self):
        src = self._mk()
        target = self.tmp / "outside"
        target.mkdir()
        (target / "stolen.cs").write_text("stolen", encoding="utf-8")
        (src / "src" / "FreedomInstaller.Core").mkdir(exist_ok=True)
        (src / "src" / "FreedomInstaller.Core" / "linkdir").symlink_to(target)
        self._try(src)


class TestRevisionsAndModes(Base):
    def test_bad_revisions(self):
        src = self.tmp / "src"
        make_fixture(src)
        dest = self.tmp / "out"
        dest.mkdir()
        cases = [None, "0" * 40, "z" * 40, "A" * 40, "abc"]
        for rev in cases:
            with self.subTest(rev=rev):
                with self.assertRaises(ValueError):
                    self.export(src, dest, mode="remote", revision=rev)

    def test_valid_remote_revision_preserved(self):
        src = self.tmp / "src"
        make_fixture(src)
        dest = self.tmp / "out"
        dest.mkdir()
        rev = "0123456789abcdef0123456789abcdef01234567"
        report = self.export(src, dest, mode="remote", revision=rev)
        self.assertEqual(report["revision"], rev)
        self.assertEqual(report["sourceMode"], "remote")

    def test_invalid_mode(self):
        src = self.tmp / "src"
        make_fixture(src)
        dest = self.tmp / "out"
        dest.mkdir()
        with self.assertRaises(ValueError):
            self.export(src, dest, mode="weird")


class TestCli(Base):
    def _run_cli(self, argv):
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as cm:
                self.mod.main()
        return cm.exception.code

    def test_nonempty_output_keeps_sentinel_and_exits_1(self):
        src = self.tmp / "src"
        make_fixture(src)
        out = self.tmp / "out"
        out.mkdir()
        (out / "sentinel").write_text("keep", encoding="utf-8")
        code = self._run_cli(["prog", "--output", str(out), "--local-source", str(src)])
        self.assertEqual(code, 1)
        self.assertEqual((out / "sentinel").read_text(), "keep")

    def test_symlink_output_refused(self):
        src = self.tmp / "src"
        make_fixture(src)
        real = self.tmp / "realout"
        real.mkdir()
        link = self.tmp / "linkout"
        link.symlink_to(real)
        code = self._run_cli(["prog", "--output", str(link), "--local-source", str(src)])
        self.assertEqual(code, 1)
        self.assertTrue(link.is_symlink())

    def test_empty_output_success_no_staging_leftover(self):
        src = self.tmp / "src"
        make_fixture(src)
        out = self.tmp / "out"
        out.mkdir()
        with mock.patch.object(sys, "argv",
                               ["prog", "--output", str(out), "--local-source", str(src)]):
            with mock.patch.object(self.mod, "_git", side_effect=ValueError("no git")):
                with mock.patch("builtins.print"):
                    self.mod.main()
        self.assertTrue((out / "boot-tool-source.json").is_file())
        report = json.loads((out / "boot-tool-source.json").read_text(encoding="utf-8"))
        self.assertEqual(report["sourceMode"], "local")
        self.assertIsNone(report["revision"])
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".boottool-src-")]
        self.assertEqual(leftovers, [])


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class TestRemoteGitIntegration(Base):
    def test_sparse_clone_roundtrip(self):
        fixture = self.tmp / "gitrepo"
        make_fixture(fixture)
        env = dict(os.environ)
        def git(*args, cwd):
            subprocess.run(["git", "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                            *args], cwd=cwd, check=True, capture_output=True, env=env)
        git("init", "-b", "main", ".", cwd=fixture)
        git("add", "-A", cwd=fixture)
        git("commit", "-m", "fixture", cwd=fixture)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=fixture,
                              check=True, capture_output=True, text=True).stdout.strip()
        # sanity: git must actually accept the sparse paths (relative paths)
        with mock.patch.object(self.mod, "REPO_URL", str(fixture)):
            stage = self.tmp / "stage"
            stage.mkdir()
            checkout, rev = self.mod.resolve_remote(stage)
            self.assertEqual(rev, head)
            dest = stage / "out"
            dest.mkdir()
            report = self.mod.export_source(checkout, dest, mode="remote", revision=rev)
        self.assertEqual(report["revision"], head)
        self.assertEqual(report["sourceMode"], "remote")
        paths = [f["path"] for f in report["files"]]
        for rel in self.mod.ROOT_FILES + self.mod.REQUIRED_CSPROJ + [self.mod.TESTKEY]:
            self.assertIn(rel, paths)


if __name__ == "__main__":
    unittest.main()
