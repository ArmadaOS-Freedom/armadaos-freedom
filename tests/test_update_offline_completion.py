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
ORIGINAL = {'a': 'e' * 64, 'b': 'f' * 64}
# Slot digests must contain a-f: an all-digit fixture would survive .upper()
# unchanged and make the uppercase rejection cases pass for the wrong reason.
NEW_BOOT = {'a': '4f3a9c1d' * 8, 'b': '9c7b2e50' * 8}
MODEL = 'TB322FC'


def uppercased(digest):
    """Uppercase a fixture digest, insisting the fixture really has letters."""
    folded = digest.upper()
    assert folded != digest, 'all-digit digest %r has no case' % digest
    return folded


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


def execute_documents(**overrides):
    """Return the four documents a completed execute leaves behind."""
    plan = {
        'schemaVersion': 1, 'operation': 'offline-upgrade',
        'policy': 'offline-dual', 'transactionId': TX, 'model': MODEL,
        'fromSlot': 'a', 'targetSlot': 'b',
        'oldDeployment': OLD, 'newDeployment': NEW,
        'originalBootSha256': dict(ORIGINAL),
        'newBootSha256': dict(NEW_BOOT),
    }
    prepared = {'transactionId': TX, 'planSha256': SHA}
    status = {
        'schemaVersion': 1, 'component': 'system',
        'operation': 'offline-upgrade', 'policy': 'offline-dual',
        'bootPolicy': 'dual-boot-no-slot-control',
        'transactionId': TX, 'model': MODEL,
        'fromSlot': 'a', 'targetSlot': 'b',
        'oldDeployment': OLD, 'newDeployment': NEW,
        'planSha256': SHA, 'state': 'awaiting_reboot',
        'phase': 'awaiting_reboot', 'requiresReboot': True,
        'bootWriteState': 'written', 'slotControlState': 'not_issued',
    }
    report = {
        'phase': 'dual_boots_written', 'model': MODEL,
        'transactionUuid': TX, 'deployment': NEW, 'fromSlot': 'a',
        'slotOrder': ['b', 'a'],
        'writes': {
            slot: {
                'sourceBootSha256': ORIGINAL[slot],
                'candidate': {'path': '/work/candidate-%s.img' % slot,
                              'bytes': 4096, 'sha256': NEW_BOOT[slot]},
                'bytesWritten': 4096,
                'readbackSha256': NEW_BOOT[slot],
            } for slot in ('a', 'b')
        },
    }
    ack = {'transactionId': TX, 'planSha256': SHA}
    documents = {'plan': plan, 'prepared': prepared,
                 'status': status, 'report': report, 'ack': ack}
    for name, value in overrides.items():
        documents[name] = value
    return documents


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


