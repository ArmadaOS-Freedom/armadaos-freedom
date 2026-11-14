#!/usr/bin/env python3
"""Shared EDL boot-requirements verifier.

Reads ``bootRequirements`` from edl-devices.json profiles and verifies that a
built root tree (and optionally a produced initramfs archive) contains the
required early firmware files and kernel driver modules.

Public surface:

    load_requirements(configuration, models=None) -> dict
    resolve_firmware(root, relative) -> Path
    verify_tree(root, kver, requirements) -> None  (raises ValueError)
    verify_initramfs(image, kver, requirements, work_parent=None) -> None

CLI:

    python build_files/edl_boot_requirements.py --config PATH \
        --root PATH --kver TEXT [--model ID ...] \
        --format {firmware,drivers,verify} [--initrd PATH] [--work-dir PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import lzma
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

_MODULE_BASENAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVER_SUFFIXES = (".ko", ".ko.xz", ".ko.zst")
_LEGACY_FIRMWARE_PREFIX = PurePosixPath("usr/lib/firmware")


def _has_control_characters(value):
    return any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _plain_text(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a nonempty string")
    if _has_control_characters(value):
        raise ValueError(f"{field} must not contain control characters")
    if value.strip() != value:
        raise ValueError(f"{field} must not have leading or trailing whitespace")
    return value


def _relative_firmware_path(value, field):
    _plain_text(value, field)
    if "\\" in value:
        raise ValueError(f"{field} must not contain backslashes")
    if value.startswith("/"):
        raise ValueError(f"{field} must be a relative path")
    pieces = value.split("/")
    for piece in pieces:
        if piece in ("", ".", ".."):
            raise ValueError(
                f"{field} must not contain empty, '.' or '..' segments"
            )
        if piece.strip() != piece:
            raise ValueError(f"{field} must not contain whitespace-padded segments")
        if any(character in piece for character in "\x00\x7f"):
            raise ValueError(f"{field} must not contain control characters")
    return value


def _driver_name(value, field):
    _plain_text(value, field)
    if "\\" in value or "/" in value:
        raise ValueError(f"{field} must be a bare module name")
    if not _MODULE_BASENAME_RE.match(value):
        raise ValueError(
            f"{field} must use only ASCII letters, digits, '_' and '-'"
        )
    return value


def _firmware_sha(value, field):
    _plain_text(value, field)
    if not _HASH_RE.match(value):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _string_list(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON list of strings")
    for index, item in enumerate(value):
        _plain_text(item, f"{field}[{index}]")
    return value


def _validate_boot_requirements(model, boot):
    field_prefix = f"profiles.{model}.bootRequirements"
    if not isinstance(boot, dict):
        raise ValueError(f"{field_prefix} must be a JSON object")

    if "earlyFirmware" not in boot:
        raise ValueError(f"{field_prefix}.earlyFirmware is required")
    if "earlyDrivers" not in boot:
        raise ValueError(f"{field_prefix}.earlyDrivers is required")
    if "firmwareSha256" not in boot:
        raise ValueError(f"{field_prefix}.firmwareSha256 is required")

    firmware_raw = boot["earlyFirmware"]
    _string_list(firmware_raw, f"{field_prefix}.earlyFirmware")
    for index, item in enumerate(firmware_raw):
        _relative_firmware_path(item, f"{field_prefix}.earlyFirmware[{index}]")

    drivers_raw = boot["earlyDrivers"]
    _string_list(drivers_raw, f"{field_prefix}.earlyDrivers")
    for index, item in enumerate(drivers_raw):
        _driver_name(item, f"{field_prefix}.earlyDrivers[{index}]")

    sha_raw = boot["firmwareSha256"]
    if not isinstance(sha_raw, dict):
        raise ValueError(f"{field_prefix}.firmwareSha256 must be a JSON object")

    listed_firmware = set(firmware_raw)
    validated_sha = {}
    for firmware_path, digest in sha_raw.items():
        _relative_firmware_path(firmware_path, f"{field_prefix}.firmwareSha256 key")
        _firmware_sha(digest, f"{field_prefix}.firmwareSha256[{firmware_path!r}]")
        if firmware_path not in listed_firmware:
            raise ValueError(
                f"{field_prefix}.firmwareSha256 names unlisted firmware {firmware_path!r}"
            )
        validated_sha[firmware_path] = digest

    # Deterministic order; dedupe while preserving first occurrence.
    firmware_ordered = []
    for fw in firmware_raw:
        if fw not in firmware_ordered:
            firmware_ordered.append(fw)
    drivers_ordered = []
    for drv in drivers_raw:
        if drv not in drivers_ordered:
            drivers_ordered.append(drv)

    sha_ordered = {}
    for fw in firmware_ordered:
        if fw in validated_sha:
            sha_ordered[fw] = validated_sha[fw]

    result = {
        "earlyFirmware": firmware_ordered,
        "earlyDrivers": drivers_ordered,
        "firmwareSha256": sha_ordered,
    }

    if "moduleAliases" in boot:
        aliases_raw = boot["moduleAliases"]
        aliases_field = f"{field_prefix}.moduleAliases"
        if not isinstance(aliases_raw, dict):
            raise ValueError(f"{aliases_field} must be a JSON object")
        driver_set = set(drivers_ordered)
        aliases_ordered = {}
        for driver_key, alias_list in aliases_raw.items():
            key_field = f"{aliases_field}[{driver_key!r}]"
            _plain_text(driver_key, f"{aliases_field} key")
            if driver_key not in driver_set:
                raise ValueError(
                    f"{aliases_field} key {driver_key!r} is not listed in earlyDrivers"
                )
            if not isinstance(alias_list, list):
                raise ValueError(f"{key_field} must be a nonempty JSON list of strings")
            if not alias_list:
                raise ValueError(f"{key_field} must be a nonempty JSON list of strings")
            cleaned = []
            for index, alias in enumerate(alias_list):
                alias_field = f"{key_field}[{index}]"
                _plain_text(alias, alias_field)
                if any(character.isspace() for character in alias):
                    raise ValueError(f"{alias_field} must not contain whitespace")
                if _has_control_characters(alias):
                    raise ValueError(
                        f"{alias_field} must not contain control characters"
                    )
                cleaned.append(alias)
            aliases_ordered[driver_key] = cleaned
        result["moduleAliases"] = aliases_ordered

    return result


def load_requirements(configuration, models=None):
    """Return per-selected-model validated boot requirements in document order."""
    if not isinstance(configuration, dict):
        raise ValueError("configuration must be a JSON object")
    profiles = configuration.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("configuration.profiles must be a nonempty JSON object")

    if models is None:
        selected = list(profiles)
    else:
        try:
            requested = list(models)
        except TypeError as error:
            raise ValueError(
                "models must be a sequence of model identifiers"
            ) from error
        if not requested:
            raise ValueError("at least one model must be selected")
        seen = set()
        for item in requested:
            if not isinstance(item, str):
                raise ValueError(
                    f"model identifiers must be strings, got {type(item).__name__}"
                )
            if item not in profiles:
                raise ValueError(f"unknown model {item!r}")
            if item in seen:
                raise ValueError(f"model {item!r} selected more than once")
            seen.add(item)
        selected = [model for model in profiles if model in seen]

    result = {}
    for model in selected:
        profile = profiles[model]
        if not isinstance(profile, dict):
            raise ValueError(f"profile {model!r} must be a JSON object")
        if "bootRequirements" not in profile:
            raise ValueError(f"profiles.{model}.bootRequirements is required")
        result[model] = _validate_boot_requirements(model, profile["bootRequirements"])
    return result


def _check_relative_firmware(relative):
    if not isinstance(relative, str):
        raise ValueError("firmware path must be a string")
    if relative.startswith("/") or "\\" in relative:
        raise ValueError(f"firmware path {relative!r} must be relative")
    pieces = relative.split("/")
    for piece in pieces:
        if piece in ("", ".", ".."):
            raise ValueError(
                f"firmware path {relative!r} must not contain empty, '.' or '..' segments"
            )
    return relative


def resolve_firmware(root, relative):
    """Return Path to a nonempty usr/lib/firmware/<relative>[.xz] under root."""
    checked = _check_relative_firmware(relative)
    base = Path(root) / "usr" / "lib" / "firmware" / PurePosixPath(checked)
    raw = base
    compressed = base.with_name(base.name + ".xz")

    if raw.exists():
        if not raw.is_file():
            raise ValueError(f"firmware {checked!r} exists but is not a file")
        if raw.stat().st_size <= 0:
            raise ValueError(f"firmware {checked!r} is empty")
        return raw
    if compressed.exists():
        if not compressed.is_file():
            raise ValueError(f"firmware {checked!r}.xz exists but is not a file")
        if compressed.stat().st_size <= 0:
            raise ValueError(f"firmware {checked!r}.xz is empty")
        return compressed
    raise ValueError(f"firmware {checked!r} is missing (tried raw and .xz)")


def _normalize_module_name(name):
    return name.replace("-", "_")


def _module_filename_stem(filename):
    for suffix in _DRIVER_SUFFIXES:
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return None


def _builtin_entries(builtin_path):
    """Parse modules.builtin into {normalized name: raw entry}.

    A missing or unreadable file yields an empty mapping, matching the result
    the previous per-query helpers produced."""
    entries = {}
    if not builtin_path.is_file():
        return entries
    try:
        with open(builtin_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                entry = stripped.split()[0]
                candidate = entry.rsplit("/", 1)[-1]
                if candidate.endswith(".ko"):
                    candidate = candidate[:-3]
                entries.setdefault(_normalize_module_name(candidate), entry)
    except OSError:
        return {}
    return entries


def _modules_builtin_contains(builtin_path, module_name):
    return _normalize_module_name(module_name) in _builtin_entries(builtin_path)


def _resolve_module_file(root, kver, module_name):
    """Return module file name for *module_name* if it is a built-in module
    according to modules.builtin, else None."""
    modules_root = Path(root) / "usr" / "lib" / "modules" / kver
    return _builtin_entries(modules_root / "modules.builtin").get(
        _normalize_module_name(module_name)
    )


def _read_builtin_modinfo(modules_root):
    path = modules_root / "modules.builtin.modinfo"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    return [entry for entry in text.split("\x00") if entry]


_UNREAD_MODINFO = object()


def _module_builtin_aliases(modules_root, module_path, *, modinfo=_UNREAD_MODINFO):
    """Return alias strings declared by a built-in module, or None if the
    modinfo file cannot be read."""
    entries = (
        _read_builtin_modinfo(modules_root)
        if modinfo is _UNREAD_MODINFO
        else modinfo
    )
    if entries is None:
        return None
    normalized = module_path.removesuffix(".ko") if module_path else module_path
    basename = normalized.rsplit("/", 1)[-1] if normalized else normalized
    prefixes = tuple(
        p + ".alias=" for p in (normalized, basename) if p
    )
    aliases = []
    for entry in entries:
        for pref in prefixes:
            if entry.startswith(pref):
                aliases.append(entry[len(pref):])
                break
    return aliases


def _alias_matches(declared, requested):
    if declared == requested:
        return True
    if declared == requested + "C*":
        return True
    return False


def _read_modules_alias(modules_root):
    """Parse modules.alias once for one module tree.

    Returns ``("missing", None)`` when absent, ``("error", message)`` when it
    cannot be read, or ``("ok", table)`` where *table* maps each declared alias
    to a list of ``(normalized target, raw target)`` pairs.
    """
    aliases_path = modules_root / "modules.alias"
    if not aliases_path.is_file():
        return "missing", None
    try:
        text = aliases_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return "error", str(error)
    table = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith("alias "):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        declared_alias = parts[1]
        target = parts[2].rsplit("/", 1)[-1]
        if target.endswith(".ko"):
            target = target[:-3]
        table.setdefault(declared_alias, []).append(
            (_normalize_module_name(target), parts[2])
        )
    return "ok", table


class _ModuleIndex:
    """Call-scoped index of one (root, kver) module tree.

    ``verify_tree`` builds one instance and shares it across every driver and
    alias check, so the kernel tree is walked once and modules.builtin,
    modules.builtin.modinfo and modules.alias are each read at most once.
    It is deliberately never cached across calls: a later ``verify_tree``
    re-reads the filesystem and therefore observes added or removed modules.
    """

    def __init__(self, root, kver):
        self.modules_root = Path(root) / "usr" / "lib" / "modules" / kver
        self.kver = kver
        self._loadable_names = None
        self._builtin_names = None
        self._modinfo_entries = None
        self._modinfo_loaded = False
        self._alias_state = None

    def _loadable(self):
        if self._loadable_names is None:
            names = set()
            kernel_dir = self.modules_root / "kernel"
            if kernel_dir.is_dir():
                for path in kernel_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    stem = _module_filename_stem(path.name)
                    if stem is None:
                        continue
                    try:
                        if path.stat().st_size <= 0:
                            continue
                    except OSError:
                        continue
                    names.add(_normalize_module_name(stem))
            self._loadable_names = names
        return self._loadable_names

    def _builtin(self):
        if self._builtin_names is None:
            self._builtin_names = _builtin_entries(
                self.modules_root / "modules.builtin"
            )
        return self._builtin_names

    def _modinfo(self):
        if not self._modinfo_loaded:
            self._modinfo_entries = _read_builtin_modinfo(self.modules_root)
            self._modinfo_loaded = True
        return self._modinfo_entries

    def module_present(self, module_name):
        if not self.modules_root.is_dir():
            return False
        wanted = _normalize_module_name(module_name)
        if wanted in self._builtin():
            return True
        return wanted in self._loadable()

    def module_aliases(self, module_path):
        return _module_builtin_aliases(
            self.modules_root, module_path, modinfo=self._modinfo()
        )

    def alias_present(self, module_name, alias):
        """Return (ok, diagnostic) for requested alias mapped to module_name."""
        module_path = self._builtin().get(_normalize_module_name(module_name))
        if module_path is not None:
            declared = self.module_aliases(module_path)
            if declared is None:
                return (
                    False,
                    f"modules.builtin.modinfo unreadable for driver {module_name!r} "
                    f"alias {alias!r}",
                )
            for declared_alias in declared:
                if _alias_matches(declared_alias, alias):
                    return True, None
            return (
                False,
                f"driver {module_name!r} is built-in but alias {alias!r} not declared "
                f"in modules.builtin.modinfo",
            )

        if self._alias_state is None:
            self._alias_state = _read_modules_alias(self.modules_root)
        status, payload = self._alias_state
        if status == "missing":
            return (
                False,
                f"driver {module_name!r} alias {alias!r} cannot be verified: "
                f"modules.alias missing under usr/lib/modules/{self.kver}",
            )
        if status == "error":
            return (
                False,
                f"driver {module_name!r} alias {alias!r}: "
                f"could not read modules.alias: {payload}",
            )
        wanted = _normalize_module_name(module_name)
        targets = payload.get(alias, []) + payload.get(alias + "C*", [])
        if any(target == wanted for target, _ in targets):
            return True, None
        if targets:
            detail = ", ".join(sorted({raw for _, raw in targets}))
            return (
                False,
                f"driver {module_name!r} alias {alias!r} is declared for {detail}",
            )
        return (
            False,
            f"driver {module_name!r} alias {alias!r} not found in modules.alias",
        )


def _module_present(root, kver, module_name, index=None):
    if index is None:
        index = _ModuleIndex(root, kver)
    return index.module_present(module_name)


def _module_alias_present(root, kver, module_name, alias, index=None):
    """Return (ok, diagnostic) for requested alias mapped to module_name."""
    if index is None:
        index = _ModuleIndex(root, kver)
    return index.alias_present(module_name, alias)


def _read_firmware_bytes(path):
    data = Path(path).read_bytes()
    lowered = path.name.lower()
    if lowered.endswith(".xz"):
        return lzma.decompress(data)
    return data


def verify_tree(root, kver, requirements):
    """Verify requirements against an extracted tree; raise ValueError if failing."""
    _plain_text(kver, "kver")
    if not isinstance(requirements, dict) or not requirements:
        raise ValueError("requirements must be a nonempty mapping")

    root_path = Path(root)
    if not root_path.is_dir():
        raise ValueError(f"root {root!r} is not a directory")

    index = _ModuleIndex(root_path, kver)
    errors = []
    for model, req in requirements.items():
        if not isinstance(req, dict):
            raise ValueError(f"requirements for {model!r} must be a mapping")
        firmware = req.get("earlyFirmware", [])
        drivers = req.get("earlyDrivers", [])
        sha_map = req.get("firmwareSha256", {})
        alias_map = req.get("moduleAliases") or {}

        for firmware_path in firmware:
            try:
                resolved = resolve_firmware(root_path, firmware_path)
            except (ValueError, OSError) as error:
                errors.append(f"{model}: firmware: {error}")
                continue
            try:
                payload = _read_firmware_bytes(resolved)
            except (OSError, lzma.LZMAError) as error:
                errors.append(
                    f"{model}: firmware {firmware_path!r} could not be decompressed: {error}"
                )
                continue
            if not payload:
                errors.append(
                    f"{model}: firmware {firmware_path!r} decompresses to an empty payload"
                )
                continue
            pinned = sha_map.get(firmware_path)
            if pinned is not None:
                digest = hashlib.sha256(payload).hexdigest()
                if digest != pinned:
                    errors.append(
                        f"{model}: firmware {firmware_path!r} sha256 mismatch "
                        f"(expected {pinned}, got {digest})"
                    )

        for module_name in drivers:
            try:
                present = _module_present(root_path, kver, module_name, index=index)
            except OSError as error:
                errors.append(f"{model}: driver {module_name!r}: {error}")
                continue
            if not present:
                errors.append(
                    f"{model}: driver {module_name!r} missing under usr/lib/modules/{kver}"
                )
                continue
            for alias in alias_map.get(module_name, []):
                try:
                    ok, detail = _module_alias_present(
                        root_path, kver, module_name, alias, index=index
                    )
                except OSError as error:
                    ok = False
                    detail = str(error)
                if not ok:
                    errors.append(
                        f"{model}: driver {module_name!r} alias {alias!r}: {detail}"
                    )

    if errors:
        raise ValueError("; ".join(errors))
    return None


def verify_initramfs(image, kver, requirements, work_parent=None, *, expected_files=None):
    """Unpack *image* with lsinitrd into a temporary dir and verify the tree."""
    image_path = Path(image)
    if not image_path.is_absolute():
        image_path = image_path.resolve()
    if not image_path.is_file():
        raise ValueError(f"initramfs image {str(image_path)!r} is not a file")
    if image_path.stat().st_size <= 0:
        raise ValueError(f"initramfs image {str(image_path)!r} is empty")

    temp_kwargs = {}
    if work_parent is not None:
        parent = Path(work_parent)
        if not parent.is_dir():
            raise ValueError(f"work-dir {str(parent)!r} is not a directory")
        temp_kwargs["dir"] = str(parent)

    with tempfile.TemporaryDirectory(**temp_kwargs) as workdir:
        command = ["lsinitrd", "--unpack", str(image_path)]
        try:
            subprocess.run(
                command,
                cwd=workdir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ValueError("lsinitrd executable not found") from error
        except subprocess.CalledProcessError as error:
            stderr = error.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            stderr = stderr.strip()
            detail = f": {stderr}" if stderr else ""
            raise ValueError(
                f"lsinitrd failed (status {error.returncode}){detail}"
            ) from error

        verify_tree(Path(workdir), kver, requirements)
        if expected_files is not None:
            if not isinstance(expected_files, dict):
                raise ValueError("expected_files must be a mapping")
            root = Path(workdir).resolve()
            for relative, item in expected_files.items():
                try:
                    _check_relative_firmware(relative)
                except ValueError as error:
                    raise ValueError(f"invalid expected initramfs path {relative!r}: {error}") from error
                if (
                    not isinstance(item, (tuple, list))
                    or len(item) != 2
                    or not isinstance(item[0], bytes)
                    or not isinstance(item[1], bool)
                ):
                    raise ValueError(
                        f"expected_files[{relative!r}] must be (bytes, executable_bool)"
                    )
                expected_bytes, executable = item
                candidate = root / PurePosixPath(relative)
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, RuntimeError, ValueError) as error:
                    raise ValueError(
                        f"expected initramfs file {relative!r} is missing or escapes the extracted root"
                    ) from error
                if not resolved.is_file():
                    raise ValueError(f"expected initramfs file {relative!r} is not regular")
                if resolved.read_bytes() != expected_bytes:
                    raise ValueError(f"expected initramfs file {relative!r} has stale content")
                if executable and not resolved.stat().st_mode & 0o111:
                    raise ValueError(f"expected initramfs script {relative!r} is not executable")
    return None


def _collect_firmware_paths(root, requirements):
    seen = set()
    ordered = []
    for model in requirements:
        for relative in requirements[model]["earlyFirmware"]:
            path = resolve_firmware(root, relative)
            # Use the lexically-absolute requested path so firmware symlink
            # aliases preserve the requested name for dracut --install.
            lexical = os.path.abspath(str(path))
            if lexical not in seen:
                seen.add(lexical)
                ordered.append(lexical)
    return ordered


def _collect_driver_names(requirements):
    seen = set()
    ordered = []
    for model in requirements:
        for driver in requirements[model]["earlyDrivers"]:
            if driver not in seen:
                seen.add(driver)
                ordered.append(driver)
    return ordered


def _import_device_config_module():
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    import edl_device_config  # noqa: WPS433 (deliberate local import)

    return edl_device_config


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify EDL boot requirements for a built tree and initramfs."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--kver", required=True, type=str)
    parser.add_argument("--model", action="append", metavar="ID")
    parser.add_argument(
        "--format", required=True, choices=("firmware", "drivers", "verify")
    )
    parser.add_argument("--initrd", type=Path)
    parser.add_argument("--work-dir", type=Path)
    arguments = parser.parse_args(argv)

    try:
        device_config = _import_device_config_module()
        configuration = device_config.load_configuration(arguments.config)
        requirements = load_requirements(configuration, arguments.model)

        verify_tree(arguments.root, arguments.kver, requirements)

        if arguments.format == "verify":
            if arguments.initrd is not None:
                verify_initramfs(
                    arguments.initrd,
                    arguments.kver,
                    requirements,
                    work_parent=arguments.work_dir,
                )
            return 0

        if arguments.format == "firmware":
            for path in _collect_firmware_paths(arguments.root, requirements):
                print(path)
            return 0

        if arguments.format == "drivers":
            for name in _collect_driver_names(requirements):
                print(name)
            return 0

        raise ValueError(f"unsupported format {arguments.format!r}")
    except (OSError, ValueError, lzma.LZMAError, subprocess.SubprocessError) as error:
        print(f"edl_boot_requirements: error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
