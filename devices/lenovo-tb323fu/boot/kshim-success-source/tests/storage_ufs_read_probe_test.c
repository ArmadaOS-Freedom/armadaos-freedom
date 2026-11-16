/* SPDX-License-Identifier: GPL-3.0-only */
/* Host-side contract tests for the bounded read-only UFS diagnostic sequence.
 * The generic claim/nop/open_lun/release transport is stubbed on purpose: this
 * suite protects ordering, ownership lifetime and the read-only contract, not
 * the hardware claim validation that storage_ufs_test already covers. The real
 * storage_block.c is linked so header/tail CRCs come from production code. */
#include <storage_ufs_read_probe.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

enum {
    REG_AHIT = 0x18, REG_IS = 0x20, REG_IE = 0x24, REG_HCS = 0x30,
    REG_BASE_LO = 0x50, REG_BASE_HI = 0x54, REG_DOORBELL = 0x58,
    REG_RUN = 0x60, REG_TASK_DB = 0x78
};
#define DMA_BASE UINT64_C(0x10000000)
#define BLOCK_SIZE 4096u
#define BLOCK_COUNT UINT64_C(600000)
#define FIRST_LBA UINT64_C(65536)
#define LAST_LBA UINT64_C(589823) /* 524288 blocks == two GiB */

#if defined(CONFIG_KSHIM_UFS_DEVICE_WAKE) && CONFIG_KSHIM_UFS_DEVICE_WAKE
#define PREP "P"
#define POWER_ENABLED 1
#else
#define PREP ""
#define POWER_ENABLED 0
#endif
static unsigned char scratch[8192];
static unsigned char arena[8192];

struct fixture {
    struct kshim_ufs_ops ops;
    struct kshim_ufs_host host;
    uint32_t reg[0x304 / 4];
    int claim_rc, claim_owns_failure;
    int nop_rc, lun_rc, gpt_rc, header_rc, tail_rc, release_rc;
    int power_rc;
    unsigned power_calls;
    bool power_evidence;
    uint32_t part_block_size;
    uint64_t part_first, part_last;
    uint8_t part_uuid[16];
    uint8_t header[BLOCK_SIZE], tail[BLOCK_SIZE];
    unsigned claim_calls, nop_calls, lun_calls, gpt_calls, release_calls;
    unsigned write_calls, read32_calls, read_count;
    uint64_t read_lba[4];
    uint32_t read_blocks[4];
    uint8_t lun_number;
    int lun_writes;
    uint32_t corrupt_off, corrupt_value, fail_read_off;
    bool link_before_valid, link_after_valid;
    struct kshim_ufs_link_state link_before, link_after;
    char log[32];
    unsigned log_len;
};
static struct fixture *G;

static void note(char c)
{
    if (G->log_len + 1 < sizeof(G->log)) G->log[G->log_len++] = c;
}
static int rd32(void *ctx, uint32_t off, uint32_t *value)
{
    struct fixture *f = ctx;
    assert(off < sizeof(f->reg));
    ++f->read32_calls;
    if (f->fail_read_off && off == f->fail_read_off) return 1;
    *value = f->reg[off / 4];
    return 0;
}
static int wr32(void *ctx, uint32_t off, uint32_t value)
{
    (void)ctx; (void)off; (void)value;
    ++G->write_calls; /* any register write from the sequence is a defect */
    return 0;
}
static int block_dev_read(void *ctx, uint64_t lba, uint32_t count, void *buffer)
{
    (void)ctx; (void)lba; (void)count; (void)buffer;
    return 1;
}

