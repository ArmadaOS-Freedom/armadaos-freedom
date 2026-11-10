"""Capture physical boot sources and authenticate them with the boot tool."""

import json
import os
from copy import deepcopy
from pathlib import Path

from .boot_candidate import (
    BootCandidateError,
    _DEFAULT_BOOT_TOOL_PATH,
    _MODE_REPORTS,
    _SLOT_FILE_KEYS,
    _require_hex64,
    _require_positive_int,
    _run_stage,
    _to_cli_claim,
    _validate_boot_tool,
    _verify_claim,
    _write_new,
)
from .boot_lock import BootLockError, read_boot_lock
from .boot_materials import BootMaterialsError, _copy_verified, _validate_output_dir
from .boot_sources import (
    BootSourcesError,
    capture_boot_sources,
    capture_recovery_boot_sources,
)
from .physical_slots import (
    PhysicalSlotsError,
    inspect_physical_slots,
    inspect_recovery_slots,
)
from .recovery_boot_sources import (
    RecoveryBootSourcesError,
    resolve_session_recovery_boot_sources,
)


_NATIVE_MODELS = frozenset({"TB322FC", "TB323FU"})
_SLOT_CLI_KEYS = ("boot", "vendorBoot", "initBoot", "abl", "xblConfig", "vbmeta")
_SOURCE_KEY_MAP = {
    "boot": "current_boot",
    "vendorBoot": "vendor_boot",
    "initBoot": "init_boot",
    "abl": "abl",
    "xblConfig": "xbl_config",
}
_LOCK_STATES = {
    "unknown": "Unknown",
    "locked": "Locked",
    "unlocked": "Unlocked",
}
_MAX_LOADER_BUDGET_BYTES = 256 * 1024 * 1024
_TRUST_KEYS = frozenset({
    "model", "mode", "lockState", "a", "b", "gblRegion", "efispSha256",
})
_SLOT_TRUST_KEYS = frozenset({
    "slot", "material", "maxRawBytes", "rootPublicKeySha256",
    "bootPublicKeySha256", "metadataSha256",
    "payloadDigestMatchesSignedMetadata", "ablRootPublicKeySha256",
})
_LOADER_BUDGET_KEYS = frozenset({
    "profile", "maxRawBytes", "knownLoaderMatch", "evidence", "formula",
})


class BootTrustError(RuntimeError):
    """Raised when read-only boot-source trust inspection fails."""


def _verify_snapshot_claims(snapshot):
    for slot in ("a", "b"):
        for key in _SLOT_FILE_KEYS:
            _verify_claim(snapshot["slots"][slot][key], f"sources.slots.{slot}.{key}")
    for label, claim in snapshot["extraFiles"].items():
        _verify_claim(claim, f"sources.extraFiles.{label}")
    for slot, claim in snapshot.get("liveBoots", {}).items():
        _verify_claim(claim, f"sources.liveBoots.{slot}")


def _normalize_snapshot(snapshot, initial, extra_labels):
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schemaVersion", "phase", "physical", "slots", "extraFiles", "partitions"
    }:
        raise BootTrustError("invalid boot-source snapshot schema")
    if (
        type(snapshot.get("schemaVersion")) is not int
        or snapshot["schemaVersion"] != 1
        or snapshot.get("phase") != "boot_sources_captured"
        or snapshot.get("physical") != initial
        or not isinstance(snapshot.get("partitions"), dict)
    ):
        raise BootTrustError("invalid boot-source snapshot header")

    slots = snapshot.get("slots")
    if not isinstance(slots, dict) or set(slots) != {"a", "b"}:
        raise BootTrustError("boot-source snapshot must contain exactly slots a and b")
    normalized = dict(snapshot)
    normalized["slots"] = {}
    for slot in ("a", "b"):
        claims = slots[slot]
        if not isinstance(claims, dict) or set(claims) != set(_SLOT_FILE_KEYS):
            raise BootTrustError(f"invalid captured claims for slot {slot}")
        normalized["slots"][slot] = {
            key: _verify_claim(claims[key], f"sources.slots.{slot}.{key}")
            for key in _SLOT_FILE_KEYS
        }

    extras = snapshot.get("extraFiles")
    if not isinstance(extras, dict) or set(extras) != set(extra_labels):
        raise BootTrustError("captured extra-file labels do not match the request")
    normalized["extraFiles"] = {
        label: _verify_claim(extras[label], f"sources.extraFiles.{label}")
        for label in extra_labels
    }
    return normalized


