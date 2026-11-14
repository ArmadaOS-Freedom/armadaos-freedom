"""Internal offline root-image exposure stage for authenticated repository import.

This module accepts an already-expanded Btrfs root filesystem image produced
from a universal update package, verifies its byte length and SHA-256 against
the authenticated package descriptor's single ``linux-root`` entry, extracts
only ``/root/ostree/repo`` using ``btrfs restore`` without mounting or loop
devices, and delegates commit verification and pull-local import to
``import_offline_repository``.  Archive assembly, gzip expansion, deployment,
and activation are separate responsibilities.
"""
import os
from pathlib import Path

from .deployment_store import DeploymentError, _require_nonsymlink_dir
from .image_source import ImageSourceError, _paths, _run_command
from .offline_source import (
    OfflineSourceError,
    _CHANNELS,
    _validate_source_location,
    import_offline_repository,
)
from .package_contract import PackageError, load_package, require_operation
from .package_media import _open_regular, _sha256_stream
from .signature import DEFAULT_TRUSTED_KEY

_RESTORE_REGEX = r"^/(|root(|/ostree(|/repo(|/.*))))$"


def _same_stat(a, b):
    return (
        a.st_dev == b.st_dev
        and a.st_ino == b.st_ino
        and a.st_size == b.st_size
        and a.st_mtime_ns == b.st_mtime_ns
    )


def _select_root_entry(package):
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
    exp_bytes = entry.get("expandedBytes")
    exp_sha = entry.get("expandedSha256")
    if (
        type(exp_bytes) is not int
        or exp_bytes <= 0
        or type(exp_sha) is not str
        or not exp_sha
    ):
        raise OfflineSourceError(
            "linux-root entry missing valid expandedBytes/expandedSha256"
        )
    return exp_bytes, exp_sha


def _validate_image_and_paths(root_image, sysroot_path, work_dir):
    if not isinstance(root_image, (str, Path)):
        raise OfflineSourceError("root_image must be a path")
    if not isinstance(sysroot_path, (str, Path)) or not isinstance(work_dir, (str, Path)):
        raise OfflineSourceError("sysroot_path and work_dir must be paths")
    image, root, work = Path(root_image), Path(sysroot_path), Path(work_dir)
    if not image.is_absolute() or not root.is_absolute() or not work.is_absolute():
        raise OfflineSourceError(
            "root_image, sysroot_path, and work_dir must be absolute"
        )
    if image.name in ("", ".", ".."):
        raise OfflineSourceError(f"invalid root_image path: {image}")
    try:
        _require_nonsymlink_dir(image.parent)
        _require_nonsymlink_dir(root)
        _require_nonsymlink_dir(root, "ostree", "repo")
    except DeploymentError as exc:
        raise OfflineSourceError(str(exc)) from exc
    if image.is_symlink():
        raise OfflineSourceError(f"symlinked path refused: {image}")
    _validate_source_location(image, root)
    if image == work or work in image.parents or image in work.parents:
        raise OfflineSourceError(f"work_dir may not overlap with root_image {image}")
    try:
        root, work = _paths(root, work)
    except (DeploymentError, ImageSourceError) as exc:
        raise OfflineSourceError(str(exc)) from exc
    return image, root, work


def _verify_expanded_image(image_path, expected_bytes, expected_sha256):
    try:
        f, st_before = _open_regular(image_path)
    except PackageError as exc:
        raise OfflineSourceError(str(exc)) from exc
    try:
        if st_before.st_size != expected_bytes:
            raise OfflineSourceError(
                f"root_image size {st_before.st_size} != declared {expected_bytes}"
            )
        digest, total = _sha256_stream(f, limit=expected_bytes + 1)
        if total != expected_bytes or digest != expected_sha256:
            raise OfflineSourceError("root_image expanded sha256/size mismatch")
        st_fd = os.fstat(f.fileno())
    except OSError as exc:
        raise OfflineSourceError(f"failed reading root_image {image_path}: {exc}") from exc
    finally:
        f.close()
    if not _same_stat(st_before, st_fd):
        raise OfflineSourceError("root_image changed during verification")
    _verify_image_identity(image_path, st_before)
    return st_before


def _verify_image_identity(image_path, expected_stat):
    try:
        f, st_now = _open_regular(image_path)
    except PackageError as exc:
        raise OfflineSourceError(str(exc)) from exc
    f.close()
    if not _same_stat(expected_stat, st_now):
        raise OfflineSourceError("root_image changed during offline import")


def import_offline_image(
    *,
    document,
    signature_b64,
    root_image,
    expected_channel,
    model,
    updater_version,
    sysroot_path,
    work_dir,
    trusted_key=DEFAULT_TRUSTED_KEY,
):
    """Verify an expanded Btrfs root image, stage its OSTree repo, and import it."""
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

    expected_bytes, expected_sha256 = _select_root_entry(package)
    image, root, work = _validate_image_and_paths(root_image, sysroot_path, work_dir)
    st_verified = _verify_expanded_image(image, expected_bytes, expected_sha256)

    content_dir = work / "content"
    try:
        content_dir.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise OfflineSourceError(f"cannot create staging directory {content_dir}: {exc}") from exc

    try:
        extract_log = _run_command(
            [
                "/usr/bin/btrfs",
                "restore",
                "-r",
                "5",
                "-x",
                "-m",
                "-S",
                "--path-regex",
                _RESTORE_REGEX,
                str(image),
                str(content_dir),
            ],
            work / "extract.log",
        )
    except ImageSourceError as exc:
        raise OfflineSourceError(str(exc)) from exc

    _verify_image_identity(image, st_verified)

    staged_repo = work / "content" / "root" / "ostree" / "repo"
    try:
        _require_nonsymlink_dir(
            work, "content", "root", "ostree", "repo"
        )
    except DeploymentError as exc:
        raise OfflineSourceError(
            f"extracted repository missing or invalid: {exc}"
        ) from exc

    imported = import_offline_repository(
        document=document,
        signature_b64=signature_b64,
        source_repo=staged_repo,
        expected_channel=expected_channel,
        model=model,
        updater_version=updater_version,
        sysroot_path=root,
        work_dir=work / "import",
        trusted_key=trusted_key,
    )
    _verify_image_identity(image, st_verified)
    result = dict(imported)
    result["extractionLog"] = str(extract_log)
    result["sourceRepository"] = str(staged_repo)
    return result
