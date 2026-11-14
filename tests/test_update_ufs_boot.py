import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class UpdateUfsBootTest(unittest.TestCase):
    def test_offline_bsg(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            exe = Path(tmp) / "update_ufs_boot_test"
            command = [
                os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra",
                "-Werror", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                "-Wl,--wrap=ioctl", "-I", str(root / "build_files"),
                str(root / "build_files/update_ufs_boot.c"),
                str(root / "tests/update_ufs_boot_test.c"), "-o", str(exe),
            ]
            built = subprocess.run(command, cwd=root, capture_output=True,
                                   text=True, timeout=60)
            self.assertEqual(built.returncode, 0,
                             msg=built.stdout + built.stderr)
            env = os.environ.copy()
            env["TMPDIR"] = tmp
            ran = subprocess.run([str(exe)], cwd=root, env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0,
                             msg=ran.stdout + ran.stderr)
            self.assertIn("PASS update_ufs_boot offline tests", ran.stdout)


if __name__ == "__main__":
    unittest.main()
