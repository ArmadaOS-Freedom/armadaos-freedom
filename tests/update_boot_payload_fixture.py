"""Boot payload fixture shared by update test suites.

Only the model/build configuration and ``_make_root`` live here.  Suites that
need a manifest-complete payload tree can reuse them without importing the
payload test suite, whose package-builder cases depend on build scripts that
are not present on every branch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "system_files/usr/lib/armada"))

import updates
from updates import boot_payload

BUILD_FILES = REPO / "build_files"
KVER = "6.6-fixture"
FW_SHA = hashlib.sha256(b"firmware").hexdigest()


class UpdateBootPayloadFixture(unittest.TestCase):
    """Carry the payload models and build a root that satisfies the manifest."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = str(BUILD_FILES)
        cls.added_path = cls.build_dir not in updates.__path__
        if cls.added_path:
            updates.__path__.append(cls.build_dir)
        cfg = json.loads(
            (BUILD_FILES / "edl-devices.json").read_text(encoding="utf-8")
        )
        for prof in cfg["profiles"].values():
            prof["bootRequirements"] = {
                "earlyFirmware": ["test/fw.bin"],
                "earlyDrivers": ["test_core"],
                "firmwareSha256": {"test/fw.bin": FW_SHA},
            }
        cls.cfg = cfg
        cls.models = list(cfg["profiles"])
        cls._case_idx = 0

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "added_path", False) and cls.build_dir in updates.__path__:
            updates.__path__.remove(cls.build_dir)

    def _make_root(self) -> Path:
        type(self)._case_idx += 1
        td = tempfile.TemporaryDirectory(prefix=f"ubp-{self._case_idx}-")
        self.addCleanup(td.cleanup)
        root = Path(td.name) / "root"
        fw = root / "usr/lib/firmware/test/fw.bin"
        fw.parent.mkdir(parents=True, exist_ok=True)
        fw.write_bytes(b"firmware")
        mod = root / f"usr/lib/modules/{KVER}"
        (mod / "dtb").mkdir(parents=True, exist_ok=True)
        (mod / "modules.builtin").write_text(
            "kernel/test_core.ko\n", encoding="utf-8"
        )
        (mod / "vmlinuz").write_bytes(b"\x00" * 56 + b"ARM\x64" + b"\x00" * 4)
        (mod / "initramfs.img").write_bytes(b"fixture")
        ub = root / "usr/share/armada/update-boot"
        rt_dir = ub / "runtimes"
        rt_dir.mkdir(parents=True, exist_ok=True)
        (ub / "edl-devices.json").write_text(
            json.dumps(self.cfg), encoding="utf-8"
        )
        dtb_bytes = struct.pack(">II", 0xD00DFEED, 40) + b"\x00" * 32
        for idx, (model, prof) in enumerate(self.cfg["profiles"].items()):
            dtb = mod / "dtb" / prof["dtb"]
            dtb.parent.mkdir(parents=True, exist_ok=True)
            dtb.write_bytes(dtb_bytes)
            runtime = bytearray(bytes([idx + 1]) * 4194304)
            marker = boot_payload.MIRROR_RUNTIME_MARKER
            runtime[:len(marker)] = marker
            (rt_dir / f"{model}.bin").write_bytes(bytes(runtime))
        return root
