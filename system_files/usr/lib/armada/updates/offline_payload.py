"""Internal offline compressed payload expansion and repository import stage."""
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import zlib

from .deployment_store import DeploymentError, _require_nonsymlink_dir
from .image_source import ImageSourceError, _paths
from .offline_image import _same_stat, import_offline_image
from .offline_source import OfflineSourceError, _CHANNELS, _validate_source_location
from .package_contract import PackageError, load_package, require_operation
from .package_media import _CHUNK, _open_regular, _sha256_stream
from .signature import DEFAULT_TRUSTED_KEY


def _select_payload_entry(package):
    matches = [
        entry
        for entry in package.get("files", ())
        if isinstance(entry, dict) and entry.get("role") == "linux-root"
    ]
    if len(matches) != 1:
        raise OfflineSourceError(
            f"expected exactly one linux-root entry, found {len(matches)}"
        )
    entry = matches[0]
    if entry.get("compression") != "gzip":
        raise OfflineSourceError(
            f"linux-root compression must be 'gzip', got {entry.get('compression')!r}"
        )
    rel_path = entry.get("path")
    size = entry.get("bytes")
    sha256 = entry.get("sha256")
    exp_bytes = entry.get("expandedBytes")
    exp_sha = entry.get("expandedSha256")
    if (
        type(rel_path) is not str
        or not rel_path
        or type(size) is not int
        or size <= 0
        or type(sha256) is not str
        or not sha256
        or type(exp_bytes) is not int
        or exp_bytes <= 0
        or type(exp_sha) is not str
        or not exp_sha
    ):
        raise OfflineSourceError("linux-root entry has invalid metadata")
    posix = PurePosixPath(rel_path)
    if posix.is_absolute() or "\\" in rel_path or not posix.parts:
        raise OfflineSourceError(f"invalid linux-root relative path: {rel_path!r}")
    for part in posix.parts:
        if part in ("", ".", ".."):
            raise OfflineSourceError(f"invalid linux-root path segment: {rel_path!r}")
    return posix.parts, size, sha256, exp_bytes, exp_sha


def _validate_and_resolve_paths(payload_directory, parts, sysroot_path, work_dir, exp_bytes):
    if not isinstance(payload_directory, (str, Path)):
        raise OfflineSourceError("payload_directory must be a path")
    if not isinstance(sysroot_path, (str, Path)) or not isinstance(work_dir, (str, Path)):
        raise OfflineSourceError("sysroot_path and work_dir must be paths")
    payload_dir, root, work = Path(payload_directory), Path(sysroot_path), Path(work_dir)
    if not payload_dir.is_absolute() or not root.is_absolute() or not work.is_absolute():
        raise OfflineSourceError(
            "payload_directory, sysroot_path, and work_dir must be absolute"
        )
    try:
        _require_nonsymlink_dir(payload_dir, *parts[:-1])
        _require_nonsymlink_dir(root)
        _require_nonsymlink_dir(root, "ostree", "repo")
        _require_nonsymlink_dir(work.parent)
    except DeploymentError as exc:
        raise OfflineSourceError(str(exc)) from exc

    payload_path = payload_dir.joinpath(*parts)
    if payload_path.is_symlink():
        raise OfflineSourceError(f"symlinked payload refused: {payload_path}")
    if work.is_symlink() or work.exists():
        raise OfflineSourceError(f"work_dir must be a new leaf: {work}")

    for source in (payload_dir, payload_path):
        _validate_source_location(source, root)

    for protected in (root, root / "ostree", root / "boot"):
        if work == protected or work in protected.parents:
            raise OfflineSourceError(f"work_dir may not overlap with {protected}")

    for src in (payload_dir, payload_path):
        if work == src or src in work.parents or work in src.parents:
            raise OfflineSourceError(f"work_dir may not overlap with payload source {src}")

    try:
        vfs = os.statvfs(work.parent)
    except OSError as exc:
        raise OfflineSourceError(f"cannot check free space on {work.parent}: {exc}") from exc
    free_bytes = vfs.f_bavail * vfs.f_frsize
    # The expanded image and the imported repository it is read from both
    # live under work_dir at the same time, so preflight both copies before
    # creating either of them.
    required_bytes = exp_bytes * 2
    if free_bytes < required_bytes:
        raise OfflineSourceError(
            f"insufficient free space in {work.parent}: need {required_bytes} bytes "
            f"for the expanded image and repository, have {free_bytes}"
        )
    return payload_path, root, work


