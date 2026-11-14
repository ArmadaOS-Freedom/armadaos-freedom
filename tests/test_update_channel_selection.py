import contextlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ARMADA_LIB = ROOT / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

import armada_os
from updates import channel_selection, system_manager as manager, trial_confirmation
from updates.boot_context import BootContextError

ORIGINAL_SET_CHANNEL = channel_selection.set_channel


@unittest.skipUnless(os.geteuid() == 0, "requires root")
class ChannelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmpdir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        os.chmod(self.tmpdir, 0o755)
        self.lock_path = self.tmpdir / "armada-update.lock"
        self.state_path = self.tmpdir / "state" / "update-channel"
        self.stack.enter_context(
            mock.patch.object(trial_confirmation, "UPDATE_LOCK_PATH", self.lock_path)
        )

    def test_set_channel_writes_all_channels_with_expected_mode(self):
        for channel in ("stable", "beta", "testing"):
            channel_selection.set_channel(channel, self.state_path)
            self.assertEqual(self.state_path.read_text(encoding="utf-8"), f"{channel}\n")
            mode = stat.S_IMODE(self.state_path.lstat().st_mode)
            self.assertEqual(mode, 0o644)

    def test_invalid_channel_and_non_root_write_nothing(self):
        flat_dest = self.tmpdir / "update-channel"
        with self.assertRaises(ValueError):
            channel_selection.set_channel("nightly", flat_dest)
        self.assertFalse(flat_dest.exists())
        self.assertEqual(list(self.tmpdir.glob(".update-channel.*")), [])

        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(PermissionError):
                channel_selection.set_channel("beta", flat_dest)
        self.assertFalse(flat_dest.exists())
        self.assertEqual(list(self.tmpdir.glob(".update-channel.*")), [])

    def test_rejects_relative_path_and_symlinks_and_unsafe_parent(self):
        with self.assertRaises(ValueError):
            channel_selection.set_channel("beta", Path("relative/update-channel"))

        real_dir = self.tmpdir / "real"
        real_dir.mkdir(mode=0o755)
        link_dir = self.tmpdir / "link"
        link_dir.symlink_to(real_dir)

        with self.assertRaises(ValueError):
            channel_selection.set_channel("beta", link_dir / "update-channel")
        with self.assertRaises(ValueError):
            channel_selection.set_channel("beta", link_dir / "nested" / "update-channel")

        dest_link = real_dir / "update-channel"
        dest_link.symlink_to(real_dir / "target")
        with self.assertRaises(ValueError):
            channel_selection.set_channel("beta", dest_link)

        writable_dir = self.tmpdir / "writable"
        writable_dir.mkdir(mode=0o775)
        os.chmod(writable_dir, 0o775)
        with self.assertRaises(ValueError):
            channel_selection.set_channel("beta", writable_dir / "update-channel")

    def test_lock_contention_and_replace_failure_preserve_existing_file(self):
        channel_selection.set_channel("stable", self.state_path)
        with trial_confirmation._locked():
            with self.assertRaises(trial_confirmation.ConfirmationError):
                channel_selection.set_channel("beta", self.state_path)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "stable\n")

        channel_selection.set_channel("beta", self.state_path)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "beta\n")

        with mock.patch("os.replace", side_effect=OSError("replace boom")):
            with self.assertRaises(OSError):
                channel_selection.set_channel("testing", self.state_path)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), "beta\n")
        self.assertEqual(list(self.state_path.parent.glob(".update-channel.*")), [])

    def test_nested_parent_directories_are_flushed_before_publication(self):
        destination = self.tmpdir / "one" / "two" / "update-channel"
        real_fsync = os.fsync
        flushed = []

        def record(fd):
            info = os.fstat(fd)
            flushed.append((info.st_dev, info.st_ino))
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=record):
            channel_selection.set_channel("beta", destination)
        self.assertEqual(destination.read_text(encoding="utf-8"), "beta\n")
        for directory in (destination.parent, destination.parent.parent, self.tmpdir):
            info = os.lstat(directory)
            self.assertIn((info.st_dev, info.st_ino), flushed)

    def test_ancestor_flush_failure_prevents_publication(self):
        destination = self.tmpdir / "one" / "two" / "update-channel"
        real_fsync = os.fsync

        def fail_on_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("fsync boom")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fail_on_directory):
            with self.assertRaisesRegex(OSError, "fsync boom"):
                channel_selection.set_channel("beta", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".update-channel.*")), [])


