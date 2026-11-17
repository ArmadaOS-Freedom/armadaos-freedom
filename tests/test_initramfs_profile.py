import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "build_files" / "55-generate-initramfs.sh"

OMITTED_MODULES = [
    "network-manager",
    "network",
    "net-lib",
    "kernel-network-modules",
    "nfs",
    "cifs",
    "qemu",
    "qemu-net",
    "virtiofs",
    "lvm",
    "mdraid",
    "nvdimm",
    "fips",
    "fips-crypto-policies",
    "clevis",
    "clevis-pin-file",
    "clevis-pin-null",
    "clevis-pin-sss",
    "clevis-pin-tang",
    "clevis-pin-tpm2",
    "fido2",
    "pkcs11",
    "tpm2-tss",
    "systemd-pcrphase",
    "walinuxagent",
]

# Sanity: modules that must NOT be omitted by the trim.
RETAINED_MODULES = ["ostree", "crypt", "dm", "kernel-modules", "kernel-modules-extra"]


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class InitramfsProfileTest(unittest.TestCase):
    def _run_script(self, tmp: Path, dracut_log: Path, lsinitrd_marker: Path, verify_marker: Path):
        bindir = tmp / "bin"
        bindir.mkdir()

        _write_exe(bindir / "ls", "#!/bin/sh\necho test-kver\n")
        _write_exe(bindir / "mkdir", "#!/bin/sh\nexit 0\n")

        # Emulate BOOT_HELPER firmware/drivers/verify output. first call -> firmware,
        # second call -> drivers, third call is the post-dracut verify.
        python_stub = (
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *--format\\ firmware*)\n'
            '    echo "qca/brhbtfw20.tlv"\n'
            '    echo "novatek_ts_csot_3k_fw.bin"\n'
            '    exit 0;;\n'
            '  *--format\\ drivers*)\n'
            '    echo "qcom_pil_info"\n'
            '    echo "hci_uart"\n'
            '    echo "btqca"\n'
            '    exit 0;;\n'
            '  *--format\\ verify*)\n'
            '    printf "%s" "$*" > "$VERIFY_MARKER"\n'
            '    exit 0;;\n'
            'esac\n'
            'exit 1\n'
        )
        _write_exe(bindir / "python3", python_stub)

        # dracut stub: capture argv to log file.
        dracut_stub = (
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > "$DRACUT_LOG"\n'
            'exit 0\n'
        )
        _write_exe(bindir / "dracut", dracut_stub)

        # lsinitrd stub: emit every required path so the required-list check passes.
        lsinitrd_stub = (
            "#!/bin/sh\n"
            'cat <<\'EOF\'\n'
            "usr/lib/systemd/system/armada-splash-initrd.service\n"
            "usr/lib/systemd/system/dracut-pre-mount.service.d/armada-splash.conf\n"
            "usr/libexec/armada/armada-splash\n"
            "usr/libexec/armada/armada-splash-launcher\n"
            "usr/libexec/armada/device-env\n"
            "usr/share/armada/splash/splash.asp\n"
            "usr/libexec/armada/armada-ostree-fallback\n"
            "usr/libexec/armada/armada-bound-deployment\n"
            "usr/lib/systemd/system/ostree-prepare-root.service.d/armada-fallback.conf\n"
            "usr/lib/ostree/ostree-prepare-root\n"
            "EOF\n"
            'printf "1" > "$LSINITRD_MARKER"\n'
            'exit 0\n'
        )
        _write_exe(bindir / "lsinitrd", lsinitrd_stub)

        env = os.environ.copy()
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        env["TMPDIR"] = str(tmp)
        env["DRACUT_LOG"] = str(dracut_log)
        env["LSINITRD_MARKER"] = str(lsinitrd_marker)
        env["VERIFY_MARKER"] = str(verify_marker)

        return subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            cwd=str(tmp),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

    def test_dracut_invocation_omits_network_vm_storage_unlock_modules(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            dracut_log = tmp / "dracut.argv"
            lsinitrd_marker = tmp / "lsinitrd.marker"
            verify_marker = tmp / "verify.marker"

            proc = self._run_script(tmp, dracut_log, lsinitrd_marker, verify_marker)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(dracut_log.exists(), "dracut was not invoked")
            self.assertTrue(lsinitrd_marker.exists(), "lsinitrd was not invoked")
            self.assertTrue(verify_marker.exists(), "post-dracut verify was not invoked")

            argv = dracut_log.read_text().splitlines()

            # Required existing behavior.
            self.assertIn("--no-hostonly", argv)
            self.assertIn("--xz", argv)
            self.assertIn("--force", argv)
            self.assertIn("--reproducible", argv)
            self.assertIn("--kver", argv)
            self.assertIn("test-kver", argv)

            # Add modules for splash and ostree fallback.
            add_values = []
            i = 0
            while i < len(argv):
                if argv[i] == "--add" and i + 1 < len(argv):
                    add_values.append(argv[i + 1])
                    i += 2
                    continue
                i += 1
            self.assertIn("ostree", add_values)
            self.assertIn("armada-splash", add_values)
            self.assertIn("armada-ostree-fallback", add_values)

            # Firmware and drivers args still present with resolver output.
            install_values = []
            add_driver_values = []
            i = 0
            while i < len(argv):
                if argv[i] == "--install" and i + 1 < len(argv):
                    install_values.append(argv[i + 1])
                    i += 2
                    continue
                if argv[i] == "--add-drivers" and i + 1 < len(argv):
                    add_driver_values.append(argv[i + 1])
                    i += 2
                    continue
                i += 1
            self.assertTrue(any("qca/brhbtfw20.tlv" in v for v in install_values), install_values)
            self.assertTrue(any("novatek_ts_csot_3k_fw.bin" in v for v in install_values), install_values)
            drivers_joined = " ".join(add_driver_values)
            self.assertIn("qcom_pil_info", drivers_joined)
            self.assertIn("hci_uart", drivers_joined)
            self.assertIn("btqca", drivers_joined)

            # New behavior: x86 microcode early load disabled for ARM64-only images.
            self.assertIn("--no-early-microcode", argv)

            # New behavior: explicit omit list with each unsupported module.
            omit_values = []
            i = 0
            while i < len(argv):
                if argv[i] == "--omit" and i + 1 < len(argv):
                    omit_values.append(argv[i + 1])
                    i += 2
                    continue
                i += 1
            self.assertTrue(omit_values, "expected at least one --omit argument")
            omitted = set()
            for value in omit_values:
                for tok in value.split():
                    omitted.add(tok)
            for module in OMITTED_MODULES:
                self.assertIn(module, omitted, f"missing --omit for {module}")
            for module in RETAINED_MODULES:
                self.assertNotIn(module, omitted, f"must not omit retained module {module}")

    def test_required_module_check_skipped_when_lsinitrd_reports_missing(self):
        # Guard: current required-list enforcement still aborts before verify.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            dracut_log = tmp / "dracut.argv"
            lsinitrd_marker = tmp / "lsinitrd.marker"
            verify_marker = tmp / "verify.marker"

            bindir = tmp / "bin"
            bindir.mkdir()
            _write_exe(bindir / "ls", "#!/bin/sh\necho test-kver\n")
            _write_exe(bindir / "mkdir", "#!/bin/sh\nexit 0\n")
            _write_exe(
                bindir / "python3",
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *--format\\ firmware*|*--format\\ drivers*)\n'
                '    exit 0;;\n'
                '  *--format\\ verify*)\n'
                '    printf "%s" "$*" > "$VERIFY_MARKER"\n'
                '    exit 0;;\n'
                'esac\n'
                'exit 1\n',
            )
            _write_exe(
                bindir / "dracut",
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DRACUT_LOG\"\nexit 0\n",
            )
            # lsinitrd reports nothing; real awk (from PATH) must surface
            # every required path as missing so the production check aborts.
            _write_exe(bindir / "lsinitrd", "#!/bin/sh\nexit 0\n")
            # grep used in failure path.
            _write_exe(bindir / "grep", "#!/bin/sh\nexit 1\n")

            env = os.environ.copy()
            env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
            env["TMPDIR"] = str(tmp)
            env["DRACUT_LOG"] = str(dracut_log)
            env["LSINITRD_MARKER"] = str(lsinitrd_marker)
            env["VERIFY_MARKER"] = str(verify_marker)

            proc = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                cwd=str(tmp),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(verify_marker.exists(), "verify must not run when required files are missing")
            self.assertIn(
                "usr/libexec/armada/armada-splash missing from initramfs",
                proc.stderr,
                proc.stderr,
            )

    def test_requirement_resolver_failure_aborts_before_dracut(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            bindir = tmp / "bin"
            bindir.mkdir()
            dracut_marker = tmp / "dracut.marker"

            _write_exe(bindir / "ls", "#!/bin/sh\necho test-kver\n")
            _write_exe(bindir / "mkdir", "#!/bin/sh\nexit 0\n")
            _write_exe(bindir / "python3", "#!/bin/sh\nexit 23\n")
            _write_exe(
                bindir / "dracut",
                "#!/bin/sh\nprintf '1' > \"$DRACUT_MARKER\"\nexit 0\n",
            )

            env = os.environ.copy()
            env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
            env["TMPDIR"] = str(tmp)
            env["DRACUT_MARKER"] = str(dracut_marker)

            proc = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                cwd=str(tmp),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 23, proc.stderr)
            self.assertFalse(dracut_marker.exists(), "dracut must not run when resolver fails")


if __name__ == "__main__":
    unittest.main()
