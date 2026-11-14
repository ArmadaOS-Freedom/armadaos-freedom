/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_UPDATE_TRIAL_BOOT_H
#define KSHIM_UPDATE_TRIAL_BOOT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "storage_block.h"

/*
 * Early-boot trial gate on top of native storage and parser code; this module
 * never reboots directly, never writes payload partitions, performs no
 * allocations, and never issues Android slot controls (selection, success
 * marking, or boot-LUN changes). An explicit prepared transaction must
 * accompany a trial boot; legacy boots are preserved.
 */

/* Retained for existing callers; the gate never requests a restart. */
#define KSHIM_UPDATE_BOOT_RESTART_REQUIRED 1

/*
 * Exact stable literal retained in the runtime binary by the used gate path.
 * Installer and package tooling require it before accepting an online, offline
 * or rollback runtime under the TB322FC/TB323FU mirrored policy. It is a
 * compatibility marker only: no role, Android slot or trust decision reads it.
 */
extern const char kshim_update_trial_boot_compat_marker[];

/*
 * Returns 0 to continue boot, or a negative kshim_storage_result / native
 * error code to refuse handoff.
 */
int kshim_update_trial_boot(int actual_slot, bool internal_linux,
                            const char *cmdline, size_t bytes,
                            const void *transaction, size_t transaction_bytes);

#endif
