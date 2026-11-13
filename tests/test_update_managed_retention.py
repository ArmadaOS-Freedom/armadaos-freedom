"""Focused tests for updater-owned deployment retention.

These tests use isolated fake OSTree objects only. They never touch a real
sysroot, repository, bootloader or deployment directory. They are plain
unittest tests so the repository's unittest discovery runs them without any
extra dependency.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LIB = Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib"
sys.path.insert(0, str(LIB))

from armada.updates import deployment_store as store  # noqa: E402


class FakeKeyFile:
    def __init__(self):
        self.values = {}

    def set_string(self, group, key, value):
        self.values[(group, key)] = value

    def get_string(self, group, key):
        return self.values[(group, key)]

    def remove_key(self, group, key):
        return self.values.pop((group, key), None) is not None


class FakeBootConfig:
    def __init__(self, options="quiet"):
        self.options = options

    def get(self, key):
        return self.options if key == "options" else None


class FakeDeployment:
    def __init__(self, osname, csum, serial, *, origin=None, pinned=False,
                 unlocked=0, options="quiet"):
        self.osname = osname
        self.csum = csum
        self.serial = serial
        self.origin = origin
        self.pinned = pinned
        self.unlocked = unlocked
        self.boot = FakeBootConfig(options)

    def get_osname(self):
        return self.osname

    def get_csum(self):
        return self.csum

    def get_deployserial(self):
        return self.serial

    def get_origin(self):
        return self.origin

    def is_pinned(self):
        return self.pinned

    def get_unlocked(self):
        return self.unlocked

    def get_bootconfig(self):
        return self.boot


class FakeSysroot:
    active = None

    def __init__(self, deployments, staged=None, repo=None, booted=None):
        self.deployments = list(deployments)
        self.staged = staged
        self.repo = repo
        self.booted = booted
        self.writes = []
        self.pin_changes = []
        self.origin_writes = []
        self.kargs_changes = []
        self.events = []
        self.deployed_origin = None

    @classmethod
    def new(cls, _gfile):
        return cls.active

    def initialize_with_mount_namespace(self, _flags):
        return True

    def try_lock(self):
        return True, True

    def load(self, _flags):
        return True

    def unlock(self):
        pass

    def get_deployments(self):
        return list(self.deployments)

    def get_staged_deployment(self):
        return self.staged

    def get_booted_deployment(self):
        return self.booted

    def get_repo(self, _flags):
        return True, self.repo

    def deployment_set_pinned(self, dep, pinned):
        dep.pinned = pinned
        self.pin_changes.append((store._binding_of(dep), pinned))
        self.events.append(("pin", store._binding_of(dep), pinned))
        return True

    def write_origin_file(self, dep, new_origin, _cancellable):
        self.origin_writes.append((store._binding_of(dep), dict(new_origin.values)))
        self.events.append(("origin", store._binding_of(dep)))
        dep.origin = new_origin
        return True

    def deployment_set_kargs(self, dep, kargs, _options):
        dep.boot.options = " ".join(kargs)
        self.kargs_changes.append((store._binding_of(dep), tuple(kargs)))
        self.events.append(("kargs", store._binding_of(dep)))
        return True

    def write_deployments(self, deployments, _options):
        self.writes.append(list(deployments))
        self.deployments = list(deployments)
        self.events.append(
            ("write", tuple(store._binding_of(d) for d in deployments)))
        return True

    def deploy_tree(self, osname, commit, origin, _parent, _ref, _flags):
        self.deployed_origin = origin
        return True, FakeDeployment(osname, commit, 7, origin=origin)


class FakeRepo:
    def __init__(self, commit, refs=None, resolve_ok=True, list_ok=True,
                 remove_fail_names=()):
        self.commit = commit
        self.refs = dict(refs or {})
        self.resolve_ok = resolve_ok
        self.list_ok = list_ok
        self.remove_fail_names = set(remove_fail_names)
        self.ref_changes = []
        self.list_calls = []
        self.events = None

    def load_commit(self, commit):
        if commit != self.commit:
            return False, None, 0
        return True, object(), 0

    def resolve_rev(self, name, _allow_noent):
        # Mirrors libostree: (True, checksum) when the ref exists,
        # (True, None) for a missing ref under allow_noent, and (False, None)
        # only when the lookup itself fails.
        if not self.resolve_ok:
            return False, None
        if name in self.refs:
            return True, self.refs[name]
        return True, None

    def list_refs_ext(self, refspec_prefix, flags, _cancellable):
        # Mirrors libostree: the argument is a refspec prefix, not a path, so a
        # trailing separator is rejected outright with "Invalid refspec" (the
        # real GI call raises GLib.Error).  A valid prefix returns FULL ref
        # names, and remote refs, which libostree keys as "remote:ref", are
        # excluded when EXCLUDE_REMOTES is set; the prefix is a plain string.
        if refspec_prefix is not None and refspec_prefix.endswith("/"):
            raise ValueError(f"Invalid refspec {refspec_prefix}")
        self.list_calls.append((refspec_prefix, flags))
        if not self.list_ok:
            return False, None
        visible = {}
        for name, checksum in self.refs.items():
            if ":" in name:
                continue
            if refspec_prefix and not name.startswith(refspec_prefix):
                continue
            visible[name] = checksum
        return True, visible

    def set_ref_immediate(self, remote, ref, commit, _cancellable):
        self.ref_changes.append((remote, ref, commit))
        if self.events is not None:
            self.events.append(("ref", ref, commit))
        if commit is None and ref in self.remove_fail_names:
            return False
        if commit is None:
            self.refs.pop(ref, None)
        else:
            self.refs[ref] = commit
        return True


class FakeGLib:
    KeyFile = FakeKeyFile


class FakeGio:
    class File:
        @staticmethod
        def new_for_path(_path):
            return object()


class FakeOSTree:
    class DeploymentUnlockedState:
        NONE = 0

    class RepoListRefsExtFlags:
        EXCLUDE_REMOTES = 1 << 1
        EXCLUDE_MIRRORS = 1 << 2

    Sysroot = FakeSysroot


def binding(commit, serial, osname="armada"):
    return f"{osname}/{commit}.{serial}"


def origin_ref(digest=None):
    return (
        "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
        "armadaos-freedom:stable@" + (digest or "sha256:" + "d" * 64)
    )


def channel_ref(channel, digest=None):
    return (
        "ostree-image-signed:docker://ghcr.io/armadaos-freedom/"
        "armadaos-freedom:" + channel + "@" + (digest or "sha256:" + "d" * 64)
    )


def make_origin(marked=True, ref=None):
    origin = FakeKeyFile()
    origin.set_string("origin", "container-image-reference", ref or origin_ref())
    if marked:
        origin.set_string("origin", "armada-managed", "1")
    return origin


def deployment(commit, serial, *, marked=True, pinned=False, unlocked=0,
               osname="armada", marker_binding=None):
    dep_binding = binding(commit, serial, osname)
    origin = make_origin(marked=marked)
    if marked:
        origin.set_string("origin", "armada-managed-binding",
                          marker_binding or dep_binding)
    return FakeDeployment(
        osname,
        commit,
        serial,
        origin=origin,
        pinned=pinned,
        unlocked=unlocked,
        options=f"quiet armada.deployment={dep_binding}",
    )


class ManagedRetentionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name) / "sysroot"
        (self.root / "ostree" / "repo").mkdir(parents=True)
        (self.root / "boot").mkdir()

        patches = [
            mock.patch.object(store, "_gi", lambda: (FakeGio, FakeGLib, FakeOSTree)),
            mock.patch.object(store, "_require_nonsymlink_dir", lambda *args: None),
            mock.patch.object(store, "_candidate_path", lambda *args: "/fake/deployment"),
            mock.patch.object(store, "_load_manifest", lambda *args: {"kernelRelease": "6.1.0"}),
            mock.patch.object(store, "_validate_commit_metadata", lambda *args: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_sysroot(self, sysroot):
        FakeSysroot.active = sysroot
        return sysroot

    def test_write_origin_uses_ostree_sysroot_api(self):
        """The origin write goes through Sysroot.write_origin_file only."""
        dep = deployment("a" * 64, 3)
        sysroot = mock.Mock()
        sysroot.write_origin_file.return_value = True

        store._write_origin(sysroot, dep)

        sysroot.write_origin_file.assert_called_once_with(dep, dep.origin, None)
        self.assertEqual(dep.origin.get_string("origin", "container-image-reference"),
                         origin_ref())

    def test_write_origin_reports_failure(self):
        dep = deployment("a" * 64, 3)
        sysroot = mock.Mock()
        sysroot.write_origin_file.return_value = False

        with self.assertRaises(store.DeploymentError):
            store._write_origin(sysroot, dep)

    def test_write_origin_rejects_missing_origin(self):
        dep = deployment("a" * 64, 3)
        dep.origin = None
        sysroot = mock.Mock()

        with self.assertRaises(store.DeploymentError):
            store._write_origin(sysroot, dep)
        sysroot.write_origin_file.assert_not_called()

    def test_two_step_release_then_remove_on_distinct_confirmation(self):
        current = deployment("a" * 64, 3, pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        old_one = deployment("c" * 64, 1, pinned=True)
        old_two = deployment("d" * 64, 1, pinned=True)
        unmarked_user_owned = deployment("e" * 64, 1, marked=False, pinned=True)
        cloned_marker = deployment("7" * 64, 1, pinned=True,
                                   marker_binding=binding("8" * 64, 1))
        unlocked = deployment("1" * 64, 1, pinned=True, unlocked=1)
        foreign = deployment("2" * 64, 1, pinned=True, osname="other-stateroot")

        deps = [current, previous, old_one, old_two, unmarked_user_owned,
                cloned_marker, unlocked, foreign]
        sysroot = self.use_sysroot(FakeSysroot(deps))

        first = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )

        self.assertEqual(first, {
            "deployment": binding("a" * 64, 3),
            "previousDeployment": binding("b" * 64, 2),
            "changed": False,
        })
        self.assertEqual(sysroot.writes, [])
        self.assertFalse(old_one.pinned)
        self.assertFalse(old_two.pinned)
        self.assertEqual(store._retire_after(old_one), binding("a" * 64, 3))
        self.assertEqual(store._retire_after(old_two), binding("a" * 64, 3))
        for stale in (old_one, old_two):
            stale_binding = store._binding_of(stale)
            self.assertLess(
                sysroot.events.index(("origin", stale_binding)),
                sysroot.events.index(("pin", stale_binding, False)),
            )
        self.assertTrue(unmarked_user_owned.pinned)
        self.assertIsNone(store._retire_after(unmarked_user_owned))
        self.assertTrue(cloned_marker.pinned)
        self.assertIsNone(store._retire_after(cloned_marker))
        self.assertFalse(store._is_updater_created(cloned_marker))
        self.assertTrue(unlocked.pinned)
        self.assertTrue(foreign.pinned)

        # Each origin write targets the redeployment's own binding, so the
        # marker keeps its per-binding identity after the release rewrite.
        released_writes = [w for w in sysroot.origin_writes]
        self.assertEqual(
            [w[0] for w in released_writes],
            [store._binding_of(old_one), store._binding_of(old_two)],
        )
        for dep_binding, values in released_writes:
            self.assertEqual(values[(store._MANAGED_GROUP, store._MANAGED_BINDING_KEY)],
                             dep_binding)
            self.assertEqual(values[(store._MANAGED_GROUP, store._RETIRE_AFTER_KEY)],
                             binding("a" * 64, 3))

        new_current = deployment("9" * 64, 4, pinned=True)
        sysroot.deployments.insert(0, new_current)
        sysroot.pin_changes.clear()
        sysroot.origin_writes.clear()

        second = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("9" * 64, 4),
            previous_binding=binding("a" * 64, 3),
        )

        expected = [
            binding("9" * 64, 4),
            binding("a" * 64, 3),
            binding("b" * 64, 2),
            binding("e" * 64, 1),
            binding("7" * 64, 1),
            binding("1" * 64, 1),
            binding("2" * 64, 1, "other-stateroot"),
        ]
        self.assertEqual(second["deployment"], binding("9" * 64, 4))
        self.assertEqual(second["previousDeployment"], binding("a" * 64, 3))
        self.assertTrue(second["changed"])
        self.assertEqual([store._binding_of(d) for d in sysroot.deployments], expected)
        self.assertEqual(len(sysroot.writes), 1)
        self.assertEqual([store._binding_of(d) for d in sysroot.writes[0]], expected)
        self.assertNotIn((binding("c" * 64, 1), False), sysroot.pin_changes)
        self.assertNotIn((binding("d" * 64, 1), False), sysroot.pin_changes)
        self.assertIn((binding("b" * 64, 2), False), sysroot.pin_changes)
        self.assertTrue(cloned_marker.pinned)
        self.assertTrue(unlocked.pinned)
        self.assertTrue(foreign.pinned)

    def test_repinned_released_deployment_is_protected_forever(self):
        current = deployment("a" * 64, 3, pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        stale = deployment("c" * 64, 1, pinned=True)
        sysroot = self.use_sysroot(FakeSysroot([current, previous, stale]))

        first = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )
        self.assertEqual([store._binding_of(d) for d in sysroot.deployments], [
            binding("a" * 64, 3),
            binding("b" * 64, 2),
            binding("c" * 64, 1),
        ])
        self.assertFalse(stale.pinned)
        self.assertEqual(store._retire_after(stale), binding("a" * 64, 3))

        stale.pinned = True  # administrator takes the released deployment back
        sysroot.pin_changes.clear()
        new_current = deployment("9" * 64, 4, pinned=True)
        sysroot.deployments.insert(0, new_current)

        second = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("9" * 64, 4),
            previous_binding=binding("a" * 64, 3),
        )

        self.assertFalse(second["changed"])
        self.assertEqual(sysroot.writes, [])
        self.assertTrue(stale.pinned)
        self.assertNotIn((binding("c" * 64, 1), False), sysroot.pin_changes)
        self.assertEqual([store._binding_of(d) for d in sysroot.deployments], [
            binding("9" * 64, 4),
            binding("a" * 64, 3),
            binding("b" * 64, 2),
            binding("c" * 64, 1),
        ])

    def test_retry_same_confirmation_is_idempotent(self):
        current = deployment("a" * 64, 3, pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        stale = deployment("c" * 64, 1, pinned=True)
        sysroot = self.use_sysroot(FakeSysroot([current, previous, stale]))

        first = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )
        pins = list(sysroot.pin_changes)
        origins = list(sysroot.origin_writes)
        self.assertFalse(stale.pinned)

        second = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )

        self.assertEqual(second, first)
        self.assertEqual(sysroot.pin_changes, pins)
        self.assertEqual(sysroot.origin_writes, origins)
        self.assertEqual(sysroot.writes, [])
        self.assertFalse(stale.pinned)

    def test_actually_booted_deployment_is_protected(self):
        current = deployment("a" * 64, 3, pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        booted_other_slot = deployment("c" * 64, 4, pinned=True)
        sysroot = self.use_sysroot(
            FakeSysroot([current, previous, booted_other_slot],
                        booted=booted_other_slot))

        result = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )

        self.assertFalse(result["changed"])
        self.assertEqual([store._binding_of(d) for d in sysroot.deployments], [
            binding("a" * 64, 3),
            binding("b" * 64, 2),
            binding("c" * 64, 4),
        ])
        self.assertEqual(sysroot.writes, [])
        self.assertTrue(booted_other_slot.pinned)
        self.assertIsNone(store._retire_after(booted_other_slot))

    def test_staged_deployment_refuses_before_any_state_change(self):
        current = deployment("a" * 64, 3, pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        stale = deployment("c" * 64, 1, pinned=True)
        staged = deployment("d" * 64, 9, pinned=True)
        sysroot = self.use_sysroot(
            FakeSysroot([current, previous, stale, staged], staged=staged))

        with self.assertRaises(store.DeploymentError):
            store.promote_confirmed_deployment(
                sysroot_path=str(self.root),
                current_binding=binding("a" * 64, 3),
                previous_binding=binding("b" * 64, 2),
            )

        self.assertEqual(sysroot.writes, [])
        self.assertEqual(sysroot.pin_changes, [])
        self.assertEqual(sysroot.origin_writes, [])
        self.assertTrue(stale.pinned)

    def test_fresh_deployment_origin_is_marked_updater_created(self):
        current = deployment("a" * 64, 3)
        target = "e" * 64
        sysroot = self.use_sysroot(FakeSysroot([current], repo=FakeRepo(target)))

        result = store.append_verified_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            target_commit=target,
            oci_digest="sha256:" + "d" * 64,
            source_revision="f" * 40,
            version="1.2.3",
            model="TB322FC",
            origin_ref=origin_ref(),
        )

        self.assertFalse(result["reused"])
        candidate = [d for d in sysroot.deployments if d.get_csum() == target][0]
        self.assertEqual(candidate.origin.get_string("origin", "armada-managed"), "1")
        self.assertEqual(
            candidate.origin.get_string("origin", "armada-managed-binding"),
            store._binding_of(candidate))
        self.assertEqual(candidate.origin.get_string("origin", "container-image-reference"),
                         origin_ref())
        self.assertEqual(sysroot.origin_writes[0][0], store._binding_of(candidate))
        self.assertTrue(store._is_updater_created(candidate))

    def test_reused_admin_target_is_not_marked(self):
        current = deployment("a" * 64, 3)
        target = "b" * 64
        admin_origin = make_origin(marked=False)
        reused = FakeDeployment(
            "armada",
            target,
            4,
            origin=admin_origin,
            options=f"quiet armada.deployment={binding(target, 4)}",
        )
        sysroot = self.use_sysroot(FakeSysroot([current, reused], repo=FakeRepo(target)))

        result = store.append_verified_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            target_commit=target,
            oci_digest="sha256:" + "d" * 64,
            source_revision="f" * 40,
            version="1.2.3",
            model="TB322FC",
            origin_ref=origin_ref(),
        )

        self.assertTrue(result["reused"])
        self.assertFalse(store._is_updater_created(reused))
        self.assertEqual(sysroot.origin_writes, [])
        with self.assertRaises(KeyError):
            reused.origin.get_string("origin", "armada-managed")

    def test_reused_released_target_clears_retirement_marker(self):
        """Re-pinning a released updater deployment resets its retirement."""
        current = deployment("a" * 64, 3, pinned=True)
        target = "b" * 64
        released = deployment(target, 4)
        released.origin.set_string(store._MANAGED_GROUP, store._RETIRE_AFTER_KEY,
                                   binding("a" * 64, 3))
        sysroot = self.use_sysroot(
            FakeSysroot([current, released], repo=FakeRepo(target)))

        result = store.append_verified_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            target_commit=target,
            oci_digest="sha256:" + "d" * 64,
            source_revision="f" * 40,
            version="1.2.3",
            model="TB322FC",
            origin_ref=origin_ref(),
        )

        self.assertTrue(result["reused"])
        self.assertTrue(released.pinned)
        self.assertIn((store._binding_of(released), True), sysroot.pin_changes)
        self.assertIsNone(store._retire_after(released))
        self.assertTrue(store._is_updater_created(released))
        self.assertEqual([w[0] for w in sysroot.origin_writes],
                         [store._binding_of(released)])
        written = sysroot.origin_writes[0][1]
        self.assertNotIn((store._MANAGED_GROUP, store._RETIRE_AFTER_KEY), written)
        self.assertEqual(
            written[(store._MANAGED_GROUP, store._MANAGED_BINDING_KEY)],
            store._binding_of(released))

        # The reused deployment carries no retirement marker any more, so a
        # later staleness pass releases it again instead of treating this
        # updater re-pin as an administrator take-over.
        new_current = deployment("9" * 64, 4, pinned=True)
        sysroot.deployments.insert(0, new_current)
        sysroot.pin_changes.clear()
        store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("9" * 64, 4),
            previous_binding=binding("a" * 64, 3),
        )

        self.assertFalse(released.pinned)
        self.assertIn((store._binding_of(released), False), sysroot.pin_changes)
        self.assertEqual(store._retire_after(released), binding("9" * 64, 4))

    def test_already_repinned_released_target_keeps_retirement_marker(self):
        """A deployment an administrator pinned again is left untouched."""
        current = deployment("a" * 64, 3, pinned=True)
        target = "b" * 64
        repinned = deployment(target, 4, pinned=True)
        repinned.origin.set_string(store._MANAGED_GROUP, store._RETIRE_AFTER_KEY,
                                   binding("a" * 64, 3))
        sysroot = self.use_sysroot(
            FakeSysroot([current, repinned], repo=FakeRepo(target)))

        result = store.append_verified_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            target_commit=target,
            oci_digest="sha256:" + "d" * 64,
            source_revision="f" * 40,
            version="1.2.3",
            model="TB322FC",
            origin_ref=origin_ref(),
        )

        self.assertTrue(result["reused"])
        self.assertTrue(repinned.pinned)
        self.assertEqual(store._retire_after(repinned), binding("a" * 64, 3))
        self.assertEqual(sysroot.pin_changes, [])
        self.assertEqual(sysroot.origin_writes, [])

    def append(self, sysroot, target, current="a" * 64, serial=3):
        return store.append_verified_deployment(
            sysroot_path=str(self.root),
            current_binding=binding(current, serial),
            target_commit=target,
            oci_digest="sha256:" + "d" * 64,
            source_revision="f" * 40,
            version="1.2.3",
            model="TB322FC",
            origin_ref=origin_ref(),
        )

    def test_append_refuses_staged_deployment_before_any_state_change(self):
        current = deployment("a" * 64, 3)
        staged = deployment("d" * 64, 9, pinned=True)
        sysroot = self.use_sysroot(
            FakeSysroot([current, staged], staged=staged))

        with self.assertRaises(store.DeploymentError):
            self.append(sysroot, "e" * 64)

        self.assertEqual(sysroot.writes, [])
        self.assertEqual(sysroot.pin_changes, [])
        self.assertEqual(sysroot.origin_writes, [])
        self.assertEqual(sysroot.kargs_changes, [])
        self.assertEqual(current.boot.options,
                         f"quiet armada.deployment={binding('a' * 64, 3)}")
        self.assertTrue(staged.pinned)

    def test_append_persists_first_install_current_self_binding(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        sysroot = self.use_sysroot(FakeSysroot([current], repo=FakeRepo(target)))

        result = self.append(sysroot, target)

        self.assertFalse(result["reused"])
        self.assertEqual(
            current.boot.options,
            f"quiet armada.deployment={binding('a' * 64, 3)}")
        self.assertEqual(
            current.boot.options.split().count(
                f"armada.deployment={binding('a' * 64, 3)}"), 1)
        self.assertEqual(
            [b for b, _ in sysroot.kargs_changes].count(binding("a" * 64, 3)), 1)
        self.assertNotIn(binding("a" * 64, 3),
                         [b for b, _ in sysroot.origin_writes])
        self.assertEqual(result["deployment"], binding(target, 7))
        candidate = [d for d in sysroot.deployments if d.get_csum() == target][0]
        self.assertTrue(store._is_updater_created(candidate))

    def test_append_holds_target_ref_until_candidate_is_registered(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        repo = FakeRepo(target)
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))
        repo.events = sysroot.events

        result = self.append(sysroot, target)

        created = [c for c in repo.ref_changes if c[2] is not None]
        self.assertEqual(len(created), 1)
        remote, held, commit = created[0]
        self.assertIsNone(remote)
        self.assertEqual(commit, target)
        self.assertRegex(held, r"^armada-update/pending/[0-9a-f]{32}$")
        self.assertFalse(result["reused"])
        self.assertEqual(repo.ref_changes[-1], (None, held, None))
        self.assertEqual(repo.refs, {})
        self.assertLess(sysroot.events.index(("ref", held, target)),
                        sysroot.events.index(("kargs", binding("a" * 64, 3))))
        registered = sysroot.events.index(
            ("write", (binding("a" * 64, 3), binding(target, 7))))
        self.assertLess(sysroot.events.index(("kargs", binding("a" * 64, 3))),
                        registered)
        self.assertLess(registered, sysroot.events.index(("ref", held, None)))

    def test_append_releases_target_ref_when_deploy_fails(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        repo = FakeRepo(target)

        class FailingDeploy(FakeSysroot):
            def deploy_tree(self, *_args):
                return False, None

        sysroot = self.use_sysroot(FailingDeploy([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError, "deploy_tree failed"):
            self.append(sysroot, target)

        self.assertEqual(len(repo.ref_changes), 2)
        remote, held, commit = repo.ref_changes[0]
        self.assertIsNone(remote)
        self.assertEqual(commit, target)
        self.assertRegex(held, r"^armada-update/pending/[0-9a-f]{32}$")
        self.assertEqual(repo.ref_changes[1], (None, held, None))
        self.assertEqual(repo.refs, {})

    def test_append_reuse_creates_no_temporary_ref(self):
        current = deployment("a" * 64, 3)
        target = "b" * 64
        reused = FakeDeployment(
            "armada", target, 4, origin=make_origin(marked=False),
            options=f"quiet armada.deployment={binding(target, 4)}")
        repo = FakeRepo(target)
        sysroot = self.use_sysroot(FakeSysroot([current, reused], repo=repo))

        result = self.append(sysroot, target)

        self.assertTrue(result["reused"])
        self.assertEqual(repo.ref_changes, [])
        self.assertEqual(repo.refs, {})

    def test_append_refuses_ref_that_already_resolves(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        held = f"{store._TEMP_REF_PREFIX}/{'0' * 32}"
        foreign_commit = "c" * 64
        repo = FakeRepo(target, refs={held: foreign_commit})
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))

        with mock.patch.object(store, "_new_temp_ref", return_value=held):
            with self.assertRaisesRegex(store.DeploymentError, "already exists"):
                self.append(sysroot, target)

        self.assertEqual(repo.refs, {held: foreign_commit})
        self.assertEqual(repo.ref_changes, [])

    def test_append_refuses_when_ref_probe_fails(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        repo = FakeRepo(target, resolve_ok=False)
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError,
                                    "failed checking temporary ref"):
            self.append(sysroot, target)

        self.assertEqual(repo.refs, {})
        self.assertEqual(repo.ref_changes, [])

    def test_cleanup_failure_keeps_operation_error_or_surfaces_alone(self):
        target = "e" * 64

        class RefusingRelease(FakeRepo):
            def set_ref_immediate(self, remote, ref, commit, cancellable):
                if commit is None:
                    self.ref_changes.append((remote, ref, commit))
                    return False
                return super().set_ref_immediate(
                    remote, ref, commit, cancellable)

        class FailingDeploy(FakeSysroot):
            def deploy_tree(self, *_args):
                return False, None

        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        repo = RefusingRelease(target)
        sysroot = self.use_sysroot(FailingDeploy([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError, "deploy_tree failed"):
            self.append(sysroot, target)
        self.assertEqual(len(repo.refs), 1)
        held = next(iter(repo.refs))
        self.assertRegex(held, r"^armada-update/pending/[0-9a-f]{32}$")
        self.assertEqual(repo.refs[held], target)

        current_ok = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        repo_ok = RefusingRelease(target)
        sysroot_ok = self.use_sysroot(FakeSysroot([current_ok], repo=repo_ok))

        with self.assertRaisesRegex(store.DeploymentError,
                                    "failed removing temporary ref"):
            self.append(sysroot_ok, target)

    def test_append_reconciles_leaked_temporary_refs_before_mutation(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        leaked_one = f"{store._TEMP_REF_PREFIX}/{'1' * 32}"
        leaked_two = f"{store._TEMP_REF_PREFIX}/{'2' * 32}"
        foreign_refs = {
            "refs/heads/main": "f" * 64,
            f"{store._TEMP_REF_PREFIX}/not-a-uuid": "c" * 64,
            f"{store._TEMP_REF_PREFIX}/{'3' * 31}": "c" * 64,
            f"{store._TEMP_REF_PREFIX}/{'4' * 32}/extra": "c" * 64,
            f"{store._TEMP_REF_PREFIX}-sibling/{'6' * 32}": "c" * 64,
            "other/pending/" + "5" * 32: "c" * 64,
            "origin:refs/heads/main": "c" * 64,
        }
        repo = FakeRepo(target, refs={
            leaked_one: "b" * 64,
            leaked_two: "c" * 64,
            **foreign_refs,
        })
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))
        repo.events = sysroot.events

        self.append(sysroot, target)

        for name in (leaked_one, leaked_two):
            self.assertNotIn(name, repo.refs)
            self.assertIn((None, name, None), repo.ref_changes)
        for name in foreign_refs:
            self.assertIn(name, repo.refs)
            self.assertNotIn((None, name, None), repo.ref_changes)

        created = [c for c in repo.ref_changes if c[2] is not None]
        self.assertEqual(len(created), 1)
        held = created[0][1]
        self.assertRegex(held, r"^armada-update/pending/[0-9a-f]{32}$")
        # The target commit is protected by this transaction's own ref before
        # any leaked ref is dropped.
        first_release = min(
            sysroot.events.index(("ref", name, None))
            for name in (leaked_one, leaked_two))
        self.assertLess(sysroot.events.index(("ref", held, target)),
                        first_release)
        # Cleanup completes before the first mutation that can prune.
        first_kargs = sysroot.events.index(("kargs", binding("a" * 64, 3)))
        self.assertLess(first_release, first_kargs)
        self.assertNotEqual(repo.list_calls, [])
        prefix, flags = repo.list_calls[0]
        self.assertEqual(prefix, store._TEMP_REF_PREFIX)
        self.assertFalse(prefix.endswith("/"))
        self.assertTrue(flags & FakeOSTree.RepoListRefsExtFlags.EXCLUDE_REMOTES)
        self.assertTrue(flags & FakeOSTree.RepoListRefsExtFlags.EXCLUDE_MIRRORS)

    def test_append_reports_leaked_ref_cleanup_failure_for_retry(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        leaked = f"{store._TEMP_REF_PREFIX}/{'1' * 32}"
        repo = FakeRepo(target, refs={leaked: "b" * 64},
                        remove_fail_names={leaked})
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError,
                                    "failed removing leaked temporary refs"):
            self.append(sysroot, target)

        # The leaked ref survives so the next attempt sees and retries it,
        # and no mutation happened on this one.
        self.assertEqual(repo.refs, {leaked: "b" * 64})
        self.assertEqual(sysroot.writes, [])
        self.assertEqual(sysroot.kargs_changes, [])
        self.assertEqual(sysroot.pin_changes, [])
        created = [c for c in repo.ref_changes if c[2] is not None]
        self.assertEqual(len(created), 1)
        self.assertEqual(repo.ref_changes[-1], (None, created[0][1], None))

    def test_append_failed_temp_ref_listing_aborts_before_mutation(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        repo = FakeRepo(target, list_ok=False)
        sysroot = self.use_sysroot(FakeSysroot([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError,
                                    "failed listing temporary refs"):
            self.append(sysroot, target)

        self.assertEqual(sysroot.kargs_changes, [])
        self.assertEqual(sysroot.writes, [])
        self.assertEqual(sysroot.pin_changes, [])
        # This attempt's own protection is released again.
        created = [c for c in repo.ref_changes if c[2] is not None]
        self.assertEqual(len(created), 1)
        self.assertEqual(repo.ref_changes[-1], (None, created[0][1], None))
        self.assertEqual(repo.refs, {})

    def test_append_failed_try_lock_touches_no_refs(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet")
        target = "e" * 64
        repo = FakeRepo(target)

        class BusySysroot(FakeSysroot):
            def try_lock(self):
                return True, False

        sysroot = self.use_sysroot(BusySysroot([current], repo=repo))

        with self.assertRaisesRegex(store.DeploymentError, "sysroot busy"):
            self.append(sysroot, target)

        self.assertEqual(repo.list_calls, [])
        self.assertEqual(repo.ref_changes, [])
        self.assertEqual(sysroot.writes, [])

    def test_existing_target_on_other_channel_gets_fresh_deployment(self):
        current = deployment("a" * 64, 3)
        target = "b" * 64
        other = FakeDeployment(
            "armada", target, 4,
            origin=make_origin(marked=False, ref=channel_ref("beta")),
            options=f"quiet armada.deployment={binding(target, 4)}")
        before = dict(other.origin.values)
        sysroot = self.use_sysroot(
            FakeSysroot([current, other], repo=FakeRepo(target)))

        result = self.append(sysroot, target)

        self.assertFalse(result["reused"])
        self.assertEqual(result["deployment"], binding(target, 7))
        self.assertEqual(dict(other.origin.values), before)
        self.assertNotIn(binding(target, 4),
                         [b for b, _ in sysroot.origin_writes])
        self.assertNotIn(binding(target, 4),
                         [b for b, _ in sysroot.kargs_changes])
        fresh = [d for d in sysroot.deployments if store._binding_of(d) == binding(target, 7)][0]
        self.assertEqual(fresh.origin.get_string("origin", "container-image-reference"),
                         origin_ref())

    def test_exact_origin_target_is_selected_over_other_channel_serial(self):
        current = deployment("a" * 64, 3)
        target = "b" * 64
        beta = FakeDeployment(
            "armada", target, 4,
            origin=make_origin(marked=False, ref=channel_ref("beta")),
            options=f"quiet armada.deployment={binding(target, 4)}")
        stable = FakeDeployment(
            "armada", target, 5,
            origin=make_origin(marked=False, ref=origin_ref()),
            options=f"quiet armada.deployment={binding(target, 5)}")
        sysroot = self.use_sysroot(
            FakeSysroot([current, beta, stable], repo=FakeRepo(target)))

        result = self.append(sysroot, target)

        self.assertTrue(result["reused"])
        self.assertEqual(result["deployment"], binding(target, 5))
        self.assertNotIn(binding(target, 4),
                         [b for b, _ in sysroot.origin_writes])
        self.assertNotIn(binding(target, 4),
                         [b for b, _ in sysroot.kargs_changes])
        self.assertEqual(sysroot.writes, [])

    def test_ambiguous_exact_origin_targets_are_rejected(self):
        current = deployment("a" * 64, 3)
        target = "b" * 64
        first = FakeDeployment(
            "armada", target, 4, origin=make_origin(marked=False, ref=origin_ref()),
            options=f"quiet armada.deployment={binding(target, 4)}")
        second = FakeDeployment(
            "armada", target, 5, origin=make_origin(marked=False, ref=origin_ref()),
            options=f"quiet armada.deployment={binding(target, 5)}")
        sysroot = self.use_sysroot(
            FakeSysroot([current, first, second], repo=FakeRepo(target)))

        with self.assertRaises(store.DeploymentError):
            self.append(sysroot, target)

        self.assertEqual(sysroot.writes, [])
        self.assertEqual(sysroot.pin_changes, [])
        self.assertEqual(sysroot.origin_writes, [])
        self.assertEqual(sysroot.kargs_changes, [])

    def test_tampered_existing_target_origin_is_rejected(self):
        target = "b" * 64
        for bad_ref in (
            channel_ref("stable", "sha256:" + "e" * 64),
            "ostree-image-signed:docker://evil.example/armadaos-freedom/"
            "armadaos-freedom:stable@sha256:" + "d" * 64,
            "docker://ghcr.io/armadaos-freedom/armadaos-freedom:"
            "stable@sha256:" + "d" * 64,
            channel_ref("nightly"),
        ):
            with self.subTest(bad_ref=bad_ref):
                current = deployment("a" * 64, 3)
                existing = FakeDeployment(
                    "armada", target, 4, origin=make_origin(marked=False, ref=bad_ref),
                    options=f"quiet armada.deployment={binding(target, 4)}")
                sysroot = self.use_sysroot(
                    FakeSysroot([current, existing], repo=FakeRepo(target)))

                with self.assertRaises(store.DeploymentError):
                    self.append(sysroot, target)

                self.assertEqual(sysroot.writes, [])
                self.assertEqual(sysroot.pin_changes, [])
                self.assertEqual(sysroot.origin_writes, [])
                self.assertEqual(sysroot.kargs_changes, [])
                self.assertEqual(existing.origin.get_string(
                    "origin", "container-image-reference"), bad_ref)

    def test_ensure_deployment_binding_persists_canonical_binding_once(self):
        dep = FakeDeployment(
            "armada", "b" * 64, 4, origin=make_origin(marked=False), options="quiet")
        sysroot = self.use_sysroot(FakeSysroot([dep]))

        first = store.ensure_deployment_binding(
            sysroot_path=str(self.root), binding=binding("b" * 64, 4))

        self.assertEqual(first, {"deployment": binding("b" * 64, 4), "changed": True})
        self.assertEqual(dep.boot.options,
                         f"quiet armada.deployment={binding('b' * 64, 4)}")
        self.assertEqual(sysroot.origin_writes, [])

        second = store.ensure_deployment_binding(
            sysroot_path=str(self.root), binding=binding("b" * 64, 4))

        self.assertEqual(second, {"deployment": binding("b" * 64, 4), "changed": False})
        self.assertEqual(len(sysroot.kargs_changes), 1)

    def test_ensure_deployment_binding_rejects_conflicting_tokens(self):
        for options in (
            f"quiet armada.deployment={binding('c' * 64, 4)}",
            f"quiet armada.deployment={binding('b' * 64, 4)} "
            f"armada.deployment={binding('c' * 64, 4)}",
            "quiet armada.deployment",
        ):
            with self.subTest(options=options):
                dep = FakeDeployment(
                    "armada", "b" * 64, 4, origin=make_origin(marked=False),
                    options=options)
                sysroot = self.use_sysroot(FakeSysroot([dep]))

                with self.assertRaises(store.DeploymentError):
                    store.ensure_deployment_binding(
                        sysroot_path=str(self.root), binding=binding("b" * 64, 4))

                self.assertEqual(sysroot.kargs_changes, [])
                self.assertEqual(dep.boot.options, options)

    def test_ensure_deployment_binding_refuses_staged_sysroot(self):
        dep = FakeDeployment(
            "armada", "b" * 64, 4, origin=make_origin(marked=False), options="quiet")
        staged = deployment("d" * 64, 9, pinned=True)
        sysroot = self.use_sysroot(FakeSysroot([dep, staged], staged=staged))

        with self.assertRaises(store.DeploymentError):
            store.ensure_deployment_binding(
                sysroot_path=str(self.root), binding=binding("b" * 64, 4))

        self.assertEqual(sysroot.kargs_changes, [])
        self.assertEqual(dep.boot.options, "quiet")
        self.assertTrue(staged.pinned)

    def test_promote_persists_unbound_current_binding(self):
        current = FakeDeployment(
            "armada", "a" * 64, 3, origin=make_origin(marked=False),
            options="quiet", pinned=True)
        previous = deployment("b" * 64, 2, pinned=True)
        sysroot = self.use_sysroot(FakeSysroot([current, previous]))

        result = store.promote_confirmed_deployment(
            sysroot_path=str(self.root),
            current_binding=binding("a" * 64, 3),
            previous_binding=binding("b" * 64, 2),
        )

        self.assertFalse(result["changed"])
        self.assertEqual(result["deployment"], binding("a" * 64, 3))
        self.assertEqual(current.boot.options,
                         f"quiet armada.deployment={binding('a' * 64, 3)}")
        self.assertEqual([b for b, _ in sysroot.kargs_changes],
                         [binding("a" * 64, 3)])
        self.assertEqual(sysroot.origin_writes, [])
        self.assertEqual(sysroot.writes, [])


if __name__ == "__main__":
    unittest.main()
