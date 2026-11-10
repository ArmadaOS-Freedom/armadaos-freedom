#!/usr/bin/env python3
import hashlib
import http.client
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates.decky_release import DeckyRelease
from updates.decky_service import DeckyService, DeckyServiceError

URL = "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64"


def make_elf(machine=183):
    buf = bytearray(80)
    buf[:4] = b"\x7fELF"
    buf[4] = 2
    buf[5] = 1
    struct.pack_into("<H", buf, 18, machine)
    return bytes(buf)


def make_rel(data, tag="v1", size=None, sha256=None):
    return DeckyRelease(
        release_id=1, tag=tag, published_at="2024-01-01T00:00:00Z",
        asset_id=10, url=URL,
        size=len(data) if size is None else size,
        sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
    )


def mk_status(active="active", pid=42, cg="", load="loaded"):
    return subprocess.CompletedProcess([], 0, stdout=(
        f"LoadState={load}\nActiveState={active}\nMainPID={pid}\n"
        f"ControlGroup={cg}\n").encode(), stderr=b"")


def ok():
    return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")


def bad():
    return subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom")


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, d):
        self.t += d


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.svc = self.root / "services"
        self.svc.mkdir()
        self.cg = self.root / "cgroup"
        self.cg.mkdir()
        (self.cg / "cgroup.controllers").write_text("cpuset cpu io memory pids\n")
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.srv = DeckyService(self.svc, self.cg, self.proc,
                                self.root / "enforce", health_seconds=1)

    def loader(self, data=None, tag="v1"):
        if data is None:
            data = make_elf()
        p = self.svc / "PluginLoader"
        p.write_bytes(data)
        (self.svc / ".loader.version").write_text(tag + "\n")
        return p

    def pid_exe(self, pid, target):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "exe").symlink_to(target)

    def patch_run(self, run):
        p = mock.patch("updates.decky_service.subprocess.run", side_effect=run)
        p.start()
        self.addCleanup(p.stop)

    def patch_time(self):
        clock = Clock()
        p = mock.patch("updates.decky_service.time")
        tm = p.start()
        tm.monotonic.side_effect = clock.monotonic
        tm.sleep.side_effect = clock.sleep
        self.addCleanup(p.stop)
        return clock


class TestStop(Base):
    def test_active_to_inactive_gone_cgroup_success(self):
        seq = [
            mk_status(active="active", pid=42, cg="/system.slice/plugin_loader.service"),
            mk_status("inactive", 0, ""),
        ]
        self.patch_run(lambda cmd, **kw: seq.pop(0) if cmd[1] == "show" else ok())
        self.srv.stop()  # group dir absent -> populated treated as 0
        self.assertEqual(seq, [])

    def test_populated_1_raises(self):
        g = self.cg / "system.slice" / "plugin_loader.service"
        g.mkdir(parents=True)
        (g / "cgroup.events").write_text("populated 1\n")
        seq = [
            mk_status("active", 42, "/system.slice/plugin_loader.service"),
            mk_status("inactive", 0, "/system.slice/plugin_loader.service"),
        ]
        self.patch_run(lambda cmd, **kw: seq.pop(0) if cmd[1] == "show" else ok())
        with self.assertRaisesRegex(DeckyServiceError, "still populated"):
            self.srv.stop()

    def test_missing_events_existing_group_raises(self):
        g = self.cg / "system.slice" / "plugin_loader.service"
        g.mkdir(parents=True)
        seq = [
            mk_status("active", 42, "/system.slice/plugin_loader.service"),
            mk_status("inactive", 0, "/system.slice/plugin_loader.service"),
        ]
        self.patch_run(lambda cmd, **kw: seq.pop(0) if cmd[1] == "show" else ok())
        with self.assertRaisesRegex(DeckyServiceError, "missing cgroup events"):
            self.srv.stop()

    def test_failed_initial_no_control_group_repairs(self):
        seq = [mk_status("failed", 0, ""), mk_status("inactive", 0, "")]
        self.patch_run(lambda cmd, **kw: seq.pop(0) if cmd[1] == "show" else ok())
        self.srv.stop()

    def test_malformed_main_pid_raises(self):
        self.patch_run(lambda cmd, **kw: mk_status(pid="abc") if cmd[1] == "show" else ok())
        with self.assertRaises(DeckyServiceError):
            self.srv.stop()

    def test_original_group_checked_when_final_empty(self):
        g = self.cg / "orig.group"
        g.mkdir(parents=True)
        (g / "cgroup.events").write_text("populated 1\n")
        seq = [mk_status("active", 42, "/orig.group"), mk_status("inactive", 0, "")]
        self.patch_run(lambda cmd, **kw: seq.pop(0) if cmd[1] == "show" else ok())
        with self.assertRaisesRegex(DeckyServiceError, "still populated"):
            self.srv.stop()


