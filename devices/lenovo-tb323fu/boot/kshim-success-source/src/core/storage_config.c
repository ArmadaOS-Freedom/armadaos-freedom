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
static int values_valid(const struct kshim_config_values *v)
{
    return v && v->device_choice <= 2 && v->default_target <= 4 && v->language <= 1 && v->last_target <= 3 && v->next_action <= 1;
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
    if (!same(p, (const uint8_t *)"KSCFGR1", 8) || (version<1 || version>3) ||
        get32(p + 12) != 4096 || !get64(p + 16) ||
        !same(p + 24, s->partition.uuid, 16) || !same(p + 40, s->installation_uuid, 16) ||
        get32(p + 56) > 2 || get32(p + 60) > (version==1 ? 3u : 4u) ||
        get32(p + 64) > 1 || (version>=2 && get32(p+68)>3) || (version==3 && get32(p+72)>1) ||
        !zero(p + (version==1 ? 68 : version==2 ? 72 : 76), version==1 ? 4024 : version==2 ? 4020 : 4016) ||
        kshim_crc32(p, 4092) != get32(p + 4092)) return -1;
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
    v->next_action = get32(p+8)==3 ? get32(p+72) : 0;
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
    uint8_t *record = s->scratch;
    for (size_t i = 0; i < KSHIM_CONFIG_SLOT_BYTES; ++i) record[i] = 0;
    copy(record, (const uint8_t *)"KSCFGR1", 8);
    put32(record + 8, 3); put32(record + 12, KSHIM_CONFIG_SLOT_BYTES);
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
