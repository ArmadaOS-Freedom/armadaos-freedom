import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ARMADA_LIB = Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import client as update_client
from updates import legacy_cli

COMMIT = "a" * 64
DEPLOY = f"os/{COMMIT}.2"


def _status_json(comp, busy, job_id, op, result):
    return json.dumps({"schemaVersion": 1, "components": {comp: {
        "busy": busy, "jobId": job_id, "operation": op, "result": result
    }}})


class LegacyCliTest(unittest.TestCase):
    def _run_cli(self, argv, fake_client=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = legacy_cli.main(argv, client=fake_client)
        return rc, out.getvalue(), err.getvalue()

    def test_probes_and_invalid_args_no_bus(self):
        with mock.patch("updates.client.UpdateClient") as cls:
            for argv in (["--supports-duplicate-detection"],
                         ["--unknown", "--supports-duplicate-detection", "extra"]):
                rc, out, err = self._run_cli(argv)
                self.assertEqual((rc, out, err), (0, "", ""))
            for argv in (["bad"], ["check", "extra"], ["--enable-duplicate-detection", "bad"]):
                rc, out, err = self._run_cli(argv)
                self.assertEqual(rc, 7)
                self.assertIn("armada-update: invalid arguments", err)
            cls.assert_not_called()

    def test_module_entrypoint_subprocess(self):
        env = dict(os.environ)
        pypath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{ARMADA_LIB}{os.pathsep}{pypath}" if pypath else str(ARMADA_LIB)
        p0 = subprocess.run([sys.executable, "-m", "updates.legacy_cli", "--supports-duplicate-detection"],
                            env=env, capture_output=True, text=True)
        self.assertEqual((p0.returncode, p0.stdout, p0.stderr), (0, "", ""))
        p7 = subprocess.run([sys.executable, "-m", "updates.legacy_cli", "--bogus"],
                            env=env, capture_output=True, text=True)
        self.assertEqual(p7.returncode, 7)
        self.assertIn("invalid arguments", p7.stderr)

    def test_duplicate_detection_modifier_routing(self):
        chk = mock.Mock()
        chk.run.return_value = {"state": "update_available", "targetCommit": COMMIT}
        rc, out, _ = self._run_cli(["--enable-duplicate-detection", "check"], chk)
        self.assertEqual((rc, out), (0, f"{COMMIT}\n"))
        chk.run.assert_called_once_with("system", "check", timeout=120)

        app = mock.Mock()
        app.run.side_effect = [
            {"state": "up_to_date"},
            {"state": "awaiting_reboot", "requiresReboot": True,
             "bootWriteState": "written", "activationOutcome": "activated"},
        ]
        rc, out, _ = self._run_cli(["--enable-duplicate-detection"], app)
        self.assertEqual((rc, out), (0, "100%\n"))
        self.assertEqual([c.args[:2] for c in app.run.call_args_list],
                         [("system", "status"), ("system", "update")])

    def test_check_states_and_commit_identity(self):
        cases = [
            ({"state": "update_available", "targetCommit": COMMIT}, 0, f"{COMMIT}\n", ""),
            ({"state": "awaiting_reboot", "requiresReboot": True, "trial": {"newDeployment": DEPLOY}},
             0, f"{COMMIT}\n", ""),
            ({"state": "update_available", "targetCommit": "BAD"}, 7, "", "invalid target commit"),
            ({"state": "awaiting_reboot", "requiresReboot": True, "trial": {"newDeployment": "os/bad.0"}},
             7, "", "canonical deployment"),
            ({"state": "awaiting_reboot", "requiresReboot": False, "trial": {"newDeployment": DEPLOY}},
             7, "", "check state awaiting_reboot"),
            ({"state": "up_to_date"}, 7, "", ""),
            ({"state": "failed", "error": "boom\r\nline2"}, 7, "", "boom  line2"),
        ]
        for res, exp_rc, exp_out, exp_err in cases:
            cli = mock.Mock()
            cli.run.return_value = res
            rc, out, err = self._run_cli(["check"], cli)
            self.assertEqual((rc, out), (exp_rc, exp_out))
            if exp_err:
                self.assertIn(exp_err, err)
                self.assertNotIn("\r", err[:-1])
            else:
                self.assertEqual(err, "")

    def test_pending_deployment_schema_and_serial_range(self):
        for deployment in (f"os/{COMMIT}.0", f"default/{COMMIT}.12",
                           f"freedom-v2/{COMMIT}.4294967295"):
            cli = mock.Mock()
            cli.run.return_value = {
                "state": "awaiting_reboot", "requiresReboot": True,
                "trial": {"newDeployment": deployment},
            }
            rc, out, _ = self._run_cli(["check"], cli)
            self.assertEqual((rc, out), (0, f"{COMMIT}\n"))

        invalid = [
            f"../{COMMIT}.0",
            f"os/{'A' * 64}.0",
            f"os/{COMMIT}.00",
            f"{'a' * 65}/{COMMIT}.0",
            f"os/{COMMIT}.4294967296",
        ]
        for deployment in invalid:
            cli = mock.Mock()
            cli.run.return_value = {
                "state": "awaiting_reboot", "requiresReboot": True,
                "trial": {"newDeployment": deployment},
            }
            rc, out, _ = self._run_cli(["check"], cli)
            self.assertEqual((rc, out), (7, ""))

    def test_apply_pending_progress_and_terminal_guards(self):
        pending = mock.Mock()
        pending.run.return_value = {"state": "awaiting_reboot", "requiresReboot": True}
        rc, out, err = self._run_cli([], pending)
        self.assertEqual((rc, out, err), (0, "100%\n", ""))
        pending.run.assert_called_once_with("system", "status", timeout=120)

        def run_with_progress(comp, op, **kw):
            if op == "status":
                return {"state": "installed"}
            cb = kw["on_progress"]
            cb({"phase": "fetch\r\n1"})
            cb({"phase": "fetch\r\n1"})
            cb({"phase": "deploy\n2"})
            return {"state": "awaiting_reboot", "requiresReboot": True,
                    "bootWriteState": "written", "activationOutcome": "activated"}

        ok = mock.Mock( run=mock.Mock(side_effect=run_with_progress) )
        rc, out, err = self._run_cli([], ok)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["0%", "100%"])
        self.assertEqual(err.splitlines(), ["CHANGED fetch  1", "CHANGED deploy 2"])

        bad_terminals = [
            {"state": "awaiting_reboot", "requiresReboot": True,
             "bootWriteState": "uncertain", "activationOutcome": "activated"},
            {"state": "awaiting_reboot", "requiresReboot": True,
             "bootWriteState": "written", "activationOutcome": "awaiting_confirmation"},
            {"state": "failed", "phase": "deploy", "error": "disk full"},
        ]
        for term in bad_terminals:
            c = mock.Mock()
            c.run.side_effect = [{"state": "up_to_date"}, term]
            rc, out, err = self._run_cli([], c)
            self.assertEqual(rc, 7)
            self.assertNotIn("100%", out)
            self.assertIn("armada-update:", err)