def _validate_loader_budget(slot, budget):
    if not isinstance(budget, dict) or set(budget) != _LOADER_BUDGET_KEYS:
        raise BootTrustError(f"trust.{slot}.material.loaderBudget must be an object with profile, maxRawBytes, knownLoaderMatch, evidence, formula")
    if budget.get("knownLoaderMatch") is not True:
        raise BootTrustError(f"trust.{slot}.material.loaderBudget.knownLoaderMatch must be true")
    if not isinstance(budget.get("profile"), str):
        raise BootTrustError(f"trust.{slot}.material.loaderBudget.profile must be a string")
    if not isinstance(budget.get("evidence"), str):
        raise BootTrustError(f"trust.{slot}.material.loaderBudget.evidence must be a string")
    if not isinstance(budget.get("formula"), str):
        raise BootTrustError(f"trust.{slot}.material.loaderBudget.formula must be a string")
    _require_positive_int(budget.get("maxRawBytes"), f"trust.{slot}.material.loaderBudget.maxRawBytes")
    if budget["maxRawBytes"] > _MAX_LOADER_BUDGET_BYTES:
        raise BootTrustError(f"trust.{slot}.material.loaderBudget.maxRawBytes exceeds 256 MiB")
    return budget["maxRawBytes"]


def _validate_slot_trust(slot, slot_trust, claims):
    if not isinstance(slot_trust, dict) or set(slot_trust) != _SLOT_TRUST_KEYS:
        raise BootTrustError(f"invalid inspect-trust evidence for slot {slot}")
    if slot_trust.get("slot") != slot:
        raise BootTrustError(f"inspect-trust slot mismatch for {slot}")
    _require_positive_int(slot_trust.get("maxRawBytes"), f"trust.{slot}.maxRawBytes")
    for key in ("rootPublicKeySha256", "bootPublicKeySha256", "metadataSha256"):
        _require_hex64(slot_trust.get(key), f"trust.{slot}.{key}")
    abl_root = slot_trust.get("ablRootPublicKeySha256")
    if abl_root is not None:
        _require_hex64(abl_root, f"trust.{slot}.ablRootPublicKeySha256")
    if not isinstance(slot_trust.get("payloadDigestMatchesSignedMetadata"), bool):
        raise BootTrustError(f"trust.{slot}.payloadDigestMatchesSignedMetadata must be Boolean")

    material = slot_trust.get("material")
    if not isinstance(material, dict):
        raise BootTrustError(f"trust.{slot}.material must be an object")
    if (
        material.get("slot") != slot
        or material.get("isKshim") is not True
        or material.get("pageSize") != 4096
    ):
        raise BootTrustError(f"invalid loader material for slot {slot}")
    budget_max = _validate_loader_budget(slot, material.get("loaderBudget"))
    if slot_trust["maxRawBytes"] > budget_max:
        raise BootTrustError(f"trust.{slot}.maxRawBytes exceeds the material loader budget")

    hashes = material.get("sourceSha256")
    lengths = material.get("sourceLengths")
    expected_keys = set(_SOURCE_KEY_MAP.values())
    if (
        not isinstance(hashes, dict) or set(hashes) != expected_keys
        or not isinstance(lengths, dict) or set(lengths) != expected_keys
    ):
        raise BootTrustError(f"invalid authenticated source maps for slot {slot}")
    for claim_key, source_key in _SOURCE_KEY_MAP.items():
        claim = claims[claim_key]
        actual_sha = _require_hex64(
            hashes[source_key], f"trust.{slot}.material.sourceSha256.{source_key}"
        )
        actual_length = _require_positive_int(
            lengths[source_key], f"trust.{slot}.material.sourceLengths.{source_key}"
        )
        if actual_sha != claim["sha256"].lower() or actual_length != claim["bytes"]:
            raise BootTrustError(
                f"authenticated {source_key} does not match captured slot {slot} source"
            )


