/* SPDX-License-Identifier: GPL-3.0-only */
#include "update_trial_store.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* A sparse memory disk backs the complete 64 KiB config region plus a
 * preceding guard block. Access outside the backed extent is an IO failure. */
static unsigned char media[69632], scratch[8192], old_slot[4096], before[69632];
static struct kshim_blockdev dev;
static struct kshim_partition part;
static struct kshim_config_store store;
static uint64_t base_lba;
static unsigned reads, writes, flushes, fail_read_call;
static int partial_bytes, fail_write, fail_flush, corrupt_readback;
static uint64_t write_lba;
static uint32_t write_count;
static const unsigned char partition_uuid[16] = {
    0xb0,0xe3,0x4a,0x51,0x64,0x12,0x45,0xec,0xb0,0x45,0xbe,0xf3,0x66,0x4d,0x14,0xd3
};
static const unsigned char install_uuid[16] = {
    0xda,0xb9,0x8c,0x77,0xcc,0x2d,0x48,0xe1,0x9f,0x93,0x69,0xdb,0x40,0x8d,0x46,0x1e
};
static const unsigned char tx_uuid[16] = {
    0x12,0x34,0x56,0x78,0x9a,0xbc,0x4d,0xef,0x81,0x23,0x45,0x67,0x89,0xab,0xcd,0xef
};

static void le32(unsigned char *p, uint32_t n) {
    for (unsigned i = 0; i < 4; i++) p[i] = (unsigned char)(n >> (8 * i));
}
static void le64(unsigned char *p, uint64_t n) {
    for (unsigned i = 0; i < 8; i++) p[i] = (unsigned char)(n >> (8 * i));
}
static uint32_t fixture_crc(const unsigned char *p, size_t n) {
    uint32_t crc = ~0u;
    while (n--) {
        unsigned char b = *p++;
        for (unsigned i = 0; i < 8; i++) {
            unsigned bit = ((crc >> 31) ^ (b >> i)) & 1u;
            crc <<= 1;
            if (bit) crc ^= 0x04c11db7u;
        }
    }
    uint32_t out = 0;
    for (unsigned i = 0; i < 32; i++) out |= ((crc >> i) & 1u) << (31 - i);
    return ~out;
}
static void seal(unsigned char *p) { le32(p + 4092, fixture_crc(p, 4092)); }
static unsigned char *descriptor(void) { return media + 4096; }
static unsigned char *trial_slot(unsigned n) {
    return media + 4096 + (n == 0 ? KSHIM_UPDATE_TRIAL_TAIL_OFFSET_A : KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B);
}

static int rd(void *ctx, uint64_t lba, uint32_t n, void *p) {
    (void)ctx;
    reads++;
    if (reads == fail_read_call) return -1;
    if (lba < base_lba || lba - base_lba > sizeof(media) / dev.block_size ||
        n > sizeof(media) / dev.block_size - (lba - base_lba))
        return -1;
    memcpy(p, media + (lba - base_lba) * dev.block_size, (size_t)n * dev.block_size);
    if (corrupt_readback && writes) ((unsigned char *)p)[100] ^= 1;
    return 0;
}
static int wr(void *ctx, uint64_t lba, uint32_t n, const void *p) {
    (void)ctx;
    writes++;
    write_lba = lba;
    write_count = n;
    assert(lba >= base_lba && (lba - base_lba) * dev.block_size + (uint64_t)n * dev.block_size <= sizeof(media));
    size_t bytes = (size_t)n * dev.block_size;
    if (partial_bytes >= 0 && bytes > (size_t)partial_bytes) bytes = (size_t)partial_bytes;
    memcpy(media + (lba - base_lba) * dev.block_size, p, bytes);
    return fail_write ? -1 : 0;
}
static int fl(void *ctx) {
    (void)ctx;
    flushes++;
    return fail_flush ? -1 : 0;
}

static void reset_faults(void) {
    reads = writes = flushes = fail_read_call = 0;
    partial_bytes = -1;
    fail_write = fail_flush = corrupt_readback = 0;
    write_lba = 0;
    write_count = 0;
}

