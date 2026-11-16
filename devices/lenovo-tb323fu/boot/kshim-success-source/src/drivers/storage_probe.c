/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_probe.h>

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

/* Original read-only collector. Register semantics were checked against
 * UFSHCI and the MIT SMMUv2 interface in Project-Aloha/CrDK at
 * b6c3335136b7f6b28271917de11e8f11982975fc. No kernel driver code is imported.
 * These windows match the TB322FC/SM8750, TB323FU/Kaanapali and TB710FU/SM8650
 * profiles (SM8650 DT: ufshc@1d84000 reg 0x3000, apps SMMU @15000000, UFS SID
 * 0x60), not a generic Qualcomm fallback. No driver initialization or SmmuProbe call is made. */
#define UFS_BASE UINT64_C(0x01d84000)
#define UFS_SIZE UINT64_C(0x3000)
#define SMMU_BASE UINT64_C(0x15000000)
#define SMMU_SIZE UINT64_C(0x100000)
#define UFS_SID UINT32_C(0x60)

struct probe_reader {
    storage_probe_read32_fn read32;
    void *ctx;
    struct storage_probe_snapshot *out;
};

static enum storage_probe_status finish(struct probe_reader *p,
                                         enum storage_probe_status status,
                                         uint64_t address, int error)
{
    p->out->status = status;
    p->out->failed_address = address;
    p->out->read_error = error;
    return status;
}

static bool capacity_available(struct probe_reader *p, uint64_t address)
{
    if (p->out->entry_count < STORAGE_PROBE_MAX_ENTRIES)
        return true;
    (void)finish(p, STORAGE_PROBE_FULL, address, 0);
    return false;
}

static bool read_value(struct probe_reader *p, uint64_t address, uint32_t *value)
{
    int error;
    bool in_ufs = address >= UFS_BASE && address < UFS_BASE + UFS_SIZE;
    bool in_smmu = address >= SMMU_BASE && address < SMMU_BASE + SMMU_SIZE;

    if ((address & UINT64_C(3)) != 0U || (!in_ufs && !in_smmu)) {
        (void)finish(p, STORAGE_PROBE_BOUNDS, address, 0);
        return false;
    }
    ++p->out->read_attempts;
    error = p->read32(p->ctx, address, value);
    if (error != 0) {
        (void)finish(p, STORAGE_PROBE_READ_FAILED, address, error);
        return false;
    }
    return true;
}

static void append_value(struct probe_reader *p, uint64_t address, uint32_t value)
{
    p->out->entries[p->out->entry_count++] =
        (struct storage_probe_entry){address, value};
}

static bool read_record(struct probe_reader *p, uint64_t address, uint32_t *result)
{
    uint32_t value;

    if (!capacity_available(p, address) || !read_value(p, address, &value))
        return false;
    append_value(p, address, value);
    if (result != NULL)
        *result = value;
    return true;
}

static bool capture_ufs(struct probe_reader *p)
{
    /* Skip write-only list-clear registers and all test/debug selectors.
     * Busy queues, disabled HCE and MCQ mode are exported without alteration. */
    static const uint32_t offsets[] = {
        0x00U, 0x04U, 0x08U, 0x0cU, 0x18U, 0x20U, 0x24U,
        0x30U, 0x34U, 0x38U, 0x3cU, 0x40U, 0x44U, 0x48U, 0x4cU,
        0x50U, 0x54U, 0x58U, 0x60U, 0x70U, 0x74U, 0x78U, 0x80U,
        0x90U, 0x94U, 0x98U, 0x9cU, 0xd8U, 0xdcU, 0xe0U,
        0xe4U, 0xfcU, 0x300U, 0x284cU,
    };

    for (size_t i = 0U; i < sizeof(offsets) / sizeof(offsets[0]); ++i)
        if (!read_record(p, UFS_BASE + offsets[i], NULL))
            return false;
    p->out->valid |= STORAGE_PROBE_VALID_UFS;
    return true;
}

static bool capture_context(struct probe_reader *p, uint32_t bank,
                            uint64_t page_size, uint64_t context_base)
{
    /* SCTLR, TCR2, both halves of TTBR0/TTBR1, TCR, MAIR0/MAIR1,
     * FSR, both halves of FAR, FSYNR0. A 64-bit pair remains two raw entries:
     * the snapshot does not claim atomicity or a decoded translation. */
    static const uint32_t offsets[] = {
        0x00U, 0x10U, 0x20U, 0x24U, 0x28U, 0x2cU, 0x30U,
        0x38U, 0x3cU, 0x58U, 0x60U, 0x64U, 0x68U,
    };
    uint64_t group1 = SMMU_BASE + page_size;
    uint64_t bank_base = context_base + (uint64_t)bank * page_size;

    if (!read_record(p, group1 + (uint64_t)bank * 4U, NULL) ||
        !read_record(p, group1 + 0x800U + (uint64_t)bank * 4U, NULL))
        return false;
    for (size_t i = 0U; i < sizeof(offsets) / sizeof(offsets[0]); ++i)
        if (!read_record(p, bank_base + offsets[i], NULL))
            return false;
    ++p->out->contexts_captured;
    return true;
}

