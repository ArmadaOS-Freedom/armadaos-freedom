"""Composed read-only inspection of a known installed recovery deployment.

inspected_recovery_context() composes the already-implemented real filesystem
reader, read-only mount manager, and installed-deployment reader so a caller
holding pre-RAM-boot evidence can inspect that exact deployment while the
recovery mounts stay alive. It does not create or choose a binding, call
bootc.snapshot, fake a booted deployment, pick the first deployment, enable
RPC/writes, or alter normal updater checks.
"""

import copy
from contextlib import contextmanager
from pathlib import Path

from .channel_selection import read_installed_channel
from .deployment_store import DeploymentError, _parse_binding
from .recovery_deployment import RecoveryDeploymentError, read_recovery_deployment
from .recovery_filesystems import inspect_recovery_filesystems
from .recovery_mounts import (
    RecoveryMountError,
    mounted_recovery_filesystems,
    mounted_recovery_update_filesystems,
)

_MODELS = frozenset(("TB322FC", "TB323FU"))
_FORBIDDEN_PHYSICAL_KEYS = ("deployment", "booted")


class RecoveryContextError(RuntimeError):
    """Raised when recovery context evidence is invalid or drifted."""


def _validate_binding(expected_binding):
    # Syntax-only validation before any disk or mount operation.
    try:
        _parse_binding(expected_binding)
    except DeploymentError as exc:
        raise RecoveryContextError(f"invalid expected_binding: {exc}") from exc
    return expected_binding


def _validate_physical(expected_physical):
    """Shallow validation only; slot-level detail is inspect_recovery_slots' job."""
    if not isinstance(expected_physical, dict):
        raise RecoveryContextError("expected_physical must be dict")
    schema = expected_physical.get("schemaVersion")
    if (not isinstance(schema, int) or isinstance(schema, bool) or schema != 1):
        raise RecoveryContextError("expected_physical schemaVersion must be integer 1")
    if expected_physical.get("phase") != "recovery_slots_checked":
        raise RecoveryContextError(
            "expected_physical phase must be 'recovery_slots_checked'"
        )
    if expected_physical.get("model") not in _MODELS:
        raise RecoveryContextError(
            "expected_physical model must be one of %s" % (sorted(_MODELS),)
        )
    for key in _FORBIDDEN_PHYSICAL_KEYS:
        if key in expected_physical:
            raise RecoveryContextError(
                "expected_physical must not contain %r key" % (key,)
            )
    # Deep copy: consumer modifications must not alter the drift baseline.
    return copy.deepcopy(expected_physical)


def _read_installed(*, sysroot_path, expected_binding, model, filesystems):
    return read_recovery_deployment(
        sysroot_path=sysroot_path,
        expected_binding=expected_binding,
        model=model,
        expected_root_uuid=filesystems["root"]["uuid"],
        expected_boot_uuid=filesystems["boot"]["uuid"],
    )


def _read_channel(*, var_path, origin_ref):
    try:
        return read_installed_channel(
            state_path=Path(var_path) / "lib/armada/update-channel",
            origin_ref=origin_ref,
        )
    except (OSError, ValueError) as exc:
        raise RecoveryContextError(
            f"installed recovery channel read failed: {exc}"
        ) from exc