/* Stubbed generic transport. Only the parts the sequence depends on. */
int kshim_ufs_claim(struct kshim_ufs_host *h, const struct kshim_ufs_ops *o,
                    void *bytes, size_t arena_bytes, uint64_t dma)
{
    struct fixture *f = G;
    assert(h == &f->host);
    assert(bytes == arena && arena_bytes == KSHIM_UFS_DMA_BYTES);
    assert(dma == DMA_BASE);
    note('C');
    ++f->claim_calls;
    if (f->claim_rc && !f->claim_owns_failure) return f->claim_rc;
    memset(h, 0, sizeof(*h));
    h->ops = *o;
    h->arena = bytes;
    h->dma_address = dma;
    h->saved_base[0] = f->reg[REG_BASE_LO / 4];
    h->saved_base[1] = f->reg[REG_BASE_HI / 4];
    h->saved_run = f->reg[REG_RUN / 4];
    h->saved_ie = f->reg[REG_IE / 4];
    h->saved_ahit = f->reg[REG_AHIT / 4];
    h->owned = 1;
    if (f->claim_rc) {
        h->poisoned = 1;
        /* A claim that took ownership left link work behind before failing. */
        if (f->link_before_valid) h->link = f->link_before;
        return f->claim_rc;
    }
    h->poisoned = 0;
    return 0;
}
int kshim_ufs_nop(struct kshim_ufs_host *h)
{
    struct fixture *f = G;
    assert(h->owned);
    note('N');
    ++f->nop_calls;
    if (f->nop_rc) h->poisoned = 1;
    return f->nop_rc;
}
int kshim_ufs_prepare_device(struct kshim_ufs_host *h)
{
    note('P');++G->power_calls;
    assert(h->owned && G->nop_calls == 1 && G->lun_calls == 0);
    if(G->power_evidence){
        h->power=(struct kshim_ufs_power_state){0x22,0x11,3,0,2};
        h->last_scsi_opcode=0x1b;h->last_scsi_lun=0xd0;
        h->last_scsi_status=2;h->last_sense_key=2;
        h->last_sense_asc=4;h->last_sense_ascq=2;h->last_sense_valid=3;
    }
    return G->power_rc;
}
int kshim_ufs_open_lun(struct kshim_ufs_host *h, uint8_t number,
                       bool allow_writes, struct kshim_ufs_lun *l)
{
    struct fixture *f = G;
    assert(h->owned);
    note('L');
    ++f->lun_calls;
    f->lun_number = number;
    f->lun_writes = allow_writes ? 1 : 0;
    if (f->lun_rc) { h->poisoned = 1; return f->lun_rc; }
    memset(l, 0, sizeof(*l));
    l->host = h;
    l->number = number;
    l->block = (struct kshim_blockdev){l, f->part_block_size, BLOCK_COUNT,
                                      block_dev_read, NULL, NULL};
    return 0;
}
int kshim_ufs_release(struct kshim_ufs_host *h)
{
    struct fixture *f = G;
    assert(h->owned);
    note('R');
    ++f->release_calls;
    if(f->power_evidence){
        h->power=(struct kshim_ufs_power_state){0x22,0x22,7,0,0};
        h->last_scsi_status=0;h->last_sense_key=0;
        h->last_sense_asc=0;h->last_sense_ascq=0;h->last_sense_valid=0;
    }
    if (f->release_rc) { h->poisoned = 1; return f->release_rc; }
    h->owned = 0;
    h->poisoned = 0;
    f->reg[REG_BASE_LO / 4] = h->saved_base[0];
    f->reg[REG_BASE_HI / 4] = h->saved_base[1];
    f->reg[REG_RUN / 4] = h->saved_run;
    f->reg[REG_IE / 4] = h->saved_ie;
    f->reg[REG_AHIT / 4] = h->saved_ahit;
    f->reg[REG_DOORBELL / 4] = 0;
    f->reg[REG_TASK_DB / 4] = 0;
    /* Cleanup rewrites the host's own link record: a report that copies it only
     * after release can no longer recover the pre-release state. */
    if (f->link_after_valid) h->link = f->link_after;
    if (f->corrupt_off) f->reg[f->corrupt_off / 4] = f->corrupt_value;
    return 0;
}

int __wrap_kshim_gpt_find_backup(const struct kshim_blockdev *d,
                                 struct kshim_partition *out,
                                 void *buffer, size_t bytes);
