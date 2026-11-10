"""Recovery filesystem mount context managers.

The inspection API mounts the already-identified root btrfs subvol, boot ext4,
and var btrfs subvol read-only. The separate update API, for use only after
recovery authorization and deployment verification, mounts root and boot
read-write while retaining var read-only. Neither API authorizes transactions,
executes mounted content, publishes RPCs, or writes boot partitions.
"""

import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager

from .recovery_filesystems import inspect_recovery_filesystems

MOUNT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 65536


class RecoveryMountError(Exception):
    """Raised when recovery filesystems cannot be safely mounted."""


def _run(argv, *, pass_fds=()):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=MOUNT_TIMEOUT_SECONDS,
            pass_fds=tuple(pass_fds),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecoveryMountError(
            "command timed out after %ds: %r" % (MOUNT_TIMEOUT_SECONDS, argv)
        ) from exc
    except OSError as exc:
        raise RecoveryMountError("failed to execute %r: %s" % (argv, exc)) from exc
    out = proc.stdout
    err = proc.stderr
    if len(out) > MAX_OUTPUT_BYTES or len(err) > MAX_OUTPUT_BYTES:
        raise RecoveryMountError(
            "command output exceeded %d bytes: %r" % (MAX_OUTPUT_BYTES, argv)
        )
    if proc.returncode != 0:
        raise RecoveryMountError(
            "command failed (%r) rc=%d stdout=%r stderr=%r"
            % (argv, proc.returncode, out, err)
        )
    return out, err


def _require(cond, msg):
    if not cond:
        raise RecoveryMountError(msg)


def _check_parent_dir(parent_dir):
    try:
        parent_dir = os.fsdecode(os.fspath(parent_dir))
    except TypeError as exc:
        raise RecoveryMountError("invalid parent_dir %r: %s" % (parent_dir, exc)) from exc
    try:
        real_parent = os.path.realpath(parent_dir)
        _require(os.path.isabs(parent_dir) and parent_dir == real_parent,
                 "parent_dir must be canonical absolute path: %r" % (parent_dir,))
        st = os.stat(parent_dir)
    except OSError as exc:
        raise RecoveryMountError("cannot stat parent_dir %r: %s" % (parent_dir, exc)) from exc
    _require(stat.S_ISDIR(st.st_mode), "parent_dir is not a directory: %r" % (parent_dir,))
    _require(st.st_uid == 0, "parent_dir not root-owned: %r" % (parent_dir,))
    _require(not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)),
             "parent_dir group/world-writable: %r" % (parent_dir,))
    return parent_dir


