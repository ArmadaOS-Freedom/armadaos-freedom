/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <storage_block.h>

#define KSHIM_AB_LUNS 6
#define KSHIM_AB_UNCERTAIN (-30)
struct kshim_ab_media {
    struct kshim_blockdev lun[KSHIM_AB_LUNS];
    uint8_t boot_id[KSHIM_AB_LUNS];
    void *ctx;
    int (*get_boot_lun)(void *, uint32_t *);
    int (*set_boot_lun)(void *, uint32_t);
};
struct kshim_ab_status { unsigned current; unsigned bootable[2]; };
/* Qualcomm GPT slot layout, with zero AOSP misc control. No GPT repair,
 * unlock, AVB rewriting, or payload writes. A failed rollback is UNCERTAIN. */
int kshim_ab_inspect(const struct kshim_ab_media *, struct kshim_ab_status *);
/* Read-only lookup of a partition's extent (first/last LBA, inclusive) and
 * unique GUID (canonical RFC 4122 byte order) by ASCII label, reusing the
 * same fully validated six-LUN GPT parse as kshim_ab_inspect (CRC,
 * backup-table agreement, misc/OTA/current checks). Performs no writes,
 * mutation, or repair, and implies no AVB trust decision. Rejects missing
 * and duplicate labels. */
int kshim_ab_find_partition(const struct kshim_ab_media *, const char *label,
                            unsigned *lun, struct kshim_partition *partition);
int kshim_ab_select(const struct kshim_ab_media *, unsigned target, unsigned expected_current);
/* Mark the current slot SUCCESS after the caller has independently verified
 * Linux health (booted from the current slot and validated it). Only touches
 * the current boot partition's attribute bits: sets SUCCESS, clears RETRIES
 * and UNBOOTABLE, preserves all other bits. Idempotent: no writes when the
 * attributes already match. Never switches slots or changes Boot LUN. */
int kshim_ab_mark_success(const struct kshim_ab_media *, unsigned expected_current);