@contextmanager
def inspected_recovery_context(*, expected_binding, expected_physical,
                                parent_dir="/run/armada-update-recovery"):
    """Inspect an exact installed deployment while recovery mounts stay alive.

    Yields a read-only inventory dict. Never treat the yielded object as normal
    booted physical identity. Body exceptions propagate unchanged; mount
    manager cleanup errors are preserved.
    """
    expected_binding = _validate_binding(expected_binding)
    physical = _validate_physical(expected_physical)
    model = physical["model"]

    try:
        # Keep private drift evidence independent of all outward reports.
        filesystems = copy.deepcopy(
            inspect_recovery_filesystems(expected_physical=physical))
    except RecoveryMountError:
        raise
    except Exception as exc:
        raise RecoveryContextError(
            f"recovery filesystem inspection failed: {exc}") from exc

    with mounted_recovery_filesystems(
        expected_filesystems=filesystems, parent_dir=parent_dir
    ) as mount:
        try:
            installed = _read_installed(
                sysroot_path=mount["sysrootPath"],
                expected_binding=expected_binding,
                model=model,
                filesystems=filesystems,
            )
            installed_baseline = copy.deepcopy(installed)
            # Independent re-reads must equal the initially read reports.
            if inspect_recovery_filesystems() != filesystems:
                raise RecoveryContextError(
                    "recovery filesystem state drifted during deployment read")
            if _read_installed(
                sysroot_path=mount["sysrootPath"],
                expected_binding=expected_binding,
                model=model,
                filesystems=filesystems,
            ) != installed_baseline:
                raise RecoveryContextError(
                    "installed deployment report changed during read")
            channel = _read_channel(
                var_path=mount["varPath"],
                origin_ref=installed_baseline["originRef"],
            )
            if _read_channel(
                var_path=mount["varPath"],
                origin_ref=installed_baseline["originRef"],
            ) != channel:
                raise RecoveryContextError(
                    "installed channel changed during read")
        except (RecoveryContextError, RecoveryMountError):
            raise
        except RecoveryDeploymentError as exc:
            raise RecoveryContextError(str(exc)) from exc
        except Exception as exc:
            raise RecoveryContextError(
                f"recovery context pre-yield check failed: {exc}") from exc

        yield copy.deepcopy({
            "schemaVersion": 1,
            "phase": "recovery_context_checked_readonly",
            "runtime": "recovery",
            "channel": channel,
            "physical": physical,
            "filesystems": mount["filesystems"],
            "installed": installed,
            "sysrootPath": mount["sysrootPath"],
            "bootPath": mount["bootPath"],
            "varPath": mount["varPath"],
        })

        # Body exited normally; recheck before unmounting.
        if inspect_recovery_filesystems() != filesystems:
            raise RecoveryContextError(
                "recovery filesystem state drifted during inspection body")
        if _read_installed(
            sysroot_path=mount["sysrootPath"],
            expected_binding=expected_binding,
            model=model,
            filesystems=filesystems,
        ) != installed_baseline:
            raise RecoveryContextError(
                "installed deployment report changed during inspection body")
        if _read_channel(
            var_path=mount["varPath"],
            origin_ref=installed_baseline["originRef"],
        ) != channel:
            raise RecoveryContextError(
                "installed channel changed during inspection body")


def _normalized_installed_report(installed, *, sysroot_path):
    """Deep-copy a report and relocate only its verified deployment path."""
    try:
        sysroot_text = str(sysroot_path)
        sysroot = Path(sysroot_text)
        resolved_sysroot = sysroot.resolve(strict=True)
        if (
            not sysroot.is_absolute()
            or sysroot.is_symlink()
            or sysroot_text != str(resolved_sysroot)
        ):
            raise RecoveryContextError(
                "installed report sysroot is not a canonical absolute path")

        report = copy.deepcopy(installed)
        path_text = report["path"]
        if not isinstance(path_text, str):
            raise RecoveryContextError("installed deployment path must be str")
        path = Path(path_text)
        resolved_path = path.resolve(strict=True)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or path_text != str(resolved_path)
        ):
            raise RecoveryContextError(
                "installed deployment path is not canonical and non-symlink")
        try:
            relative_path = resolved_path.relative_to(resolved_sysroot)
        except ValueError as exc:
            raise RecoveryContextError(
                "installed deployment path is outside its validated sysroot"
            ) from exc
        report["path"] = str(relative_path)
        return report
    except RecoveryContextError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise RecoveryContextError(
            f"installed deployment report normalization failed: {exc}"
        ) from exc


