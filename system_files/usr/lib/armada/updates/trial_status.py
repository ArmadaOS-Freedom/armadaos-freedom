"""Read-only integration of update trial journal status."""

import subprocess
import uuid
from pathlib import Path

from . import (
    boot_context,
    finalization_receipt,
    offline_context,
    recovery_device,
    trial_record,
    trial_store,
)


_RECORD_STATES = {
    trial_record.PREPARED: "prepared",
    trial_record.TRIAL: "trial",
    trial_record.CONFIRMED: "confirmed",
    trial_record.ROLLED_BACK: "rolled_back",
}


def _unavailable(error):
    return {"state": "unavailable", "error": str(error) or type(error).__name__}


def _canonical_deployment(os_name, commit, serial):
    return f"{os_name}/{commit.hex()}.{serial}"


def read_trial_status(
    context,
    transaction_path=Path(
        "/sys/firmware/devicetree/base/chosen/armada,update-transaction"),
):
    if not isinstance(context, dict):
        return _unavailable("Boot context is malformed")
    if (context.get("state") != "available"
            or context.get("actualSlot") not in ("a", "b")
            or not isinstance(context.get("deployment"), str)
            or not context.get("deployment")):
        return _unavailable(context.get("error") or "Boot context is unavailable")

    actual_slot = context["actualSlot"]
    deployment = context["deployment"]
    try:
        with recovery_device.open_recovery(writable=False) as opened:
            fd, part_uuid, board = opened
            snapshot = trial_store.load(fd, part_uuid, board)
        record = snapshot.record
        if record is None:
            return {"state": "empty"}

        record_state = _RECORD_STATES.get(record.state)
        if record_state is None:
            raise ValueError(f"Unsupported trial record state: {record.state}")

        old_deployment = _canonical_deployment(
            record.os_name, record.old_commit, record.old_serial)
        new_deployment = _canonical_deployment(
            record.os_name, record.new_commit, record.new_serial)
        result = {
            "state": "available",
            "recordState": record_state,
            "policy": "offline-dual" if record.policy == trial_record.OFFLINE_DUAL else "online-single",
            "transactionId": str(uuid.UUID(bytes=record.transaction_uuid)),
            "sequence": record.sequence,
            "fromSlot": "ab"[record.from_slot],
            "targetSlot": "ab"[record.target_slot],
            "remainingAttempts": record.remaining,
            "attemptLimit": record.attempt_limit,
            "oldDeployment": old_deployment,
            "newDeployment": new_deployment,
            "planSha256": record.plan_sha256.hex(),
            "runningDeployment": None,
        }

        # Running the old deployment is evidence only when the live boot
        # witness slot and the cmdline deployment both match the trial's own
        # from-slot binding.  The slot the journal selected for writing is never
        # used as a substitute for that evidence.
        # An offline dual transaction legitimately sits on the old deployment
        # while it is prepared (written but not rebooted) and after a rollback.
        old_is_expected = (
            record.policy == trial_record.ONLINE_SINGLE
            or record.state in (trial_record.PREPARED, trial_record.ROLLED_BACK)
        )
        if (old_is_expected
                and actual_slot == "ab"[record.from_slot]
                and deployment == old_deployment):
            result["runningDeployment"] = "old"
        elif (deployment == new_deployment
                and (actual_slot == "ab"[record.target_slot]
                     or (record.policy == trial_record.OFFLINE_DUAL
                         and record.state == trial_record.CONFIRMED
                         and actual_slot == "ab"[record.from_slot]))):
            try:
                marker = boot_context._read_regular(transaction_path, 16)
            except (RuntimeError, ValueError, OSError,
                    subprocess.SubprocessError) as error:
                result["bootEvidenceError"] = str(error) or type(error).__name__
            else:
                if marker == record.transaction_uuid:
                    result["runningDeployment"] = "new"
                else:
                    result["bootEvidenceError"] = (
                        "The transaction marker does not match the trial record"
                    )

        result["bootConfirmed"] = (
            record.state == trial_record.CONFIRMED
            and result["runningDeployment"] == "new"
        )
        if (
            record.state == trial_record.CONFIRMED
            and result["runningDeployment"] == "new"
        ):
            result["finalization"] = finalization_receipt.read(record)
        return result
    except (RuntimeError, ValueError, OSError,
            subprocess.SubprocessError) as error:
        return _unavailable(error)


def read_legacy_trial_status(context):
    """Read the durable journal for a controller-only legacy boot.

    This is a pre-reboot reader.  It identifies the running side of the
    transaction from the exact booted deployment, never from a slot: a legacy
    system has no witness, so ``actualSlot``/``inactiveSlot`` stay null and
    ``bootConfirmed`` is always false.  Confirmation requires the new witness
    and finalization evidence after the reboot.
    """
    if (
        not isinstance(context, dict)
        or context.get("bootContext") != offline_context.LEGACY_BOOT_CONTEXT
        or context.get("actualSlot") is not None
        or context.get("inactiveSlot") is not None
        or not isinstance(context.get("deployment"), str)
        or not context.get("deployment")
    ):
        return _unavailable("Legacy boot context is malformed")

    deployment = context["deployment"]
    try:
        with recovery_device.open_recovery(writable=False) as opened:
            fd, part_uuid, board = opened
            snapshot = trial_store.load(fd, part_uuid, board)
        record = snapshot.record
        if record is None:
            return {"state": "empty",
                    "bootContext": offline_context.LEGACY_BOOT_CONTEXT}

        record_state = _RECORD_STATES.get(record.state)
        if record_state is None:
            raise ValueError(f"Unsupported trial record state: {record.state}")

        old_deployment = _canonical_deployment(
            record.os_name, record.old_commit, record.old_serial)
        new_deployment = _canonical_deployment(
            record.os_name, record.new_commit, record.new_serial)
        if deployment == old_deployment:
            running = "old"
        elif deployment == new_deployment:
            running = "new"
        else:
            running = None
        return {
            "state": "available",
            "bootContext": offline_context.LEGACY_BOOT_CONTEXT,
            "actualSlot": None,
            "inactiveSlot": None,
            "recordState": record_state,
            "policy": (
                "offline-dual" if record.policy == trial_record.OFFLINE_DUAL
                else "online-single"
            ),
            "transactionId": str(uuid.UUID(bytes=record.transaction_uuid)),
            "sequence": record.sequence,
            "fromSlot": "ab"[record.from_slot],
            "targetSlot": "ab"[record.target_slot],
            "remainingAttempts": record.remaining,
            "attemptLimit": record.attempt_limit,
            "oldDeployment": old_deployment,
            "newDeployment": new_deployment,
            "planSha256": record.plan_sha256.hex(),
            "runningDeployment": running,
            "bootConfirmed": False,
        }
    except (RuntimeError, ValueError, OSError,
            subprocess.SubprocessError) as error:
        return _unavailable(error)