static void fixture(unsigned bs) {
    memset(media, 0, sizeof(media));
    memset(media, 0xa5, 4096); /* guard / archive-adjacent */
    memset(media + 8192, 0x5a, 8192); /* settings slots */
    memset(media + 16384, 0x3c, 16384); /* logs */
    dev = (struct kshim_blockdev){0, bs, 0, rd, wr, fl};
    part.first_lba = 128;
    part.last_lba = part.first_lba + KSHIM_CONFIG_PARTITION_BYTES / bs - 1;
    memcpy(part.uuid, partition_uuid, 16);
    dev.block_count = part.last_lba + 129;
    base_lba = part.last_lba + 1 - sizeof(media) / bs;
    unsigned char *d = descriptor();
    memset(d, 0, 4096);
    memcpy(d, "KSCFGV3", 8);
    le32(d + 8, 3);
    le32(d + 12, 4096);
    le64(d + 16, UINT64_C(2147483648));
    le64(d + 24, UINT64_C(82127306));
    le64(d + 32, UINT64_C(2147418112));
    le32(d + 40, 65536);
    le32(d + 44, 4096);
    memcpy(d + 48, partition_uuid, 16);
    memcpy(d + 64, install_uuid, 16);
    le32(d + 80, 4096);
    le32(d + 84, 8192);
    le32(d + 88, 2);
    le32(d + 92, 28672);
    le32(d + 96, 32768);
    le32(d + 100, 4096);
    seal(d);
    reset_faults();
    memset(&store, 0, sizeof(store));
    assert(kshim_config_open(&store, &dev, &part, 2, scratch, sizeof(scratch)) == KSHIM_STORAGE_OK);
    reset_faults();
}

static struct kshim_update_trial make_rec(uint64_t seq) {
    struct kshim_update_trial r;
    memset(&r, 0, sizeof(r));
    r.sequence = seq;
    memcpy(r.partition_uuid, partition_uuid, 16);
    memcpy(r.installation_uuid, install_uuid, 16);
    memcpy(r.transaction_uuid, tx_uuid, 16);
    r.state = KSHIM_UPDATE_TRIAL_PREPARED;
    r.from_slot = 0;
    r.target_slot = 1;
    r.remaining = 3;
    r.attempt_limit = 3;
    r.old_serial = 10;
    r.new_serial = 11;
    memcpy(r.os_name, "debian-trixie", 14);
    memset(r.old_commit, 0x11, 32);
    memset(r.new_commit, 0x22, 32);
    memset(r.old_boot_sha256, 0x33, 32);
    memset(r.new_boot_sha256, 0x44, 32);
    memset(r.plan_sha256, 0x55, 32);
    return r;
}

static void assert_only_trial_slot_changed(unsigned n, const struct kshim_update_trial *expected, int anchored) {
    size_t rel = n == 0 ? KSHIM_UPDATE_TRIAL_TAIL_OFFSET_A : KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B;
    size_t begin = 4096 + rel;
    size_t tail = begin + 4096;
    unsigned char enc[4096];
    assert(kshim_update_trial_encode(expected, enc, sizeof(enc)) == KSHIM_STORAGE_OK);
    assert(memcmp(before, media, begin) == 0);
    assert(memcmp(media + begin, enc, 4096) == 0);
    assert(write_lba == store.region_lba + rel / dev.block_size);
    assert(write_count == 4096 / dev.block_size);
    if (anchored) {
        /* The initial anchor wrote only the exact marker into copy B before
         * the record went to copy A. */
        unsigned char marker[4096];
        size_t anchor = 4096 + KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B;
        assert(kshim_update_trial_empty_marker(marker, sizeof(marker)) == KSHIM_STORAGE_OK);
        assert(memcmp(media + anchor, marker, 4096) == 0);
        tail = anchor + 4096;
        assert(writes == 2 && flushes == 2 && reads == 5);
    } else {
        assert(writes == 1 && flushes == 1 && reads == 3);
    }
    assert(memcmp(before + tail, media + tail, sizeof(media) - tail) == 0);
}

