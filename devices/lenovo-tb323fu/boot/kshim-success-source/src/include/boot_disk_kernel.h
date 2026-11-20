/* include/boot_disk_kernel.h */
#ifndef BOOT_DISK_KERNEL_H
#define BOOT_DISK_KERNEL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Prepare an already-authenticated Arm64 Linux Image that was loaded by the
 * disk reader at a 2MiB-aligned RAM base.
 *
 * Inputs:
 *   base        : RAM base of the loaded Image (must be 2MiB aligned)
 *   file_bytes  : number of bytes actually loaded (>= 64)
 *   capacity    : bytes reserved for the candidate kernel window
 *   load_addr   : out, target address where the image will execute
 *   image_bytes : out, effective image size after BSS zeroing
 *
 * Returns 0 on success; nonzero on validation failure. Outputs are written
 * only on success. Caller is responsible for cache maintenance and handoff.
 */
int kshim_disk_prepare_kernel(void *base,
                              size_t file_bytes,
                              size_t capacity,
                              uint64_t *load_addr,
                              uint64_t *image_bytes);

#ifdef __cplusplus
}
#endif

#endif /* BOOT_DISK_KERNEL_H */
