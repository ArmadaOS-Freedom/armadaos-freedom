#!/usr/bin/env python3
"""Armada Steam CEF-to-update-service bridge.

Connects to the local Steam client CEF remote debugging endpoint, injects the
packaged Armada updates page scripts into the trusted steamloopback context,
and serves a small validated request bridge to the update service D-Bus client.
No Decky dependency; runs as an unprivileged user process.

Every newly opened websocket is accepted only when the connected loopback peer
is an established socket owned by this non-root user, as recorded in the
network namespace's /proc tcp and tcp6 tables.
"""

from collections import OrderedDict
import ipaddress
import json
import logging
import os
import re
import signal
import sys
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("armada.steam_bridge")

DISCOVERY_URL = "http://127.0.0.1:8080/json"
DISCOVERY_TIMEOUT = 5.0
DISCOVERY_MAX_BYTES = 1024 * 1024
JS_BASE = "/usr/share/armada/steam-updates"
JS_FILES = ("native-client.js", "page.js")
PROC_NET_DIR = "/proc/self/net"
NET_TABLE_MAX_BYTES = 1024 * 1024
TCP_ESTABLISHED = "01"
BINDING_NAME = "__armadaUpdatesRequest"
BINDING_EVENT = "Runtime.bindingCalled"
TRUSTED_ORIGIN = "https://steamloopback.host"
TARGET_TITLES = (
    "Steam",
    "SP",
    "SharedJSContext",
    "Steam Shared Context presented by Valve\u2122",
)
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_ID_LEN = 80
MAX_BINDING_BYTES = 8192
REQUEST_KEYS = {"id", "method", "argument"}
ALLOWED_METHODS = {
    "GetStatus", "StartCheck", "RefreshStatus", "StartSystemUpdate",
    "StartSystemRollback", "SetSystemChannel", "StartDeckyAction",
}
REPLY_JS_TEMPLATE = (
    "window.ArmadaUpdatesPage && "
    "window.ArmadaUpdatesPage.receive(%s);"
)


class BridgeError(Exception):
    pass


def dispatch(request, client):
    """Validate a binding request and forward it to UpdateClient."""
    if not isinstance(request, dict):
        raise BridgeError("request must be an object")
    keys = set(request)
    if not keys <= REQUEST_KEYS or len(keys) != len(request):
        raise BridgeError("unknown or duplicate request keys")
    request_id = request.get("id")
    if (not isinstance(request_id, str) or not request_id
            or len(request_id) > MAX_ID_LEN or not ID_RE.fullmatch(request_id)):
        raise BridgeError("invalid request id")
    method = request.get("method")
    if not isinstance(method, str) or method not in ALLOWED_METHODS:
        raise BridgeError("method is not allowed")
    argument = request.get("argument")
    if method == "GetStatus":
        if argument is not None:
            raise BridgeError("GetStatus takes no argument")
        return {"id": request_id, "ok": True, "result": client.snapshot()}
    # Argument validation mirrors the client allowlist before calling out.
    allowed_args = {
        "StartCheck": ("system", "decky"),
        "RefreshStatus": ("system", "decky"),
        "SetSystemChannel": ("stable", "beta", "testing"),
        "StartDeckyAction": ("update", "repair", "recover"),
    }.get(method)
    if allowed_args is None:
        if method == "StartSystemRollback":
            if (not isinstance(argument, str)
                    or re.fullmatch(r"[0-9a-f]{64}", argument) is None):
                raise BridgeError("invalid rollback checksum")
        elif argument is not None:
            raise BridgeError("method takes no argument")
    elif argument not in allowed_args:
        raise BridgeError("invalid method argument")
    job_id = client.call(method, argument)
    return {"id": request_id, "ok": True, "result": {"jobId": job_id}}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirects disallowed", headers, fp)


