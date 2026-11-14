"""Small, allow-listed D-Bus client for the update service."""

import json
import re
import time

BUS_NAME = "org.armada.Updates"
PATH = "/org/armada/Updates"
IFACE = "org.armada.Updates1"
BUSY_ERROR = "org.armada.Updates.Error.Busy"


def is_busy_error(error):
    """Return True only for the service's own Busy D-Bus error."""
    try:
        from gi.repository import Gio
        remote = Gio.DBusError.get_remote_error(error)
    except Exception:
        remote = None
    if remote is not None:
        return remote == BUSY_ERROR
    return BUSY_ERROR in str(error)


class UpdateClient:
    _METHODS = {
        "GetStatus": None,
        "StartCheck": ("system", "decky"),
        "RefreshStatus": ("system", "decky"),
        "StartSystemUpdate": None,
        "StartSystemRollback": None,
        "SetSystemChannel": ("stable", "beta", "testing"),
        "StartDeckyAction": ("update", "repair", "recover"),
    }
    _OPERATIONS = {
        "system": {"status": ("RefreshStatus", "system"),
                   "check": ("StartCheck", "system"),
                   "update": ("StartSystemUpdate", None),
                   "rollback": ("StartSystemRollback", None),
                   "select_channel": ("SetSystemChannel", None)},
        "decky": {"status": ("RefreshStatus", "decky"),
                  "check": ("StartCheck", "decky"),
                  "update": ("StartDeckyAction", "update"),
                  "repair": ("StartDeckyAction", "repair"),
                  "recover": ("StartDeckyAction", "recover")},
    }

    def __init__(self, connection=None, poll_interval=0.25):
        self._connection = connection
        self.poll_interval = poll_interval

    def call(self, method, argument=None):
        if method not in self._METHODS:
            raise ValueError("update method is not allowed")
        allowed = self._METHODS[method]
        if allowed is None:
            if method == "StartSystemRollback":
                if (not isinstance(argument, str)
                        or re.fullmatch(r"[0-9a-f]{64}", argument) is None):
                    raise ValueError("invalid rollback checksum")
            elif argument is not None:
                raise ValueError("method takes no argument")
        elif argument not in allowed:
            raise ValueError("invalid update method argument")
        from gi.repository import Gio, GLib
        connection = self._connection
        if connection is None:
            connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        parameters = None if argument is None else GLib.Variant("(s)", (argument,))
        reply = connection.call_sync(
            BUS_NAME, PATH, IFACE, method, parameters, None,
            Gio.DBusCallFlags.NONE, 10000, None)
        values = reply.unpack()
        if not isinstance(values, tuple) or len(values) != 1 or not isinstance(values[0], str):
            raise RuntimeError("update service returned an invalid reply")
        return values[0]

    def snapshot(self):
        try:
            data = json.loads(self.call("GetStatus"))
            if (not isinstance(data, dict) or data.get("schemaVersion") != 1
                    or not isinstance(data.get("components"), dict)):
                raise ValueError("invalid status schema")
            return data
        except Exception as exc:
            raise RuntimeError("cannot read update status: %s" % exc) from exc

    def run(self, component, operation, channel=None, on_progress=None, timeout=None,
            checksum=None):
        operations = self._OPERATIONS.get(component)
        if operations is None or operation not in operations:
            raise ValueError("unknown component or operation")
        if operation == "select_channel":
            if channel not in ("stable", "beta", "testing"):
                raise ValueError("invalid update channel")
        elif channel is not None:
            raise ValueError("channel is only valid for channel selection")
        if operation == "rollback":
            if (not isinstance(checksum, str)
                    or re.fullmatch(r"[0-9a-f]{64}", checksum) is None):
                raise ValueError("invalid rollback checksum")
        elif checksum is not None:
            raise ValueError("checksum is only valid for system rollback")
        method, argument = operations[operation]
        if operation == "select_channel":
            argument = channel
        elif operation == "rollback":
            argument = checksum
        job_id = self.call(method, argument)
        if not job_id:
            raise RuntimeError("update service returned an empty job id")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            record = self.snapshot()["components"].get(component)
            if not isinstance(record, dict) or type(record.get("busy")) is not bool:
                raise RuntimeError("invalid component status")
            if record.get("jobId") == job_id:
                if record.get("operation") != operation:
                    raise RuntimeError("update job changed while waiting")
                result = record.get("result")
                if not isinstance(result, dict) or result.get("component") != component:
                    raise RuntimeError("invalid update result")
                if on_progress is not None:
                    on_progress(result)
                if not record["busy"]:
                    return result
            else:
                result = self._completed_result(record, job_id, component)
                if result is None:
                    raise RuntimeError("update job result is no longer available")
                if on_progress is not None:
                    on_progress(result)
                return result
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for update job")
            time.sleep(self.poll_interval)

    @staticmethod
    def _completed_result(record, job_id, component):
        completed = record.get("completed")
        if completed is None:
            return None
        if not isinstance(completed, dict):
            raise RuntimeError("update service returned invalid job history")
        if job_id not in completed:
            return None
        result = completed[job_id]
        if not isinstance(result, dict) or result.get("component") != component:
            raise RuntimeError("invalid update result")
        return result
