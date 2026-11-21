/* SPDX-License-Identifier: MIT */
/* src/include/boot_disk_memory.h */
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "dt.h"

/*
 * Locate a bounded, 2 MiB aligned RAM window of `bytes` bytes for the
 * disk-boot path.  The window is placed inside a single enabled
 * /memory (or /memory@...) bank, below the 4 GiB MMU coverage limit,
 * and must not overlap any DT reservation (with or without no-map),
 * nor any caller supplied excluded range.
 *
 * Returns 0 and writes *base on success; negative on failure.
 * Requires that fdt has already passed dt_validate().
 */
int kshim_disk_boot_find_memory(const void *fdt, size_t fdt_bytes,
                                uint64_t bytes,
                                const struct dt_range *excluded,
                                size_t excluded_count,
                                uint64_t *base);
