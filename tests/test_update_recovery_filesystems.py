import copy
import ctypes
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest import mock

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / 'system_files/usr/lib/armada'),
)
from updates import physical_slots as slots
from updates import recovery_filesystems as rf

REAL_LSTAT = os.lstat
REAL_FSTAT = os.fstat
REAL_OPEN = os.open
REAL_CLOSE = os.close

GIB = 1024 * 1024 * 1024
BOOT_BYTES = 1 * GIB
ROOT_BYTES = 64 * GIB
BACKUP_BYTES = 2 * GIB

BOOT_PART_UUID = '11111111-2222-4333-8444-555555555501'
ROOT_PART_UUID = '11111111-2222-4333-8444-555555555502'
BACKUP_PART_UUID = '11111111-2222-4333-8444-555555555503'

BOOT_FS_UUID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee01'
ROOT_FS_UUID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeee02'


def changed_stat(info, *, mode=None, size=None, rdev=None):
    attrs = {
        name: getattr(info, name)
        for name in dir(info)
        if name.startswith('st_')
    }
    if mode is not None:
        attrs['st_mode'] = mode
    if size is not None:
        attrs['st_size'] = size
    if rdev is not None:
        attrs['st_rdev'] = rdev
    return SimpleNamespace(**attrs)


class RecoveryFilesystemsTests(unittest.TestCase):
    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(text), encoding='ascii')

    @contextmanager
    def environment(self, bs=4096, current=0, board=1):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            images = root / 'images'
            images.mkdir()

            lun0_bytes = 70 * GIB
            lun_other_bytes = 64 * 1024 * 1024
            boot_start_lba = (4 * 1024 * 1024) // bs
            boot_blocks = BOOT_BYTES // bs
            root_start_lba = boot_start_lba + boot_blocks
            root_blocks = ROOT_BYTES // bs
            backup_start_lba = root_start_lba + root_blocks
            backup_blocks = BACKUP_BYTES // bs

            env = SimpleNamespace(
                bs=bs,
                current=current,
                board=board,
                root=root,
                images=[],
                part_images={},
                sysfs=root / 'sys',
                class_block=root / 'sys/class/block',
                dev_block=root / 'dev/block',
                controller=root / 'sys/devices/controller.ufs',
                nodes=[],
                rdevs=[],
                part_nodes={},
                part_rdevs={},
                mapping={},
                opened=[],
                open_flags=[],
                closed=[],
                blkid_calls=[],
            )

            self.write(env.controller / 'attributes/boot_lun_enabled', current + 1)
            identities = {}
            by_path = {}

            for lun in range(6):
                img = images / f'lun{lun}.img'
                img.write_bytes(b'\x00')
                env.images.append(img)
                node = (
                    env.controller
                    / 'host0/target0:0:0'
                    / f'0:0:0:{lun}/block/sd{chr(97 + lun)}'
                )
                minor = 16 * lun
                self.write(node / 'dev', f'8:{minor}')
                self.write(node / 'queue/logical_block_size', bs)
                total_bytes = lun0_bytes if lun == 0 else lun_other_bytes
                self.write(node / 'size', total_bytes // 512)
                self.write(
                    node.parent.parent / 'unit_descriptor/boot_lun_id',
                    (0, 1, 2, 0, 0, 0)[lun],
                )
                rdev = os.makedev(8, minor)
                env.nodes.append(node)
                env.rdevs.append(rdev)
                env.mapping[rdev] = node
                env.class_block.mkdir(parents=True, exist_ok=True)
                (env.class_block / node.name).symlink_to(node)
                env.dev_block.mkdir(parents=True, exist_ok=True)
                (env.dev_block / f'8:{minor}').symlink_to(img)
                info = REAL_LSTAT(img)
                identities[(info.st_dev, info.st_ino)] = rdev
                by_path[img.resolve()] = rdev

            env.extents = {
                'ARMADA_BOOT': {
                    'lun': 0,
                    'firstLba': boot_start_lba,
                    'blockCount': boot_blocks,
                    'uuid': BOOT_PART_UUID,
                    'part_no': 1,
                    'minor': 1,
                },
                'ARMADA_ROOT': {
                    'lun': 0,
                    'firstLba': root_start_lba,
                    'blockCount': root_blocks,
                    'uuid': ROOT_PART_UUID,
                    'part_no': 2,
                    'minor': 2,
                },
                'ARMADA_BACKUP': {
                    'lun': 0,
                    'firstLba': backup_start_lba,
                    'blockCount': backup_blocks,
                    'uuid': BACKUP_PART_UUID,
                    'part_no': 3,
                    'minor': 3,
                },
            }

            for label, spec in env.extents.items():
                pno = spec['part_no']
                minor = spec['minor']
                pnode = env.nodes[0] / f'sda{pno}'
                pimg = images / f'sda{pno}.img'
                pimg.write_bytes(b'\x00')
                self.write(pnode / 'partition', pno)
                self.write(pnode / 'start', (spec['firstLba'] * bs) // 512)
                self.write(pnode / 'size', (spec['blockCount'] * bs) // 512)
                self.write(pnode / 'dev', f'8:{minor}')
                prdev = os.makedev(8, minor)
                (env.class_block / pnode.name).symlink_to(pnode)
                (env.dev_block / f'8:{minor}').symlink_to(pimg)
                env.part_images[label] = pimg
                env.part_nodes[label] = pnode
                env.part_rdevs[label] = prdev
                env.mapping[prdev] = pnode
                info = REAL_LSTAT(pimg)
                identities[(info.st_dev, info.st_ino)] = prdev
                by_path[pimg.resolve()] = prdev

            env.by_path = by_path
            env.identities = identities
            env.recovery = (
                env.part_images['ARMADA_BACKUP'],
                BACKUP_PART_UUID,
                board,
                env.part_rdevs['ARMADA_BACKUP'],
            )
            env.context = {
                'actualSlot': 'ab'[current],
                'inactiveSlot': 'ba'[current],
            }
            env.blkid_outputs = {
                env.part_rdevs['ARMADA_BOOT']: (
                    f'DEVNAME=/proc/self/fd/9\n'
                    f'TYPE=ext4\n'
                    f'UUID={BOOT_FS_UUID}\n'
                    f'USAGE=filesystem\n'
                ),
                env.part_rdevs['ARMADA_ROOT']: (
                    f'DEVNAME=/proc/self/fd/10\n'
                    f'TYPE=btrfs\n'
                    f'UUID={ROOT_FS_UUID}\n'
                    f'UUID_SUB=12345678-1234-4123-8123-123456789abc\n'
                    f'USAGE=filesystem\n'
                ),
            }

            def fake_lstat(path, *args, **kwargs):
                info = REAL_LSTAT(path, *args, **kwargs)
                rdev = by_path.get(Path(path))
                if rdev is None:
                    return info
                return changed_stat(info, mode=stat.S_IFBLK | 0o600, rdev=rdev)

            def fake_fstat(fd):
                info = REAL_FSTAT(fd)
                rdev = identities.get((info.st_dev, info.st_ino))
                if rdev is None:
                    return info
                return changed_stat(info, mode=stat.S_IFBLK | 0o600, rdev=rdev)

            active_block_fds = set()

            def tracked_open(path, flags, *args, **kwargs):
                fd = REAL_OPEN(path, flags, *args, **kwargs)
                resolved = Path(path).resolve() if Path(path).exists() else Path(path)
                if resolved in by_path:
                    env.opened.append(fd)
                    active_block_fds.add(fd)
                    env.open_flags.append(flags)
                return fd

            def tracked_close(fd):
                if fd in active_block_fds:
                    active_block_fds.discard(fd)
                    env.closed.append(fd)
                return REAL_CLOSE(fd)

            def fake_locator(fd_arr, bs_arr, bc_arr, bid_arr, enabled, label_bytes, result, out_uuid):
                label = label_bytes.decode('ascii')
                if label not in env.extents:
                    return -2
                spec = env.extents[label]
                result[0] = spec['lun']
                result[1] = spec['firstLba']
                result[2] = spec['blockCount']
                raw = uuid.UUID(spec['uuid']).bytes
                for i, b in enumerate(raw):
                    out_uuid[i] = b
                return 0

            def fake_run(cmd, *, pass_fds=(), capture_output=False, text=False, check=False, timeout=None):
                self.assertEqual(cmd[:5], ['blkid', '-p', '-o', 'export', '--'])
                self.assertEqual(len(pass_fds), 1)
                fd = pass_fds[0]
                self.assertEqual(cmd[5], f'/proc/self/fd/{fd}')
                self.assertTrue(capture_output)
                self.assertTrue(text)
                self.assertTrue(check)
                self.assertEqual(timeout, 15)
                acc_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
                self.assertEqual(acc_mode, os.O_RDONLY)
                st = fake_fstat(fd)
                env.blkid_calls.append(st.st_rdev)
                if st.st_rdev not in env.blkid_outputs:
                    raise AssertionError(f'unexpected blkid probe on rdev {st.st_rdev}')
                out = env.blkid_outputs[st.st_rdev]
                if isinstance(out, Exception):
                    raise out
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr='')

            env.fake_locator = fake_locator
            env.fake_run = fake_run
            env.fake_fstat = fake_fstat

            stack.enter_context(mock.patch.object(slots, 'SYS_CLASS_BLOCK', env.class_block))
            stack.enter_context(mock.patch.object(slots, 'DEV_BLOCK', env.dev_block))
            stack.enter_context(mock.patch.object(slots.os, 'lstat', side_effect=fake_lstat))
            stack.enter_context(mock.patch.object(slots.os, 'fstat', side_effect=fake_fstat))
            stack.enter_context(mock.patch.object(slots.os, 'open', side_effect=tracked_open))
            stack.enter_context(mock.patch.object(slots.os, 'close', side_effect=tracked_close))
            stack.enter_context(
                mock.patch.object(slots, '_sysfs_block_node', side_effect=lambda r: env.mapping[r])
            )
            stack.enter_context(
                mock.patch.object(rf, '_sysfs_block_node', side_effect=lambda r: env.mapping[r])
            )
            env.locate_recovery_mock = stack.enter_context(
                mock.patch.object(slots, 'locate_recovery_partition', side_effect=lambda **kw: env.recovery)
            )
            stack.enter_context(
                mock.patch.object(rf, 'locate_recovery_partition', side_effect=lambda **kw: env.recovery)
            )
            stack.enter_context(
                mock.patch.object(slots.boot_context, 'read_boot_slot', side_effect=lambda: dict(env.context))
            )
            stack.enter_context(
                mock.patch.object(slots, '_read_slot_flags',
                                  side_effect=lambda fds, found: (env.current, {'a': True, 'b': True}))
            )
            stack.enter_context(
                mock.patch.object(rf, '_load_locator', side_effect=lambda: env.fake_locator)
            )
            env.run_mock = stack.enter_context(
                mock.patch.object(rf.subprocess, 'run', side_effect=fake_run)
            )
            env.snapshot_mock = stack.enter_context(
                mock.patch.object(
                    slots.armada_os,
                    'snapshot',
                    side_effect=AssertionError('armada_os.snapshot must not be called in recovery check'),
                )
            )
            yield env

    def test_valid_discovery_across_block_sizes_and_slots(self):
        for bs in (512, 4096):
            for current, board, model in ((0, 1, 'TB322FC'), (1, 2, 'TB323FU')):
                with self.subTest(bs=bs, current=current, board=board):
                    with self.environment(bs=bs, current=current, board=board) as env:
                        expected_phys = slots.inspect_recovery_slots()
                        env.opened.clear()
                        env.closed.clear()
                        env.open_flags.clear()
                        env.blkid_calls.clear()

                        report = rf.inspect_recovery_filesystems(expected_physical=expected_phys)
                        self.assertEqual(report['schemaVersion'], 1)
                        self.assertEqual(report['phase'], 'recovery_filesystems_checked')
                        self.assertEqual(report['physical'], expected_phys)
                        self.assertEqual(report['physical']['model'], model)
                        self.assertNotIn('deployment', report['physical'])
                        self.assertNotIn('booted', report['physical'])

                        self.assertEqual(
                            report['boot'],
                            {
                                'path': str(env.part_images['ARMADA_BOOT'].resolve()),
                                'rdev': env.part_rdevs['ARMADA_BOOT'],
                                'lun': 0,
                                'startBytes': 4 * 1024 * 1024,
                                'bytes': BOOT_BYTES,
                                'partUuid': BOOT_PART_UUID,
                                'fsType': 'ext4',
                                'uuid': BOOT_FS_UUID,
                            },
                        )
                        self.assertEqual(
                            report['root'],
                            {
                                'path': str(env.part_images['ARMADA_ROOT'].resolve()),
                                'rdev': env.part_rdevs['ARMADA_ROOT'],
                                'lun': 0,
                                'startBytes': 4 * 1024 * 1024 + BOOT_BYTES,
                                'bytes': ROOT_BYTES,
                                'partUuid': ROOT_PART_UUID,
                                'fsType': 'btrfs',
                                'uuid': ROOT_FS_UUID,
                            },
                        )
                        self.assertEqual(
                            report['backup'],
                            {
                                'path': str(env.part_images['ARMADA_BACKUP'].resolve()),
                                'rdev': env.part_rdevs['ARMADA_BACKUP'],
                                'lun': 0,
                                'startBytes': 4 * 1024 * 1024 + BOOT_BYTES + ROOT_BYTES,
                                'bytes': BACKUP_BYTES,
                                'partUuid': BACKUP_PART_UUID,
                            },
                        )
                        self.assertNotIn('fsType', report['backup'])
                        self.assertNotIn('uuid', report['backup'])
                        self.assertEqual(
                            env.blkid_calls,
                            [env.part_rdevs['ARMADA_BOOT'], env.part_rdevs['ARMADA_ROOT']],
                        )
                        self.assertEqual(len(env.opened), len(env.closed))
                        self.assertEqual(set(env.opened), set(env.closed))
                        for flags in env.open_flags:
                            self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                        env.snapshot_mock.assert_not_called()

    def test_expected_physical_validation(self):
        with self.environment() as env:
            live = slots.inspect_recovery_slots()
            for bad in (
                'not-a-dict',
                {**live, 'actualSlot': 'b'},
                {**live, 'deployment': 'forged'},
                {**live, 'booted': {'forged': True}},
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems(expected_physical=bad)
            self.assertEqual(len(env.opened), len(env.closed))

    def test_rejects_wrong_lun_or_out_of_bounds_extents(self):
        for label, mutator in (
            ('boot_on_lun1', lambda e: e.extents['ARMADA_BOOT'].update(lun=1)),
            ('root_on_lun2', lambda e: e.extents['ARMADA_ROOT'].update(lun=2)),
            ('backup_on_lun4', lambda e: e.extents['ARMADA_BACKUP'].update(lun=4)),
            ('zero_blocks', lambda e: e.extents['ARMADA_BOOT'].update(blockCount=0)),
            ('past_lun0_end', lambda e: e.extents['ARMADA_BACKUP'].update(blockCount=999999999)),
        ):
            with self.subTest(case=label):
                with self.environment() as env:
                    mutator(env)
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems()
                    self.assertEqual(len(env.opened), len(env.closed))

    def test_rejects_invalid_sizes_and_non_adjacent_layout(self):
        def shrink_boot(env):
            env.extents['ARMADA_BOOT']['blockCount'] = (512 * 1024 * 1024) // env.bs

        def shrink_root(env):
            env.extents['ARMADA_ROOT']['blockCount'] = (62 * GIB) // env.bs

        def bad_backup_size(env):
            env.extents['ARMADA_BACKUP']['blockCount'] = (1 * GIB) // env.bs

        def gap_boot_root(env):
            env.extents['ARMADA_ROOT']['firstLba'] += 1
            env.extents['ARMADA_BACKUP']['firstLba'] += 1

        def overlap_root_backup(env):
            env.extents['ARMADA_BACKUP']['firstLba'] -= 1

        for name, fn in (
            ('shrink_boot', shrink_boot),
            ('shrink_root', shrink_root),
            ('bad_backup_size', bad_backup_size),
            ('gap_boot_root', gap_boot_root),
            ('overlap_root_backup', overlap_root_backup),
        ):
            with self.subTest(case=name):
                with self.environment() as env:
                    fn(env)
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems()
                    self.assertEqual(len(env.opened), len(env.closed))

    def test_rejects_bad_or_mismatched_partuuids(self):
        for name, mutator in (
            ('zero_uuid', lambda e: e.extents['ARMADA_BOOT'].update(uuid='00000000-0000-0000-0000-000000000000')),
            ('duplicate_partuuid', lambda e: e.extents['ARMADA_ROOT'].update(uuid=BOOT_PART_UUID)),
            ('backup_mismatch', lambda e: e.extents['ARMADA_BACKUP'].update(uuid='11111111-2222-4333-8444-555555555599')),
        ):
            with self.subTest(case=name):
                with self.environment() as env:
                    mutator(env)
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems()
                    self.assertEqual(len(env.opened), len(env.closed))

    def test_allows_nonpartition_device_and_subsystem_symlinks(self):
        with self.environment() as env:
            (env.nodes[0] / 'device').symlink_to(env.controller)
            (env.nodes[0] / 'subsystem').symlink_to(env.class_block)

            report = rf.inspect_recovery_filesystems()

            self.assertEqual(report['phase'], 'recovery_filesystems_checked')
            self.assertEqual(report['boot']['uuid'], BOOT_FS_UUID)
            self.assertEqual(report['root']['uuid'], ROOT_FS_UUID)
            self.assertEqual(len(env.opened), len(env.closed))

    def test_rejects_sysfs_partition_anomalies(self):
        def add_duplicate_candidate(env):
            dup = env.nodes[0] / 'sda99'
            self.write(dup / 'partition', 99)
            self.write(dup / 'start', self._read_int(env.part_nodes['ARMADA_BOOT'] / 'start'))
            self.write(dup / 'size', self._read_int(env.part_nodes['ARMADA_BOOT'] / 'size'))
            self.write(dup / 'dev', '8:99')

        def wrong_parent_mapping(env):
            other = env.nodes[1] / 'sdb1'
            self.write(other / 'partition', 1)
            env.mapping[env.part_rdevs['ARMADA_BOOT']] = other

        def symlink_child_in_lun0(env):
            (env.nodes[0] / ' rogue_link').symlink_to(env.part_nodes['ARMADA_BOOT'])

        def backup_rdev_mismatch(env):
            env.recovery = (
                env.part_images['ARMADA_BACKUP'],
                BACKUP_PART_UUID,
                env.board,
                env.part_rdevs['ARMADA_ROOT'],
            )

        for name, fn in (
            ('duplicate_candidate', add_duplicate_candidate),
            ('wrong_parent_mapping', wrong_parent_mapping),
            ('symlink_child_in_lun0', symlink_child_in_lun0),
            ('backup_rdev_mismatch', backup_rdev_mismatch),
        ):
            with self.subTest(case=name):
                with self.environment() as env:
                    fn(env)
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems()
                    self.assertEqual(len(env.opened), len(env.closed))

    @staticmethod
    def _read_int(path):
        return int(path.read_text(encoding='ascii').strip())

    def test_rejects_filesystem_probe_errors(self):
        for name, boot_out, root_out in (
            (
                'wrong_boot_fs',
                f'TYPE=vfat\nUUID={BOOT_FS_UUID}\n',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'wrong_root_fs',
                f'TYPE=ext4\nUUID={BOOT_FS_UUID}\n',
                f'TYPE=ext4\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'zero_fs_uuid',
                'TYPE=ext4\nUUID=00000000-0000-0000-0000-000000000000\n',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'uppercase_noncanonical_uuid',
                f'TYPE=ext4\nUUID={BOOT_FS_UUID.upper()}\n',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'duplicate_fs_uuid',
                f'TYPE=ext4\nUUID={BOOT_FS_UUID}\n',
                f'TYPE=btrfs\nUUID={BOOT_FS_UUID}\n',
            ),
            (
                'duplicate_export_key',
                f'TYPE=ext4\nTYPE=ext4\nUUID={BOOT_FS_UUID}\n',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'empty_export',
                '',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'oversized_export',
                f'TYPE=ext4\nUUID={BOOT_FS_UUID}\nEXTRA=' + ('A' * 17000) + '\n',
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
            (
                'subprocess_error',
                subprocess.CalledProcessError(2, ['blkid']),
                f'TYPE=btrfs\nUUID={ROOT_FS_UUID}\n',
            ),
        ):
            with self.subTest(case=name):
                with self.environment() as env:
                    env.blkid_outputs[env.part_rdevs['ARMADA_BOOT']] = boot_out
                    env.blkid_outputs[env.part_rdevs['ARMADA_ROOT']] = root_out
                    with self.assertRaises(rf.RecoveryFilesystemsError):
                        rf.inspect_recovery_filesystems()
                    self.assertNotIn(env.part_rdevs['ARMADA_BACKUP'], env.blkid_calls)
                    self.assertEqual(len(env.opened), len(env.closed))

    def test_allows_blkid_partition_metadata_and_ignores_labels(self):
        with self.environment() as env:
            env.blkid_outputs[env.part_rdevs['ARMADA_BOOT']] = (
                f'TYPE=ext4\n'
                f'UUID={BOOT_FS_UUID}\n'
                f'PART_ENTRY_NAME=ARMADA_ROOT\n'
                f'PART_ENTRY_UUID={ROOT_PART_UUID}\n'
                f'PARTLABEL=UNRELATED_BOOT_LABEL\n'
                f'USAGE=filesystem\n'
            )
            env.blkid_outputs[env.part_rdevs['ARMADA_ROOT']] = (
                f'TYPE=btrfs\n'
                f'UUID={ROOT_FS_UUID}\n'
                f'PART_ENTRY_NAME=ARMADA_BOOT\n'
                f'PART_ENTRY_UUID={BOOT_PART_UUID}\n'
                f'PARTLABEL=UNRELATED_ROOT_LABEL\n'
                f'USAGE=filesystem\n'
            )

            report = rf.inspect_recovery_filesystems()

            self.assertEqual(report['boot']['uuid'], BOOT_FS_UUID)
            self.assertEqual(report['root']['uuid'], ROOT_FS_UUID)
            self.assertEqual(report['boot']['partUuid'], BOOT_PART_UUID)
            self.assertEqual(report['root']['partUuid'], ROOT_PART_UUID)
            self.assertEqual(
                report['boot']['path'],
                str(env.part_images['ARMADA_BOOT'].resolve()),
            )
            self.assertEqual(
                report['root']['path'],
                str(env.part_images['ARMADA_ROOT'].resolve()),
            )
            self.assertEqual(
                env.blkid_calls,
                [env.part_rdevs['ARMADA_BOOT'], env.part_rdevs['ARMADA_ROOT']],
            )
            self.assertEqual(len(env.opened), len(env.closed))

    def test_rejects_midcheck_drift(self):
        with self.subTest(case='partition_size_drift'):
            with self.environment() as env:
                orig_run = env.fake_run

                def drifting_run(*args, **kwargs):
                    res = orig_run(*args, **kwargs)
                    self.write(env.part_nodes['ARMADA_ROOT'] / 'size', 12345)
                    return res

                env.run_mock.side_effect = drifting_run
                with self.assertRaises(rf.RecoveryFilesystemsError):
                    rf.inspect_recovery_filesystems()
                self.assertEqual(len(env.opened), len(env.closed))

        with self.subTest(case='fd_rdev_drift'):
            with self.environment() as env:
                orig_run = env.fake_run

                def drifting_fd_run(*args, **kwargs):
                    res = orig_run(*args, **kwargs)
                    for k in list(env.identities):
                        if env.identities[k] == env.part_rdevs['ARMADA_BOOT']:
                            env.identities[k] = os.makedev(8, 250)
                    return res

                env.run_mock.side_effect = drifting_fd_run
                with self.assertRaises(rf.RecoveryFilesystemsError):
                    rf.inspect_recovery_filesystems()
                self.assertEqual(len(env.opened), len(env.closed))

        with self.subTest(case='recovery_locator_drift'):
            with self.environment() as env:
                orig_run = env.fake_run

                def drifting_rec_run(*args, **kwargs):
                    res = orig_run(*args, **kwargs)
                    env.recovery = (
                        env.part_images['ARMADA_BACKUP'],
                        BACKUP_PART_UUID,
                        2,
                        env.part_rdevs['ARMADA_BACKUP'],
                    )
                    return res

                env.run_mock.side_effect = drifting_rec_run
                with self.assertRaises(rf.RecoveryFilesystemsError):
                    rf.inspect_recovery_filesystems()
                self.assertEqual(len(env.opened), len(env.closed))


if __name__ == '__main__':
    unittest.main()
