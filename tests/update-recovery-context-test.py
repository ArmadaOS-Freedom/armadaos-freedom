#!/usr/bin/python3
import copy
import errno
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, OSTree
from updates import recovery_context as rc
from updates import recovery_mounts as rm


def load_fixture(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mount_module = load_fixture("_recovery_context_mount_fixture",
                            "update-recovery-mounts-test.py")
deployment_module = load_fixture("_recovery_context_deployment_fixture",
                                 "update-recovery-deployment-test.py")


class BodySentinel(Exception):
    pass


class RecoveryContextFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mount_module.MountFixture = mount_module.RecoveryMountsIntegrationTest
        deployment_module.DeploymentFixture = deployment_module.RecoveryDeploymentIntegrationTests
        mount_module.MountFixture.setUpClass()
        cls.addClassCleanup(mount_module.MountFixture.tearDownClass)
        deployment_module.DeploymentFixture.setUpClass()
        cls.addClassCleanup(deployment_module.DeploymentFixture.tearDownClass)
        cls.deployment = deployment_module.DeploymentFixture(
            "test_second_registered_deployment_is_selected_explicitly")
        cls.deployment.setUp()
        cls.addClassCleanup(cls.deployment.doCleanups)
        cls.mounts = mount_module.MountFixture("assert_clean")
        cls.f = cls.deployment.f
        cls.physical = {
            "schemaVersion": 1,
            "phase": "recovery_slots_checked",
            "model": cls.f.model,
            "actualSlot": "a",
            "inactiveSlot": "b",
            "selectedSlot": "b",
            "bootable": {"a": False, "b": True},
        }
        mount_module.MountFixture.report["physical"] = copy.deepcopy(cls.physical)
        cls.second = cls.f._call(1)
        cls.second_binding = cls.second["deployment"]
        cls.first_binding = cls.f.initial_binding
        for binding in (cls.first_binding, cls.second_binding):
            options = cls.deployment._dep(binding).get_bootconfig().get("options")
            tokens = options.split()
            for index, token in enumerate(tokens):
                if token.startswith("root=UUID="):
                    tokens[index] = "root=UUID=" + mount_module.MountFixture.report["root"]["uuid"]
                elif token.startswith("boot=UUID="):
                    tokens[index] = "boot=UUID=" + mount_module.MountFixture.report["boot"]["uuid"]
            cls.deployment._set_kargs(binding, tokens)
        root_mount = mount_module.MountFixture.base / "context-root"
        boot_mount = root_mount / "boot"
        var_mount = mount_module.MountFixture.base / "context-var"
        root_mount.mkdir()
        var_mount.mkdir()
        try:
            cls.command(["mount", "-o", "subvol=root", "--",
                         mount_module.MountFixture.root_dev, str(root_mount)])
            mount_module.MountFixture.setup_mounts.append(root_mount)
            cls.command(["mount", "--", mount_module.MountFixture.boot_dev,
                         str(boot_mount)])
            mount_module.MountFixture.setup_mounts.append(boot_mount)
            cls.command(["mount", "-o", "subvol=var", "--",
                         mount_module.MountFixture.root_dev, str(var_mount)])
            mount_module.MountFixture.setup_mounts.append(var_mount)
            subprocess.run(["cp", "-a", "--no-preserve=links", str(cls.f.root) + "/.", str(root_mount)],
                           stdin=subprocess.DEVNULL, check=True, timeout=120)
            channel_dir = var_mount / "lib/armada"
            channel_dir.mkdir(parents=True, exist_ok=True)
            os.chown(var_mount, 0, 0)
            os.chown(var_mount / "lib", 0, 0)
            os.chown(channel_dir, 0, 0)
            channel_file = channel_dir / "update-channel"
            channel_file.write_text("beta\n", encoding="ascii")
            os.chown(channel_file, 0, 0)
            channel_file.chmod(0o644)
        finally:
            while mount_module.MountFixture.setup_mounts:
                target = mount_module.MountFixture.setup_mounts[-1]
                cls.command(["umount", "--", str(target)])
                mount_module.MountFixture.setup_mounts.pop()
        mount_module.MountFixture.root_hash = mount_module.MountFixture.image_hash(
            mount_module.MountFixture.root_image)
        mount_module.MountFixture.boot_hash = mount_module.MountFixture.image_hash(
            mount_module.MountFixture.boot_image)

    @staticmethod
    def command(argv):
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True, timeout=60)

    def setUp(self):
        self.inventory = mount_module.MountFixture.report
        self.enterContext(mock.patch.object(rc, "inspect_recovery_filesystems",
                                            side_effect=self.read_inventory))
        self.enterContext(mock.patch.object(rm, "inspect_recovery_filesystems",
                                            side_effect=self.read_inventory))

    def read_inventory(self, expected_physical=None):
        if expected_physical is not None:
            self.assertEqual(expected_physical, self.physical)
        return copy.deepcopy(self.inventory)

    def context(self, binding=None, physical=None):
        return rc.inspected_recovery_context(
            expected_binding=binding or self.second_binding,
            expected_physical=self.physical if physical is None else physical,
            parent_dir=str(mount_module.MountFixture.parent),
        )

