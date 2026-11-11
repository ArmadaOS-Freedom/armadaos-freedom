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
import stat
import sys
import tarfile

BUNDLE_NAME = 'armada-offline-updater'
BUNDLE_VERSION = '0.8.0'
MANIFEST_NAME = 'manifest.json'

UPDATES_DIR = 'usr/lib/armada/updates'
ENTRY_PATH = 'usr/libexec/armada/armada-offline-update'
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
    'offline_recovery',
)

# Modules the assembled rootfs receives from build_files rather than from the
# repository system_files tree.  Naming them turns a missing file into an
# actionable error instead of a bare "missing" message.
BUILD_FILES_MODULES = ('edl_device_config', 'edl_boot_requirements')

_CHUNK = 1024 * 1024
_MAX_FILES = 4096


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
        return located
    init = parent / parts[-1] / '__init__.py'
    if init.is_file():
        located.append((UPDATES_DIR + '/' + '/'.join(parts) + '/__init__.py', init))
        return located
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
    # the package instead of inside it.
    pending = [('offline_rpc', True), ('armada_os', True)]
    while pending:
        name, required = pending.pop()
        if name in names:
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

    for path in _boot_tool_files(root):
        relative = path.relative_to(root).as_posix()
        sources[relative] = _require_input(root, relative)

    if len(sources) > _MAX_FILES:
        raise BundleError('bundle exceeds %d files' % _MAX_FILES)
    return sources


def _runtime_version(root):
    """Read the updater version constant the bundle actually ships."""
    path = _require_input(root, UPDATES_DIR + '/offline_session.py')
    data = path.read_bytes()
    marker = b"_UPDATER_VERSION = '"
    for line in data.splitlines():
        if line.startswith(marker) and line.endswith(b"'"):
            return line[len(marker):-1].decode('ascii', 'strict')
    raise BundleError('shipped offline_session.py has no _UPDATER_VERSION constant')


def _mode_text(path):
    return '0755' if _is_executable(path) else '0644'


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_bytes(entries, fmt):
    files = [
        {'path': name, 'bytes': path.stat().st_size, 'sha256': _sha256(path),
         'mode': _mode_text(path)}
        for name, path in sorted(entries.items())
    ]
    document = {
        'schemaVersion': 1,
        'bundle': BUNDLE_NAME,
        'version': BUNDLE_VERSION,
        'format': fmt,
        'entry': ENTRY_PATH,
        'files': files,
    }
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8')


def _archive_bytes(entries, manifest):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT,
                      encoding='utf-8') as archive:
        for name, data in ((MANIFEST_NAME, manifest),):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
        for name, path in sorted(entries.items()):
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            info.mtime = 0
            info.mode = 0o755 if _is_executable(path) else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.type = tarfile.REGTYPE
            with path.open('rb') as stream:
                archive.addfile(info, stream)
    return buffer.getvalue()


def _write_output(output, payload, gzipped):
    if gzipped:
        buffer = io.BytesIO()
        with gzip.GzipFile(filename='', mode='wb', fileobj=buffer, mtime=0,
                           compresslevel=9) as stream:
            stream.write(payload)
        payload = buffer.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.' + output.name + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
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

    if _runtime_version(root) != BUNDLE_VERSION:
        raise BundleError('updater runtime version does not match bundle version %s'
                          % BUNDLE_VERSION)

    entries = _collect(root)
    fmt = 'tar' if output.name.endswith('.tar') else 'tar.gz'
    manifest = _manifest_bytes(entries, fmt)
    payload = _archive_bytes(entries, manifest)
    written = _write_output(output, payload, gzipped=fmt == 'tar.gz')
    return {
        'output': str(output),
        'format': fmt,
        'bundle': BUNDLE_NAME,
        'version': BUNDLE_VERSION,
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
