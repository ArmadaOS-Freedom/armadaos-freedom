import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = (
    REPO_ROOT
    / "system_files"
    / "usr"
    / "lib"
    / "dracut"
    / "modules.d"
    / "91armada-ostree-fallback"
)
SELECTOR_SCRIPT = MODULE_DIR / "armada-bound-deployment.sh"
FALLBACK_SCRIPT = MODULE_DIR / "armada-ostree-fallback.sh"

OSNAME = "default"
BOOTCSUM = "1" * 64
OTHER_BOOTCSUM = "2" * 64
OLD_COMMIT = "a" * 64
NEW_COMMIT = "b" * 64


def snapshot_tree(root: Path) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        rel_dir = os.path.relpath(dirpath, root)
        cur = Path(dirpath)
        if cur.is_symlink():
            entries.append((rel_dir, "L", os.readlink(cur)))
            continue
        entries.append((rel_dir, "D", ""))
        for name in dirnames:
            child = cur / name
            if child.is_symlink():
                rel_child = os.path.relpath(child, root)
                entries.append((rel_child, "L", os.readlink(child)))
        for name in filenames:
            child = cur / name
            rel_child = os.path.relpath(child, root)
            if child.is_symlink():
                entries.append((rel_child, "L", os.readlink(child)))
            else:
                digest = hashlib.sha256(child.read_bytes()).hexdigest()
                entries.append((rel_child, "F", digest))
    return sorted(entries)


def create_sysroot(
    base: Path,
    *,
    create_old: bool = True,
    create_new: bool = True,
    boot0_links: dict[str, str | Path] | None = None,
    boot1_links: dict[str, str | Path] | None = None,
    boot0_csum: str = BOOTCSUM,
    boot1_csum: str = BOOTCSUM,
) -> tuple[Path, Path, Path]:
    sysroot = base / "sysroot"
    deploy_dir = sysroot / "ostree" / "deploy" / OSNAME / "deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)

    old_deploy = deploy_dir / f"{OLD_COMMIT}.0"
    new_deploy = deploy_dir / f"{NEW_COMMIT}.0"
    if create_old:
        old_deploy.mkdir(parents=True, exist_ok=True)
        (old_deploy / ".commit").write_text(OLD_COMMIT, encoding="utf-8")
    if create_new:
        new_deploy.mkdir(parents=True, exist_ok=True)
        (new_deploy / ".commit").write_text(NEW_COMMIT, encoding="utf-8")

    for boot_ver, links, csum in (
        ("0", boot0_links, boot0_csum),
        ("1", boot1_links, boot1_csum),
    ):
        if links is None:
            continue
        sub = sysroot / "ostree" / f"boot.{boot_ver}.0"
        idx_dir = sub / OSNAME / csum
        idx_dir.mkdir(parents=True, exist_ok=True)
        boot_link = sysroot / "ostree" / f"boot.{boot_ver}"
        boot_link.symlink_to(f"boot.{boot_ver}.0")
        for idx, target in links.items():
            link_path = idx_dir / str(idx)
            if isinstance(target, Path):
                rel_target = os.path.relpath(target, idx_dir)
                link_path.symlink_to(rel_target)
            else:
                link_path.symlink_to(target)

    return sysroot, old_deploy, new_deploy


