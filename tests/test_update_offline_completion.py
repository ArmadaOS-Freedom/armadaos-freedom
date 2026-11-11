# SPDX-License-Identifier: GPL-3.0-only
"""Normal-completion binding: session receipt versus live trial evidence."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))
from updates import offline_completion  # noqa: E402

TX = '11111111-2222-4333-8444-555555555555'
OTHER_TX = '99999999-8888-4777-8666-555555555555'
LETTERED_TX = 'abcdef01-2345-4abc-8def-0123456789ab'
OLD = 'armada/' + ('a' * 64) + '.0'
NEW = 'armada/' + ('b' * 64) + '.1'
SHA = 'c' * 64
OTHER_SHA = 'd' * 64


def receipt(**overrides):
    body = {
        'state': 'awaiting_reboot',
        'transactionId': TX,
        'oldDeployment': OLD,
        'newDeployment': NEW,
        'fromSlot': 'a',
        'targetSlot': 'b',
        'planSha256': SHA,
    }
    body.update(overrides)
    return {'state': 'awaiting_reboot', 'result': body}


def trial(**overrides):
    body = {
        'state': 'available',
        'transactionId': TX,
        'oldDeployment': OLD,
        'newDeployment': NEW,
        'fromSlot': 'a',
        'targetSlot': 'b',
        'planSha256': SHA,
        'recordState': 'prepared',
        'runningDeployment': 'old',
        'bootConfirmed': False,
        'finalization': {'state': 'pending', 'error': 'not booted in'},
    }
    body.update(overrides)
    return body


class IdentityTests(unittest.TestCase):
    def test_session_identity_requires_every_binding(self):
        self.assertEqual(offline_completion.session_identity(receipt())['transactionId'], TX)
        for field in ('transactionId', 'oldDeployment', 'newDeployment',
                      'fromSlot', 'targetSlot', 'planSha256'):
            with self.subTest(field=field):
                self.assertIsNone(
                    offline_completion.session_identity(receipt(**{field: None})))
        self.assertIsNone(offline_completion.session_identity(
            receipt(fromSlot='b', targetSlot='b')))
        self.assertEqual(
            offline_completion.session_identity(
                receipt(transactionId=LETTERED_TX))['transactionId'],
            LETTERED_TX)
        self.assertIsNone(offline_completion.session_identity(
            receipt(transactionId=LETTERED_TX.upper())))
        self.assertIsNone(offline_completion.session_identity(
            receipt(newDeployment='armada/' + ('B' * 64) + '.1')))
        self.assertIsNone(offline_completion.session_identity(None))

    def test_document_identity_checks_plan_digest_and_transaction(self):
        plan = {'transactionId': TX, 'oldDeployment': OLD, 'newDeployment': NEW,
                'fromSlot': 'a', 'targetSlot': 'b'}
        prepared = {'transactionId': TX, 'planSha256': SHA}
        identity = offline_completion.document_identity(prepared, plan, SHA)
        self.assertEqual(identity,
                         {'transactionId': TX, 'oldDeployment': OLD,
                          'newDeployment': NEW, 'fromSlot': 'a',
                          'targetSlot': 'b', 'planSha256': SHA})
        self.assertIsNone(offline_completion.document_identity(prepared, plan, OTHER_SHA))
        self.assertIsNone(offline_completion.document_identity(
            dict(prepared, planSha256=OTHER_SHA), plan, SHA))
        self.assertIsNone(offline_completion.document_identity(
            prepared, dict(plan, transactionId=OTHER_TX)))
        self.assertIsNone(offline_completion.document_identity(prepared, None))
        self.assertIsNone(offline_completion.document_identity(None, plan))


class VerdictTests(unittest.TestCase):
    def test_missing_identity_is_unverified_and_never_restartable(self):
        verdict = offline_completion.evaluate({'state': 'awaiting_reboot'}, trial())
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'session_identity_missing')
        self.assertFalse(verdict['restartEligible'])

    def test_confirmed_requires_trial_boot_and_finalization_to_agree(self):
        verdict = offline_completion.evaluate(
            receipt(),
            trial(recordState='confirmed', runningDeployment='new',
                  bootConfirmed=True, finalization={'state': 'complete'}))
        self.assertEqual(verdict['state'], 'confirmed')
        self.assertEqual(verdict['transactionId'], TX)
        self.assertEqual(verdict['deployment'], NEW)
        self.assertEqual(verdict['recordState'], 'confirmed')
        self.assertTrue(verdict['bootConfirmed'])
        self.assertFalse(verdict['restartEligible'])

    def test_confirmed_with_missing_finalization_receipt_is_finalizing(self):
        for finalization in ({'state': 'pending', 'error': 'busy'},
                             {'state': 'pending'},
                             None,
                             'complete'):
            with self.subTest(finalization=finalization):
                verdict = offline_completion.evaluate(
                    receipt(),
                    trial(recordState='confirmed', runningDeployment='new',
                          bootConfirmed=True, finalization=finalization))
                self.assertEqual(verdict['state'], 'finalizing')
                self.assertFalse(verdict['restartEligible'])

    def test_confirmed_without_boot_evidence_stays_unverified(self):
        verdict = offline_completion.evaluate(
            receipt(),
            trial(recordState='confirmed', runningDeployment='new',
                  bootConfirmed=False, bootEvidenceError='witness missing',
                  finalization={'state': 'complete'}))
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'boot_evidence_missing')
        self.assertEqual(verdict['error'], 'witness missing')

    def test_unknown_running_deployment_is_missing_evidence(self):
        for record_state in ('confirmed', 'rolled_back'):
            with self.subTest(record_state=record_state):
                verdict = offline_completion.evaluate(
                    receipt(),
                    trial(recordState=record_state, runningDeployment=None,
                          bootConfirmed=False,
                          bootEvidenceError='transaction marker mismatch'))
                self.assertEqual(verdict['state'], 'unverified')
                self.assertEqual(verdict['reason'], 'boot_evidence_missing')
                self.assertEqual(verdict['error'], 'transaction marker mismatch')
        self.assertEqual(
            offline_completion.evaluate(
                receipt(), trial(recordState='rolled_back',
                                 runningDeployment='new'))['state'],
            'mismatch')

    def test_different_transaction_or_plan_is_a_mismatch(self):
        cases = (
            ('transactionId', OTHER_TX),
            ('oldDeployment', 'armada/' + ('1' * 64) + '.0'),
            ('newDeployment', 'armada/' + ('2' * 64) + '.1'),
            ('fromSlot', 'b'),
            ('targetSlot', 'a'),
        )
        for field, value in cases:
            with self.subTest(field=field):
                verdict = offline_completion.evaluate(
                    receipt(),
                    trial(recordState='confirmed', runningDeployment='new',
                          bootConfirmed=True, finalization={'state': 'complete'},
                          **{field: value}))
                self.assertEqual(verdict['state'], 'mismatch')
                self.assertEqual(verdict['reason'], 'transaction_mismatch')
                self.assertFalse(verdict['restartEligible'])

        verdict = offline_completion.evaluate(
            receipt(),
            trial(recordState='confirmed', runningDeployment='new',
                  bootConfirmed=True, finalization={'state': 'complete'},
                  planSha256=OTHER_SHA))
        self.assertEqual(verdict['state'], 'mismatch')
        self.assertEqual(verdict['reason'], 'plan_mismatch')

    def test_newer_latest_transaction_is_not_accepted_as_this_session(self):
        verdict = offline_completion.evaluate(
            receipt(),
            trial(transactionId=OTHER_TX, recordState='confirmed',
                  runningDeployment='new', bootConfirmed=True,
                  finalization={'state': 'complete'}))
        self.assertEqual(verdict['state'], 'mismatch')
        self.assertEqual(verdict['transactionId'], TX)

    def test_rollback_and_terminal_failure(self):
        rolled = offline_completion.evaluate(
            receipt(),
            trial(recordState='rolled_back', runningDeployment='old',
                  bootConfirmed=True))
        self.assertEqual(rolled['state'], 'rolled_back')
        self.assertFalse(rolled['restartEligible'])

        failed = offline_completion.evaluate(
            receipt(state='recovery_required'), trial())
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['reason'], 'recovery_required')

    def test_awaiting_reboot_and_restart_eligibility(self):
        eligible = offline_completion.evaluate(receipt(), trial())
        self.assertEqual(eligible['state'], 'awaiting_reboot')
        self.assertTrue(eligible['restartEligible'])
        self.assertIsNone(eligible['restartState'])

        uncertain = offline_completion.evaluate(
            receipt(state='execution_uncertain'), trial())
        self.assertEqual(uncertain['state'], 'awaiting_reboot')
        self.assertFalse(uncertain['restartEligible'])
        self.assertEqual(uncertain['reason'], 'execution_uncertain')

        already_new = offline_completion.evaluate(
            receipt(), trial(runningDeployment='new'))
        self.assertEqual(already_new['state'], 'awaiting_confirmation')
        self.assertFalse(already_new['restartEligible'])

    def test_restart_state_only_mirrors_this_transaction(self):
        for state in ('dispatching', 'dispatched', 'uncertain', 'failed'):
            with self.subTest(state=state):
                value = offline_completion.evaluate(
                    dict(receipt(), restart={'schemaVersion': 1, 'state': state,
                                             'transactionId': TX}), trial())
                self.assertEqual(value['restartState'], state)
        wrong = offline_completion.evaluate(
            dict(receipt(), restart={'schemaVersion': 1, 'state': 'dispatched',
                                     'transactionId': OTHER_TX}), trial())
        self.assertIsNone(wrong['restartState'])
        bogus = offline_completion.evaluate(
            dict(receipt(), restart={'schemaVersion': 1, 'state': 'rebooting',
                                     'transactionId': TX}), trial())
        self.assertIsNone(bogus['restartState'])

    def test_unavailable_trial_degrades_instead_of_confirming(self):
        for value in ({'state': 'unavailable', 'error': 'no witness'},
                      {'state': 'empty'}, None, 'available'):
            with self.subTest(trial=value):
                verdict = offline_completion.evaluate(receipt(), value)
                self.assertEqual(verdict['state'], 'unverified')
                self.assertEqual(verdict['reason'], 'trial_unavailable')


BOOT_ID = '0e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b'
PHYSICAL = {
    'model': 'TB322FC', 'actualSlot': None, 'inactiveSlot': None,
    'selectedSlot': 'a', 'bootContext': 'legacy-controller',
    'deployment': OLD, 'recoveryPartUuid':
    '12345678-1234-5678-9abc-123456789abc',
}


def legacy_receipt(**overrides):
    body = {
        'state': 'awaiting_reboot',
        'transactionId': TX,
        'oldDeployment': OLD,
        'newDeployment': NEW,
        'fromSlot': 'a',
        'targetSlot': 'b',
        'planSha256': SHA,
    }
    body.update(overrides)
    return {'state': 'awaiting_reboot', 'physical': dict(PHYSICAL), 'result': body}


ORIGINAL_BOOT = {'a': '3' * 64, 'b': '4' * 64}
NEW_BOOT = {'a': '5' * 64, 'b': '6' * 64}


def _writes():
    result = {}
    for index, slot in enumerate(('a', 'b')):
        digest = NEW_BOOT[slot]
        result[slot] = {
            'candidate': {'path': '/tx/boot-%s.img' % slot, 'bytes': 16 + index,
                          'sha256': digest},
            'sourceBootSha256': ORIGINAL_BOOT[slot],
            'bytesWritten': 16 + index,
            'readbackSha256': digest,
        }
    return result


def legacy_evidence(**overrides):
    body = {
        'schemaVersion': 1,
        'state': 'available',
        'bootContext': 'legacy-controller',
        'bootId': BOOT_ID,
        'deployment': OLD,
        'status': {
            'transactionId': TX, 'oldDeployment': OLD, 'newDeployment': NEW,
            'planSha256': SHA, 'state': 'awaiting_reboot',
            'bootWriteState': 'written', 'activationOutcome': 'activated',
            'bootId': BOOT_ID,
        },
        'journal': {
            'state': 'available', 'bootContext': 'legacy-controller',
            'actualSlot': None, 'inactiveSlot': None, 'recordState': 'prepared',
            'policy': 'offline-dual',
            'transactionId': TX, 'oldDeployment': OLD, 'newDeployment': NEW,
            'fromSlot': 'a', 'targetSlot': 'b', 'planSha256': SHA,
            'runningDeployment': 'old', 'bootConfirmed': False,
        },
        'plan': {
            'transactionId': TX, 'oldDeployment': OLD, 'newDeployment': NEW,
            'fromSlot': 'a', 'targetSlot': 'b',
            'originalBootSha256': dict(ORIGINAL_BOOT),
            'newBootSha256': dict(NEW_BOOT),
        },
        'writeReport': {
            'phase': 'dual_boots_written', 'transactionUuid': TX,
            'deployment': NEW, 'fromSlot': 'a', 'slotOrder': ['b', 'a'],
            'physical': dict(PHYSICAL), 'writes': _writes(),
        },
        'activationReport': {
            'phase': 'offline_slots_activated', 'transactionUuid': TX,
            'deployment': NEW, 'fromSlot': 'a', 'targetSlot': 'b',
            'selectedSlot': 'b', 'recordSequence': 1,
            'currentReadbackSha256': _writes()['a']['readbackSha256'],
            'targetReadbackSha256': _writes()['b']['readbackSha256'],
        },
    }
    body.update(overrides)
    return body


class LegacyPreRebootVerdictTests(unittest.TestCase):
    """A controller-only boot may only reach awaiting_reboot, never confirmed."""

    def test_eligible_only_in_the_original_boot_with_complete_evidence(self):
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), legacy_evidence())
        self.assertEqual(verdict['state'], 'awaiting_reboot')
        self.assertTrue(verdict['restartEligible'])
        self.assertEqual(verdict['evidenceKind'], 'legacy-pre-reboot')
        self.assertEqual(verdict['bootContext'], 'legacy-controller')
        self.assertIsNone(verdict['actualSlot'])
        self.assertEqual(verdict['bootId'], BOOT_ID)
        self.assertEqual(verdict['recordState'], 'prepared')
        self.assertNotEqual(verdict['state'], 'confirmed')
        self.assertNotIn('bootConfirmed', verdict)

    def test_a_changed_boot_is_never_restart_eligible(self):
        evidence = legacy_evidence(bootId='11111111-2222-4333-8444-555555555555')
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'boot_changed')
        self.assertFalse(verdict['restartEligible'])

    def test_wrong_transaction_or_plan_is_a_mismatch(self):
        for field, value in (('transactionId', OTHER_TX),
                             ('planSha256', OTHER_SHA)):
            with self.subTest(field=field):
                evidence = legacy_evidence()
                evidence['journal'][field] = value
                verdict = offline_completion.evaluate_legacy_pre_reboot(
                    legacy_receipt(), evidence)
                self.assertEqual(verdict['state'], 'mismatch')
                self.assertFalse(verdict['restartEligible'])

    def test_confirmed_journal_without_the_new_witness_stays_unverified(self):
        evidence = legacy_evidence()
        evidence['journal']['recordState'] = 'confirmed'
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'],
                         'legacy_confirmation_requires_witness')
        self.assertFalse(verdict['restartEligible'])

    def test_running_the_new_deployment_waits_for_confirmation(self):
        evidence = legacy_evidence(deployment=NEW)
        evidence['journal']['runningDeployment'] = 'new'
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'awaiting_confirmation')
        self.assertFalse(verdict['restartEligible'])

    def test_missing_or_tampered_write_records_are_not_eligible(self):
        evidence = legacy_evidence()
        evidence['writeReport']['writes']['b']['readbackSha256'] = 'f' * 64
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'legacy_write_evidence_missing')
        self.assertFalse(verdict['restartEligible'])

        evidence = legacy_evidence()
        evidence['plan']['newBootSha256']['b'] = '9' * 64
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'legacy_write_evidence_missing')

        evidence = legacy_evidence()
        evidence['activationReport']['selectedSlot'] = 'a'
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), evidence)
        self.assertEqual(verdict['state'], 'unverified')
        self.assertEqual(verdict['reason'], 'legacy_activation_evidence_missing')

    def test_unavailable_or_malformed_evidence_fails_closed(self):
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), {'schemaVersion': 1, 'state': 'unavailable',
                               'error': 'no journal'})
        self.assertEqual(verdict['reason'], 'legacy_evidence_unavailable')
        extra = legacy_evidence()
        extra['actualSlot'] = 'b'
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(), extra)
        self.assertEqual(verdict['reason'], 'legacy_evidence_malformed')

    def test_terminal_failure_and_restart_state(self):
        verdict = offline_completion.evaluate_legacy_pre_reboot(
            legacy_receipt(state='recovery_required'), legacy_evidence())
        self.assertEqual(verdict['state'], 'failed')
        self.assertFalse(verdict['restartEligible'])

        verdict = offline_completion.evaluate_legacy_pre_reboot(
            dict(legacy_receipt(), restart={'schemaVersion': 1,
                                            'state': 'dispatched',
                                            'transactionId': TX}),
            legacy_evidence())
        self.assertEqual(verdict['restartState'], 'dispatched')


if __name__ == '__main__':
    unittest.main()
