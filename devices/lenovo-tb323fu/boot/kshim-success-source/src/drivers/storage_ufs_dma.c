/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_ufs_dma.h>

#include <stddef.h>

/* Architectural SMMUv2 field positions, used as facts only. This decoder is
 * original, read-only and narrow: it interprets one inherited configuration
 * captured by storage_probe.c and never walks tables, writes registers or
 * allocates memory. */
#define REG_CR0 0x00U
#define REG_ID0 0x20U
#define REG_ID1 0x24U
#define REG_ID2 0x28U
#define REG_SMR 0x800U
#define REG_SMR_END 0xc00U
#define REG_S2CR 0xc00U
#define REG_CBAR 0x000U
#define REG_CBA2R 0x800U

#define CR0_CLIENTPD (UINT32_C(1) << 0)
#define CR0_EXIDENABLE (UINT32_C(1) << 3)

#define ID0_NUMSMRG UINT32_C(0xff)
#define ID0_SMS (UINT32_C(1) << 27)
#define ID0_S1TS (UINT32_C(1) << 30)

#define ID1_NUMCB UINT32_C(0xff)
#define ID1_NUMS2CB (UINT32_C(0xff) << 16)
#define ID1_NUMPAGENDXB (UINT32_C(7) << 28)
#define ID1_PAGESIZE (UINT32_C(1) << 31)

#define ID2_IAS UINT32_C(0x0f)
#define ID2_OAS (UINT32_C(0x0f) << 4)
/* Supported-translation-granule capabilities are independent bits: 4 KiB must
 * be supported, but 16 KiB and 64 KiB may also be advertised without changing
 * the granule this configuration actually uses. */
#define ID2_PTFS_4K (UINT32_C(1) << 12)

#define SMR_VALID (UINT32_C(1) << 31)
#define SMR_MASK (UINT32_C(0x7fff) << 16)
#define SMR_ID UINT32_C(0xffff)

#define S2CR_TYPE (UINT32_C(3) << 16)
#define S2CR_CBNDX UINT32_C(0xff)

#define CBAR_TYPE (UINT32_C(3) << 16)
#define CBA2R_VA64 (UINT32_C(1) << 0)
#define SCTLR_M (UINT32_C(1) << 0)

#define UFS_STREAM_ID UINT32_C(0x60)
#define PAGE_4K UINT64_C(0x1000)
#define PAGE_64K UINT64_C(0x10000)

/* Every supported board profile (SM8750, SM8850, SM8650) places the SMMU here; the collector and the
 * live DTS agree on this address. There is no register-pattern discovery. */
#define SMMU_PROFILE_BASE UINT64_C(0x15000000)
#define SMMU_WINDOW_BYTES UINT64_C(0x100000)

/* Read a record that must occur exactly once. A duplicated address is treated
 * as missing evidence rather than silently selecting a copy. */
static bool one_value(const struct storage_probe_snapshot *snapshot,
                      uint64_t address, uint32_t *value)
{
    uint32_t result = 0U;
    uint32_t found = 0U;

    for (uint32_t i = 0U; i < snapshot->entry_count; ++i) {
        if (snapshot->entries[i].address != address)
            continue;
        if (found != 0U)
            return false;
        result = snapshot->entries[i].value;
        ++found;
    }
    if (found == 0U)
        return false;
    *value = result;
    return true;
}

/* Only the six encodings the architecture defines are accepted. */
static bool width_from_code(uint32_t code, uint32_t *bits)
{
    switch (code) {
    case 0U: *bits = 32U; return true;
    case 1U: *bits = 36U; return true;
    case 2U: *bits = 40U; return true;
    case 3U: *bits = 42U; return true;
    case 4U: *bits = 44U; return true;
    case 5U: *bits = 48U; return true;
    default: return false;
    }
}

/* [base, base + bytes) must be representable at the given width. */
static bool span_fits(uint64_t base, uint64_t bytes, uint32_t bits)
{
    uint64_t limit = UINT64_C(1) << bits;

    return base < limit && bytes <= limit - base;
}

static bool matching_stream(const struct storage_probe_snapshot *snapshot,
                            uint64_t base, uint32_t stream_count,
                            uint32_t *smr_value, uint32_t *s2cr_value)
{
    uint64_t first = base + REG_SMR;
    uint64_t limit = base + REG_SMR_END;
    uint64_t address = 0U;
    uint32_t smr = 0U;
    uint32_t found = 0U;
    uint32_t index;

    for (uint32_t i = 0U; i < snapshot->entry_count; ++i) {
        uint64_t entry_address = snapshot->entries[i].address;
        uint32_t value = snapshot->entries[i].value;

        if (entry_address < first || entry_address >= limit)
            continue;
        if ((value & SMR_VALID) == 0U)
            continue;
        if (((entry_address - first) & (UINT64_C(4) - 1U)) != 0U)
            return false;
        if (found != 0U)
            return false;
        address = entry_address;
        smr = value;
        found = 1U;
    }
    if (found == 0U)
        return false;
    index = (uint32_t)((address - first) / UINT64_C(4));
    if (index >= stream_count)
        return false;
    if (!one_value(snapshot, base + REG_S2CR + (uint64_t)index * 4U,
                   s2cr_value))
        return false;
    *smr_value = smr;
    return true;
}

/* Stage-1 translate with stage-2 bypass is the only translated form accepted.
 * The bank index must be a real stage-1 bank, not a stage-2-only one, and the
 * associated bank must still have its MMU disabled. */
