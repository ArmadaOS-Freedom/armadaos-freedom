"""Read-only verification helpers for universal split-ZIP update packages.

Functions here perform media/schema verification only.  Signature checking and
trust provenance are handled by package_contract.load_package; every function
re-validates the descriptor schema before use and never executes package code,
extracts archives, or writes new files.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import stat
import zipfile
import zlib
from pathlib import Path

from .package_contract import PackageError, validate_package

_CHUNK = 1024 * 1024  # 1 MiB streaming bound
_MAX_ENTRY_BYTES = 1024 ** 4  # 1 TiB sanity bound


def _sha256_stream(stream, limit: int | None = None) -> tuple[str, int]:
    """Hash *stream* in <=1MiB chunks.  Returns (hexdigest, total bytes)."""
    h = hashlib.sha256()
    total = 0
    while True:
        n = _CHUNK
        if limit is not None and total + n > limit:
            n = limit - total
        chunk = stream.read(n) if n > 0 else b""
        if not chunk:
            break
        total += len(chunk)
        h.update(chunk)
    return h.hexdigest(), total


def _open_regular(path: Path):
    """Open *path* read-only, O_NOFOLLOW, regular files only."""
    flags = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise PackageError(f"cannot open {path!s}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PackageError(f"not a regular file: {path}")
        f = os.fdopen(fd, "rb", closefd=True)
    except Exception:
        os.close(fd)
        raise
    return f, st


def _check_unchanged(f, st_before, label: str) -> None:
    st_after = os.fstat(f.fileno())
    if st_after.st_size != st_before.st_size or st_after.st_mtime_ns != st_before.st_mtime_ns:
        raise PackageError(f"{label} changed during read")


def _revalidate(descriptor: dict) -> dict:
    if not isinstance(descriptor, dict):
        raise PackageError("descriptor must be a dict")
    return validate_package(descriptor)


def verify_parts(directory: Path, descriptor: dict) -> dict:
    """Verify split parts in *directory* against an authenticated descriptor.

    Each part is opened read-only (O_NOFOLLOW, regular files only), size-checked,
    and hashed; the concatenated stream is additionally hashed whole so a
    reordered-but-individually-valid split always fails.  No merge or writes.
    """
    try:
        desc = _revalidate(descriptor)
        archive = desc["archive"]
        parts = desc.get("parts") or []
        if not parts:
            raise PackageError("descriptor has no parts; nothing to verify")
        directory = Path(directory).resolve(strict=True)

        whole = hashlib.sha256()
        total = 0
        count = 0
        for part in parts:
            path = directory / part["fileName"]
            f, st = _open_regular(path)
            try:
                if st.st_size != part["bytes"]:
                    raise PackageError(
                        f"part {part['fileName']!r} size {st.st_size} != {part['bytes']}")
                part_hash = hashlib.sha256()
                n = 0
                limit = part["bytes"] + 1
                while n < limit:
                    chunk = f.read(min(_CHUNK, limit - n))
                    if not chunk:
                        break
                    n += len(chunk)
                    total += len(chunk)
                    part_hash.update(chunk)
                    whole.update(chunk)
                if part_hash.hexdigest() != part["sha256"] or n != part["bytes"]:
                    raise PackageError(f"part {part['fileName']!r} hash mismatch")
                _check_unchanged(f, st, f"part {part['fileName']!r}")
            finally:
                f.close()
            count += 1

        if total != archive["bytes"] or whole.hexdigest() != archive["sha256"]:
            raise PackageError("concatenated parts do not match archive hash/bytes")
        return {"bytes": total, "parts": count}
    except PackageError:
        raise
    except (OSError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
        raise PackageError(f"part verification failed: {exc}") from exc


def _verify_zip_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo, entry: dict,
                      verify_expanded: bool) -> None:
    if info.is_dir():
        raise PackageError(f"directory entry in ZIP: {info.filename!r}")
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise PackageError(f"symlink entry in ZIP: {info.filename!r}")
    if file_type not in (0, stat.S_IFREG):
        raise PackageError(f"non-regular entry in ZIP: {info.filename!r}")
    if info.compress_type != zipfile.ZIP_STORED:
        raise PackageError(f"entry {info.filename!r} is not ZIP_STORED")
    flag_bits = getattr(info, "flag_bits", 0) & 0x1
    if flag_bits:
        raise PackageError(f"entry {info.filename!r} is encrypted")
    if info.file_size != entry["bytes"] or info.compress_size != entry["bytes"]:
        raise PackageError(f"entry {info.filename!r} size mismatch")
    if entry["bytes"] > _MAX_ENTRY_BYTES:
        raise PackageError(f"entry {info.filename!r} exceeds max bytes")

    with zf.open(info, "r") as raw:
        digest, n = _sha256_stream(raw, limit=entry["bytes"] + 1)
    if digest != entry["sha256"] or n != entry["bytes"]:
        raise PackageError(f"entry {info.filename!r} hash mismatch")

    if verify_expanded and entry.get("compression") == "gzip":
        with zf.open(info, "r") as raw2:
            try:
                with gzip.GzipFile(fileobj=raw2, mode="rb") as gz:
                    edigest, en = _sha256_stream(gz, limit=entry["expandedBytes"] + 1)
            except (gzip.BadGzipFile, zlib.error, EOFError, OSError) as exc:
                raise PackageError(f"entry {info.filename!r} gzip error: {exc}") from exc
        if en != entry["expandedBytes"] or edigest != entry["expandedSha256"]:
            raise PackageError(f"entry {info.filename!r} expanded hash/size mismatch")


def verify_archive(path: Path, descriptor: dict, *, verify_expanded: bool = True) -> dict:
    """Verify a ZIP archive file against an authenticated descriptor."""
    try:
        desc = _revalidate(descriptor)
        archive = desc["archive"]
        entries = {f["path"]: f for f in desc["files"]}

        f, st = _open_regular(Path(path))
        try:
            if st.st_size != archive["bytes"]:
                raise PackageError(
                    f"archive size {st.st_size} != declared {archive['bytes']}")
            digest, n = _sha256_stream(f, limit=archive["bytes"] + 1)
            if digest != archive["sha256"] or n != archive["bytes"]:
                raise PackageError("archive hash mismatch")
            _check_unchanged(f, st, "archive")

            f.seek(0)
            try:
                with zipfile.ZipFile(f, "r") as zf:
                    infos = zf.infolist()
                    names = [i.filename for i in infos]
                    if len(names) != len(set(names)):
                        raise PackageError("duplicate ZIP entries present")
                    info_map = {i.filename: i for i in infos}
                    missing = set(entries) - set(info_map)
                    extra = set(info_map) - set(entries)
                    if missing:
                        raise PackageError(f"missing ZIP entries: {sorted(missing)!r}")
                    if extra:
                        raise PackageError(f"unexpected ZIP entries: {sorted(extra)!r}")
                    for name, entry in entries.items():
                        _verify_zip_entry(zf, info_map[name], entry, verify_expanded)
            except zipfile.BadZipFile as exc:
                raise PackageError(f"invalid ZIP: {exc}") from exc
            _check_unchanged(f, st, "archive")
        finally:
            f.close()

        return {
            "archiveBytes": archive["bytes"],
            "files": len(entries),
            "expandedVerified": bool(verify_expanded),
        }
    except PackageError:
        raise
    except (OSError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
        raise PackageError(f"archive verification failed: {exc}") from exc