class TestStart(Base):
    def test_start_ok_enforcing_but_chcon_succeeds(self):
        self.loader()
        (self.root / "enforce").write_text("1\n")
        self.patch_run(lambda cmd, **kw: ok())
        self.srv.start()

    def test_reset_failed_nonzero_raises(self):
        self.loader()
        self.patch_run(lambda cmd, **kw: bad() if cmd[1] == "reset-failed" else ok())
        with self.assertRaises(DeckyServiceError):
            self.srv.start()

    def test_start_nonzero_raises(self):
        self.loader()
        self.patch_run(lambda cmd, **kw: bad() if cmd[1] == "start" else ok())
        with self.assertRaises(DeckyServiceError):
            self.srv.start()

    def test_selinux_enforcing_chcon_fail_blocks(self):
        self.loader()
        (self.root / "enforce").write_text("1\n")
        self.patch_run(lambda cmd, **kw: bad() if cmd[0] == "/usr/bin/chcon" else ok())
        with self.assertRaises(DeckyServiceError):
            self.srv.start()

    def test_selinux_absent_chcon_missing_allows(self):
        self.loader()
        def run(cmd, **kw):
            if cmd[0] == "/usr/bin/chcon":
                raise FileNotFoundError("chcon")
            return ok()
        self.patch_run(run)
        self.srv.start()


class TestHttpProbe(Base):
    def probe(self, resp):
        conn = mock.Mock()
        conn.getresponse.return_value = resp
        p = mock.patch("updates.decky_service.http.client.HTTPConnection",
                       return_value=conn)
        with p:
            return self.srv._http_probe(), conn, resp

    def test_200_closes_without_read(self):
        resp = mock.Mock(status=200)
        result, conn, _ = self.probe(resp)
        self.assertTrue(result)
        self.assertFalse(resp.read.called)
        resp.close.assert_called_once_with()
        conn.close.assert_called_once_with()

    def test_non_200_and_http_exception_false(self):
        self.assertFalse(self.probe(mock.Mock(status=500))[0])
        conn = mock.Mock()
        conn.getresponse.side_effect = http.client.HTTPException("x")
        with mock.patch("updates.decky_service.http.client.HTTPConnection",
                        return_value=conn):
            self.assertFalse(self.srv._http_probe())


class TestHealthy(Base):
    def http_200(self):
        p = mock.patch("updates.decky_service.http.client.HTTPConnection")
        conn = p.start()
        self.addCleanup(p.stop)
        conn.return_value.getresponse.return_value = mock.Mock(status=200)

    def test_healthy_valid_hash_tag_same_pid(self):
        p = self.loader()
        rel = make_rel(p.read_bytes())
        self.pid_exe(42, p)
        self.http_200()
        self.patch_time()
        self.patch_run(lambda cmd, **kw: mk_status() if cmd[1] == "show" else ok())
        self.assertTrue(self.srv.healthy(rel))

    def test_wrong_hash_or_tag_or_missing_process_or_wrong_exe(self):
        p = self.loader()
        self.http_200()
        self.patch_time()
        run = lambda cmd, **kw: mk_status() if cmd[1] == "show" else ok()
        self.patch_run(run)
        self.assertFalse(self.srv.healthy(make_rel(p.read_bytes(), sha256="0" * 64)))
        self.assertFalse(self.srv.healthy(make_rel(p.read_bytes(), tag="v2")))
        self.loader()
        self.assertFalse(self.srv.healthy(make_rel(p.read_bytes())))  # no proc dir
        other = self.svc / "other"
        other.write_bytes(p.read_bytes())
        self.pid_exe(42, other)
        self.assertFalse(self.srv.healthy(make_rel(p.read_bytes())))
        exe = self.proc / "42" / "exe"
        exe.unlink()
        exe.symlink_to(p)
        self.assertTrue(self.srv.healthy(make_rel(p.read_bytes())))

    def test_pid_changing_resets_stability(self):
        p = self.loader()
        rel = make_rel(p.read_bytes())
        for i in range(1, 40):
            self.pid_exe(i, p)
        self.http_200()
        self.patch_time()
        n = {"i": 0}
        def run(cmd, **kw):
            if cmd[1] == "show":
                n["i"] += 1
                return mk_status(pid=n["i"])
            return ok()
        self.patch_run(run)
        self.assertFalse(self.srv.healthy(rel))

    def test_payload_changed_midprobe_cannot_return_healthy(self):
        p = self.loader()
        rel = make_rel(p.read_bytes())
        self.pid_exe(42, p)
        self.http_200()
        self.patch_time()
        n = {"i": 0}
        def run(cmd, **kw):
            if cmd[1] == "show":
                n["i"] += 1
                if n["i"] == 2:
                    p.write_bytes(make_elf(machine=62))  # same path, bad ELF
                return mk_status()
            return ok()
        self.patch_run(run)
        self.assertFalse(self.srv.healthy(rel))

    def test_release_none_skips_metadata_but_needs_active_process_http(self):
        p = self.loader()
        self.pid_exe(42, p)
        self.http_200()
        self.patch_time()
        self.patch_run(lambda cmd, **kw: mk_status() if cmd[1] == "show" else ok())
        self.assertTrue(self.srv.healthy(None))


if __name__ == "__main__":
    unittest.main()
