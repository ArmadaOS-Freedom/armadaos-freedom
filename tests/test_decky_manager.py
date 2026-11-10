from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates import decky_manager, decky_source
from updates.decky_manager import DeckyManager
from updates.decky_release import DeckyRelease
from updates.decky_transaction import DeckyTransaction

URL_V2 = "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v2.0.0/PluginLoader-arm64"
RELEASE_MARKER_NAME = "managed.json"
PENDING_JOURNAL_NAME = "pending.json"


def make_elf(payload: bytes = b"armada-loader-v2", total_len: int = 96) -> bytes:
    buf = bytearray(max(total_len, 64))
    buf[:4] = b"\x7fELF"
    buf[4] = 2
    buf[5] = 1
    struct.pack_into("<H", buf, 18, 183)
    buf[64:] = payload[: total_len - 64].ljust(total_len - 64, b"Z")
    return bytes(buf)


def make_release(data: bytes, tag: str = "v2.0.0", url: str = URL_V2) -> DeckyRelease:
    return DeckyRelease(
        release_id=200,
        tag=tag,
        published_at="2025-01-01T00:00:00Z",
        asset_id=2000,
        url=url,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class FakeDeckyService:
    def __init__(self, active: bool = False, new_healthy: bool = True, rollback_healthy: bool = True) -> None:
        self.active = active
        self.new_healthy = new_healthy
        self.rollback_healthy = rollback_healthy
        self.calls: list[tuple[str, str | None]] = []

    def is_active(self) -> bool:
        self.calls.append(("is_active", None))
        return self.active

    def stop(self) -> None:
        self.calls.append(("stop", None))
        self.active = False

    def start(self) -> None:
        self.calls.append(("start", None))
        self.active = True

    def healthy(self, release: DeckyRelease | None = None) -> bool:
        self.calls.append(("healthy", None if release is None else release.tag))
        if not self.active:
            return False
        return self.rollback_healthy if release is None else self.new_healthy


class TestDeckyManager(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.homebrew = self.root / "var" / "home" / "armada" / "homebrew"
        self.services_dir = self.homebrew / "services"
        self.state_dir = self.root / "var" / "lib" / "armada" / "updates" / "decky"
        self.lock_path = self.root / "run" / "lock" / "armada-update.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.v2_bytes = make_elf(b"loader-v2-payload")
        self.v2_rel = make_release(self.v2_bytes, tag="v2.0.0")
        self.old_bytes = make_elf(b"loader-v1-legacy")

    def _make_manager(self, service: FakeDeckyService) -> DeckyManager:
        return DeckyManager(
            services_dir=self.services_dir,
            state_dir=self.state_dir,
            lock_path=self.lock_path,
            service=service,
        )

    def _seed_installed(self, version: str, data: bytes, mode: int = 0o755) -> None:
        self.services_dir.mkdir(parents=True, exist_ok=True)
        bin_path = self.services_dir / "PluginLoader"
        bin_path.write_bytes(data)
        os.chmod(bin_path, mode)
        (self.services_dir / ".loader.version").write_text(version + "\n", encoding="utf-8")

    def _open_https_bytes(self, payload: bytes):
        def _opener(url: str, headers: dict[str, str] | None = None, timeout: float = 30.0):
            return io.BytesIO(payload)

        return _opener

    def test_missing_dirs_status_and_apply_creates_install_and_preserves_siblings(self) -> None:
        svc = FakeDeckyService(active=False, new_healthy=True)
        mgr = self._make_manager(svc)
        self.assertFalse(self.homebrew.exists())
        st = mgr.status()
        self.assertEqual(st["state"], "repair_required")
        self.assertFalse(st["binaryPresent"])

        (self.homebrew / "plugins").mkdir(parents=True, exist_ok=True)
        (self.homebrew / "settings").mkdir(parents=True, exist_ok=True)
        plugin_sentinel = self.homebrew / "plugins" / "keep.txt"
        settings_sentinel = self.homebrew / "settings" / "cfg.json"
        plugin_sentinel.write_text("plugin-ok", encoding="utf-8")
        settings_sentinel.write_text("{\"ok\":true}", encoding="utf-8")
        os.rmdir(self.homebrew / "services") if self.services_dir.exists() else None

        with (
            mock.patch.object(decky_manager, "fetch_preview", return_value=self.v2_rel) as fetch_mock,
            mock.patch.object(decky_source, "_open_https", side_effect=self._open_https_bytes(self.v2_bytes)),
        ):
            res = mgr.apply()

        self.assertEqual(res["state"], "completed")
        self.assertEqual(res["currentVersion"], "v2.0.0")
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual((self.services_dir / "PluginLoader").read_bytes(), self.v2_bytes)
        self.assertEqual((self.services_dir / ".loader.version").read_text(encoding="utf-8").strip(), "v2.0.0")
        self.assertTrue((self.state_dir / RELEASE_MARKER_NAME).is_file())
        self.assertTrue(svc.active)
        self.assertIn(("start", None), svc.calls)
        self.assertFalse((self.state_dir / PENDING_JOURNAL_NAME).exists())
        self.assertEqual([p.name for p in self.state_dir.iterdir() if p.name.startswith("download-")], [])
        self.assertEqual(plugin_sentinel.read_text(encoding="utf-8"), "plugin-ok")
        self.assertEqual(settings_sentinel.read_text(encoding="utf-8"), "{\"ok\":true}")

    def test_check_matrix_restores_clean_state_between_cases(self) -> None:
        with mock.patch.object(decky_manager, "fetch_preview", return_value=self.v2_rel):
            self._seed_installed("v2.0.0", self.v2_bytes)
            svc_ok = FakeDeckyService(active=True, new_healthy=True)
            self.assertEqual(self._make_manager(svc_ok).check()["state"], "up_to_date")

            self._seed_installed("v2.0.0", self.old_bytes)
            svc_corrupt = FakeDeckyService(active=True, new_healthy=True)
            res_corrupt = self._make_manager(svc_corrupt).check()
            self.assertEqual(res_corrupt["state"], "repair_required")
            self.assertNotIn(("stop", None), svc_corrupt.calls)

            self._seed_installed("v2.0.0", self.v2_bytes)
            svc_inactive = FakeDeckyService(active=False, new_healthy=False)
            res_inactive = self._make_manager(svc_inactive).check()
            self.assertEqual(res_inactive["state"], "repair_required")
            self.assertFalse(res_inactive["serviceActive"])

            self._seed_installed("v1.5.0", self.old_bytes)
            svc_old = FakeDeckyService(active=True, new_healthy=True)
            res_old = self._make_manager(svc_old).check()
            self.assertEqual(res_old["state"], "update_available")
            self.assertEqual(res_old["currentVersion"], "v1.5.0")
            self.assertEqual(res_old["targetVersion"], "v2.0.0")

    def test_fetch_failure_preserves_installed_facts_and_status_never_fetches(self) -> None:
        self._seed_installed("v1.9.0", self.old_bytes)
        svc = FakeDeckyService(active=True)
        mgr = self._make_manager(svc)
        with mock.patch.object(decky_manager, "fetch_preview", side_effect=RuntimeError("network boom")) as fetch_mock:
            st = mgr.status()
            self.assertEqual(st["state"], "installed")
            self.assertEqual(fetch_mock.call_count, 0)
            chk = mgr.check()
            self.assertEqual(fetch_mock.call_count, 1)
        self.assertEqual(chk["state"], "check_failed")
        self.assertNotEqual(chk["state"], "up_to_date")
        self.assertIsNone(chk["targetVersion"])
        self.assertEqual(chk["currentVersion"], "v1.9.0")
        self.assertTrue(chk["binaryPresent"])
        self.assertTrue(chk["binaryExecutable"])
        self.assertTrue(chk["serviceActive"])
        self.assertNotIn(("start", None), svc.calls)

    def test_download_tampered_payload_aborts_before_stopping_service_and_cleans_temp(self) -> None:
        self._seed_installed("v1.0.0", self.old_bytes)
        svc = FakeDeckyService(active=True)
        mgr = self._make_manager(svc)
        for tampered in (self.v2_bytes[:-3], make_elf(b"wrong-hash-same-len")):
            with self.subTest(len_tampered=len(tampered)):
                with (
                    mock.patch.object(decky_manager, "fetch_preview", return_value=self.v2_rel),
                    mock.patch.object(decky_source, "_open_https", side_effect=self._open_https_bytes(tampered)),
                ):
                    res = mgr.apply()
                self.assertEqual(res["state"], "failed")
                self.assertEqual(res["phase"], "download")
                self.assertEqual((self.services_dir / "PluginLoader").read_bytes(), self.old_bytes)
                self.assertEqual((self.services_dir / ".loader.version").read_text(encoding="utf-8").strip(), "v1.0.0")
                self.assertNotIn(("stop", None), svc.calls)
                self.assertEqual([p for p in self.state_dir.iterdir() if p.name.startswith("download-")], [])

    def test_unhealthy_new_loader_rolls_back_legacy_install_truthfully(self) -> None:
        self._seed_installed("v1.0.0", self.old_bytes)
        (self.homebrew / "plugins").mkdir(parents=True, exist_ok=True)
        (self.homebrew / "settings").mkdir(parents=True, exist_ok=True)
        p_file = self.homebrew / "plugins" / "p.json"
        s_file = self.homebrew / "settings" / "s.json"
        p_file.write_text("plugins-intact", encoding="utf-8")
        s_file.write_text("settings-intact", encoding="utf-8")
        svc = FakeDeckyService(active=True, new_healthy=False, rollback_healthy=True)
        mgr = self._make_manager(svc)

        with (
            mock.patch.object(decky_manager, "fetch_preview", return_value=self.v2_rel),
            mock.patch.object(decky_source, "_open_https", side_effect=self._open_https_bytes(self.v2_bytes)),
        ):
            res = mgr.apply()

        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["currentVersion"], "v1.0.0")
        self.assertEqual((self.services_dir / "PluginLoader").read_bytes(), self.old_bytes)
        self.assertEqual((self.services_dir / ".loader.version").read_text(encoding="utf-8").strip(), "v1.0.0")
        self.assertFalse((self.state_dir / RELEASE_MARKER_NAME).exists())
        tx = res["transaction"]
        self.assertTrue(tx["rollback_files_restored"])
        self.assertTrue(tx["rollback_service_healthy"])
        self.assertEqual(p_file.read_text(encoding="utf-8"), "plugins-intact")
        self.assertEqual(s_file.read_text(encoding="utf-8"), "settings-intact")

    def test_invalid_pending_journal_fails_recover_phase_before_fetch_or_download(self) -> None:
        self._seed_installed("v1.0.0", self.old_bytes)
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        journal = self.state_dir / PENDING_JOURNAL_NAME
        journal.write_text("{corrupt-json", encoding="utf-8")
        svc = FakeDeckyService(active=True)
        mgr = self._make_manager(svc)

        with (
            mock.patch.object(decky_manager, "fetch_preview") as fetch_mock,
            mock.patch.object(decky_source, "_open_https") as http_mock,
        ):
            res = mgr.apply()

        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["phase"], "recover")
        self.assertEqual(fetch_mock.call_count, 0)
        self.assertEqual(http_mock.call_count, 0)
        self.assertTrue(journal.exists())
        self.assertEqual((self.services_dir / "PluginLoader").read_bytes(), self.old_bytes)

    def test_insufficient_disk_space_and_nonroot_permission_guards(self) -> None:
        svc = FakeDeckyService(active=True)
        mgr = self._make_manager(svc)
        zero_vfs = os.statvfs_result((4096, 4096, 100, 0, 0, 100, 0, 0, 0, 255))
        with (
            mock.patch.object(decky_manager, "fetch_preview", return_value=self.v2_rel),
            mock.patch("os.statvfs", return_value=zero_vfs),
            mock.patch.object(decky_source, "_open_https") as http_mock,
        ):
            res = mgr.apply()
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["phase"], "prepare")
        self.assertEqual(http_mock.call_count, 0)
        self.assertNotIn(("stop", None), svc.calls)

        nonroot_state = self.root / "nonroot-state"
        svc2 = FakeDeckyService(active=True)
        mgr2 = DeckyManager(self.services_dir, nonroot_state, self.lock_path, service=svc2)
        with (
            mock.patch("os.geteuid", return_value=1000),
            mock.patch.object(decky_manager, "fetch_preview") as fetch_mock,
        ):
            with self.assertRaises(PermissionError):
                mgr2.apply()
            with self.assertRaises(PermissionError):
                mgr2.recover()
        self.assertFalse(nonroot_state.exists())
        self.assertEqual(fetch_mock.call_count, 0)
        self.assertEqual(svc2.calls, [])

    def test_cli_main_rejects_unsupported_args_and_maps_check_failed_exit_code(self) -> None:
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                decky_manager.main(["bogus-cmd"])
        self.assertEqual(ctx.exception.code, 2)

        fake_mgr = mock.MagicMock()
        fake_mgr.check.return_value = {
            "component": "decky",
            "schemaVersion": 1,
            "state": "check_failed",
            "phase": "check",
            "currentVersion": "v1.0.0",
            "targetVersion": None,
            "requiresReboot": False,
            "error": "offline",
        }
        out = io.StringIO()
        with (
            mock.patch.object(decky_manager, "DeckyManager", return_value=fake_mgr),
            mock.patch("sys.stdout", out),
        ):
            rc = decky_manager.main(["check"])
        self.assertEqual(rc, 1)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed["state"], "check_failed")
        self.assertEqual(parsed["phase"], "check")

    def test_recover_maps_unhealthy_rollback_service_to_repair_required(self) -> None:
        self._seed_installed("v1.0.0", self.old_bytes)
        self.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        mgr = self._make_manager(FakeDeckyService(active=False))
        low_level = {
            "state": "recovered",
            "rollback_files_restored": True,
            "rollback_service_healthy": False,
        }
        with mock.patch.object(DeckyTransaction, "recover", return_value=low_level):
            res = mgr.recover()
        self.assertEqual(res["phase"], "recover")
        self.assertEqual(res["state"], "repair_required")
        self.assertEqual(res["currentVersion"], "v1.0.0")
        self.assertEqual(res["recovery"], low_level)


if __name__ == "__main__":
    unittest.main()
