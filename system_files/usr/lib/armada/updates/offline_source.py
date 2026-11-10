"""Authenticated offline acquisition of a package OSTree repository."""
import os
from pathlib import Path

from .deployment_store import (
    DeploymentError,
    _binding_of,
    _gi,
    _require_nonsymlink_dir,
)
from .image_source import (
    ImageSourceError,
    _paths,
    _run_command,
    _validate_repo_commit,
)
from .package_contract import PackageError, load_package, require_operation
from .signature import DEFAULT_TRUSTED_KEY

_CHANNELS = frozenset(("stable", "beta", "testing"))


class OfflineSourceError(ImageSourceError):
    """An offline repository import failed validation or acquisition."""


def _validate_source_location(source, root):
    workspace = root / ".armada-update"
    if source == root or source in root.parents:
        raise OfflineSourceError("source must be outside target sysroot")
    if root in source.parents and source != workspace and workspace not in source.parents:
        raise OfflineSourceError(
            "source inside target sysroot must be within .armada-update"
        )


def _validate_source_and_paths(source_repo, sysroot_path, work_dir):
    if not isinstance(source_repo, (str, Path)):
        raise OfflineSourceError("source_repo must be a path")
    if not isinstance(sysroot_path, (str, Path)) or not isinstance(work_dir, (str, Path)):
        raise OfflineSourceError("sysroot_path and work_dir must be paths")
    source, root, work = Path(source_repo), Path(sysroot_path), Path(work_dir)
    if not source.is_absolute() or not root.is_absolute() or not work.is_absolute():
        raise OfflineSourceError("source_repo, sysroot_path, and work_dir must be absolute")
    try:
        _require_nonsymlink_dir(source)
        _require_nonsymlink_dir(root)
        _require_nonsymlink_dir(root, "ostree", "repo")
    except DeploymentError as exc:
        raise OfflineSourceError(str(exc)) from exc
    expected_repo = root / "ostree" / "repo"
    try:
        if os.path.samefile(source, expected_repo) or os.path.samefile(source, root):
            raise OfflineSourceError("source_repo must be distinct from destination sysroot/repo")
    except OSError as exc:
        raise OfflineSourceError(f"cannot compare source and destination paths: {exc}") from exc
    _validate_source_location(source, root)
    if work == source or source in work.parents or work in source.parents:
        raise OfflineSourceError(f"work_dir may not overlap with source_repo {source}")
    try:
        root, work = _paths(root, work)
    except (DeploymentError, ImageSourceError) as exc:
        raise OfflineSourceError(str(exc)) from exc
    return source, root, work, expected_repo


def import_offline_repository(
    *,
    document,
    signature_b64,
    source_repo,
    expected_channel,
    model,
    updater_version,
    sysroot_path,
    work_dir,
    trusted_key=DEFAULT_TRUSTED_KEY,
):
    """Authenticate a local package repository and import its target commit."""
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

    source, root, work, expected_repo = _validate_source_and_paths(
        source_repo, sysroot_path, work_dir
    )
    oci_digest = package["ociDigest"]
    digest = package["ostreeCommit"]
    origin_ref = (
        "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
        f"armadaos-freedom:{expected_channel}@{oci_digest}"
    )

    try:
        Gio, GLib, OSTree = _gi()
    except Exception as exc:
        raise OfflineSourceError(f"cannot initialize OSTree bindings: {exc}") from exc

    sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
    try:
        ok, locked = sysroot.try_lock()
    except Exception as exc:
        raise OfflineSourceError(f"sysroot lock failed: {exc}") from exc
    if not (ok and locked):
        raise OfflineSourceError("sysroot busy: try_lock not acquired")

    try:
        if not sysroot.load(None):
            raise OfflineSourceError("sysroot load failed")
        before = [_binding_of(dep) for dep in (sysroot.get_deployments() or [])]
        if not before:
            raise OfflineSourceError("offline upgrade requires existing deployments in sysroot")
        ok, repo = sysroot.get_repo(None)
        if not ok or repo is None:
            raise OfflineSourceError("no repo in sysroot")
        repo_file = repo.get_path()
        raw_repo = repo_file.get_path() if repo_file is not None else None
        if not raw_repo:
            raise OfflineSourceError(f"unexpected sysroot repository path: {raw_repo!r}")
        try:
            same_repo = os.path.samefile(raw_repo, expected_repo)
        except OSError as exc:
            raise OfflineSourceError(
                f"cannot verify sysroot repository identity {raw_repo!r}: {exc}"
            ) from exc
        if not same_repo:
            raise OfflineSourceError(f"unexpected sysroot repository path: {raw_repo!r}")

        src_repo = OSTree.Repo.new(Gio.File.new_for_path(str(source)))
        if not src_repo.open(None):
            raise OfflineSourceError(f"cannot open source repository {source}")
        try:
            _validate_repo_commit(src_repo, digest, package)
        except ImageSourceError as exc:
            raise OfflineSourceError(str(exc)) from exc

        try:
            log_path = _run_command(
                [
                    "/usr/bin/ostree",
                    "pull-local",
                    f"--repo={expected_repo}",
                    "--untrusted",
                    "--disable-verify-bindings",
                    str(source),
                    digest,
                ],
                work / "import.log",
            )
        except ImageSourceError as exc:
            raise OfflineSourceError(str(exc)) from exc

        if not sysroot.load(None):
            raise OfflineSourceError("sysroot reload after import failed")
        after = [_binding_of(dep) for dep in (sysroot.get_deployments() or [])]
        if after != before:
            raise OfflineSourceError("deployment bindings changed during repository import")
        ok, repo = sysroot.get_repo(None)
        if not ok or repo is None:
            raise OfflineSourceError("no repo in sysroot")
        try:
            _validate_repo_commit(repo, digest, package)
        except ImageSourceError as exc:
            raise OfflineSourceError(str(exc)) from exc

        return {
            "package": package,
            "originRef": origin_ref,
            "repo": str(expected_repo),
            "commit": digest,
            "importLog": str(log_path),
            "phase": "imported",
        }
    except OfflineSourceError:
        raise
    except Exception as exc:
        raise OfflineSourceError(str(exc)) from exc
    finally:
        sysroot.unlock()
