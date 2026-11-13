#!/usr/bin/env python3

import json
import os
import re
import signal

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from .coordinator import BusyError, Coordinator


BUS_NAME = "org.armada.Updates"
PATH = "/org/armada/Updates"
IFACE = "org.armada.Updates1"

INTROSPECTION_XML = """
<node>
  <interface name="org.armada.Updates1">
    <method name="GetStatus">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="StartCheck">
      <arg type="s" name="component" direction="in"/>
      <arg type="s" name="jobid" direction="out"/>
    </method>
    <method name="RefreshStatus">
      <arg type="s" name="component" direction="in"/>
      <arg type="s" name="jobid" direction="out"/>
    </method>
    <method name="StartSystemUpdate">
      <arg type="s" name="jobid" direction="out"/>
    </method>
    <method name="StartSystemRollback">
      <arg type="s" name="checksum" direction="in"/>
      <arg type="s" name="jobid" direction="out"/>
    </method>
    <method name="SetSystemChannel">
      <arg type="s" name="channel" direction="in"/>
      <arg type="s" name="jobid" direction="out"/>
    </method>
    <method name="StartDeckyAction">
      <arg type="s" name="operation" direction="in"/>
      <arg type="s" name="jobid" direction="out"/>
    </method>
  </interface>
</node>
"""

INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"
ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
BUSY_ERROR = "org.armada.Updates.Error.Busy"
FAILED_ERROR = "org.armada.Updates.Error.Failed"
UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
MAX_AUTH_PENDING = 16


class PolkitAuthorizer:
    def __init__(self, connection):
        self.connection = connection

    def check(self, sender, action, callback):
        if not isinstance(sender, str) or len(sender) < 2 or not sender.startswith(":"):
            callback(False, "Invalid unique bus sender name")
            return

        subject = (
            "system-bus-name",
            {"name": GLib.Variant("s", sender)},
        )
        parameters = GLib.Variant(
            "((sa{sv})sa{ss}us)",
            (subject, action, {}, 0, ""),
        )
        self.connection.call(
            "org.freedesktop.PolicyKit1",
            "/org/freedesktop/PolicyKit1/Authority",
            "org.freedesktop.PolicyKit1.Authority",
            "CheckAuthorization",
            parameters,
            GLib.VariantType("((bba{ss}))"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            self._finished,
            callback,
        )

    @staticmethod
    def _finished(connection, result, callback):
        try:
            reply = connection.call_finish(result)
            allowed = bool(reply.unpack()[0][0])
            error = None
        except Exception as exc:
            allowed = False
            error = str(exc)
        callback(allowed, error)


class UpdateDBusService:
    def __init__(self, connection, coordinator=None, authorizer=None):
        self.connection = connection
        self.coordinator = coordinator if coordinator is not None else Coordinator()
        self.authorizer = (
            authorizer if authorizer is not None else PolkitAuthorizer(connection)
        )
        self.closed = False
        self.auth_pending = 0
        node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self.registration_id = connection.register_object(
            PATH,
            node_info.interfaces[0],
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
        if self.closed:
            invocation.return_dbus_error(FAILED_ERROR, "Service is closing")
            return

        if method_name == "GetStatus":
            try:
                status = json.dumps(self.coordinator.snapshot(), allow_nan=False)
                invocation.return_value(GLib.Variant("(s)", (status,)))
            except Exception as exc:
                invocation.return_dbus_error(FAILED_ERROR, str(exc))
            return

        channel = None
        checksum = None
        if method_name in ("StartCheck", "RefreshStatus"):
            component = parameters.unpack()[0]
            if component not in ("system", "decky"):
                invocation.return_dbus_error(INVALID_ARGS, "Invalid component")
                return
            operation = "check" if method_name == "StartCheck" else "status"
            action = "org.armada.updates.check"
        elif method_name == "StartSystemUpdate":
            component = "system"
            operation = "update"
            action = "org.armada.updates.system"
        elif method_name == "StartSystemRollback":
            component = "system"
            operation = "rollback"
            checksum = parameters.unpack()[0]
            if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
                invocation.return_dbus_error(INVALID_ARGS, "Invalid rollback checksum")
                return
            action = "org.armada.updates.system"
        elif method_name == "SetSystemChannel":
            component = "system"
            operation = "select_channel"
            channel = parameters.unpack()[0]
            if channel not in ("stable", "beta", "testing"):
                invocation.return_dbus_error(INVALID_ARGS, "Invalid update channel")
                return
            action = "org.armada.updates.channel"
        elif method_name == "StartDeckyAction":
            component = "decky"
            operation = parameters.unpack()[0]
            if operation not in ("update", "repair", "recover"):
                invocation.return_dbus_error(INVALID_ARGS, "Invalid Decky operation")
                return
            action = "org.armada.updates.decky"
        else:
            invocation.return_dbus_error(
                UNKNOWN_METHOD, "Unknown method: %s" % method_name
            )
            return

        if self.auth_pending >= MAX_AUTH_PENDING:
            invocation.return_dbus_error(
                FAILED_ERROR, "Too many authorization requests pending"
            )
            return

        self.auth_pending += 1
        try:
            self.authorizer.check(
                sender,
                action,
                lambda allowed, error: self._authorization_finished(
                    invocation, component, operation, allowed, error, channel,
                    checksum
                ),
            )
        except Exception as exc:
            self.auth_pending -= 1
            invocation.return_dbus_error(FAILED_ERROR, str(exc))

    def _authorization_finished(
        self, invocation, component, operation, allowed, error, channel=None,
        checksum=None
    ):
        try:
            if self.closed:
                invocation.return_dbus_error(FAILED_ERROR, "Service is closing")
                return
            if not allowed:
                invocation.return_dbus_error(
                    ACCESS_DENIED, error or "Authorization denied"
                )
                return
            try:
                if channel is None and checksum is None:
                    job_id = self.coordinator.start(component, operation)
                elif checksum is not None:
                    job_id = self.coordinator.start(
                        component, operation, checksum=checksum)
                else:
                    job_id = self.coordinator.start(
                        component, operation, channel=channel)
            except BusyError as exc:
                invocation.return_dbus_error(BUSY_ERROR, str(exc))
            except Exception as exc:
                invocation.return_dbus_error(FAILED_ERROR, str(exc))
            else:
                invocation.return_value(GLib.Variant("(s)", (job_id,)))
        finally:
            self.auth_pending -= 1

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.connection.unregister_object(self.registration_id)
        self.coordinator.close()


def main():
    if os.geteuid() != 0:
        raise RuntimeError("The updates D-Bus service must run as root")

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    except Exception as exc:
        raise RuntimeError("Could not connect to the system D-Bus: %s" % exc) from exc

    reply = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", (BUS_NAME, 4)),
        GLib.VariantType("(u)"),
        Gio.DBusCallFlags.NONE,
        5000,
        None,
    )
    if reply[0] != 1:
        raise RuntimeError("Could not acquire D-Bus name %s" % BUS_NAME)

    service = None
    try:
        service = UpdateDBusService(connection)
        # Activation must not enqueue synthetic status jobs. A component has a
        # single job slot, so a fabricated status job would race the first
        # caller-driven RefreshStatus/check/apply and make it fail with Busy.
        # Status is populated only when a client asks for it.

        loop = GLib.MainLoop()

        def stop_loop():
            loop.quit()
            return GLib.SOURCE_REMOVE

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop_loop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop_loop)
        loop.run()
    finally:
        if service is not None:
            service.close()
        connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ReleaseName",
            GLib.Variant("(s)", (BUS_NAME,)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )


if __name__ == "__main__":
    main()
