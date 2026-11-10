"""Read-only validation of an explicitly selected installed OSTree deployment."""

import re

from .deployment_options import (
    DeploymentOptionsError,
    _resolve_sysroot,
    _validate_entry,
)
from .deployment_store import (
    DeploymentError,
    _binding_of,
    _candidate_path,
    _gi,
    _load_manifest,
    _parse_binding,
)

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


class RecoveryDeploymentError(RuntimeError):
    """Raised when installed deployment recovery evidence is invalid."""


def _validate_uuid(value, label):
    if not isinstance(value, str):
        raise RecoveryDeploymentError(f"{label} must be str")
    if not _UUID_RE.fullmatch(value) or value == _ZERO_UUID:
        raise RecoveryDeploymentError(
            f"{label} must be a nonzero canonical lowercase UUID"
        )
    return value


def read_recovery_deployment(*, sysroot_path, expected_binding, model,
                             expected_root_uuid, expected_boot_uuid):
    """Read and verify an installed deployment without selecting a boot slot."""
    try:
        Gio, GLib, OSTree = _gi()
    except Exception as exc:
        raise RecoveryDeploymentError(f"OSTree GI unavailable: {exc}") from exc
    glib_err = getattr(GLib, "Error", Exception)
    sysroot = None
    validation_sysroot = None
    try:
        root_uuid = _validate_uuid(expected_root_uuid, "expected_root_uuid")
        boot_uuid = _validate_uuid(expected_boot_uuid, "expected_boot_uuid")
        if root_uuid == boot_uuid:
            raise RecoveryDeploymentError("root and boot UUIDs must differ")
        try:
            osname, commit, _serial = _parse_binding(expected_binding)
        except DeploymentError as exc:
            raise RecoveryDeploymentError(f"invalid expected_binding: {exc}") from exc

        root = _resolve_sysroot(sysroot_path)
        sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
        if not sysroot.load(None):
            raise RecoveryDeploymentError("sysroot load failed")
        if sysroot.get_staged_deployment() is not None:
            raise RecoveryDeploymentError("staged deployment present in sysroot")

        def _read_origin_ref(dep):
            origin = dep.get_origin()
            if origin is None:
                return None
            try:
                return origin.get_string("origin", "container-image-reference")
            except glib_err as exc:
                if (
                    hasattr(exc, "matches")
                    and (
                        exc.matches(
                            GLib.key_file_error_quark(),
                            GLib.KeyFileError.KEY_NOT_FOUND,
                        )
                        or exc.matches(
                            GLib.key_file_error_quark(),
                            GLib.KeyFileError.GROUP_NOT_FOUND,
                        )
                    )
                ):
                    return None
                raise

        def select_and_validate(loaded_sysroot):
            try:
                matches = [
                    dep for dep in (loaded_sysroot.get_deployments() or [])
                    if _binding_of(dep) == expected_binding
                ]
                if len(matches) != 1:
                    raise RecoveryDeploymentError(
                        "expected deployment binding not found exactly once"
                    )
                dep = matches[0]
                if dep.get_unlocked() != OSTree.DeploymentUnlockedState.NONE:
                    raise RecoveryDeploymentError("expected deployment is unlocked")
                path = _candidate_path(loaded_sysroot, root, dep, osname, commit)
                tokens, found_root, found_boot = _validate_entry(
                    dep, root, path, osname, {expected_binding}, "recovery"
                )
                if (found_root, found_boot) != (root_uuid, boot_uuid):
                    raise RecoveryDeploymentError(
                        "deployment root/boot UUIDs do not match expected evidence"
                    )
                origin_ref = _read_origin_ref(dep)
                return path, tokens, found_root, found_boot, origin_ref
            finally:
                loaded_sysroot = None

        path, tokens, found_root, found_boot, origin_ref = select_and_validate(sysroot)
        verified = _load_manifest(path, model)

        validation_sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root)))
        if not validation_sysroot.load(None):
            raise RecoveryDeploymentError("sysroot reload failed")
        if validation_sysroot.get_staged_deployment() is not None:
            raise RecoveryDeploymentError("staged deployment appeared during read")
        (
            final_path,
            final_tokens,
            final_root,
            final_boot,
            final_origin_ref,
        ) = select_and_validate(validation_sysroot)
        if final_path != path:
            raise RecoveryDeploymentError("deployment directory changed during read")
        if final_tokens != tokens:
            raise RecoveryDeploymentError("deployment BLS options changed during read")
        if (final_root, final_boot) != (found_root, found_boot):
            raise RecoveryDeploymentError("deployment UUIDs changed during read")
        if final_origin_ref != origin_ref:
            raise RecoveryDeploymentError("deployment origin changed during read")

        return {
            "schemaVersion": 1,
            "phase": "recovery_deployment_checked",
            "model": model,
            "installedDeployment": expected_binding,
            "path": path,
            "options": " ".join(tokens),
            "rootUuid": found_root,
            "bootUuid": found_boot,
            "originRef": origin_ref,
            "kernelRelease": verified["kernelRelease"],
        }
    except RecoveryDeploymentError:
        raise
    except (DeploymentOptionsError, DeploymentError, glib_err, OSError, RuntimeError) as exc:
        raise RecoveryDeploymentError(str(exc)) from exc
    finally:
        if validation_sysroot is not None:
            validation_sysroot.unload()
            validation_sysroot = None
        if sysroot is not None:
            sysroot.unload()
            sysroot = None
