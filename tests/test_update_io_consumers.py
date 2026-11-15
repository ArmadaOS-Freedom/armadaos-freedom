"""Strict-JSON contracts at the public entry points of the remaining consumers.

Each case reaches a real domain entry with duplicate-key or non-finite input and
requires that entry's own failure type, so the shared helper can never silently
widen or narrow a caller's accepted document.
"""

from collections import OrderedDict
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ARMADA_LIB = Path(__file__).resolve().parents[1] / "system_files" / "usr" / "lib" / "armada"
if str(ARMADA_LIB) not in sys.path:
    sys.path.insert(0, str(ARMADA_LIB))

from updates import client as update_client
from updates import offline_execute, recovery_layout, steam_bridge


class ClientStatusTest(unittest.TestCase):
    def test_duplicate_and_nonfinite_status_are_runtime_errors(self):
        for payload in ('{"schemaVersion":1,"components":{ },"components":{}}',
                        '{"schemaVersion":1,"components":{},"drift":NaN}'):
            with self.subTest(payload=payload):
                cli = update_client.UpdateClient()
                with mock.patch.object(cli, "call", return_value=payload):
                    with self.assertRaises(RuntimeError):
                        cli.snapshot()

    def test_finite_status_is_returned_unchanged(self):
        document = {"schemaVersion": 1, "components": {"system": {"busy": False}}}
        cli = update_client.UpdateClient()
        with mock.patch.object(cli, "call", return_value=json.dumps(document)):
            self.assertEqual(cli.snapshot(), document)


class RecoveryLayoutHeaderTest(unittest.TestCase):
    @staticmethod
    def _header(text):
        raw = text.encode("utf-8")
        return raw + b" " * (recovery_layout.REGION_BYTES - len(raw))

    def test_duplicate_and_nonfinite_metadata_are_layout_errors(self):
        for text in ('{"installationId":"a","installationId":"b"}',
                     '{"installationId":"a","imageBytes":NaN}'):
            with self.subTest(text=text):
                with self.assertRaises(recovery_layout.RecoveryLayoutError):
                    recovery_layout.parse_layout(
                        self._header(text),
                        b"\x00" * recovery_layout.REGION_BYTES,
                        "11111111-2222-4333-8444-555555555555",
                        1,
                    )


class OfflineExecuteDocumentTest(unittest.TestCase):
    def test_duplicate_and_nonfinite_documents_raise_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(os.path.realpath(tmp))
            for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
                with self.subTest(raw=raw):
                    (work / "plan.json").write_bytes(raw)
                    with self.assertRaises(ValueError):
                        offline_execute._load_json(work, "plan.json")

    def test_valid_finite_document_is_returned_with_its_raw_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(os.path.realpath(tmp))
            document = {"schemaVersion": 1, "value": 1.5, "items": [True, None]}
            raw = json.dumps(document, separators=(",", ":")).encode("ascii")
            (work / "plan.json").write_bytes(raw)
            self.assertEqual(
                offline_execute._load_json(work, "plan.json"), (document, raw))


class SteamBridgeBindingTest(unittest.TestCase):
    @staticmethod
    def _connection(client):
        connection = steam_bridge.SteamConnection(
            ws_factory=lambda: None, client=client, js_source="")
        connection._contexts[7] = OrderedDict()
        return connection

    def test_duplicate_and_nonfinite_payloads_never_reach_dispatch(self):
        client = mock.Mock()
        connection = self._connection(client)
        for payload in ('{"id":"a","id":"b"}',
                        '{"id":"a","method":"GetStatus","argument":NaN}'):
            with self.subTest(payload=payload):
                connection._handle_binding_event(
                    {"executionContextId": 7, "payload": payload})
        client.snapshot.assert_not_called()
        client.call.assert_not_called()

    def test_valid_finite_payload_is_dispatched_once(self):
        client = mock.Mock()
        client.snapshot.return_value = {"schemaVersion": 1, "components": {}}
        connection = self._connection(client)
        connection._ws = mock.Mock()
        connection._handle_binding_event({
            "executionContextId": 7,
            "payload": '{"id":"r1","method":"GetStatus"}',
        })
        client.snapshot.assert_called_once_with()
        connection._ws.send.assert_called_once()
        expression = json.loads(connection._ws.send.call_args.args[0])["params"]["expression"]
        self.assertIn('"ok":true', expression)
        self.assertIn('"id":"r1"', expression)


if __name__ == "__main__":
    unittest.main()
