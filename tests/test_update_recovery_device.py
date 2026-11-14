import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ARMADA_LIB = ROOT / 'system_files' / 'usr' / 'lib' / 'armada'
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import recovery_device as rd
from updates.recovery_layout import RecoveryLayoutError

PART_UUID = '12345678-1234-5678-9abc-def012345678'


class TestUpdateRecoveryDevice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.dt = self.base / 'dt'
        self.chosen = self.dt / 'chosen'
        self.chosen.mkdir(parents=True)
        (self.dt / 'compatible').write_bytes(b'lenovo,elden\0qcom,sm8750\0')
        self._write_settings()

        self.dev = self.base / 'dev_sda19'
        self.dev.write_bytes(b'')
        self.sys_block = self.base / 'sys' / 'dev' / 'block'
        self.sys_block.mkdir(parents=True)
        self.node = self._make_sysfs_node('1.ufs/sda/sda19')
        (self.sys_block / '0:0').symlink_to(self.node)

    def _write_settings(self, **overrides):
        v = [0] * 16
        v[0] = 1
        v[1] = 1
        v[5] = 1
        v[13] = 1
        for idx, val in overrides.items():
            v[int(idx)] = val
        (self.chosen / 'kshim,settings-v1').write_bytes(struct.pack('>16I', *v))

    def _make_sysfs_node(self, rel_path, *, size='4194304', block_size='4096'):
        node = self.base / 'sys' / 'devices' / 'soc' / rel_path
        node.mkdir(parents=True, exist_ok=True)
        (node.parent / 'queue').mkdir(parents=True, exist_ok=True)
        (node / 'partition').write_text('19\n')
        (node / 'size').write_text(f'{size}\n')
        (node.parent / 'queue' / 'logical_block_size').write_text(f'{block_size}\n')
        return node

    def _path_router(self, raw):
        s = str(raw)
        if s == '/proc/device-tree':
            return self.dt
        if s.startswith('/sys/dev/block/'):
            return self.sys_block / s[len('/sys/dev/block/'):]
        return Path(raw)

    def _locate(self, *, require_edl=False, lines=None):
        stdout = f'{self.dev}\n' if lines is None else lines
        with (
            mock.patch.object(rd, 'Path', side_effect=self._path_router),
            mock.patch.object(rd.stat, 'S_ISBLK', return_value=True),
            mock.patch.object(rd.subprocess, 'run', return_value=SimpleNamespace(stdout=stdout)) as run_mock,
            mock.patch.object(rd.subprocess, 'check_output', return_value=f'{PART_UUID}\n') as out_mock,
        ):
            return rd.locate_recovery_partition(require_edl=require_edl), run_mock, out_mock

    def test_locate_edl_toggle_and_tuple(self):
        (res, _, _) = self._locate(require_edl=False)
        self.assertEqual(res, (self.dev.resolve(), PART_UUID, 1, 0))

        with self.assertRaises(FileNotFoundError):
            self._locate(require_edl=True)

        (self.chosen / 'kshim,edl-request-version').write_bytes(struct.pack('>I', 1))
        (res_edl, _, _) = self._locate(require_edl=True)
        self.assertEqual(res_edl, (self.dev.resolve(), PART_UUID, 1, 0))

    def test_locate_invalid_settings_fails_before_blkid(self):
        self._write_settings(**{'13': 0})
        with (
            mock.patch.object(rd, 'Path', side_effect=self._path_router),
            mock.patch.object(rd.subprocess, 'run') as run_mock,
        ):
            with self.assertRaisesRegex(RecoveryLayoutError, 'usable internal settings'):
                rd.locate_recovery_partition(require_edl=False)
            run_mock.assert_not_called()

    def test_locate_candidate_and_sysfs_rejections(self):
        with self.subTest('wrong_size'):
            (self.node / 'size').write_text('4194303\n')
            with self.assertRaisesRegex(RecoveryLayoutError, 'not exactly 2GiB'):
                self._locate()
            (self.node / 'size').write_text('4194304\n')

        with self.subTest('wrong_block_size'):
            (self.node.parent / 'queue' / 'logical_block_size').write_text('1024\n')
            with self.assertRaisesRegex(RecoveryLayoutError, 'Unsupported logical block size'):
                self._locate()
            (self.node.parent / 'queue' / 'logical_block_size').write_text('4096\n')

        with self.subTest('duplicate_candidates'):
            with self.assertRaisesRegex(RecoveryLayoutError, 'Expected one internal UFS'):
                self._locate(lines=f'{self.dev}\n{self.dev}\n')

        with self.subTest('non_ufs_node'):
            non_ufs = self._make_sysfs_node('1.mmc/mmcblk0/mmcblk0p19')
            (self.sys_block / '0:0').unlink()
            (self.sys_block / '0:0').symlink_to(non_ufs)
            with self.assertRaisesRegex(RecoveryLayoutError, 'Expected one internal UFS'):
                self._locate()

    def test_open_recovery_modes_and_cleanup(self):
        loc = (Path('/dev/example'), PART_UUID, 1, 77)
        blk = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=77)
        for writable, mode_flag in ((False, os.O_RDONLY), (True, os.O_RDWR)):
            with self.subTest(writable=writable):
                with (
                    mock.patch.object(rd, 'locate_recovery_partition', return_value=loc) as loc_mock,
                    mock.patch.object(rd.os, 'open', return_value=91) as open_mock,
                    mock.patch.object(rd.os, 'fstat', return_value=blk),
                    mock.patch.object(rd.os, 'close') as close_mock,
                ):
                    with rd.open_recovery(writable=writable) as handle:
                        self.assertEqual(handle, (91, PART_UUID, 1))
                    loc_mock.assert_called_once_with(require_edl=False)
                    open_mock.assert_called_once_with(
                        '/dev/example', mode_flag | os.O_CLOEXEC | os.O_NOFOLLOW
                    )
                    close_mock.assert_called_once_with(91)

        with (
            mock.patch.object(rd, 'locate_recovery_partition', return_value=loc),
            mock.patch.object(rd.os, 'open', return_value=91),
            mock.patch.object(rd.os, 'fstat', return_value=blk),
            mock.patch.object(rd.os, 'close') as close_mock,
        ):
            with self.assertRaises(RuntimeError):
                with rd.open_recovery():
                    raise RuntimeError('boom')
            close_mock.assert_called_once_with(91)

    def test_open_recovery_validation_failures(self):
        with mock.patch.object(rd.os, 'open') as open_mock:
            with self.assertRaises(TypeError):
                with rd.open_recovery(writable='yes'):
                    pass
            open_mock.assert_not_called()

        loc = (Path('/dev/example'), PART_UUID, 1, 77)
        bad_stats = [
            SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=78),
            SimpleNamespace(st_mode=stat.S_IFREG, st_rdev=77),
        ]
        for bad in bad_stats:
            with self.subTest(st_mode=bad.st_mode, st_rdev=bad.st_rdev):
                with (
                    mock.patch.object(rd, 'locate_recovery_partition', return_value=loc),
                    mock.patch.object(rd.os, 'open', return_value=91),
                    mock.patch.object(rd.os, 'fstat', return_value=bad),
                    mock.patch.object(rd.os, 'close') as close_mock,
                ):
                    with self.assertRaisesRegex(RecoveryLayoutError, 'not a block device'):
                        with rd.open_recovery():
                            pass
                    close_mock.assert_called_once_with(91)


if __name__ == '__main__':
    unittest.main()
