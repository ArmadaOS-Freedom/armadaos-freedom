"""Unified model capability registry for Freedom updates.

Every updater policy table is derived from this one frozen description of the
shipped boards, so a capability change is made once and cannot drift between
the catalog, package, boot, recovery, and execution gates.

The flags describe implemented backends, not hardware.  Every shipped board
has the installer-provisioned recovery layout and the ARMADA_BOOT disk boot
(``disk_boot``): updates and rollback switch a signed Linux boot generation on
``ARMADA_BOOT`` and never rewrite Android Boot.  TB322FC and TB323FU also keep
the older embedded layout, whose updates rebuild both raw Android Boot images
(``mirrored_boot``).  TB710FU (board 3) only has the ARMADA_BOOT path: its
installer keeps the AOSP-preserved chain and writes boot_a/boot_b once, at
install or boot maintenance, so the raw-Boot A/B writer must never see it.
Like TB322FC it never uses a GBL envelope.  Nothing here is registered at
runtime and no dependency outside the standard library is used.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ModelCapabilities:
    """Static capabilities of one supported board.

    ``compatible`` and ``soc_compatible`` hold the device-tree tokens the board
    reports.  ``board_id`` is the native settings-report identity, present only
    for boards with a recovery layout; the remaining flags describe which
    updater backends are implemented for the board.
    """

    compatible: tuple[bytes, ...]
    soc_compatible: tuple[bytes, ...]
    board_id: int | None
    offline_upgrade: bool
    mirrored_boot: bool
    gbl_envelope: bool
    disk_boot: bool


MODELS = MappingProxyType({
    "TB322FC": ModelCapabilities(
        compatible=(b"lenovo,elden",),
        soc_compatible=(b"qcom,sm8750",),
        board_id=1,
        offline_upgrade=True,
        mirrored_boot=True,
        gbl_envelope=False,
        disk_boot=True,
    ),
    "TB323FU": ModelCapabilities(
        compatible=(b"lenovo,baldur", b"lenovo,tb323fu"),
        soc_compatible=(b"qcom,sm8850", b"qcom,kaanapali"),
        board_id=2,
        offline_upgrade=True,
        mirrored_boot=True,
        gbl_envelope=True,
        disk_boot=True,
    ),
    "TB710FU": ModelCapabilities(
        compatible=(b"lenovo,tb710fu",),
        soc_compatible=(b"qcom,sm8650",),
        board_id=3,
        offline_upgrade=True,
        mirrored_boot=False,
        gbl_envelope=False,
        disk_boot=True,
    ),
})

# Every shipped contract model: install is allowed, nothing else is implied.
AUTHORIZED_MODELS = frozenset(MODELS)

# Models whose installed systems have the physical dual-slot backend the
# offline upgrade drives.
OFFLINE_UPGRADE_MODELS = frozenset(
    name for name, capability in MODELS.items() if capability.offline_upgrade
)

# Models that must carry the mirrored-policy runtime marker.
MIRROR_POLICY_MODELS = frozenset(
    name for name, capability in MODELS.items() if capability.mirrored_boot
)

# Models whose updates may take the embedded-layout path that rewrites both raw
# Android Boot images (boot trust, raw writers, slot activation).  A model that
# only has ARMADA_BOOT is outside it even though it takes offline upgrades.
RAW_BOOT_UPGRADE_MODELS = frozenset(
    name for name, capability in MODELS.items()
    if capability.offline_upgrade and capability.mirrored_boot
)

# Models with the ARMADA_BOOT disk boot: signed boot generations, the board-id
# manifest and the boot capsule in the unified package.
DISK_BOOT_MODELS = frozenset(
    name for name, capability in MODELS.items() if capability.disk_boot
)

# ARMADA_BOOT manifest board id -> model name (the same native board ids).
DISK_BOOT_BOARDS = MappingProxyType({
    name: capability.board_id
    for name, capability in MODELS.items()
    if capability.disk_boot and capability.board_id is not None
})


def upgrade_path_allowed(model, boot_source):
    """True when ``model`` may update through the boot path ``boot_source``
    reports: ``armada_boot`` needs the disk boot, anything else the raw-Boot
    mirrored writer."""
    if boot_source == "armada_boot":
        return model in DISK_BOOT_MODELS
    return model in RAW_BOOT_UPGRADE_MODELS

# Models whose native boot envelope is GBL and therefore needs region evidence.
GBL_MODELS = frozenset(
    name for name, capability in MODELS.items() if capability.gbl_envelope
)

# Device-tree compatible token -> model name.
COMPATIBLE_MODELS = MappingProxyType({
    alias: name
    for name, capability in MODELS.items()
    for alias in capability.compatible
})

# Native settings-report board id -> model name.
BOARD_MODELS = MappingProxyType({
    capability.board_id: name
    for name, capability in MODELS.items()
    if capability.board_id is not None
})

# (alias, accepted SoC tokens, board id) for every board with a recovery
# layout.  Each alias is listed on its own so a device tree that reports either
# TB323FU name still resolves to the same board.
RECOVERY_PROFILES = tuple(
    (alias, capability.soc_compatible, capability.board_id)
    for capability in MODELS.values()
    if capability.board_id is not None
    for alias in capability.compatible
)
