import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ARMADA_LIB = REPO_ROOT / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import signature
from updates.signature import SignatureError, load_signed_json, verify_document


class TestUpdateSignature(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="test-update-sig-")
        cls.addClassCleanup(cls._tmpdir.cleanup)
        cls.tmpdir = Path(cls._tmpdir.name)
        cls.priv1 = cls.tmpdir / "priv1.pem"
        cls.pub1 = cls.tmpdir / "pub1.pem"
        cls.priv2 = cls.tmpdir / "priv2.pem"
        cls.pub2 = cls.tmpdir / "pub2.pem"

        cls._gen_keypair(cls.priv1, cls.pub1)
        cls._gen_keypair(cls.priv2, cls.pub2)

    @classmethod
    def _gen_keypair(cls, priv_path: Path, pub_path: Path):
        if not Path(signature.OPENSSL).is_file():
            raise AssertionError(f"Missing required binary: {signature.OPENSSL}")
        res1 = subprocess.run(
            [
                signature.OPENSSL,
                "genpkey",
                "-algorithm",
                "EC",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-out",
                str(priv_path),
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if res1.returncode != 0:
            raise AssertionError("Failed to generate synthetic private key")
        res2 = subprocess.run(
            [
                signature.OPENSSL,
                "pkey",
                "-in",
                str(priv_path),
                "-pubout",
                "-out",
                str(pub_path),
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
        if res2.returncode != 0:
            raise AssertionError("Failed to derive synthetic public key")

    @classmethod
    def _sign(cls, data: bytes, priv_path: Path = None) -> bytes:
        if priv_path is None:
            priv_path = cls.priv1
        res = subprocess.run(
            [signature.OPENSSL, "dgst", "-sha256", "-sign", str(priv_path)],
            input=data,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if res.returncode != 0:
            raise AssertionError("Failed to sign test payload")
        return base64.b64encode(res.stdout)

    def test_valid_utf8_json_with_chinese_and_trailing_newline(self):
        doc = '{"channel":"stable","notes":"更新已就绪","version":1}'.encode("utf-8")
        sig = self._sign(doc) + b"\n"
        verify_document(doc, sig, trusted_key=self.pub1)
        parsed = load_signed_json(doc, sig, trusted_key=self.pub1)
        self.assertEqual(parsed, {"channel": "stable", "notes": "更新已就绪", "version": 1})

    def test_bytearray_accepted(self):
        doc = bytearray(b'{"ok":true}')
        sig = bytearray(self._sign(bytes(doc)))
        parsed = load_signed_json(doc, sig, trusted_key=self.pub1)
        self.assertEqual(parsed, {"ok": True})

    def test_bytearray_snapshotted_before_verify_and_parsed_from_snapshot(self):
        raw = b'{"channel":"stable","version":2}'
        doc = bytearray(raw)
        sig = self._sign(raw)
        real_verify = signature.verify_document
        seen = {}

        def verify_then_mutate(document, signature_b64, **kwargs):
            seen["type"] = type(document)
            seen["value"] = bytes(document)
            doc[:] = b'{"channel":"changed","version":0}'
            return real_verify(document, signature_b64, **kwargs)

        with mock.patch.object(signature, "verify_document", side_effect=verify_then_mutate):
            parsed = load_signed_json(doc, sig, trusted_key=self.pub1)

        self.assertIs(seen["type"], bytes)
        self.assertEqual(seen["value"], raw)
        self.assertEqual(bytes(doc), b'{"channel":"changed","version":0}')
        self.assertEqual(parsed, {"channel": "stable", "version": 2})

    def test_reject_modified_document_whitespace_signature_and_wrong_key(self):
        doc = b'{"a":1,"b":2}'
        sig = self._sign(doc)

        with self.assertRaises(SignatureError):
            verify_document(b'{"a":1,"b":3}', sig, trusted_key=self.pub1)

        with self.assertRaises(SignatureError):
            verify_document(b'{"a": 1, "b": 2}', sig, trusted_key=self.pub1)

        raw_sig = bytearray(base64.b64decode(sig))
        raw_sig[-1] ^= 0x01
        tampered_sig = base64.b64encode(bytes(raw_sig))
        with self.assertRaises(SignatureError):
            verify_document(doc, tampered_sig, trusted_key=self.pub1)

        with self.assertRaises(SignatureError):
            verify_document(doc, sig, trusted_key=self.pub2)

    def test_invalid_signature_checked_before_json_loads(self):
        doc = b'{"valid":true}'
        bad_sig = self._sign(doc, priv_path=self.priv2)
        with mock.patch("updates.signature.json.loads") as mock_loads:
            with self.assertRaises(SignatureError):
                load_signed_json(doc, bad_sig, trusted_key=self.pub1)
            mock_loads.assert_not_called()

    def test_reject_signed_duplicate_keys_root_and_nested(self):
        for raw in (b'{"a":1,"a":2}', b'{"nested":{"dup":1,"dup":2}}'):
            sig = self._sign(raw)
            with self.assertRaises(SignatureError):
                load_signed_json(raw, sig, trusted_key=self.pub1)

    def test_reject_signed_nonfinite_constants_and_float_overflow(self):
        for raw in (
            b'{"v":NaN}',
            b'{"v":Infinity}',
            b'{"v":-Infinity}',
            b'{"v":1e999}',
            b'{"v":-1e999}',
        ):
            sig = self._sign(raw)
            with self.assertRaises(SignatureError):
                load_signed_json(raw, sig, trusted_key=self.pub1)

    def test_reject_signed_non_dict_root(self):
        for raw in (b'[1,2,3]', b'"string"', b'123', b'true', b'null'):
            sig = self._sign(raw)
            with self.assertRaises(SignatureError):
                load_signed_json(raw, sig, trusted_key=self.pub1)

    def test_reject_signed_bom_invalid_utf8_invalid_json_and_recursion(self):
        bom_doc = b"\xef\xbb\xbf" + b'{"a":1}'
        with self.assertRaises(SignatureError):
            load_signed_json(bom_doc, self._sign(bom_doc), trusted_key=self.pub1)

        bad_utf8 = b'{"a":"\xff\xfe"}'
        with self.assertRaises(SignatureError):
            load_signed_json(bad_utf8, self._sign(bad_utf8), trusted_key=self.pub1)

        bad_json = b'{"a":1,'
        with self.assertRaises(SignatureError):
            load_signed_json(bad_json, self._sign(bad_json), trusted_key=self.pub1)

        # Parser recursion limits vary by Python runtime; fault-inject to test error normalization deterministically.
        valid_json = b'{"a": 1}'
        valid_sig = self._sign(valid_json)
        with mock.patch.object(signature.json, "loads", side_effect=RecursionError("depth exceeded")) as mock_loads:
            with self.assertRaises(SignatureError):
                load_signed_json(valid_json, valid_sig, trusted_key=self.pub1)
            mock_loads.assert_called_once()

    def test_input_validation_before_subprocess(self):
        valid_doc = b'{"a":1}'
        valid_sig = base64.b64encode(b"0" * 64)

        cases = [
            ("not-bytes", valid_sig),
            (b"", valid_sig),
            (b"x" * (signature.MAX_DOCUMENT_BYTES + 1), valid_sig),
            (valid_doc, "not-bytes"),
            (valid_doc, b""),
            (valid_doc, b"x" * (signature.MAX_SIGNATURE_BYTES + 1)),
            (valid_doc, b"   \n\t  "),
            (valid_doc, b"!!!not_base64!!!"),
        ]
        with mock.patch("updates.signature.subprocess.run") as mock_run:
            for doc, sig in cases:
                with self.subTest(doc_type=type(doc), sig_len=len(sig) if hasattr(sig, "__len__") else None):
                    with self.assertRaises(SignatureError):
                        verify_document(doc, sig, trusted_key=self.pub1)
            mock_run.assert_not_called()

    def test_openssl_missing_or_timeout_raises_signature_error(self):
        doc = b'{"a":1}'
        sig = self._sign(doc)

        with mock.patch.object(signature, "OPENSSL", str(self.tmpdir / "nonexistent-openssl")):
            with self.assertRaises(SignatureError):
                verify_document(doc, sig, trusted_key=self.pub1)

        timeout_exc = subprocess.TimeoutExpired(cmd=[signature.OPENSSL], timeout=1)
        with mock.patch("updates.signature.subprocess.run", side_effect=timeout_exc):
            with self.assertRaises(SignatureError):
                verify_document(doc, sig, trusted_key=self.pub1)


if __name__ == "__main__":
    unittest.main()