static void test_lifecycle_and_rejects(unsigned bs) {
    struct kshim_update_trial rec, snap, r1, r2, r3, bad, conflict;
    unsigned sel = 77, written = 99;

    fixture(bs);
    memset(&rec, 0xab, sizeof(rec));
    memcpy(&snap, &rec, sizeof(rec));
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(memcmp(&rec, &snap, sizeof(rec)) == 0 && sel == 77 && writes == 0);

    r1 = make_rec(1);
    memcpy(before, media, sizeof(media));
    reset_faults();
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);
    assert_only_trial_slot_changed(0, &r1, 1);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 1);

    r2 = make_rec(2);
    memcpy(before, media, sizeof(media));
    reset_faults();
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_OK && written == 1);
    assert_only_trial_slot_changed(1, &r2, 0);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 1 && rec.sequence == 2);

    r3 = make_rec(3);
    memcpy(before, media, sizeof(media));
    reset_faults();
    assert(kshim_update_trial_save(&store, 2, &r3, &written) == KSHIM_STORAGE_OK && written == 0);
    assert_only_trial_slot_changed(0, &r3, 0);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 3);

    /* Rejections: stale expected, invalid next seq, wrong identity, invalid state, UINT64_MAX */
    reset_faults(); written = 99;
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_CONFLICT && writes == 0 && written == 99);
    bad = make_rec(5);
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_CONFLICT && writes == 0 && written == 99);
    bad = make_rec(4); bad.partition_uuid[0] ^= 1;
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_CONFLICT && writes == 0 && written == 99);
    bad = make_rec(4); bad.installation_uuid[0] ^= 1;
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_CONFLICT && writes == 0 && written == 99);
    bad = make_rec(4); bad.remaining = 5;
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_FORMAT && writes == 0 && written == 99);
    bad = make_rec(0);
    assert(kshim_update_trial_save(&store, UINT64_MAX, &bad, &written) == KSHIM_STORAGE_OVERFLOW && writes == 0 && written == 99);

    /* Unsupported descriptor version & missing callbacks */
    bad = make_rec(4);
    store.descriptor_version = 2;
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_FORMAT && writes == 0 && written == 99);
    store.descriptor_version = 3;
    dev.write = NULL;
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_READ_ONLY && writes == 0 && written == 99);
    dev.write = wr; dev.flush = NULL;
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_READ_ONLY && writes == 0 && written == 99);
    dev.flush = fl;

    /* Equal-sequence conflict and both corrupt/nonzero */
    conflict = r3;
    conflict.transaction_uuid[0] ^= 1;
    assert(kshim_update_trial_encode(&conflict, trial_slot(1), 4096) == KSHIM_STORAGE_OK);
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_save(&store, 3, &bad, &written) == KSHIM_STORAGE_CONFLICT && writes == 0 && written == 99);
    memset(trial_slot(0), 0xee, 4096);
    memset(trial_slot(1), 0xff, 4096);
    assert(kshim_update_trial_load(&store, &rec, &sel) != KSHIM_STORAGE_OK);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) != KSHIM_STORAGE_OK && writes == 0 && written == 99);
}

static void seed_seq1(unsigned bs, struct kshim_update_trial *r2) {
    struct kshim_update_trial r1 = make_rec(1);
    unsigned written = 99;
    fixture(bs);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);
    memcpy(old_slot, trial_slot(0), 4096);
    memcpy(before, media, sizeof(media));
    reset_faults();
    *r2 = make_rec(2);
}

static void assert_b_bounds_and_a_intact(void) {
    size_t begin = 4096 + KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B;
    assert(memcmp(trial_slot(0), old_slot, 4096) == 0);
    assert(memcmp(before, media, begin) == 0);
    assert(memcmp(before + begin + 4096, media + begin + 4096, sizeof(media) - begin - 4096) == 0);
}

static void test_io_faults(unsigned bs) {
    struct kshim_update_trial r2, rec, snap;
    unsigned sel = 77, written = 99;

    /* (a) partial write ~100 bytes then write failure */
    seed_seq1(bs, &r2);
    partial_bytes = 100; fail_write = 1; written = 99;
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_IO && written == 99);
    assert_b_bounds_and_a_intact();
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 1);

    /* (b) flush failure */
    seed_seq1(bs, &r2);
    fail_flush = 1; written = 99;
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_IO && written == 99);
    assert_b_bounds_and_a_intact();

    /* (c) readback read failure on third read of save */
    seed_seq1(bs, &r2);
    fail_read_call = 3; written = 99;
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_IO && written == 99);
    assert_b_bounds_and_a_intact();

    /* (d) readback buffer altered only after write */
    seed_seq1(bs, &r2);
    corrupt_readback = 1; written = 99;
    assert(kshim_update_trial_save(&store, 1, &r2, &written) == KSHIM_STORAGE_IO && written == 99);
    assert_b_bounds_and_a_intact();

    /* Load read failure at call 1 and 2 must return IO, outputs unchanged */
    for (unsigned call = 1; call <= 2; call++) {
        reset_faults();
        fail_read_call = call;
        memset(&rec, 0x5c, sizeof(rec));
        memcpy(&snap, &rec, sizeof(rec));
        sel = 88;
        assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_IO);
        assert(memcmp(&rec, &snap, sizeof(rec)) == 0 && sel == 88);
    }
}

