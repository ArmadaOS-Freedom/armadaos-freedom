import dataclasses
import fcntl
import hashlib
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates.decky_release import DeckyRelease
from updates.decky_transaction import DeckyTransaction

URL = "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64"
OLD = {"PluginLoader": b"old-loader", ".loader.version": b"v0\n", "managed.json": b"old-managed"}
MODES = {"PluginLoader": 0o751, ".loader.version": 0o604, "managed.json": 0o640}


def make_elf():
    buf = bytearray(80)
    buf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", buf, 18, 183)
    buf[64:] = b"payload-bytes!".ljust(16, b"X")
    return bytes(buf)


def make_release(data):
    return DeckyRelease(release_id=1, tag="v1", published_at="2024-01-01T00:00:00Z",
                        asset_id=10, url=URL, size=len(data), sha256=hashlib.sha256(data).hexdigest())


class PowerLoss(BaseException):
    pass


class Service:
    def __init__(self, active=True, fail_start=False, stop_error=False, ok_new=True, ok_none=True):
        self.active = active
        self.fail_start = fail_start
        self.stop_error = stop_error
        self.ok_new = ok_new
        self.ok_none = ok_none
        self.failed_start = False
        self.calls = []

    def is_active(self):
        self.calls.append("is_active")
        return self.active

    def stop(self):
        self.calls.append("stop")
        if self.stop_error:
            raise OSError("stop failed")
        self.active = False

    def start(self):
        self.calls.append("start")
        if self.fail_start and not self.failed_start:
            self.failed_start = True
            self.active = False
            raise OSError("new service failed")
        self.active = True

    def healthy(self, release):
        self.calls.append(("healthy", release))
        return self.ok_none if release is None else self.ok_new


