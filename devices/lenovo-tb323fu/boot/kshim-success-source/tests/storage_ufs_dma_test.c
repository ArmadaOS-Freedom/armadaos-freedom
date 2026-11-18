/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_probe.h>
#include <storage_ufs_dma.h>

#include <assert.h>
#include <stdint.h>
#include <string.h>

/* Captured TB322FC Gen4 firmware state: CR0, ID0..ID2, one SMR/S2CR pair, and
 * the stage-1/stage-2-bypass context bank that the collector captured. */
#define SMMU_BASE UINT64_C(0x15000000)
#define ALL_VALID (STORAGE_PROBE_VALID_UFS | STORAGE_PROBE_VALID_SMMU_IDS | \
                   STORAGE_PROBE_VALID_SMMU_LAYOUT)

static void add_entry(struct storage_probe_snapshot *snapshot,
                      uint64_t address, uint32_t value)
{
    assert(snapshot->entry_count < STORAGE_PROBE_MAX_ENTRIES);
    snapshot->entries[snapshot->entry_count].address = address;
    snapshot->entries[snapshot->entry_count].value = value;
    ++snapshot->entry_count;
}

static void set_value(struct storage_probe_snapshot *snapshot,
                      uint64_t address, uint32_t value)
{
    for (uint32_t i = 0U; i < snapshot->entry_count; ++i) {
        if (snapshot->entries[i].address == address) {
            snapshot->entries[i].value = value;
            return;
        }
    }
    assert(0);
}

static void drop_entry(struct storage_probe_snapshot *snapshot,
                       uint64_t address)
{
    uint32_t i;

    for (i = 0U; i < snapshot->entry_count; ++i) {
        if (snapshot->entries[i].address == address)
            break;
    }
    assert(i < snapshot->entry_count);
    for (; i + 1U < snapshot->entry_count; ++i)
        snapshot->entries[i] = snapshot->entries[i + 1U];
    --snapshot->entry_count;
}

static void duplicate_entry(struct storage_probe_snapshot *snapshot,
                            uint64_t address)
{
    for (uint32_t i = 0U; i < snapshot->entry_count; ++i) {
        if (snapshot->entries[i].address == address) {
            add_entry(snapshot, address, snapshot->entries[i].value);
            return;
        }
    }
    assert(0);
}

static void captured(struct storage_probe_snapshot *snapshot)
{
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->version = STORAGE_PROBE_VERSION;
    snapshot->board = DEVICE_BOARD_TB322FC;
    snapshot->status = STORAGE_PROBE_OK;
    snapshot->valid = ALL_VALID;
    snapshot->read_attempts = 186U;
    snapshot->stream_count = 132U;
    snapshot->streams_scanned = 132U;
    snapshot->matching_streams = 1U;
    snapshot->contexts_captured = 1U;

    add_entry(snapshot, SMMU_BASE + 0x00U, UINT32_C(0x002f0406));
    add_entry(snapshot, SMMU_BASE + 0x20U, UINT32_C(0x4c017e84));
    add_entry(snapshot, SMMU_BASE + 0x24U, UINT32_C(0x60000053));
    add_entry(snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00005111));
    add_entry(snapshot, SMMU_BASE + 0x800U, UINT32_C(0x80000060));
    add_entry(snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00000000));
    add_entry(snapshot, SMMU_BASE + 0x1000U, UINT32_C(0x0001f000));
    add_entry(snapshot, SMMU_BASE + 0x1800U, UINT32_C(0x00000001));
    add_entry(snapshot, SMMU_BASE + 0x80000U, UINT32_C(0x0a9f00e0));
}

/* A 64 KiB MMIO stride profile: ID1=0x80000002 means two register pages and
 * two context banks, so group 1 starts at +0x10000 and the bank pages at
 * +0x20000. This mirrors the valid case in storage_probe_test.c. */
