/* src/dt/boot_disk_kernel.c */
#include "boot_disk_kernel.h"

#include <string.h>

#define KSHIM_ARM64_MAGIC   0x644d5241u   /* "ARM\x64" LE at offset 56 */
#define KSHIM_TEXT_OFF_MAX  0x200000u     /* 2 MiB */
#define KSHIM_MIN_BYTES     64u
#define KSHIM_BASE_ALIGN    0x200000u

static uint64_t kshim_rd_le64(const uint8_t *p)
{
    uint64_t v = 0;
    for (int i = 7; i >= 0; --i)
        v = (v << 8) | p[i];
    return v;
}

static uint32_t kshim_rd_le32(const uint8_t *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

int kshim_disk_prepare_kernel(void *base,
                              size_t file_bytes,
                              size_t capacity,
                              uint64_t *load_addr,
                              uint64_t *image_bytes)
{
    if (base == NULL || load_addr == NULL || image_bytes == NULL)
        return -1;
    if (file_bytes < KSHIM_MIN_BYTES || file_bytes > capacity)
        return -1;

    uintptr_t b = (uintptr_t)base;
    if ((b & (KSHIM_BASE_ALIGN - 1u)) != 0)
        return -1;
    if (b > UINTPTR_MAX - (uintptr_t)capacity)
        return -1;
    if (b > UINTPTR_MAX - (uintptr_t)file_bytes)
        return -1;

    const uint8_t *hdr = (const uint8_t *)base;
    uint64_t text_offset = kshim_rd_le64(hdr + 8);
    uint64_t image_size  = kshim_rd_le64(hdr + 16);
    uint32_t flags       = kshim_rd_le32(hdr + 24);
    uint32_t magic       = kshim_rd_le32(hdr + 56);

    if (magic != KSHIM_ARM64_MAGIC)
        return -1;
    if ((flags & 1u) != 0u)             /* little endian required */
        return -1;
    if (image_size == 0)
        return -1;
    if (text_offset >= KSHIM_TEXT_OFF_MAX)
        return -1;
    if (text_offset > (uint64_t)capacity)
        return -1;

    uint64_t effective_bytes = image_size;
    if ((uint64_t)file_bytes > effective_bytes)
        effective_bytes = (uint64_t)file_bytes;

    uint64_t cap64 = (uint64_t)capacity;
    if (effective_bytes > cap64 - text_offset)      /* cannot overflow: text_offset <= cap64 */
        return -1;

    if (text_offset > (uint64_t)UINTPTR_MAX - (uint64_t)b)
        return -1;
    uintptr_t target = b + (uintptr_t)text_offset;
    if (target > UINTPTR_MAX - (uintptr_t)file_bytes)
        return -1;
    if (target > UINTPTR_MAX - (uintptr_t)effective_bytes)
        return -1;

    memmove((void *)target, base, file_bytes);

    uint64_t tail_start = (uint64_t)file_bytes;
    if (effective_bytes > tail_start) {
        uint8_t *tail = (uint8_t *)(target + (uintptr_t)tail_start);
        size_t   n    = (size_t)(effective_bytes - tail_start);
        memset(tail, 0, n);
    }

    *load_addr   = (uint64_t)target;
    *image_bytes = effective_bytes;
    return 0;
}
