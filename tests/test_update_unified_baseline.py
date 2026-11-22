"""Baseline checks for the version of the unified-package updater.

The initial all-model package replaces the pre-unification 0.8.0 updater
artifact, so the installed updater reports 0.8.2. These tests pin that
behaviour through the modules that expose or forward the version, not through
source text, and keep the installer baseline at 0.8.0 out of scope.
"""

import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "system_files/usr/lib/armada"))

from updates import (  # noqa: E402
    offline_execute,
    offline_prepare,
    offline_session,
    system_manager,
    system_transaction,
)

EXPECTED_UPDATER_VERSION = "0.8.2"

_FORWARDING_SITES = (
    (offline_prepare, "prepare_offline_update"),
    (offline_prepare, "_prepare_offline_update"),
    (offline_execute, "execute_offline_update"),
    (offline_execute, "_execute_offline_update"),
    (system_transaction, "run_online_transaction"),
    (system_transaction, "_run_transaction"),
)


def _default_version(module, name):
    signature = inspect.signature(getattr(module, name))
    return signature.parameters["updater_version"].default


class UnifiedUpdaterVersionTests(unittest.TestCase):
    def test_runtime_updater_version_is_the_unified_baseline(self):
        self.assertEqual(system_manager.UPDATER_VERSION, EXPECTED_UPDATER_VERSION)
        self.assertEqual(offline_session._UPDATER_VERSION, EXPECTED_UPDATER_VERSION)

    def test_entrypoint_defaults_agree_with_the_unified_baseline(self):
        defaults = {
            "%s.%s" % (module.__name__, name): _default_version(module, name)
            for module, name in _FORWARDING_SITES
        }
        for label, value in defaults.items():
            self.assertEqual(value, EXPECTED_UPDATER_VERSION, label)
        self.assertEqual(
            set(defaults.values()),
            {system_manager.UPDATER_VERSION, offline_session._UPDATER_VERSION},
        )

    def test_entrypoints_forward_the_unified_version(self):
        with patch.object(
                offline_prepare, "_prepare_offline_update", return_value={}
        ) as forwarded:
            offline_prepare.prepare_offline_update(
                document=b"{}",
                signature_b64=b"signature",
                payload_directory=None,
                expected_channel="stable",
                sysroot_path=Path("/"),
                work_dir=Path("/tmp/armada-work"),
            )
        self.assertEqual(
            forwarded.call_args.kwargs["updater_version"],
            EXPECTED_UPDATER_VERSION,
        )

        with patch.object(
                offline_execute, "_execute_offline_update", return_value={}
        ) as forwarded:
            offline_execute.execute_offline_update(
                work_dir=Path("/tmp/armada-work"), recovery_ack={}
            )
        self.assertEqual(
            forwarded.call_args.kwargs["updater_version"],
            EXPECTED_UPDATER_VERSION,
        )

        with patch.object(
                system_transaction, "_run_transaction", return_value={}
        ) as forwarded:
            system_transaction.run_online_transaction(
                channel_url="https://example.invalid/repo",
                signature_url="https://example.invalid/repo.sig",
                expected_channel="stable",
                sysroot_path=Path("/"),
                work_dir=Path("/tmp/armada-work"),
            )
        self.assertEqual(
            forwarded.call_args.kwargs["updater_version"],
            EXPECTED_UPDATER_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
