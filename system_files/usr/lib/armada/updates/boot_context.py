"""Strict read-only decoding of firmware boot context evidence."""

import os
import re
import stat
import struct
from pathlib import Path

WITNESS_PATH = Path('/sys/firmware/devicetree/base/chosen/armada,boot-witness')
CMDLINE_PATH = Path('/proc/cmdline')

WITNESS_BYTES = 16
CMDLINE_MAX = 64 * 1024

_OSNAME = re.compile(r'[0-9A-Za-z][0-9A-Za-z_.-]{0,63}\Z')
_UINT32_MAX = 0xFFFFFFFF


class BootContextError(ValueError):
    """Raised when boot context evidence is missing, unreadable, or invalid."""


def _read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BootContextError(f'{path} is not a regular file')
        if info.st_size > limit:
            raise BootContextError(f'{path} exceeds the {limit}-byte limit')
        chunks = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    if total > limit:
        raise BootContextError(f'{path} exceeds the {limit}-byte limit')
    return b''.join(chunks)


def _tokenize(cmdline):
    return [token for token in re.split(r'[ \t\n\r\f\v]+', cmdline) if token]


def _decode_witness(witness):
    if not isinstance(witness, (bytes, bytearray)) or len(witness) != WITNESS_BYTES:
        raise BootContextError('boot witness must be exactly 16 raw bytes')
    fields = struct.unpack('>4I', bytes(witness))
    if fields[0] != 1 or fields[1] != 1 or fields[3] != 0:
        raise BootContextError('boot witness header fields are invalid')
    if fields[2] not in (0, 1):
        raise BootContextError('boot witness slot is invalid')
    return fields[2]


def _deployment_binding(tokens):
    deployment = [t for t in tokens if t.startswith('armada.deployment=')]
    if len(deployment) != 1:
        raise BootContextError(
            'cmdline must contain exactly one armada.deployment token')
    value = deployment[0][len('armada.deployment='):]
    match = re.fullmatch(r'([^/]+)/([0-9a-f]{64})\.(0|[1-9][0-9]*)', value)
    if not match:
        raise BootContextError('armada.deployment value is malformed')
    osname, checksum, serial = match.groups()
    if not _OSNAME.fullmatch(osname):
        raise BootContextError('deployment osname is malformed')
    if len(serial) > 10 or int(serial) > _UINT32_MAX:
        raise BootContextError('deployment serial exceeds uint32')
    return osname, checksum, int(serial)


def _ostree_binding(tokens, osname):
    ostree = [t for t in tokens if t.startswith('ostree=')]
    if len(ostree) != 1:
        raise BootContextError('cmdline must contain exactly one ostree token')
    value = ostree[0][len('ostree='):]
    match = re.fullmatch(
        r'/ostree/boot\.([01])/([^/]+?)/([0-9a-f]{64})/(0|[1-9][0-9]*)', value)
    if not match:
        raise BootContextError('ostree value is malformed')
    _bootver, name, _bootcsum, index = match.groups()
    if name != osname:
        raise BootContextError('ostree osname differs from deployment osname')
    if len(index) > 10 or int(index) > _UINT32_MAX:
        raise BootContextError('ostree boot index exceeds uint32')


def _reject_bare_and_duplicates(tokens):
    seen = set()
    for token in tokens:
        for key in ('armada.deployment', 'ostree'):
            if token == key:
                raise BootContextError(f'bare {key} token is not allowed')
        if token.startswith('armada.deployment=') or token.startswith('ostree='):
            if token in seen:
                raise BootContextError(f'duplicate token rejected: {token}')
            seen.add(token)


def _check_booted(booted, osname, checksum, serial):
    if not isinstance(booted, dict):
        raise BootContextError('snapshot has no booted deployment record')
    if booted.get('incompatible'):
        raise BootContextError('booted deployment is marked incompatible')
    stateroot = booted.get('stateroot')
    if stateroot != osname:
        raise BootContextError('booted stateroot does not match cmdline osname')
    if booted.get('checksum') != checksum:
        raise BootContextError('booted checksum does not match deployment')
    booted_serial = booted.get('serial')
    if type(booted_serial) is not int or not 0 <= booted_serial <= _UINT32_MAX:
        raise BootContextError('booted serial is not a uint32 integer')
    if booted_serial != serial:
        raise BootContextError('booted serial does not match deployment')


def decode_boot_context(witness, cmdline, booted):
    """Validate raw firmware boot evidence and return the boot context."""
    if not isinstance(cmdline, str):
        raise BootContextError('cmdline must be a string')
    if '\x00' in cmdline:
        raise BootContextError('cmdline contains a NUL byte')
    try:
        encoded_cmdline = cmdline.encode('utf-8')
    except UnicodeError as error:
        raise BootContextError('cmdline is not valid UTF-8') from error
    if len(encoded_cmdline) > CMDLINE_MAX:
        raise BootContextError('cmdline exceeds the 64KiB limit')
    witness_slot = _decode_witness(witness)
    tokens = _tokenize(cmdline)
    _reject_bare_and_duplicates(tokens)
    osname, checksum, serial = _deployment_binding(tokens)
    _ostree_binding(tokens, osname)
    _check_booted(booted, osname, checksum, serial)
    actual = 'a' if witness_slot == 0 else 'b'
    inactive = 'b' if actual == 'a' else 'a'
    return {
        'state': 'available',
        'actualSlot': actual,
        'inactiveSlot': inactive,
        'deployment': f'{osname}/{checksum}.{serial}',
    }


def read_boot_context(booted, witness_path=WITNESS_PATH, cmdline_path=CMDLINE_PATH):
    """Read and decode boot context evidence from the running system."""
    try:
        witness = _read_regular(witness_path, WITNESS_BYTES)
        raw = _read_regular(cmdline_path, CMDLINE_MAX)
    except OSError as error:
        raise BootContextError(f'cannot read boot evidence: {error}') from error
    try:
        cmdline = raw.decode('utf-8')
    except UnicodeError as error:
        raise BootContextError('cmdline is not valid UTF-8') from error
    if '\x00' in cmdline:
        raise BootContextError('cmdline contains a NUL byte')
    try:
        return decode_boot_context(witness, cmdline, booted)
    except BootContextError:
        raise
    except (ValueError, struct.error) as error:
        raise BootContextError(f'invalid boot evidence: {error}') from error


def read_boot_slot(witness_path=WITNESS_PATH):
    """Read only the firmware boot witness and return actual/inactive slots.

    This infers no deployment; it exists for the RAM recovery updater.
    """
    try:
        witness = _read_regular(witness_path, WITNESS_BYTES)
        slot = _decode_witness(witness)
    except BootContextError:
        raise
    except (OSError, ValueError, struct.error) as error:
        raise BootContextError(f'cannot read boot witness: {error}') from error
    return {
        'actualSlot': 'a' if slot == 0 else 'b',
        'inactiveSlot': 'b' if slot == 0 else 'a',
    }
