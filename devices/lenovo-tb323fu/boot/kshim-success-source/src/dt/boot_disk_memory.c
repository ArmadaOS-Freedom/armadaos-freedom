#include "boot_disk_memory.h"
#include <string.h>

static uint64_t max_blocker_end(uint64_t start, uint64_t end,
                                const struct dt_range *ranges, size_t n)
{
    uint64_t best = 0;
    size_t i;
    for (i = 0; i < n; i++) {
        uint64_t rb = ranges[i].base;
        uint64_t rs = ranges[i].size;
        uint64_t re;
        if (rs == 0) {
            continue;
        }
        if (rb > UINT64_MAX - rs) {
            continue;
        }
        re = rb + rs;
        if (rb < end && start < re) {
            if (re > best) {
                best = re;
            }
        }
    }
    return best;
}

int kshim_disk_boot_find_memory(const void *fdt, size_t fdt_bytes,
                                uint64_t bytes,
                                const struct dt_range *excluded,
                                size_t excluded_count,
                                uint64_t *base)
{
    struct dt_range ranges[128];
    bool no_map[128];
    size_t count = 0;
    const uint64_t limit = 0x100000000ULL;
    const uint64_t align = 0x200000ULL;
    uint64_t best = 0;
    int found = 0;
    int off;
    size_t i;
    unsigned ac;
    unsigned sc;

    if (base == NULL) {
        return -1;
    }
    if (fdt == NULL || fdt_bytes == 0) {
        return -1;
    }
    if (dt_validate(fdt, fdt_bytes) != 0) {
        return -1;
    }
    if (bytes == 0 || bytes > limit) {
        return -1;
    }
    if (excluded_count && !excluded) {
        return -1;
    }
    if (excluded_count > 128) {
        return -1;
    }
    memset(ranges, 0, sizeof(ranges));
    memset(no_map, 0, sizeof(no_map));
    if (dt_reservations(fdt, ranges, no_map, 128, &count) != 0) {
        return -1;
    }
    if (count > 128) {
        return -1;
    }
    for (i = 0; i < count; i++) {
        if (ranges[i].size != 0 &&
            ranges[i].base > UINT64_MAX - ranges[i].size) {
            return -1;
        }
    }
    for (i = 0; i < excluded_count; i++) {
        if (excluded[i].size != 0 &&
            excluded[i].base > UINT64_MAX - excluded[i].size) {
            return -1;
        }
    }

    ac = (unsigned)fdt_address_cells(fdt, 0);
    sc = (unsigned)fdt_size_cells(fdt, 0);
    if (ac == 0 || ac > 2 || sc == 0 || sc > 2) {
        return -1;
    }

    for (off = fdt_first_subnode(fdt, 0); off >= 0;
         off = fdt_next_subnode(fdt, off)) {
        const void *prop;
        int plen = 0;
        size_t tuple_bytes;
        size_t ntuples;
        size_t t;
        int selected = 0;
        int bad = 0;

        if (!dt_available(fdt, off)) {
            continue;
        }
        prop = fdt_getprop(fdt, off, "device_type", &plen);
        if (prop == NULL || plen != 7) {
            continue;
        }
        if (memcmp(prop, "memory", 7) != 0) {
            continue;
        }
        selected = 1;
        prop = fdt_getprop(fdt, off, "reg", &plen);
        if (prop == NULL || plen <= 0) {
            bad = 1;
        } else {
            tuple_bytes = (size_t)(ac + sc) * sizeof(uint32_t);
            if (tuple_bytes == 0 || (size_t)plen % tuple_bytes != 0) {
                bad = 1;
            } else {
                ntuples = (size_t)plen / tuple_bytes;
                for (t = 0; t < ntuples; t++) {
                    struct dt_range reg;
                    uint64_t cand;
                    uint64_t bank_end;

                    memset(&reg, 0, sizeof(reg));
                    if (dt_raw_reg(fdt, off, (unsigned)t, &reg.base,
                                   &reg.size) != 0) {
                        bad = 1;
                        break;
                    }
                    if (reg.size == 0) {
                        continue;
                    }
                    if (reg.base > UINT64_MAX - reg.size) {
                        bad = 1;
                        break;
                    }
                    if (reg.base >= limit) {
                        continue;
                    }
                    if (reg.size > limit - reg.base) {
                        reg.size = limit - reg.base;
                    }
                    bank_end = reg.base + reg.size;
                    cand = reg.base;
                    if (cand > UINT64_MAX - (align - 1)) {
                        continue;
                    }
                    cand = (cand + align - 1) & ~(align - 1);
                    while (cand <= bank_end && bytes <= bank_end - cand) {
                        uint64_t end = cand + bytes;
                        uint64_t b1, b2, m;

                        b1 = max_blocker_end(cand, end, ranges, count);
                        b2 = max_blocker_end(cand, end, excluded,
                                             excluded_count);
                        m = b1 > b2 ? b1 : b2;
                        if (m == 0) {
                            if (!found || cand < best) {
                                best = cand;
                                found = 1;
                            }
                            break;
                        }
                        if (m <= cand) {
                            break;
                        }
                        if (m > UINT64_MAX - (align - 1)) {
                            break;
                        }
                        cand = (m + align - 1) & ~(align - 1);
                    }
                }
            }
        }
        if (selected && bad) {
            return -1;
        }
    }
    if (!found) {
        return -2;
    }
    *base = best;
    return 0;
}
