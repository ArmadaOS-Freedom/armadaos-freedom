from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
from typing import Any
from urllib.parse import unquote, urlsplit

ASSET_NAME = "PluginLoader-arm64"
_TAG_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")


class DeckyReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class DeckyRelease:
    release_id: int
    tag: str
    published_at: str
    asset_id: int
    url: str
    size: int
    sha256: str


def _require_pos_int(val: Any, name: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise DeckyReleaseError(f"Invalid {name}")
    return val


def _require_bool(val: Any, name: str) -> bool:
    if not isinstance(val, bool):
        raise DeckyReleaseError(f"Invalid {name}")
    return val


def _parse_iso8601(val: Any) -> datetime:
    if not isinstance(val, str) or not val:
        raise DeckyReleaseError("Invalid published_at")
    try:
        dt = datetime.fromisoformat(val[:-1] + "+00:00" if val.endswith("Z") else val)
    except ValueError as exc:
        raise DeckyReleaseError("Malformed published_at") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise DeckyReleaseError("published_at must be timezone-aware")
    return dt


def _validate_url(url: Any, tag: str) -> str:
    if (
        not isinstance(url, str)
        or not url
        or "?" in url
        or "#" in url
        or "\\" in url
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)
    ):
        raise DeckyReleaseError("Invalid download URL")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise DeckyReleaseError("Malformed download URL") from exc
    if (
        parts.scheme != "https"
        or parts.netloc != "github.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.query
        or parts.fragment
    ):
        raise DeckyReleaseError("Disallowed download URL authority or components")
    expected_path = f"/SteamDeckHomebrew/decky-loader/releases/download/{tag}/{ASSET_NAME}"
    if unquote(parts.path) != expected_path:
        raise DeckyReleaseError("Download URL path mismatch")
    return url


def select_preview(releases: Any) -> DeckyRelease:
    if not isinstance(releases, list):
        raise DeckyReleaseError("Releases payload must be a list")

    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for item in releases:
        if not isinstance(item, dict):
            raise DeckyReleaseError("Release entry must be an object")
        draft = _require_bool(item.get("draft"), "draft")
        prerelease = _require_bool(item.get("prerelease"), "prerelease")
        if draft or not prerelease:
            continue
        rel_id = _require_pos_int(item.get("id"), "release id")
        dt = _parse_iso8601(item.get("published_at"))
        candidates.append((dt, rel_id, item))

    if not candidates:
        raise DeckyReleaseError("No preview releases found")

    _, rel_id, latest = max(candidates, key=lambda c: (c[0], c[1]))

    tag = latest.get("tag_name")
    if not isinstance(tag, str) or tag in (".", "..") or not _TAG_RE.fullmatch(tag):
        raise DeckyReleaseError("Invalid tag_name")

    assets = latest.get("assets")
    if not isinstance(assets, list):
        raise DeckyReleaseError("Invalid assets list")

    matched: list[dict[str, Any]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise DeckyReleaseError("Asset entry must be an object")
        if asset.get("name") == ASSET_NAME:
            matched.append(asset)

    if len(matched) != 1:
        raise DeckyReleaseError(f"Expected exactly 1 {ASSET_NAME} asset, found {len(matched)}")

    asset = matched[0]
    asset_id = _require_pos_int(asset.get("id"), "asset id")
    size = _require_pos_int(asset.get("size"), "asset size")
    url = _validate_url(asset.get("browser_download_url"), tag)

    digest = asset.get("digest")
    if not isinstance(digest, str):
        raise DeckyReleaseError("Missing or invalid asset digest")
    m = _DIGEST_RE.fullmatch(digest)
    if not m:
        raise DeckyReleaseError("Invalid asset sha256 digest")

    return DeckyRelease(
        release_id=rel_id,
        tag=tag,
        published_at=latest["published_at"],
        asset_id=asset_id,
        url=url,
        size=size,
        sha256=m.group(1).lower(),
    )


def _open_payload(path: str | Path) -> int:
    """Open the payload once, without following a link or blocking on a node.

    Everything after this point inspects the returned descriptor with
    ``fstat``, so a path swapped between validation and reading cannot
    redirect the verification, and a FIFO or device node cannot hang it.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        return os.open(os.fspath(path), flags)
    except OSError as exc:
        raise DeckyReleaseError(f"Payload read failed: {exc}") from exc


def verify_payload(release: DeckyRelease, path: str | Path) -> None:
    if not isinstance(release, DeckyRelease):
        raise DeckyReleaseError("Invalid release metadata")
    if not isinstance(release.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
        raise DeckyReleaseError("Invalid release sha256")
    _require_pos_int(release.size, "release size")

    expected_size = release.size
    hasher = hashlib.sha256()
    total = 0
    header = b""

    fd = _open_payload(path)
    try:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise DeckyReleaseError("Payload is not a regular file")
            # The declared size is compared to the descriptor before a single
            # byte is read, so the bounded loop can never be driven past it.
            if st.st_size != expected_size:
                raise DeckyReleaseError(
                    f"Size mismatch: expected {expected_size}, got {st.st_size}"
                )
            while total < expected_size:
                chunk = os.read(fd, min(expected_size - total, 1 << 20))
                if not chunk:
                    break
                if len(header) < 64:
                    header = (header + chunk)[:64]
                total += len(chunk)
                hasher.update(chunk)
            if total != expected_size:
                raise DeckyReleaseError(
                    f"Size mismatch: expected {expected_size}, got {total}"
                )
            if os.read(fd, 1):
                raise DeckyReleaseError(
                    f"Size mismatch: expected {expected_size}, got more than declared"
                )
            end_st = os.fstat(fd)
        except OSError as exc:
            raise DeckyReleaseError(f"Payload read failed: {exc}") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    if (end_st.st_size != st.st_size
            or (end_st.st_dev, end_st.st_ino) != (st.st_dev, st.st_ino)):
        raise DeckyReleaseError("Payload changed while reading")
    if hasher.hexdigest().lower() != release.sha256:
        raise DeckyReleaseError("SHA256 mismatch")
    if len(header) < 64 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise DeckyReleaseError("Invalid ELF64 little-endian header")
    if struct.unpack_from("<H", header, 18)[0] != 183:
        raise DeckyReleaseError("ELF binary is not AArch64 (e_machine != 183)")
