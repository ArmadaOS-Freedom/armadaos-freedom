#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the deterministic offline-updater bootstrap bundle.

The bundle is a plain tar (or tar.gz) of the updater runtime that already
exists in an assembled Linux rootfs.  It is deliberately *not* a system image:
only the offline updater entry point, the Python modules it actually imports,
the fixed A/B native library, the boot tool with its runtime files, the
kshim-build-boot helper and the release public key are copied.

Every required input is verified before anything is written.  A missing or
non-regular input aborts the build instead of producing a partial runtime.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile

BUNDLE_NAME = 'armada-offline-updater'
MANIFEST_NAME = 'manifest.json'

# Bundle capabilities are explicit claims about the runtime the bundle ships.
# A claim is only emitted after the source in the bundle has been checked to
# provide it, so an installer can require the contract without executing any
# bundle code.
PREFLIGHT_SESSION_CAPABILITY = 'preflight-session-v1'
MANIFEST_CAPABILITIES = (PREFLIGHT_SESSION_CAPABILITY,)

UPDATES_DIR = 'usr/lib/armada/updates'
ENTRY_PATH = 'usr/libexec/armada/armada-offline-update'
UPDATE_BOOT_DIR = 'usr/share/armada/update-boot'
BOOT_TOOL_DIR = 'usr/libexec/armada/boot-tool'
BOOT_TOOL_PATH = BOOT_TOOL_DIR + '/armada-boot-tool'
AB_LIBRARY_PATH = 'usr/libexec/armada/libarmada-update-ab.so'
DTB_HELPER_PATH = 'usr/libexec/armada/kshim-build-boot.py'
ARMADA_OS_PATH = 'usr/lib/armada/armada_os.py'
UPDATE_LIB_PATH = 'usr/lib/armada/update-lib'
# systemd-sysext merges /usr and /opt only, never /etc.  The trusted key has to
# live in the merged tree, otherwise the runtime cannot verify anything on a
# host whose /etc/pki/containers has no key yet (the pre-0.8 tablets).
TRUST_KEY_SOURCE = 'etc/pki/containers/armadaos-freedom.pub'
TRUST_KEY_PATH = 'usr/share/armada/updates/armadaos-freedom.pub'

# Modules the runtime executes by path instead of importing (runtime_health
# launches wayland_probe.py as a script), so the import closure cannot see them.
PATH_CONSUMED_MODULES = ('wayland_probe',)

# Sanity floor for the import closure: these must always be part of the
# offline prepare/execute runtime even if the entry point changes shape.
# armada_os imports updates.client at module load time, so client.py is part of
# the runtime the installer requires as well.
CORE_MODULES = (
    'client',
    'offline_rpc',
    'offline_jobs',
    'offline_session',
    'offline_prepare',
    'offline_execute',
    'offline_payload',
    'offline_boot_writer',
)

# Modules the assembled rootfs receives from build_files rather than from the
# repository system_files tree.  Naming them turns a missing file into an
# actionable error instead of a bare "missing" message.
BUILD_FILES_MODULES = ('edl_device_config', 'edl_boot_requirements')

# The legacy rollback path stages EDL boot materials from the installed updater
# sysext instead of a per-deployment manifest: the device configuration and one
# runtime per supported model, plus the rollback module itself.  The entry point
# does not import that module, so it is collected as an explicit required root.
# TB710FU has no legacy rollback runtime and must never be guessed into the
# bundle.
LEGACY_RUNTIME_MODELS = ('TB322FC', 'TB323FU')
REQUIRED_LEGACY_PATHS = (
    UPDATE_BOOT_DIR + '/edl-devices.json',
) + tuple(UPDATE_BOOT_DIR + '/runtimes/' + model + '.bin'
          for model in LEGACY_RUNTIME_MODELS)

# Match the consumer in armada-offline-bootstrap.
_MAX_BUNDLE_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_TEXT_BYTES = 256 * 1024
_MAX_FILES = 4096
_UPDATER_VERSION_LINE = re.compile(
    r'UPDATER_VERSION\s*=\s*"(\d+(?:\.\d+)*)"\s*\Z', re.ASCII)
