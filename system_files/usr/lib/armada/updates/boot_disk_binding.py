import re
import struct

_OSTREE_RE = re.compile(r'/ostree/boot\.([01])/default/([0-9a-f]{64})/(0|[1-9][0-9]{0,9})')
_DEPLOY_RE = re.compile(r'default/([0-9a-f]{64})\.(0|[1-9][0-9]{0,9})')
_MAX = 2**31 - 1
_ZERO = '0' * 64


def encode_binding(ostree_pointer: str, deployment: str) -> bytes:
    if not isinstance(ostree_pointer, str) or not isinstance(deployment, str):
        raise TypeError("arguments must be str")
    mo = _OSTREE_RE.fullmatch(ostree_pointer)
    if mo is None:
        raise ValueError("malformed ostree pointer")
    md = _DEPLOY_RE.fullmatch(deployment)
    if md is None:
        raise ValueError("malformed deployment")
    version = int(mo.group(1))
    boot_checksum = mo.group(2)
    index = int(mo.group(3))
    commit = md.group(1)
    serial = int(md.group(2))
    if boot_checksum == _ZERO or commit == _ZERO:
        raise ValueError("zero checksum")
    if serial > _MAX or index > _MAX:
        raise ValueError("value out of range")
    return struct.pack(
        "<8sB7x64s64sII",
        b"ARMDEP1\0",
        version,
        boot_checksum.encode("ascii"),
        commit.encode("ascii"),
        serial,
        index,
    )
