#!/usr/bin/python3
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
from updates import recovery_mounts as rm


class BodySentinel(Exception):
    pass


class RecoveryMountsFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("TMPDIR"):
            raise RuntimeError("TMPDIR must be explicitly set")
        if os.geteuid() != 0:
            raise RuntimeError("integration tests require root")
        for tool in ("mkfs.btrfs", "mkfs.ext4", "losetup", "mount", "umount",
                     "findmnt", "btrfs"):
            if not shutil.which(tool):
                raise RuntimeError("missing required tool: " + tool)
        cls.base = Path(tempfile.mkdtemp(dir=os.environ["TMPDIR"])).resolve()
        cls.parent = cls.base / "parent"
        cls.parent.mkdir(mode=0o700)
        cls.root_image = cls.base / "root.img"
        cls.boot_image = cls.base / "boot.img"
        cls.loops = []
        cls.setup_mounts = []
        try:
            cls.root_image.touch()
            with cls.root_image.open("wb") as stream:
                stream.truncate(256 * 1024 * 1024)
            cls.boot_image.touch()
            with cls.boot_image.open("wb") as stream:
                stream.truncate(32 * 1024 * 1024)
            cls.command(["mkfs.btrfs", "-q", str(cls.root_image)])
            cls.command(["mkfs.ext4", "-q", "-F", "-E",
                         "lazy_itable_init=0,lazy_journal_init=0", str(cls.boot_image)])
            cls.root_dev = cls.attach(cls.root_image)
            cls.boot_dev = cls.attach(cls.boot_image)
            root_mp = cls.base / "setup-root"
            boot_mp = cls.base / "setup-boot"
            root_mp.mkdir()
            boot_mp.mkdir()
            cls.command(["mount", "--", cls.root_dev, str(root_mp)])
            cls.setup_mounts.append(root_mp)
            for name in ("root", "var", "home"):
                cls.command(["btrfs", "subvolume", "create", str(root_mp / name)])
            (root_mp / "root/boot").mkdir()
            (root_mp / "root/root-marker").write_text("root marker\n")
            (root_mp / "var/var-marker").write_text("var marker\n")
            (root_mp / "home/personal-marker").write_text("personal\n")
            root_uuid = cls.findmnt_entry(str(root_mp))["uuid"]
            cls.command(["mount", "--", cls.boot_dev, str(boot_mp)])
            cls.setup_mounts.append(boot_mp)
            (boot_mp / "boot-marker").write_text("boot marker\n")
            boot_uuid = cls.findmnt_entry(str(boot_mp))["uuid"]
            while cls.setup_mounts:
                target = cls.setup_mounts[-1]
                cls.command(["umount", "--", str(target)])
                cls.setup_mounts.pop()
            cls.root_hash = cls.image_hash(cls.root_image)
            cls.boot_hash = cls.image_hash(cls.boot_image)
            cls.report = {
                "schemaVersion": 1,
                "phase": "recovery_filesystems_checked",
                "physical": {"device": "synthetic-loop-report"},
                "root": {"path": cls.root_dev,
                         "rdev": os.stat(cls.root_dev).st_rdev, "uuid": root_uuid},
                "boot": {"path": cls.boot_dev,
                         "rdev": os.stat(cls.boot_dev).st_rdev, "uuid": boot_uuid},
                "backup": {"path": str(cls.base / "never-accessed-backup"),
                           "uuid": "unused-backup-uuid"},
            }
        except Exception:
            while cls.setup_mounts:
                target = cls.setup_mounts[-1]
                cls.command(["umount", "--", str(target)])
                cls.setup_mounts.pop()
            cls.assert_no_mounts_under(str(cls.base))
            cls.detach_all()
            shutil.rmtree(cls.base)
            raise

    @classmethod
    def tearDownClass(cls):
        cls.assert_no_mounts_under(str(cls.base))
        cls.detach_all()
        shutil.rmtree(cls.base)

    @classmethod
    def command(cls, argv, **kwargs):
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=60, **kwargs)

    @classmethod
    def attach(cls, image):
        out = cls.command(["losetup", "--find", "--show", "--nooverlap", str(image)]).stdout
        device = out.decode().strip()
        cls.loops.append((device, image.resolve()))
        if not device or not stat.S_ISBLK(os.stat(device).st_mode):
            raise RuntimeError("losetup did not return a block device")
        return device

    @classmethod
    def detach_all(cls):
        while cls.loops:
            device, image = cls.loops[-1]
            out = cls.command(["losetup", "--noheadings", "--output", "BACK-FILE", device]).stdout
            if Path(out.decode().strip()).resolve() != image:
                raise RuntimeError("refusing to detach loop not backed by owned image")
            cls.command(["losetup", "--detach", device])
            cls.loops.pop()

    @staticmethod
    def image_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def findmnt_entry(target):
        out = subprocess.check_output([
            "findmnt", "--json", "--mountpoint", str(target),
            "--output", "TARGET,FSTYPE,SOURCE,UUID,FSROOT,VFS-OPTIONS,ID"])
        entries = json.loads(out)["filesystems"]
        if len(entries) != 1:
            raise RuntimeError("unexpected findmnt entries for " + str(target))
        return entries[0]

    @classmethod
    def assert_no_mounts_under(cls, prefix):
        data = json.loads(subprocess.check_output(["findmnt", "--json", "--output", "TARGET"]))
        def walk(entries):
            for entry in entries:
                target = entry["target"]
                if target == prefix or target.startswith(prefix + "/"):
                    raise AssertionError("mount still present under " + prefix + ": " + target)
                walk(entry.get("children", []))
        walk(data["filesystems"])

    def assert_clean(self, fds=()):
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assert_no_mounts_under(str(self.parent))
        self.assertEqual(self.image_hash(self.root_image), self.root_hash)
        self.assertEqual(self.image_hash(self.boot_image), self.boot_hash)
        for fd in fds:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def recording_opener(self, fds):
        real = rm._open_block_device
        def opener(path, expect_rdev):
            fd = real(path, expect_rdev)
            fds.append(fd)
            return fd
        return opener

    def assert_readonly_write_fails(self, directory):
        with self.assertRaises(OSError) as caught:
            (Path(directory) / "must-not-write").write_text("no")
        self.assertEqual(caught.exception.errno, errno.EROFS)