_UPDATER_INIT_PATH = UPDATES_DIR + '/__init__.py'
_REQUIRED_EXECUTABLES = frozenset({ENTRY_PATH, BOOT_TOOL_PATH})


class BundleError(RuntimeError):
    """A required payload input is missing or unsafe."""


def _is_executable(path):
    return bool(path.stat().st_mode & 0o111)


def _require_input(root, relative, *, executable=False, allow_empty=False):
    """Return a canonical regular file below root or raise BundleError."""
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise BundleError('required input is missing: %s (%s)' % (relative, error))
    if resolved != path:
        raise BundleError('required input must be canonical, symlinks rejected: %s' % relative)
    try:
        info = path.stat()
    except OSError as error:
        raise BundleError('cannot stat required input %s: %s' % (relative, error))
    if not stat.S_ISREG(info.st_mode):
        raise BundleError('required input is not a regular file: %s' % relative)
    if info.st_size <= 0 and not allow_empty:
        raise BundleError('required input is empty: %s' % relative)
    if executable and not _is_executable(path):
        raise BundleError('required input is not executable: %s' % relative)
    return path


def _parse_module(path):
    try:
        return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise BundleError('cannot parse updater module %s: %s' % (path.name, error))


def _local_imports(path):
    """Return (required, optional) module names imported by one updater module.

    ``required`` holds relative imports and absolute ``updates.*`` imports: they
    have to resolve inside the bundled tree.  ``optional`` holds bare absolute
    names (stdlib, third-party, or a sibling installed from build_files such as
    ``edl_device_config``); they only join the closure when they resolve locally.
    """
    tree = _parse_module(path)
    required = set()
    optional = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    required.add(node.module)
                else:
                    required.update(alias.name for alias in node.names)
            elif node.module == 'updates':
                required.update(alias.name for alias in node.names)
            elif node.module and node.module.startswith('updates.'):
                required.add(node.module[len('updates.'):])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('updates.'):
                    required.add(alias.name[len('updates.'):])
                elif alias.name != 'updates':
                    optional.add(alias.name)
    return ({name for name in required if name and name != '*'}, optional)


def _flat_modules(located, dotted):
    """Return located modules, rejecting paths the bootstrap cannot install.

    armada-offline-bootstrap only accepts direct ``updates/*.py`` files, so a
    subpackage would be written into the archive and then rejected on install.
    """
    for archive_name, _path in located:
        if archive_name == ARMADA_OS_PATH:
            continue
        directory, _sep, filename = archive_name.rpartition('/')
        if directory != UPDATES_DIR or not filename.endswith('.py'):
            raise BundleError(
                'updater module %s maps to nested path %s; the offline bootstrap '
                'only accepts flat modules in %s' % (dotted, archive_name, UPDATES_DIR))
    return located


def _locate_module(updates_root, dotted):
    """Return [(archive name, source path)] for a locally resolvable import."""
    parts = dotted.split('.')
    if not parts or any(not part.isidentifier() for part in parts):
        return None
    located = []
    for index in range(1, len(parts)):
        prefix = '/'.join(parts[:index])
        init = updates_root.joinpath(*parts[:index]) / '__init__.py'
        if not init.is_file():
            return None
        located.append((UPDATES_DIR + '/' + prefix + '/__init__.py', init))
    parent = updates_root.joinpath(*parts[:-1])
    module = parent / (parts[-1] + '.py')
    if module.is_file():
        directory = UPDATES_DIR + '/'
        if len(parts) > 1:
            directory += '/'.join(parts[:-1]) + '/'
        located.append((directory + parts[-1] + '.py', module))
        return _flat_modules(located, dotted)
    init = parent / parts[-1] / '__init__.py'
    if init.is_file():
        located.append((UPDATES_DIR + '/' + '/'.join(parts) + '/__init__.py', init))
        return _flat_modules(located, dotted)
    if parts == ['armada_os']:
        support = updates_root.parent / 'armada_os.py'
        if support.is_file():
            return [(ARMADA_OS_PATH, support)]
    return None


def _missing_module_message(name):
    if name in BUILD_FILES_MODULES:
        return ('required updater module is missing: %s/%s.py '
                '(the assembled rootfs supplies it from build_files/%s.py)'
                % (UPDATES_DIR, name, name))
    if name == 'armada_os':
        return 'required input is missing: %s' % ARMADA_OS_PATH
    return 'consumed updater module is missing: %s/%s.py' % (UPDATES_DIR, name)


