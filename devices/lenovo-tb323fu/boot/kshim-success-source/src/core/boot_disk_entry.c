/* src/core/boot_disk_entry.c */
#include "boot_disk_entry.h"

#include <string.h>

#define KSHIM_DISK_ENTRY_SIZE 64u
#define KSHIM_DISK_ENTRY_MAGIC "ARMDSK1\0"

static uint32_t read_le32(const uint8_t *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

static bool is_zero_bytes(const uint8_t *p, size_t n)
{
    size_t i;
    for (i = 0; i < n; i++) {
        if (p[i] != 0u) {
            return false;
        }
    }
    return true;
}

int kshim_disk_entry_decode(const void *data, size_t bytes,
                            struct kshim_disk_entry *out)
{
    const uint8_t *p = (const uint8_t *)data;
    uint32_t version;
    uint32_t diagnostic;
    struct kshim_disk_entry entry;

    if (data == NULL || out == NULL) {
        return -1;
    }

    if (bytes != KSHIM_DISK_ENTRY_SIZE) {
        return -1;
    }

    if (memcmp(p, KSHIM_DISK_ENTRY_MAGIC, 8u) != 0) {
        return -1;
    }

    version = read_le32(p + 8);
    if (version != 1u) {
        return -1;
    }

    diagnostic = read_le32(p + 12);
    if (diagnostic != 0u && diagnostic != 1u) {
        return -1;
    }

    if (is_zero_bytes(p + 16, 16u)) {
        return -1;
    }
    if (is_zero_bytes(p + 32, 16u)) {
        return -1;
    }

    if (!is_zero_bytes(p + 48, 16u)) {
        return -1;
    }

    entry.diagnostic = (diagnostic != 0u);
    memcpy(entry.boot_uuid, p + 16, 16u);
    memcpy(entry.root_uuid, p + 32, 16u);

    *out = entry;
    return 0;
}
