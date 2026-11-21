"""Stage and switch signed ARMADA_BOOT generations under the update lock."""

import base64
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import struct
from pathlib import Path

from . import _io, boot_context, boot_disk_generation, boot_disk_selector, boot_payload
from . import deployment_options, deployment_store, signature

_COMPLETED_NAME = "finished-switch.json"
_PENDING_NAME = "pending-switch.json"
_PENDING_SCHEMA = 1
_RECEIPT_KEYS = {'transactionId', 'planSha256', 'fromSlot', 'targetSlot'}
_MAX_PENDING_BYTES = 4096
BOOT_RECOVERY_VERSION_PATH = Path("/sys/firmware/devicetree/base/chosen/armada,boot-recovery-version")
RECOVERY_SERVICE_PATH = Path("/usr/lib/systemd/system/armada-linux-recovery.service")
RECOVERY_WANTS_PATH = Path("/etc/systemd/system/sysinit.target.wants/armada-linux-recovery.service")
_DEPLOYMENT_RE = re.compile(r"default/([0-9a-f]{64})\.(0|[1-9][0-9]{0,9})")
_GEN_DIR_RE = re.compile(r"(?:seed|gen-[0-9a-f]{32})")
_UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class BootDiskSwitchError(RuntimeError):
    """An installed disk boot generation could not be verified or switched."""


class BootDiskSwitchUncertainError(BootDiskSwitchError):
    """Selector state is uncertain: the switch may or may not have taken effect."""


def _require_binding(binding, label):
    if not isinstance(binding, str) or not _DEPLOYMENT_RE.fullmatch(binding):
        raise BootDiskSwitchError(f"{label} is not a canonical deployment binding")


def _resolve_boot(root):
    root = Path(root)
    canonical_root = Path("/sysroot")
    if root == canonical_root:
        candidates = [Path("/boot"), root / "boot"]
    else:
        candidates = [root / "boot"]

    source = _io.read_bounded(Path("/proc/cmdline"), 64 * 1024).decode("ascii")
    matches = [token[10:] for token in source.split() if token.startswith("boot=UUID=")]
    if len(matches) != 1 or not _UUID_RE.fullmatch(matches[0]):
        raise BootDiskSwitchError("boot evidence has no unique filesystem UUID")
    device = (Path("/dev/disk/by-uuid") / matches[0]).stat()
    if not stat.S_ISBLK(device.st_mode):
        raise BootDiskSwitchError("boot evidence does not name a block device")

    for candidate in candidates:
        try:
            lst = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISDIR(lst.st_mode):
            continue
        if not candidate.is_mount():
            continue
        try:
            st = candidate.stat()
        except OSError:
            continue
        if st.st_dev != device.st_rdev:
            continue
        armada = candidate / "armada"
        try:
            alst = armada.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(alst.st_mode) or not stat.S_ISDIR(alst.st_mode):
            continue
        return candidate

    raise BootDiskSwitchError("ARMADA_BOOT mount does not match boot evidence")


# ext4 UAPI _IOW('f', 43, __u32); zero flags = normal checkpoint
_EXT4_IOC_CHECKPOINT = 0x4004662B


def _checkpoint_boot(boot, *, dry_run=False):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(boot, flags)
    try:
        fcntl.ioctl(fd, _EXT4_IOC_CHECKPOINT, struct.pack('=I', 4 if dry_run else 0))
    finally:
        os.close(fd)


def _verified_generation(boot, name, model, deployment, trusted_key):
    if not isinstance(name, str) or not _GEN_DIR_RE.fullmatch(name):
        raise BootDiskSwitchError("invalid generation name")
    generation = boot / "armada" / name
    boot_disk_generation.verify_generation(generation, model, trusted_key=trusted_key)
    if boot_disk_generation.decode_binding(generation / "binding.bin") != deployment:
        raise BootDiskSwitchError("boot generation does not match requested deployment")


