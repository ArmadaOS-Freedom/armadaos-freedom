"""Prepare credentials and sshd configuration for one recovery boot."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .deployment_store import _parse_binding
from .installer_authorization import AuthorizationError, fingerprint_raw, parse_public_key

COMMAND = "/usr/bin/python3 -I /usr/libexec/armada/armada-recovery-update"
_MODELS = {"TB322FC", "TB323FU", "TB710FU"}
_KEYS = {"schemaVersion", "sessionId", "model", "recoveryPartUuid", "installedDeployment"}
_KEYS_V2 = _KEYS | {"entrySlot", "originalBoots"}
_ORIGINAL_BOOT_KEYS = {"partitionUuid", "bytes", "sha256"}
_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class RecoveryAuthorizationError(Exception):
    pass


def _path(value, name):
    text = os.fspath(value)
    if not os.path.isabs(text) or os.path.realpath(text) != text:
        raise RecoveryAuthorizationError(f"{name} must be an absolute canonical path")
    if any(ord(c) < 33 or ord(c) == 127 or c in "'\"\\" for c in text):
        raise RecoveryAuthorizationError(f"unsafe {name}")
    return Path(text)


def _directory(path, session=False):
    try:
        info = path.lstat()
    except OSError as err:
        raise RecoveryAuthorizationError("unsafe input directory") from err
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise RecoveryAuthorizationError("input directory must be root-owned and not a symlink")
    if session and stat.S_IMODE(info.st_mode) != 0o700:
        raise RecoveryAuthorizationError("session directory must have mode 0700")
    if not session and info.st_mode & 0o022:
        raise RecoveryAuthorizationError("unsafe runtime parent permissions")
    current = path if session else path.parent
    while True:
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RecoveryAuthorizationError("unsafe directory ancestor")
        if current.parent == current:
            break
        current = current.parent


def _open_validated(path, limit, secret):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = None
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise RecoveryAuthorizationError("input must be a root-owned regular file")
        mode = stat.S_IMODE(info.st_mode)
        if (secret and mode != 0o600) or (not secret and mode & 0o022):
            raise RecoveryAuthorizationError("unsafe input permissions")
        if info.st_size > limit:
            raise RecoveryAuthorizationError("input exceeds size limit")
    except RecoveryAuthorizationError:
        if fd is not None:
            os.close(fd)
        raise
    except OSError as err:
        if fd is not None:
            os.close(fd)
        raise RecoveryAuthorizationError("cannot safely read input") from err
    return fd


def _read(path, limit, secret=False):
    try:
        fd = _open_validated(path, limit, secret)
        with os.fdopen(fd, "rb", closefd=True) as stream:
            data = stream.read(limit + 1)
    except RecoveryAuthorizationError:
        raise
    except OSError as err:
        raise RecoveryAuthorizationError("cannot safely read input") from err
    if len(data) > limit:
        raise RecoveryAuthorizationError("input exceeds size limit")
    return data


def _host_public(path):
    fd = _open_validated(path, 16384, True)
    try:
        host_key = os.pread(fd, 16385, 0)
        if len(host_key) > 16384:
            raise RecoveryAuthorizationError("input exceeds size limit")
        result = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", f"/proc/self/fd/{fd}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(fd,), timeout=10, check=True)
        if os.pread(fd, 16385, 0) != host_key:
            raise RecoveryAuthorizationError("host key changed during validation")
    except RecoveryAuthorizationError:
        raise
    except (OSError, subprocess.SubprocessError) as err:
        raise RecoveryAuthorizationError("host key is not a valid unencrypted ed25519 key") from err
    finally:
        os.close(fd)
    try:
        host_raw, host_public = _public_line(result.stdout, "host public key", allow_trailing_comment=True)
    except RecoveryAuthorizationError as err:
        raise RecoveryAuthorizationError("host key is not a valid unencrypted ed25519 key") from err
    return host_key, host_raw, host_public


def _public_line(data, source, allow_trailing_comment=False):
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as err:
        raise RecoveryAuthorizationError(f"{source} is not ASCII") from err
    if text.endswith("\n"):
        text = text[:-1]
    if "\n" in text or "\r" in text:
        raise RecoveryAuthorizationError(f"{source} must contain one public-key line")
    try:
        raw, encoded = parse_public_key(text, allow_trailing_comment=allow_trailing_comment)
    except AuthorizationError as err:
        raise RecoveryAuthorizationError(f"invalid {source}") from err
    return raw, f"ssh-ed25519 {encoded}"


def _session(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RecoveryAuthorizationError("duplicate session JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as err:
        raise RecoveryAuthorizationError("invalid session JSON") from err
    if not isinstance(value, dict):
        raise RecoveryAuthorizationError("session JSON has incorrect fields")
    version = value.get("schemaVersion")
    if type(version) is not int or version not in (1, 2):
        raise RecoveryAuthorizationError("unsupported session schema")
    if set(value) != (_KEYS if version == 1 else _KEYS_V2):
        raise RecoveryAuthorizationError("session JSON has incorrect fields")
    if not isinstance(value["sessionId"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["sessionId"]):
        raise RecoveryAuthorizationError("invalid sessionId")
    if type(value["model"]) is not str or value["model"] not in _MODELS:
        raise RecoveryAuthorizationError("invalid model")
    uuid = value["recoveryPartUuid"]
    if not isinstance(uuid, str) or not re.fullmatch(_UUID_RE, uuid):
        raise RecoveryAuthorizationError("invalid recovery partition UUID")
    if version == 2 and not any(c in "123456789abcdef" for c in uuid):
        raise RecoveryAuthorizationError("invalid recovery partition UUID")
    try:
        _parse_binding(value["installedDeployment"])
    except Exception as err:
        raise RecoveryAuthorizationError("invalid installedDeployment") from err
    if version == 1:
        return value
    if value["entrySlot"] not in ("a", "b"):
        raise RecoveryAuthorizationError("invalid entrySlot")
    boots = value["originalBoots"]
    if not isinstance(boots, dict) or set(boots) != {"a", "b"}:
        raise RecoveryAuthorizationError("invalid originalBoots")
    for slot in ("a", "b"):
        boot = boots[slot]
        if not isinstance(boot, dict) or set(boot) != _ORIGINAL_BOOT_KEYS:
            raise RecoveryAuthorizationError(f"invalid originalBoots[{slot}]")
        part_uuid = boot["partitionUuid"]
        if not isinstance(part_uuid, str) or not re.fullmatch(_UUID_RE, part_uuid) or \
                not any(c in "123456789abcdef" for c in part_uuid):
            raise RecoveryAuthorizationError(f"invalid originalBoots[{slot}].partitionUuid")
        size = boot["bytes"]
        if type(size) is not int or not 1 <= size <= 256 * 1024 * 1024:
            raise RecoveryAuthorizationError(f"invalid originalBoots[{slot}].bytes")
        if not isinstance(boot["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", boot["sha256"]):
            raise RecoveryAuthorizationError(f"invalid originalBoots[{slot}].sha256")
    if boots["a"]["partitionUuid"] == boots["b"]["partitionUuid"]:
        raise RecoveryAuthorizationError("original boot partition UUIDs must differ")
    return value


def _write_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def prepare_recovery_ssh(*, session_dir=Path("/etc/armada-update-recovery/session"), runtime_dir=Path("/run/armada-update-recovery/ssh")):
    if os.geteuid() != 0:
        raise RecoveryAuthorizationError("recovery SSH preparation requires root")
    session_dir, runtime_dir = _path(session_dir, "session_dir"), _path(runtime_dir, "runtime_dir")
    _directory(session_dir, True)
    _directory(runtime_dir.parent)
    identity = _session(_read(session_dir / "session.json", 8192, True))
    _, client = _public_line(_read(session_dir / "client_ed25519.pub", 4096), "client public key")
    host_key, host_raw, host_public = _host_public(session_dir / "ssh_host_ed25519_key")
    paths = {name: runtime_dir / name for name in ("ssh_host_ed25519_key", "authorized_keys", "sshd_config", "identity.json")}
    config = f"""Port 22