def _http_get_discovery():
    handlers = [urllib.request.ProxyHandler({}), _NoRedirectHandler()]  # never use proxies or follow redirects
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(DISCOVERY_URL, method="GET")
    with opener.open(req, timeout=DISCOVERY_TIMEOUT) as resp:
        if resp.status != 200 or resp.geturl() != DISCOVERY_URL:
            raise BridgeError("unexpected discovery response")
        data = resp.read(DISCOVERY_MAX_BYTES + 1)
    if len(data) > DISCOVERY_MAX_BYTES:
        raise BridgeError("discovery payload too large")
    return json.loads(data.decode("utf-8"))


def _valid_target_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.hostname != "steamloopback.host":
        return False
    if port not in (None, 443):
        return False
    path = parsed.path
    if path != "/index.html" and not path.startswith("/routes/"):
        return False
    return True


def _valid_ws_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "ws":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost") or port != 8080:
        return False
    if parsed.query or parsed.fragment:
        return False
    prefix = "/devtools/page/"
    if not parsed.path.startswith(prefix):
        return False
    target_id = parsed.path[len(prefix):]
    return bool(target_id) and bool(ID_RE.fullmatch(target_id))


def discover_target():
    """Return (target_id, ws_url) for a single acceptable Steam page target."""
    pages = _http_get_discovery()
    if not isinstance(pages, list):
        raise BridgeError("discovery payload is not a list")
    best = None
    for page in pages:
        if not isinstance(page, dict):
            continue
        if page.get("type") != "page":
            continue
        title = page.get("title")
        url = page.get("url")
        ws_url = page.get("webSocketDebuggerUrl")
        if title not in TARGET_TITLES:
            continue
        if not _valid_target_url(url):
            continue
        if not _valid_ws_url(ws_url):
            continue
        if best is None or title in ("Steam", "SP"):
            best = (page.get("id"), ws_url)
            if title in ("Steam", "SP"):
                break
    if best is None:
        raise BridgeError("no acceptable Steam target")
    return best


def load_js_sources(base=JS_BASE):
    chunks = []
    total = 0
    for name in JS_FILES:
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            raise BridgeError("missing packaged script: %s" % path)
        with open(path, "r", encoding="utf-8") as handle:
            chunk = handle.read()
        total += len(chunk)
        if total > DISCOVERY_MAX_BYTES:
            raise BridgeError("packaged scripts too large")
        chunks.append(chunk)
    return "\n;\n".join(chunks)


