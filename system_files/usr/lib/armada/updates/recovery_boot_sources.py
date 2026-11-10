"""Explicitly resolve pre-RAM original A/B boots vs current recovery boot.

This module performs offline validation of already-captured source claims
only. It is NOT an AVB trust, readiness, or write authorization step, and
it never mutates the filesystem or any supplied input.
"""

import copy
import json

from . import physical_slots
from .boot_candidate import BootCandidateError, _verify_claim
from .physical_slots import PhysicalSlotsError
from .recovery_authorization import RecoveryAuthorizationError as _RecoveryAuthorizationError
from .recovery_authorization import _session

_MAX_PARTITION_BYTES = 256 * 1024 * 1024
_SCHEMA_VERSION = 1
_PHASE = 'recovery_boot_sources_resolved'
_ALLOWED_MODELS = frozenset(('TB322FC', 'TB323FU'))
_SLOTS = ('a', 'b')
_SLOT_KEYS = ('boot', 'vendorBoot', 'initBoot', 'abl', 'xblConfig', 'vbmeta')
_SLOT_LABEL_MAP = {
    'boot': 'boot',
    'vendorBoot': 'vendor_boot',
    'initBoot': 'init_boot',
    'abl': 'abl',
    'xblConfig': 'xbl_config',
    'vbmeta': 'vbmeta',
}
_EXTRA_ALLOWED = frozenset((
    'recovery_a', 'recovery_b',
    'vbmeta_system_a', 'vbmeta_system_b',
    'dtbo_a', 'dtbo_b',
    'efisp',
))
_HEX_DIGITS = frozenset('0123456789abcdef')
_COMPLEMENT = {'a': 'b', 'b': 'a'}


class RecoveryBootSourcesError(RuntimeError):
    """Raised when recovery boot source resolution cannot be completed."""


def _require_hex64(value, field):
    if not isinstance(value, str) or len(value) != 64 or \
            any(ch not in _HEX_DIGITS for ch in value):
        raise RecoveryBootSourcesError(
            f'{field} must be a 64-char lowercase hex string')
    return value


def _require_exact_keys(mapping, expected, field):
    if not isinstance(mapping, dict) or set(mapping.keys()) != set(expected):
        raise RecoveryBootSourcesError(
            f'{field} must have exact keys {sorted(expected)}')


def _inspect_recovery_slots_fresh(expected_physical, when):
    try:
        fresh = physical_slots.inspect_recovery_slots()
    except PhysicalSlotsError as error:
        raise RecoveryBootSourcesError(
            f'fresh recovery slot inspection failed {when}: {error}') from error
    if fresh != expected_physical:
        raise RecoveryBootSourcesError(
            f'physical recovery slot report drifted {when}')


def resolve_recovery_boot_sources(live_snapshot, original_boots, *,
                                  expected_original_sha256,
                                  temporary_boot_sha256):
    """Resolve original A/B boot sources against the current RAM recovery boot.

    Callers must hold the update lock and bind all arguments to an
    authenticated one-time installer session. The expected hashes MUST come
    from installer records made before RAM entry (expected_original_sha256)
    and from the verified temporary candidate (temporary_boot_sha256); they
    are never inferred from supplied files. This function is preparation
    only: it is not AVB trust and not write authorization. It performs no
    shell execution, no block writes, and no output files.
    """
    try:
        return _resolve(live_snapshot, original_boots,
                        expected_original_sha256, temporary_boot_sha256)
    except RecoveryBootSourcesError:
        raise
    except (BootCandidateError, PhysicalSlotsError, OSError,
            KeyError, TypeError, ValueError) as error:
        raise RecoveryBootSourcesError(f'{error}') from error