@unittest.skipUnless(os.geteuid() == 0, "requires root")
class ManagerChannelIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmpdir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        os.chmod(self.tmpdir, 0o755)
        self.lock_path = self.tmpdir / "armada-update.lock"
        self.state_path = self.tmpdir / "state" / "update-channel"
        self.config_path = self.tmpdir / "channels.json"
        self.compatible_path = self.tmpdir / "compatible"
        self.compatible_path.write_bytes(b"lenovo,elden\x00")
        self.config_path.write_text(
            json.dumps({
                "schemaVersion": 1,
                "channels": {
                    "stable": {
                        "url": "https://updates.example.com/stable.json",
                        "signatureUrl": "https://updates.example.com/stable.sig",
                    },
                    "beta": {
                        "url": "https://updates.example.com/beta.json",
                        "signatureUrl": "https://updates.example.com/beta.sig",
                    },
                }
            }),
            encoding="utf-8",
        )
        self.trial_status = {"state": "none", "recordState": "none"}
        self.stack.enter_context(
            mock.patch.object(trial_confirmation, "UPDATE_LOCK_PATH", self.lock_path)
        )
        self.stack.enter_context(
            mock.patch.object(
                channel_selection,
                "set_channel",
                side_effect=lambda ch: ORIGINAL_SET_CHANNEL(ch, self.state_path),
            )
        )
        self.stack.enter_context(
            mock.patch.object(armada_os, "snapshot", side_effect=self._snapshot)
        )
        self.stack.enter_context(
            mock.patch.object(
                manager, "read_boot_context", side_effect=BootContextError("no boot ctx")
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                manager,
                "read_trial_status",
                side_effect=lambda _be: dict(self.trial_status),
            )
        )
        self.mgr = manager.SystemManager(
            config_path=self.config_path,
            compatible_path=self.compatible_path,
            witness_path=self.tmpdir / "witness",
            cmdline_path=self.tmpdir / "cmdline",
        )

    def _snapshot(self):
        ch = (
            self.state_path.read_text(encoding="utf-8").strip()
            if self.state_path.exists()
            else "stable"
        )
        return {
            "repository": next(iter(armada_os.REPOSITORIES)),
            "channel": ch,
            "booted": {"version": "1.0.0", "checksum": "a" * 64},
            "staged": None,
            "rollback_queued": False,
        }

    def test_select_beta_then_check_fetches_beta_catalog(self):
        with mock.patch.object(manager, "fetch_update") as fetch_mock, mock.patch(
            "subprocess.run"
        ) as run_mock:
            res = self.mgr.select_channel("beta")
            fetch_mock.assert_not_called()
            run_mock.assert_not_called()
        self.assertEqual(res["phase"], "select_channel")
        self.assertEqual(res["state"], "installed")
        self.assertEqual(res["channel"], "beta")
        self.assertIsNone(res["targetVersion"])
        self.assertFalse(res["requiresReboot"])

        catalog = {
            "channel": {
                "version": "1.1.0-beta.1",
                "ostreeCommit": "b" * 64,
                "releaseId": "2025.02",
                "ociDigest": "sha256:" + "c" * 64,
            }
        }
        with mock.patch.object(manager, "fetch_update", return_value=catalog) as fetch_mock:
            chk = self.mgr.check()
        fetch_mock.assert_called_once_with(
            "https://updates.example.com/beta.json",
            "https://updates.example.com/beta.sig",
            expected_channel="beta",
            model=res["model"],
            updater_version=manager.UPDATER_VERSION,
        )
        self.assertEqual(chk["state"], "update_available")
        self.assertEqual(chk["channel"], "beta")
        self.assertEqual(chk["targetVersion"], "1.1.0-beta.1")

    def test_select_succeeds_without_config_and_check_reports_not_configured(self):
        self.config_path.unlink()
        res = self.mgr.select_channel("testing")
        self.assertEqual(res["phase"], "select_channel")
        self.assertEqual(res["state"], "installed")
        self.assertEqual(res["channel"], "testing")
        with mock.patch.object(manager, "fetch_update") as fetch_mock:
            chk = self.mgr.check()
            fetch_mock.assert_not_called()
        self.assertEqual(chk["state"], "not_configured")
        self.assertEqual(chk["channel"], "testing")

    def test_select_channel_preserves_awaiting_confirmation_state(self):
        self.trial_status = {
            "state": "available",
            "recordState": "trial",
            "runningDeployment": "new",
        }
        res = self.mgr.select_channel("beta")
        self.assertEqual(res["phase"], "select_channel")
        self.assertEqual(res["channel"], "beta")
        self.assertEqual(res["state"], "awaiting_confirmation")
        self.assertFalse(res["requiresReboot"])
