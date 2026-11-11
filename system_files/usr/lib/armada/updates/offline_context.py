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
# A container digest; only used to recognise a digest-pinned reference, never
# to decide the repository, which is parsed before the tag is considered.
_DIGEST = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:[-+._][A-Za-z0-9]+)*:[0-9a-fA-F]{32,128}\Z')
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


def _boot_pointer(bootver, name, bootcsum, index):
    """Return the kernel ostree pointer for one BLS boot entry."""
    return f'/ostree/boot.{bootver}/{name}/{bootcsum}/{index}'


def _resolve_boot_pointer(pointer, osname, checksum, serial, sysroot_path=None):
    """Confirm ``pointer`` resolves to the booted deployment in the sysroot.

    A rewritten BLS entry is trusted only when its symlink resolves to the
    exact deployment directory libostree reports as booted.  The boot serial,
    the running cmdline and the deployment list are never used to infer that
    target; a pointer that resolves anywhere else is refused.
    """
    if sysroot_path is None:
        root = DEFAULT_SYSROOT
    else:
        root = Path(sysroot_path)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise OfflineContextError('sysroot must be an absolute non-symlink directory')
    expected = root.joinpath(
        'ostree', 'deploy', osname, 'deploy', f'{checksum}.{serial}')
    link = root.joinpath(*pointer.lstrip('/').split('/'))
    try:
        if not link.is_symlink():
            raise OfflineContextError(
                f'the current {pointer} ostree pointer is not a symlink')
        resolved = link.resolve(strict=True)
    except OfflineContextError:
        raise
    except OSError as error:
        raise OfflineContextError(
            f'cannot resolve the current {pointer} ostree pointer: {error}') from error
    if resolved != expected:
        raise OfflineContextError(
            f'the current ostree pointer resolves to {resolved}, '
            f'not the booted deployment {expected}')
    return resolved


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


