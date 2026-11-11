# SPDX-License-Identifier: GPL-3.0-only
"""Read-only boot context for OFFLINE dual-slot upgrades.

Systems that provide the firmware boot witness, or that carry an
``armada.deployment`` kernel argument, keep the strict rules of
:mod:`boot_context`.  Everything else is a legacy 0.7 system: it has neither
piece of evidence, so the running deployment is bound with libostree instead of
being inferred from the firmware.  A legacy system can never tell which physical
slot it booted from, so this module never invents an actual slot.

On the device the booted deployment only exists in the *default* libostree
runtime sysroot (root ``/``): ``Sysroot.new('/sysroot')`` loads but reports no
booted deployment in the composefs runtime.  The booted identity is therefore
always read from the default sysroot, while ``/sysroot`` and ``/boot`` are bound
separately through their block mounts.  Nothing here writes, stages, selects,
reorders, or deletes a deployment.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import armada_os
from . import boot_context

DEFAULT_SYSROOT = Path('/sysroot')
RUNTIME_ROOT = Path('/')
CMDLINE_PATH = boot_context.CMDLINE_PATH
MOUNTINFO_PATH = Path('/proc/self/mountinfo')
BY_UUID_PATH = Path('/dev/disk/by-uuid')
# /etc/os-release is a standard symlink to ../usr/lib/os-release on the image.
# O_NOFOLLOW rejects the symlink, so this helper reads the authoritative
# root-owned regular file directly instead of resolving a mutable /etc link.
OS_RELEASE_PATH = Path('/usr/lib/os-release')
BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')
DEV_ROOT = Path('/dev')
CMDLINE_MAX = boot_context.CMDLINE_MAX
OS_RELEASE_MAX = 64 * 1024
BOOT_ID_MAX = 64

ROOT_MOUNT = '/sysroot'
BOOT_MOUNT = '/boot'

# Filesystems whose mountinfo major:minor is the anonymous filesystem device
# number instead of the block device that backs the mount.
_ANONYMOUS_DEVICE_FILESYSTEMS = frozenset({'btrfs'})

LEGACY_BOOT_CONTEXT = 'legacy-controller'
WITNESS_BOOT_CONTEXT = 'witness'

_OSNAME = re.compile(r'[0-9A-Za-z][0-9A-Za-z_.-]{0,63}\Z')
_OSTREE = re.compile(
    r'/ostree/boot\.([01])/([0-9A-Za-z][0-9A-Za-z_.-]{0,63})/'
    r'([0-9a-f]{64})/(0|[1-9][0-9]{0,9})\Z')
_UUID = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_ZERO_UUID = '00000000-0000-0000-0000-000000000000'
_UINT32_MAX = 0xFFFFFFFF
_CHANNEL_TAGS = {'stable': 'stable', 'beta': 'beta', 'testing': 'preview'}
_CANONICAL_CHANNELS = ('stable', 'beta', 'preview')


class OfflineContextError(ValueError):
    """Legacy boot evidence is missing, inconsistent, or not trustworthy."""


def _tokens(text):
    return [token for token in re.split(r'[ \t\n\r\f\v]+', text) if token]


def _read_cmdline(path=CMDLINE_PATH):
    try:
        raw = boot_context._read_regular(path, CMDLINE_MAX)
    except Exception as error:
        raise OfflineContextError(f'cannot read the running cmdline: {error}') from error
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        raise OfflineContextError('running cmdline is not valid UTF-8') from error
    if '\x00' in text:
        raise OfflineContextError('running cmdline contains a NUL byte')
    return text


def new_evidence_present(*, witness_path=None, cmdline_path=CMDLINE_PATH):
    """Return True when modern firmware evidence may exist.

    A missing/unreadable probe fails closed: without proof of absence the legacy
    path is never allowed to take over.
    """
    try:
        text = _read_cmdline(cmdline_path)
    except OfflineContextError:
        return True
    for token in _tokens(text):
        if token == 'armada.deployment' or token.startswith('armada.deployment='):
            return True
    path = boot_context.WITNESS_PATH if witness_path is None else Path(witness_path)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _gi():
    import gi
    gi.require_version('OSTree', '1.0')
    from gi.repository import Gio, GLib, OSTree
    return Gio, GLib, OSTree


def _boot_options(deployment):
    bootconfig = deployment.get_bootconfig()
    if bootconfig is None:
        raise OfflineContextError('booted deployment has no bootconfig')
    get_string = getattr(bootconfig, 'get_string', None)
    if callable(get_string):
        try:
            return get_string('bootconfig', 'options')
        except Exception:
            pass
    return bootconfig.get('options')


def _origin_ref(deployment):
    origin = deployment.get_origin()
    if origin is None:
        return None
    try:
        value = origin.get_string('origin', 'container-image-reference')
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _deployment_identity(deployment, label):
    """Return the validated ``(osname, checksum, deployserial)`` of a deployment."""
    osname = deployment.get_osname()
    checksum = deployment.get_csum()
    serial = deployment.get_deployserial()
    if not isinstance(osname, str) or _OSNAME.fullmatch(osname) is None:
        raise OfflineContextError(f'{label} deployment osname is malformed')
    if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
        raise OfflineContextError(f'{label} deployment checksum is malformed')
    if type(serial) is not int or not 0 <= serial <= _UINT32_MAX:
        raise OfflineContextError(f'{label} deployment serial is not a uint32 integer')
    return osname, checksum, serial


def _read_ostree_booted(sysroot_path=None):
    """Read the booted deployment with libostree without changing any sysroot.

    ``sysroot_path`` is a diagnostic/test override only.  Production callers
    pass nothing so the *default* runtime sysroot decides which deployment is
    booted; ``Sysroot.new('/sysroot')`` loads but reports no booted deployment
    in the composefs runtime, and a booted deployment is never inferred from
    the deployment list or the ostree pointer index.

    A pending transaction never hides the running system: the booted identity
    is returned with the staged deployment or queued rollback so read-only
    queries can describe the device.  Only :func:`read_legacy_boot_state`
    refuses a pending transaction, and only for its mutation callers.
    """
    try:
        Gio, _GLib, OSTree = _gi()
    except Exception as error:
        raise OfflineContextError(f'OSTree introspection is unavailable: {error}') from error
    if sysroot_path is None:
        root = None
    else:
        root = Path(sysroot_path)
        if root == RUNTIME_ROOT:
            root = None
        elif not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise OfflineContextError('sysroot must be an absolute non-symlink directory')
    try:
        if root is None:
            sysroot = OSTree.Sysroot.new_default()
        else:
            sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
        if not sysroot.load(None):
            raise OfflineContextError('cannot load the OSTree sysroot')
        deployment = sysroot.get_booted_deployment()
        staged_deployment = sysroot.get_staged_deployment()
    except OfflineContextError:
        raise
    except Exception as error:
        raise OfflineContextError(f'cannot read the booted OSTree deployment: {error}') from error
    if deployment is None:
        raise OfflineContextError('libostree reports no booted deployment')

    osname, checksum, serial = _deployment_identity(deployment, 'booted')
    bootserial = deployment.get_bootserial()
    bootcsum = deployment.get_bootcsum()
    if type(bootserial) is not int or not 0 <= bootserial <= _UINT32_MAX:
        raise OfflineContextError('booted deployment boot serial is not a uint32 integer')
    if not isinstance(bootcsum, str) or _HEX64.fullmatch(bootcsum) is None:
        raise OfflineContextError('booted deployment boot checksum is malformed')
    options = _boot_options(deployment)
    if not isinstance(options, str) or not options or len(options) > CMDLINE_MAX:
        raise OfflineContextError('booted deployment options are missing or oversized')

    staged = None
    if staged_deployment is not None:
        staged_osname, staged_checksum, staged_serial = _deployment_identity(
            staged_deployment, 'staged')
        staged = {'stateroot': staged_osname, 'checksum': staged_checksum,
                  'serial': staged_serial, 'incompatible': False}
    # libostree reports the deployment that boots next first.  The pending
    # default change is a comparison of that first deployment with the booted
    # one; a pinned deployment appended by prepare stays behind the default, so
    # it is never mistaken for a rollback.  Without a usable ordering the
    # pending state is unknown and must not be reported as "nothing queued".
    try:
        default_identity = _deployment_identity(
            sysroot.get_deployments()[0], 'default')
    except OfflineContextError:
        raise
    except Exception as error:
        raise OfflineContextError(
            f'cannot establish the default deployment order: {error}') from error
    rollback_queued = staged is None and default_identity != (osname, checksum, serial)
    return {
        'osname': osname,
        'checksum': checksum,
        'serial': serial,
        'bootserial': bootserial,
        'bootcsum': bootcsum,
        'options': options,
        'originRef': _origin_ref(deployment),
        'staged': staged,
        'rollback_queued': rollback_queued,
        'booted': {
            'stateroot': osname,
            'checksum': checksum,
            'serial': serial,
            'incompatible': False,
        },
    }


def _armada_os_release(path=OS_RELEASE_PATH):
    try:
        raw = boot_context._read_regular(path, OS_RELEASE_MAX)
    except Exception as error:
        raise OfflineContextError(f'cannot read the Armada os-release: {error}') from error
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        raise OfflineContextError('os-release is not valid UTF-8') from error
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _separator, value = line.partition('=')
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        fields[key] = value
    if fields.get('ID') != 'armada':
        raise OfflineContextError('the running system is not a recognized Armada OS')
    if not fields.get('VERSION_ID'):
        raise OfflineContextError('recognized Armada os-release has no VERSION_ID')
    return fields


def _unescape_mount(value):
    for encoded, plain in (('\\040', ' '), ('\\011', '\t'), ('\\012', '\n'), ('\\134', '\\')):
        value = value.replace(encoded, plain)
    return value


def _mount_entries(mountpoint, mountinfo_path):
    """Return the single mountinfo entry that carries ``mountpoint``.

    The optional-fields/`` - ``/filesystem/source layout is parsed explicitly:
    the major:minor in the fifth field belongs to the mount, not necessarily to
    the block device (Btrfs reports an anonymous filesystem device such as
    0:38 while the source is /dev/sda16).
    """
    try:
        text = Path(mountinfo_path).read_text(encoding='utf-8')
    except OSError as error:
        raise OfflineContextError(f'cannot read {mountinfo_path}: {error}') from error
    entries = []
    for line in text.splitlines():
        if not line.strip():
            continue
        left, separator, right = line.partition(' - ')
        fields = left.split()
        if len(fields) >= 5 and _unescape_mount(fields[4]) == mountpoint:
            if not separator:
                raise OfflineContextError(
                    f'mountinfo entry for {mountpoint} has no separator')
            after = right.split()
            if len(fields) < 6 or len(after) < 2:
                raise OfflineContextError(f'malformed mountinfo entry for {mountpoint}')
            entries.append((fields, after))
    if len(entries) != 1:
        raise OfflineContextError(f'expected exactly one mount for {mountpoint}')
    return entries[0]


def _resolve_block_source(source, label):
    """Resolve one mountinfo source to the block device that backs it."""
    path = Path(source)
    if not path.is_absolute():
        raise OfflineContextError(f'{label} mount source is not absolute: {source!r}')
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise OfflineContextError(
            f'cannot resolve the {label} mount source: {error}') from error
    if DEV_ROOT not in resolved.parents:
        raise OfflineContextError(
            f'{label} mount source is outside {DEV_ROOT}: {resolved}')
    try:
        info = os.stat(resolved)
    except OSError as error:
        raise OfflineContextError(
            f'cannot stat the {label} mount source: {error}') from error
    if not stat.S_ISBLK(info.st_mode):
        raise OfflineContextError(
            f'{label} mount source is not a block device: {resolved}')
    return info.st_rdev


def _mount_device(mountpoint, mountinfo_path):
    """Return the block device rdev behind exactly one ``mountpoint`` mount.

    A Btrfs subvolume mount is normal: its ``root`` field is not ``/`` and its
    mountinfo major:minor is anonymous, so the source and filesystem are taken
    from the `` - ``-separated tail.  Non-Btrfs mounts additionally have to
    agree with their major:minor, which rejects unrelated or malformed sources.
    """
    fields, after = _mount_entries(mountpoint, mountinfo_path)
    major, separator, minor = fields[2].partition(':')
    if not separator or not major.isdigit() or not minor.isdigit():
        raise OfflineContextError(f'malformed mountinfo device for {mountpoint}')
    filesystem = after[0]
    source = _unescape_mount(after[1])
    if not filesystem or filesystem in ('none', 'nodev') or not source:
        raise OfflineContextError(f'unrelated mountinfo source for {mountpoint}')
    rdev = _resolve_block_source(source, mountpoint)
    if (filesystem not in _ANONYMOUS_DEVICE_FILESYSTEMS and
            os.makedev(int(major), int(minor)) != rdev):
        raise OfflineContextError(
            f'{mountpoint} mount device does not match its block source')
    return rdev


def _uuid_entry_rdev(entry):
    try:
        resolved = entry.resolve(strict=True)
        info = os.lstat(resolved)
    except OSError:
        return None
    return info.st_rdev if stat.S_ISBLK(info.st_mode) else None


def read_mount_uuid(mountpoint, *, mountinfo_path=MOUNTINFO_PATH, by_uuid_path=BY_UUID_PATH):
    """Return the filesystem UUID of the block device mounted at ``mountpoint``."""
    rdev = _mount_device(mountpoint, mountinfo_path)
    try:
        entries = sorted(Path(by_uuid_path).iterdir())
    except OSError as error:
        raise OfflineContextError(f'cannot list {by_uuid_path}: {error}') from error
    matches = set()
    for entry in entries:
        name = entry.name.lower()
        if _UUID.fullmatch(name) is None:
            continue
        if _uuid_entry_rdev(entry) == rdev:
            matches.add(name)
    if len(matches) != 1:
        raise OfflineContextError(f'expected one UUID for the {mountpoint} mount')
    return matches.pop()


def _ostree_pointer(tokens, label):
    values = []
    for token in tokens:
        if token == 'ostree':
            raise OfflineContextError(f'{label} contains a bare ostree token')
        if token.startswith('ostree='):
            values.append(token[len('ostree='):])
    if len(values) != 1:
        raise OfflineContextError(f'{label} must contain exactly one ostree token')
    match = _OSTREE.fullmatch(values[0])
    if match is None:
        raise OfflineContextError(f'{label} ostree pointer is malformed')
    bootver, name, bootcsum, index = match.groups()
    if int(index) > _UINT32_MAX:
        raise OfflineContextError(f'{label} ostree index exceeds uint32')
    return bootver, name, bootcsum, int(index)


def _uuid_token(tokens, key, label):
    prefix = f'{key}='
    values = []
    for token in tokens:
        if token == key:
            raise OfflineContextError(f'{label} contains a bare {key} token')
        if token.startswith(prefix):
            values.append(token[len(prefix):])
    if len(values) != 1:
        raise OfflineContextError(f'{label} must contain exactly one {key} token')
    value = values[0]
    if not value.startswith('UUID='):
        raise OfflineContextError(f'{label} {key}= must use UUID= syntax')
    value = value[len('UUID='):].lower()
    if _UUID.fullmatch(value) is None or value == _ZERO_UUID:
        raise OfflineContextError(f'{label} has an invalid or zero {key} UUID')
    return value


def _reject_new_tokens(tokens, label):
    for token in tokens:
        if token == 'armada.deployment' or token.startswith('armada.deployment='):
            raise OfflineContextError(
                f'{label} carries new deployment evidence; legacy binding refused')


def bind_legacy_evidence(evidence, cmdline, root_uuid, boot_uuid):
    """Validate legacy boot evidence and return the bound boot context.

    ``root_uuid``/``boot_uuid`` are the UUIDs of the internal partitions the
    running /sysroot and /boot are mounted from.
    """
    if not isinstance(evidence, dict):
        raise OfflineContextError('legacy evidence must be a mapping')
    osname = evidence.get('osname')
    checksum = evidence.get('checksum')
    serial = evidence.get('serial')
    bootserial = evidence.get('bootserial')
    bootcsum = evidence.get('bootcsum')
    if not isinstance(osname, str) or _OSNAME.fullmatch(osname) is None:
        raise OfflineContextError('legacy deployment osname is malformed')
    if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
        raise OfflineContextError('legacy deployment checksum is malformed')
    if type(serial) is not int or not 0 <= serial <= _UINT32_MAX:
        raise OfflineContextError('legacy deployment serial is not a uint32 integer')
    if type(bootserial) is not int or not 0 <= bootserial <= _UINT32_MAX:
        raise OfflineContextError('legacy deployment boot serial is not a uint32 integer')
    if not isinstance(bootcsum, str) or _HEX64.fullmatch(bootcsum) is None:
        raise OfflineContextError('legacy boot checksum is malformed')
    if not isinstance(cmdline, str):
        raise OfflineContextError('running cmdline must be text')

    cmd_tokens = _tokens(cmdline)
    _reject_new_tokens(cmd_tokens, 'running cmdline')
    bootver, name, cmd_bootcsum, index = _ostree_pointer(cmd_tokens, 'running cmdline')
    if name != osname:
        raise OfflineContextError('cmdline ostree osname differs from the booted deployment')
    if cmd_bootcsum != bootcsum:
        raise OfflineContextError('cmdline ostree checksum differs from the booted deployment')
    # The final ostree pointer index is the boot serial.  The deployment is
    # still identified by its deploy serial in the deployment string.
    if index != bootserial:
        raise OfflineContextError('cmdline ostree index differs from the boot serial')

    option_tokens = _tokens(evidence.get('options') if isinstance(
        evidence.get('options'), str) else '')
    if not option_tokens:
        raise OfflineContextError('booted deployment options are missing')
    _reject_new_tokens(option_tokens, 'booted deployment options')
    opt_bootver, opt_name, opt_bootcsum, opt_index = _ostree_pointer(
        option_tokens, 'booted deployment options')
    if (opt_bootver, opt_name, opt_bootcsum, opt_index) != (
            bootver, name, cmd_bootcsum, index):
        raise OfflineContextError(
            'booted deployment options do not describe the running ostree pointer')

    option_root = _uuid_token(option_tokens, 'root', 'booted deployment options')
    option_boot = _uuid_token(option_tokens, 'boot', 'booted deployment options')
    if option_root == option_boot:
        raise OfflineContextError('booted deployment root and boot UUIDs must differ')
    if _uuid_token(cmd_tokens, 'root', 'running cmdline') != option_root:
        raise OfflineContextError('running root UUID differs from the booted deployment')
    if _uuid_token(cmd_tokens, 'boot', 'running cmdline') != option_boot:
        raise OfflineContextError('running boot UUID differs from the booted deployment')

    if not isinstance(root_uuid, str) or root_uuid.lower() != option_root:
        raise OfflineContextError('the /sysroot mount UUID differs from the booted deployment')
    if not isinstance(boot_uuid, str) or boot_uuid.lower() != option_boot:
        raise OfflineContextError('the /boot mount UUID differs from the booted deployment')

    return {
        'state': 'available',
        'actualSlot': None,
        'inactiveSlot': None,
        'bootContext': LEGACY_BOOT_CONTEXT,
        'deployment': f'{osname}/{checksum}.{serial}',
    }


def read_boot_id(path=BOOT_ID_PATH):
    """Return the canonical kernel boot id of the running boot."""
    try:
        raw = boot_context._read_regular(path, BOOT_ID_MAX)
    except Exception as error:
        raise OfflineContextError(f'cannot read the kernel boot id: {error}') from error
    try:
        text = raw.decode('ascii').strip()
    except UnicodeError as error:
        raise OfflineContextError('the kernel boot id is not ASCII') from error
    if _UUID.fullmatch(text) is None or text == _ZERO_UUID:
        raise OfflineContextError('the kernel boot id is malformed')
    return text


def read_legacy_boot_state(*, sysroot_path=None, cmdline_path=CMDLINE_PATH,
                           os_release_path=OS_RELEASE_PATH, mount_lookup=read_mount_uuid,
                           reject_pending=True):
    """Return ``(booted, context)`` for a legacy system, or raise.

    The booted deployment comes from the default runtime sysroot; the
    ``/sysroot`` and ``/boot`` block mounts are bound separately by UUID.

    ``reject_pending`` is the mutation gate.  Preflight, prepare, execute, the
    boot writer and activation keep the default and refuse either a staged
    deployment or a queued rollback; the read-only identity query passes
    ``False`` so a connected system is still identified, with the pending
    transaction reported as data.
    """
    evidence = _read_ostree_booted(sysroot_path)
    if reject_pending:
        if evidence.get('staged') is not None:
            raise OfflineContextError(
                'a pending OSTree transaction (staged) refuses a legacy offline upgrade')
        if evidence.get('rollback_queued'):
            raise OfflineContextError(
                'a pending OSTree transaction (rollback) refuses a legacy offline upgrade')
    _armada_os_release(os_release_path)
    cmdline = _read_cmdline(cmdline_path)
    root_uuid = mount_lookup(ROOT_MOUNT)
    boot_uuid = mount_lookup(BOOT_MOUNT)
    context = bind_legacy_evidence(evidence, cmdline, root_uuid, boot_uuid)
    return dict(evidence['booted']), context


def legacy_channel(origin_ref):
    """Return ``(repository, channel, inferred)`` for a legacy origin."""
    repository, tag = armada_os.ref_repository_tag(origin_ref or '')
    if repository is None:
        # A raw local OSTree origin names a commit, not a container channel.  The
        # migration channel is inferred and explicitly recorded as such.
        return armada_os.FREEDOM, 'stable', True
    if repository in armada_os.REPOSITORIES and tag in _CHANNEL_TAGS:
        return repository, _CHANNEL_TAGS[tag], False
    raise OfflineContextError('legacy image origin is not a supported Freedom channel')


def snapshot(*, sysroot_path=None):
    """Return the strict snapshot, or an explicit legacy snapshot when allowed.

    ``sysroot_path`` is a diagnostic/test override for the booted-deployment
    read only; the authoritative value is the default runtime sysroot.  The
    legacy snapshot reports the staged deployment and a rollback queued by a
    differing default deployment as data, so a read-only query can describe the
    device while prepare still refuses either state.
    """
    try:
        return armada_os.snapshot()
    except Exception as error:
        if new_evidence_present():
            raise
        try:
            evidence = _read_ostree_booted(sysroot_path)
        except OfflineContextError:
            raise
        except Exception as exc:
            raise OfflineContextError(
                f'cannot read the legacy deployment: {exc}') from exc
    rollback_queued = evidence.get('rollback_queued')
    if type(rollback_queued) is not bool:
        raise OfflineContextError('legacy pending update state is unavailable')
    repository, channel, inferred = legacy_channel(evidence.get('originRef'))
    return {
        'booted': dict(evidence['booted']),
        'rollback': None,
        'staged': evidence.get('staged'),
        'rollback_queued': rollback_queued,
        'channel': channel,
        'repository': repository,
        'channels': list(_CANONICAL_CHANNELS) if repository in armada_os.REPOSITORIES else [],
        'bootContext': LEGACY_BOOT_CONTEXT,
        'channelInferred': inferred,
        'originRef': evidence.get('originRef'),
    }
