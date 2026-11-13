"""Real-filesystem tests for private offline session storage."""

import json
import os
import sys
import tempfile
import time
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as release  # noqa: E402
import test_update_package_contract as fixture  # noqa: E402
from updates import offline_session, trial_record  # noqa: E402
from updates.package_contract import validate_package  # noqa: E402

_MIB = 1024 * 1024


class OfflineSessionStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmpdir.name)
        cls.priv_key, cls.pub_key = release._generate_keypair(cls.tmp, "trusted")
        cls.other_priv, cls.other_pub = release._generate_keypair(cls.tmp, "other")

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def _inspect(self, model="TB322FC"):
        return lambda: {
            "model": model, "actualSlot": "a", "inactiveSlot": "b",
            "deployment": "stable", "recoveryPartUuid": "00000000-0000",
        }

    def _signed_package(self, data=None, signer=None):
        data = data if data is not None else os.urandom(4096)
        pkg = release._upgrade_package_dict()
        pkg["minimumInstallerVersion"] = "0.8.0"
        pkg["minimumUpdaterVersion"] = "0.8.0"
        for record in pkg["files"]:
            if record["role"] == "linux-root":
                record["bytes"] = len(data)
                record["sha256"] = sha256(data).hexdigest()
                record["expandedBytes"] = len(data) * 2
                record["expandedSha256"] = sha256(data + b"x").hexdigest()
        doc = json.dumps(validate_package(pkg), sort_keys=True, separators=(",", ":")).encode()
        key = signer if signer is not None else self.priv_key
        return doc, release._sign_bytes(doc, key), data

    def _store(self, base, model="TB322FC", key=None):
        patcher = mock.patch.object(
            offline_session.armada_os, "snapshot", return_value={"channel": "stable"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return offline_session.OfflineSessionStore(
            base=base, trusted_key=key or self.pub_key, inspect=self._inspect(model))

    def _payload_file(self, session, receipt):
        return session.joinpath("incoming", *receipt["payload"]["path"].split("/"))

    @staticmethod
    def _age(path, seconds):
        when = time.time() - seconds
        os.utime(path, (when, when))

    @staticmethod
    def _space(free):
        return SimpleNamespace(f_bavail=free // 4096, f_frsize=4096)

    def test_upload_roundtrip_and_seal(self):
        for model in ("TB322FC", "TB323FU"):
            with self.subTest(model=model):
                doc, sig, data = self._signed_package()
                base = self.tmp / model / "offline"
                store = self._store(base, model)
                receipt = store.begin(doc, sig)
                session = base / receipt["sessionId"]
                self.assertEqual((session / "package.json").read_bytes(), doc)
                store.upload(receipt["sessionId"], 0, data[:1500])
                for offset, chunk in ((99, data[:10]), (len(data) - 5, data[-6:])):
                    with self.assertRaises(offline_session.OfflineSessionError):
                        store.upload(receipt["sessionId"], offset, chunk)
                for bad in (True, 1.0, "0", -1):
                    with self.assertRaises(ValueError):
                        store.upload(receipt["sessionId"], bad, b"x")
                reopened = self._store(base, model)
                self.assertEqual(reopened.status(receipt["sessionId"])["receivedBytes"], 1500)
                store.upload(receipt["sessionId"], 1500, data[1500:])
                sealed = store.seal(receipt["sessionId"])
                self.assertEqual(sealed["state"], "uploaded")
                with self.assertRaises(offline_session.OfflineSessionError):
                    store.upload(receipt["sessionId"], len(data), b"z")

    def test_info_advertises_boot_policy_across_a_transfer(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "info-transfer" / "offline"
        store = self._store(base)
        before = store.info()
        self.assertEqual(before['bootPolicy'], 'dual-boot-no-slot-control')
        receipt = store.begin(doc, sig)
        session_id = receipt["sessionId"]
        store.upload(session_id, 0, data)
        store.seal(session_id)
        after = store.info()
        self.assertEqual(after['bootPolicy'], trial_record.BOOT_POLICY)
        self.assertEqual(before['bootPolicy'], after['bootPolicy'])

    def test_begin_rejects_invalid_packages_before_session_creation(self):
        doc, sig, data = self._signed_package()
        good_sig = sig
        _, other_sig, _ = self._signed_package(data, signer=self.other_priv)
        base = self.tmp / "reject" / "offline"
        store = self._store(base)
        for label, document, signature, inspector in (
            ("signature", doc, other_sig, self._inspect()),
            ("model", doc, good_sig, self._inspect("OTHERMODEL")),
        ):
            with self.subTest(label=label):
                store._inspect = inspector
                with self.assertRaises((offline_session.OfflineSessionError, ValueError)):
                    store.begin(document, signature)
        with self.subTest(label="oversized-document"):
            with self.assertRaises(ValueError):
                store.begin(b"x" * (2 * _MIB + 1), good_sig)
        self.assertFalse(base.exists())

    def test_begin_rejects_denied_operation(self):
        doc, sig, _data = self._signed_package()
        package = json.loads(doc)
        package["operations"] = ["install"]
        denied_doc = json.dumps(
            package, sort_keys=True, separators=(",", ":")).encode()
        denied_sig = release._sign_bytes(denied_doc, self.priv_key)
        base = self.tmp / "denied" / "offline"
        store = self._store(base)
        with self.assertRaisesRegex(ValueError, "operation"):
            store.begin(denied_doc, denied_sig)
        self.assertFalse(base.exists())

    def test_seal_resets_only_complete_corruption(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "seal" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        payload = self._payload_file(base / sid, receipt)
        payload.write_bytes(data[:-3] + b"abc")
        with self.assertRaisesRegex(offline_session.OfflineSessionError, "reset"):
            store.seal(sid)
        self.assertFalse(payload.exists())
        status = store.status(sid)
        self.assertEqual(status["state"], "uploading")
        self.assertEqual(status["receivedBytes"], 0)
        payload.write_bytes(data[:100])
        with self.assertRaises(offline_session.OfflineSessionError):
            store.seal(sid)
        self.assertEqual(payload.stat().st_size, 100)
        payload.write_bytes(data + b"x")
        with self.assertRaises(offline_session.OfflineSessionError):
            store.status(sid)
        self.assertEqual(payload.read_bytes(), data + b"x")

    def test_seal_resets_complete_corruption_for_a_full_retry(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "seal-retry" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        payload = self._payload_file(base / sid, receipt)
        corrupt = data[:-1] + bytes([data[-1] ^ 0xFF])
        payload.write_bytes(corrupt)
        with self.assertRaises(offline_session.OfflineSessionError):
            store.seal(sid)
        store.upload(sid, 0, data[:700])
        store.upload(sid, 700, data[700:])
        self.assertEqual(store.seal(sid)["state"], "uploaded")

    def test_seal_keeps_partial_upload_resumable(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "seal-partial" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data[:128])
        payload = self._payload_file(base / sid, receipt)
        with self.assertRaises(offline_session.OfflineSessionError):
            store.seal(sid)
        self.assertEqual(payload.read_bytes(), data[:128])
        store.upload(sid, 128, data[128:])
        self.assertEqual(store.seal(sid)["state"], "uploaded")

    def test_upload_rejects_unsafe_payload_targets_and_session_ids(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "unsafe" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        payload = self._payload_file(base / sid, receipt)
        with self.subTest(label="fifo"):
            os.mkfifo(payload)
            with self.assertRaises((OSError, offline_session.OfflineSessionError)):
                store.upload(sid, 0, data[:16])
            payload.unlink()
        with self.subTest(label="symlink"):
            payload.symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                store.upload(sid, 0, data[:16])
            payload.unlink()
        for bad_id in ("ZZZ", "../" + sid, 123, None, sid.upper()):
            with self.subTest(sessionId=bad_id):
                with self.assertRaises(ValueError):
                    store.upload(bad_id, 0, b"x")

    def test_begin_reclaims_stale_upload_only_under_space_pressure(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "reclaim" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        payload = self._payload_file(base / sid, receipt)

        store.begin(doc, sig)
        self.assertTrue(payload.exists())

        self._age(base / sid / "receipt.json", 2 * 86400)
        self._age(payload, 2 * 86400)
        with mock.patch.object(
                offline_session.os, "statvfs",
                side_effect=[self._space(0), os.statvfs(base)]):
            store.begin(doc, sig)

        self.assertFalse(payload.exists())
        self.assertTrue((base / sid / "receipt.json").exists())
        resumed = store.status(sid)
        self.assertEqual(resumed["state"], "uploading")
        self.assertEqual(resumed["receivedBytes"], 0)

    def test_begin_leaves_active_and_slow_uploads_alone(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "active" / "offline"
        store = self._store(base)
        slow = store.begin(doc, sig)
        slow_sid = slow["sessionId"]
        store.upload(slow_sid, 0, data)
        slow_payload = self._payload_file(base / slow_sid, slow)
        # The receipt is old but the payload write is recent: still an upload.
        self._age(base / slow_sid / "receipt.json", 2 * 86400)

        fresh = store.begin(doc, sig)
        fresh_sid = fresh["sessionId"]
        store.upload(fresh_sid, 0, data[:256])
        fresh_payload = self._payload_file(base / fresh_sid, fresh)

        with mock.patch.object(
                offline_session.os, "statvfs",
                side_effect=[self._space(0), self._space(0)]):
            with self.assertRaises(offline_session.OfflineSessionError):
                store.begin(doc, sig)

        self.assertEqual(slow_payload.read_bytes(), data)
        self.assertEqual(fresh_payload.read_bytes(), data[:256])
        self.assertEqual(store.status(slow_sid)["receivedBytes"], len(data))
        self.assertEqual(store.status(fresh_sid)["receivedBytes"], 256)

    def test_begin_does_not_wait_for_a_locked_session(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "locked" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        payload = self._payload_file(base / sid, receipt)
        self._age(base / sid / "receipt.json", 2 * 86400)
        self._age(payload, 2 * 86400)
        with store.session_lock(sid):
            with mock.patch.object(
                    offline_session.os, "statvfs",
                    side_effect=[self._space(0), self._space(0)]):
                with self.assertRaises(offline_session.OfflineSessionError):
                    store.begin(doc, sig)
        self.assertTrue(payload.exists())

    def test_begin_preserves_transaction_symlink_and_ambiguous_sessions(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "preserve" / "offline"
        store = self._store(base)

        first = store.begin(doc, sig)
        tx_sid = first["sessionId"]
        store.upload(tx_sid, 0, data)
        tx_payload = self._payload_file(base / tx_sid, first)
        (base / tx_sid / "transaction").mkdir(0o700)
        self._age(base / tx_sid / "receipt.json", 2 * 86400)
        self._age(tx_payload, 2 * 86400)

        second = store.begin(doc, sig)
        link_sid = second["sessionId"]
        store.upload(link_sid, 0, data)
        link_payload = self._payload_file(base / link_sid, second)
        target = base / "target"
        target.write_bytes(data)
        link_payload.unlink()
        link_payload.symlink_to(target)
        self._age(base / link_sid / "receipt.json", 2 * 86400)

        third = store.begin(doc, sig)
        state_sid = third["sessionId"]
        store.upload(state_sid, 0, data)
        state_payload = self._payload_file(base / state_sid, third)
        self._age(base / state_sid / "receipt.json", 2 * 86400)
        self._age(state_payload, 2 * 86400)
        with store.session_lock(state_sid) as session:
            receipt = store.read_receipt(session)
            receipt["state"] = "prepared"
            store.update_receipt(session, receipt)

        fourth = store.begin(doc, sig)
        ack_sid = fourth["sessionId"]
        store.upload(ack_sid, 0, data)
        ack_payload = self._payload_file(base / ack_sid, fourth)
        (base / ack_sid / "ack.json").write_text("{}")
        self._age(base / ack_sid / "receipt.json", 2 * 86400)
        self._age(ack_payload, 2 * 86400)

        fifth = store.begin(doc, sig)
        big_sid = fifth["sessionId"]
        store.upload(big_sid, 0, data)
        big_payload = self._payload_file(base / big_sid, fifth)
        (base / big_sid / "package.json").write_bytes(b"x" * (2 * _MIB + 1))
        self._age(base / big_sid / "receipt.json", 2 * 86400)
        self._age(big_payload, 2 * 86400)

        with mock.patch.object(
                offline_session.os, "statvfs",
                side_effect=[self._space(0), self._space(0)]):
            with self.assertRaises(offline_session.OfflineSessionError):
                store.begin(doc, sig)

        self.assertTrue(tx_payload.exists())
        self.assertTrue((base / tx_sid / "transaction").is_dir())
        self.assertTrue(link_payload.is_symlink())
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue(state_payload.exists())
        self.assertTrue(ack_payload.exists())
        self.assertTrue(big_payload.exists())

    def test_begin_reclaims_completed_payload_proven_unused(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "complete" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        store.seal(sid)
        with store.session_lock(sid) as session:
            saved = store.read_receipt(session)
            saved.update(state="up_to_date", jobOperation="prepare",
                         result={"state": "up_to_date"})
            store.update_receipt(session, saved)
        payload = self._payload_file(base / sid, receipt)
        self._age(base / sid / "receipt.json", 2 * 86400)
        self._age(payload, 2 * 86400)

        with mock.patch.object(
                offline_session.os, "statvfs",
                side_effect=[self._space(0), os.statvfs(base)]):
            store.begin(doc, sig)

        self.assertFalse(payload.exists())
        self.assertTrue((base / sid).is_dir())
        self.assertEqual(store.status(sid)["state"], "up_to_date")

    def _export_session(self, state="awaiting_recovery_export"):
        doc, sig, data = self._signed_package()
        base = self.tmp / "export" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        store.seal(sid)
        tx = base / sid / "transaction"
        tx.mkdir(0o700)
        blob = os.urandom(2 * _MIB + _MIB + 17)
        (tx / "boot").mkdir(0o700)
        (tx / "boot" / "recovery.img").write_bytes(blob)
        manifest = {"files": [{"path": "boot/recovery.img",
                               "bytes": len(blob), "sha256": sha256(blob).hexdigest()}]}
        (tx / "recovery-manifest.json").write_text(json.dumps(manifest))
        (tx / "status.json").write_text(json.dumps({"state": state}))
        (tx / "plan.json").write_text('{"plan": true}')
        (tx / "trust.json").write_text("secret")
        return store, sid, blob, tx

    def test_export_streams_only_requested_range(self):
        store, sid, blob, tx = self._export_session()
        real_pread = os.pread
        calls = []

        def counting_pread(fd, count, offset):
            calls.append(count)
            return real_pread(fd, count, offset)

        with mock.patch.object(offline_session.os, "pread", side_effect=counting_pread):
            for offset, length in ((0, _MIB), (_MIB, _MIB),
                                   (len(blob) - 10, 10), (len(blob) - 5, _MIB)):
                with self.subTest(offset=offset, length=length):
                    chunk = store.export(sid, "boot/recovery.img", offset, length)
                    self.assertEqual(chunk, blob[offset:offset + length])
            self.assertEqual(store.export(sid, "plan.json", 0, 8), (tx / "plan.json").read_bytes()[:8])
        self.assertTrue(calls)
        self.assertTrue(all(n <= _MIB for n in calls))
        self.assertLessEqual(sum(calls), 2 * _MIB + _MIB + 32)

    def test_export_rejects_unlisted_and_unsafe_paths(self):
        store, sid, blob, tx = self._export_session()
        for path in ("trust.json", "../receipt.json", "boot/../plan.json",
                     "/boot/recovery.img", "boot//recovery.img", "missing.img"):
            with self.subTest(path=path):
                with self.assertRaises((offline_session.OfflineSessionError, ValueError)):
                    store.export(sid, path, 0, 16)
        link = tx / "boot" / "link.img"
        link.symlink_to("../plan.json")
        (tx / "recovery-manifest.json").write_text(json.dumps(
            {"files": [{"path": "boot/link.img", "bytes": 13,
                       "sha256": sha256(b'{"plan": true}').hexdigest()}]}))
        with self.assertRaises(OSError):
            store.export(sid, "boot/link.img", 0, 4)
        link.unlink()

    def test_export_length_and_state_handling(self):
        store, sid, blob, tx = self._export_session()
        self.assertEqual(store.export(sid, "boot/recovery.img", len(blob), 16), b"")
        with self.assertRaises(ValueError):
            store.export(sid, "boot/recovery.img", len(blob) + 1, 4)
        (tx / "status.json").write_text(json.dumps({"state": "recovery_required"}))
        self.assertEqual(store.export(sid, "boot/recovery.img", 5, 5), blob[5:10])
        image = tx / "boot" / "recovery.img"
        image.write_bytes(blob[:-5])
        with self.assertRaises(offline_session.OfflineSessionError):
            store.export(sid, "boot/recovery.img", 0, 4)
        (tx / "status.json").write_text(json.dumps({"state": "complete"}))
        with self.assertRaises(offline_session.OfflineSessionError):
            store.export(sid, "boot/recovery.img", 0, 4)


if __name__ == "__main__":
    unittest.main()
