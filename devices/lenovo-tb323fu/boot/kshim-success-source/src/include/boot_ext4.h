/* src/include/boot_ext4.h */
/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_BOOT_EXT4_H
#define KSHIM_BOOT_EXT4_H

#include <stddef.h>
#include <stdint.h>

#include "storage_block.h"

#define KSHIM_EXT4_SCRATCH_BYTES 16384u

/* Freestanding, strictly read-only ext4 file reader over an already-validated
 * partition. Supports the subset: 32/64-byte group descriptors, 256-byte
 * inodes, extents (max depth 4 with depth/index checks), no inline data,
 * regular files only, fast symlinks, journaled layouts
 * read without replay. Returns 0 on success, nonzero on error. On error,
 * bytes_out is zeroed (if non-NULL) and never reports success. */
int kshim_ext4_read_file(const struct kshim_blockdev *dev,
                         const struct kshim_partition *part,
                         const char *path,
                         void *out,
                         size_t capacity,
                         size_t *bytes_out,
                         void *scratch,
                         size_t scratch_bytes);

int kshim_ext4_read_uuid(const struct kshim_blockdev *dev, const struct kshim_partition *part, uint8_t out[16], void *scratch, size_t scratch_bytes);

#endif /* KSHIM_BOOT_EXT4_H */