class ExecuteCompletionTests(unittest.TestCase):
    """The shared validator must reject any write report it cannot verify."""

    def check(self, documents=None, model=MODEL):
        documents = documents if documents is not None else execute_documents()
        return offline_completion.execute_completion(
            documents['prepared'], documents['plan'], SHA,
            documents['status'], documents['report'], documents['ack'], model)

    def test_accepts_a_verified_dual_slot_write(self):
        self.assertEqual(self.check()['transactionId'], TX)
        documents = execute_documents()
        documents['report']['physical'] = {
            'model': MODEL, 'actualSlot': 'a', 'inactiveSlot': 'b',
            'deployment': OLD,
        }
        self.assertIsNotNone(self.check(documents))
        self.assertIsNone(self.check(model='TB323FU'))
        self.assertIsNone(self.check(dict(documents, prepared=None)))
        self.assertIsNone(self.check(dict(documents, report=None)))

    def test_slot_entries_must_be_verified_candidates(self):
        def claim(value):
            def mutate(report):
                report['writes']['b']['candidate'] = value
            return mutate

        def entry(**changes):
            def mutate(report):
                report['writes']['b'].update(changes)
            return mutate

        def boolean_size(report):
            report['writes']['b']['candidate']['bytes'] = 1
            report['writes']['b']['bytesWritten'] = True

        cases = (
            ('empty slot entries',
             lambda report: report['writes'].update({'a': {}, 'b': {}})),
            ('empty candidate object', claim({})),
            ('candidate not an object', claim(['/work/b.img'])),
            ('empty candidate path',
             claim({'path': '', 'bytes': 4096, 'sha256': NEW_BOOT['b']})),
            ('zero candidate bytes',
             claim({'path': '/work/b.img', 'bytes': 0,
                    'sha256': NEW_BOOT['b']})),
            ('uppercase candidate digest',
             claim({'path': '/work/b.img', 'bytes': 4096,
                    'sha256': uppercased(NEW_BOOT['b'])})),
            ('candidate digest mismatch',
             claim({'path': '/work/b.img', 'bytes': 4096,
                    'sha256': ORIGINAL['b']})),
            ('short write', entry(bytesWritten=4095)),
            ('boolean byte count', boolean_size),
            ('readback mismatch', entry(readbackSha256='3' * 64)),
            ('missing readback', entry(readbackSha256=None)),
            ('source mismatch', entry(sourceBootSha256=NEW_BOOT['b'])),
            ('extra evidence field', entry(slot='b')),
            ('missing writes', lambda report: report.pop('writes')),
            ('writes as a list', lambda report: report.update(writes=[])),
            ('single slot', lambda report: report['writes'].pop('b')),
        )
        for name, mutate in cases:
            with self.subTest(case=name):
                documents = execute_documents()
                mutate(documents['report'])
                self.assertIsNone(self.check(documents))

    def test_report_must_bind_plan_order_and_identity(self):
        cases = (
            ('running slot first',
             lambda report: report.update(slotOrder=['a', 'b'])),
            ('missing slot order', lambda report: report.pop('slotOrder')),
            ('slot order as text', lambda report: report.update(slotOrder='ba')),
            ('foreign transaction',
             lambda report: report.update(transactionUuid=OTHER_TX)),
            ('foreign deployment',
             lambda report: report.update(
                 deployment='armada/' + ('3' * 64) + '.2')),
            ('foreign model', lambda report: report.update(model='TB323FU')),
            ('wrong phase', lambda report: report.update(phase='writing')),
            ('physical slot conflict',
             lambda report: report.update(
                 physical={'model': MODEL, 'actualSlot': 'b',
                           'inactiveSlot': 'a'})),
            ('physical deployment conflict',
             lambda report: report.update(
                 physical={'model': MODEL, 'actualSlot': 'a',
                           'inactiveSlot': 'b', 'deployment': NEW})),
            ('physical not an object',
             lambda report: report.update(physical='a')),
        )
        for name, mutate in cases:
            with self.subTest(case=name):
                documents = execute_documents()
                mutate(documents['report'])
                self.assertIsNone(self.check(documents))

    def test_plan_must_carry_both_canonical_slot_digest_maps(self):
        cases = (
            ('missing map', {}),
            ('map is null', {'newBootSha256': None}),
            ('map is a list', {'newBootSha256': [NEW_BOOT['a'], NEW_BOOT['b']]}),
            ('missing slot', {'newBootSha256': {'a': NEW_BOOT['a']}}),
            ('extra slot',
             {'newBootSha256': dict(NEW_BOOT, c='3' * 64)}),
            ('uppercase digest',
             {'newBootSha256': {'a': uppercased(NEW_BOOT['a']),
                                'b': NEW_BOOT['b']}}),
            ('non-string digest',
             {'newBootSha256': {'a': 1, 'b': NEW_BOOT['b']}}),
            ('missing source map', {'originalBootSha256': None}),
        )
        for name, changes in cases:
            with self.subTest(case=name):
                documents = execute_documents()
                if name == 'missing map':
                    documents['plan'].pop('newBootSha256')
                else:
                    documents['plan'].update(changes)
                self.assertIsNone(self.check(documents))

    def test_status_and_acknowledgement_must_confirm_the_same_write(self):
        cases = (
            ('status write uncertain', 'status',
             {'bootWriteState': 'uncertain'}),
            ('status slot control issued', 'status',
             {'slotControlState': 'issued'}),
            ('status no reboot', 'status', {'requiresReboot': False}),
            ('status schema type', 'status', {'schemaVersion': True}),
            ('status foreign transaction', 'status',
             {'transactionId': OTHER_TX}),
            ('status foreign plan', 'status', {'planSha256': OTHER_SHA}),
            ('status foreign deployment', 'status',
             {'newDeployment': 'armada/' + ('3' * 64) + '.2'}),
            ('status still executing', 'status',
             {'state': 'executing', 'phase': 'writing_boot'}),
            ('ack foreign transaction', 'ack', {'transactionId': OTHER_TX}),
            ('ack foreign plan', 'ack', {'planSha256': OTHER_SHA}),
            ('ack not an object', 'ack', None),
            ('prepared not an object', 'prepared', None),
        )
        for name, document, changes in cases:
            with self.subTest(case=name):
                documents = execute_documents()
                if changes is None:
                    documents[document] = None
                else:
                    documents[document].update(changes)
                self.assertIsNone(self.check(documents))


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


if __name__ == '__main__':
    unittest.main()
