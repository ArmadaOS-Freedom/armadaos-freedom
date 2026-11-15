import importlib.util
import io
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "system_files/usr/libexec/armada/steam-menu"
WS_URL = "ws://127.0.0.1:8080/devtools/page/a-b"
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
from updates import steam_bridge as sb  # noqa: E402

TRUE_RESULT = {"type": "boolean", "value": True}


def load_helper():
    """Load the extension-less CLI as a module, as the target executes it."""
    loader = SourceFileLoader("armada_steam_menu", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


menu = load_helper()


def ok_reply():
    return json.dumps({"id": menu.REQUEST_ID,
                       "result": {"result": dict(TRUE_RESULT)}})


def value_reply(value):
    return json.dumps({"id": menu.REQUEST_ID,
                       "result": {"result": dict(value)}})


class Clock:
    """Monotonic stand-in that only advances when a read times out."""
    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        return self.now


class FakeSocket:
    """The verified bridge socket boundary, with no real networking."""
    def __init__(self, messages=(), clock=None, close_error=None):
        self.messages = list(messages)
        self.clock = clock
        self.close_error = close_error
        self.sent = []
        self.closed = 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.clock is not None:
            self.clock.now += self.clock.step
        raise TimeoutError("idle")

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class DispatchTests(unittest.TestCase):
    """One Runtime.evaluate per request, a close on every outcome, no replay."""

    def test_home_and_qam_send_one_request_and_close(self):
        for action in ("home", "qam"):
            with self.subTest(action=action):
                ws = FakeSocket([ok_reply()], clock=Clock())
                self.assertTrue(menu.dispatch_menu(lambda: ws, action))
                self.assertEqual(len(ws.sent), 1)
                self.assertEqual(ws.sent[0]["method"], "Runtime.evaluate")
                self.assertEqual(ws.sent[0]["params"]["expression"],
                                 menu.expression_for(action))
                self.assertIs(ws.sent[0]["params"]["returnByValue"], True)
                self.assertEqual(ws.closed, 1)

    def test_expressions_are_fixed_and_distinct_per_menu(self):
        home, qam = menu.expression_for("home"), menu.expression_for("qam")
        self.assertNotEqual(home, qam)
        self.assertEqual(home, menu.expression_for("home"))
        for action in ("menu", None, ["home"]):
            with self.subTest(action=action):
                with self.assertRaises(sb.BridgeError):
                    menu.expression_for(action)

    def test_notifications_before_the_matching_reply_are_ignored(self):
        ws = FakeSocket([
            json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}),
            json.dumps({"id": 7, "result": {"result": {"type": "string",
                                                       "value": "other"}}}),
            json.dumps({"method": "Page.frameNavigated",
                        "params": {"frame": {"id": "root"}}}),
            ok_reply(),
        ], clock=Clock())
        self.assertTrue(menu.dispatch_menu(lambda: ws, "qam"))
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)

    def test_cdp_error_fails_without_replay(self):
        ws = FakeSocket([json.dumps({"id": menu.REQUEST_ID,
                                     "error": {"message": "not allowed"}})],
                        clock=Clock())
        with self.assertRaises(sb.BridgeError):
            menu.dispatch_menu(lambda: ws, "home")
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)

    def test_js_exception_fails_without_replay(self):
        message = {"id": menu.REQUEST_ID,
                   "result": {
                       "result": {"type": "object", "subtype": "error"},
                       "exceptionDetails": {
                           "text": "Uncaught",
                           "exception": {"description": "Error: nope"}}}}
        ws = FakeSocket([json.dumps(message)], clock=Clock())
        with self.assertRaises(sb.BridgeError):
            menu.dispatch_menu(lambda: ws, "home")
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)

    def test_non_true_results_fail_without_replay(self):
        values = ({"type": "boolean", "value": False},
                  {"type": "string", "value": "ok"},
                  {"type": "undefined"},
                  {"type": "number", "value": 1})
        for value in values:
            with self.subTest(value=value):
                ws = FakeSocket([value_reply(value)], clock=Clock())
                with self.assertRaises(sb.BridgeError):
                    menu.dispatch_menu(lambda: ws, "home")
                self.assertEqual(len(ws.sent), 1)
                self.assertEqual(ws.closed, 1)
        empty = FakeSocket([json.dumps({"id": menu.REQUEST_ID, "result": {}})],
                           clock=Clock())
        with self.assertRaises(sb.BridgeError):
            menu.dispatch_menu(lambda: empty, "home")
        self.assertEqual(len(empty.sent), 1)
        self.assertEqual(empty.closed, 1)

    def test_idle_connection_times_out_without_replay(self):
        clock = Clock(step=0.75)
        ws = FakeSocket(clock=clock)
        with self.assertRaises(sb.BridgeError):
            menu.dispatch_menu(lambda: ws, "home", timeout=1.0, clock=clock)
        self.assertGreaterEqual(clock.now, 1.0)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)

    def test_closed_and_malformed_messages_fail(self):
        for messages in ([""], [b"\xff\xfe"], ["not json"], ["[]"], ["null"]):
            with self.subTest(messages=messages):
                ws = FakeSocket(messages, clock=Clock())
                with self.assertRaises(sb.BridgeError):
                    menu.dispatch_menu(lambda: ws, "home")
                self.assertEqual(ws.closed, 1)

    def test_send_failure_and_close_failure_still_close(self):
        class BrokenSend(FakeSocket):
            def send(self, raw):
                self.sent.append(json.loads(raw))
                raise OSError("socket is gone")

        ws = BrokenSend(clock=Clock())
        with self.assertRaises(OSError):
            menu.dispatch_menu(lambda: ws, "home")
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)
        ws = FakeSocket([ok_reply()], clock=Clock(),
                        close_error=OSError("close failed"))
        self.assertTrue(menu.dispatch_menu(lambda: ws, "home"))
        self.assertEqual(ws.closed, 1)

    def test_connection_attempt_is_not_repeated(self):
        attempts = []

        def factory():
            attempts.append(1)
            raise sb.BridgeError("websocket peer is not loopback")

        with self.assertRaises(sb.BridgeError):
            menu.dispatch_menu(factory, "home")
        self.assertEqual(len(attempts), 1)


