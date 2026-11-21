# Launcher settings storage

The TB322FC and TB323FU launchers store settings in the internal UFS backup
partition. They can remember the last boot target, queue a one-shot boot and
control whether the launcher writes boot logs.

The storage policy discovers internal UFS and TF independently when adapters are available. Each usable medium
must have a GPT partition named ARMADA_BACKUP with a nonzero unique partition UUID
and an exact size of 2 GiB. The launcher does not claim unallocated disk space, resize
partitions, format media, or provision an old backup automatically.

Valid internal settings take precedence over valid TF settings. Sequence numbers are
local to one installation and are not compared between media. If neither medium has
settings, an explicitly provisioned empty internal store is preferred, followed by TF.
A failed save stays an error on the selected medium; it does not silently write elsewhere.

## Backup layout

All offsets below are relative to the start of ARMADA_BACKUP.

| Region | Offset | Length | Ownership |
|---|---:|---:|---|
| Recovery JSON header | 0 | 65536 | Installer |
| Compressed recovery tar | 65536 | Header compressedBytes | Installer |
| Zero padding | Payload end | Up to config region | Installer |
| Config descriptor | 2147418112 | 4096 | Installer |
| Config slot A | 2147422208 | 4096 | Launcher |
| Config slot B | 2147426304 | 4096 | Launcher |
| Boot log slots (descriptor v3) | 2147430400 | 16384 | Launcher |
| Update transaction slots (descriptor v3) | 2147446784 | 8192 | Launcher and updater |
| Reserved zero bytes (descriptor v3) | 2147454976 | 28672 | Reserved |

The v3 outer JSON header declares configRegionOffset, configRegionBytes,
configPartitionUuid and configBoardId. These fields are checked against the descriptor,
installation ID, supported model and the recovery manifest's backup-partition UUID.
Recovery SHA-256 continues to cover the compressed backup. The two settings slots do
not participate in recovery validation, so an interrupted settings write cannot invalidate
a sound recovery archive. The descriptor and reserved bytes remain immutable and checked.

The v2 reader retains its original requirement that all bytes after the compressed member
are zero. An existing v2 image therefore needs an explicit, validated upgrade before the
launcher can use its tail. Merely finding zeros is not authorization to write there. The installer maintenance tool BackupSettingsUpgrade validates the complete legacy image and prepares bounded before/after header and tail files, bound to the target GPT UUID. Applying and verifying those changes on a device is a separate step; preparation does not enable settings by itself.

## Binary records

Integers are little-endian; UUIDs use RFC 4122 byte order, not GPT mixed-endian encoding.
Each 4096-byte structure ends with IEEE CRC-32 over its first 4092 bytes.

Descriptor:
- 0: KSCFGV3 followed by NUL; 8: descriptor version 1, 2 or 3; 12: length 4096.
- 16: partition length u64; 24: recovery payload end u64; 32: region offset u64.
- 40: region length 65536; 44: slot length 4096.
- 48: partition UUID; 64: installation UUID.
- 80/84: slot offsets 4096/8192 relative to config region.
- 88: actual board ID (1 TB322FC, 2 TB323FU).
- Descriptors 1/2: 92..4091 must be zero. Descriptor 2 permits the reserved
  boot-log area to contain opaque bytes.
- Descriptor 3: 92/96: update transaction slot offsets 28672/32768 relative to
  the config region; 100: slot length 4096; 104..4091: zero. Boot log slots start
  at offsets 12288 and 20480. Each uses the first 4096 bytes of its 8192-byte slot.

Settings record:
- 0: KSCFGR1 followed by NUL (family magic); 8: record version 1, 2, 3 or 4; 12: length 4096.
- 16: nonzero sequence u64; 24: partition UUID; 40: installation UUID.
- 56: device choice (auto / TB322FC / TB323FU = 0/1/2).
- 60: default target (manifest / Android / internal / TF = 0/1/2/3; versions 2 through 4 also accept remember-last = 4).
- 64: language (Chinese / English = 0/1).
- Version 1: 68..4091 must be zero; loaded last target is 0 (no history).
- Versions 2 through 4: 68: last target (none / Android / internal / TF = 0/1/2/3).
- Version 2: 72..4091 must be zero.
- Version 3: 72: next action (none / one-shot EDL = 0/1); 76..4091 must be zero.

Version 4 uses the word at offset 72. The low byte selects none (0), EDL (1),
Android (2), internal Linux (3), or internal Linux diagnostic (4). Bit 8 disables
boot logging. Bits 9 through 31 and bytes 76 through 4091 must be zero.

A save uses version 3 when the word is 0 or 1 and uses version 4 otherwise.
All four record versions use the same region and descriptor. A rollback that
preserves active version 4 settings needs a compatible launcher.

The newest valid record wins. Two different valid records with equal sequence numbers
are a conflict. Save writes the older or invalid slot, flushes the device and reads back
the exact record before reporting success. The previously valid slot remains available
if the new write is incomplete. A flush or readback error can occur after the new bytes reached media; the session therefore marks the save uncertain and forces a real retry even when the user reselects the previous value.

## Remember last booted system

Settings / Default boot offers “记住上一次启动的系统” (“Remember last booted system”)
when a usable settings store is available. Fixed Android, internal Linux and TF choices
remain available. Remember mode selects the previous launch target at the next startup;
if that target is absent, ambiguous or incompatible with the selected board, automatic
boot is disabled until the user makes a selection.

The launcher records a known system target after preparing its boot and before handing
off control. This records the attempted launch, not confirmation that the OS finished
booting. Merely highlighting a row, changing language or using a power action does not
change the history. Fixed default modes also retain the actual launch history. Enabling
remember mode without history preserves the current valid default. A repeated launch
of the same recorded target skips writing unless an earlier save is uncertain.

