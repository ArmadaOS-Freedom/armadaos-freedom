#!/usr/bin/env python3

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "system_files"


def run(*args, env=None):
    return subprocess.run(
        [str(arg) for arg in args], text=True, capture_output=True,
        timeout=5, env=env,
    )


class DesktopCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def install(self, source, replacements):
        text = (FILES / source).read_text()
        for old, new in replacements:
            self.assertEqual(text.count(old), 1)
            text = text.replace(old, new)
        target = self.dir / Path(source).name
        target.write_text(text)
        return target

    def test_steamos_update_login_gate_and_forwarding(self):
        login = self.dir / "loginusers.vdf"
        hook = self.dir / "hook"
        log = self.dir / "argv.json"
        hook.write_text(
            "#!/usr/bin/python3\n"
            "import json, os, sys\n"
            f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "print('hook stdout')\n"
            "print('hook stderr', file=sys.stderr)\n"
            "raise SystemExit(int(os.environ['HOOK_EXIT']))\n"
        )
        hook.chmod(0o755)
        script = self.install("usr/bin/steamos-update", [
            ("/var/home/armada/.local/share/Steam/config/loginusers.vdf", str(login)),
            ("/usr/libexec/armada/armada-update", str(hook)),
        ])
        probe = run("bash", script, "--supports-duplicate-detection")
        self.assertEqual((probe.returncode, probe.stdout, probe.stderr), (0, "", ""))
        for content in (None, '"User"\n{\n}\n'):
            if content is None:
                login.unlink(missing_ok=True)
            else:
                login.write_text(content)
            result = run("bash", script, "check")
            self.assertEqual(result.returncode, 7)
            self.assertFalse(log.exists())
        login.write_text('"users" { "1" { "AccountName" "test" } }')
        for code in (0, 1, 7):
            env = os.environ.copy()
            env["HOOK_EXIT"] = str(code)
            args = ["arg with spaces", "quote'and;$(touch no)", "check"]
            result = run("bash", script, *args, env=env)
            self.assertEqual(result.returncode, code)
            self.assertEqual(result.stdout, "hook stdout\n")
            self.assertEqual(result.stderr, "hook stderr\n")
            self.assertEqual(json.loads(log.read_text()), args)
            log.unlink()

    def test_armada_update_python_module_and_real_argument_handling(self):
        package = self.dir / "fake/updates"
        package.mkdir(parents=True)
        log = self.dir / "legacy.json"
        (package / "legacy_cli.py").write_text(
            "import json, sys\n"
            f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(23)\n"
        )
        wrapper = self.install("usr/libexec/armada/armada-update", [
            ("PYTHONPATH=/usr/lib/armada", f"PYTHONPATH={package.parent}")
        ])
        args = ["two words", "';$(false)", "--enable-duplicate-detection"]
        result = run("bash", wrapper, *args)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(log.read_text()), args)
        real = self.install("usr/libexec/armada/armada-update", [
            ("PYTHONPATH=/usr/lib/armada", f"PYTHONPATH={FILES / 'usr/lib/armada'}")
        ])
        self.assertEqual(
            run("bash", real, "--supports-duplicate-detection").returncode, 0
        )
        unknown = run("bash", real, "not-a-real-option")
        self.assertEqual(unknown.returncode, 7)
        self.assertIn("invalid arguments", unknown.stderr)

    def make_selector(self):
        libdir = self.dir / "lib"
        libdir.mkdir()
        state = self.dir / "channel"
        policy = self.dir / "update-policy"
        overrides = (
            f"\nARMADA_CHANNEL_STATE={state}\nARMADA_UPDATE_POLICY={policy}\n"
            "armada_booted_ref() { echo 'ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:testing'; }\n"
            "armada_update_log() { echo \"$*\"; }\n"
        )
        (libdir / "update-lib").write_text(
            (FILES / "usr/lib/armada/update-lib").read_text() + overrides
        )
        channel = self.dir / "channel"
        writer = self.dir / "writer.py"
        writer.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(FILES / 'usr/lib/armada')!r})\n"
            "from updates import trial_confirmation\n"
            "from updates.channel_selection import set_channel\n"
            f"trial_confirmation.UPDATE_LOCK_PATH = {str(self.dir / 'lock')!r}\n"
            f"raise SystemExit(set_channel(sys.argv[1], {str(channel)!r}))\n"
        )
        selector = self.install("usr/libexec/armada/armada-select-branch", [
            ("source /usr/lib/armada/update-lib", f"source {libdir / 'update-lib'}"),
            ('PYTHONPATH=/usr/lib/armada exec /usr/bin/python3 -P -m updates.channel_selection "$channel"',
             f'exec /usr/bin/python3 -P {writer} "$channel"'),
        ])
        return selector, channel

    def test_selector_uses_isolated_python_module_path(self):
        source = (FILES / "usr/libexec/armada/armada-select-branch").read_text()
        invocation = (
            'PYTHONPATH=/usr/lib/armada exec /usr/bin/python3 -P '
            '-m updates.channel_selection "$channel"'
        )
        self.assertEqual(source.count(invocation), 1)
        self.assertNotIn("python3 -m updates.channel_selection", source)

    def test_channel_listing_and_fallback(self):
        selector, _ = self.make_selector()
        listed = run("bash", selector, "-l")
        self.assertEqual((listed.returncode, listed.stdout.splitlines()),
                         (0, ["rel", "beta", "preview"]))
        current = run("bash", selector, "-c")
        self.assertEqual((current.returncode, current.stdout.strip()), (0, "preview"))

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "channel writer requires root")
    def test_channel_aliases_permissions_and_invalid_input(self):
        selector, channel = self.make_selector()
        for alias, canonical, token in (("beta", "beta", "beta"), ("rel", "stable", "rel"),
                                        ("preview", "testing", "preview"), ("main", "testing", "preview")):
            result = run("bash", selector, alias)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(channel.read_text().strip(), canonical)
            self.assertEqual(stat.S_IMODE(channel.stat().st_mode), 0o644)
            current = run("bash", selector, "-c")
            self.assertEqual((current.returncode, current.stdout.strip()), (0, token))
        before = channel.read_bytes()
        invalid = run("bash", selector, "nightly")
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(channel.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
