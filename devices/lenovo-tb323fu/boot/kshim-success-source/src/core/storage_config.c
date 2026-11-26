/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_config.h"

static uint32_t get32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
           (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static uint64_t get64(const uint8_t *p) { return get32(p) | (uint64_t)get32(p + 4) << 32; }
static void put32(uint8_t *p, uint32_t v)
{
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static void put64(uint8_t *p, uint64_t v)
{
    for (unsigned i = 0; i < 8; ++i) p[i] = (uint8_t)(v >> (8 * i));
}
static int zero(const uint8_t *p, size_t n)
{
    while (n--) if (*p++) return 0;
    return 1;
}
static int same(const uint8_t *a, const uint8_t *b, size_t n)
{
    while (n--) if (*a++ != *b++) return 0;
    return 1;
}
static void copy(uint8_t *dst, const uint8_t *src, size_t n)
{
    while (n--) *dst++ = *src++;
}
/* Shared validator for the packed next_action. Rejects unknown action
 * values and unknown flag bits; used by both config and settings layers. */
static int next_action_valid(uint32_t packed)
{
    return (packed & ~(KSHIM_ACTION_MASK | KSHIM_FLAGS_KNOWN)) == 0 &&
           kshim_next_action_value(packed) <= KSHIM_ACTION_MAX;
}
static int values_valid(const struct kshim_config_values *v)
{
    return v && v->device_choice <= 2 && v->default_target <= 4 &&
           v->language <= 1 && v->last_target <= 3 &&
           next_action_valid(v->next_action);
}
int kshim_config_open(struct kshim_config_store *store,
                      const struct kshim_blockdev *dev,
                      const struct kshim_partition *partition, uint32_t board_id,
                      void *scratch, size_t scratch_bytes)
{
    if (!store) return KSHIM_STORAGE_ARG;
    store->ready = 0;
    if (!dev || !partition || !scratch || scratch_bytes < KSHIM_STORAGE_SCRATCH_BYTES ||
        (dev->block_size != 512 && dev->block_size != 4096) || !dev->read || !board_id)
        return KSHIM_STORAGE_ARG;
    if (partition->first_lba > partition->last_lba ||
        partition->last_lba >= dev->block_count ||
        partition->last_lba - partition->first_lba != KSHIM_CONFIG_PARTITION_BYTES / dev->block_size - 1)
        return KSHIM_STORAGE_RANGE;
    if (zero(partition->uuid, 16)) return KSHIM_STORAGE_FORMAT;
    uint64_t region_offset = KSHIM_CONFIG_PARTITION_BYTES - KSHIM_CONFIG_REGION_BYTES;
    uint64_t region_lba = partition->first_lba + region_offset / dev->block_size;
    uint8_t *d = scratch;
    int rc = kshim_block_read(dev, region_lba, KSHIM_CONFIG_SLOT_BYTES / dev->block_size, d);
    if (rc) return rc;
    uint32_t descriptor_version = get32(d + 8);
    if (!same(d, (const uint8_t *)"KSCFGV3", 8) || descriptor_version < 1 ||
        descriptor_version > 3 || get32(d + 12) != KSHIM_CONFIG_SLOT_BYTES)
        return KSHIM_STORAGE_FORMAT;
    if (kshim_crc32(d, 4092) != get32(d + 4092)) return KSHIM_STORAGE_CRC;
    if (get64(d + 16) != KSHIM_CONFIG_PARTITION_BYTES ||
        get64(d + 24) < 65536 || get64(d + 24) > region_offset ||
        get64(d + 32) != region_offset || get32(d + 40) != KSHIM_CONFIG_REGION_BYTES ||
        get32(d + 44) != KSHIM_CONFIG_SLOT_BYTES || get32(d + 80) != 4096 ||
        get32(d + 84) != 8192) return KSHIM_STORAGE_RANGE;
    if (!same(d + 48, partition->uuid, 16) || zero(d + 64, 16) ||
        get32(d + 88) != board_id) return KSHIM_STORAGE_FORMAT;
    if (descriptor_version < 3) {
        if (!zero(d + 92, 4000)) return KSHIM_STORAGE_FORMAT;
    } else if (get32(d + 92) != 28672 || get32(d + 96) != 32768 ||
               get32(d + 100) != 4096 || !zero(d + 104, 3988)) {
        return KSHIM_STORAGE_FORMAT;
    }
    /* Check only the reserved tail, without reading mutable logs, trial data,
     * or any recovery payload, and without allocating memory. */
    for (uint32_t offset = descriptor_version == 3 ? 9 * KSHIM_CONFIG_SLOT_BYTES :
         descriptor_version == 2 ? 7 * KSHIM_CONFIG_SLOT_BYTES :
         3 * KSHIM_CONFIG_SLOT_BYTES;
         offset < KSHIM_CONFIG_REGION_BYTES; offset += KSHIM_CONFIG_SLOT_BYTES) {
        uint8_t *reserved = d + KSHIM_CONFIG_SLOT_BYTES;
        rc = kshim_block_read(dev, region_lba + offset / dev->block_size,
                              KSHIM_CONFIG_SLOT_BYTES / dev->block_size, reserved);
        if (rc) return rc;
        if (!zero(reserved, KSHIM_CONFIG_SLOT_BYTES)) return KSHIM_STORAGE_FORMAT;
    }
    store->dev = dev;
    store->partition = *partition;
    store->region_lba = region_lba;
    copy(store->installation_uuid, d + 64, 16);
    store->scratch = scratch;
    store->board_id = board_id;
    store->descriptor_version = descriptor_version;
    store->ready = 1;
    return KSHIM_STORAGE_OK;
}
static int store_valid(const struct kshim_config_store *s)
{
    return s && s->ready == 1 && s->dev && s->scratch;
}
/* Returns 1 valid, 0 erased, -1 malformed. A malformed peer is recoverable
 * while another slot remains valid, but IO failures are never erased data. */
static int record_valid(const struct kshim_config_store *s, const uint8_t *p)
{
    if (zero(p, 4096)) return 0;
    uint32_t version=get32(p+8);
    if (!same(p, (const uint8_t *)"KSCFGR1", 8) || (version<1 || version>4) ||
        get32(p + 12) != 4096 || !get64(p + 16) ||
        !same(p + 24, s->partition.uuid, 16) || !same(p + 40, s->installation_uuid, 16) ||
        get32(p + 56) > 2 || get32(p + 60) > (version==1 ? 3u : 4u) ||
        get32(p + 64) > 1 || (version>=2 && get32(p+68)>3)) return -1;
    /* v3 caps action <= 1; v4 uses the packed validator. */
    if (version==3 && get32(p+72)>1) return -1;
    if (version==4 && !next_action_valid(get32(p+72))) return -1;
    if (!zero(p + (version==1 ? 68 : version==2 ? 72 : 76),
              version==1 ? 4024 : version==2 ? 4020 : 4016)) return -1;
    if (kshim_crc32(p, 4092) != get32(p + 4092)) return -1;
    return 1;
}
static int read_slots(struct kshim_config_store *s, int *best, int *target)
{
    const struct kshim_blockdev *d = s->dev;
    uint32_t blocks = KSHIM_CONFIG_SLOT_BYTES / d->block_size;
    for (unsigned i = 0; i < 2; ++i) {
        int rc = kshim_block_read(d, s->region_lba + (i + 1) * blocks,
                                  blocks, s->scratch + i * KSHIM_CONFIG_SLOT_BYTES);
        if (rc) return rc;
    }
    const uint8_t *a = s->scratch, *b = a + KSHIM_CONFIG_SLOT_BYTES;
    int va = record_valid(s, a), vb = record_valid(s, b);
    *best = -1; *target = 0;
    if (va == 1 && vb == 1) {
        uint64_t sa = get64(a + 16), sb = get64(b + 16);
        if (sa == sb && !same(a, b, KSHIM_CONFIG_SLOT_BYTES)) return KSHIM_STORAGE_CONFLICT;
        *best = sa >= sb ? 0 : 1;
        *target = 1 - *best;
    } else if (va == 1 || vb == 1) {
        *best = va == 1 ? 0 : 1;
        *target = 1 - *best;
    } else {
        return va == 0 && vb == 0 ? KSHIM_STORAGE_EMPTY : KSHIM_STORAGE_FORMAT;
    }
    return KSHIM_STORAGE_OK;
}
int kshim_config_load(struct kshim_config_store *s, struct kshim_config_values *v,
                      uint64_t *sequence)
{
    if (!store_valid(s) || !v || !sequence) return KSHIM_STORAGE_ARG;
    int best, target;
    int rc = read_slots(s, &best, &target);
    if (rc) return rc;
    const uint8_t *p = s->scratch + best * KSHIM_CONFIG_SLOT_BYTES;
    v->device_choice = get32(p + 56);
    v->default_target = get32(p + 60);
    v->language = get32(p + 64);
    v->last_target = get32(p+8)>=2 ? get32(p+68) : 0;
    v->next_action = get32(p+8)>=3 ? get32(p+72) : 0;
    *sequence = get64(p + 16);
    return KSHIM_STORAGE_OK;
}
int kshim_config_save(struct kshim_config_store *s,
                      const struct kshim_config_values *v, uint64_t *sequence)
{
    if (!store_valid(s) || !values_valid(v) || !sequence) return KSHIM_STORAGE_ARG;
    if (!s->dev->write || !s->dev->flush) return KSHIM_STORAGE_READ_ONLY;
    int best, target;
    int rc = read_slots(s, &best, &target);
    if (rc != KSHIM_STORAGE_OK && rc != KSHIM_STORAGE_EMPTY && rc != KSHIM_STORAGE_FORMAT) return rc;
    uint64_t next = best < 0 ? 1 : get64(s->scratch + best * KSHIM_CONFIG_SLOT_BYTES + 16);
    if (best >= 0) {
        if (next == UINT64_MAX) return KSHIM_STORAGE_OVERFLOW;
        ++next;
    }
    /* v3 expresses only the bare action (0..1) with zero flags; anything
     * richer requires v4, which reads flags at offset 72. */
    uint32_t version = v->next_action <= KSHIM_ACTION_EDL ? 3 : 4;
    uint8_t *record = s->scratch;
    for (size_t i = 0; i < KSHIM_CONFIG_SLOT_BYTES; ++i) record[i] = 0;
    copy(record, (const uint8_t *)"KSCFGR1", 8);
    put32(record + 8, version); put32(record + 12, KSHIM_CONFIG_SLOT_BYTES);
    put64(record + 16, next);
    copy(record + 24, s->partition.uuid, 16);
    copy(record + 40, s->installation_uuid, 16);
    put32(record + 56, v->device_choice); put32(record + 60, v->default_target);
    put32(record + 64, v->language); put32(record + 68, v->last_target);
    put32(record + 72, v->next_action);
    put32(record + 4092, kshim_crc32(record, 4092));
    uint32_t blocks = KSHIM_CONFIG_SLOT_BYTES / s->dev->block_size;
    uint64_t lba = s->region_lba + (uint64_t)(target + 1) * blocks;
    rc = kshim_block_write(s->dev, lba, blocks, record);
    if (rc) return rc;
    rc = kshim_block_flush(s->dev);
    if (rc) return rc;
    uint8_t *readback = record + KSHIM_CONFIG_SLOT_BYTES;
    rc = kshim_block_read(s->dev, lba, blocks, readback);
    if (rc) return rc;
    if (!same(record, readback, KSHIM_CONFIG_SLOT_BYTES)) return KSHIM_STORAGE_IO;
    *sequence = next;
    return KSHIM_STORAGE_OK;
}


/* ---------- Boot-log record writer ---------- */

#define KSHIM_BOOTLOG_MAGIC      "KSBOOTL1"
#define KSHIM_BOOTLOG_MAGIC_LEN  8u
#define KSHIM_BOOTLOG_REC_BYTES  4096u
#define KSHIM_BOOTLOG_VERSION    1u
/* Descriptor v3 designates the mutable log area at region offsets 12288
 * and 20480 (two alternating 8 KiB physical slots, of which we only use
 * the first logical 4096 bytes). Offsets are in bytes from region start. */
#define KSHIM_BOOTLOG_SLOT0_OFF  12288u
#define KSHIM_BOOTLOG_SLOT1_OFF  20480u

static int bootlog_target_ok(uint32_t t) { return t <= 3u; }
static int bootlog_phase_ok(uint32_t p) { return p >= 1u && p <= 4u; }

/* Validates a single 4096-byte boot-log record against this store.
 * Returns 1 valid, 0 erased, -1 malformed (wrong magic/exclusive
 * partition/installation mismatch, bad CRC, bad version, bad fields,
 * or non-zero padding). A "-1" peer is only reusable if the caller can
 * prove via `bootlog_owned` that it was produced by this installation. */
static int bootlog_record_valid(const struct kshim_config_store *s,
                                const uint8_t *p)
{
    if (zero(p, KSHIM_BOOTLOG_REC_BYTES)) return 0;
    if (!same(p, (const uint8_t *)KSHIM_BOOTLOG_MAGIC, KSHIM_BOOTLOG_MAGIC_LEN))
        return -1;
    if (get32(p + 8) != KSHIM_BOOTLOG_VERSION) return -1;
    if (get32(p + 12) != KSHIM_BOOTLOG_REC_BYTES) return -1;
    if (get64(p + 16) == 0) return -1;
    if (!same(p + 24, s->partition.uuid, 16)) return -1;
    if (!same(p + 40, s->installation_uuid, 16)) return -1;
    if (get32(p + 56) != s->board_id) return -1;
    if (!bootlog_target_ok(get32(p + 60))) return -1;
    if (!bootlog_phase_ok(get32(p + 64))) return -1;
    if (!zero(p + 72, 4092u - 72u)) return -1;
    if (kshim_crc32(p, 4092) != get32(p + 4092)) return -1;
    return 1;
}

/* Proves a slot is ours for overwrite purposes even though it fails
 * record_valid: the magic says "this is a boot log", and both UUIDs match.
 * In that case we may recycle the malformed peer, because the
 * good peer still exists. */
static int bootlog_owned(const struct kshim_config_store *s, const uint8_t *p)
{
    return same(p, (const uint8_t *)KSHIM_BOOTLOG_MAGIC, KSHIM_BOOTLOG_MAGIC_LEN) &&
           same(p + 24, s->partition.uuid, 16) &&
           same(p + 40, s->installation_uuid, 16);
}

int kshim_config_log_boot(struct kshim_config_store *s, uint32_t target,
                          uint32_t phase, int32_t status)
{
    if (!store_valid(s) || !bootlog_target_ok(target) || !bootlog_phase_ok(phase))
        return KSHIM_STORAGE_ARG;
    if (!s->dev->write || !s->dev->flush) return KSHIM_STORAGE_READ_ONLY;
    if (s->descriptor_version != 2u && s->descriptor_version != 3u)
        return KSHIM_STORAGE_FORMAT;
    /* Only v3 designates mutable log slots. Refuse v2; do NOT upgrade. */
    if (s->descriptor_version != 3u) return KSHIM_STORAGE_FORMAT;
    if (s->dev->block_size != 512u && s->dev->block_size != 4096u)
        return KSHIM_STORAGE_RANGE;

    uint32_t blocks = KSHIM_CONFIG_SLOT_BYTES / s->dev->block_size; /* 8 or 1 */
    uint64_t lba0 = s->region_lba + KSHIM_BOOTLOG_SLOT0_OFF / s->dev->block_size;
    uint64_t lba1 = s->region_lba + KSHIM_BOOTLOG_SLOT1_OFF / s->dev->block_size;

    /* Reuse s->scratch as two 4096-byte read buffers. */
    uint8_t *a = s->scratch;
    uint8_t *b = s->scratch + KSHIM_CONFIG_SLOT_BYTES;
    int rc = kshim_block_read(s->dev, lba0, blocks, a);
    if (rc) return rc;
    rc = kshim_block_read(s->dev, lba1, blocks, b);
    if (rc) return rc;

    int va = bootlog_record_valid(s, a);
    int vb = bootlog_record_valid(s, b);

    int best, target_idx;
    if (va == 1 && vb == 1) {
        uint64_t sa = get64(a + 16), sb = get64(b + 16);
        if (sa == sb && !same(a, b, KSHIM_BOOTLOG_REC_BYTES))
            return KSHIM_STORAGE_CONFLICT;
        best = (sa >= sb) ? 0 : 1;
        target_idx = 1 - best;
    } else if (va == 1 || vb == 1) {
        best = (va == 1) ? 0 : 1;
        target_idx = 1 - best;
        /* Malformed peer is reusable only if it is a boot log we own. */
        const uint8_t *peer = (target_idx == 0) ? a : b;
        int peer_state = (target_idx == 0) ? va : vb;
        if (peer_state == -1 && !bootlog_owned(s, peer))
            return KSHIM_STORAGE_FORMAT;
    } else if (va == 0 && vb == 0) {
        best = -1;
        target_idx = 0; /* both erased -> first slot */
    } else {
        /* No valid record and at least one malformed one: usable only
         * when every non-erased slot is a boot log we own. */
        if ((va == -1 && !bootlog_owned(s, a)) ||
            (vb == -1 && !bootlog_owned(s, b)))
            return KSHIM_STORAGE_FORMAT;
        best = -1;
        target_idx = 0;
    }

    /* Boards 1-3 (the ARMADA_BOOT boards): a nominal normal target=1 phase=1 status=0 duplicate
     * that directly follows a validated diagnostic target=2 phase=4 status=0
     * record is skipped so the handoff retains that one diagnostic record.
     * No format or slot-layout change; compare the newest live record. */
    if ((s->board_id == 1 || s->board_id == 2 || s->board_id == 3) && best >= 0 &&
        target == 1 && phase == 1 && status == 0) {
        const uint8_t *live = (best == 0) ? a : b;
        if (get32(live + 60) == 2 && get32(live + 64) == 4 &&
            get32(live + 68) == 0)
            return KSHIM_STORAGE_OK;
    }

    uint64_t next;
    if (best < 0) {
        next = 1;
    } else {
        uint64_t live = get64((best == 0 ? a : b) + 16);
        if (live == UINT64_MAX) return KSHIM_STORAGE_OVERFLOW;
        next = live + 1;
    }

    /* Compose the record into buffer a (about to be read back into b). */
    uint8_t *rec = a;
    for (size_t i = 0; i < KSHIM_BOOTLOG_REC_BYTES; ++i) rec[i] = 0;
    copy(rec, (const uint8_t *)KSHIM_BOOTLOG_MAGIC, KSHIM_BOOTLOG_MAGIC_LEN);
    put32(rec + 8, KSHIM_BOOTLOG_VERSION);
    put32(rec + 12, KSHIM_BOOTLOG_REC_BYTES);
    put64(rec + 16, next);
    copy(rec + 24, s->partition.uuid, 16);
    copy(rec + 40, s->installation_uuid, 16);
    put32(rec + 56, s->board_id);
    put32(rec + 60, target);
    put32(rec + 64, phase);
    put32(rec + 68, (uint32_t)status); /* bit pattern preserved */
    /* rec[72..4091] already zero */
    put32(rec + 4092, kshim_crc32(rec, 4092));

    uint64_t lba = (target_idx == 0) ? lba0 : lba1;
    rc = kshim_block_write(s->dev, lba, blocks, rec);
    if (rc) return rc;
    rc = kshim_block_flush(s->dev);
    if (rc) return rc;

    /* Compare exactly the 4096 bytes we intended. */
    rc = kshim_block_read(s->dev, lba, blocks, b);
    if (rc) return rc;
    if (!same(rec, b, KSHIM_BOOTLOG_REC_BYTES)) return KSHIM_STORAGE_IO;
    return KSHIM_STORAGE_OK;
}
