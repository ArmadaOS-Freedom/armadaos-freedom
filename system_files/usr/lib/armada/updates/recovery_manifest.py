"""Recovery manifest schema validation and binding against universal packages."""

from copy import deepcopy
import hashlib
import json
import re

from .package_contract import PackageError, _portable_path, validate_package

MAX_RECOVERY_MANIFEST_BYTES = 64 * 1024
_KERNEL_RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_TOP_KEYS = frozenset((
    "schemaVersion",
    "kind",
    "deviceModel",
    "version",
    "sourceRevision",
    "ostreeCommit",
    "protocol",
    "kernelRelease",
    "artifacts",
))
_ARTIFACT_KEYS = frozenset(("kernel", "initramfs", "dtb", "runtime"))
_REQUIRED_ROLES = {
    "kernel": "kernel",
    "runtime": "runtime",
    "initramfs": "recovery",
    "dtb": "recovery",
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise PackageError(f"invalid JSON constant: {value}")


def _reject_float(value):
    raise PackageError("JSON floating-point numbers are not allowed")


def _parse_json(data: bytes, source: str) -> dict:
    if len(data) > MAX_RECOVERY_MANIFEST_BYTES:
        raise PackageError(f"{source} exceeds {MAX_RECOVERY_MANIFEST_BYTES} bytes limit")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise PackageError(f"{source} must be valid UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (ValueError, RecursionError) as exc:
        raise PackageError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{source} must contain a JSON object")
    return value


def validate_recovery_manifest(document: bytes, package: dict, model: str) -> dict:
    """Validate a bounded recovery manifest document and bind it against a package."""
    validated_pkg = validate_package(package)
    if not isinstance(document, bytes):
        raise PackageError("recovery manifest document must be bytes")
    if len(document) > MAX_RECOVERY_MANIFEST_BYTES:
        raise PackageError(
            f"recovery manifest exceeds {MAX_RECOVERY_MANIFEST_BYTES} bytes limit"
        )
    if not isinstance(model, str) or model not in validated_pkg["supportedModels"]:
        raise PackageError(f"model {model} is not supported by package")
    if model not in validated_pkg["recoveryManifests"]:
        raise PackageError(f"model {model} has no recovery manifest in package")

    manifest_path = validated_pkg["recoveryManifests"][model]
    files_by_path = {f["path"]: f for f in validated_pkg["files"]}
    manifest_record = files_by_path.get(manifest_path)
    if (manifest_record is None or manifest_record["compression"] != "none"
            or manifest_record["role"] != "manifest"):
        raise PackageError(f"recovery manifest file record is invalid: {manifest_path}")
    if (len(document) != manifest_record["bytes"]
            or hashlib.sha256(document).hexdigest() != manifest_record["sha256"]):
        raise PackageError(f"recovery manifest bytes do not match package record: {manifest_path}")

    parsed = _parse_json(document, f"recovery manifest for {model}")
    if set(parsed.keys()) != _TOP_KEYS:
        raise PackageError("recovery manifest has invalid top-level keys")

    if type(parsed["schemaVersion"]) is not int or parsed["schemaVersion"] != 1:
        raise PackageError("recovery manifest schemaVersion must be integer 1")
    if parsed["kind"] != "freedom-update-recovery":
        raise PackageError("recovery manifest kind must be freedom-update-recovery")
    if parsed["deviceModel"] != model:
        raise PackageError(f"recovery manifest deviceModel {parsed['deviceModel']!r} does not match {model}")
    if parsed["version"] != validated_pkg["version"]:
        raise PackageError("recovery manifest version does not match package")
    if parsed["sourceRevision"] != validated_pkg["sourceRevision"]:
        raise PackageError("recovery manifest sourceRevision does not match package")
    if parsed["ostreeCommit"] != validated_pkg["ostreeCommit"]:
        raise PackageError("recovery manifest ostreeCommit does not match package")
    if parsed["protocol"] != "offline-rpc-v1":
        raise PackageError("recovery manifest protocol must be offline-rpc-v1")

    krelease = parsed["kernelRelease"]
    if not isinstance(krelease, str) or not _KERNEL_RELEASE_PATTERN.fullmatch(krelease):
        raise PackageError("recovery manifest kernelRelease is invalid")

    artifacts = parsed["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts.keys()) != _ARTIFACT_KEYS:
        raise PackageError("recovery manifest artifacts must contain exactly kernel, initramfs, dtb, runtime")

    art_paths = {}
    for key in ("kernel", "initramfs", "dtb", "runtime"):
        val = artifacts[key]
        path = _portable_path(val, f"recovery manifest artifacts.{key}")
        art_paths[key] = path

    if len(set(art_paths.values())) != 4:
        raise PackageError("recovery manifest artifact paths must be distinct")

    files_by_path = {f["path"]: f for f in validated_pkg["files"]}
    for key, expected_role in _REQUIRED_ROLES.items():
        path = art_paths[key]
        if path not in files_by_path:
            raise PackageError(f"recovery manifest {key} path {path!r} is not present in package.files")
        entry = files_by_path[path]
        if entry["compression"] != "none":
            raise PackageError(f"recovery manifest artifact {path!r} must have compression none")
        if entry["role"] != expected_role:
            raise PackageError(
                f"recovery manifest artifact {path!r} has role {entry['role']!r}, expected {expected_role!r}"
            )

    return deepcopy(parsed)
