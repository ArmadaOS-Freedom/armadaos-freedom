/* src/include/boot_disk_entry.h */
#ifndef KSHIM_BOOT_DISK_ENTRY_H
#define KSHIM_BOOT_DISK_ENTRY_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct kshim_disk_entry {
    bool diagnostic;
    uint8_t boot_uuid[16];
    uint8_t root_uuid[16];
};

/*
 * Decode an ARMDSK1 DiskLinux descriptor.
 *
 * Format:
 *   [ 0..7]   magic "ARMDSK1\0"
 *   [ 8..11]  LE32 version (must be 1)
 *   [12..15]  LE32 diagnostic flag (must be 0 or 1)
 *   [16..31]  boot ext4 filesystem UUID (canonical byte order)
 *   [32..47]  root ext4 filesystem UUID (canonical byte order)
 *   [48..63]  reserved, must be all zero
 *
 * Returns 0 on success and fills *out.
 * Returns -1 on any error; *out is left untouched.
 */
int kshim_disk_entry_decode(const void *data, size_t bytes,
                            struct kshim_disk_entry *out);

#endif