static void captured_64k_stride(struct storage_probe_snapshot *snapshot)
{
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->version = STORAGE_PROBE_VERSION;
    snapshot->board = DEVICE_BOARD_TB322FC;
    snapshot->status = STORAGE_PROBE_OK;
    snapshot->valid = ALL_VALID;
    snapshot->read_attempts = 186U;
    snapshot->stream_count = 132U;
    snapshot->streams_scanned = 132U;
    snapshot->matching_streams = 1U;
    snapshot->contexts_captured = 1U;

    add_entry(snapshot, SMMU_BASE + 0x00U, UINT32_C(0x002f0406));
    add_entry(snapshot, SMMU_BASE + 0x20U, UINT32_C(0x4c017e84));
    add_entry(snapshot, SMMU_BASE + 0x24U, UINT32_C(0x80000002));
    add_entry(snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00005111));
    add_entry(snapshot, SMMU_BASE + 0x800U, UINT32_C(0x80000060));
    add_entry(snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00000001));
    add_entry(snapshot, SMMU_BASE + 0x10004U, UINT32_C(0x0001f000));
    add_entry(snapshot, SMMU_BASE + 0x10804U, UINT32_C(0x00000001));
    add_entry(snapshot, SMMU_BASE + 0x30000U, UINT32_C(0x0a9f00e0));
}

/* Every call must leave the evidence snapshot byte-for-byte untouched. */
static bool accepts(const struct storage_probe_snapshot *snapshot,
                    uint64_t physical_base, uint64_t bytes)
{
    struct storage_probe_snapshot before = *snapshot;
    bool result = kshim_ufs_dma_is_identity(snapshot, physical_base, bytes);

    assert(memcmp(&before, snapshot, sizeof(before)) == 0);
    return result;
}

static void test_captured_accepts(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));
    assert(accepts(&snapshot, UINT64_C(0x80000000), UINT64_C(0x4000)));
    assert(accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x8000)));
    /* Highest page representable in the captured 36-bit IAS/OAS. */
    assert(accepts(&snapshot, UINT64_C(0xffffff000), UINT64_C(0x1000)));

    /* The same evidence is valid when reported for the sibling board. */
    snapshot.board = DEVICE_BOARD_TB323FU;
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));
    /* ...and for TB710FU, whose SM8650 profile shares the windows and SID. */
    snapshot.board = DEVICE_BOARD_TB710FU;
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));
}

static void test_gen5_inherited_context(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    snapshot.board = DEVICE_BOARD_TB323FU;
    snapshot.read_attempts = 176U;
    snapshot.stream_count = snapshot.streams_scanned = 122U;
    set_value(&snapshot, SMMU_BASE, UINT32_C(0x00270406));
    set_value(&snapshot, SMMU_BASE + 0x20U, UINT32_C(0x4c017e7a));
    set_value(&snapshot, SMMU_BASE + 0x24U, UINT32_C(0x60000051));
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00005211));
    assert(accepts(&snapshot, UINT64_C(0x80000000), UINT64_C(0x8000)));
    assert(accepts(&snapshot, UINT64_C(0xffffff000), UINT64_C(0x1000)));
    assert(!accepts(&snapshot, UINT64_C(0x1000000000), UINT64_C(0x1000)));

    set_value(&snapshot, SMMU_BASE + 0x80000U, UINT32_C(0x0a9f00e1));
    assert(!accepts(&snapshot, UINT64_C(0x80000000), UINT64_C(0x8000)));
}

/* 16 KiB and 64 KiB granule capabilities are independent supported bits and
 * must not cause a rejection on their own. */
static void test_granule_capabilities_accepted(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00007111));
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00003111));
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));
}

/* A supported 64 KiB MMIO stride must derive the same geometry the collector
 * does, without an extra factor of two. */
static void test_64k_stride_accepts(void)
{
    struct storage_probe_snapshot snapshot;

    captured_64k_stride(&snapshot);
    assert(accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x2000)));
    assert(accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));

    /* The bank-1 SGCTLR record is the one at the derived +0x30000. */
    captured_64k_stride(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0x30000U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_span_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    assert(!accepts(&snapshot, UINT64_C(0x1000), 0U));
    assert(!accepts(&snapshot, 0U, UINT64_C(0x1000)));
    assert(!accepts(&snapshot, UINT64_MAX - UINT64_C(0xfff), UINT64_C(0x2000)));
    /* 2^36 and 2^44 are outside the captured 36-bit address sizes. */
    assert(!accepts(&snapshot, UINT64_C(0x1000000000), UINT64_C(0x1000)));
    assert(!accepts(&snapshot, UINT64_C(0x100000000000), UINT64_C(0x1000)));
}

