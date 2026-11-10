/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_UPDATE_TRIAL_BOOT_H
#define KSHIM_UPDATE_TRIAL_BOOT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "storage_block.h"

/*
 * Early-boot trial gate on top of native storage and parser code; this module
 * never reboots directly, never writes payload partitions, and performs no
 * allocations. An explicit prepared transaction must accompany a trial boot;
 * legacy boots are preserved.
 */

/* Positive: caller must restart boot (slot switched). */
#define KSHIM_UPDATE_BOOT_RESTART_REQUIRED 1

/*
 * Returns 0 to continue boot, KSHIM_UPDATE_BOOT_RESTART_REQUIRED when the
 * boot LUN was switched back to the old slot and boot must restart, or a
 * negative kshim_storage_result / native error code to refuse handoff.
 */
int kshim_update_trial_boot(int actual_slot, bool internal_linux,
                            const char *cmdline, size_t bytes,
                            const void *transaction, size_t transaction_bytes);

#endif