def _open_block_device(path, expect_rdev):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RecoveryMountError("cannot open block device %r: %s" % (path, exc)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISBLK(st.st_mode):
            raise RecoveryMountError("not a block device: %r" % (path,))
        if st.st_rdev != expect_rdev:
            raise RecoveryMountError(
                "device identity changed for %r: rdev %d != expected %d"
                % (path, st.st_rdev, expect_rdev))
    except Exception:
        os.close(fd)
        raise
    return fd


def _mount_entry(target):
    out, _err = _run([
        "findmnt", "--json", "--mountpoint", target,
        "--output", "TARGET,FSTYPE,SOURCE,UUID,FSROOT,VFS-OPTIONS,ID",
    ])
    try:
        data = json.loads(out.decode("utf-8", "strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecoveryMountError("findmnt output not valid UTF-8 JSON for %r" % (target,)) from exc
    entries = data.get("filesystems") if isinstance(data, dict) else None
    _require(isinstance(entries, list) and len(entries) == 1,
             "expected exactly one findmnt result for %r" % (target,))
    entry = entries[0]
    _require(isinstance(entry, dict) and "children" not in entry,
             "expected flat findmnt entry for %r" % (target,))
    _require(entry.get("target") == target,
             "findmnt target mismatch for %r: %r" % (target, entry.get("target")))
    mount_id = entry.get("id")
    _require(isinstance(mount_id, int) and not isinstance(mount_id, bool) and mount_id > 0,
             "invalid mount ID for %r: %r" % (target, mount_id))
    return entry


def _verify_mount(entry, *, target, fstype, uuid, fsroot, read_only=True):
    _require(entry.get("fstype") == fstype,
             "findmnt fstype mismatch for %r: %r" % (target, entry.get("fstype")))
    _require(entry.get("uuid") == uuid,
             "findmnt UUID mismatch for %r: %r" % (target, entry.get("uuid")))
    _require(entry.get("fsroot") == fsroot,
             "findmnt fsroot mismatch for %r: %r" % (target, entry.get("fsroot")))
    opts = entry.get("vfs-options")
    _require(isinstance(opts, str), "invalid vfs-options for %r: %r" % (target, opts))
    opt_items = opts.split(",")
    expected_opt = "ro" if read_only else "rw"
    forbidden_opt = "rw" if read_only else "ro"
    _require(expected_opt in opt_items,
             "mount is not %s for %r: %r" % (expected_opt, target, opts))
    _require(forbidden_opt not in opt_items,
             "mount has %s for %r: %r" % (forbidden_opt, target, opts))


def _umount(target, expected_id):
    if expected_id is not None:
        _verify_mount_identity(target, expected_id)
    _run(["umount", "--", target])


def _verify_mount_identity(target, expected_id):
    entry = _mount_entry(target)
    current = entry["id"]
    if current != expected_id:
        raise RecoveryMountError(
            "mount replaced under us for %r: id %d != expected %d"
            % (target, current, expected_id))
    return current


def _rmdir_if_empty(path):
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


@contextmanager
def mounted_recovery_filesystems(*, expected_filesystems,
                                 parent_dir="/run/armada-update-recovery"):
    """Mount root/boot/var recovery filesystems read-only for inspection.

    Yields a report dict. All mounts and the session directory are unwound on
    exit; on unmount failure mounts and directories are retained and an error
    is raised.
    """
    with _mounted_filesystems(
        expected_filesystems=expected_filesystems,
        parent_dir=parent_dir,
        for_update=False,
    ) as report:
        yield report


@contextmanager
def mounted_recovery_update_filesystems(
    *, expected_filesystems, parent_dir="/run/armada-update-recovery"
):
    """Mount root and boot read-write and var read-only for an authorized update.

    The caller must already have authenticated recovery authorization and
    verified the read-only deployment. This helper does not authorize an
    update, execute mounted content, publish RPCs, or write boot partitions.
    Mount and cleanup behavior otherwise matches the inspection API.
    The fixed parent/update path keeps prepared paths stable across calls.
    The caller holds the update lock while this helper is active.
    An existing entry is refused, not adopted.
    """
    with _mounted_filesystems(
        expected_filesystems=expected_filesystems,
        parent_dir=parent_dir,
        for_update=True,
    ) as report:
        yield report


@contextmanager
def _mounted_filesystems(*, expected_filesystems, parent_dir, for_update):
    _require(os.geteuid() == 0, "recovery mounts require root (euid 0)")
    parent_dir = _check_parent_dir(parent_dir)

    report = inspect_recovery_filesystems()
    _require(report == expected_filesystems,
             "recovery filesystem report does not match expected state")

    session = None
    owned_dirs = []
    mounted = []           # (target, mount_id or None) in mount order
    root_fd = None
    boot_fd = None

    def _mount_and_verify(argv, *, pass_fds, target, fstype, uuid, fsroot,
                          read_only=True):
        _run(argv, pass_fds=pass_fds)
        mounted.append((target, None))
        entry = _mount_entry(target)
        mounted[-1] = (target, entry["id"])
        _verify_mount(entry, target=target, fstype=fstype, uuid=uuid, fsroot=fsroot,
                      read_only=read_only)

    try:
        try:
            if for_update:
                # Stable path across calls: hashed offline_prepare plans bind
                # canonical sysrootPath/materials/file-claims to this directory.
                # Sessions are serialized by the caller's outer update lock;
                # os.mkdir is exclusive so an existing entry (leftover mount,
                # directory, or symlink) fails here before any device open or
                # mount, and is never removed or adopted.
                session = os.path.join(parent_dir, "update")
                os.mkdir(session, 0o700)
                owned_dirs.append(session)
            else:
                session = tempfile.mkdtemp(prefix="inspect-", dir=parent_dir)
                owned_dirs.append(session)
            os.chmod(session, 0o700)
            sysroot_mp = os.path.join(session, "sysroot")
            var_mp = os.path.join(session, "var")
            os.mkdir(sysroot_mp, 0o700)
            owned_dirs.append(sysroot_mp)
            os.mkdir(var_mp, 0o700)
            owned_dirs.append(var_mp)
        except OSError as exc:
            raise RecoveryMountError(
                "failed to prepare recovery mount session under %r: %s" % (parent_dir, exc)
            ) from exc

        root_fd = _open_block_device(report["root"]["path"], report["root"]["rdev"])
        boot_fd = _open_block_device(report["boot"]["path"], report["boot"]["rdev"])

        root_dev = "/proc/self/fd/%d" % root_fd
        boot_dev = "/proc/self/fd/%d" % boot_fd

        _mount_and_verify(
            ["mount", "-t", "btrfs",
             "-o", ("rw,nosuid,nodev,noexec,subvol=root" if for_update else
                    "ro,rescue=nologreplay,nosuid,nodev,noexec,subvol=root"),
             "--", root_dev, sysroot_mp],
            pass_fds=(root_fd,), target=sysroot_mp,
            fstype="btrfs", uuid=report["root"]["uuid"], fsroot="/root",
            read_only=not for_update,
        )

        boot_mp = os.path.join(sysroot_mp, "boot")
        try:
            st = os.lstat(boot_mp)
        except OSError as exc:
            raise RecoveryMountError("cannot stat %r on mounted root" % (boot_mp,)) from exc
        _require(stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode),
                 "sysroot/boot is not a canonical directory")

        _mount_and_verify(
            ["mount", "-t", "ext4",
             "-o", "rw,nosuid,nodev,noexec" if for_update else
                    "ro,noload,nosuid,nodev,noexec",
             "--", boot_dev, boot_mp],
            pass_fds=(boot_fd,), target=boot_mp,
            fstype="ext4", uuid=report["boot"]["uuid"], fsroot="/",
            read_only=not for_update,
        )

        _mount_and_verify(
            ["mount", "-t", "btrfs",
             "-o", ("ro,nosuid,nodev,noexec,subvol=var" if for_update else
                    "ro,rescue=nologreplay,nosuid,nodev,noexec,subvol=var"),
             "--", root_dev, var_mp],
            pass_fds=(root_fd,), target=var_mp,
            fstype="btrfs", uuid=report["root"]["uuid"], fsroot="/var",
        )

        # Drift check after mounting: do not yield if state changed.
        fresh_report = inspect_recovery_filesystems()
        _require(fresh_report == report,
                 "recovery filesystem state drifted after mounting; not yielding")

        yield {
            "schemaVersion": 1,
            "phase": ("recovery_filesystems_mounted_update" if for_update else
                      "recovery_filesystems_mounted_readonly"),
            "filesystems": fresh_report,
            "sysrootPath": sysroot_mp,
            "bootPath": boot_mp,
            "varPath": var_mp,
        }
    finally:
        try:
            # Reverse-order unmount; stop on first failure, retain everything.
            for target, mid in reversed(mounted):
                try:
                    _umount(target, mid)
                except (RecoveryMountError, OSError) as exc:
                    raise RecoveryMountError(
                        "unmount failed for %r; mounts and session dir %r retained: %s"
                        % (target, session, exc)
                    ) from exc
            # All unmounted; remove only our own empty directories.
            for path in reversed(owned_dirs):
                try:
                    _rmdir_if_empty(path)
                except OSError as exc:
                    raise RecoveryMountError(
                        "failed to remove recovery mount dir %r: %s" % (path, exc)
                    ) from exc
        finally:
            try:
                if boot_fd is not None:
                    os.close(boot_fd)
            finally:
                if root_fd is not None:
                    os.close(root_fd)
