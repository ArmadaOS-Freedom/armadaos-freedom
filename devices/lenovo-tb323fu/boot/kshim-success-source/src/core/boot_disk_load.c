/* SPDX-License-Identifier: GPL-3.0-only */
#include "boot_disk_load.h"
#include "boot_disk_manifest.h"
#include "boot_ext4.h"

#include <bearssl_hash.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>

#define KSHIM_MANIFEST_SIG_MAX  72u

#define KSHIM_BOARD_TB322FC     1u
#define KSHIM_BOARD_TB323FU     2u

struct kshim_board_paths {
    uint32_t board;
    const char *manifest;
    const char *manifest_sig;
    const char *dtb;
};

static const struct kshim_board_paths kshim_board_table[] = {
    {
        KSHIM_BOARD_TB322FC,
        "/armada/boot/TB322FC/manifest.bin",
        "/armada/boot/TB322FC/manifest.sig",
        "/armada/boot/TB322FC/board.dtb",
    },
    {
        KSHIM_BOARD_TB323FU,
        "/armada/boot/TB323FU/manifest.bin",
        "/armada/boot/TB323FU/manifest.sig",
        "/armada/boot/TB323FU/board.dtb",
    },
};

#define KSHIM_KERNEL_PATH       "/armada/boot/kernel.img"
#define KSHIM_INITRD_PATH       "/armada/boot/initramfs.img"

static const struct kshim_board_paths *kshim_board_lookup(uint32_t board)
{
    size_t i;

    for (i = 0; i < sizeof(kshim_board_table) / sizeof(kshim_board_table[0]); i++) {
        if (kshim_board_table[i].board == board) {
            return &kshim_board_table[i];
        }
    }
    return NULL;
}

static int read_hashed(const struct kshim_blockdev *dev,
                       const struct kshim_partition *part,
                       const char *path,
                       void *dst,
                       size_t expected_bytes,
                       const uint8_t expected_sha256[32],
                       void *scratch,
                       size_t scratch_bytes)
{
    size_t actual = 0;
    br_sha256_context ctx;
    uint8_t computed[32];
    int rc;

    if (dev == NULL || part == NULL || path == NULL || dst == NULL ||
        expected_sha256 == NULL) {
        return KSHIM_STORAGE_CRC;
    }
    if (expected_bytes == 0 || expected_bytes > (size_t)-1) {
        return KSHIM_STORAGE_CRC;
    }

    rc = kshim_ext4_read_file(dev, part, path, dst, expected_bytes,
                              &actual, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }
    if (actual != expected_bytes) {
        return KSHIM_STORAGE_CRC;
    }

    br_sha256_init(&ctx);
    br_sha256_update(&ctx, dst, expected_bytes);
    br_sha256_out(&ctx, computed);

    if (memcmp(computed, expected_sha256, 32) != 0) {
        return KSHIM_STORAGE_CRC;
    }
    return 0;
}

