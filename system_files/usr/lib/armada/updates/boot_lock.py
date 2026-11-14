"""Decode the firmware boot-lock witness without fallback inference."""

import struct

from .boot_context import BootContextError, _read_regular


LOCK_PATH = "/sys/firmware/devicetree/base/chosen/armada,boot-lock"


class BootLockError(ValueError):
    """The boot-lock witness is present but unreadable or malformed."""


def decode_boot_lock(raw):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 16:
        raise BootLockError("boot-lock witness must be exactly 16 bytes")

    version, source, state, flags = struct.unpack(">4I", raw)
    if version != 1 or source != 1 or flags != 0 or state not in (1, 2):
        raise BootLockError("invalid boot-lock witness cells")

    return "locked" if state == 1 else "unlocked"


def read_boot_lock(path=LOCK_PATH):
    try:
        raw = _read_regular(path, 16)
    except FileNotFoundError:
        return "unknown"
    except (OSError, BootContextError) as exc:
        raise BootLockError("unable to read boot-lock witness") from exc

    return decode_boot_lock(raw)