class RecoveryContextIntegrationTest(RecoveryContextFixture):
    def test_second_deployment_real_ostree_and_readonly(self):
        with self.context() as context:
            self.assertEqual(context["channel"], "beta")
            installed = context["installed"]
            self.assertEqual(installed["model"], self.f.model)
            self.assertEqual(installed["installedDeployment"], self.second_binding)
            self.assertEqual(installed["originRef"], deployment_module._fixture.ORIGINS[1])
            self.assertEqual(Path(installed["path"]).relative_to(context["sysrootPath"]),
                             Path(self.second["path"]).relative_to(self.f.root))
            self.assertNotEqual(installed["installedDeployment"], self.first_binding)
            sysroot = OSTree.Sysroot.new(
                Gio.File.new_for_path(str(context["sysrootPath"])))
            try:
                self.assertTrue(sysroot.load(None))
                self.assertIsNone(sysroot.get_booted_deployment())
            finally:
                sysroot.unload()
                sysroot = None
            for key in ("sysrootPath", "bootPath", "varPath"):
                self.mounts.assert_readonly_write_fails(context[key])
            home_mount = Path(context["sysrootPath"]) / "home"
            findmnt = subprocess.run(
                ["findmnt", "--noheadings", "--output", "TARGET", str(home_mount)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
            self.assertEqual(findmnt.returncode, 1, findmnt.stdout)
            self.assertFalse((home_mount / "personal-marker").exists())
            self.assertFalse(any(path.name == "home" for path in
                                 mount_module.MountFixture.parent.iterdir()))
        self.mounts.assert_clean()

    def test_original_body_exception_propagates_and_cleans(self):
        sentinel = BodySentinel("original body failure")
        with self.assertRaises(BodySentinel) as caught:
            with self.context():
                raise sentinel
        self.assertIs(caught.exception, sentinel)
        self.mounts.assert_clean()

    def test_invalid_inputs_and_unknown_binding(self):
        bad_physical = copy.deepcopy(self.physical)
        bad_physical["deployment"] = self.second_binding
        for binding, physical in (("not-a-binding", self.physical),
                                  (self.second_binding, bad_physical)):
            with self.subTest(binding=binding, physical=physical):
                with self.assertRaises(rc.RecoveryContextError):
                    with self.context(binding=binding, physical=physical):
                        self.fail("invalid input yielded a context")
                self.assertEqual(rc.inspect_recovery_filesystems.call_count, 0)
                self.assertEqual(rm.inspect_recovery_filesystems.call_count, 0)
                self.mounts.assert_clean()
        unknown = "default/" + "a" * 64 + ".0"
        with self.assertRaises(rc.RecoveryContextError):
            with self.context(binding=unknown):
                self.fail("unknown binding yielded a context")
        self.mounts.assert_clean()

    def test_outward_copies_and_hardware_drift(self):
        original_inventory = copy.deepcopy(self.inventory)
        original_physical = copy.deepcopy(self.physical)
        with self.context() as context:
            context["installed"]["options"] = ["caller-mutated"]
            context["filesystems"]["root"]["uuid"] = "caller-mutated"
            context["physical"]["selectedSlot"] = "a"
        self.assertEqual(self.inventory, original_inventory)
        self.assertEqual(self.physical, original_physical)
        self.mounts.assert_clean()

        changed = copy.deepcopy(original_inventory)
        changed["backup"]["uuid"] = "changed-after-yield"
        with self.assertRaises(rc.RecoveryContextError):
            with self.context() as context:
                self.inventory = changed
                context["filesystems"].clear()
                context["filesystems"].update(copy.deepcopy(changed))
        self.assertEqual(self.inventory, changed)
        self.mounts.assert_clean()


class RecoveryUpdateContextIntegrationTest(RecoveryContextFixture):
    def assert_update_clean(self):
        self.assertFalse(any(mount_module.MountFixture.parent.iterdir()))
        self.mounts.assert_no_mounts_under(str(mount_module.MountFixture.parent))

    def update_context(self, **overrides):
        args = {
            "expected_binding": self.second_binding,
            "expected_physical": self.physical,
            "parent_dir": mount_module.MountFixture.parent,
        }
        args.update(overrides)
        return rc.opened_recovery_update_context(**args)

    def test_update_context_rejects_unknown_binding_before_mounting(self):
        unknown = "default/" + "a" * 64 + ".0"
        with mock.patch.object(
                rc, "mounted_recovery_update_filesystems") as update_mounts:
            with self.assertRaises(rc.RecoveryContextError):
                with self.update_context(expected_binding=unknown):
                    self.fail("body must not execute")
        update_mounts.assert_not_called()
        self.assert_update_clean()

    def test_update_context_detects_entry_report_drift(self):
        real_read_installed = rc._read_installed

        def drifting_read(*args, **kwargs):
            report = copy.deepcopy(real_read_installed(**kwargs))
            entry = rm._mount_entry(str(kwargs["sysroot_path"]))
            if "rw" in entry["vfs-options"].split(","):
                report["originRef"] = report["originRef"] + "-drifted"
            return report

        with mock.patch.object(rc, "_read_installed", drifting_read):
            with self.assertRaises(rc.RecoveryContextError):
                with self.update_context():
                    self.fail("body must not execute")
        self.assert_update_clean()

    def test_update_context_body_exception_propagates_and_snapshots_unchanged(self):
        class BodySentinel(Exception):
            pass

        sentinel = BodySentinel()
        try:
            with self.update_context():
                raise sentinel
        except BodySentinel as raised:
            self.assertIs(raised, sentinel)
        else:
            self.fail("body exception was not propagated unchanged")
        self.assert_update_clean()

        original_physical = copy.deepcopy(self.physical)
        with self.update_context() as context:
            context["physical"]["model"] = "mutated"
            context["filesystems"]["root"]["uuid"] = "mutated"
            context["installed"]["originRef"] = "mutated"
        self.assertEqual(self.physical, original_physical)
        self.assert_update_clean()

    def test_update_context_detects_post_body_report_drift(self):
        real_read_installed = rc._read_installed
        drift = {"active": False}

        def drifting_read(*args, **kwargs):
            report = copy.deepcopy(real_read_installed(**kwargs))
            if drift["active"]:
                report["originRef"] = report["originRef"] + "-drifted"
            return report

        with mock.patch.object(rc, "_read_installed", drifting_read):
            with self.assertRaises(rc.RecoveryContextError):
                with self.update_context():
                    drift["active"] = True
        self.assert_update_clean()

    def test_expected_selected_slot_after_allows_only_requested_switch(self):
        physical = copy.deepcopy(self.physical)
        physical["selectedSlot"] = "a"
        original_physical = copy.deepcopy(physical)
        inventory = copy.deepcopy(self.inventory)
        inventory["physical"] = copy.deepcopy(physical)
        with mock.patch.object(self, "physical", physical), \
                mock.patch.object(self, "inventory", inventory):
            with self.update_context(expected_selected_slot_after="b") as context:
                self.assertEqual(context["physical"]["selectedSlot"], "a")
                self.inventory["physical"]["selectedSlot"] = "b"
            self.assertEqual(context["physical"], original_physical)
            self.assertEqual(physical, original_physical)
            self.assert_update_clean()
            self.inventory["physical"] = copy.deepcopy(original_physical)
            for option in ("c", "a"):
                with self.subTest(option=option):
                    with self.assertRaises(rc.RecoveryContextError):
                        with self.update_context(expected_selected_slot_after=option):
                            self.fail("invalid target yielded a context")
                    self.assert_update_clean()

    def test_expected_selected_slot_after_still_rejects_other_drift(self):
        for name in ("default-switch", "no-switch", "root-uuid", "actual-slot"):
            with self.subTest(case=name):
                physical = copy.deepcopy(self.physical)
                physical["selectedSlot"] = "a"
                inventory = copy.deepcopy(self.inventory)
                inventory["physical"] = copy.deepcopy(physical)
                with mock.patch.object(self, "physical", physical), \
                        mock.patch.object(self, "inventory", inventory):
                    overrides = ({} if name == "default-switch" else
                                 {"expected_selected_slot_after": "b"})
                    with self.assertRaises(rc.RecoveryContextError):
                        with self.update_context(**overrides):
                            if name != "no-switch":
                                self.inventory["physical"]["selectedSlot"] = "b"
                            if name == "root-uuid":
                                self.inventory["root"]["uuid"] = "changed-after-yield"
                            elif name == "actual-slot":
                                self.inventory["physical"]["actualSlot"] = "b"
                    self.assert_update_clean()

    def test_append_verified_deployment_preserves_old_and_user_files(self):
        with rc.opened_recovery_update_context(
                expected_binding=self.second_binding,
                expected_physical=self.physical,
                parent_dir=mount_module.MountFixture.parent) as context:
            self.assertEqual(context["phase"], "recovery_context_checked_update")
            self.assertEqual(context["channel"], "beta")
            self.assertEqual(context["installed"]["installedDeployment"],
                             self.second_binding)
            root_path = Path(context["sysrootPath"])
            boot_path = Path(context["bootPath"])
            var_path = Path(context["varPath"])
            sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root_path)))
            try:
                self.assertTrue(sysroot.load(None))
                self.assertIsNone(sysroot.get_booted_deployment())
            finally:
                sysroot.unload()
                sysroot = None
            for path in (root_path, boot_path):
                probe = path / ".update-write-probe"
                probe.write_bytes(b"writable\n")
                probe.unlink()
            self.mounts.assert_readonly_write_fails(var_path)
            home_mount = root_path / "home"
            findmnt = subprocess.run(
                ["findmnt", "--noheadings", "--output", "TARGET", str(home_mount)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=60)
            self.assertEqual(findmnt.returncode, 1, findmnt.stdout)
            preserved = [root_path / "var/root-marker",
                         root_path / "var/home/game-marker",
                         root_path / "ostree/deploy/default/var/home/personal-marker",
                         var_path / "var-marker", root_path / "root-marker",
                         boot_path / "boot-marker"]
            original_contents = {path: path.read_bytes() for path in preserved}
            new = self.f._call(2, sysroot_path=root_path,
                               current_binding=self.second_binding)
            new_binding = new["deployment"]
            self.assertTrue(new_binding)
            sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(root_path)))
            try:
                self.assertTrue(sysroot.load(None))
                deployments = sysroot.get_deployments()
                self.assertEqual([deployment_module._fixture.binding(dep)
                                  for dep in deployments],
                                 [self.first_binding, self.second_binding, new_binding])
                self.assertTrue(all(dep.is_pinned() for dep in deployments))
            finally:
                sysroot.unload()
                sysroot = None
            new_path = Path(new["path"])
            new_path.relative_to(root_path)
            self.assertEqual((new_path / "etc/custom.conf").read_bytes(),
                             b"site=retained\n")
            self.assertEqual({path: path.read_bytes() for path in preserved},
                             original_contents)
        self.assert_update_clean()
        before_ro = (mount_module.MountFixture.image_hash(
                         mount_module.MountFixture.root_image),
                     mount_module.MountFixture.image_hash(
                         mount_module.MountFixture.boot_image))
        with rc.inspected_recovery_context(
                expected_binding=new_binding, expected_physical=self.physical,
                parent_dir=str(mount_module.MountFixture.parent)) as inspected:
            sysroot = OSTree.Sysroot.new(
                Gio.File.new_for_path(str(inspected["sysrootPath"])))
            try:
                self.assertTrue(sysroot.load(None))
                bindings = [deployment_module._fixture.binding(dep)
                            for dep in sysroot.get_deployments()]
                self.assertIn(self.second_binding, bindings)
                self.assertIsNone(sysroot.get_booted_deployment())
            finally:
                sysroot.unload()
                sysroot = None
        self.assert_update_clean()
        after_ro = (mount_module.MountFixture.image_hash(
                        mount_module.MountFixture.root_image),
                    mount_module.MountFixture.image_hash(
                        mount_module.MountFixture.boot_image))
        self.assertEqual(after_ro, before_ro)


if __name__ == "__main__":
    unittest.main()
