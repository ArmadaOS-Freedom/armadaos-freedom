"""Prepare boot materials (files only) for a verified OSTree system update.

This layer reads a registered, pinned target deployment, verifies its update
boot manifest with the trusted currently-installed helpers, and stages raw
artifact copies plus DTBs into an exclusive output directory. It performs no
signing, no device writes, no slot activation, and no /etc or /var/home
mutation; a later layer signs and installs these materials.

The caller is required to hold the global update lock.
"""

import hashlib
import importlib.util
import json
import os
import stat
import uuid
from pathlib import Path

from .boot_context import _read_regular
from .boot_payload import (
    _resolve_regular_file,
    _resolve_root,
    verify_manifest,
)
from .boot_recipe import diagnostic_boot_args, model_boot_args
from .deployment_options import read_deployment_options
from .deployment_store import _parse_binding

_MAX_MANIFEST_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024


class BootMaterialsError(RuntimeError):
    """Expected failure while preparing boot materials."""


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with open(fd, "wb", closefd=False) as out:
            out.write(data)
            out.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def _stat_snapshot(fd):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise BootMaterialsError("manifest source is not a regular file")
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _copy_verified(src_fd, out_path, expected_bytes):
    before = _stat_snapshot(src_fd)
    if before[2] != expected_bytes:
        raise BootMaterialsError(
            f"source size {before[2]} != manifest record {expected_bytes}"
        )
    digest = hashlib.sha256()
    total = 0
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while total < expected_bytes:
            chunk = os.read(src_fd, min(_CHUNK, expected_bytes - total))
            if not chunk:
                raise BootMaterialsError("source truncated during copy")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise BootMaterialsError("short write while staging boot artifact")
                view = view[written:]
            total += len(chunk)
        extra = os.read(src_fd, 1)
        if extra:
            raise BootMaterialsError("source grew beyond manifest record size")
        os.fsync(fd)
    finally:
        os.close(fd)
    if total != expected_bytes:
        raise BootMaterialsError("copy size mismatch")
    after = _stat_snapshot(src_fd)
    if before != after:
        raise BootMaterialsError("source changed during copy (size/mtime/inode)")
    return digest.hexdigest()


def _deployment_root(sysroot, binding):
    osname, commit, serial = _parse_binding(binding)
    return (sysroot / "ostree" / "deploy" / osname / "deploy" / f"{commit}.{serial}").resolve()


def _validate_output_dir(output_dir, forbidden_roots):
    if not isinstance(output_dir, (str, Path)):
        raise BootMaterialsError("output_dir must be str or Path")
    raw = Path(output_dir)
    if not str(raw) or not raw.is_absolute():
        raise BootMaterialsError(f"output_dir must be absolute: {raw}")
    if raw.is_symlink() or raw.exists():
        raise BootMaterialsError(f"output_dir must be nonexistent non-symlink path: {raw}")
    try:
        resolved = raw.resolve(strict=False)
    except OSError as exc:
        raise BootMaterialsError(f"cannot canonicalize output_dir: {exc}") from exc
    if resolved != raw:
        raise BootMaterialsError(f"output_dir must be canonical: {raw}")
    parent = raw.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BootMaterialsError(f"output_dir parent must be existing non-symlink dir: {parent}")
    try:
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise BootMaterialsError(f"cannot resolve output parent: {exc}") from exc
    if parent_resolved != parent:
        raise BootMaterialsError(f"output_dir parent must be canonical: {parent}")
    for root in forbidden_roots:
        if resolved == root or root in resolved.parents:
            raise BootMaterialsError(f"output_dir must not be inside deployment root {root}")
    return resolved