class SteamConnection:
    """One websocket connection to the chosen CEF page target."""

    def __init__(self, ws_factory, client, js_source, stop_event=None):
        self._ws_factory = ws_factory
        self._client = client
        self._js_source = js_source
        self.stop_event = stop_event or threading.Event()
        self._ws = None
        self._next_id = 1
        self._pending = {}  # send id -> callback(ws, result)
        self._contexts = {}  # executionContextId -> seen-request-id deque
        self._root_frame_id = None
        self._binding_ready = False

    def _send(self, method, params, callback=None):
        if self._ws is None:
            raise BridgeError("websocket is closed")
        send_id = self._next_id
        self._next_id += 1
        if callback is not None:
            self._pending[send_id] = callback
        self._ws.send(json.dumps({"id": send_id, "method": method,
                                  "params": params}))
        return send_id

    def _on_reply(self, message):
        msg_id = message.get("id")
        if type(msg_id) is not int:
            return
        callback = self._pending.pop(msg_id, None)
        if callback is None:
            return
        error = message.get("error")
        if error:
            err_msg = error.get("message", "error") if isinstance(error, dict) else str(error)
            raise BridgeError("critical CDP command error: %s" % err_msg)
        callback(self, message.get("result") if isinstance(message.get("result"), dict) else {})

    def _on_event(self, message):
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if method == "Page.frameNavigated":
            frame = params.get("frame")
            if isinstance(frame, dict) and not frame.get("parentId"):
                self._drop_all_contexts()
                frame_id = frame.get("id")
                self._root_frame_id = frame_id if isinstance(frame_id, str) and frame_id else None
        elif method == "Runtime.executionContextCreated":
            self._handle_context_created(params.get("context"))
        elif method == "Runtime.executionContextDestroyed":
            self._drop_context(params.get("executionContextId"))
        elif method == "Runtime.executionContextsCleared":
            self._drop_all_contexts()
        elif method == BINDING_EVENT:
            if params.get("name") == BINDING_NAME:
                self._handle_binding_event(params)

    def _handle_context_created(self, context):
        if not self._binding_ready or not isinstance(context, dict):
            return
        ctx_id = context.get("id")
        origin = context.get("origin")
        if type(ctx_id) is not int or ctx_id <= 0 or origin != TRUSTED_ORIGIN:
            return
        aux_data = context.get("auxData")
        if not isinstance(aux_data, dict):
            return
        if not isinstance(self._root_frame_id, str) or not self._root_frame_id:
            return
        if aux_data.get("isDefault") is not True or aux_data.get("frameId") != self._root_frame_id:
            return  # never run in iframes or non-default contexts
        if ctx_id in self._contexts:
            return
        self._contexts[ctx_id] = OrderedDict()
        try:
            self._send("Runtime.evaluate",
                       {"expression": self._js_source, "contextId": ctx_id,
                        "returnByValue": True})
        except Exception as exc:
            log.warning("script injection failed: %s", exc)

    def _drop_context(self, ctx_id):
        if type(ctx_id) is not int or ctx_id <= 0:
            return
        self._contexts.pop(ctx_id, None)

    def _drop_all_contexts(self):
        self._contexts.clear()

    def _handle_binding_event(self, params):
        ctx_id = params.get("executionContextId")
        if type(ctx_id) is not int or ctx_id <= 0:
            return
        seen = self._contexts.get(ctx_id)
        if seen is None:
            log.debug("binding event from unknown context ignored")
            return
        payload = params.get("payload")
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_BINDING_BYTES:
            log.debug("malformed binding payload ignored")
            return
        def _reject_duplicates(pairs):
            res = {}
            for k, v in pairs:
                if k in res:
                    raise ValueError("duplicate key: %r" % (k,))
                res[k] = v
            return res

        try:
            request = json.loads(payload, object_pairs_hook=_reject_duplicates)
        except (ValueError, UnicodeDecodeError):
            log.debug("binding payload is not valid JSON or contains duplicate keys")
            return
        if not isinstance(request, dict):
            return
        envelope = self._process_request(ctx_id, seen, request)
        if envelope is not None and ctx_id in self._contexts:
            self._reply(ctx_id, envelope)

    def _process_request(self, ctx_id, seen, request):
        request_id = None
        candidate = request.get("id")
        if (isinstance(candidate, str) and candidate
                and len(candidate) <= MAX_ID_LEN and ID_RE.fullmatch(candidate)):
            request_id = candidate
        if request_id is not None and request_id in seen:
            return {"id": request_id, "ok": False,
                    "error": "duplicate request id"}
        try:
            envelope = dispatch(request, self._client)
        except BridgeError as exc:
            envelope = {"id": request_id, "ok": False, "error": str(exc)}
        except Exception as exc:
            log.warning("update dispatch failed: %s", exc)
            envelope = {"id": request_id, "ok": False,
                        "error": "update service unavailable"}
        if isinstance(envelope.get("id"), str):
            seen[request_id] = True
            while len(seen) > 256:
                seen.popitem(last=False)
        return envelope

    def _reply(self, ctx_id, envelope):
        serialized = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True)
        expression = REPLY_JS_TEMPLATE % (serialized,)
        self._send("Runtime.evaluate",
                   {"expression": expression, "contextId": ctx_id,
                    "returnByValue": True})

    def _on_page_enabled(self, _conn, _result=None):
        self._send("Page.getFrameTree", {}, self._on_frame_tree)

    def _on_frame_tree(self, _conn, result):
        frame_tree = result.get("frameTree") if isinstance(result, dict) else {}
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else {}
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        if not isinstance(frame_id, str) or not frame_id:
            raise BridgeError("failed to retrieve root frame id")
        self._root_frame_id = frame_id
        self._send("Runtime.addBinding", {"name": BINDING_NAME}, self._on_binding_added)

    def _on_binding_added(self, _conn, _result=None):
        self._binding_ready = True
        self._send("Runtime.enable", {}, self._on_runtime_enabled)

    def _on_runtime_enabled(self, _conn, _result=None):
        pass

    def run(self):
        """Connect and serve until stop_event is set or the socket drops."""
        import websocket  # python3-websocket-client, lazy import
        self._ws = self._ws_factory()
        try:
            self._send("Page.enable", {}, self._on_page_enabled)
            while not self.stop_event.is_set():
                try:
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise BridgeError("websocket closed")
                self._consume(raw)
        finally:
            ws, self._ws = self._ws, None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            self._pending.clear()  # discard queued callbacks, no replay
            self._drop_all_contexts()
            self._binding_ready = False
            self._root_frame_id = None

    def _consume(self, raw):
        if len(raw) > 2 * 1024 * 1024:
            log.debug("CDP message exceeds 2MiB limit, dropped")
            return
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        if "id" in message:
            self._on_reply(message)
        else:
            self._on_event(message)


