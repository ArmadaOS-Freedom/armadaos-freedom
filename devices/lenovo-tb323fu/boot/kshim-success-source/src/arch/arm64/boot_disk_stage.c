/* src/arch/arm64/boot_disk_stage.c */
#include "boot_disk_stage.h"
#include "boot_disk_native.h"
#include "boot_disk_memory.h"
#include "boot_disk_kernel.h"

#include <stdint.h>
#include <string.h>
#include <libfdt.h>

#define KSHIM_KERNEL_OFFSET 0
#define KSHIM_KERNEL_CAP    (130ULL * 1024 * 1024)
#define KSHIM_INITRD_OFFSET (130ULL * 1024 * 1024)
#define KSHIM_INITRD_CAP    (128ULL * 1024 * 1024)
#define KSHIM_DTB_OFFSET    (130ULL * 1024 * 1024 + 128ULL * 1024 * 1024)
#define KSHIM_DTB_CAP       (256ULL * 1024)
#define KSHIM_WINDOW_BYTES  (KSHIM_DTB_OFFSET + KSHIM_DTB_CAP)

int kshim_disk_boot_stage(const void *runtime_fdt, size_t runtime_fdt_bytes,
                          const struct dt_range *excluded, size_t excluded_count,
                          const struct kshim_disk_entry *entry,
                          const uint8_t pubkey[65], uint32_t board,
                          struct kshim_disk_boot_image *out,
                          void *scratch, size_t scratch_bytes)
{
    if (out == 0)
        return -1;

    uint64_t base = 0;
    int rc = kshim_disk_boot_find_memory(runtime_fdt, runtime_fdt_bytes,
                                         KSHIM_WINDOW_BYTES,
                                         excluded, excluded_count, &base);
    if (rc != 0)
        return rc;

    struct kshim_disk_boot_buffers buffers;
    buffers.kernel = (void *)(uintptr_t)(base + KSHIM_KERNEL_OFFSET);
    buffers.kernel_capacity = 128ULL * 1024 * 1024;
    buffers.initrd = (void *)(uintptr_t)(base + KSHIM_INITRD_OFFSET);
    buffers.initrd_capacity = (size_t)KSHIM_INITRD_CAP;
    buffers.dtb = (void *)(uintptr_t)(base + KSHIM_DTB_OFFSET);
    buffers.dtb_capacity = (size_t)KSHIM_DTB_CAP;

    struct kshim_disk_manifest manifest;
    struct kshim_disk_binding binding;
    rc = kshim_disk_boot_native_read(entry, pubkey, board, &buffers, &manifest,
                                     &binding, scratch, scratch_bytes);
    if (rc != 0)
        return rc;

    struct kshim_disk_boot_image staged;
    rc = kshim_disk_prepare_kernel(buffers.kernel, manifest.kernel_bytes,
                                   (size_t)KSHIM_KERNEL_CAP,
                                   &staged.kernel_start, &staged.kernel_bytes);
    if (rc != 0)
        return rc;

    rc = dt_validate(buffers.dtb, manifest.dtb_bytes);
    if (rc != 0)
        return rc;

    int chosen = fdt_path_offset(buffers.dtb, "/chosen");
    if (chosen < 0)
        return chosen;
    int commit_len = 0;
    const char *release_commit = fdt_getprop(buffers.dtb, chosen,
                                            "armada,release-commit", &commit_len);
    if (release_commit == NULL || commit_len != (int)sizeof(binding.commit) ||
        memcmp(release_commit, binding.commit, sizeof(binding.commit)) != 0)
        return -FDT_ERR_BADVALUE;

    staged.initrd_start = (uint64_t)(uintptr_t)buffers.initrd;
    staged.initrd_bytes = manifest.initrd_bytes;
    staged.dtb = buffers.dtb;
    staged.dtb_bytes = (size_t)manifest.dtb_bytes;
    staged.binding = binding;

    *out = staged;
    return 0;
}