class SelectorAndFallbackTests(unittest.TestCase):
    def run_selector(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SELECTOR_SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def assert_selector_ok(self, sysroot: Path, cmdline: str, expected: str) -> None:
        before = snapshot_tree(sysroot)
        res = self.run_selector(cmdline, str(sysroot))
        after = snapshot_tree(sysroot)
        self.assertEqual(before, after, "selector must not mutate sysroot")
        self.assertEqual(res.returncode, 0, f"unexpected stderr: {res.stderr}")
        self.assertEqual(res.stdout, f"{expected}\n")
        self.assertEqual(res.stderr, "")

    def assert_selector_fails(self, sysroot: Path, cmdline: str) -> None:
        before = snapshot_tree(sysroot)
        res = self.run_selector(cmdline, str(sysroot))
        after = snapshot_tree(sysroot)
        self.assertEqual(before, after, "selector must not mutate sysroot")
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")
        self.assertTrue(res.stderr.strip(), "expected non-empty stderr on failure")

    def test_selector_valid_resolutions_and_no_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # 1) Original bootlink directly valid
            s1, old1, new1 = create_sysroot(
                base / "case1",
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            cmd1 = (
                f"quiet ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            self.assert_selector_ok(s1, cmd1, f"/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0")

            # 2) Old bootversion gone, new bootversion same index matches
            s2, old2, new2 = create_sysroot(
                base / "case2",
                boot0_links=None,
                boot1_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            self.assert_selector_ok(s2, cmd1, f"/ostree/boot.1/{OSNAME}/{BOOTCSUM}/0")

            # 3) Existing pointer at index 0 points to NEW deployment, index 1 points to bound OLD
            s3, old3, new3 = create_sysroot(
                base / "case3",
                boot1_links={
                    "0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0",
                    "1": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0",
                },
            )
            cmd3 = (
                f"ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            self.assert_selector_ok(s3, cmd3, f"/ostree/boot.1/{OSNAME}/{BOOTCSUM}/1")

            # 4) Lookalike kernel args ignored
            cmd4 = (
                f"my.ostree=/bad foo_armada.deployment=bad armada.deployment_extra=1 "
                f"ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            self.assert_selector_ok(s3, cmd4, f"/ostree/boot.1/{OSNAME}/{BOOTCSUM}/1")

    def test_selector_missing_or_mismatched_targets_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Nonexistent bound deployment
            s1, _, _ = create_sysroot(
                base / "missing_bound",
                create_old=False,
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0"},
            )
            cmd = (
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            self.assert_selector_fails(s1, cmd)

            # Bound deployment exists, but all bootlinks point to wrong commit (no fallback allowed)
            s2, _, _ = create_sysroot(
                base / "only_wrong_commit",
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0"},
                boot1_links={"0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0"},
            )
            self.assert_selector_fails(s2, cmd)

            # Correct target deployment, but only under wrong bootcsum
            s3, _, _ = create_sysroot(
                base / "wrong_bootcsum",
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
                boot0_csum=OTHER_BOOTCSUM,
            )
            self.assert_selector_fails(s3, cmd)

            # Boot index entry is a direct directory instead of a symlink
            s4, _, _ = create_sysroot(base / "direct_dir", boot0_links={})
            direct_idx = s4 / "ostree" / "boot.0.0" / OSNAME / BOOTCSUM / "0"
            direct_idx.mkdir(parents=True, exist_ok=True)
            self.assert_selector_fails(s4, cmd)

            # Non-numeric index symlink pointing to bound deployment must be ignored
            s5, _, _ = create_sysroot(
                base / "non_numeric_idx",
                boot0_links={"01": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            self.assert_selector_fails(s5, cmd)

    def test_selector_canonical_sysroot_and_escape_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside_deploy"
            outside.mkdir(parents=True)

            # Bound deployment itself is a symlink alias
            s1, old1, _ = create_sysroot(base / "alias_deploy", create_old=False)
            real_dir = s1 / "ostree" / "deploy" / OSNAME / "deploy" / f"{NEW_COMMIT}.0"
            old1.symlink_to(real_dir)
            idx_dir = s1 / "ostree" / "boot.0.0" / OSNAME / BOOTCSUM
            idx_dir.mkdir(parents=True)
            (s1 / "ostree" / "boot.0").symlink_to("boot.0.0")
            (idx_dir / "0").symlink_to(old1)
            cmd = (
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            self.assert_selector_fails(s1, cmd)

            # Bound deployment symlink escaping canonical sysroot
            s2, old2, _ = create_sysroot(base / "escape_deploy", create_old=False)
            old2.symlink_to(outside)
            idx_dir2 = s2 / "ostree" / "boot.0.0" / OSNAME / BOOTCSUM
            idx_dir2.mkdir(parents=True)
            (s2 / "ostree" / "boot.0").symlink_to("boot.0.0")
            (idx_dir2 / "0").symlink_to(old2)
            self.assert_selector_fails(s2, cmd)

            # Parent directory under ostree/deploy/<osname> escaping canonical sysroot
            s3 = base / "escape_parent" / "sysroot"
            os_parent = s3 / "ostree" / "deploy" / OSNAME
            os_parent.mkdir(parents=True)
            outside_deploy_root = base / "outside_deploy_root"
            (outside_deploy_root / f"{OLD_COMMIT}.0").mkdir(parents=True)
            (os_parent / "deploy").symlink_to(outside_deploy_root)
            idx_dir3 = s3 / "ostree" / "boot.0.0" / OSNAME / BOOTCSUM
            idx_dir3.mkdir(parents=True)
            (s3 / "ostree" / "boot.0").symlink_to("boot.0.0")
            (idx_dir3 / "0").symlink_to(outside_deploy_root / f"{OLD_COMMIT}.0")
            self.assert_selector_fails(s3, cmd)

    def test_selector_argument_and_cmdline_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sysroot, _, _ = create_sysroot(
                base / "valid",
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            valid_ostree = f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0"
            valid_bind = f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"

            # Invocation arg errors
            for bad_args in (
                [],
                [f"{valid_ostree} {valid_bind}"],
                [f"{valid_ostree} {valid_bind}", "relative/sysroot"],
                [f"{valid_ostree} {valid_bind}", str(base / "no_such_dir")],
            ):
                with self.subTest(bad_args=bad_args):
                    res = self.run_selector(*bad_args)
                    self.assertNotEqual(res.returncode, 0)
                    self.assertEqual(res.stdout, "")
                    self.assertTrue(res.stderr.strip())

            invalid_cmdlines = [
                # Missing binding or ostree
                valid_ostree,
                valid_bind,
                # Bare tokens
                f"armada.deployment {valid_ostree} {valid_bind}",
                f"ostree {valid_ostree} {valid_bind}",
                # Duplicates
                f"{valid_ostree} {valid_bind} armada.deployment={OSNAME}/{NEW_COMMIT}.0",
                f"{valid_ostree} ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/0 {valid_bind}",
                # Traversal / invalid osname / bad commit / bad serial
                f"{valid_ostree} armada.deployment=../{OLD_COMMIT}.0",
                f"{valid_ostree} armada.deployment=.hidden/{OLD_COMMIT}.0",
                f"{valid_ostree} armada.deployment={OSNAME}/{'A' * 64}.0",
                f"{valid_ostree} armada.deployment={OSNAME}/{'a' * 63}.0",
                f"{valid_ostree} armada.deployment={OSNAME}/{OLD_COMMIT}.01",
                f"{valid_ostree} armada.deployment={OSNAME}/{OLD_COMMIT}.-1",
                # Bad ostree path / osname mismatch / bad bootcsum / bad boot index
                f"ostree=/ostree/boot.2/{OSNAME}/{BOOTCSUM}/0 {valid_bind}",
                f"ostree=/ostree/boot.0/otheros/{BOOTCSUM}/0 {valid_bind}",
                f"ostree=/ostree/boot.0/{OSNAME}/{'F' * 64}/0 {valid_bind}",
                f"ostree=/ostree/boot.0/{OSNAME}/{'1' * 63}/0 {valid_bind}",
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/01 {valid_bind}",
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/1/extra {valid_bind}",
            ]
            for cmdline in invalid_cmdlines:
                with self.subTest(cmdline=cmdline):
                    self.assert_selector_fails(sysroot, cmdline)

    def prepare_fallback_env(
        self,
        base: Path,
        sysroot: Path,
        cmdline: str,
        *,
        include_helper: bool = True,
    ) -> dict[str, Path]:
        base.mkdir(parents=True, exist_ok=True)
        proc_cmdline = base / "proc_cmdline"
        proc_cmdline.write_text(f"{cmdline}\n", encoding="utf-8")
        run_armada = base / "run_armada"
        kmsg = base / "kmsg"
        kmsg.write_text("", encoding="utf-8")
        mount_log = base / "mount.log"
        mount_log.write_text("", encoding="utf-8")
        captured_bind = base / "captured_bind_cmdline"

        helper_path = base / "libexec" / "armada-bound-deployment"
        helper_path.parent.mkdir(parents=True, exist_ok=True)
        if include_helper:
            shutil.copy2(SELECTOR_SCRIPT, helper_path)
            helper_path.chmod(0o755)

        bin_dir = base / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        mock_mount = bin_dir / "mount"
        mock_mount.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            'echo "mount $*" >> "${MOCK_MOUNT_LOG}"\n'
            'if [ "${1:-}" = "--bind" ]; then\n'
            '    [ "${MOCK_BIND_FAIL:-0}" = "1" ] && exit 1\n'
            '    cp -- "$2" "${MOCK_CAPTURED_BIND}"\n'
            '    cp -- "$2" "$3"\n'
            "    exit 0\n"
            "fi\n"
            'if [ "${1:-}" = "-o" ] && [ "${2:-}" = "remount,bind,ro" ]; then\n'
            '    [ "${MOCK_REMOUNT_FAIL:-0}" = "1" ] && exit 1\n'
            "    exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        mock_mount.chmod(0o755)

        mock_umount = bin_dir / "umount"
        mock_umount.write_text(
            "#!/bin/bash\n"
            "set -u\n"
            'echo "umount $*" >> "${MOCK_MOUNT_LOG}"\n'
            'if [ "${MOCK_UMOUNT_FAIL:-0}" = "1" ]; then\n'
            "    exit 1\n"
            "fi\n"
            'if [ -n "${MOCK_ORIG_CMDLINE:-}" ] && [ -f "${MOCK_ORIG_CMDLINE}" ]; then\n'
            '    cp -- "${MOCK_ORIG_CMDLINE}" "$1"\n'
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        mock_umount.chmod(0o755)

        orig_cmdline = base / "orig_proc_cmdline"
        orig_cmdline.write_text(f"{cmdline}\n", encoding="utf-8")

        fallback_copy = base / "armada-ostree-fallback.sh"
        text = FALLBACK_SCRIPT.read_text(encoding="utf-8")
        text = text.replace("/usr/libexec/armada/armada-bound-deployment", str(helper_path))
        text = text.replace("/run/armada", str(run_armada))
        text = text.replace("/proc/cmdline", str(proc_cmdline))
        text = text.replace("/dev/kmsg", str(kmsg))
        text = text.replace("/sysroot", str(sysroot))
        fallback_copy.write_text(text, encoding="utf-8")
        fallback_copy.chmod(0o755)

        return {
            "script": fallback_copy,
            "proc_cmdline": proc_cmdline,
            "orig_cmdline": orig_cmdline,
            "run_armada": run_armada,
            "marker": run_armada / "ostree-fallback-remapped",
            "staged_alias": run_armada / "cmdline",
            "kmsg": kmsg,
            "mount_log": mount_log,
            "captured_bind": captured_bind,
            "bin_dir": bin_dir,
        }

    def run_fallback(
        self,
        env_paths: dict[str, Path],
        *args: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = f"{env_paths['bin_dir']}:{env.get('PATH', '')}"
        env["MOCK_MOUNT_LOG"] = str(env_paths["mount_log"])
        env["MOCK_CAPTURED_BIND"] = str(env_paths["captured_bind"])
        env["MOCK_ORIG_CMDLINE"] = str(env_paths["orig_cmdline"])
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(env_paths["script"]), *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=30,
        )

    def test_fallback_bound_remap_and_healthy_noop_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Case 1: Bound deployment with wrong existing index 0 -> remaps to index 1 and preserves literal *
            s1, _, _ = create_sysroot(
                base / "remap",
                boot1_links={
                    "0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0",
                    "1": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0",
                },
            )
            cmdline = (
                f"quiet glob=* ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0 console=ttyS0"
            )
            env1 = self.prepare_fallback_env(base / "env1", s1, cmdline)
            res1 = self.run_fallback(env1)
            self.assertEqual(res1.returncode, 0, res1.stderr)
            expected_cmdline = (
                f"quiet glob=* ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/1 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0 console=ttyS0\n"
            )
            self.assertEqual(env1["captured_bind"].read_text(encoding="utf-8"), expected_cmdline)
            self.assertEqual(env1["proc_cmdline"].read_text(encoding="utf-8"), expected_cmdline)
            self.assertEqual(
                env1["marker"].read_text(encoding="utf-8"),
                f"/ostree/boot.1/{OSNAME}/{BOOTCSUM}/1\n",
            )
            self.assertFalse(env1["staged_alias"].exists(), "writable staged alias must be removed")
            log_lines = env1["mount_log"].read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                log_lines,
                [
                    f"mount --bind {env1['staged_alias']} {env1['proc_cmdline']}",
                    f"mount -o remount,bind,ro {env1['proc_cmdline']}",
                ],
            )

            # --restore unmounts /proc/cmdline when marker exists
            res_restore = self.run_fallback(env1, "--restore")
            self.assertEqual(res_restore.returncode, 0)
            self.assertIn(
                f"umount {env1['proc_cmdline']}",
                env1["mount_log"].read_text(encoding="utf-8").splitlines(),
            )

            # Case 2: Healthy bound bootlink needs no mount
            healthy_cmd = (
                f"quiet ostree=/ostree/boot.1/{OSNAME}/{BOOTCSUM}/1 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            env2 = self.prepare_fallback_env(base / "env2", s1, healthy_cmd)
            res2 = self.run_fallback(env2)
            self.assertEqual(res2.returncode, 0)
            self.assertEqual(env2["mount_log"].read_text(encoding="utf-8"), "")
            self.assertFalse(env2["marker"].exists())
            self.assertFalse(env2["staged_alias"].exists())

    def test_fallback_bound_failure_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # 1) Missing bound target fails nonzero even when existing bootlink points to another valid deployment
            s_wrong, _, _ = create_sysroot(
                base / "wrong_only",
                create_old=False,
                boot0_links={"0": f"../../../deploy/{OSNAME}/deploy/{NEW_COMMIT}.0"},
            )
            cmd_wrong = (
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )
            env_wrong = self.prepare_fallback_env(base / "env_wrong", s_wrong, cmd_wrong)
            res_wrong = self.run_fallback(env_wrong)
            self.assertNotEqual(res_wrong.returncode, 0)
            self.assertEqual(env_wrong["mount_log"].read_text(encoding="utf-8"), "")
            self.assertFalse(env_wrong["marker"].exists())

            # Setup sysroot needing remap for remaining failure injection cases
            s_remap, _, _ = create_sysroot(
                base / "remap_sysroot",
                boot0_links=None,
                boot1_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            cmd_remap = (
                f"ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0 "
                f"armada.deployment={OSNAME}/{OLD_COMMIT}.0"
            )

            # 2) Missing helper binary -> nonzero
            env_no_helper = self.prepare_fallback_env(
                base / "env_no_helper", s_remap, cmd_remap, include_helper=False
            )
            res_no_helper = self.run_fallback(env_no_helper)
            self.assertNotEqual(res_no_helper.returncode, 0)
            self.assertEqual(env_no_helper["mount_log"].read_text(encoding="utf-8"), "")

            # 3) Marker write fails (marker path pre-created as directory) -> nonzero & staged alias cleaned
            env_marker_dir = self.prepare_fallback_env(base / "env_marker_dir", s_remap, cmd_remap)
            env_marker_dir["marker"].mkdir(parents=True)
            res_marker_dir = self.run_fallback(env_marker_dir)
            self.assertNotEqual(res_marker_dir.returncode, 0)
            self.assertFalse(env_marker_dir["staged_alias"].exists())
            self.assertEqual(env_marker_dir["mount_log"].read_text(encoding="utf-8"), "")

            # 4) mount --bind fails -> nonzero, marker and staged alias cleaned
            env_bind_fail = self.prepare_fallback_env(base / "env_bind_fail", s_remap, cmd_remap)
            res_bind_fail = self.run_fallback(env_bind_fail, extra_env={"MOCK_BIND_FAIL": "1"})
            self.assertNotEqual(res_bind_fail.returncode, 0)
            self.assertFalse(env_bind_fail["marker"].exists())
            self.assertFalse(env_bind_fail["staged_alias"].exists())

            # 5) ro remount fails, umount succeeds -> nonzero, umount called, marker & alias cleaned
            env_remount_fail = self.prepare_fallback_env(base / "env_remount_fail", s_remap, cmd_remap)
            res_remount_fail = self.run_fallback(
                env_remount_fail, extra_env={"MOCK_REMOUNT_FAIL": "1"}
            )
            self.assertNotEqual(res_remount_fail.returncode, 0)
            self.assertFalse(env_remount_fail["marker"].exists())
            self.assertFalse(env_remount_fail["staged_alias"].exists())
            self.assertEqual(
                env_remount_fail["mount_log"].read_text(encoding="utf-8").splitlines(),
                [
                    f"mount --bind {env_remount_fail['staged_alias']} {env_remount_fail['proc_cmdline']}",
                    f"mount -o remount,bind,ro {env_remount_fail['proc_cmdline']}",
                    f"umount {env_remount_fail['proc_cmdline']}",
                ],
            )

            # 6) ro remount fails AND umount fails -> nonzero, marker retained for --restore retry, alias removed
            env_both_fail = self.prepare_fallback_env(base / "env_both_fail", s_remap, cmd_remap)
            res_both_fail = self.run_fallback(
                env_both_fail,
                extra_env={"MOCK_REMOUNT_FAIL": "1", "MOCK_UMOUNT_FAIL": "1"},
            )
            self.assertNotEqual(res_both_fail.returncode, 0)
            self.assertTrue(
                env_both_fail["marker"].is_file(),
                "marker must be retained when umount fails so --restore can retry",
            )
            self.assertFalse(env_both_fail["staged_alias"].exists())

    def test_fallback_legacy_unbound_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Unbound unfixable (no surviving bootlink) still returns 0
            s_empty, _, _ = create_sysroot(base / "empty", boot0_links=None, boot1_links=None)
            cmd_unbound = f"quiet ostree=/ostree/boot.0/{OSNAME}/{BOOTCSUM}/0"
            env_unfixable = self.prepare_fallback_env(base / "env_unfixable", s_empty, cmd_unbound)
            res_unfixable = self.run_fallback(env_unfixable)
            self.assertEqual(res_unfixable.returncode, 0)

            # Unbound remap where mount --bind fails still returns 0 and cleans up
            s_legacy, _, _ = create_sysroot(
                base / "legacy",
                boot0_links=None,
                boot1_links={"0": f"../../../deploy/{OSNAME}/deploy/{OLD_COMMIT}.0"},
            )
            env_legacy_fail = self.prepare_fallback_env(base / "env_legacy_fail", s_legacy, cmd_unbound)
            res_legacy_fail = self.run_fallback(
                env_legacy_fail, extra_env={"MOCK_BIND_FAIL": "1"}
            )
            self.assertEqual(res_legacy_fail.returncode, 0)
            self.assertFalse(env_legacy_fail["marker"].exists())
            self.assertFalse(env_legacy_fail["staged_alias"].exists())


if __name__ == "__main__":
    unittest.main()
