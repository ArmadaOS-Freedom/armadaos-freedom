#!/usr/bin/env python3

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates.client import UpdateClient
from updates.coordinator import Coordinator
from updates import dbus_service, legacy_cli
from updates.dbus_service import (
    BUS_NAME,
    IFACE,
    PATH,
    PolkitAuthorizer,
    UpdateDBusService,
)

POLICY_BUS_NAME = "org.freedesktop.PolicyKit1"
POLICY_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLICY_IFACE = "org.freedesktop.PolicyKit1.Authority"
POLICY_XML = """
<node>
  <interface name="org.freedesktop.PolicyKit1.Authority">
    <method name="CheckAuthorization">
      <arg type="(sa{sv})" name="subject" direction="in"/>
      <arg type="s" name="action_id" direction="in"/>
      <arg type="a{ss}" name="details" direction="in"/>
      <arg type="u" name="flags" direction="in"/>
      <arg type="s" name="cancellation_id" direction="in"/>
      <arg type="(bba{ss})" name="result" direction="out"/>
    </method>
  </interface>
</node>
"""


class FakeManager:
    def __init__(self, component):
        self.component = component
        self.calls = []
        self.selected_channels = []
        self.rollback_checksums = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()

    def block(self):
        self.entered.clear()
        self.gate.clear()

    def release(self):
        self.gate.set()

    def _run(self, method_name, state, phase, target=None):
        self.calls.append(method_name)
        self.entered.set()
        if not self.gate.wait(2.0):
            raise TimeoutError(f"{self.component}.{method_name} gate timed out")
        return {
            "component": self.component,
            "state": state,
            "phase": phase,
            "currentVersion": "1.0.0",
            "targetVersion": target,
            "requiresReboot": False,
        }

    def status(self):
        return self._run("status", "up_to_date", "status")

    def check(self):
        return self._run("check", "update_available", "check", "1.1.0")

    def apply(self):
        return self._run("apply", "updated", "update", "1.1.0")

    def repair(self):
        return self._run("repair", "up_to_date", "repair")

    def recover(self):
        return self._run("recover", "up_to_date", "recover")

    def select_channel(self, channel):
        result = self._run("select_channel", "installed", "select_channel")
        result["channel"] = channel
        self.selected_channels.append(channel)
        return result

    def rollback(self, checksum):
        result = self._run("rollback", "awaiting_reboot", "awaiting_reboot")
        result.update(
            requiresReboot=True,
            bootWriteState="written",
            bootPolicy="dual-boot-no-slot-control",
            slotControlState="not_issued",
        )
        self.rollback_checksums.append(checksum)
        return result


class FakePolicyAuthority:
    def __init__(self, connection):
        self.connection = connection
        self.allowed = True
        self.challenge = False
        self.error_name = None
        self.requests = []
        node = Gio.DBusNodeInfo.new_for_xml(POLICY_XML)
        self.registration_id = connection.register_object(
            POLICY_PATH,
            node.interfaces[0],
            self._on_method_call,
            None,
            None,
        )

    def _on_method_call(
        self,
        connection,
        sender,
        object_path,
        interface_name,
        method_name,
        parameters,
        invocation,
    ):
        subject, action_id, details, flags, cancellation_id = parameters.unpack()
        self.requests.append(
            {
                "subject": subject,
                "action": action_id,
                "details": details,
                "flags": flags,
                "cancellation_id": cancellation_id,
            }
        )
        if self.error_name:
            invocation.return_dbus_error(self.error_name, " simulated policy error")
            return
        invocation.return_value(
            GLib.Variant("((bba{ss}))", ((self.allowed, self.challenge, {}),))
        )

    def close(self):
        if self.registration_id:
            self.connection.unregister_object(self.registration_id)
            self.registration_id = 0


