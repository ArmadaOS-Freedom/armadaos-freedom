import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_FILES = REPO_ROOT / "build_files"


def _load_repo_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, BUILD_FILES / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


boot_req = _load_repo_module("update_contents_requirements", "edl_boot_requirements.py")
compose_ostree = _load_repo_module("update_contents_producer", "make-edl-raw-package.py")

DROPIN_REL = "usr/lib/systemd/system/ostree-prepare-root.service.d/armada-fallback.conf"
DROPIN_BYTES = (
    b"[Service]\n"
    b"ExecStartPre=/usr/libexec/armada/armada-ostree-fallback\n"
    b"ExecStartPost=/usr/libexec/armada/armada-ostree-fallback --restore\n"
)
BOUND_REL = "usr/libexec/armada/armada-bound-deployment"
FALLBACK_REL = "usr/libexec/armada/armada-ostree-fallback"
BOUND_BYTES = b"#!/bin/sh\necho bound-v1\n"
FALLBACK_BYTES = b"#!/bin/sh\necho fallback-v1\n"


def _write_file(root, rel, data, mode):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _populate_valid_tree(root, bound=BOUND_BYTES, fallback=FALLBACK_BYTES, dropin=DROPIN_BYTES):
    _write_file(root, BOUND_REL, bound, 0o755)
    _write_file(root, FALLBACK_REL, fallback, 0o755)
    _write_file(root, DROPIN_REL, dropin, 0o644)


def _expected_map(bound=BOUND_BYTES, fallback=FALLBACK_BYTES, dropin=DROPIN_BYTES):
    return {
        BOUND_REL: (bound, True),
        FALLBACK_REL: (fallback, True),
        DROPIN_REL: (dropin, False),
    }


def _build_cpio(tree_root, out_image):
    rel_paths = ["."]
    for p in sorted(Path(tree_root).rglob("*")):
        rel_paths.append(p.relative_to(tree_root).as_posix())
    payload = b"\0".join(os.fsencode(p) for p in rel_paths) + b"\0"
    res = subprocess.run(
        ["cpio", "-o", "-H", "newc", "--null"],
        cwd=tree_root,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    Path(out_image).write_bytes(res.stdout)


class InitramfsContentsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("cpio"), "cpio executable is required")
        self.assertIsNotNone(shutil.which("lsinitrd"), "lsinitrd executable is required")

    def test_verify_initramfs_expected_files_matrix(self):
        req = {"synthetic": {}}
        kver = "6.1.0-test"

        def run_case(mutate_tree=None, mutate_expected=None, omit_expected=False):
            with tempfile.TemporaryDirectory() as outer:
                outer_path = Path(outer)
                tree = outer_path / "tree"
                tree.mkdir()
                _populate_valid_tree(tree)
                expected = _expected_map()
                if mutate_tree:
                    mutate_tree(tree, outer_path)
                if mutate_expected:
                    mutate_expected(expected)
                image = outer_path / "image.img"
                _build_cpio(tree, image)
                work_parent = outer_path / "work"
                work_parent.mkdir()
                try:
                    if omit_expected:
                        boot_req.verify_initramfs(image, kver, req, work_parent=work_parent)
                    else:
                        boot_req.verify_initramfs(
                            image, kver, req, work_parent=work_parent, expected_files=expected
                        )
                finally:
                    self.assertEqual(list(work_parent.iterdir()), [])

        with self.subTest("valid_exact_and_default_omitted"):
            run_case()
            run_case(
                mutate_tree=lambda t, _: _write_file(t, BOUND_REL, b"stale", 0o644),
                omit_expected=True,
            )

        failure_cases = [
            (
                "altered_script_bytes",
                lambda t, _: _write_file(t, FALLBACK_REL, b"#!/bin/sh\necho wrong\n", 0o755),
                None,
                "stale content",
            ),
            (
                "altered_dropin_bytes",
                lambda t, _: _write_file(t, DROPIN_REL, b"[Service]\n", 0o644),
                None,
                "stale content",
            ),
            (
                "missing_expected_file",
                lambda t, _: (t / FALLBACK_REL).unlink(),
                None,
                "missing or escapes",
            ),
            (
                "non_executable_script",
                lambda t, _: os.chmod(t / BOUND_REL, 0o644),
                None,
                "not executable",
            ),
            (
                "symlink_escape",
                lambda t, out: (
                    (out / "outside.sh").write_bytes(BOUND_BYTES),
                    os.chmod(out / "outside.sh", 0o755),
                    (t / BOUND_REL).unlink(),
                    (t / BOUND_REL).symlink_to(out / "outside.sh"),
                ),
                None,
                "missing or escapes",
            ),
            (
                "directory_instead_of_regular_file",
                lambda t, _: ((t / DROPIN_REL).unlink(), (t / DROPIN_REL).mkdir()),
                None,
                "not regular",
            ),
            (
                "invalid_relative_path",
                None,
                lambda exp: exp.__setitem__("../escape", (b"x", False)),
                "invalid expected initramfs path",
            ),
        ]

        for label, m_tree, m_exp, msg in failure_cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, msg):
                    run_case(mutate_tree=m_tree, mutate_expected=m_exp)

    def test_verify_model_boot_requirements_producer_plumbing(self):
        kver = "6.1.0-test"
        src_rel = "system_files/usr/lib/dracut/modules.d/91armada-ostree-fallback"

        def run_producer(bound_src=BOUND_BYTES, fallback_src=FALLBACK_BYTES, mutate_src=None, mutate_tree=None):
            with tempfile.TemporaryDirectory() as outer:
                outer_path = Path(outer)
                source = outer_path / "source"
                _write_file(source, f"{src_rel}/armada-bound-deployment.sh", bound_src, 0o755)
                _write_file(source, f"{src_rel}/armada-ostree-fallback.sh", fallback_src, 0o755)
                if mutate_src:
                    mutate_src(source)
                deployment = outer_path / "deployment"
                (deployment / "usr/lib/modules" / kver).mkdir(parents=True)
                tree = outer_path / "tree"
                tree.mkdir()
                _populate_valid_tree(tree)
                if mutate_tree:
                    mutate_tree(tree)
                initrd = outer_path / "initrd.img"
                _build_cpio(tree, initrd)
                work = outer_path / "work"
                work.mkdir()
                proxy = types.SimpleNamespace(
                    load_requirements=mock.Mock(return_value={"synthetic": {}}),
                    verify_tree=boot_req.verify_tree,
                    verify_initramfs=boot_req.verify_initramfs,
                )
                with mock.patch.object(compose_ostree, "_load_module", return_value=proxy):
                    try:
                        compose_ostree._verify_model_boot_requirements(
                            source, deployment, initrd, outer_path / "cfg.json", ["m"], work
                        )
                    finally:
                        self.assertEqual(list(work.iterdir()), [])

        with self.subTest("valid_producer"):
            run_producer()

        with self.subTest("source_archive_mismatch"):
            with self.assertRaisesRegex(ValueError, "stale content"):
                run_producer(bound_src=b"#!/bin/sh\necho mismatched-source\n")

        with self.subTest("missing_source_script"):
            with self.assertRaisesRegex(ValueError, "source file is missing"):
                run_producer(
                    mutate_src=lambda s: (s / f"{src_rel}/armada-ostree-fallback.sh").unlink()
                )

        with self.subTest("corrupted_dropin_in_archive"):
            with self.assertRaisesRegex(ValueError, "stale content"):
                run_producer(
                    mutate_tree=lambda t: _write_file(t, DROPIN_REL, b"[Service]\nBad=1\n", 0o644)
                )


if __name__ == "__main__":
    unittest.main()