int __wrap_kshim_block_read(const struct kshim_blockdev *d, uint64_t lba,
                            uint32_t count, void *buffer);

int __wrap_kshim_gpt_find_backup(const struct kshim_blockdev *d,
                                 struct kshim_partition *out,
                                 void *buffer, size_t bytes)
{
    struct fixture *f = G;
    assert(d);
    assert(d->block_size == f->part_block_size);
    assert(buffer == scratch && bytes == sizeof(scratch));
    note('G');
    ++f->gpt_calls;
    if (f->gpt_rc) { f->host.poisoned = 1; return f->gpt_rc; }
    out->first_lba = f->part_first;
    out->last_lba = f->part_last;
    memcpy(out->uuid, f->part_uuid, sizeof(out->uuid));
    return 0;
}
int __wrap_kshim_block_read(const struct kshim_blockdev *d, uint64_t lba,
                            uint32_t count, void *buffer)
{
    struct fixture *f = G;
    assert(d && buffer == scratch && count == 1);
    assert(f->read_count < sizeof(f->read_lba) / sizeof(f->read_lba[0]));
    f->read_lba[f->read_count] = lba;
    f->read_blocks[f->read_count] = count;
    ++f->read_count;
    if (lba == f->part_first) {
        note('H');
        if (f->header_rc) { f->host.poisoned = 1; return f->header_rc; }
        memcpy(buffer, f->header, d->block_size);
        return 0;
    }
    assert(lba == f->part_last);
    note('T');
    if (f->tail_rc) { f->host.poisoned = 1; return f->tail_rc; }
    memcpy(buffer, f->tail, d->block_size);
    return 0;
}

static void fixture_init(struct fixture *f)
{
    memset(f, 0, sizeof(*f));
    memset(scratch, 0, sizeof(scratch));
    memset(arena, 0, sizeof(arena));
    f->log_len = 0; /* struct was just zeroed, log included */
    G = f;
    f->ops = (struct kshim_ufs_ops){f, rd32, wr32, NULL, NULL, NULL, NULL, false, false};
    f->reg[REG_HCS / 4] = 0x0f;
    f->reg[REG_IS / 4] = 0x08;
    f->reg[REG_DOORBELL / 4] = 4; /* pre-release value, capture must see it */
    f->reg[REG_TASK_DB / 4] = 0;
    f->reg[REG_BASE_LO / 4] = 0x11110000;
    f->reg[REG_BASE_HI / 4] = 2;
    f->reg[REG_RUN / 4] = 1;
    f->reg[REG_IE / 4] = 0x400;
    f->reg[REG_AHIT / 4] = 0x123;
    f->part_block_size = BLOCK_SIZE;
    f->part_first = FIRST_LBA;
    f->part_last = LAST_LBA;
    for (unsigned i = 0; i < sizeof(f->part_uuid); ++i)
        f->part_uuid[i] = (uint8_t)(0x30 + i);
    for (unsigned i = 0; i < BLOCK_SIZE; ++i) {
        f->header[i] = (uint8_t)(i * 7 + 0x11);
        f->tail[i] = (uint8_t)(i * 13 + 0x5a);
    }
    arena[8] = 0xf3; /* only the OCS nibble may reach the report */
    for (unsigned i = 0; i < 16; ++i) arena[1536 + i] = (uint8_t)(0xa0 + i);
}
static void report_init(struct kshim_ufs_read_report *r)
{
    memset(r, 0, sizeof(*r));
    r->version = KSHIM_UFS_READ_REPORT_VERSION;
    r->board = 0x323f;
}
static bool run(struct fixture *f, struct kshim_ufs_read_report *r)
{
    return kshim_ufs_read_probe(&f->host, &f->ops, arena, DMA_BASE, scratch,
                                sizeof(scratch), r);
}

