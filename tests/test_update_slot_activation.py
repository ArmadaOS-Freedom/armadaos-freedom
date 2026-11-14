# SPDX-License-Identifier: GPL-3.0-only
"""Inactive slot activation tests."""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT / 'system_files/usr/lib/armada', ROOT / 'tests'):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import test_update_boot_writer as wf
import test_update_physical_slots as pf
import test_update_trial_store as jf
from updates import boot_writer as writer
from updates import physical_slots as slots
from updates import recovery_device
from updates import slot_activation as activation
from updates import trial_store as store


class SlotActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        wf.BootWriterTests.setUpClass()
        cls.addClassCleanup(wf.BootWriterTests.doClassCleanups)
        base = Path(pf.PhysicalSlotsTests.tmp.name)
        kshim = pf.PhysicalSlotsTests.kshim
        cls.library = base / 'libarmada-update-ab-activation.so'
        subprocess.run([
            'cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L',
            '-D_FILE_OFFSET_BITS=64', '-Wall', '-Wextra', '-Werror',
            '-fPIC', '-shared',
            '-I', str(ROOT / 'build_files'),
            '-I', str(kshim / 'src/include'),
            '-I', str(kshim / 'tests'),
            str(ROOT / 'build_files/update_ab_inspect.c'),
            str(kshim / 'src/core/storage_ab.c'),
            str(kshim / 'src/core/storage_block.c'),
            str(ROOT / 'tests/update_slot_activation_shim.c'),
            '-Wl,--wrap=fstat64',
            '-o', str(cls.library),
        ], check=True)
        cls.shim = ctypes.CDLL(str(cls.library))
        cls.shim.fixture_configure.argtypes = [ctypes.c_char_p, ctypes.c_int]
        cls.shim.fixture_configure.restype = ctypes.c_int
        cls.shim.fixture_set_bsg_fd.argtypes = [ctypes.c_int]
        cls.shim.fixture_set_bsg_fd.restype = ctypes.c_int
        cls.shim.fixture_calls.argtypes = []
        cls.shim.fixture_calls.restype = ctypes.c_int
        cls.shim.fixture_set_calls.argtypes = []
        cls.shim.fixture_set_calls.restype = ctypes.c_int

    @staticmethod
    def _patch_journal_board(fd: int, board: int) -> None:
        raw = os.pread(fd, jf.HEADER_BYTES, 0)
        meta = json.loads(raw.rstrip(b' ').decode('ascii'))
        meta['configBoardId'] = board
        header = json.dumps(meta).encode('ascii').ljust(jf.HEADER_BYTES, b' ')
        os.pwrite(fd, header, 0)
        desc = bytearray(os.pread(fd, 4096, jf.REGION_OFFSET))
        struct.pack_into('<I', desc, 88, board)
        crc = zlib.crc32(bytes(desc[:4092])) & 0xFFFFFFFF
        struct.pack_into('<I', desc, 4092, crc)
        os.pwrite(fd, bytes(desc), jf.REGION_OFFSET)

    def _prepare(self, stack: ExitStack, *, bs: int = 512, current: int = 0,
                 board: int = 1, native_mode: int = 0) -> SimpleNamespace:
        journal = jf.TrialStoreTest('test_roundtrip_region_bounds')
        journal.setUp()
        stack.callback(journal.doCleanups)

        writer_test = wf.BootWriterTests()
        writer_test.setUp()
        env, trust, candidate, *_ = writer_test._prepare(
            stack, bs=bs, current=current, board=board)

        if board != 1:
            self._patch_journal_board(journal.fd, board)

        env.recovery = (str(journal.path), journal.part, board, env.recovery[3])
        stack.enter_context(mock.patch.object(
            slots, 'locate_recovery_partition', return_value=env.recovery))
        stack.enter_context(mock.patch.object(
            recovery_device, 'locate_recovery_partition', return_value=env.recovery))

        physical = slots.inspect_physical_slots()
        trust['physical'] = physical
        trust['sources']['physical'] = physical
        report = writer.write_inactive_boot(trust, candidate)

        root = env.images[0].parent.parent
        node = env.controller / 'host0/ufs-bsg0/bsg/ufs-bsg0'
        pf.PhysicalSlotsTests.write(node / 'dev', '240:7')
        class_root = env.sysfs / 'class/bsg'
        class_root.mkdir(parents=True, exist_ok=True)
        (class_root / 'ufs-bsg0').symlink_to(node)
        char_root = env.sysfs / 'dev/char'
        char_root.mkdir(parents=True, exist_ok=True)
        (char_root / '240:7').symlink_to(node)
        dev_bsg = root / 'dev/bsg'
        dev_bsg.mkdir(parents=True, exist_ok=True)
        bsg = dev_bsg / 'ufs-bsg0'
        bsg.write_bytes(b'')

        attribute = env.controller / 'attributes/boot_lun_enabled'
        rc = self.shim.fixture_configure(os.fsencode(attribute), native_mode)
        self.assertEqual(rc, 0)

        for target, attr, val in [
            (slots, 'LIBRARY_PATH', self.library),
            (activation, 'SYS_CLASS_BSG', class_root),
            (activation, 'SYS_DEV_CHAR', char_root),
            (activation, 'DEV_BSG', dev_bsg),
        ]:
            stack.enter_context(mock.patch.object(target, attr, val))

        bsg_info = pf.REAL_LSTAT(bsg)
        bsg_id = (bsg_info.st_dev, bsg_info.st_ino)
        bsg_rdev = os.makedev(240, 7)
        j_info = pf.REAL_FSTAT(journal.fd)
        j_id = (j_info.st_dev, j_info.st_ino)
        j_rdev = env.recovery[3]
        bsg_path = Path(bsg)

        prev_fstat = os.fstat
        prev_lstat = os.lstat
        prev_open = os.open
        opens: list[tuple[int, Path, int]] = []

        def fake_fstat(fd: int):
            info = prev_fstat(fd)
            ident = (info.st_dev, info.st_ino)
            if ident == j_id:
                return pf.changed_stat(
                    info, mode=stat.S_IFBLK | 0o600, rdev=j_rdev)
            if ident == bsg_id:
                return pf.changed_stat(
                    info, mode=stat.S_IFCHR | 0o600, rdev=bsg_rdev)
            return info

        def fake_lstat(path, *args, **kwargs):
            info = prev_lstat(path, *args, **kwargs)
            if Path(path) == bsg_path:
                return pf.changed_stat(
                    info, mode=stat.S_IFCHR | 0o600, rdev=bsg_rdev)
            return info

        def fake_open(path, flags, *args, **kwargs):
            fd = prev_open(path, flags, *args, **kwargs)
            p = Path(path)
            opens.append((fd, p, flags))
            if p == bsg_path:
                self.assertEqual(self.shim.fixture_set_bsg_fd(fd), 0)
            return fd

        stack.enter_context(mock.patch.object(os, 'fstat', side_effect=fake_fstat))
        stack.enter_context(mock.patch.object(os, 'lstat', side_effect=fake_lstat))
        stack.enter_context(mock.patch.object(os, 'open', side_effect=fake_open))

        active = 'ab'[current]
        part = trust['sources']['partitions'][f'boot_{active}']
        act_bytes = env.images[part['lun']].read_bytes()[
            part['firstLba'] * bs:(part['firstLba'] + part['blockCount']) * bs]
        old_boot_sha = hashlib.sha256(act_bytes).digest()
        new_boot_sha = bytes.fromhex(report['readbackSha256'])

        snap = store.load(journal.fd, journal.part, board)
        record = journal.sample(
            sequence=1,
            transaction_uuid=uuid.UUID(wf.TX_UUID).bytes,
            from_slot=current,
            target_slot=1 - current,
            old_serial=0,
            new_serial=0,
            os_name='armada',
            old_commit=bytes.fromhex('a' * 64),
            new_commit=bytes.fromhex('b' * 64),
            old_boot_sha256=old_boot_sha,
            new_boot_sha256=new_boot_sha,
        )
        store.save(journal.fd, snap, record)

        env.opened.clear()
        env.closed.clear()
        opens.clear()

        return SimpleNamespace(
            env=env, report=report, trust=trust, journal=journal,
            attribute=attribute, bsg=bsg, node=node, class_root=class_root,
            char_root=char_root, opens=opens, board=board, current=current, bs=bs,
        )

    @staticmethod
    def _baseline(case: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(
            disks=[p.read_bytes() for p in case.env.images],
            header=os.pread(case.journal.fd, jf.HEADER_BYTES, 0),
            archive=os.pread(case.journal.fd, 64, jf.HEADER_BYTES),
            region=os.pread(case.journal.fd, jf.REGION_BYTES, jf.REGION_OFFSET),
        )

    def _assert_disks_and_journal(self, case: SimpleNamespace,
                                  base: SimpleNamespace, *,
                                  gpt_changed: bool = True) -> None:
        for old, path in zip(base.disks, case.env.images):
            now = path.read_bytes()
            if not gpt_changed:
                self.assertEqual(now, old)
                continue
            bs = len(old) // pf.BLOCKS
            ab = (2048 + bs - 1) // bs
            p_end = (2 + ab) * bs
            b_start = (pf.BLOCKS - 1 - ab) * bs
            self.assertEqual(now[:bs], old[:bs])
            self.assertEqual(now[p_end:b_start], old[p_end:b_start])
        self.assertEqual(os.pread(case.journal.fd, jf.HEADER_BYTES, 0), base.header)
        self.assertEqual(os.pread(case.journal.fd, 64, jf.HEADER_BYTES), base.archive)
        self.assertEqual(
            os.pread(case.journal.fd, jf.REGION_BYTES, jf.REGION_OFFSET),
            base.region,
        )

    def _assert_all_closed(self, case: SimpleNamespace) -> None:
        self.assertTrue(set(case.env.opened).issubset(set(case.env.closed)))
        for fd, _, _ in case.opens:
            with self.assertRaises(OSError) as ctx:
                pf.REAL_FSTAT(fd)
            self.assertEqual(ctx.exception.errno, errno.EBADF)
        self.assertGreaterEqual(pf.REAL_FSTAT(case.journal.fd).st_size, jf.SIZE)

    @staticmethod
    def _inspect_selected_slot(case: SimpleNamespace) -> int:
        _, found = slots._enumerate_luns(case.env.recovery[3])
        fds = []
        try:
            for lun in sorted(found):
                item = found[lun]
                fds.append(slots._device_fd(item['path'], item['rdev']))
            return slots._run_inspection(fds, found)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_activate_prepared_slot_matrix(self) -> None:
        for board in (1, 2):
            for current in (0, 1):
                for bs in (512, 4096):
                    with self.subTest(board=board, current=current, bs=bs), ExitStack() as stack:
                        case = self._prepare(stack, bs=bs, current=current, board=board)
                        base = self._baseline(case)
                        res = activation.activate_prepared_slot(case.report)
                        from_slot = 'ab'[current]
                        target_idx = 1 - current
                        target_slot = 'ab'[target_idx]
                        self.assertEqual(res, {
                            'schemaVersion': 1,
                            'phase': 'slot_activated',
                            'model': case.trust['physical']['model'],
                            'fromSlot': from_slot,
                            'targetSlot': target_slot,
                            'selectedSlot': target_slot,
                            'transactionUuid': wf.TX_UUID,
                            'deployment': wf.TARGET_DEP,
                            'recordSequence': 1,
                            'readbackSha256': case.report['readbackSha256'],
                        })
                        self.assertEqual(self.shim.fixture_set_calls(), 1)
                        self.assertEqual(
                            case.attribute.read_text(encoding='ascii').strip(),
                            str(target_idx + 1),
                        )
                        self.assertEqual(
                            slots._read_boot_state()[1]['actualSlot'],
                            from_slot,
                        )
                        rw = [p for _, p, fl in case.opens
                              if (fl & os.O_ACCMODE) == os.O_RDWR]
                        ro = [p for _, p, fl in case.opens
                              if (fl & os.O_ACCMODE) != os.O_RDWR]
                        self.assertEqual(len(rw), 7)
                        self.assertEqual(set(rw), set(case.env.images) | {case.bsg})
                        self.assertTrue(all(
                            (fl & os.O_ACCMODE) == os.O_RDONLY
                            for _, _, fl in case.opens
                            if (fl & os.O_ACCMODE) != os.O_RDWR
                        ))
                        self.assertIn( Path(case.journal.path), set(ro))
                        self.assertEqual(self._inspect_selected_slot(case), target_idx)
                        self._assert_disks_and_journal(case, base, gpt_changed=True)
                        self._assert_all_closed(case)


    def test_native_undo_and_uncertain_modes(self) -> None:
        for mode in (1, 2):
            with self.subTest(mode=mode), ExitStack() as stack:
                case = self._prepare(stack, native_mode=mode)
                base = self._baseline(case)
                with self.assertRaises(activation.ActivationError) as ctx:
                    activation.activate_prepared_slot(case.report)
                err = ctx.exception
                if mode == 1:
                    self.assertEqual(err.outcome, 'unchanged')
                    self.assertNotEqual(err.native_result, 0)
                    self.assertNotEqual(err.native_result, -30)
                    self.assertEqual(
                        case.attribute.read_text(encoding='ascii').strip(), '1')
                    self._assert_disks_and_journal(
                        case, base, gpt_changed=False)
                else:
                    self.assertEqual(err.outcome, 'uncertain')
                    self.assertEqual(err.native_result, -30)
                    self._assert_disks_and_journal(
                        case, base, gpt_changed=True)
                self.assertEqual(self.shim.fixture_set_calls(), 2)
                self._assert_all_closed(case)

    def test_preinvoke_state_drift_after_bsg_open(self) -> None:
        with ExitStack() as stack:
            case = self._prepare(stack)
            base = self._baseline(case)
            prev = os.open

            def drift_open(path, flags, *args, **kwargs):
                fd = prev(path, flags, *args, **kwargs)
                if Path(path) == Path(case.bsg) and flags & os.O_RDWR:
                    case.env.context['deployment'] = 'armada/' + ('c' * 64) + '.0'
                return fd

            stack.enter_context(mock.patch.object(
                os, 'open', side_effect=drift_open))
            with self.assertRaises(activation.ActivationError) as ctx:
                activation.activate_prepared_slot(case.report)
            self.assertEqual(ctx.exception.outcome, 'not_attempted')
            self.assertIsNone(ctx.exception.native_result)
            self.assertIn(
                'pre-activation physical slot recheck drifted',
                str(ctx.exception))
            self.assertEqual(self.shim.fixture_calls(), 0)
            self._assert_disks_and_journal(case, base, gpt_changed=False)
            self._assert_all_closed(case)

    def test_state_drift_during_activation(self) -> None:
        with ExitStack() as stack:
            case = self._prepare(stack)
            base = self._baseline(case)
            actual = activation._load_selector()
            calls = []

            def drifting_selector(*args):
                rc = actual(*args)
                calls.append(1)
                case.env.context['deployment'] = 'armada/' + ('c' * 64) + '.0'
                return rc

            stack.enter_context(mock.patch.object(
                activation, '_load_selector', return_value=drifting_selector))
            with self.assertRaises(activation.ActivationError) as ctx:
                activation.activate_prepared_slot(case.report)
            self.assertEqual(ctx.exception.outcome, 'uncertain')
            self.assertEqual(ctx.exception.native_result, 0)
            self.assertIn(
                'boot state drifted during activation', str(ctx.exception))
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.shim.fixture_set_calls(), 1)
            self.assertEqual(
                case.attribute.read_text(encoding='ascii').strip(), '2')
            self._assert_disks_and_journal(case, base, gpt_changed=True)
            self._assert_all_closed(case)

    def test_lun2_open_and_identity_failures(self) -> None:
        for kind in ('open', 'identity'):
            with self.subTest(kind=kind), ExitStack() as stack:
                case = self._prepare(stack)
                target = case.env.images[2]
                base = self._baseline(case)
                prev_open = os.open
                captured = []

                def fail_open(path, flags, *args, **kwargs):
                    rw = flags & os.O_ACCMODE == os.O_RDWR
                    if kind == 'open' and rw and Path(path) == target:
                        raise OSError(errno.EIO, 'fixture EIO')
                    fd = prev_open(path, flags, *args, **kwargs)
                    if kind == 'identity' and rw and Path(path) == target:
                        captured.append(fd)
                    return fd

                stack.enter_context(mock.patch.object(
                    os, 'open', side_effect=fail_open))
                if kind == 'identity':
                    prev_fstat = os.fstat

                    def drift_fstat(fd):
                        info = prev_fstat(fd)
                        if fd in captured:
                            return pf.changed_stat(
                                info, rdev=os.makedev(8, 999))
                        return info

                    stack.enter_context(mock.patch.object(
                        os, 'fstat', side_effect=drift_fstat))
                with self.assertRaises(activation.ActivationError) as ctx:
                    activation.activate_prepared_slot(case.report)
                self.assertEqual(ctx.exception.outcome, 'not_attempted')
                self.assertIsNone(ctx.exception.native_result)
                if kind == 'open':
                    self.assertIn('cannot open LUN2', str(ctx.exception))
                else:
                    self.assertRegex(
                        str(ctx.exception), r'LUN2 .*identity mismatch')
                self.assertEqual(self.shim.fixture_calls(), 0)
                rw = [p for _, p, fl in case.opens
                      if fl & os.O_ACCMODE == os.O_RDWR]
                self.assertEqual(len(rw), 2 if kind == 'open' else 3)
                self.assertNotIn(Path(case.bsg), rw)
                self._assert_disks_and_journal(case, base, gpt_changed=False)
                self._assert_all_closed(case)

    def test_journal_mismatch(self) -> None:
        import dataclasses
        from updates import trial_record as rec
        for kind in ('missing', 'trial', 'wrongtx', 'wronghash'):
            with self.subTest(kind=kind), ExitStack() as stack:
                case = self._prepare(stack)
                if kind == 'missing':
                    for off in (rec.TAIL_OFFSET_A, rec.TAIL_OFFSET_B):
                        os.pwrite(
                            case.journal.fd, b'\0' * 4096,
                            jf.REGION_OFFSET + off)
                else:
                    snap = store.load(
                        case.journal.fd, case.journal.part, case.board)
                    record = snap.record
                    if kind == 'trial':
                        record = dataclasses.replace(
                            record, sequence=record.sequence + 1,
                            state=rec.TRIAL, remaining=2)
                    elif kind == 'wrongtx':
                        record = dataclasses.replace(
                            record, sequence=record.sequence + 1,
                            transaction_uuid=uuid.UUID(
                                '99999999-9999-4999-8999-999999999999').bytes)
                    else:
                        record = dataclasses.replace(
                            record, sequence=record.sequence + 1,
                            new_boot_sha256=bytes.fromhex('c' * 64))
                    store.save(case.journal.fd, snap, record)
                base = self._baseline(case)
                with self.assertRaises(activation.ActivationError) as ctx:
                    activation.activate_prepared_slot(case.report)
                self.assertEqual(ctx.exception.outcome, 'not_attempted')
                self.assertIsNone(ctx.exception.native_result)
                if kind == 'missing':
                    self.assertTrue(str(ctx.exception).strip())
                else:
                    self.assertIn(
                        'match prepared activation', str(ctx.exception))
                self.assertEqual(self.shim.fixture_calls(), 0)
                self.assertEqual([
                    1 for _, _, fl in case.opens
                    if fl & os.O_ACCMODE == os.O_RDWR], [])
                self._assert_disks_and_journal(case, base, gpt_changed=False)
                self._assert_all_closed(case)

    def test_bsg_binding_mismatch(self) -> None:
        for kind in ('controller', 'rdev'):
            with self.subTest(kind=kind), ExitStack() as stack:
                case = self._prepare(stack)
                base = self._baseline(case)
                if kind == 'controller':
                    outside = (
                        case.env.controller.parent
                        / 'other.ufs/host0/ufs-bsg0/bsg/ufs-bsg0')
                    outside.mkdir(parents=True, exist_ok=True)
                    (outside / 'dev').write_text('240:7', encoding='ascii')
                    (case.class_root / 'ufs-bsg0').unlink()
                    (case.class_root / 'ufs-bsg0').symlink_to(outside)
                    (case.char_root / '240:7').unlink()
                    (case.char_root / '240:7').symlink_to(outside)
                else:
                    (case.node / 'dev').write_text('240:8', encoding='ascii')
                    (case.char_root / '240:8').symlink_to(case.node)
                with self.assertRaises(activation.ActivationError) as ctx:
                    activation.activate_prepared_slot(case.report)
                self.assertEqual(ctx.exception.outcome, 'not_attempted')
                self.assertIsNone(ctx.exception.native_result)
                if kind == 'controller':
                    self.assertIn('is not under', str(ctx.exception))
                else:
                    self.assertIn(
                        'not the expected BSG char device',
                        str(ctx.exception))
                self.assertEqual(self.shim.fixture_calls(), 0)
                self.assertEqual([
                    1 for _, _, fl in case.opens
                    if fl & os.O_ACCMODE == os.O_RDWR], [])
                self._assert_disks_and_journal(case, base, gpt_changed=False)
                self._assert_all_closed(case)


if __name__ == '__main__':
    unittest.main()