static int buffer_is_zero(const void *p, size_t n) {
    const unsigned char *b = p;
    for (size_t i = 0; i < n; ++i) if (b[i]) return 0;
    return 1;
}

/*
 * Initial-anchor tests.  The only durable thing before the first record is
 * the exact marker in copy B, so every fault below must end either in "plain
 * empty" (marker never landed) or "logical empty" (exact marker in B), never
 * in an unreadable journal.  Foreign or CRC-valid content must stay refused.
 */
static void test_initial_anchor(unsigned bs) {
    struct kshim_update_trial r1, rec, foreign;
    unsigned char marker[4096], altered[4096];
    unsigned sel = 77, written = 99;

    memset(marker, 0, sizeof(marker));
    assert(kshim_update_trial_empty_marker(marker, sizeof(marker)) == KSHIM_STORAGE_OK);
    assert(kshim_update_trial_is_empty_marker(marker, sizeof(marker)) == 1);
    assert(kshim_update_trial_decode(marker, sizeof(marker), partition_uuid,
                                     install_uuid, &rec) == KSHIM_STORAGE_CRC);
    memcpy(altered, marker, sizeof(altered));
    altered[0] ^= 1;
    assert(kshim_update_trial_is_empty_marker(altered, sizeof(altered)) == 0);
    memcpy(altered, marker, sizeof(altered));
    altered[KSHIM_UPDATE_TRIAL_EMPTY_MARKER_OFFSET] ^= 2;
    assert(kshim_update_trial_is_empty_marker(altered, sizeof(altered)) == 0);
    assert(kshim_update_trial_empty_marker(marker, 4095) == KSHIM_STORAGE_ARG);

    /* (a) anchor write tears before the distinguishing bit: copy B stays
     * zero and nothing reaches A.  The journal is plain empty and retries. */
    fixture(bs);
    r1 = make_rec(1);
    partial_bytes = 100; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO);
    assert(written == 99);
    assert(buffer_is_zero(trial_slot(0), 4096));
    assert(buffer_is_zero(trial_slot(1), 4096));
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY && sel == 77);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 1);

    /* (b) the marker bit lands but the first record write tears: load must
     * recover the exact logical-empty state from copy B, and sequence 1 may
     * be retried. */
    fixture(bs);
    r1 = make_rec(1);
    partial_bytes = 4092; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO);
    assert(written == 99);
    assert(memcmp(trial_slot(1), marker, 4096) == 0);
    assert(!buffer_is_zero(trial_slot(0), 4096));
    reset_faults();
    memset(&rec, 0x4d, sizeof(rec)); sel = 77;
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(sel == 77);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 1);

    /* (c) marker write lands but reports failure: copy A is untouched and a
     * retry accepts the already durable marker without rewriting it. */
    fixture(bs);
    r1 = make_rec(1);
    fail_write = 1; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO && written == 99);
    assert(memcmp(trial_slot(1), marker, 4096) == 0);
    assert(buffer_is_zero(trial_slot(0), 4096));
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);

    /* (d) marker flush fails: same state, still retryable. */
    fixture(bs);
    r1 = make_rec(1);
    fail_flush = 1; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO && written == 99);
    assert(memcmp(trial_slot(1), marker, 4096) == 0);
    assert(buffer_is_zero(trial_slot(0), 4096));
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);

    /* (e) marker readback is altered: refused before copy A is written. */
    fixture(bs);
    r1 = make_rec(1);
    corrupt_readback = 1; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO && written == 99);
    assert(memcmp(trial_slot(1), marker, 4096) == 0);
    assert(buffer_is_zero(trial_slot(0), 4096));
    reset_faults();
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);

    /* (f) a complete but foreign record image with a valid CRC in A plus the
     * exact marker in B is refused, never downgraded to empty. */
    fixture(bs);
    assert(kshim_update_trial_empty_marker(trial_slot(1), 4096) == KSHIM_STORAGE_OK);
    foreign = make_rec(1);
    foreign.partition_uuid[0] ^= 1;
    assert(kshim_update_trial_encode(&foreign, trial_slot(0), 4096) == KSHIM_STORAGE_OK);
    reset_faults();
    r1 = make_rec(1); written = 99;
    assert(kshim_update_trial_load(&store, &rec, &sel) != KSHIM_STORAGE_OK);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_FORMAT);
    assert(writes == 0 && written == 99);

    /* (g) the same holds for an incompatible but CRC-valid image. */
    fixture(bs);
    assert(kshim_update_trial_empty_marker(trial_slot(1), 4096) == KSHIM_STORAGE_OK);
    r1 = make_rec(1);
    assert(kshim_update_trial_encode(&r1, trial_slot(0), 4096) == KSHIM_STORAGE_OK);
    trial_slot(0)[0] ^= 1; /* wrong magic, CRC resealed below */
    seal(trial_slot(0));
    reset_faults();
    written = 99;
    assert(kshim_update_trial_load(&store, &rec, &sel) != KSHIM_STORAGE_OK);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_FORMAT);
    assert(writes == 0 && written == 99);

    /* (h) the marker is only recognised in its designated copy B. */
    fixture(bs);
    r1 = make_rec(1);
    assert(kshim_update_trial_empty_marker(trial_slot(0), 4096) == KSHIM_STORAGE_OK);
    reset_faults();
    written = 99;
    assert(kshim_update_trial_load(&store, &rec, &sel) != KSHIM_STORAGE_OK);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_FORMAT);
    assert(writes == 0 && written == 99);

    /* (i) CRC damage next to a zeroed copy stays ordinary corruption. */
    fixture(bs);
    r1 = make_rec(1);
    assert(kshim_update_trial_encode(&r1, trial_slot(0), 4096) == KSHIM_STORAGE_OK);
    trial_slot(0)[100] ^= 1;
    reset_faults();
    written = 99;
    assert(kshim_update_trial_load(&store, &rec, &sel) != KSHIM_STORAGE_OK);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_FORMAT);
    assert(writes == 0 && written == 99);
}

