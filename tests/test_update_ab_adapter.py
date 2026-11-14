import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestUpdateAbAdapter(unittest.TestCase):
    def test_compile_and_run_update_ab_fd_test(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        kshim = repo / "devices" / "lenovo-tb323fu" / "boot" / "kshim-success-source"
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_path = Path(tmpdir) / "update_ab_fd_test"
            cmd = [
                "cc",
                "-std=c11",
                "-D_POSIX_C_SOURCE=200809L",
                "-D_FILE_OFFSET_BITS=64",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=address,undefined",
                "-fno-omit-frame-pointer",
                "-g",
                f"-I{kshim / 'src' / 'include'}",
                str(repo / "build_files" / "update_ab_inspect.c"),
                str(repo / "build_files" / "update_ufs_boot.c"),
                str(kshim / "src" / "core" / "storage_ab.c"),
                str(kshim / "src" / "core" / "storage_block.c"),
                str(repo / "tests" / "update_ab_fd_test.c"),
                "-o",
                str(bin_path),
            ]
            comp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            self.assertEqual(
                comp.returncode,
                0,
                f"compile failed:\nSTDOUT:\n{comp.stdout}\nSTDERR:\n{comp.stderr}",
            )

            env = os.environ.copy()
            env["TMPDIR"] = tmpdir
            env["ASAN_OPTIONS"] = "detect_leaks=1"
            run = subprocess.run(
                [str(bin_path)],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            self.assertEqual(
                run.returncode,
                0,
                f"test binary failed:\nSTDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}",
            )
            self.assertIn("PASS", run.stdout)


if __name__ == "__main__":
    unittest.main()
