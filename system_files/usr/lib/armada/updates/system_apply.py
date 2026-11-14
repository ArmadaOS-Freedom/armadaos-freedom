"""Isolated no-automatic-reboot worker applying approved ArmadaOS Freedom system updates.

Runs the real system_transaction.run_online_transaction in a dedicated
process (never the threaded D-Bus service) because OSTree deployment
modifies the mount namespace.
"""

import argparse
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

import armada_os

from . import system_manager
from .finalization_receipt import _durable_ancestors
from .system_transaction import (
    SystemTransactionError, run_online_transaction, run_rollback_transaction)

CONFIG_PATH = Path("/etc/armada/updates/channels.json")
COMPATIBLE_PATH = Path("/sys/firmware/devicetree/base/compatible")
SYSROOT_PATH = Path("/sysroot")
STATE_DIR = Path("/var/lib/armada/updates/system")

SUPPORTED_MODELS = {"TB322FC", "TB323FU"}
TERMINAL_STATES = {"awaiting_reboot", "up_to_date"}


class PreflightError(RuntimeError):
    """Local precondition failure before any transaction side effects."""


def _preflight_failure(error):
    return {
        "schemaVersion": 1,
        "component": "system",
        "state": "failed",
        "phase": "preflight",
        "model": None,
        "channel": None,
        "currentVersion": None,
        "targetVersion": None,
        "currentCommit": None,
        "pendingDeployment": None,
        "requiresReboot": False,
        "bootPolicy": "dual-boot-no-slot-control",
        "bootWriteState": "not_started",
        "slotControlState": "not_issued",
        "error": str(error) or type(error).__name__,
    }


def _prepare_state_root():
    if not STATE_DIR.is_absolute():
        raise PreflightError(f"Non-canonical state path: {STATE_DIR}")
    root = STATE_DIR.resolve(strict=False)
    if root != STATE_DIR:
        raise PreflightError(f"Non-canonical state path: {STATE_DIR}")
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PreflightError(f"State path is not a directory: {root}")
    if info.st_uid != 0:
        raise PreflightError(f"State directory is not root owned: {root}")
    if info.st_mode & 0o022:
        raise PreflightError(
            f"State directory is group/world writable: {root}")
    # The validated directory must be durable before any transaction creates
    # boot backups or writes media.  The whole chain is re-flushed on every
    # call, so a retry after a failed flush makes a previously created path
    # durable instead of skipping straight to the boot writes.  A failure here
    # propagates as a preflight error and stops the job.
    _durable_ancestors(root)
    return root


def _preflight(channel, rollback=False):
    snapshot = armada_os.snapshot()
    model = system_manager._model(COMPATIBLE_PATH)
    if model not in SUPPORTED_MODELS:
        raise PreflightError(
            f"Physical model {model} is not supported by the system apply worker")
    system_manager._base(snapshot, model, "rollback" if rollback else "apply")
    if not rollback:
        local = system_manager._channel(snapshot)
        if local != channel:
            raise PreflightError(
                f"Local channel {local} differs from requested channel {channel}")
    if snapshot.get("repository") not in armada_os.REPOSITORIES:
        raise PreflightError(
            "The current deployment repository is not recognized for updates")
    if snapshot.get("staged"):
        raise PreflightError("A staged deployment already exists")
    if snapshot.get("rollback_queued"):
        raise PreflightError("A rollback is queued; no update can be applied")
    if rollback:
        return None
    return system_manager._configuration(CONFIG_PATH, channel)


def execute(channel, job_id, *, rollback_checksum=None):
    """Apply an update or queue a validated rollback under *job_id*."""
    try:
        if os.geteuid() != 0:
            raise PreflightError("The system apply worker must run as root")
        if rollback_checksum is None and channel not in ("stable", "beta", "testing"):
            raise PreflightError(f"Unknown channel: {channel!r}")
        if not isinstance(job_id, str):
            raise PreflightError(f"Job id is not a canonical UUID: {job_id!r}")
        parsed_job = uuid.UUID(job_id)
        if str(parsed_job) != job_id or parsed_job.int == 0:
            raise PreflightError(f"Job id is not a canonical UUID: {job_id!r}")
        if (rollback_checksum is not None
                and (not isinstance(rollback_checksum, str)
                     or not system_manager.COMMIT_PATTERN.fullmatch(rollback_checksum))):
            raise PreflightError("Rollback checksum is not a canonical lowercase OSTree checksum")
        selected = _preflight(channel, rollback=rollback_checksum is not None)
        state_root = _prepare_state_root()
        work_dir = state_root / job_id
        if work_dir.exists() or work_dir.is_symlink():
            raise PreflightError(
                f"Job work directory already exists: {work_dir}")
        resolved_root = SYSROOT_PATH.resolve(strict=True)
        if str(resolved_root) != str(SYSROOT_PATH):
            raise PreflightError(f"Non-canonical sysroot path: {SYSROOT_PATH}")
        if rollback_checksum is not None:
            return run_rollback_transaction(
                expected_checksum=rollback_checksum,
                sysroot_path=resolved_root,
                work_dir=work_dir,
            )
        return run_online_transaction(
            channel_url=selected["url"],
            signature_url=selected["signatureUrl"],
            expected_channel=channel,
            sysroot_path=resolved_root,
            work_dir=work_dir,
            updater_version=system_manager.UPDATER_VERSION,
        )
    except SystemTransactionError as error:
        if isinstance(error.result, dict):
            return error.result
        raise
    except PreflightError as error:
        return _preflight_failure(error)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        return _preflight_failure(error)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--channel", choices=("stable", "beta", "testing"))
    source.add_argument("--rollback-checksum")
    parser.add_argument("--job-id", required=True)
    arguments = parser.parse_args()
    result = execute(arguments.channel, arguments.job_id,
                     rollback_checksum=arguments.rollback_checksum)
    print(json.dumps(result, sort_keys=True))
    state = result.get("state") if isinstance(result, dict) else None
    return 0 if state in TERMINAL_STATES else 1


if __name__ == "__main__":
    raise SystemExit(main())
