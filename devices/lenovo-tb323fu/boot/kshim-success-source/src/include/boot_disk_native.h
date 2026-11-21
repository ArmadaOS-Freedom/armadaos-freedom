/* src/include/boot_disk_native.h */
/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_BOOT_DISK_NATIVE_H
#define KSHIM_BOOT_DISK_NATIVE_H

#include <stddef.h>
#include <stdint.h>

#include "boot_disk_entry.h"
#include "boot_disk_load.h"
#include "boot_disk_manifest.h"
#include "boot_disk_binding.h"
#include "storage_block.h"

/*
 * Firmware bridge: read an authenticated boot payload from the native
 * storage controller via storage_settings_native_read_boot().
 *
 * Contract:
 *  - No allocation, no writes, no jumps, no Android/TF/mem-schedule logic.
 *  - Caller owns every buffer, dtb_scratch and the scratch region; none of
 *    them may overlap each other or the entry descriptor. dtb_scratch is
 *    used solely to authenticate the board DTB before placement.
 *  - place/place_ctx are forwarded to kshim_disk_boot_read unchanged; the
 *    placement callback fills buffers->kernel/initrd/dtb after the DTB has
 *    been authenticated. The callback performs no I/O and runs while the
 *    native controller ownership is active.
 *  - The callback handed to storage_settings_native_read_boot is static
 *    and completes synchronously while the controller ownership is active.
 *  - On success the final payload buffers (including the copied DTB) are
 *    cache-cleaned before return. *out and *out_binding are published only
 *    after storage_settings_native_read_boot returns success (including
 *    controller release). On any failure the caller's outputs are
 *    untouched. out_binding must be non-NULL.
 *
 * Returns KSHIM_STORAGE_OK on success, nonzero on failure.
 */
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
                                size_t scratch_bytes);

#endif /* KSHIM_BOOT_DISK_NATIVE_H */
