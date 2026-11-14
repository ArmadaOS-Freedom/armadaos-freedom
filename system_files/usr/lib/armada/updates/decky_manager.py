"""Root-side Decky Loader update and repair coordinator for Armada OS."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from .decky_release import DeckyRelease, verify_payload
from .decky_service import DeckyService
from .decky_source import download_payload, fetch_preview
from .decky_transaction import DeckyTransaction

COMPONENT_NAME = "decky"
SCHEMA_VERSION = 1
DEFAULT_SERVICES_DIR = Path("/var/home/armada/homebrew/services")
DEFAULT_STATE_DIR = Path("/var/lib/armada/updates/decky")
DEFAULT_LOCK_PATH = Path("/run/lock/armada-update.lock")
LOADER_BINARY_NAME = "PluginLoader"
VERSION_FILE_NAME = ".loader.version"
MAX_VERSION_BYTES = 4096
SPACE_SAFETY_MARGIN_BYTES = 1024 * 1024


class DeckyManager:
    """Coordinates Decky Loader status, checks, updates, repairs, and recovery."""

    def __init__(
        self,
        services_dir: Path = DEFAULT_SERVICES_DIR,
        state_dir: Path = DEFAULT_STATE_DIR,
        lock_path: Path = DEFAULT_LOCK_PATH,
        service: DeckyService | None = None,
    ) -> None:
        self.services_dir = Path(services_dir)
        self.state_dir = Path(state_dir)
        self.lock_path = Path(lock_path)
        self.service = service if service is not None else DeckyService(self.services_dir)

    @property
    def binary_path(self) -> Path:
        return self.services_dir / LOADER_BINARY_NAME

    @property
    def version_path(self) -> Path:
        return self.services_dir / VERSION_FILE_NAME

    def _base_result(
        self,
        state: str,
        phase: str,
        current_version: str | None,
        target_version: str | None = None,
        error: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "component": COMPONENT_NAME,
            "schemaVersion": SCHEMA_VERSION,
            "state": state,
            "phase": phase,
            "currentVersion": current_version,
            "targetVersion": target_version,
            "requiresReboot": False,
        }
        if error is not None:
            result["error"] = error
        result.update(extra)
        return result

    def _read_installed_version(self) -> str | None:
        path = self.version_path
        # One descriptor is used for both the validation and the read, so the
        # inode that is checked for type and size is the inode whose bytes are
        # decoded.  O_NOFOLLOW refuses a symlinked version file outright and
        # O_NONBLOCK keeps a FIFO from blocking the open.
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RuntimeError(f"Invalid version file type at {path}") from exc
            raise RuntimeError(f"Failed to read {path}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError(f"Invalid version file type at {path}")
            if st.st_size > MAX_VERSION_BYTES:
                raise RuntimeError(f"Version file {path} exceeds {MAX_VERSION_BYTES} bytes")
            raw = bytearray()
            while len(raw) <= MAX_VERSION_BYTES:
                chunk = os.read(fd, MAX_VERSION_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
        except OSError as exc:
            raise RuntimeError(f"Failed to read {path}: {exc}") from exc
        finally:
            os.close(fd)
        if len(raw) > MAX_VERSION_BYTES:
            raise RuntimeError(f"Version file {path} exceeds {MAX_VERSION_BYTES} bytes")
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"Version file {path} is not valid UTF-8") from exc
        return value or None

    def _inspect_binary(self) -> tuple[bool, bool, int]:
        path = self.binary_path
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False, False, 0
        except OSError as exc:
            raise RuntimeError(f"Failed to stat {path}: {exc}") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return False, False, 0
        executable = os.access(path, os.X_OK)
        return True, executable, int(st.st_size)

    def _require_root(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError("Root privileges are required for Decky mutations")

    def _validate_or_create_state_dir(self, create: bool) -> bool:
        path = self.state_dir
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return False
            parent = path.parent
            try:
                parent_st = os.lstat(parent)
            except FileNotFoundError:
                parent.mkdir(parents=True, mode=0o755, exist_ok=True)
                parent_st = os.lstat(parent)
            if stat.S_ISLNK(parent_st.st_mode) or not stat.S_ISDIR(parent_st.st_mode):
                raise RuntimeError(f"State directory parent {parent} is not a real directory")
            path.mkdir(mode=0o700, exist_ok=False)
            os.chmod(path, 0o700)
            return True
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"State path {path} must be a real directory")
        if create:
            os.chmod(path, 0o700)
        return True

    def _check_disk_space(self, release: DeckyRelease, existing_loader_bytes: int) -> None:
        required = (4 * int(release.size)) + (2 * max(0, int(existing_loader_bytes))) + SPACE_SAFETY_MARGIN_BYTES
        for check_path in (self.state_dir, self.services_dir):
            probe = check_path
            while not probe.exists():
                probe = probe.parent
            vfs = os.statvfs(probe)
            available = int(vfs.f_bavail) * int(vfs.f_frsize)
            if available < required:
                raise RuntimeError(
                    f"Insufficient disk space at {check_path}: {available} bytes available, {required} bytes required"
                )

    def status(self) -> dict[str, Any]:
        current_version: str | None = None
        binary_present = False
        binary_executable = False
        local_error: str | None = None
        try:
            current_version = self._read_installed_version()
            binary_present, binary_executable, _ = self._inspect_binary()
        except Exception as exc:
            local_error = str(exc)

        service_active: bool | None = None
        service_error: str | None = None
        try:
            service_active = bool(self.service.is_active())
        except Exception as exc:
            service_error = f"Failed to query Decky service state: {exc}"

        error = local_error or service_error
        if error is not None:
            state = "failed"
        elif not binary_present or not binary_executable or current_version is None:
            state = "repair_required"
        elif service_active:
            state = "installed"
        else:
            state = "inactive"

        return self._base_result(
            state=state,
            phase="status",
            current_version=current_version,
            target_version=None,
            error=error,
            binaryPresent=binary_present,
            binaryExecutable=binary_executable,
            serviceActive=service_active,
        )

    def check(self) -> dict[str, Any]:
        local = self.status()
        current_version = local["currentVersion"]
        binary_present = bool(local.get("binaryPresent"))
        binary_executable = bool(local.get("binaryExecutable"))
        service_active = local.get("serviceActive")

        try:
            release = fetch_preview()
        except Exception as exc:
            return self._base_result(
                state="check_failed",
                phase="check",
                current_version=current_version,
                target_version=None,
                error=f"Failed to fetch official Decky release metadata: {exc}",
                binaryPresent=binary_present,
                binaryExecutable=binary_executable,
                serviceActive=service_active,
            )

        release_meta = {
            "releaseId": release.release_id,
            "publishedAt": release.published_at,
            "assetId": release.asset_id,
            "size": release.size,
            "sha256": release.sha256,
        }

        if local["state"] == "failed":
            return self._base_result(
                state="failed",
                phase="check",
                current_version=current_version,
                target_version=release.tag,
                error=local.get("error"),
                binaryPresent=binary_present,
                binaryExecutable=binary_executable,
                serviceActive=service_active,
                release=release_meta,
            )

        if not binary_present or not binary_executable or current_version is None:
            return self._base_result(
                state="repair_required",
                phase="check",
                current_version=current_version,
                target_version=release.tag,
                binaryPresent=binary_present,
                binaryExecutable=binary_executable,
                serviceActive=service_active,
                release=release_meta,
            )

        if current_version != release.tag:
            return self._base_result(
                state="update_available",
                phase="check",
                current_version=current_version,
                target_version=release.tag,
                binaryPresent=binary_present,
                binaryExecutable=binary_executable,
                serviceActive=service_active,
                release=release_meta,
            )

        try:
            verify_payload(release, self.binary_path)
            healthy = bool(self.service.healthy(release))
        except Exception:
            healthy = False

        state = "up_to_date" if healthy else "repair_required"
        return self._base_result(
            state=state,
            phase="check",
            current_version=current_version,
            target_version=release.tag,
            binaryPresent=binary_present,
            binaryExecutable=binary_executable,
            serviceActive=service_active,
            release=release_meta,
        )

    def recover(self) -> dict[str, Any]:
        self._require_root()
        current_version: str | None = None
        try:
            current_version = self._read_installed_version()
        except Exception:
            current_version = None

        try:
            exists = self._validate_or_create_state_dir(create=False)
            if not exists:
                return self._base_result(
                    state="idle",
                    phase="recover",
                    current_version=current_version,
                    target_version=None,
                    recovery=None,
                )
            tx = DeckyTransaction(
                services_dir=self.services_dir,
                state_dir=self.state_dir,
                lock_path=self.lock_path,
                service=self.service,
            )
            recovery = tx.recover()
        except Exception as exc:
            try:
                current_version = self._read_installed_version()
            except Exception:
                pass
            return self._base_result(
                state="failed",
                phase="recover",
                current_version=current_version,
                target_version=None,
                error=str(exc),
            )

        try:
            current_version = self._read_installed_version()
        except Exception:
            current_version = None

        if recovery is None:
            return self._base_result(
                state="idle",
                phase="recover",
                current_version=current_version,
                target_version=None,
                recovery=None,
            )

        rec_state = recovery.get("state", "completed") if isinstance(recovery, dict) else "completed"
        rec_error = recovery.get("error") or recovery.get("rollback_error") if isinstance(recovery, dict) else None
        rollback_healthy = recovery.get("rollback_service_healthy") if isinstance(recovery, dict) else None
        if rec_state == "failed" or rec_error:
            final_state = "failed"
        elif rec_state == "recovered" and rollback_healthy is False:
            final_state = "repair_required"
        else:
            final_state = "completed"
        return self._base_result(
            state=final_state,
            phase="recover",
            current_version=current_version,
            target_version=None,
            error=rec_error,
            recovery=recovery,
        )

    def apply(self) -> dict[str, Any]:
        self._require_root()
        current_version: str | None = None
        try:
            current_version = self._read_installed_version()
        except Exception:
            current_version = None

        try:
            self._validate_or_create_state_dir(create=True)
        except Exception as exc:
            return self._base_result(
                state="failed",
                phase="prepare",
                current_version=current_version,
                target_version=None,
                error=str(exc),
            )

        recovery_result = self.recover()
        if recovery_result["state"] == "failed":
            return self._base_result(
                state="failed",
                phase="recover",
                current_version=recovery_result.get("currentVersion", current_version),
                target_version=None,
                error=recovery_result.get("error") or "Recovery of previous Decky transaction failed",
                recovery=recovery_result.get("recovery"),
            )
        current_version = recovery_result.get("currentVersion", current_version)

        try:
            release = fetch_preview()
            _, _, existing_bytes = self._inspect_binary()
            self._check_disk_space(release, existing_bytes)
        except Exception as exc:
            return self._base_result(
                state="failed",
                phase="prepare",
                current_version=current_version,
                target_version=None,
                error=str(exc),
            )

        try:
            with tempfile.TemporaryDirectory(prefix="download-", dir=self.state_dir) as temp_dir_str:
                os.chmod(temp_dir_str, 0o700)
                temp_dir = Path(temp_dir_str)
                try:
                    candidate_path = download_payload(release, temp_dir / "PluginLoader-arm64")
                except Exception as exc:
                    return self._base_result(
                        state="failed",
                        phase="download",
                        current_version=current_version,
                        target_version=release.tag,
                        error=str(exc),
                    )

                tx = DeckyTransaction(
                    services_dir=self.services_dir,
                    state_dir=self.state_dir,
                    lock_path=self.lock_path,
                    service=self.service,
                )
                try:
                    tx_result = tx.install(release, candidate_path)
                except Exception as exc:
                    try:
                        current_version = self._read_installed_version()
                    except Exception:
                        pass
                    return self._base_result(
                        state="failed",
                        phase="failed",
                        current_version=current_version,
                        target_version=release.tag,
                        error=str(exc),
                    )
        except Exception as exc:
            return self._base_result(
                state="failed",
                phase="prepare",
                current_version=current_version,
                target_version=release.tag,
                error=str(exc),
            )

        try:
            current_version = self._read_installed_version()
        except Exception:
            current_version = None

        tx_state = tx_result.get("state", "failed") if isinstance(tx_result, dict) else "failed"
        tx_error = None
        if isinstance(tx_result, dict):
            tx_error = tx_result.get("error") or tx_result.get("rollback_error")

        if tx_state == "completed" and not tx_error:
            return self._base_result(
                state="completed",
                phase="completed",
                current_version=current_version,
                target_version=release.tag,
                transaction=tx_result,
            )

        return self._base_result(
            state="failed",
            phase="failed",
            current_version=current_version,
            target_version=release.tag,
            error=tx_error or "Decky installation transaction failed",
            transaction=tx_result,
        )

    def repair(self) -> dict[str, Any]:
        return self.apply()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m updates.decky_manager",
        description="Manage Armada Decky Loader updates and recovery.",
    )
    parser.add_argument(
        "command",
        choices=("status", "check", "update", "repair", "recover"),
        help="Operation to perform",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    manager = DeckyManager()

    try:
        if args.command == "status":
            result = manager.status()
        elif args.command == "check":
            result = manager.check()
        elif args.command in ("update", "repair"):
            result = manager.apply()
        elif args.command == "recover":
            result = manager.recover()
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
    except Exception as exc:
        result = manager._base_result(
            state="failed",
            phase=args.command,
            current_version=None,
            target_version=None,
            error=str(exc),
        )

    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 1 if result.get("state") in ("failed", "check_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
