from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
from typing import Any

SCHEMA_VERSION = 1
MANIFEST_KIND = "freedom-update-boot"
RUNTIME_SIZE_BYTES = 4 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
ARM64_MAGIC_OFFSET = 56
ARM64_MAGIC = b"ARM\x64"
FDT_MAGIC = 0xD00DFEED
FDT_MIN_HEADER_BYTES = 40
KERNEL_RELEASE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]*\Z")


def _load_helpers() -> tuple[Any, Any]:
    from . import edl_boot_requirements, edl_device_config

    return edl_device_config, edl_boot_requirements


def _resolve_root(root: str | Path) -> tuple[Path, Path]:
    root_path = Path(root)
    if not root_path.exists():
        raise ValueError(f"Root directory does not exist: {root_path}")
    canonical_root = root_path.resolve(strict=True)
    if not canonical_root.is_dir():
        raise ValueError(f"Root path is not a directory: {canonical_root}")
    usr_path = canonical_root / "usr"
    if not usr_path.exists():
        raise ValueError(f"Missing usr directory under root: {usr_path}")
    canonical_usr = usr_path.resolve(strict=True)
    if not canonical_usr.is_dir():
        raise ValueError(f"usr path is not a directory: {canonical_usr}")
    try:
        canonical_usr.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError(f"usr path escapes root: {canonical_usr}") from exc
    return canonical_root, canonical_usr


def _normalize_relpath(relpath: str) -> PurePosixPath:
    if not isinstance(relpath, str) or not relpath:
        raise ValueError("Relative path must be a non-empty string")
    if "\\" in relpath or relpath.startswith("/"):
        raise ValueError(f"Relative path must be canonical relative POSIX path: {relpath}")
    pure = PurePosixPath(relpath)
    if str(pure) != relpath:
        raise ValueError(f"Non-canonical relative path: {relpath}")
    parts = pure.parts
    if not parts or parts[0] != "usr" or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Relative path must stay under usr/: {relpath}")
    return pure


def _resolve_regular_file(canonical_root: Path, canonical_usr: Path, relpath: str) -> tuple[Path, str]:
    pure = _normalize_relpath(relpath)
    candidate = canonical_root.joinpath(*pure.parts)
    if candidate.is_symlink():
        raise ValueError(f"Refusing symlink leaf: {relpath}")
    if not candidate.exists():
        raise ValueError(f"Missing required file: {relpath}")
    parent = candidate.parent.resolve(strict=True)
    try:
        parent.relative_to(canonical_usr)
    except ValueError as exc:
        raise ValueError(f"File parent escapes canonical usr/: {relpath}") from exc
    resolved = candidate.resolve(strict=True)
    if resolved != parent / candidate.name:
        raise ValueError(f"Non-canonical file resolution for: {relpath}")
    try:
        resolved.relative_to(canonical_usr)
    except ValueError as exc:
        raise ValueError(f"Resolved file escapes canonical usr/: {relpath}") from exc
    if not resolved.is_file():
        raise ValueError(f"Path is not a regular file: {relpath}")
    return resolved, pure.as_posix()


def _discover_kernel_release(canonical_root: Path, canonical_usr: Path) -> str:
    modules_dir = canonical_root / "usr" / "lib" / "modules"
    if modules_dir.is_symlink() or not modules_dir.exists():
        raise ValueError(f"Missing modules directory: {modules_dir}")
    resolved_modules = modules_dir.resolve(strict=True)
    if not resolved_modules.is_dir():
        raise ValueError(f"Modules path is not a directory: {resolved_modules}")
    try:
        resolved_modules.relative_to(canonical_usr)
    except ValueError as exc:
        raise ValueError(f"Modules directory escapes usr/: {resolved_modules}") from exc
    entries = sorted(resolved_modules.iterdir(), key=lambda item: item.name)
    if len(entries) != 1:
        raise ValueError(f"Expected exactly 1 kernel directory in {resolved_modules}, found {len(entries)}")
    entry = entries[0]
    if entry.is_symlink() or not entry.is_dir():
        raise ValueError(f"Kernel release entry must be a real directory: {entry}")
    kver = entry.name
    if not KERNEL_RELEASE_RE.match(kver):
        raise ValueError(f"Invalid kernel release name: {kver!r}")
    return kver


