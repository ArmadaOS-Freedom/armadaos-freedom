#!/usr/bin/env python3
import base64
import hashlib
import io
import json
import os
import pwd
import stat
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "system_files/usr/lib/armada"))
import updates.installer_authorization as auth


def synthetic_key(seed=0):
    key_bytes = bytes((seed + i) % 256 for i in range(32))
    wire = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + key_bytes
    assert len(wire) == 51
    return wire, "ssh-ed25519 " + base64.b64encode(wire).decode("ascii")


INSTALLER_WIRE, INSTALLER_KEY = synthetic_key()
HOST_WIRE, HOST_KEY = synthetic_key(64)
EXPECTED_ENTRY = (
    'restrict,command="/usr/bin/sudo -n -- /usr/bin/python3 -I '
    '/usr/libexec/armada/armada-offline-update" ssh-ed25519 '
    + INSTALLER_KEY.split()[1]
    + " armada-offline-installer"
)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_ARTIFACT_ROOT"))
        self.root = self._tmp.name
        self.home = os.path.join(self.root, "home")
        os.mkdir(self.home)
        self.ssh_dir = os.path.join(self.home, ".ssh")
        self.host_path = os.path.join(self.root, "host.pub")
        with open(self.host_path, "wb") as stream:
            stream.write((HOST_KEY + " normal trailing comment\n").encode("ascii"))
        self.uid = os.getuid()

    def tearDown(self):
        self._tmp.cleanup()

    @property
    def auth_path(self):
        return os.path.join(self.ssh_dir, "authorized_keys")

    @property
    def lock_path(self):
        return os.path.join(self.ssh_dir, ".armada_auth.lock")

    def authorize(self):
        return auth.authorize_key(INSTALLER_KEY, self.home, self.uid, self.host_path)

    def make_ssh(self):
        os.mkdir(self.ssh_dir, 0o700)

    def test_first_enrollment_exact_modes_and_repeat_byte_idempotent(self):
        host_fp, installer_fp = self.authorize()
        self.assertEqual(stat.S_IMODE(os.lstat(self.ssh_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.auth_path).st_mode), 0o600)
        with open(self.auth_path, "rb") as stream:
            first = stream.read()
        self.assertEqual(first, (EXPECTED_ENTRY + "\n").encode("ascii"))
        expected_host = "SHA256:" + base64.b64encode(hashlib.sha256(HOST_WIRE).digest()).decode("ascii").rstrip("=")
        expected_installer = "SHA256:" + base64.b64encode(hashlib.sha256(INSTALLER_WIRE).digest()).decode("ascii").rstrip("=")
        self.assertEqual((host_fp, installer_fp), (expected_host, expected_installer))
        self.assertEqual(self.authorize(), (host_fp, installer_fp))
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(), first)
        os.chmod(self.auth_path, 0o644)
        self.authorize()
        self.assertEqual(stat.S_IMODE(os.lstat(self.auth_path).st_mode), 0o600)
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(), first)

    def test_crlf_invalid_utf8_and_unmatched_quote_comment_are_byte_preserved(self):
        self.make_ssh()
        original = b'# unrelated unmatched " quote\r\n\xffbinary-comment\r\n'
        with open(self.auth_path, "wb") as stream:
            stream.write(original)
        self.authorize()
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(), original + (EXPECTED_ENTRY + "\n").encode("ascii"))

    def test_same_key_bare_or_with_alternate_quoted_command_rejected_unchanged(self):
        variants = [
            INSTALLER_KEY + "\n",
            'command="/bin/echo harmless" ' + INSTALLER_KEY + " other\n",
        ]
        for index, contents in enumerate(variants):
            with self.subTest(index=index):
                self.make_ssh()
                with open(self.auth_path, "wb") as stream:
                    stream.write(contents.encode("ascii"))
                with self.assertRaises(auth.AuthorizationError):
                    self.authorize()
                with open(self.auth_path, "rb") as stream:
                    self.assertEqual(stream.read(), contents.encode("ascii"))
                os.unlink(self.auth_path)
                if os.path.lexists(self.lock_path):
                    os.unlink(self.lock_path)
                os.rmdir(self.ssh_dir)

    def test_unrelated_quoted_forced_command_is_preserved(self):
        self.make_ssh()
        other_wire, other_key = synthetic_key(128)
        del other_wire
        original = ('command="/bin/echo \\"quoted value\\"" ' + other_key + ' other trailing " unmatched quote\n').encode("ascii")
        with open(self.auth_path, "wb") as stream:
            stream.write(original)
        self.authorize()
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(), original + (EXPECTED_ENTRY + "\n").encode("ascii"))

    def test_dangling_authorized_keys_symlink_rejected_and_left_dangling(self):
        self.make_ssh()
        target = os.path.join(self.root, "absent-target")
        os.symlink(target, self.auth_path)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertTrue(os.path.islink(self.auth_path))
        self.assertEqual(os.readlink(self.auth_path), target)
        self.assertFalse(os.path.exists(target))

    def test_lock_symlink_rejected_and_target_untouched(self):
        self.make_ssh()
        target = os.path.join(self.root, "harmless")
        with open(target, "wb") as stream:
            stream.write(b"do not modify")
        os.symlink(target, self.lock_path)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertTrue(os.path.islink(self.lock_path))
        with open(target, "rb") as stream:
            self.assertEqual(stream.read(), b"do not modify")
        self.assertFalse(os.path.exists(self.auth_path))

    def test_lock_hardlink_rejected_without_modifying_either_name(self):
        self.make_ssh()
        target = os.path.join(self.root, "lock-original")
        with open(target, "wb") as stream:
            stream.write(b"original")
        os.link(target, self.lock_path)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertEqual(os.lstat(self.lock_path).st_nlink, 2)
        for path in (target, self.lock_path):
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), b"original")
        self.assertFalse(os.path.exists(self.auth_path))

    def test_lock_fifo_rejected_without_blocking(self):
        self.make_ssh()
        os.mkfifo(self.lock_path)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.lock_path).st_mode))

    def test_authorized_keys_fifo_directory_and_hardlink_rejected_unchanged(self):
        for kind in ("fifo", "directory", "hardlink"):
            with self.subTest(kind=kind):
                self.make_ssh()
                if kind == "fifo":
                    os.mkfifo(self.auth_path)
                elif kind == "directory":
                    os.mkdir(self.auth_path)
                else:
                    other = os.path.join(self.root, "other-link")
                    with open(other, "wb") as stream:
                        stream.write(b"original\n")
                    os.link(other, self.auth_path)
                with self.assertRaises(auth.AuthorizationError):
                    self.authorize()
                mode = os.lstat(self.auth_path).st_mode
                if kind == "fifo":
                    self.assertTrue(stat.S_ISFIFO(mode))
                elif kind == "directory":
                    self.assertTrue(stat.S_ISDIR(mode))
                else:
                    self.assertEqual(os.lstat(self.auth_path).st_nlink, 2)
                    with open(self.auth_path, "rb") as stream:
                        self.assertEqual(stream.read(), b"original\n")
                if kind == "directory":
                    os.rmdir(self.auth_path)
                else:
                    os.unlink(self.auth_path)
                if os.path.lexists(self.lock_path):
                    os.unlink(self.lock_path)
                os.rmdir(self.ssh_dir)

    def test_home_and_ssh_symlinks_rejected(self):
        for which in ("home", "ssh"):
            with self.subTest(which=which):
                target = os.path.join(self.root, which + "-target")
                os.mkdir(target)
                if which == "home":
                    os.rmdir(self.home)
                    os.symlink(target, self.home)
                else:
                    os.symlink(target, self.ssh_dir)
                with self.assertRaises(auth.AuthorizationError):
                    self.authorize()
                self.assertEqual(os.listdir(target), [])
                if which == "home":
                    os.unlink(self.home)
                    os.mkdir(self.home)

    def test_unsafe_ssh_mode_and_foreign_owner_rejected(self):
        self.make_ssh()
        os.chmod(self.ssh_dir, 0o777)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertEqual(stat.S_IMODE(os.lstat(self.ssh_dir).st_mode), 0o777)
        self.assertFalse(os.path.exists(self.auth_path))
        if os.getuid() == 0:
            os.chmod(self.ssh_dir, 0o700)
            os.chown(self.ssh_dir, self.uid + 1, os.getgid())
            with self.assertRaises(auth.AuthorizationError):
                self.authorize()
            self.assertEqual(os.lstat(self.ssh_dir).st_uid, self.uid + 1)

    def test_oversized_authorized_keys_rejected_unchanged(self):
        self.make_ssh()
        with open(self.auth_path, "wb") as stream:
            stream.write(b"start")
            stream.truncate(auth.MAX_AUTHORIZED_KEYS_SIZE + 1)
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertEqual(os.path.getsize(self.auth_path), auth.MAX_AUTHORIZED_KEYS_SIZE + 1)
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(5), b"start")

    def test_noncanonical_and_malformed_installer_keys_rejected(self):
        good_b64 = INSTALLER_KEY.split()[1]
        bad_name_length = struct.pack(">I", 10) + INSTALLER_WIRE[4:]
        bad_key_length = INSTALLER_WIRE[:15] + struct.pack(">I", 31) + INSTALLER_WIRE[19:]
        bad_type = INSTALLER_WIRE[:4] + b"ssh-ed2551X" + INSTALLER_WIRE[15:]
        malformed = [
            "ssh-ed25519 " + base64.b64encode(INSTALLER_WIRE[:-1]).decode("ascii"),
            "ssh-ed25519 " + base64.b64encode(INSTALLER_WIRE + b"x").decode("ascii"),
            "ssh-ed25519 " + base64.b64encode(bad_name_length).decode("ascii"),
            "ssh-ed25519 " + base64.b64encode(bad_key_length).decode("ascii"),
            "ssh-ed25519 " + base64.b64encode(bad_type).decode("ascii"),
            "ssh-rsa " + good_b64,
            "ssh-ed25519 " + good_b64 + "=",
            "restrict " + INSTALLER_KEY,
            INSTALLER_KEY + " comment",
            INSTALLER_KEY + "\n",
            INSTALLER_KEY + "\r",
            INSTALLER_KEY + "\0",
        ]
        for candidate in malformed:
            with self.subTest(candidate=repr(candidate)):
                with self.assertRaises(auth.AuthorizationError):
                    auth.parse_public_key(candidate, allow_trailing_comment=False)

    def test_invalid_host_key_creates_no_ssh_directory(self):
        with open(self.host_path, "wb") as stream:
            stream.write(b"ssh-ed25519 definitely-not-base64 host\n")
        with self.assertRaises(auth.AuthorizationError):
            self.authorize()
        self.assertFalse(os.path.exists(self.ssh_dir))

    def test_ssh_directory_home_flush_failure_propagates_and_retries_flush_again(self):
        home_ino = os.lstat(self.home).st_ino
        real_fsync = os.fsync
        armed = {"fail": True}
        flushed = []

        def fsync(fd):
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode) and st.st_ino == home_ino:
                flushed.append(st.st_ino)
                if armed["fail"]:
                    raise OSError(5, "simulated home directory flush failure")
            return real_fsync(fd)

        with mock.patch.object(auth.os, "fsync", side_effect=fsync):
            # Creating .ssh is not durable until the verified home directory
            # holding the new entry has been flushed, so a failed flush is
            # reported and nothing is written inside the directory.
            with self.assertRaisesRegex(auth.AuthorizationError, "flush"):
                self.authorize()
            self.assertTrue(os.path.isdir(self.ssh_dir))
            self.assertFalse(os.path.lexists(self.auth_path))
            self.assertEqual(flushed, [home_ino])

            # The retry must not treat the already-visible .ssh entry as
            # durable: it flushes the same verified home directory again.
            with self.assertRaisesRegex(auth.AuthorizationError, "flush"):
                self.authorize()
            self.assertFalse(os.path.lexists(self.auth_path))
            self.assertEqual(flushed, [home_ino, home_ino])

            armed["fail"] = False
            self.authorize()

        self.assertEqual(flushed, [home_ino, home_ino, home_ino])
        with open(self.auth_path, "rb") as stream:
            self.assertEqual(stream.read(), (EXPECTED_ENTRY + "\n").encode("ascii"))


