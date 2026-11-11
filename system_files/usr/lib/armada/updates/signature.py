#!/usr/bin/env python3
"""Offline SHA-256 ECDSA P-256 detached DER-in-base64 signature verification.

Trust comes from the host, never from the payload: the /etc policy key is used
when it exists, otherwise the key shipped inside the updater extension under
/usr, because systemd-sysext merges /usr and /opt but never /etc.  Verification
runs through the system OpenSSL binary.
"""

import base64
import binascii
import json
import math
import subprocess
import tempfile
from pathlib import Path

ETC_TRUSTED_KEY = Path('/etc/pki/containers/armadaos-freedom.pub')
PACKAGED_TRUSTED_KEY = Path('/usr/share/armada/updates/armadaos-freedom.pub')
DEFAULT_TRUSTED_KEY = ETC_TRUSTED_KEY
OPENSSL = '/usr/bin/openssl'
OPENSSL_TIMEOUT_SECONDS = 15
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_SIGNATURE_BYTES = 4096


class SignatureError(ValueError):
    """Raised when signature verification fails or input is malformed."""


def trusted_key_path(trusted_key=DEFAULT_TRUSTED_KEY):
    """Return the key to verify with.

    The default keeps an existing /etc policy key and otherwise uses the key
    packaged in the extension.  A caller-supplied path is honoured as given so
    an explicit trust choice is never silently replaced.
    """
    if trusted_key is not None and Path(trusted_key) != DEFAULT_TRUSTED_KEY:
        return Path(trusted_key)
    if ETC_TRUSTED_KEY.is_file():
        return ETC_TRUSTED_KEY
    return PACKAGED_TRUSTED_KEY


def _reject_pairs(pairs):
    hook = dict(pairs)
    if len(hook) != len(pairs):
        raise SignatureError('duplicate JSON object key')
    return hook


def _reject_constant(name):
    raise SignatureError('non-finite JSON constant: %s' % name)


def _parse_float(value):
    num = float(value)
    if not math.isfinite(num):
        raise SignatureError('non-finite JSON float: %s' % value)
    return num


def verify_document(document, signature_b64, *, trusted_key=DEFAULT_TRUSTED_KEY):
    """Verify a detached signature over document bytes. Raise SignatureError on any failure.

    signature_b64 must be ASCII base64 of the DER signature; a single surrounding
    whitespace strip (e.g. trailing newline from command output) is tolerated.
    """
    if not isinstance(document, (bytes, bytearray)):
        raise SignatureError('document must be bytes')
    document = bytes(document)
    if not document:
        raise SignatureError('document must not be empty')
    if len(document) > MAX_DOCUMENT_BYTES:
        raise SignatureError('document exceeds %d bytes' % MAX_DOCUMENT_BYTES)
    if not isinstance(signature_b64, (bytes, bytearray)):
        raise SignatureError('signature must be bytes')
    signature_b64 = bytes(signature_b64)
    if not signature_b64:
        raise SignatureError('signature must not be empty')
    if len(signature_b64) > MAX_SIGNATURE_BYTES:
        raise SignatureError('signature exceeds %d bytes' % MAX_SIGNATURE_BYTES)

    try:
        stripped = signature_b64.strip()
        signature_der = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError('signature is not valid base64') from exc
    if not signature_der:
        raise SignatureError('decoded signature is empty')
    if len(signature_der) > MAX_SIGNATURE_BYTES:
        raise SignatureError('decoded signature exceeds %d bytes' % MAX_SIGNATURE_BYTES)

    key_path = trusted_key_path(trusted_key)
    with tempfile.TemporaryDirectory(prefix='armada-signature-') as tmpdir:
        sig_path = Path(tmpdir) / 'signature.bin'
        try:
            with open(sig_path, 'wb') as fh:
                fh.write(signature_der)
            sig_path.chmod(0o600)
        except OSError as exc:
            raise SignatureError('failed to stage signature file') from exc
        try:
            result = subprocess.run(
                [OPENSSL, 'dgst', '-sha256', '-verify', str(key_path),
                 '-signature', str(sig_path)],
                input=document,
                capture_output=True,
                timeout=OPENSSL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SignatureError('openssl verification timed out') from exc
        except OSError as exc:
            raise SignatureError('failed to execute openssl') from exc

    if result.returncode != 0:
        raise SignatureError('signature verification failed')


def load_signed_json(document, signature_b64, *, trusted_key=DEFAULT_TRUSTED_KEY):
    """Verify the signature first, then strictly decode the document as JSON.

    Returns the decoded dict. Duplicate keys (recursively), NaN/Infinity, a
    non-dict root, BOM, invalid UTF-8, or invalid JSON all raise SignatureError.
    Only document bytes and JSON shape are authenticated here; no semantic
    update-readiness claim is made.
    """
    verify_document(document, signature_b64, trusted_key=trusted_key)
    if isinstance(document, bytearray):
        document = bytes(document)
    if document.startswith(b'\xef\xbb\xbf'):
        raise SignatureError('UTF-8 BOM not allowed')
    try:
        text = document.decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise SignatureError('document is not valid UTF-8') from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_pairs,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except SignatureError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SignatureError('document is not valid JSON') from exc
    if not isinstance(parsed, dict):
        raise SignatureError('JSON root must be an object')
    return parsed
