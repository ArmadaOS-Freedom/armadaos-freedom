#!/usr/bin/env python3
import argparse
from contextlib import ExitStack, contextmanager, nullcontext
import dataclasses
from dataclasses import replace
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import stat as statmod
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "system_files/usr/libexec/armada/armada-installer"
loader = importlib.machinery.SourceFileLoader("installer", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
i = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = i
loader.exec_module(i)


def table(names=(), device="/dev/sda", sector=4096):
    def part(number, start_mib, end_mib, name, kind=i.LINUX_TYPE):
        return i.Partition(number, start_mib * i.MIB // sector, end_mib * i.MIB // sector,
                           name, kind, str(uuid.UUID(int=number)))
    parts = [part(30, 1, 16, "vendor_a", "01234567-1234-1234-1234-123456789abc"),
             part(17, 1024, 32 * 1024 if names else 128 * 1024, "userdata")]
    bounds = [32 * 1024, 32 * 1024 + 512, 34 * 1024, 128 * 1024]
    for index, name in enumerate(names):
        end = bounds[index + 1] if index < len(names) - 1 else 128 * 1024
        parts.append(part(index + 1, bounds[index], end, name, i.ESP_TYPE if index == 0 else i.LINUX_TYPE))
    return i.Table(device, sector, 34, 256 * i.GIB // sector - 34, str(uuid.UUID(int=100)), tuple(sorted(parts, key=lambda p: p.number)))


def applied(plan):
    parts = [replace(p, end=plan.userdata_end) if p == plan.userdata else p
             for p in plan.table.parts if p not in plan.remove]
    parts += [replace(p, uuid=str(uuid.UUID(int=1000 + p.number))) for p in plan.create]
    return replace(plan.table, parts=tuple(sorted(parts, key=lambda p: p.number)), unused=())


def table_json(t):
    return dict(partitiontable=dict(device=t.device, label="gpt", unit="sectors", sectorsize=t.sector_size,
                                   firstlba=t.first, lastlba=t.last, id=t.disk_id, **{"table-length": str(t.entry_count)},
                                   partitions=[dict(node=i.part_node(t.device, p.number), start=p.start, size=p.end-p.start,
                                                    name=p.name, type=p.type, uuid=p.uuid, attrs=p.attrs) for p in t.parts]))


class PlanningTests(unittest.TestCase):
    def test_unused_gpt_slots_ignore_stale_overlapping_geometry(self):
        t = replace(table(), entry_count=32)
        data = table_json(t)
        data["partitiontable"]["partitions"].append(dict(node="/dev/sda18", start=1000, size=999999999,
                                                       type="00000000-0000-0000-0000-000000000000"))
        t = replace(t, unused=(18,))
        self.assertEqual(i.Table.parse(t.device, data), t)
        plan = i.Plan.make(t, 32)
        self.assertNotIn("999999999", plan.script())
        plan.verify(applied(plan))
        with self.assertRaises(i.Error):
            plan.verify(replace(applied(plan), entry_count=128))

    def test_planner_does_not_allocate_beyond_gpt_capacity(self):
        t = table()
        t = replace(t, entry_count=3, parts=tuple(replace(p, number=n) for n, p in enumerate(t.parts, 1)))
        with self.assertRaisesRegex(i.Error, "Not enough free GPT partition slots"):
            i.Plan.make(t, 32)

    def test_parse_roundtrip_devices_and_sector_sizes(self):
        for device in ("/dev/sda", "/dev/mmcblk0", "/dev/nvme0n1"):
            for sector in (512, 4096):
                t = table(device=device, sector=sector)
                self.assertEqual(i.Table.parse(device, table_json(t)), t)
                plan = i.Plan.make(t, 16)
                plan.verify(applied(plan))
                self.assertEqual(plan.create[0].start, plan.userdata_end)
                self.assertEqual((plan.create[0].end-plan.create[0].start)*sector, i.ESP_SIZE)
                self.assertGreaterEqual((plan.create[-1].end-plan.create[-1].start)*sector, i.ROOT_MIN)

    def test_bad_partition_output_is_rejected(self):
        t = table()
        for change in (
            lambda d: d.update(label="dos"),
            lambda d: d.pop("sectorsize"),
            lambda d: d.update(sectorsize=True),
            lambda d: d["partitions"][0].update(node="/dev/sdb1"),
            lambda d: d["partitions"][0].update(size=-1),
            lambda d: d["partitions"][0].update(start=True),
            lambda d: d["partitions"][0].update(start=d["lastlba"]),
            lambda d: d["partitions"][1].update(start=d["partitions"][0]["start"]),
        ):
            data = table_json(t)
            change(data["partitiontable"])
            with self.assertRaises(i.Error):
                i.Table.parse(t.device, data)

    def test_fresh_preserves_android_start_type_uuid_and_high_numbered_vendor(self):
        plan = i.Plan.make(table(), 16)
        actual = applied(plan)
        plan.verify(actual)
        android = next(p for p in actual.parts if p.name == "userdata")
        self.assertEqual(replace(android, end=plan.userdata.end), plan.userdata)
        self.assertEqual([p.number for p in plan.create], [18, 19, 20])
        self.assertIn(next(p for p in plan.table.parts if p.name == "vendor_a"), actual.parts)
        self.assertIn(f"uuid={plan.userdata.uuid}", plan.script())
        self.assertIn(f"/dev/sda17: start={plan.userdata.start}, size={plan.userdata_end-plan.userdata.start}", plan.script())

    def test_replacement_reuses_slots_immediately_after_userdata(self):
        t = table(["ARMADA", "ARMADA_BOOT", "ARMADA_ROOT"])
        t = replace(t, parts=tuple(replace(p, number=p.number+17) if p.number < 4 else p for p in t.parts))
        plan = i.Plan.make(t)
        self.assertEqual([p.number for p in plan.create], [18, 19, 20])
        plan.verify(applied(plan))

    def test_occupied_preferred_slot_falls_back_without_moving_vendor(self):
        for names in ((), ("OTHER_BOOT", "OTHER_ROOT")):
            for occupied in (18, 19, 20):
                with self.subTest(names=names, occupied=occupied):
                    t = table(names)
                    t = replace(t, parts=tuple(replace(p, number=occupied) if p.number == 30 else p for p in t.parts))
                    plan = i.Plan.make(t, None if names else 16)
                    self.assertEqual([p.number for p in plan.create], [1, 2, 3])
                    self.assertIn(next(p for p in t.parts if p.number == occupied), applied(plan).parts)
                    plan.verify(applied(plan))

    def test_preferred_slots_beyond_capacity_fall_back(self):
        t = table()
        t = replace(t, entry_count=32, parts=tuple(replace(p, number=31) if p.name == "userdata" else p for p in t.parts))
        plan = i.Plan.make(t, 16)
        self.assertEqual([p.number for p in plan.create], [1, 2, 3])
        plan.verify(applied(plan))

    def test_userdata_attributes_are_preserved(self):
        attrs = "RequiredPartition LegacyBIOSBootable GUID:48,60,63"
        t = table()
        t = replace(t, parts=tuple(replace(p, attrs=attrs) if p.name == "userdata" else p for p in t.parts))
        self.assertIn('attrs="' + attrs + '"', i.Plan.make(t, 16).script())

    def test_replacement_uses_position_regardless_of_names_or_types(self):
        for names in (["ARMADA", "ARMADA_BOOT", "ARMADA_ROOT"], ["OTHER_BOOT", "OTHER_ROOT"],
                      ["ROCKNIX", "STORAGE"], ["vendor", "metadata"], ["root", "root"], ["", ""]):
            with self.subTest(names=names):
                t = table(names)
                t = replace(t, parts=tuple(replace(p, type="01234567-1234-1234-1234-123456789abc")
                                          if p.name in names else p for p in t.parts))
                plan = i.Plan.make(t)
                self.assertEqual(t.info()["mode"], "occupied")
                self.assertEqual(plan.operation, "replace")
                self.assertEqual(plan.userdata_end, plan.userdata.end)
                self.assertEqual(plan.remove, tuple(sorted((p for p in t.parts if p.start >= plan.userdata.end), key=lambda p: p.start)))
                self.assertNotIn(plan.userdata, plan.remove)
                self.assertIn("Erase ALL partitions after Android userdata", plan.describe())
                for p in plan.remove:
                    self.assertIn(i.part_node(t.device, p.number), plan.describe())
                    self.assertIn(i.part_node(t.device, p.number), t.info()["partition_names"])
                plan.verify(applied(plan))

    def test_single_unknown_tail_partition_can_be_replaced(self):
        plan = i.Plan.make(table(["custom-storage"]))
        self.assertEqual(len(plan.remove), 1)
        self.assertEqual(len(plan.create), 3)

    def test_earlier_partitions_are_preserved_regardless_of_label_or_number(self):
        for name in ("ARMADA", "vendor_a", ""):
            t = table(["custom-storage"])
            t = replace(t, parts=tuple(replace(p, name=name) if p.number == 30 else p for p in t.parts))
            plan = i.Plan.make(t)
            before = next(p for p in t.parts if p.number == 30)
            self.assertNotIn(before, plan.remove)
            self.assertIn(before, applied(plan).parts)
            plan.verify(applied(plan))

    def test_missing_or_duplicate_userdata_is_rejected(self):
        for old, new in (("vendor_a", "userdata"), ("userdata", "data")):
            t = table()
            parts = tuple(replace(p, name=new) if p.name == old else p for p in t.parts)
            with self.assertRaises(i.Error):
                i.Plan.make(replace(t, parts=parts), 16)

    def test_too_small_and_invalid_sizes_are_rejected(self):
        t = table()
        for size in (None, True, 0, 7, 1000):
            with self.assertRaises(i.Error):
                i.Plan.make(t, size)
        short = replace(t, parts=tuple(replace(p, end=p.start+16*i.GIB//t.sector_size) if p.name == "userdata" else p for p in t.parts))
        self.assertEqual(short.info()["mode"], "toosmall")
        with self.assertRaises(i.Error):
            i.Plan.make(short, 8)
        with self.assertRaises(i.Error):
            i.Plan.make(table(["OTHER_BOOT", "OTHER_ROOT"]), 16)

    def test_verification_rejects_changed_android_and_extra_partitions(self):
        plan = i.Plan.make(table(["OTHER_BOOT", "OTHER_ROOT"]))
        good = applied(plan)
        for t in (replace(good, disk_id=str(uuid.uuid4())),
                  replace(good, parts=tuple(replace(p, end=p.end-1) if p.name == "userdata" else p for p in good.parts)),
                  replace(good, parts=tuple(replace(p, uuid=str(uuid.uuid4())) if p.name == "vendor_a" else p for p in good.parts)),
                  replace(good, parts=good.parts[:-1])):
            with self.assertRaises(i.Error):
                plan.verify(t)


class ExecutionTests(unittest.TestCase):
    def test_fingerprint_binds_table_and_kernel_device_identity(self):
        t = table()
        with patch.object(i.os, "stat", return_value=argparse.Namespace(st_rdev=os.makedev(8, 0))), patch.object(Path, "read_text", return_value="10\n"):
            expected = i.table_fingerprint(t)
            self.assertEqual(expected, i.table_fingerprint(t))
            for changed in (replace(t, device="/dev/sdb"), replace(t, disk_id="changed"),
                            replace(t, parts=tuple(replace(p, end=p.end-1) if p.name == "userdata" else p for p in t.parts))):
                self.assertNotEqual(expected, i.table_fingerprint(changed))
            with patch.object(Path, "read_text", return_value="11\n"):
                self.assertNotEqual(expected, i.table_fingerprint(t))
            with patch.object(i.os, "stat", return_value=argparse.Namespace(st_rdev=os.makedev(8, 16))):
                self.assertNotEqual(expected, i.table_fingerprint(t))

    def test_changed_confirmation_aborts_before_planning(self):
        args = argparse.Namespace(command="install", device="/dev/sda", expect_table="confirmed")
        with patch.object(i.os, "geteuid", return_value=0), patch.object(i, "discover_device", return_value="/dev/sda"), patch.object(i, "read_table", return_value=table()), patch.object(i, "table_fingerprint", return_value="changed"), patch.object(i.Plan, "make") as make, patch.object(i, "run") as run:
            with self.assertRaisesRegex(i.Error, "changed after confirmation"):
                i.execute(args)
        make.assert_not_called()
        run.assert_not_called()

    def test_kernel_geometry_in_512_byte_sectors_and_device_nodes(self):
        for sector in (512, 4096):
            for names, size in (((), 16), (("OTHER_BOOT", "OTHER_ROOT"), None)):
                plan = i.Plan.make(table(names, sector=sector), size)
                parts = {i.part_node(plan.table.device, p.number): p for p in applied(plan).parts}
                def read(path):
                    p = parts["/dev/" + path.parent.name]
                    return dict(start=str(p.start * sector // 512), size=str((p.end-p.start) * sector // 512), dev=f"8:{p.number}")[path.name]
                def stat(node):
                    return argparse.Namespace(st_rdev=os.makedev(8, parts[node].number))
                with patch.object(Path, "read_text", read), patch.object(i.os, "stat", side_effect=stat):
                    plan.verify_kernel()
                    for attribute in ("start", "size", "dev"):
                        def bad_read(path):
                            return ("8:99" if attribute == "dev" else "1") if path.name == attribute else read(path)
                        with patch.object(Path, "read_text", bad_read), self.assertRaisesRegex(i.Error, "does not match"):
                            plan.verify_kernel()
                    with patch.object(Path, "read_text", side_effect=FileNotFoundError("missing partition")), self.assertRaisesRegex(i.Error, "Could not verify"):
                        plan.verify_kernel()

    def test_kernel_mismatch_stops_before_userdata_wipe(self):
        plan = i.Plan.make(table(), 16)
        job = i.Installation(plan)
        with patch.object(i, "refuse_boot_disk"), patch.object(i, "require_idle"), patch.object(i, "read_table", side_effect=[plan.table, applied(plan)]), patch.object(i.Plan, "verify_kernel", side_effect=i.Error("stale kernel geometry")), patch.object(i, "run") as run:
            with self.assertRaisesRegex(i.Error, "stale kernel geometry"):
                job.partition()
        self.assertFalse(any(call.args[0] == "dd" for call in run.call_args_list))
        self.assertTrue(job.changed)

    def test_preflight_uses_source_context_without_target_discovery(self):
        for failed in (False, True):
            events = []
            @contextmanager
            def source():
                events.append("locked")
                try:
                    if failed:
                        raise i.Error("corrupt source")
                    yield Mock(checksum="selected")
                finally:
                    events.append("released")
            with patch.object(i.os, "geteuid", return_value=0), patch.object(i, "locked_source", source), patch.object(i, "inhibit_sleep", return_value=nullcontext()), patch.object(i, "repo_path", return_value="/source/repo"), patch.object(i, "run") as run, patch.object(i, "discover_device") as discover, patch.object(i.Installation, "partition") as partition, patch("sys.stdout", new_callable=io.StringIO) as out:
                if failed:
                    with self.assertRaisesRegex(i.Error, "corrupt source"):
                        i.execute(argparse.Namespace(command="preflight"))
                else:
                    i.execute(argparse.Namespace(command="preflight"))
                    self.assertIn("Validated source commit: selected", out.getvalue())
                    self.assertIn("seconds; internal storage was not touched", out.getvalue())
                    run.assert_called_once_with("ostree", "fsck", "--repo=/source/repo")
            self.assertEqual(events, ["locked", "released"])
            discover.assert_not_called()
            partition.assert_not_called()

    def test_changed_table_aborts_without_writing(self):
        plan = i.Plan.make(table(), 16)
        job = i.Installation(plan)
        with patch.object(i, "refuse_boot_disk"), patch.object(i, "read_table", return_value=replace(plan.table, disk_id="changed")), patch.object(i, "run") as run:
            with self.assertRaisesRegex(i.Error, "changed after inspection"):
                job.partition()
        run.assert_not_called()
        self.assertFalse(job.changed)

    def test_in_use_target_aborts_without_writing(self):
        plan = i.Plan.make(table(), 16)
        job = i.Installation(plan)
        with patch.object(i, "refuse_boot_disk"), patch.object(i, "read_table", return_value=plan.table), patch.object(i, "require_idle", side_effect=i.Error("in use")), patch.object(i, "run") as run:
            with self.assertRaisesRegex(i.Error, "in use"):
                job.partition()
        run.assert_not_called()
        self.assertFalse(job.changed)

    def test_partition_operations_wipe_android_only_on_fresh_install(self):
        for t, size in ((table(), 16), (table(["OTHER_BOOT", "OTHER_ROOT"]), None)):
            plan = i.Plan.make(t, size)
            job = i.Installation(plan)
            with patch.object(i, "refuse_boot_disk"), patch.object(i, "require_idle"), patch.object(i, "read_table", side_effect=[t, applied(plan)]), patch.object(i.Plan, "verify_kernel"), patch.object(i, "run") as run:
                job.partition()
            commands = [call.args for call in run.call_args_list]
            self.assertEqual(sum(c[0] == "dd" for c in commands), int(plan.operation == "fresh"))
            self.assertTrue(job.changed)

    def test_partition_failure_arms_recovery_and_stops_commands(self):
        plan = i.Plan.make(table(), 16)
        with patch.object(i, "refuse_boot_disk"), patch.object(i, "require_idle"), patch.object(i, "read_table", return_value=plan.table), patch.object(i, "run", side_effect=i.Error("write failed")) as run, patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaisesRegex(i.Error, "write failed"):
                with i.operation(plan) as job:
                    job.partition()
        self.assertEqual(run.call_count, 1)
        self.assertIn("Internal storage was modified", err.getvalue())

    def test_source_repository_is_readonly_before_validation(self):
        for remount_fails in (False, True):
            api = Mock()
            sysroot = api.Sysroot.new_default.return_value
            repo = Mock()
            repo.get_path.return_value.get_path.return_value = "/sysroot/ostree/repo"
            sysroot.get_repo.return_value = (True, repo)
            events = []
            def command(*args):
                events.append(args)
                if remount_fails and "remount,bind,ro" in args:
                    raise i.Error("read-only mount failed")
            def validate(*args):
                self.assertIn(("mount", "-o", "remount,bind,ro", "/sysroot/ostree/repo"), events)
                return "validated"
            with patch.object(i, "ostree_api", return_value=(Mock(), api)), patch.object(i, "repo_path", return_value="/sysroot/ostree/repo"), patch.object(i, "run", side_effect=command), patch.object(i.SourceImage, "read", side_effect=validate) as read:
                if remount_fails:
                    with self.assertRaisesRegex(i.Error, "read-only mount failed"):
                        with i.locked_source():
                            self.fail("unprotected source was accepted")
                    read.assert_not_called()
                else:
                    with i.locked_source() as source:
                        self.assertEqual(source, "validated")
                        sysroot.unlock.assert_not_called()
                self.assertEqual(events[-1], ("umount", "/sysroot/ostree/repo"))
                sysroot.unlock.assert_called_once()

    def test_source_failure_never_reaches_partitioning(self):
        args = argparse.Namespace(command="install", device=None, userdata_gib=16, assume_yes=True, expect_table=None)
        @contextmanager
        def bad_source():
            raise i.Error("missing source layer")
            yield
        with tempfile.TemporaryFile() as lock, patch.object(i.os, "geteuid", return_value=0), patch.object(i, "discover_device", return_value="/dev/sda"), patch.object(i, "read_table", return_value=table()), patch.object(i, "require_commands"), patch.object(i, "refuse_boot_disk"), patch.object(i, "require_idle"), patch.object(i, "open", return_value=lock, create=True), patch.object(i, "inhibit_sleep", return_value=nullcontext()), patch.object(i, "base_kargs", return_value=[]), patch.object(i, "locked_source", bad_source), patch.object(i.Installation, "partition") as partition:
            with self.assertRaisesRegex(i.Error, "missing source layer"):
                i.execute(args)
        partition.assert_not_called()

    def test_boot_image_scratch_ignores_sd_tmpdir(self):
        job = i.Installation(i.Plan.make(table(), 16))
        source = Mock(checksum="selected")
        source.origin.to_data.return_value = ("origin", 6)
        mkdtemp = tempfile.mkdtemp
        with tempfile.TemporaryDirectory() as work:
            def temporary(*args, **kwargs):
                if kwargs.get("dir") == "/run":
                    kwargs["dir"] = work
                return mkdtemp(*args, **kwargs)
            def command(*args, **kwargs):
                if args[:2] == ("ostree", "checkout"):
                    args[-1].mkdir()
                elif args[0] == i.BOOTIMG:
                    env = kwargs["env"]
                    scratch = Path(env["TMPDIR"])
                    self.assertEqual(scratch.parent, job.work / "target")
                    self.assertTrue(scratch.is_dir())
                    (Path(env["ESP"]) / "KERNEL").write_bytes(b"boot image")
            with patch.dict(i.os.environ, TMPDIR="/sd/slow"), patch.object(i.tempfile, "mkdtemp", side_effect=temporary), patch.object(i, "output", return_value="valid-uuid"), patch.object(i, "run", side_effect=command) as run, patch.object(i, "configure_deployment"):
                job.deploy(source, [])
                self.assertTrue(any(call.args[0] == i.BOOTIMG for call in run.call_args_list))
                self.assertTrue(any(call.args[-2:] == ("sysroot.readonly", "true") for call in run.call_args_list))
                self.assertEqual(list((job.work / "target").glob(".armada-bootimg.*")), [])
                job.close()

    def test_interrupted_mount_is_still_cleaned_up(self):
        for mounted in (True, False):
            job = i.Installation(i.Plan.make(table(), 16))
            with tempfile.TemporaryDirectory() as work:
                target = Path(work) / "target"
                with patch.object(i, "run", side_effect=i.Interrupted(signal.SIGTERM)), patch.object(Path, "is_mount", return_value=mounted):
                    with self.assertRaises(i.Interrupted):
                        job.mount("/dev/fake", target)
                self.assertEqual(job.mounts, [target] if mounted else [])

    def test_copy_failure_prevents_deployment_and_boot_image(self):
        job = i.Installation(i.Plan.make(table(), 16))
        source = Mock()
        source.copy.side_effect = i.Error("corrupt destination")
        with tempfile.TemporaryDirectory() as work:
            with patch.object(i.tempfile, "mkdtemp", return_value=work), patch.object(i, "output", return_value="a-valid-uuid"), patch.object(i, "run") as run:
                with self.assertRaisesRegex(i.Error, "corrupt destination"):
                    job.deploy(source, [])
                commands = [call.args for call in run.call_args_list]
                self.assertFalse(any(c[:3] == ("ostree", "admin", "deploy") or c[0] == i.BOOTIMG for c in commands))
                source.copy.assert_called_once_with(Path(work) / "target")
                mounts = list(job.mounts)
                job.close()
                self.assertEqual([call.args[1] for call in run.call_args_list if call.args[0] == "umount"][1:], list(reversed(mounts)))

    def test_cleanup_is_reverse_order_and_preserves_failed_mount(self):
        job = i.Installation(i.Plan.make(table(), 16))
        with tempfile.TemporaryDirectory() as work:
            job.work = Path(work) / "keep"
            job.work.mkdir()
            job.mounts = [job.work / "root", job.work / "root/boot", job.work / "root/boot/efi"]
            def unmount(*args):
                if args[1] == job.mounts[-1]:
                    raise i.Error("busy")
            with patch.object(i, "run", side_effect=unmount) as run, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(i.Error):
                    job.close()
            self.assertEqual([call.args[1] for call in run.call_args_list], list(reversed(job.mounts)))
            self.assertTrue(job.work.exists())

    def test_cleanup_failure_does_not_hide_original_failure(self):
        with patch.object(i.Installation, "close", side_effect=i.Error("cleanup failed")):
            with self.assertRaisesRegex(i.Error, "original"):
                with i.operation(i.Plan.make(table(), 16)):
                    raise i.Error("original")

    def test_boot_disk_alias_is_rejected(self):
        with patch.object(Path, "is_mount", return_value=True), patch.object(Path, "is_block_device", return_value=True), patch.object(i, "output", side_effect=["/dev/dm-0[/root]", "253:0\n8:2\n8:0"]), patch.object(i.os, "stat", return_value=argparse.Namespace(st_rdev=os.makedev(8, 0))):
            with self.assertRaisesRegex(i.Error, "booted from"):
                i.refuse_boot_disk("/dev/disk/by-id/internal")

    def test_mount_in_pid1_namespace_blocks_install(self):
        plan = i.Plan.make(table(), 16)
        def read(path, *args, **kwargs):
            return "1 0 8:17 / /android rw - ext4 /dev/sda17 rw\n" if str(path) == "/proc/1/mountinfo" else "Filename Type Size Used Priority\n" if str(path) == "/proc/swaps" else ""
        with patch.object(Path, "read_text", read), patch.object(Path, "iterdir", return_value=iter(())), patch.object(i.os, "stat", return_value=argparse.Namespace(st_rdev=os.makedev(8, 17), st_mode=statmod.S_IFBLK)):
            with self.assertRaisesRegex(i.Error, "in use"):
                i.require_idle(plan)

    def test_gui_confirms_all_tail_partitions_without_distro_detection(self):
        info = table(["vendor", "metadata"]).info()
        info.update(device="/dev/sda", table_fingerprint="confirmed")
        gui = i.Gui(None)
        with patch.object(i, "output", return_value=json.dumps(info)), patch.object(gui, "confirm") as confirm, patch.object(gui, "progress") as progress, patch.object(gui, "dialog", return_value=Mock(returncode=1)):
            gui.start()
        self.assertIn("All data in that space will be erased", confirm.call_args.args[0])
        self.assertIn(info["partition_names"], confirm.call_args.args[0])
        progress.assert_called_once_with(["install", "--expect-table", "confirmed"])
        self.assertEqual(gui.command, ["sudo", i.SELF, "--device", "/dev/sda"])

    def test_gui_failure_shows_bounded_log_and_recovery(self):
        gui = i.Gui(None)
        with tempfile.TemporaryDirectory() as work:
            log = Path(work) / "install.log"
            fd = os.open(log, os.O_CREAT | os.O_WRONLY, 0o600)
            def command(*args, **kwargs):
                kwargs["stdout"].write("old output\n" * 1000 + "ERROR: missing source layer\n")
                return Mock(returncode=1)
            with patch.object(i.tempfile, "mkstemp", return_value=(fd, str(log))), patch.object(i.subprocess, "Popen"), patch.object(i, "run", side_effect=command):
                with self.assertRaises(i.Error) as error:
                    gui.progress(["install"])
            message = str(error.exception)
            self.assertIn("ERROR: missing source layer", message)
            self.assertIn("select SD as the boot source in ABL", message)
            self.assertIn("available until reboot", message)
            self.assertLess(len(message), 5000)

    def test_reset_is_not_a_command(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "reset", "-y"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("UNINSTALL CFW", result.stdout)
        self.assertNotIn("reset", result.stdout)

    def test_toml_kargs_are_parsed_and_filtered(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root/"a.toml").write_text('kargs = ["quiet", "root=old", "rootflags=old", "ostree=old"]\n')
            (root/"b.toml").write_text('match-architectures = ["other"]\nkargs = ["bad"]\n')
            self.assertEqual(i.base_kargs(root), ["quiet"])

    def test_subprocess_cancellation_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as work:
            ready, cleaned = Path(work)/"ready", Path(work)/"cleaned"
            child_code = f"""
import signal,sys,time
from pathlib import Path
def stop(signum, frame):
    time.sleep(0.1)
    Path({str(cleaned)!r}).touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(ready)!r}).touch()
time.sleep(10)
"""
            parent_code = f"""
import runpy,sys
m=runpy.run_path({str(SCRIPT)!r})
try: m['run'](sys.executable, '-c', {child_code!r})
except m['Interrupted']: sys.exit(143)
"""
            parent = subprocess.Popen([sys.executable, "-c", parent_code])
            try:
                deadline = time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                parent.send_signal(signal.SIGTERM)
                self.assertEqual(parent.wait(timeout=5), 143)
                self.assertTrue(cleaned.exists())
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()


BOOT_PARTUUID = "0d0b0d0b-1111-4111-8111-00000000b007"
ROOT_PARTUUID = "0d0b0d0b-2222-4222-8222-00000000f007"
BOOT_FS_UUID = "aaaa1111-0000-4000-8000-00000000b007"
ROOT_FS_UUID = "bbbb2222-0000-4000-8000-00000000f007"
SEED_DIRECTORY = "/.armada-install-seed"
PREPARED_RECORD = ".armada-install-prepared.json"


def existing_part(sector, number, start_mib, size_mib, name, kind=None, partuuid="", attrs=""):
    return i.Partition(number, start_mib * i.MIB // sector, (start_mib + size_mib) * i.MIB // sector,
                       name, i.LINUX_TYPE if kind is None else kind, partuuid, attrs)


def existing_table(device="/dev/sda", sector=4096):
    start, userdata_size = 1024, 128 * 1024
    boot_start = start + userdata_size
    root_start = boot_start + 1024
    end = root_start + 64 * 1024
    parts = (existing_part(sector, 1, 1, 15, "vendor_a", kind="01234567-1234-1234-1234-123456789abc"),
             existing_part(sector, 2, boot_start, 1024, "ARMADA_BOOT", partuuid=BOOT_PARTUUID),
             existing_part(sector, 3, 16, start - 16, "metadata", attrs="GUID:48,60,63"),
             existing_part(sector, 4, start, userdata_size, "userdata"),
             existing_part(sector, 5, root_start, 64 * 1024, "ARMADA_ROOT", partuuid=ROOT_PARTUUID))
    return i.Table(device, sector, 34, (end + 4096) * i.MIB // sector, str(uuid.UUID(int=100)),
                   tuple(sorted(parts, key=lambda p: p.number)))


def measured_table(device="/dev/sda", sector=4096):
    """Fixture using the extents measured on the real TB322FC 4Kn target.

    Only the userdata/ARMADA_BOOT/ARMADA_ROOT extents, the inclusive table
    ``last`` and the 4Kn sector size are actual measured values. The vendor_a
    and metadata entries, the table ``first`` bound and the disk id remain the
    synthetic placeholders copied from ``existing_table``; this fixture is not a
    complete captured GPT.
    """
    vendor = existing_part(sector, 1, 1, 15, "vendor_a", kind="01234567-1234-1234-1234-123456789abc")
    metadata = existing_part(sector, 3, 16, 1008, "metadata", attrs="GUID:48,60,63")
    boot = i.Partition(2, 39526912, 39789056, "ARMADA_BOOT", i.LINUX_TYPE, BOOT_PARTUUID)
    userdata = i.Partition(4, 5972392, 39526824, "userdata", i.LINUX_TYPE)
    root = i.Partition(5, 39789056, 123871227, "ARMADA_ROOT", i.LINUX_TYPE, ROOT_PARTUUID)
    return i.Table(device, sector, 34, 123871226, str(uuid.UUID(int=100)),
                   tuple(sorted((vendor, boot, metadata, userdata, root), key=lambda p: p.number)))


def part_named(t, name):
    return next(p for p in t.parts if p.name == name)


def replace_part(t, selector, **changes):
    return replace(t, parts=tuple(replace(p, **changes) if p.name == selector else p for p in t.parts))


def shift_part(t, name, sectors):
    return replace(t, parts=tuple(replace(p, start=p.start + sectors, end=p.end + sectors)
                                  if p.name == name else p for p in t.parts))


def prepared_record(t, **changes):
    boot, root = part_named(t, "ARMADA_BOOT"), part_named(t, "ARMADA_ROOT")
    record = {
        "table_fingerprint": "fingerprint",
        "device": t.device,
        "sector_size": t.sector_size,
        "boot": {"partuuid": boot.uuid, "filesystem_uuid": BOOT_FS_UUID,
                 "start": boot.start, "end": boot.end,
                 "size_bytes": (boot.end - boot.start) * t.sector_size},
        "root": {"partuuid": root.uuid, "filesystem_uuid": ROOT_FS_UUID,
                 "start": root.start, "end": root.end,
                 "size_bytes": (root.end - root.start) * t.sector_size},
        "subvolumes": ["root", "var", "home"],
        "seed_directory": SEED_DIRECTORY,
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(record.get(key), dict):
            record[key] = {**record[key], **value}
        else:
            record[key] = value
    return record


@contextmanager
def subvolume_show():
    """Mock only ``btrfs subvolume show``; everything else must not be needed."""
    def fake(*args, **kwargs):
        args = [str(arg) for arg in args]
        if args[:3] == ["btrfs", "subvolume", "show"]:
            return subprocess.CompletedProcess(args, 0, f"Name: {Path(args[-1]).name}\n", "")
        raise AssertionError("unexpected command: " + " ".join(args))
    with patch.object(i, "run", side_effect=fake):
        yield


@contextmanager
def prepared_tree(record, subvolumes=("root", "var", "home"), root_link=False, populate_root=False):
    with tempfile.TemporaryDirectory() as work:
        top = Path(work) / "top"
        top.mkdir()
        for name in subvolumes:
            if name == "root" and root_link:
                continue
            (top / name).mkdir()
        if root_link:
            (top / "root").symlink_to(top / "var")
        if populate_root:
            (top / "root" / "leftover").write_text("partial")
        (top / PREPARED_RECORD).write_text(json.dumps(record))
        yield top


def prepare_args(expect="fingerprint", device="/dev/sda"):
    return argparse.Namespace(command="prepare-existing", device=device, boot_partuuid=BOOT_PARTUUID,
                              root_partuuid=ROOT_PARTUUID, expect_table=expect)


SEED_COMMIT = "f" * 64
SEED_MANIFEST_DIGEST = "sha256:" + "a" * 64
SEED_ORIGIN = "ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing"


def deploy_args(**overrides):
    values = dict(command="deploy-existing", device="/dev/sda", boot_partuuid=BOOT_PARTUUID,
                  root_partuuid=ROOT_PARTUUID, expect_table="fingerprint", seed_commit=SEED_COMMIT,
                  seed_manifest_digest=SEED_MANIFEST_DIGEST, origin=SEED_ORIGIN)
    values.update(overrides)
    return argparse.Namespace(**values)


def emitted_json(text):
    candidates = [text.strip()] + [line.strip() for line in reversed(text.splitlines())]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and "table_fingerprint" in data:
            return data
    return None


def comparable(record):
    return {key: value for key, value in record.items() if key != "device"}


PREPARE_TOOLS = ("sfdisk", "lsblk", "blkid", "mkfs.ext4", "mkfs.btrfs", "btrfs", "mount", "umount")
DEPLOY_TOOLS = ("sfdisk", "lsblk", "blkid", "btrfs", "mount", "umount", "ostree", "cp")
LEGACY_ONLY_TOOLS = ("bootc", "ostree", "systemd-inhibit", "mkfs.vfat", "partprobe", "udevadm", "dd")


class PrepareSandbox:
    """Small fake for the command/device/sysfs boundary used by prepare_existing."""

    def __init__(self, table, work, **options):
        self.table = table
        self.work = Path(work)
        self.fingerprint = options.get("fingerprint", "fingerprint")
        self.deploy = options.get("deploy", False)
        self.types = dict(options.get("types") or {})
        self.labels = dict(options.get("labels") or {})
        self.uuids = dict(options.get("uuids") or {})
        self.mountinfo = options.get("mountinfo", "")
        self.swaps = options.get("swaps", "")
        self.holders = list(options.get("holders") or ())
        self.fail_umount = options.get("fail_umount", False)
        self.fail_command = options.get("fail_command")
        self.mismatch_kernel = options.get("mismatch_kernel", False)
        self.blkid_status = dict(options.get("blkid_status") or {})
        self.commands = []
        self.mounts = []
        self.formatted = []
        self.stdout = io.StringIO()
        self.record = None
        self.sysfs = {}
        self.rdev = {}
        by_name = {}
        for part in table.parts:
            by_name.setdefault(part.name, part)
        for name in ("userdata", "ARMADA_BOOT", "ARMADA_ROOT"):
            part = by_name[name]
            node = i.part_node(table.device, part.number)
            base = f"/sys/class/block/{Path(table.device).name}/{Path(node).name}"
            self.sysfs[base + "/start"] = "1" if self.mismatch_kernel else str(part.start * table.sector_size // 512)
            self.sysfs[base + "/size"] = str((part.end - part.start) * table.sector_size // 512)
            self.sysfs[base + "/dev"] = f"8:{part.number}"
            self.rdev[node] = os.makedev(8, part.number)

    def __enter__(self):
        self.stack = ExitStack()
        self.lock = tempfile.TemporaryFile()
        self.stack.callback(self.lock.close)
        real_open, real_stat, real_mkdtemp = open, os.stat, tempfile.mkdtemp
        real_read_text, real_iterdir = Path.read_text, Path.iterdir
        real_which = i.shutil.which

        def read_text(path, *args, **kwargs):
            name = str(path)
            if name in self.sysfs:
                return self.sysfs[name]
            if name in ("/proc/1/mountinfo", "/proc/self/mountinfo"):
                return self.mountinfo
            if name == "/proc/swaps":
                return "Filename Type Size Used Priority\n" + self.swaps
            return real_read_text(path, *args, **kwargs)

        def iterdir(path):
            if "holders" in str(path):
                return iter(self.holders)
            return real_iterdir(path)

        def open_device(path, *args, **kwargs):
            if "lock" in str(path):
                return self.lock
            return real_open(path, *args, **kwargs)

        def stat(path, *args, **kwargs):
            if str(path) in self.rdev:
                return argparse.Namespace(st_rdev=self.rdev[str(path)], st_mode=statmod.S_IFBLK)
            return real_stat(path, *args, **kwargs)

        def mkdtemp(*args, **kwargs):
            return real_mkdtemp(prefix="sandbox.", dir=str(self.work))

        def which(name, *args, **kwargs):
            allowed = DEPLOY_TOOLS if self.deploy else PREPARE_TOOLS
            if name in allowed:
                return "/usr/bin/" + name
            if name in LEGACY_ONLY_TOOLS or name == i.BOOTIMG:
                return None
            return real_which(name, *args, **kwargs)

        enter = self.stack.enter_context
        enter(patch.object(i.os, "geteuid", return_value=0))
        enter(patch.object(i, "discover_device", return_value=self.table.device))
        enter(patch.object(i, "read_table", return_value=self.table))
        enter(patch.object(i, "table_fingerprint", return_value=self.fingerprint))
        enter(patch.object(i, "run", self._run))
        enter(patch.object(i, "open", open_device, create=True))
        enter(patch.object(i.tempfile, "mkdtemp", mkdtemp))
        enter(patch.object(i.shutil, "which", which))
        enter(patch.object(Path, "read_text", read_text))
        enter(patch.object(Path, "iterdir", iterdir))
        enter(patch.object(i.os, "stat", stat))
        enter(patch("sys.stdout", self.stdout))
        return self

    def __exit__(self, *error):
        self._snapshot()
        return self.stack.__exit__(*error)

    def _blkid(self, args):
        """Return (output, status) modeling util-linux blkid probe semantics.

        A node with a recognized signature or attribute is identified (status 0),
        and filtering a tag it does not carry yields blank output, still status 0.
        A node with no recognized signature has no info (status 2), not a blank
        empty filesystem. A node is recognized once it carries a known type or
        label, or once this fake has formatted it, so a preexisting filesystem
        with a provided UUID reports it even when it was not formatted in this run.
        """
        node = args[-1]
        joined = " ".join(args)
        override = self.blkid_status.get(node)
        if override is not None:
            return "", override
        formatted = {command[-1] for command in self.formatted}
        identified = node in self.types or node in self.labels or node in formatted
        if "TYPE" in joined:
            value = self.types.get(node, "")
        elif "LABEL" in joined:
            value = self.labels.get(node, "")
        else:
            value = self.uuids.get(node, "") if identified else ""
        if value:
            return value, 0
        return "", 0 if identified else 2

    def _snapshot(self):
        for path in self.work.rglob(PREPARED_RECORD):
            try:
                self.record = json.loads(path.read_text())
            except (OSError, ValueError):
                pass

    def _run(self, *args, capture=False, check=True, input=None, **kwargs):
        args = tuple(str(arg) for arg in args)
        self._snapshot()
        self.commands.append(args)
        if self.fail_command is not None and self.fail_command(args):
            raise i.Error("failed: " + " ".join(args))
        command = args[0]
        status = 0
        if command == "blkid":
            output, status = self._blkid(args)
        elif command == "mount":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            self.mounts.append(Path(args[-1]))
            output = ""
        elif command == "umount":
            if self.fail_umount:
                raise i.Error("umount failed")
            output = ""
        elif command == "btrfs" and args[1:3] == ("subvolume", "create"):
            for path in args[3:]:
                Path(path).mkdir(parents=True, exist_ok=True)
            output = ""
        elif command in ("mkfs.ext4", "mkfs.btrfs", "mkfs.vfat"):
            self.formatted.append(args)
            output = ""
        else:
            output = ""
        self._snapshot()
        result = subprocess.CompletedProcess(list(args), status, output, "")
        if check and status:
            raise i.Error(f"{' '.join(args)} failed ({status})")
        return result


class ExistingTargetTests(unittest.TestCase):
    def test_approved_layout_is_accepted_without_mutating_the_table(self):
        t = existing_table()
        before = replace(t)
        with patch.object(i.Plan, "make") as plan_make:
            target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        plan_make.assert_not_called()
        self.assertEqual(t, before)
        self.assertEqual(target.table, t)
        self.assertEqual((target.userdata, target.boot, target.root),
                         (part_named(t, "userdata"), part_named(t, "ARMADA_BOOT"), part_named(t, "ARMADA_ROOT")))
        self.assertEqual((target.boot.uuid, target.root.uuid), (BOOT_PARTUUID, ROOT_PARTUUID))
        self.assertEqual(target.boot.start, target.userdata.end)
        self.assertEqual(target.root.start, target.boot.end)
        self.assertEqual((target.userdata.end - target.userdata.start) * t.sector_size, 128 * i.GIB)
        self.assertEqual((target.boot.end - target.boot.start) * t.sector_size, i.GIB)
        self.assertGreaterEqual((target.root.end - target.root.start) * t.sector_size, i.ROOT_MIN)
        for name in ("vendor_a", "metadata"):
            self.assertIn(part_named(t, name), target.table.parts)
        self.assertIn("GUID:48,60,63", part_named(t, "metadata").attrs)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            target.boot = None

    def test_measured_tb322fc_layout_is_accepted_with_its_aligned_boot_gap(self):
        t = measured_table()
        before = replace(t)
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        self.assertEqual(t, before)
        self.assertEqual(t.last, 123871226)
        self.assertEqual(target.table, t)
        self.assertEqual((target.userdata.start, target.userdata.end), (5972392, 39526824))
        self.assertEqual((target.boot.start, target.boot.end), (39526912, 39789056))
        self.assertEqual((target.root.start, target.root.end), (39789056, 123871227))
        self.assertEqual((target.userdata.end - target.userdata.start) * t.sector_size, 128 * i.GIB)
        self.assertEqual((target.boot.end - target.boot.start) * t.sector_size, i.GIB)
        self.assertEqual(target.boot.start - target.userdata.end, 88)
        self.assertEqual(target.root.start, target.boot.end)
        self.assertGreaterEqual((target.root.end - target.root.start) * t.sector_size, i.ROOT_MIN)
        for name in ("vendor_a", "metadata"):
            self.assertIn(part_named(t, name), target.table.parts)

    def test_measured_fixture_rejects_misaligned_or_non_adjacent_layout(self):
        base = measured_table()
        for delta in (-89, -88, -1, 1, 256):
            with self.subTest(boot_shift=delta):
                shifted = shift_part(shift_part(base, "ARMADA_BOOT", delta), "ARMADA_ROOT", delta)
                self.assertEqual(part_named(shifted, "ARMADA_ROOT").start,
                                 part_named(shifted, "ARMADA_BOOT").end)
                self.assertEqual(part_named(shifted, "ARMADA_BOOT").end - part_named(shifted, "ARMADA_BOOT").start,
                                 part_named(base, "ARMADA_BOOT").end - part_named(base, "ARMADA_BOOT").start)
                with self.assertRaises(i.Error):
                    i.ExistingTarget.make(shifted, BOOT_PARTUUID, ROOT_PARTUUID)
        for delta in (-1, 1):
            with self.subTest(root_shift=delta), self.assertRaises(i.Error):
                i.ExistingTarget.make(shift_part(base, "ARMADA_ROOT", delta), BOOT_PARTUUID, ROOT_PARTUUID)

    def test_devices_sector_sizes_and_table_partition_numbers_are_used_as_given(self):
        for device in ("/dev/sda", "/dev/mmcblk0", "/dev/nvme0n1"):
            for sector in (512, 4096):
                with self.subTest(device=device, sector=sector):
                    t = existing_table(device=device, sector=sector)
                    target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
                    self.assertEqual(target.boot.start, target.userdata.end)
                    self.assertEqual(target.root.start, target.boot.end)
                    self.assertEqual(i.part_node(device, target.boot.number),
                                     i.part_node(device, part_named(t, "ARMADA_BOOT").number))
        renumbered = replace(existing_table(), parts=tuple(
            replace(p, number=42 if p.name == "ARMADA_BOOT" else 7 if p.name == "ARMADA_ROOT" else p.number)
            for p in existing_table().parts))
        target = i.ExistingTarget.make(renumbered, BOOT_PARTUUID, ROOT_PARTUUID)
        self.assertEqual((target.boot.number, target.root.number), (42, 7))

    def test_partuuid_selection_rejects_missing_invalid_duplicate_and_swapped(self):
        t = existing_table()
        for boot, root in ((None, ROOT_PARTUUID), ("", ROOT_PARTUUID), ("not-a-uuid", ROOT_PARTUUID),
                           (BOOT_PARTUUID, None), (BOOT_PARTUUID, ""), (BOOT_PARTUUID, "unknown-partuuid"),
                           (BOOT_PARTUUID, BOOT_PARTUUID), (ROOT_PARTUUID, BOOT_PARTUUID)):
            with self.subTest(boot=boot, root=root), self.assertRaises(i.Error):
                i.ExistingTarget.make(t, boot, root)

    def test_duplicate_special_labels_are_rejected(self):
        base = existing_table()
        for duplicate in ("ARMADA_BOOT", "ARMADA_ROOT", "userdata"):
            with self.subTest(duplicate=duplicate), self.assertRaises(i.Error):
                i.ExistingTarget.make(replace_part(base, "metadata", name=duplicate), BOOT_PARTUUID, ROOT_PARTUUID)

    def test_wrong_sizes_types_order_and_extra_tail_are_rejected(self):
        base = existing_table()
        userdata, boot, root = (part_named(base, name) for name in ("userdata", "ARMADA_BOOT", "ARMADA_ROOT"))
        small_root = replace_part(base, "ARMADA_ROOT", end=root.start + 31 * 1024 * i.MIB // base.sector_size)
        tail = existing_part(base.sector_size, 30, root.end * base.sector_size // i.MIB + 1, 512, "ESP", i.ESP_TYPE)
        broken = (replace_part(base, "userdata", end=userdata.end - 1),
                  replace_part(base, "ARMADA_BOOT", end=boot.end - 1),
                  small_root,
                  replace_part(base, "ARMADA_BOOT", type=i.ESP_TYPE),
                  replace_part(base, "ARMADA_ROOT", type=i.ESP_TYPE),
                  shift_part(base, "ARMADA_BOOT", i.MIB // base.sector_size),
                  shift_part(base, "ARMADA_ROOT", i.MIB // base.sector_size),
                  replace(base, parts=base.parts + (tail,)))
        for index, broken_table in enumerate(broken):
            with self.subTest(case=index), self.assertRaises(i.Error):
                i.ExistingTarget.make(broken_table, BOOT_PARTUUID, ROOT_PARTUUID)

    def test_verify_kernel_uses_512_byte_sectors_and_rejects_mismatch(self):
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                t = existing_table(sector=sector)
                target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
                parts = {i.part_node(t.device, p.number): p for p in (target.userdata, target.boot, target.root)}

                def read(path):
                    part = parts["/dev/" + path.parent.name]
                    return {"start": str(part.start * sector // 512),
                            "size": str((part.end - part.start) * sector // 512),
                            "dev": f"8:{part.number}"}[path.name]

                def stat(node):
                    return argparse.Namespace(st_rdev=os.makedev(8, parts[node].number))

                with patch.object(Path, "read_text", read), patch.object(i.os, "stat", side_effect=stat):
                    target.verify_kernel()
                    for attribute in ("start", "size", "dev"):
                        def bad(path, attribute=attribute):
                            return ("8:99" if attribute == "dev" else "1") if path.name == attribute else read(path)
                        with patch.object(Path, "read_text", bad), self.assertRaises(i.Error):
                            target.verify_kernel()
                with patch.object(Path, "read_text", side_effect=FileNotFoundError("missing")), \
                        patch.object(i.os, "stat", side_effect=stat), self.assertRaises(i.Error):
                    target.verify_kernel()

    def test_require_idle_rejects_mounted_swapped_or_held_writable_targets(self):
        t = existing_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        nodes = {"boot": i.part_node(t.device, target.boot.number),
                 "root": i.part_node(t.device, target.root.number)}
        numbers = {"boot": target.boot.number, "root": target.root.number}

        def stat(path, *args, **kwargs):
            for key, node in nodes.items():
                if str(path) == node:
                    return argparse.Namespace(st_rdev=os.makedev(8, numbers[key]), st_mode=statmod.S_IFBLK)
            raise OSError(f"unexpected stat: {path}")

        def reader(mountinfo="", swaps=""):
            def read(path, *args, **kwargs):
                return {"/proc/1/mountinfo": mountinfo, "/proc/self/mountinfo": mountinfo,
                        "/proc/swaps": "Filename Type Size Used Priority\n" + swaps}.get(str(path), "")
            return read

        with patch.object(Path, "read_text", reader()), patch.object(Path, "iterdir", return_value=iter(())), \
                patch.object(i.os, "stat", side_effect=stat):
            target.require_idle()
        mounted = f"1 0 8:{numbers['boot']} / /mnt rw - ext4 {nodes['boot']} rw\n"
        with patch.object(Path, "read_text", reader(mountinfo=mounted)), \
                patch.object(Path, "iterdir", return_value=iter(())), \
                patch.object(i.os, "stat", side_effect=stat), self.assertRaises(i.Error):
            target.require_idle()
        with patch.object(Path, "read_text", reader(swaps=f"{nodes['root']} file 0 0\n")), \
                patch.object(Path, "iterdir", return_value=iter(())), \
                patch.object(i.os, "stat", side_effect=stat), self.assertRaises(i.Error):
            target.require_idle()
        with patch.object(Path, "read_text", reader()), patch.object(Path, "iterdir", return_value=iter(["dm-0"])), \
                patch.object(i.os, "stat", side_effect=stat), self.assertRaises(i.Error):
            target.require_idle()


class IdleMountSourceTests(unittest.TestCase):
    """Btrfs reports an anonymous device in mountinfo field 3, not the partition.

    ``require_idle_partitions`` compares the partition ``major:minor`` against
    field 3, so a target really mounted through a Btrfs (or other multi-device)
    filesystem looks idle. These tests drive the real
    ``require_idle_partitions``/``ExistingTarget.require_idle`` with fake
    mountinfo, sysfs holders and block-device ``stat`` results: the mounted
    partition must be recognised from the mount source after the ``-``
    separator. Production and the fake sandbox stay untouched.
    """

    @staticmethod
    def _block(rdev):
        return argparse.Namespace(st_rdev=rdev, st_mode=statmod.S_IFBLK)

    @staticmethod
    def _regular(rdev):
        return argparse.Namespace(st_rdev=rdev, st_mode=statmod.S_IFREG)

    @staticmethod
    def _escaped(path):
        """Encode a mount source with the mountinfo specials in one pass."""
        return (path.replace("\\", "\\134").replace("\n", "\\012")
                .replace("\t", "\\011").replace(" ", "\\040"))

    @staticmethod
    def _line(field3, source, fstype="btrfs", optional=("shared:7", "master:1")):
        return f"36 1 {field3} / /mnt/target rw,relatime {' '.join(optional)} - {fstype} {source} rw\n"

    @contextmanager
    def _mounts(self, mountinfo=(), statmap=None):
        mountinfo = dict(mountinfo)
        statmap = dict(statmap or {})
        real_read_text, real_iterdir = Path.read_text, Path.iterdir

        def read_text(path, *args, **kwargs):
            name = str(path)
            if name in ("/proc/1/mountinfo", "/proc/self/mountinfo"):
                return mountinfo.get(name, "")
            if name == "/proc/swaps":
                return "Filename Type Size Used Priority\n"
            return real_read_text(path, *args, **kwargs)

        def iterdir(path):
            if "holders" in str(path):
                return iter(())
            return real_iterdir(path)

        def stat(path, *args, **kwargs):
            name = str(path)
            if name in statmap:
                value = statmap[name]
                if isinstance(value, BaseException):
                    raise value
                return value
            raise AssertionError(f"unexpected stat: {name}")

        with patch.object(Path, "read_text", read_text), \
                patch.object(Path, "iterdir", iterdir), \
                patch.object(i.os, "stat", stat):
            yield

    def _target(self):
        t = existing_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        return target, i.part_node(t.device, target.root.number), i.part_node(t.device, target.boot.number)

    def test_btrfs_anonymous_mount_source_blocks_target_in_either_namespace(self):
        target, root_node, boot_node = self._target()
        root_dev, boot_dev = os.makedev(259, 10), os.makedev(259, 11)
        self.assertNotEqual(root_dev, boot_dev)
        statmap = {root_node: self._block(root_dev), boot_node: self._block(boot_dev)}
        line = self._line("0:110", root_node)
        for namespace in ("/proc/1/mountinfo", "/proc/self/mountinfo"):
            with self.subTest(namespace=namespace):
                with self._mounts(mountinfo={namespace: line}, statmap=statmap):
                    with self.assertRaisesRegex(i.Error, "in use"):
                        target.require_idle()

    def test_btrfs_source_alias_and_octal_escapes_block_target(self):
        target, root_node, boot_node = self._target()
        root_dev, boot_dev = os.makedev(259, 10), os.makedev(259, 11)
        sources = (
            "/dev/disk/by-id/ata-ARMADA_ROOT",
            "/dev/disk/by-id/ata ARMADA_ROOT",
            "/dev/disk/by-id/ata\tARMADA_ROOT",
            "/dev/disk/by-id/ata\nARMADA_ROOT",
            "/dev/disk/by-id/ata\\ARMADA_ROOT",
            "/dev/disk/by-id/ata\\040ARMADA_ROOT",
        )
        for source in sources:
            with self.subTest(source=source):
                statmap = {root_node: self._block(root_dev), boot_node: self._block(boot_dev),
                           source: self._block(root_dev)}
                line = self._line("0:110", self._escaped(source))
                with self._mounts(mountinfo={"/proc/1/mountinfo": line}, statmap=statmap):
                    with self.assertRaisesRegex(i.Error, "in use"):
                        target.require_idle()

    def test_non_target_sources_leave_partitions_idle(self):
        target, root_node, boot_node = self._target()
        root_dev, boot_dev = os.makedev(259, 10), os.makedev(259, 11)
        base = {root_node: self._block(root_dev), boot_node: self._block(boot_dev)}
        cases = (
            ("missing", "/dev/disk/by-id/ata-MISSING", FileNotFoundError()),
            ("non-directory", "/dev/disk/by-id/ata-FILE/child", NotADirectoryError()),
            ("regular", "/dev/disk/by-id/ata-REGULAR", self._regular(root_dev)),
            ("unrelated-block", "/dev/disk/by-id/ata-OTHER", self._block(os.makedev(7, 0))),
            ("pseudo", "overlay", None),
        )
        for name, source, value in cases:
            with self.subTest(case=name):
                statmap = dict(base)
                if value is not None:
                    statmap[source] = value
                line = self._line("0:110", source, fstype="overlay" if name == "pseudo" else "btrfs")
                with self._mounts(mountinfo={"/proc/1/mountinfo": line}, statmap=statmap):
                    target.require_idle()

    def test_unreadable_mount_source_is_not_silently_treated_as_idle(self):
        target, root_node, boot_node = self._target()
        source = "/dev/disk/by-id/ata-UNREADABLE"
        statmap = {root_node: self._block(os.makedev(259, 10)),
                   boot_node: self._block(os.makedev(259, 11)),
                   source: PermissionError()}
        line = self._line("0:110", source)
        with self._mounts(mountinfo={"/proc/1/mountinfo": line}, statmap=statmap):
            with self.assertRaises((PermissionError, i.Error)):
                target.require_idle()


class PrepareExistingTests(unittest.TestCase):
    def test_execute_routes_prepare_existing_without_planning(self):
        args = prepare_args(expect="fingerprint")

        def old_flow(*_args, **_kwargs):
            raise AssertionError("execute entered the old install flow for prepare-existing")

        with patch.object(i.os, "geteuid", return_value=0), \
                patch.object(i, "discover_device", side_effect=old_flow), \
                patch.object(i, "prepare_existing") as prepare, \
                patch.object(i.Plan, "make") as plan_make:
            i.execute(args)
        prepare.assert_called_once_with(args)
        plan_make.assert_not_called()

    def test_cli_requires_boot_root_partuuids_and_fingerprint(self):
        base = ["--device", "/dev/sda", "prepare-existing"]
        pairs = [("--boot-partuuid", BOOT_PARTUUID), ("--root-partuuid", ROOT_PARTUUID),
                 ("--expect-table", "fingerprint")]
        for index in range(len(pairs)):
            argv = base + [token for position, pair in enumerate(pairs) if position != index for token in pair]
            with patch.object(i, "execute") as execute, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    i.main(argv)
            self.assertEqual(error.exception.code, 2)
            execute.assert_not_called()
        with patch.object(i, "execute") as execute:
            self.assertEqual(i.main(base + [token for pair in pairs for token in pair]), 0)
        parsed = execute.call_args.args[0]
        self.assertEqual((parsed.command, parsed.boot_partuuid, parsed.root_partuuid, parsed.expect_table),
                         ("prepare-existing", BOOT_PARTUUID, ROOT_PARTUUID, "fingerprint"))

    def test_stale_fingerprint_prevents_formatting(self):
        t = existing_table()
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, fingerprint="current")
            with sandbox, self.assertRaises(i.Error):
                i.prepare_existing(prepare_args(expect="stale"))
            self.assertFalse(any(command[0] in ("mount", "mkfs.ext4", "mkfs.btrfs") for command in sandbox.commands))

    def test_kernel_mismatch_prevents_formatting(self):
        t = existing_table()
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, mismatch_kernel=True)
            with sandbox, self.assertRaises(i.Error):
                i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
            self.assertFalse(any(command[0].startswith("mkfs") or command[0] == "mount"
                                 for command in sandbox.commands))

    def test_busy_target_prevents_formatting(self):
        t = existing_table()
        node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        mountinfo = f"1 0 8:{part_named(t, 'ARMADA_BOOT').number} / /mnt rw - ext4 {node} rw\n"
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, mountinfo=mountinfo)
            with sandbox, self.assertRaises(i.Error):
                i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
            self.assertFalse(any(command[0].startswith("mkfs") or command[0] == "mount"
                                 for command in sandbox.commands))

    def test_held_lock_prevents_formatting(self):
        t = existing_table()
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work)
            with sandbox, patch.object(i.fcntl, "flock", side_effect=BlockingIOError), self.assertRaises(i.Error):
                i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
            self.assertFalse(any(command[0].startswith("mkfs") for command in sandbox.commands))

    def test_prepared_filesystem_signatures_reject_before_mkfs(self):
        t = existing_table()
        boot_node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        root_node = i.part_node(t.device, part_named(t, "ARMADA_ROOT").number)
        uuids = {boot_node: BOOT_FS_UUID, root_node: ROOT_FS_UUID}
        for types, labels in (({root_node: "btrfs"}, {}), ({boot_node: "ext4"}, {boot_node: "boot"})):
            with self.subTest(types=types, labels=labels), tempfile.TemporaryDirectory() as work:
                sandbox = PrepareSandbox(t, work, uuids=uuids, types=types, labels=labels)
                with sandbox, self.assertRaises(i.Error):
                    i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
                self.assertFalse(any(command[0].startswith("mkfs") for command in sandbox.commands))
                self.assertFalse(any(SEED_DIRECTORY in " ".join(command) for command in sandbox.commands))

    def test_stale_android_signatures_do_not_prove_prepared_state(self):
        t = existing_table()
        boot_node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        root_node = i.part_node(t.device, part_named(t, "ARMADA_ROOT").number)
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, uuids={boot_node: BOOT_FS_UUID, root_node: ROOT_FS_UUID},
                                     types={boot_node: "ext4", root_node: "ext4"}, labels={boot_node: "Android-stale"})
            with sandbox:
                i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
            self.assertTrue(any(command[0] == "mkfs.ext4" for command in sandbox.commands))
            self.assertTrue(any(command[0] == "mkfs.btrfs" for command in sandbox.commands))

    def test_blkid_probe_errors_abort_before_formatting(self):
        t = existing_table()
        boot_node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        root_node = i.part_node(t.device, part_named(t, "ARMADA_ROOT").number)
        for status in (4, 8):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as work:
                sandbox = PrepareSandbox(t, work, blkid_status={boot_node: status, root_node: status})
                with sandbox, self.assertRaises(i.Error):
                    i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
                self.assertFalse(any(command[0].startswith("mkfs") or command[0] == "mount"
                                     for command in sandbox.commands))

    def test_successful_prepare_formats_only_the_two_nodes_and_records_state(self):
        t = existing_table()
        boot = part_named(t, "ARMADA_BOOT")
        root = part_named(t, "ARMADA_ROOT")
        boot_node, root_node = (i.part_node(t.device, part.number) for part in (boot, root))
        userdata_node = i.part_node(t.device, part_named(t, "userdata").number)
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, uuids={boot_node: BOOT_FS_UUID, root_node: ROOT_FS_UUID})
            with sandbox:
                result = i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
            self.assertEqual(sorted(command[0] for command in sandbox.formatted), ["mkfs.btrfs", "mkfs.ext4"])
            ext4 = next(command for command in sandbox.formatted if command[0] == "mkfs.ext4")
            btrfs = next(command for command in sandbox.formatted if command[0] == "mkfs.btrfs")
            self.assertEqual(ext4[-1], boot_node)
            self.assertIn("boot", ext4)
            self.assertEqual(btrfs[-1], root_node)
            self.assertIn("root", btrfs)
            subvolumes = {Path(path).name for command in sandbox.commands if command[:3] == ("btrfs", "subvolume", "create")
                          for path in command[3:]}
            self.assertEqual(subvolumes, {"root", "var", "home"})
            for banned in ("dd", "sgdisk", "partprobe", "mkfs.vfat", "ostree", "bootc", "systemd-inhibit", "cp",
                           "truncate", "rm", "shred"):
                self.assertFalse(any(command[0] == banned for command in sandbox.commands), banned)
            self.assertFalse(any(command[0] in ("dd", "sgdisk", "wipefs", "mkfs.ext4", "mkfs.btrfs", "mkfs.vfat")
                                 and userdata_node in command for command in sandbox.commands))
            self.assertFalse(any(SEED_DIRECTORY in " ".join(command) for command in sandbox.commands))
            record = sandbox.record
            self.assertIsNotNone(record, "prepared record was not written")
            self.assertEqual(record["table_fingerprint"], sandbox.fingerprint)
            self.assertEqual(record["sector_size"], t.sector_size)
            self.assertEqual(record["subvolumes"], ["root", "var", "home"])
            self.assertEqual(record["seed_directory"], SEED_DIRECTORY)
            for key, part, filesystem_uuid in (("boot", boot, BOOT_FS_UUID), ("root", root, ROOT_FS_UUID)):
                self.assertEqual(record[key]["partuuid"], part.uuid)
                self.assertEqual(record[key]["filesystem_uuid"], filesystem_uuid)
                self.assertEqual(record[key]["start"], part.start)
                self.assertEqual(record[key]["end"], part.end)
                self.assertEqual(record[key]["size_bytes"], (part.end - part.start) * t.sector_size)
            self.assertIsInstance(result, dict)
            self.assertEqual(comparable(result), comparable(record))
            printed = emitted_json(sandbox.stdout.getvalue())
            self.assertIsNotNone(printed, "prepared JSON result was not printed")
            self.assertEqual(comparable(printed), comparable(record))

    def test_failure_cleanup_unmounts_and_preserves_failed_mount(self):
        t = existing_table()
        boot_node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        root_node = i.part_node(t.device, part_named(t, "ARMADA_ROOT").number)
        for fail_umount in (False, True):
            with self.subTest(fail_umount=fail_umount), tempfile.TemporaryDirectory() as work:
                sandbox = PrepareSandbox(t, work, fail_umount=fail_umount,
                                         uuids={boot_node: BOOT_FS_UUID, root_node: ROOT_FS_UUID},
                                         fail_command=lambda command: command[:3] == ("btrfs", "subvolume", "create"))
                with sandbox, self.assertRaises(i.Error):
                    i.prepare_existing(prepare_args(expect=sandbox.fingerprint))
                self.assertTrue(any(command[0] == "umount" for command in sandbox.commands))
                if fail_umount:
                    self.assertTrue(sandbox.mounts)
                    self.assertTrue(all(path.exists() for path in sandbox.mounts))


class DeployExistingTests(unittest.TestCase):
    def assert_scoped_out(self, sandbox):
        """Pre-mount gates must not reach any mutating command."""
        for command in sandbox.commands:
            self.assertNotIn(command[0], ("sfdisk", "dd", "mkfs.ext4", "mkfs.btrfs", "mkfs.vfat",
                                          "partprobe", "mount", "umount", "systemd-inhibit"), command)
            self.assertNotEqual(command[0], i.BOOTIMG, command)

    def test_execute_routes_deploy_existing_without_planning_or_legacy_flow(self):
        args = deploy_args()
        with patch.object(i.os, "geteuid", return_value=0), \
                patch.object(i, "deploy_existing") as deploy, \
                patch.object(i, "discover_device", return_value="/dev/sda"), \
                patch.object(i, "inhibit_sleep") as inhibit, \
                patch.object(i, "locked_source") as locked, \
                patch.object(i.Plan, "make") as plan_make:
            i.execute(args)
        deploy.assert_called_once_with(args)
        inhibit.assert_not_called()
        locked.assert_not_called()
        plan_make.assert_not_called()

    def test_cli_requires_all_deploy_existing_arguments(self):
        base = ["--device", "/dev/sda", "deploy-existing"]
        pairs = [("--boot-partuuid", BOOT_PARTUUID), ("--root-partuuid", ROOT_PARTUUID),
                 ("--expect-table", "fingerprint"), ("--seed-commit", SEED_COMMIT),
                 ("--seed-manifest-digest", SEED_MANIFEST_DIGEST), ("--origin", SEED_ORIGIN)]
        for index in range(len(pairs)):
            argv = base + [token for position, pair in enumerate(pairs) if position != index for token in pair]
            with patch.object(i, "execute") as execute, patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    i.main(argv)
            self.assertEqual(error.exception.code, 2)
            execute.assert_not_called()
        with patch.object(i, "execute") as execute:
            self.assertEqual(i.main(base + [token for pair in pairs for token in pair]), 0)
        parsed = execute.call_args.args[0]
        self.assertEqual((parsed.command, parsed.boot_partuuid, parsed.root_partuuid, parsed.expect_table,
                          parsed.seed_commit, parsed.seed_manifest_digest, parsed.origin),
                         ("deploy-existing", BOOT_PARTUUID, ROOT_PARTUUID, "fingerprint",
                          SEED_COMMIT, SEED_MANIFEST_DIGEST, SEED_ORIGIN))

    def test_stale_fingerprint_prevents_any_target_write(self):
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(existing_table(), work, fingerprint="current", deploy=True)
            with sandbox, self.assertRaises(i.Error):
                i.deploy_existing(deploy_args(expect_table="stale"))
            self.assert_scoped_out(sandbox)

    def test_kernel_mismatch_prevents_any_target_write(self):
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(existing_table(), work, mismatch_kernel=True, deploy=True)
            with sandbox, self.assertRaises(i.Error):
                i.deploy_existing(deploy_args(expect_table=sandbox.fingerprint))
            self.assert_scoped_out(sandbox)

    def test_busy_target_prevents_any_target_write(self):
        t = existing_table()
        number = part_named(t, "ARMADA_BOOT").number
        mountinfo = f"1 0 8:{number} / /mnt rw - ext4 {i.part_node(t.device, number)} rw\n"
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(t, work, mountinfo=mountinfo, deploy=True)
            with sandbox, self.assertRaises(i.Error):
                i.deploy_existing(deploy_args(expect_table=sandbox.fingerprint))
            self.assert_scoped_out(sandbox)

    def test_held_lock_prevents_any_target_write(self):
        with tempfile.TemporaryDirectory() as work:
            sandbox = PrepareSandbox(existing_table(), work, deploy=True)
            with sandbox, patch.object(i.fcntl, "flock", side_effect=BlockingIOError), self.assertRaises(i.Error):
                i.deploy_existing(deploy_args(expect_table=sandbox.fingerprint))
            self.assert_scoped_out(sandbox)

    def test_unprepared_preexisting_filesystems_reject_before_mount_or_write(self):
        t = existing_table()
        boot_node = i.part_node(t.device, part_named(t, "ARMADA_BOOT").number)
        root_node = i.part_node(t.device, part_named(t, "ARMADA_ROOT").number)
        types = {boot_node: "ext4", root_node: "btrfs"}
        labels = {boot_node: "boot", root_node: "root"}
        uuids = {boot_node: BOOT_FS_UUID, root_node: ROOT_FS_UUID}
        broken = (
            ("boot-type", {"types": {**types, boot_node: "vfat"}}),
            ("root-type", {"types": {**types, root_node: "ext4"}}),
            ("boot-label", {"labels": {**labels, boot_node: "other"}}),
            ("root-label", {"labels": {**labels, root_node: "other"}}),
            ("probe-4", {"blkid_status": {boot_node: 4, root_node: 4}}),
            ("probe-8", {"blkid_status": {boot_node: 8, root_node: 8}}),
        )
        defaults = {"types": types, "labels": labels, "uuids": uuids}
        for case, options in broken:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as work:
                sandbox = PrepareSandbox(t, work, deploy=True, **{**defaults, **options})
                with sandbox, self.assertRaises(i.Error):
                    i.deploy_existing(deploy_args(expect_table=sandbox.fingerprint))
                self.assert_scoped_out(sandbox)


class ReadPreparedTests(unittest.TestCase):
    def read(self, top, target, uuids=None, fingerprint="fingerprint"):
        with subvolume_show():
            return i.read_prepared(top, target, uuids or {"boot": BOOT_FS_UUID, "root": ROOT_FS_UUID},
                                   fingerprint)

    def test_valid_receipt_is_accepted_and_left_byte_identical(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        record = prepared_record(t)
        with prepared_tree(record) as top:
            path = top / PREPARED_RECORD
            before = path.read_bytes()
            self.read(top, target)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(path.read_text()), record)

    def test_device_field_is_diagnostic_only(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        for device in ("/dev/other", None):
            with self.subTest(device=device), prepared_tree(prepared_record(t, device=device)) as top:
                self.read(top, target)
        record = prepared_record(t)
        record.pop("device")
        with prepared_tree(record) as top:
            self.read(top, target)

    def test_missing_or_malformed_receipt_is_rejected(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        with prepared_tree(prepared_record(t)) as top:
            (top / PREPARED_RECORD).unlink()
            with self.assertRaises(i.Error):
                self.read(top, target)
        for text in ("", "{", "[]", "null", "not json"):
            with self.subTest(text=text), prepared_tree(prepared_record(t)) as top:
                (top / PREPARED_RECORD).write_text(text)
                with self.assertRaises(i.Error):
                    self.read(top, target)

    def test_altered_receipt_fields_are_rejected(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        boot, root = part_named(t, "ARMADA_BOOT"), part_named(t, "ARMADA_ROOT")
        changes = (
            {"table_fingerprint": "other"},
            {"sector_size": 512},
            {"subvolumes": ["root", "var"]},
            {"subvolumes": ["root", "var", "home", "opt"]},
            {"subvolumes": ["root", "home", "var"]},
            {"seed_directory": "/elsewhere"},
            {"boot": {"partuuid": str(uuid.uuid4())}},
            {"root": {"partuuid": str(uuid.uuid4())}},
            {"boot": {"filesystem_uuid": str(uuid.uuid4())}},
            {"root": {"filesystem_uuid": str(uuid.uuid4())}},
            {"boot": {"start": boot.start + 1}},
            {"boot": {"end": boot.end + 1}},
            {"root": {"start": root.start + 1}},
            {"root": {"end": root.end - 1}},
        )
        for change in changes:
            with self.subTest(change=change), prepared_tree(prepared_record(t, **change)) as top:
                with self.assertRaises(i.Error):
                    self.read(top, target)

    def test_populated_root_subvolume_is_rejected(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        with prepared_tree(prepared_record(t), populate_root=True) as top:
            with self.assertRaises(i.Error):
                self.read(top, target)

    def test_symlinked_root_subvolume_is_rejected(self):
        t = measured_table()
        target = i.ExistingTarget.make(t, BOOT_PARTUUID, ROOT_PARTUUID)
        with prepared_tree(prepared_record(t), root_link=True) as top:
            with self.assertRaises(i.Error):
                self.read(top, target)


if __name__ == "__main__":
    unittest.main()