## One-shot boot and boot logging

The launcher Settings page can queue Android, internal Linux or internal Linux
diagnostic for the next reboot. The launcher consumes the request durably before
dispatch and keeps the permanent default and last normal boot target. An absent
or ambiguous target falls back to the regular boot selection.

A one-shot TB322FC or TB323FU diagnostic schedules ordinary Linux for the
following boot. The matching initramfs reboots at the dracut pre-mount
checkpoint. A diagnostic chosen directly from the TB322FC boot menu still stops
at the pre-mount shell; a TB323FU manual diagnostic instead continues using
verbose startup arguments. With logging enabled, the automatic return
preserves the diagnostic and ordinary handoffs in the two recovery-area log
records.

With the advertised `kshim,launcher-settings-version` capability, Linux can make
the same request with `sudo armada-reboot-edl --next android` (also `linux`,
`diagnostic` or `edl`), then request a normal reboot. `--check` reads state,
and `--cancel` clears a pending request.

The Boot logging setting is available when the internal backup has a version 3
descriptor. It controls future log writes for the internal Linux and diagnostic entries, and keeps existing records.
`sudo armada-reboot-edl --log on` and `--log off` change the same setting in Linux.

## Linux entry to EDL

`sudo armada-reboot-edl --check` checks the installed launcher's advertised capability
and the provisioned internal UFS settings region without writing. `sudo armada-reboot-edl --reboot` writes a one-shot EDL request into the alternate settings slot, flushes and
verifies it, then requests a normal Linux reboot. `--cancel` removes a pending request
without rebooting. Without arguments the command performs the read-only check.

The launcher clears the request durably before invoking its EDL action. A failed clear
never starts EDL; settings and boot history are retained. EDL uses the device's existing
firmware reboot interface and still requires the matching computer-side Sahara/Firehose
loader. TB322FC and TB323FU use different loaders. On TB322FC, the normal profile
prepares SDI before EDL reset. The Linux helper and the launcher's EDL menu action
share the same reset backend.

A launch-history update after menu teardown temporarily restores the existing runtime
page tables for the UFS transaction, then returns to the prior MMU-off state after safe
controller release.

## Read-only EDL capability diagnostic

`CONFIG_KSHIM_EDL_PROBE` is disabled by default and requires QCOM_REBOOT and
UFS_SETTINGS. The diagnostic build queries firmware capabilities and reads the
DT-selected download register before the menu. It does not write download or
PMIC reason fields, access SPMI, or invoke a reset. Existing EDL reset selection
and the settings record format are unchanged.

The Linux handoff exports `/chosen/kshim,edl-probe` as 11 big-endian 32-bit cells:
version (1), signed status, SCM availability mask, PSCI version, signed RESET2
feature status, signed OEM availability-call status, OEM available result,
route (0 none / 1 OEM / 2 PSCI RESET2), download address high/low, download value.
SCM mask bits 0/1/2 correspond to availability IDs 0x02000601/0x02000501/0x02000502.
A route is reported only after its capability checks and the register read
succeed. It describes the current launcher's selection rules, not a successful
reset or a working Firehose session. A nonzero status is a diagnostic result;
absent or malformed properties must be reported as capture failures.

`CONFIG_KSHIM_EDL_DISABLE_SDI` is enabled in the normal TB322FC profile and remains
off by default for other profiles. It follows the SM8750 kernel's fast SCM
BOOT/WDOG_DEBUG_PART call (service 1, command 9,
arguments 1, 0) before EDL reset. Support is checked before changing the download
cookie. If the call fails, the existing cookie rollback runs and no reset is
attempted. Other boot targets and the read-only capability probe do not issue
this call. This implementation does not restore the prior SDI state if reset returns;
like the kernel, it leaves secure debugging disabled. The preparation preserves
the original reset choice and does not halt the PMIC arbiter.

Reference: Linux mainline qcom_scm_disable_sdi and SM8750 kernel
[6028f47faddaa27700f8dd3a1d83906ea8f27170](https://github.com/OnePlusOSS/android_kernel_oneplus_sm8750/blob/6028f47faddaa27700f8dd3a1d83906ea8f27170/drivers/firmware/qcom_scm.c).

## Driver integration

storage_ufs.c implements a single legacy UFSHCI transfer list slot and bounded polling,
with READ/WRITE16, FUA and SYNCHRONIZE_CACHE10. It requires an already verified link,
clock state and DMA mapping from the board adapter. It neither initializes a PHY nor
assumes a CPU pointer is a DMA bus address. A failed release prohibits payload handoff
and freeing the DMA arena. The native settings adapter claims and restores the inherited
controller for each transaction, including link and device-power wake/restore. The normal
TB322FC profile does not require boot diagnostics or an automatic settings-seed test.

storage_sdhci.c implements SD initialization and single-block PIO at low speed on one
data line. The board adapter supplies powered clocks, pin configuration, voltage
selection, card detection and the actual parent clock rate. DMA, UHS tuning, eMMC and
SDIO are outside this transport's implemented scope.

kshim_platform_bind_settings binds a discovered settings session to the menu. The menu
loads only a session bound to the actual detected board; a manual device choice cannot
change the storage hardware profile. The hardware owner must unbind before releasing
the storage session and quiesce controllers before handing off to a payload.

Register and wire format references are pinned to Linux commit
330517c63aa9b48227b6ab45f01003effac77ae4: include/ufs/ufshci.h,
include/ufs/ufs.h, include/uapi/scsi/scsi_bsg_ufs.h, drivers/mmc/host/sdhci.h,
drivers/scsi/sd.c and the Qualcomm SM8750/Kaanapali device trees.
The runtime code is original GPL-3.0-only code; kernel host-driver source was not copied.
