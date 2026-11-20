"""Stage and verify signed ARMADA_BOOT generations."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import struct
import uuid
from pathlib import Path
from typing import Mapping, Optional

from . import _io
from . import signature
from .boot_disk_binding import encode_binding


MAGIC_DOC = b"ARMBTV1\0"       # 8 bytes
MAGIC_BINDING = b"ARMDEP1\0"   # 8 bytes

MANIFEST_LEN = 160
BINDING_LEN = 152

CAP_KERNEL = 128 * 2**20
CAP_INITRAMFS = 128 * 2**20
CAP_DTB = 256 * 2**10

MIN_SIG_LEN = 8
MAX_SIG_LEN = 72

VERSION = 1

# Offsets within the 160-byte manifest
_OFF_MAGIC = 0
_OFF_HEAD = 8      # <II version, board
_OFF_SIZES = 16    # <3Q kernel, initramfs, dtb
_OFF_SHA = 40      # <32s32s32s
_OFF_ZERO = 136    # <24s  -> total 160

_HEAD = struct.Struct("<II")
_SIZES = struct.Struct("<3Q")
_SHA = struct.Struct("<32s32s32s")
_ZERO = struct.Struct("<24s")
_BINDING = struct.Struct("<8sB7s64s64sII")   # 152 bytes

BOARD_TB322FC = 1
BOARD_TB323FU = 2
_MODEL_BOARD: Mapping[str, int] = {
    "TB322FC": BOARD_TB322FC,
    "TB323FU": BOARD_TB323FU,
}

_HEX64_RE = re.compile(rb"[0-9a-f]{64}")
_GEN_RE = re.compile(r"gen-[0-9a-f]{32}")

_MAX_U32_SIGNED = 2**31 - 1


def _check_file(
    path: Path,
    size: int,
    digest: str,
    output: Optional[Path] = None,
) -> None:
    """Open path as a regular file (no follow), verify exact size and SHA-256.

    Streams up to `size + 1` bytes in chunks of at most 1 MiB to detect
    over-size inputs. If `output` is provided, copies verified bytes to it
    with write/fsync. Raises on any mismatch.
    """
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    out_fd = -1
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file: %s" % (path,))
        if st.st_size != size:
            raise ValueError("size mismatch: %s" % (path,))

        if output is not None:
            out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            out_flags |= getattr(os, "O_NOFOLLOW", 0)
            out_flags |= getattr(os, "O_CLOEXEC", 0)
            out_fd = os.open(output, out_flags, 0o644)

        h = hashlib.sha256()
        remaining = size + 1
        total = 0
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise ValueError("file larger than declared: %s" % (path,))
            h.update(chunk)
            if out_fd >= 0:
                _io.write_all(out_fd, chunk)
            remaining -= len(chunk)

        if total != size:
            raise ValueError("short read: %s" % (path,))
        if h.hexdigest() != digest:
            raise ValueError("digest mismatch: %s" % (path,))

        if out_fd >= 0:
            os.fsync(out_fd)
    finally:
        if out_fd >= 0:
            os.close(out_fd)
        os.close(fd)


def _parse_manifest(raw: bytes) -> tuple:
    """Parse and structurally validate the 160-byte manifest. Returns fields."""
    if len(raw) != MANIFEST_LEN:
        raise ValueError("manifest length != 160")
    if raw[_OFF_MAGIC:_OFF_MAGIC + 8] != MAGIC_DOC:
        raise ValueError("manifest magic")

    version, board = _HEAD.unpack_from(raw, _OFF_HEAD)
    if version != VERSION:
        raise ValueError("manifest version != 1")

    k_size, i_size, d_size = _SIZES.unpack_from(raw, _OFF_SIZES)
    if not 0 < k_size <= CAP_KERNEL:
        raise ValueError("kernel over cap")
    if not 0 < i_size <= CAP_INITRAMFS:
        raise ValueError("initramfs over cap")
    if not 0 < d_size <= CAP_DTB:
        raise ValueError("dtb over cap")

    k_sha, i_sha, d_sha = _SHA.unpack_from(raw, _OFF_SHA)

    zero, = _ZERO.unpack_from(raw, _OFF_ZERO)
    if zero != b"\0" * 24:
        raise ValueError("manifest zero pad")

    return (
        board, k_size, i_size, d_size,
        k_sha.hex(), i_sha.hex(), d_sha.hex(),
    )


def _verify_signature(der: bytes, raw: bytes, trusted_key) -> None:
    if not (MIN_SIG_LEN <= len(der) <= MAX_SIG_LEN):
        raise ValueError("signature length out of range")
    signature.verify_document(
        raw, base64.b64encode(der), trusted_key=trusted_key
    )


def verify_generation(
    path: Path,
    model: str,
    *,
    trusted_key=signature.DEFAULT_TRUSTED_KEY,
) -> None:
    """Verify a staged generation directory in place. Return None on success.

    Order: model ID, directory reality, manifest parse, signature, then
    content verification.
    """
    if model not in _MODEL_BOARD:
        raise ValueError("unknown model")
    board_expected = _MODEL_BOARD[model]

    gen = Path(path)
    if gen.is_symlink() or not gen.is_dir():
        raise ValueError("generation not a real directory")

    model_dir = gen / model
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise ValueError("model dir invalid")

    manifest_path = model_dir / "manifest.bin"
    sig_path = model_dir / "manifest.sig"
    kernel_path = gen / "kernel.img"
    initramfs_path = gen / "initramfs.img"
    dtb_path = model_dir / "board.dtb"

    # Reject symlinks on all inputs up front.
    for p in (manifest_path, sig_path, kernel_path, initramfs_path, dtb_path):
        if p.is_symlink():
            raise ValueError("symlink in generation: %s" % (p.name,))

    raw = _io.read_bounded(manifest_path, MANIFEST_LEN)
    if len(raw) != MANIFEST_LEN:
        raise ValueError("manifest length != 160")
    (board, k_size, i_size, d_size,
     k_sha, i_sha, d_sha) = _parse_manifest(raw)

    if board != board_expected:
        raise ValueError("board mismatch")

    der = _io.read_bounded(sig_path, MAX_SIG_LEN)
    _verify_signature(der, raw, trusted_key)

    _check_file(kernel_path, k_size, k_sha)
    _check_file(initramfs_path, i_size, i_sha)
    _check_file(dtb_path, d_size, d_sha)


def decode_binding(path: Path) -> str:
    """Read and decode a 152-byte binding; return default/<commit>.<serial>."""
    return _decode_binding(_io.read_bounded(path, BINDING_LEN))


def _decode_binding(raw: bytes) -> str:
    if len(raw) != BINDING_LEN:
        raise ValueError("binding length != 152")

    magic, bootversion, zero, boot_c, dep_c, serial, index = _BINDING.unpack(raw)
    if magic != MAGIC_BINDING or bootversion not in (0, 1) or any(zero):
        raise ValueError("binding magic")
    if not _HEX64_RE.fullmatch(boot_c):
        raise ValueError("binding boot checksum")
    if not _HEX64_RE.fullmatch(dep_c):
        raise ValueError("binding deployment commit")
    if index > _MAX_U32_SIGNED or serial > _MAX_U32_SIGNED:
        raise ValueError("binding serial/index out of range")
    if boot_c == b"0" * 64 or dep_c == b"0" * 64:
        raise ValueError("binding all-zero hash")

    return "default/%s.%d" % (dep_c.decode("ascii"), serial)


def stage_generation(
    boot: Path,
    model: str,
    *,
    kernel: Path,
    initramfs: Path,
    binding: bytes,
    capsule: Mapping[str, bytes],
    trusted_key=signature.DEFAULT_TRUSTED_KEY,
) -> str:
    """Stage a new generation under boot/armada.

    Steps:
      1. Model ID first.
      2. boot/armada exists as a real directory.
      3. Parse+verify manifest signature over parsed record.
      4. Create gen dir exclusively, copy files with _check_file using the
         verified record's sizes and digests; verify again on staged outputs.

    On failure, removes only the directory created by this call.
    """
    if model not in _MODEL_BOARD:
        raise ValueError("unknown model")

    boot = Path(boot)
    armada = boot / "armada"
    if armada.is_symlink() or not armada.is_dir():
        raise ValueError("armada not a real directory")

    # Parse and verify inputs before touching the filesystem.
    _decode_binding(binding)
    if set(capsule) != {"manifest.bin", "manifest.sig", "board.dtb"}:
        raise ValueError("invalid boot capsule fields")
    mf_raw = capsule["manifest.bin"]
    if len(mf_raw) != MANIFEST_LEN:
        raise ValueError("manifest length != 160")
    (board, k_size, i_size, d_size,
     k_sha, i_sha, d_sha) = _parse_manifest(mf_raw)

    if board != _MODEL_BOARD[model]:
        raise ValueError("board mismatch")

    der = capsule["manifest.sig"]
    _verify_signature(der, mf_raw, trusted_key)

    dtb = capsule["board.dtb"]
    if len(dtb) != d_size or hashlib.sha256(dtb).hexdigest() != d_sha:
        raise ValueError("boot capsule DTB does not match its authenticated manifest")

    for existing_name in sorted(
        p.name for p in armada.iterdir()
        if _GEN_RE.fullmatch(p.name) and not p.is_symlink() and p.is_dir()
    ):
        existing = armada / existing_name
        try:
            existing_model = existing / model
            if existing_model.is_symlink() or not existing_model.is_dir():
                continue
            existing_binding = _io.read_bounded(
                existing / "binding.bin", BINDING_LEN)
            if existing_binding != binding:
                continue
            existing_manifest = _io.read_bounded(
                existing_model / "manifest.bin", MANIFEST_LEN)
            if existing_manifest != mf_raw:
                continue
            verify_generation(existing, model, trusted_key=trusted_key)
        except (OSError, ValueError, signature.SignatureError):
            continue
        _check_file(Path(kernel), k_size, k_sha)
        _check_file(Path(initramfs), i_size, i_sha)
        return existing_name

    name = "gen-%s" % (uuid.uuid4().hex,)
    gen = armada / name
    os.mkdir(gen, 0o755)          # exclusive; fails if exists

    try:
        model_dir = gen / model
        os.mkdir(model_dir, 0o755)

        # Copy + verify each file from source, writing exclusive durable outputs.
        _check_file(Path(kernel), k_size, k_sha, gen / "kernel.img")
        _check_file(Path(initramfs), i_size, i_sha, gen / "initramfs.img")
        _write_exclusive(model_dir / "board.dtb", dtb, 0o644)

        # Manifest and signature are already verified; write bytes directly.
        _write_exclusive(model_dir / "manifest.bin", mf_raw, 0o644)
        _write_exclusive(model_dir / "manifest.sig", der, 0o644)
        _write_exclusive(gen / "binding.bin", binding, 0o644)

        # Durability order: contents -> dirs containing them -> armada.
        _io.fsync_dir(model_dir)
        _io.fsync_dir(gen)
        _io.fsync_dir(armada)
        _io.fsync_dir(boot)

        # Re-verify the staged generation with the same verifier.
        verify_generation(gen, model, trusted_key=trusted_key)
    except BaseException:
        shutil.rmtree(gen, ignore_errors=True)
        raise

    return name


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, mode)
    try:
        _io.write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
