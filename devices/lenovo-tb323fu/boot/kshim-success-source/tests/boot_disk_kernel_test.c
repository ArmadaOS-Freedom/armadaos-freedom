/* test_boot_disk_kernel.c  (synthetic; no device ops) */
#include "boot_disk_kernel.h"
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define ALIGN_2M 0x200000u
#define ALIGN_4M 0x400000u

static void put_le64(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; ++i) p[i] = (uint8_t)(v >> (8 * i));
}

int main(void) {
    void *base = NULL;
    if (posix_memalign(&base, ALIGN_2M, ALIGN_4M) != 0) return 1;
    size_t capacity = ALIGN_4M;
    memset(base, 0, capacity);

    /* Build a minimal synthetic Arm64 Image at base. */
    uint8_t *h = (uint8_t *)base;
    memset(h, 0, 64);
    put_le64(h + 8, 0x80000u);   /* text_offset */
    put_le64(h + 16, 512u);      /* image_size */
    h[24] = 1u;                  /* flags bit0=1 (BE) -> must reject */
    h[56] = 0x41; h[57] = 0x52; h[58] = 0x4d; h[59] = 0x64; /* "ARM\x64" */
    h[64] = 0xAB;                /* payload marker */

    uint64_t out_load = 0xDEADBEEFDEADBEEFull;
    uint64_t out_size = 0xCAFEBABECAFEBABEull;

    /* Reject: BE flag bit0=1. Sentinels untouched. */
    assert(kshim_disk_prepare_kernel(base, 128, capacity, &out_load, &out_size) != 0);
    assert(out_load == 0xDEADBEEFDEADBEEFull);
    assert(out_size == 0xCAFEBABECAFEBABEull);

    /* Reject: file_bytes too small (< 64). Sentinels untouched. */
    assert(kshim_disk_prepare_kernel(base, 32, capacity, &out_load, &out_size) != 0);
    assert(out_load == 0xDEADBEEFDEADBEEFull);
    assert(out_size == 0xCAFEBABECAFEBABEull);

    /* Reject: capacity too small to hold text_offset + image. */
    assert(kshim_disk_prepare_kernel(base, 128, 0x40000u, &out_load, &out_size) != 0);
    assert(out_load == 0xDEADBEEFDEADBEEFull);
    assert(out_size == 0xCAFEBABECAFEBABEull);

    /* Accept: LE flag. image_size=512 > file_bytes=128, text_offset=0x80000. */
    h[24] = 0u;
    out_load = 0xDEADBEEFDEADBEEFull;
    out_size = 0xCAFEBABECAFEBABEull;
    assert(kshim_disk_prepare_kernel(base, 128, capacity, &out_load, &out_size) == 0);
    assert(out_load == (uint64_t)(uintptr_t)base + 0x80000u);
    assert(out_size == 512u);

    /* Exact payload copied. */
    uintptr_t target = (uintptr_t)out_load;
    const uint8_t *t = (const uint8_t *)target;
    assert(t[56] == 0x41 && t[57] == 0x52 && t[58] == 0x4d && t[59] == 0x64);
    assert(t[64] == 0xAB);               /* payload marker preserved */

    /* BSS zeroed between file_bytes and image_size. */
    for (size_t i = 128; i < 512; ++i) assert(t[i] == 0);

    free(base);
    return 0;
}
