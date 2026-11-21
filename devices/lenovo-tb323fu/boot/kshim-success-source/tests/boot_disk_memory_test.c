#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include "boot_disk_memory.h"

static void *aligned_blob(size_t sz) {
    void *p = NULL;
    if (posix_memalign(&p, 4096, sz)) return NULL;
    memset(p, 0, sz);
    return p;
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

    free(buf);
    return rc;
}
