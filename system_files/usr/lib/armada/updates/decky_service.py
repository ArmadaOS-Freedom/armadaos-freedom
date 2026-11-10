#!/usr/bin/env python3
"""Low-level systemd adapter for the plugin_loader.service unit.

Production-only adapter; used by the update transaction interface. Tests
inject alternate roots/paths via the constructor. No CLI, no user input.
"""
from __future__ import annotations

import http.client
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .decky_release import verify_payload

SYSTEMCTL = "/usr/bin/systemctl"
UNIT = "plugin_loader.service"
CHCON = "/usr/bin/chcon"
LOADER_REL = "PluginLoader"
AUTH_URL = ("127.0.0.1", 1337, "/auth/token")
PROBE_TIMEOUT = 2
MIN_SAMPLE_SEPARATION = 0.25


class DeckyServiceError(RuntimeError):
    pass


def _require_absolute(path: Path) -> Path:
    p = str(path)
    if not p.startswith("/") or p == "/" or ".." in p.split("/"):
        raise DeckyServiceError(f"unsafe cgroup path: {p!r}")
    return Path(p)


class DeckyService:
    def __init__(
        self,
        services_dir: Path = Path("/var/home/armada/homebrew/services"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc_root: Path = Path("/proc"),
        selinux_enforce: Path = Path("/sys/fs/selinux/enforce"),
        health_seconds: int = 30,
    ) -> None:
        self.services_dir = Path(services_dir)
        self.cgroup_root = Path(cgroup_root)
        self.proc_root = Path(proc_root)
        self.selinux_enforce = Path(selinux_enforce)
        self.health_seconds = int(health_seconds)
        self._loader = self.services_dir / LOADER_REL

    # ------------------------------------------------------------------
    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [SYSTEMCTL, *args], shell=False, check=False,
            capture_output=True, timeout=40,
        )

    def _status(self) -> dict:
        res = self._systemctl(
            "show", "--property=LoadState,ActiveState,MainPID,ControlGroup",
            "--no-pager", UNIT,
        )
        if res.returncode != 0:
            raise DeckyServiceError(
                f"systemctl show failed rc={res.returncode}: "
                f"{res.stderr.decode(errors='replace')[:200]}")
        out: dict = {}
        for line in res.stdout.decode(errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in ("LoadState", "ActiveState", "MainPID", "ControlGroup"):
                    out[k] = v
        missing = {"LoadState", "ActiveState", "MainPID", "ControlGroup"} - set(out)
        if missing:
            raise DeckyServiceError(f"missing properties: {sorted(missing)}")
        if out["LoadState"] != "loaded":
            raise DeckyServiceError(
                f"unit {UNIT} not loaded (LoadState={out['LoadState']}); "
                "refusing to create it")
        try:
            out["MainPID"] = int(out["MainPID"])
        except ValueError as exc:
            raise DeckyServiceError(
                f"malformed MainPID: {out['MainPID']!r}") from exc
        if out["MainPID"] < 0:
            raise DeckyServiceError(f"negative MainPID: {out['MainPID']}")
        return out

    def _cgroup_events_populated(self, cg: str) -> Optional[int]:
        if not cg:
            return 0
        _require_absolute(cg)
        group = self.cgroup_root / cg.lstrip("/")
        events = group / "cgroup.events"
        try:
            text = events.read_text()
        except FileNotFoundError as exc:
            if not group.is_dir():
                return 0
            raise DeckyServiceError(f"missing cgroup events: {events}") from exc
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0] == "populated":
                if len(parts) != 2 or parts[1] not in ("0", "1"):
                    raise DeckyServiceError(
                        f"malformed populated value in {events}: {line!r}")
                return int(parts[1])
        raise DeckyServiceError(f"missing populated value in {events}")

    def _cgroup_empty(self, cg: str) -> bool:
        if not (self.cgroup_root / "cgroup.controllers").exists():
            raise DeckyServiceError("cgroup v2 not mounted")
        val = self._cgroup_events_populated(cg)
        return val == 0

    def is_active(self) -> bool:
        return self._status()["ActiveState"] == "active"

    def stop(self) -> None:
        status = self._status()
        original_cg = status["ControlGroup"]
        if original_cg:
            _require_absolute(original_cg)
        res = self._systemctl("stop", UNIT)
        if res.returncode != 0:
            raise DeckyServiceError(
                f"systemctl stop failed rc={res.returncode}: "
                f"{res.stderr.decode(errors='replace')[:200]}")
        final = self._status()
        if final["MainPID"] != 0 or final["ActiveState"] not in ("inactive", "failed"):
            raise DeckyServiceError(
                f"unit not stopped: MainPID={final['MainPID']} "
                f"ActiveState={final['ActiveState']}")
        final_cg = final["ControlGroup"]
        if not original_cg and not final_cg:
            if not (self.cgroup_root / "cgroup.controllers").exists():
                raise DeckyServiceError("cgroup v2 not mounted")
            return
        for cg in filter(None, {original_cg, final_cg}):
            if not self._cgroup_empty(cg):
                raise DeckyServiceError(
                    f"cgroup {cg} still populated; children alive after stop")

    def start(self) -> None:
        try:
            res = subprocess.run(
                [CHCON, "-t", "bin_t", str(self._loader)],
                shell=False, check=False, capture_output=True, timeout=40,
            )
            if res.returncode != 0:
                self._raise_if_enforcing()
        except FileNotFoundError:
            self._raise_if_enforcing()
        r1 = self._systemctl("reset-failed", UNIT)
        if r1.returncode != 0:
            raise DeckyServiceError(
                f"systemctl reset-failed failed rc={r1.returncode}: "
                f"{r1.stderr.decode(errors='replace')[:200]}")
        r2 = self._systemctl("start", UNIT)
        if r2.returncode != 0:
            raise DeckyServiceError(
                f"systemctl start failed rc={r2.returncode}: "
                f"{r2.stderr.decode(errors='replace')[:200]}")

    def _raise_if_enforcing(self) -> None:
        try:
            enforcing = self.selinux_enforce.read_text().strip() == "1"
        except FileNotFoundError:
            return  # SELinux not mounted; ignore chcon failure
        if enforcing:
            raise DeckyServiceError("chcon failed under SELinux enforcement")

    def _http_probe(self) -> bool:
        conn = http.client.HTTPConnection(AUTH_URL[0], AUTH_URL[1], timeout=PROBE_TIMEOUT)
        try:
            conn.request("GET", AUTH_URL[2])
            resp = conn.getresponse()
            ok = resp.status == 200
            resp.close()
            return ok
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def _exe_samefile(self, pid: int) -> bool:
        try:
            return os.path.samefile(
                self.proc_root / str(pid) / "exe", self._loader)
        except OSError:
            return False

    def _sample_ok(self, pid: int) -> bool:
        return (
            pid > 0
            and (self.proc_root / str(pid)).exists()
            and self._exe_samefile(pid)
            and self._http_probe()
        )

    def healthy(self, release_or_none) -> bool:
        deadline = time.monotonic() + self.health_seconds
        expected_pid: Optional[int] = None
        stable = 0
        if release_or_none is not None:
            try:
                verify_payload(release_or_none, self._loader)
                tag = release_or_none.tag
                if self._loader_tag() != tag:
                    return False
            except Exception:
                return False
        else:
            tag = None
        while time.monotonic() < deadline:
            try:
                status = self._status()
                pid = status["MainPID"]
                ok = (
                    status["ActiveState"] == "active"
                    and self._sample_ok(pid)
                    and (tag is None or self._loader_tag() == tag)
                )
            except Exception:
                ok = False
                pid = None
                status = {"ActiveState": "unknown"}
            if status.get("ActiveState") != "active":
                ok = False
            if ok and pid == expected_pid:
                stable += 1
            elif ok:
                expected_pid = pid
                stable = 1
            else:
                expected_pid = None
                stable = 0
            if stable >= 3:
                try:
                    if release_or_none is not None:
                        verify_payload(release_or_none, self._loader)
                    if tag is not None and self._loader_tag() != tag:
                        return False
                except Exception:
                    return False
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(MIN_SAMPLE_SEPARATION, remaining))
        return False

    def _loader_tag(self) -> str:
        return (self.services_dir / ".loader.version").read_text().strip()
