"""Read-only retrieval of signed Freedom update channel metadata."""

from http.client import HTTPException
from math import isfinite
from numbers import Real
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import CatalogError, _url, load_channel, require_compatible
from .channel_release import bind_package
from .signature import (
    DEFAULT_TRUSTED_KEY,
    MAX_DOCUMENT_BYTES,
    MAX_SIGNATURE_BYTES,
)


_CHANNELS = frozenset(("stable", "beta", "testing"))


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect target before urllib processes it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, "redirect URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_bytes(url: str, limit: int, timeout) -> bytes:
    _url(url, "fetch URL")
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "freedom-updater/0.8",
        },
        method="GET",
    )
    opener = build_opener(HTTPSOnlyRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            _url(response.geturl(), "final response URL")
            status = response.getcode()
            if status != 200:
                raise CatalogError(f"fetch of {url} returned HTTP status {status}")
            document = response.read(limit + 1)
    except CatalogError:
        raise
    except (TypeError, URLError, HTTPException, OSError) as exc:
        raise CatalogError(
            f"failed to fetch {url}: {type(exc).__name__}: {exc}"
        ) from exc
    if len(document) > limit:
        raise CatalogError(f"document fetched from {url} exceeds {limit} bytes")
    return document


def _validate_timeout(timeout) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, Real):
        raise CatalogError("timeout must be a finite positive real number")
    try:
        value = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CatalogError("timeout must be a finite positive real number") from exc
    if not isfinite(value) or value <= 0:
        raise CatalogError("timeout must be a finite positive real number")
    return value


def fetch_update(
    channel_url: str,
    signature_url: str,
    *,
    expected_channel: str,
    model: str,
    updater_version: str,
    trusted_key=DEFAULT_TRUSTED_KEY,
    timeout=20,
) -> dict:
    """Fetch, authenticate, and bind a channel and its package descriptor."""
    if not isinstance(expected_channel, str) or expected_channel not in _CHANNELS:
        raise CatalogError("unknown expected channel")
    timeout = _validate_timeout(timeout)
    _url(channel_url, "channel_url")
    _url(signature_url, "signature_url")
    if channel_url == signature_url:
        raise CatalogError("channel and signature URLs must differ")

    raw_channel = _fetch_bytes(channel_url, MAX_DOCUMENT_BYTES, timeout)
    raw_channel_signature = _fetch_bytes(
        signature_url, MAX_SIGNATURE_BYTES, timeout
    )
    channel = load_channel(
        raw_channel,
        raw_channel_signature,
        expected_channel=expected_channel,
        trusted_key=trusted_key,
    )
    require_compatible(channel, model, updater_version)

    descriptor = channel["packageDescriptor"]
    raw_package = _fetch_bytes(descriptor["url"], descriptor["bytes"], timeout)
    raw_package_signature = _fetch_bytes(
        descriptor["signatureUrl"], MAX_SIGNATURE_BYTES, timeout
    )
    package = bind_package(
        channel, raw_package, raw_package_signature, trusted_key=trusted_key
    )
    return {"channel": channel, "package": package}
