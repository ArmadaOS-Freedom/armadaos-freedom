# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import base64
import io
import json
import unittest
from unittest import mock

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'system_files/usr/lib/armada'))
from updates import offline_rpc

SID = 'a' * 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def _manager() -> mock.MagicMock:
    mgr = mock.MagicMock()
    mgr.store.info.return_value = {'protocol': 1}
    mgr.store.preflight.return_value = {'phase': 'preflight_passed'}
    mgr.store.begin.return_value = {'sessionId': SID, 'state': 'uploading'}
    mgr.status.return_value = {'sessionId': SID, 'state': 'uploaded'}
    mgr.store.upload.return_value = {'offset': 9}
    mgr.store.seal.return_value = {'sessionId': SID, 'state': 'uploaded'}
    mgr.submit_prepare.return_value = {'sessionId': SID, 'state': 'prepare_queued'}
    mgr.submit_execute.return_value = {'sessionId': SID, 'state': 'execute_queued'}
    mgr.request_restart.return_value = {
        'schemaVersion': 1, 'sessionId': SID, 'state': 'awaiting_reboot',
        'restart': {'schemaVersion': 1, 'state': 'dispatched'}}
    mgr.store.export.return_value = b'recovery-bytes'
    return mgr


def _run_lines(mgr: mock.MagicMock, raw: bytes, *, allowed_methods=None):
    reader = io.BytesIO(raw)
    writer = io.BytesIO()
    rc = offline_rpc.serve(reader, writer, mgr,
                           allowed_methods=allowed_methods)
    out = [json.loads(line.decode('utf-8'))
           for line in writer.getvalue().splitlines()]
    return rc, out


def _assert_no_calls(mgr: mock.MagicMock) -> None:
    assert not mgr.mock_calls and not mgr.store.mock_calls