static bool bank_ok(const struct storage_probe_snapshot *snapshot,
                    uint64_t group1, uint64_t context_base,
                    uint64_t page_size, uint32_t bank,
                    uint32_t bank_count, uint32_t stage2_banks)
{
    uint32_t cbar, cba2r, sctlr;

    if (bank >= bank_count || bank < stage2_banks)
        return false;
    if (!one_value(snapshot, group1 + REG_CBAR + (uint64_t)bank * 4U, &cbar) ||
        !one_value(snapshot, group1 + REG_CBA2R + (uint64_t)bank * 4U, &cba2r) ||
        !one_value(snapshot, context_base + (uint64_t)bank * page_size,
                   &sctlr))
        return false;
    if ((cbar & CBAR_TYPE) != (UINT32_C(1) << 16))
        return false;
    if ((cba2r & CBA2R_VA64) == 0U)
        return false;
    if ((sctlr & SCTLR_M) != 0U)
        return false;
    return true;
}

bool kshim_ufs_dma_is_identity(const struct storage_probe_snapshot *snapshot,
                               uint64_t physical_base, uint64_t bytes)
{
    uint64_t page_size, register_pages, group1, context_base;
    uint32_t control, id0, id1, id2, smr, s2cr;
    uint32_t stream_count, bank_count, stage2_banks;
    uint32_t ias_bits, oas_bits, type;
    const uint64_t base = SMMU_PROFILE_BASE;

    if (snapshot == NULL)
        return false;
    if (snapshot->version != STORAGE_PROBE_VERSION ||
        snapshot->status != STORAGE_PROBE_OK)
        return false;
    if ((snapshot->valid & (STORAGE_PROBE_VALID_UFS |
                            STORAGE_PROBE_VALID_SMMU_IDS |
                            STORAGE_PROBE_VALID_SMMU_LAYOUT)) !=
        (STORAGE_PROBE_VALID_UFS | STORAGE_PROBE_VALID_SMMU_IDS |
         STORAGE_PROBE_VALID_SMMU_LAYOUT))
        return false;
    if (snapshot->board != DEVICE_BOARD_TB323FU &&
        snapshot->board != DEVICE_BOARD_TB322FC &&
        snapshot->board != DEVICE_BOARD_TB710FU)
        return false;
    if (snapshot->entry_count > STORAGE_PROBE_MAX_ENTRIES)
        return false;
    if (snapshot->stream_count == 0U || snapshot->stream_count > 255U ||
        snapshot->streams_scanned != snapshot->stream_count ||
        snapshot->matching_streams != 1U)
        return false;
    if (physical_base == 0U || bytes == 0U ||
        physical_base > UINT64_MAX - bytes)
        return false;

    /* The fixed profile base is the only window this decoder will read. */
    if (!one_value(snapshot, base + REG_CR0, &control) ||
        !one_value(snapshot, base + REG_ID0, &id0) ||
        !one_value(snapshot, base + REG_ID1, &id1) ||
        !one_value(snapshot, base + REG_ID2, &id2))
        return false;
    if ((control & (CR0_CLIENTPD | CR0_EXIDENABLE)) != 0U)
        return false;
    if ((id0 & (ID0_SMS | ID0_S1TS)) != (ID0_SMS | ID0_S1TS))
        return false;
    if ((id2 & ID2_PTFS_4K) == 0U)
        return false;

    stream_count = id0 & ID0_NUMSMRG;
    if (stream_count != snapshot->stream_count)
        return false;

    if (!width_from_code(id2 & ID2_IAS, &ias_bits) ||
        !width_from_code((id2 & ID2_OAS) >> 4U, &oas_bits))
        return false;
    if (!span_fits(physical_base, bytes, ias_bits) ||
        !span_fits(physical_base, bytes, oas_bits))
        return false;

    bank_count = id1 & ID1_NUMCB;
    stage2_banks = (id1 & ID1_NUMS2CB) >> 16U;
    /* ID1.PAGESIZE is the MMIO stride between register groups, not the
     * translation granule. */
    page_size = (id1 & ID1_PAGESIZE) != 0U ? PAGE_64K : PAGE_4K;
    register_pages = UINT64_C(1) <<
        (((id1 & ID1_NUMPAGENDXB) >> 28U) + 1U);

    /* Mirror the collector's bounded-geometry rules exactly. */
    if (bank_count == 0U || bank_count > register_pages ||
        stage2_banks > bank_count ||
        register_pages * page_size * 2U > SMMU_WINDOW_BYTES)
        return false;
    group1 = base + page_size;
    context_base = base + register_pages * page_size;

    if (!matching_stream(snapshot, base, stream_count, &smr, &s2cr))
        return false;
    if ((smr & SMR_ID) != UFS_STREAM_ID || (smr & SMR_MASK) != 0U)
        return false;

    type = (s2cr & S2CR_TYPE) >> 16U;
    if (type == 1U) /* Bypass: no context bank may have been captured. */
        return snapshot->contexts_captured == 0U;
    if (type != 0U) /* Fault, reserved or a wider mapping is not proven. */
        return false;
    if (snapshot->contexts_captured != 1U)
        return false;
    return bank_ok(snapshot, group1, context_base, page_size,
                   s2cr & S2CR_CBNDX, bank_count, stage2_banks);
}
