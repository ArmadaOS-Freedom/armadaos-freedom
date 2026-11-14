"""Read-only extraction and normalization of candidate BLS deployment options."""

import re
from pathlib import Path

from .deployment_store import (
    DeploymentError,
    _binding_of,
    _candidate_path,
    _gi,
    _parse_binding,
    _require_nonsymlink_dir,
)

_MAX_OPTIONS_BYTES = 64 * 1024
_UINT32_MAX = 0xFFFFFFFF
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_OSTREE_RE = re.compile(
    r"/ostree/boot\.[01]/([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([0-9a-f]{64})/(0|[1-9][0-9]{0,9})"
)
_FORBIDDEN_KEYS = ("init", "rdinit", "rd.break")


class DeploymentOptionsError(RuntimeError):
    """Raised when OSTree deployment BLS options are missing or invalid."""


def _parse_binding_arg(binding, label):
    if not isinstance(binding, str):
        raise DeploymentOptionsError(f"{label} must be str")
    try:
        return _parse_binding(binding)
    except DeploymentError as exc:
        raise DeploymentOptionsError(f"invalid {label}: {exc}") from exc


def _resolve_sysroot(sysroot_path):
    if not isinstance(sysroot_path, (str, Path)):
        raise DeploymentOptionsError("sysroot_path must be str or Path")
    raw = Path(sysroot_path)
    if not str(raw) or not raw.is_absolute():
        raise DeploymentOptionsError(f"sysroot_path must be absolute: {raw}")
    if raw.is_symlink() or not raw.is_dir():
        raise DeploymentOptionsError(f"sysroot_path must be non-symlink dir: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise DeploymentOptionsError(f"cannot resolve sysroot_path: {exc}") from exc
    if resolved != raw:
        raise DeploymentOptionsError(f"non-canonical sysroot_path: {raw}")
    _require_nonsymlink_dir(resolved, "ostree", "repo")
    return resolved


def _parse_options_tokens(raw_options, label):
    if not isinstance(raw_options, str) or not raw_options:
        raise DeploymentOptionsError(f"{label} bootconfig options missing or empty")
    if len(raw_options) > _MAX_OPTIONS_BYTES:
        raise DeploymentOptionsError(f"{label} bootconfig options exceed 64KiB")
    for ch in raw_options:
        code = ord(ch)
        if ch in ("'", '"', "\\") or (code > 0x7E) or (code < 0x20 and ch not in (" ", "\t")):
            raise DeploymentOptionsError(f"{label} options contain invalid character: {ch!r}")
    tokens = [tok for tok in re.split(r"[ \t]+", raw_options) if tok]
    if not tokens:
        raise DeploymentOptionsError(f"{label} bootconfig options contain no tokens")
    return tokens


def _extract_uuid(tokens, key, label):
    prefix = f"{key}="
    vals = []
    for tok in tokens:
        if tok == key:
            raise DeploymentOptionsError(f"{label} options contain bare {key}")
        if tok.startswith(prefix):
            vals.append(tok[len(prefix):])
    if len(vals) != 1:
        raise DeploymentOptionsError(f"{label} options must have exactly one {key}= token")
    val = vals[0]
    if not val.startswith("UUID="):
        raise DeploymentOptionsError(f"{label} {key}= must use UUID= syntax")
    uuid_str = val[len("UUID="):]
    if not _UUID_RE.fullmatch(uuid_str) or uuid_str == _ZERO_UUID:
        raise DeploymentOptionsError(f"{label} invalid or zero {key} UUID: {uuid_str!r}")
    return uuid_str


def _validate_entry(dep, root, expected_path, osname, allowed_bindings, label):
    bootcfg = dep.get_bootconfig()
    if bootcfg is None:
        raise DeploymentOptionsError(f"{label} deployment missing bootconfig")
    tokens = _parse_options_tokens(bootcfg.get("options"), label)
    for tok in tokens:
        for bad in _FORBIDDEN_KEYS:
            if tok == bad or tok.startswith(f"{bad}="):
                raise DeploymentOptionsError(f"{label} options forbid {bad}")
    root_uuid = _extract_uuid(tokens, "root", label)
    boot_uuid = _extract_uuid(tokens, "boot", label)
    if root_uuid == boot_uuid:
        raise DeploymentOptionsError(f"{label} root and boot UUIDs must differ")

    ostree_vals = []
    armada_vals = []
    for tok in tokens:
        if tok in ("ostree", "armada.deployment"):
            raise DeploymentOptionsError(f"{label} options contain bare {tok}")
        if tok.startswith("ostree="):
            ostree_vals.append(tok[len("ostree="):])
        elif tok.startswith("armada.deployment="):
            armada_vals.append(tok[len("armada.deployment="):])
    if len(ostree_vals) != 1:
        raise DeploymentOptionsError(f"{label} options must have exactly one ostree= token")
    m = _OSTREE_RE.fullmatch(ostree_vals[0])
    if not m or m.group(1) != osname or int(m.group(3)) > _UINT32_MAX:
        raise DeploymentOptionsError(f"{label} malformed ostree= pointer: {ostree_vals[0]!r}")
    ptr_path = root / ostree_vals[0].lstrip("/")
    if not ptr_path.is_symlink():
        raise DeploymentOptionsError(f"{label} ostree pointer is not a symlink: {ptr_path}")
    try:
        resolved_ptr = ptr_path.resolve(strict=True)
    except OSError as exc:
        raise DeploymentOptionsError(f"{label} cannot resolve ostree pointer {ptr_path}: {exc}") from exc
    if resolved_ptr != Path(expected_path):
        raise DeploymentOptionsError(f"{label} ostree pointer resolves to {resolved_ptr}, expected {expected_path}")

    if len(armada_vals) > 1:
        raise DeploymentOptionsError(f"{label} duplicate armada.deployment token")
    if armada_vals and armada_vals[0] not in allowed_bindings:
        raise DeploymentOptionsError(f"{label} conflicting armada.deployment: {armada_vals[0]!r}")
    return tokens, root_uuid, boot_uuid


def read_deployment_options(*, sysroot_path, current_binding, target_binding):
    try:
        Gio, GLib, OSTree = _gi()
    except Exception as exc:
        raise DeploymentOptionsError(f"OSTree GI unavailable: {exc}") from exc
    glib_err = getattr(GLib, "Error", Exception)
    try:
        cur_os, cur_commit, cur_serial = _parse_binding_arg(current_binding, "current_binding")
        tgt_os, tgt_commit, tgt_serial = _parse_binding_arg(target_binding, "target_binding")
        if current_binding == target_binding:
            raise DeploymentOptionsError("current_binding and target_binding must differ")
        if cur_os != tgt_os:
            raise DeploymentOptionsError("current and target osname mismatch")
        if cur_commit == tgt_commit:
            raise DeploymentOptionsError("current and target commits must differ")

        root = _resolve_sysroot(sysroot_path)
        sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
        ok, acquired = sysroot.try_lock()
        if not (ok and acquired):
            raise DeploymentOptionsError("sysroot busy: try_lock not acquired")
        try:
            if not sysroot.load(None):
                raise DeploymentOptionsError("sysroot load failed")
            deployments = list(sysroot.get_deployments() or [])
            cur_matches = [d for d in deployments if _binding_of(d) == current_binding]
            tgt_matches = [d for d in deployments if _binding_of(d) == target_binding]
            if len(cur_matches) != 1 or len(tgt_matches) != 1:
                raise DeploymentOptionsError("current and target bindings must each appear once")
            cur_dep, tgt_dep = cur_matches[0], tgt_matches[0]
            if (cur_dep.get_unlocked() != OSTree.DeploymentUnlockedState.NONE or
                    tgt_dep.get_unlocked() != OSTree.DeploymentUnlockedState.NONE):
                raise DeploymentOptionsError("current or target deployment is unlocked")
            if cur_dep.get_deployserial() != cur_serial or tgt_dep.get_deployserial() != tgt_serial:
                raise DeploymentOptionsError("deployment serial mismatch")
            if not cur_dep.is_pinned() or not tgt_dep.is_pinned():
                raise DeploymentOptionsError("current and target deployments must be pinned")

            cur_path = _candidate_path(sysroot, root, cur_dep, cur_os, cur_commit)
            tgt_path = _candidate_path(sysroot, root, tgt_dep, tgt_os, tgt_commit)
            _, cur_root, cur_boot = _validate_entry(
                cur_dep, root, cur_path, cur_os, {current_binding}, "current"
            )
            tgt_tokens, tgt_root, tgt_boot = _validate_entry(
                tgt_dep, root, tgt_path, tgt_os, {current_binding, target_binding}, "target"
            )
            if (cur_root, cur_boot) != (tgt_root, tgt_boot):
                raise DeploymentOptionsError("current and target root/boot UUID pair mismatch")

            kept = [t for t in tgt_tokens if not t.startswith("armada.deployment=")]
            kept.append(f"armada.deployment={target_binding}")
            normal_options = " ".join(kept)
            if len(normal_options) > _MAX_OPTIONS_BYTES:
                raise DeploymentOptionsError("normalized options exceed 64KiB")
            return {
                "deployment": target_binding,
                "path": tgt_path,
                "options": normal_options,
                "rootUuid": tgt_root,
                "bootUuid": tgt_boot,
            }
        finally:
            sysroot.unlock()
    except DeploymentOptionsError:
        raise
    except (DeploymentError, glib_err, OSError, RuntimeError) as exc:
        raise DeploymentOptionsError(str(exc)) from exc
