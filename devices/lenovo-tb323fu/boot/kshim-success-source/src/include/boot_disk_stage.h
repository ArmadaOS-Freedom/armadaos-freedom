/* src/include/boot_disk_stage.h */
#ifndef KSHIM_BOOT_DISK_STAGE_H
#define KSHIM_BOOT_DISK_STAGE_H

#include "boot_disk_entry.h"
#include "boot_disk_binding.h"
#include "dt.h"

#include <stddef.h>
#include <stdint.h>

struct kshim_disk_boot_image {
    uint64_t kernel_start, kernel_bytes, initrd_start, initrd_bytes;
    const void *dtb;
    size_t dtb_bytes;
    struct kshim_disk_binding binding;
};

int kshim_disk_boot_stage(const void *runtime_fdt, size_t runtime_fdt_bytes,
                          const struct dt_range *excluded, size_t excluded_count,
                          const struct kshim_disk_entry *entry,
                          const uint8_t pubkey[65], uint32_t board,
                          struct kshim_disk_boot_image *out,
                          void *scratch, size_t scratch_bytes);

#endif