def _net_endpoint(token, v6):
    """Decode one local_address:port column of a /proc/net/tcp{,6} table."""
    try:
        raw, raw_port = token.split(":", 1)
        data = bytes.fromhex(raw)
        port = int(raw_port, 16)
    except ValueError:
        raise BridgeError("malformed network table endpoint")
    if v6:
        if len(data) != 16:
            raise BridgeError("malformed network table endpoint")
        data = b"".join(data[i:i + 4][::-1] for i in range(0, 16, 4))
        address = ipaddress.IPv6Address(data)
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    elif len(data) == 4:
        address = ipaddress.IPv4Address(data[::-1])
    else:
        raise BridgeError("malformed network table endpoint")
    if not 0 <= port <= 0xFFFF:
        raise BridgeError("malformed network table endpoint")
    return address, port


def _net_table_rows(text, v6):
    """Yield (local, remote, state, uid) for each row of a tcp table."""
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] == "sl":
            continue
        if len(fields) < 8:
            raise BridgeError("malformed network table row")
        try:
            uid = int(fields[7])
        except ValueError:
            raise BridgeError("malformed network table row")
        yield (_net_endpoint(fields[1], v6), _net_endpoint(fields[2], v6),
               fields[3], uid)


def _read_net_table(v6, missing_ok=False):
    """Read one /proc/net table; None only for a tcp6 absent without IPv6."""
    path = os.path.join(PROC_NET_DIR, "tcp6" if v6 else "tcp")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(NET_TABLE_MAX_BYTES + 1)
    except FileNotFoundError:
        if missing_ok and v6:
            # A kernel without IPv6 has no tcp6 table at all, so no IPv6
            # socket can exist and nothing there can contradict the tcp row.
            return None
        raise BridgeError("network table unavailable: %s" % path)
    except OSError as exc:
        raise BridgeError("network table unavailable: %s" % exc)
    if len(text) > NET_TABLE_MAX_BYTES:
        raise BridgeError("network table too large")
    return text


def _net_table_sources(v6):
    """Return the (is_ipv6, text) tables that can hold the accepted socket.

    A native IPv4 listener records the accepted socket in ``tcp``. A dual-stack
    IPv6 listener accepts the same ordinary IPv4 client through an IPv6 socket,
    and that accepted row then exists only in ``tcp6`` with the IPv4 endpoints
    stored as IPv4-mapped addresses. Both tables are therefore consulted for an
    IPv4 peer; an IPv6 peer can only ever be recorded in ``tcp6``.
    """
    if v6:
        return ((True, _read_net_table(True)),)
    return ((False, _read_net_table(False)),
            (True, _read_net_table(True, missing_ok=True)))