def _validate_trust(result, initial, lock_state, snapshot):
    trust = result.get("trust")
    if not isinstance(trust, dict) or set(trust) != _TRUST_KEYS:
        raise BootTrustError("invalid inspect-trust core evidence")
    if trust.get("model") != initial["model"]:
        raise BootTrustError("inspect-trust model mismatch")
    if trust.get("lockState") != _LOCK_STATES[lock_state]:
        raise BootTrustError("inspect-trust lock-state mismatch")
    mode = trust.get("mode")
    if not isinstance(mode, str) or mode not in _MODE_REPORTS.values():
        raise BootTrustError(f"unsupported inspect-trust mode: {mode!r}")

    for slot in ("a", "b"):
        _validate_slot_trust(slot, trust.get(slot), snapshot["slots"][slot])

    if mode == "AospResign":
        if not all(
            trust[slot]["payloadDigestMatchesSignedMetadata"] is True
            for slot in ("a", "b")
        ):
            raise BootTrustError("AOSP resignation requires matching signed payload metadata")
        if trust.get("gblRegion") is not None or trust.get("efispSha256") is not None:
            raise BootTrustError("AOSP resignation must not carry GBL region or efisp evidence")
    else:
        if trust.get("gblRegion") not in ("PRC", "ROW"):
            raise BootTrustError("GBL preservation requires a PRC or ROW region")
        if "efisp" not in snapshot["extraFiles"]:
            raise BootTrustError("GBL preservation requires a captured efisp source")
        efisp = snapshot["extraFiles"]["efisp"]
        if _require_hex64(trust.get("efispSha256"), "trust.efispSha256") != efisp["sha256"]:
            raise BootTrustError("inspect-trust efisp digest does not match the capture")
    return trust


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _slot_request(snapshot, slot):
    request = {
        key: _to_cli_claim(snapshot["slots"][slot][key]) for key in _SLOT_CLI_KEYS
    }
    request["recovery"] = _to_cli_claim(snapshot["extraFiles"][f"recovery_{slot}"])
    request["vbmetaSystem"] = _to_cli_claim(
        snapshot["extraFiles"][f"vbmeta_system_{slot}"]
    )
    return request


def _finalize_trust(
    tool_path, out, tmp, initial, initial_lock, request, snapshot, phase, recheck_physical
):
    """Shared final trust stage: CLI request, validation, rechecks, report write."""
    _, result = _run_stage(
        tool_path, "inspect-trust", request, out, tmp, "trust_inspected", None
    )
    trust = _validate_trust(result, initial, initial_lock, snapshot)

    _verify_snapshot_claims(snapshot)
    if not recheck_physical():
        raise BootTrustError("physical boot slots changed during trust inspection")
    if read_boot_lock() != initial_lock:
        raise BootTrustError("boot-lock state changed during trust inspection")

    report = {
        "schemaVersion": 1,
        "phase": phase,
        "physical": initial,
        "lockState": initial_lock,
        "mode": next(
            normalized for normalized, cli_mode in _MODE_REPORTS.items()
            if cli_mode == trust["mode"]
        ),
        "trust": trust,
        "sources": snapshot,
    }
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("ascii")
    path = out / "boot-trust.json"
    _write_new(path, payload)
    try:
        _fsync_directory(out)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return report


def capture_boot_trust(*, output_dir, boot_tool_path=_DEFAULT_BOOT_TOOL_PATH):
    """Capture and authenticate current physical boot sources without writing them."""
    try:
        out = _validate_output_dir(output_dir, [])
        tool_path = _validate_boot_tool(boot_tool_path, [], out)

        initial = inspect_physical_slots()
        if not isinstance(initial, dict) or initial.get("model") not in _NATIVE_MODELS:
            raise BootTrustError("native A/B boot trust is supported only on TB322FC and TB323FU")
        initial_lock = read_boot_lock()
        if not isinstance(initial_lock, str) or initial_lock not in _LOCK_STATES:
            raise BootTrustError(f"unsupported boot-lock state: {initial_lock!r}")

        out.mkdir(mode=0o700)
        tmp = out / "tmp"
        tmp.mkdir(mode=0o700)

        extra_labels = (
            "recovery_a", "recovery_b", "vbmeta_system_a", "vbmeta_system_b"
        )
        if initial["model"] == "TB323FU":
            extra_labels += ("efisp",)
        snapshot = capture_boot_sources(
            initial, out / "sources", extra_labels=extra_labels
        )
        snapshot = _normalize_snapshot(snapshot, initial, extra_labels)

        request = {
            "schemaVersion": 1,
            "model": initial["model"],
            "lockState": initial_lock,
            "a": _slot_request(snapshot, "a"),
            "b": _slot_request(snapshot, "b"),
            "efisp": (
                _to_cli_claim(snapshot["extraFiles"]["efisp"])
                if initial["model"] == "TB323FU" else None
            ),
        }
        return _finalize_trust(
            tool_path, out, tmp, initial, initial_lock, request, snapshot,
            "boot_trust_checked",
            lambda: inspect_physical_slots() == initial,
        )
    except BootTrustError:
        raise
    except (
        BootCandidateError,
        BootMaterialsError,
        BootSourcesError,
        BootLockError,
        PhysicalSlotsError,
        OSError,
    ) as exc:
        raise BootTrustError(str(exc)) from exc


