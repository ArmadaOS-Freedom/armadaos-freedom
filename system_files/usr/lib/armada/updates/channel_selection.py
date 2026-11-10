"""Persistent system update channel selection."""

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from . import trial_confirmation


CHANNELS = ("stable", "beta", "testing")
DEFAULT_STATE_PATH = Path("/var/lib/armada/update-channel")


def set_channel(channel, state_path=DEFAULT_STATE_PATH):
    if channel not in CHANNELS:
        raise ValueError("unsupported update channel")
    if os.geteuid() != 0:
        raise PermissionError("channel selection requires root")

    destination = Path(state_path)
    if not destination.is_absolute():
        raise ValueError("channel state destination must be an absolute path")
    parent = destination.parent
    with trial_confirmation._locked():
        if parent.resolve(strict=False) != parent:
            raise ValueError("channel state parent must not traverse symlinks")
        parent.mkdir(parents=True, exist_ok=True)
        if parent.resolve(strict=True) != parent:
            raise ValueError("channel state parent must not traverse symlinks")
        parent_info = parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != 0
                or parent_info.st_mode & 0o022):
            raise ValueError("channel state parent is unsafe")
        try:
            destination_info = destination.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(destination_info.st_mode):
                raise ValueError("channel state destination is not a regular file")

        fd = None
        temporary = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=".%s." % destination.name, dir=str(parent))
            temporary = Path(temporary_name)
            os.fchmod(fd, 0o644)
            stream = os.fdopen(fd, "w", encoding="utf-8")
            fd = None
            with stream:
                stream.write(channel + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(destination))
            temporary = None
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(str(parent), directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


def read_installed_channel(*, state_path, origin_ref):
    """Resolve the installed channel without consulting live system state."""
    if not isinstance(origin_ref, str) or any(
            ord(character) < 33 or ord(character) == 127
            for character in origin_ref):
        raise ValueError("invalid OSTree origin reference")
    remainder = origin_ref
    for prefix in (
            "ostree-image-signed:docker://",
            "ostree-unverified-image:docker://",
            "ostree-unverified-registry:docker://",
            "ostree-unverified-registry:",
            "docker://"):
        if origin_ref.startswith(prefix):
            remainder = origin_ref[len(prefix):]
            break
    repositories = (
        "ghcr.io/armadaos-freedom/armadaos-freedom",
        "ghcr.io/mydanyi/armadaos-freedom",
        "ghcr.io/armada-os/armada",
        "ghcr.io/virtudude/armada",
    )
    match = re.fullmatch(
        r"(?:%s)(?::(stable|beta|testing))?(?:@sha256:([0-9a-fA-F]{64}))?"
        % "|".join(re.escape(repository) for repository in repositories),
        remainder)
    if match is None or not any(match.groups()):
        raise ValueError("invalid OSTree origin reference")
    origin_channel = match.group(1)

    destination = Path(state_path)
    if not destination.is_absolute():
        raise ValueError("channel state path must be absolute")
    if destination.resolve(strict=False) != destination:
        raise ValueError("channel state path must not traverse symlinks")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        if origin_channel is not None:
            return origin_channel
        raise ValueError("digest-only origin requires installed channel state")
    if not stat.S_ISREG(destination_info.st_mode):
        raise ValueError("channel state is not a regular file")

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(str(destination), flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_size > 32):
            raise ValueError("channel state file is unsafe or oversized")
        content = os.read(fd, 33)
    finally:
        os.close(fd)
    try:
        content = content.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("channel state is not ASCII")
    for channel in CHANNELS:
        if content in (channel, channel + "\n"):
            return channel
    raise ValueError("channel state is corrupt")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Select the system update channel")
    parser.add_argument("channel", choices=CHANNELS)
    args = parser.parse_args(argv)
    try:
        set_channel(args.channel)
    except (OSError, RuntimeError, ValueError) as error:
        print("channel selection failed: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