static enum storage_probe_status capture_smmu(struct probe_reader *p)
{
    uint32_t control, id0, id1, id2;
    uint32_t stream_count, bank_count, stage2_banks;
    uint64_t page_size, register_pages, context_base;
    bool captured[256] = {false};

    /* Read and retain these before interpreting any capability. In
     * particular, CLIENTPD never causes us to enable or reset the SMMU. */
    if (!read_record(p, SMMU_BASE, &control) ||
        !read_record(p, SMMU_BASE + 0x20U, &id0) ||
        !read_record(p, SMMU_BASE + 0x24U, &id1) ||
        !read_record(p, SMMU_BASE + 0x28U, &id2))
        return p->out->status;
    p->out->valid |= STORAGE_PROBE_VALID_SMMU_IDS;

    /* SMS, stage-1, AArch64 4K translation format; no EXIDENABLE decoding. */
    if ((id0 & UINT32_C(0x48000000)) != UINT32_C(0x48000000) ||
        (id2 & UINT32_C(0x1000)) == 0U || (control & 8U) != 0U)
        return finish(p, STORAGE_PROBE_SMMU_UNSUPPORTED, 0U, 0);

    stream_count = id0 & 0xffU;
    bank_count = id1 & 0xffU;
    stage2_banks = (id1 >> 16U) & 0xffU;
    page_size = (id1 & UINT32_C(0x80000000)) != 0U ?
                UINT64_C(0x10000) : UINT64_C(0x1000);
    register_pages = UINT64_C(1) << (((id1 >> 28U) & 7U) + 1U);

    /* arm-smmu.h (torvalds/linux 330517c63aa9) defines ID0.NUMSMRG as
     * GENMASK(7,0) with SMR at 0x800 + n*4 and S2CR at 0xc00 + n*4.
     * MAX_CBS=128 bounds context banks only, never the stream count.
     * Both register groups and the entire context-bank half must fit the
     * board's mapped window. Refuse excessive group counts rather than
     * silently representing a truncated scan as a complete snapshot. */
    if (stream_count == 0U ||
        bank_count == 0U || bank_count > register_pages ||
        stage2_banks > bank_count ||
        register_pages * page_size * 2U > SMMU_SIZE)
        return finish(p, STORAGE_PROBE_BOUNDS, 0U, 0);
    context_base = SMMU_BASE + register_pages * page_size;
    p->out->stream_count = stream_count;
    p->out->valid |= STORAGE_PROBE_VALID_SMMU_LAYOUT;

    for (uint32_t i = 0U; i < stream_count; ++i) {
        uint64_t smr_address = SMMU_BASE + 0x800U + (uint64_t)i * 4U;
        uint32_t smr, s2cr, mask, type, bank;

        if (!capacity_available(p, smr_address) ||
            !read_value(p, smr_address, &smr))
            return p->out->status;
        ++p->out->streams_scanned;
        if ((smr & UINT32_C(0x80000000)) == 0U)
            continue;
        mask = (smr >> 16U) & 0x7fffU;
        if (((UFS_SID ^ (smr & 0x7fffU)) & (~mask & 0x7fffU)) != 0U)
            continue;
        append_value(p, smr_address, smr);
        ++p->out->matching_streams;
        if (!read_record(p, SMMU_BASE + 0xc00U + (uint64_t)i * 4U, &s2cr))
            return p->out->status;
        type = (s2cr >> 16U) & 3U;
        if (type == 1U || type == 2U) /* Bypass or fault, CB index irrelevant. */
            continue;
        if (type != 0U)
            return finish(p, STORAGE_PROBE_SMMU_UNSUPPORTED, 0U, 0);
        bank = s2cr & 0xffU;
        if (bank >= bank_count)
            return finish(p, STORAGE_PROBE_BOUNDS, 0U, 0);
        if (!captured[bank]) {
            if (!capture_context(p, bank, page_size, context_base))
                return p->out->status;
            captured[bank] = true;
        }
    }
    return finish(p, STORAGE_PROBE_OK, 0U, 0);
}

enum storage_probe_status storage_probe_collect(
    enum device_board actual_board, storage_probe_read32_fn read32,
    void *ctx, struct storage_probe_snapshot *out)
{
    struct probe_reader reader = {read32, ctx, out};

    if (out == NULL)
        return STORAGE_PROBE_INVALID_ARGUMENT;
    memset(out, 0, sizeof(*out));
    out->version = STORAGE_PROBE_VERSION;
    out->board = actual_board;
    out->status = STORAGE_PROBE_NOT_RUN;
    if (actual_board != DEVICE_BOARD_TB323FU && actual_board != DEVICE_BOARD_TB322FC &&
        actual_board != DEVICE_BOARD_TB710FU)
        return finish(&reader, STORAGE_PROBE_BOARD_UNSUPPORTED, 0U, 0);
    if (read32 == NULL)
        return finish(&reader, STORAGE_PROBE_INVALID_ARGUMENT, 0U, 0);
    if (!capture_ufs(&reader))
        return out->status;
    return capture_smmu(&reader);
}
