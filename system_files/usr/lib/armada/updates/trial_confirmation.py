# SPDX-License-Identifier: GPL-3.0-only
"""Internal confirmation transaction for the Linux update trial journal.

Serializes with the shared Decky update lock, verifies pre- and post-stability
evidence against the pending trial record, then writes the CONFIRMED trial
record into the opposite journal slot."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Iterator

from . import (
    deployment_store,
    finalization_receipt,
    runtime_health,
    slot_activation,
    trial_store,
)
from .boot_health import BootEvidence, collect_boot_evidence
from .recovery_device import open_recovery
from .runtime_health import RuntimeEvidence, collect_runtime_evidence
from .trial_record import CONFIRMED, TRIAL, TrialRecordError, encode_record, expected_boot_sha256

UPDATE_LOCK_PATH = Path("/run/lock/armada-update.lock")
SYSROOT_PATH = Path("/sysroot")
STABILITY_SECONDS = 30


class ConfirmationError(RuntimeError):
    """Raised when confirmation cannot proceed or complete safely."""


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(UPDATE_LOCK_PATH, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfirmationError("update lock path is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfirmationError("update lock is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _check_boot_evidence(evidence: object, record) -> BootEvidence:
    if type(evidence) is not BootEvidence:
        raise ConfirmationError("boot evidence has wrong type")
    try:
        expected_sha256 = expected_boot_sha256(record, evidence.actual_slot)
    except TrialRecordError as exc:
        raise ConfirmationError("boot evidence slot is not valid for trial record") from exc
    expected_deployment = (
        f"{record.os_name}/{record.new_commit.hex()}.{record.new_serial}"
    )
    if evidence.deployment != expected_deployment:
        raise ConfirmationError("boot evidence deployment does not match expected deployment")
    if evidence.transaction_uuid != record.transaction_uuid:
        raise ConfirmationError("boot evidence transaction_uuid mismatch")
    if evidence.boot_sha256 != expected_sha256:
        raise ConfirmationError("boot evidence boot_sha256 mismatch")
    if not evidence.kernel_release:
        raise ConfirmationError("boot evidence kernel_release is empty")
    return evidence


def _check_runtime_evidence(evidence: object) -> RuntimeEvidence:
    if type(evidence) is not RuntimeEvidence:
        raise ConfirmationError("runtime evidence has wrong type")
    if not evidence.boot_id or not evidence.session_id or not evidence.display:
        raise ConfirmationError("runtime evidence identity fields are empty")
    if type(evidence.uid) is not int or evidence.uid <= 0:
        raise ConfirmationError("runtime evidence uid is not a positive int")
    if evidence.services != runtime_health.REQUIRED_UNITS:
        raise ConfirmationError("runtime evidence services do not match REQUIRED_UNITS")
    return evidence


def _finalize_deployment(record, boot: BootEvidence, *, sysroot_path=None) -> dict:
    previous_binding = (
        f"{record.os_name}/{record.old_commit.hex()}.{record.old_serial}"
    )
    expected_tx = str(uuid.UUID(bytes=bytes(record.transaction_uuid)))
    expected_slot = "ab"[boot.actual_slot]
    try:
        marked = slot_activation.mark_confirmed_slot(record)
        if (
            not isinstance(marked, dict)
            or marked.get("schemaVersion") != 1
            or marked.get("phase") != "slot_confirmed"
            or marked.get("targetSlot") != expected_slot
            or marked.get("deployment") != boot.deployment
            or marked.get("transactionUuid") != expected_tx
            or marked.get("recordSequence") != record.sequence
        ):
            raise ConfirmationError("confirmed slot metadata mismatch")
        promoted = deployment_store.promote_confirmed_deployment(
            sysroot_path=SYSROOT_PATH if sysroot_path is None else sysroot_path,
            current_binding=boot.deployment,
            previous_binding=previous_binding,
        )
    except Exception as exc:
        if isinstance(exc, ConfirmationError):
            raise
        raise ConfirmationError(f"trial finalization failed: {exc}") from exc
    if (
        not isinstance(promoted, dict)
        or promoted.get("deployment") != boot.deployment
        or promoted.get("previousDeployment") != previous_binding
        or not isinstance(promoted.get("changed"), bool)
    ):
        raise ConfirmationError("promoted deployment metadata mismatch")
    try:
        finalization_receipt.write(record)
    except Exception as exc:
        if isinstance(exc, ConfirmationError):
            raise
        raise ConfirmationError(
            f"finalization receipt write failed: {exc}"
        ) from exc
    return promoted


def confirm_trial() -> dict:
    """Confirm the pending trial record after verified stability evidence."""
    with _locked():
        with open_recovery(writable=False) as (fd, partition_uuid, board):
            snapshot = trial_store.load(fd, partition_uuid, board)

        record = snapshot.record
        if record is None or record.state != TRIAL:
            if record is not None and record.state == CONFIRMED:
                boot = _check_boot_evidence(collect_boot_evidence(record), record)
                _finalize_deployment(record, boot)
            return {
                "state": "not_pending",
                "recordState": None if record is None else record.state,
            }

        boot_before = _check_boot_evidence(
            collect_boot_evidence(record), record
        )
        runtime_before = _check_runtime_evidence(collect_runtime_evidence())

        time.sleep(STABILITY_SECONDS)

        boot_after = _check_boot_evidence(
            collect_boot_evidence(record), record
        )
        runtime_after = _check_runtime_evidence(collect_runtime_evidence())

        if boot_before != boot_after:
            raise ConfirmationError("boot evidence changed during stability window")
        if runtime_before != runtime_after:
            raise ConfirmationError("runtime evidence changed during stability window")

        next_record = dataclasses.replace(
            record, sequence=record.sequence + 1, state=CONFIRMED
        )
        encode_record(next_record)  # validates before any writable open

        with open_recovery(writable=True) as (wfd, wuuid, wboard):
            if wuuid != partition_uuid or wboard != board:
                raise ConfirmationError(
                    "reopened recovery partition identity changed"
                )
            fresh = trial_store.load(wfd, wuuid, wboard)
            if fresh != snapshot:
                raise ConfirmationError("concurrent modification of trial journal")
            trial_store.save(wfd, snapshot, next_record)
            verified = trial_store.load(wfd, wuuid, wboard)
            if verified.record != next_record:
                raise ConfirmationError("post-write journal verification failed")

        _finalize_deployment(next_record, boot_before)

        return {
            "state": "confirmed",
            "sequence": next_record.sequence,
            "transactionId": str(uuid.UUID(bytes=next_record.transaction_uuid)),
            "targetSlot": "a" if next_record.target_slot == 0 else "b",
            "deployment": boot_before.deployment,
            "firmwareSlotSuccess": "confirmed",
        }


def main() -> int:
    """Service entrypoint: run one confirmation attempt and report as JSON."""
    import argparse
    import json
    import subprocess
    import sys

    parser = argparse.ArgumentParser(
        prog="trial_confirmation",
        description="Confirm the pending Armada Linux update trial record.",
    )
    parser.parse_args()

    try:
        result = confirm_trial()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        failure = {
            "state": "confirmation_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(failure, separators=(",", ":")), file=sys.stderr)
        return 1

    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