/*
 * An exact marker in copy B is not proof of durability: the bytes may be
 * cached while the flush that would have committed them failed.  Every retry
 * must therefore flush the existing marker again and keep copy A blank until
 * the flush succeeds.
 */
static void test_initial_anchor_repeated_flush(unsigned bs) {
    struct kshim_update_trial r1, rec;
    unsigned char marker[4096];
    unsigned sel = 77, written = 99;

    assert(kshim_update_trial_empty_marker(marker, sizeof(marker)) == KSHIM_STORAGE_OK);

    fixture(bs);
    r1 = make_rec(1);
    fail_flush = 1; written = 99;
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO);
    assert(written == 99);
    assert(memcmp(trial_slot(1), marker, 4096) == 0);
    assert(buffer_is_zero(trial_slot(0), 4096));

    /* The marker is visible but never confirmed durable: each retry rejects
     * the state without rewriting the marker or writing record A. */
    for (unsigned retry = 0; retry < 3; retry++) {
        reset_faults();
        fail_flush = 1; written = 99;
        assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
        assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_IO);
        assert(written == 99);
        assert(writes == 0 && flushes == 1);
        assert(buffer_is_zero(trial_slot(0), 4096));
        assert(memcmp(trial_slot(1), marker, 4096) == 0);
    }

    /* With the flush working again the same marker is accepted as-is and
     * sequence 1 commits. */
    reset_faults();
    written = 99;
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_EMPTY);
    assert(kshim_update_trial_save(&store, 0, &r1, &written) == KSHIM_STORAGE_OK && written == 0);
    assert(kshim_update_trial_load(&store, &rec, &sel) == KSHIM_STORAGE_OK && sel == 0 && rec.sequence == 1);
}

int main(void) {
    const unsigned sizes[] = {512, 4096};
    for (unsigned i = 0; i < 2; i++) {
        test_lifecycle_and_rejects(sizes[i]);
        test_io_faults(sizes[i]);
        test_initial_anchor(sizes[i]);
        test_initial_anchor_repeated_flush(sizes[i]);
    }
    puts("update_trial_store_test: ok");
    return 0;
}
