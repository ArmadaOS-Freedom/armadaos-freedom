# SPDX-License-Identifier: GPL-3.0-only
"""Root-only, line-delimited JSON stdio RPC for offline updates over trusted SSH."""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys

from . import offline_jobs, offline_session

PROTOCOL = 1
_FRAME_MAX = 4 * 1024 * 1024
_ID_MAX = 2147483647
_DOCUMENT_MAX = 2 * 1024 * 1024
_SIGNATURE_MAX = 16 * 1024
_CHUNK_MAX = 1024 * 1024
_MESSAGE_MAX = 500
_WORKER_OK = frozenset({'prepared', 'up_to_date', 'awaiting_reboot'})
_REQUEST_KEYS = frozenset({'protocolVersion', 'id', 'method', 'params'})
_SESSION_HEX = frozenset('0123456789abcdef')
_METHOD_PARAMS = {
    'preflight': frozenset({'document', 'signature'}),
    'info': frozenset(),
    'begin': frozenset({'document', 'signature'}),
    'status': frozenset({'sessionId'}),
    'upload': frozenset({'sessionId', 'offset', 'data'}),
    'seal': frozenset({'sessionId'}),
    'prepare': frozenset({'sessionId'}),
    'execute': frozenset({'sessionId', 'acknowledgement'}),
    'export': frozenset({'sessionId', 'path', 'offset', 'length'}),
    'restart_to_verify': frozenset({'sessionId'}),
    'recovery_boot_begin': frozenset({'sessionId', 'temporaryBootSha256'}),
    'recovery_boot_status': frozenset({'sessionId'}),
    'recovery_boot_upload': frozenset({'sessionId', 'slot', 'offset', 'data'}),
    'recovery_boot_seal': frozenset({'sessionId'}),
}
# Only preflight may additionally name one existing session.  Every other key
# on every method stays mandatory, and any other extra key is still rejected.
_METHOD_OPTIONAL_PARAMS = {
    'preflight': frozenset({'sessionId'}),
}
# Reported by this exact service, not inferred from a version string: a client
# may gate the optional preflight session id on this only.  It is truthful
# because the optional parameter and its forwarder above are already present.
_CAPABILITIES = ('preflight-session-v1',)
_NORMAL_METHODS = frozenset({
    'preflight', 'info', 'begin', 'status', 'upload', 'seal', 'prepare', 'execute',
    'export', 'restart_to_verify',
})


class _Invalid(Exception):
    """A request is malformed; it is the caller's fault, not the manager's."""


class _Denied(Exception):
    """A known method is not offered by this endpoint."""


def _reject_constant(name):
    raise _Invalid('JSON constant %s is not allowed' % name)


def _object_pairs(pairs):
    keys = set()
    for key, _value in pairs:
        if key in keys:
            raise _Invalid('duplicate JSON object key')
        keys.add(key)
    return dict(pairs)


