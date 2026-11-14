"""Read-only retrieval of signed Freedom update channel metadata."""

from http.client import HTTPException
from math import isfinite
from numbers import Real
from socket import SHUT_RDWR
from threading import Event, Timer
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


class _DeadlineSocketShutdown:
    """Shut a live response socket down at one absolute deadline.

    A single ``HTTPResponse.read1`` call can consume several chunks of
    transfer framing internally (for example while decoding chunk sizes),
    so a socket timeout applied to each internal ``recv`` does not bound
    the call as a whole: every internal read can make progress within the
    budget while the call overruns it. One daemon timer is armed per live
    response with the exact socket CPython exposes when body reading
    starts; at the deadline it calls ``shutdown(SHUT_RDWR)``, which makes
    an in-flight read return EOF or raise instead of running on.

    ``shutdown`` is used instead of ``close`` on purpose. Closing from the
    timer would race with ``http.client`` closing the same connection and
    could release a descriptor the process has already recycled. A socket
    object that ``http.client`` has closed invalidates its own descriptor,
    so shutting it down then raises ``EBADF``; that is ignored because the
    response is already finished. The timer is cancelled and joined on
    every path, so no thread outlives the response.
    """

    def __init__(self, sock, remaining):
        self._sock = sock
        self._fired = Event()
        self._timer = None
        if sock is not None:
            timer = Timer(max(remaining, 0.0), self._expire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    @property
    def fired(self):
        """Whether the deadline passed and the socket was shut down."""
        return self._fired.is_set()

    def _expire(self):
        self._fired.set()
        try:
            self._sock.shutdown(SHUT_RDWR)
        except OSError:
            pass

    def stop(self):
        """Cancel and join the timer; call once on every exit path."""
        timer = self._timer
        if timer is None:
            return
        timer.cancel()
        timer.join()


def _fetch_bytes(url: str, limit: int, timeout) -> bytes:
    """Fetch at most ``limit`` bytes of one response body under a deadline.

    ``timeout`` is passed to ``urllib``, so DNS resolution, connection
    setup and header ingestion are bounded per socket operation by it; they
    are not covered by the single absolute body deadline below.

    For the body, ``read1`` returns whatever has arrived so the deadline is
    re-checked between chunks instead of after an unbounded
    ``read(limit + 1)``; a slow trickle of bytes therefore cannot hold the
    fetch past ``timeout``. The check also runs after the EOF read, so a
    body that only finishes after the deadline is rejected rather than
    accepted as complete. Each read is additionally bounded by the
    remaining budget through the connected socket.

    A real ``HTTPResponse`` read may still consume several framing chunks
    inside one call, which per-read socket timeouts cannot interrupt, so
    one daemon timer is armed with the socket captured when body reading
    starts. At the absolute deadline it shuts that socket down; the blocked
    read then ends and the fetch fails closed as a timeout even if the read
    was making progress. The socket is looked up again before every read
    because a read that still returns body bytes may already have closed
    ``response.fp`` and the descriptor; reusing the object captured earlier
    would make the next ``settimeout`` raise EBADF on a response that is
    otherwise complete. When the transport exposes no socket (an in-memory
    or mocked response), only the between-read checks apply.
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

    def timed_out():
        return CatalogError(f"fetch of {url} timed out after {timeout} seconds")

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
            guard = _DeadlineSocketShutdown(
                _body_socket(response), deadline - monotonic()
            )
            try:
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise timed_out()
                    sock = _body_socket(response)
                    if sock is not None:
                        sock.settimeout(remaining)
                    try:
                        chunk = read1(min(_READ_SIZE, limit + 1 - total))
                    except (OSError, HTTPException):
                        if guard.fired:
                            raise timed_out() from None
                        raise
                    if not chunk:
                        if guard.fired or deadline - monotonic() <= 0:
                            raise timed_out()
                        break
                    body.extend(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise CatalogError(
                            f"document fetched from {url} exceeds {limit} bytes"
                        )
                    if guard.fired or deadline - monotonic() <= 0:
                        raise timed_out()
            finally:
                guard.stop()
            if guard.fired:
                raise timed_out()
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
