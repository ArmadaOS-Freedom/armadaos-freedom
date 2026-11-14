"""Freedom OS update status, signed update checks, and apply operations."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

import armada_os

from .boot_context import BootContextError, read_boot_context
from .trial_record import BOOT_POLICY
from .trial_status import read_trial_status
from .channel_source import fetch_update
from . import channel_selection, offline_completion

UPDATER_VERSION = "0.8.0"
MAX_COMPATIBLE_BYTES = 4096
MAX_CONFIG_BYTES = 64 * 1024
COMMIT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
CHANNEL_MAP = {"stable": "stable", "beta": "beta",
               "preview": "testing", "testing": "testing"}
MODELS = {
    b"lenovo,elden": "TB322FC",
    b"lenovo,baldur": "TB323FU",
    b"lenovo,tb323fu": "TB323FU",
    b"lenovo,tb710fu": "TB710FU",
}

SYSTEM_APPLY_STATE_ROOT = Path("/var/lib/armada/updates/system")
SYSTEM_APPLY_ARGV = ["/usr/bin/python3", "-m", "updates.system_apply"]
OFFLINE_SESSION_ROOT = Path("/var/lib/armada-updates/offline")
# Receipt states from which an execute result may still be missing or already
# recorded; anything else cannot describe a written, unrebooted transaction.
_OFFLINE_RECEIPT_STATES = frozenset({
    "execute_queued", "executing", "dispatch_uncertain", "awaiting_reboot",
})
_APPLY_STATES = (
    "awaiting_reboot", "up_to_date", "failed", "uncertain", "recovery_required")


def _apply_uncertain(base, phase, error, work_dir):
    outcome = dict(base) if isinstance(base, dict) else {}
    outcome.update({
        "schemaVersion": 1,
        "component": "system",
        "state": "uncertain",
        "phase": phase,
        "requiresReboot": False,
        "bootPolicy": BOOT_POLICY,
        "bootWriteState": "uncertain",
        "slotControlState": "uncertain",
        "workDir": str(work_dir) if work_dir is not None else None,
        "error": str(error)[:1024] or "unknown worker failure",
    })
    return outcome


def _apply_not_started(error, result=None, operation="apply"):
    outcome = _failure(operation, "failed", error, result)
    outcome.update({
        "requiresReboot": False,
        "bootPolicy": BOOT_POLICY,
        "bootWriteState": "not_started",
        "slotControlState": "not_issued",
        "error": outcome["error"][:1024],
    })
    return outcome


def _apply_outcome(stdout, returncode, base, work_dir):
    def uncertain(reason):
        return _apply_uncertain(
            base, "worker_result", reason, work_dir)

    if not isinstance(stdout, str):
        return uncertain(
            "The system apply worker returned no usable result")
    try:
        if len(stdout.encode("utf-8", "replace")) > MAX_CONFIG_BYTES:
            raise ValueError("worker output exceeds the size limit")
        report = json.loads(
            stdout, object_pairs_hook=_no_duplicate_properties)
    except (TypeError, ValueError):
        return uncertain(
            "The system apply worker returned no usable result")
    if not isinstance(report, dict):
        return uncertain(
            "The system apply worker returned no usable result")
    if (type(report.get("schemaVersion")) is not int
            or report["schemaVersion"] != 1
            or report.get("component") != "system"
            or not isinstance(report.get("phase"), str)
            or not report["phase"]
            or type(report.get("requiresReboot")) is not bool
            or report.get("state") not in _APPLY_STATES):
        return uncertain(
            "The system apply worker returned an invalid result")
    state = report["state"]
    if returncode == 0 and state not in ("awaiting_reboot", "up_to_date"):
        return uncertain(
            "The system apply worker exited without a success state")
    if returncode != 0 and state not in ("failed", "uncertain", "recovery_required"):
        return uncertain(
            "The system apply worker failed without a failure state")
    # A report from a worker that predates the mirrored boot policy (or that
    # claims Android slot control was issued) is not compatible with this
    # manager.  Treat it as uncertain instead of merging its policy fields.
    # A genuine worker "uncertain" value is preserved for failure states,
    # never rewritten to definite evidence.
    if (report.get("bootPolicy") != BOOT_POLICY
            or report.get("slotControlState") not in ("not_issued", "uncertain")):
        return uncertain(
            "The system apply worker returned incompatible boot-policy evidence")
    # A success report must confirm that slot control was definitely not
    # issued; an absent or ambiguous value may come from an old worker whose
    # actual slot state is unknown.
    if (state in ("awaiting_reboot", "up_to_date")
            and report.get("slotControlState") != "not_issued"):
        return uncertain(
            "The system apply worker did not confirm definite slot control")
    if state == "awaiting_reboot":
        if (not report["requiresReboot"]
                or report.get("bootWriteState") != "written"
                or report.get("bootPolicy") != BOOT_POLICY):
            return uncertain(
                "The system apply worker reported inconsistent reboot evidence")
    elif report["requiresReboot"]:
        return uncertain(
            "The system apply worker reported a reboot requirement "
            "in state %s" % state)
    if "workDir" in report and report["workDir"] != str(work_dir):
        return uncertain(
            "The system apply worker reported an unexpected workDir")
    outcome = dict(base) if isinstance(base, dict) else {}
    outcome.update(report)
    return outcome


class NotConfiguredError(RuntimeError):
    pass


class UnsupportedError(RuntimeError):
    pass


def _read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        source = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{path} is not a regular file")
        if info.st_size > limit:
            raise RuntimeError(f"{path} exceeds the {limit}-byte limit")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{path} exceeds the {limit}-byte limit")
    return data


def _model(path):
    data = _read_regular(path, MAX_COMPATIBLE_BYTES)
    detected = {MODELS[token] for token in data.split(b"\0") if token in MODELS}
    if len(detected) != 1:
        detail = "ambiguous" if detected else "unknown"
        raise UnsupportedError(
            f"Unsupported device: {detail} firmware compatible identity"
        )
    return detected.pop()


def _channel(snapshot):
    value = snapshot.get("channel", "")
    if value == "":
        return "stable"
    if not isinstance(value, str) or value not in CHANNEL_MAP:
        raise RuntimeError(f"Malformed local update channel: {value!r}")
    return CHANNEL_MAP[value]


def _base(snapshot, model, phase):
    booted = snapshot.get("booted")
    if not isinstance(booted, dict):
        raise RuntimeError("The local snapshot has no booted deployment")
    version = booted.get("version")
    commit = booted.get("checksum")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("The booted deployment has no usable version")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise RuntimeError("The booted deployment has an invalid OSTree checksum")
    if booted.get("incompatible"):
        raise RuntimeError("The booted deployment is marked incompatible")
    return {
        "schemaVersion": 1,
        "component": "system",
        "state": "installed",
        "phase": phase,
        "model": model,
        "channel": _channel(snapshot),
        "bootPolicy": BOOT_POLICY,
        "currentVersion": version,
        "targetVersion": None,
        "currentCommit": commit,
        "pendingDeployment": snapshot.get("staged"),
        "requiresReboot": False,
    }


def _failure(phase, state, error, result=None):
    if result is None:
        result = {
            "schemaVersion": 1,
            "component": "system",
            "phase": phase,
            "model": None,
            "channel": None,
            "bootPolicy": BOOT_POLICY,
            "currentVersion": None,
            "targetVersion": None,
            "currentCommit": None,
            "pendingDeployment": None,
            "requiresReboot": False,
        }
    result["state"] = state
    result["error"] = str(error) or type(error).__name__
    return result


def _no_duplicate_properties(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def _valid_trial_receipt(trial, model):
    try:
        root = SYSTEM_APPLY_STATE_ROOT
        if not root.is_absolute() or root.resolve(strict=True) != root:
            return False
        root_info = os.lstat(root)
        if (not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != 0
                or root_info.st_mode & 0o022):
            return False
        raw_pointer = _read_regular(root / "current.json", MAX_CONFIG_BYTES)
        pointer = json.loads(
            raw_pointer, object_pairs_hook=_no_duplicate_properties)
        if (not isinstance(pointer, dict)
                or set(pointer) != {"schemaVersion", "jobId"}
                or type(pointer["schemaVersion"]) is not int
                or pointer["schemaVersion"] != 1
                or not isinstance(pointer["jobId"], str)):
            return False
        job_id = pointer["jobId"]
        parsed_job_id = uuid.UUID(job_id)
        if parsed_job_id.int == 0 or str(parsed_job_id) != job_id:
            return False
        work_dir = root / job_id
        if work_dir.parent != root or work_dir.resolve(strict=True) != work_dir:
            return False
        work_info = os.lstat(work_dir)
        if not stat.S_ISDIR(work_info.st_mode):
            return False
        raw_receipt = _read_regular(
            work_dir / "status.json", MAX_CONFIG_BYTES)
        receipt = json.loads(
            raw_receipt, object_pairs_hook=_no_duplicate_properties)
        if not isinstance(receipt, dict):
            return False
        if (type(receipt.get("schemaVersion")) is not int
                or receipt["schemaVersion"] != 1
                or receipt.get("component") != "system"
                or receipt.get("workDir") != str(work_dir)
                or receipt.get("transactionId") != trial.get("transactionId")
                or receipt.get("fromSlot") != trial.get("fromSlot")
                or receipt.get("targetSlot") != trial.get("targetSlot")
                or receipt.get("oldDeployment") != trial.get("oldDeployment")
                or receipt.get("newDeployment") != trial.get("newDeployment")
                or receipt.get("planSha256") != trial.get("planSha256")
                or receipt.get("model") != model):
            return False
        return (receipt.get("state") == "awaiting_reboot"
                and receipt.get("phase") == "awaiting_reboot"
                and receipt.get("requiresReboot") is True
                and receipt.get("bootPolicy") == BOOT_POLICY
                and receipt.get("bootWriteState") == "written"
                and receipt.get("slotControlState") == "not_issued")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return False


def _private_directory(path):
    info = os.lstat(path)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022):
        raise RuntimeError(f"{path} is not a private root-owned directory")


def _json_bytes(raw):
    return json.loads(raw, object_pairs_hook=_no_duplicate_properties)


def _json_document(path):
    return _json_bytes(_read_regular(path, MAX_CONFIG_BYTES))


def _valid_offline_completion(trial, model):
    """Match a durable offline execute result to the live trial journal.

    Only the private pointer filed under this journal's canonical transaction
    id is followed, so another queued session cannot displace an older pending
    result.  The session's own documents must then pass the shared completion
    validator against the approved plan and report a verified boot write with
    slot control never issued.  A missing, foreign, misowned or contradictory
    document simply fails the check.
    """
    try:
        if not isinstance(trial, dict):
            return False
        root = OFFLINE_SESSION_ROOT
        if not root.is_absolute() or root.resolve(strict=True) != root:
            return False
        _private_directory(root)
        pointer = offline_completion.completion_pointer(
            root, trial.get("transactionId"))
        if pointer is None:
            return False
        _private_directory(pointer.parent)
        session_id = offline_completion.pointer_session(
            _json_document(pointer), trial.get("transactionId"))
        if session_id is None:
            return False
        session = root / session_id
        if session.parent != root or session.resolve(strict=True) != session:
            return False
        _private_directory(session)
        receipt = _json_document(session / "receipt.json")
        if (not isinstance(receipt, dict)
                or type(receipt.get("schemaVersion")) is not int
                or receipt["schemaVersion"] != 1
                or receipt.get("sessionId") != session_id
                or receipt.get("state") not in _OFFLINE_RECEIPT_STATES
                or receipt.get("model") != model):
            return False
        transaction = session / "transaction"
        if transaction.parent != session or transaction.resolve(strict=True) != transaction:
            return False
        _private_directory(transaction)
        prepared = _json_document(transaction / "prepared.json")
        # Read the approved plan once: the digest must be computed over the
        # exact bytes that were parsed, never over a second read that could
        # observe a replaced file.
        plan_raw = _read_regular(transaction / "plan.json", MAX_CONFIG_BYTES)
        plan = _json_bytes(plan_raw)
        status = _json_document(transaction / "status.json")
        report = _json_document(transaction / "write-report.json")
        ack = _json_document(session / "ack.json")
        identity = offline_completion.execute_completion(
            prepared, plan, hashlib.sha256(plan_raw).hexdigest(),
            status, report, ack, model)
        if identity is None:
            return False
        return (
            trial.get("transactionId") == identity["transactionId"]
            and trial.get("fromSlot") == identity["fromSlot"]
            and trial.get("targetSlot") == identity["targetSlot"]
            and trial.get("oldDeployment") == identity["oldDeployment"]
            and trial.get("newDeployment") == identity["newDeployment"]
            and trial.get("planSha256") == identity["planSha256"]
            and trial.get("recordState") == "prepared"
            and trial.get("runningDeployment") == "old")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return False


def _configuration(path, channel):
    try:
        raw = _read_regular(path, MAX_CONFIG_BYTES)
    except FileNotFoundError as error:
        raise NotConfiguredError(
            f"Update channel configuration is missing at {path}; "
            "the publisher must install an approved channels.json"
        ) from error
    config = json.loads(raw, object_pairs_hook=_no_duplicate_properties)
    if not isinstance(config, dict) or set(config) != {"schemaVersion", "channels"}:
        raise ValueError("Update configuration must contain schemaVersion and channels")
    if (type(config["schemaVersion"]) is not int
            or config["schemaVersion"] != 1):
        raise ValueError("Unsupported update configuration schemaVersion")
    channels = config["channels"]
    if not isinstance(channels, dict) or not channels:
        raise ValueError("Update configuration must define at least one channel")
    if not set(channels).issubset({"stable", "beta", "testing"}):
        raise ValueError("Update configuration contains an unknown channel")
    if channel not in channels:
        raise NotConfiguredError(
            f"The selected {channel} channel is not configured in {path}; "
            "configure that channel explicitly rather than using another source"
        )
    selected = channels[channel]
    if not isinstance(selected, dict) or set(selected) != {"url", "signatureUrl"}:
        raise ValueError(f"The {channel} channel must contain url and signatureUrl")
    if not all(isinstance(selected[key], str) for key in selected):
        raise ValueError(f"The {channel} channel URLs must be strings")
    return selected


class SystemManager:
    def __init__(
        self,
        config_path=Path("/etc/armada/updates/channels.json"),
        compatible_path=Path("/sys/firmware/devicetree/base/compatible"),
        witness_path=Path(
            "/sys/firmware/devicetree/base/chosen/armada,boot-witness"),
        cmdline_path=Path("/proc/cmdline"),
    ):
        self.config_path = config_path
        self.compatible_path = compatible_path
        self.witness_path = witness_path
        self.cmdline_path = cmdline_path
        self._active_work = None

    def _boot_context(self, snapshot):
        try:
            return read_boot_context(
                snapshot.get("booted"), self.witness_path, self.cmdline_path)
        except BootContextError as error:
            return {
                "state": "unavailable",
                "actualSlot": None,
                "inactiveSlot": None,
                "deployment": None,
                "error": str(error),
            }

    def _pending_trial(self, result):
        trial = result["trial"]
        if trial.get("state") != "available":
            return False
        record_state = trial.get("recordState")
        if record_state == "confirmed":
            result["requiresReboot"] = False
            if trial.get("runningDeployment") == "new":
                finalization = trial.get("finalization")
                if (
                    isinstance(finalization, dict)
                    and finalization.get("state") == "complete"
                ):
                    result["lastUpdateState"] = "confirmed"
                    return False
                result["state"] = "awaiting_confirmation"
                result["lastUpdateState"] = "finalizing"
                error = (
                    finalization.get("error")
                    if isinstance(finalization, dict)
                    else None
                )
                result["error"] = (
                    error
                    if isinstance(error, str) and error
                    else "Update finalization is not complete"
                )
                return True
            result["state"] = "pending_conflict"
            result["error"] = (
                "Confirmed trial record does not match the running deployment"
            )
            return True
        if record_state == "rolled_back":
            result["requiresReboot"] = False
            if trial.get("runningDeployment") == "old":
                result["lastUpdateState"] = "rolled_back"
                return False
            result["state"] = "pending_conflict"
            result["error"] = (
                "Rolled-back trial record does not match the running deployment"
            )
            return True
        if record_state not in {"prepared", "trial"}:
            return False
        result["requiresReboot"] = False
        if (trial.get("runningDeployment") == "new"):
            result["state"] = "awaiting_confirmation"
        elif (trial.get("recordState") == "prepared"
                and trial.get("runningDeployment") == "old"
                and (_valid_trial_receipt(trial, result["model"])
                     or _valid_offline_completion(trial, result["model"]))):
            result.update({
                "state": "awaiting_reboot",
                "requiresReboot": True,
                "pendingFromSlot": trial["fromSlot"],
                "pendingTargetSlot": trial["targetSlot"],
                "pendingTransactionId": trial["transactionId"],
            })
        else:
            result["state"] = "pending_conflict"
            result["error"] = (
                "Pending trial/activation is incomplete or unverifiable")
        return True

    def status(self):
        result = None
        try:
            snapshot = armada_os.snapshot()
            result = _base(snapshot, _model(self.compatible_path), "status")
            result["bootEvidence"] = self._boot_context(snapshot)
            result["trial"] = read_trial_status(result["bootEvidence"])
            self._pending_trial(result)
            return result
        except UnsupportedError as error:
            return _failure("status", "unsupported", error, result)
        except (RuntimeError, ValueError, OSError,
                subprocess.SubprocessError) as error:
            return _failure("status", "failed", error, result)

    def select_channel(self, channel):
        result = None
        try:
            if channel not in ("stable", "beta", "testing"):
                raise ValueError("unsupported update channel")
            if os.geteuid() != 0:
                raise PermissionError("channel selection requires root")
            snapshot = armada_os.snapshot()
            result = _base(
                snapshot, _model(self.compatible_path), "select_channel")
            if snapshot.get("repository") not in armada_os.REPOSITORIES:
                raise RuntimeError(
                    "The current deployment repository is not recognized for updates"
                )
            channel_selection.set_channel(channel)
            result = self.status()
            result["phase"] = "select_channel"
            return result
        except UnsupportedError as error:
            return _failure("select_channel", "unsupported", error, result)
        except (RuntimeError, ValueError, OSError,
                subprocess.SubprocessError) as error:
            return _failure("select_channel", "failed", error, result)

    def check(self):
        result = None
        try:
            snapshot = armada_os.snapshot()
            result = _base(snapshot, _model(self.compatible_path), "check")
            result["bootEvidence"] = self._boot_context(snapshot)
            result["trial"] = read_trial_status(result["bootEvidence"])
            if self._pending_trial(result):
                return result
            if snapshot.get("repository") not in armada_os.REPOSITORIES:
                raise RuntimeError(
                    "The current deployment repository is not recognized for updates"
                )
            selected = _configuration(self.config_path, result["channel"])
            update = fetch_update(
                selected["url"],
                selected["signatureUrl"],
                expected_channel=result["channel"],
                model=result["model"],
                updater_version=UPDATER_VERSION,
            )
            channel = update["channel"]
            for required in ("version", "ostreeCommit", "releaseId", "ociDigest"):
                if required not in channel:
                    raise RuntimeError(
                        f"The catalog entry is missing the {required} field"
                    )
            target_commit = channel["ostreeCommit"]
            result.update({
                "targetVersion": channel["version"],
                "targetRelease": channel["releaseId"],
                "targetDigest": channel["ociDigest"],
                "targetCommit": channel["ostreeCommit"],
            })
            pending = snapshot.get("staged")
            if snapshot.get("rollback_queued"):
                result["state"] = "pending_conflict"
                result["error"] = "A rollback is queued; no update can be applied"
            elif pending and pending.get("incompatible"):
                result["state"] = "pending_conflict"
                result["error"] = "The existing pending deployment is incompatible"
            elif pending and pending.get("checksum") != target_commit:
                result["state"] = "pending_conflict"
                result["error"] = (
                    "An existing pending deployment differs from the validated target"
                )
            elif target_commit == result["currentCommit"]:
                result["state"] = "up_to_date"
            elif pending:
                result["state"] = "staged"
            else:
                result["state"] = "update_available"
            return result
        except NotConfiguredError as error:
            return _failure("check", "not_configured", error, result)
        except UnsupportedError as error:
            return _failure("check", "unsupported", error, result)
        except (RuntimeError, ValueError, OSError,
                subprocess.SubprocessError) as error:
            return _failure("check", "check_failed", error, result)

    def apply(self):
        return self._execute_action("apply")

    def rollback(self, checksum):
        return self._execute_action("rollback", checksum)

    def _execute_action(self, operation, checksum=None):
        result = None
        job_id = None
        work_dir = None
        try:
            if os.geteuid() != 0:
                raise PermissionError(
                    "The system apply operation requires root")
            if (operation == "rollback"
                    and (not isinstance(checksum, str)
                         or not COMMIT_PATTERN.fullmatch(checksum))):
                raise ValueError(
                    "Rollback checksum is not a canonical lowercase OSTree checksum")
            snapshot = armada_os.snapshot()
            result = _base(snapshot, _model(self.compatible_path), operation)
            channel = result["channel"]
            job_id = str(uuid.uuid4())
            work_dir = SYSTEM_APPLY_STATE_ROOT / job_id
            self._active_work = work_dir
            if operation == "rollback":
                argv = SYSTEM_APPLY_ARGV + [
                    "--rollback-checksum", checksum, "--job-id", job_id]
            else:
                argv = SYSTEM_APPLY_ARGV + [
                    "--channel", channel, "--job-id", job_id]
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
            except OSError as error:
                return _apply_not_started(error, result, operation)
            except subprocess.SubprocessError as error:
                return _apply_uncertain(result, "worker_spawn", error, work_dir)
            return _apply_outcome(
                completed.stdout, completed.returncode, result, work_dir)
        except (RuntimeError, ValueError, OSError,
                subprocess.SubprocessError) as error:
            if job_id is None:
                return _apply_not_started(error, result, operation)
            return _apply_uncertain(result, operation, error, work_dir)
        finally:
            self._active_work = None

    def progress(self):
        work_dir = self._active_work
        if work_dir is None:
            return None
        try:
            raw = _read_regular(
                work_dir / "status.json", MAX_CONFIG_BYTES)
            report = json.loads(
                raw, object_pairs_hook=_no_duplicate_properties)
        except (OSError, ValueError, RuntimeError):
            return None
        if (not isinstance(report, dict)
                or type(report.get("schemaVersion")) is not int
                or report["schemaVersion"] != 1
                or report.get("component") != "system"
                or report.get("workDir") != str(work_dir)
                or not isinstance(report.get("phase"), str)
                or not report["phase"]):
            return None
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("status", "check", "apply"))
    arguments = parser.parse_args()
    manager = SystemManager()
    result = getattr(manager, arguments.operation)()
    print(json.dumps(result, sort_keys=True))
    return int(result["state"] in {
        "failed", "check_failed", "not_configured", "unsupported",
        "uncertain", "recovery_required"
    })


if __name__ == "__main__":
    raise SystemExit(main())