def _stage_original_boots(out, resolved):
    """Copy authenticated original A/B boot bytes into the transaction.

    The resolver has already authenticated the original claims and matched
    live temporary vs original identity; this re-reads the exact original
    bytes with a bounded, fstat-checked copy so the inspect-trust request
    and later guards never depend on the ephemeral upload directory.
    """
    originals = out / "originals"
    originals.mkdir(mode=0o700)
    for slot in ("a", "b"):
        claim = resolved["originalBoots"][slot]
        src_fd = os.open(
            claim["path"],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        try:
            copied_sha = _copy_verified(
                src_fd, originals / f"boot_{slot}.img", claim["bytes"]
            )
        finally:
            os.close(src_fd)
        if copied_sha != claim["sha256"].lower():
            raise BootTrustError(
                f"staged original boot {slot} digest does not match the "
                "authenticated session claim"
            )
        staged = dict(claim)
        staged["path"] = str(originals / f"boot_{slot}.img")
        resolved["slots"][slot]["boot"] = staged
        resolved["originalBoots"][slot] = staged
    _fsync_directory(originals)


def capture_recovery_boot_trust(
    *,
    output_dir,
    original_boots,
    session_identity,
    temporary_boot_sha256,
    boot_tool_path=_DEFAULT_BOOT_TOOL_PATH,
):
    """Capture current RAM boot sources and authenticate the original A/B boots.

    The caller must hold the update lock and bind original_boots,
    session_identity and temporary_boot_sha256 to an authenticated one-time
    installer session. All recovery trust preparation is session-bound via
    the schema 2 session resolver; no raw resolver bypass exists here. The
    inspect-trust CLI request is built from the resolver's original boot
    slots, never from the temporary recovery boot; liveBoots are preserved
    for later write guards.
    """
    try:
        out = _validate_output_dir(output_dir, [])
        tool_path = _validate_boot_tool(boot_tool_path, [], out)

        initial = inspect_recovery_slots()
        if not isinstance(initial, dict) or initial.get("model") not in _NATIVE_MODELS:
            raise BootTrustError("native A/B boot trust is supported only on TB322FC and TB323FU")
        initial_lock = read_boot_lock()
        if not isinstance(initial_lock, str) or initial_lock not in _LOCK_STATES:
            raise BootTrustError(f"unsupported boot-lock state: {initial_lock!r}")

        out.mkdir(mode=0o700)
        tmp = out / "tmp"
        tmp.mkdir(mode=0o700)

        extra_labels = (
            "recovery_a", "recovery_b", "vbmeta_system_a", "vbmeta_system_b"
        )
        if initial["model"] == "TB323FU":
            extra_labels += ("efisp",)
        snapshot = capture_recovery_boot_sources(
            initial, out / "sources", extra_labels=extra_labels
        )

        resolved = resolve_session_recovery_boot_sources(
            session_identity,
            snapshot,
            original_boots,
            temporary_boot_sha256=temporary_boot_sha256,
        )
        resolved = deepcopy(resolved)
        _stage_original_boots(out, resolved)

        request = {
            "schemaVersion": 1,
            "model": initial["model"],
            "lockState": initial_lock,
            "a": _slot_request(resolved, "a"),
            "b": _slot_request(resolved, "b"),
            "efisp": (
                _to_cli_claim(resolved["extraFiles"]["efisp"])
                if initial["model"] == "TB323FU" else None
            ),
        }
        return _finalize_trust(
            tool_path, out, tmp, initial, initial_lock, request, resolved,
            "recovery_boot_trust_checked",
            lambda: inspect_recovery_slots() == initial,
        )
    except BootTrustError:
        raise
    except (
        BootCandidateError,
        BootMaterialsError,
        BootSourcesError,
        BootLockError,
        PhysicalSlotsError,
        RecoveryBootSourcesError,
        OSError,
    ) as exc:
        raise BootTrustError(str(exc)) from exc
