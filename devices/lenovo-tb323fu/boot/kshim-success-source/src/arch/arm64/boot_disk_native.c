/* src/arch/arm64/boot_disk_native.c */
/* SPDX-License-Identifier: GPL-3.0-only */

#include "boot_disk_native.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "boot_ext4.h"

#include "storage_settings_native.h"
#include "kshim_mmu.h"

struct kshim_boot_native_ctx {
    const struct kshim_disk_entry *entry;
    const uint8_t *public_key;          /* points to the 65-byte array */
    uint32_t board;
    void *dtb_scratch;
    size_t dtb_scratch_bytes;
    kshim_disk_boot_place_fn place;
    void *place_ctx;
    struct kshim_disk_boot_buffers *buffers;
    void *scratch;
    size_t scratch_bytes;
    struct kshim_disk_manifest manifest; /* filled only on full success */
    struct kshim_disk_binding binding;   /* filled only on full success */
};

static int kshim_boot_native_verify_and_read(struct kshim_boot_native_ctx *ctx,
                                             const struct kshim_blockdev *dev)
{
    struct kshim_partition part;
    uint8_t fs_uuid[16];
    int rc;

    if (ctx == NULL || dev == NULL || ctx->entry == NULL ||
        ctx->public_key == NULL || ctx->buffers == NULL ||
        ctx->dtb_scratch == NULL || ctx->dtb_scratch_bytes == 0 ||
        ctx->place == NULL ||
        ctx->scratch == NULL || ctx->scratch_bytes == 0)
        return KSHIM_STORAGE_ARG;

    memset(&part, 0, sizeof(part));

    /* Locate the ARMADA_BOOT partition (reuses caller scratch). */
    rc = kshim_gpt_find_linux_boot(dev, &part, ctx->scratch, ctx->scratch_bytes);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    /* Verify the ext4 filesystem UUID against the signed descriptor. */
    memset(fs_uuid, 0, sizeof(fs_uuid));
    rc = kshim_ext4_read_uuid(dev, &part, fs_uuid,
                              ctx->scratch, ctx->scratch_bytes);
    if (rc != 0)
        return rc;

    if (memcmp(fs_uuid, ctx->entry->boot_uuid, sizeof(fs_uuid)) != 0)
        return KSHIM_STORAGE_FORMAT;

    /*
     * Read the deployment binding from the verified boot filesystem
     * before loading the large payload. Uses a fixed-size stack buffer
     * plus a separate scratch region so the caller's scratch is not
     * disturbed before the large payload read.
     */
    {
        uint8_t binding_bytes[KSHIM_DISK_BINDING_BYTES];
        size_t actual = 0;

        memset(binding_bytes, 0, sizeof(binding_bytes));

        rc = kshim_ext4_read_file(dev, &part, "/armada/boot/binding.bin",
                                  binding_bytes, sizeof(binding_bytes),
                                  &actual, ctx->scratch,
                                  ctx->scratch_bytes);
        if (rc != 0)
            return rc;

        if (actual != (size_t)KSHIM_DISK_BINDING_BYTES)
            return KSHIM_STORAGE_FORMAT;

        memset(&ctx->binding, 0, sizeof(ctx->binding));
        rc = kshim_disk_binding_decode(binding_bytes, actual, &ctx->binding);
        if (rc != 0)
            return rc;
    }

    /* Signature verify, one authenticated DTB read into dtb_scratch,
     * placement callback, then payload reads into final buffers. Never
     * publish the manifest or binding until this returns success. */
    rc = kshim_disk_boot_read(dev, &part, ctx->public_key, ctx->board,
                              ctx->dtb_scratch, ctx->dtb_scratch_bytes,
                              ctx->place, ctx->place_ctx,
                              ctx->buffers, &ctx->manifest,
                              ctx->scratch, ctx->scratch_bytes);
    if (rc != 0)
        return rc;

    /* Payload buffers are now populated: flush each so the native
     * wrapper can safely disable the MMU before entering the kernel. */
    kshim_mmu_clean_payload((uint64_t)(uintptr_t)ctx->buffers->kernel,
                            ctx->manifest.kernel_bytes);
    kshim_mmu_clean_payload((uint64_t)(uintptr_t)ctx->buffers->initrd,
                            ctx->manifest.initrd_bytes);
    kshim_mmu_clean_payload((uint64_t)(uintptr_t)ctx->buffers->dtb,
                            ctx->manifest.dtb_bytes);
    return KSHIM_STORAGE_OK;
}

static int kshim_boot_native_reader(const struct kshim_blockdev *dev, void *opaque)
{
    struct kshim_boot_native_ctx *ctx = opaque;

    if (ctx == NULL)
        return KSHIM_STORAGE_ARG;

    return kshim_boot_native_verify_and_read(ctx, dev);
}

int kshim_disk_boot_native_read(const struct kshim_disk_entry *entry,
                                const uint8_t public_key[65],
                                uint32_t board,
                                void *dtb_scratch,
                                size_t dtb_scratch_bytes,
                                kshim_disk_boot_place_fn place,
                                void *place_ctx,
                                struct kshim_disk_boot_buffers *buffers,
                                struct kshim_disk_manifest *out,
                                struct kshim_disk_binding *out_binding,
                                void *scratch,
                                size_t scratch_bytes)
{
    struct kshim_boot_native_ctx ctx;
    int rc;

    if (entry == NULL || public_key == NULL || buffers == NULL || out == NULL ||
        out_binding == NULL || dtb_scratch == NULL ||
        dtb_scratch_bytes == 0 || place == NULL ||
        scratch == NULL || scratch_bytes == 0)
        return KSHIM_STORAGE_ARG;

    memset(&ctx, 0, sizeof(ctx));
    ctx.entry = entry;
    ctx.public_key = public_key;
    ctx.board = board;
    ctx.dtb_scratch = dtb_scratch;
    ctx.dtb_scratch_bytes = dtb_scratch_bytes;
    ctx.place = place;
    ctx.place_ctx = place_ctx;
    ctx.buffers = buffers;
    ctx.scratch = scratch;
    ctx.scratch_bytes = scratch_bytes;

    rc = storage_settings_native_read_boot(kshim_boot_native_reader, &ctx);
    if (rc != 0) {
        /* Controller release or callback failure: do not publish. */
        return rc;
    }

    /* Publish the manifest and binding only after the native call (and
     * its controller release) succeeded. */
    *out = ctx.manifest;
    *out_binding = ctx.binding;
    return KSHIM_STORAGE_OK;
}
