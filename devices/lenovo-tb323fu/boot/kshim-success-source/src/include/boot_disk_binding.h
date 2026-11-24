/* boot_disk_binding.h */
#ifndef BOOT_DISK_BINDING_H
#define BOOT_DISK_BINDING_H

#include <stddef.h>
#include <stdint.h>
#include "boot_disk_entry.h"

#define KSHIM_DISK_BINDING_BYTES 152u

struct kshim_disk_binding {
    uint8_t  version;
    char     boot_checksum[65];
    char     commit[65];
    uint32_t serial;
    uint32_t index;
};

int kshim_disk_binding_decode(const void *data, size_t bytes,
                              struct kshim_disk_binding *out);

int kshim_disk_bootargs(char out[2048], const char *signed_args,
                        const struct kshim_disk_entry *entry,
                        const struct kshim_disk_binding *binding,
                        uint32_t board, int auto_return);

#endif /* BOOT_DISK_BINDING_H */
