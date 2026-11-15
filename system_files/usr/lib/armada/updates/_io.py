"""Shared strict JSON and bounded IO primitives for the offline update stack.

These helpers carry no policy of their own: callers keep their ownership,
mode, symlink, size and protocol rules and translate failures into their own
domain error types.
"""

import hashlib
import json
import math
import os
import stat

_CHUNK_SIZE = 1024 * 1024


class ShortWriteError(OSError):
    """A single os.write call made no progress."""


def object_pairs(pairs):
    """Build a JSON object, rejecting duplicate keys at any depth."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: %s' % (key,))
        result[key] = value
    return result


def reject_constant(value):
    """Reject the non-standard NaN, Infinity and -Infinity constants."""
    raise ValueError('non-finite JSON constant: %s' % (value,))


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite JSON float: %s' % (value,))
    return number


def strict_loads(data):
    """Decode JSON bytes or text with strict object and number handling.

    Malformed JSON still raises json.JSONDecodeError; duplicate keys and
    non-finite numbers raise ValueError so callers can apply their own
    domain error type.
    """
    return json.loads(
        data,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
        parse_float=_finite_float,
    )


def fsync_dir(path, *, nofollow=False):
    """fsync a directory, always closing the descriptor this helper opened."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd, data):
    """Write every byte of data, retrying partial writes.

    The descriptor is neither opened, replaced, fsynced nor closed here; a
    zero or negative write raises ShortWriteError instead of looping forever.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ShortWriteError('write returned %d bytes' % (written,))
        view = view[written:]


def sha256_hex(data):
    """Lowercase SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream):
    """Hash from the current position in bounded chunks, leaving it open."""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def read_fd_bounded(fd, limit):
    """Read at most limit bytes from fd without closing, seeking or resetting it."""
    if type(limit) is not int or limit < 0:
        raise ValueError('limit must be a nonnegative integer')
    chunks = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b''.join(chunks)
    if len(data) > limit:
        raise ValueError('data exceeds %d bytes' % (limit,))
    return data


def read_bounded(path, limit):
    """Read at most limit bytes from a regular file, refusing symlinks and FIFOs."""
    fd = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('not a regular file: %s' % (path,))
        return read_fd_bounded(fd, limit)
    finally:
        os.close(fd)
