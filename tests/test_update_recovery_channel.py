import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from system_files.usr.lib.armada.updates import channel_selection as selection


FREEDOM = "ghcr.io/armadaos-freedom/armadaos-freedom"
UPSTREAM = "ghcr.io/armada-os/armada"
DIGEST = "a" * 64


def origin(repository=FREEDOM, channel="stable", transport=""):
    return transport + repository + ":" + channel


class InstalledChannelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="armada-channel-", dir=os.environ.get("TMPDIR", "/tmp"))
        self.root = Path(self.temporary.name)
        self.state = self.root / "var/lib/armada/update-channel"
        self.addCleanup(self.temporary.cleanup)
        self.lock_patch = mock.patch.object(
            selection.trial_confirmation, "UPDATE_LOCK_PATH",
            self.root / "trial.lock")
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        if os.geteuid() != 0:
            original_fstat = os.fstat
            owner = os.geteuid()
            def root_owned_fixture(fd):
                values = list(original_fstat(fd))
                if values[4] == owner:
                    values[4] = 0
                return os.stat_result(values)
            ownership = mock.patch.object(selection.os, "fstat", side_effect=root_owned_fixture)
            ownership.start()
            self.addCleanup(ownership.stop)

    def read(self, ref, path=None):
        return selection.read_installed_channel(
            state_path=self.state if path is None else path, origin_ref=ref)

    def write_state(self, content):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_bytes(content)
        self.state.chmod(0o644)

    DISTRO_UID = 4321

    @contextmanager
    def owner(self, uid):
        """Report a fixed file owner without depending on a real chown."""
        original = os.fstat

        def owned(fd):
            values = list(original(fd))
            values[4] = uid
            return os.stat_result(values)

        with mock.patch.object(selection.os, "fstat", side_effect=owned):
            yield

    @contextmanager
    def account(self, uid):
        """Emulate the distro account lookup (``None`` means no such account)."""
        if uid is None:
            patcher = mock.patch.object(
                selection.pwd, "getpwnam", side_effect=KeyError("armada"))
        else:
            patcher = mock.patch.object(
                selection.pwd, "getpwnam", return_value=mock.Mock(pw_uid=uid))
        with patcher as lookup:
            yield lookup

    @unittest.skipUnless(os.geteuid() == 0, "channel writer requires root")
    def test_all_channels_written_by_set_channel_override_origin(self):
        for channel in selection.CHANNELS:
            with self.subTest(channel=channel):
                selection.set_channel(channel, self.state)
                self.assertEqual(channel, self.read(origin(channel="stable")))

    def test_missing_file_and_parents_fall_back_to_origin_tag(self):
        missing_parent = self.root / "missing/parents/channel"
        self.assertEqual("beta", self.read(origin(channel="beta"), missing_parent))
        self.assertFalse((self.root / "missing").exists())
        self.state.parent.mkdir(parents=True)
        self.assertEqual("testing", self.read(origin(channel="testing")))
        self.assertFalse(self.state.exists())

    def test_digest_only_requires_valid_state(self):
        digest_ref = FREEDOM + "@sha256:" + DIGEST
        with self.assertRaisesRegex(ValueError, "installed channel state"):
            self.read(digest_ref)
        self.write_state(b"testing\n")
        self.assertEqual("testing", self.read(digest_ref))

    def test_exact_transports_and_repositories(self):
        transports = (
            "",
            "ostree-image-signed:docker://",
            "ostree-unverified-image:docker://",
            "ostree-unverified-registry:docker://",
            "ostree-unverified-registry:",
            "docker://",
        )
        repositories = (
            FREEDOM,
            "ghcr.io/mydanyi/armadaos-freedom",
            UPSTREAM,
            "ghcr.io/virtudude/armada",
        )
        for repository in repositories:
            for transport in transports:
                with self.subTest(repository=repository, transport=transport):
                    self.assertEqual(
                        "beta", self.read(origin(repository, "beta", transport)))

    def test_invalid_origins_rejected_even_with_valid_state(self):
        self.write_state(b"testing\n")
        tagged = origin(channel="beta")
        invalid = (
            FREEDOM,
            FREEDOM + ":",
            FREEDOM + ":feature",
            FREEDOM + ".evil:stable",
            FREEDOM + "/lookalike:stable",
            "https://" + FREEDOM + ":stable",
            "ostree-image-signed:https://" + FREEDOM + ":stable",
            tagged + " ",
            tagged + "\n",
            tagged + "@sha256:" + DIGEST + "@sha256:" + DIGEST,
            tagged + "@sha256:" + "a" * 63,
            tagged + "@sha256:" + "g" * 64,
            tagged + "@SHA256:" + DIGEST,
            None,
            42,
        )
        for ref in invalid:
            with self.subTest(ref=ref):
                with self.assertRaises(ValueError):
                    self.read(ref)
        self.assertEqual("testing", self.read(tagged))

    def test_empty_corrupt_oversized_and_non_ascii_state_rejected(self):
        contents = (
            b"", b"\n", b"stable\n\n", b"stable \n", b"beta\r\n",
            b"stable\x00", b"\xff\n", b"x" * 33,
        )
        for content in contents:
            with self.subTest(content=content):
                self.write_state(content)
                with self.assertRaises(ValueError):
                    self.read(origin(channel="stable"))

    def test_leaf_ancestor_and_dangling_symlinks_rejected(self):
        target = self.root / "target"
        target.write_text("beta\n")
        self.state.parent.mkdir(parents=True)
        self.state.symlink_to(target)
        with self.assertRaises(ValueError):
            self.read(origin())

        self.state.unlink()
        real_parent = self.root / "real"
        real_parent.mkdir()
        linked_parent = self.root / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.read(origin(), linked_parent / "channel")

        dangling_leaf = self.root / "dangling-leaf"
        dangling_leaf.symlink_to(self.root / "absent-target")
        with self.assertRaises(ValueError):
            self.read(origin(), dangling_leaf)
        dangling_parent = self.root / "dangling-parent"
        dangling_parent.symlink_to(self.root / "absent-directory")
        with self.assertRaises(ValueError):
            self.read(origin(), dangling_parent / "channel")

    def test_fifo_does_not_hang(self):
        self.state.parent.mkdir(parents=True)
        os.mkfifo(self.state)
        with self.assertRaises(ValueError):
            self.read(origin())

    def test_unsafe_mode_and_root_ownership(self):
        self.write_state(b"beta\n")
        self.state.chmod(0o666)
        with self.assertRaises(ValueError):
            self.read(origin())
        self.state.chmod(0o644)
        if os.geteuid() == 0:
            os.chown(self.state, 12345, 0)
            with self.assertRaises(ValueError):
                self.read(origin())
        else:
            values = list(os.stat(self.state))
            values[4] = 12345
            with mock.patch.object(
                    selection.os, "fstat", return_value=os.stat_result(values)):
                with self.assertRaises(ValueError):
                    self.read(origin())

    def test_distro_account_owned_state_is_accepted(self):
        # The install's tmpfiles stamp declares the channel file for the
        # distro account, so the unprivileged writer's file must be read.
        self.write_state(b"beta\n")
        with self.account(self.DISTRO_UID) as lookup, \
                self.owner(self.DISTRO_UID):
            self.assertEqual("beta", self.read(origin(channel="stable")))
            self.assertEqual(
                "beta", self.read(FREEDOM + "@sha256:" + DIGEST))
        lookup.assert_called_with("armada")

    def test_root_owned_state_is_accepted_without_the_distro_account(self):
        self.write_state(b"beta\n")
        with self.account(None), self.owner(0):
            self.assertEqual("beta", self.read(origin(channel="stable")))

    def test_unknown_owner_is_still_rejected(self):
        self.write_state(b"beta\n")
        with self.account(self.DISTRO_UID), self.owner(self.DISTRO_UID + 1):
            with self.assertRaises(ValueError):
                self.read(origin(channel="stable"))

    def test_missing_distro_account_allows_root_only(self):
        self.write_state(b"beta\n")
        with self.account(None):
            with self.owner(self.DISTRO_UID):
                with self.assertRaises(ValueError):
                    self.read(origin(channel="stable"))
            with self.owner(0):
                self.assertEqual("beta", self.read(origin(channel="stable")))

    def test_distro_account_owned_state_keeps_the_other_checks(self):
        self.write_state(b"beta\n")
        self.state.chmod(0o666)
        with self.account(self.DISTRO_UID), self.owner(self.DISTRO_UID):
            with self.assertRaises(ValueError):
                self.read(origin(channel="stable"))

        self.state.chmod(0o644)
        self.state.unlink()
        target = self.root / "target"
        target.write_text("beta\n")
        self.state.symlink_to(target)
        with self.account(self.DISTRO_UID), self.owner(self.DISTRO_UID):
            with self.assertRaises(ValueError):
                self.read(origin(channel="stable"))

    def test_read_does_not_change_file_or_directory_inventory(self):
        self.write_state(b"beta\n")
        before_content = self.state.read_bytes()
        before_stat = self.state.stat()
        before_inventory = sorted(
            (str(path.relative_to(self.root)), path.is_dir())
            for path in self.root.rglob("*"))
        self.assertEqual("beta", self.read(origin(channel="stable")))
        after_stat = self.state.stat()
        self.assertEqual(before_content, self.state.read_bytes())
        self.assertEqual(stat.S_IMODE(before_stat.st_mode),
                         stat.S_IMODE(after_stat.st_mode))
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_inventory, sorted(
            (str(path.relative_to(self.root)), path.is_dir())
            for path in self.root.rglob("*")))

    def test_state_path_must_be_absolute_and_canonical(self):
        with self.assertRaises(ValueError):
            self.read(origin(), Path("relative/channel"))
        noncanonical = self.root / "missing" / ".." / "channel"
        with self.assertRaises(ValueError):
            self.read(origin(), noncanonical)


if __name__ == "__main__":
    unittest.main()
