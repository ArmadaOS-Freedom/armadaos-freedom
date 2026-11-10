import contextlib
import copy
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "system_files/usr/lib/armada")
)

from updates import coordinator as module  # noqa: E402
from updates.coordinator import BusyError, Coordinator  # noqa: E402


class FakeManager:
    def __init__(self, component):
        self.component = component
        self.calls = []
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.result = {
            "component": component,
            "state": "up_to_date",
            "phase": "status",
            "currentVersion": "1.0.0",
            "targetVersion": "1.0.0",
            "requiresReboot": False,
        }
        self.error = None

    def block(self):
        self.started.clear()
        self.gate.clear()

    def _invoke(self, name):
        self.calls.append(name)
        self.started.set()
        if not self.gate.wait(2.0):
            raise TimeoutError(f"{self.component}.{name} gate timed out")
        if self.error is not None:
            raise self.error
        if isinstance(self.result, dict) and "phase" in self.result:
            out = copy.copy(self.result)
            out["phase"] = name
            return out
        return self.result

    def status(self):
        return self._invoke("status")

    def check(self):
        return self._invoke("check")

    def apply(self):
        return self._invoke("apply")

    def repair(self):
        return self._invoke("repair")

    def recover(self):
        return self._invoke("recover")

    def rollback(self, checksum):
        self.last_checksum = checksum
        return self._invoke("rollback")


