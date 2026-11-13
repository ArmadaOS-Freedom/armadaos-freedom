/* SPDX-License-Identifier: GPL-3.0-only */
#include "update_trial_store.h"
#include <string.h>

static int trial_store_valid(const struct kshim_config_store *s)
{
    if (!s || !s->dev || !s->scratch || !s->dev->read || s->ready != 1u)
        return KSHIM_STORAGE_ARG;
    if (s->dev->block_size != 512u && s->dev->block_size != 4096u)
        return KSHIM_STORAGE_ARG;
    if (s->descriptor_version != KSHIM_UPDATE_TRIAL_DESCRIPTOR_MIN_VERSION)
        return KSHIM_STORAGE_FORMAT;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_load(struct kshim_config_store *s,
                            struct kshim_update_trial *record,
                            unsigned *selected)
{
    uint32_t blocks;
    int rc;
    if (!record || !selected)
        return KSHIM_STORAGE_ARG;
    rc = trial_store_valid(s);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    blocks = KSHIM_UPDATE_TRIAL_RECORD_BYTES / s->dev->block_size;
    rc = kshim_block_read(s->dev,
        s->region_lba + KSHIM_UPDATE_TRIAL_TAIL_OFFSET_A / s->dev->block_size,
        blocks, s->scratch);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    rc = kshim_block_read(s->dev,
        s->region_lba + KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B / s->dev->block_size,
        blocks, s->scratch + KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    return kshim_update_trial_select(s->scratch,
        s->scratch + KSHIM_UPDATE_TRIAL_RECORD_BYTES,
        KSHIM_UPDATE_TRIAL_RECORD_BYTES, s->partition.uuid,
        s->installation_uuid, record, selected);
}

static int trial_store_all_zero(const uint8_t *p, size_t n)
{
    size_t i;

    for (i = 0; i < n; ++i) {
        if (p[i] != 0u)
            return 0;
    }
    return 1;
}

/*
 * Anchor an initial journal write.  Copy B may hold either the exact marker
 * (an earlier attempt already wrote it) or zeros (the marker write did not
 * land).  Anything else is a foreign/unknown pair and must not be
 * overwritten.  An exact marker is not rewritten, but it is still flushed and
 * read back: reaching the cache does not prove that an earlier flush
 * succeeded.  Only then may copy A be touched, so an interrupted first record
 * write leaves a recoverable empty journal instead of two unusable copies.
 */
static int trial_store_ensure_empty_marker(struct kshim_config_store *s)
{
    uint8_t *marker = s->scratch;
    uint8_t *readback = s->scratch + KSHIM_UPDATE_TRIAL_RECORD_BYTES;
    uint32_t blocks =
        KSHIM_UPDATE_TRIAL_RECORD_BYTES / s->dev->block_size;
    uint64_t lba = s->region_lba +
        KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B / s->dev->block_size;
    int rc;

    rc = kshim_block_read(s->dev, lba, blocks, readback);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (!kshim_update_trial_is_empty_marker(readback,
                                            KSHIM_UPDATE_TRIAL_RECORD_BYTES)) {
        if (!trial_store_all_zero(readback, KSHIM_UPDATE_TRIAL_RECORD_BYTES))
            return KSHIM_STORAGE_FORMAT;
        rc = kshim_update_trial_empty_marker(marker,
                                             KSHIM_UPDATE_TRIAL_RECORD_BYTES);
        if (rc != KSHIM_STORAGE_OK)
            return rc;
        rc = kshim_block_write(s->dev, lba, blocks, marker);
        if (rc != KSHIM_STORAGE_OK)
            return rc;
    }
    rc = kshim_block_flush(s->dev);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    rc = kshim_block_read(s->dev, lba, blocks, readback);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (!kshim_update_trial_is_empty_marker(readback,
                                            KSHIM_UPDATE_TRIAL_RECORD_BYTES))
        return KSHIM_STORAGE_IO;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_save(struct kshim_config_store *s,
                            uint64_t expected_sequence,
                            const struct kshim_update_trial *next,
                            unsigned *written)
{
    struct kshim_update_trial current;
    unsigned selected, target;
    uint32_t blocks;
    uint64_t lba;
    int rc;
    if (!next || !written)
        return KSHIM_STORAGE_ARG;
    rc = trial_store_valid(s);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (!s->dev->write || !s->dev->flush)
        return KSHIM_STORAGE_READ_ONLY;
    if (expected_sequence == UINT64_MAX)
        return KSHIM_STORAGE_OVERFLOW;
    if (next->sequence != expected_sequence + 1u ||
        memcmp(next->partition_uuid, s->partition.uuid, 16) != 0 ||
        memcmp(next->installation_uuid, s->installation_uuid, 16) != 0)
        return KSHIM_STORAGE_CONFLICT;
    rc = kshim_update_trial_encode(next, s->scratch,
                                  KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    rc = kshim_update_trial_load(s, &current, &selected);
    if (rc == KSHIM_STORAGE_EMPTY) {
        if (expected_sequence != 0u)
            return KSHIM_STORAGE_CONFLICT;
        target = 0;
        rc = trial_store_ensure_empty_marker(s);
        if (rc != KSHIM_STORAGE_OK)
            return rc;
    } else {
        if (rc != KSHIM_STORAGE_OK)
            return rc;
        if (current.sequence != expected_sequence)
            return KSHIM_STORAGE_CONFLICT;
        target = selected ^ 1u;
    }
    rc = kshim_update_trial_encode(next, s->scratch,
                                  KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    blocks = KSHIM_UPDATE_TRIAL_RECORD_BYTES / s->dev->block_size;
    lba = s->region_lba + (target == 0u ? KSHIM_UPDATE_TRIAL_TAIL_OFFSET_A :
          KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B) / s->dev->block_size;
    rc = kshim_block_write(s->dev, lba, blocks, s->scratch);
    if (rc == KSHIM_STORAGE_OK)
        rc = kshim_block_flush(s->dev);
    if (rc == KSHIM_STORAGE_OK)
        rc = kshim_block_read(s->dev, lba, blocks,
            s->scratch + KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (memcmp(s->scratch, s->scratch + KSHIM_UPDATE_TRIAL_RECORD_BYTES,
               KSHIM_UPDATE_TRIAL_RECORD_BYTES) != 0)
        return KSHIM_STORAGE_IO;
    *written = target;
    return KSHIM_STORAGE_OK;
}
