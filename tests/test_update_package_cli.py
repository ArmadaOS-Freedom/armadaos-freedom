import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_update_package_builder as builder_fixture
from updates.signature import MAX_DOCUMENT_BYTES, MAX_SIGNATURE_BYTES


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "build_files" / "make-universal-update-descriptor.py"


class UpdatePackageCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.archive = builder_fixture.UpdatePackageBuilderTests()._make_bundle(
            self.directory, models=("TB322FC", "TB323FU")
        )
        self.descriptor = self.directory / "desc.json"
        self.signature = self.directory / "desc.sig"

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *(str(item) for item in arguments)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_openssl(self, *arguments):
        return subprocess.run(
            ["openssl", *(str(item) for item in arguments)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def make_descriptor(self):
        result = self.run_cli(
            "make", "--archive", self.archive, "--release-id", "rel-1",
            "--output", self.descriptor,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def make_keypair(self, name):
        private = self.directory / f"{name}-private.pem"
        public = self.directory / f"{name}-public.pem"
        self.run_openssl(
            "ecparam", "-name", "prime256v1", "-genkey", "-noout",
            "-out", private,
        )
        self.run_openssl("ec", "-in", private, "-pubout", "-out", public)
        return private, public

    def sign_descriptor(self, private):
        raw_signature = self.directory / "signature.bin"
        self.run_openssl(
            "dgst", "-sha256", "-sign", private,
            "-out", raw_signature, self.descriptor,
        )
        self.signature.write_bytes(
            base64.b64encode(raw_signature.read_bytes())
        )

    def test_make_writes_unsigned_descriptor_and_preserves_existing_output(self):
        result = self.make_descriptor()
        document = json.loads(self.descriptor.read_text(encoding="utf-8"))
        self.assertEqual(document["releaseId"], "rel-1")
        self.assertIn("not signed", result.stdout)

        original = self.descriptor.read_bytes()
        result = self.run_cli(
            "make", "--archive", self.archive, "--release-id", "rel-1",
            "--output", self.descriptor,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.descriptor.read_bytes(), original)
        self.assertNotIn("Traceback", result.stderr)

    def test_real_openssl_signature_verifies_and_tampering_fails(self):
        private, public = self.make_keypair("trusted")
        _, wrong_public = self.make_keypair("wrong")
        self.make_descriptor()
        self.sign_descriptor(private)
        signed_document = self.descriptor.read_bytes()

        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature, "--trusted-key", public,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "rel-1")

        self.descriptor.write_bytes(signed_document + b"  \n")
        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature, "--trusted-key", public,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)

        self.descriptor.write_bytes(signed_document)
        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature, "--trusted-key", wrong_public,
        )
        self.assertEqual(result.returncode, 1)

    def test_missing_and_corrupt_archives_do_not_create_output(self):
        corrupt = self.directory / "corrupt.zip"
        corrupt.write_bytes(b"not a zip archive")
        for archive in (self.directory / "missing.zip", corrupt):
            with self.subTest(archive=archive.name):
                output = self.directory / f"{archive.name}.json"
                result = self.run_cli(
                    "make", "--archive", archive, "--release-id", "rel-1",
                    "--output", output,
                )
                self.assertEqual(result.returncode, 1)
                self.assertFalse(output.exists())

    def test_oversized_signature_and_descriptor_are_rejected(self):
        _, public = self.make_keypair("limits")
        self.descriptor.write_bytes(b"{}")
        self.signature.write_bytes(b"x" * (MAX_SIGNATURE_BYTES + 1))
        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature, "--trusted-key", public,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("signature exceeds", result.stderr)

        self.descriptor.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        self.signature.write_bytes(b"x")
        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature, "--trusted-key", public,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("descriptor exceeds", result.stderr)

    def test_verify_requires_trusted_key(self):
        result = self.run_cli(
            "verify", "--descriptor", self.descriptor,
            "--signature", self.signature,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