class RecoveryMountsIntegrationTest(RecoveryMountsFixture):
    def test_baseline_real_mounts_and_complete_cleanup(self):
        fds = []
        with mock.patch.object(rm, "inspect_recovery_filesystems", side_effect=[self.report, self.report]), \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)):
            with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                  parent_dir=self.parent) as result:
                sysroot, boot, var = map(Path, (result["sysrootPath"], result["bootPath"], result["varPath"]))
                session = sysroot.parent
                self.assertEqual(result["phase"], "recovery_filesystems_mounted_readonly")
                self.assertEqual(var.parent, session)
                self.assertNotEqual(var.parent, sysroot)
                self.assertEqual((sysroot / "root-marker").read_text(), "root marker\n")
                self.assertEqual((boot / "boot-marker").read_text(), "boot marker\n")
                self.assertEqual((var / "var-marker").read_text(), "var marker\n")
                self.assertFalse((sysroot / "home/personal-marker").exists())
                self.assertNotEqual(subprocess.run(["findmnt", "--mountpoint", str(sysroot / "home")],
                                                   stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL).returncode, 0)
                for target in (sysroot, boot, var):
                    self.assertIn("ro", self.findmnt_entry(str(target))["vfs-options"].split(","))
                    self.assert_readonly_write_fails(target)
                self.assertFalse((var / "personal-marker").exists())
        self.assertTrue(self.parent.is_dir())
        self.assertFalse(session.exists())
        self.assert_clean(fds)

    def test_body_sentinel_propagates_and_cleans(self):
        fds = []
        sentinel = BodySentinel("original body failure")
        with mock.patch.object(rm, "inspect_recovery_filesystems", return_value=self.report), \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)):
            with self.assertRaises(BodySentinel) as caught:
                with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                      parent_dir=self.parent):
                    raise sentinel
        self.assertIs(caught.exception, sentinel)
        self.assert_clean(fds)

    def test_initial_report_mismatch_opens_nothing(self):
        expected = copy.deepcopy(self.report)
        expected["physical"]["device"] = "different"
        fds = []
        with mock.patch.object(rm, "inspect_recovery_filesystems", return_value=self.report) as inspect, \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)):
            with self.assertRaises(rm.RecoveryMountError):
                with rm.mounted_recovery_filesystems(expected_filesystems=expected,
                                                      parent_dir=self.parent):
                    self.fail("must not yield")
        inspect.assert_called_once_with()
        self.assert_clean(fds)

    def test_midflight_physical_drift_never_yields(self):
        drifted = copy.deepcopy(self.report)
        drifted["physical"]["device"] = "changed-after-mount"
        fds = []
        with mock.patch.object(rm, "inspect_recovery_filesystems", side_effect=[self.report, drifted]), \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)):
            with self.assertRaisesRegex(rm.RecoveryMountError, "drifted"):
                with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                      parent_dir=self.parent):
                    self.fail("must not yield")
        self.assert_clean(fds)

    def test_wrong_root_uuid_rejected_before_boot_mount(self):
        wrong = copy.deepcopy(self.report)
        wrong["root"]["uuid"] = "00000000-0000-0000-0000-000000000000"
        fds, ext4_mounts = [], []
        real_run = rm._run
        def run(argv, **kwargs):
            if argv[:3] == ["mount", "-t", "ext4"]:
                ext4_mounts.append(argv)
            return real_run(argv, **kwargs)
        with mock.patch.object(rm, "inspect_recovery_filesystems", return_value=wrong), \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)), \
             mock.patch.object(rm, "_run", side_effect=run):
            with self.assertRaisesRegex(rm.RecoveryMountError, "UUID mismatch"):
                with rm.mounted_recovery_filesystems(expected_filesystems=wrong,
                                                      parent_dir=self.parent):
                    self.fail("must not yield")
        self.assertEqual(ext4_mounts, [])
        self.assert_clean(fds)

    def test_boot_mount_failure_unwinds_root(self):
        fds = []
        real_run = rm._run
        def run(argv, **kwargs):
            if argv[:3] == ["mount", "-t", "ext4"]:
                raise rm.RecoveryMountError("injected ext4 mount failure")
            return real_run(argv, **kwargs)
        with mock.patch.object(rm, "inspect_recovery_filesystems", return_value=self.report), \
             mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)), \
             mock.patch.object(rm, "_run", side_effect=run):
            with self.assertRaisesRegex(rm.RecoveryMountError, "injected ext4"):
                with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                      parent_dir=self.parent):
                    self.fail("must not yield")
        self.assert_clean(fds)

    def test_var_unmount_failure_retains_then_exact_manual_cleanup(self):
        fds = []
        paths = []
        mount_ids = {}
        session = None
        real_umount = rm._umount
        failed = [False]
        def failing_umount(target, expected_id):
            if target.endswith("/var") and not failed[0]:
                failed[0] = True
                raise rm.RecoveryMountError("injected var unmount failure")
            return real_umount(target, expected_id)
        try:
            with mock.patch.object(rm, "inspect_recovery_filesystems", return_value=self.report), \
                 mock.patch.object(rm, "_open_block_device", side_effect=self.recording_opener(fds)), \
                 mock.patch.object(rm, "_umount", side_effect=failing_umount):
                with self.assertRaisesRegex(rm.RecoveryMountError, "retained"):
                    with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                          parent_dir=self.parent) as result:
                        paths = [Path(result[name]) for name in ("varPath", "bootPath", "sysrootPath")]
                        session = paths[2].parent
                        for path in paths:
                            entry = self.findmnt_entry(str(path))
                            self.assertIsInstance(entry["id"], int)
                            self.assertEqual(entry["target"], str(path))
                            mount_ids[path] = entry["id"]
            for path in paths:
                entry = self.findmnt_entry(str(path))
                self.assertEqual(entry["target"], str(path))
                self.assertEqual(entry["id"], mount_ids[path])
            for fd in fds:
                with self.assertRaises(OSError) as caught:
                    os.fstat(fd)
                self.assertEqual(caught.exception.errno, errno.EBADF)
        finally:
            if paths:
                for path in paths:
                    entry = self.findmnt_entry(str(path))
                    if entry["target"] != str(path) or entry["id"] != mount_ids[path]:
                        raise RuntimeError("mount ID mismatch before manual unmount: " + str(path))
                    self.command(["umount", "--", str(path)])
                os.rmdir(paths[0])
                os.rmdir(paths[2])
                os.rmdir(session)
        self.assert_clean(fds)

    def test_findmnt_parser_subtests(self):
        valid = {"target": "/target", "id": 42}
        cases = {
            "wrong target": ([dict(valid, target="/other")], "target mismatch"),
            "two entries": ([dict(valid), dict(valid)], "exactly one"),
            "boolean id": ([dict(valid, id=True)], "invalid mount ID"),
        }
        for name, (entries, message) in cases.items():
            with self.subTest(name=name):
                output = json.dumps({"filesystems": entries}).encode()
                with mock.patch.object(rm, "_run", return_value=(output, b"")):
                    with self.assertRaisesRegex(rm.RecoveryMountError, message):
                        rm._mount_entry("/target")
        with self.subTest(name="valid entry"):
            output = json.dumps({"filesystems": [valid]}).encode()
            with mock.patch.object(rm, "_run", return_value=(output, b"")):
                self.assertEqual(rm._mount_entry("/target"), valid)



