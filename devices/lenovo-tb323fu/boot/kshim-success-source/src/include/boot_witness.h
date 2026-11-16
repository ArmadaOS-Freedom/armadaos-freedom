#ifndef KSHIM_BOOT_WITNESS_H
#define KSHIM_BOOT_WITNESS_H

/*
 * Boot-time slot and lock witnesses.
 *
 * Freeze evidence from the incoming Android firmware DT before any menu or
 * slot configuration mutates state, and publish read-only witness properties
 * into the private Linux handoff DT.
 */

/* Evidence sources published in the witness "source" cell. */
#define KSHIM_BOOT_EVIDENCE_BOOTARGS 1   /* firmware /chosen/bootargs */
#define KSHIM_BOOT_EVIDENCE_BOOTCONFIG 2 /* firmware initrd bootconfig */

/*
 * Read the booted slot from the firmware DT /chosen/bootargs.
 * Returns 0 for slot _a, 1 for slot _b, or -1 when the evidence is
 * absent, malformed, or conflicting (including duplicate keys).
 *
 * When bootargs carries no androidboot.slot_suffix token at all but does
 * carry the exact "bootconfig" switch among the kernel parameters before any
 * "--", the AOSP bootconfig trailer at the end
 * of the firmware initrd (/chosen linux,initrd-start..end, inside a declared
 * memory range, with a valid size and checksum) is read instead with the same
 * duplicate, bare-key and value rules. Bootargs evidence, valid or not, is
 * never combined with bootconfig. *source (optional) receives
 * KSHIM_BOOT_EVIDENCE_BOOTARGS or KSHIM_BOOT_EVIDENCE_BOOTCONFIG for a known
 * slot and 0 otherwise.
 * Pure functions: never modify the input DT or the initrd.
 */
int kshim_boot_slot_read_source(const void *firmware_fdt, int *source);
int kshim_boot_slot_read(const void *firmware_fdt);

/*
 * Read boot-lock state from original firmware DT /chosen/bootargs, or from
 * the firmware bootconfig under the same rule as kshim_boot_slot_read_source()
 * when bootargs carries none of the three keys below.
 * androidboot.verifiedbootstate is required: green/yellow mean locked and
 * orange means unlocked. vbmeta.device_state and flash.locked may only
 * corroborate it. Returns 0 for unknown, 1 for locked, or 2 for unlocked.
 * Missing evidence, malformed values, bare keys, duplicates, and conflicts
 * return 0. *source (optional) is set as for the slot reader.
 * Never modifies the input DT.
 */
int kshim_boot_lock_read_source(const void *firmware_fdt, int *source);
int kshim_boot_lock_read(const void *firmware_fdt);

/*
 * Export the boot slot witness into the Linux handoff DT as a single
 * property "armada,boot-witness" with 4 big-endian u32 cells:
 * [version=1, source, slot, flags=0], where source is
 * KSHIM_BOOT_EVIDENCE_BOOTARGS or KSHIM_BOOT_EVIDENCE_BOOTCONFIG.
 * A slot outside 0/1 or an unknown source still performs removal of any
 * stale witness and returns 0 (no evidence published). Returns a libfdt error
 * code or 0. kshim_boot_witness_export() publishes source 1.
 */
int kshim_boot_witness_export_source(void *linux_fdt, int booted_slot,
                                     int source);
int kshim_boot_witness_export(void *linux_fdt, int booted_slot);

/*
 * Export "armada,boot-lock" as 4 big-endian u32 cells:
 * [version=1, source, state, flags=0] with source as for the witness.
 * State 1 is locked and state 2 is unlocked. Any other state or source clears
 * stale evidence without publishing a property. Does not create /chosen.
 * Returns a libfdt error code or 0. kshim_boot_lock_export() publishes
 * source 1.
 */
int kshim_boot_lock_export_source(void *linux_fdt, int state, int source);
int kshim_boot_lock_export(void *linux_fdt, int state);

#endif /* KSHIM_BOOT_WITNESS_H */
