"""In-memory asynchronous update job coordination."""

import copy
import functools
import json
import re
import threading
import uuid

from .decky_manager import DeckyManager
from .system_manager import SystemManager


class BusyError(RuntimeError):
    """Raised when a component already has a pending job."""


class Coordinator:
    _OPERATIONS = {
        "system": {"status": "status", "check": "check",
                   "update": "apply", "select_channel": "select_channel",
                   "rollback": "rollback"},
        "decky": {
            "status": "status",
            "check": "check",
            "update": "apply",
            "repair": "repair",
            "recover": "recover",
        },
    }

    def __init__(self, system=None, decky=None):
        self._managers = {
            "system": SystemManager() if system is None else system,
            "decky": DeckyManager() if decky is None else decky,
        }
        self._lock = threading.Lock()
        self._closed = False
        self._workers = {}
        self._components = {
            component: {
                "busy": False,
                "jobId": None,
                "operation": None,
                "result": self._initial_result(component),
            }
            for component in self._OPERATIONS
        }

    @staticmethod
    def _initial_result(component):
        return {
            "component": component,
            "state": "not_checked",
            "phase": "status",
            "currentVersion": None,
            "targetVersion": None,
            "requiresReboot": False,
        }

    def start(self, component, operation, channel=None, checksum=None):
        operations = self._OPERATIONS.get(component)
        if operations is None or operation not in operations:
            raise ValueError("unknown component or operation")
        if operation == "select_channel":
            if channel not in ("stable", "beta", "testing"):
                raise ValueError("invalid update channel")
        elif channel is not None:
            raise ValueError("channel is only valid for system channel selection")
        if operation == "rollback":
            if (not isinstance(checksum, str)
                    or re.fullmatch(r"[0-9a-f]{64}", checksum) is None):
                raise ValueError("invalid rollback checksum")
        elif checksum is not None:
            raise ValueError("checksum is only valid for system rollback")

        method_name = operations[operation]
        method = getattr(self._managers[component], method_name)
        if operation == "select_channel":
            method = functools.partial(method, channel)
        elif operation == "rollback":
            method = functools.partial(method, checksum)
        job_id = uuid.uuid4().hex

        with self._lock:
            if self._closed:
                raise RuntimeError("coordinator is closed")
            record = self._components[component]
            if record["busy"]:
                raise BusyError("component already has a pending job")

            previous = copy.deepcopy(record)
            prior = previous["result"]
            running = {
                "component": component,
                "state": "running",
                "phase": operation,
                "currentVersion": prior.get("currentVersion"),
                "targetVersion": prior.get("targetVersion"),
                "requiresReboot": False,  # cannot claim reboot while running
            }
            for key in ("currentCommit", "model", "channel"):
                if key in prior:
                    running[key] = copy.deepcopy(prior[key])

            try:
                record.update(busy=True, jobId=job_id, operation=operation,
                              result=running)
                worker = threading.Thread(
                    target=self._run,
                    args=(component, operation, job_id, method,
                          prior.get("currentVersion")),
                    name="armada-update-%s" % component,
                    daemon=False,
                )
                self._workers[component] = worker
                worker.start()
            except BaseException:
                self._workers.pop(component, None)
                self._components[component] = previous
                raise

        return job_id

    def _run(self, component, operation, job_id, method, prior_version):
        failure = {
            "component": component,
            "state": "failed",
            "phase": operation,
            "currentVersion": prior_version,
            "targetVersion": None,
            "requiresReboot": False,
            "error": "operation interrupted",
        }
        completed = False
        outcome = None
        try:
            try:
                raw = copy.deepcopy(method())
                if not isinstance(raw, dict):
                    raise TypeError("manager result must be a dict")
                if raw.get("component") != component:
                    raise ValueError("manager result has wrong component")
                try:
                    json.dumps(raw, allow_nan=False)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "manager result is not JSON serializable: %s" % exc)
                outcome = raw
            except Exception as exc:
                failure = {
                    "component": component,
                    "state": "failed",
                    "phase": operation,
                    "currentVersion": prior_version,
                    "targetVersion": None,
                    "requiresReboot": False,
                    "error": str(exc),
                }
                outcome = failure
            completed = True
        finally:
            with self._lock:
                record = self._components[component]
                if record["jobId"] == job_id:
                    record["busy"] = False
                    record["result"] = (
                        outcome if completed else copy.deepcopy(failure))
                current = threading.current_thread()
                if self._workers.get(component) is current:
                    self._workers.pop(component, None)

    def snapshot(self):
        with self._lock:
            data = copy.deepcopy({
                "schemaVersion": 1,
                "components": self._components,
            })
        record = data["components"].get("system")
        manager = self._managers.get("system")
        if (record is not None and record["busy"]
                and record["operation"] in ("update", "rollback")
                and hasattr(manager, "progress")):
            try:
                report = manager.progress()
            except Exception:
                report = None
            with self._lock:
                data = copy.deepcopy({
                    "schemaVersion": 1,
                    "components": self._components,
                })
                current = data["components"].get("system")
                if (isinstance(report, dict)
                        and current is not None
                        and current["busy"]
                        and current["jobId"] == record["jobId"]
                        and current["operation"] in ("update", "rollback")):
                    current["result"].update(copy.deepcopy(report))
                    current["result"]["state"] = "running"
                    current["result"]["requiresReboot"] = False
        return data

    def close(self):
        with self._lock:
            self._closed = True
            workers = tuple(self._workers.values())
        current = threading.current_thread()
        for worker in workers:
            if worker is not current:
                worker.join()
