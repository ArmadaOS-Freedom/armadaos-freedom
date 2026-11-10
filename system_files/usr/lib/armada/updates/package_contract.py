"""Validation for signed Freedom package and split-media descriptors."""

from copy import deepcopy
from pathlib import Path
import re

from .catalog import CatalogError, _identity, _updater_tuple
from .signature import DEFAULT_TRUSTED_KEY, SignatureError, load_signed_json


_MODELS = frozenset(("TB322FC", "TB323FU", "TB710FU"))
_OPERATIONS = frozenset(("install", "offline-upgrade", "decky-repair"))
_ROLES = frozenset((
    "manifest", "linux-root", "linux-boot", "kernel", "initramfs", "runtime",
    "dtb-normal", "dtb-diagnostic", "recovery", "metadata",
))
_TOP_KEYS = frozenset((
    "schemaVersion", "kind", "product", "architecture", "releaseId", "version",
    "sourceRevision", "ociDigest", "ostreeCommit", "minimumInstallerVersion",
    "minimumUpdaterVersion", "supportedModels", "operations", "archive", "parts",
    "files", "modelManifests", "recoveryManifests", "deckySnapshot",
))
_ARCHIVE_KEYS = frozenset(("fileName", "bytes", "sha256"))
_FILE_KEYS = frozenset((
    "path", "bytes", "sha256", "expandedBytes", "expandedSha256", "compression", "role",
))
_DECKY_KEYS = frozenset((
    "releaseId", "tag", "publishedAt", "assetId", "url", "size", "sha256",
))
_HEX40 = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{number}" for number in range(1, 10)]
    + [f"LPT{number}" for number in range(1, 10)]
)
_MAX_FILE_BYTES = 1024 ** 4
_MAX_PART_BYTES = 2 * 1024 ** 3 - 1


class PackageError(ValueError):
    """A package descriptor is invalid, untrusted, or incompatible."""


def load_package(document: bytes, signature_b64: bytes, *,
                 trusted_key: Path = DEFAULT_TRUSTED_KEY) -> dict:
    """Verify the signed descriptor bytes before validating their contents."""
    try:
        data = load_signed_json(document, signature_b64, trusted_key=trusted_key)
    except SignatureError as exc:
        raise PackageError(f"package signature verification failed: {exc}") from exc
    return validate_package(data)


def _positive_int(value, field: str, maximum: int | None = None) -> None:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        limit = f" and at most {maximum}" if maximum is not None else ""
        raise PackageError(f"{field} must be a positive integer{limit}")


