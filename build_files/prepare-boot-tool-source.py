#!/usr/bin/env python3
"""Prepare exact BootTool source context for the container image build stage.

Remote mode resolves ArmadaOS-Freedom/armadaos-freedom-installer main once,
records the actual 40-hex revision, and exports only allow-listed inputs.
Local mode (--local-source) uses an explicit checkout for offline validation
and reports sourceMode "local" without claiming a published clean state.
If required source files are absent, this tool MUST fail clearly; it never
fakes a public SHA and never silently skips BootTool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_URL = "https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer.git"
GIT_TIMEOUT = 120
TESTKEY = "vendor/aosp/testkey_rsa4096.pem"
TESTKEY_SHA256 = "6a224754880a57ab9cbd308267cd157d94cf05a1c8cb851aec4090e045d24121"

ROOT_FILES = ["Directory.Build.props", "LICENSE"]
RECURSIVE_DIRS = ["src/FreedomInstaller.Core", "src/FreedomInstaller.BootTool"]
REQUIRED_CSPROJ = [
    "src/FreedomInstaller.Core/FreedomInstaller.Core.csproj",
    "src/FreedomInstaller.BootTool/FreedomInstaller.BootTool.csproj",
]
MAINTENANCE = [
    "maintenance/fetch_build_inputs.py",
    "maintenance/build-inputs.json",
    "maintenance/armada_tf.py",
]
DOCS = [
    "docs/GblRuntime-NOTICE.txt",
    "docs/GblRuntime-LICENSE.txt",
    "docs/SharpCompress-LICENSE.txt",
]
TOOLS = [
    "tools/publish-boot-tool.py",
    "tools/verify-boot-tool.py",
    "tools/boot-tool.Containerfile",
    "tools/boot-tool.Containerfile.dockerignore",
    "tools/boot-tool-inputs/.gitkeep",
]
EXCLUDED_NAMES = {"bin", "obj", ".git"}
HEX_DIGITS = set("0123456789abcdef")


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _validate_revision(rev: str) -> str:
    if len(rev) != 40 or any(c not in HEX_DIGITS for c in rev) or rev == "0" * 40:
        raise ValueError("revision must be a 40-character lowercase non-zero hex SHA")
    return rev


def _git(args: list[str], cwd: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, check=False, capture_output=True,
            text=True, timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"git {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise ValueError(f"git {' '.join(args[:2])} failed: {err[-1] if err else 'unknown error'}")
    return proc.stdout.strip()


def resolve_remote(tempdir: Path) -> tuple[Path, str]:
    tempdir_abs = tempdir.absolute()
    checkout = tempdir_abs / "checkout"
    _git(["clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
          "--branch", "main", REPO_URL, str(checkout)], tempdir_abs)
    _git(["sparse-checkout", "set", "--no-cone",
          *ROOT_FILES, *RECURSIVE_DIRS, *MAINTENANCE, *DOCS, TESTKEY, *TOOLS],
         checkout)
    _git(["checkout", "--detach"], checkout)
    rev = _validate_revision(_git(["rev-parse", "HEAD"], checkout))
    return checkout, rev


def _ensure_no_symlink_chain(root: Path, rel: Path) -> None:
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"symlink parent directory rejected: {cur.relative_to(root).as_posix()}")
        if not cur.is_dir():
            raise ValueError(f"parent directory missing: {cur.relative_to(root).as_posix()}")


def _copy_file(root: Path, rel: Path, dst: Path) -> None:
    _ensure_no_symlink_chain(root, rel)
    src = root / rel
    if src.is_symlink() or not src.is_file():
        raise ValueError(f"input is symlink or special file: {rel.as_posix()}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=False)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_version(source: Path) -> str:
    props = source / "Directory.Build.props"
    try:
        root = ET.parse(props).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"failed to parse Directory.Build.props: {exc}") from exc
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag == "Version" and elem.text and elem.text.strip():
            return elem.text.strip()
    raise ValueError("Directory.Build.props has no nonempty <Version>")


def export_source(source: Path, destination: Path, *, mode: str,
                  revision: str | None) -> dict:
    if mode not in ("remote", "local"):
        raise ValueError(f"invalid sourceMode: {mode}")
    if mode == "remote":
        if revision is None:
            raise ValueError("remote mode requires a revision")
        _validate_revision(revision)
    elif revision is not None:
        _validate_revision(revision)

    if source.is_symlink() or not source.is_dir():
        raise ValueError("source root is missing or a symlink")

    fixed_files = ROOT_FILES + MAINTENANCE + DOCS + [TESTKEY] + TOOLS
    for rel_str in fixed_files + REQUIRED_CSPROJ:
        rel = Path(rel_str)
        _ensure_no_symlink_chain(source, rel)
        p = source / rel
        if p.is_symlink():
            raise ValueError(f"required input is a symlink: {rel_str}")
        if not p.is_file():
            raise ValueError(f"required input absent: {rel_str}")

    for d in RECURSIVE_DIRS:
        rel_d = Path(d)
        _ensure_no_symlink_chain(source, rel_d)
        target_d = source / rel_d
        if target_d.is_symlink():
            raise ValueError(f"required directory is a symlink: {d}")
        if not target_d.is_dir():
            raise ValueError(f"required directory absent: {d}")

    key_sha = _sha256(source / TESTKEY)
    if key_sha != TESTKEY_SHA256:
        raise ValueError(f"testkey sha mismatch: {key_sha}")

    copied: list[Path] = []
    for rel_str in fixed_files:
        rel = Path(rel_str)
        dst = destination / rel
        _copy_file(source, rel, dst)
        copied.append(dst)

    for d in RECURSIVE_DIRS:
        base = source / d
        for root_str, dirs, filenames in os.walk(base, topdown=True, followlinks=False):
            root_path = Path(root_str)
            kept_dirs: list[str] = []
            for dname in sorted(dirs):
                if dname in EXCLUDED_NAMES:
                    continue
                cand_dir = root_path / dname
                if cand_dir.is_symlink():
                    raise ValueError(
                        f"symlink directory rejected: {cand_dir.relative_to(source).as_posix()}"
                    )
                kept_dirs.append(dname)
            dirs[:] = kept_dirs

            for fname in sorted(filenames):
                if fname in EXCLUDED_NAMES:
                    continue
                p = root_path / fname
                rel = p.relative_to(source)
                if p.is_symlink() or not p.is_file():
                    raise ValueError(f"input is symlink or special file: {rel.as_posix()}")
                dst = destination / rel
                _copy_file(source, rel, dst)
                copied.append(dst)

    files = []
    for p in sorted(copied, key=lambda q: q.relative_to(destination).as_posix()):
        rel_posix = p.relative_to(destination).as_posix()
        files.append({"path": rel_posix, "bytes": p.stat().st_size,
                      "sha256": _sha256(p)})

    return {
        "schemaVersion": 1,
        "repository": REPO_URL,
        "revision": revision,
        "sourceMode": mode,
        "version": _parse_version(source),
        "files": files,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--local-source", type=Path, default=None)
    args = ap.parse_args()

    output = args.output.absolute()
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir():
        _fail(f"output parent does not exist or is a symlink: {parent.name}")
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        _fail("output exists and is not a real directory; refusing to touch")
    if output.is_dir() and any(output.iterdir()):
        _fail("output directory not empty; caller must remove it")

    try:
        with tempfile.TemporaryDirectory(prefix=".boottool-src-", dir=parent) as td:
            stage = Path(td).absolute()
            if args.local_source is not None:
                source = args.local_source.absolute()
                if source.is_symlink() or not source.is_dir():
                    _fail("--local-source directory does not exist or is a symlink")
                mode, revision = "local", None
                try:
                    rev = _git(["rev-parse", "HEAD"], source)
                except ValueError:
                    revision = None
                else:
                    revision = _validate_revision(rev)
            else:
                source, revision = resolve_remote(stage)
                mode = "remote"

            dest = stage / "source"
            dest.mkdir()
            report = export_source(source, dest, mode=mode, revision=revision)
            (dest / "boot-tool-source.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")

            if output.is_dir():
                output.rmdir()  # empty only
            os.replace(dest, output)
    except ValueError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"filesystem error: {exc}")

    print(json.dumps({"sourceMode": report["sourceMode"],
                      "revision": report["revision"],
                      "version": report["version"],
                      "fileCount": len(report["files"])}))


if __name__ == "__main__":
    main()
