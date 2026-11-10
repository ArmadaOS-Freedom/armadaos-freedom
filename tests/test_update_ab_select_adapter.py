import os
import pathlib
import subprocess
import tempfile
import unittest


class UpdateAbSelectAdapterTest(unittest.TestCase):
    def test_native_fd_selection(self):
        repo = pathlib.Path(__file__).resolve().parents[1]
        kshim = repo / "devices/lenovo-tb323fu/boot/kshim-success-source"
        with tempfile.TemporaryDirectory() as tmp:
            exe = pathlib.Path(tmp) / "update_ab_select_fd_test"
            cmd = [
                os.environ.get("CC", "cc"), "-std=c11", "-O1", "-g",
                "-Wall", "-Wextra", "-Werror", "-D_FILE_OFFSET_BITS=64",
                "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                "-I", str(repo / "build_files"),
                "-I", str(kshim / "src/include"),
                "-I", str(kshim / "tests"), "-I", str(repo / "tests"),
                str(repo / "tests/update_ab_select_fd_test.c"),
                str(repo / "build_files/update_ab_inspect.c"),
                str(repo / "build_files/update_ufs_boot.c"),
                str(kshim / "src/core/storage_ab.c"),
                str(kshim / "src/core/storage_block.c"),
                "-Wl,--wrap=ioctl", "-Wl,--wrap=pwrite64",
                "-Wl,--wrap=fsync", "-o", str(exe),
            ]
            subprocess.run(cmd, check=True, timeout=60)
            env = os.environ.copy()
            env["TMPDIR"] = tmp
            run = subprocess.run([str(exe)], check=False, timeout=60,
                                 env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("PASS native A/B fd selection tests (12 cases)",
                          run.stdout)
            self.assertIn("PASS native A/B fd mark-success tests (12 cases)",
                          run.stdout)


if __name__ == "__main__":
    unittest.main()