int kshim_disk_boot_read(const struct kshim_blockdev *dev,
                         const struct kshim_partition *part,
                         const uint8_t public_key[65],
                         uint32_t board,
                         void *dtb_scratch,
                         size_t dtb_scratch_bytes,
                         kshim_disk_boot_place_fn place,
                         void *place_ctx,
                         struct kshim_disk_boot_buffers *buffers,
                         struct kshim_disk_manifest *out_manifest,
                         void *scratch,
                         size_t scratch_bytes)
{
    uint8_t manifest_raw[KSHIM_DISK_MANIFEST_WIRE_BYTES];
    uint8_t manifest_sig[KSHIM_MANIFEST_SIG_MAX];
    size_t sig_actual = 0;
    size_t manifest_actual = 0;
    struct kshim_disk_manifest verified;
    const struct kshim_board_paths *paths;
    int rc;

    if (out_manifest == NULL) {
        return KSHIM_STORAGE_CRC;
    }
    memset(out_manifest, 0, sizeof(*out_manifest));

    if (dev == NULL || part == NULL || public_key == NULL || buffers == NULL) {
        return KSHIM_STORAGE_CRC;
    }
    if (dtb_scratch == NULL || dtb_scratch_bytes == 0 || place == NULL) {
        return KSHIM_STORAGE_CRC;
    }
    if (scratch == NULL || scratch_bytes < KSHIM_EXT4_SCRATCH_BYTES) {
        return KSHIM_STORAGE_CRC;
    }

    paths = kshim_board_lookup(board);
    if (paths == NULL) {
        return KSHIM_STORAGE_CRC;
    }

    rc = kshim_ext4_read_file(dev, part, paths->manifest,
                              manifest_raw, sizeof(manifest_raw),
                              &manifest_actual, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }
    if (manifest_actual != KSHIM_DISK_MANIFEST_WIRE_BYTES) {
        return KSHIM_STORAGE_CRC;
    }

    rc = kshim_ext4_read_file(dev, part, paths->manifest_sig,
                              manifest_sig, sizeof(manifest_sig),
                              &sig_actual, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }
    if (sig_actual == 0 || sig_actual > sizeof(manifest_sig)) {
        return KSHIM_STORAGE_CRC;
    }

    rc = kshim_disk_manifest_verify(manifest_raw, manifest_sig, sig_actual,
                                    public_key, board, &verified);
    if (rc != 0) {
        return rc;
    }

    if (verified.kernel_bytes == 0 || verified.initrd_bytes == 0 ||
        verified.dtb_bytes == 0) {
        return KSHIM_STORAGE_CRC;
    }
    if (verified.dtb_bytes > (uint64_t)dtb_scratch_bytes) {
        return KSHIM_STORAGE_CRC;
    }

    /* Authenticate the board DTB exactly once, into dtb_scratch, before any
     * placement decision. The bytes in dtb_scratch are the only DTB source
     * the placement callback may rely on. */
    rc = read_hashed(dev, part, paths->dtb, dtb_scratch,
                     (size_t)verified.dtb_bytes,
                     verified.dtb_sha256, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }

    /* Placement: caller fills all final buffers based on the verified DTB.
     * No I/O or mutation of the verified DTB is permitted in the callback. */
    rc = place(place_ctx, dtb_scratch, (size_t)verified.dtb_bytes, buffers);
    if (rc != 0) {
        return KSHIM_STORAGE_CRC;
    }

    if (buffers->kernel == NULL || buffers->initrd == NULL ||
        buffers->dtb == NULL) {
        return KSHIM_STORAGE_CRC;
    }
    if (verified.kernel_bytes > (uint64_t)buffers->kernel_capacity) {
        return KSHIM_STORAGE_CRC;
    }
    if (verified.initrd_bytes > (uint64_t)buffers->initrd_capacity) {
        return KSHIM_STORAGE_CRC;
    }
    if (verified.dtb_bytes > (uint64_t)buffers->dtb_capacity) {
        return KSHIM_STORAGE_CRC;
    }

    /* Reject null/overflow/overlap using the verified byte ranges before any
     * payload read. Capacities keep their meaning; overlaps are checked
     * pairwise over the actual verified lengths as uintptr_t. The three paths
     * are disjoint exact board paths proven by code (distinct board table
     * entries and fixed kernel/initrd paths), so buffers must be disjoint. */
    {
        uintptr_t k = (uintptr_t)buffers->kernel;
        uintptr_t i = (uintptr_t)buffers->initrd;
        uintptr_t d = (uintptr_t)buffers->dtb;
        uintptr_t kb = (uintptr_t)verified.kernel_bytes;
        uintptr_t ib = (uintptr_t)verified.initrd_bytes;
        uintptr_t db = (uintptr_t)verified.dtb_bytes;

        if (k == 0 || i == 0 || d == 0) {
            return KSHIM_STORAGE_CRC;
        }
        if (kb > (uintptr_t)-1 - k || ib > (uintptr_t)-1 - i ||
            db > (uintptr_t)-1 - d) {
            return KSHIM_STORAGE_CRC;
        }
        if (k < i + ib && i < k + kb) {
            return KSHIM_STORAGE_CRC;
        }
        if (k < d + db && d < k + kb) {
            return KSHIM_STORAGE_CRC;
        }
        if (i < d + db && d < i + ib) {
            return KSHIM_STORAGE_CRC;
        }
    }

    rc = read_hashed(dev, part, KSHIM_KERNEL_PATH, buffers->kernel,
                     (size_t)verified.kernel_bytes,
                     verified.kernel_sha256, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }

    rc = read_hashed(dev, part, KSHIM_INITRD_PATH, buffers->initrd,
                     (size_t)verified.initrd_bytes,
                     verified.initrd_sha256, scratch, scratch_bytes);
    if (rc != 0) {
        return rc;
    }

    /* Copy the already-verified DTB. Do not reread from storage. */
    memmove(buffers->dtb, dtb_scratch, (size_t)verified.dtb_bytes);

    *out_manifest = verified;
    return 0;
}
