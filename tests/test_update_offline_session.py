"""Real-filesystem tests for private offline session storage."""

import base64
import errno
import json
import os
import sys
import tempfile
import unittest
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
sys.path.insert(0, str(ROOT / "tests"))

import test_update_channel_release as release  # noqa: E402
import test_update_package_contract as fixture  # noqa: E402
from updates import offline_rpc, offline_session  # noqa: E402
from updates.package_contract import validate_package  # noqa: E402

_MIB = 1024 * 1024
# Captured before any test replaces it, so a multi-device fixture can still ask
# the real filesystem about every path it does not overrule.
_REAL_STAT = os.stat


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

    def _store(self, base, model="TB322FC", key=None, sysroot_path=None,
               inspect=None, snapshot=None):
        patcher = mock.patch.object(
            offline_session.armada_os, "snapshot",
            return_value=({"channel": "stable"} if snapshot is None else snapshot))
        patcher.start()
        self.addCleanup(patcher.stop)
        kwargs = {} if sysroot_path is None else {"sysroot_path": sysroot_path}
        return offline_session.OfflineSessionStore(
            base=base, trusted_key=key or self.pub_key,
            inspect=self._inspect(model) if inspect is None else inspect,
            **kwargs)

    def _payload_file(self, session, receipt):
        return session.joinpath("incoming", *receipt["payload"]["path"].split("/"))

    def test_info_reports_genuine_legacy_bootable_slots(self):
        for target, attribute, value in (
            (offline_session.os, "geteuid", mock.Mock(return_value=0)),
            (offline_session.armada_os, "snapshot",
             mock.Mock(return_value={"channel": "stable"})),
        ):
            patcher = mock.patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The live legacy device: selected A bootable, inactive B unbootable.
        report = {
            "model": "TB322FC", "actualSlot": None, "inactiveSlot": None,
            "selectedSlot": "a", "bootContext": "legacy-controller",
            "deployment": "stable", "recoveryPartUuid": "00000000-0000",
            "bootableSlots": {"a": True, "b": False},
        }
        base = self.tmp / "legacy-info" / "offline"
        info = offline_session.OfflineSessionStore(
            base=base, trusted_key=self.pub_key,
            info_reader=lambda: dict(report)).info()
        self.assertEqual(info["bootableSlots"], {"a": True, "b": False})
        self.assertEqual(info["selectedSlot"], "a")
        self.assertIsNone(info["actualSlot"])
        self.assertIsNone(info["inactiveSlot"])
        self.assertFalse(base.exists())

        invented = dict(report)
        del invented["bootableSlots"]
        with self.assertRaises(offline_session.OfflineSessionError):
            offline_session.OfflineSessionStore(
                base=base, trusted_key=self.pub_key,
                info_reader=lambda: dict(invented)).info()

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

    def test_seal_rejects_mismatched_payload_and_retains_file(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "seal" / "offline"
        store = self._store(base)
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        store.upload(sid, 0, data)
        payload = self._payload_file(base / sid, receipt)
        payload.write_bytes(data[:-3] + b"abc")
        with self.assertRaises(offline_session.OfflineSessionError):
            store.seal(sid)
        self.assertTrue(payload.exists())
        payload.write_bytes(data[:100])
        with self.assertRaises(offline_session.OfflineSessionError):
            store.seal(sid)
        self.assertEqual(payload.stat().st_size, 100)
        payload.write_bytes(data + b"x")
        with self.assertRaises(offline_session.OfflineSessionError):
            store.status(sid)
        self.assertEqual(payload.read_bytes(), data + b"x")

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

    _MINIMAL_REPOSITORY_CONFIG = "[core]\nrepo_version=1\n"

    def _write_repository(self, sysroot, config=None):
        """Create the minimal OSTree repository metadata preflight accepts.

        ``objects`` is the directory libostree requires and ``config`` names
        the supported format, so every test that is not about repository
        validity starts from a repository an actual prepare could open.
        ``config`` defaults to the bare format; reserve tests override it.
        """
        repository = sysroot / "ostree" / "repo"
        repository.mkdir(parents=True, exist_ok=True)
        (repository / "objects").mkdir(exist_ok=True)
        (repository / "config").write_text(
            self._MINIMAL_REPOSITORY_CONFIG if config is None else config)
        return repository

    def _preflight_sysroot(self, name):
        sysroot = (self.tmp / ("sysroot-" + name)).resolve()
        self._write_repository(sysroot)
        return sysroot

    @staticmethod
    def _preflight_required(data, stored=0):
        """Upload remainder plus the expansion, extraction and import allowances.

        Each of the last three is planned with the authenticated expanded size;
        the repository reserve is charged separately.
        """
        return (len(data) - stored) + 3 * (2 * len(data))

    def _preflight_identity(self, model="TB322FC"):
        """A realistic witness identity: strict slots and a canonical binding."""
        return lambda: {
            "model": model, "actualSlot": "a", "inactiveSlot": "b",
            "deployment": "default/%s.0" % ("a" * 64),
            "recoveryPartUuid": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        }

    @staticmethod
    def _preflight_snapshot():
        return {"channel": "stable",
                "repository": offline_session.armada_os.FREEDOM,
                "staged": None, "rollback_queued": False}

    def _preflight_store(self, base, free_bytes, model="TB322FC", sysroot_path=None,
                         snapshot=None, total_bytes=0):
        """A store whose read-only preflight sees the given free space.

        The inspector, snapshot and deployment binding are realistic; the
        updater dependencies and the recovery journal stay read-only fixtures
        so the only variable left is the storage accounting under test.
        ``total_bytes`` sizes the filesystem for the OSTree repository reserve,
        which stays unset here so the plain byte arithmetic is exact.
        """
        store = self._store(
            base, sysroot_path=sysroot_path or self._preflight_sysroot("default"),
            inspect=self._preflight_identity(model),
            snapshot=self._preflight_snapshot() if snapshot is None else snapshot)
        for patcher in (
            mock.patch.object(
                offline_session.OfflineSessionStore,
                "_check_updater_dependencies"),
            mock.patch.object(
                offline_session.offline_prepare, "_check_recovery",
                return_value=SimpleNamespace(
                    layout=SimpleNamespace(descriptor_version=3))),
            mock.patch.object(
                offline_session.os, "statvfs",
                return_value=SimpleNamespace(
                    f_bavail=free_bytes, f_blocks=total_bytes, f_frsize=1)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return store

    def _storage_store(self, base, sysroot, layout, snapshot=None):
        """A preflight store whose paths report the filesystems in ``layout``.

        ``layout`` is a sequence of ``(prefix, st_dev, free_bytes, total_bytes)``
        in match order, so a test can place the session storage and the import
        destination on different filesystems while every other read keeps
        touching the real filesystem.
        """
        store = self._store(
            base, sysroot_path=sysroot, inspect=self._preflight_identity(),
            snapshot=self._preflight_snapshot() if snapshot is None else snapshot)
        real_stat = _REAL_STAT

        def owner(path):
            text = str(Path(path))
            for prefix, device, free, total in layout:
                if text == prefix or text.startswith(prefix + os.sep):
                    return device, free, total
            return None

        def fake_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            found = owner(path)
            if found is None:
                return result
            # st_dev is index 2 of stat_result's first ten fields; index 0 is
            # st_mode, so overwriting it would erase the file type.
            values = list(result)
            values[2] = found[0]
            return os.stat_result(values)

        def fake_statvfs(path):
            found = owner(path)
            if found is None:
                raise AssertionError("unexpected statvfs: %s" % path)
            return SimpleNamespace(f_bavail=found[1], f_blocks=found[2], f_frsize=1)

        for patcher in (
            mock.patch.object(
                offline_session.OfflineSessionStore,
                "_check_updater_dependencies"),
            mock.patch.object(
                offline_session.offline_prepare, "_check_recovery",
                return_value=SimpleNamespace(
                    layout=SimpleNamespace(descriptor_version=3))),
            # These fixtures model non-Btrfs filesystems: the block-device
            # fallback must key the layout below, on any host filesystem.
            mock.patch.object(
                offline_session.fcntl, "ioctl",
                side_effect=OSError(errno.ENOTTY, "not a btrfs filesystem")),
            mock.patch.object(offline_session.os, "stat", side_effect=fake_stat),
            mock.patch.object(offline_session.os, "statvfs", side_effect=fake_statvfs),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return store

    def _session_for_preflight(self, base, state="prepared", received=None):
        """Create a real session, optionally leave it partially uploaded."""
        doc, sig, data = self._signed_package()
        store = self._store(base, inspect=self._preflight_identity())
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        keep = len(data) if received is None else received
        store.upload(sid, 0, data[:keep])
        if keep == len(data):
            store.seal(sid)
        session = base / sid
        receipt = store.read_receipt(session)
        receipt["state"] = state
        store.update_receipt(session, receipt)
        return sid, doc, sig, data

    def _write_completed_expansion(self, base, sid, doc, expanded, *,
                                   descriptor_sha=None, image_size=None,
                                   status_state="awaiting_recovery_export"):
        """Write the bound transaction documents a completed prepare leaves."""
        transaction = base / sid / "transaction"
        (transaction / "import").mkdir(parents=True, mode=0o700)
        (transaction / "import" / "linux-root.img").write_bytes(
            b"\0" * (expanded if image_size is None else image_size))
        (transaction / "prepared.json").write_text(json.dumps({
            "schemaVersion": 1, "operation": "offline-upgrade",
            "transactionId": "transaction-1", "plan": "plan.json",
            "packageDescriptor": "package.json",
            "packageDescriptorSha256": (
                sha256(doc).hexdigest()
                if descriptor_sha is None else descriptor_sha),
        }))
        (transaction / "status.json").write_text(json.dumps({
            "schemaVersion": 1, "transactionId": "transaction-1",
            "state": status_state}))

    @staticmethod
    def _tree(path):
        entries = []
        for root, dirs, files in os.walk(path):
            for name in sorted(dirs) + sorted(files):
                full = Path(root) / name
                info = full.lstat()
                entries.append((str(full.relative_to(path)), info.st_mode,
                                info.st_size, info.st_mtime_ns))
        return sorted(entries)

    def test_preflight_without_a_session_requires_payload_and_expansion(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "preflight-new" / "offline"
        store = self._preflight_store(base, len(data) - 1)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            store.preflight(doc, sig)
        self.assertFalse(base.exists())

        # The 4 KiB compressed payload, its 8 KiB expansion, the 8 KiB
        # repository extraction and the 8 KiB import must all fit.
        required = self._preflight_required(data)
        self.assertEqual(required, 7 * len(data))
        self.assertEqual(
            self._preflight_store(base, required).preflight(doc, sig)["phase"],
            "preflight_passed")
        self.assertFalse(base.exists())

    def test_preflight_charges_compressed_and_expanded_space_together(self):
        doc, sig, data = self._signed_package()
        self.assertEqual(len(data), 4 * 1024)
        base = self.tmp / "preflight-space" / "offline"
        required = self._preflight_required(data)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._preflight_store(base, required - 1).preflight(doc, sig)
        self.assertEqual(
            self._preflight_store(base, required).preflight(doc, sig)["phase"],
            "preflight_passed")
        self.assertFalse(base.exists())

    @staticmethod
    def _lock_snapshot(lock):
        """Identity and content of an existing lock, or None when it is absent."""
        if not lock.exists():
            return None
        info = lock.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                info.st_mtime_ns, lock.read_bytes())

    def _session_without_lock(self, base):
        """Create a real session whose creation path never takes the lock.

        ``begin`` writes only package and receipt documents, so a session that
        was never uploaded or sealed has no ``.lock`` at all.  It is a valid
        preflight target because its receipt is bound to this device.
        """
        doc, sig, data = self._signed_package()
        store = self._store(base, inspect=self._preflight_identity())
        receipt = store.begin(doc, sig)
        sid = receipt["sessionId"]
        session = base / sid
        receipt = store.read_receipt(session)
        receipt["state"] = "prepared"
        store.update_receipt(session, receipt)
        return sid, doc, sig, data

    def _assert_preflight_is_read_only(self, base, sid, doc, sig, free_bytes,
                                       sysroot_name):
        """Run a preflight and prove it neither wrote nor removed anything."""
        lock = base / sid / ".lock"
        before_tree = self._tree(base)
        before_lock = self._lock_snapshot(lock)
        opens = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, *, dir_fd=None):
            opens.append((Path(path), flags))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        store = self._preflight_store(
            base, free_bytes, sysroot_path=self._preflight_sysroot(sysroot_name))
        with mock.patch.object(offline_session.os, "open", recording_open):
            result = store.preflight(doc, sig, sid)

        self.assertEqual(result["phase"], "preflight_passed")
        self.assertEqual(result["sessionId"], sid)
        # An existing lock is fixture state, not preflight's to remove; an
        # absent lock must stay absent because preflight never creates one.
        self.assertEqual(self._lock_snapshot(lock), before_lock)
        self.assertEqual(self._tree(base), before_tree)
        self.assertTrue(opens)
        for path, flags in opens:
            self.assertEqual(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC |
                         os.O_APPEND), 0,
                "preflight opened %s with write flags %#x" % (path, flags))
        return result

    def test_preflight_of_a_prepared_session_credits_its_expansion_read_only(self):
        # A completed, bound prepare leaves nothing new to allocate.  The
        # upload/seal fixture already created the session lock, so preflight
        # must leave that existing lock untouched rather than delete it.
        base = self.tmp / "preflight-prepared" / "offline"
        sid, doc, sig, data = self._session_for_preflight(base)
        self._write_completed_expansion(base, sid, doc, 2 * len(data))

        # The unlock-only session must exist before the zero-space fixture is
        # installed: begin() measures free space too, and the preflight mocks
        # below live until cleanup, so a session created afterwards would see
        # zero bytes free and fail to start.
        unlocked = self.tmp / "preflight-unlocked" / "offline"
        unlocked_sid, unlocked_doc, unlocked_sig, unlocked_data = \
            self._session_without_lock(unlocked)

        self.assertTrue((base / sid / ".lock").exists())
        self._assert_preflight_is_read_only(
            base, sid, doc, sig, 0, "prepared")

        # The same read-only guarantee on a session that has no lock yet: the
        # full compressed payload plus expansion, extraction and import is
        # still required and no lock may appear as a side effect.
        self.assertFalse((unlocked / unlocked_sid / ".lock").exists())
        self._assert_preflight_is_read_only(
            unlocked, unlocked_sid, unlocked_doc, unlocked_sig,
            self._preflight_required(unlocked_data), "unlocked")

    def test_preflight_does_not_credit_expansion_without_bound_proof(self):
        for label, proof in (
            ("missing", None),
            ("foreign-package", {"descriptor_sha": "0" * 64}),
            ("incomplete-image", {"image_size": 1}),
            ("unfinished-prepare", {"status_state": "deploying"}),
        ):
            with self.subTest(reason=label):
                base = self.tmp / ("preflight-proof-" + label) / "offline"
                sid, doc, sig, data = self._session_for_preflight(base)
                if proof is not None:
                    self._write_completed_expansion(
                        base, sid, doc, 2 * len(data), **proof)
                store = self._preflight_store(
                    base, 3 * (2 * len(data)) - 1,
                    sysroot_path=self._preflight_sysroot("proof"))
                with self.assertRaisesRegex(
                        offline_session.OfflineSessionError, "free space"):
                    store.preflight(doc, sig, sid)

    def test_preflight_credits_only_this_sessions_stored_bytes(self):
        base = self.tmp / "preflight-partial" / "offline"
        sid, doc, sig, data = self._session_for_preflight(
            base, state="uploading", received=1000)
        # Another session of a different package already stores most bytes.
        other_doc, other_sig, other_data = self._signed_package()
        other = self._store(base).begin(other_doc, other_sig)
        self._store(base).upload(other["sessionId"], 0, other_data[:-16])

        # A partial upload only reduces the compressed part it already stored;
        # the full expansion, extraction and import are still required.
        required = self._preflight_required(data, stored=1000)
        sysroot = self._preflight_sysroot("partial")
        self.assertEqual(
            self._preflight_store(base, required, sysroot_path=sysroot)
            .preflight(doc, sig, sid)["sessionId"],
            sid)
        # One byte less than this session's own remaining requirement must still
        # fail: no other session, package or device contributes to the credit.
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._preflight_store(base, required - 1, sysroot_path=sysroot) \
                .preflight(doc, sig, sid)

    def test_preflight_rejects_an_unknown_or_invalid_session(self):
        base = self.tmp / "preflight-unknown" / "offline"
        _sid, doc, sig, data = self._session_for_preflight(base)
        store = self._preflight_store(base, 3 * len(data))
        with self.assertRaises(offline_session.OfflineSessionError):
            store.preflight(doc, sig, "b" * 32)
        for bad in ("BAD", "A" * 32, 123, True):
            with self.assertRaises(ValueError):
                store.preflight(doc, sig, bad)

    def test_preflight_rejects_session_package_and_device_mismatches(self):
        base = self.tmp / "preflight-mismatch" / "offline"
        sid, doc, sig, data = self._session_for_preflight(base)
        session = base / sid
        sysroot = self._preflight_sysroot("mismatch")
        room = 3 * len(data)

        # A different signed document never reuses this session's payload.
        other_doc, other_sig, _other = self._signed_package()
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "different package"):
            self._preflight_store(base, room, sysroot_path=sysroot) \
                .preflight(other_doc, other_sig, sid)

        # The same package on another model/device state is not this session.
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "different device"):
            self._preflight_store(base, room, model="TB323FU",
                                  sysroot_path=sysroot).preflight(doc, sig, sid)

        # A payload that grew past the signed descriptor is never credited.
        payload = self._payload_file(session, json.loads(
            (session / "receipt.json").read_text()))
        payload.write_bytes(data + b"x")
        with self.assertRaises(offline_session.OfflineSessionError):
            self._preflight_store(base, room, sysroot_path=sysroot) \
                .preflight(doc, sig, sid)
        payload.write_bytes(data)

        # A symlinked payload is refused and its target is never counted.
        target = self.tmp / "outside-payload"
        target.write_bytes(data)
        payload.unlink()
        payload.symlink_to(target)
        with self.assertRaises((OSError, offline_session.OfflineSessionError)):
            self._preflight_store(base, room, sysroot_path=sysroot) \
                .preflight(doc, sig, sid)
        payload.unlink()
        payload.write_bytes(data)

    def test_preflight_rejects_executing_or_interrupted_sessions(self):
        base = self.tmp / "preflight-states" / "offline"
        sid, doc, sig, data = self._session_for_preflight(base)
        session = base / sid
        store = self._preflight_store(base, 3 * len(data))

        receipt = store.read_receipt(session)
        for state in ("executing", "execute_queued", "failed",
                      "dispatch_uncertain", "awaiting_reboot"):
            with self.subTest(state=state):
                receipt["state"] = state
                store.update_receipt(session, receipt)
                with self.assertRaisesRegex(
                        offline_session.OfflineSessionError, "cannot be preflighted"):
                    store.preflight(doc, sig, sid)

        receipt["state"] = "prepared"
        receipt["attentionRequired"] = True
        store.update_receipt(session, receipt)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "interrupted"):
            store.preflight(doc, sig, sid)

    def test_preflight_rejects_an_unsupported_ostree_repository(self):
        doc, sig, data = self._signed_package()
        base = self.tmp / "preflight-repo" / "offline"
        snapshot = self._preflight_snapshot()
        snapshot["repository"] = "ghcr.io/example/untrusted"
        store = self._preflight_store(base, 1 << 30, snapshot=snapshot)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "repository"):
            store.preflight(doc, sig)
        self.assertFalse(base.exists())

    def _preflight_work(self, name):
        """A resolved working directory holding both the session and sysroot.

        The sysroot starts with a usable repository, so these tests vary only
        the storage they are about.
        """
        work = (self.tmp / name).resolve()
        work.mkdir(exist_ok=True)
        sysroot = work / "image"
        self._write_repository(sysroot)
        return work, work / "offline", sysroot

    def test_preflight_rejects_a_destination_without_room_despite_an_ample_session(self):
        # The session tree and the import destination are different
        # filesystems: room for the session never covers the pull-local target.
        _work, base, sysroot = self._preflight_work("preflight-destination")
        sid, doc, sig, data = self._session_for_preflight(base)
        import_allowance = 2 * len(data)
        short = [(str(sysroot), 2, import_allowance - 1, 0),
                 (str(base), 1, 1 << 30, 0)]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(base, sysroot, short).preflight(doc, sig, sid)
        room = [(str(sysroot), 2, import_allowance, 0),
                (str(base), 1, 1 << 30, 0)]
        self.assertEqual(
            self._storage_store(base, sysroot, room).preflight(doc, sig, sid)["sessionId"],
            sid)

    def test_preflight_rejects_missing_repository_extraction_space(self):
        # The session filesystem has room for the expanded image but not for
        # the repository tree btrfs restore extracts beside it.
        _work, base, sysroot = self._preflight_work("preflight-extraction")
        sid, doc, sig, data = self._session_for_preflight(base)
        expanded = 2 * len(data)
        short = [(str(sysroot), 2, 1 << 30, 0),
                 (str(base), 1, expanded + 1, 0)]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(base, sysroot, short).preflight(doc, sig, sid)
        room = [(str(sysroot), 2, 1 << 30, 0),
                (str(base), 1, 2 * expanded, 0)]
        self.assertEqual(
            self._storage_store(base, sysroot, room).preflight(doc, sig, sid)["sessionId"],
            sid)

    def test_preflight_sums_a_shared_filesystem_and_keeps_the_repository_reserve(self):
        # One filesystem holds the expansion, the extraction and the import, so
        # the three allowances are summed and the repository's default reserve
        # (3 percent, because the fixture config declares no reserve keys) is
        # charged once more on top.
        work, base, sysroot = self._preflight_work("preflight-shared")
        sid, doc, sig, data = self._session_for_preflight(base)
        total = 1_000_000
        reserve = total * 3 // 100
        required = 3 * (2 * len(data)) + reserve
        short = [(str(work), 1, required - 1, total)]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(base, sysroot, short).preflight(doc, sig, sid)
        room = [(str(work), 1, required, total)]
        self.assertEqual(
            self._storage_store(base, sysroot, room).preflight(doc, sig, sid)["sessionId"],
            sid)

    def test_preflight_honours_the_repository_reserve_config(self):
        work, base, sysroot = self._preflight_work("preflight-reserve")
        sid, doc, sig, data = self._session_for_preflight(base)
        repository = sysroot / "ostree" / "repo"
        repository.mkdir(parents=True, exist_ok=True)
        config = repository / "config"
        config.write_text("[core]\nrepo_version=1\nmin-free-space-percent=10\n"
                          "min-free-space-size=1MB\n")
        total = 1_000_000
        allowances = 3 * (2 * len(data))
        # min-free-space-size wins over min-free-space-percent the way older
        # libostree resolves them: 1 MiB, not 10 percent of this filesystem.
        # Newer releases take the lesser of the two, so this deliberate
        # older-compatible precedence only ever overstates the reserve.
        percent_only = allowances + total * 10 // 100
        size_reserve = 1024 * 1024
        short = [(str(work), 1, percent_only, total)]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(base, sysroot, short).preflight(doc, sig, sid)
        room = [(str(work), 1, allowances + size_reserve, total)]
        self.assertEqual(
            self._storage_store(base, sysroot, room).preflight(doc, sig, sid)["sessionId"],
            sid)

        # A reserve this module cannot honour is refused, never ignored.
        config.write_text("[core]\nrepo_version=1\nmin-free-space-size=3XB\n")
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "reserve size"):
            self._storage_store(base, sysroot, room).preflight(doc, sig, sid)

        # A zero size disables only the absolute form: the percent reserve the
        # same config still names keeps applying, rather than the reserve
        # collapsing to nothing.
        config.write_text("[core]\nrepo_version=1\nmin-free-space-size=0\n"
                          "min-free-space-percent=10\n")
        percent_reserve = total * 10 // 100
        percent_short = [(str(work), 1, allowances + percent_reserve - 1, total)]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(base, sysroot, percent_short).preflight(doc, sig, sid)
        percent_room = [(str(work), 1, allowances + percent_reserve, total)]
        self.assertEqual(
            self._storage_store(base, sysroot, percent_room).preflight(doc, sig, sid)["sessionId"],
            sid)

        # The percent form is bounded to 0..99, so 100 is refused, not clamped.
        config.write_text("[core]\nrepo_version=1\nmin-free-space-percent=100\n")
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "reserve percent"):
            self._storage_store(base, sysroot, percent_room).preflight(
                doc, sig, sid)

    def test_preflight_does_not_recharge_a_prepared_sessions_completed_work(self):
        work, base, sysroot = self._preflight_work("preflight-prepared-storage")
        sid, doc, sig, data = self._session_for_preflight(base)
        self._write_completed_expansion(base, sid, doc, 2 * len(data))
        unproven = work / "offline-unproven"
        other_sid, other_doc, other_sig, _other = self._session_for_preflight(unproven)

        # The bound transaction proves the upload, the expansion, the
        # extraction and the import are done, so no free space is required
        # again and the repository reserve is not recharged either.
        empty = [(str(work), 1, 0, 1_000_000)]
        self.assertEqual(
            self._storage_store(base, sysroot, empty).preflight(doc, sig, sid)["sessionId"],
            sid)
        # The same empty filesystem still refuses a session without that proof.
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            self._storage_store(unproven, sysroot, empty).preflight(
                other_doc, other_sig, other_sid)

    def test_rpc_forwards_the_named_session_and_credits_its_space(self):
        # The endpoint must forward a named session to the store's own
        # read-only preflight.  With a completed prepare and no free space, the
        # bound request passes only because the real store credited it, while
        # the same store refuses the unbound request for capacity.
        work, base, sysroot = self._preflight_work("preflight-rpc")
        sid, doc, sig, data = self._session_for_preflight(base)
        self._write_completed_expansion(base, sid, doc, 2 * len(data))

        store = self._preflight_store(base, 0, sysroot_path=sysroot)
        before_tree = self._tree(work)
        before_lock = self._lock_snapshot(base / sid / ".lock")
        signed = {"document": base64.b64encode(doc).decode("ascii"),
                  "signature": base64.b64encode(sig).decode("ascii")}
        requests = [{"protocolVersion": 1, "id": index, "method": "preflight",
                     "params": dict(signed, **extra)}
                    for index, extra in ((1, {"sessionId": sid}), (2, {}))]
        reader = BytesIO(b"".join(
            json.dumps(request, separators=(",", ":")).encode() + b"\n"
            for request in requests))
        writer = BytesIO()
        # Only the store is real; the endpoint adds nothing but forwarding.
        manager = SimpleNamespace(store=store)
        self.assertEqual(offline_rpc.serve(reader, writer, manager), 0)

        bound, unbound = [
            json.loads(line) for line in writer.getvalue().splitlines()]
        self.assertTrue(bound["ok"], bound)
        self.assertEqual(bound["id"], 1)
        self.assertEqual(bound["result"]["phase"], "preflight_passed")
        self.assertEqual(bound["result"]["sessionId"], sid)
        self.assertFalse(unbound["ok"], unbound)
        self.assertEqual(unbound["id"], 2)
        self.assertEqual(unbound["error"]["code"], "operation_failed")
        self.assertIn("free space", unbound["error"]["message"])

        # Purely read-only: no file, no lock and no receipt change appears, and
        # no prepare or execute job was queued for either answer.
        self.assertEqual(self._tree(work), before_tree)
        self.assertEqual(self._lock_snapshot(base / sid / ".lock"), before_lock)
        receipt = store.read_receipt(base / sid)
        self.assertEqual(receipt["state"], "prepared")
        self.assertNotIn("jobOperation", receipt)

    def test_preflight_reads_the_repository_config_without_touching_it(self):
        work, base, sysroot = self._preflight_work("preflight-read-only")
        sid, doc, sig, data = self._session_without_lock(base)
        repository = sysroot / "ostree" / "repo"
        repository.mkdir(parents=True, exist_ok=True)
        config = repository / "config"
        config.write_text("[core]\nrepo_version=1\nmin-free-space-percent=1\n")
        before = (self._tree(work), config.read_bytes())
        opens = []
        real_open = os.open

        def recording_open(path, flags, mode=0o777, *, dir_fd=None):
            opens.append((Path(path), flags))
            return real_open(path, flags, mode, dir_fd=dir_fd)

        store = self._preflight_store(
            base, self._preflight_required(data), sysroot_path=sysroot)
        with mock.patch.object(offline_session.os, "open", recording_open):
            self.assertEqual(store.preflight(doc, sig, sid)["sessionId"], sid)
        # The canonical config is only read: no file, no lock and no directory
        # appears, and no open carries a write flag.
        self.assertEqual((self._tree(work), config.read_bytes()), before)
        self.assertFalse((base / sid / ".lock").exists())
        self.assertTrue(opens)
        for path, flags in opens:
            self.assertEqual(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC |
                         os.O_APPEND), 0,
                "preflight opened %s with write flags %#x" % (path, flags))

    def test_preflight_rejects_a_missing_repository_destination(self):
        work = (self.tmp / "preflight-no-repo").resolve()
        work.mkdir()
        sysroot = work / "image"
        sysroot.mkdir()
        base = work / "offline"
        sid, doc, sig, _data = self._session_for_preflight(base)
        store = self._preflight_store(base, 1 << 30, sysroot_path=sysroot)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "repository destination"):
            store.preflight(doc, sig, sid)
        self.assertFalse((sysroot / "ostree").exists())

    @unittest.skipUnless(os.name == "posix", "symlinks are POSIX only")
    def test_preflight_rejects_a_symlinked_repository_destination(self):
        work = (self.tmp / "preflight-repo-link").resolve()
        work.mkdir()
        sysroot = work / "image"
        (sysroot / "ostree").mkdir(parents=True)
        outside = work / "outside"
        outside.mkdir()
        os.symlink(outside, sysroot / "ostree" / "repo")
        base = work / "offline"
        sid, doc, sig, _data = self._session_for_preflight(base)
        store = self._preflight_store(base, 1 << 30, sysroot_path=sysroot)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "symlinked path segment"):
            store.preflight(doc, sig, sid)
        self.assertEqual(list(outside.iterdir()), [])

    @staticmethod
    def _objects_as_a_file(repository):
        (repository / "objects").rmdir()
        (repository / "objects").write_text("not a directory")

    def test_preflight_requires_a_usable_repository_without_writing(self):
        # An existing repository directory is not enough: ``ostree pull-local``
        # needs the ``objects`` directory and a bounded config naming the
        # supported repo_version.  Every other shape is refused before any
        # storage promise is made, and the valid one passes without a single
        # file, directory or config change in the sysroot.
        cases = (
            ("valid", None),
            ("absent-config", lambda repo: (repo / "config").unlink()),
            ("absent-objects", lambda repo: (repo / "objects").rmdir()),
            ("file-objects", self._objects_as_a_file),
            ("unsupported-version", lambda repo: (repo / "config").write_text(
                "[core]\nrepo_version=2\n")),
            ("missing-version", lambda repo: (repo / "config").write_text(
                "[core]\nmin-free-space-percent=1\n")),
            ("malformed-version", lambda repo: (repo / "config").write_text(
                "[core]\nrepo_version=one\n")),
            ("malformed-config", lambda repo: (repo / "config").write_text(
                "[core]\nrepo_version\n")),
        )
        for label, prepare in cases:
            with self.subTest(repository=label):
                work, base, sysroot = self._preflight_work(
                    "preflight-repository-" + label)
                sid, doc, sig, data = self._session_without_lock(base)
                repository = sysroot / "ostree" / "repo"
                if prepare is not None:
                    prepare(repository)
                before = self._tree(work)
                store = self._preflight_store(
                    base, self._preflight_required(data),
                    sysroot_path=sysroot)
                if label == "valid":
                    self.assertEqual(
                        store.preflight(doc, sig, sid)["sessionId"], sid)
                else:
                    with self.assertRaises(
                            offline_session.OfflineSessionError):
                        store.preflight(doc, sig, sid)
                self.assertEqual(self._tree(work), before)

    def _space_dirs(self, name, count):
        root = (self.tmp / name).resolve()
        paths = []
        for index in range(count):
            path = root / ("d%d" % index)
            path.mkdir(parents=True)
            paths.append(path)
        return paths

    def _filesystem_doubles(self, fsids, free, total, devices=None):
        """Model filesystem identity and capacity for direct space checks.

        ``fsids`` maps a path to its 16-byte Btrfs FSID, or to ``None`` for a
        filesystem that answers ENOTTY and must fall back to ``st_dev``
        (overridden by ``devices``).  ``free`` maps each path to its own
        available bytes and ``total`` sizes the percent reserve.  The ioctl
        double mutates the buffer in place, the way the kernel does.
        """
        opened = []
        closed = []
        current = {}
        real_open = os.open
        real_close = os.close
        real_stat = _REAL_STAT
        devices = {} if devices is None else devices

        def fake_open(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            current[fd] = str(Path(path))
            opened.append((Path(path), flags, fd))
            return fd

        def fake_close(fd):
            closed.append(fd)
            real_close(fd)

        def fake_ioctl(fd, request, arg):
            self.assertEqual(request, 0x8400941F)
            self.assertEqual(len(arg), 1024)
            fsid = fsids[current[fd]]
            if fsid is None:
                raise OSError(errno.ENOTTY, "not a btrfs filesystem")
            arg[16:32] = fsid
            return bytes(arg)

        def fake_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            text = str(Path(path))
            if text not in devices:
                return result
            values = list(result)
            values[2] = devices[text]
            return os.stat_result(values)

        def fake_statvfs(path):
            text = str(Path(path))
            if text not in free:
                raise AssertionError("unexpected statvfs: %s" % path)
            return SimpleNamespace(f_bavail=free[text], f_blocks=total,
                                   f_frsize=1)

        for patcher in (
            mock.patch.object(offline_session.os, "open", side_effect=fake_open),
            mock.patch.object(offline_session.os, "close", side_effect=fake_close),
            mock.patch.object(offline_session.fcntl, "ioctl", side_effect=fake_ioctl),
            mock.patch.object(offline_session.os, "stat", side_effect=fake_stat),
            mock.patch.object(offline_session.os, "statvfs", side_effect=fake_statvfs),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return opened, closed

    def test_free_space_joins_btrfs_subvolumes_by_filesystem_fsid(self):
        # Two subvolumes report different st_dev but one pool FSID: their
        # allowances are summed and the repository reserve is charged once.
        first, second = self._space_dirs("btrfs-shared", 2)
        fsid = bytes(range(16))
        free = {str(first): 249, str(second): 1 << 30}
        opened, closed = self._filesystem_doubles(
            {str(first): fsid, str(second): fsid}, free, 1000,
            {str(first): 101, str(second): 202})
        destinations = [(first, 100, "payload"), (second, 100, "import")]
        reserve = (second, ("size", 50))
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            offline_session.OfflineSessionStore._require_free_space(
                destinations, reserve=reserve)
        # The smaller subvolume must still fit the shared pool requirement.
        free[str(first)] = 250
        offline_session.OfflineSessionStore._require_free_space(
            destinations, reserve=reserve)
        self.assertEqual(len(opened), len(closed))

    def test_free_space_keeps_distinct_btrfs_filesystems_independent(self):
        first, second = self._space_dirs("btrfs-split", 2)
        free = {str(first): 150, str(second): 99}
        self._filesystem_doubles(
            {str(first): b"A" * 16, str(second): b"B" * 16}, free, 1000)
        destinations = [(first, 100, "payload"), (second, 100, "import")]
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            offline_session.OfflineSessionStore._require_free_space(destinations)
        free[str(second)] = 100
        offline_session.OfflineSessionStore._require_free_space(destinations)

    def test_free_space_falls_back_to_st_dev_without_btrfs_ioctl(self):
        first, second = self._space_dirs("non-btrfs", 2)
        free = {str(first): 100, str(second): 100}
        devices = {str(first): 7, str(second): 7}
        opened, closed = self._filesystem_doubles(
            {str(first): None, str(second): None}, free, 1000, devices)
        destinations = [(first, 100, "payload"), (second, 100, "import")]
        # One block device: both allowances are summed against one capacity.
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "free space"):
            offline_session.OfflineSessionStore._require_free_space(destinations)
        # A different st_dev keeps its own capacity and its own requirement.
        devices[str(second)] = 8
        offline_session.OfflineSessionStore._require_free_space(destinations)
        self.assertEqual(len(opened), len(closed))

    def test_free_space_refuses_an_ioctl_error_and_closes_the_descriptor(self):
        (path,) = self._space_dirs("btrfs-error", 1)
        opened = []
        closed = []
        real_open = os.open
        real_close = os.close

        def fake_open(name, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(name, flags, mode, dir_fd=dir_fd)
            opened.append((Path(name), flags, fd))
            return fd

        def fake_close(fd):
            closed.append(fd)
            real_close(fd)

        with mock.patch.object(offline_session.os, "open", side_effect=fake_open), \
                mock.patch.object(offline_session.os, "close", side_effect=fake_close), \
                mock.patch.object(
                    offline_session.fcntl, "ioctl",
                    side_effect=OSError(errno.EACCES, "permission denied")):
            with self.assertRaisesRegex(
                    offline_session.OfflineSessionError, "filesystem"):
                offline_session.OfflineSessionStore._require_free_space(
                    [(path, 1, "payload")])
        _name, flags, fd = opened[0]
        self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT), 0)
        self.assertTrue(flags & os.O_DIRECTORY)
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_CLOEXEC)
        self.assertEqual(closed, [fd])

    def test_free_space_rejects_an_empty_btrfs_fsid(self):
        (path,) = self._space_dirs("btrfs-empty-fsid", 1)
        self._filesystem_doubles(
            {str(path): b"\0" * 16}, {str(path): 1 << 30}, 1000)
        with self.assertRaisesRegex(
                offline_session.OfflineSessionError, "empty"):
            offline_session.OfflineSessionStore._require_free_space(
                [(path, 100, "payload")])

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
