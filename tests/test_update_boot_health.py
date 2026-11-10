# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "system_files/usr/lib/armada"))

from updates import boot_context, boot_health as health, boot_payload, trial_record as rec
import test_update_boot_payload as payload_tests
from test_update_boot_payload import KVER


class UpdateBootHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload_tests.UpdateBootPayloadTests.setUpClass()
        cls.addClassCleanup(payload_tests.UpdateBootPayloadTests.tearDownClass)

    def setUp(self) -> None:
        self.fixture = payload_tests.UpdateBootPayloadTests("test_roundtrip_and_zero_writes")
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture._make_root()
        self.model = self.fixture.models[0]
        evidence = self.root.parent / "evidence"
        evidence.mkdir()
        self.marker = evidence / "marker"
        self.witness = evidence / "witness"
        self.cmdline = evidence / "cmdline"
        self.compatible = evidence / "compatible"
        required = self.fixture.cfg["profiles"][self.model]["required"]
        self.compatible.write_bytes(b"\0".join(x.encode() for x in required) + b"\0")

    def record(self, **changes):
        fields = dict(
            partition_uuid=bytes(range(16)), installation_uuid=bytes(range(16, 32)),
            transaction_uuid=bytes(range(32, 48)), sequence=1, state=rec.TRIAL,
            from_slot=1, target_slot=0, remaining=2, attempt_limit=3,
            old_serial=6, new_serial=7, os_name="armada", old_commit=b"a" * 32,
            new_commit=b"b" * 32, old_boot_sha256=b"c" * 32,
            new_boot_sha256=b"d" * 32, plan_sha256=b"e" * 32,
        )
        fields.update(changes)
        return rec.TrialRecord(**fields)

    def live(self, record, slot=0):
        commit = record.new_commit.hex()
        booted = {"stateroot": record.os_name, "checksum": commit,
                  "serial": record.new_serial}
        self.marker.write_bytes(record.transaction_uuid)
        self.witness.write_bytes(struct.pack(">4I", 1, 1, slot, 0))
        self.cmdline.write_text(
            f"quiet armada.deployment={record.os_name}/{commit}.{record.new_serial} "
            f"ostree=/ostree/boot.{1 - slot}/{record.os_name}/{'f' * 64}/0",
            encoding="utf-8",
        )
        return booted

    def install_manifest(self, root=None):
        root = self.root if root is None else root
        manifest = boot_payload.create_manifest(root)
        (root / health.MANIFEST_RELPATH).write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def snapshot(self):
        return {p: (p.stat().st_mtime_ns, p.stat().st_size)
                for p in self.root.rglob("*") if p.is_file()}

    def test_collect_real_evidence_and_payload_for_both_slots(self):
        self.install_manifest()
        for slot, state in ((0, rec.TRIAL), (1, rec.CONFIRMED)):
            with self.subTest(slot=slot, state=state):
                record = self.record(target_slot=slot, from_slot=1 - slot, state=state)
                booted = self.live(record, slot)
                encoded = rec.encode_record(record)
                before = self.snapshot()
                real_read = boot_context.read_boot_context

                def read_context(value):
                    return real_read(value, self.witness, self.cmdline)

                with mock.patch.object(health, "ROOT_PATH", self.root), \
                     mock.patch.object(health, "TRANSACTION_PATH", self.marker), \
                     mock.patch.object(health, "COMPATIBLE_PATH", self.compatible), \
                     mock.patch.object(health.armada_os, "snapshot", return_value={"booted": booted}), \
                     mock.patch.object(health.boot_context, "read_boot_context", side_effect=read_context), \
                     mock.patch.object(health.os, "uname", return_value=SimpleNamespace(release=KVER)), \
                     mock.patch.object(health.recovery_device, "locate_recovery_partition",
                                       return_value=(Path("recovery"), "uuid", 1, 99)), \
                     mock.patch.object(health, "_locate_boot_partition",
                                       return_value=(Path("boot"), 100)) as locate, \
                     mock.patch.object(health, "_hash_boot_partition",
                                       return_value=record.new_boot_sha256) as digest:
                    result = health.collect_boot_evidence(record)
                self.assertEqual(result.actual_slot, slot)
                self.assertEqual(result.kernel_release, KVER)
                self.assertEqual(result.boot_sha256, record.new_boot_sha256)
                self.assertEqual(rec.encode_record(record), encoded)
                self.assertEqual(self.snapshot(), before)
                locate.assert_called_once_with("ab"[slot], 99, mock.ANY)
                digest.assert_called_once_with(Path("boot"), 100, mock.ANY)

    def test_early_record_and_context_rejections_skip_payload_and_devices(self):
        base = self.record()
        context = {"actualSlot": "a", "deployment":
                   f"armada/{base.new_commit.hex()}.{base.new_serial}"}
        cases = [
            (replace(base, state=rec.PREPARED, remaining=3), context, base.transaction_uuid),
            (replace(base, state=rec.ROLLED_BACK), context, base.transaction_uuid),
            (base, dict(context, actualSlot="b"), base.transaction_uuid),
            (base, dict(context, deployment=context["deployment"].replace(".7", ".8")),
             base.transaction_uuid),
            (base, dict(context, deployment=f"armada/{'a' * 64}.7"), base.transaction_uuid),
            (base, context, b"x" * 16),
        ]
        for record, ctx, marker in cases:
            with self.subTest(state=record.state, context=ctx, marker=marker), \
                 mock.patch.object(health, "_read_live_evidence",
                                   return_value=({}, ctx, marker)), \
                 mock.patch.object(health, "_verify_boot_payload") as payload, \
                 mock.patch.object(health.recovery_device, "locate_recovery_partition") as recovery, \
                 mock.patch.object(health, "_locate_boot_partition") as locate, \
                 mock.patch.object(health, "_hash_boot_partition") as digest:
                with self.assertRaises(health.BootHealthError):
                    health.collect_boot_evidence(record)
                payload.assert_not_called()
                recovery.assert_not_called()
                locate.assert_not_called()
                digest.assert_not_called()

    def test_payload_hash_and_changed_evidence_rejections(self):
        with mock.patch.object(health, "COMPATIBLE_PATH", self.compatible):
            with self.subTest(case="kernel"), mock.patch.object(
                    health.os, "uname", return_value=SimpleNamespace(release="wrong")):
                self.install_manifest()
                with self.assertRaises(health.BootHealthError):
                    health._verify_boot_payload(self.root)
            for name in ("usr/lib/firmware/test/fw.bin",
                         f"usr/lib/modules/{KVER}/modules.builtin"):
                with self.subTest(case=name):
                    root = self.fixture._make_root()
                    self.install_manifest(root)
                    (root / name).unlink()
                    with self.assertRaises((ValueError, OSError, health.BootHealthError)):
                        health._verify_boot_payload(root)
            with self.subTest(case="manifest"):
                (self.root / health.MANIFEST_RELPATH).write_text("{", encoding="utf-8")
                with self.assertRaises(health.BootHealthError):
                    health._verify_boot_payload(self.root)
        record = self.record()
        ctx = {"actualSlot": "a", "deployment": f"armada/{record.new_commit.hex()}.7"}
        common = [mock.patch.object(health, "_verify_boot_payload", return_value=(KVER, 4096)),
                  mock.patch.object(health.recovery_device, "locate_recovery_partition",
                                    return_value=(Path("r"), "u", 1, 1)),
                  mock.patch.object(health, "_locate_boot_partition", return_value=(Path("b"), 2))]
        for changed in (False, True):
            with self.subTest(changed=changed):
                samples = [({}, ctx, record.transaction_uuid),
                           ({"changed": True} if changed else {}, ctx, record.transaction_uuid)]
                digest = b"z" * 32 if not changed else record.new_boot_sha256
                with mock.patch.object(health, "_read_live_evidence", side_effect=samples), \
                     mock.patch.object(health, "_hash_boot_partition", return_value=digest), \
                     common[0], common[1], common[2]:
                    with self.assertRaises(health.BootHealthError):
                        health.collect_boot_evidence(record)

    def test_hash_partition_partial_reads_exact_digest_and_readonly_close(self):
        data = bytes(range(256)) * ((2 * 1024 * 1024 + 4096) // 256)
        calls = []

        def pread(fd, size, offset):
            calls.append(size)
            amount = min(size, 777)
            return data[offset:offset + amount]

        with mock.patch.object(health.os, "open", return_value=9) as op, \
             mock.patch.object(health.os, "fstat", return_value=SimpleNamespace(
                 st_mode=stat.S_IFBLK, st_rdev=55)), \
             mock.patch.object(health.fcntl, "ioctl", return_value=struct.pack("=Q", len(data))), \
             mock.patch.object(health.os, "pread", side_effect=pread), \
             mock.patch.object(health.os, "close") as close:
            got = health._hash_boot_partition(Path("/dev/fake"), 55, len(data))
        self.assertEqual(got, hashlib.sha256(data).digest())
        self.assertEqual(op.call_args.args[1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.assertLessEqual(max(calls), 1024 * 1024)
        close.assert_called_once_with(9)

    def test_hash_partition_rejects_device_size_and_eof_and_always_closes(self):
        for case in ("nonblock", "rdev", "size", "eof"):
            with self.subTest(case=case):
                mode = stat.S_IFREG if case == "nonblock" else stat.S_IFBLK
                rdev = 56 if case == "rdev" else 55
                size = 8192 if case == "size" else 4096
                with mock.patch.object(health.os, "open", return_value=9), \
                     mock.patch.object(health.os, "fstat", return_value=SimpleNamespace(
                         st_mode=mode, st_rdev=rdev)), \
                     mock.patch.object(health.fcntl, "ioctl", return_value=struct.pack("=Q", size)), \
                     mock.patch.object(health.os, "pread", return_value=b""), \
                     mock.patch.object(health.os, "close") as close:
                    with self.assertRaises(health.BootHealthError):
                        health._hash_boot_partition(Path("/dev/fake"), 55, 4096)
                    close.assert_called_once_with(9)

    def test_locate_partition_sysfs_aliases_filters_and_validation(self):
        tmp = self.root.parent / "sysfs"
        sysdev = tmp / "sys/dev/block"
        sysdev.mkdir(parents=True)
        devices = tmp / "dev"
        devices.mkdir()
        infos, labels = {}, {}
        original_stat = Path.stat

        def node(controller, name, major, minor):
            value = tmp / "sys" / controller / "host" / "sda" / name
            value.mkdir(parents=True)
            (value / "partition").write_text("1", encoding="utf-8")
            (value / "size").write_text("8", encoding="utf-8")
            (value.parent / "queue").mkdir(exist_ok=True)
            (value.parent / "queue/logical_block_size").write_text("4096", encoding="utf-8")
            rdev = os.makedev(major, minor)
            link = sysdev / f"{major}:{minor}"
            link.symlink_to(value)
            return value, rdev

        def dev(name, rdev, mode=stat.S_IFBLK):
            path = devices / name
            path.touch()
            infos[str(path.resolve())] = SimpleNamespace(st_mode=mode, st_rdev=rdev)
            labels[str(path.resolve())] = "boot_a"
            return path

        recovery_node, recovery_rdev = node("controller1.ufs", "sda9", 8, 0)
        boot_node, boot_rdev = node("controller1.ufs", "sda1", 8, 1)
        first, alias = dev("sda1", boot_rdev), dev("alias", boot_rdev)
        _, other_rdev = node("controller2.ufs", "sdb1", 8, 2)
        other = dev("sdb1", other_rdev)
        nonufs = tmp / "sys/pci/sda/sda1"
        nonufs.mkdir(parents=True)
        nonufs_rdev = os.makedev(8, 3)
        (sysdev / "8:3").symlink_to(nonufs)
        external = dev("external", nonufs_rdev)
        names = [str(first), str(alias), str(other), str(external)]

        def runner(cmd, **kwargs):
            self.assertEqual(kwargs["timeout"], health.BLKID_TIMEOUT_SECONDS)
            self.assertTrue(kwargs["capture_output"] and kwargs["text"] and kwargs["check"])
            out = "\n".join(names) if "-t" in cmd else labels[str(Path(cmd[-1]).resolve())]
            return SimpleNamespace(stdout=out)

        def stat_shim(path, *args, **kwargs):
            return infos.get(str(path), original_stat(path, *args, **kwargs))

        patches = (mock.patch.object(health, "SYS_DEV_BLOCK", sysdev),
                   mock.patch.object(Path, "stat", new=stat_shim),
                   mock.patch.object(health.subprocess, "run", side_effect=runner))
        with patches[0], patches[1], patches[2] as run:
            self.assertEqual(health._locate_boot_partition("a", recovery_rdev, 4096),
                             (first.resolve(), boot_rdev))
            self.assertEqual(run.call_args_list[0].args[0][:4],
                             ["blkid", "-t", "PARTLABEL=boot_a", "-o"])
            second_node, second_rdev = node("controller1.ufs", "sda2", 8, 4)
            names.append(str(dev("sda2", second_rdev)))
            with self.assertRaises(health.BootHealthError):
                health._locate_boot_partition("a", recovery_rdev, 4096)
            names.pop()
            for case in ("size", "logical", "label", "nonblock"):
                with self.subTest(case=case):
                    if case == "size":
                        (boot_node / "size").write_text("9", encoding="utf-8")
                    elif case == "logical":
                        (boot_node.parent / "queue/logical_block_size").write_text("1024", encoding="utf-8")
                    elif case == "label":
                        labels[str(first.resolve())] = labels[str(alias.resolve())] = "wrong"
                    else:
                        infos[str(first.resolve())].st_mode = stat.S_IFREG
                    with self.assertRaises(health.BootHealthError):
                        health._locate_boot_partition("a", recovery_rdev, 4096)
                    if case == "size":
                        (boot_node / "size").write_text("8", encoding="utf-8")
                    elif case == "logical":
                        (boot_node.parent / "queue/logical_block_size").write_text("4096", encoding="utf-8")
                    elif case == "label":
                        labels[str(first.resolve())] = labels[str(alias.resolve())] = "boot_a"
                    else:
                        infos[str(first.resolve())].st_mode = stat.S_IFBLK

    def test_ufs_controller_requires_exact_unique_path_component(self):
        base = self.root.parent / "ufs"
        node = base / "controller1.ufs/host/sda/sda1"
        node.mkdir(parents=True)
        self.assertEqual(health._ufs_controller(node), base / "controller1.ufs")
        plain = base / "plain/sda1"
        plain.mkdir(parents=True)
        multiple = base / "one.ufs/two.ufs/sda1"
        multiple.mkdir(parents=True)
        for path in (plain, multiple):
            with self.subTest(path=path), self.assertRaises(health.BootHealthError):
                health._ufs_controller(path)

    def test_select_model_rejects_unknown_ambiguous_forbidden_and_bad_rules(self):
        one = {"profiles": {"m": {"required": ["soc"], "forbidden": ["bad"]}}}
        self.assertEqual(health._select_model(one, {"soc", "board"}), "m")
        ambiguous = {"profiles": {"m": one["profiles"]["m"],
                                  "n": {"required": ["soc"], "forbidden": []}}}
        malformed = {"profiles": {"m": {"required": [], "forbidden": []}}}
        for config, compatible in ((one, {"other"}), (one, {"soc", "bad"}),
                                   (ambiguous, {"soc"}), (malformed, {"soc"})):
            with self.subTest(config=config, compatible=compatible), \
                 self.assertRaises(health.BootHealthError):
                health._select_model(config, compatible)


if __name__ == "__main__":
    unittest.main()
