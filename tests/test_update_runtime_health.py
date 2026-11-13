import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "system_files/usr/lib/armada")
)

from updates import runtime_health, wayland_probe


def _session_properties(**changes):
    props = {
        "Active": "yes",
        "Remote": "no",
        "Type": "wayland",
        "Class": "user",
        "State": "active",
        "Seat": "seat0",
        "User": "1000",
    }
    props.update(changes)
    return props


def _session_output(props):
    return "".join(f"{key}={value}\n" for key, value in props.items())


def _fake_wayland_library():
    lib = mock.MagicMock()
    lib.wl_display_connect = mock.MagicMock(return_value=0x1234)
    lib.wl_display_roundtrip = mock.MagicMock(return_value=0)
    lib.wl_display_disconnect = mock.MagicMock()
    return lib


class RuntimeHealthTests(unittest.TestCase):
    def test_boot_id_uses_bounded_regular_file_reader_and_ascii_uuid(self):
        boot_id = "12345678-1234-5678-1234-567812345678"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "boot_id"
            with mock.patch.object(runtime_health, "BOOT_ID_PATH", path):
                path.write_bytes((boot_id + "\n").encode("ascii"))
                self.assertEqual(runtime_health._read_boot_id(), boot_id)

                cases = {
                    "malformed": b"not-a-uuid\n",
                    "nonascii": b"12345678-1234-5678-1234-56781234567\xff\n",
                    "oversize": b"a" * 65,
                }
                for name, contents in cases.items():
                    with self.subTest(case=name):
                        path.write_bytes(contents)
                        with self.assertRaises(runtime_health.RuntimeHealthError):
                            runtime_health._read_boot_id()

    def test_key_parser_and_exact_required_service_commands(self):
        parsed = runtime_health._parse_kv(
            "Active=yes\nSeat=\nType=\n", {"Seat", "Type"}, "session test"
        )
        self.assertEqual(parsed, {"Active": "yes", "Seat": "", "Type": ""})
        invalid = {
            "duplicate": "Seat=seat0\nSeat=seat1\n",
            "missing": "Active=yes\n",
            "malformed": "Seat=seat0\nnot-a-property\n",
        }
        for name, output in invalid.items():
            with self.subTest(case=name):
                with self.assertRaises(runtime_health.RuntimeHealthError):
                    runtime_health._parse_kv(output, {"Seat"}, "session test")

        with mock.patch.object(
            runtime_health,
            "_run_cmd",
            return_value="LoadState=loaded\nActiveState=active\n",
        ) as run:
            self.assertEqual(
                runtime_health._verify_required_units(),
                runtime_health.REQUIRED_UNITS,
            )
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [
                        runtime_health.SYSTEMCTL_BIN,
                        "show",
                        "--property=LoadState,ActiveState",
                        "--no-pager",
                        unit,
                    ],
                    timeout=5,
                )
                for unit in runtime_health.REQUIRED_UNITS
            ],
        )
        for output in (
            "LoadState=not-found\nActiveState=active\n",
            "LoadState=loaded\nActiveState=inactive\n",
        ):
            with self.subTest(output=output):
                with mock.patch.object(runtime_health, "_run_cmd", return_value=output):
                    with self.assertRaises(runtime_health.RuntimeHealthError):
                        runtime_health._verify_required_units()

    def test_session_selection_filters_deduplicates_and_queries_all_properties(self):
        sessions = {
            "local": _session_properties(),
            "ssh": _session_properties(Seat="", Type=""),
            "greeter": _session_properties(Class="greeter"),
            "remote": _session_properties(Remote="yes"),
            "x11": _session_properties(Type="x11"),
            "root": _session_properties(User="0"),
        }
        listing = "local 1000 seat0\n" + "".join(
            f"{sid} 1000 seat0\n" for sid in ("local", "ssh", "greeter", "remote", "x11", "root")
        )
        show_commands = []

        def run_cmd(argv, *, timeout):
            self.assertEqual(timeout, 5)
            if argv[1] == "list-sessions":
                self.assertEqual(
                    argv,
                    [runtime_health.LOGINCTL_BIN, "list-sessions", "--no-legend", "--no-pager"],
                )
                return listing
            self.assertEqual(argv[1], "show-session")
            show_commands.append(argv)
            self.assertIn("--all", argv)
            return _session_output(sessions[argv[2]])

        with mock.patch.object(runtime_health, "_run_cmd", side_effect=run_cmd), mock.patch.object(
            runtime_health.pwd, "getpwuid", return_value=mock.Mock(pw_gid=1000)
        ) as getpwuid:
            sid, uid, gid, props = runtime_health._select_wayland_session()
        self.assertEqual((sid, uid, gid), ("local", 1000, 1000))
        self.assertEqual(props, sessions["local"])
        self.assertEqual(len(show_commands), len(sessions))
        getpwuid.assert_called_once_with(1000)

        with mock.patch.object(runtime_health, "_list_session_ids", return_value=["a", "b"]), mock.patch.object(
            runtime_health, "_show_session", return_value=_session_properties()
        ):
            with self.assertRaises(runtime_health.RuntimeHealthError):
                runtime_health._select_wayland_session()
        with mock.patch.object(runtime_health, "_list_session_ids", return_value=["ssh"]), mock.patch.object(
            runtime_health, "_show_session", return_value=_session_properties(Seat="", Type="")
        ):
            with self.assertRaises(runtime_health.RuntimeHealthError):
                runtime_health._select_wayland_session()

    def test_candidate_sockets_use_real_bounded_secure_runtime_directory(self):
        uid = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_dir = root / str(uid)
            runtime_dir.mkdir(mode=0o700)
            bound = []
            try:
                for index in range(10):
                    path = runtime_dir / f"wayland-{index}"
                    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    endpoint.bind(str(path))
                    bound.append(endpoint)
                (runtime_dir / "wayland-regular").write_text("not a socket")
                (runtime_dir / "wayland-9.lock").write_text("lock")
                (runtime_dir / "wayland-link").symlink_to(runtime_dir / "wayland-0")

                with mock.patch.object(runtime_health, "RUNTIME_USERS_ROOT", root):
                    found_dir, sockets = runtime_health._candidate_sockets(uid)
                    self.assertEqual(found_dir, runtime_dir)
                    self.assertEqual(
                        sockets,
                        [runtime_dir / f"wayland-{index}" for index in range(8)],
                    )

                    runtime_dir.chmod(0o770)
                    with self.assertRaises(runtime_health.RuntimeHealthError):
                        runtime_health._candidate_sockets(uid)
                    runtime_dir.chmod(0o700)

                    real_lstat = os.lstat
                    original_stat = real_lstat(runtime_dir)

                    def wrong_owner(path):
                        if Path(path) == runtime_dir:
                            return mock.Mock(st_mode=original_stat.st_mode, st_uid=uid + 1)
                        return real_lstat(path)

                    with mock.patch.object(runtime_health.os, "lstat", side_effect=wrong_owner):
                        with self.assertRaises(runtime_health.RuntimeHealthError):
                            runtime_health._candidate_sockets(uid)

                    real_dir = root / "real-runtime"
                    runtime_dir.rename(real_dir)
                    runtime_dir.symlink_to(real_dir, target_is_directory=True)
                    with self.assertRaises(runtime_health.RuntimeHealthError):
                        runtime_health._candidate_sockets(uid)
            finally:
                for endpoint in bound:
                    endpoint.close()

    def test_readiness_error_only_marks_absent_wayland_evidence(self):
        with mock.patch.object(runtime_health, "_run_cmd", return_value=""):
            with self.assertRaises(runtime_health.RuntimeNotReady):
                runtime_health._list_session_ids()

        with mock.patch.object(
            runtime_health, "_list_session_ids", return_value=["ssh"]
        ), mock.patch.object(
            runtime_health,
            "_show_session",
            return_value=_session_properties(Seat="", Type=""),
        ):
            with self.assertRaises(runtime_health.RuntimeNotReady):
                runtime_health._select_wayland_session()

        with mock.patch.object(
            runtime_health, "_list_session_ids", return_value=["a", "b"]
        ), mock.patch.object(
            runtime_health, "_show_session", return_value=_session_properties()
        ):
            with self.assertRaises(runtime_health.RuntimeHealthError) as ambiguous:
                runtime_health._select_wayland_session()
        self.assertNotIsInstance(ambiguous.exception, runtime_health.RuntimeNotReady)

        with mock.patch.object(
            runtime_health, "_list_session_ids", return_value=["local"]
        ), mock.patch.object(
            runtime_health, "_show_session", return_value=_session_properties()
        ), mock.patch.object(
            runtime_health.pwd, "getpwuid", side_effect=KeyError("missing")
        ):
            with self.assertRaises(runtime_health.RuntimeHealthError) as bad_identity:
                runtime_health._select_wayland_session()
        self.assertNotIsInstance(bad_identity.exception, runtime_health.RuntimeNotReady)

        uid = os.getuid()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(runtime_health, "RUNTIME_USERS_ROOT", root):
                with self.assertRaises(runtime_health.RuntimeNotReady):
                    runtime_health._candidate_sockets(uid)

                runtime_dir = root / str(uid)
                runtime_dir.mkdir(mode=0o700)
                with self.assertRaises(runtime_health.RuntimeNotReady):
                    runtime_health._candidate_sockets(uid)

                (runtime_dir / "wayland-0").write_text("not a socket")
                with self.assertRaises(runtime_health.RuntimeHealthError) as unusable:
                    runtime_health._candidate_sockets(uid)
                self.assertNotIsInstance(
                    unusable.exception, runtime_health.RuntimeNotReady
                )

                (runtime_dir / "wayland-0").unlink()
                runtime_dir.chmod(0o770)
                with self.assertRaises(runtime_health.RuntimeHealthError) as unsafe:
                    runtime_health._candidate_sockets(uid)
                self.assertNotIsInstance(
                    unsafe.exception, runtime_health.RuntimeNotReady
                )

    def test_probe_sockets_invokes_isolated_helper_with_exact_security_context(self):
        runtime_dir = Path("/run/user/1000")
        sockets = [
            runtime_dir / "wayland-0",
            runtime_dir / "wayland-1",
            runtime_dir / "gamescope-0",
        ]
        calls = [
            subprocess.TimeoutExpired(cmd=["probe"], timeout=3),
            subprocess.CompletedProcess(args=[], returncode=1),
            subprocess.CompletedProcess(args=[], returncode=0),
        ]
        with mock.patch.object(runtime_health.subprocess, "run", side_effect=calls) as run:
            self.assertEqual(
                runtime_health._probe_sockets(runtime_dir, sockets, 1000, 1000),
                "gamescope-0",
            )
        expected_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "XDG_RUNTIME_DIR": str(runtime_dir),
        }
        self.assertTrue(runtime_health.WAYLAND_PROBE_PATH.is_absolute())
        expected = [
            mock.call(
                [runtime_health.PYTHON_BIN, "-I", str(runtime_health.WAYLAND_PROBE_PATH), str(sock)],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
                user=1000,
                group=1000,
                extra_groups=[],
                env=expected_env,
                cwd="/",
            )
            for sock in sockets
        ]
        self.assertEqual(run.call_args_list, expected)

        with mock.patch.object(
            runtime_health.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["probe"], timeout=3),
        ) as run:
            with self.assertRaises(runtime_health.RuntimeHealthError):
                runtime_health._probe_sockets(runtime_dir, sockets, 1000, 1000)
        self.assertEqual(run.call_count, 3)

    def test_collect_runtime_evidence_joins_state_and_rejects_changes(self):
        props = _session_properties()
        runtime_dir = Path("/run/user/1000")
        sockets = [runtime_dir / "wayland-7"]

        def collect(boot_ids, final_props):
            with mock.patch.object(runtime_health, "_read_boot_id", side_effect=boot_ids), mock.patch.object(
                runtime_health, "_verify_required_units", return_value=runtime_health.REQUIRED_UNITS
            ), mock.patch.object(
                runtime_health,
                "_select_wayland_session",
                return_value=("session-1", 1000, 1000, props),
            ), mock.patch.object(
                runtime_health, "_candidate_sockets", return_value=(runtime_dir, sockets)
            ), mock.patch.object(
                runtime_health, "_probe_sockets", return_value="wayland-7"
            ) as probe, mock.patch.object(
                runtime_health, "_show_session", return_value=final_props
            ) as show_session:
                result = runtime_health.collect_runtime_evidence()
                probe.assert_called_once_with(runtime_dir, sockets, 1000, 1000)
                show_session.assert_called_once_with("session-1")
                return result

        evidence = collect(["boot-a", "boot-a"], props)
        self.assertEqual(
            evidence,
            runtime_health.RuntimeEvidence(
                boot_id="boot-a",
                session_id="session-1",
                uid=1000,
                display="wayland-7",
                services=runtime_health.REQUIRED_UNITS,
            ),
        )
        with self.assertRaises(runtime_health.RuntimeHealthError):
            collect(["boot-a", "boot-a"], _session_properties(Active="no"))
        with self.assertRaises(runtime_health.RuntimeHealthError):
            collect(["boot-a", "boot-b"], props)

    def test_wayland_probe_configures_ffi_and_disconnects_after_zero_event_roundtrip(self):
        lib = _fake_wayland_library()
        path = "/run/user/1000/wayland-0"
        with mock.patch.object(wayland_probe.ctypes, "CDLL", return_value=lib) as cdll:
            self.assertEqual(wayland_probe.probe_socket(path), 0)
        cdll.assert_called_once_with(wayland_probe.WAYLAND_CLIENT_SONAME, use_errno=True)
        self.assertEqual(lib.wl_display_connect.argtypes, [wayland_probe.ctypes.c_char_p])
        self.assertEqual(lib.wl_display_connect.restype, wayland_probe.ctypes.c_void_p)
        self.assertEqual(lib.wl_display_roundtrip.argtypes, [wayland_probe.ctypes.c_void_p])
        self.assertEqual(lib.wl_display_roundtrip.restype, wayland_probe.ctypes.c_int)
        self.assertEqual(lib.wl_display_disconnect.argtypes, [wayland_probe.ctypes.c_void_p])
        self.assertIsNone(lib.wl_display_disconnect.restype)
        lib.wl_display_connect.assert_called_once_with(os.fsencode(path))
        lib.wl_display_roundtrip.assert_called_once_with(0x1234)
        lib.wl_display_disconnect.assert_called_once_with(0x1234)

    def test_wayland_probe_failures_bad_arguments_and_exception_cleanup(self):
        path = "/run/user/1000/wayland-0"
        lib = _fake_wayland_library()
        lib.wl_display_connect.return_value = None
        with mock.patch.object(wayland_probe.ctypes, "CDLL", return_value=lib):
            self.assertEqual(wayland_probe.probe_socket(path), 1)
        lib.wl_display_disconnect.assert_not_called()

        lib = _fake_wayland_library()
        lib.wl_display_roundtrip.return_value = -1
        with mock.patch.object(wayland_probe.ctypes, "CDLL", return_value=lib):
            self.assertEqual(wayland_probe.probe_socket(path), 1)
        lib.wl_display_disconnect.assert_called_once_with(0x1234)

        with mock.patch.object(wayland_probe.ctypes, "CDLL", side_effect=OSError("missing")):
            self.assertEqual(wayland_probe.probe_socket(path), 1)

        lib = _fake_wayland_library()
        lib.wl_display_roundtrip.side_effect = RuntimeError("ffi failure")
        with mock.patch.object(wayland_probe.ctypes, "CDLL", return_value=lib):
            with self.assertRaises(RuntimeError):
                wayland_probe.probe_socket(path)
        lib.wl_display_disconnect.assert_called_once_with(0x1234)

        with mock.patch.object(wayland_probe.ctypes, "CDLL") as cdll:
            for invalid in ("", "relative/wayland-0", "/tmp/bad\x00path"):
                with self.subTest(path=invalid):
                    self.assertEqual(wayland_probe.probe_socket(invalid), 1)
            self.assertEqual(wayland_probe.main([]), 1)
            self.assertEqual(wayland_probe.main([path, path]), 1)
            cdll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