def _module_closure(updates_root):
    """Resolve the updater import closure, failing on any unresolved module."""
    files = {}
    names = set()
    # armada_os is imported by the updater and itself imports updates.client, so
    # it is traversed as a required support module even though it lives beside
    # the package instead of inside it.  offline_payload and offline_boot_writer
    # are the same kind of root: armada-offline-bootstrap rejects a bundle
    # without them, so they are collected even if the entry stops importing them.
    pending = [('offline_rpc', True), ('armada_os', True),
               ('offline_payload', True), ('offline_boot_writer', True)]
    while pending:
        name, required = pending.pop()
        if name in names:
            continue
        # UPDATER_VERSION is a version constant exported directly from
        # __init__.py (verified by _runtime_version), not a submodule.
        if name == 'UPDATER_VERSION':
            continue
        located = _locate_module(updates_root, name)
        if located is None:
            if not required:
                continue
            raise BundleError(_missing_module_message(name))
        names.add(name)
        for archive_name, path in located:
            files[archive_name] = path
            imports, optional = _local_imports(path)
            pending.extend((import_name, True) for import_name in sorted(imports))
            pending.extend((import_name, False) for import_name in sorted(optional))
    missing = [name for name in CORE_MODULES if name not in names]
    if missing:
        raise BundleError('updater import closure is incomplete: %s' % ', '.join(missing))
    for name in PATH_CONSUMED_MODULES + ('__init__',):
        located = _locate_module(updates_root, name)
        if located is None:
            raise BundleError(_missing_module_message(name))
        for archive_name, path in located:
            files[archive_name] = path
    return files


def _string_collection(node):
    """Return the string constants of a set/list/tuple/frozenset literal."""
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elements = node.elts
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id in ('frozenset', 'set', 'tuple', 'list')
          and len(node.args) == 1 and not node.keywords
          and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))):
        elements = node.args[0].elts
    else:
        return None
    values = set()
    for element in elements:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.add(element.value)
    return values


def _is_session_argument(node):
    """True for the exact ``_session_id(params)`` argument the RPC forwards."""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_session_id'
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == 'params')


def _provides_session_preflight(source):
    """True when the shipped RPC accepts and forwards preflight ``sessionId``.

    The installer never executes bundle code, so the contract is read from the
    source the bundle ships: the ``preflight`` operation must be statically
    reachable via a strict dispatch table, the ``_op_preflight`` handler must
    return a store ``preflight`` call that forwards the validated
    ``_session_id(params)`` as a keyword argument, and the ``_dispatch`` guard
    must compare the params keys against the exact preflight-only optional
    ``sessionId`` set.
    """
    try:
        module = ast.parse(source, filename='offline_rpc.py')
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise BundleError('shipped offline_rpc.py is not valid: %s' % exc) from exc
    handler = None
    offered = None
    dispatch = None
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == '_NORMAL_METHODS':
                    offered = _string_collection(node.value)
        elif isinstance(node, ast.FunctionDef):
            if node.name == '_op_preflight':
                handler = node
            elif node.name == '_dispatch':
                dispatch = node
    if handler is None or offered is None or dispatch is None:
        return False
    if 'preflight' not in offered:
        return False

    forwarded = False
    for node in ast.walk(handler):
        if (isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == 'preflight'
                and isinstance(node.value.func.value, ast.Attribute)
                and node.value.func.value.attr == 'store'):
            for keyword in node.value.keywords:
                if keyword.arg != 'session_id':
                    continue
                if _is_session_argument(keyword.value):
                    forwarded = True
    if not forwarded:
        return False

    guarded = False
    for node in ast.walk(dispatch):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.NotIn):
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != 'keys':
            continue
        if not isinstance(node.comparators[0], ast.Tuple):
            continue
        elements = node.comparators[0].elts
        if len(elements) != 2 or not isinstance(elements[1], ast.BinOp):
            continue
        base, extra = elements
        if not isinstance(extra.op, ast.BitOr):
            continue
        if not isinstance(extra.right, ast.Set) or len(extra.right.elts) != 1:
            continue
        member = extra.right.elts[0]
        if not isinstance(member, ast.Constant) or member.value != 'sessionId':
            continue
        if not isinstance(extra.left, ast.Name) or extra.left.id != 'expected':
            continue
        if not isinstance(base, ast.Name) or base.id != 'expected':
            continue
        guarded = True
    return guarded


