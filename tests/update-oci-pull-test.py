#!/usr/bin/env python3
"""
Actual bootc/OSTree protocol regression fixture.

Clarification on trust model:
Signed trust is NOT tested here. This synthetic private fixture explicitly
imports via 'ostree-unverified-image:oci:{path}:latest' solely to validate
real engine digestfile output, metadata injection, and reproducible pinned
commit hashes in isolation. Production image_source NEVER uses unverified
image references.
"""

import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import image_source

BOOTC_BIN = "/usr/bin/bootc"
OSTREE_BIN = "/usr/bin/ostree"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _run(cmd, check=True):
    res = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60
    )
    if check and res.returncode != 0:
        raise AssertionError(
            f"Command failed (rc={res.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return res


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _build_deterministic_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for d in ("usr", "usr/lib", "usr/share", "usr/etc"):
            ti = tarfile.TarInfo(name=d)
            ti.type = tarfile.DIRTYPE
            ti.mode = 0o755
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0
            tf.addfile(ti)

        files = [
            ("usr/lib/os-release", b'ID="synthetic"\nVERSION_ID="1.0"\n'),
            ("usr/share/test.txt", b"synthetic-oci-fixture-payload\n"),
        ]
        for name, content in files:
            ti = tarfile.TarInfo(name=name)
            ti.type = tarfile.REGTYPE
            ti.mode = 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return buf.getvalue()


def _create_oci_layout(oci_dir: Path) -> dict:
    blobs_dir = oci_dir / "blobs" / "sha256"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    layer_bytes = _build_deterministic_tar()
    layer_sha = _sha256_bytes(layer_bytes)
    (blobs_dir / layer_sha).write_bytes(layer_bytes)

    config_obj = {
        "architecture": "arm64",
        "os": "linux",
        "created": "2026-01-01T00:00:00Z",
        "config": {
            "Labels": {
                "org.opencontainers.image.revision": "1" * 40,
                "org.opencontainers.image.version": "fixture-1",
            }
        },
        "rootfs": {
            "type": "layers",
            "diff_ids": [f"sha256:{layer_sha}"],
        },
        "history": [{"created": "2026-01-01T00:00:00Z", "created_by": "fixture"}],
    }
    config_bytes = _json_bytes(config_obj)
    config_sha = _sha256_bytes(config_bytes)
    (blobs_dir / config_sha).write_bytes(config_bytes)

    manifest_obj = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_sha}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": f"sha256:{layer_sha}",
                "size": len(layer_bytes),
            }
        ],
    }
    manifest_bytes = _json_bytes(manifest_obj)
    manifest_sha = _sha256_bytes(manifest_bytes)
    (blobs_dir / manifest_sha).write_bytes(manifest_bytes)

    index_obj = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_sha}",
                "size": len(manifest_bytes),
                "annotations": {"org.opencontainers.image.ref.name": "latest"},
                "platform": {"architecture": "arm64", "os": "linux"},
            }
        ],
    }
    (oci_dir / "index.json").write_bytes(_json_bytes(index_obj))
    (oci_dir / "oci-layout").write_bytes(_json_bytes({"imageLayoutVersion": "1.0.0"}))

    return {
        "layer_sha": layer_sha,
        "config_sha": config_sha,
        "manifest_sha": manifest_sha,
        "manifest_digest": f"sha256:{manifest_sha}",
    }


def _init_repo(repo_dir: Path):
    repo_dir.mkdir(parents=True, exist_ok=True)
    _run([OSTREE_BIN, "init", f"--repo={repo_dir}", "--mode=bare"])


def _pull_image(repo_dir: Path, oci_dir: Path, digestfile: Path, check=True):
    img_ref = f"ostree-unverified-image:oci:{oci_dir}:latest"
    cmd = [
        BOOTC_BIN,
        "internals",
        "ostree-ext",
        "container",
        "image",
        "pull",
        "--auth-anonymous",
        "--ostree-digestfile",
        str(digestfile),
        str(repo_dir),
        img_ref,
    ]
    return _run(cmd, check=check)


