import base64
import binascii
import fcntl
import grp
import hashlib
import os
import pwd
import stat
import struct
import sys
import tempfile

MAX_KEY_LINE_LENGTH = 4096
MAX_AUTHORIZED_KEYS_SIZE = 1 * 1024 * 1024
HOST_KEY_PATH = "/etc/ssh/ssh_host_ed25519_key.pub"
RESTRICTED_COMMAND = "/usr/bin/sudo -n -- /usr/bin/python3 -I /usr/libexec/armada/armada-offline-update"
ENTRY_COMMENT = "armada-offline-installer"


class AuthorizationError(Exception):
    pass


def parse_ed25519_wire_blob(raw_bytes):
    if len(raw_bytes) != 4 + 11 + 4 + 32:
        raise AuthorizationError("Invalid RFC4253 ed25519 blob length")
    name_len = struct.unpack(">I", raw_bytes[0:4])[0]
    if name_len != 11:
        raise AuthorizationError("Invalid ed25519 key type length")
    key_type = raw_bytes[4:15]
    if key_type != b"ssh-ed25519":
        raise AuthorizationError("Unsupported key type in wire blob")
    key_len = struct.unpack(">I", raw_bytes[15:19])[0]
    if key_len != 32:
        raise AuthorizationError("Invalid ed25519 public key data length")
    return raw_bytes[19:51]


def parse_public_key(line_str, allow_trailing_comment=False):
    if not isinstance(line_str, str):
        raise AuthorizationError("Key input must be a string")
    if len(line_str) > MAX_KEY_LINE_LENGTH:
        raise AuthorizationError("Key string exceeds maximum allowed length")
    if any(c in line_str for c in ("\n", "\r", "\0")):
        raise AuthorizationError("Key string contains invalid control characters")

    tokens = line_str.strip().split()
    if not tokens:
        raise AuthorizationError("Key input is empty")

    if allow_trailing_comment:
        if len(tokens) < 2:
            raise AuthorizationError("Host public key must have at least type and base64 blob")
        key_type, b64_blob = tokens[0], tokens[1]
    else:
        if len(tokens) != 2:
            raise AuthorizationError("Installer key must consist of exactly type and base64 blob")
        key_type, b64_blob = tokens[0], tokens[1]

    if key_type != "ssh-ed25519":
        raise AuthorizationError(f"Unsupported key type: {key_type}")

    try:
        raw_blob = base64.b64decode(b64_blob.encode("ascii"), validate=True)
    except (binascii.Error, ValueError) as err:
        raise AuthorizationError(f"Invalid base64 encoding: {err}") from err

    canonical_b64 = base64.b64encode(raw_blob).decode("ascii")
    if canonical_b64 != b64_blob:
        raise AuthorizationError("Base64 encoding is not canonical")

    parse_ed25519_wire_blob(raw_blob)
    return raw_blob, canonical_b64


def fingerprint_raw(raw_bytes):
    digest = hashlib.sha256(raw_bytes).digest()
    b64_digest = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64_digest}"


def parse_authorized_keys_line(line):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, None, None

    in_quote = False
    escape = False
    pos = 0
    tokens = []
    current = []

    while pos < len(stripped):
        c = stripped[pos]
        if escape:
            current.append(c)
            escape = False
        elif c == "\\":
            current.append(c)
            escape = True
        elif c == '"':
            in_quote = not in_quote
            current.append(c)
        elif c in (" ", "\t") and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(c)
        pos += 1

    if current:
        tokens.append("".join(current))

    if not tokens:
        return None, None, None

    if tokens[0].startswith(("ssh-", "ecdsa-", "sk-")):
        options = None
        key_type = tokens[0]
        key_b64 = tokens[1] if len(tokens) > 1 else ""
        return options, key_type, key_b64
    if len(tokens) >= 3:
        return tokens[0], tokens[1], tokens[2]
    return None, None, None


