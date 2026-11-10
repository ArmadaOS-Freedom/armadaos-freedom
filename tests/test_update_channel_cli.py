"""Subprocess tests for the update-channel CLI script."""

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".github/scripts/make-update-channel.py"
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as cr_fixture  # noqa: E402
import test_update_package_contract as pkg_fixture  # noqa: E402
from updates.package_contract import validate_package  # noqa: E402
from updates.signature import MAX_DOCUMENT_BYTES  # noqa: E402

_DESC_URL = cr_fixture._DESC_URL
_SIG_URL = cr_fixture._SIG_URL
_PUBLISHED_AT = cr_fixture._PUBLISHED_AT


class UpdateChannelCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

        self.priv, self.pub = cr_fixture._generate_keypair(self.dir, "trusted")
        self.other_priv, self.other_pub = cr_fixture._generate_keypair(self.dir, "other")

        self.pkg = validate_package(cr_fixture._upgrade_package_dict())
        self.desc_bytes = json.dumps(self.pkg, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.desc_sig = cr_fixture._sign_bytes(self.desc_bytes, self.priv)

        self.desc_path = self.dir / "package.json"
        self.desc_sig_path = self.dir / "package.json.sig"
        self.desc_path.write_bytes(self.desc_bytes)
        self.desc_sig_path.write_bytes(self.desc_sig)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def _make_args(self, output: Path, channel: str = "stable", **overrides) -> list[str]:
        opts = {
            "--descriptor": str(self.desc_path),
            "--descriptor-signature": str(self.desc_sig_path),
            "--trusted-key": str(self.pub),
            "--channel": channel,
            "--descriptor-url": _DESC_URL,
            "--signature-url": _SIG_URL,
            "--published-at": _PUBLISHED_AT,
            "--expected-oci-digest": self.pkg["ociDigest"],
            "--output": str(output),
        }
        withdrawn = overrides.pop("withdrawn", False)
        opts.update({k: str(v) for k, v in overrides.items()})
        cmd = ["make"]
        for k, v in opts.items():
            cmd.extend([k, v])
        if withdrawn:
            cmd.append("--withdrawn")
        return cmd

    def _verify_args(self, channel_file: Path, channel_sig: Path, expected: str = "stable", **overrides) -> list[str]:
        opts = {
            "--channel-file": str(channel_file),
            "--channel-signature": str(channel_sig),
            "--descriptor": str(self.desc_path),
            "--descriptor-signature": str(self.desc_sig_path),
            "--trusted-key": str(self.pub),
            "--expected-channel": expected,
        }
        opts.update({k: str(v) for k, v in overrides.items()})
        cmd = ["verify"]
        for k, v in opts.items():
            cmd.extend([k, v])
        return cmd

    def test_promote_across_channels_deterministic_and_inputs_unchanged(self):
        docs = {}
        for ch in ("testing", "beta", "stable"):
            out1 = self.dir / f"{ch}-1.json"
            out2 = self.dir / f"{ch}-2.json"
            res1 = self._run(*self._make_args(out1, channel=ch))
            res2 = self._run(*self._make_args(out2, channel=ch))
            self.assertEqual(res1.returncode, 0, res1.stderr)
            self.assertEqual(res2.returncode, 0, res2.stderr)
            raw1 = out1.read_bytes()
            self.assertEqual(raw1, out2.read_bytes())
            docs[ch] = json.loads(raw1.decode("utf-8"))
            self.assertEqual(docs[ch]["channel"], ch)
            self.assertFalse(docs[ch]["withdrawn"])

        for ch, doc in docs.items():
            for field in ("releaseId", "version", "sourceRevision", "ociDigest", "ostreeCommit", "minimumUpdaterVersion"):
                self.assertEqual(doc[field], self.pkg[field])
            self.assertEqual(doc["packageDescriptor"], {
                "url": _DESC_URL,
                "signatureUrl": _SIG_URL,
                "bytes": len(self.desc_bytes),
                "sha256": sha256(self.desc_bytes).hexdigest(),
            })

        self.assertEqual(self.desc_path.read_bytes(), self.desc_bytes)
        self.assertEqual(self.desc_sig_path.read_bytes(), self.desc_sig)

    def test_sign_and_verify_roundtrip_and_tamper_failures(self):
        ch_file = self.dir / "stable.json"
        self.assertEqual(self._run(*self._make_args(ch_file)).returncode, 0)
        ch_bytes = ch_file.read_bytes()
        ch_sig = self.dir / "stable.json.sig"
        ch_sig.write_bytes(cr_fixture._sign_bytes(ch_bytes, self.priv))

        ok = self._run(*self._verify_args(ch_file, ch_sig, expected="stable"))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.strip(), self.pkg["releaseId"])

        tampered_ch = self.dir / "tampered-channel.json"
        tampered_ch.write_bytes(ch_bytes + b"\n")
        tampered_desc = self.dir / "tampered-desc.json"
        tampered_desc.write_bytes(self.desc_bytes + b" ")

        failures = [
            self._verify_args(ch_file, ch_sig, expected="beta"),
            self._verify_args(tampered_ch, ch_sig, expected="stable"),
            self._verify_args(ch_file, ch_sig, expected="stable", **{"--descriptor": tampered_desc}),
            self._verify_args(ch_file, ch_sig, expected="stable", **{"--trusted-key": self.other_pub}),
        ]
        for cmd in failures:
            res = self._run(*cmd)
            self.assertEqual(res.returncode, 1)
            self.assertNotIn("Traceback", res.stderr)

    def test_withdrawn_channel_structurally_verifiable_without_claiming_publication(self):
        ch_file = self.dir / "withdrawn.json"
        make_res = self._run(*self._make_args(ch_file, withdrawn=True))
        self.assertEqual(make_res.returncode, 0, make_res.stderr)
        self.assertIn("Created unsigned channel metadata", make_res.stdout)
        self.assertNotIn("Published", make_res.stdout)
        doc = json.loads(ch_file.read_text("utf-8"))
        self.assertTrue(doc["withdrawn"])

        ch_sig = self.dir / "withdrawn.json.sig"
        ch_sig.write_bytes(cr_fixture._sign_bytes(ch_file.read_bytes(), self.priv))
        ver_res = self._run(*self._verify_args(ch_file, ch_sig, expected="stable"))
        self.assertEqual(ver_res.returncode, 0, ver_res.stderr)
        self.assertEqual(ver_res.stdout.strip(), self.pkg["releaseId"])

    def test_rejections_preserve_existing_and_leave_no_output(self):
        install_pkg = validate_package(pkg_fixture._base_package())
        inst_bytes = json.dumps(install_pkg, sort_keys=True, separators=(",", ":")).encode("utf-8")
        inst_path = self.dir / "install.json"
        inst_sig_path = self.dir / "install.json.sig"
        inst_path.write_bytes(inst_bytes)
        inst_sig_path.write_bytes(cr_fixture._sign_bytes(inst_bytes, self.priv))

        out_inst = self.dir / "out-inst.json"
        res_inst = self._run(*self._make_args(
            out_inst, **{"--descriptor": inst_path, "--descriptor-signature": inst_sig_path,
                         "--expected-oci-digest": install_pkg["ociDigest"]}
        ))
        self.assertEqual(res_inst.returncode, 1)
        self.assertFalse(out_inst.exists())

        out_oci = self.dir / "out-oci.json"
        res_oci = self._run(*self._make_args(out_oci, **{"--expected-oci-digest": f"sha256:{pkg_fixture._h(123)}"}))
        self.assertEqual(res_oci.returncode, 1)
        self.assertFalse(out_oci.exists())

        existing = self.dir / "existing.json"
        sentinel = b"{\"keep\": true}\n"
        existing.write_bytes(sentinel)
        res_exist = self._run(*self._make_args(existing))
        self.assertEqual(res_exist.returncode, 1)
        self.assertEqual(existing.read_bytes(), sentinel)

    def test_oversized_missing_and_invalid_cli_args(self):
        big_path = self.dir / "big.json"
        big_path.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
        out_big = self.dir / "out-big.json"
        res_big = self._run(*self._make_args(out_big, **{"--descriptor": big_path}))
        self.assertEqual(res_big.returncode, 1)
        self.assertNotIn("Traceback", res_big.stderr)
        self.assertFalse(out_big.exists())

        out_missing = self.dir / "out-missing.json"
        res_missing = self._run(*self._make_args(out_missing, **{"--descriptor": self.dir / "nope.json"}))
        self.assertEqual(res_missing.returncode, 1)
        self.assertFalse(out_missing.exists())

        res_bad_arg = self._run("make", "--channel", "nightly")
        self.assertNotEqual(res_bad_arg.returncode, 0)


if __name__ == "__main__":
    unittest.main()
