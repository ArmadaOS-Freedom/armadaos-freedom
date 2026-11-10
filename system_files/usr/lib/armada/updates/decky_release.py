from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
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


def verify_payload(release: DeckyRelease, path: str | Path) -> None:
    if not isinstance(release, DeckyRelease):
        raise DeckyReleaseError("Invalid release metadata")
    if not isinstance(release.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", release.sha256):
        raise DeckyReleaseError("Invalid release sha256")
    _require_pos_int(release.size, "release size")

    hasher = hashlib.sha256()
    total = 0
    header = b""

    try:
        with Path(path).open("rb") as f:
            while chunk := f.read(65536):
                if len(header) < 64:
                    header += chunk[: 64 - len(header)]
                total += len(chunk)
                hasher.update(chunk)
    except OSError as exc:
        raise DeckyReleaseError(f"Payload read failed: {exc}") from exc

    if total != release.size:
        raise DeckyReleaseError(f"Size mismatch: expected {release.size}, got {total}")
    if hasher.hexdigest().lower() != release.sha256:
        raise DeckyReleaseError("SHA256 mismatch")
    if len(header) < 64 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1:
        raise DeckyReleaseError("Invalid ELF64 little-endian header")
    if struct.unpack_from("<H", header, 18)[0] != 183:
        raise DeckyReleaseError("ELF binary is not AArch64 (e_machine != 183)")
