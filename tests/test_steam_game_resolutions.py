import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "system_files/usr/libexec/armada/steam-game-resolutions"
LOADER = importlib.machinery.SourceFileLoader("steam_game_resolutions", str(HELPER))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
resolutions = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(resolutions)


class MockSteamSocket:
    def __init__(self, installed=False, rejected=False):
        self.ready = installed
        self.rejected = rejected
        self.commands = []
        self.pause = False

    def settimeout(self, _):
        pass

    def send(self, message):
        self.commands.append(json.loads(message))

    def recv(self):
        request = self.commands[-1]
        if self.pause:
            self.pause = False
            return json.dumps({"method": "Debugger.paused", "params": {
                "callFrames": [{"callFrameId": "frame"}], "hitBreakpoints": ["breakpoint"],
            }})
        if request["method"] == "Runtime.evaluate":
            result = {"result": {"value": self.ready}}
        elif request["method"] == "Debugger.setBreakpointByUrl":
            result = {"breakpointId": "breakpoint"}
        elif request["method"] == "Debugger.evaluateOnCallFrame" and self.rejected:
            result = {"exceptionDetails": {}}
        else:
            result = {}
            if request["method"] == "Page.reload":
                self.pause = True
            if request["method"] == "Debugger.resume":
                self.ready = True
        return json.dumps({"id": request["id"], "result": result})


class SteamGameResolutionsTest(unittest.TestCase):
    def test_install_reloads_once_and_removes_breakpoint(self):
        ws = MockSteamSocket()
        self.assertTrue(resolutions.install(ws, "script"))
        self.assertEqual([entry["method"] for entry in ws.commands], [
            "Runtime.evaluate", "Debugger.enable", "Debugger.setBreakpointByUrl",
            "Page.reload", "Debugger.evaluateOnCallFrame", "Debugger.removeBreakpoint",
            "Debugger.resume", "Debugger.disable", "Runtime.evaluate",
        ])
        self.assertEqual(ws.commands[4]["params"], {"callFrameId": "frame", "expression": "script"})

        ws = MockSteamSocket(installed=True)
        self.assertTrue(resolutions.install(ws, "script"))
        self.assertEqual([entry["method"] for entry in ws.commands], ["Runtime.evaluate"])

    def test_failed_injection_disables_the_debugger(self):
        ws = MockSteamSocket(rejected=True)
        with self.assertRaisesRegex(RuntimeError, "rejected the resolution hook"):
            resolutions.install(ws, "script")
        self.assertEqual(ws.commands[-1]["method"], "Debugger.disable")

    def test_watch_restores_a_restarted_ui_and_skips_an_unsupported_build(self):
        old_asset = "https://steamloopback.host/sp.js?contenthash=old"
        new_asset = "https://steamloopback.host/sp.js?contenthash=new"
        ws = mock.Mock()
        bridge = SimpleNamespace(
            BridgeError=RuntimeError,
            discover_target=lambda: ("test", "ws://127.0.0.1:8080/devtools/page/test"),
            _default_ws_factory=lambda _: lambda: ws,
        )
        websocket = SimpleNamespace(WebSocketException=OSError)
        states = [
            {"asset": old_asset, "installed": True},
            {"asset": old_asset, "installed": False},
            {"asset": new_asset, "installed": False},
            {"asset": new_asset, "installed": False},
        ]

        with (
            mock.patch.dict(sys.modules, {"websocket": websocket}),
            mock.patch.object(resolutions, "ui_state", side_effect=states),
            mock.patch.object(resolutions, "install", side_effect=[True, False]) as install,
            mock.patch.object(resolutions.time, "monotonic", side_effect=lambda: ws.close.call_count * 40),
            mock.patch.object(resolutions.time, "sleep", side_effect=[None, None, None, KeyboardInterrupt]),
            mock.patch("builtins.print"),
            self.assertRaises(KeyboardInterrupt),
        ):
            resolutions.watch(bridge, "script")

        self.assertEqual(install.call_args_list, [mock.call(ws, "script"), mock.call(ws, "script")])
        self.assertEqual(ws.close.call_count, len(states))

    def test_resolutions_keep_configured_order_and_deduplicate(self):
        self.assertEqual(
            resolutions.configured_modes("1600x900 1520x952 1600x900"),
            ["1600x900", "1520x952"],
        )

    def test_rejects_invalid_or_out_of_range_modes(self):
        for invalid in ("bad", "0x952", "4096x952", "1520x1905", "3040x1904"):
            with self.subTest(mode=invalid), self.assertRaises(ValueError):
                resolutions.configured_modes(invalid)

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_only_the_single_game_menu_receives_custom_options(self):
        subprocess.run([shutil.which("node"), str(ROOT / "tests/steam-game-resolutions-test.cjs")], check=True)


if __name__ == "__main__":
    unittest.main()