class UpdateOciPullTest(unittest.TestCase):
    @staticmethod
    def _object_files(repo):
        objects = repo / "objects"
        return {
            str(path.relative_to(objects)): path.read_bytes()
            for path in objects.rglob("*") if path.is_file()
        }

    def _validate_metadata(self, stage, digest, meta):
        md_res = _run([
            OSTREE_BIN, "show", f"--repo={stage}",
            "--print-metadata-key=ostree.manifest-digest", digest,
        ])
        self.assertEqual(ast.literal_eval(md_res.stdout.strip()), meta["manifest_digest"])
        cfg_res = _run([
            OSTREE_BIN, "show", f"--repo={stage}",
            "--print-metadata-key=ostree.container.image-config", digest,
        ])
        config = json.loads(ast.literal_eval(cfg_res.stdout.strip()))
        self.assertEqual(config.get("architecture"), "arm64")
        self.assertEqual(config.get("os"), "linux")
        labels = config.get("config", {}).get("Labels", {})
        self.assertEqual(labels.get("org.opencontainers.image.revision"), "1" * 40)
        self.assertEqual(labels.get("org.opencontainers.image.version"), "fixture-1")

    def test_reproducible_pull_and_metadata(self):
        with tempfile.TemporaryDirectory(prefix="oci-pull-") as tmp:
            base = Path(tmp)
            oci_dir = base / "oci"
            meta = _create_oci_layout(oci_dir)

            repo1 = base / "repo1"
            repo2 = base / "repo2"
            _init_repo(repo1)
            _init_repo(repo2)

            df1 = base / "digest1"
            df2 = base / "digest2"
            _pull_image(repo1, oci_dir, df1)
            _pull_image(repo2, oci_dir, df2)

            commit1 = df1.read_text(encoding="ascii").strip()
            commit2 = df2.read_text(encoding="ascii").strip()
            self.assertRegex(commit1, HEX64_RE)
            self.assertEqual(commit1, commit2)

            for repo in (repo1, repo2):
                _run([OSTREE_BIN, "fsck", f"--repo={repo}"])

            md_res = _run([
                OSTREE_BIN, "show", f"--repo={repo1}",
                "--print-metadata-key=ostree.manifest-digest", commit1
            ])
            self.assertEqual(ast.literal_eval(md_res.stdout.strip()), meta["manifest_digest"])

            cfg_res = _run([
                OSTREE_BIN, "show", f"--repo={repo1}",
                "--print-metadata-key=ostree.container.image-config", commit1
            ])
            cfg_parsed = json.loads(ast.literal_eval(cfg_res.stdout.strip()))
            self.assertEqual(cfg_parsed.get("architecture"), "arm64")
            labels = cfg_parsed.get("config", {}).get("Labels", {})
            self.assertEqual(labels.get("org.opencontainers.image.revision"), "1" * 40)
            self.assertEqual(labels.get("org.opencontainers.image.version"), "fixture-1")

            df1_cached = base / "digest1-cached"
            _pull_image(repo1, oci_dir, df1_cached)
            self.assertTrue(df1_cached.exists())
            self.assertEqual(df1_cached.read_text(encoding="ascii").strip(), commit1)

    def test_corrupted_layer_isolated_from_live_and_fresh_retry(self):
        with tempfile.TemporaryDirectory(prefix="oci-corrupt-") as tmp:
            base = Path(tmp)
            oci_dir = base / "oci"
            meta = _create_oci_layout(oci_dir)
            reference = base / "reference"
            _init_repo(reference)
            reference_digest = base / "reference-digest"
            _pull_image(reference, oci_dir, reference_digest)
            expected = reference_digest.read_text(encoding="ascii").strip()
            self.assertRegex(expected, HEX64_RE)

            destination = base / "destination"
            _init_repo(destination)
            refs_before = _run([OSTREE_BIN, "refs", f"--repo={destination}"]).stdout
            objects_before = self._object_files(destination)
            layer_path = oci_dir / "blobs" / "sha256" / meta["layer_sha"]
            original_bytes = layer_path.read_bytes()
            payload = b"synthetic-oci-fixture-payload\n"
            corrupted = bytearray(original_bytes)
            offset = corrupted.find(payload)
            self.assertNotEqual(offset, -1)
            corrupted[offset] ^= 0xFF
            layer_path.write_bytes(bytes(corrupted))
            origin = f"ostree-unverified-image:oci:{oci_dir}:latest"
            bad_work = base / "work-bad"
            bad_work.mkdir(mode=0o700)

            with self.assertRaises(image_source.ImageSourceError) as caught:
                image_source._import_verified(
                    destination, origin, bad_work, expected,
                    lambda stage, digest: self._validate_metadata(stage, digest, meta),
                )
            diagnostic = str(caught.exception)
            self.assertFalse((bad_work / "commit").exists(), diagnostic)
            self.assertFalse((bad_work / "import.log").exists(), diagnostic)
            self.assertTrue((bad_work / "pull.log").is_file(), diagnostic)
            bad_stage = bad_work / "staging-repo"
            self.assertTrue(bad_stage.is_dir(), diagnostic)
            self.assertEqual(_run([OSTREE_BIN, "refs", f"--repo={destination}"]).stdout, refs_before)
            self.assertEqual(self._object_files(destination), objects_before)
            bad_stage_objects = self._object_files(bad_stage)
            bad_stage_refs = _run([OSTREE_BIN, "refs", f"--repo={bad_stage}"]).stdout

            layer_path.write_bytes(original_bytes)
            good_work = base / "work-good"
            good_work.mkdir(mode=0o700)
            pull_log = image_source._import_verified(
                destination, origin, good_work, expected,
                lambda stage, digest: self._validate_metadata(stage, digest, meta),
            )
            self.assertEqual(pull_log, good_work / "pull.log")
            self.assertEqual((good_work / "commit").read_text(encoding="ascii").strip(), expected)
            self.assertTrue((good_work / "import.log").is_file())
            self.assertFalse((good_work / "staging-repo").exists())
            _run([OSTREE_BIN, "fsck", f"--repo={destination}"])
            cat = _run([
                OSTREE_BIN, "cat", f"--repo={destination}", expected,
                "/usr/share/test.txt",
            ])
            self.assertEqual(cat.stdout, payload.decode())
            self.assertEqual(_run([OSTREE_BIN, "refs", f"--repo={destination}"]).stdout, refs_before)
            self.assertEqual(self._object_files(bad_stage), bad_stage_objects)
            self.assertEqual(_run([OSTREE_BIN, "refs", f"--repo={bad_stage}"]).stdout, bad_stage_refs)

    def test_expected_commit_mismatch_rejected_before_live_import(self):
        with tempfile.TemporaryDirectory(prefix="oci-mismatch-") as tmp:
            base = Path(tmp)
            oci_dir = base / "oci"
            meta = _create_oci_layout(oci_dir)
            destination = base / "destination"
            _init_repo(destination)
            refs_before = _run([OSTREE_BIN, "refs", f"--repo={destination}"]).stdout
            objects_before = self._object_files(destination)
            work = base / "work"
            work.mkdir(mode=0o700)
            callback = mock.Mock()
            origin = f"ostree-unverified-image:oci:{oci_dir}:latest"
            with self.assertRaisesRegex(image_source.ImageSourceError, "does not match channel"):
                image_source._import_verified(destination, origin, work, "f" * 64, callback)
            callback.assert_not_called()
            self.assertFalse((work / "import.log").exists())
            self.assertEqual(_run([OSTREE_BIN, "refs", f"--repo={destination}"]).stdout, refs_before)
            self.assertEqual(self._object_files(destination), objects_before)


if __name__ == "__main__":
    unittest.main()