/* Only the SMMU window this decoder knows about counts; copied evidence at any
 * relocated address is not accepted. */
static void test_relocated_window_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    for (uint32_t i = 0U; i < snapshot.entry_count; ++i)
        snapshot.entries[i].address += UINT64_C(0x100000);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    for (uint32_t i = 0U; i < snapshot.entry_count; ++i)
        snapshot.entries[i].address = UINT64_C(0x16000000) +
            (snapshot.entries[i].address - SMMU_BASE);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_control_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    /* CLIENTPD: the SMMU would be bypassing translation entirely. */
    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x00U, UINT32_C(0x002f0407));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    /* EXIDENABLE: stage-1 uses stream IDs beyond SMR.ID[15:0]. */
    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x00U, UINT32_C(0x002f040e));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_id_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    /* No 4 KiB translation support. */
    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00004011));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    /* SMS or stage-1 translation missing. */
    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x20U, UINT32_C(0x4c017e84) &
              ~UINT32_C(0x08000000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    /* Stream count in ID0 disagrees with the captured count. */
    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x20U, UINT32_C(0x4c017e83));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_reserved_width_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00005116));
    assert(!accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x28U, UINT32_C(0x00005161));
    assert(!accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));
}

static void test_incomplete_snapshot_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    snapshot.status = STORAGE_PROBE_READ_FAILED;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.valid &= ~STORAGE_PROBE_VALID_SMMU_LAYOUT;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.streams_scanned = 131U;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.stream_count = 0U;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.matching_streams = 2U;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.version = STORAGE_PROBE_VERSION + 1U;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.board = (enum device_board)99;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    snapshot.entry_count = STORAGE_PROBE_MAX_ENTRIES + 1U;
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_missing_record_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0x00U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    duplicate_entry(&snapshot, SMMU_BASE + 0x20U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0xc00U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0x1000U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0x1800U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    drop_entry(&snapshot, SMMU_BASE + 0x80000U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_stream_mapping_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x800U, UINT32_C(0x80010060));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x800U, UINT32_C(0x80000061));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    duplicate_entry(&snapshot, SMMU_BASE + 0x800U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00020000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00030000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_context_bank_rejected(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x80000U, UINT32_C(0x0a9f00e1));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x1000U, UINT32_C(0x00030000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x1000U, UINT32_C(0x00000000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x1800U, UINT32_C(0x00000000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00000053));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x24U, UINT32_C(0x60010053));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_bypass(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00010000));
    snapshot.contexts_captured = 0U;
    assert(accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00010000));
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00010000));
    snapshot.contexts_captured = 0U;
    drop_entry(&snapshot, SMMU_BASE + 0x800U);
    assert(!accepts(&snapshot, UINT64_C(0x1000), UINT64_C(0x1000)));
}

static void test_derived_bank_geometry(void)
{
    struct storage_probe_snapshot snapshot;

    captured(&snapshot);
    set_value(&snapshot, SMMU_BASE + 0x24U, UINT32_C(0x60000008));
    set_value(&snapshot, SMMU_BASE + 0xc00U, UINT32_C(0x00000005));
    add_entry(&snapshot, SMMU_BASE + 0x1014U, UINT32_C(0x0001f000));
    add_entry(&snapshot, SMMU_BASE + 0x1814U, UINT32_C(0x00000001));
    add_entry(&snapshot, SMMU_BASE + 0x85000U, UINT32_C(0x0a9f00e0));
    assert(accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));

    /* One stage-2-only bank exists, but bank 5 is still a valid stage-1 bank. */
    set_value(&snapshot, SMMU_BASE + 0x24U, UINT32_C(0x60010008));
    assert(accepts(&snapshot, UINT64_C(0x100000000), UINT64_C(0x1000)));
}

int main(void)
{
    test_captured_accepts();
    test_gen5_inherited_context();
    test_granule_capabilities_accepted();
    test_64k_stride_accepts();
    test_span_rejected();
    test_relocated_window_rejected();
    test_control_rejected();
    test_id_rejected();
    test_reserved_width_rejected();
    test_incomplete_snapshot_rejected();
    test_missing_record_rejected();
    test_stream_mapping_rejected();
    test_context_bank_rejected();
    test_bypass();
    test_derived_bank_geometry();
    return 0;
}