static void test_success(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGHTR") == 0);
    assert(f.claim_calls == 1 && f.nop_calls == 1 && f.lun_calls == 1);
    assert(f.gpt_calls == 1 && f.release_calls == 1 && f.write_calls == 0);
    assert(f.lun_number == 0 && f.lun_writes == 0);
    assert(f.read_count == 2);
    assert(f.read_lba[0] == FIRST_LBA && f.read_blocks[0] == 1);
    assert(f.read_lba[1] == LAST_LBA && f.read_blocks[1] == 1);
    assert(r.version == KSHIM_UFS_READ_REPORT_VERSION && r.board == 0x323f);
    assert(r.stage == KSHIM_UFS_READ_STAGE_DONE);
    assert(r.status == KSHIM_STORAGE_OK);
    assert(r.release_status == KSHIM_STORAGE_OK);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
    assert(r.block_size == BLOCK_SIZE && r.block_count == BLOCK_COUNT);
    assert(r.dma_base == DMA_BASE);
    assert(r.first_lba == FIRST_LBA && r.last_lba == LAST_LBA);
    assert(memcmp(r.partition_uuid, f.part_uuid, sizeof(r.partition_uuid)) == 0);
    assert(r.header_crc == kshim_crc32(f.header, BLOCK_SIZE));
    assert(r.tail_crc == kshim_crc32(f.tail, BLOCK_SIZE));
    assert(r.header_crc != r.tail_crc);
    assert(memcmp(r.header_prefix, f.header, sizeof(r.header_prefix)) == 0);
    assert(r.hcs == 0x0f && r.is == 0x08 && r.doorbell == 4);
    assert(r.ocs == 3);
    for (unsigned i = 0; i < 16; ++i)
        assert(r.response_prefix[i] == (uint8_t)(0xa0 + i));
    assert(r.debug_valid == 15u);
    assert(f.host.owned == 0); /* release cleared it; probe must not re-own */
}

static void test_preconditions(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(NULL, &f.ops, arena, DMA_BASE, scratch,
                                 sizeof(scratch), &r));
    assert(r.status == KSHIM_STORAGE_ARG);
    assert(r.stage == KSHIM_UFS_READ_STAGE_NOT_RUN);
    assert(r.handoff_safe == 0 && r.restore_verified == 0);
    assert(r.release_status == KSHIM_UFS_READ_UNRUN);
    assert(r.version == KSHIM_UFS_READ_REPORT_VERSION && r.board == 0x323f);
    assert(f.log_len == 0 && f.write_calls == 0 && f.release_calls == 0);

    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(&f.host, NULL, arena, DMA_BASE, scratch,
                                 sizeof(scratch), &r));
    assert(r.status == KSHIM_STORAGE_ARG && f.log_len == 0);

    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(&f.host, &f.ops, NULL, DMA_BASE, scratch,
                                 sizeof(scratch), &r));
    assert(r.status == KSHIM_STORAGE_ARG && f.log_len == 0);

    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(&f.host, &f.ops, arena, 0, scratch,
                                 sizeof(scratch), &r));
    assert(r.status == KSHIM_STORAGE_ARG && f.log_len == 0);

    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(&f.host, &f.ops, arena, DMA_BASE, NULL,
                                 sizeof(scratch), &r));
    assert(r.status == KSHIM_STORAGE_ARG && f.log_len == 0);

    fixture_init(&f);
    report_init(&r);
    assert(!kshim_ufs_read_probe(&f.host, &f.ops, arena, DMA_BASE, scratch,
                                 sizeof(scratch) - 1, &r));
    assert(r.status == KSHIM_STORAGE_ARG && r.handoff_safe == 0);
    assert(f.log_len == 0 && f.write_calls == 0);

    fixture_init(&f);
    assert(!kshim_ufs_read_probe(&f.host, &f.ops, arena, DMA_BASE, scratch,
                                 sizeof(scratch), NULL));
    assert(f.log_len == 0);
}

static void test_preowned(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.host.owned = 1;
    f.host.arena = arena;
    assert(!run(&f, &r));
    assert(r.stage == KSHIM_UFS_READ_STAGE_CLAIM);
    assert(r.status == KSHIM_UFS_BUSY);
    assert(r.handoff_safe == 0 && r.restore_verified == 0);
    assert(f.log_len == 0 && f.release_calls == 0);
    assert(f.host.owned == 1); /* foreign ownership is never revoked */
}