ListenAddress 172.16.42.1
HostKey {paths['ssh_host_ed25519_key']}
HostKeyAlgorithms ssh-ed25519
PidFile {runtime_dir / 'sshd.pid'}
AllowUsers root
PermitRootLogin forced-commands-only
PubkeyAuthentication yes
AuthenticationMethods publickey
AuthorizedKeysFile {paths['authorized_keys']}
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
UsePAM no
StrictModes yes
DisableForwarding yes
PermitTTY no
X11Forwarding no
PermitUserRC no
PermitUserEnvironment no
ForceCommand {COMMAND}
"""
    outputs = [(paths["ssh_host_ed25519_key"], host_key), (paths["authorized_keys"], f'restrict,command="{COMMAND}" {client}\n'.encode()), (paths["sshd_config"], config.encode()), (paths["identity.json"], (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode())]
    made = []
    os.mkdir(runtime_dir, 0o700)
    try:
        for path, data in outputs:
            _write_file(path, data)
            made.append(path)
    except Exception:
        for path in reversed(made):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            runtime_dir.rmdir()
        except OSError:
            pass
        raise
    return {"identity": os.fspath(paths["identity.json"]), "hostPublicKey": host_public, "hostFingerprint": fingerprint_raw(host_raw), "configPath": os.fspath(paths["sshd_config"])}