class TestDeckyTransaction(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.services = self.root / "services"
        self.state = self.root / "state"
        self.lock = self.root / "transaction.lock"
        self.data = make_elf()
        self.release = make_release(self.data)
        self.payload = self.root / "payload"
        self.payload.write_bytes(self.data)
        self.reset()

    def reset(self):
        for directory in (self.services, self.state):
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir()
        for name, data in OLD.items():
            path = self.target(name)
            path.write_bytes(data)
            path.chmod(MODES[name])
        (self.services / "plugins").mkdir()
        (self.state / "settings").mkdir()
        (self.services / "plugins" / "sentinel").write_bytes(b"plugin-sentinel")
        (self.state / "settings" / "sentinel").write_bytes(b"settings-sentinel")

    def target(self, name):
        return self.state / name if name == "managed.json" else self.services / name

    def transaction(self, service):
        return DeckyTransaction(self.services, self.state, self.lock, service)

    def assert_old(self):
        for name, data in OLD.items():
            path = self.target(name)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), MODES[name])
        self.assert_sentinels()

    def assert_new(self):
        self.assertEqual(self.target("PluginLoader").read_bytes(), self.data)
        self.assertEqual(self.target(".loader.version").read_bytes(), b"v1\n")
        self.assertEqual(json.loads(self.target("managed.json").read_bytes()), dataclasses.asdict(self.release))
        for name, mode in (("PluginLoader", 0o755), (".loader.version", 0o644), ("managed.json", 0o600)):
            self.assertEqual(stat.S_IMODE(self.target(name).stat().st_mode), mode)
        self.assert_sentinels()

    def assert_sentinels(self):
        self.assertEqual((self.services / "plugins" / "sentinel").read_bytes(), b"plugin-sentinel")
        self.assertEqual((self.state / "settings" / "sentinel").read_bytes(), b"settings-sentinel")

    def test_successful_upgrade(self):
        service = Service()
        self.assertEqual(self.transaction(service).install(self.release, self.payload), {"state": "completed"})
        self.assert_new()
        self.assertTrue(service.active)
        self.assertIn(("healthy", self.release), service.calls)

    def test_fresh_stopped_and_broken_loaders(self):
        for case in ("fresh", "stopped", "broken"):
            with self.subTest(case=case):
                self.reset()
                if case == "fresh":
                    for name in OLD:
                        self.target(name).unlink()
                elif case == "broken":
                    self.target("PluginLoader").write_bytes(b"broken")
                service = Service(active=case == "broken")
                result = self.transaction(service).install(self.release, self.payload)
                self.assertEqual(result["state"], "completed")
                self.assert_new()
                self.assertTrue(service.active)

    def test_tampered_payload_never_stops_or_mutates(self):
        tampered = bytearray(self.data)
        tampered[-1] ^= 1
        self.payload.write_bytes(tampered)  # Release metadata already contains the original hash.
        service = Service()
        result = self.transaction(service).install(self.release, self.payload)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("stop", service.calls)
        self.assert_old()

    def test_failed_new_start_rolls_back_files_metadata_and_absence(self):
        self.target("managed.json").unlink()
        service = Service(fail_start=True, ok_new=False, ok_none=True)
        result = self.transaction(service).install(self.release, self.payload)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["rollback_files_restored"])
        self.assertTrue(result["rollback_service_healthy"])
        self.assertEqual(service.calls.count("start"), 2)
        self.assertFalse(self.target("managed.json").exists())
        for name in ("PluginLoader", ".loader.version"):
            info = self.target(name).stat()
            self.assertEqual(self.target(name).read_bytes(), OLD[name])
            self.assertEqual((info.st_uid, info.st_gid), (os.geteuid(), os.getegid()))
            self.assertEqual(stat.S_IMODE(info.st_mode), MODES[name])

    def test_stop_failure_preserves_active_service_and_files(self):
        service = Service(stop_error=True)
        result = self.transaction(service).install(self.release, self.payload)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(service.active)
        self.assertNotIn("start", service.calls)
        self.assert_old()

    def test_power_loss_at_each_target_requires_recovery(self):
        for name, previous in (("PluginLoader", True), (".loader.version", True),
                               ("managed.json", True), ("PluginLoader", False)):
            with self.subTest(target=name, previous_active=previous):
                self.reset()
                tx = self.transaction(Service(active=previous))
                real = tx._atomic
                raised = [False]
                def interrupt(path, data, mode, uid=None, gid=None):
                    real(path, data, mode, uid, gid)
                    if path == tx.targets[name] and not raised[0]:
                        raised[0] = True
                        raise PowerLoss()
                tx._atomic = interrupt
                with self.assertRaises(PowerLoss):
                    tx.install(self.release, self.payload)
                self.assertTrue(tx.pending.exists())
                recovery_service = Service(active=False)
                recovered = self.transaction(recovery_service)
                self.assertEqual(recovered.recover()["state"], "recovered")
                self.assert_old()
                self.assertFalse(recovered.pending.exists())
                self.assertIsNone(recovered.recover())
                self.assertEqual("start" in recovery_service.calls, previous)

    def test_corrupt_backup_prevalidates_all_and_retains_pending(self):
        tx = self.transaction(Service())
        real = tx._atomic
        def interrupt(path, data, mode, uid=None, gid=None):
            real(path, data, mode, uid, gid)
            if path == tx.targets["PluginLoader"]:
                raise PowerLoss()
        tx._atomic = interrupt
        with self.assertRaises(PowerLoss):
            tx.install(self.release, self.payload)
        journal = json.loads(tx.pending.read_text())
        backup = tx.txroot / journal["txid"] / ".loader.version.backup"
        backup.write_bytes(backup.read_bytes() + b"corrupt")
        current = {name: self.target(name).read_bytes() for name in OLD}
        recovery = self.transaction(Service(active=False))
        with self.assertRaises(ValueError):
            recovery.recover()
        self.assertEqual({name: self.target(name).read_bytes() for name in OLD}, current)
        self.assertTrue(recovery.pending.exists())
        with self.assertRaisesRegex(RuntimeError, "Recovery required"):
            recovery.install(self.release, self.payload)

    def test_committed_interruption_finishes_without_service_actions(self):
        tx = self.transaction(Service())
        real = tx._journal
        def interrupt(txid, phase, previous):
            real(txid, phase, previous)
            if phase == "committed":
                raise PowerLoss()
        tx._journal = interrupt
        with self.assertRaises(PowerLoss):
            tx.install(self.release, self.payload)
        service = Service(active=False)
        recovery = self.transaction(service)
        self.assertEqual(recovery.recover()["state"], "completed")
        self.assertEqual(service.calls, [])
        self.assert_new()
        self.assertIsNone(recovery.recover())

    def test_one_shot_pending_removal_error_recovers_committed_cleanup(self):
        tx = self.transaction(Service())
        real = tx._remove_pending
        failed = [False]
        def interrupt():
            if not failed[0]:
                failed[0] = True
                raise OSError("injected removal failure")
            real()
        tx._remove_pending = interrupt
        self.assertEqual(tx.install(self.release, self.payload)["state"], "completed")
        self.assertFalse(tx.pending.exists())
        self.assert_new()

    def test_lock_busy_and_malformed_or_symlink_journal(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "lock is busy"):
                self.transaction(Service()).install(self.release, self.payload)
            self.assert_old()
        finally:
            os.close(fd)
        for malformed in (True, False):
            with self.subTest(malformed=malformed):
                self.reset()
                tx = self.transaction(Service())
                tx._setup()
                if malformed:
                    tx.pending.write_text("{")
                    with self.assertRaises(ValueError):
                        tx.recover()
                else:
                    outside = self.root / "outside-journal"
                    outside.write_text("{}")
                    tx.pending.symlink_to(outside)
                    with self.assertRaises(RuntimeError):
                        tx.recover()


if __name__ == "__main__":
    unittest.main()