@contextmanager
def opened_recovery_update_context(*, expected_binding, expected_physical,
                                   parent_dir="/run/armada-update-recovery",
                                   expected_selected_slot_after=None):
    """Revalidate the inspected deployment inside explicit update mounts.

    The caller holds recovery authorization and the updater transaction lock.
    This composes inspection and mounts only; it does not execute OSTree
    deployment operations or write boot partitions.
    """
    if expected_selected_slot_after not in (None, "a", "b"):
        raise RecoveryContextError(
            "expected_selected_slot_after must be None, 'a', or 'b'")
    with inspected_recovery_context(
        expected_binding=expected_binding,
        expected_physical=expected_physical,
        parent_dir=parent_dir,
    ) as readonly:
        baseline = copy.deepcopy(readonly)
        installed_baseline = _normalized_installed_report(
            baseline["installed"], sysroot_path=baseline["sysrootPath"])

    if (expected_selected_slot_after is not None
            and expected_selected_slot_after
            != baseline["physical"]["inactiveSlot"]):
        raise RecoveryContextError(
            "expected selected slot after update must be the inactive slot")

    # The read-only mounts and their session directory are fully gone here.
    with mounted_recovery_update_filesystems(
        expected_filesystems=baseline["filesystems"], parent_dir=parent_dir
    ) as mount:
        try:
            installed = _read_installed(
                sysroot_path=mount["sysrootPath"],
                expected_binding=baseline["installed"]["installedDeployment"],
                model=baseline["physical"]["model"],
                filesystems=baseline["filesystems"],
            )
            if _normalized_installed_report(
                installed, sysroot_path=mount["sysrootPath"]
            ) != installed_baseline:
                raise RecoveryContextError(
                    "installed deployment report differs from read-only baseline")
            channel = _read_channel(
                var_path=mount["varPath"],
                origin_ref=installed_baseline["originRef"],
            )
            if channel != baseline["channel"]:
                raise RecoveryContextError(
                    "installed channel differs from read-only baseline")
            if _normalized_installed_report(
                _read_installed(
                    sysroot_path=mount["sysrootPath"],
                    expected_binding=baseline["installed"]["installedDeployment"],
                    model=baseline["physical"]["model"],
                    filesystems=baseline["filesystems"],
                ),
                sysroot_path=mount["sysrootPath"],
            ) != installed_baseline:
                raise RecoveryContextError(
                    "installed deployment report changed during update read")
            if _read_channel(
                var_path=mount["varPath"],
                origin_ref=installed_baseline["originRef"],
            ) != baseline["channel"]:
                raise RecoveryContextError(
                    "installed channel changed during update read")
            if inspect_recovery_filesystems() != baseline["filesystems"]:
                raise RecoveryContextError(
                    "recovery filesystem state drifted during update reads")
        except (RecoveryContextError, RecoveryMountError):
            raise
        except RecoveryDeploymentError as exc:
            raise RecoveryContextError(str(exc)) from exc
        except Exception as exc:
            raise RecoveryContextError(
                f"recovery update context pre-yield check failed: {exc}"
            ) from exc

        yield copy.deepcopy({
            "schemaVersion": 1,
            "phase": "recovery_context_checked_update",
            "runtime": "recovery",
            "channel": channel,
            "physical": baseline["physical"],
            "filesystems": mount["filesystems"],
            "installed": installed,
            "sysrootPath": mount["sysrootPath"],
            "bootPath": mount["bootPath"],
            "varPath": mount["varPath"],
        })

        # New deployments may have been appended; the selected original must not change.
        expected_filesystems = copy.deepcopy(baseline["filesystems"])
        if expected_selected_slot_after is not None:
            expected_filesystems["physical"]["selectedSlot"] = (
                expected_selected_slot_after)
        if inspect_recovery_filesystems() != expected_filesystems:
            raise RecoveryContextError(
                "recovery filesystem state drifted during update body")
        if _normalized_installed_report(
            _read_installed(
                sysroot_path=mount["sysrootPath"],
                expected_binding=baseline["installed"]["installedDeployment"],
                model=baseline["physical"]["model"],
                filesystems=baseline["filesystems"],
            ),
            sysroot_path=mount["sysrootPath"],
        ) != installed_baseline:
            raise RecoveryContextError(
                "installed deployment report changed during update body")
        if _read_channel(
            var_path=mount["varPath"],
            origin_ref=installed_baseline["originRef"],
        ) != baseline["channel"]:
            raise RecoveryContextError(
                "installed channel changed during update body")
