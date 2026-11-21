#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include "boot_disk_memory.h"
#include "boot_disk_native.h"
#include "boot_disk_stage.h"
#include "boot_disk_kernel.h"

static void *aligned_blob(size_t sz) {
    void *p = NULL;
    if (posix_memalign(&p, 4096, sz)) return NULL;
    memset(p, 0, sz);
    return p;
}

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
                                size_t scratch_bytes) {
    (void)entry;
    (void)public_key;
    (void)board;
    (void)out;
    (void)out_binding;
    (void)scratch;
    (void)scratch_bytes;
    if (!place) return -124;
    if (place(place_ctx, dtb_scratch, dtb_scratch_bytes, buffers) != 0) return -124;
    if (buffers && buffers->kernel == (void *)(uintptr_t)0x88200000u) return -123;
    return -124;
}

int kshim_disk_prepare_kernel(void *base,
                              size_t file_bytes,
                              size_t capacity,
                              uint64_t *load_addr,
                              uint64_t *image_bytes) {
    (void)base;
    (void)file_bytes;
    (void)capacity;
    (void)load_addr;
    (void)image_bytes;
    return -125;
}

int main(void) {
    const size_t BLOB = 65536;
    void *buf = aligned_blob(BLOB);
    if (!buf) return 1;
    struct fdt_header *fdt = (struct fdt_header *)buf;
    if (fdt_create_empty_tree(fdt, BLOB) != 0) return 2;

    int rc = 0;
    int root = fdt_path_offset(fdt, "/");
    if (root < 0) return 3;
    uint32_t ac = 2;
    if ((rc = fdt_setprop_u32(fdt, root, "#address-cells", ac)) != 0) return 4;
    uint32_t sc = 2;
    if ((rc = fdt_setprop_u32(fdt, root, "#size-cells", sc)) != 0) return 4;
    int mem = fdt_add_subnode(fdt, root, "memory@80000000");
    if (mem < 0) return 5;
    if ((rc = fdt_setprop_string(fdt, mem, "device_type", "memory")) != 0) return 6;
    fdt32_t reg[12];
    reg[0] = cpu_to_fdt32(0x00000000);
    reg[1] = cpu_to_fdt32(0x00000000);
    reg[2] = cpu_to_fdt32(0x00000000);
    reg[3] = cpu_to_fdt32(0x00000000);
    reg[4] = cpu_to_fdt32(0x00000000);
    reg[5] = cpu_to_fdt32(0x80000000);
    reg[6] = cpu_to_fdt32(0x00000000);
    reg[7] = cpu_to_fdt32(0x02000000);
    reg[8] = cpu_to_fdt32(0x00000000);
    reg[9] = cpu_to_fdt32(0x90000000);
    reg[10] = cpu_to_fdt32(0x00000000);
    reg[11] = cpu_to_fdt32(0x04000000);
    if ((rc = fdt_setprop(fdt, mem, "reg", reg, sizeof(reg))) != 0) return 7;

    uint64_t rsv_base = 0x80000000ull, rsv_sz = 0x200000ull;
    if (fdt_add_mem_rsv(fdt, rsv_base, rsv_sz)) return 8;

    struct dt_range excl[1];
    excl[0].base = 0x80200000ull;
    excl[0].size = 0x200000ull;
    size_t fdt_bytes = fdt_totalsize(fdt);
    uint64_t base = 0;

    base = 0;
    if (kshim_disk_boot_find_memory(fdt, fdt_bytes, 6ull*1024*1024, excl, 1, &base) != 0
        || base != 0x80400000ull) return 9;

    base = 0;
    if (kshim_disk_boot_find_memory(fdt, fdt_bytes, 32ull*1024*1024, excl, 1, &base) != 0
        || base != 0x90000000ull) return 10;

    base = 0xDEADBEEFull;
    if (kshim_disk_boot_find_memory(fdt, fdt_bytes, 96ull*1024*1024, excl, 1, &base) == 0) return 11;
    if (base != 0xDEADBEEFull) return 12;

    base = 0;
    if (kshim_disk_boot_find_memory(NULL, fdt_bytes, 6ull*1024*1024, excl, 1, &base) == 0) return 13;
    if (kshim_disk_boot_find_memory(fdt, fdt_bytes, 6ull*1024*1024, NULL, 1, &base) == 0) return 14;
    if (kshim_disk_boot_find_memory(fdt, fdt_bytes, 6ull*1024*1024, excl, 0, &base) != 0) return 15;

    struct dt_range ovf[1];
    ovf[0].base = 0xFFFFFFFFFFFFF000ull;
    ovf[0].size = 0x2000ull;
    base = 0xCAFEBABEull;
    int orc = kshim_disk_boot_find_memory(fdt, fdt_bytes, 6ull*1024*1024, ovf, 1, &base);
    if (orc == 0 || base != 0xCAFEBABEull) return 16;

    {
        /* Edge test: no-own-memreserve runtime FDT + caller exclusion + BLOB memreserve. */
        if (fdt_create_empty_tree(fdt, BLOB) != 0) { free(buf); return 17; }
        int r2 = fdt_path_offset(fdt, "/");
        if (r2 < 0) { free(buf); return 18; }
        uint32_t ac2 = 2, sc2 = 2;
        if (fdt_setprop_u32(fdt, r2, "#address-cells", ac2) != 0) { free(buf); return 19; }
        if (fdt_setprop_u32(fdt, r2, "#size-cells", sc2) != 0) { free(buf); return 19; }
        int mem2 = fdt_add_subnode(fdt, r2, "memory@80000000");
        if (mem2 < 0) { free(buf); return 20; }
        if (fdt_setprop_string(fdt, mem2, "device_type", "memory") != 0) { free(buf); return 21; }
        fdt32_t reg2[4];
        reg2[0] = cpu_to_fdt32(0x00000000);
        reg2[1] = cpu_to_fdt32(0x80000000);
        reg2[2] = cpu_to_fdt32(0x00000000);
        reg2[3] = cpu_to_fdt32(0x40000000);
        if (fdt_setprop(fdt, mem2, "reg", reg2, sizeof(reg2)) != 0) { free(buf); return 22; }

        void *signed_dtb = aligned_blob(BLOB);
        if (!signed_dtb) { free(buf); return 23; }
        struct fdt_header *sfdt = (struct fdt_header *)signed_dtb;
        if (fdt_create_empty_tree(sfdt, BLOB) != 0) { free(signed_dtb); free(buf); return 24; }
        if (fdt_add_mem_rsv(sfdt, 0x80000000ull, 0x08000000ull) != 0) { free(signed_dtb); free(buf); return 25; }

        struct dt_range exclude;
        exclude.base = 0x88000000ull;
        exclude.size = 0x00200000ull;

        struct kshim_disk_entry entry;
        memset(&entry, 0, sizeof(entry));
        uint8_t key[65];
        memset(key, 0, sizeof(key));
        struct kshim_disk_boot_image out, out_copy;
        memset(&out, 0xAA, sizeof(out));
        memcpy(&out_copy, &out, sizeof(out));
        unsigned char scratch[256];
        memset(scratch, 0, sizeof(scratch));

        int sres = kshim_disk_boot_stage(buf, (uint32_t)BLOB, &exclude, 1, &entry, key,
                                          1, &out, scratch, sizeof(scratch),
                                          signed_dtb, BLOB);
        if (sres != -123 || memcmp(&out, &out_copy, sizeof(out)) != 0) { free(signed_dtb); free(buf); return 26; }
        free(signed_dtb);
    }

    free(buf);
    return 0;
}