def resolve_session_recovery_boot_sources(identity, live_snapshot,
                                          original_boots, *,
                                          temporary_boot_sha256):
    """Session-bind recovery boot-source resolution to a schema 2 identity.

    The caller must have loaded the identity from a root-owned
    authenticated installer session. This helper validates a private copy of
    the identity, pins it against the live snapshot and the supplied original
    boot claims, then delegates to resolve_recovery_boot_sources with the
    independently pinned per-slot boot hashes and the verified temporary
    boot hash. It rehashes the supplied files, reads no credentials, and mutates no inputs.
    """
    try:
        serialized = json.dumps(identity).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RecoveryBootSourcesError(
            f'session identity is not JSON-serializable: {error}') from error
    try:
        parsed = _session(serialized)
    except _RecoveryAuthorizationError as error:
        raise RecoveryBootSourcesError(
            f'invalid session identity: {error}') from error
    if parsed['schemaVersion'] != 2:
        raise RecoveryBootSourcesError(
            'session-bound recovery boot resolution requires identity schema 2')

    live = copy.deepcopy(live_snapshot)
    if not isinstance(live, dict):
        raise RecoveryBootSourcesError('live_snapshot must be a dict')
    physical = live.get('physical')
    if not isinstance(physical, dict):
        raise RecoveryBootSourcesError('live_snapshot.physical must be a dict')
    if physical.get('model') != parsed['model']:
        raise RecoveryBootSourcesError(
            'live physical model does not match the session identity')
    if physical.get('recoveryPartUuid') != parsed['recoveryPartUuid']:
        raise RecoveryBootSourcesError(
            'live recovery partition UUID does not match the session identity')
    if physical.get('actualSlot') != parsed['entrySlot']:
        raise RecoveryBootSourcesError(
            'live actualSlot does not match the session identity entrySlot')
    partitions = live.get('partitions')
    if not isinstance(partitions, dict):
        raise RecoveryBootSourcesError(
            'live_snapshot.partitions must be a dict')
    for slot in _SLOTS:
        claim = partitions.get(f'boot_{slot}')
        if not isinstance(claim, dict) or \
                claim.get('partitionUuid') != \
                parsed['originalBoots'][slot]['partitionUuid']:
            raise RecoveryBootSourcesError(
                f'live boot partition UUID for slot {slot} does not match the '
                f'pinned identity UUID')

    originals = copy.deepcopy(original_boots)
    if not isinstance(originals, dict) or set(originals) != set(_SLOTS):
        raise RecoveryBootSourcesError('original_boots must have exact keys a/b')
    for slot in _SLOTS:
        claim = originals[slot]
        if not isinstance(claim, dict) or \
                claim.get('bytes') != parsed['originalBoots'][slot]['bytes']:
            raise RecoveryBootSourcesError(
                f'original boot {slot} bytes do not match the pinned identity '
                f'bytes')

    resolved = resolve_recovery_boot_sources(
        live, originals,
        expected_original_sha256={
            slot: parsed['originalBoots'][slot]['sha256']
            for slot in _SLOTS
        },
        temporary_boot_sha256=temporary_boot_sha256,
    )
    resolved['recoverySessionId'] = parsed['sessionId']
    return resolved


