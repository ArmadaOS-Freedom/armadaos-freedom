import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"))

from updates.decky_release import DeckyRelease, DeckyReleaseError, verify_payload

URL = "https://github.com/SteamDeckHomebrew/decky-loader/releases/download/v1/PluginLoader-arm64"


def make_elf(ei_class=2, ei_data=1, machine=183, total_len=80, magic=b"\x7fELF"):
    buf = bytearray(max(total_len, 20))
    buf[:4] = magic
    buf[4] = ei_class
    buf[5] = ei_data
    struct.pack_into("<H" if ei_data == 1 else ">H", buf, 18, machine)
    if total_len > 64:
        buf[64:] = b"payload-bytes!"[: total_len - 64].ljust(total_len - 64, b"X")
    return bytes(buf[:total_len])


def make_rel(data, size=None, sha256=None):
    return DeckyRelease(
        release_id=1,
        tag="v1",
        published_at="2024-01-01T00:00:00Z",
        asset_id=10,
        url=URL,
        size=len(data) if size is None else size,
        sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
    )


class TestVerifyPayload(unittest.TestCase):
    def test_valid_aarch64_elf_unchanged(self):
        data = make_elf()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "PluginLoader-arm64"
            p.write_bytes(data)
            verify_payload(make_rel(data), p)
            self.assertEqual(p.read_bytes(), data)

    def test_size_and_hash_mismatch_and_bad_meta(self):
        data = make_elf()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bin"
            p.write_bytes(data)
            for rel in [
                make_rel(data, size=len(data) + 1),
                make_rel(data, sha256=("0" if hashlib.sha256(data).hexdigest()[0] != "0" else "1") * 64),
                make_rel(data, sha256="BADSHA"),
                make_rel(data, size=0),
            ]:
                with self.subTest(rel=rel):
                    with self.assertRaises(DeckyReleaseError):
                        verify_payload(rel, p)
            with self.assertRaises(DeckyReleaseError):
                verify_payload(make_rel(data), Path(td) / "missing")

    def test_rejected_elf_headers_with_matching_size_and_hash(self):
        for bad in [
            make_elf(machine=62),
            make_elf(ei_class=1),
            make_elf(ei_data=2),
            make_elf(total_len=63),
            make_elf(magic=b"\x7fNOPE"[:4]),
        ]:
            with self.subTest(bad=bad[:20]):
                with tempfile.TemporaryDirectory() as td:
                    p = Path(td) / "bad"
                    p.write_bytes(bad)
                    with self.assertRaises(DeckyReleaseError):
                        verify_payload(make_rel(bad), p)


if __name__ == "__main__":
    unittest.main()
