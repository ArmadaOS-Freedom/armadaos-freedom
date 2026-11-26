"""Signed ARMADA_BOOT generations for every disk-boot board, TB710FU (board 3) included.

The manifest comes from the real build script (build_files/boot-disk-manifest.py) and is signed with a throwaway
P-256 key, so staging and verification run against the same 160-byte record a release ships. A wrong board, a wrong
key or a tampered file is refused before a generation is left behind.
"""

import base64
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
ARMADA_LIB = REPO / "system_files" / "usr" / "lib" / "armada"
for path in (str(ARMADA_LIB), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import test_update_channel_release as release  # noqa: E402
from updates import boot_disk_generation, models, signature  # noqa: E402

MANIFEST_SCRIPT = REPO / "build_files" / "boot-disk-manifest.py"
_COMMIT = "b" * 64
_BINDING = f"default/{_COMMIT}.0"
_POINTER = f"/ostree/boot.1/default/{_COMMIT}/0"


def _arm64_image(size: int) -> bytes:
    head = bytearray(64)
    struct.pack_into("<Q", head, 8, 0)            # text_offset
    struct.pack_into("<Q", head, 16, size)        # image_size
    struct.pack_into("<I", head, 56, 0x644D5241)  # ARM64 magic
    return bytes(head) + os.urandom(size - 64)


class BootDiskGenerationBoardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._keys = tempfile.TemporaryDirectory(prefix="armada-gen-keys-")
        keys = Path(cls._keys.name)
        cls.priv, cls.pub = release._generate_keypair(keys, "trusted")
        cls.other_priv, _ = release._generate_keypair(keys, "other")

    @classmethod
    def tearDownClass(cls):
        cls._keys.cleanup()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="armada-gen-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.kernel = self.work / "kernel.img"
        self.kernel.write_bytes(_arm64_image(4096))
        self.initramfs = self.work / "initramfs.img"
        self.initramfs.write_bytes(os.urandom(2048))
        self.dtb = self.work / "board.dtb"
        self.dtb.write_bytes(b"\xd0\x0d\xfe\xed" + os.urandom(508))
        self.boot = self.work / "boot-mount"
        (self.boot / "armada").mkdir(parents=True)
        self.binding = boot_disk_generation.encode_binding(_POINTER, _BINDING)

    def _manifest(self, model: str) -> bytes:
        out = self.work / f"manifest-{model}.bin"
        subprocess.run(
            [sys.executable, str(MANIFEST_SCRIPT), "--model", model,
             "--kernel", str(self.kernel), "--initramfs", str(self.initramfs),
             "--dtb", str(self.dtb), "--output", str(out)],
            check=True, capture_output=True)
        return out.read_bytes()

    def _capsule(self, model: str, key=None) -> dict:
        manifest = self._manifest(model)
        der = base64.b64decode(release._sign_bytes(manifest, key or self.priv))
        return {"manifest.bin": manifest, "manifest.sig": der, "board.dtb": self.dtb.read_bytes()}

    def _stage(self, model: str, capsule: dict) -> str:
        return boot_disk_generation.stage_generation(
            self.boot, model, kernel=self.kernel, initramfs=self.initramfs,
            binding=self.binding, capsule=capsule, trusted_key=self.pub)

    def _generations(self):
        return sorted(p.name for p in (self.boot / "armada").iterdir())

    def test_board_ids_follow_the_capability_table(self):
        self.assertEqual(
            dict(boot_disk_generation._MODEL_BOARD), {"TB322FC": 1, "TB323FU": 2, "TB710FU": 3})
        self.assertEqual(set(boot_disk_generation._MODEL_BOARD), set(models.DISK_BOOT_MODELS))

    def test_every_disk_boot_model_stages_and_verifies_its_own_board(self):
        for model, board in sorted(models.DISK_BOOT_BOARDS.items()):
            with self.subTest(model=model):
                capsule = self._capsule(model)
                self.assertEqual(struct.unpack_from("<I", capsule["manifest.bin"], 12)[0], board)
                name = self._stage(model, capsule)
                gen = self.boot / "armada" / name
                boot_disk_generation.verify_generation(gen, model, trusted_key=self.pub)
                self.assertEqual(
                    (gen / model / "manifest.bin").read_bytes(), capsule["manifest.bin"])

    def test_a_manifest_for_another_board_is_refused_before_staging(self):
        for model, other in (("TB710FU", "TB322FC"), ("TB322FC", "TB710FU"), ("TB710FU", "TB323FU")):
            with self.subTest(model=model, manifest_for=other):
                with self.assertRaisesRegex(ValueError, "board mismatch"):
                    self._stage(model, self._capsule(other))
                self.assertEqual([], self._generations())

    def test_a_staged_tb710fu_generation_does_not_verify_as_another_board(self):
        name = self._stage("TB710FU", self._capsule("TB710FU"))
        gen = self.boot / "armada" / name
        # The other boards look in their own directory, and a board-3 record never passes as board 1.
        (gen / "TB322FC").mkdir()
        for member in ("manifest.bin", "manifest.sig", "board.dtb"):
            (gen / "TB322FC" / member).write_bytes((gen / "TB710FU" / member).read_bytes())
        with self.assertRaisesRegex(ValueError, "board mismatch"):
            boot_disk_generation.verify_generation(gen, "TB322FC", trusted_key=self.pub)

    def test_a_signature_from_another_key_is_refused_before_staging(self):
        with self.assertRaises(signature.SignatureError):
            self._stage("TB710FU", self._capsule("TB710FU", key=self.other_priv))
        self.assertEqual([], self._generations())

    def test_a_tampered_manifest_is_refused_before_staging(self):
        capsule = self._capsule("TB710FU")
        tampered = bytearray(capsule["manifest.bin"])
        tampered[40] ^= 0x01  # a kernel digest byte, still a well-formed record
        capsule["manifest.bin"] = bytes(tampered)
        with self.assertRaises(signature.SignatureError):
            self._stage("TB710FU", capsule)
        self.assertEqual([], self._generations())

    def test_a_kernel_changed_after_staging_fails_verification(self):
        name = self._stage("TB710FU", self._capsule("TB710FU"))
        gen = self.boot / "armada" / name
        kernel = gen / "kernel.img"
        data = bytearray(kernel.read_bytes())
        data[-1] ^= 0x01
        kernel.write_bytes(bytes(data))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            boot_disk_generation.verify_generation(gen, "TB710FU", trusted_key=self.pub)


if __name__ == "__main__":
    unittest.main()