class OfflineRpcTests(unittest.TestCase):
    def test_all_eight_operations_forward_and_succeed(self):
        mgr = _manager()
        ack = {'transactionId': 'tx-1', 'confirmed': True}
        reqs = [
            {'protocolVersion': 1, 'id': 1, 'method': 'info', 'params': {}},
            {'protocolVersion': 1, 'id': 2, 'method': 'begin',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig')}},
            {'protocolVersion': 1, 'id': 3, 'method': 'status',
             'params': {'sessionId': SID}},
            {'protocolVersion': 1, 'id': 4, 'method': 'upload',
             'params': {'sessionId': SID, 'offset': 5, 'data': _b64(b'chunk')}},
            {'protocolVersion': 1, 'id': 5, 'method': 'seal',
             'params': {'sessionId': SID}},
            {'protocolVersion': 1, 'id': 6, 'method': 'prepare',
             'params': {'sessionId': SID}},
            {'protocolVersion': 1, 'id': 7, 'method': 'execute',
             'params': {'sessionId': SID, 'acknowledgement': ack}},
            {'protocolVersion': 1, 'id': 8, 'method': 'export',
             'params': {'sessionId': SID, 'path': 'recovery.tar', 'offset': 0, 'length': 14}},
        ]
        payload = b''.join((json.dumps(r) + '\n').encode('utf-8') for r in reqs)
        rc, out = _run_lines(mgr, payload)
        self.assertEqual(rc, 0)
        self.assertEqual([r['id'] for r in out], list(range(1, 9)))
        self.assertTrue(all(r['ok'] for r in out))
        self.assertEqual(out[7]['result'], {'data': _b64(b'recovery-bytes')})
        mgr.store.info.assert_called_once_with()
        mgr.store.begin.assert_called_once_with(b'doc', b'sig')
        mgr.status.assert_called_once_with(SID)
        mgr.store.upload.assert_called_once_with(SID, 5, b'chunk')
        mgr.store.seal.assert_called_once_with(SID)
        mgr.submit_prepare.assert_called_once_with(SID)
        mgr.submit_execute.assert_called_once_with(SID, ack)
        mgr.store.export.assert_called_once_with(SID, 'recovery.tar', 0, 14)

    def test_preflight_keeps_the_document_only_shape_and_accepts_a_session(self):
        mgr = _manager()
        requests = [
            {'protocolVersion': 1, 'id': 1, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig')}},
            {'protocolVersion': 1, 'id': 2, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig'),
                        'sessionId': SID}},
        ]
        rc, out = _run_lines(
            mgr, b''.join((json.dumps(r) + '\n').encode('utf-8') for r in requests))
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(r['ok'] for r in out))
        self.assertEqual(
            [{'phase': 'preflight_passed'}, {'phase': 'preflight_passed'}],
            [r['result'] for r in out])
        self.assertEqual(
            [call.args for call in mgr.store.preflight.call_args_list],
            [(b'doc', b'sig'), (b'doc', b'sig', SID)])
        self.assertEqual(mgr.store.begin.call_count, 0)

    def test_preflight_rejects_unknown_keys_and_malformed_sessions(self):
        mgr = _manager()
        bad = [
            # signature stays mandatory even with a session id
            {'protocolVersion': 1, 'id': 1, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'sessionId': SID}},
            # any other extra key is still rejected
            {'protocolVersion': 1, 'id': 2, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig'),
                        'offset': 0}},
            # sessionId is validated as 32 lowercase hex characters
            {'protocolVersion': 1, 'id': 3, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig'),
                        'sessionId': 'BAD'}},
            {'protocolVersion': 1, 'id': 4, 'method': 'preflight',
             'params': {'document': _b64(b'doc'), 'signature': _b64(b'sig'),
                        'sessionId': SID.upper()}},
        ]
        rc, out = _run_lines(
            mgr, b''.join((json.dumps(r) + '\n').encode('utf-8') for r in bad))
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), len(bad))
        for response in out:
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'], 'invalid_request')
        _assert_no_calls(mgr)

    def test_restart_to_verify_passes_only_the_session_id(self):
        mgr = _manager()
        request = {'protocolVersion': 1, 'id': 1,
                   'method': 'restart_to_verify', 'params': {'sessionId': SID}}
        rc, out = _run_lines(mgr, (json.dumps(request) + '\n').encode('utf-8'))
        self.assertEqual(rc, 0)
        self.assertTrue(out[0]['ok'])
        self.assertEqual(out[0]['result'], mgr.request_restart.return_value)
        mgr.request_restart.assert_called_once_with(SID)

    def test_restart_to_verify_is_denied_on_a_restricted_endpoint(self):
        mgr = _manager()
        request = {'protocolVersion': 1, 'id': 1,
                   'method': 'restart_to_verify', 'params': {'sessionId': SID}}
        rc, out = _run_lines(mgr, (json.dumps(request) + '\n').encode('utf-8'),
                             allowed_methods=frozenset({'status'}))
        self.assertEqual(rc, 0)
        self.assertFalse(out[0]['ok'])
        self.assertEqual(out[0]['error']['code'], 'operation_not_available')
        _assert_no_calls(mgr)

    def test_restart_to_verify_rejects_extra_or_malformed_params(self):
        mgr = _manager()
        bad = [
            {'protocolVersion': 1, 'id': 1, 'method': 'restart_to_verify',
             'params': {}},
            {'protocolVersion': 1, 'id': 2, 'method': 'restart_to_verify',
             'params': {'sessionId': SID, 'command': 'reboot'}},
            {'protocolVersion': 1, 'id': 3, 'method': 'restart_to_verify',
             'params': {'sessionId': SID, 'delay': 0}},
            {'protocolVersion': 1, 'id': 4, 'method': 'restart_to_verify',
             'params': {'sessionId': SID.upper()}},
        ]
        rc, out = _run_lines(
            mgr, b''.join((json.dumps(r) + '\n').encode('utf-8') for r in bad))
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), len(bad))
        for response in out:
            self.assertFalse(response['ok'])
            self.assertEqual(response['error']['code'], 'invalid_request')
        _assert_no_calls(mgr)

    def test_invalid_requests_rejected_without_side_effects(self):
        mgr = _manager()
        big_sig = _b64(b'x' * (offline_rpc._SIGNATURE_MAX + 1))
        bad_lines = [
            b'{"protocolVersion":1,"id":1,"id":2,"method":"info","params":{}}\n',
            b'{"protocolVersion":1,"id":1,"method":"info","params":{"x":NaN}}\n',
            b'{"protocolVersion":1,"id":1,"method":"info","params":{},"extra":1}\n',
            b'{"protocolVersion":1,"id":1,"method":"info"}\n',
            b'{"protocolVersion":true,"id":1,"method":"info","params":{}}\n',
            b'{"protocolVersion":1,"id":true,"method":"info","params":{}}\n',
            b'{"protocolVersion":1,"id":0,"method":"info","params":{}}\n',
            b'{"protocolVersion":1,"id":2147483648,"method":"info","params":{}}\n',
            b'{"protocolVersion":1,"id":1,"method":"unknown","params":{}}\n',
            b'{"protocolVersion":1,"id":1,"method":"info","params":{"extra":1}}\n',
            b'{"protocolVersion":1,"id":1,"method":"status","params":{}}\n',
            b'{"protocolVersion":1,"id":1,"method":"status","params":{"sessionId":"BAD"}}\n',
            b'{"protocolVersion":1,"id":1,"method":"upload","params":{"sessionId":"' + SID.encode() + b'","offset":true,"data":"YWJj"}}\n',
            b'{"protocolVersion":1,"id":1,"method":"begin","params":{"document":"not-b64!","signature":"YWJj"}}\n',
            b'{"protocolVersion":1,"id":1,"method":"begin","params":{"document":"YWJj","signature":"' + big_sig.encode() + b'"}}\n',
            b'{"protocolVersion":1,"id":1,"method":"execute","params":{"sessionId":"' + SID.encode() + b'","acknowledgement":[]}}\n',
        ]
        rc, out = _run_lines(mgr, b''.join(bad_lines))
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), len(bad_lines))
        for resp in out:
            self.assertFalse(resp['ok'])
            self.assertEqual(resp['error']['code'], 'invalid_request')
        _assert_no_calls(mgr)

    def test_framing_errors_and_unterminated_tail(self):
        mgr = _manager()
        self.assertEqual(_run_lines(mgr, b'')[0], 0)
        for frame in (b'\n', b'{"protocolVersion":1}', b'x' * (offline_rpc._FRAME_MAX + 1) + b'\n'):
            rc, out = _run_lines(mgr, frame)
            self.assertEqual(rc, 1)
            self.assertEqual(out[0]['error']['code'], 'invalid_request')
        _assert_no_calls(mgr)
        valid = b'{"protocolVersion":1,"id":1,"method":"info","params":{}}\n'
        unterminated = b'{"protocolVersion":1,"id":2,"method":"seal","params":{"sessionId":"' + SID.encode() + b'"}'
        rc, out = _run_lines(mgr, valid + unterminated)
        self.assertEqual(rc, 1)
        self.assertTrue(out[0]['ok'])
        self.assertFalse(out[1]['ok'])
        mgr.store.info.assert_called_once_with()
        mgr.store.seal.assert_not_called()

    def test_operation_oserror_recovers_and_writer_broken_pipe_stops(self):
        mgr = _manager()
        mgr.store.seal.side_effect = OSError('disk full ' + ('x' * 700))
        payload = (
            b'{"protocolVersion":1,"id":1,"method":"seal","params":{"sessionId":"' + SID.encode() + b'"}}\n'
            b'{"protocolVersion":1,"id":2,"method":"info","params":{}}\n'
        )
        rc, out = _run_lines(mgr, payload)
        self.assertEqual(rc, 0)
        self.assertFalse(out[0]['ok'])
        self.assertEqual(out[0]['error']['code'], 'operation_failed')
        self.assertLessEqual(len(out[0]['error']['message']), offline_rpc._MESSAGE_MAX)
        self.assertTrue(out[1]['ok'])

        mgr2 = _manager()
        writer = mock.MagicMock()
        writer.write.side_effect = BrokenPipeError('pipe closed')
        rc2 = offline_rpc.serve(io.BytesIO(payload), writer, mgr2)
        self.assertEqual(rc2, 1)
        mgr2.store.seal.assert_called_once_with(SID)
        mgr2.store.info.assert_not_called()

    def test_main_privilege_and_worker_contract(self):
        with mock.patch('os.geteuid', return_value=1000), \
                mock.patch('updates.offline_jobs.OfflineJobManager') as mcls:
            with self.assertRaises(PermissionError):
                offline_rpc.main([])
            mcls.assert_not_called()
        with mock.patch('os.geteuid', return_value=0), \
                mock.patch('updates.offline_jobs.OfflineJobManager') as mcls:
            self.assertEqual(offline_rpc.main(['--bad']), 2)
            self.assertEqual(offline_rpc.main(['--worker', 'bad', SID]), 1)
            self.assertEqual(offline_rpc.main(['--worker', 'prepare', 'not-hex']), 1)
            mcls.assert_not_called()
            for state in ('prepared', 'up_to_date', 'awaiting_reboot'):
                mcls.return_value.run_job.return_value = {'state': state}
                self.assertEqual(offline_rpc.main(['--worker', 'prepare', SID]), 0)
            mcls.return_value.run_job.return_value = {'state': 'failed'}
            self.assertEqual(offline_rpc.main(['--worker', 'execute', SID]), 1)
