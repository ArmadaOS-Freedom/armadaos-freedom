"""Authenticated online acquisition of an update image into an existing repo."""
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .boot_context import _read_regular
from .channel_source import fetch_update
from .deployment_store import (
    DeploymentError,
    _binding_of,
    _gi,
    _require_nonsymlink_dir,
    _validate_commit_metadata,
)
from .signature import DEFAULT_TRUSTED_KEY

_PULL_TIMEOUT = 7200
_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "HOME": "/root",
}


class ImageSourceError(RuntimeError):
    pass


def _paths(sysroot_path, work_dir):
    if not isinstance(sysroot_path, (str, Path)) or not isinstance(work_dir, (str, Path)):
        raise ImageSourceError("sysroot_path and work_dir must be paths")
    root, work = Path(sysroot_path), Path(work_dir)
    if not root.is_absolute() or not work.is_absolute():
        raise ImageSourceError("sysroot_path and work_dir must be absolute")
    _require_nonsymlink_dir(root)
    _require_nonsymlink_dir(root, "ostree", "repo")
    if work.is_symlink() or work.exists():
        raise ImageSourceError(f"work_dir must be a new leaf: {work}")
    _require_nonsymlink_dir(work.parent)
    for protected in (root / "ostree", root / "boot"):
        if work == protected or protected in work.parents:
            raise ImageSourceError(f"work_dir may not be inside {protected}")
    try:
        work.mkdir(mode=0o700)
    except OSError as exc:
        raise ImageSourceError(f"cannot create work_dir {work}: {exc}") from exc
    return root, work


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_command(command, log_path):
    log_path = Path(log_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(log_path, flags, 0o600)
    except OSError as exc:
        raise ImageSourceError(f"cannot create log {log_path}: {exc}") from exc
    with os.fdopen(fd, "wb") as log:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, env=_ENV, shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            raise ImageSourceError(f"cannot start command; log {log_path}: {exc}") from exc
        try:
            status = process.wait(timeout=_PULL_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            raise ImageSourceError(f"command timed out; log retained at {log_path}") from exc
        except BaseException:
            _kill_group(process)
            raise
    if status != 0:
        raise ImageSourceError(f"command failed with status {status}; log {log_path}")
    return log_path


def _pull(repo, origin_ref, work):
    work = Path(work)
    command = [
        "/usr/bin/bootc", "internals", "ostree-ext", "container", "image",
        "pull", "--auth-anonymous", "--ostree-digestfile", str(work / "commit"),
        str(repo), origin_ref,
    ]
    return _run_command(command, work / "pull.log")


def _read_commit(work):
    path = Path(work) / "commit"
    try:
        raw = _read_regular(path, 256)
        digest = raw.decode("ascii").strip()
    except Exception as exc:
        raise ImageSourceError(f"invalid pull digest file {path}: {exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ImageSourceError(f"non-canonical commit digest in {path}")
    return digest


def _validate_repo_commit(repo, digest, package):
    ok, variant, state = repo.load_commit(digest)
    if not ok or variant is None or int(state) != 0:
        raise ImageSourceError(f"pulled commit is unavailable or partial: {digest}")
    try:
        _validate_commit_metadata(
            variant, package["ociDigest"], package["sourceRevision"], package["version"]
        )
    except DeploymentError as exc:
        raise ImageSourceError(str(exc)) from exc


def _import_verified(destination_repo, origin_ref, work, expected_commit, validate):
    work = Path(work)
    stage = work / "staging-repo"
    try:
        stage.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        raise ImageSourceError(f"cannot create staging repo {stage}: {exc}") from exc
    _run_command(
        ["/usr/bin/ostree", "init", f"--repo={stage}", "--mode=bare"],
        work / "init.log",
    )
    pull_log = _pull(stage, origin_ref, work)
    digest = _read_commit(work)
    if digest != expected_commit:
        raise ImageSourceError(f"pulled commit does not match channel; log {pull_log}")
    validate(stage, digest)
    _run_command(
        [
            "/usr/bin/ostree", "pull-local", f"--repo={destination_repo}",
            "--untrusted", "--disable-verify-bindings", str(stage), digest,
        ],
        work / "import.log",
    )
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        raise ImageSourceError(f"cannot remove staging repo {stage}: {exc}") from exc
    return pull_log


def fetch_online_image(*, channel_url, signature_url, expected_channel, model,
                       updater_version, sysroot_path, work_dir,
                       trusted_key=DEFAULT_TRUSTED_KEY):
    """Authenticate and download an image; never deploy or activate it."""
    acquired = fetch_update(
        channel_url, signature_url, expected_channel=expected_channel,
        model=model, updater_version=updater_version, trusted_key=trusted_key,
    )
    channel, package = acquired["channel"], acquired["package"]
    oci_digest = package["ociDigest"]
    origin_ref = (
        "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
        f"armadaos-freedom:{expected_channel}@{oci_digest}"
    )
    try:
        root, work = _paths(sysroot_path, work_dir)
    except DeploymentError as exc:
        raise ImageSourceError(str(exc)) from exc
    Gio, GLib, OSTree = _gi()
    sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
    ok, locked = sysroot.try_lock()
    if not (ok and locked):
        raise ImageSourceError("sysroot busy: try_lock not acquired")
    try:
        if not sysroot.load(None):
            raise ImageSourceError("sysroot load failed")
        before = [_binding_of(dep) for dep in (sysroot.get_deployments() or [])]
        ok, repo = sysroot.get_repo(None)
        if not ok or repo is None:
            raise ImageSourceError("no repo in sysroot")
        repo_file = repo.get_path()
        raw_repo = repo_file.get_path() if repo_file is not None else None
        expected_repo = root / "ostree" / "repo"
        if not raw_repo:
            raise ImageSourceError(f"unexpected sysroot repository path: {raw_repo!r}")
        try:
            same_repo = os.path.samefile(raw_repo, expected_repo)
        except OSError as exc:
            raise ImageSourceError(
                f"cannot verify sysroot repository identity {raw_repo!r}: {exc}"
            ) from exc
        if not same_repo:
            raise ImageSourceError(f"unexpected sysroot repository path: {raw_repo!r}")

        def _validate_stage(stage_path, stage_digest):
            stage_repo = OSTree.Repo.new(Gio.File.new_for_path(str(stage_path)))
            if not stage_repo.open(None):
                raise ImageSourceError(f"cannot open staging repository {stage_path}")
            _validate_repo_commit(stage_repo, stage_digest, package)

        digest = package["ostreeCommit"]
        log_path = _import_verified(expected_repo, origin_ref, work, digest, _validate_stage)
        if not sysroot.load(None):
            raise ImageSourceError("sysroot reload after pull failed")
        after = [_binding_of(dep) for dep in (sysroot.get_deployments() or [])]
        if after != before:
            raise ImageSourceError("deployment bindings changed during image pull")
        ok, repo = sysroot.get_repo(None)
        if not ok or repo is None:
            raise ImageSourceError("no repo in sysroot")
        _validate_repo_commit(repo, digest, package)
        return {
            "channel": channel, "package": package, "originRef": origin_ref,
            "repo": str(expected_repo), "commit": digest,
            "pullLog": str(log_path), "phase": "downloaded",
        }
    finally:
        sysroot.unlock()