static void test_claim_fails_without_ownership(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.claim_rc = KSHIM_UFS_BUSY;
    assert(run(&f, &r));
    assert(strcmp(f.log, "C") == 0);
    assert(f.release_calls == 0);
    assert(r.stage == KSHIM_UFS_READ_STAGE_CLAIM);
    assert(r.status == KSHIM_UFS_BUSY);
    assert(r.release_status == KSHIM_STORAGE_OK);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
    assert(f.host.owned == 0);
    assert(r.block_size == 0 && r.block_count == 0 && r.first_lba == 0);
    assert(r.header_crc == 0 && r.tail_crc == 0 && f.read_count == 0);
}

static void test_claim_fails_after_ownership(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.claim_rc = KSHIM_STORAGE_IO;
    f.claim_owns_failure = 1;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CR") == 0);
    assert(f.release_calls == 1);
    assert(r.stage == KSHIM_UFS_READ_STAGE_CLAIM);
    assert(r.status == KSHIM_STORAGE_IO);
    assert(r.release_status == KSHIM_STORAGE_OK);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
    assert((r.debug_valid & 8u) == 0 && r.ocs == 0);
    assert(f.nop_calls == 0 && f.lun_calls == 0 && f.read_count == 0);
}

static void test_nop_failure(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.nop_rc = KSHIM_UFS_TIMEOUT;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CNR") == 0);
    assert(f.release_calls == 1 && f.write_calls == 0);
    assert(r.stage == KSHIM_UFS_READ_STAGE_NOP);
    assert(r.status == KSHIM_UFS_TIMEOUT);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
    assert(f.lun_calls == 0 && f.read_count == 0 && r.block_size == 0);
    assert((r.debug_valid & 8u) == 0);
}

static void test_open_lun_failure(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.lun_rc = KSHIM_UFS_SCSI;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LR") == 0);
    assert(f.lun_writes == 0 && f.lun_number == 0);
    assert(f.release_calls == 1);
    assert(r.stage == KSHIM_UFS_READ_STAGE_LUN);
    assert(r.status == KSHIM_UFS_SCSI);
    assert(r.block_size == 0 && r.block_count == 0 && f.read_count == 0);
}

static void test_gpt_failure(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.gpt_rc = KSHIM_STORAGE_CRC;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGR") == 0);
    assert(f.release_calls == 1);
    assert(r.stage == KSHIM_UFS_READ_STAGE_GPT);
    assert(r.status == KSHIM_STORAGE_CRC);
    assert(r.first_lba == 0 && r.last_lba == 0);
    assert(f.read_count == 0 && r.header_crc == 0 && r.tail_crc == 0);
}

static void test_geometry_rejected_before_read(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    /* One block short of two GiB: rejected inside the GPT stage. */
    fixture_init(&f);
    report_init(&r);
    f.part_last = LAST_LBA - 1;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGR") == 0);
    assert(r.stage == KSHIM_UFS_READ_STAGE_GPT);
    assert(r.status == KSHIM_STORAGE_FORMAT);
    assert(f.read_count == 0);
    assert(r.first_lba == 0 && r.last_lba == 0);
    assert(r.header_crc == 0 && r.tail_crc == 0);
    assert(f.release_calls == 1 && r.restore_verified == 1);

    /* A block size outside 512/4096 is refused before any block read. */
    fixture_init(&f);
    report_init(&r);
    f.part_block_size = 1024;
    assert(run(&f, &r));
    assert(r.stage == KSHIM_UFS_READ_STAGE_GPT);
    assert(r.status == KSHIM_STORAGE_FORMAT);
    assert(f.read_count == 0 && f.release_calls == 1);

    /* Inverted range is a range fault, still before the first data read. */
    fixture_init(&f);
    report_init(&r);
    f.part_first = LAST_LBA;
    f.part_last = FIRST_LBA;
    assert(run(&f, &r));
    assert(r.status == KSHIM_STORAGE_RANGE && f.read_count == 0);
}