class UpdateDBusTest(unittest.TestCase):
    def setUp(self):
        self.proc = subprocess.Popen(
            ["dbus-daemon", "--session", "--nofork", "--print-address"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._cleanup_bus_process)
        self.bus_address = self.proc.stdout.readline().strip()
        self.assertTrue(self.bus_address, "dbus-daemon did not print an address")

        self.connections = []
        self.addCleanup(self._cleanup_connections)

        self.loop = GLib.MainLoop()
        self.loop_thread = threading.Thread(target=self.loop.run, daemon=True)
        self.loop_thread.start()
        self.addCleanup(self._cleanup_loop)

        self.policy_conn = self._new_connection()
        self._request_name(self.policy_conn, POLICY_BUS_NAME)
        self.policy = FakePolicyAuthority(self.policy_conn)
        self.addCleanup(self.policy.close)

        self.system_mgr = FakeManager("system")
        self.decky_mgr = FakeManager("decky")

        self.server_conn = self._new_connection()
        self._request_name(self.server_conn, BUS_NAME)
        self.coordinator = Coordinator(system=self.system_mgr, decky=self.decky_mgr)
        self.authorizer = PolkitAuthorizer(self.server_conn)
        self.service = UpdateDBusService(
            self.server_conn,
            coordinator=self.coordinator,
            authorizer=self.authorizer,
        )
        self.addCleanup(self.service.close)
        self.addCleanup(self.system_mgr.release)
        self.addCleanup(self.decky_mgr.release)

        self.client_conn = self._new_connection()

    def _new_connection(self):
        conn = Gio.DBusConnection.new_for_address_sync(
            self.bus_address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.connections.append(conn)
        return conn

    def _request_name(self, conn, name):
        reply = conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (name, 4)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self.assertEqual(reply.unpack()[0], 1)

    def _cleanup_loop(self):
        self.loop.quit()
        self.loop_thread.join(2.0)
        self.assertFalse(self.loop_thread.is_alive(), "GLib.MainLoop did not stop")

    def _cleanup_connections(self):
        for conn in reversed(self.connections):
            if not conn.is_closed():
                conn.close_sync(None)

    def _cleanup_bus_process(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()

    def _call(self, method, params=None, conn=None):
        target_conn = conn or self.client_conn
        variant = GLib.Variant("(s)", (params,)) if params is not None else None
        reply = target_conn.call_sync(
            BUS_NAME,
            PATH,
            IFACE,
            method,
            variant,
            GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return reply.unpack()[0]

    def _get_status(self, conn=None):
        return json.loads(self._call("GetStatus", conn=conn))

    def _wait_job(self, component, job_id, conn=None):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            comp = self._get_status(conn=conn)["components"][component]
            if not comp["busy"] and comp["jobId"] == job_id:
                return comp
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {component} job {job_id}")

    def test_1_initial_get_status_without_auth_or_manager_calls(self):
        status = self._get_status()
        self.assertEqual(status["schemaVersion"], 1)
        self.assertEqual(set(status["components"].keys()), {"system", "decky"})
        for name in ("system", "decky"):
            comp = status["components"][name]
            self.assertFalse(comp["busy"])
            self.assertIsNone(comp["jobId"])
            self.assertIsNone(comp["operation"])
            self.assertEqual(comp["result"]["component"], name)
            self.assertEqual(comp["result"]["state"], "not_checked")
        self.assertEqual(self.policy.requests, [])
        self.assertEqual(self.system_mgr.calls, [])
        self.assertEqual(self.decky_mgr.calls, [])

    def test_2_refresh_and_start_check_operations(self):
        jid1 = self._call("RefreshStatus", "system")
        comp1 = self._wait_job("system", jid1)
        self.assertEqual(comp1["operation"], "status")
        self.assertEqual(comp1["result"]["state"], "up_to_date")

        jid2 = self._call("StartCheck", "system")
        comp2 = self._wait_job("system", jid2)
        self.assertEqual(comp2["operation"], "check")
        self.assertEqual(comp2["result"]["state"], "update_available")

        jid3 = self._call("StartCheck", "decky")
        comp3 = self._wait_job("decky", jid3)
        self.assertEqual(comp3["operation"], "check")
        self.assertEqual(comp3["result"]["targetVersion"], "1.1.0")

        self.assertEqual(self.system_mgr.calls, ["status", "check"])
        self.assertEqual(self.decky_mgr.calls, ["check"])
        self.assertEqual(
            [r["action"] for r in self.policy.requests],
            ["org.armada.updates.check"] * 3,
        )

    def test_3_decky_actions_and_polkit_subject_details(self):
        expected_calls = []
        for op, method, state in (
            ("update", "apply", "updated"),
            ("repair", "repair", "up_to_date"),
            ("recover", "recover", "up_to_date"),
        ):
            jid = self._call("StartDeckyAction", op)
            comp = self._wait_job("decky", jid)
            self.assertEqual(comp["operation"], op)
            self.assertEqual(comp["result"]["state"], state)
            self.assertEqual(comp["result"]["phase"], op)
            expected_calls.append(method)
            self.assertEqual(self.decky_mgr.calls, expected_calls)

        self.assertEqual(len(self.policy.requests), 3)
        for req in self.policy.requests:
            self.assertEqual(req["action"], "org.armada.updates.decky")
            self.assertEqual(
                req["subject"],
                ("system-bus-name", {"name": self.client_conn.get_unique_name()}),
            )
            self.assertEqual(req["details"], {})
            self.assertEqual(req["flags"], 0)
            self.assertEqual(req["cancellation_id"], "")

    def test_4_policy_deny_and_challenge_only_rejected(self):
        for allowed, challenge in ((False, False), (False, True)):
            self.policy.allowed = allowed
            self.policy.challenge = challenge
            with self.assertRaises(GLib.Error) as ctx:
                self._call("StartCheck", "system")
            self.assertIn("org.freedesktop.DBus.Error.AccessDenied", str(ctx.exception))
        self.assertEqual(self.system_mgr.calls, [])
        self.assertEqual(self.decky_mgr.calls, [])

    def test_5_policy_dbus_error_denies_and_clears_auth_pending(self):
        self.policy.error_name = "org.freedesktop.PolicyKit1.Error.Failed"
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartDeckyAction", "update")
        self.assertIn("org.freedesktop.DBus.Error.AccessDenied", str(ctx.exception))
        self.assertEqual(self.service.auth_pending, 0)
        self.assertEqual(self.decky_mgr.calls, [])

    def test_6_invalid_arguments_and_unknown_method_rejected_before_auth(self):
        for method, arg in (
            ("StartCheck", "invalid"),
            ("RefreshStatus", "invalid"),
            ("StartDeckyAction", "invalid"),
        ):
            with self.assertRaises(GLib.Error) as ctx:
                self._call(method, arg)
            self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))

        with self.assertRaises(GLib.Error) as ctx:
            self._call("NoSuchMethod")
        self.assertIn("org.freedesktop.DBus.Error.UnknownMethod", str(ctx.exception))
        self.assertEqual(self.policy.requests, [])

    def test_7_busy_component_isolation_and_responsive_status(self):
        self.decky_mgr.block()
        decky_jid = self._call("StartCheck", "decky")
        self.assertTrue(self.decky_mgr.entered.wait(2.0))

        status = self._get_status()
        self.assertTrue(status["components"]["decky"]["busy"])
        self.assertEqual(status["components"]["decky"]["jobId"], decky_jid)

        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartDeckyAction", "update")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))

        sys_jid = self._call("StartCheck", "system")
        sys_comp = self._wait_job("system", sys_jid)
        self.assertEqual(sys_comp["result"]["state"], "update_available")

        self.decky_mgr.release()
        decky_comp = self._wait_job("decky", decky_jid)
        self.assertEqual(decky_comp["result"]["state"], "update_available")

    def test_8_job_persists_after_client_disconnect(self):
        self.decky_mgr.block()
        job_id = self._call("StartCheck", "decky")
        self.assertTrue(self.decky_mgr.entered.wait(2.0))

        self.client_conn.close_sync(None)
        self.assertTrue(self.client_conn.is_closed())

        second_client = self._new_connection()
        self.decky_mgr.release()

        comp = self._wait_job("decky", job_id, conn=second_client)
        self.assertEqual(comp["jobId"], job_id)
        self.assertEqual(comp["result"]["state"], "update_available")
        self.assertEqual(self.decky_mgr.calls, ["check"])

    def test_9_system_update_zero_args_and_polkit_subject(self):
        job_id = self._call("StartSystemUpdate")
        comp = self._wait_job("system", job_id)
        self.assertEqual(comp["operation"], "update")
        self.assertEqual(comp["result"]["state"], "updated")
        self.assertEqual(comp["result"]["phase"], "update")
        self.assertEqual(self.system_mgr.calls, ["apply"])
        self.assertEqual(len(self.policy.requests), 1)
        request = self.policy.requests[0]
        self.assertEqual(request["action"], "org.armada.updates.system")
        self.assertEqual(
            request["subject"],
            ("system-bus-name", {"name": self.client_conn.get_unique_name()}),
        )

    def test_10_system_update_denied_and_string_arg_rejected(self):
        for challenge in (False, True):
            self.policy.allowed = False
            self.policy.challenge = challenge
            with self.assertRaises(GLib.Error) as ctx:
                self._call("StartSystemUpdate")
            self.assertIn("org.freedesktop.DBus.Error.AccessDenied", str(ctx.exception))
            self.assertEqual(self.service.auth_pending, 0)
        self.assertEqual(self.system_mgr.calls, [])
        requests = len(self.policy.requests)
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemUpdate", "system")
        self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))
        self.assertEqual(len(self.policy.requests), requests)
        self.assertEqual(self.system_mgr.calls, [])
        self.assertEqual(self.service.auth_pending, 0)

    def test_11_system_update_busy_status_and_decky_isolation(self):
        self.system_mgr.block()
        job_id = self._call("StartSystemUpdate")
        self.assertTrue(self.system_mgr.entered.wait(2.0))
        status = self._get_status()
        self.assertTrue(status["components"]["system"]["busy"])
        self.assertEqual(status["components"]["system"]["jobId"], job_id)
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemUpdate")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        decky_job = self._call("StartCheck", "decky")
        decky = self._wait_job("decky", decky_job)
        self.assertEqual(decky["result"]["state"], "update_available")
        self.system_mgr.release()
        comp = self._wait_job("system", job_id)
        self.assertEqual(comp["result"]["state"], "updated")
        self.assertEqual(self.system_mgr.calls, ["apply"])
        self.assertEqual(self.decky_mgr.calls, ["check"])

    def test_12_set_system_channel_result_and_authorization(self):
        for channel in ("stable", "beta", "testing"):
            job_id = self._call("SetSystemChannel", channel)
            comp = self._wait_job("system", job_id)
            self.assertEqual(comp["result"]["channel"], channel)
            self.assertEqual(comp["result"]["state"], "installed")
            self.assertEqual(comp["result"]["phase"], "select_channel")
            self.assertIsNone(comp["result"]["targetVersion"])
            self.assertFalse(comp["result"]["requiresReboot"])
        self.assertEqual(self.system_mgr.calls, ["select_channel"] * 3)
        self.assertEqual(self.system_mgr.selected_channels, ["stable", "beta", "testing"])
        self.assertEqual(self.decky_mgr.calls, [])
        self.assertEqual(len(self.policy.requests), 3)
        for request in self.policy.requests:
            self.assertEqual(request["action"], "org.armada.updates.channel")
            self.assertEqual(
                request["subject"],
                ("system-bus-name", {"name": self.client_conn.get_unique_name()}),
            )

    def test_13_set_system_channel_invalid_denied_and_wrong_arity(self):
        for channel in ("", "preview", "nightly", "stable;id"):
            with self.assertRaises(GLib.Error) as ctx:
                self._call("SetSystemChannel", channel)
            self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))
        self.assertEqual(self.policy.requests, [])
        for challenge in (False, True):
            self.policy.allowed = False
            self.policy.challenge = challenge
            with self.assertRaises(GLib.Error) as ctx:
                self._call("SetSystemChannel", "beta")
            self.assertIn("org.freedesktop.DBus.Error.AccessDenied", str(ctx.exception))
            self.assertEqual(self.service.auth_pending, 0)
        requests = len(self.policy.requests)
        with self.assertRaises(GLib.Error) as ctx:
            self._call("SetSystemChannel")
        self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))
        self.assertEqual(len(self.policy.requests), requests)
        self.assertEqual(self.system_mgr.calls, [])
        self.assertEqual(self.service.auth_pending, 0)

    def test_14_channel_selection_busy_and_decky_isolation(self):
        self.system_mgr.block()
        update_job = self._call("StartSystemUpdate")
        self.assertTrue(self.system_mgr.entered.wait(2.0))
        with self.assertRaises(GLib.Error) as ctx:
            self._call("SetSystemChannel", "beta")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        self.system_mgr.release()
        self._wait_job("system", update_job)
        self.system_mgr.block()
        channel_job = self._call("SetSystemChannel", "testing")
        self.assertTrue(self.system_mgr.entered.wait(2.0))
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemUpdate")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        decky_job = self._call("StartCheck", "decky")
        decky = self._wait_job("decky", decky_job)
        self.assertEqual(decky["result"]["state"], "update_available")
        self.system_mgr.release()
        comp = self._wait_job("system", channel_job)
        self.assertEqual(comp["result"]["channel"], "testing")
        self.assertEqual(self.system_mgr.calls, ["apply", "select_channel"])
        self.assertEqual(self.system_mgr.selected_channels, ["testing"])
        self.assertEqual(self.decky_mgr.calls, ["check"])

    def test_15_private_client_operations_and_authorization(self):
        client = UpdateClient(connection=self.client_conn, poll_interval=0.01)
        status = client.run("system", "status")
        self.assertEqual(status["state"], "up_to_date")
        check = client.run("system", "check")
        self.assertEqual(check["state"], "update_available")
        selected = client.run("system", "select_channel", channel="beta")
        self.assertEqual(selected["channel"], "beta")
        repaired = client.run("decky", "repair")
        self.assertEqual(repaired["phase"], "repair")
        self.assertEqual(self.system_mgr.calls, ["status", "check", "select_channel"])
        self.assertEqual(self.system_mgr.selected_channels, ["beta"])
        self.assertEqual(self.decky_mgr.calls, ["repair"])
        self.assertEqual(
            [request["action"] for request in self.policy.requests],
            ["org.armada.updates.check", "org.armada.updates.check",
             "org.armada.updates.channel", "org.armada.updates.decky"],
        )

    def test_16_legacy_cli_real_bus_complete_and_uncertain_apply(self):
        client = UpdateClient(connection=self.client_conn, poll_interval=0.01)

        def status():
            return self.system_mgr._run("status", "installed", "status")

        def completed():
            result = self.system_mgr._run("apply", "awaiting_reboot", "update", "1.1.0")
            result.update(requiresReboot=True, bootWriteState="written",
                          bootPolicy="dual-boot-no-slot-control",
                          slotControlState="not_issued")
            return result

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(self.system_mgr, "status", side_effect=status), \
                mock.patch.object(self.system_mgr, "apply", side_effect=completed), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(legacy_cli.main([], client=client), 0)
        self.assertEqual(stdout.getvalue().splitlines(), ["0%", "100%"])

        def uncertain():
            result = self.system_mgr._run("apply", "awaiting_reboot", "update", "1.1.0")
            result.update(requiresReboot=True, bootWriteState="written",
                          bootPolicy="legacy-single-slot",
                          slotControlState="not_issued")
            return result

        stdout = io.StringIO()
        with mock.patch.object(self.system_mgr, "status", side_effect=status), \
                mock.patch.object(self.system_mgr, "apply", side_effect=uncertain), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(legacy_cli.main([], client=client), 7)
        self.assertNotIn("100%", stdout.getvalue())
        self.assertEqual(self.system_mgr.calls, ["status", "apply", "status", "apply"])

    def test_17_private_client_busy_disconnect_and_job_completion(self):
        self.system_mgr.block()
        first = UpdateClient(connection=self.client_conn, poll_interval=0.01)
        job_id = first.call("StartSystemUpdate")
        self.assertTrue(self.system_mgr.entered.wait(2.0))
        competing = UpdateClient(connection=self._new_connection(), poll_interval=0.01)
        with self.assertRaises(GLib.Error) as ctx:
            competing.run("system", "update")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        self.assertEqual(self.system_mgr.calls, ["apply"])

        self.client_conn.close_sync(None)
        observer = UpdateClient(connection=self._new_connection(), poll_interval=0.01)
        record = observer.snapshot()["components"]["system"]
        self.assertTrue(record["busy"])
        self.assertEqual(record["jobId"], job_id)
        self.assertEqual(record["operation"], "update")
        self.system_mgr.release()
        deadline = time.monotonic() + 2.0
        while record["busy"] and time.monotonic() < deadline:
            time.sleep(0.02)
            record = observer.snapshot()["components"]["system"]
        self.assertFalse(record["busy"])
        self.assertEqual(record["jobId"], job_id)
        self.assertEqual(record["result"]["state"], "updated")
        self.assertEqual(self.system_mgr.calls, ["apply"])

    def test_18_system_rollback_dbus_auth_validation_busy_and_client(self):
        checksum = "a" * 64
        other_checksum = "b" * 64
        job_id = self._call("StartSystemRollback", checksum)
        comp = self._wait_job("system", job_id)
        self.assertEqual(comp["operation"], "rollback")
        self.assertEqual(comp["result"]["state"], "awaiting_reboot")
        self.assertEqual(comp["result"]["phase"], "awaiting_reboot")
        self.assertTrue(comp["result"]["requiresReboot"])
        self.assertEqual(comp["result"]["bootWriteState"], "written")
        self.assertEqual(comp["result"]["bootPolicy"], "dual-boot-no-slot-control")
        self.assertEqual(comp["result"]["slotControlState"], "not_issued")
        self.assertEqual(self.system_mgr.calls, ["rollback"])
        self.assertEqual(self.system_mgr.rollback_checksums, [checksum])
        self.assertEqual(len(self.policy.requests), 1)
        self.assertEqual(self.policy.requests[0]["action"], "org.armada.updates.system")
        self.assertEqual(
            self.policy.requests[0]["subject"],
            ("system-bus-name", {"name": self.client_conn.get_unique_name()}),
        )

        for bad in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
            with self.assertRaises(GLib.Error) as ctx:
                self._call("StartSystemRollback", bad)
            self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemRollback")
        self.assertIn("org.freedesktop.DBus.Error.InvalidArgs", str(ctx.exception))
        self.assertEqual(len(self.policy.requests), 1)
        self.assertEqual(self.system_mgr.calls, ["rollback"])

        for challenge in (False, True):
            self.policy.allowed = False
            self.policy.challenge = challenge
            with self.assertRaises(GLib.Error) as ctx:
                self._call("StartSystemRollback", checksum)
            self.assertIn("org.freedesktop.DBus.Error.AccessDenied", str(ctx.exception))
            self.assertEqual(self.service.auth_pending, 0)
        self.policy.allowed = True
        self.policy.challenge = False
        self.assertEqual(self.system_mgr.calls, ["rollback"])
        self.assertEqual(self.system_mgr.rollback_checksums, [checksum])

        self.system_mgr.block()
        busy_job = self._call("StartSystemRollback", checksum)
        self.assertTrue(self.system_mgr.entered.wait(2.0))
        status = self._get_status()["components"]["system"]
        self.assertTrue(status["busy"])
        self.assertEqual(status["jobId"], busy_job)
        self.assertEqual(status["operation"], "rollback")
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemUpdate")
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        with self.assertRaises(GLib.Error) as ctx:
            self._call("StartSystemRollback", other_checksum)
        self.assertIn("org.armada.Updates.Error.Busy", str(ctx.exception))
        decky_job = self._call("StartCheck", "decky")
        decky = self._wait_job("decky", decky_job)
        self.assertEqual(decky["result"]["state"], "update_available")
        self.system_mgr.release()
        self._wait_job("system", busy_job)

        client = UpdateClient(connection=self.client_conn, poll_interval=0.01)
        res = client.run("system", "rollback", checksum=other_checksum)
        self.assertEqual(res["state"], "awaiting_reboot")
        self.assertEqual(res["phase"], "awaiting_reboot")
        self.assertTrue(res["requiresReboot"])
        self.assertEqual(self.system_mgr.calls, ["rollback", "rollback", "rollback"])
        self.assertEqual(
            self.system_mgr.rollback_checksums,
            [checksum, checksum, other_checksum],
        )
        self.assertEqual(self.decky_mgr.calls, ["check"])


