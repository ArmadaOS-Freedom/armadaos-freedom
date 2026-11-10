"""Focused offline tests for resolve_recovery_boot_sources."""

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_SOURCE_ROOT = (Path(__file__).resolve().parents[1] / 'system_files' /
                'usr' / 'lib' / 'armada')
sys.path.insert(0, str(_SOURCE_ROOT))

from updates import recovery_boot_sources  # noqa: E402
from updates.recovery_boot_sources import (  # noqa: E402
    RecoveryBootSourcesError,
    resolve_recovery_boot_sources,
)

_SLOT_KEYS = ('boot', 'vendorBoot', 'initBoot', 'abl', 'xblConfig', 'vbmeta')


def _write(path, data):
    path.write_bytes(data)
    return {'path': str(path), 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest()}


class RecoveryBootSourcesTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix='rbs-test-', dir=os.environ.get('TMPDIR'))
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

    def _build(self, actual):
        """Build synthetic snapshot, originals, and expected hashes."""
        d = Path(tempfile.mkdtemp(prefix=f'case-{actual}-', dir=self.base))
        original_bytes = {
            'a': (b'ORIGINAL-BOOT-A' * 4096)[:4096],
            'b': (b'ORIGINAL-BOOT-B-DISTINCT' * 4096)[:4096],
        }
        temp_bytes = (b'TEMPORARY-RECOVERY-BOOT' * 4096)[:4096]
        originals = {}
        for slot in ('a', 'b'):
            originals[slot] = _write(d / f'original-{slot}.img',
                                     original_bytes[slot])
        slots = {'a': {}, 'b': {}}
        for slot in ('a', 'b'):
            for key in _SLOT_KEYS:
                if key == 'boot':
                    data = temp_bytes if slot == actual else original_bytes[slot]
                else:
                    data = f'live-{key}-{slot}'.encode() * 40
                slots[slot][key] = _write(d / f'live-{slot}-{key}.img', data)
        extras = {
            'recovery_a': _write(d / 'recovery_a.img', b'extra-recovery-a' * 20),
            'efisp': _write(d / 'efisp.img', b'extra-efisp' * 20),
        }
        partitions = {}
        partition_index = 0

        def add_partition(name, lun, block_count):
            nonlocal partition_index
            partition_index += 1
            partitions[name] = {
                'lun': lun,
                'firstLba': 2048 + partition_index * 16,
                'blockCount': block_count,
                'partitionUuid': (
                    f'{partition_index:08x}-1234-4abc-8def-'
                    f'{partition_index:012x}'),
            }

        for slot in ('a', 'b'):
            for key in _SLOT_KEYS:
                label = {
                    'vendorBoot': 'vendor_boot', 'initBoot': 'init_boot',
                    'xblConfig': 'xbl_config',
                }.get(key, key)
                add_partition(f'{label}_{slot}', 1, 8)
        for name in extras:
            add_partition(name, 2, 4)
        physical = {
            'schemaVersion': 1,
            'phase': 'recovery_slots_checked',
            'model': 'TB322FC',
            'actualSlot': actual,
            'inactiveSlot': 'b' if actual == 'a' else 'a',
        }
        snapshot = {
            'schemaVersion': 1,
            'phase': 'recovery_boot_sources_captured',
            'physical': physical,
            'slots': slots,
            'extraFiles': extras,
            'partitions': partitions,
        }
        expected_original = {
            slot: originals[slot]['sha256'] for slot in ('a', 'b')}
        temporary_sha = hashlib.sha256(temp_bytes).hexdigest()
        return snapshot, originals, expected_original, temporary_sha

    def _resolve(self, actual, inspector=None):
        snapshot, originals, expected_original, temporary_sha = \
            self._build(actual)
        inputs_before = copy.deepcopy(
            (snapshot, originals, expected_original, temporary_sha))
        paths = [originals[slot]['path'] for slot in ('a', 'b')]
        paths.extend(
            snapshot['slots'][slot][key]['path']
            for slot in ('a', 'b') for key in _SLOT_KEYS)
        paths.extend(claim['path']
                     for claim in snapshot['extraFiles'].values())
        files_before = {
            path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in paths
        }
        physical = snapshot['physical']
        if inspector is None:
            inspector = [copy.deepcopy(physical), copy.deepcopy(physical)]
        with patch.object(recovery_boot_sources.physical_slots,
                          'inspect_recovery_slots',
                          side_effect=list(inspector)) as inspect_mock:
            result = resolve_recovery_boot_sources(
                snapshot, originals,
                expected_original_sha256=copy.deepcopy(expected_original),
                temporary_boot_sha256=temporary_sha)
        self.assertEqual(inspect_mock.call_count, 2)
        return (result, snapshot, originals, expected_original, temporary_sha,
                inputs_before, files_before)

    def test_happy_path_a_and_b(self):
        for actual in ('a', 'b'):
            with self.subTest(actual=actual):
                (result, snapshot, originals, expected_original, temp_sha,
                 inputs_before, files_before) = self._resolve(actual)
                self.assertEqual(result['schemaVersion'], 1)
                self.assertEqual(result['phase'],
                                 'recovery_boot_sources_resolved')
                self.assertEqual(result['physical'], snapshot['physical'])
                self.assertEqual(result['partitions'], snapshot['partitions'])
                inactive = 'b' if actual == 'a' else 'a'
                for slot in ('a', 'b'):
                    self.assertEqual(result['originalBoots'][slot],
                                     originals[slot])
                    self.assertEqual(result['slots'][slot]['boot'],
                                     originals[slot])
                    for key in _SLOT_KEYS:
                        if key != 'boot':
                            self.assertEqual(
                                result['slots'][slot][key],
                                snapshot['slots'][slot][key])
                self.assertEqual(result['liveBoots'][actual]['sha256'],
                                 temp_sha)
                self.assertEqual(result['liveBoots'][inactive]['sha256'],
                                 expected_original[inactive])
                self.assertNotEqual(result['liveBoots'][actual]['sha256'],
                                    result['originalBoots'][actual]['sha256'])
                self.assertEqual(result['liveBoots'][inactive]['sha256'],
                                 result['originalBoots'][inactive]['sha256'])

                # Inputs and their files remain unchanged.
                self.assertEqual(
                    (snapshot, originals, expected_original, temp_sha),
                    inputs_before)
                self.assertEqual({
                    path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    for path in files_before
                }, files_before)

                # Mutating outputs cannot alias any supplied input.
                result['slots']['a']['boot']['sha256'] = 'mutated'
                self.assertNotEqual(originals['a']['sha256'], 'mutated')
                result['liveBoots'][actual]['sha256'] = 'mutated'
                self.assertNotEqual(
                    snapshot['slots'][actual]['boot']['sha256'], 'mutated')
                result['extraFiles']['efisp']['sha256'] = 'mutated'
                self.assertNotEqual(
                    snapshot['extraFiles']['efisp']['sha256'], 'mutated')

    def test_hash_and_size_mismatches(self):
        def expect_failure(snap, orig, exp, temp, pattern):
            with patch.object(
                    recovery_boot_sources.physical_slots,
                    'inspect_recovery_slots',
                    return_value=copy.deepcopy(snap['physical'])):
                with self.assertRaisesRegex(RecoveryBootSourcesError, pattern):
                    resolve_recovery_boot_sources(
                        snap, orig, expected_original_sha256=exp,
                        temporary_boot_sha256=temp)

        snapshot, originals, expected, temp_sha = self._build('a')
        bad = copy.deepcopy(expected)
        bad['a'] = 'f' * 64
        expect_failure(snapshot, originals, bad, temp_sha,
                       'original boot a hash does not equal')

        swapped = {'a': originals['b'], 'b': originals['a']}
        expect_failure(snapshot, swapped, expected, temp_sha,
                       'original boot a hash does not equal')
        expect_failure(snapshot, originals, expected, 'e' * 64,
                       'live boot on actual slot a does not match')

        # The rehashed claim remains valid for its modified, same-size file.
        snap2, orig2, exp2, temp2 = self._build('a')
        inactive_path = Path(snap2['slots']['b']['boot']['path'])
        modified = (b'TAMPERED-INACTIVE' * 4096)[:4096]
        inactive_path.write_bytes(modified)
        snap2['slots']['b']['boot']['bytes'] = len(modified)
        snap2['slots']['b']['boot']['sha256'] = \
            hashlib.sha256(modified).hexdigest()
        expect_failure(snap2, orig2, exp2, temp2,
                       'live boot on inactive slot b does not match')

        # The independently updated expected hash passes hashing, then size
        # validation rejects the grown original.
        snap3, orig3, exp3, temp3 = self._build('a')
        grown = b'G' * 4097
        grown_path = Path(orig3['a']['path']).with_name('grown.img')
        orig3['a']['path'] = str(grown_path)
        grown_path.write_bytes(grown)
        orig3['a']['bytes'] = len(grown)
        orig3['a']['sha256'] = hashlib.sha256(grown).hexdigest()
        exp3['a'] = orig3['a']['sha256']
        expect_failure(snap3, orig3, exp3, temp3,
                       'original boot a size does not equal live boot a size')

    def test_invalid_snapshots_and_claims(self):
        snapshot, originals, expected, temp_sha = self._build('a')
        inspector = [copy.deepcopy(snapshot['physical'])] * 2

        def run(snap, pattern, exp=None):
            supplied_expected = (
                copy.deepcopy(expected) if exp is None else exp)
            with patch.object(recovery_boot_sources.physical_slots,
                              'inspect_recovery_slots',
                              side_effect=list(inspector)):
                with self.assertRaisesRegex(RecoveryBootSourcesError, pattern):
                    resolve_recovery_boot_sources(
                        snap, copy.deepcopy(originals),
                        expected_original_sha256=supplied_expected,
                        temporary_boot_sha256=temp_sha)

        for bad_version in (True, 1.0):
            bad_live_schema = copy.deepcopy(snapshot)
            bad_live_schema['schemaVersion'] = bad_version
            run(bad_live_schema, 'live_snapshot schemaVersion must be 1')
            bad_physical_schema = copy.deepcopy(snapshot)
            bad_physical_schema['physical']['schemaVersion'] = bad_version
            run(bad_physical_schema,
                'physical must be a recovery_slots_checked report')
        run(snapshot, 'expected_original_sha256 must have exact keys', exp={})

        bad_phase = copy.deepcopy(snapshot)
        bad_phase['phase'] = 'boot_sources_captured'
        run(bad_phase, 'live_snapshot phase must be')
        missing_key = copy.deepcopy(snapshot)
        del missing_key['slots']['a']['vbmeta']
        run(missing_key, r'live_snapshot\.slots\[a\] must have exact keys')
        extra_key = copy.deepcopy(snapshot)
        extra_key['slots']['b']['unexpected'] = extra_key['slots']['b']['abl']
        run(extra_key, r'live_snapshot\.slots\[b\] must have exact keys')
        deployment = copy.deepcopy(snapshot)
        deployment['physical']['deployment'] = {'id': 'x'}
        run(deployment, 'must not contain deployment or booted')
        bad_model = copy.deepcopy(snapshot)
        bad_model['physical']['model'] = 'TB999XX'
        run(bad_model, 'unsupported model')
        tampered = copy.deepcopy(snapshot)
        tampered['slots']['a']['abl']['sha256'] = 'd' * 64
        run(tampered, r'live_snapshot\.slots\[a\]\[abl\] content mismatch')
        truncated = copy.deepcopy(snapshot)
        truncated['extraFiles']['efisp']['bytes'] = 4
        truncated['extraFiles']['efisp']['sha256'] = 'a' * 64
        run(truncated, r'live_snapshot\.extraFiles\[efisp\] content mismatch')
        symlink_dir = self.base / 'symlink'
        symlink_dir.mkdir()
        target = symlink_dir / 'target.img'
        target.write_bytes(b'x' * 64)
        link = symlink_dir / 'link.img'
        link.symlink_to(target)
        symlinked = copy.deepcopy(snapshot)
        symlinked['slots']['b']['boot'] = {
            'path': str(link), 'bytes': 64,
            'sha256': hashlib.sha256(b'x' * 64).hexdigest()}
        run(symlinked, 'symlink|regular file')
        bad_extras = copy.deepcopy(snapshot)
        bad_extras['extraFiles']['unexpected_extra'] = \
            bad_extras['extraFiles']['efisp']
        bad_extras['partitions']['unexpected_extra'] = {}
        run(bad_extras, 'extraFiles entry not permitted')

    def test_physical_drift_rejected(self):
        snapshot, originals, expected, temp_sha = self._build('a')
        drifted = copy.deepcopy(snapshot['physical'])
        drifted['model'] = 'TB323FU'
        # Drift before hashing.
        with patch.object(recovery_boot_sources.physical_slots,
                          'inspect_recovery_slots',
                          return_value=drifted):
            with self.assertRaisesRegex(
                    RecoveryBootSourcesError,
                    'physical recovery slot report drifted before hashing'):
                resolve_recovery_boot_sources(
                    snapshot, copy.deepcopy(originals),
                    expected_original_sha256=copy.deepcopy(expected),
                    temporary_boot_sha256=temp_sha)
        # Drift between hashing and return.
        with patch.object(recovery_boot_sources.physical_slots,
                          'inspect_recovery_slots',
                          side_effect=[copy.deepcopy(snapshot['physical']),
                                       drifted]):
            with self.assertRaisesRegex(
                    RecoveryBootSourcesError,
                    'physical recovery slot report drifted before return'):
                resolve_recovery_boot_sources(
                    snapshot, copy.deepcopy(originals),
                    expected_original_sha256=copy.deepcopy(expected),
                    temporary_boot_sha256=temp_sha)


if __name__ == '__main__':
    unittest.main()