def _hash(value, field: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise PackageError(f"{field} must be 64 lowercase hexadecimal characters")


def _archive_member(value, field: str, maximum: int | None) -> None:
    if not isinstance(value, dict) or set(value) != _ARCHIVE_KEYS:
        raise PackageError(f"{field} must contain exactly fileName, bytes, and sha256")
    name = _portable_path(value["fileName"], f"{field}.fileName")
    if "/" in name:
        raise PackageError(f"{field}.fileName must be a nonempty basename")
    _positive_int(value["bytes"], f"{field}.bytes", maximum)
    _hash(value["sha256"], f"{field}.sha256")


def _portable_path(value, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise PackageError(f"{field} must be a nonempty path of at most 1024 characters")
    if value.startswith("/") or any(
        char in '\\:\\<>"|?*' or char == "\0" or ord(char) < 32 or ord(char) == 127
        for char in value
    ):
        raise PackageError(f"{field} contains a forbidden path character")
    components = value.split("/")
    for component in components:
        if component in ("", ".", "..") or component.endswith((".", " ")):
            raise PackageError(f"{field} contains a nonportable path component")
        if component.split(".", 1)[0].upper() in _RESERVED:
            raise PackageError(f"{field} contains a reserved Windows basename")
    return value


def _manifest_paths(package: dict, files_by_path: dict) -> None:
    models = package["supportedModels"]
    manifests = package["modelManifests"]
    if not isinstance(manifests, dict) or set(manifests) != set(models):
        raise PackageError("modelManifests must have exactly the supportedModels keys")
    recovery = package["recoveryManifests"]
    if not isinstance(recovery, dict) or not set(recovery).issubset(models):
        raise PackageError("recoveryManifests must be an object keyed by a subset of supportedModels")
    model_paths = list(manifests.values())
    recovery_paths = list(recovery.values())
    for field, paths in (("modelManifests", model_paths), ("recoveryManifests", recovery_paths)):
        for model, path in (manifests if field == "modelManifests" else recovery).items():
            if not isinstance(path, str) or path not in files_by_path:
                raise PackageError(f"{field}.{model} must reference an indexed file")
            if files_by_path[path]["role"] != "manifest":
                raise PackageError(f"{field}.{model} must reference a manifest file")
        if len(set(paths)) != len(paths):
            raise PackageError(f"{field} paths must be distinct")
    if set(model_paths) & set(recovery_paths):
        raise PackageError("model and recovery manifest paths must be distinct")
    # Recovery manifests feed the RAM rescue path; a normal-Linux offline upgrade
    # does not need them, so an absent or partial object stays valid and every
    # present entry is still checked above.


def _decky_snapshot(value) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != _DECKY_KEYS:
        raise PackageError("deckySnapshot must be null or contain exactly the snapshot fields")
    for field in ("releaseId", "assetId", "size"):
        _positive_int(value[field], f"deckySnapshot.{field}")
    for field in ("tag", "publishedAt", "url"):
        if not isinstance(value[field], str) or not value[field]:
            raise PackageError(f"deckySnapshot.{field} must be a nonempty string")
    _hash(value["sha256"], "deckySnapshot.sha256")


def _validate(data: dict) -> None:
    if not isinstance(data, dict) or set(data) != _TOP_KEYS:
        raise PackageError("package must contain exactly the schema 1 top-level keys")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        raise PackageError("schemaVersion must be integer 1")
    if (data["kind"] != "freedom-universal-package"
            or data["product"] != "armadaos-freedom" or data["architecture"] != "aarch64"):
        raise PackageError("package kind, product, or architecture is invalid")
    _identity(data["releaseId"], "releaseId")
    _identity(data["version"], "version")
    if not isinstance(data["sourceRevision"], str) or not _HEX40.fullmatch(data["sourceRevision"]):
        raise PackageError("sourceRevision must be 40 lowercase hexadecimal characters")
    digest = data["ociDigest"]
    if not isinstance(digest, str) or not digest.startswith("sha256:") or not _HEX64.fullmatch(digest[7:]):
        raise PackageError("ociDigest must be sha256 followed by 64 lowercase hexadecimal characters")
    _hash(data["ostreeCommit"], "ostreeCommit")
    _updater_tuple(data["minimumInstallerVersion"], "minimumInstallerVersion")
    _updater_tuple(data["minimumUpdaterVersion"], "minimumUpdaterVersion")
    for field, allowed in (("supportedModels", _MODELS), ("operations", _OPERATIONS)):
        values = data[field]
        if not isinstance(values, list) or not values or any(
            type(value) is not str or value not in allowed for value in values
        ):
            raise PackageError(f"{field} must be a nonempty list of supported values")
        if len(set(values)) != len(values):
            raise PackageError(f"{field} must not contain duplicates")

    archive = data["archive"]
    _archive_member(archive, "archive", None)
    if not archive["fileName"].endswith(".zip"):
        raise PackageError("archive.fileName must end in .zip")
    parts = data["parts"]
    if not isinstance(parts, list) or len(parts) > 999:
        raise PackageError("parts must be empty or contain 1 through 999 entries")
    for index, part in enumerate(parts, 1):
        _archive_member(part, f"parts[{index - 1}]", _MAX_PART_BYTES)
        expected = f"{archive['fileName']}.{index:03d}"
        if part["fileName"] != expected:
            raise PackageError(f"parts[{index - 1}].fileName must be {expected}")
    if parts and sum(part["bytes"] for part in parts) != archive["bytes"]:
        raise PackageError("part sizes must sum exactly to archive.bytes")

    records = data["files"]
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        raise PackageError("files must contain 1 through 10000 records")
    by_path = {}
    folded = set()
    roles = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _FILE_KEYS:
            raise PackageError(f"files[{index}] has invalid fields")
        path = _portable_path(record["path"], f"files[{index}].path")
        if path.casefold() in folded:
            raise PackageError("file paths must be unique case-insensitively")
        folded.add(path.casefold())
        _positive_int(record["bytes"], f"{path}.bytes", _MAX_FILE_BYTES)
        _positive_int(record["expandedBytes"], f"{path}.expandedBytes", _MAX_FILE_BYTES)
        _hash(record["sha256"], f"{path}.sha256")
        _hash(record["expandedSha256"], f"{path}.expandedSha256")
        compression = record["compression"]
        if compression not in ("none", "gzip"):
            raise PackageError(f"{path}.compression must be none or gzip")
        if compression == "none" and (
            record["bytes"] != record["expandedBytes"]
            or record["sha256"] != record["expandedSha256"]
        ):
            raise PackageError(f"{path} uncompressed size and digest must match")

        if record["role"] not in _ROLES:
            raise PackageError(f"{path} has an unsupported role")
        by_path[path] = record
        roles[record["role"]] = roles.get(record["role"], 0) + 1
    for path in by_path:
        components = path.split("/")
        for end in range(1, len(components)):
            if "/".join(components[:end]).casefold() in folded:
                raise PackageError("file paths must not collide with parent files")
    if roles.get("linux-root") != 1 or roles.get("linux-boot") != 1:
        raise PackageError("exactly one linux-root and one linux-boot record are required")
    bundle = by_path.get("bundle.json")
    if bundle is None or bundle["role"] != "manifest":
        raise PackageError("bundle.json must be an indexed manifest")
    _manifest_paths(data, by_path)
    if "offline-upgrade" in data["operations"] and any(
        by_path[path]["compression"] != "gzip"
        for path, record in by_path.items()
        if record["role"] in ("linux-root", "linux-boot")
    ):
        raise PackageError("offline-upgrade requires gzip root and boot images")
    _decky_snapshot(data["deckySnapshot"])
    if "decky-repair" in data["operations"] and data["deckySnapshot"] is None:
        raise PackageError("decky-repair requires deckySnapshot metadata")


def validate_package(data: dict) -> dict:
    """Validate descriptor metadata and return an independent copy."""
    try:
        _validate(data)
    except PackageError:
        raise
    except (CatalogError, TypeError, ValueError) as exc:
        raise PackageError(str(exc)) from exc
    return deepcopy(data)


def require_operation(package: dict, model: str, operation: str, *,
                      installer_version: str | None = None,
                      updater_version: str | None = None) -> None:
    """Check declared operation compatibility; this is not a readiness claim."""
    validated = validate_package(package)
    if model not in validated["supportedModels"]:
        raise PackageError("model is not supported by this package")
    if operation not in validated["operations"]:
        raise PackageError("operation is not declared by this package")
    if installer_version is None and updater_version is None:
        raise PackageError("at least one installer or updater version is required")
    for label, field, current in (
        ("installer_version", "minimumInstallerVersion", installer_version),
        ("updater_version", "minimumUpdaterVersion", updater_version),
    ):
        if current is not None:
            try:
                caller_ver = _updater_tuple(current, label)
            except CatalogError as exc:
                raise PackageError(str(exc)) from exc
            if caller_ver < _updater_tuple(validated[field], field):
                raise PackageError(f"version is older than {field}")
