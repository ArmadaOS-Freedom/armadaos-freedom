# system_files/usr/lib/armada/updates/boot_disk_selector.py
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path
from typing import Union

from . import _io

__all__ = ["ensure_root_binding", "activate_generation"]

_DEPLOYMENT_RE = re.compile(r"default/([0-9a-f]{64})\.(0|[1-9][0-9]{0,9})")
_TARGET_RE = re.compile(r"(?:seed|gen-[0-9a-f]{32})")
_INT32_MAX = 2147483647


def _canon(p: Path) -> Path:
    return Path(os.path.realpath(os.fspath(p)))


def ensure_root_binding(sysroot: Union[str, os.PathLike, Path], deployment: str) -> str:
    if not isinstance(deployment, str):
        raise TypeError("deployment must be str")
    m = _DEPLOYMENT_RE.fullmatch(deployment)
    if m is None:
        raise ValueError("deployment not canonical default/<64hex>.<serial>")
    checksum = m.group(1)
    if int(checksum, 16) == 0:
        raise ValueError("deployment checksum must be nonzero")
    serial = int(m.group(2))
    if serial < 0 or serial > _INT32_MAX:
        raise ValueError("deployment serial out of int32 range")
    basename = f"{checksum}.{serial}"

    sysroot_p = Path(sysroot)
    if not sysroot_p.is_absolute():
        raise ValueError("sysroot must be absolute")
    resolved_root = _canon(sysroot_p)
    ostree_dir = resolved_root / "ostree"
    deploy_dir = ostree_dir / "deploy" / "default" / "deploy"
    expected_deploy = deploy_dir / basename
    if _canon(expected_deploy) != expected_deploy:
        raise ValueError("deployment path is non-canonical")
    try:
        st = expected_deploy.lstat()
    except OSError as exc:
        raise ValueError(f"deployment directory missing: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError("deployment must be a real directory")

    armada_dir = ostree_dir / "armada"
    try:
        armada_lst = armada_dir.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(os.fspath(armada_dir), 0o755)
        except FileExistsError:
            pass
        _io.fsync_dir(ostree_dir)
        armada_lst = armada_dir.lstat()
    except OSError as exc:
        raise ValueError(f"cannot stat armada directory: {exc}") from exc
    if stat.S_ISLNK(armada_lst.st_mode) or not stat.S_ISDIR(armada_lst.st_mode):
        raise ValueError("/ostree/armada must be a real directory")
    if _canon(armada_dir) != armada_dir:
        raise ValueError("/ostree/armada path is non-canonical")

    link_path = armada_dir / basename
    expected_target_rel = "../deploy/default/deploy/" + basename
    try:
        link_lst = link_path.lstat()
    except FileNotFoundError:
        link_lst = None
    except OSError as exc:
        raise ValueError(f"cannot stat binding link: {exc}") from exc

    if link_lst is None:
        try:
            os.symlink(expected_target_rel, os.fspath(link_path))
        except FileExistsError:
            link_lst = link_path.lstat()
    if link_lst is not None:
        if not stat.S_ISLNK(link_lst.st_mode):
            raise ValueError("binding path exists and is not a symlink")
        if os.readlink(os.fspath(link_path)) != expected_target_rel:
            raise ValueError("binding symlink has unexpected target")

    _io.fsync_dir(armada_dir)
    return f"/ostree/armada/{basename}"


def activate_generation(boot: Union[str, os.PathLike, Path], expected: str, target: str) -> None:
    for name in (expected, target):
        if not isinstance(name, str) or _TARGET_RE.fullmatch(name) is None:
            raise ValueError("generation name not canonical")
    if expected == target:
        raise ValueError("expected and target must differ")

    boot_p = Path(boot)
    if not boot_p.is_absolute():
        raise ValueError("boot must be absolute")
    resolved_boot = _canon(boot_p)
    armada_dir = resolved_boot / "armada"
    try:
        armada_lst = armada_dir.lstat()
    except OSError as exc:
        raise ValueError(f"boot/armada missing: {exc}") from exc
    if stat.S_ISLNK(armada_lst.st_mode) or not stat.S_ISDIR(armada_lst.st_mode):
        raise ValueError("boot/armada must be a real directory")
    if _canon(armada_dir) != armada_dir:
        raise ValueError("boot/armada path is non-canonical")

    target_dir = armada_dir / target
    try:
        target_lst = target_dir.lstat()
    except OSError as exc:
        raise ValueError(f"target generation missing: {exc}") from exc
    if stat.S_ISLNK(target_lst.st_mode) or not stat.S_ISDIR(target_lst.st_mode):
        raise ValueError("target generation must be a real directory")
    if _canon(target_dir) != target_dir:
        raise ValueError("target generation path is non-canonical")

    boot_link = armada_dir / "boot"
    try:
        boot_lst = boot_link.lstat()
    except OSError as exc:
        raise ValueError(f"boot/armada/boot missing: {exc}") from exc
    if not stat.S_ISLNK(boot_lst.st_mode):
        raise ValueError("boot/armada/boot must be a symlink")
    if os.readlink(os.fspath(boot_link)) != expected:
        raise ValueError("boot/armada/boot does not point at expected generation")

    tmp_link = armada_dir / f".act.{secrets.token_hex(12)}"
    os.symlink(target, os.fspath(tmp_link))
    try:
        os.replace(os.fspath(tmp_link), os.fspath(boot_link))
        _io.fsync_dir(armada_dir)
    finally:
        tmp_link.unlink(missing_ok=True)