class UpdateClientTest(unittest.TestCase):
    def test_operation_mapping_and_polling(self):
        expected = [
            (("system", "status", None, None), ("RefreshStatus", "system")),
            (("system", "check", None, None), ("StartCheck", "system")),
            (("system", "update", None, None), ("StartSystemUpdate", None)),
            (("system", "rollback", None, COMMIT), ("StartSystemRollback", COMMIT)),
            (("system", "select_channel", "beta", None), ("SetSystemChannel", "beta")),
            (("decky", "status", None, None), ("RefreshStatus", "decky")),
            (("decky", "check", None, None), ("StartCheck", "decky")),
            (("decky", "update", None, None), ("StartDeckyAction", "update")),
            (("decky", "repair", None, None), ("StartDeckyAction", "repair")),
            (("decky", "recover", None, None), ("StartDeckyAction", "recover")),
        ]
        for (comp, op, chan, csum), exp_start in expected:
            cli = update_client.UpdateClient(poll_interval=0)
            seen, calls = [], []
            replies = [
                "job-1",
                _status_json(comp, True, "job-1", op, {"component": comp, "phase": "run"}),
                _status_json(comp, False, "job-1", op, {"component": comp, "state": "up_to_date"}),
            ]

            def fake_call(method, argument=None, _r=replies, _c=calls):
                _c.append((method, argument))
                return _r.pop(0)

            with mock.patch.object(cli, "call", side_effect=fake_call), mock.patch("time.sleep"):
                res = cli.run(comp, op, channel=chan, checksum=csum, on_progress=seen.append)
            self.assertEqual(calls[0], exp_start)
            self.assertEqual([c[0] for c in calls[1:]], ["GetStatus", "GetStatus"])
            self.assertEqual(len(seen), 2)
            self.assertEqual(res["state"], "up_to_date")

    def test_validation_before_call(self):
        cli = update_client.UpdateClient()
        with mock.patch.object(cli, "call") as mcall:
            for args, kw in [(("bad", "check"), {}), (("system", "bad"), {}),
                             (("system", "select_channel"), {"channel": "nightly"}),
                             (("system", "check"), {"channel": "stable"}),
                             (("system", "rollback"), {}),
                             (("system", "rollback"), {"checksum": "bad"}),
                             (("system", "rollback"), {"checksum": "A" * 64}),
                             (("system", "rollback"), {"checksum": COMMIT, "channel": "stable"}),
                             (("system", "update"), {"checksum": COMMIT}),
                             (("system", "select_channel"), {"channel": "stable", "checksum": COMMIT}),
                             (("decky", "rollback"), {"checksum": COMMIT})]:
                with self.assertRaises(ValueError):
                    cli.run(*args, **kw)
            mcall.assert_not_called()
        for method, arg in [("Bad", None), ("GetStatus", "x"), ("StartCheck", "bad"), ("StartCheck", None),
                            ("StartSystemRollback", None), ("StartSystemRollback", "bad"), ("StartSystemRollback", "A" * 64)]:
            with self.assertRaises(ValueError):
                cli.call(method, arg)

    def test_poll_errors_and_timeout_do_not_restart(self):
        bad_polls = [
            _status_json("system", True, "job-2", "check", {"component": "system"}),
            _status_json("system", True, "job-1", "update", {"component": "system"}),
            _status_json("system", True, "job-1", "check", {"component": "decky"}),
            _status_json("system", "yes", "job-1", "check", {"component": "system"}),
        ]
        for payload in bad_polls:
            cli = update_client.UpdateClient(poll_interval=0)
            calls = []
            items = ["job-1", payload]
            with mock.patch.object(cli, "call", side_effect=lambda m, a=None, _i=items, _c=calls: (_c.append(m), _i.pop(0))[1]):
                with self.assertRaises(RuntimeError):
                    cli.run("system", "check")
            self.assertEqual(calls, ["StartCheck", "GetStatus"])

        cli = update_client.UpdateClient(poll_interval=0.5)
        calls = []
        busy = _status_json("system", True, "job-9", "check", {"component": "system"})
        seq = ["job-9", busy, busy]
        with mock.patch.object(cli, "call", side_effect=lambda m, a=None: (calls.append(m), seq.pop(0))[1]), \
             mock.patch("time.monotonic", side_effect=[10.0, 10.5, 11.0]), \
             mock.patch("time.sleep") as msleep:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                cli.run("system", "check", timeout=1.0)
        self.assertEqual(calls, ["StartCheck", "GetStatus", "GetStatus"])
        msleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