class CoordinatorTest(unittest.TestCase):
    @contextlib.contextmanager
    def env(self):
        sys_mgr = FakeManager("system")
        decky_mgr = FakeManager("decky")
        coord = Coordinator(system=sys_mgr, decky=decky_mgr)
        try:
            yield coord, sys_mgr, decky_mgr
        finally:
            sys_mgr.gate.set()
            decky_mgr.gate.set()
            coord.close()

    def wait_idle(self, coord, component):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            comp = coord.snapshot()["components"][component]
            if not comp["busy"]:
                return comp
            time.sleep(0.005)
        self.fail(f"{component} stayed busy past timeout")

    def wait_started(self, mgr):
        self.assertTrue(mgr.started.wait(2.0), f"{mgr.component} did not start")

    def test_initial_state_and_unknown_operations_rejected(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            before = coord.snapshot()
            self.assertEqual((sys_mgr.calls, decky_mgr.calls), ([], []))
            for comp, op in (
                ("steam", "check"),
                ("system", "repair"),
                ("decky", "unknown"),
            ):
                with self.assertRaises(ValueError):
                    coord.start(comp, op)
            self.assertEqual(coord.snapshot(), before)
            self.assertEqual((sys_mgr.calls, decky_mgr.calls), ([], []))

    def test_operation_mapping(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            for op in ("status", "check", "update"):
                coord.start("system", op)
                self.wait_idle(coord, "system")
            for op in ("status", "check", "update", "repair", "recover"):
                coord.start("decky", op)
                self.wait_idle(coord, "decky")
            self.assertEqual(sys_mgr.calls, ["status", "check", "apply"])
            self.assertEqual(
                decky_mgr.calls, ["status", "check", "apply", "repair", "recover"]
            )

    def test_deep_copy_and_running_preserves_metadata(self):
        with self.env() as (coord, sys_mgr, _):
            nested = {"tags": ["a"]}
            sys_mgr.result = {
                "component": "system",
                "state": "update_available",
                "phase": "check",
                "currentVersion": "2.1.0",
                "targetVersion": "2.2.0",
                "requiresReboot": True,
                "currentCommit": ["c1"],
                "model": "rc71l",
                "channel": "stable",
                "extra": nested,
            }
            coord.start("system", "check")
            done = self.wait_idle(coord, "system")["result"]
            nested["tags"].append("mutated_source")
            done["extra"]["tags"].append("mutated_snapshot")
            fresh = coord.snapshot()["components"]["system"]["result"]
            self.assertEqual(fresh["extra"]["tags"], ["a"])

            sys_mgr.block()
            coord.start("system", "status")
            self.wait_started(sys_mgr)
            running = coord.snapshot()["components"]["system"]["result"]
            self.assertEqual(running["state"], "running")
            self.assertEqual(running["phase"], "status")
            self.assertEqual(running["currentVersion"], "2.1.0")
            self.assertEqual(running["targetVersion"], "2.2.0")
            self.assertFalse(running["requiresReboot"])
            self.assertEqual(running["currentCommit"], ["c1"])
            self.assertEqual(running["model"], "rc71l")
            self.assertEqual(running["channel"], "stable")
            sys_mgr.gate.set()
            self.wait_idle(coord, "system")

    def test_live_progress_is_snapshot_only_and_lock_free(self):
        for op, kw, expected_phase in (
            ("update", {}, "apply"),
            ("rollback", {"checksum": "a" * 64}, "rollback"),
        ):
            with self.subTest(op=op), self.env() as (coord, sys_mgr, _):
                sys_mgr.block()
                live = {
                    "phase": "writing_boot",
                    "state": "awaiting_reboot",
                    "requiresReboot": True,
                }

                def progress():
                    self.assertTrue(
                        coord._lock.acquire(blocking=False),
                        "progress callback held coordinator lock",
                    )
                    coord._lock.release()
                    return live

                sys_mgr.progress = progress
                coord.start("system", op, **kw)
                self.wait_started(sys_mgr)
                with coord._lock:
                    internal = copy.deepcopy(coord._components["system"]["result"])
                running = coord.snapshot()["components"]["system"]["result"]
                self.assertEqual(running["state"], "running")
                self.assertEqual(running["phase"], "writing_boot")
                self.assertFalse(running["requiresReboot"])
                with coord._lock:
                    self.assertEqual(coord._components["system"]["result"], internal)
                self.assertEqual(
                    live,
                    {
                        "phase": "writing_boot",
                        "state": "awaiting_reboot",
                        "requiresReboot": True,
                    },
                )
                sys_mgr.gate.set()
                done = self.wait_idle(coord, "system")["result"]
                self.assertEqual(done["state"], "up_to_date")
                self.assertEqual(done["phase"], expected_phase)

    def test_stale_progress_cannot_overwrite_completed_result(self):
        with self.env() as (coord, sys_mgr, _):
            sys_mgr.block()

            def progress():
                self.assertTrue(
                    coord._lock.acquire(blocking=False),
                    "progress callback held coordinator lock",
                )
                coord._lock.release()
                sys_mgr.gate.set()
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    with coord._lock:
                        if (
                            "system" not in coord._workers
                            or not coord._components["system"]["busy"]
                        ):
                            break
                    time.sleep(0.001)
                return {
                    "phase": "writing_boot",
                    "state": "running",
                    "requiresReboot": False,
                }

            sys_mgr.progress = progress
            coord.start("system", "update")
            self.wait_started(sys_mgr)
            done = coord.snapshot()["components"]["system"]
            self.assertFalse(done["busy"])
            self.assertEqual(done["result"]["state"], "up_to_date")
            self.assertEqual(done["result"]["phase"], "apply")
            self.assertNotEqual(done["result"]["state"], "running")

    def test_busy_component_blocks_same_allows_other(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            decky_mgr.block()
            coord.start("decky", "check")
            self.wait_started(decky_mgr)
            for op in ("update", "check"):
                with self.assertRaises(BusyError):
                    coord.start("decky", op)
            coord.start("system", "check")
            sys_comp = self.wait_idle(coord, "system")
            self.assertEqual(sys_comp["result"]["state"], "up_to_date")
            self.assertTrue(coord.snapshot()["components"]["decky"]["busy"])
            decky_mgr.gate.set()
            self.wait_idle(coord, "decky")

    def test_failure_isolation_while_other_component_blocked(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            decky_mgr.block()
            decky_job = coord.start("decky", "check")
            self.wait_started(decky_mgr)

            sys_mgr.error = RuntimeError("boom")
            coord.start("system", "check")
            sys_comp = self.wait_idle(coord, "system")
            self.assertEqual(sys_comp["result"]["state"], "failed")
            self.assertIn("boom", sys_comp["result"]["error"])

            decky_live = coord.snapshot()["components"]["decky"]
            self.assertTrue(decky_live["busy"])
            self.assertEqual(decky_live["jobId"], decky_job)
            self.assertEqual(decky_live["result"]["state"], "running")

            decky_mgr.gate.set()
            decky_done = self.wait_idle(coord, "decky")
            self.assertEqual(decky_done["result"]["state"], "up_to_date")

    def test_both_components_run_concurrently(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            sys_mgr.block()
            decky_mgr.block()
            coord.start("system", "check")
            coord.start("decky", "check")
            self.wait_started(sys_mgr)
            self.wait_started(decky_mgr)
            snap = coord.snapshot()["components"]
            self.assertTrue(snap["system"]["busy"])
            self.assertTrue(snap["decky"]["busy"])
            sys_mgr.gate.set()
            decky_mgr.gate.set()
            self.wait_idle(coord, "system")
            self.wait_idle(coord, "decky")

    def test_invalid_manager_results_and_exceptions_fail_own_component(self):
        with self.env() as (coord, sys_mgr, _):
            bad_payloads = [
                {"component": "decky", "state": "up_to_date"},
                ["not-a-dict"],
                {"component": "system", "bad": {1, 2}},
                {"component": "system", "bad": float("nan")},
            ]
            for payload in bad_payloads:
                sys_mgr.result = payload
                coord.start("system", "check")
                comp = self.wait_idle(coord, "system")
                self.assertEqual(comp["result"]["state"], "failed")
                self.assertEqual(
                    coord.snapshot()["components"]["decky"]["result"]["state"],
                    "not_checked",
                )

    def test_thread_creation_or_start_failure_rolls_back_state(self):
        with self.env() as (coord, sys_mgr, _):
            coord.start("system", "status")
            self.wait_idle(coord, "system")
            baseline = coord.snapshot()

            with mock.patch.object(
                module.threading, "Thread", side_effect=RuntimeError("ctor fail")
            ):
                with self.assertRaises(RuntimeError):
                    coord.start("system", "check")
            self.assertEqual(coord.snapshot(), baseline)
            self.assertEqual(coord._workers, {})

            with mock.patch.object(
                module.threading.Thread,
                "start",
                side_effect=RuntimeError("start fail"),
            ):
                with self.assertRaises(RuntimeError):
                    coord.start("system", "check")
            self.assertEqual(coord.snapshot(), baseline)
            self.assertEqual(coord._workers, {})

            coord.start("system", "check")
            comp = self.wait_idle(coord, "system")
            self.assertEqual(comp["result"]["state"], "up_to_date")
            self.assertEqual(sys_mgr.calls, ["status", "check"])

    def test_close_waits_for_in_flight_worker_and_rejects_new_starts(self):
        with self.env() as (coord, _, decky_mgr):
            decky_mgr.block()
            coord.start("decky", "check")
            self.wait_started(decky_mgr)

            closer_done = threading.Event()
            closer = threading.Thread(
                target=lambda: (coord.close(), closer_done.set())
            )
            closer.start()
            try:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    with coord._lock:
                        if coord._closed:
                            break
                    time.sleep(0.005)
                else:
                    self.fail("coordinator did not mark _closed")

                self.assertFalse(closer_done.wait(0.05))
                with self.assertRaises(RuntimeError):
                    coord.start("system", "status")

                decky_mgr.gate.set()
                self.assertTrue(closer_done.wait(2.0))
            finally:
                decky_mgr.gate.set()
                closer.join(2.0)
                self.assertFalse(closer.is_alive())

    def test_sequential_jobs_clean_up_worker_tracking(self):
        with self.env() as (coord, sys_mgr, decky_mgr):
            for _ in range(3):
                coord.start("system", "check")
                self.wait_idle(coord, "system")
                coord.start("decky", "update")
                self.wait_idle(coord, "decky")
            with coord._lock:
                self.assertEqual(coord._workers, {})
            self.assertEqual(len(sys_mgr.calls), 3)
            self.assertEqual(len(decky_mgr.calls), 3)


if __name__ == "__main__":
    unittest.main()
