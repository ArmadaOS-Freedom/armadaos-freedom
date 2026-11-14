# Linux system update boot policy

Linux updates, rollback, boot and confirmation treat the device as **one
logical deployment**, regardless of which physical Android slot the bootloader
picked.  The advertised policy identifier is `dual-boot-no-slot-control`.
The offline RPC `info` result and every system-update status/apply result carry
that exact `bootPolicy` string, so clients cannot mistake this deployment for
legacy single-slot readiness.

## Transaction (online update and explicit rollback)

1. Read-only preflight observes the physical slots, the per-slot boot sources
   and their authenticated per-slot material.  Observation is evidence only;
   it is never a request to reboot into the other slot.
2. Online: the authenticated OSTree commit is appended as a new deployment and
   stays non-default until confirmation.  Rollback: the retained previous
   OSTree deployment is used as the target; nothing is fetched.
3. Boot materials are produced once per slot from that slot's own boot
   material, so both slots are rebuilt independently for the same deployment.
4. The non-running (inactive) slot is written first, fsynced and fully read
   back while the running image and the old journal are still valid.  The
   running slot is not touched at this point.
5. At the commit boundary, one `OFFLINE_DUAL` (v2) PREPARED trial record is
   written with compare-and-swap into the alternate copy of the dual-copy
   journal, fsynced and read back in full.  Only after that durable readback
   does the writer touch the running slot, which is also written and read
   back.  `fromSlot`/`targetSlot` index the two boot hashes and the write order
   only; `other_boot_sha256` carries the hash of the running slot's mirrored
   boot image.
6. Power loss before the commit boundary leaves the old journal and the old
   running image valid.  A pre-commit failure restores the already-written
   non-running image only when a full journal re-read proves the previous
   snapshot, including both record copies, is unchanged.  At or after the
   boundary the PREPARED journal and the verified non-running image always form
   one complete matching pair, so a committed matching image is never restored
   merely because a later status, report, pwrite/readback or journal I/O step
   failed.  A committed failure preserves that image and reports
   `recovery_required`; an ambiguous commit (including an fsync failure,
   ambiguous compare-and-swap, or unreadable readback) preserves it and reports
   `recovery_required` with `bootWriteState=uncertain` and
   `journalCommitState=ambiguous`.  These preserved failures carry
   `bootRestoreState=not_restored`.  Failure statuses never advertise a reboot;
   `requiresReboot=true` is emitted only after both slots and the journal have
   been verified.
7. Status reports `state=awaiting_reboot`, `bootWriteState=written`,
   `bootPolicy=dual-boot-no-slot-control` and `slotControlState=not_issued`.
   No Android slot selection, slot-success marking, retry/priority or UFS
   boot-LUN change, or Android BCB write is issued.

### Power-loss boundary and limits

The durability boundary is the journal record, not a single partition write.
A `pwrite`/`fsync`/readback of one boot partition is not claimed to be atomic
across a power interruption: a torn image is tolerated only because the other
image and the selected journal state still form a complete matching pair.  The
journal itself is copy-on-write, so the previously selected record copy stays
intact until the new record has been fsynced and re-read. A complete matching
image on one physical slot does not guarantee that Android will select it on
the next power-on. If power fails while the selected slot is being rewritten,
the other slot can hold the only verified image. The Linux updater does not
change Android's slot selection to boot that image.

The mirrored policy deliberately uses exactly the two physical boot slots.
There is no third Linux slot and no automated repartition or recovery: if both
mirrored images are unusable at once, the transaction reports the failure and
stops.  An extra selector, stale prepared record, or boot-control fallback is
never synthesized to escape that limit.

## Boot

The Android bootloader keeps owning slot selection and raw boot state.  Linux
only reads the boot witness and the running deployment.

## Confirmation

Confirmation accepts evidence from **either** physical slot when the running
deployment equals the mirrored deployment and that slot's boot partition hash
equals the corresponding recorded hash.  It then:

* writes the CONFIRMED trial record;
* commits the new deployment as the OSTree default (`promote_confirmed_deployment`);
* keeps the previous deployment retained and pinned as the rollback target;
* writes a finalization receipt with `firmwareSlotControl=not_issued`.

## Rollback

An explicit rollback repacks **both** physical slots from the retained previous
OSTree deployment, using the same non-running-then-commit-then-running order
and the same v2 trial record for the previous deployment.  Confirmation of that
record commits the previous deployment as the default and retains the newer
one.  There is no slot selection anywhere in the flow.

## Read-only dependencies that remain

* `physical_slots.inspect_physical_slots()` reports the observed boot witness
  slot and the controller-selected slot separately, together with the real
  per-slot Android bootable bits.  A pending switch or an Android-marked
  unbootable slot is telemetry, not a gate: the running slot still comes from
  the verified boot witness and both slots are mirrored from their own signed
  material.  An all-zero bootability witness (neither slot bootable) and an
  unparsable/all-zero GPT are still refused.  In the mirrored update path,
  drift comparisons ignore only these telemetry fields; the boot witness,
  deployment, recovery identity, LUN inventory, extents, sizes and trust
  material remain identity/race checks.
  This is read-only observation of Android-owned state; it does not write or
  request any slot change.
* The boot-time interpretation of the trial record (attempt accounting) is
  handled by the kShim built from this tree. It accounts for attempts on either physical slot without changing Android slot state.

## Runtime compatibility marker

The TB322FC/TB323FU kShim runtime retains the exact literal
`ARMADA_LINUX_MIRROR_V1` in its used update gate.  Boot materials, boot
candidate preparation and the EDL package builder reject a target runtime
without that literal before accepting or packaging the deployment, including
rollback and older signed packages.  The check is byte presence only, not an
authentication or boot claim.  It does not apply to TB710FU or to TF-card
packages, and legacy deployments remain readable through the existing
read-only inspection paths.

## What an internal Linux update preserves for Android

The source is the installed Boot and its companion partitions on each physical slot, not a stock Android package or an earlier backup. BootTool checks the Android base kernel again inside the rebuilt candidate. On the supported GBL route, it compares the signed boot metadata bytes with that slot's installed bytes. Before opening either boot target for writing, Linux independently requires the candidate metadata digest to equal that slot's authenticated metadata digest. On the AOSP route, only an already recognized project signing chain is eligible: the changed boot payload requires a new matching signed digest. An unknown or mixed chain is rejected; there is no fallback from GBL to AOSP.

The Boot writer targets only the two boot extents; the Linux deployment and the project's trial journal have their own writes. Installing Android partitions, resetting the bootloader, or restoring stock firmware is not part of a Linux update. Matching Android data and signature evidence is necessary but cannot prove the Android boot handoff or touchscreen works on hardware.

The automatic boot-image sync service and OSTree finalization hook do not run the older single-slot writer or the ABL ESP writer on an identified internal TB322FC/TB323FU boot. They check the board and root medium before consulting the older boot-method setting. This rule also covers the boot-time snapshot and does not require a pending update. A recognized internal boot can therefore restart without a second writer rewriting the verified Boot pair.
