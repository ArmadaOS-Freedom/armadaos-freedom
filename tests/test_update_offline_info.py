# SPDX-License-Identifier: GPL-3.0-only
"""Read-only offline info identity while an activated slot awaits its reboot."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))

from updates import offline_session, physical_slots  # noqa: E402

CHECKSUM = 'b' * 64
PART_UUID = '12345678-1234-5678-9abc-123456789abc'
FIXTURE = {
    'model': 'TB322FC', 'actualSlot': 'a', 'inactiveSlot': 'b',
    'deployment': 'armada/' + ('a' * 64) + '.0',
    'recoveryPartUuid': '00000000-0000-4000-8000-000000000000',
}


def booted(serial=2, checksum=CHECKSUM, stateroot='armada'):
    return {'stateroot': stateroot, 'checksum': checksum, 'serial': serial,
            'incompatible': False}


def context_for(record, actual='b'):
    return {'state': 'available', 'actualSlot': actual,
            'inactiveSlot': 'a' if actual == 'b' else 'b',
            'deployment': '%s/%s.%d' % (record['stateroot'], record['checksum'],
                                        record['serial'])}


class DeviceIdentityInfoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / 'offline'
        # The store refuses to exist off-device; storage is never touched here.
        self._install(mock.patch.object(
            offline_session.os, 'geteuid', return_value=0))
        self.record = booted()
        self.snapshot = self._install(mock.patch.object(
            offline_session.armada_os, 'snapshot',
            return_value={'booted': self.record, 'channel': 'beta'}))
        self.boot = self._install(mock.patch.object(
            offline_session.boot_context, 'read_boot_context',
            side_effect=lambda record: context_for(record)))
        self.recovery = self._install(mock.patch.object(
            offline_session.recovery_device, 'locate_recovery_partition',
            return_value=(Path('/dev/armada-backup'), PART_UUID, 1, 2049)))

    def _install(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def store(self, **kwargs):
        return offline_session.OfflineSessionStore(base=self.base, **kwargs)

    def _strict_inspector(self):
        return self._install(mock.patch.object(
            physical_slots, 'inspect_physical_slots',
            side_effect=physical_slots.PhysicalSlotsError(
                'actual boot != next selected slot; restart or repair required')))

    def test_info_describes_the_booted_system_while_a_switch_is_pending(self):
        strict = self._strict_inspector()
        info = self.store().info()
        strict.assert_not_called()
        self.assertEqual(info, {
            'schemaVersion': 1,
            'updaterVersion': '0.8.0',
            'model': 'TB322FC',
            'actualSlot': 'b',
            'inactiveSlot': 'a',
            'deployment': 'armada/%s.2' % CHECKSUM,
            'recoveryPartUuid': PART_UUID,
            'channel': 'beta',
        })
        self.boot.assert_called_once_with(self.record)
        self.recovery.assert_called_once_with(require_edl=False)

    def test_identity_reader_reports_only_verified_fields(self):
        self.assertEqual(offline_session.read_device_identity(), {
            'model': 'TB322FC', 'actualSlot': 'b', 'inactiveSlot': 'a',
            'deployment': 'armada/%s.2' % CHECKSUM,
            'recoveryPartUuid': PART_UUID,
        })

    def test_info_uses_the_verified_board_for_the_model(self):
        for board, model in ((1, 'TB322FC'), (2, 'TB323FU')):
            with self.subTest(board=board):
                self.recovery.return_value = (
                    Path('/dev/armada-backup'), PART_UUID, board, 2049)
                self.assertEqual(self.store().info()['model'], model)

    def test_info_rejects_a_snapshot_without_a_booted_deployment(self):
        self.snapshot.return_value = {'channel': 'beta'}
        with self.assertRaisesRegex(offline_session.OfflineSessionError, 'booted'):
            self.store().info()

    def test_info_rejects_unverifiable_boot_context(self):
        self.boot.side_effect = offline_session.boot_context.BootContextError(
            'boot witness must be exactly 16 raw bytes')
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'verify the booted slot'):
            self.store().info()

    def test_info_rejects_a_context_that_disagrees_with_the_booted_record(self):
        self.boot.side_effect = lambda record: dict(
            context_for(record), deployment='armada/%s.9' % ('c' * 64))
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'booted deployment'):
            self.store().info()

    def test_info_rejects_inconsistent_slots(self):
        self.boot.side_effect = lambda record: {
            'actualSlot': 'a', 'inactiveSlot': 'a',
            'deployment': context_for(record)['deployment']}
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'invalid slots'):
            self.store().info()

    def test_info_rejects_an_incomplete_booted_record(self):
        self.record['serial'] = '2'
        self.boot.side_effect = None
        self.boot.return_value = {'actualSlot': 'b', 'inactiveSlot': 'a',
                                  'deployment': 'ignored'}
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'incomplete'):
            self.store().info()

    def test_info_rejects_an_unverified_recovery_partition(self):
        self.recovery.side_effect = \
            offline_session.recovery_device.RecoveryLayoutError('Unsupported board')
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'recovery partition'):
            self.store().info()

    def test_info_rejects_unsupported_board_or_partuuid(self):
        self.recovery.return_value = (Path('/dev/armada-backup'), PART_UUID, 3, 2049)
        with self.assertRaisesRegex(offline_session.OfflineSessionError,
                                    'TB322FC/TB323FU'):
            self.store().info()
        self.recovery.return_value = (Path('/dev/armada-backup'), '', 1, 2049)
        with self.assertRaisesRegex(offline_session.OfflineSessionError, 'PARTUUID'):
            self.store().info()

    def test_begin_still_requires_the_strict_physical_precheck(self):
        strict = mock.Mock(side_effect=physical_slots.PhysicalSlotsError(
            'actual boot != next selected slot; restart or repair required'))
        store = self.store(inspect=strict)
        with self.assertRaises(physical_slots.PhysicalSlotsError):
            store.begin(b'{}', b'signature')
        strict.assert_called_once_with()
        self.boot.assert_not_called()
        self.recovery.assert_not_called()

    def test_injected_inspector_still_supplies_the_info_fixture(self):
        info = self.store(inspect=lambda: dict(FIXTURE)).info()
        self.assertEqual(info['model'], FIXTURE['model'])
        self.assertEqual(info['actualSlot'], FIXTURE['actualSlot'])
        self.assertEqual(info['deployment'], FIXTURE['deployment'])
        self.assertEqual(info['recoveryPartUuid'], FIXTURE['recoveryPartUuid'])
        self.boot.assert_not_called()
        self.recovery.assert_not_called()

    def test_explicit_info_reader_overrides_an_injected_inspector(self):
        strict = mock.Mock(side_effect=physical_slots.PhysicalSlotsError(
            'actual boot != next selected slot; restart or repair required'))
        info = self.store(inspect=strict, info_reader=lambda: dict(FIXTURE)).info()
        self.assertEqual(info['actualSlot'], 'a')
        self.assertEqual(info['deployment'], FIXTURE['deployment'])
        strict.assert_not_called()

    def test_info_rejects_injected_evidence_without_every_field(self):
        for field in ('actualSlot', 'inactiveSlot', 'deployment', 'recoveryPartUuid'):
            with self.subTest(field=field):
                fixture = dict(FIXTURE, **{field: ''})
                with self.assertRaises(offline_session.OfflineSessionError):
                    self.store(inspect=lambda: dict(fixture)).info()
        missing = dict(FIXTURE)
        del missing['recoveryPartUuid']
        with self.assertRaises(offline_session.OfflineSessionError):
            self.store(inspect=lambda: dict(missing)).info()


if __name__ == '__main__':
    unittest.main()
