/* boot_disk_manifest.h */
#ifndef BOOT_DISK_MANIFEST_H
#define BOOT_DISK_MANIFEST_H

#include <stddef.h>
#include <stdint.h>

#define KSHIM_DISK_MANIFEST_WIRE_BYTES 160u

struct kshim_disk_manifest {
    uint64_t kernel_bytes;
    uint64_t initrd_bytes;
    uint64_t dtb_bytes;
    uint8_t kernel_sha256[32];
    uint8_t initrd_sha256[32];
    uint8_t dtb_sha256[32];
};

/* Returns 0 on success, nonzero on any failure. */
int kshim_disk_manifest_verify(const uint8_t data[160],
                               const uint8_t *signature,
                               size_t sig_bytes,
                               const uint8_t public_key[65],
                               uint32_t expected_board,
                               struct kshim_disk_manifest *out);

#endif /* BOOT_DISK_MANIFEST_H */
