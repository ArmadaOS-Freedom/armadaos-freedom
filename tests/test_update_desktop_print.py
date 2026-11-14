import copy
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))
import armada_tools


class DesktopPrintStatusTest(unittest.TestCase):
    def setUp(self):
        self.state = {
            "booted": {"version": "20260919.current"},
            "rollback": {"version": "20260907.previous", "checksum": "b" * 64,
                         "incompatible": False},
            "staged": None,
            "rollback_queued": False,
            "reboot_ready": False,
            "update_target": None,
            "channel": "stable",
            "update_message": "",
        }

    def status(self, **changes):
        state = copy.deepcopy(self.state)
        state.update(changes)
        output = io.StringIO()
        with patch.object(armada_tools, "privileged_request", return_value=state) as request, \
                redirect_stdout(output):
            self.assertEqual(armada_tools.print_status("Operating System"), 0)
        request.assert_called_once_with("get-os")
        text = output.getvalue()
        self.assertIn("Current Version: 20260919.current", text)
        self.assertIn("Previous Version: 20260907.previous", text)
        self.assertIn("Update Channel: stable", text)
        return text

    def test_awaiting_reboot_prints_target_and_status(self):
        text = self.status(reboot_ready=True, update_target="20260920.next",
                           update_message="Update installed. Restart to apply.")
        self.assertIn("After Restart: 20260920.next", text)
        self.assertIn("Update Status: Update installed. Restart to apply.", text)

    def test_confirmed_state_has_no_restart_or_pending_target(self):
        text = self.status(update_message="Update confirmed.")
        self.assertNotIn("After Restart:", text)
        self.assertNotIn("Pending Update:", text)
        self.assertIn("Update Status: Update confirmed.", text)

    def test_busy_state_ignores_stale_rollback_queue(self):
        text = self.status(rollback_queued=True, update_target="20260920.next",
                           update_message="Downloading update (42%).")
        self.assertNotIn("After Restart:", text)
        self.assertIn("Pending Update: 20260920.next", text)
        self.assertIn("Update Status: Downloading update (42%).", text)


if __name__ == "__main__":
    unittest.main()