class MainTests(unittest.TestCase):
    def run_main(self, uid=1000, euid=1000, pw_entry=None, group=None):
        if pw_entry is None:
            pw_entry = SimpleNamespace(pw_name="alice", pw_dir="/passwd/home/alice", pw_gid=10)
        if group is None:
            group = SimpleNamespace(gr_gid=10, gr_mem=[])
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(auth.sys, "argv", ["armada-authorize-installer", INSTALLER_KEY]), \
                mock.patch.object(auth.os, "getuid", return_value=uid), \
                mock.patch.object(auth.os, "geteuid", return_value=euid), \
                mock.patch.object(auth.pwd, "getpwuid", return_value=pw_entry), \
                mock.patch.object(auth.grp, "getgrnam", return_value=group), \
                mock.patch.object(auth.sys, "stdout", stdout), \
                mock.patch.object(auth.sys, "stderr", stderr), \
                mock.patch.object(auth, "authorize_key", return_value=("HOSTFP", "INSTALLERFP")) as authorize:
            result = auth.main()
        return result, stdout.getvalue(), stderr.getvalue(), authorize

    def test_main_rejects_root_and_different_effective_uid(self):
        for uid, euid in ((0, 0), (1000, 1001)):
            with self.subTest(uid=uid, euid=euid):
                result, stdout, stderr, authorize = self.run_main(uid, euid)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Error:", stderr)
                authorize.assert_not_called()

    def test_main_rejects_nonwheel_user(self):
        pw_entry = SimpleNamespace(pw_name="alice", pw_dir="/home/alice", pw_gid=20)
        group = SimpleNamespace(gr_gid=10, gr_mem=["bob"])
        result, stdout, stderr, authorize = self.run_main(pw_entry=pw_entry, group=group)
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("wheel", stderr)
        authorize.assert_not_called()

    def test_main_uses_passwd_home_not_environment_and_emits_schema(self):
        with mock.patch.dict(os.environ, {"HOME": "/must/not/be/used"}):
            result, stdout, stderr, authorize = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {
            "schema": 1,
            "userName": "alice",
            "hostKeyFingerprint": "HOSTFP",
            "installerKeyFingerprint": "INSTALLERFP",
        })
        authorize.assert_called_once_with(INSTALLER_KEY, "/passwd/home/alice", 1000)


if __name__ == "__main__":
    unittest.main()