def _inspect_file(path: Path, rel_posix: str, kind: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    total = 0
    header = bytearray()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            if len(header) < 64:
                need = 64 - len(header)
                header.extend(chunk[:need])
            total += len(chunk)
            digest.update(chunk)
    if total <= 0:
        raise ValueError(f"File must not be empty: {rel_posix}")
    if kind == "runtime" and total != RUNTIME_SIZE_BYTES:
        raise ValueError(f"Runtime {rel_posix} must be {RUNTIME_SIZE_BYTES} bytes, got {total}")
    elif kind == "kernel":
        if total < 64 or bytes(header[ARM64_MAGIC_OFFSET:60]) != ARM64_MAGIC:
            raise ValueError(f"Kernel {rel_posix} is not a valid ARM64 Image")
    elif kind == "dtb":
        if total < FDT_MIN_HEADER_BYTES:
            raise ValueError(f"DTB {rel_posix} is smaller than FDT header")
        magic, totalsize = struct.unpack(">II", bytes(header[:8]))
        if magic != FDT_MAGIC or totalsize < FDT_MIN_HEADER_BYTES or totalsize > total:
            raise ValueError(f"DTB {rel_posix} has invalid FDT header")
    return {"path": rel_posix, "bytes": total, "sha256": digest.hexdigest()}


def _record(canonical_root: Path, canonical_usr: Path, relpath: str, kind: str) -> dict[str, Any]:
    resolved, rel_posix = _resolve_regular_file(canonical_root, canonical_usr, relpath)
    return _inspect_file(resolved, rel_posix, kind)


def _collect(root: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical_root, canonical_usr = _resolve_root(root)
    kver = _discover_kernel_release(canonical_root, canonical_usr)
    edl_device_config, edl_boot_requirements = _load_helpers()
    config_rel = "usr/share/armada/update-boot/edl-devices.json"
    config_path, _ = _resolve_regular_file(canonical_root, canonical_usr, config_rel)
    config = edl_device_config.load_configuration(config_path)
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Configuration must define a non-empty profiles mapping")
    model_names = list(profiles)
    requirements = edl_boot_requirements.load_requirements(config, model_names)
    edl_boot_requirements.verify_tree(canonical_root, kver, requirements)
    config_record = _record(canonical_root, canonical_usr, config_rel, "config")
    kernel_record = _record(canonical_root, canonical_usr, f"usr/lib/modules/{kver}/vmlinuz", "kernel")
    initramfs_record = _record(
        canonical_root, canonical_usr, f"usr/lib/modules/{kver}/initramfs.img", "initramfs"
    )
    models: dict[str, Any] = {}
    for model, profile in profiles.items():
        if not isinstance(model, str) or not model or not isinstance(profile, dict):
            raise ValueError(f"Invalid model profile entry: {model!r}")
        dtb_name = profile.get("dtb")
        if not isinstance(dtb_name, str) or not dtb_name:
            raise ValueError(f"Model {model} is missing a valid dtb filename")
        runtime_record = _record(
            canonical_root,
            canonical_usr,
            f"usr/share/armada/update-boot/runtimes/{model}.bin",
            "runtime",
        )
        dtb_record = _record(
            canonical_root,
            canonical_usr,
            f"usr/lib/modules/{kver}/dtb/{dtb_name}",
            "dtb",
        )
        models[model] = {"runtime": runtime_record, "dtb": dtb_record}
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "kernelRelease": kver,
        "configuration": config_record,
        "kernel": kernel_record,
        "initramfs": initramfs_record,
        "models": models,
    }
    return manifest, config


def _canonical_json(document: dict[str, Any]) -> str:
    if not isinstance(document, dict):
        raise ValueError("Manifest document must be a JSON object")
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Manifest document is not valid canonical JSON") from exc


def create_manifest(root: str | Path) -> dict[str, Any]:
    manifest, _ = _collect(root)
    return manifest


def verify_manifest(root: str | Path, model: str, document: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(model, str) or not model:
        raise ValueError("Model must be a non-empty string")
    provided_json = _canonical_json(document)
    expected_manifest, config = _collect(root)
    if provided_json != _canonical_json(expected_manifest):
        raise ValueError("Boot payload manifest does not match deployment root")
    models = expected_manifest["models"]
    if model not in models:
        raise ValueError(f"Unknown model in boot payload manifest: {model}")
    model_entry = models[model]
    return {
        "kernelRelease": expected_manifest["kernelRelease"],
        "profile": dict(config["profiles"][model]),
        "kernel": expected_manifest["kernel"],
        "initramfs": expected_manifest["initramfs"],
        "runtime": model_entry["runtime"],
        "dtb": model_entry["dtb"],
    }