static void test_header_failure(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.header_rc = KSHIM_STORAGE_IO;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGHR") == 0);
    assert(f.read_count == 1 && f.read_lba[0] == FIRST_LBA);
    assert(r.stage == KSHIM_UFS_READ_STAGE_HEADER);
    assert(r.status == KSHIM_STORAGE_IO);
    assert(r.header_crc == 0 && r.tail_crc == 0);
    for (unsigned i = 0; i < sizeof(r.header_prefix); ++i)
        assert(r.header_prefix[i] == 0);
    assert(f.release_calls == 1 && r.restore_verified == 1);
}

static void test_tail_failure(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.tail_rc = KSHIM_STORAGE_IO;
    assert(run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGHTR") == 0);
    assert(f.read_count == 2 && f.read_lba[1] == LAST_LBA);
    assert(r.stage == KSHIM_UFS_READ_STAGE_TAIL);
    assert(r.status == KSHIM_STORAGE_IO);
    assert(r.header_crc == kshim_crc32(f.header, BLOCK_SIZE));
    assert(r.tail_crc == 0);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
}

static void test_release_failure_quarantines(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.release_rc = KSHIM_UFS_BUSY;
    assert(!run(&f, &r));
    assert(strcmp(f.log, "CN" PREP "LGHTR") == 0);
    assert(f.release_calls == 1);
    assert(r.stage == KSHIM_UFS_READ_STAGE_DONE);
    assert(r.status == KSHIM_STORAGE_OK);
    assert(r.release_status == KSHIM_UFS_BUSY);
    assert(r.restore_verified == 0 && r.handoff_safe == 0);
    assert(f.host.owned == 1 && f.host.poisoned == 1);
}

static void test_restore_mismatch_rejects_safe(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.corrupt_off = REG_RUN;
    f.corrupt_value = 0; /* firmware run bit never restored */
    assert(!run(&f, &r));
    assert(f.release_calls == 1);
    assert(r.status == KSHIM_STORAGE_OK); /* the probe result is unchanged */
    assert(r.release_status == KSHIM_STORAGE_IO);
    assert(r.restore_verified == 0 && r.handoff_safe == 0);

    fixture_init(&f);
    report_init(&r);
    f.corrupt_off = REG_DOORBELL;
    f.corrupt_value = 1;
    assert(!run(&f, &r));
    assert(r.release_status == KSHIM_STORAGE_IO && r.handoff_safe == 0);

    fixture_init(&f);
    report_init(&r);
    f.corrupt_off = REG_TASK_DB;
    f.corrupt_value = 2;
    assert(!run(&f, &r));
    assert(r.release_status == KSHIM_STORAGE_IO && r.handoff_safe == 0);

    fixture_init(&f);
    report_init(&r);
    f.corrupt_off = REG_BASE_LO;
    f.corrupt_value = 0;
    assert(!run(&f, &r));
    assert(r.restore_verified == 0 && r.handoff_safe == 0);
}

static void test_restore_read_failure_rejects_safe(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;
    fixture_init(&f);
    report_init(&r);
    f.fail_read_off = REG_IE;
    assert(!run(&f, &r));
    assert(f.release_calls == 1);
    assert(r.status == KSHIM_STORAGE_OK);
    assert(r.release_status == KSHIM_STORAGE_IO);
    assert(r.restore_verified == 0 && r.handoff_safe == 0);
}

/* Link evidence must survive cleanup. A claim that already took ownership and
 * then failed (-23) still has to publish the FSM it
 * observed before release and the one cleanup left behind. The fixture
 * overwrites host->link in release, so copying only afterwards loses the
 * pre-release state. */
static void test_claim_failure_keeps_link_evidence(void)
{
    struct fixture f;
    struct kshim_ufs_read_report r;

    fixture_init(&f);
    report_init(&r);
    f.claim_rc = KSHIM_UFS_RESPONSE;
    f.claim_owns_failure = 1;
    f.link_before_valid = true;
    f.link_before = (struct kshim_ufs_link_state){
        .initial_tx = 1, .current_tx = 2, .last_command = 1,
        .command_result = 0, .last_is = 0x400, .power_status = 1,
        .phase = 3, .uncertain = 0, .restore_hibern8 = 1,
    };
    f.link_after_valid = true;
    f.link_after = (struct kshim_ufs_link_state){
        .initial_tx = 0, .current_tx = 1, .last_command = 0,
        .command_result = 0, .last_is = 0, .power_status = 0,
        .phase = 6, .uncertain = 0, .restore_hibern8 = 0,
    };
    assert(run(&f, &r));
    assert(strcmp(f.log, "CR") == 0);
    assert(f.release_calls == 1);
    assert(r.stage == KSHIM_UFS_READ_STAGE_CLAIM);
    assert(r.status == KSHIM_UFS_RESPONSE);
    assert(r.release_status == KSHIM_STORAGE_OK);
    assert(r.restore_verified == 1 && r.handoff_safe == 1);
    assert(r.link_before_release.initial_tx == 1);
    assert(r.link_before_release.current_tx == 2);
    assert(r.link_before_release.last_command == 1);
    assert(r.link_before_release.command_result == 0);
    assert(r.link_before_release.last_is == 0x400);
    assert(r.link_before_release.power_status == 1);
    assert(r.link_before_release.phase == 3);
    assert(r.link_before_release.uncertain == 0);
    assert(r.link_before_release.restore_hibern8 == 1);
    assert(r.link_after_release.phase == 6);
    assert(r.link_after_release.current_tx == 1);
    assert(r.link_after_release.restore_hibern8 == 0);
    assert(f.host.owned == 0);
}

static void test_power_prepare_and_evidence(void)
{
    struct fixture f;struct kshim_ufs_read_report r;
    fixture_init(&f);report_init(&r);
    f.power_rc=KSHIM_UFS_SCSI;f.power_evidence=true;
    assert(run(&f,&r));
    if(!POWER_ENABLED){assert(f.power_calls==0);return;}
    assert(strcmp(f.log,"CNPR")==0 && f.lun_calls==0);
    assert(r.stage==10 && r.status==KSHIM_UFS_SCSI);
    assert(r.power_before_release.current_mode==0x11);
    assert(r.power_before_release.restore_mode==2);
    assert(r.power_after_release.current_mode==0x22);
    assert(r.power_after_release.restore_mode==0);
    assert(r.scsi_before_release.opcode==0x1b);
    assert(r.scsi_before_release.lun==0xd0);
    assert(r.scsi_before_release.status==2 && r.scsi_before_release.sense_key==2);
    assert(r.scsi_before_release.asc==4 && r.scsi_before_release.ascq==2);
    assert(r.scsi_before_release.sense_valid==3);
    assert(r.scsi_after_release.status==0 && r.scsi_after_release.sense_valid==0);
    fixture_init(&f);report_init(&r);f.power_rc=KSHIM_UFS_SCSI;
    f.power_evidence=true;f.release_rc=KSHIM_UFS_BUSY;
    assert(!run(&f,&r));
    assert(r.power_before_release.restore_mode==2 && r.power_after_release.phase==7);
    assert(r.scsi_before_release.status==2 && r.scsi_after_release.status==0);
    assert(r.handoff_safe==0);
}
int main(void)
{
    test_power_prepare_and_evidence();
    test_success();
    test_preconditions();
    test_preowned();
    test_claim_fails_without_ownership();
    test_claim_fails_after_ownership();
    test_nop_failure();
    test_open_lun_failure();
    test_gpt_failure();
    test_geometry_rejected_before_read();
    test_header_failure();
    test_tail_failure();
    test_release_failure_quarantines();
    test_restore_mismatch_rejects_safe();
    test_restore_read_failure_rejects_safe();
    test_claim_failure_keeps_link_evidence();
    puts("storage_ufs_read_probe_test: ok");
    return 0;
}