def _established_owners(peer, local, text, v6):
    """Collect owners of established rows that hold the reversed tuple."""
    return [uid for row_local, row_remote, state, uid in _net_table_rows(text, v6)
            if state == TCP_ESTABLISHED and row_local == peer and row_remote == local]


def _verify_peer(peer, local, sources, euid):
    """Require exactly one established same-uid socket across the proc tables.

    The bridge owns the client socket (local -> peer); the CDP server owns the
    accepting socket (peer -> local). Only that established server-side row is
    evidence; a listening socket alone says nothing about the connected peer.
    Endpoints are canonicalized, so the IPv4-mapped row of a dual-stack
    listener matches the same reversed tuple as a native IPv4 row, and matches
    from every source are counted together: exactly one owner must be found,
    and it must be the expected uid. A ``None`` source is a tcp6 table absent
    because the kernel has no IPv6; any other read failure raises before it can
    be mistaken for a match. This is a same-uid boundary check only: it does
    not attest the peer binary.
    """
    if not isinstance(euid, int) or euid <= 0:
        raise BridgeError("peer ownership check requires a non-root uid")
    owners = []
    for v6, text in sources:
        if text is not None:
            owners.extend(_established_owners(peer, local, text, v6))
    if not owners:
        raise BridgeError("no established socket for the connected peer")
    if len(owners) > 1:
        raise BridgeError("ambiguous established socket for the connected peer")
    if owners[0] != euid:
        raise BridgeError("connected peer socket is owned by another user")


def _loopback_endpoint(address):
    """Canonicalize a socket endpoint tuple and require loopback."""
    try:
        host = ipaddress.ip_address(address[0])
        port = address[1]
    except (IndexError, TypeError, ValueError):
        raise BridgeError("malformed socket endpoint")
    if host.version == 6 and host.ipv4_mapped is not None:
        host = host.ipv4_mapped
    if not host.is_loopback:
        raise BridgeError("websocket peer is not loopback")
    if not isinstance(port, int) or not 0 < port <= 0xFFFF:
        raise BridgeError("malformed socket endpoint")
    return host, port


def _verify_ws_peer(ws):
    """Reject the websocket unless its connected peer is a same-uid loopback socket."""
    sock = getattr(ws, "sock", None)
    if sock is None:
        raise BridgeError("websocket has no socket")
    try:
        peer = _loopback_endpoint(sock.getpeername())
        local = _loopback_endpoint(sock.getsockname())
    except OSError as exc:
        raise BridgeError("websocket endpoint unavailable: %s" % exc)
    if peer[0].version != local[0].version:
        raise BridgeError("websocket endpoints use different address families")
    _verify_peer(peer, local, _net_table_sources(peer[0].version == 6),
                 os.geteuid())


def _default_ws_factory(ws_url):
    import websocket

    def factory():
        ws = websocket.WebSocket()
        try:
            ws.connect(
                ws_url,
                timeout=DISCOVERY_TIMEOUT,
                http_no_proxy=["localhost", "127.0.0.1"],
                suppress_origin=True,
            )
            ws.settimeout(1.0)
            _verify_ws_peer(ws)  # never hand an unverified peer to SteamConnection
            return ws
        except Exception:
            try:
                ws.close()
            except Exception:
                pass
            raise

    return factory


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if os.geteuid() == 0:
        log.error("refusing to run as root")
        return 1
    for path in (os.path.join(JS_BASE, name) for name in JS_FILES):
        if not os.path.isfile(path):
            log.error("missing required file: %s", path)
            return 1
    from updates.client import UpdateClient
    client = UpdateClient()
    try:
        js_source = load_js_sources()
    except BridgeError as exc:
        log.error("%s", exc)
        return 1
    stop_event = threading.Event()

    def _stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop_event.is_set():
        try:
            _target_id, ws_url = discover_target()
            connection = SteamConnection(_default_ws_factory(ws_url), client,
                                         js_source, stop_event)
            connection.run()
        except Exception as exc:
            log.info("bridge cycle ended (%s); rediscovering", exc)
        if not stop_event.is_set():
            stop_event.wait(5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