class UpdateDBusActivationTest(unittest.TestCase):
    def test_activation_queues_no_synthetic_jobs(self):
        starts = []

        class FakeCoordinator:
            def start(self, component, operation):
                starts.append((component, operation))
                return "unexpected-job"

            def close(self):
                pass

        class FakeService:
            def __init__(self, connection):
                self.connection = connection
                self.coordinator = FakeCoordinator()
                self.closed = False

            def close(self):
                self.closed = True

        class FakeLoop:
            def run(self):
                pass

            def quit(self):
                pass

        bus_calls = []

        class FakeConnection:
            def call_sync(self, bus_name, path, iface, method, *args):
                bus_calls.append(method)
                return (1,)

        connection = FakeConnection()
        with mock.patch.object(dbus_service.os, "geteuid", return_value=0), \
                mock.patch.object(
                    dbus_service.Gio, "bus_get_sync", return_value=connection), \
                mock.patch.object(
                    dbus_service, "UpdateDBusService", FakeService), \
                mock.patch.object(
                    dbus_service.GLib, "MainLoop", return_value=FakeLoop()), \
                mock.patch.object(
                    dbus_service.GLib, "unix_signal_add", return_value=1):
            dbus_service.main()

        self.assertEqual(starts, [])
        self.assertEqual(bus_calls[0], "RequestName")
        self.assertEqual(bus_calls[-1], "ReleaseName")


if __name__ == "__main__":
    unittest.main()
