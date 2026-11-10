"""Prepare boot candidate files only; no physical trust, OTA, or device writes are authorized.

The caller must hold the operation lock and supply verified materials and the current trusted helper.
TB710FU uses payload build, not A/B; this does not perform a complete OS update.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from .boot_materials import BootMaterialsError, _validate_helper_path, _validate_output_dir
from .deployment_store import DeploymentError, _parse_binding

_ALLOWED_MODELS = frozenset({"TB322FC", "TB323FU", "TB710FU"})
_ALLOWED_SLOTS = frozenset({"a", "b"})
_MODE_REPORTS = {
    "aosp-resign": "AospResign",
    "gbl-preserve-metadata": "GblPreserveMetadata",
}
_SLOT_FILE_KEYS = ("boot", "vendorBoot", "initBoot", "abl", "xblConfig", "vbmeta")
_MATERIAL_ARTIFACT_KEYS = ("kernel", "initramfs", "runtime", "dtbNormal", "dtbDiagnostic")
_HEX64 = frozenset("0123456789abcdef")
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_REPORT_BYTES = 64 * 1024
_SUBPROCESS_TIMEOUT_SECONDS = 300
_DEFAULT_BOOT_TOOL_PATH = Path("/usr/libexec/armada/boot-tool/armada-boot-tool")


class BootCandidateError(RuntimeError):
    """Raised when file-only boot candidate preparation fails."""


def _require_nonempty_str(value, field):
    if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
        raise BootCandidateError(f"{field} must be a non-empty control-free string")
    return value


def _require_positive_int(value, field, max_value=_MAX_FILE_BYTES):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > max_value:
        raise BootCandidateError(f"{field} must be an integer in 1..{max_value}")
    return value


def _require_hex64(value, field):
    if not isinstance(value, str) or len(value) != 64:
        raise BootCandidateError(f"{field} must be 64-char hexadecimal")
    normalized = value.lower()
    if any(ch not in _HEX64 for ch in normalized):
        raise BootCandidateError(f"{field} must be 64-char hexadecimal")
    return normalized


def _require_canonical_dir(path_value, field):
    raw = Path(_require_nonempty_str(path_value, field))
    if not raw.is_absolute() or raw.is_symlink() or not raw.is_dir():
        raise BootCandidateError(f"{field} must be an absolute existing non-symlink directory: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise BootCandidateError(f"cannot resolve {field}: {exc}") from exc
    if resolved != raw:
        raise BootCandidateError(f"{field} must be canonical: {raw}")
    return resolved


def _validate_boot_tool(boot_tool_path, forbidden_roots, output_dir):
    try:
        resolved = _validate_helper_path(boot_tool_path, forbidden_roots, output_dir)
    except BootMaterialsError as exc:
        raise BootCandidateError(str(exc).replace("dtb_helper_path", "boot_tool_path")) from exc
    if not os.access(resolved, os.X_OK):
        raise BootCandidateError(f"boot_tool_path is not executable: {resolved}")
    return resolved


def _hash_regular_file(path, max_bytes=_MAX_FILE_BYTES):
    if not path.is_absolute() or path.is_symlink():
        raise BootCandidateError(f"refusing non-canonical or symlink file: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BootCandidateError(f"cannot resolve file {path}: {exc}") from exc
    if resolved != path:
        raise BootCandidateError(f"file path must be canonical: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise BootCandidateError(f"cannot open regular file {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BootCandidateError(f"not a regular file: {path}")
        if st.st_size <= 0 or st.st_size > max_bytes:
            raise BootCandidateError(f"file size {st.st_size} out of bounds for {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BootCandidateError(f"file grew beyond {max_bytes} bytes: {path}")
            digest.update(chunk)
        if total != st.st_size:
            raise BootCandidateError(f"file size changed while hashing {path}")
        return total, digest.hexdigest()
    finally:
        os.close(fd)


def _verify_claim(claim, field, expected_path=None, size_key="bytes", max_bytes=_MAX_FILE_BYTES):
    if not isinstance(claim, dict) or set(claim.keys()) != {"path", size_key, "sha256"}:
        raise BootCandidateError(f"{field} must have exact keys path, {size_key}, sha256")
    raw_path = Path(_require_nonempty_str(claim["path"], f"{field}.path"))
    if expected_path is not None and raw_path != expected_path:
        raise BootCandidateError(f"{field}.path mismatch: expected {expected_path}, got {raw_path}")
    expected_size = _require_positive_int(claim[size_key], f"{field}.{size_key}", max_bytes)
    expected_sha = _require_hex64(claim["sha256"], f"{field}.sha256")
    actual_size, actual_sha = _hash_regular_file(raw_path, max_bytes=max_bytes)
    if actual_size != expected_size or actual_sha != expected_sha:
        raise BootCandidateError(f"{field} content mismatch for {raw_path}")
    return {"path": str(raw_path), "bytes": actual_size, "sha256": actual_sha}


def _to_cli_claim(claim):
    return {"path": claim["path"], "length": claim["bytes"], "sha256": claim["sha256"]}


def _write_new(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise BootCandidateError(f"short write to {path}")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def _validate_inputs(slot, slot_files, materials, mode, max_raw_bytes):
    if not isinstance(slot, str) or slot not in _ALLOWED_SLOTS:
        raise BootCandidateError(f"unsupported slot: {slot!r}")
    if not isinstance(mode, str) or mode not in _MODE_REPORTS:
        raise BootCandidateError(f"unsupported mode: {mode!r}")
    budget = _require_positive_int(max_raw_bytes, "max_raw_bytes", _MAX_FILE_BYTES)
    if not isinstance(slot_files, dict) or set(slot_files.keys()) != set(_SLOT_FILE_KEYS):
        raise BootCandidateError("slot_files must contain exact keys boot, vendorBoot, initBoot, abl, xblConfig, vbmeta")
    if not isinstance(materials, dict):
        raise BootCandidateError("materials must be a dict")
    required_material_keys = {
        "schemaVersion",
        "phase",
        "model",
        "deployment",
        "target",
        "kernelRelease",
        "rootUuid",
        "bootUuid",
        "normalBootArgs",
        "diagnosticBootArgs",
        "artifacts",
    }
    material_keys = set(materials.keys())
    if (material_keys != required_material_keys and
            material_keys != required_material_keys | {"transactionUuid"}):
        raise BootCandidateError("materials keys do not match prepare_boot_materials report schema")
    if type(materials.get("schemaVersion")) is not int or materials.get("schemaVersion") != 1:
        raise BootCandidateError("materials.schemaVersion must be 1")
    if materials.get("phase") != "materials_prepared":
        raise BootCandidateError("materials.phase must be 'materials_prepared'")
    model = materials.get("model")
    if not isinstance(model, str) or model not in _ALLOWED_MODELS:
        raise BootCandidateError(f"unsupported materials.model: {model!r}")
    deployment = _require_nonempty_str(materials.get("deployment"), "materials.deployment")
    try:
        _parse_binding(deployment)
    except DeploymentError as exc:
        raise BootCandidateError(str(exc)) from exc
    target_path = _require_canonical_dir(materials.get("target"), "materials.target")
    kernel_release = _require_nonempty_str(materials.get("kernelRelease"), "materials.kernelRelease")
    root_uuid = _require_nonempty_str(materials.get("rootUuid"), "materials.rootUuid")
    boot_uuid = _require_nonempty_str(materials.get("bootUuid"), "materials.bootUuid")
    normal_args = _require_nonempty_str(materials.get("normalBootArgs"), "materials.normalBootArgs")
    diag_args = _require_nonempty_str(materials.get("diagnosticBootArgs"), "materials.diagnosticBootArgs")
    transaction_uuid = None
    if "transactionUuid" in materials:
        transaction_uuid = materials["transactionUuid"]
        if not isinstance(transaction_uuid, str):
            raise BootCandidateError("materials.transactionUuid must be a canonical string")
        try:
            parsed_transaction = uuid.UUID(transaction_uuid)
        except ValueError as exc:
            raise BootCandidateError("invalid materials.transactionUuid") from exc
        if parsed_transaction.int == 0 or str(parsed_transaction) != transaction_uuid:
            raise BootCandidateError("materials.transactionUuid must be canonical and nonzero")
    raw_artifacts = materials.get("artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts.keys()) != set(_MATERIAL_ARTIFACT_KEYS):
        raise BootCandidateError("materials.artifacts must contain exact keys kernel, initramfs, runtime, dtbNormal, dtbDiagnostic")
    verified_slot_files = {
        key: _verify_claim(slot_files[key], f"slot_files.{key}")
        for key in _SLOT_FILE_KEYS
    }
    verified_artifacts = {
        key: _verify_claim(raw_artifacts[key], f"materials.artifacts.{key}")
        for key in _MATERIAL_ARTIFACT_KEYS
    }
    validated = {
        "budget": budget,
        "model": model,
        "deployment": deployment,
        "target": target_path,
        "kernelRelease": kernel_release,
        "rootUuid": root_uuid,
        "bootUuid": boot_uuid,
        "normalBootArgs": normal_args,
        "diagnosticBootArgs": diag_args,
        "slotFiles": verified_slot_files,
        "artifacts": verified_artifacts,
    }
    if "transactionUuid" in materials:
        validated["transactionUuid"] = transaction_uuid
    return validated


def _run_stage(tool_path, subcommand, request_doc, out_dir, tmp_dir, expected_phase, slot):
    request_path = out_dir / f"{subcommand}-request.json"
    step_dir = out_dir / subcommand
    stdout_log = out_dir / f"{subcommand}.stdout.log"
    stderr_log = out_dir / f"{subcommand}.stderr.log"
    _write_new(request_path, json.dumps(request_doc, indent=2, sort_keys=True).encode("utf-8"))
    env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "TMPDIR": str(tmp_dir),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    }
    try:
        proc = subprocess.run(
            [str(tool_path), subcommand, str(request_path), str(step_dir)],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootCandidateError(f"{subcommand} timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise BootCandidateError(f"failed to execute {subcommand}: {exc}") from exc
    stdout_bytes = (proc.stdout or "").encode("utf-8", errors="replace")
    stderr_bytes = (proc.stderr or "").encode("utf-8", errors="replace")
    _write_new(stdout_log, stdout_bytes)
    _write_new(stderr_log, stderr_bytes)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise BootCandidateError(f"{subcommand} failed with exit code {proc.returncode}: {tail}")
    if len(stdout_bytes) == 0 or len(stdout_bytes) > _MAX_REPORT_BYTES:
        raise BootCandidateError(f"{subcommand} stdout report size out of bounds")
    result_path = step_dir / "result.json"
    result_size, _ = _hash_regular_file(result_path, max_bytes=_MAX_REPORT_BYTES)
    result_bytes = result_path.read_bytes()
    if len(result_bytes) != result_size:
        raise BootCandidateError(f"{subcommand} result.json changed during read")
    try:
        stdout_doc = json.loads(stdout_bytes.decode("utf-8"))
        result_doc = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootCandidateError(f"{subcommand} produced invalid JSON report: {exc}") from exc
    if stdout_doc != result_doc or not isinstance(result_doc, dict):
        raise BootCandidateError(f"{subcommand} stdout JSON did not match result.json")
    if (
        type(result_doc.get("schemaVersion")) is not int
        or result_doc.get("schemaVersion") != 1
        or result_doc.get("operation") != subcommand
        or result_doc.get("phase") != expected_phase
        or result_doc.get("slot") != slot
    ):
        raise BootCandidateError(f"{subcommand} report header validation failed")
    return step_dir, result_doc


def prepare_boot_candidate(
    *,
    slot,
    slot_files,
    materials,
    mode,
    max_raw_bytes,
    output_dir,
    boot_tool_path=_DEFAULT_BOOT_TOOL_PATH,
):
    """Build files only under the caller's lock, verified materials, and trusted helper."""
    stage = "validate"
    try:
        validated = _validate_inputs(slot, slot_files, materials, mode, max_raw_bytes)
        forbidden_roots = (validated["target"],)
        try:
            out = _validate_output_dir(output_dir, forbidden_roots)
        except BootMaterialsError as exc:
            raise BootCandidateError(str(exc)) from exc
        tool_path = _validate_boot_tool(boot_tool_path, forbidden_roots, out)

        os.mkdir(out, 0o700)
        tmp_dir = out / "tmp"
        os.mkdir(tmp_dir, 0o700)

        stage = "inspect-slot"
        inspect_req = {
            "schemaVersion": 1,
            "slot": slot,
            "files": {
                k: _to_cli_claim(v) for k, v in validated["slotFiles"].items() if k != "vbmeta"
            },
        }
        inspect_dir, inspect_doc = _run_stage(
            tool_path, "inspect-slot", inspect_req, out, tmp_dir, "slot_inspected", slot
        )
        base_claim = _verify_claim(
            inspect_doc.get("baseImage"),
            "inspect.baseImage",
            expected_path=inspect_dir / "base.bin",
            size_key="length",
        )
        material = inspect_doc.get("material")
        if not isinstance(material, dict):
            raise BootCandidateError("inspect.material must be an object")
        loader_budget = material.get("loaderBudget")
        if not isinstance(loader_budget, dict):
            raise BootCandidateError("inspect.material.loaderBudget must be an object")
        measured_budget = _require_positive_int(
            loader_budget.get("maxRawBytes"), "inspect.material.loaderBudget.maxRawBytes"
        )
        if (
            material.get("slot") != slot
            or material.get("isKshim") is not True
            or material.get("baseImageLength") != base_claim["bytes"]
            or _require_hex64(material.get("baseImageSha256"), "inspect.material.baseImageSha256")
            != base_claim["sha256"]
        ):
            raise BootCandidateError("inspect.material does not match extracted KSHIM base image")
        if validated["budget"] > measured_budget:
            raise BootCandidateError(
                f"caller max_raw_bytes {validated['budget']} exceeds loader budget {measured_budget}"
            )

        stage = "build-raw"
        raw_files = {"baseImage": _to_cli_claim(base_claim)}
        for key in _MATERIAL_ARTIFACT_KEYS:
            raw_files[key] = _to_cli_claim(validated["artifacts"][key])
        raw_req = {
            "schemaVersion": 1,
            "slot": slot,
            "model": validated["model"],
            "normalBootArgs": validated["normalBootArgs"],
            "diagnosticBootArgs": validated["diagnosticBootArgs"],
            "maxRawBytes": validated["budget"],
            "files": raw_files,
        }
        raw_dir, raw_doc = _run_stage(
            tool_path, "build-raw", raw_req, out, tmp_dir, "raw_prepared", slot
        )
        raw_info = raw_doc.get("raw")
        if not isinstance(raw_info, dict):
            raise BootCandidateError("build-raw report missing raw object")
        packed_claim = _verify_claim(
            raw_info.get("packedRaw"),
            "raw.packedRaw",
            expected_path=raw_dir / "PackedKernel",
            size_key="length",
            max_bytes=validated["budget"],
        )
        normal_dtb = raw_info.get("normalDtb")
        diag_dtb = raw_info.get("diagnosticDtb")
        if (
            raw_info.get("model") != validated["model"]
            or raw_info.get("rawBudgetBytes") != validated["budget"]
            or not isinstance(normal_dtb, dict)
            or not isinstance(diag_dtb, dict)
            or normal_dtb.get("deviceModel") != validated["model"]
            or normal_dtb.get("kind") != "Normal"
            or normal_dtb.get("length") != validated["artifacts"]["dtbNormal"]["bytes"]
            or _require_hex64(normal_dtb.get("sha256"), "raw.normalDtb.sha256")
            != validated["artifacts"]["dtbNormal"]["sha256"]
            or diag_dtb.get("deviceModel") != validated["model"]
            or diag_dtb.get("kind") != "Diagnostic"
            or diag_dtb.get("length") != validated["artifacts"]["dtbDiagnostic"]["bytes"]
            or _require_hex64(diag_dtb.get("sha256"), "raw.diagnosticDtb.sha256")
            != validated["artifacts"]["dtbDiagnostic"]["sha256"]
        ):
            raise BootCandidateError("build-raw metadata did not match prepared boot materials")

        stage = "repack-envelope"
        repack_files = {k: _to_cli_claim(v) for k, v in validated["slotFiles"].items()}
        repack_files["packedRaw"] = _to_cli_claim(packed_claim)
        repack_req = {
            "schemaVersion": 1,
            "slot": slot,
            "mode": mode,
            "maxRawBytes": validated["budget"],
            "files": repack_files,
        }
        envelope_dir, envelope_doc = _run_stage(
            tool_path, "repack-envelope", repack_req, out, tmp_dir, "envelope_prepared", slot
        )
        envelope = envelope_doc.get("envelope")
        if not isinstance(envelope, dict):
            raise BootCandidateError("repack-envelope report missing envelope object")
        candidate_claim = _verify_claim(
            envelope.get("candidate"),
            "envelope.candidate",
            expected_path=envelope_dir / "candidate.img",
            size_key="length",
        )
        if candidate_claim["bytes"] != validated["slotFiles"]["boot"]["bytes"]:
            raise BootCandidateError("candidate.img size does not match source boot partition size")
        source_material = envelope.get("sourceMaterial")
        if (
            envelope.get("mode") != _MODE_REPORTS[mode]
            or envelope.get("rawBudgetBytes") != validated["budget"]
            or not isinstance(source_material, dict)
            or source_material.get("slot") != slot
            or source_material.get("isKshim") is not True
            or _require_hex64(source_material.get("baseImageSha256"), "envelope.sourceMaterial.baseImageSha256")
            != base_claim["sha256"]
        ):
            raise BootCandidateError("repack-envelope metadata validation failed")
        root_key_sha = _require_hex64(envelope.get("rootPublicKeySha256"), "envelope.rootPublicKeySha256")
        boot_key_sha = _require_hex64(envelope.get("bootPublicKeySha256"), "envelope.bootPublicKeySha256")
        metadata_sha = _require_hex64(envelope.get("metadataSha256"), "envelope.metadataSha256")
        digest_matches = envelope.get("payloadDigestMatchesSignedMetadata")
        if not isinstance(digest_matches, bool):
            raise BootCandidateError("envelope.payloadDigestMatchesSignedMetadata must be a bool")
        if mode == "aosp-resign" and not digest_matches:
            raise BootCandidateError("aosp-resign candidate must have payloadDigestMatchesSignedMetadata=True")

        stage = "reverify"
        for key, claim in validated["slotFiles"].items():
            _verify_claim(claim, f"recheck.slot_files.{key}")
        for key, claim in validated["artifacts"].items():
            _verify_claim(claim, f"recheck.materials.artifacts.{key}")
        _verify_claim(base_claim, "recheck.baseImage", expected_path=inspect_dir / "base.bin")
        _verify_claim(
            packed_claim,
            "recheck.packedRaw",
            expected_path=raw_dir / "PackedKernel",
            max_bytes=validated["budget"],
        )
        _verify_claim(candidate_claim, "recheck.candidate", expected_path=envelope_dir / "candidate.img")

        report = {
            "schemaVersion": 1,
            "phase": "candidate_prepared",
            "slot": slot,
            "model": validated["model"],
            "deployment": validated["deployment"],
            "kernelRelease": validated["kernelRelease"],
            "rootUuid": validated["rootUuid"],
            "bootUuid": validated["bootUuid"],
            "mode": mode,
            "maxRawBytes": validated["budget"],
            "candidate": candidate_claim,
            "sourceBootSha256": validated["slotFiles"]["boot"]["sha256"],
            "baseImageSha256": base_claim["sha256"],
            "metadataSha256": metadata_sha,
            "rootPublicKeySha256": root_key_sha,
            "bootPublicKeySha256": boot_key_sha,
            "payloadDigestMatchesSignedMetadata": digest_matches,
        }
        if "transactionUuid" in validated:
            report["transactionUuid"] = validated["transactionUuid"]
        _write_new(out / "candidate.json", json.dumps(report, indent=2, sort_keys=True).encode("utf-8"))
        dir_fd = os.open(
            out,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return report
    except BootCandidateError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        raise BootCandidateError(f"boot candidate preparation failed during {stage}: {exc}") from exc
