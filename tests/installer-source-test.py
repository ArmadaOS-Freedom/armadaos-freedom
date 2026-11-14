#!/usr/bin/python3
"""Run with root and bootc/OSTree/PyGObject in a disposable Armada container."""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
import weakref
from unittest.mock import Mock, patch

import gi
gi.require_version("OSTree", "1.0")
from gi.repository import Gio, GLib, OSTree

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "system_files/usr/libexec/armada/armada-installer"
module = runpy.run_path(str(INSTALLER))
SourceImage = module["SourceImage"]
Error = module["Error"]
EXT = ["bootc", "internals", "ostree-ext", "container", "image"]
ORIGIN = "ostree-image-signed:docker://ghcr.io/armada-os/armada:beta"
FREEDOM = "ghcr.io/armadaos-freedom/armadaos-freedom"
FREEDOM_ORIGIN = "ostree-image-signed:docker://" + FREEDOM + ":testing"


def run(*args):
    return subprocess.check_output([str(arg) for arg in args], text=True, stderr=subprocess.STDOUT).strip()


def oci(dest, marker, arch='arm64'):
    (dest / 'blobs/sha256').mkdir(parents=True)

    def blob(data, media):
        h = hashlib.sha256(data).hexdigest()
        (dest / 'blobs/sha256' / h).write_bytes(data)
        return {'mediaType': media, 'digest': 'sha256:' + h, 'size': len(data)}

    layers = []
    for files in [
        {'usr/lib/os-release': b'ID=armada-audit\nVERSION_ID=1\n',
         'usr/share/audit/base': b'base layer\n',
         'usr/lib/modules/6.1.0/vmlinuz': b'FAKE KERNEL FOR METADATA TEST ONLY\n',
         'usr/lib/modules/6.1.0/initramfs.img': b'FAKE INITRAMFS\n',
         'usr/lib/modules/6.1.0/dtb/test.dtb': b'FAKE DTB\n',
         'usr/lib/bootc/kargs.d/10-armada.toml':
             b'match-architectures = ["aarch64"]\n'
             b'kargs = ["source-only-flag", "quiet", "root=UUID=filtered", "boot=UUID=filtered", '
             b'"rootflags=subvol=filtered", "ostree=filtered"]\n',
         'var/lib/audit/seeded': b'seeded var content\n'},
        {'usr/share/audit/content': marker.encode(),
         'usr/etc/audit.conf': b'factory=true\n'},
    ]:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as tar:
            dirs = sorted({str(parent) for f in files for parent in Path(f).parents
                           if str(parent) != '.'})
            for d in dirs:
                info = tarfile.TarInfo(d)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            for f, data in files.items():
                info = tarfile.TarInfo(f)
                info.mode = 0o644
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        layers.append(blob(stream.getvalue(), 'application/vnd.oci.image.layer.v1.tar'))
    config = {'architecture': arch, 'os': 'linux', 'config': {
        'Labels': {'containers.bootc': '1', 'ostree.bootable': 'true'}},
        'rootfs': {'type': 'layers', 'diff_ids': [x['digest'] for x in layers]}}
    config_desc = blob(json.dumps(config).encode(), 'application/vnd.oci.image.config.v1+json')
    manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'config': config_desc, 'layers': layers}
    desc = blob(json.dumps(manifest).encode(), manifest['mediaType'])
    desc['annotations'] = {'org.opencontainers.image.ref.name': 'latest'}
    (dest / 'index.json').write_text(json.dumps({'schemaVersion': 2, 'manifests': [desc]}))
    (dest / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    return f'ostree-unverified-image:oci:{dest}:latest'


def seed_manifest_digest(fixture):
    """Expected OCI manifest digest read directly from the fixture index.json."""
    return json.loads((fixture / "index.json").read_text())["manifests"][0]["digest"]


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installer-source-test.")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.path = self.work / "repo"
        run("ostree", "init", f"--repo={self.path}", "--mode=bare")
        run(*EXT, "pull", self.path, oci(self.work / "v1", "running image"))
        self.repo = OSTree.Repo.new(Gio.File.new_for_path(str(self.path)))
        self.repo.open(None)
        self.image_ref = next(r for r in self.refs() if r.startswith("ostree/container/image/"))
        self.commit = run("ostree", "rev-parse", f"--repo={self.path}", self.image_ref)
        self.origin = GLib.KeyFile.new()
        self.origin.set_string("origin", "container-image-reference", ORIGIN)
        self.deployment = OSTree.Deployment.new(0, "default", self.commit, 7, None, 0)
        self.deployment.set_origin(self.origin)
        self.prepared = self.work / "prepared"
        self.prepared.mkdir()

    def refs(self):
        return run("ostree", "refs", f"--repo={self.path}").splitlines()

    def prepare(self):
        self.source = SourceImage.read(self.repo, self.deployment)

    def target(self, origin=ORIGIN):
        target = self.work / "target"
        target.mkdir()
        run("ostree", "admin", "init-fs", "--modern", target)
        repo = target / "ostree/repo"
        run("ostree", "config", f"--repo={repo}", "set", "sysroot.bootloader", "none")
        run("ostree", "config", f"--repo={repo}", "set", "sysroot.bootprefix", "true")
        run("ostree", "config", f"--repo={repo}", "set", "sysroot.readonly", "true")
        run("ostree", "admin", "os-init", f"--sysroot={target}", "default")
        self.source.copy(target)
        (self.prepared / "origin").write_text(self.source.origin.to_data()[0])
        run("ostree", "admin", "deploy", "--no-merge", f"--sysroot={target}",
            "--os=default", f"--origin-file={self.prepared / 'origin'}", self.commit)
        run(*EXT, "prune-layers", "--repo", repo)
        run("ostree", "prune", "--refs-only", "--depth=0", f"--repo={repo}")
        run("ostree", "fsck", f"--repo={repo}")
        for ref, expected in self.source.refs.items():
            self.assertEqual(run("ostree", "rev-parse", f"--repo={repo}", ref), expected)
        self.assertFalse(any(r.startswith("ostree/container/image/") for r in
                             run("ostree", "refs", f"--repo={repo}").splitlines()))
        origins = list((target / "ostree/deploy/default/deploy").glob("*.origin"))
        self.assertEqual(len(origins), 1)
        self.assertIn(origin, origins[0].read_text())
        self.assertEqual((origins[0].with_suffix("") / "etc/audit.conf").read_text(), "factory=true\n")
        module["configure_deployment"](target, self.source, dict(root="root-uuid", boot="boot-uuid", esp="esp-uuid"), ["quiet"])
        entry = next((target / "boot/loader/entries").glob("*.conf")).read_text()
        self.assertIn("root=UUID=root-uuid", entry)
        self.assertIn("rootflags=subvol=root,", entry)
        self.assertIn("fdtdir /boot/", entry)
        self.assertIn("UUID=root-uuid /var/home", (origins[0].with_suffix("") / "etc/fstab").read_text())
        return repo

    def check_deployment_history(self, selected_index):
        sd = self.work / "sd"
        sd.mkdir()
        run("ostree", "admin", "init-fs", "--modern", sd)
        self.path = sd / "ostree/repo"
        run("ostree", "config", f"--repo={self.path}", "set", "sysroot.bootloader", "none")
        run("ostree", "admin", "os-init", f"--sysroot={sd}", "default")
        origin_file = self.work / "history.origin"
        origin_file.write_text(self.origin.to_data()[0])
        images = {}
        for version in range(5):
            ref = oci(self.work / f"history-{version}", f"image {version}")
            run(*EXT, "pull", self.path, ref)
            image_ref = next(r for r in self.refs() if r.startswith("ostree/container/image/"))
            checksum = run("ostree", "rev-parse", f"--repo={self.path}", image_ref)
            images[checksum] = ref
            run("ostree", "admin", "deploy", "--no-merge", "--retain", f"--sysroot={sd}",
                "--os=default", f"--origin-file={origin_file}", checksum)
            history = OSTree.Sysroot.new(Gio.File.new_for_path(str(sd)))
            history.load(None)
            if len(history.get_deployments()) > 2:
                run("ostree", "admin", "undeploy", f"--sysroot={sd}", "2")
            run("ostree", "refs", f"--repo={self.path}", "--delete", image_ref)
            run(*EXT, "prune-layers", "--repo", self.path)
            run("ostree", "prune", "--refs-only", "--depth=0", f"--repo={self.path}")
        sysroot = OSTree.Sysroot.new(Gio.File.new_for_path(str(sd)))
        sysroot.load(None)
        deployments = sysroot.get_deployments()
        self.assertEqual(len(deployments), 2)
        self.deployment = deployments[selected_index]
        self.commit = self.deployment.get_csum()
        self.repo = OSTree.Repo.new(Gio.File.new_for_path(str(self.path)))
        self.repo.open(None)
        with self.assertRaises(GLib.Error):
            self.repo.load_commit(next(iter(images)))
        for deployment in deployments:
            root = sd / "ostree/deploy/default/deploy" / f"{deployment.get_csum()}.{deployment.get_deployserial()}"
            (root / "etc/audit.conf").write_text("local-user-change=true\n")
        (sd / "ostree/deploy/default/var/local-user-data").write_text("do not copy")
        self.assertFalse(any(r.startswith("ostree/container/image/") for r in self.refs()))
        fresh = self.work / "fresh-repo"
        run("ostree", "init", f"--repo={fresh}", "--mode=bare")
        run(*EXT, "pull", fresh, images[self.commit])
        fresh_ref = next(r for r in run("ostree", "refs", f"--repo={fresh}").splitlines()
                         if r.startswith("ostree/container/image/"))
        fresh_commit = run("ostree", "rev-parse", f"--repo={fresh}", fresh_ref)
        fresh_repo = OSTree.Repo.new(Gio.File.new_for_path(str(fresh)))
        fresh_repo.open(None)
        selected = self.repo.load_commit(self.commit)[1].unpack()
        imported = fresh_repo.load_commit(fresh_commit)[1].unpack()
        # Import timestamps can differ; tree checksums identify filesystem content.
        self.assertEqual(selected[6:], imported[6:])
        self.prepare()
        target = self.target()
        self.assertFalse((target.parents[1] / "ostree/deploy/default/var/local-user-data").exists())
        for key in ("ostree.manifest", "ostree.manifest-digest", "ostree.container.image-config"):
            self.assertEqual(selected[0][key], imported[0][key])
        for ref, checksum in self.source.refs.items():
            fresh_checksum = fresh_commit if ref == self.commit else run("ostree", "rev-parse", f"--repo={fresh}", ref)
            self.assertEqual(self.repo.load_commit(checksum)[1].unpack()[6:],
                             fresh_repo.load_commit(fresh_checksum)[1].unpack()[6:])
        self.assertEqual(run("ostree", "cat", f"--repo={target}", self.commit, "/usr/share/audit/content"),
                         f"image {4-selected_index}")

    def test_repeated_deployments_and_pruning_match_fresh_import(self):
        self.check_deployment_history(0)

    def test_older_selected_deployment_survives_newer_image_and_pruning(self):
        self.check_deployment_history(1)

    def test_pending_image_does_not_change_selected_commit(self):
        run(*EXT, "pull", self.path, oci(self.work / "v2", "pending image"))
        pending_ref = next(r for r in self.refs() if "/v2_3A_latest" in r)
        pending = run("ostree", "rev-parse", f"--repo={self.path}", pending_ref)
        run("ostree", "refs", f"--repo={self.path}", "--force", f"--create={self.image_ref}", pending)
        before = {ref: run("ostree", "rev-parse", f"--repo={self.path}", ref) for ref in self.refs()}
        self.prepare()
        self.assertEqual(self.source.checksum, self.commit)
        target = self.target()
        self.assertEqual(run("ostree", "cat", f"--repo={target}", self.commit, "/usr/share/audit/content"), "running image")
        self.assertEqual(before, {ref: run("ostree", "rev-parse", f"--repo={self.path}", ref) for ref in self.refs()})

    @unittest.skipUnless(os.environ.get("ARMADA_TEST_MOUNTS") == "1", "requires a disposable container with CAP_SYS_ADMIN")
    def test_readonly_source_copies_and_corruption_does_not_write_markers(self):
        for damaged in (False, True):
            if damaged:
                content = next(p for p in (self.path / "objects").glob("*/*.file")
                               if p.is_file() and p.read_bytes() == b"running image")
                content.unlink()
            before = {str(p.relative_to(self.path)): (p.lstat().st_size, p.lstat().st_mtime_ns)
                      for p in self.path.rglob("*")}
            run("mount", "--bind", self.path, self.path)
            try:
                run("mount", "-o", "remount,bind,ro", self.path)
                with self.assertRaises(OSError) as error:
                    (self.path / "write-probe").write_text("must fail")
                self.assertEqual(error.exception.errno, 30)
                if damaged:
                    self.prepare()
                    target = self.work / "damaged-target"
                    target.mkdir()
                    run("ostree", "admin", "init-fs", "--modern", target)
                    with self.assertRaises(Error):
                        self.source.copy(target)
                    with self.assertRaises(Error):
                        module["run"]("ostree", "fsck", f"--repo={self.path}")
                else:
                    self.prepare()
                    self.target()
                after = {str(p.relative_to(self.path)): (p.lstat().st_size, p.lstat().st_mtime_ns)
                         for p in self.path.rglob("*")}
                self.assertEqual(before, after)
            finally:
                run("umount", self.path)

    def test_missing_image_name_is_supported(self):
        run("ostree", "refs", f"--repo={self.path}", "--delete", self.image_ref)
        self.prepare()
        self.target()

    def test_fd_backed_repository_path_is_resolved_for_children(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            repo = Mock(wraps=self.repo)
            repo.get_path.return_value = Gio.File.new_for_path(f"/proc/self/fd/{fd}")
            self.source = SourceImage.read(repo, self.deployment)
            self.target()
        finally:
            os.close(fd)

    def test_missing_layer_fails_before_prepared_source_exists(self):
        layer = next(r for r in self.refs() if r.startswith("ostree/container/blob/"))
        run("ostree", "refs", f"--repo={self.path}", "--delete", layer)
        with self.assertRaisesRegex(Error, "no supported complete container image"):
            self.prepare()
        self.assertEqual(list(self.prepared.iterdir()), [])

    def test_corrupt_object_fails_during_verified_copy(self):
        content = next(p for p in (self.path / "objects").glob("*/*.file")
                       if p.is_file() and p.read_bytes() == b"running image")
        content.chmod(0o644)
        content.write_bytes(b"damaged image")
        self.prepare()
        with self.assertRaises(Error):
            self.target()
        self.assertEqual(list(self.prepared.iterdir()), [])

    def test_origin_is_exact_selected_deployment(self):
        other = OSTree.Deployment.new(1, "default", self.commit, 8, None, 0)
        other_origin = GLib.KeyFile.new()
        other_origin.set_string("origin", "container-image-reference", ORIGIN.replace(":beta", ":testing"))
        other.set_origin(other_origin)
        self.prepare()
        self.assertEqual(self.source.origin.get_string("origin", "container-image-reference"), ORIGIN)
        self.assertNotIn(":testing", self.source.origin.to_data()[0])

    def test_missing_or_modified_origin_is_rejected(self):
        self.deployment.set_origin(None)
        with self.assertRaisesRegex(Error, "Missing update origin"):
            self.prepare()
        self.origin.set_string("packages", "requested", "example")
        self.deployment.set_origin(self.origin)
        with self.assertRaisesRegex(Error, "local package modifications"):
            self.prepare()
        self.assertEqual(list(self.prepared.iterdir()), [])

    def test_derived_commit_does_not_fall_back_to_parent(self):
        derived = run("ostree", "commit", f"--repo={self.path}", "--branch=derived",
                      f"--parent={self.commit}", f"--tree=ref={self.commit}")
        self.deployment = OSTree.Deployment.new(0, "default", derived, 0, None, 0)
        self.deployment.set_origin(self.origin)
        with self.assertRaises(Error):
            self.prepare()
        self.assertEqual(list(self.prepared.iterdir()), [])

    def test_native_image_bindings(self):
        native = run("ostree", "commit", f"--repo={self.path}", "--branch=native",
                     "--parent=none", f"--tree=ref={self.commit}")
        image = f"oci:{self.work}/native:latest"
        run("bootc", "internals", "ostree-ext", "container", "encapsulate", "--repo", self.path, native, image)
        run(*EXT, "pull", self.path, "ostree-unverified-image:" + image)
        image_ref = next(r for r in self.refs() if "/native_3A_latest" in r)
        self.commit = run("ostree", "rev-parse", f"--repo={self.path}", image_ref)
        self.deployment = OSTree.Deployment.new(0, "default", self.commit, 2, None, 0)
        self.deployment.set_origin(self.origin)
        self.prepare()
        self.target()

    def test_non_booted_system_is_rejected(self):
        with self.assertRaisesRegex(Error, "booted OSTree system"):
            with module["locked_source"]():
                self.fail("accepted a non-booted container")

    def test_normalizing_origin_does_not_modify_source_origin(self):
        unsigned = ORIGIN.replace("ostree-image-signed:docker://", "ostree-unverified-registry:")
        self.origin.set_string("origin", "container-image-reference", unsigned)
        self.deployment.set_origin(self.origin)
        self.prepare()
        self.assertEqual(self.source.origin.get_string("origin", "container-image-reference"), ORIGIN)
        self.assertEqual(self.deployment.get_origin().get_string("origin", "container-image-reference"), unsigned)

    def test_seed_copies_and_deploys_with_freedom_origin(self):
        self.source = SourceImage.from_seed(
            self.repo, self.commit, FREEDOM_ORIGIN, seed_manifest_digest(self.work / "v1"))
        self.assertEqual(self.source.checksum, self.commit)
        self.assertIsInstance(self.source.origin, GLib.KeyFile)
        self.assertEqual(self.source.origin.get_string("origin", "container-image-reference"), FREEDOM_ORIGIN)
        self.target(origin=FREEDOM_ORIGIN)

    def test_seed_accepts_supported_channel_origins_exactly(self):
        digest = seed_manifest_digest(self.work / "v1")
        for tag in ("stable", "beta", "testing"):
            origin = f"ostree-image-signed:docker://{FREEDOM}:{tag}"
            source = SourceImage.from_seed(self.repo, self.commit, origin, digest)
            self.assertEqual(source.checksum, self.commit)
            self.assertEqual(source.origin.get_string("origin", "container-image-reference"), origin)
        pinned = f"ostree-image-signed:docker://{FREEDOM}:testing@{digest}"
        source = SourceImage.from_seed(self.repo, self.commit, pinned, digest)
        self.assertEqual(source.origin.get_string("origin", "container-image-reference"), pinned)

    def test_seed_rejects_bad_commit(self):
        digest = seed_manifest_digest(self.work / "v1")
        for checksum in (None, "", "abc", "z" * 64, "0" * 64, "sha256:" + self.commit, self.commit + "0"):
            with self.assertRaises(Error):
                SourceImage.from_seed(self.repo, checksum, FREEDOM_ORIGIN, digest)

    def test_seed_rejects_bad_expected_digest(self):
        good = seed_manifest_digest(self.work / "v1")
        hexpart = good.split(":", 1)[1]
        for digest in (None, "", self.commit, "sha256:" + "z" * 64, "sha256:" + hexpart.upper(),
                       "sha256:" + "0" * 64, good[:12]):
            with self.assertRaises(Error):
                SourceImage.from_seed(self.repo, self.commit, FREEDOM_ORIGIN, digest)

    def test_seed_selects_explicit_commit_over_newer_import(self):
        run(*EXT, "pull", self.path, oci(self.work / "v2", "newer image"))
        newer_ref = next(r for r in self.refs() if "/v2_3A_latest" in r)
        newer = run("ostree", "rev-parse", f"--repo={self.path}", newer_ref)
        self.assertNotEqual(newer, self.commit)
        self.source = SourceImage.from_seed(
            self.repo, self.commit, FREEDOM_ORIGIN, seed_manifest_digest(self.work / "v1"))
        self.assertEqual(self.source.checksum, self.commit)
        target_repo = self.target(origin=FREEDOM_ORIGIN)
        self.assertEqual(run("ostree", "cat", f"--repo={target_repo}",
                             self.commit, "/usr/share/audit/content"), "running image")

    def test_seed_rejects_invalid_origins(self):
        digest = seed_manifest_digest(self.work / "v1")
        hexpart = digest.split(":", 1)[1]
        invalid = [
            None, "", " ",
            FREEDOM,
            f"docker://{FREEDOM}:testing",
            f"ostree-image-signed:docker://{FREEDOM}",
            f"ostree-image-signed:docker://{FREEDOM}@{digest}",
            f"ostree-image-signed:docker://{FREEDOM}:latest",
            f"ostree-image-signed:docker://{FREEDOM}:Testing",
            "ostree-image-signed:docker://ghcr.io/armada-os/armada:testing",
            "ostree-image-signed:docker://ghcr.io/virtudude/armada:testing",
            f"ostree-unverified-registry:{FREEDOM}:testing",
            f"ostree-unverified-image:docker://{FREEDOM}:testing",
            f"ostree-image-signed:docker://{FREEDOM}:testing@sha256:1234",
            f"ostree-image-signed:docker://{FREEDOM}:testing@{hexpart}",
            f"ostree-image-signed:docker://{FREEDOM}:testing@sha256:{hexpart.upper()}",
            f"ostree-image-signed:docker://{FREEDOM}:testing ",
            f"ostree-image-signed:docker://{FREEDOM}:testing\n",
            f"ostree-image-signed:docker://{FREEDOM}:testing?x=1",
            f"ostree-image-signed:docker://{FREEDOM}:testing/extra",
            f"ostree-image-signed:docker://{FREEDOM}:testing:extra",
            f"ostree-image-signed:docker://{FREEDOM}:testing@{digest}&x=1",
        ]
        for origin in invalid:
            with self.assertRaises(Error):
                SourceImage.from_seed(self.repo, self.commit, origin, digest)

    def test_seed_rejects_missing_layer_reference(self):
        digest = seed_manifest_digest(self.work / "v1")
        layer = next(r for r in self.refs() if r.startswith("ostree/container/blob/"))
        run("ostree", "refs", f"--repo={self.path}", "--delete", layer)
        with self.assertRaises(Error):
            SourceImage.from_seed(self.repo, self.commit, FREEDOM_ORIGIN, digest)

    def test_seed_rejects_derived_commit_without_container_metadata(self):
        digest = seed_manifest_digest(self.work / "v1")
        derived = run("ostree", "commit", f"--repo={self.path}", "--branch=derived",
                      f"--parent={self.commit}", f"--tree=ref={self.commit}")
        with self.assertRaises(Error):
            SourceImage.from_seed(self.repo, derived, FREEDOM_ORIGIN, digest)

    def test_seed_origins_are_independent(self):
        digest = seed_manifest_digest(self.work / "v1")
        beta = f"ostree-image-signed:docker://{FREEDOM}:beta"
        first = SourceImage.from_seed(self.repo, self.commit, FREEDOM_ORIGIN, digest)
        second = SourceImage.from_seed(self.repo, self.commit, beta, digest)
        self.assertIsNot(first.origin, second.origin)
        first.origin.set_string("origin", "container-image-reference",
                                "ostree-image-signed:docker://mutated.invalid/x:stable")
        self.assertEqual(second.origin.get_string("origin", "container-image-reference"), beta)
        third = SourceImage.from_seed(self.repo, self.commit, FREEDOM_ORIGIN, digest)
        self.assertEqual(third.origin.get_string("origin", "container-image-reference"), FREEDOM_ORIGIN)
        self.assertEqual(self.deployment.get_origin().get_string("origin", "container-image-reference"), ORIGIN)

    def assert_closed_fd(self, fd):
        with self.assertRaises(OSError) as closed:
            os.fstat(fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)

    @staticmethod
    def traceback_frame_names(trace):
        names = []
        while trace is not None:
            names.append(trace.tb_frame.f_code.co_name)
            trace = trace.tb_next
        return names

    def test_seed_source_releases_independent_repo_and_closes_native_fd(self):
        digest = seed_manifest_digest(self.work / "v1")
        with module["seed_source"](self.path, self.commit, FREEDOM_ORIGIN, digest) as source:
            fd = source.repo.get_dfd()
            os.fstat(fd)
            self.assertEqual(source.checksum, self.commit)
        # The yielded object is kept strongly bound so a naive implementation that only
        # drops its own local alias cannot pass; the repo must be invalidated on exit.
        self.assertIsNone(source.repo)
        self.assert_closed_fd(fd)
        # The borrowed fixture Repo is untouched: we must not close what we did not open.
        os.fstat(self.repo.get_dfd())

    def test_seed_source_validation_failure_preserves_retained_exception_chain(self):
        digest = seed_manifest_digest(self.work / "v1")
        wrong = "sha256:" + "0" * 64
        self.assertNotEqual(wrong, digest)
        observed = []
        retained = []
        real_from_seed = SourceImage.from_seed

        def observer(cls, repo, checksum, origin_ref, expected_manifest_digest):
            # Record only the raw fd and a weak handle; never retain the repo itself.
            observed.append(repo.get_dfd())
            retained.append(weakref.ref(repo))
            return real_from_seed(repo, checksum, origin_ref, expected_manifest_digest)

        error = None
        with patch.object(SourceImage, "from_seed", classmethod(observer)):
            try:
                with module["seed_source"](self.path, self.commit, FREEDOM_ORIGIN, wrong):
                    self.fail("seed_source accepted a mismatched manifest digest")
            except Error as caught:
                error = caught

        self.assertTrue(observed, "source repo was never opened for validation")
        # Check the observed fd before touching the traceback, so no incidental file IO can reuse it.
        self.assert_closed_fd(observed[0])
        os.fstat(self.repo.get_dfd())
        self.assertIsNotNone(error)
        self.assertIsInstance(error, Error)
        self.assertIn("manifest digest does not match the expected seed", str(error))
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertIsNotNone(error.__traceback__)
        names = self.traceback_frame_names(error.__traceback__)
        self.assertIn("_validated", names)
        self.assertIn("from_seed", names)
        self.assertIn("observer", names)
        self.assertIsNone(retained[0]())

    def test_seed_source_copy_failure_releases_frame_held_repo_alias(self):
        digest = seed_manifest_digest(self.work / "v1")
        error = None
        # The try must sit OUTSIDE the seed_source context: the failure has to propagate
        # across the context boundary (and its unwind) before we observe it, exactly as
        # the deploy_existing path does.
        try:
            with module["seed_source"](self.path, self.commit, FREEDOM_ORIGIN, digest) as source:
                fd = source.repo.get_dfd()
                os.fstat(fd)
                copy_globals = SourceImage.copy.__globals__
                real_run = copy_globals["run"]
                intercepted = []

                def fail_copy_cause():
                    # Hold the raw repo in a frame distinct from the top Error traceback,
                    # so clearing only the top frame cannot accidentally pass this test.
                    repo_alias = source.repo
                    self.assertEqual(repo_alias.get_dfd(), fd)
                    raise ValueError("copy-cause")

                def failing_run(*args, **kwargs):
                    if not intercepted and "pull-local" in [str(arg) for arg in args]:
                        intercepted.append(True)
                        try:
                            fail_copy_cause()
                        except ValueError as cause:
                            raise Error("copy-injected") from cause
                    return real_run(*args, **kwargs)

                with patch.dict(copy_globals, {"run": failing_run}):
                    source.copy(self.work / "copy-target")
        except Error as caught:
            error = caught

        self.assertTrue(intercepted, "the real copy path did not reach the first pull-local")
        self.assertIsNone(source.repo)
        self.assert_closed_fd(fd)
        os.fstat(self.repo.get_dfd())
        self.assertIsNotNone(error)
        self.assertIsInstance(error, Error)
        self.assertIn("copy-injected", str(error))
        self.assertIsInstance(error.__cause__, ValueError)
        self.assertIn("copy-cause", str(error.__cause__))
        self.assertIsNotNone(error.__traceback__)
        self.assertIn("failing_run", self.traceback_frame_names(error.__traceback__))
        self.assertNotIn("fail_copy_cause", self.traceback_frame_names(error.__traceback__))
        # The cause must still hold its own unwound frame (and hence the raw repo alias)
        # after the fd has been closed by the context unwind.
        self.assertIsNotNone(error.__cause__.__traceback__)
        self.assertIn("fail_copy_cause", self.traceback_frame_names(error.__cause__.__traceback__))

    def boot_asset(self, target, value):
        text = str(value)
        relative = text[len("/boot/"):] if text.startswith("/boot/") else text.lstrip("/")
        return target / "boot" / relative

    def test_seed_kargs_prefers_source_architecture_over_host(self):
        self.prepare()
        with patch.object(platform, "machine", return_value="x86_64"):
            kargs = module["seed_kargs"](self.source, self.work / "seed-kargs")
        self.assertIn("source-only-flag", kargs)
        self.assertIn("quiet", kargs)
        self.assertFalse(any(k.startswith(("root=", "boot=", "rootflags=", "ostree=")) for k in kargs))

    def test_deploy_tree_kshimc_needs_only_boot_and_root(self):
        self.source = SourceImage.from_seed(self.repo, self.commit, FREEDOM_ORIGIN,
                                            seed_manifest_digest(self.work / "v1"))
        target = self.work / "tree-target"
        target.mkdir()
        run("ostree", "admin", "init-fs", "--modern", target)
        # With real mounts target/var and target/var/home are separate btrfs subvolumes.
        (target / "var").mkdir(exist_ok=True)
        (target / "var/home").mkdir(exist_ok=True)
        job = module["Installation"](None)
        job.work = self.work / "tree-work"
        job.work.mkdir()
        observer = []
        globals_map = module["Installation"].deploy_tree.__globals__
        real_run = globals_map["run"]

        def spy(*args, **kwargs):
            args = [str(arg) for arg in args]
            observer.append(args)
            if args[0] == str(module["BOOTIMG"]):
                raise AssertionError("boot image update must not run for kshimc")
            return real_run(*args, **kwargs)

        with patch.dict(globals_map, {"run": spy}):
            result = job.deploy_tree(target, self.source, {"boot": "boot-uuid", "root": "root-uuid"},
                                     ["quiet"], boot_method="kshimc")
        self.assertEqual(result["deployment_checksum"], self.commit)
        self.assertEqual(result["filesystem_uuids"], {"boot": "boot-uuid", "root": "root-uuid"})
        origin_file = next((target / "ostree/deploy/default/deploy").glob("*.origin"))
        self.assertIn(FREEDOM_ORIGIN, origin_file.read_text())
        self.assertTrue((target / "var/lib/audit/seeded").is_file())
        deploy = origin_file.with_suffix("")
        self.assertEqual((deploy / "etc/armada/boot-method").read_text(), "kshimc\n")
        for name in ("etc/systemd/system/armada-bootimg-sync.service",
                     "etc/systemd/system/armada-esp-rename.service",
                     "etc/systemd/system/ostree-finalize-staged.service.d/10-armada-bootimg.conf"):
            link = deploy / name
            self.assertTrue(link.is_symlink(), name)
            self.assertEqual(os.readlink(link), "/dev/null")
        self.assertFalse((deploy / "etc/systemd/system/ostree-finalize-staged.service").is_symlink())
        fstab = (deploy / "etc/fstab").read_text()
        self.assertIn("UUID=boot-uuid /boot ", fstab)
        self.assertIn("UUID=root-uuid /var ", fstab)
        self.assertNotIn("/boot/efi", fstab)
        self.assertFalse((target / "boot/efi").exists())
        entries = list((target / "boot/loader/entries").glob("*.conf"))
        self.assertEqual(len(entries), 1)
        fields = dict(line.split(None, 1) for line in entries[0].read_text().splitlines() if line.strip())
        self.assertIn("root=UUID=root-uuid", fields["options"])
        self.assertIn("boot=UUID=boot-uuid", fields["options"])
        self.assertIn("ostree=", fields["options"])
        # result images are boot-relative while the on-disk entry uses a /boot/ prefix.
        self.assertFalse(Path(result["image"]).is_absolute())
        self.assertEqual((target / "boot" / result["image"]).resolve(),
                         self.boot_asset(target, fields["linux"]).resolve())
        boot_root = (target / "boot").resolve()
        for key in ("bls", "image", "initrd", "fdtdir"):
            value = Path(result[key])
            self.assertFalse(value.is_absolute(), key)
            self.assertNotIn("..", value.parts, key)
            node = (target / "boot" / value).resolve()
            self.assertTrue(node.exists(), key)
            self.assertTrue(str(node).startswith(str(boot_root) + os.sep), key)
        for command in observer:
            self.assertNotIn(command[0], ("sfdisk", "dd", "mkfs.btrfs", "mkfs.ext4", "systemd-inhibit"), command)
            self.assertNotEqual(command[0], str(module["BOOTIMG"]))

    def test_configure_kshimc_rejects_escaping_or_missing_boot_assets(self):
        self.prepare()
        self.target()
        target = self.work / "target"
        entry = next((target / "boot/loader/entries").glob("*.conf"))
        good = entry.read_text()
        uuids = {"boot": "boot-uuid", "root": "root-uuid"}
        module["configure_deployment"](target, self.source, uuids, ["quiet"], boot_method="kshimc")
        entry.write_text(good)
        for bad in ("/boot/../../etc/hostname", "/boot/ostree/absent/vmlinuz"):
            with self.subTest(asset=bad):
                entry.write_text(re.sub(r"(?m)^linux .*$", "linux " + bad, good))
                with self.assertRaises(Error):
                    module["configure_deployment"](target, self.source, uuids, ["quiet"], boot_method="kshimc")


if __name__ == "__main__":
    if Path("/run/ostree-booted").exists():
        sys.exit("Run these tests inside a disposable container, not a booted OSTree host")
    unittest.main()