class MainTests(unittest.TestCase):
    """The CLI keeps every trust decision inside the existing bridge."""

    def run_main(self, argv):
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = menu.main(argv)
        return code, stderr.getvalue()

    def test_invalid_cli_is_rejected_before_any_trust_check(self):
        for argv in ([], ["home", "qam"], ["menu"], ["HOME"], [""], ["--help"]):
            with self.subTest(argv=argv):
                with mock.patch.object(menu.os, "geteuid") as euid, \
                        mock.patch.object(sb, "_verify_guard") as guard, \
                        mock.patch.object(sb, "discover_target") as discover, \
                        mock.patch.object(sb, "_default_ws_factory") as factory:
                    code, _stderr = self.run_main(argv)
                self.assertEqual(code, 2)
                euid.assert_not_called()
                guard.assert_not_called()
                discover.assert_not_called()
                factory.assert_not_called()

    def test_root_is_rejected_without_connecting(self):
        with mock.patch.object(menu.os, "geteuid", return_value=0), \
                mock.patch.object(sb, "_verify_guard") as guard, \
                mock.patch.object(sb, "discover_target") as discover, \
                mock.patch.object(sb, "_default_ws_factory") as factory:
            code, stderr = self.run_main(["home"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)
        guard.assert_not_called()
        discover.assert_not_called()
        factory.assert_not_called()

    def test_guard_and_discovery_failures_stay_offline(self):
        with mock.patch.object(menu.os, "geteuid", return_value=1000), \
                mock.patch.object(sb, "_verify_guard",
                                  side_effect=sb.BridgeError("no proof")) as guard, \
                mock.patch.object(sb, "discover_target") as discover, \
                mock.patch.object(sb, "_default_ws_factory") as factory:
            code, stderr = self.run_main(["qam"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)
        guard.assert_called_once_with(1000)
        discover.assert_not_called()
        factory.assert_not_called()
        with mock.patch.object(menu.os, "geteuid", return_value=1000), \
                mock.patch.object(sb, "_verify_guard") as guard, \
                mock.patch.object(sb, "discover_target",
                                  side_effect=sb.BridgeError("no target")) as discover, \
                mock.patch.object(sb, "_default_ws_factory") as factory:
            code, stderr = self.run_main(["qam"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)
        guard.assert_called_once_with(1000)
        discover.assert_called_once_with()
        factory.assert_not_called()

    def test_success_uses_the_bridge_factory_and_sends_one_request(self):
        ws = FakeSocket([ok_reply()])
        with mock.patch.object(menu.os, "geteuid", return_value=1000), \
                mock.patch.object(sb, "_verify_guard") as guard, \
                mock.patch.object(sb, "discover_target",
                                  return_value=("target", WS_URL)) as discover, \
                mock.patch.object(sb, "_default_ws_factory",
                                  return_value=lambda: ws) as factory:
            code, stderr = self.run_main(["qam"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        guard.assert_called_once_with(1000)
        discover.assert_called_once_with()
        factory.assert_called_once_with(WS_URL)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]["params"]["expression"],
                         menu.expression_for("qam"))
        self.assertEqual(ws.closed, 1)

    def test_failed_dispatch_exits_nonzero_and_closes(self):
        ws = FakeSocket([value_reply({"type": "string", "value": "unavailable"})])
        with mock.patch.object(menu.os, "geteuid", return_value=1000), \
                mock.patch.object(sb, "_verify_guard"), \
                mock.patch.object(sb, "discover_target",
                                  return_value=("target", WS_URL)), \
                mock.patch.object(sb, "_default_ws_factory",
                                  return_value=lambda: ws):
            code, stderr = self.run_main(["home"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.closed, 1)


NODE_HARNESS = """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const mode = process.argv[3];
const calls = [];
const sandbox = {window: {}};
if (mode !== 'missing') {
  sandbox.window = {SteamUIStore: {m_GamepadNavigationManager: {
    DispatchVirtualGamepad: function (name, args) {
      calls.push([name, args]);
      if (mode === 'faildown' && name === 'vgp_onbuttondown') {
        throw new Error('press rejected');
      }
    }
  }}};
}
let outcome;
try {
  outcome = {ok: true, value: vm.runInNewContext(source, sandbox)};
} catch (error) {
  outcome = {ok: false,
             error: String(error && error.message ? error.message : error)};
}
outcome.calls = calls;
process.stdout.write(JSON.stringify(outcome));
"""


@unittest.skipUnless(shutil.which("node"), "node is unavailable")
class NodeExpressionTests(unittest.TestCase):
    """Run the exact production expression in Node's vm with a Steam stub."""

    def run_expression(self, action, mode="ok"):
        node = shutil.which("node")
        with tempfile.TemporaryDirectory() as base:
            expression = Path(base, "expression.js")
            expression.write_text(menu.expression_for(action), encoding="utf-8")
            harness = Path(base, "harness.js")
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(harness), str(expression), mode],
                                    capture_output=True, text=True, timeout=60,
                                    check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_press_and_release_for_both_menus(self):
        for action, button in (("home", 27), ("qam", 28)):
            with self.subTest(action=action):
                outcome = self.run_expression(action)
                self.assertTrue(outcome["ok"], outcome.get("error"))
                self.assertIs(outcome["value"], True)
                self.assertEqual(outcome["calls"], [
                    ["vgp_onbuttondown",
                     {"button": button, "source": 1, "is_repeat": False}],
                    ["vgp_onbuttonup",
                     {"button": button, "source": 1, "is_repeat": False}],
                ])

    def test_release_runs_when_the_press_throws(self):
        outcome = self.run_expression("qam", "faildown")
        self.assertFalse(outcome["ok"])
        self.assertEqual([call[0] for call in outcome["calls"]],
                         ["vgp_onbuttondown", "vgp_onbuttonup"])
        self.assertEqual(outcome["calls"][1][1],
                         {"button": 28, "source": 1, "is_repeat": False})

    def test_missing_native_api_is_rejected_before_dispatch(self):
        outcome = self.run_expression("home", "missing")
        self.assertFalse(outcome["ok"])
        self.assertIn("unavailable", outcome["error"])
        self.assertEqual(outcome["calls"], [])


if __name__ == "__main__":
    unittest.main()
