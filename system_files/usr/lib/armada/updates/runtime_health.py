"""Read-only local service and Wayland compositor runtime evidence probe."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import uuid

from . import boot_context

SYSTEMCTL_BIN = "/usr/bin/systemctl"
LOGINCTL_BIN = "/usr/bin/loginctl"
PYTHON_BIN = "/usr/bin/python3"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
RUNTIME_USERS_ROOT = Path("/run/user")
WAYLAND_PROBE_PATH = Path(__file__).with_name("wayland_probe.py")
REQUIRED_UNITS: tuple[str, ...] = (
    "dbus.service",
    "systemd-logind.service",
    "local-fs.target",
)
MAX_SOCKET_CANDIDATES = 8

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SOCKET_NAME_RE = re.compile(r"^(?:wayland|gamescope)-[A-Za-z0-9_.-]+$")


class RuntimeHealthError(RuntimeError):
    """Raised when basic runtime service/compositor evidence is not ready."""


class RuntimeNotReady(RuntimeHealthError):
    """Raised when the expected Wayland session has not appeared yet.

    This is limited to the normal graphical-startup gap: logind reports no
    sessions at all, no session is an eligible local seat0 Wayland user
    session, or the user's runtime directory/socket does not exist yet.
    Ambiguous, unsafe or inconsistent evidence stays a plain
    RuntimeHealthError so it is never retried as if it were merely early.
    """


@dataclass(frozen=True)
class RuntimeEvidence:
    boot_id: str
    session_id: str
    uid: int
    display: str
    services: tuple[str, ...]


def _run_cmd(argv: list[str], *, timeout: int = 5) -> str:
    try:
        proc = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeHealthError(f"Command failed ({argv[0]}): {exc}") from exc
    return proc.stdout


def _parse_kv(output: str, required_keys: set[str], context: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise RuntimeHealthError(f"Malformed {context} line: {line!r}")
        key, value = line.split("=", 1)
        if not key or key in parsed:
            raise RuntimeHealthError(f"Invalid or duplicate key {key!r} in {context}")
        parsed[key] = value
    missing = required_keys - parsed.keys()
    if missing:
        raise RuntimeHealthError(f"Missing {sorted(missing)} in {context}")
    return parsed


def _read_boot_id() -> str:
    try:
        raw_bytes = boot_context._read_regular(Path(BOOT_ID_PATH), 64)
        raw = raw_bytes.decode("ascii").strip()
        parsed = uuid.UUID(raw)
    except UnicodeDecodeError as exc:
        raise RuntimeHealthError(f"Invalid boot_id: non-ASCII data: {exc}") from exc
    except (OSError, ValueError, boot_context.BootContextError) as exc:
        raise RuntimeHealthError(f"Invalid boot_id: {exc}") from exc
    if str(parsed) != raw.lower():
        raise RuntimeHealthError(f"Non-canonical boot_id: {raw!r}")
    return str(parsed)


def _verify_required_units() -> tuple[str, ...]:
    for unit in REQUIRED_UNITS:
        out = _run_cmd(
            [
                SYSTEMCTL_BIN,
                "show",
                "--property=LoadState,ActiveState",
                "--no-pager",
                unit,
            ],
            timeout=5,
        )
        props = _parse_kv(out, {"LoadState", "ActiveState"}, f"unit {unit}")
        if props["LoadState"] != "loaded" or props["ActiveState"] != "active":
            raise RuntimeHealthError(
                f"Unit {unit} not ready ({props['LoadState']}/{props['ActiveState']})"
            )
    return REQUIRED_UNITS


def _list_session_ids() -> list[str]:
    out = _run_cmd([LOGINCTL_BIN, "list-sessions", "--no-legend", "--no-pager"], timeout=5)
    seen: list[str] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        sid = line.split(None, 1)[0]
        if not _SESSION_ID_RE.match(sid):
            raise RuntimeHealthError(f"Invalid session id from loginctl: {sid!r}")
        if sid not in seen:
            seen.append(sid)
    if not seen:
        raise RuntimeNotReady("No login sessions reported")
    return seen


_SESSION_KEYS = {"Active", "Remote", "Type", "Class", "State", "Seat", "User"}


def _show_session(session_id: str) -> dict[str, str]:
    out = _run_cmd(
        [
            LOGINCTL_BIN,
            "show-session",
            session_id,
            "--all",
            "--no-pager",
            "--property=Active,Remote,Type,Class,State,Seat,User",
        ],
        timeout=5,
    )
    return _parse_kv(out, _SESSION_KEYS, f"session {session_id}")


def _eligible_uid(props: dict[str, str]) -> int | None:
    if (
        props["Active"] != "yes"
        or props["Remote"] != "no"
        or props["Type"] != "wayland"
        or props["Class"] != "user"
        or props["State"] != "active"
        or props["Seat"] != "seat0"
    ):
        return None
    user_raw = props["User"]
    if not user_raw.isdigit():
        return None
    uid = int(user_raw)
    return uid if uid > 0 else None


def _select_wayland_session() -> tuple[str, int, int, dict[str, str]]:
    eligible: list[tuple[str, int, dict[str, str]]] = []
    for sid in _list_session_ids():
        props = _show_session(sid)
        uid = _eligible_uid(props)
        if uid is not None:
            eligible.append((sid, uid, props))
    if not eligible:
        raise RuntimeNotReady("No eligible seat0 Wayland user session reported")
    if len(eligible) > 1:
        raise RuntimeHealthError(
            f"Expected exactly 1 eligible seat0 Wayland user session, found {len(eligible)}"
        )
    sid, uid, props = eligible[0]
    try:
        pw_gid = pwd.getpwuid(uid).pw_gid
    except KeyError as exc:
        raise RuntimeHealthError(f"Session user uid {uid} missing from passwd") from exc
    return sid, uid, pw_gid, props


def _candidate_sockets(uid: int) -> tuple[Path, list[Path]]:
    runtime_dir = Path(RUNTIME_USERS_ROOT) / str(uid)
    try:
        st = os.lstat(runtime_dir)
    except FileNotFoundError as exc:
        raise RuntimeNotReady(f"Missing runtime dir {runtime_dir}: {exc}") from exc
    except OSError as exc:
        raise RuntimeHealthError(f"Missing runtime dir {runtime_dir}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeHealthError(f"Invalid runtime dir mode for {runtime_dir}")
    if st.st_uid != uid or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise RuntimeHealthError(f"Unsafe runtime dir ownership/permissions on {runtime_dir}")
    try:
        names = sorted(os.listdir(runtime_dir))
    except FileNotFoundError as exc:
        raise RuntimeNotReady(f"Missing runtime dir {runtime_dir}: {exc}") from exc
    except OSError as exc:
        raise RuntimeHealthError(f"Cannot list runtime dir {runtime_dir}: {exc}") from exc

    sockets: list[Path] = []
    matched_name = False
    for name in names:
        # Compositors keep "<socket>.lock" sidecars next to their sockets.
        # They are never connection endpoints and must not count as socket
        # evidence, or a directory holding only sidecars would be reported as
        # unusable instead of merely not ready yet.
        if name.endswith(".lock"):
            continue
        if not _SOCKET_NAME_RE.match(name):
            continue
        matched_name = True
        candidate = runtime_dir / name
        try:
            cst = os.lstat(candidate)
        except OSError:
            continue
        if stat.S_ISLNK(cst.st_mode) or not stat.S_ISSOCK(cst.st_mode):
            continue
        if cst.st_uid != uid:
            continue
        sockets.append(candidate)
        if len(sockets) >= MAX_SOCKET_CANDIDATES:
            break
    if not sockets:
        if matched_name:
            raise RuntimeHealthError(f"No usable Wayland sockets in {runtime_dir}")
        raise RuntimeNotReady(f"No candidate Wayland sockets in {runtime_dir}")
    return runtime_dir, sockets


def _probe_sockets(runtime_dir: Path, sockets: list[Path], uid: int, gid: int) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "XDG_RUNTIME_DIR": str(runtime_dir),
    }
    for sock in sockets:
        try:
            proc = subprocess.run(
                [PYTHON_BIN, "-I", str(WAYLAND_PROBE_PATH), str(sock)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
                user=uid,
                group=gid,
                extra_groups=[],
                env=env,
                cwd="/",
            )
        except subprocess.TimeoutExpired:
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeHealthError(f"Wayland probe execution failed: {exc}") from exc
        if proc.returncode == 0:
            return sock.name
    raise RuntimeHealthError("No responsive Wayland compositor socket found")


def collect_runtime_evidence() -> RuntimeEvidence:
    boot_id_start = _read_boot_id()
    services = _verify_required_units()
    session_id, uid, gid, initial_props = _select_wayland_session()
    runtime_dir, sockets = _candidate_sockets(uid)
    display = _probe_sockets(runtime_dir, sockets, uid, gid)
    if _show_session(session_id) != initial_props:
        raise RuntimeHealthError(f"Session {session_id} changed state during probe")
    boot_id_end = _read_boot_id()
    if boot_id_end != boot_id_start:
        raise RuntimeHealthError("boot_id changed during runtime health check")
    return RuntimeEvidence(
        boot_id=boot_id_end,
        session_id=session_id,
        uid=uid,
        display=display,
        services=services,
    )
