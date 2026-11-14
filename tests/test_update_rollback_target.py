# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'system_files/usr/lib/armada'))

from updates import deployment_store, system_transaction, trial_record


OLD = '11' * 32
NEW = '22' * 32
OTHER = '33' * 32


def confirmed(state=trial_record.CONFIRMED):
    return trial_record.TrialRecord(
        sequence=1, partition_uuid=b'p' * 16, installation_uuid=b'i' * 16,
        transaction_uuid=b't' * 16, state=state, from_slot=1, target_slot=0,
        remaining=2, attempt_limit=3, old_serial=7, new_serial=8,
        os_name='armada', old_commit=bytes.fromhex(OLD),
        new_commit=bytes.fromhex(NEW), old_boot_sha256=b'a' * 32,
        new_boot_sha256=b'b' * 32, plan_sha256=b'c' * 32)


def snapshot(os_name='armada'):
    return {'repository': next(iter(system_transaction.armada_os.REPOSITORIES)),
            'staged': False, 'rollback_queued': False, 'channel': 'stable',
            'rollback': {'incompatible': False, 'checksum': OLD, 'serial': 7,
                         'stateroot': os_name, 'version': '1.2.3'}}


class RollbackTargetTests(unittest.TestCase):
    def validate(self, snap=None, record=None, checksum=OLD, old=('armada', NEW, 8)):
        if snap is None:
            snap = snapshot()
        if record is None:
            record = confirmed()
        return system_transaction._validate_rollback_target(snap, checksum, old, record)

    def reject(self, name, mutate, record=None, old=None):
        snap = snapshot()
        mutate(snap)
        with self.subTest(name), self.assertRaises(
                (ValueError, deployment_store.DeploymentError)):
            self.validate(snap, record=record, old=old or ('armada', NEW, 8))

    def test_valid_os_forms_and_optional_version(self):
        for os_name in ('armada', 'default'):
            with self.subTest(os_name):
                record = confirmed()
                if os_name != 'armada':
                    record = trial_record.TrialRecord(**{**record.__dict__, 'os_name': os_name})
                deployment, target, version = self.validate(
                    snapshot(os_name), record, old=(os_name, NEW, 8))
                self.assertEqual(deployment, '%s/%s.7' % (os_name, OLD))
                self.assertEqual(target, (os_name, OLD, 7))
                self.assertEqual(version, '1.2.3')
        snap = snapshot()
        del snap['rollback']['version']
        self.assertIsNone(self.validate(snap)[2])
        snap['rollback']['version'] = ''
        self.assertIsNone(self.validate(snap)[2])

    def test_checksum_and_rollback_rejections(self):
        for checksum in ('0' * 63, 'A' * 64, 'g' * 64, 1, None):
            with self.subTest(checksum=checksum), self.assertRaises(ValueError):
                self.validate(checksum=checksum)
        self.reject('stale', lambda s: s['rollback'].update(checksum=OTHER))
        for value in (None, True, 0, 'false'):
            def change(s, value=value):
                if value is None:
                    s['rollback'].pop('incompatible')
                else:
                    s['rollback']['incompatible'] = value
            self.reject('incompatible', change)
        self.reject('not-dict', lambda s: s.update(rollback=None))

    def test_serial_os_commit_and_journal_rejections(self):
        for serial in (True, False, 7.0, -1, 2 ** 32, '7', None):
            self.reject('serial', lambda s, v=serial: s['rollback'].update(serial=v))
        self.reject('bad-os', lambda s: s['rollback'].update(stateroot='bad/os'))
        self.reject('empty-os', lambda s: s['rollback'].update(stateroot=''))
        self.reject('cross-os', lambda s: s['rollback'].update(stateroot='default'))
        self.reject('same-commit', lambda s: None, old=('armada', OLD, 8))
        for state in (None, trial_record.TRIAL, trial_record.ROLLED_BACK):
            with self.subTest(state=state), self.assertRaises(ValueError):
                if state is None:
                    system_transaction._validate_rollback_target(
                        snapshot(), OLD, ('armada', NEW, 8), None)
                else:
                    self.validate(record=confirmed(state))
        for field, value in (('old_serial', 6), ('old_commit', bytes.fromhex(OTHER)),
                             ('os_name', 'default')):
            base = confirmed()
            record = trial_record.TrialRecord(**{**base.__dict__, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(record=record)

    def test_recheck_accepts_and_rejects_drift(self):
        target = ('armada', OLD, 7)
        with mock.patch.object(system_transaction.armada_os, 'snapshot', return_value=snapshot()):
            self.assertIsNone(system_transaction._recheck_rollback_snapshot(OLD, target))
        cases = {
            'repository': lambda s: s.update(repository='invalid'),
            'staged': lambda s: s.update(staged=True),
            'rollback_queued': lambda s: s.update(rollback_queued=True),
            'checksum': lambda s: s['rollback'].update(checksum=OTHER),
            'serial': lambda s: s['rollback'].update(serial=8),
            'float-serial': lambda s: s['rollback'].update(serial=7.0),
            'bool-serial': lambda s: s['rollback'].update(serial=True),
            'stateroot': lambda s: s['rollback'].update(stateroot='default'),
            'incompatible': lambda s: s['rollback'].update(incompatible=True),
        }
        for name, mutate in cases.items():
            snap = snapshot()
            mutate(snap)
            with self.subTest(name), mock.patch.object(
                    system_transaction.armada_os, 'snapshot', return_value=snap), \
                    self.assertRaises(ValueError):
                system_transaction._recheck_rollback_snapshot(OLD, target)

    def test_run_rollback_preflight_rejects_empty_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sysroot').mkdir()
            work = root / 'work'
            lock_path = root / 'update.lock'
            initial = {'model': 'TB322FC', 'actualSlot': 'a', 'inactiveSlot': 'b',
                       'recoveryPartUuid': 'PARTUUID', 'deployment': 'armada/%s.8' % NEW}
            opened = mock.MagicMock()
            opened.__enter__.return_value = (3, 'PARTUUID', 'board')
            opened.__exit__.return_value = False
            with mock.patch.object(system_transaction.trial_confirmation, 'UPDATE_LOCK_PATH', lock_path), \
                    mock.patch.object(system_transaction.physical_slots, 'inspect_physical_slots',
                                      return_value=initial), \
                    mock.patch.object(system_transaction.armada_os, 'snapshot', return_value=snapshot()), \
                    mock.patch.object(system_transaction.boot_materials, '_validate_output_dir'), \
                    mock.patch.object(system_transaction.recovery_device, 'open_recovery',
                                      return_value=opened), \
                    mock.patch.object(system_transaction.trial_store, 'load',
                                      return_value=SimpleNamespace(record=None)), \
                    mock.patch.object(system_transaction.physical_slots, '_MODEL', {'board': 'TB322FC'}), \
                    self.assertRaises(system_transaction.SystemTransactionError) as caught:
                system_transaction.run_rollback_transaction(
                    expected_checksum=OLD, sysroot_path=root / 'sysroot', work_dir=work)
            self.assertEqual(caught.exception.result['state'], 'failed')
            self.assertIn('previous bound deployment unavailable', caught.exception.result['error'])
            self.assertFalse(work.exists())


if __name__ == '__main__':
    unittest.main()
