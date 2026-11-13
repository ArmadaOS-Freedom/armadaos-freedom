import copy
import json
import re
import subprocess

from updates.client import UpdateClient

UPDATE = "/usr/libexec/armada/armada-update"
SELECT = "/usr/libexec/armada/armada-select-branch"
CHANNELS = ("stable", "beta", "preview")
FREEDOM = "ghcr.io/armadaos-freedom/armadaos-freedom"
REPOSITORIES = (FREEDOM, "ghcr.io/mydanyi/armadaos-freedom")
# Transport prefixes bootc may report on a staged image reference.
TRANSPORTS = (
    "ostree-image-signed:docker://",
    "ostree-unverified-image:docker://",
    "ostree-unverified-registry:docker://",
    "ostree-unverified-registry:",
    "docker://",
)


def command(arguments, timeout=15):
    try:
        return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError((error.stderr or "").strip() or str(error)) from error


def ref_repository_tag(image):
    """Return (repository, tag) for a supported image ref, else (None, None).

    Raw references carry no transport prefix, which is how bootc reports staged
    images. An unknown scheme, a missing tag, or a malformed repository never
    matches, so a prefix lookalike cannot be mistaken for the Freedom channel.
    """
    if not image or any(character.isspace() for character in image):
        return None, None
    for prefix in TRANSPORTS:
        if image.startswith(prefix):
            image = image[len(prefix):]
            break
    else:
        if "://" in image:
            return None, None
    image = image.split("@", 1)[0]
    repository, separator, tag = image.rpartition(":")
    if not separator or "/" in tag or "/" not in repository or ":" in repository:
        return None, None
    return repository, tag


def staged_matches_channel(staged, channel):
    if not staged:
        return False
    repository, tag = ref_repository_tag(staged.get("image", ""))
    if repository != FREEDOM:
        return False
    return tag == ("testing" if channel == "preview" else channel)


def deployment(value):
    if not value:
        return None
    image = value.get("image") or {}
    ostree = value.get("ostree") or {}
    return {"version": image.get("version") or image.get("imageDigest") or ostree.get("checksum") or "Unknown version",
            "image": (image.get("image") or {}).get("image", ""),
            "checksum": ostree.get("checksum"), "serial": ostree.get("deploySerial"),
            "stateroot": ostree.get("stateroot"), "incompatible": bool(value.get("incompatible"))}