def _b64(value, cap, label):
    if type(value) is not str:
        raise _Invalid('%s must be a base64 string' % label)
    try:
        raw = base64.b64decode(value.encode('ascii'), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise _Invalid('%s is not valid base64' % label) from None
    if not 0 < len(raw) <= cap:
        raise _Invalid('%s exceeds its size limit' % label)
    return raw


def _int(value, label):
    if type(value) is not int or value < 0:
        raise _Invalid('%s must be a nonnegative integer' % label)
    return value


def _valid_session_id(value):
    return (type(value) is str and len(value) == 32 and
            all(ch in _SESSION_HEX for ch in value))


def _session_id(params):
    session_id = params.get('sessionId')
    if not _valid_session_id(session_id):
        raise _Invalid('sessionId must be 32 lowercase hex characters')
    return session_id


def _op_info(manager, _params):
    # Copy before adding: the store may hand back the mapping it keeps
    # internally, and advertising this endpoint's capabilities must never
    # rewrite the stored device info.
    info = dict(manager.store.info())
    info['capabilities'] = list(_CAPABILITIES)
    return info


def _op_preflight(manager, params):
    document = _b64(params['document'], _DOCUMENT_MAX, 'document')
    signature = _b64(params['signature'], _SIGNATURE_MAX, 'signature')
    if 'sessionId' not in params:
        return manager.store.preflight(document, signature)
    # The optional session id only selects one existing protected session; it
    # is validated before any store work exactly like every other session id.
    return manager.store.preflight(document, signature, _session_id(params))


def _op_begin(manager, params):
    document = _b64(params['document'], _DOCUMENT_MAX, 'document')
    signature = _b64(params['signature'], _SIGNATURE_MAX, 'signature')
    return manager.store.begin(document, signature)


def _op_status(manager, params):
    return manager.status(_session_id(params))


def _op_upload(manager, params):
    session_id = _session_id(params)
    offset = _int(params['offset'], 'offset')
    data = _b64(params['data'], _CHUNK_MAX, 'data')
    return manager.store.upload(session_id, offset, data)


def _op_seal(manager, params):
    return manager.store.seal(_session_id(params))


def _op_prepare(manager, params):
    return manager.submit_prepare(_session_id(params))


def _op_execute(manager, params):
    session_id = _session_id(params)
    ack = params['acknowledgement']
    if type(ack) is not dict:
        raise _Invalid('acknowledgement must be a JSON object')
    return manager.submit_execute(session_id, ack)


def _op_restart_to_verify(manager, params):
    # Only a session id is accepted: no command, target, or delay is client
    # controlled, and the manager re-verifies the exact transaction itself.
    return manager.request_restart(_session_id(params))


def _recovery_temp_hash(params):
    value = params['temporaryBootSha256']
    if (type(value) is not str or len(value) != 64 or
            not all(ch in _SESSION_HEX for ch in value)):
        raise _Invalid('temporaryBootSha256 must be 64 lowercase hex characters')
    return value


def _op_recovery_boot_begin(manager, params):
    session_id = _session_id(params)
    temp_hash = _recovery_temp_hash(params)
    return manager.store.recovery_boot_begin(session_id, temp_hash)


def _op_recovery_boot_status(manager, params):
    return manager.store.recovery_boot_status(_session_id(params))


def _op_recovery_boot_upload(manager, params):
    session_id = _session_id(params)
    slot = params['slot']
    if type(slot) is not str or slot not in ('a', 'b'):
        raise _Invalid('slot must be a or b')
    offset = _int(params['offset'], 'offset')
    data = _b64(params['data'], _CHUNK_MAX, 'data')
    return manager.store.recovery_boot_upload(session_id, slot, offset, data)


def _op_recovery_boot_seal(manager, params):
    return manager.store.recovery_boot_seal(_session_id(params))


def _op_export(manager, params):
    session_id = _session_id(params)
    path = params['path']
    if type(path) is not str or not path:
        raise _Invalid('path must be a nonempty string')
    offset = _int(params['offset'], 'offset')
    length = _int(params['length'], 'length')
    data = manager.store.export(session_id, path, offset, length)
    return {'data': base64.b64encode(data).decode('ascii')}


_OPERATIONS = {
    'preflight': _op_preflight,
    'info': _op_info,
    'begin': _op_begin,
    'status': _op_status,
    'upload': _op_upload,
    'seal': _op_seal,
    'prepare': _op_prepare,
    'execute': _op_execute,
    'export': _op_export,
    'restart_to_verify': _op_restart_to_verify,
    'recovery_boot_begin': _op_recovery_boot_begin,
    'recovery_boot_status': _op_recovery_boot_status,
    'recovery_boot_upload': _op_recovery_boot_upload,
    'recovery_boot_seal': _op_recovery_boot_seal,
}


def _dispatch(manager, method, params, *, allowed_methods=None):
    if type(params) is not dict:
        raise _Invalid('params must be a JSON object')
    expected = _METHOD_PARAMS.get(method)
    if expected is None:
        raise _Invalid('unknown method')
    optional = _METHOD_OPTIONAL_PARAMS.get(method, frozenset())
    supplied = frozenset(params)
    if not expected <= supplied or supplied - expected - optional:
        raise _Invalid('unexpected or missing method parameters')
    offered = _NORMAL_METHODS if allowed_methods is None else allowed_methods
    if method not in offered:
        raise _Denied('method is not available on this endpoint')
    return _OPERATIONS[method](manager, params)


def _write(writer, response):
    writer.write((json.dumps(
        response, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8'))
    writer.flush()


def _failure(request_id, code, message):
    return {'protocolVersion': PROTOCOL, 'id': request_id, 'ok': False,
            'error': {'code': code, 'message': str(message)[:_MESSAGE_MAX]}}


def serve(reader, writer, manager, *, allowed_methods=None):
    """Serve one compact JSON request per line; no request data is ever logged."""
    while True:
        try:
            line = reader.readline(_FRAME_MAX + 1)
        except OSError:
            return 1
        if line == b'':
            return 0
        if len(line) > _FRAME_MAX or not line.endswith(b'\n') or not line[:-1]:
            try:
                _write(writer, _failure(
                    None, 'invalid_request',
                    'frame is empty, unterminated, or oversized'))
            except OSError:
                return 1
            return 1
        try:
            request = json.loads(
                line[:-1].decode('utf-8'), parse_constant=_reject_constant,
                object_pairs_hook=_object_pairs)
        except (_Invalid, UnicodeDecodeError, ValueError):
            try:
                _write(writer, _failure(
                    None, 'invalid_request', 'request is not valid JSON'))
            except OSError:
                return 1
            continue
        request_id = None
        try:
            if type(request) is not dict:
                raise _Invalid('request must be a JSON object')
            raw_id = request.get('id')
            if type(raw_id) is int and 1 <= raw_id <= _ID_MAX:
                request_id = raw_id
            if frozenset(request) != _REQUEST_KEYS:
                raise _Invalid('request must contain protocolVersion, id, method, and params')
            version = request['protocolVersion']
            if type(version) is not int or version != PROTOCOL:
                raise _Invalid('protocolVersion must be %d' % PROTOCOL)
            if request_id is None:
                raise _Invalid('id must be a positive integer')
            method = request['method']
            if type(method) is not str:
                raise _Invalid('method must be a string')
            result = _dispatch(manager, method, request['params'],
                               allowed_methods=allowed_methods)
            response = {'protocolVersion': PROTOCOL, 'id': request_id,
                        'ok': True, 'result': result}
        except _Invalid as exc:
            response = _failure(request_id, 'invalid_request', exc)
        except _Denied as exc:
            response = _failure(request_id, 'operation_not_available', exc)
        except Exception as exc:
            response = _failure(request_id, 'operation_failed', exc)
        try:
            _write(writer, response)
        except OSError:
            return 1


def main(argv=None):
    if os.geteuid() != 0:
        raise PermissionError('offline update service requires euid 0')
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv:
        return serve(sys.stdin.buffer, sys.stdout.buffer,
                     offline_jobs.OfflineJobManager())
    if len(argv) == 3 and argv[0] == '--worker':
        operation, session_id = argv[1], argv[2]
        if (operation not in ('prepare', 'execute') or
                not _valid_session_id(session_id)):
            return 1
        try:
            result = offline_jobs.OfflineJobManager().run_job(
                operation, session_id)
        except (ValueError, offline_session.OfflineSessionError):
            return 1
        if isinstance(result, dict) and result.get('state') in _WORKER_OK:
            return 0
        return 1
    return 2
