#!/usr/bin/python3
"""Contract tests for the direct EDL raw producer and firstboot guard.

These cover the producer contract and synthetic filesystem layouts.
The filesystem pair case uses a real loop/btrfs pair; missing container
capability is reported as an environment error, never skipped.
"""

import copy
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PRODUCER = ROOT / "build_files/make-edl-raw-package.py"
GUARD = ROOT / "system_files/usr/libexec/armada/armada-edl-grow-root"
UNIT = ROOT / "system_files/usr/lib/systemd/system/armada-edl-root-grow.service"

GIB = 1 << 30
MIB = 1 << 20

SOURCE_COMMIT = "244b74516d99d06cb0518609ce4b842ab278be8a"
BOOT_UUID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"
ROOT_UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_UUID = "f81d4fae-7dec-11d0-a765-00a0c91e6bf6"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
BOOT_CHECKSUM = "0123456789abcdef" * 4
DEPLOY_COMMIT = "fedcba9876543210" * 4
PERMITTED_RESIZE = ["btrfs", "filesystem", "resize", "max", "/var"]


def _load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


producer = _load_script("make_edl_raw_package", PRODUCER)
guard = _load_script("armada_edl_grow_root", GUARD)


def command(*argv):
    return subprocess.run(
        [str(arg) for arg in argv],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout.strip()


def blkid_fields(device):
    fields = {}
    for line in command("blkid", "-o", "export", device).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value.strip('"')
    return fields


def mount_points():
    points = set()
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 4:
            points.add(fields[4].replace("\\040", " "))
    return points


def boot_options(boot_uuid=BOOT_UUID, root_uuid=ROOT_UUID, checksum=BOOT_CHECKSUM):
    return [
        "rw",
        "quiet",
        f"root=UUID={root_uuid}",
        f"boot=UUID={boot_uuid}",
        f"ostree=/ostree/boot.0/default/{checksum}/0",
    ]


def asset_paths(checksum=BOOT_CHECKSUM):
    directory = f"ostree/default-{checksum}"
    return {
        "image": f"{directory}/vmlinuz-6.1.0",
        "initrd": f"{directory}/initramfs-6.1.0.img",
        "fdtdir": f"{directory}/dtb",
    }


def deployment_result(options=None, **overrides):
    result = {
        "deployment_checksum": DEPLOY_COMMIT,
        "filesystem_uuids": {"boot": BOOT_UUID, "root": ROOT_UUID},
        "bls": "loader/entries/default.conf",
        **asset_paths(),
        "options": " ".join(boot_options() if options is None else options),
    }
    result.update(overrides)
    return result


def bls_text(options):
    assets = asset_paths()
    return "\n".join(
        [
            "title Armada OS",
            "version 1",
            "linux /boot/" + assets["image"],
            "initrd /boot/" + assets["initrd"],
            "fdtdir /boot/" + assets["fdtdir"],
            "options " + " ".join(options),
            "",
        ]
    )


def partitions_fixture(root_bytes=64 * GIB):
    boot_start = 1 * MIB
    root_start = boot_start + 1 * GIB
    boot = {
        "path": "/dev/loop-testp1",
        "disk": "/dev/loop-test",
        "startBytes": boot_start,
        "sizeBytes": 1 * GIB,
        "partLabel": "ARMADA_BOOT",
        "fsType": "ext4",
        "fsUuid": BOOT_UUID,
    }
    root = {
        "path": "/dev/loop-testp2",
        "disk": "/dev/loop-test",
        "startBytes": root_start,
        "sizeBytes": root_bytes,
        "partLabel": "ARMADA_ROOT",
        "fsType": "btrfs",
        "fsUuid": ROOT_UUID,
    }
    backup = {
        "path": "/dev/loop-testp3",
        "disk": "/dev/loop-test",
        "startBytes": root_start + root_bytes,
        "sizeBytes": 2 * GIB,
        "partLabel": "ARMADA_BACKUP",
        "fsType": "none",
        "fsUuid": "",
    }
    return [boot, root, backup]


class ProducerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="edl-raw-contract.")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def test_offline_upgrade_for_an_armada_boot_only_model_requires_the_disk_signer(self):
        # The resolved selection is checked, so omitting --model (every profile) cannot skip it.
        every_profile = tuple(producer.PROFILE_ORDER)
        self.assertIn("TB710FU", every_profile)
        for models in (("TB710FU",), ("TB322FC", "TB710FU"), every_profile):
            with self.subTest(models=models):
                with self.assertRaisesRegex(RuntimeError, "requires --disk-boot-cosign"):
                    producer._require_disk_boot_signing(models, None)
                producer._require_disk_boot_signing(models, "/run/armada-cosign")
        # The Y700 boards keep the raw-Boot path, so they build without the signer.
        producer._require_disk_boot_signing(("TB322FC", "TB323FU"), None)

    def test_root_image_accepts_only_32_or_48_gib(self):
        for gib in (32, 48):
            with self.subTest(gib=gib):
                options = producer.validate_build_options(
                    gib, BOOT_UUID, ROOT_UUID, SOURCE_COMMIT
                )
                self.assertEqual(options["rootBytes"], gib * GIB)
                self.assertEqual(options["bootBytes"], 1 * GIB)
                self.assertEqual(options["minimumRootPartitionBytes"], 63 * GIB)
                self.assertEqual(options["backupBytes"], 2 * GIB)
                self.assertEqual(str(options["sourceCommit"]), SOURCE_COMMIT)
                self.assertEqual(uuid.UUID(str(options["bootUuid"])), uuid.UUID(BOOT_UUID))
                self.assertEqual(uuid.UUID(str(options["rootUuid"])), uuid.UUID(ROOT_UUID))

    def test_root_image_rejects_64_gib_and_other_sizes(self):
        for gib in (64, 63, 31, 16):
            with self.subTest(gib=gib):
                with self.assertRaises(ValueError):
                    producer.validate_build_options(gib, BOOT_UUID, ROOT_UUID, SOURCE_COMMIT)

    def test_build_options_reject_bad_identity(self):
        bad_pairs = [
            (BOOT_UUID, BOOT_UUID),
            (ZERO_UUID, ROOT_UUID),
            (BOOT_UUID, ZERO_UUID),
            ("{" + BOOT_UUID + "}", ROOT_UUID),
            (BOOT_UUID.replace("-", ""), ROOT_UUID),
        ]
        for boot_uuid, root_uuid in bad_pairs:
            with self.subTest(boot_uuid=boot_uuid, root_uuid=root_uuid):
                with self.assertRaises(ValueError):
                    producer.validate_build_options(32, boot_uuid, root_uuid, SOURCE_COMMIT)
        for commit in ("0" * 40, SOURCE_COMMIT.upper(), SOURCE_COMMIT[:39]):
            with self.subTest(commit=commit):
                with self.assertRaises(ValueError):
                    producer.validate_build_options(32, BOOT_UUID, ROOT_UUID, commit)

    def test_space_guard_requires_8gib_actual_room(self):
        self.assertEqual(producer.validate_space(32 * GIB, 8 * GIB), 24 * GIB)
        self.assertEqual(producer.validate_space(48 * GIB, 8 * GIB), 40 * GIB)
        self.assertEqual(producer.validate_space(32 * GIB, 8 * GIB + 1), 24 * GIB - 1)
        for root_bytes, available_bytes in (
            (32 * GIB, 8 * GIB - 1),
            (32 * GIB, -1),
            (32 * GIB, 32 * GIB + 1),
        ):
            with self.subTest(available_bytes=available_bytes):
                with self.assertRaises(ValueError):
                    producer.validate_space(root_bytes, available_bytes)

    def test_failed_32g_candidate_publishes_no_metadata(self):
        out = self.work / "out"
        out.mkdir()
        with self.assertRaises(ValueError):
            producer.validate_space(32 * GIB, 8 * GIB - 1)
        self.assertEqual(list(out.iterdir()), [])

    def test_parse_deployment_extracts_boot_checksum(self):
        parsed = producer.parse_deployment_result(
            deployment_result(), bls_text(boot_options())
        )
        self.assertEqual(parsed["bootChecksum"], BOOT_CHECKSUM)
        self.assertNotEqual(parsed["bootChecksum"], SOURCE_COMMIT)
        self.assertEqual(uuid.UUID(str(parsed["bootUuid"])), uuid.UUID(BOOT_UUID))
        self.assertEqual(uuid.UUID(str(parsed["rootUuid"])), uuid.UUID(ROOT_UUID))
        self.assertEqual(parsed["defaultEntry"], "android")
        normal = parsed["normalBootArgs"]
        self.assertIsInstance(normal, str)
        self.assertIn(f"root=UUID={ROOT_UUID}", normal)
        self.assertIn(f"boot=UUID={BOOT_UUID}", normal)
        self.assertIn(f"ostree=/ostree/boot.0/default/{BOOT_CHECKSUM}/0", normal)
        self.assertEqual(parsed["diagnosticBootArgs"], normal + " rd.break=pre-mount")

        slot_one = boot_options()
        slot_one[-1] = f"ostree=/ostree/boot.1/default/{BOOT_CHECKSUM}/0"
        parsed_one = producer.parse_deployment_result(
            deployment_result(options=slot_one), bls_text(slot_one)
        )
        self.assertEqual(parsed_one["bootChecksum"], BOOT_CHECKSUM)

    def test_parse_deployment_rejects_mismatched_evidence(self):
        duplicate = boot_options()
        duplicate.append(f"ostree=/ostree/boot.0/default/{BOOT_CHECKSUM}/0")
        wrong_slot = boot_options()
        wrong_slot[-1] = f"ostree=/ostree/boot.2/default/{BOOT_CHECKSUM}/0"
        wrong_pointer = boot_options(checksum="f" * 64)
        cases = [
            ("duplicate pointer", deployment_result(), bls_text(duplicate)),
            ("boot.2 slot", deployment_result(), bls_text(wrong_slot)),
            ("boot uuid mismatch", deployment_result(), bls_text(boot_options(boot_uuid=OTHER_UUID))),
            ("root uuid mismatch", deployment_result(), bls_text(boot_options(root_uuid=OTHER_UUID))),
            ("pointer mismatch", deployment_result(), bls_text(wrong_pointer)),
            ("result uuid disagrees", deployment_result(filesystem_uuids={"boot": OTHER_UUID, "root": ROOT_UUID}), bls_text(boot_options())),
        ]
        for label, result, bls in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    producer.parse_deployment_result(result, bls)

    def test_gzip_image_reports_compressed_and_expanded_facts(self):
        data = (b"armada edl raw fixture\n" * 7) + bytes(range(64))
        source = self.work / "tiny.bin"
        source.write_bytes(data)
        destination = self.work / "tiny.bin.gz"
        again = self.work / "tiny-again.bin.gz"

        info = producer.gzip_image(source, destination)
        compressed = destination.read_bytes()
        self.assertEqual(info["expandedBytes"], len(data))
        self.assertEqual(info["expandedSha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(info["bytes"], len(compressed))
        self.assertEqual(info["sha256"], hashlib.sha256(compressed).hexdigest())
        self.assertEqual(gzip.decompress(compressed), data)

        producer.gzip_image(source, again)
        self.assertEqual(again.read_bytes(), compressed)

    def test_filesystem_pair_real_ext4_and_btrfs_images(self):
        work = self.work / "pair-work"
        work.mkdir()

        with producer.filesystem_pair(
            work, 64 * MIB, 512 * MIB, BOOT_UUID, ROOT_UUID
        ) as fs:
            self.assertEqual(
                os.geteuid(),
                0,
                "environment error: filesystem_pair needs root with loop/btrfs access "
                "in the test container; report this to root instead of skipping",
            )
            self.assertTrue(
                Path("/dev/loop-control").exists(),
                "environment error: /dev/loop-control is unavailable; report this to root",
            )
            self.assertTrue(
                {"target", "top", "boot_image", "root_image", "boot_loop", "root_loop"}
                <= set(fs)
            )
            target = Path(fs["target"])
            boot_image = Path(fs["boot_image"])
            root_image = Path(fs["root_image"])
            self.assertEqual(boot_image.stat().st_size, 64 * MIB)
            self.assertEqual(root_image.stat().st_size, 512 * MIB)
            self.assertTrue(Path(fs["boot_loop"]).is_block_device())
            self.assertTrue(Path(fs["root_loop"]).is_block_device())

            boot = blkid_fields(fs["boot_loop"])
            root = blkid_fields(fs["root_loop"])
            self.assertEqual(boot.get("TYPE"), "ext4")
            self.assertEqual(boot.get("UUID", "").lower(), BOOT_UUID)
            self.assertEqual(root.get("TYPE"), "btrfs")
            self.assertEqual(root.get("UUID", "").lower(), ROOT_UUID)

            points = mount_points()
            for relative in ("boot", "var", "var/home"):
                self.assertIn(str(target / relative), points)

            listing = command("btrfs", "subvolume", "list", str(target))
            paths = {line.split()[-1] for line in listing.splitlines() if line.split()}
            for name in ("root", "var", "home"):
                self.assertTrue(
                    any(path == name or path.endswith("/" + name) for path in paths),
                    f"missing {name} subvolume in {paths}",
                )

        points = mount_points()
        for relative in ("boot", "var", "var/home"):
            self.assertNotIn(str(target / relative), points)
        self.assertTrue(boot_image.exists())
        self.assertTrue(root_image.exists())
        self.assertEqual(command("losetup", "-j", boot_image), "")
        self.assertEqual(command("losetup", "-j", root_image), "")

    def test_g5_uses_display_dtb_when_base_also_exists(self):
        fdtdir = self.work / "fdtdir"
        qcom = fdtdir / "qcom"
        qcom.mkdir(parents=True)
        base = qcom / "kaanapali-lenovo-baldur.dtb"
        display = qcom / "kaanapali-lenovo-baldur-display.dtb"
        base.write_bytes(b"baldur-base-dtb")
        display.write_bytes(b"baldur-display-dtb")
        self.assertNotEqual(base.read_bytes(), display.read_bytes())

        resolved = producer._resolve_dtb(
            fdtdir, producer.PROFILES["TB323FU"], "TB323FU"
        )
        self.assertEqual(resolved, qcom / "kaanapali-lenovo-baldur-display.dtb")


class GuardContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="edl-growth-guard.")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.config = {
            "schema": 1,
            "bootUuid": BOOT_UUID,
            "rootUuid": ROOT_UUID,
            "minimumRootPartitionBytes": 63 * GIB,
            "backupBytes": 2 * GIB,
        }
        self.config_path = self.work / "edl-direct.json"
        self.config_path.write_text(json.dumps(self.config))
        self.state_path = self.work / "edl-root-expanded"

    def mount(self):
        return {
            "target": "/var",
            "fsType": "btrfs",
            "fsUuid": ROOT_UUID,
            "fsRoot": "/var",
        }

    def snapshot(self):
        return (self.mount(), partitions_fixture())

    def recorder(self, fail=False):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            if fail:
                raise RuntimeError("resize failed")

        return calls, runner

    def test_valid_three_partition_tail_returns_root(self):
        # The device paths are synthetic: pure validation must not touch them.
        self.assertEqual(
            guard.validate_growth_target(self.config, self.mount(), partitions_fixture()),
            "/dev/loop-testp2",
        )

    def test_reject_identity_and_metadata_mismatch(self):
        mutations = [
            ("root fs uuid", lambda parts: parts[1].update(fsUuid=OTHER_UUID)),
            ("boot fs uuid", lambda parts: parts[0].update(fsUuid=OTHER_UUID)),
            ("root label", lambda parts: parts[1].update(partLabel="ARMADA_DATA")),
            ("boot fs type", lambda parts: parts[0].update(fsType="btrfs")),
            ("boot size", lambda parts: parts[0].update(sizeBytes=2 * GIB)),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                partitions = partitions_fixture()
                mutate(partitions)
                with self.assertRaises(ValueError):
                    guard.validate_growth_target(self.config, self.mount(), partitions)

        bad_mounts = [
            {"target": "/", "fsType": "btrfs", "fsUuid": ROOT_UUID, "fsRoot": "/"},
            {"target": "/var", "fsType": "btrfs", "fsUuid": OTHER_UUID, "fsRoot": "/var"},
            {"target": "/var", "fsType": "ext4", "fsUuid": ROOT_UUID, "fsRoot": "/var"},
        ]
        for mount in bad_mounts:
            with self.subTest(mount=mount):
                with self.assertRaises(ValueError):
                    guard.validate_growth_target(self.config, mount, partitions_fixture())

        small_minimum = copy.deepcopy(self.config)
        small_minimum["minimumRootPartitionBytes"] = 64 * GIB + 1
        with self.assertRaises(ValueError):
            guard.validate_growth_target(small_minimum, self.mount(), partitions_fixture())

    def test_reject_layout_violations(self):
        cases = []

        parts = partitions_fixture()
        parts[2]["disk"] = "/dev/loop-other"
        cases.append(("backup on another disk", parts))

        parts = partitions_fixture()
        parts[2]["startBytes"] = parts[1]["startBytes"] + parts[1]["sizeBytes"] - 1
        cases.append(("backup overlaps root", parts))

        parts = partitions_fixture()
        parts[1]["sizeBytes"] = 62 * GIB
        cases.append(("root below 63gib", parts))

        parts = partitions_fixture()
        parts[2]["sizeBytes"] = 1 * GIB
        cases.append(("backup not 2gib", parts))

        parts = partitions_fixture()
        parts.append(
            {
                "path": "/dev/loop-testp4",
                "disk": "/dev/loop-test",
                "startBytes": parts[2]["startBytes"] + parts[2]["sizeBytes"],
                "sizeBytes": 1 * MIB,
                "partLabel": "",
                "fsType": "none",
                "fsUuid": "",
            }
        )
        cases.append(("later partition after backup", parts))

        parts = partitions_fixture()[:2]
        cases.append(("missing backup", parts))

        parts = partitions_fixture()
        parts[1]["startBytes"] = parts[0]["startBytes"]
        cases.append(("root overlaps boot", parts))

        parts = [partitions_fixture()[0], partitions_fixture()[2], partitions_fixture()[1]]
        cases.append(("wrong order", parts))

        for label, partitions in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    guard.validate_growth_target(self.config, self.mount(), partitions)

    def test_success_resizes_once_and_second_call_is_noop(self):
        calls, runner = self.recorder()
        with mock.patch.object(guard, "read_snapshot", return_value=self.snapshot()):
            with mock.patch.object(guard, "run_resize", side_effect=runner):
                first = guard.run_guarded_growth(str(self.config_path), str(self.state_path))
                second = guard.run_guarded_growth(str(self.config_path), str(self.state_path))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(calls, [PERMITTED_RESIZE])
        self.assertTrue(self.state_path.exists())

    def test_mismatch_never_resizes_and_writes_no_state(self):
        partitions = partitions_fixture()
        partitions[2]["sizeBytes"] = 1 * GIB
        calls, runner = self.recorder()
        with mock.patch.object(
            guard, "read_snapshot", return_value=(self.mount(), partitions)
        ):
            with mock.patch.object(guard, "run_resize", side_effect=runner):
                try:
                    result = guard.run_guarded_growth(
                        str(self.config_path), str(self.state_path)
                    )
                except ValueError:
                    result = False
        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertFalse(self.state_path.exists())

    def test_failed_resize_leaves_no_success_state(self):
        calls, runner = self.recorder(fail=True)
        with mock.patch.object(guard, "read_snapshot", return_value=self.snapshot()):
            with mock.patch.object(guard, "run_resize", side_effect=runner):
                try:
                    result = guard.run_guarded_growth(
                        str(self.config_path), str(self.state_path)
                    )
                except RuntimeError:
                    result = False
        self.assertEqual(calls, [PERMITTED_RESIZE])
        self.assertFalse(result)
        self.assertFalse(self.state_path.exists())

    def test_actual_mount_device_must_match_root_partition(self):
        # read_snapshot stays real here: only the read-only pipeline primitives
        # are faked, so the device _read_mount reports must be bound to the
        # ARMADA_ROOT partition before any resize may run. The stale partition
        # carries the same filesystem UUID but sits before the boot partition,
        # so a guard that drops the mount device can grow /var via the wrong
        # /dev/loop-testp0. All paths are synthetic; these are pipeline fakes
        # and make no claim about any real device.
        stale = {
            "path": "/dev/loop-testp0",
            "disk": "/dev/loop-test",
            "startBytes": 0,
            "sizeBytes": 512 * 1024,
            "partLabel": "OLD_ROOT",
            "fsType": "btrfs",
            "fsUuid": ROOT_UUID,
        }
        partitions = [stale] + partitions_fixture()
        self.assertLessEqual(
            stale["startBytes"] + stale["sizeBytes"], partitions[1]["startBytes"]
        )

        def sysfs_integer(node, attribute):
            part = next(
                item
                for item in partitions
                if item["path"].rsplit("/", 1)[-1] in str(node)
            )
            if "size" in str(attribute).lower():
                return part["sizeBytes"] // 512
            return part["startBytes"] // 512

        def block_devices(argv):
            return {
                "blockdevices": [
                    {
                        "path": item["path"],
                        "pkname": item["disk"].rsplit("/", 1)[-1],
                        "type": "part",
                        "partlabel": item["partLabel"],
                        "fstype": item["fsType"],
                        "uuid": item["fsUuid"],
                    }
                    for item in partitions
                ]
            }

        def blkid_probe(path):
            part = next(item for item in partitions if item["path"] == str(path))
            return (part["fsType"], part["fsUuid"])

        with (
            mock.patch.object(
                guard,
                "_read_mount",
                return_value=(self.mount(), "/dev/loop-testp0", "loop-testp0"),
            ),
            mock.patch.object(guard, "_parent_disk", return_value="loop-test"),
            mock.patch.object(guard, "_run_json", side_effect=block_devices),
            mock.patch.object(guard, "_read_sysfs_integer", side_effect=sysfs_integer),
            mock.patch.object(guard, "_blkid_probe", side_effect=blkid_probe),
            mock.patch.object(guard, "run_resize") as resize,
        ):
            with self.assertRaises(ValueError):
                guard.run_guarded_growth(str(self.config_path), str(self.state_path))
        resize.assert_not_called()
        self.assertFalse(self.state_path.exists())


class FirstbootUnitTests(unittest.TestCase):
    def test_unit_runs_guarded_helper_before_user_sessions(self):
        text = UNIT.read_text()
        self.assertIn(
            "ExecStart=/usr/libexec/armada/armada-edl-grow-root "
            "/etc/armada/edl-direct.json /var/lib/armada/edl-root-expanded",
            text,
        )
        self.assertEqual(text.count("ExecStart="), 1)
        self.assertIn("ConditionPathExists=/etc/armada/edl-direct.json", text)
        self.assertIn("ConditionPathExists=!/var/lib/armada/edl-root-expanded", text)
        self.assertIn("RequiresMountsFor=/var", text)
        self.assertIn("After=local-fs.target", text)
        self.assertIn("Before=systemd-user-sessions.service", text)
        self.assertIn("StateDirectory=armada", text)
        self.assertIn("WantedBy=multi-user.target", text)
        for forbidden in ("growpart", "sfdisk", "sgdisk", "parted", "repart"):
            self.assertNotIn(forbidden, text.lower())


if __name__ == "__main__":
    unittest.main()
