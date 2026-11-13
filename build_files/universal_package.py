"""Build a universal package descriptor from existing media."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates.package_contract import (
    AUTHORIZED_MODELS, OFFLINE_UPGRADE_MODELS, PackageError, _portable_path,
    validate_package,
)
from updates.package_media import _check_unchanged, _open_regular, verify_archive, verify_parts
from updates.recovery_manifest import validate_recovery_manifest

_METADATA_LIMIT = 2 * 1024 * 1024
_CHUNK = 1024 * 1024
_ROLE_MAP = {
    "linux-root-raw": "linux-root",
    "linux-boot-raw": "linux-boot",
    "kernel": "kernel",
    "initramfs": "initramfs",
    "runtime": "runtime",
    "dtb-normal": "dtb-normal",
    "dtb-diagnostic": "dtb-diagnostic",
}
_COMPRESSED = frozenset(("linux-root", "linux-boot"))
_REQUIRED = frozenset(_ROLE_MAP.values())
_HEX = frozenset("0123456789abcdef")


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


def _json_bytes(data, source):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_reject_constant, parse_float=_reject_float)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PackageError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{source} must contain a JSON object")
    return value


def _read_metadata(stream, source):
    data = stream.read(_METADATA_LIMIT + 1)
    if len(data) > _METADATA_LIMIT:
        raise PackageError(f"{source} exceeds the metadata size limit")
    return _json_bytes(data, source)


def _digest(stream, limit=None):
    if limit is not None and limit <= 0:
        raise PackageError("file grew while hashing")
    digest = hashlib.sha256()
    size = 0
    while True:
        to_read = _CHUNK if limit is None else min(_CHUNK, limit - size)
        if to_read <= 0:
            raise PackageError("file grew while hashing")
        block = stream.read(to_read)
        if not block:
            break
        size += len(block)
        if limit is not None and size >= limit:
            raise PackageError("file grew while hashing")
        digest.update(block)
    return size, digest.hexdigest()


def _file_digest(path):
    try:
        f, st = _open_regular(path)
        with f:
            result = _digest(f, limit=st.st_size + 1)
            _check_unchanged(f, st, str(path))
            return result
    except OSError as exc:
        raise PackageError(f"cannot read {path}: {exc}") from exc


def _hash(value, field):
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise PackageError(f"{field} must be a lowercase SHA-256 digest")


def _positive(value, field):
    if type(value) is not int or value <= 0:
        raise PackageError(f"{field} must be a positive integer")


def _members(zf):
    members = {}
    folded = set()
    for info in zf.infolist():
        name = info.filename
        if info.is_dir() or name.endswith("/"):
            raise PackageError(f"archive directory entry is not allowed: {name}")
        if info.compress_type != zipfile.ZIP_STORED:
            raise PackageError(f"archive member is not stored: {name}")
        if info.flag_bits & 1:
            raise PackageError(f"encrypted archive member is not allowed: {name}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise PackageError(f"non-regular archive member is not allowed: {name}")
        _portable_path(name, "archive member")
        key = name.casefold()
        if name in members or key in folded:
            raise PackageError(f"duplicate or case-colliding archive member: {name}")
        folded.add(key)
        members[name] = info
    return members


def _member_json(zf, members, path):
    info = members.get(path)
    if info is None:
        raise PackageError(f"missing archive metadata member: {path}")
    if info.file_size > _METADATA_LIMIT:
        raise PackageError(f"{path} exceeds the metadata size limit")
    with zf.open(info) as stream:
        return _read_metadata(stream, path)


def _artifact(item, model, shared, directory):
    if not isinstance(item, dict):
        raise PackageError(f"{model} artifact must be an object")
    for field in ("role", "file", "bytes", "sha256"):
        if field not in item:
            raise PackageError(f"{model} artifact is missing {field}")
    if not isinstance(item["role"], str) or item["role"] not in _ROLE_MAP:
        raise PackageError(f"{model} has unknown artifact role {item['role']!r}")
    role = _ROLE_MAP[item["role"]]
    logical = _portable_path(item["file"], f"{model} artifact file")
    path = shared.get(logical, directory + "/" + logical)
    _positive(item["bytes"], f"{path}.bytes")
    _hash(item["sha256"], f"{path}.sha256")
    record = {
        "path": path, "bytes": item["bytes"], "sha256": item["sha256"],
        "expandedBytes": item["bytes"], "expandedSha256": item["sha256"],
        "compression": "none", "role": role,
    }
    if role in _COMPRESSED:
        if item.get("compression") != "gzip":
            raise PackageError(f"{path} must declare gzip compression")
        _positive(item.get("expandedBytes"), f"{path}.expandedBytes")
        _hash(item.get("expandedSha256"), f"{path}.expandedSha256")
        record.update(compression="gzip", expandedBytes=item["expandedBytes"],
                      expandedSha256=item["expandedSha256"])
    elif item.get("compression", "none") != "none" or any(
            item.get(field) is not None for field in ("expandedBytes", "expandedSha256")):
        raise PackageError(f"{path} must not declare expanded compression metadata")
    return record


def build_descriptor(archive_path: Path, *, release_id: str,
                     minimum_installer_version="0.8.0",
                     minimum_updater_version="0.8.0",
                     parts_manifest: Path | None = None,
                     enable_offline_upgrade: bool = False,
                     install_models=()) -> dict:
    if type(enable_offline_upgrade) is not bool:
        raise PackageError("enable_offline_upgrade must be a boolean")
    if isinstance(install_models, (str, bytes)):
        raise PackageError("install_models must be a sequence of model identifiers")
    try:
        install_models = tuple(install_models)
    except TypeError as exc:
        raise PackageError(
            "install_models must be a sequence of model identifiers"
        ) from exc
    for model in install_models:
        if not isinstance(model, str):
            raise PackageError("install_models entries must be model identifiers")
    archive_path = Path(archive_path)
    archive_size, archive_sha = _file_digest(archive_path)
    try:
        with zipfile.ZipFile(archive_path) as zf:
            members = _members(zf)
            bundle = _member_json(zf, members, "bundle.json")
            if (bundle.get("schemaVersion") != 2
                    or bundle.get("bundleId") != "armadaos-freedom"
                    or not isinstance(bundle.get("version"), str)):
                raise PackageError("invalid bundle schema, identity, or version")
            if "offlineUpgrade" in bundle and type(bundle["offlineUpgrade"]) is not bool:
                raise PackageError("bundle.offlineUpgrade must be a boolean")
            offline_upgrade_capable = bundle.get("offlineUpgrade") is True
            packages = bundle.get("packages")
            if not isinstance(packages, list) or not packages:
                raise PackageError("bundle.packages must be nonempty")
            models, manifests, overrides = [], {}, {}
            model_install_artifacts = {}
            common_paths = {role: set() for role in _COMPRESSED | {"kernel", "initramfs"}}
            source_revision = deployment_commit = manifest_digest = None
            for entry in packages:
                if not isinstance(entry, dict):
                    raise PackageError("bundle package entry must be an object")
                model = entry.get("deviceModel")
                directory = _portable_path(entry.get("directory"), "package directory")
                if not isinstance(model, str) or not model or model in models:
                    raise PackageError("package deviceModel is invalid or duplicated")
                shared = {}
                destinations = set()
                shared_files = entry.get("sharedFiles", [])
                if not isinstance(shared_files, list):
                    raise PackageError("sharedFiles must be a list")
                for item in shared_files:
                    if not isinstance(item, dict) or set(item) != {"source", "destination"}:
                        raise PackageError("invalid sharedFiles entry")
                    source = _portable_path(item["source"], "shared source")
                    destination = _portable_path(item["destination"], "shared destination")
                    if destination.casefold() in destinations:
                        raise PackageError("duplicate shared destination")
                    destinations.add(destination.casefold())
                    shared[destination] = source
                manifest_path = directory + "/package.json"
                document = _member_json(zf, members, manifest_path)
                deployment = document.get("deployment")
                if (document.get("schemaVersion") != 2
                        or document.get("deviceModel") != model
                        or document.get("version") != bundle["version"]
                        or document.get("testOnly") is not False
                        or not isinstance(document.get("sourceCommit"), str)
                        or not isinstance(deployment, dict)
                        or deployment.get("kind") != "edl-raw-v1"
                        or not isinstance(document.get("installTargets"), list)
                        or "internal" not in document["installTargets"]):
                    raise PackageError(f"invalid model manifest for {model}")
                revision = document["sourceCommit"]
                commit = deployment.get("commit")
                digest = deployment.get("manifestDigest")
                if not isinstance(commit, str) or not isinstance(digest, str):
                    raise PackageError(f"invalid deployment identity for {model}")
                if models and (revision, commit, digest) != (
                        source_revision, deployment_commit, manifest_digest):
                    raise PackageError("model source and deployment identities differ")
                source_revision, deployment_commit, manifest_digest = revision, commit, digest
                artifacts = document.get("artifacts")
                if not isinstance(artifacts, list):
                    raise PackageError(f"{model}.artifacts must be a list")
                roles = set()
                model_install_artifacts[model] = {}
                for item in artifacts:
                    record = _artifact(item, model, shared, directory)
                    if record["role"] in roles:
                        raise PackageError(f"duplicate artifact role for {model}")
                    roles.add(record["role"])
                    model_install_artifacts[model][record["role"]] = record["path"]
                    if record["role"] in common_paths:
                        common_paths[record["role"]].add(record["path"])
                    if record["path"] in overrides and overrides[record["path"]] != record:
                        raise PackageError(f"conflicting artifact record: {record['path']}")
                    overrides[record["path"]] = record
                if roles != _REQUIRED:
                    raise PackageError(f"{model} must declare exactly the seven required roles")
                models.append(model)
                manifests[model] = manifest_path
            if any(len(paths) != 1 for paths in common_paths.values()):
                raise PackageError("root, boot, kernel, and initramfs must each be shared")

            custom_models = tuple(
                model for model in models if model not in AUTHORIZED_MODELS
            )
            if len(set(install_models)) != len(install_models):
                raise PackageError("install_models must not contain duplicates")
            if set(install_models) != set(custom_models):
                raise PackageError(
                    "install_models must name exactly the non-default package models"
                )
            upgrade_models = tuple(
                model for model in models if model not in OFFLINE_UPGRADE_MODELS
            )
            if enable_offline_upgrade and upgrade_models:
                raise PackageError(
                    "offline-upgrade is restricted to the models with the dual-slot "
                    "backend; build this package install-only"
                )

            if "recoveryManifests" in bundle and not isinstance(bundle["recoveryManifests"], dict):
                raise PackageError("bundle.recoveryManifests must be an object")
            bundle_recovery = bundle.get("recoveryManifests", {})
            recovery_manifests = {}
            recovery_initramfs_paths = set()
            recovery_dtb_paths = set()
            raw_recovery_docs = {}
            if bundle_recovery:
                if not set(bundle_recovery) <= set(models):
                    raise PackageError("bundle.recoveryManifests must be a subset of the selected models")
                seen_rec_paths = set()
                for model, rpath in bundle_recovery.items():
                    if not isinstance(rpath, str):
                        raise PackageError(f"recoveryManifest path for {model} must be a string")
                    norm_path = _portable_path(rpath, f"recovery manifest for {model}")
                    if norm_path.casefold() in seen_rec_paths:
                        raise PackageError("duplicate recoveryManifest path")
                    seen_rec_paths.add(norm_path.casefold())
                    info = members.get(norm_path)
                    if info is None:
                        raise PackageError(f"missing recovery manifest member: {norm_path}")
                    if info.file_size > 65536:
                        raise PackageError(f"{norm_path} exceeds the 64KiB recovery manifest limit")
                    with zf.open(info) as stream:
                        doc_bytes = stream.read(65537)
                    if len(doc_bytes) > 65536:
                        raise PackageError(f"{norm_path} exceeds the 64KiB recovery manifest limit")
                    raw_recovery_docs[model] = (norm_path, doc_bytes)
                    recovery_manifests[model] = norm_path

            if raw_recovery_docs:
                for model, (norm_path, doc_bytes) in raw_recovery_docs.items():
                    rec_doc = _json_bytes(doc_bytes, norm_path)
                    artifacts_map = rec_doc.get("artifacts")
                    if not isinstance(artifacts_map, dict):
                        raise PackageError(f"{norm_path} artifacts must be an object")
                    for art_key in ("kernel", "initramfs", "dtb", "runtime"):
                        if art_key not in artifacts_map:
                            raise PackageError(f"{norm_path} missing artifact {art_key}")
                        _portable_path(artifacts_map[art_key], f"{art_key} path in {norm_path}")
                    rec_initramfs = artifacts_map["initramfs"]
                    rec_dtb = artifacts_map["dtb"]
                    recovery_initramfs_paths.add(rec_initramfs)
                    if rec_dtb.casefold() in {p.casefold() for p in recovery_dtb_paths}:
                        raise PackageError(f"recovery dtb collision: {rec_dtb}")
                    recovery_dtb_paths.add(rec_dtb)

            recovery_path_overlap = recovery_dtb_paths & recovery_initramfs_paths
            if recovery_path_overlap:
                raise PackageError(
                    f"recovery dtb and initramfs paths overlap: {sorted(recovery_path_overlap)[0]}"
                )

            install_paths = set(overrides)
            all_manifest_paths = {"bundle.json", *manifests.values(), *recovery_manifests.values()}
            if set(manifests.values()) & set(recovery_manifests.values()):
                raise PackageError("modelManifests and recoveryManifests must not overlap")
            for dtb_path in recovery_dtb_paths:
                if dtb_path in install_paths or dtb_path in all_manifest_paths:
                    raise PackageError(f"recovery dtb collides with install artifact or manifest: {dtb_path}")
            for init_path in recovery_initramfs_paths:
                if init_path in all_manifest_paths:
                    raise PackageError(f"recovery initramfs collides with manifest: {init_path}")

            actual = {}
            for name, info in members.items():
                with zf.open(info) as stream:
                    size, sha = _digest(stream)
                actual[name] = (size, sha)
            for record in overrides.values():
                if actual.get(record["path"]) != (record["bytes"], record["sha256"]):
                    raise PackageError(f"artifact size or digest mismatch: {record['path']}")

            files = []
            for name in members:
                if name in overrides:
                    files.append(deepcopy(overrides[name]))
                else:
                    size, sha = actual[name]
                    if name in all_manifest_paths:
                        role = "manifest"
                    elif name in recovery_initramfs_paths or name in recovery_dtb_paths:
                        role = "recovery"
                    else:
                        role = "metadata"
                    files.append({"path": name, "bytes": size, "sha256": sha,
                                  "expandedBytes": size, "expandedSha256": sha,
                                  "compression": "none",
                                  "role": role})
    except PackageError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise PackageError(f"cannot inspect archive: {exc}") from exc
    parts = []
    if parts_manifest is not None:
        parts_manifest = Path(parts_manifest)
        try:
            with parts_manifest.open("rb") as stream:
                split = _read_metadata(stream, str(parts_manifest))
        except OSError as exc:
            raise PackageError(f"cannot read parts manifest: {exc}") from exc
        if (type(split.get("schemaVersion")) is not int
                or split.get("schemaVersion") != 1 or split.get("format") != "raw-split"):
            raise PackageError("invalid raw-split manifest")
        expected = {"fileName": archive_path.name, "bytes": archive_size, "sha256": archive_sha}
        if (split.get("archive") != expected or not isinstance(split.get("parts"), list)
                or not split["parts"]):
            raise PackageError("raw-split archive identity does not match")
        parts = deepcopy(split["parts"])

    operations = ["install"]
    if enable_offline_upgrade:
        if not offline_upgrade_capable:
            raise PackageError(
                "offline-upgrade requires a bundle built from an image with the normal updater"
            )
        operations.append("offline-upgrade")

    descriptor = {
        "schemaVersion": 1, "kind": "freedom-universal-package",
        "product": "armadaos-freedom", "architecture": "aarch64",
        "releaseId": release_id, "version": bundle["version"],
        "sourceRevision": source_revision, "ociDigest": manifest_digest,
        "ostreeCommit": deployment_commit,
        "minimumInstallerVersion": minimum_installer_version,
        "minimumUpdaterVersion": minimum_updater_version,
        "supportedModels": models, "operations": operations,
        "archive": {"fileName": archive_path.name, "bytes": archive_size,
                    "sha256": archive_sha},
        "parts": parts, "files": files, "modelManifests": manifests,
        "recoveryManifests": recovery_manifests, "deckySnapshot": None,
    }
    result = validate_package(descriptor, install_models=install_models)
    if raw_recovery_docs:
        for model, (norm_path, doc_bytes) in raw_recovery_docs.items():
            rec_doc = validate_recovery_manifest(doc_bytes, result, model)
            artifacts = rec_doc["artifacts"]
            if artifacts["kernel"] != model_install_artifacts[model]["kernel"]:
                raise PackageError(f"{model} recovery kernel does not match install kernel")
            if artifacts["runtime"] != model_install_artifacts[model]["runtime"]:
                raise PackageError(f"{model} recovery runtime does not match install runtime")
    verify_archive(archive_path, result, verify_expanded=True,
                   install_models=install_models)
    if parts_manifest is not None:
        verify_parts(parts_manifest.parent, result, install_models=install_models)
    return result
