# build_files/armada_boot_install.py
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

_BINDING_MAGIC = b"ARMDEP1\0"
_BINDING_LEN = 152
_MODELS = ("TB322FC", "TB323FU", "TB710FU")  # = updates/models.py DISK_BOOT_MODELS


def _write_binding(path: Path, binding: bytes) -> None:
    if len(binding) != _BINDING_LEN:
        raise ValueError(f"binding must be {_BINDING_LEN} bytes, got {len(binding)}")
    if not binding.startswith(_BINDING_MAGIC):
        raise ValueError(f"binding must start with {_BINDING_MAGIC!r}")
    with open(path, "xb") as f:
        f.write(binding)


def _hardlink_verified(src: Path, dst: Path) -> None:
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"source not found: {src}")
    # Verify src and dst resolve to same inode after link (aliases to OSTree content)
    os.link(src, dst)
    if os.stat(src).st_ino != os.stat(dst).st_ino:
        raise RuntimeError(f"hardlink identity mismatch: {src} -> {dst}")


def _copy_dtb(src: Path, dst: Path) -> None:
    with open(src, "rb") as f:
        data = f.read()
    if not data:
        raise ValueError(f"dtb is empty: {src}")
    with open(dst, "xb") as f:
        f.write(data)


def stage_install_boot(
    boot: Path,
    kernel: Path,
    initrd: Path,
    binding: bytes,
    dtb_by_model: dict[str, Path],
    record_builder: Path,
    sign: Callable[[Path], bytes],
) -> None:
    armada = boot / "armada"
    boot_dst = armada / "seed"

    # armada may be a normal existing directory; boot target must not exist.
    if armada.exists() and not armada.is_dir():
        raise NotADirectoryError(f"not a directory: {armada}")
    armada.mkdir(exist_ok=True)
    if boot_dst.exists():
        raise FileExistsError(f"boot target already exists: {boot_dst}")
    active_link = armada / "boot"
    if active_link.is_symlink() or active_link.exists():
        raise FileExistsError(f"refusing to replace pre-existing active link: {active_link}")
    boot_dst.mkdir()

    # Fixed boot binding.
    _write_binding(boot_dst / "binding.bin", binding)

    # Kernel and initrd as hardlink aliases to OSTree content on same /boot.
    _hardlink_verified(kernel, boot_dst / "kernel.img")
    _hardlink_verified(initrd, boot_dst / "initramfs.img")

    for model, src_dtb in dtb_by_model.items():
        if model not in _MODELS:
            raise ValueError(f"unsupported disk boot model: {model}")
        model_dir = boot_dst / model
        model_dir.mkdir()
        dtb_path = model_dir / "board.dtb"
        _copy_dtb(src_dtb, dtb_path)

        manifest = model_dir / "manifest.bin"
        cmd = [
            sys.executable,
            str(record_builder),
            "--model", model,
            "--kernel", str(boot_dst / "kernel.img"),
            "--initramfs", str(boot_dst / "initramfs.img"),
            "--dtb", str(dtb_path),
            "--output", str(manifest),
        ]
        subprocess.run(cmd, check=True)

        sig = sign(manifest)
        if not isinstance(sig, (bytes, bytearray)):
            raise TypeError("sign() must return bytes")
        if not (8 <= len(sig) <= 72):
            raise ValueError(f"signature must be 8..72 DER bytes, got {len(sig)}")
        with open(model_dir / "manifest.sig", "xb") as f:
            f.write(sig)

    active_link.symlink_to("seed")