def _rollback_binding(target):
    if not isinstance(target, dict) or target.get("incompatible") is not False:
        return None
    checksum = target.get("checksum")
    serial = target.get("serial")
    stateroot = target.get("stateroot")
    if (not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or type(serial) is not int or not 0 <= serial <= 0xffffffff
            or not isinstance(stateroot, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", stateroot)):
        return None
    return f"{stateroot}/{checksum}.{serial}"


def snapshot():
    raw = json.loads(command(["/usr/bin/bootc", "status", "--format", "json"]))
    status = raw.get("status") or {}
    booted = deployment(status.get("booted"))
    if not booted or not booted["checksum"]:
        raise RuntimeError("No booted OSTree deployment is available")
    try:
        target = command(["/bin/bash", "-c", 'source /usr/lib/armada/update-lib; armada_update_target "$(armada_booted_ref)"'], timeout=60).strip().split()
    except (subprocess.SubprocessError, RuntimeError):
        target = []
    repository, channel = target if len(target) == 2 else ("", "")
    if channel == "testing":
        channel = "preview"
    state = {"booted": booted, "rollback": deployment(status.get("rollback")),
             "staged": deployment(status.get("staged")), "rollback_queued": bool(status.get("rollbackQueued")),
             "channel": channel, "repository": repository,
             "channels": list(CHANNELS) if repository in REPOSITORIES and channel in CHANNELS else []}
    return state


def _service_status(client=None):
    if client is None:
        client = UpdateClient()
    try:
        record = client.snapshot()["components"].get("system")
        if (not isinstance(record, dict) or type(record.get("busy")) is not bool
                or not isinstance(record.get("result"), dict)
                or record["result"].get("component") != "system"):
            raise RuntimeError("update service returned invalid system status")
        if record["busy"]:
            return copy.deepcopy(record["result"]), True
        result = client.run("system", "status", timeout=120)
        if not isinstance(result, dict) or result.get("component") != "system":
            raise RuntimeError("update service returned invalid system result")
        return result, False
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("cannot read update service status: %s" % exc) from exc


def status():
    state = snapshot()
    update, busy = _service_status()
    result_state = update.get("state")
    idle = not busy
    state["update"] = update
    state["update_busy"] = busy
    state["update_allowed"] = bool(
        idle and not state["rollback_queued"]
        and state["repository"] in REPOSITORIES and state["channel"] in CHANNELS
        and result_state in ("installed", "up_to_date", "update_available"))
    trial = update.get("trial")
    target = None
    if result_state in ("awaiting_reboot", "awaiting_confirmation"):
        target = update.get("targetVersion")
        if not target and isinstance(trial, dict):
            target = trial.get("newDeployment")
        if not target:
            target = update.get("newDeployment")
    state["update_target"] = target
    binding = _rollback_binding(state["rollback"])
    state["rollback_allowed"] = bool(
        idle and not state["staged"] and not state["rollback_queued"]
        and state["repository"] in REPOSITORIES
        and result_state in ("installed", "up_to_date", "update_available")
        and update.get("model") in ("TB322FC", "TB323FU")
        and isinstance(trial, dict) and trial.get("recordState") == "confirmed"
        and trial.get("runningDeployment") == "new"
        and update.get("lastUpdateState") == "confirmed"
        and binding is not None and trial.get("oldDeployment") == binding)
    state["reboot_ready"] = bool(
        idle and result_state == "awaiting_reboot" and update.get("requiresReboot") is True)
    message = ""
    if busy:
        phase = update.get("phase")
        message = "Update operation is running" + (f" ({phase})." if phase else ".")
    elif result_state == "awaiting_reboot":
        if update.get("requiresReboot") is True:
            message = "Update installed. Restart to apply."
        else:
            message = "Update status is incomplete."
    elif result_state == "awaiting_confirmation":
        if update.get("lastUpdateState") == "finalizing" or update.get("phase") == "finalizing":
            message = "Update is finalizing."
        elif isinstance(trial, dict):
            message = "Update trial is awaiting confirmation."
        else:
            message = "Update is awaiting confirmation."
    elif result_state in ("pending_conflict", "failed", "check_failed", "not_configured",
                          "unsupported", "uncertain"):
        message = {
            "pending_conflict": "Another update operation is pending.",
            "failed": "Update failed.",
            "check_failed": "Update check failed.",
            "not_configured": "Updates are not configured.",
            "unsupported": "This deployment does not support updates.",
            "uncertain": "Update status is uncertain.",
        }[result_state]
        if update.get("error"):
            message += " " + str(update["error"])
    elif state["rollback_queued"]:
        message = "Unverified legacy rollback is queued."
    elif update.get("lastUpdateState") == "confirmed":
        message = "Update confirmed."
    elif update.get("lastUpdateState") == "rolled_back":
        message = "Update rolled back."
    state["update_message"] = message
    return state


def select_channel(channel):
    if channel not in CHANNELS:
        raise ValueError("Unknown update channel")
    if channel not in snapshot()["channels"]:
        raise RuntimeError("Channel selection is unavailable for this image")
    result = UpdateClient().run(
        "system", "select_channel", "testing" if channel == "preview" else channel, timeout=120)
    if (not isinstance(result, dict) or result.get("component") != "system"
            or result.get("state") not in (
                "installed", "up_to_date", "awaiting_reboot", "awaiting_confirmation")):
        error = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError("Update channel selection failed" + (f": {error}" if error else ""))
    return {"ok": True}


def restart():
    if not status()["reboot_ready"]:
        raise RuntimeError("There is no pending OS change to restart into")
    command(["/usr/bin/systemctl", "reboot"])
    return {"ok": True}


def execute(operation, checksum=None):
    if operation == "update":
        state = status()
        update = state["update"]
        if state["update_busy"]:
            raise RuntimeError("An update service operation is already running")
        if update.get("state") == "awaiting_reboot" and update.get("requiresReboot") is True:
            return {"message": "Update installed. Restart to apply."}
        if not state["update_allowed"]:
            raise RuntimeError(state["update_message"] or "No supported OS update is currently available")
        subprocess.run([UPDATE], check=True)
        current = status()
        update = current["update"]
        if (not current["update_busy"] and update.get("state") == "awaiting_reboot"
                and update.get("requiresReboot") is True):
            return {"message": "Update installed. Restart to apply."}
        if not current["update_busy"] and update.get("state") in ("installed", "up_to_date"):
            return {"message": "No new image was needed."}
        details = ": ".join(str(value) for value in (update.get("phase"), update.get("error")) if value)
        raise RuntimeError("Update did not reach a ready state" + (f": {details}" if details else ""))
    if operation != "rollback":
        raise ValueError("Unknown OS operation")
    state = status()
    if state["update_busy"]:
        raise RuntimeError("An update service operation is already running")
    target = state["rollback"]
    if not target or not target["checksum"] or target["incompatible"]:
        raise RuntimeError("No compatible previous deployment is available")
    if not checksum or checksum != target["checksum"]:
        raise RuntimeError("The rollback target changed. Refresh and confirm rollback again.")
    binding = _rollback_binding(target)
    update = state["update"]
    if update.get("state") == "awaiting_reboot" and update.get("requiresReboot") is True:
        trial = update.get("trial")
        selected = trial.get("newDeployment") if isinstance(trial, dict) else None
        if not selected:
            selected = update.get("newDeployment")
        if binding is not None and selected == binding:
            return {"message": "Rollback ready. Restart to apply."}
        raise RuntimeError("Another OS change is already awaiting restart")
    if not state["rollback_allowed"]:
        raise RuntimeError("The previous bound deployment is unavailable")
    print("Selecting the previous OS deployment", flush=True)
    result = UpdateClient().run("system", "rollback", checksum=checksum)
    if (isinstance(result, dict) and result.get("component") == "system"
            and result.get("state") == "awaiting_reboot"
            and result.get("requiresReboot") is True
            and result.get("bootWriteState") == "written"
            and result.get("bootPolicy") == "dual-boot-no-slot-control"
            and result.get("slotControlState") == "not_issued"
            and result.get("newDeployment") == binding):
        return {"message": "Rollback ready. Restart to apply."}
    raw_details = (result.get("phase"), result.get("error")) if isinstance(result, dict) else ()
    details = ": ".join(str(value)[:300] for value in raw_details if value)
    raise RuntimeError("Rollback did not reach a verified ready state"
                       + (f": {details}" if details else ""))
