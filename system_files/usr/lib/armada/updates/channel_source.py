"""Read-only retrieval of signed Freedom update channel metadata."""

from http.client import HTTPException
from math import isfinite
from numbers import Real
from time import monotonic
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
_READ_SIZE = 64 * 1024


class HTTPSOnlyRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect target before urllib processes it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, "redirect URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _body_socket(response):
    """Return the connected body socket CPython exposes right now, if any.

    ``http.client`` drops ``response.fp`` once the body is complete, so the
    answer changes over the life of the response and must be re-read rather
    than captured once.
    """
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    return getattr(raw, "_sock", None)


def _fetch_bytes(url: str, limit: int, timeout) -> bytes:
    """Fetch at most ``limit`` bytes, bounded by one monotonic deadline.

    ``read1`` returns whatever has arrived so the deadline is re-checked
    between chunks instead of after an unbounded ``read(limit + 1)``; a
    slow trickle of bytes therefore cannot hold the fetch past ``timeout``.
    The check also runs after the EOF read, so a body that only finishes
    after the deadline is rejected rather than accepted as complete. When
    CPython exposes the connected socket, each read is additionally bounded
    by the remaining budget so one read cannot block past the deadline. The
    socket is looked up again before every read because a read that still
    returns body bytes may already have closed ``response.fp`` and the
    descriptor; reusing the object captured earlier would make the next
    ``settimeout`` raise EBADF on a response that is otherwise complete.
    """
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
    deadline = monotonic() + timeout
    body = bytearray()
    total = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            _url(response.geturl(), "final response URL")
            status = response.getcode()
            if status != 200:
                raise CatalogError(f"fetch of {url} returned HTTP status {status}")
            read1 = getattr(response, "read1", None)
            if not callable(read1):
                raise CatalogError(
                    f"fetch of {url} response does not support bounded reads"
                )
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CatalogError(
                        f"fetch of {url} timed out after {timeout} seconds"
                    )
                sock = _body_socket(response)
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = read1(min(_READ_SIZE, limit + 1 - total))
                if not chunk:
                    if deadline - monotonic() <= 0:
                        raise CatalogError(
                            f"fetch of {url} timed out after {timeout} seconds"
                        )
                    break
                body.extend(chunk)
                total += len(chunk)
                if total > limit:
                    raise CatalogError(
                        f"document fetched from {url} exceeds {limit} bytes"
                    )
                if deadline - monotonic() <= 0:
                    raise CatalogError(
                        f"fetch of {url} timed out after {timeout} seconds"
                    )
    except CatalogError:
        raise
    except (TypeError, URLError, HTTPException, OSError) as exc:
        raise CatalogError(
            f"failed to fetch {url}: {type(exc).__name__}: {exc}"
        ) from exc
    return bytes(body)


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
