import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
from updates import steam_bridge as sb


class Client:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return {"state": "idle"}

    def call(self, method, argument):
        self.calls.append((method, argument))
        return "job-1"


class WS:
    def __init__(self, stop, responder=None, recv_error=None):
        self.stop, self.responder, self.recv_error = stop, responder, recv_error
        self.queue, self.sent, self.closed = [], [], False

    def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.responder:
            self.responder(self, msg)

    def recv(self):
        if self.recv_error:
            raise self.recv_error
        if self.queue:
            return self.queue.pop(0)
        self.stop.set()
        return "{}"

    def close(self):
        self.closed = True


def connection(ws=None):
    client = Client()
    stop_event = ws.stop if ws is not None else None
    return sb.SteamConnection(lambda: ws, client, "injected", stop_event), client


def context(cid=1, frame="root"):
    return {"id": cid, "origin": sb.TRUSTED_ORIGIN,
            "auxData": {"isDefault": True, "frameId": frame}}


def binding(cid, payload):
    return {"executionContextId": cid, "payload": payload}


class BridgeTests(unittest.TestCase):
    def test_dispatch_all_methods_and_rejections(self):
        client = Client()
        self.assertEqual(sb.dispatch({"id": "a-b", "method": "GetStatus"}, client),
                         {"id": "a-b", "ok": True, "result": {"state": "idle"}})
        valid = [("StartCheck", "system"), ("RefreshStatus", "decky"),
                 ("StartSystemUpdate", None), ("StartSystemRollback", "a" * 64),
                 ("SetSystemChannel", "beta"), ("StartDeckyAction", "repair")]
        for method, argument in valid:
            with self.subTest(method=method):
                result = sb.dispatch({"id": "x", "method": method, "argument": argument}, client)
                self.assertEqual(result, {"id": "x", "ok": True, "result": {"jobId": "job-1"}})
        before = list(client.calls)
        bad = [None, [], {"id": "x", "method": "GetStatus", "extra": 1},
               {"id": "", "method": "GetStatus"}, {"id": True, "method": "GetStatus"},
               {"id": "a b", "method": "GetStatus"}, {"id": "x", "method": "Nope"},
               {"id": "x", "method": "GetStatus", "argument": 1},
               {"id": "x", "method": "StartCheck", "argument": "other"},
               {"id": "x", "method": "StartSystemUpdate", "argument": 1},
               {"id": "x", "method": "StartSystemRollback", "argument": "a"}]
        for request in bad:
            with self.assertRaises(sb.BridgeError):
                sb.dispatch(request, client)
        self.assertEqual(client.calls, before)

    def test_binding_validation_and_malformed_contexts(self):
        conn, client = connection()
        conn._contexts[1] = sb.OrderedDict()
        conn._reply = mock.Mock()
        ignored = [binding(1, '{"id":"x","id":"y","method":"GetStatus"}'),
                   binding(1, '{bad'), binding(1, '[]'),
                   binding({}, '{"id":"x","method":"GetStatus"}'),
                   binding(True, '{"id":"x","method":"GetStatus"}'),
                   binding(99, '{"id":"x","method":"GetStatus"}')]
        for event in ignored:
            conn._handle_binding_event(event)
        self.assertEqual(client.calls, [])
        conn._reply.assert_not_called()
        for bad in ({}, [], None, True, 0, -1):
            conn._drop_context(bad)
            conn._handle_context_created(context(bad))
        conn._root_frame_id = None
        conn._binding_ready = True
        conn._handle_context_created({"id": 2, "origin": sb.TRUSTED_ORIGIN,
                                      "auxData": {"isDefault": True}})
        self.assertNotIn(2, conn._contexts)

    def test_handshake_events_order_and_single_root_injection(self):
        import threading
        stop = threading.Event()

        def respond(ws, msg):
            method, mid = msg["method"], msg["id"]
            replies = {"Page.enable": {}, "Page.getFrameTree": {"frameTree": {"frame": {"id": "root"}}},
                       "Runtime.addBinding": {}, "Runtime.enable": {}}
            if method in replies:
                ws.queue.append(json.dumps({"id": mid, "result": replies[method]}))
            if method == "Runtime.enable":
                for ctx in (context(1), context(2, "iframe")):
                    ws.queue.append(json.dumps({"method": "Runtime.executionContextCreated",
                                                "params": {"context": ctx}}))

        ws = WS(stop, respond)
        conn, _ = connection(ws)
        conn.run()
        self.assertIs(conn.stop_event, stop)
        methods = [m["method"] for m in ws.sent]
        self.assertEqual(methods, ["Page.enable", "Page.getFrameTree", "Runtime.addBinding",
                                   "Runtime.enable", "Runtime.evaluate"])
        self.assertEqual(ws.sent[2]["params"], {"name": sb.BINDING_NAME})
        self.assertEqual(ws.sent[-1]["params"]["contextId"], 1)
        self.assertTrue(ws.closed)

    def test_wrong_binding_name_and_foreign_contexts_ignored(self):
        conn, client = connection()
        conn._contexts[1] = sb.OrderedDict()
        conn._reply = mock.Mock()
        conn._on_event({"method": sb.BINDING_EVENT,
                        "params": {"name": "other", "executionContextId": 1,
                                   "payload": '{"id":"x","method":"GetStatus"}'}})
        conn._reply.assert_not_called()
        self.assertEqual(client.calls, [])
        conn._binding_ready, conn._root_frame_id = True, "root"
        conn._send = mock.Mock()
        ignored = [context(4) | {"origin": "https://evil.example"},
                   context(5, "iframe"),
                   context(6) | {"auxData": {"isDefault": False, "frameId": "root"}}]
        for ctx in ignored:
            conn._on_event({"method": "Runtime.executionContextCreated",
                            "params": {"context": ctx}})
        conn._send.assert_not_called()
        for cid in (4, 5, 6):
            self.assertNotIn(cid, conn._contexts)

    def test_runtime_enable_cdp_error_closes(self):
        import threading
        stop = threading.Event()

        def respond(ws, msg):
            method, mid = msg["method"], msg["id"]
            if method == "Runtime.enable":
                ws.queue.append(json.dumps({"id": mid, "error": {"message": "denied"}}))
            elif method in ("Page.enable", "Page.getFrameTree", "Runtime.addBinding"):
                reply = {"Page.getFrameTree": {"frameTree": {"frame": {"id": "root"}}}}.get(method, {})
                ws.queue.append(json.dumps({"id": mid, "result": reply}))

        ws = WS(stop, respond)
        conn, _ = connection(ws)
        with self.assertRaises(sb.BridgeError):
            conn.run()
        self.assertTrue(ws.closed)
        self.assertEqual([m["method"] for m in ws.sent],
                         ["Page.enable", "Page.getFrameTree", "Runtime.addBinding", "Runtime.enable"])

    def test_dedupe_cap_navigation_and_repeated_context(self):
        conn, client = connection()
        conn._binding_ready, conn._root_frame_id = True, "root"
        conn._send = mock.Mock()
        conn._handle_context_created(context())
        conn._handle_context_created(context())
        self.assertEqual(conn._send.call_count, 1)
        payload = json.dumps({"id": "same", "method": "StartSystemUpdate"})
        conn._handle_binding_event(binding(1, payload))
        conn._handle_binding_event(binding(1, payload))
        self.assertEqual(client.calls, [("StartSystemUpdate", None)])
        for i in range(300):
            conn._handle_binding_event(binding(1, json.dumps({"id": "id%d" % i, "method": "GetStatus"})))
        self.assertEqual(len(conn._contexts[1]), 256)
        conn._on_event({"method": "Page.frameNavigated", "params": {"frame": {"id": "new"}}})
        self.assertEqual(conn._contexts, {})
        conn._handle_binding_event(binding(1, payload))
        self.assertEqual(len(client.calls), 1)
        conn._handle_context_created(context(3, "new"))
        conn._handle_binding_event(binding(3, payload))
        self.assertEqual(len(client.calls), 2)

    def test_reply_serializes_json_object_and_escapes_error(self):
        conn, _ = connection()
        conn._ws = mock.Mock()
        conn._reply(1, {"id": "x", "ok": False, "error": 'bad "quote"\n'})
        expression = json.loads(conn._ws.send.call_args[0][0])["params"]["expression"]
        encoded = expression.split("receive(", 1)[1].rsplit(");", 1)[0]
        self.assertEqual(json.loads(encoded), {"id": "x", "ok": False, "error": 'bad "quote"\n'})
        self.assertNotIn('receive(bad', expression)

    def test_run_finally_cleans_on_receive_and_setup_errors(self):
        import threading
        for raw in (RuntimeError("recv"), None):
            stop = threading.Event()
            ws = WS(stop, recv_error=raw)
            if raw is None:
                ws.responder = lambda w, m: w.queue.append(json.dumps({"id": m["id"], "error": {"message": "no"}}))
            conn, client = connection(ws)
            conn._contexts[1], conn._root_frame_id, conn._binding_ready = sb.OrderedDict(), "root", True
            with self.assertRaises(sb.BridgeError if raw is None else RuntimeError):
                conn.run()
            self.assertTrue(ws.closed)
            self.assertEqual((conn._pending, conn._contexts), ({}, {}))
            self.assertIsNone(conn._root_frame_id)
            self.assertFalse(conn._binding_ready)
            self.assertEqual(client.calls, [])
            self.assertEqual(len(ws.sent), 1)

    def test_discovery_targets(self):
        def page(title="SharedJSContext", url="https://steamloopback.host/index.html", ws=None, id="id"):
            return {"type": "page", "title": title, "url": url, "id": id,
                    "webSocketDebuggerUrl": ws or "ws://127.0.0.1:8080/devtools/page/a-b"}

        with mock.patch.object(sb, "_http_get_discovery", return_value=[page(), page("SP")]):
            self.assertEqual(sb.discover_target()[0], "id")
        with mock.patch.object(sb, "_http_get_discovery",
                               return_value=[page(id="shared"), page("SP", id="sp")]):
            self.assertEqual(sb.discover_target()[0], "sp")
        with mock.patch.object(sb, "_http_get_discovery",
                               return_value=[page("SP", id="sp"), page(id="shared")]):
            self.assertEqual(sb.discover_target()[0], "sp")
        allowed = ["https://steamloopback.host/index.html?query=1",
                   "https://steamloopback.host:443/index.html", "https://steamloopback.host/routes/a?b=c"]
        for url in allowed:
            self.assertTrue(sb._valid_target_url(url))
        rejected = ["https://steamloopback.host/", "https://user@steamloopback.host/index.html",
                    "https://steamloopback.host:444/index.html", "https://steamloopback.host:bad/index.html",
                    "http://steamloopback.host/index.html"]
        for url in rejected:
            self.assertFalse(sb._valid_target_url(url))
        bad_ws = ["ws://192.0.2.10:8080/devtools/page/x", "ws://localhost:8081/devtools/page/x",
                  "ws://user@localhost:8080/devtools/page/x", "ws://localhost:8080/devtools/page/x?q=1",
                  "ws://localhost:bad/devtools/page/x"]
        for url in bad_ws:
            self.assertFalse(sb._valid_ws_url(url))
        with mock.patch.object(sb, "_http_get_discovery", return_value=[page("Other")]):
            with self.assertRaises(sb.BridgeError):
                sb.discover_target()

    def test_redirect_and_bounded_discovery(self):
        req = urllib.request.Request(sb.DISCOVERY_URL)
        with self.assertRaises(urllib.error.HTTPError):
            sb._NoRedirectHandler().redirect_request(req, None, 302, "", {}, "http://other")

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return sb.DISCOVERY_URL
            def read(self, limit):
                self.limit = limit
                return b"[]"

        response = Response()
        opener = mock.Mock()
        opener.open.return_value.__enter__ = mock.Mock(return_value=response)
        opener.open.return_value.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(sb.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(sb._http_get_discovery(), [])
        self.assertEqual(response.limit, sb.DISCOVERY_MAX_BYTES + 1)
        handlers = build.call_args[0]
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertIsInstance(handlers[1], sb._NoRedirectHandler)

    def test_ws_factory_timeout_and_close(self):
        ws = mock.Mock()
        module = mock.Mock(WebSocket=mock.Mock(return_value=ws))
        with mock.patch.dict(sys.modules, {"websocket": module}):
            factory = sb._default_ws_factory("ws://localhost:8080/devtools/page/x")
            self.assertIs(factory(), ws)
            ws.connect.assert_called_once_with("ws://localhost:8080/devtools/page/x", timeout=5.0,
                http_no_proxy=["localhost", "127.0.0.1"], suppress_origin=True)
            ws.settimeout.assert_called_once_with(1.0)
            ws.connect.side_effect = OSError("no")
            with self.assertRaises(OSError):
                factory()
            self.assertTrue(ws.close.called)

    def test_load_sources(self):
        with tempfile.TemporaryDirectory() as base:
            for name in sb.JS_FILES:
                Path(base, name).write_text(name, encoding="utf-8")
            self.assertEqual(sb.load_js_sources(base), "native-client.js\n;\npage.js")
            Path(base, sb.JS_FILES[0]).unlink()
            with self.assertRaises(sb.BridgeError):
                sb.load_js_sources(base)


if __name__ == "__main__":
    unittest.main()