def bind_legacy_evidence(evidence, cmdline, root_uuid, boot_uuid, *, sysroot_path=None):
    """Validate legacy boot evidence and return the bound boot context.

    ``root_uuid``/``boot_uuid`` are the UUIDs of the internal partitions the
    running /sysroot and /boot are mounted from.  ``sysroot_path`` overrides
    the runtime sysroot used to resolve a rewritten BLS pointer in tests.

    libostree's booted deployment stays authoritative: the running cmdline
    must still name its osname and boot checksum, and the *current* BLS
    pointer must name its osname, boot checksum and boot serial.  A prepare
    that appended a deployment may have rewritten the boot version and/or the
    entry index while /proc/cmdline kept the original pointer, so those two
    cmdline fields are not required to match; a divergent pointer is instead
    resolved in the runtime sysroot and must land exactly on the booted
    deployment.
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

    option_tokens = _tokens(evidence.get('options') if isinstance(
        evidence.get('options'), str) else '')
    if not option_tokens:
        raise OfflineContextError('booted deployment options are missing')
    _reject_new_tokens(option_tokens, 'booted deployment options')
    opt_bootver, opt_name, opt_bootcsum, opt_index = _ostree_pointer(
        option_tokens, 'booted deployment options')
    if opt_name != osname:
        raise OfflineContextError(
            'booted deployment options ostree osname differs from the booted deployment')
    if opt_bootcsum != bootcsum:
        raise OfflineContextError(
            'booted deployment options ostree checksum differs from the booted deployment')
    # The final ostree pointer index is the boot serial.  The deployment is
    # still identified by its deploy serial in the deployment string.
    if opt_index != bootserial:
        raise OfflineContextError(
            'booted deployment options ostree index differs from the boot serial')
    if (opt_bootver, opt_name, opt_bootcsum, opt_index) != (
            bootver, name, cmd_bootcsum, index):
        # append_verified_deployment/write_deployments may rewrite the BLS
        # entry (boot version and/or index) while the running cmdline keeps the
        # pointer this boot used.  The rewritten pointer is accepted only when
        # it resolves to the deployment libostree reports as booted.
        _resolve_boot_pointer(
            _boot_pointer(opt_bootver, opt_name, opt_bootcsum, opt_index),
            osname, checksum, serial, sysroot_path=sysroot_path)

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
    ``sysroot_path`` is a diagnostic/test override for that read and for
    resolving a rewritten BLS pointer; production passes nothing.

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
    context = bind_legacy_evidence(
        evidence, cmdline, root_uuid, boot_uuid, sysroot_path=sysroot_path)
    return dict(evidence['booted']), context


def _installed_channel(state_path, origin_ref):
    """Read the validated installed channel with the shared selection policy."""
    # Imported lazily: channel_selection pulls in the trial/activation stack,
    # which imports this module, so a module-level import would be cyclic.
    from . import channel_selection
    try:
        return channel_selection.read_installed_channel(
            state_path=state_path, origin_ref=origin_ref)
    except (OSError, ValueError) as error:
        raise OfflineContextError(
            f'installed update channel state is invalid: {error}') from error


def _read_channel_state(state_path):
    """Return the validated persisted channel, or ``None`` when none exists."""
    try:
        os.lstat(state_path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OfflineContextError(
            f'cannot inspect the installed update channel: {error}') from error
    # A raw local OSTree origin names a commit, not a container channel, so it
    # cannot be handed to the strict origin parser.  The supported reference
    # only satisfies that parser; the validated state file decides the channel.
    return _installed_channel(state_path, f'{armada_os.FREEDOM}:stable')


def _container_reference(origin_ref):
    """Return the container reference of ``origin_ref``, or ``None`` for a local one.

    A legacy origin is a raw OSTree commit or ref (``armada/stable``, a 64-hex
    checksum) unless it carries a known transport prefix or names a registry in
    its first path component.  The digest is stripped only afterwards, in
    :func:`_container_repository`, so a digest-only container reference is
    never mistaken for a local commit.
    """
    if origin_ref is None:
        return None
    if not isinstance(origin_ref, str):
        raise OfflineContextError('legacy image origin is not text')
    if not origin_ref:
        return None
    if any(ord(character) < 33 or ord(character) == 127 for character in origin_ref):
        raise OfflineContextError('legacy image origin is malformed')
    for prefix in armada_os.TRANSPORTS:
        if origin_ref.startswith(prefix):
            reference = origin_ref[len(prefix):]
            if not reference:
                raise OfflineContextError('legacy image origin is malformed')
            return reference
    if '://' in origin_ref:
        raise OfflineContextError('legacy image origin uses an unsupported transport')
    head = origin_ref.split('/', 1)[0]
    if '@' in origin_ref or '.' in head or ':' in head:
        # Registry-qualified or digest-pinned: a container reference even
        # without a transport prefix, which is how libostree can report it.
        return origin_ref
    return None


def _container_repository(reference):
    """Return the repository of a container reference, independent of its tag.

    The digest and the tag are optional and are removed in that order, so a
    digest-only reference still names its repository.  A reference that cannot
    be split into a repository, an optional tag and an optional digest is
    refused instead of being guessed.
    """
    name, at, digest = reference.partition('@')
    if at and (not name or _DIGEST.fullmatch(digest) is None):
        raise OfflineContextError('legacy image origin has a malformed digest')
    separator = name.rfind(':')
    if separator > name.rfind('/'):
        repository, tag = name[:separator], name[separator + 1:]
        if not tag or '/' in tag:
            raise OfflineContextError('legacy image origin has a malformed tag')
    else:
        repository = name
    if not repository or repository.startswith('/') or '/' not in repository:
        raise OfflineContextError('legacy image origin has a malformed repository')
    return repository


def legacy_channel(origin_ref, *, state_path=None):
    """Return ``(repository, channel, inferred)`` for a legacy origin.

    The validated persisted selection in ``/var/lib/armada/update-channel``
    wins over the image origin, so a channel selected after installation is
    what the snapshot and the submission identity checks report.  The origin
    tag is a fallback only while that state file is absent, and only the
    established stable inference for a genuine raw local commit/ref remains.

    A container origin is classified by its repository, not by its tag: an
    unsupported or malformed repository is refused with or without a digest,
    and a digest-only origin of a supported repository needs the persisted
    selection instead of being inferred as stable.
    """
    if state_path is None:
        from . import channel_selection
        state_path = channel_selection.DEFAULT_STATE_PATH
    reference = _container_reference(origin_ref)
    if reference is None:
        # A raw local OSTree commit/ref names no container channel, so it is
        # never handed to the strict origin parser.  The validated state file
        # decides the channel; only a missing selection is inferred as stable
        # and explicitly recorded as such.
        channel = _read_channel_state(state_path)
        if channel is None:
            return armada_os.FREEDOM, 'stable', True
        return armada_os.FREEDOM, _CHANNEL_TAGS[channel], False
    repository = _container_repository(reference)
    if repository not in armada_os.REPOSITORIES:
        raise OfflineContextError(
            'legacy image origin is not a supported Freedom channel')
    # The source is trusted only after that check; the persisted selection then
    # still wins, and a digest-only origin without it is refused by the same
    # strict reader instead of being inferred as stable.
    return repository, _CHANNEL_TAGS[
        _installed_channel(state_path, origin_ref)], False


def snapshot(*, sysroot_path=None, state_path=None):
    """Return the strict snapshot, or an explicit legacy snapshot when allowed.

    ``sysroot_path`` is a diagnostic/test override for the booted-deployment
    read only; the authoritative value is the default runtime sysroot.  The
    legacy snapshot reports the staged deployment and a rollback queued by a
    differing default deployment as data, so a read-only query can describe the
    device while prepare still refuses either state.  ``state_path`` overrides
    the persisted update-channel file in tests only.
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
    repository, channel, inferred = legacy_channel(
        evidence.get('originRef'), state_path=state_path)
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
