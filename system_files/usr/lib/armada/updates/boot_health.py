# SPDX-License-Identifier: GPL-3.0-only
"""Read-only boot evidence collection for system update trial confirmation."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
from typing import Any

import armada_os
from . import boot_context, boot_payload, recovery_device
from .trial_record import (
    CONFIRMED,
    TRIAL,
    TrialRecord,
    TrialRecordError,
    encode_record,
    expected_boot_sha256,
)

ROOT_PATH = Path("/")
SYS_DEV_BLOCK = Path("/sys/dev/block")
COMPATIBLE_PATH = Path("/sys/firmware/devicetree/base/compatible")
TRANSACTION_PATH = Path("/sys/firmware/devicetree/base/chosen/armada,update-transaction")
MANIFEST_RELPATH = "usr/share/armada/update-boot/manifest.json"
CONFIG_RELPATH = "usr/share/armada/update-boot/edl-devices.json"

TRANSACTION_BYTES = 16
COMPATIBLE_MAX_BYTES = 4096
MANIFEST_MAX_BYTES = 1024 * 1024
BOOT_MAX_BYTES = 512 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
BLKID_TIMEOUT_SECONDS = 15
BLKGETSIZE64 = 0x80081272


class BootHealthError(RuntimeError):
    """Raised when running boot evidence does not satisfy the update trial."""


@dataclass(frozen=True)
class BootEvidence:
    transaction_uuid: bytes
    actual_slot: int
    deployment: str
    kernel_release: str
    boot_sha256: bytes


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BootHealthError(message)


def _validate_boot_bytes(value: object) -> int:
    if type(value) is not int or value <= 0 or value > BOOT_MAX_BYTES or value % 4096 != 0:
        raise BootHealthError("Invalid expected bootPartitionBytes")
    return value


def _read_transaction_marker(path: Path | None = None) -> bytes:
    path = TRANSACTION_PATH if path is None else path
    raw = boot_context._read_regular(path, TRANSACTION_BYTES)
    if len(raw) != TRANSACTION_BYTES or raw == (b"\x00" * TRANSACTION_BYTES):
        raise BootHealthError("Update transaction DT marker must be 16 nonzero bytes")
    return raw


def _read_compatible_tokens(path: Path | None = None) -> set[str]:
    path = COMPATIBLE_PATH if path is None else path
    raw = boot_context._read_regular(path, COMPATIBLE_MAX_BYTES)
    if not raw:
        raise BootHealthError("Device-tree compatible property is empty")
    try:
        tokens = {part.decode("ascii") for part in raw.split(b"\x00") if part}
    except UnicodeDecodeError as exc:
        raise BootHealthError("Device-tree compatible property is not ASCII") from exc
    _require(bool(tokens), "Device-tree compatible property has no tokens")
    return tokens


def _select_model(config: dict[str, Any], compatible: set[str]) -> str:
    profiles = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise BootHealthError("Device configuration defines no profiles")
    matches: list[str] = []
    for model, profile in profiles.items():
        if not isinstance(model, str) or not model or not isinstance(profile, dict):
            raise BootHealthError("Invalid model profile in device configuration")
        required = profile.get("required")
        forbidden = profile.get("forbidden")
        if (
            not isinstance(required, list)
            or not required
            or not all(isinstance(item, str) and item for item in required)
            or not isinstance(forbidden, list)
            or not all(isinstance(item, str) and item for item in forbidden)
        ):
            raise BootHealthError(f"Invalid compatible rules for model {model}")
        if all(req in compatible for req in required) and not any(forb in compatible for forb in forbidden):
            matches.append(model)
    _require(len(matches) == 1, f"Expected 1 compatible model profile, matched {len(matches)}")
    return matches[0]


def _verify_boot_payload(root: Path = ROOT_PATH) -> tuple[str, int]:
    canonical_root, canonical_usr = boot_payload._resolve_root(root)
    manifest_path, _ = boot_payload._resolve_regular_file(
        canonical_root, canonical_usr, MANIFEST_RELPATH
    )
    config_path, _ = boot_payload._resolve_regular_file(
        canonical_root, canonical_usr, CONFIG_RELPATH
    )
    try:
        manifest_raw = boot_context._read_regular(manifest_path, MANIFEST_MAX_BYTES).decode("utf-8")
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootHealthError("Boot payload manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise BootHealthError("Boot payload manifest must be a JSON object")

    edl_device_config, _ = boot_payload._load_helpers()
    config = edl_device_config.load_configuration(config_path)
    model = _select_model(config, _read_compatible_tokens())
    verified = boot_payload.verify_manifest(canonical_root, model, manifest)
    kernel_release = verified.get("kernelRelease")
    running_release = os.uname().release
    _require(
        isinstance(kernel_release, str) and kernel_release == running_release,
        "Running kernel release does not match verified boot payload manifest",
    )
    profile = verified.get("profile")
    if not isinstance(profile, dict):
        raise BootHealthError("Verified boot payload is missing model profile")
    expected_bytes = _validate_boot_bytes(profile.get("bootPartitionBytes"))
    return running_release, expected_bytes


def _sysfs_block_node(rdev: int) -> Path:
    return (SYS_DEV_BLOCK / f"{os.major(rdev)}:{os.minor(rdev)}").resolve(strict=True)


def _ufs_controller(sysfs_node: Path) -> Path:
    resolved = sysfs_node.resolve(strict=True)
    matches = [idx for idx, part in enumerate(resolved.parts) if part.endswith(".ufs")]
    if len(matches) != 1:
        raise BootHealthError(f"Block sysfs path does not have a unique UFS controller: {resolved}")
    return Path(*resolved.parts[: matches[0] + 1])


def _locate_boot_partition(slot_letter: str, recovery_rdev: int, expected_bytes: int) -> tuple[Path, int]:
    _require(slot_letter in ("a", "b"), "Invalid boot slot letter")
    expected_bytes = _validate_boot_bytes(expected_bytes)
    recovery_ufs = _ufs_controller(_sysfs_block_node(recovery_rdev))
    expected_label = f"boot_{slot_letter}"
    result = subprocess.run(
        ["blkid", "-t", f"PARTLABEL={expected_label}", "-o", "device"],
        capture_output=True,
        text=True,
        check=True,
        timeout=BLKID_TIMEOUT_SECONDS,
    )
    candidates: dict[int, Path] = {}
    for line in result.stdout.splitlines():
        raw_name = line.strip()
        if not raw_name:
            continue
        dev = Path(raw_name).resolve(strict=True)
        info = dev.stat()
        _require(stat.S_ISBLK(info.st_mode), f"Boot candidate is not a block device: {dev}")
        node = _sysfs_block_node(info.st_rdev)
        try:
            candidate_ufs = _ufs_controller(node)
        except BootHealthError:
            continue
        if candidate_ufs != recovery_ufs:
            continue
        _require((node / "partition").is_file(), f"Boot candidate is not a partition: {dev}")
        size_sectors = int((node / "size").read_text(encoding="utf-8").strip())
        _require(size_sectors * 512 == expected_bytes, "Boot partition sysfs size mismatch")
        logical_block = int((node.parent / "queue" / "logical_block_size").read_text(encoding="utf-8").strip())
        _require(logical_block in (512, 4096), "Unsupported boot partition logical block size")
        label_res = subprocess.run(
            ["blkid", "-s", "PARTLABEL", "-o", "value", str(dev)],
            capture_output=True,
            text=True,
            check=True,
            timeout=BLKID_TIMEOUT_SECONDS,
        )
        _require(label_res.stdout.strip() == expected_label, "Boot partition PARTLABEL mismatch")
        candidates.setdefault(info.st_rdev, dev)
    _require(len(candidates) == 1, f"Expected 1 internal {expected_label} partition, found {len(candidates)}")
    rdev, dev = next(iter(candidates.items()))
    return dev, rdev


def _hash_boot_partition(dev: Path, expected_rdev: int, expected_bytes: int) -> bytes:
    expected_bytes = _validate_boot_bytes(expected_bytes)
    fd = os.open(str(dev), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        _require(
            stat.S_ISBLK(info.st_mode) and info.st_rdev == expected_rdev,
            "Opened boot device does not match verified block device",
        )
        size_buf = fcntl.ioctl(fd, BLKGETSIZE64, b"\x00" * 8)
        (actual_bytes,) = struct.unpack("=Q", size_buf)
        _require(actual_bytes == expected_bytes, "Boot block device ioctl size mismatch")
        digest = hashlib.sha256()
        offset = 0
        while offset < expected_bytes:
            chunk = os.pread(fd, min(READ_CHUNK_BYTES, expected_bytes - offset), offset)
            if not chunk:
                raise BootHealthError("Unexpected EOF while hashing boot partition")
            digest.update(chunk)
            offset += len(chunk)
        return digest.digest()
    finally:
        os.close(fd)


def _read_live_evidence() -> tuple[dict[str, Any], dict[str, str], bytes]:
    snap = armada_os.snapshot()
    if not isinstance(snap, dict):
        raise BootHealthError("OS snapshot must be a dictionary")
    booted = snap.get("booted")
    context = boot_context.read_boot_context(booted)
    marker = _read_transaction_marker()
    return dict(booted), context, marker


def collect_boot_evidence(record: TrialRecord) -> BootEvidence:
    encode_record(record)
    _require(record.state in (TRIAL, CONFIRMED), "Trial record state cannot be confirmed")
    expected_deployment = f"{record.os_name}/{record.new_commit.hex()}.{record.new_serial}"

    initial_booted, initial_context, initial_marker = _read_live_evidence()
    actual_slot_letter = initial_context.get("actualSlot")
    _require(actual_slot_letter in ("a", "b"), "Booted slot is not a strict A/B slot")
    actual_idx = "ab".index(actual_slot_letter)
    try:
        expected_sha256 = expected_boot_sha256(record, actual_idx)
    except TrialRecordError as exc:
        raise BootHealthError(
            "actual slot is not valid for the trial record"
        ) from exc
    _require(
        initial_context.get("deployment") == expected_deployment,
        "Booted deployment does not match trial target deployment",
    )
    _require(
        initial_marker == record.transaction_uuid,
        "Device-tree update transaction marker does not match trial transaction_uuid",
    )

    kernel_release, expected_bytes = _verify_boot_payload(ROOT_PATH)
    _, _, _, recovery_rdev = recovery_device.locate_recovery_partition(require_edl=False)
    boot_dev, boot_rdev = _locate_boot_partition(actual_slot_letter, recovery_rdev, expected_bytes)
    boot_sha256 = _hash_boot_partition(boot_dev, boot_rdev, expected_bytes)
    _require(boot_sha256 == expected_sha256, "Active boot partition digest mismatch")

    final_booted, final_context, final_marker = _read_live_evidence()
    _require(
        final_booted == initial_booted
        and final_context == initial_context
        and final_marker == initial_marker,
        "Boot context evidence changed during verification",
    )
    return BootEvidence(
        transaction_uuid=record.transaction_uuid,
        actual_slot=actual_idx,
        deployment=expected_deployment,
        kernel_release=kernel_release,
        boot_sha256=boot_sha256,
    )
