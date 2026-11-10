#ifndef KSHIM_BOOT_WITNESS_H
#define KSHIM_BOOT_WITNESS_H

/*
 * Boot-time slot and lock witnesses.
 *
 * Freeze evidence from the incoming Android firmware DT before any menu or
 * slot configuration mutates state, and publish read-only witness properties
 * into the private Linux handoff DT.
 */

/*
 * Read the booted slot from the firmware DT /chosen/bootargs.
 * Returns 0 for slot _a, 1 for slot _b, or -1 when the evidence is
 * absent, malformed, or conflicting (including duplicate keys).
 * Pure function: never modifies the input DT.
 */
int kshim_boot_slot_read(const void *firmware_fdt);

/*
 * Read boot-lock state solely from original firmware DT /chosen/bootargs.
 * androidboot.verifiedbootstate is required: green/yellow mean locked and
 * orange means unlocked. vbmeta.device_state and flash.locked may only
 * corroborate it. Returns 0 for unknown, 1 for locked, or 2 for unlocked.
 * Missing or bootconfig-only evidence, malformed values, bare keys,
 * duplicates, and conflicts return 0. Never modifies the input DT.
 */
int kshim_boot_lock_read(const void *firmware_fdt);

/*
 * Export the boot slot witness into the Linux handoff DT as a single
 * property "armada,boot-witness" with 4 big-endian u32 cells:
 * [version=1, source=1 (firmware bootargs), slot, flags=0].
 * A slot outside 0/1 still performs removal of any stale witness and
 * returns 0 (no evidence published). Returns a libfdt error code or 0.
 */
int kshim_boot_witness_export(void *linux_fdt, int booted_slot);

/*
 * Export "armada,boot-lock" as 4 big-endian u32 cells:
 * [version=1, source=1 (firmware bootargs), state, flags=0].
 * State 1 is locked and state 2 is unlocked. Any other state clears stale
 * evidence without publishing a property. Does not create /chosen.
 * Returns a libfdt error code or 0.
 */
int kshim_boot_lock_export(void *linux_fdt, int state);

#endif /* KSHIM_BOOT_WITNESS_H */