def _check_dir_stat(st, dir_path, expected_uid):
    if not stat.S_ISDIR(st.st_mode):
        raise AuthorizationError(f"Path {dir_path} is not a directory")
    if st.st_uid != expected_uid:
        raise AuthorizationError(f"Directory {dir_path} is owned by UID {st.st_uid}, expected {expected_uid}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise AuthorizationError(f"Directory {dir_path} is group or world writable")


def _verify_safe_dir(dir_path, expected_uid):
    try:
        st = os.lstat(dir_path)
    except OSError as err:
        raise AuthorizationError(f"Cannot access directory {dir_path}: {err}") from err

    if stat.S_ISLNK(st.st_mode):
        raise AuthorizationError(f"Directory {dir_path} cannot be a symbolic link")
    _check_dir_stat(st, dir_path, expected_uid)


def _open_verified_dir(dir_path, expected_uid):
    """Open and verify the directory itself, returning its descriptor.

    The descriptor, not the path, is what the caller acts on, so the checks
    below apply to the very directory that will be flushed: its inode must
    still be the one at ``dir_path``, it must be owned by the user and it must
    not be group or world writable.
    """
    try:
        fd = os.open(
            dir_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as err:
        raise AuthorizationError(f"Cannot open directory {dir_path}: {err}") from err
    try:
        st = os.fstat(fd)
        try:
            path_st = os.lstat(dir_path)
        except OSError as err:
            raise AuthorizationError(f"Cannot access directory {dir_path}: {err}") from err
        if (st.st_dev, st.st_ino) != (path_st.st_dev, path_st.st_ino):
            raise AuthorizationError(f"Directory {dir_path} changed while being verified")
        _check_dir_stat(st, dir_path, expected_uid)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _ensure_ssh_dir(ssh_dir, expected_uid):
    user_home = os.path.dirname(ssh_dir)
    home_fd = _open_verified_dir(user_home, expected_uid)
    try:
        try:
            os.mkdir(os.path.basename(ssh_dir), 0o700, dir_fd=home_fd)
        except FileExistsError:
            pass
        except OSError as err:
            raise AuthorizationError(f"Failed to create {ssh_dir}: {err}") from err
        # Flush the parent's directory entry for .ssh before anything is
        # created inside it.  This runs on every attempt, not only when the
        # directory is new: an earlier attempt whose flush failed must not be
        # reported as durable just because the name is already visible.
        try:
            os.fsync(home_fd)
        except OSError as err:
            raise AuthorizationError(
                f"Failed to flush {user_home} after ensuring {ssh_dir}: {err}"
            ) from err
    finally:
        os.close(home_fd)

    _verify_safe_dir(ssh_dir, expected_uid)
    st = os.lstat(ssh_dir)
    current_mode = stat.S_IMODE(st.st_mode)
    if current_mode != 0o700:
        try:
            os.chmod(ssh_dir, 0o700)
        except OSError as err:
            raise AuthorizationError(f"Failed to set mode 0700 on {ssh_dir}: {err}") from err


def authorize_key(installer_key_str, user_home, user_uid, host_key_file=HOST_KEY_PATH):
    _, canonical_b64 = parse_public_key(installer_key_str, allow_trailing_comment=False)

    try:
        with open(host_key_file, "r", encoding="ascii") as f:
            host_line = f.read()
    except OSError as err:
        raise AuthorizationError(f"Failed to read host public key {host_key_file}: {err}") from err

    host_raw, _ = parse_public_key(host_line.strip(), allow_trailing_comment=True)
    host_fp = fingerprint_raw(host_raw)
    installer_raw = base64.b64decode(canonical_b64.encode("ascii"))
    installer_fp = fingerprint_raw(installer_raw)

    _verify_safe_dir(user_home, user_uid)
    ssh_dir = os.path.join(user_home, ".ssh")
    _ensure_ssh_dir(ssh_dir, user_uid)

    expected_entry = f'restrict,command="{RESTRICTED_COMMAND}" ssh-ed25519 {canonical_b64} {ENTRY_COMMENT}'
    auth_keys_path = os.path.join(ssh_dir, "authorized_keys")
    lock_path = os.path.join(ssh_dir, ".armada_auth.lock")

    try:
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError as err:
        raise AuthorizationError(f"Failed to open lock file: {err}") from err

    lock_acquired = False
    try:
        lock_st = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_st.st_mode):
            raise AuthorizationError(f"{lock_path} is not a regular file")
        if lock_st.st_nlink != 1:
            raise AuthorizationError(f"{lock_path} has hard links")
        if lock_st.st_uid != user_uid:
            raise AuthorizationError(f"{lock_path} is not owned by UID {user_uid}")
        try:
            os.fchmod(lock_fd, 0o600)
        except OSError as err:
            raise AuthorizationError(f"Failed to set mode 0600 on {lock_path}: {err}") from err
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            raise AuthorizationError(f"Failed to acquire flock: {err}") from err
        lock_acquired = True
    except BaseException:
        os.close(lock_fd)
        raise

    try:

        existing_content = b""
        if os.path.lexists(auth_keys_path):
            try:
                auth_fd = os.open(auth_keys_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError as err:
                raise AuthorizationError(f"Failed to open {auth_keys_path}: {err}") from err
            try:
                st = os.fstat(auth_fd)
                if not stat.S_ISREG(st.st_mode):
                    raise AuthorizationError(f"{auth_keys_path} is not a regular file")
                if st.st_nlink != 1:
                    raise AuthorizationError(f"{auth_keys_path} has hard links")
                if st.st_uid != user_uid:
                    raise AuthorizationError(f"{auth_keys_path} is not owned by UID {user_uid}")
                if st.st_size > MAX_AUTHORIZED_KEYS_SIZE:
                    raise AuthorizationError("authorized_keys file exceeds size limit")

                chunks = []
                total = 0
                while True:
                    chunk = os.read(auth_fd, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_AUTHORIZED_KEYS_SIZE:
                        raise AuthorizationError("authorized_keys file exceeds size limit")
                    chunks.append(chunk)
                existing_content = b"".join(chunks)
            finally:
                os.close(auth_fd)

            target_entry_found = False
            for raw_line in existing_content.split(b"\n"):
                line = raw_line.decode("utf-8", "surrogateescape").rstrip("\r")
                opts, k_type, k_b64 = parse_authorized_keys_line(line)
                if k_b64 == canonical_b64:
                    if line.strip() == expected_entry:
                        target_entry_found = True
                    else:
                        raise AuthorizationError(
                            "Installer key is already present in authorized_keys with different options or permissions"
                        )

            if target_entry_found:
                current_mode = stat.S_IMODE(st.st_mode)
                if current_mode != 0o600:
                    try:
                        os.chmod(auth_keys_path, 0o600)
                    except OSError as err:
                        raise AuthorizationError(f"Failed to set mode 0600 on {auth_keys_path}: {err}") from err
                return host_fp, installer_fp

        new_content = existing_content
        if new_content and not new_content.endswith(b"\n"):
            new_content += b"\n"
        new_content += (expected_entry + "\n").encode("ascii")

        temp_fd, temp_path = tempfile.mkstemp(dir=ssh_dir)
        try:
            try:
                os.fchmod(temp_fd, 0o600)
                written = 0
                while written < len(new_content):
                    written += os.write(temp_fd, new_content[written:])
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)

            os.replace(temp_path, auth_keys_path)

            dir_fd = os.open(ssh_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            if os.path.lexists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise
    finally:
        if lock_acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(lock_fd)

    return host_fp, installer_fp


def main():
    if len(sys.argv) != 2:
        sys.stderr.write("Usage: armada-authorize-installer '<ssh-ed25519-public-key>'\n")
        return 1

    installer_key_str = sys.argv[1]

    real_uid = os.getuid()
    effective_uid = os.geteuid()
    if real_uid != effective_uid:
        sys.stderr.write("Error: Effective and real UID must match.\n")
        return 1
    if real_uid == 0:
        sys.stderr.write("Error: Running as root is forbidden. Must run as wheel user.\n")
        return 1

    try:
        pw_entry = pwd.getpwuid(real_uid)
    except KeyError:
        sys.stderr.write(f"Error: UID {real_uid} not found in passwd database.\n")
        return 1

    user_name = pw_entry.pw_name
    user_home = pw_entry.pw_dir

    try:
        wheel_grp = grp.getgrnam("wheel")
        wheel_gid = wheel_grp.gr_gid
        wheel_members = wheel_grp.gr_mem
    except KeyError:
        sys.stderr.write("Error: 'wheel' group does not exist on system.\n")
        return 1

    if pw_entry.pw_gid != wheel_gid and user_name not in wheel_members:
        sys.stderr.write(f"Error: User '{user_name}' is not a member of the 'wheel' group.\n")
        return 1

    try:
        host_fp, installer_fp = authorize_key(installer_key_str, user_home, real_uid)
    except AuthorizationError as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1
    except Exception as err:
        sys.stderr.write(f"Unexpected error: {err}\n")
        return 1

    import json
    result = {
        "schema": 1,
        "userName": user_name,
        "hostKeyFingerprint": host_fp,
        "installerKeyFingerprint": installer_fp,
    }
    sys.stdout.write(json.dumps(result, indent=None) + "\n")
    return 0