def _expand_verified_gzip(f, expanded_path, exp_bytes, exp_sha):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        out_fd = os.open(str(expanded_path), flags, 0o600)
    except OSError as exc:
        raise OfflineSourceError(f"cannot create expanded image {expanded_path}: {exc}") from exc
    try:
        with os.fdopen(out_fd, "wb", closefd=True) as out_f:
            f.seek(0)
            hasher = hashlib.sha256()
            total = 0
            with gzip.GzipFile(fileobj=f, mode="rb") as gz:
                while True:
                    remaining = (exp_bytes + 1) - total
                    n = _CHUNK if remaining > _CHUNK else remaining
                    chunk = gz.read(n) if n > 0 else b""
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > exp_bytes:
                        raise OfflineSourceError(
                            f"expanded payload exceeds declared {exp_bytes} bytes"
                        )
                    hasher.update(chunk)
                    out_f.write(chunk)
            if total != exp_bytes or hasher.hexdigest() != exp_sha:
                raise OfflineSourceError("expanded payload sha256/size mismatch")
            out_f.flush()
            os.fsync(out_f.fileno())
    except OfflineSourceError:
        raise
    except (OSError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
        raise OfflineSourceError(f"failed expanding payload to {expanded_path}: {exc}") from exc


def import_offline_payload(
    *,
    document,
    signature_b64,
    payload_directory,
    expected_channel,
    model,
    updater_version,
    sysroot_path,
    work_dir,
    trusted_key=DEFAULT_TRUSTED_KEY,
):
    """Verify a staged gzip linux-root payload, expand it, and import its OSTree commit."""
    if type(expected_channel) is not str or expected_channel not in _CHANNELS:
        raise OfflineSourceError(f"invalid expected_channel: {expected_channel!r}")
    if type(model) is not str or type(updater_version) is not str:
        raise OfflineSourceError("model and updater_version must be strings")
    try:
        package = load_package(document, signature_b64, trusted_key=trusted_key)
        require_operation(
            package,
            model,
            "offline-upgrade",
            updater_version=updater_version,
        )
    except (PackageError, TypeError, ValueError) as exc:
        raise OfflineSourceError(str(exc)) from exc

    parts, comp_bytes, comp_sha, exp_bytes, exp_sha = _select_payload_entry(package)
    payload_path, root, work = _validate_and_resolve_paths(
        payload_directory, parts, sysroot_path, work_dir, exp_bytes
    )

    try:
        f, st_before = _open_regular(payload_path)
    except PackageError as exc:
        raise OfflineSourceError(str(exc)) from exc

    try:
        try:
            if st_before.st_size != comp_bytes:
                raise OfflineSourceError(
                    f"payload size {st_before.st_size} != declared {comp_bytes}"
                )
            digest, total = _sha256_stream(f, limit=comp_bytes + 1)
            if total != comp_bytes or digest != comp_sha:
                raise OfflineSourceError("payload compressed sha256/size mismatch")
            st_hashed = os.fstat(f.fileno())
        except OSError as exc:
            raise OfflineSourceError(f"failed reading payload {payload_path}: {exc}") from exc
        if not _same_stat(st_before, st_hashed):
            raise OfflineSourceError("payload changed during verification")

        try:
            root, work = _paths(root, work)
        except (DeploymentError, ImageSourceError) as exc:
            raise OfflineSourceError(str(exc)) from exc

        expanded_image = work / "linux-root.img"
        _expand_verified_gzip(f, expanded_image, exp_bytes, exp_sha)

        try:
            st_expanded = os.fstat(f.fileno())
        except OSError as exc:
            raise OfflineSourceError(f"failed stating payload {payload_path}: {exc}") from exc
        if not _same_stat(st_before, st_expanded):
            raise OfflineSourceError("payload changed during expansion")
    finally:
        f.close()

    try:
        f_check, st_after = _open_regular(payload_path)
    except PackageError as exc:
        raise OfflineSourceError(str(exc)) from exc
    f_check.close()
    if not _same_stat(st_before, st_after):
        raise OfflineSourceError("payload identity changed after expansion")

    imported = import_offline_image(
        document=document,
        signature_b64=signature_b64,
        root_image=expanded_image,
        expected_channel=expected_channel,
        model=model,
        updater_version=updater_version,
        sysroot_path=root,
        work_dir=work / "import",
        trusted_key=trusted_key,
    )
    result = dict(imported)
    result["payloadPath"] = str(payload_path)
    result["expandedImage"] = str(expanded_image)
    return result
