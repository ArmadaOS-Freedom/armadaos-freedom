#!/usr/bin/env python3
"""Exercise old and new Freedom identities across the organization transfer."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NEW = "ghcr.io/armadaos-freedom/armadaos-freedom"
OLD = "ghcr.io/mydanyi/armadaos-freedom"


class OrganizationMigrationTests(unittest.TestCase):
    def resolve(self, origin, selected=""):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "channel"
            state.write_text(selected)
            return subprocess.run(
                ["bash", "-c",
                 'source "$1"; ARMADA_CHANNEL_STATE="$2"; armada_update_target "$3"',
                 "migration-test", str(ROOT / "system_files/usr/lib/armada/update-lib"),
                 str(state), origin], capture_output=True, text=True,
                env={**os.environ, "LC_ALL": "C"})

    def test_old_and_new_sources_keep_channel(self):
        for repository in (OLD, NEW):
            for channel in ("stable", "beta", "testing"):
                for suffix in ("", "@sha256:" + "a" * 64):
                    with self.subTest(repository=repository, channel=channel, suffix=suffix):
                        result = self.resolve(f"ostree-image-signed:docker://{repository}:{channel}{suffix}")
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), f"{NEW} {channel}")

    def test_selected_channel_overrides_old_source(self):
        result = self.resolve(f"ostree-image-signed:docker://{OLD}:stable", "beta")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"{NEW} beta")

    def test_lookalike_and_unknown_channels_are_rejected(self):
        for repository in (NEW + "-evil", "example.invalid/" + NEW, NEW + "/extra"):
            with self.subTest(repository=repository):
                self.assertNotEqual(self.resolve(f"docker://{repository}:stable").returncode, 0)
        self.assertNotEqual(self.resolve(f"docker://{NEW}:nightly").returncode, 0)

    def test_canonical_trust_is_pinned_and_old_source_stays_closed(self):
        policy = json.loads((ROOT / "system_files/etc/containers/policy.json").read_text())
        docker = policy["transports"]["docker"]
        self.assertIn(NEW, docker)
        self.assertEqual(docker[NEW], [{
            "type": "sigstoreSigned",
            "keyPaths": ["/etc/pki/containers/armadaos-freedom.pub"],
            "signedIdentity": {"type": "matchRepository"},
        }])
        self.assertEqual(docker[OLD], [{"type": "reject"}])


if __name__ == "__main__":
    unittest.main()
