from __future__ import annotations

import http.client
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.error
from urllib.parse import urlsplit
import urllib.request

from .decky_release import (
    ASSET_NAME,
    DeckyRelease,
    DeckyReleaseError,
    select_preview,
    verify_payload,
)

RELEASES_API_URL = "https://api.github.com/repos/SteamDeckHomebrew/decky-loader/releases?per_page=100"
MAX_METADATA_BYTES = 16 * 1024 * 1024
USER_AGENT = "armada-decky-updater/1.0"
CHUNK_SIZE = 65536


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or not parts.netloc:
            raise DeckyReleaseError("Disallowed redirect target")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_timeout(timeout: int | float) -> float:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise DeckyReleaseError("Invalid timeout")
    return float(timeout)


def _open_https(req: urllib.request.Request, timeout: float):
    parts = urlsplit(req.full_url)
    if parts.scheme != "https" or not parts.netloc:
        raise DeckyReleaseError("HTTPS URL required")
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    try:
        resp = opener.open(req, timeout=timeout)
    except DeckyReleaseError:
        raise
    except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DeckyReleaseError(f"Network request failed: {exc}") from exc

    status = getattr(resp, "status", None) or resp.getcode()
    if status != 200:
        resp.close()
        raise DeckyReleaseError(f"Unexpected HTTP status: {status}")
    return resp


def _validate_release(release: DeckyRelease) -> DeckyRelease:
    if not isinstance(release, DeckyRelease):
        raise DeckyReleaseError("Invalid release metadata")
    if not isinstance(release.sha256, str) or release.sha256 != release.sha256.lower():
        raise DeckyReleaseError("Invalid release sha256")
    return select_preview(
        [
            {
                "id": release.release_id,
                "tag_name": release.tag,
                "published_at": release.published_at,
                "draft": False,
                "prerelease": True,
                "assets": [
                    {
                        "name": ASSET_NAME,
                        "id": release.asset_id,
                        "size": release.size,
                        "browser_download_url": release.url,
                        "digest": f"sha256:{release.sha256}",
                    }
                ],
            }
        ]
    )


def fetch_preview(*, timeout: int | float = 20) -> DeckyRelease:
    valid_timeout = _validate_timeout(timeout)
    req = urllib.request.Request(
        RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with _open_https(req, valid_timeout) as resp:
            raw = resp.read(MAX_METADATA_BYTES + 1)
    except DeckyReleaseError:
        raise
    except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DeckyReleaseError(f"Metadata read failed: {exc}") from exc

    if len(raw) > MAX_METADATA_BYTES:
        raise DeckyReleaseError("Releases metadata exceeds 16 MiB limit")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeckyReleaseError(f"Invalid releases JSON: {exc}") from exc

    return select_preview(payload)


def download_payload(
    release: DeckyRelease, destination: Path, *, timeout: int | float = 30
) -> Path:
    validated = _validate_release(release)
    valid_timeout = _validate_timeout(timeout)
    if not isinstance(destination, Path):
        raise DeckyReleaseError("Destination must be a Path")

    parent = destination.parent
    if not parent.is_dir():
        raise DeckyReleaseError(f"Destination directory does not exist: {parent}")
    if destination.exists() and destination.is_dir():
        raise DeckyReleaseError(f"Destination is a directory: {destination}")

    req = urllib.request.Request(
        validated.url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )

    tmp_path: Path | None = None
    try:
        with _open_https(req, valid_timeout) as resp:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=parent
            )
            tmp_path = Path(tmp_name)
            total = 0
            with os.fdopen(fd, "wb") as tmp_file:
                while chunk := resp.read(CHUNK_SIZE):
                    total += len(chunk)
                    if total > validated.size:
                        raise DeckyReleaseError("Download exceeded declared asset size")
                    tmp_file.write(chunk)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

        verify_payload(validated, tmp_path)
        os.replace(tmp_path, destination)
        tmp_path = None
        return destination
    except DeckyReleaseError:
        raise
    except (http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise DeckyReleaseError(f"Payload download failed: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