def _capabilities(rpc_source):
    """Capabilities the bundle can honestly declare, or a build error."""
    if _provides_session_preflight(rpc_source):
        return MANIFEST_CAPABILITIES
    raise BundleError(
        'updater runtime does not provide session-aware preflight; cannot '
        'declare the %s capability' % PREFLIGHT_SESSION_CAPABILITY)


def _boot_tool_files(root):
    """Collect the shipped boot tool directory without following links."""
    directory = root / BOOT_TOOL_DIR
    if directory.is_symlink() or not directory.is_dir():
        raise BundleError('required input is missing: %s' % BOOT_TOOL_DIR)
    files = []
    for base, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(base) / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise BundleError('boot tool directory contains a non-regular file: %s' % path)
            files.append(path)
    if not files:
        raise BundleError('boot tool directory is empty: %s' % BOOT_TOOL_DIR)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _collect(root):
    """Return the ordered mapping archive-name -> source path."""
    updates_root = root / UPDATES_DIR
    if updates_root.is_symlink() or not updates_root.is_dir():
        raise BundleError('required input is missing: %s' % UPDATES_DIR)
    closure = _module_closure(updates_root)

    sources = {}
    sources[ENTRY_PATH] = _require_input(root, ENTRY_PATH, executable=True)
    sources[BOOT_TOOL_PATH] = _require_input(root, BOOT_TOOL_PATH, executable=True)
    sources[AB_LIBRARY_PATH] = _require_input(root, AB_LIBRARY_PATH)
    sources[DTB_HELPER_PATH] = _require_input(root, DTB_HELPER_PATH)
    sources[ARMADA_OS_PATH] = _require_input(root, ARMADA_OS_PATH)
    sources[UPDATE_LIB_PATH] = _require_input(root, UPDATE_LIB_PATH)
    # Read the repository/assembled-rootfs /etc key but ship it under /usr: the
    # extension is merged into /usr and /opt, so a key placed in /etc here would
    # never become visible.  Existing /etc policy is never written by the build.
    sources[TRUST_KEY_PATH] = _require_input(root, TRUST_KEY_SOURCE)

    for archive_name, path in sorted(closure.items()):
        relative = path.relative_to(root).as_posix()
        # The package marker is legitimately empty in this tree.
        sources[archive_name] = _require_input(
            root, relative, allow_empty=archive_name == UPDATES_DIR + '/__init__.py')

    # These are required inputs even though no import reaches them: without the
    # device configuration, both model runtimes and the rollback module, an
    # installed updater could not stage legacy rollback materials at all.
    for relative in REQUIRED_LEGACY_PATHS:
        sources[relative] = _require_input(root, relative)

    for path in _boot_tool_files(root):
        relative = path.relative_to(root).as_posix()
        sources[relative] = _require_input(root, relative)

    if len(sources) + 1 > _MAX_FILES:
        raise BundleError('bundle exceeds %d files including the manifest' % _MAX_FILES)
    return sources


def _runtime_version(data):
    """Read the canonical version from the same bytes the bundle ships."""
    if len(data) > _MAX_TEXT_BYTES:
        raise BundleError('updates/__init__.py exceeds the bootstrap text limit')
    try:
        source = data.decode('ascii', 'strict')
        tree = ast.parse(source, filename=_UPDATER_INIT_PATH)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise BundleError('shipped updates/__init__.py is not valid: ' + str(exc))
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'UPDATER_VERSION':
                    if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                        raise BundleError('UPDATER_VERSION in updates/__init__.py is not a bare string constant')
                    values.append(node.value.value)
    if len(values) != 1:
        raise BundleError('updates/__init__.py must contain exactly one bare UPDATER_VERSION assignment')
    parts = values[0].split('.')
    if not 2 <= len(parts) <= 4:
        raise BundleError('UPDATER_VERSION must have 2 to 4 decimal components')
    for part in parts:
        if not part.isdigit() or not part.isascii():
            raise BundleError('UPDATER_VERSION components must be decimal')
    canonical_values = [
        match.group(1) for line in source.splitlines()
        if (match := _UPDATER_VERSION_LINE.fullmatch(line)) is not None
    ]
    if canonical_values != values:
        raise BundleError('UPDATER_VERSION must be a bare, double-quoted assignment')
    return values[0]