def _active(root, initial, trusted_key):
    if initial.get("bootSource") != "armada_boot":
        raise BootDiskSwitchError("running system did not boot from ARMADA_BOOT")
    boot = _resolve_boot(root)
    _, expected = _read_boot_active(boot)
    _verified_generation(boot, expected, initial["model"], initial["deployment"], trusted_key)
    return boot, expected


def _validate_receipt(receipt):
    if not isinstance(receipt, dict):
        raise BootDiskSwitchError("receipt is not a dictionary")
    if set(receipt) != _RECEIPT_KEYS:
        raise BootDiskSwitchError(
            f"receipt keys mismatch: {sorted(set(receipt) ^ _RECEIPT_KEYS)}")
    tid = receipt["transactionId"]
    if not isinstance(tid, str) or not _UUID_RE.fullmatch(tid) or tid == "00000000-0000-0000-0000-000000000000":
        raise BootDiskSwitchError("receipt.transactionId not canonical UUID")
    sha = receipt["planSha256"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise BootDiskSwitchError("receipt.planSha256 not lowercase hex digest")
    fslot, tslot = receipt["fromSlot"], receipt["targetSlot"]
    if fslot is None and tslot is None:
        pass
    elif isinstance(fslot, str) and isinstance(tslot, str) and {fslot, tslot} == {"a", "b"}:
        pass
    else:
        raise BootDiskSwitchError("receipt fromSlot/targetSlot malformed")


def _read_pending(boot, *, completed=False):
    path = boot / "armada" / (_COMPLETED_NAME if completed else _PENDING_NAME)
    try:
        lst = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot stat pending record: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        raise BootDiskSwitchError("pending record is not a regular file")
    raw = _io.read_bounded(path, _MAX_PENDING_BYTES)
    try:
        doc = _io.strict_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BootDiskSwitchError(f"pending record unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise BootDiskSwitchError("pending record is not a JSON object")
    required = {"schemaVersion", "expected", "target", "currentDeployment",
                "newDeployment", "model"}
    missing = required - set(doc)
    if missing or not set(doc) <= required | {"receipt"}:
        raise BootDiskSwitchError(f"pending record missing fields: {sorted(missing)}")
    if doc["schemaVersion"] != _PENDING_SCHEMA:
        raise BootDiskSwitchError("pending record schema version mismatch")
    for key in ("expected", "target"):
        if not isinstance(doc[key], str) or _GEN_DIR_RE.fullmatch(doc[key]) is None:
            raise BootDiskSwitchError(f"pending record {key} not canonical")
    if doc["expected"] == doc["target"]:
        raise BootDiskSwitchError("pending record expected == target")
    _require_binding(doc["currentDeployment"], "pending.currentDeployment")
    _require_binding(doc["newDeployment"], "pending.newDeployment")
    if not isinstance(doc["model"], str) or not doc["model"]:
        raise BootDiskSwitchError("pending record model invalid")
    if "receipt" in doc:
        _validate_receipt(doc["receipt"])
    return doc


def _require_recovery_support():
    """Require old launcher/system supports recovery rollback before switching."""
    try:
        raw = boot_context._read_regular(BOOT_RECOVERY_VERSION_PATH, 4)
    except (OSError, ValueError) as exc:
        raise BootDiskSwitchError(
            "old launcher lacks boot-recovery-version device-tree capability") from exc
    if raw != b"\x00\x00\x00\x01":
        raise BootDiskSwitchError(
            "old launcher lacks boot-recovery-version device-tree capability")
    service = RECOVERY_SERVICE_PATH
    try:
        st = service.lstat()
    except OSError as exc:
        raise BootDiskSwitchError(
            "old deployment lacks recovery service") from exc
    if not stat.S_ISREG(st.st_mode):
        raise BootDiskSwitchError("old deployment recovery service is not a regular file")
    wants = RECOVERY_WANTS_PATH
    try:
        target = wants.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise BootDiskSwitchError(
            "old deployment lacks recovery wants link") from exc
    if target != service:
        raise BootDiskSwitchError("old deployment recovery wants link is not the recovery service")


def _write_switch_durably(boot, name, doc):
    """Exclusive creation under boot/armada, temp + fsync + rename + fsync dir."""
    armada = boot / "armada"
    if not armada.is_dir() or armada.is_symlink():
        raise BootDiskSwitchError("boot/armada missing for pending write")
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_PENDING_BYTES:
        raise BootDiskSwitchError("pending record payload exceeds size bound")
    tmp_name = f".pending.{secrets.token_hex(12)}.tmp"
    tmp_path = armada / tmp_name
    final_path = armada / name
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            _io.write_all(fd, payload)
        except _io.ShortWriteError as exc:
            raise BootDiskSwitchError(f"short write pending record: {exc}") from exc
        os.fsync(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        if name == _PENDING_NAME:
            os.link(tmp_path, final_path)
        else:
            os.replace(tmp_path, final_path)
        _io.fsync_dir(armada)
    finally:
        tmp_path.unlink(missing_ok=True)


def _publish_recovery_pointer(boot, expected):
    armada = boot / "armada"
    if not armada.is_dir() or armada.is_symlink():
        raise BootDiskSwitchError("boot/armada missing for recovery pointer")
    link = armada / "recovery"
    try:
        lst = link.lstat()
    except FileNotFoundError:
        lst = None
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot inspect recovery pointer: {exc}") from exc
    if lst is not None and not stat.S_ISLNK(lst.st_mode):
        raise BootDiskSwitchError("boot/armada/recovery exists and is not a symlink")
    tmp_name = f".recovery.{secrets.token_hex(12)}.tmp"
    tmp_path = armada / tmp_name
    try:
        os.symlink(expected, tmp_path)
        os.replace(tmp_path, link)
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot publish recovery pointer: {exc}") from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        _io.fsync_dir(armada)
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot fsync recovery pointer: {exc}") from exc


def _remove_recovery_pointer(boot):
    armada = boot / "armada"
    link = armada / "recovery"
    try:
        lst = link.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot inspect recovery pointer: {exc}") from exc
    if not stat.S_ISLNK(lst.st_mode):
        raise BootDiskSwitchError("boot/armada/recovery is not a symlink; refusing removal")
    try:
        link.unlink()
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot remove recovery pointer: {exc}") from exc
    try:
        _io.fsync_dir(armada)
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot fsync recovery pointer removal: {exc}") from exc


def _clear_pending_durably(boot):
    armada = boot / "armada"
    final_path = armada / _PENDING_NAME
    try:
        final_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BootDiskSwitchError(f"cannot remove pending record: {exc}") from exc
    _io.fsync_dir(armada)


def _read_boot_active(boot):
    armada = boot / "armada"
    link = armada / "boot"
    try:
        lst = link.lstat()
    except OSError as exc:
        raise BootDiskSwitchError(f"boot/armada/boot missing: {exc}") from exc
    if not stat.S_ISLNK(lst.st_mode):
        raise BootDiskSwitchError("boot/armada/boot must be a symlink")
    target = os.readlink(os.fspath(link))
    if _GEN_DIR_RE.fullmatch(target) is None:
        raise BootDiskSwitchError(f"boot/armada/boot not canonical: {target!r}")
    return link, target


def prepare(root, initial, target, package, *, trusted_key, on_stage=None):
    boot, expected = _active(root, initial, trusted_key)
    options = deployment_options.read_deployment_options(
        sysroot_path=root,
        current_binding=initial["deployment"],
        target_binding=target,
    )
    target_path = Path(options["path"])
    tgt_commit = target.split("/", 1)[1].split(".", 1)[0]
    model = initial["model"]
    if tgt_commit != package["ostreeCommit"] or model not in package.get("armadaBootCapsules", {}):
        raise BootDiskSwitchError("target binding or model not in package contract")
    manifest = _io.strict_loads(
        _io.read_bounded(target_path / "usr/share/armada/update-boot/manifest.json", 1024**2)
    )
    verified = boot_payload.verify_manifest(
        target_path, model, manifest, require_runtime_marker=True
    )
    target_root, usr = boot_payload._resolve_root(target_path)
    kernel, _ = boot_payload._resolve_regular_file(
        target_root, usr, verified["kernel"]["path"]
    )
    initramfs, _ = boot_payload._resolve_regular_file(
        target_root, usr, verified["initramfs"]["path"]
    )
    ostree = [t[len("ostree="):] for t in options["options"].split() if t.startswith("ostree=")]
    if len(ostree) != 1:
        raise BootDiskSwitchError("target options must have exactly one ostree= token")
    legacy_ostree_pointer = ostree[0]
    capsule = {
        key: base64.b64decode(value, validate=True)
        for key, value in package["armadaBootCapsules"][model].items()
    }
    boot_disk_selector.ensure_root_binding(root, target)
    staged = boot_disk_generation.stage_generation(
        boot,
        model,
        kernel=kernel,
        initramfs=initramfs,
        binding=boot_disk_generation.encode_binding(legacy_ostree_pointer, target),
        capsule=capsule,
        trusted_key=trusted_key,
        on_stage=on_stage,
    )
    return {
        "expected": expected,
        "target": staged,
        "currentDeployment": initial["deployment"],
        "newDeployment": target,
        "model": model,
    }


def plan_rollback(root, initial, target, *, trusted_key):
    boot, expected = _active(root, initial, trusted_key)
    boot_disk_selector.ensure_root_binding(root, target)
    record = _read_pending(boot, completed=True)
    if record is not None:
        if (record["target"] != expected
                or record["newDeployment"] != initial["deployment"]
                or record["currentDeployment"] != target
                or record["model"] != initial["model"]):
            raise BootDiskSwitchError("completed switch record does not match rollback")
        retained = record["expected"]
        _verified_generation(boot, retained, initial["model"], target, trusted_key)
        return {
            "expected": expected, "target": retained,
            "currentDeployment": initial["deployment"], "newDeployment": target,
            "model": initial["model"],
        }
    candidates = []
    for entry in (boot / "armada").iterdir():
        if entry.name == expected or not _GEN_DIR_RE.fullmatch(entry.name) or entry.is_symlink():
            continue
        try:
            if boot_disk_generation.decode_binding(entry / "binding.bin") != target:
                continue
            _verified_generation(boot, entry.name, initial["model"], target, trusted_key)
        except (OSError, ValueError, BootDiskSwitchError, signature.SignatureError):
            continue
        candidates.append(entry.name)
    if len(candidates) != 1:
        raise BootDiskSwitchError("rollback does not match a unique verified boot generation")
    return {
        "expected": expected, "target": candidates[0],
        "currentDeployment": initial["deployment"], "newDeployment": target,
        "model": initial["model"],
    }


def activate(root, initial, plan, *, trusted_key, receipt=None):
    boot, expected = _active(root, initial, trusted_key)
    target = plan["target"]
    if (expected != plan["expected"] or expected == target or plan["model"] != initial["model"]
            or plan["currentDeployment"] != initial["deployment"]):
        raise BootDiskSwitchError("running ARMADA_BOOT generation changed after preparation")
    _verified_generation(boot, target, plan["model"], plan["newDeployment"], trusted_key)
    _require_recovery_support()
    boot_disk_selector.ensure_root_binding(root, plan["newDeployment"])
    if _read_pending(boot) is not None:
        raise BootDiskSwitchError("another ARMADA_BOOT update awaits reboot")
    record = {"schemaVersion": _PENDING_SCHEMA, **plan}
    if receipt is not None:
        _validate_receipt(receipt)
        record["receipt"] = receipt
    try:
        _checkpoint_boot(boot, dry_run=True)
    except OSError as checkpoint_exc:
        raise BootDiskSwitchError(
            "ARMADA_BOOT checkpoint unsupported or not permitted") from checkpoint_exc
    _write_switch_durably(boot, _PENDING_NAME, record)
    _publish_recovery_pointer(boot, expected)
    try:
        _checkpoint_boot(boot)
    except OSError as checkpoint_exc:
        try:
            _remove_recovery_pointer(boot)
        except BootDiskSwitchError:
            pass
        raise BootDiskSwitchUncertainError(
            "ARMADA_BOOT activation uncertain: checkpoint failed") from checkpoint_exc
    try:
        boot_disk_selector.activate_generation(boot, expected, target)
    except Exception as exc:
        try:
            selected = _read_boot_active(boot)[1]
            pending_now = _read_pending(boot)
        except Exception as read_exc:
            raise BootDiskSwitchUncertainError(
                "ARMADA_BOOT activation uncertain: selector read failed") from read_exc
        if selected == expected and pending_now == record:
            raise
        if selected == target and pending_now == record:
            try:
                boot_disk_selector.resync_armada_directory(boot)
            except Exception as resync_exc:
                raise BootDiskSwitchUncertainError(
                    "ARMADA_BOOT activation uncertain: resync failed") from resync_exc
        else:
            raise BootDiskSwitchUncertainError(
                "ARMADA_BOOT activation uncertain: selector state ambiguous") from exc
    try:
        matched = _read_boot_active(boot)[1] == target and _read_pending(boot) == record
    except (OSError, ValueError, BootDiskSwitchError) as read_exc:
        raise BootDiskSwitchUncertainError(
            "ARMADA_BOOT activation uncertain: readback failed") from read_exc
    if not matched:
        raise BootDiskSwitchUncertainError(
            "ARMADA_BOOT activation uncertain: readback did not match")
    try:
        _checkpoint_boot(boot)
    except OSError as checkpoint_exc:
        raise BootDiskSwitchUncertainError(
            "ARMADA_BOOT activation uncertain: checkpoint failed") from checkpoint_exc


def confirm(root, initial, *, trusted_key):
    if initial.get("bootSource") != "armada_boot":
        return False
    boot = _resolve_boot(root)
    pending = _read_pending(boot)
    if pending is None:
        return False
    expected, target = pending["expected"], pending["target"]
    selected = _read_boot_active(boot)[1]
    if initial["model"] != pending["model"]:
        raise BootDiskSwitchError("pending ARMADA_BOOT switch is for another model")
    if initial["deployment"] == pending["currentDeployment"]:
        if selected == expected:
            _remove_recovery_pointer(boot)
            _clear_pending_durably(boot)
        elif selected != target:
            raise BootDiskSwitchError("pending ARMADA_BOOT selector drift")
        return False
    if selected != target or initial["deployment"] != pending["newDeployment"]:
        raise BootDiskSwitchError("pending ARMADA_BOOT switch does not match boot evidence")
    _verified_generation(boot, target, initial["model"], initial["deployment"], trusted_key)
    deployment_store.promote_confirmed_deployment(
        sysroot_path=root, current_binding=pending["newDeployment"],
        previous_binding=pending["currentDeployment"])
    _write_switch_durably(boot, _COMPLETED_NAME, pending)
    for entry in (boot / "armada").iterdir():
        if (entry.name.startswith("gen-") and _GEN_DIR_RE.fullmatch(entry.name)
                and entry.name not in {target, expected} and entry.is_dir() and not entry.is_symlink()):
            shutil.rmtree(entry)
    _remove_recovery_pointer(boot)
    _clear_pending_durably(boot)
    return True


def ensure_ready(root, initial):
    """Reject another activation while a previously selected generation awaits confirmation."""
    boot = _resolve_boot(root)
    pending = _read_pending(boot)
    if pending is None:
        return
    if (initial["deployment"] == pending["currentDeployment"]
            and _read_boot_active(boot)[1] == pending["expected"]):
        _remove_recovery_pointer(boot)
        _clear_pending_durably(boot)
        return
    raise BootDiskSwitchError("an ARMADA_BOOT change already awaits restart or confirmation")


def recover_pending(root, initial, *, trusted_key):
    """Recover an interrupted switch when boot witness indicates recovery.

    Idempotent: selector may already be old or still target.
    """
    if initial.get("bootSource") != "armada_boot":
        return False
    boot = _resolve_boot(root)
    pending = _read_pending(boot)
    if pending is None:
        return False
    if initial.get("model") != pending.get("model"):
        raise BootDiskSwitchError("pending ARMADA_BOOT switch is for another model")
    if initial.get("deployment") != pending.get("currentDeployment"):
        raise BootDiskSwitchError("running deployment is not the pending previous generation")
    completed = _read_pending(boot, completed=True)
    if completed == pending:
        raise BootDiskSwitchError("pending ARMADA_BOOT switch already confirmed")
    active = _read_boot_active(boot)[1]
    link = boot / "armada" / "recovery"
    try:
        target_link = os.readlink(os.fspath(link))
    except FileNotFoundError:
        target_link = None
        if active != pending["expected"]:
            raise BootDiskSwitchError("recovery pointer missing for pending switch")
    except OSError as exc:
        raise BootDiskSwitchError("recovery pointer missing for pending switch") from exc
    if target_link is not None and target_link != pending["expected"]:
        raise BootDiskSwitchError("recovery pointer does not match pending expected generation")
    if active not in {pending["expected"], pending["target"]}:
        raise BootDiskSwitchError("active ARMADA_BOOT generation is not pending expected or target")
    if active == pending["target"] and target_link != pending["expected"]:
        raise BootDiskSwitchError("recovery pointer does not match pending expected generation")
    _verified_generation(
        boot, pending["expected"], pending["model"], pending["currentDeployment"], trusted_key)
    boot_disk_selector.ensure_root_binding(root, pending["currentDeployment"])
    if active == pending["target"]:
        try:
            _checkpoint_boot(boot, dry_run=True)
        except OSError as checkpoint_exc:
            raise BootDiskSwitchError(
                "ARMADA_BOOT checkpoint unsupported or not permitted") from checkpoint_exc
        boot_disk_selector.activate_generation(boot, pending["target"], pending["expected"])
    if _read_boot_active(boot)[1] != pending["expected"]:
        raise BootDiskSwitchError("recovery did not restore previous ARMADA_BOOT generation")
    if _read_pending(boot) != pending:
        raise BootDiskSwitchError("pending ARMADA_BOOT record changed during recovery")
    try:
        _checkpoint_boot(boot)
    except OSError as checkpoint_exc:
        raise BootDiskSwitchError(
            "ARMADA_BOOT checkpoint failed after recovery") from checkpoint_exc
    _remove_recovery_pointer(boot)
    _clear_pending_durably(boot)
    return True


def read_trial_status(context, root=Path("/sysroot")):
    """Report selector and deployment evidence without changing boot state."""
    boot = _resolve_boot(root)
    pending = _read_pending(boot)
    record = pending if pending is not None else _read_pending(boot, completed=True)
    if record is None:
        return {"state": "empty"}
    if "receipt" not in record:
        return {"state": "unavailable", "error": "ARMADA_BOOT transaction has no receipt"}
    selected = _read_boot_active(boot)[1]
    if selected != record["target"]:
        return {"state": "unavailable", "error": "ARMADA_BOOT target is not selected"}
    actual = context.get("deployment")
    if actual == record["newDeployment"]:
        running = "new"
    elif pending is not None and actual == record["currentDeployment"]:
        running = "old"
    else:
        return {"state": "unavailable", "error": "ARMADA_BOOT target does not match running evidence"}
    binding = boot / "armada" / selected / "binding.bin"
    if boot_disk_generation.decode_binding(binding) != record["newDeployment"]:
        return {"state": "unavailable", "error": "ARMADA_BOOT selected binding changed"}
    result = {
        "state": "available", "policy": "offline-dual",
        "bootPolicy": "dual-boot-no-slot-control", "bootSource": "armada_boot",
        "recordState": "prepared" if pending is not None else "confirmed",
        "oldDeployment": record["currentDeployment"], "newDeployment": record["newDeployment"],
        "runningDeployment": running, "bootConfirmed": pending is None,
        **record["receipt"],
    }
    if pending is None:
        result["finalization"] = {"state": "complete"}
    return result