def _validate_helper_path(helper, forbidden_roots, output_dir):
    if not isinstance(helper, (str, Path)):
        raise BootMaterialsError("dtb_helper_path must be str or Path")
    raw = Path(helper)
    if not raw.is_absolute():
        raise BootMaterialsError(f"dtb_helper_path must be absolute: {raw}")
    if raw.is_symlink() or not raw.is_file():
        raise BootMaterialsError(f"dtb_helper_path must be existing regular non-symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise BootMaterialsError(f"cannot resolve dtb_helper_path: {exc}") from exc
    if resolved != raw:
        raise BootMaterialsError(f"dtb_helper_path must be canonical: {raw}")
    for root in forbidden_roots:
        if resolved == root or root in resolved.parents:
            raise BootMaterialsError(f"dtb_helper_path must not live under deployment root {root}")
    if resolved == output_dir or output_dir in resolved.parents:
        raise BootMaterialsError("dtb_helper_path must not live inside output_dir")
    return resolved


def _load_helper(helper_path):
    spec = importlib.util.spec_from_file_location("armada_dtb_helper", helper_path)
    if spec is None or spec.loader is None:
        raise BootMaterialsError("cannot load dtb helper module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_helper_env(output_dir):
    work = output_dir / "dtb-work"
    logs = output_dir / "dtb-logs"
    tmp = output_dir / "dtb-tmp"
    for d in (work, logs, tmp):
        os.mkdir(d, 0o700)
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
    }
    return work, logs, tmp, env


def _call_helper(helper, output_dir, args, boot_stage, tag,
                 transaction_uuid=None):
    try:
        result = helper.build_dtb(
            {"dtb": str(output_dir / "source.dtb")},
            args,
            boot_stage=boot_stage,
            root_medium="internal",
            initrd_index=5,
            tag=tag,
            transaction_uuid=transaction_uuid,
        )
    except helper.StageError as exc:
        raise BootMaterialsError(f"dtb helper build_dtb ({tag}) failed: {exc}") from exc
    try:
        _path, data, _changes, _dts = result
    except (TypeError, ValueError) as exc:
        raise BootMaterialsError(f"dtb helper returned malformed result: {exc}") from exc
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise BootMaterialsError("dtb helper returned empty or non-bytes payload")
    return bytes(data)


def prepare_boot_materials(
    *,
    sysroot_path,
    current_binding,
    target_binding,
    model,
    output_dir,
    dtb_helper_path=Path("/usr/libexec/armada/kshim-build-boot.py"),
    transaction_uuid=None,
):
    """Stage verified raw boot artifacts; return the materials report."""
    if transaction_uuid is not None:
        if not isinstance(transaction_uuid, str):
            raise BootMaterialsError("transaction_uuid must be a canonical string")
        try:
            parsed_transaction = uuid.UUID(transaction_uuid)
        except ValueError as exc:
            raise BootMaterialsError("invalid transaction_uuid") from exc
        if parsed_transaction.int == 0 or str(parsed_transaction) != transaction_uuid:
            raise BootMaterialsError("transaction_uuid must be canonical and nonzero")
    try:
        options = read_deployment_options(
            sysroot_path=sysroot_path,
            current_binding=current_binding,
            target_binding=target_binding,
        )
        sysroot = Path(sysroot_path).resolve()
        forbidden = [
            _deployment_root(sysroot, current_binding),
            _deployment_root(sysroot, target_binding),
        ]
        out = _validate_output_dir(output_dir, forbidden)
        helper_path = _validate_helper_path(dtb_helper_path, forbidden, out)

        target_path = Path(options["path"])
        manifest_rel = "usr/share/armada/update-boot/manifest.json"
        manifest_bytes = _read_regular(target_path / manifest_rel, _MAX_MANIFEST_BYTES)
        manifest_doc = json.loads(manifest_bytes.decode("utf-8"))
        verified = verify_manifest(Path(target_path), model, manifest_doc)
        profile = verified["profile"]

        os.mkdir(out, 0o700)

        root, usr = _resolve_root(Path(target_path))
        artifacts = {}
        for record_key, out_name in (
            ("kernel", "kernel.img"),
            ("initramfs", "initramfs.img"),
            ("runtime", "runtime.bin"),
            ("dtb", "source.dtb"),
        ):
            record = verified[record_key]
            src_path, _ = _resolve_regular_file(root, usr, record["path"])
            src_fd = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                sha = _copy_verified(src_fd, out / out_name, record["bytes"])
            finally:
                os.close(src_fd)
            if sha != record["sha256"]:
                raise BootMaterialsError(
                    f"{record_key} sha256 mismatch: got {sha}, expected {record['sha256']}"
                )
            if record_key != "dtb":
                artifacts[record_key] = {
                    "path": str(out / out_name),
                    "bytes": record["bytes"],
                    "sha256": sha,
                }

        normal_args = model_boot_args(options["options"], profile)
        diagnostic_args = diagnostic_boot_args(normal_args, profile)

        helper = _load_helper(helper_path)
        work, logs, tmp, env = _prepare_helper_env(out)
        helper.WORK = work
        helper.LOGS = logs
        helper.TMP = tmp
        helper.ENV = env
        helper.CHECKS = []
        helper.COMMANDS = []
        helper.TOOL = {}
        helper.REQUIRED_COMPATIBLE = list(profile["required"])
        helper.FORBIDDEN_COMPATIBLE = list(profile["forbidden"])
        try:
            for tool_name in ("fdtget", "fdtput", "dtc"):
                helper.require_tool(tool_name)
        except helper.StageError as exc:
            raise BootMaterialsError(f"dtb helper tool check failed: {exc}") from exc

        dtb_normal = _call_helper(helper, out, normal_args, "os", "normal",
                                  transaction_uuid=transaction_uuid)
        dtb_diagnostic = _call_helper(helper, out, diagnostic_args, "maintenance",
                                      "diagnostic", transaction_uuid=transaction_uuid)
        for data, out_name, key in (
            (dtb_normal, "dtb-normal.img", "dtbNormal"),
            (dtb_diagnostic, "dtb-diagnostic.img", "dtbDiagnostic"),
        ):
            _write_new(out / out_name, data)
            artifacts[key] = {
                "path": str(out / out_name),
                "bytes": len(data),
                "sha256": _sha256_hex(data),
            }

        recheck = read_deployment_options(
            sysroot_path=sysroot_path,
            current_binding=current_binding,
            target_binding=target_binding,
        )
        if recheck != options:
            raise BootMaterialsError("deployment options changed during preparation")
        current_manifest_bytes = _read_regular(target_path / manifest_rel, _MAX_MANIFEST_BYTES)
        current_manifest_doc = json.loads(current_manifest_bytes.decode("utf-8"))
        if current_manifest_bytes != manifest_bytes or current_manifest_doc != manifest_doc:
            raise BootMaterialsError("boot manifest changed during preparation")
        reverified = verify_manifest(Path(target_path), model, current_manifest_doc)
        if reverified != verified:
            raise BootMaterialsError("boot manifest verification changed during preparation")

        report = {
            "schemaVersion": 1,
            "phase": "materials_prepared",
            "model": model,
            "deployment": options["deployment"],
            "target": str(target_path),
            "kernelRelease": verified["kernelRelease"],
            "rootUuid": options["rootUuid"],
            "bootUuid": options["bootUuid"],
            "normalBootArgs": normal_args,
            "diagnosticBootArgs": diagnostic_args,
            "artifacts": artifacts,
        }
        if transaction_uuid is not None:
            report["transactionUuid"] = transaction_uuid
        _write_new(out / "materials.json", json.dumps(report, indent=2, sort_keys=True).encode("utf-8"))
        return report
    except BootMaterialsError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        raise BootMaterialsError(f"boot materials preparation failed: {exc}") from exc