class RecoveryUpdateMountsIntegrationTest(RecoveryMountsFixture):
    def assert_rw_clean(self, fds=()):
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assert_no_mounts_under(str(self.parent))
        for fd in fds:
            with self.assertRaises(OSError) as caught:
                os.fstat(fd)
            self.assertEqual(caught.exception.errno, errno.EBADF)

    def enter_update(self, fds):
        inspector = mock.patch.object(rm, "inspect_recovery_filesystems",
                                      return_value=self.report)
        opener = mock.patch.object(rm, "_open_block_device",
                                   side_effect=self.recording_opener(fds))
        inspector.start()
        opener.start()
        self.addCleanup(opener.stop)
        self.addCleanup(inspector.stop)
        return rm.mounted_recovery_update_filesystems(expected_filesystems=self.report,
                                                      parent_dir=self.parent)

    def write_synced(self, path, text):
        with open(path, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())

    def test_update_mounts_rw_root_boot_ro_var_persists(self):
        fds = []
        with self.enter_update(fds) as result:
            self.assertEqual(result["phase"], "recovery_filesystems_mounted_update")
            sysroot, boot, var = map(Path, (result["sysrootPath"], result["bootPath"],
                                            result["varPath"]))
            for target, expect_rw in ((sysroot, True), (boot, True), (var, False)):
                opts = self.findmnt_entry(str(target))["vfs-options"].split(",")
                self.assertIn("rw" if expect_rw else "ro", opts)
                self.assertNotIn("ro" if expect_rw else "rw", opts)
                for hardening in ("nosuid", "nodev", "noexec"):
                    self.assertIn(hardening, opts)
            self.write_synced(sysroot / "update-root", "new root bytes\n")
            self.write_synced(boot / "update-boot", "new boot bytes\n")
            with self.assertRaises(OSError) as caught:
                (var / "update-var").write_text("no")
            self.assertEqual(caught.exception.errno, errno.EROFS)
            self.assertFalse((sysroot / "home/personal-marker").exists())
            self.assertNotEqual(subprocess.run(
                ["findmnt", "--mountpoint", str(sysroot / "home")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60).returncode, 0)
            self.assertEqual((sysroot / "root-marker").read_text(), "root marker\n")
            self.assertEqual((boot / "boot-marker").read_text(), "boot marker\n")
            self.assertEqual((var / "var-marker").read_text(), "var marker\n")
        self.assert_rw_clean(fds)
        home_dir = self.base / "home-check"
        home_dir.mkdir()
        self.command(["mount", "-t", "btrfs",
                      "-o", "ro,rescue=nologreplay,nosuid,nodev,noexec,subvol=home",
                      "--", self.root_dev, str(home_dir)])
        mount_id = None
        try:
            mount_id = rm._mount_entry(str(home_dir))["id"]
            self.assertEqual((home_dir / "personal-marker").read_text(), "personal\n")
        finally:
            rm._umount(str(home_dir), mount_id)
            os.rmdir(home_dir)
        after_root = self.image_hash(self.root_image)
        after_boot = self.image_hash(self.boot_image)
        with mock.patch.object(rm, "inspect_recovery_filesystems",
                               return_value=self.report):
            with rm.mounted_recovery_filesystems(expected_filesystems=self.report,
                                                 parent_dir=self.parent) as ro:
                self.assertEqual((Path(ro["sysrootPath"]) / "update-root").read_text(),
                                 "new root bytes\n")
                self.assertEqual((Path(ro["bootPath"]) / "update-boot").read_text(),
                                 "new boot bytes\n")
                self.assertEqual((Path(ro["sysrootPath"]) / "root-marker").read_text(),
                                 "root marker\n")
                self.assertEqual((Path(ro["bootPath"]) / "boot-marker").read_text(),
                                 "boot marker\n")
                self.assertEqual((Path(ro["varPath"]) / "var-marker").read_text(),
                                 "var marker\n")
                self.assert_readonly_write_fails(ro["sysrootPath"])
                self.assert_readonly_write_fails(ro["bootPath"])
                self.assert_readonly_write_fails(ro["varPath"])
        self.assertEqual(self.image_hash(self.root_image), after_root)
        self.assertEqual(self.image_hash(self.boot_image), after_boot)
        self.assert_rw_clean()

    def test_update_mounts_use_stable_path_across_two_sessions(self):
        fds = []
        with self.enter_update(fds) as first:
            sysroot = Path(first["sysrootPath"])
            boot = Path(first["bootPath"])
            var = Path(first["varPath"])
            self.assertEqual(sysroot.parent, self.parent / "update")
            self.assertEqual(var.parent, self.parent / "update")
            for target in (sysroot, boot, var):
                entry = self.findmnt_entry(str(target))
                self.assertEqual(entry["uuid"],
                                 self.report["boot"]["uuid"] if target == boot
                                 else self.report["root"]["uuid"])
            self.write_synced(sysroot / "reconnect-marker", "persisted bytes\n")
        self.assertFalse((self.parent / "update").exists())
        self.assert_rw_clean(fds)
        fds2 = []
        with self.enter_update(fds2) as second:
            self.assertEqual(second["sysrootPath"], str(sysroot))
            self.assertEqual(second["bootPath"], str(boot))
            self.assertEqual(second["varPath"], str(var))
            for target in (sysroot, boot, var):
                entry = self.findmnt_entry(str(target))
                self.assertEqual(entry["uuid"],
                                 self.report["boot"]["uuid"] if target == boot
                                 else self.report["root"]["uuid"])
            self.assertEqual((sysroot / "reconnect-marker").read_text(),
                             "persisted bytes\n")
        self.assertFalse((self.parent / "update").exists())
        self.assert_rw_clean(fds2)

    def test_update_reserved_dir_collision_rejected_without_adoption(self):
        reserved = self.parent / "update"
        marker = reserved / "collider-marker"
        reserved.mkdir(mode=0o700)
        self.write_synced(marker, "preexisting\n")
        fds = []
        try:
            with self.assertRaisesRegex(rm.RecoveryMountError,
                                        "failed to prepare recovery mount session"):
                with self.enter_update(fds):
                    self.fail("must not yield")
            self.assertTrue(marker.is_file())
            self.assertEqual(marker.read_text(), "preexisting\n")
            self.assertEqual(fds, [])
            self.assert_no_mounts_under(str(self.parent))
            marker.unlink()
            reserved.rmdir()
            temp_target = Path(tempfile.mkdtemp(dir=str(self.base)))
            link_marker = temp_target / "link-marker"
            try:
                reserved.symlink_to(temp_target, target_is_directory=True)
                link_marker = temp_target / "link-marker"
                self.write_synced(link_marker, "linked\n")
                fds2 = []
                with self.assertRaisesRegex(rm.RecoveryMountError,
                                            "failed to prepare recovery mount session"):
                    with self.enter_update(fds2):
                        self.fail("must not yield")
                self.assertEqual(fds2, [])
                self.assertTrue(reserved.is_symlink())
                self.assertEqual(link_marker.read_text(), "linked\n")
                self.assert_no_mounts_under(str(self.parent))
            finally:
                if reserved.is_symlink():
                    reserved.unlink()
                if link_marker.exists():
                    link_marker.unlink()
                temp_target.rmdir()
        finally:
            if marker.exists():
                marker.unlink()
            if reserved.is_symlink():
                reserved.unlink()
            elif reserved.exists():
                reserved.rmdir()
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assert_no_mounts_under(str(self.parent))

    def test_update_nested_attempt_rejected_while_session_alive(self):
        fds = []
        with self.enter_update(fds) as outer:
            sysroot = Path(outer["sysrootPath"])
            boot = Path(outer["bootPath"])
            var = Path(outer["varPath"])
            outer_ids = {}
            for target in (sysroot, boot, var):
                outer_ids[target] = self.findmnt_entry(str(target))["id"]
            self.write_synced(sysroot / "outer-marker", "outer bytes\n")
            fds_nested = []
            with self.assertRaisesRegex(rm.RecoveryMountError,
                                        "failed to prepare recovery mount session"):
                with self.enter_update(fds_nested):
                    self.fail("must not yield")
            self.assertEqual(fds_nested, [])
            for target in (sysroot, boot, var):
                self.assertEqual(self.findmnt_entry(str(target))["id"],
                                 outer_ids[target])
            self.assertEqual((sysroot / "outer-marker").read_text(), "outer bytes\n")
        self.assertFalse((self.parent / "update").exists())
        self.assert_rw_clean(fds)

    def test_update_body_sentinel_propagates_and_cleans(self):
        fds = []
        sentinel = BodySentinel("update body failure")
        with self.assertRaises(BodySentinel) as caught:
            with self.enter_update(fds):
                raise sentinel
        self.assertIs(caught.exception, sentinel)
        self.assert_rw_clean(fds)

    def test_update_boot_mount_failure_unwinds_root(self):
        fds = []
        real_run = rm._run
        def run(argv, **kwargs):
            if argv[:3] == ["mount", "-t", "ext4"]:
                raise rm.RecoveryMountError("injected ext4 update mount failure")
            return real_run(argv, **kwargs)
        with mock.patch.object(rm, "_run", side_effect=run):
            with self.assertRaisesRegex(rm.RecoveryMountError, "injected ext4"):
                with self.enter_update(fds):
                    self.fail("must not yield")
        self.assert_rw_clean(fds)

    def test_verify_mount_mode_negative_cases(self):
        base = {"fstype": "btrfs", "uuid": "u", "fsroot": "/root",
                "vfs-options": "rw,nosuid,nodev,noexec", "id": 1}
        cases = (
            ("rw mounted where ro expected", "rw,nosuid", True, "mount is not ro"),
            ("ro mounted where rw expected", "ro,nosuid", False, "mount is not rw"),
            ("both ro and rw", "ro,rw", True, "mount has rw"),
            ("neither ro nor rw", "nosuid,nodev", True, "mount is not ro"),
        )
        for label, opts, read_only, message in cases:
            with self.subTest(label=label):
                entry = dict(base)
                entry["vfs-options"] = opts
                with self.assertRaisesRegex(rm.RecoveryMountError, message):
                    rm._verify_mount(entry, target="/t", fstype="btrfs", uuid="u",
                                     fsroot="/root", read_only=read_only)


if __name__ == "__main__":
    unittest.main()