def _read_entries(sources):
    # A single bounded read feeds both the manifest and the tar member. A file
    # changing between collection and packaging cannot split those two views.
    entries = {}
    total = 0
    for name, path in sorted(sources.items()):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MEMBER_BYTES:
                    raise BundleError('invalid bundle member size or type: %s' % name)
                total += info.st_size
                if total > _MAX_BUNDLE_BYTES:
                    raise BundleError('bundle payload is too large')
                mode = '0755' if info.st_mode & 0o111 else '0644'
                if name in _REQUIRED_EXECUTABLES and mode != '0755':
                    raise BundleError('required runtime file is not executable: %s' % name)
                data = stream.read(info.st_size + 1)
                if len(data) != info.st_size:
                    raise BundleError('bundle member size changed while reading: %s' % name)
        except OSError as exc:
            raise BundleError('cannot read bundle member %s: %s' % (name, exc)) from exc
        entries[name] = (data, mode)
    return entries


def _manifest_bytes(entries, fmt, capabilities, version):
    files = [
        {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
         'mode': mode}
        for name, (data, mode) in entries.items()
    ]
    document = {
        'schemaVersion': 1,
        'bundle': BUNDLE_NAME,
        'version': version,
        'format': fmt,
        'entry': ENTRY_PATH,
        'capabilities': list(capabilities),
        'files': files,
    }
    manifest = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')
    if len(manifest) > _MAX_MANIFEST_BYTES:
        raise BundleError('bundle manifest is too large')
    return manifest


def _archive_bytes(entries, manifest):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT,
                      encoding='utf-8') as archive:
        for name, (data, mode) in {MANIFEST_NAME: (manifest, '0644'), **entries}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = int(mode, 8)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _write_output(output, payload, gzipped):
    if gzipped:
        buffer = io.BytesIO()
        with gzip.GzipFile(filename='', mode='wb', fileobj=buffer, mtime=0,
                           compresslevel=9) as stream:
            stream.write(payload)
        payload = buffer.getvalue()
    if len(payload) > _MAX_BUNDLE_BYTES:
        raise BundleError('bundle archive is too large')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + output.name + '.', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            os.fchmod(stream.fileno(), 0o644)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return payload


def build(root, output):
    """Build one bundle and return its summary."""
    root = Path(root)
    output = Path(output)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise BundleError('root must be an absolute non-symlink directory: %s' % root)
    if root.resolve(strict=True) != root:
        raise BundleError('root must be canonical: %s' % root)
    if not output.is_absolute() or not output.name:
        raise BundleError('output must be an absolute file path: %s' % output)

    entries = _read_entries(_collect(root))
    version = _runtime_version(entries[_UPDATER_INIT_PATH][0])
    capabilities = _capabilities(entries[UPDATES_DIR + '/offline_rpc.py'][0])
    fmt = 'tar' if output.name.endswith('.tar') else 'tar.gz'
    manifest = _manifest_bytes(entries, fmt, capabilities, version)
    payload = _archive_bytes(entries, manifest)
    written = _write_output(output, payload, gzipped=fmt == 'tar.gz')
    return {
        'output': str(output),
        'format': fmt,
        'bundle': BUNDLE_NAME,
        'version': version,
        'bytes': len(written),
        'sha256': hashlib.sha256(written).hexdigest(),
        'files': len(entries) + 1,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', required=True, type=Path,
                        help='assembled Linux rootfs to read the updater runtime from')
    parser.add_argument('--output', required=True, type=Path,
                        help='bundle path; .tar stays uncompressed, otherwise gzip')
    args = parser.parse_args(argv)
    try:
        summary = build(args.root, args.output)
    except BundleError as error:
        print('bundle build failed: %s' % error, file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