def _resolve(live_snapshot, original_boots, expected_original_sha256,
             temporary_boot_sha256):
    if not isinstance(live_snapshot, dict):
        raise RecoveryBootSourcesError('live_snapshot must be a dict')
    if not isinstance(original_boots, dict):
        raise RecoveryBootSourcesError('original_boots must be a dict')
    if not isinstance(expected_original_sha256, dict):
        raise RecoveryBootSourcesError('expected_original_sha256 must be a dict')
    _require_exact_keys(expected_original_sha256, _SLOTS,
                        'expected_original_sha256')
    expected_original = {
        slot: _require_hex64(expected_original_sha256[slot],
                             f'expected_original_sha256[{slot}]')
        for slot in _SLOTS
    }
    _require_hex64(temporary_boot_sha256, 'temporary_boot_sha256')

    live = copy.deepcopy(live_snapshot)
    originals = copy.deepcopy(original_boots)

    if type(live.get('schemaVersion')) is not int or \
            live.get('schemaVersion') != 1:
        raise RecoveryBootSourcesError('live_snapshot schemaVersion must be 1')
    if live.get('phase') != 'recovery_boot_sources_captured':
        raise RecoveryBootSourcesError(
            'live_snapshot phase must be recovery_boot_sources_captured')
    physical = live.get('physical')
    if not isinstance(physical, dict):
        raise RecoveryBootSourcesError('live_snapshot.physical must be a dict')
    if type(physical.get('schemaVersion')) is not int or \
            physical.get('schemaVersion') != 1 or \
            physical.get('phase') != 'recovery_slots_checked':
        raise RecoveryBootSourcesError(
            'live_snapshot.physical must be a recovery_slots_checked report')
    if 'deployment' in physical or 'booted' in physical:
        raise RecoveryBootSourcesError(
            'live_snapshot.physical must not contain deployment or booted')
    if physical.get('model') not in _ALLOWED_MODELS:
        raise RecoveryBootSourcesError(
            f'unsupported model {physical.get("model")!r} in physical report')
    actual = physical.get('actualSlot')
    if actual not in _SLOTS or \
            physical.get('inactiveSlot') != _COMPLEMENT.get(actual):
        raise RecoveryBootSourcesError(
            'physical actualSlot/inactiveSlot must be complementary a/b')
    inactive = _COMPLEMENT[actual]

    live_slots = live.get('slots')
    _require_exact_keys(live_slots, _SLOTS, 'live_snapshot.slots')
    for slot in _SLOTS:
        _require_exact_keys(live_slots[slot], _SLOT_KEYS,
                            f'live_snapshot.slots[{slot}]')
    extra_files = live.get('extraFiles')
    if not isinstance(extra_files, dict):
        raise RecoveryBootSourcesError('live_snapshot.extraFiles must be a dict')
    for name in extra_files:
        if name not in _EXTRA_ALLOWED:
            raise RecoveryBootSourcesError(
                f'extraFiles entry not permitted: {name}')

    slot_labels = set()
    for slot in _SLOTS:
        for key in _SLOT_KEYS:
            slot_labels.add(f'{_SLOT_LABEL_MAP[key]}_{slot}')
    partitions = live.get('partitions')
    if not isinstance(partitions, dict):
        raise RecoveryBootSourcesError(
            'live_snapshot.partitions must be a dict')
    _require_exact_keys(partitions, slot_labels | set(extra_files.keys()),
                        'live_snapshot.partitions')

    _require_exact_keys(originals, _SLOTS, 'original_boots')

    # Fresh physical state must match the captured snapshot before hashing.
    _inspect_recovery_slots_fresh(physical, 'before hashing')

    # Rehash all supplied original claims.
    verified_originals = {}
    for slot in _SLOTS:
        verified_originals[slot] = _verify_claim(
            originals[slot], f'original_boots[{slot}]',
            max_bytes=_MAX_PARTITION_BYTES)
        if verified_originals[slot]['sha256'] != expected_original[slot]:
            raise RecoveryBootSourcesError(
                f'original boot {slot} hash does not equal the independent '
                f'installer-recorded expected hash')

    # Rehash all live per-slot claims (normalized by _verify_claim).
    verified_live_slots = {'a': {}, 'b': {}}
    for slot in _SLOTS:
        for key in _SLOT_KEYS:
            verified_live_slots[slot][key] = _verify_claim(
                live_slots[slot][key], f'live_snapshot.slots[{slot}][{key}]',
                max_bytes=_MAX_PARTITION_BYTES)
        if verified_originals[slot]['bytes'] != \
                verified_live_slots[slot]['boot']['bytes']:
            raise RecoveryBootSourcesError(
                f'original boot {slot} size does not equal live boot {slot} '
                f'size')

    # Rehash live extra file claims.
    verified_extras = {}
    for name in sorted(extra_files):
        verified_extras[name] = _verify_claim(
            extra_files[name], f'live_snapshot.extraFiles[{name}]',
            max_bytes=_MAX_PARTITION_BYTES)

    if verified_live_slots[actual]['boot']['sha256'] != temporary_boot_sha256:
        raise RecoveryBootSourcesError(
            f'live boot on actual slot {actual} does not match the verified '
            f'temporary boot hash')
    if verified_live_slots[inactive]['boot']['sha256'] != \
            expected_original[inactive]:
        raise RecoveryBootSourcesError(
            f'live boot on inactive slot {inactive} does not match its '
            f'original expected hash')

    # Fresh physical state must still match immediately before returning.
    _inspect_recovery_slots_fresh(physical, 'before return')

    resolved_slots = {'a': {}, 'b': {}}
    for slot in _SLOTS:
        for key in _SLOT_KEYS:
            if key == 'boot':
                resolved_slots[slot]['boot'] = copy.deepcopy(
                    verified_originals[slot])
            else:
                resolved_slots[slot][key] = copy.deepcopy(
                    verified_live_slots[slot][key])

    return {
        'schemaVersion': _SCHEMA_VERSION,
        'phase': _PHASE,
        'physical': copy.deepcopy(physical),
        'slots': resolved_slots,
        'extraFiles': copy.deepcopy(verified_extras),
        'partitions': copy.deepcopy(partitions),
        'liveBoots': {
            slot: copy.deepcopy(verified_live_slots[slot]['boot'])
            for slot in _SLOTS
        },
        'originalBoots': {
            slot: copy.deepcopy(verified_originals[slot])
            for slot in _SLOTS
        },
    }
