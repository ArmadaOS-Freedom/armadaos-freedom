"""Unit tests for read-only update package media verification."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "system_files/usr/lib/armada"))

from updates.package_contract import PackageError  # noqa: E402
import updates.package_media as media  # noqa: E402


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _gzip_bytes(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _build_zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for item, raw in entries:
            if isinstance(item, zipfile.ZipInfo):
                zf.writestr(item, raw)
            else:
                zf.writestr(item, raw, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def _set_outer(desc: dict, archive_bytes: bytes, archive_path: Path | None = None) -> dict:
    desc["archive"] = {
        "fileName": "package.zip",
        "bytes": len(archive_bytes),
        "sha256": _sha256(archive_bytes),
    }
    if archive_path is not None:
        archive_path.write_bytes(archive_bytes)
    return desc


class PackageMediaTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)

    def _make_package(self, *, split_sizes: tuple[int, ...] = ()) -> tuple[Path, dict, dict[str, bytes]]:
        root_raw = b"root-image-payload-" * 8
        boot_raw = b"boot-image-payload-" * 6
        payloads = {
            "bundle.json": (b"{}", b"{}", "none", "manifest"),
            "manifests/tb322fc.json": (b"{}", b"{}", "none", "manifest"),
            "images/root.img.gz": (_gzip_bytes(root_raw), root_raw, "gzip", "linux-root"),
            "images/boot.img.gz": (_gzip_bytes(boot_raw), boot_raw, "gzip", "linux-boot"),
        }
        files = [
            {
                "path": path,
                "bytes": len(stored),
                "sha256": _sha256(stored),
                "expandedBytes": len(expanded),
                "expandedSha256": _sha256(expanded),
                "compression": comp,
                "role": role,
            }
            for path, (stored, expanded, comp, role) in payloads.items()
        ]
        stored_map = {p: s for p, (s, _, _, _) in payloads.items()}
        archive_bytes = _build_zip(list(stored_map.items()))
        archive_path = self.tmpdir / "package.zip"
        archive_path.write_bytes(archive_bytes)
        parts = []
        if split_sizes:
            offset = 0
            for idx, size in enumerate(split_sizes, 1):
                chunk = archive_bytes[offset:offset + size]
                offset += size
                name = f"package.zip.{idx:03d}"
                (self.tmpdir / name).write_bytes(chunk)
                parts.append({"fileName": name, "bytes": len(chunk), "sha256": _sha256(chunk)})
        desc = {
            "schemaVersion": 1,
            "kind": "freedom-universal-package",
            "product": "armadaos-freedom",
            "architecture": "aarch64",
            "releaseId": "2026.04.1",
            "version": "1.2.3",
            "sourceRevision": "a" * 40,
            "ociDigest": "sha256:" + "b" * 64,
            "ostreeCommit": "c" * 64,
            "minimumInstallerVersion": "1.0.0",
            "minimumUpdaterVersion": "1.0.0",
            "supportedModels": ["TB322FC"],
            "operations": ["install"],
            "archive": {"fileName": "package.zip", "bytes": len(archive_bytes), "sha256": _sha256(archive_bytes)},
            "parts": parts,
            "files": files,
            "modelManifests": {"TB322FC": "manifests/tb322fc.json"},
            "recoveryManifests": {},
            "deckySnapshot": None,
        }
        return archive_path, desc, stored_map

    def test_valid_archive_summaries_and_tree_unchanged(self) -> None:
        archive_path, desc, _ = self._make_package()
        before = {p.name: p.read_bytes() for p in self.tmpdir.iterdir()}
        res_full = media.verify_archive(archive_path, desc)
        res_fast = media.verify_archive(archive_path, desc, verify_expanded=False)
        after = {p.name: p.read_bytes() for p in self.tmpdir.iterdir()}
        self.assertEqual(res_full, {"archiveBytes": desc["archive"]["bytes"], "files": 4, "expandedVerified": True})
        self.assertEqual(res_fast, {"archiveBytes": desc["archive"]["bytes"], "files": 4, "expandedVerified": False})
        self.assertEqual(before, after)

    def test_split_parts_valid_and_failure_modes(self) -> None:
        archive_path, desc_no_parts, _ = self._make_package()
        with self.assertRaises(PackageError):
            media.verify_parts(self.tmpdir, desc_no_parts)
        total = archive_path.stat().st_size
        archive_path.unlink()
        s1 = total // 3
        s2 = total // 3
        _, desc, _ = self._make_package(split_sizes=(s1, s2, total - s1 - s2))
        archive_path.unlink()
        before_names = sorted(p.name for p in self.tmpdir.iterdir())
        self.assertEqual(media.verify_parts(self.tmpdir, desc), {"bytes": total, "parts": 3})
        self.assertEqual(sorted(p.name for p in self.tmpdir.iterdir()), before_names)

        bad_whole = dict(desc, archive=dict(desc["archive"], sha256="0" * 64))
        with self.assertRaisesRegex(PackageError, "concatenated parts"):
            media.verify_parts(self.tmpdir, bad_whole)

        p2 = self.tmpdir / "package.zip.002"
        orig_p2 = p2.read_bytes()
        p2.write_bytes(bytes([orig_p2[0] ^ 0xFF]) + orig_p2[1:])
        with self.assertRaisesRegex(PackageError, "hash mismatch"):
            media.verify_parts(self.tmpdir, desc)

        p2.write_bytes(orig_p2[:-1])
        with self.assertRaisesRegex(PackageError, "size"):
            media.verify_parts(self.tmpdir, desc)

        p2.unlink()
        with self.assertRaisesRegex(PackageError, "cannot open"):
            media.verify_parts(self.tmpdir, desc)

    def test_outer_and_payload_hash_or_size_mismatches(self) -> None:
        archive_path, desc, stored = self._make_package()
        bad_hash = dict(desc, archive=dict(desc["archive"], sha256="f" * 64))
        with self.assertRaisesRegex(PackageError, "archive hash mismatch"):
            media.verify_archive(archive_path, bad_hash)

        raw = archive_path.read_bytes()
        archive_path.write_bytes(raw + b"x")
        with self.assertRaisesRegex(PackageError, "archive size"):
            media.verify_archive(archive_path, desc)

        tampered = dict(stored, **{"bundle.json": b"[]"})
        _set_outer(desc, _build_zip(list(tampered.items())), archive_path)
        with self.assertRaisesRegex(PackageError, "hash mismatch"):
            media.verify_archive(archive_path, desc)

    def test_expanded_gzip_mismatches_and_truncated_stream(self) -> None:
        archive_path, desc, stored = self._make_package()
        bad_hash_desc = {**desc, "files": [dict(f) for f in desc["files"]]}
        bad_hash_desc["files"][2]["expandedSha256"] = "e" * 64
        with self.assertRaisesRegex(PackageError, "expanded hash/size mismatch"):
            media.verify_archive(archive_path, bad_hash_desc)
        self.assertFalse(media.verify_archive(archive_path, bad_hash_desc, verify_expanded=False)["expandedVerified"])

        low_size_desc = {**desc, "files": [dict(f) for f in desc["files"]]}
        low_size_desc["files"][2]["expandedBytes"] = 5
        with self.assertRaisesRegex(PackageError, "expanded hash/size mismatch"):
            media.verify_archive(archive_path, low_size_desc)

        trunc_gz = stored["images/root.img.gz"][:-6]
        trunc_stored = dict(stored, **{"images/root.img.gz": trunc_gz})
        trunc_desc = {**desc, "files": [dict(f) for f in desc["files"]]}
        trunc_desc["files"][2]["bytes"] = len(trunc_gz)
        trunc_desc["files"][2]["sha256"] = _sha256(trunc_gz)
        _set_outer(trunc_desc, _build_zip(list(trunc_stored.items())), archive_path)
        with self.assertRaisesRegex(PackageError, "gzip error"):
            media.verify_archive(archive_path, trunc_desc)

    def test_zip_entry_anomalies_and_special_modes(self) -> None:
        archive_path, desc, stored = self._make_package()
        items = list(stored.items())

        def check_zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]], pattern: str) -> None:
            _set_outer(desc, _build_zip(entries), archive_path)
            with self.assertRaisesRegex(PackageError, pattern):
                media.verify_archive(archive_path, desc)

        check_zip(items[:-1], "missing ZIP entries")
        check_zip(items + [("extra.txt", b"1")], "unexpected ZIP entries")
        check_zip(items + [("bundle.json", b"{}")], "duplicate ZIP entries")
        check_zip([("BUNDLE.JSON" if k == "bundle.json" else k, v) for k, v in items], "missing ZIP entries")

        deflated = zipfile.ZipInfo("bundle.json")
        deflated.compress_type = zipfile.ZIP_DEFLATED
        check_zip([(deflated if k == "bundle.json" else k, v) for k, v in items], "not ZIP_STORED")

        for mode_bits, pattern in ((stat.S_IFLNK | 0o777, "symlink entry"), (stat.S_IFIFO | 0o600, "non-regular entry")):
            special = zipfile.ZipInfo("bundle.json")
            special.compress_type = zipfile.ZIP_STORED
            special.external_attr = mode_bits << 16
            check_zip([(special if k == "bundle.json" else k, v) for k, v in items], pattern)

    def test_filesystem_non_regular_and_missing_directory(self) -> None:
        archive_path, single_desc, _ = self._make_package()
        link_path = self.tmpdir / "link.zip"
        link_path.symlink_to(archive_path)
        with self.assertRaises(PackageError):
            media.verify_archive(link_path, single_desc)

        fifo_path = self.tmpdir / "pipe.zip"
        os.mkfifo(fifo_path)
        with self.assertRaises(PackageError):
            media.verify_archive(fifo_path, single_desc)

        desc = dict(single_desc)
        desc["parts"] = [{"fileName": "package.zip.001", "bytes": single_desc["archive"]["bytes"], "sha256": single_desc["archive"]["sha256"]}]
        with self.assertRaisesRegex(PackageError, "part verification failed"):
            media.verify_parts(self.tmpdir / "does-not-exist", desc)

    def test_bounded_stream_and_mtime_change_detection(self) -> None:
        payload = b"0123456789abcdef" * 8

        class StrictStream:
            def __init__(self, data: bytes) -> None:
                self._buf = data
                self._pos = 0

            def read(self, size: int = -1) -> bytes:
                if size <= 0 or size > media._CHUNK:
                    raise AssertionError(f"invalid read size: {size}")
                chunk = self._buf[self._pos:self._pos + size]
                self._pos += len(chunk)
                return chunk

        digest, count = media._sha256_stream(StrictStream(payload), limit=25)
        self.assertEqual((digest, count), (_sha256(payload[:25]), 25))
        digest_all, count_all = media._sha256_stream(StrictStream(payload))
        self.assertEqual((digest_all, count_all), (_sha256(payload), len(payload)))

        archive_path, desc, _ = self._make_package()
        real_hash = media._sha256_stream

        def touch_during_hash(stream, limit=None):
            res = real_hash(stream, limit=limit)
            st = archive_path.stat()
            os.utime(archive_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return res

        with patch.object(media, "_sha256_stream", side_effect=touch_during_hash):
            with self.assertRaisesRegex(PackageError, "changed during read"):
                media.verify_archive(archive_path, desc)


if __name__ == "__main__":
    unittest.main()
