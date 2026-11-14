/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_CONFIG_H
#define KSHIM_STORAGE_CONFIG_H
#include "storage_block.h"
#define KSHIM_CONFIG_PARTITION_BYTES UINT64_C(2147483648)
#define KSHIM_CONFIG_REGION_BYTES 65536u
#define KSHIM_CONFIG_SLOT_BYTES 4096u
struct kshim_config_values {
    uint32_t device_choice; /* 0..2 */
    uint32_t default_target; /* 0..4; 4 remembers the last system */
    uint32_t language;       /* 0..1 */
    uint32_t last_target;    /* 0 unknown, 1 Android, 2 internal, 3 TF */
    uint32_t next_action;    /* 0 none, 1 one-shot EDL; consumed before reset */
};
struct kshim_config_store {
    const struct kshim_blockdev *dev;
    struct kshim_partition partition;
    uint64_t region_lba;
    uint8_t installation_uuid[16];
    uint8_t *scratch;
    uint32_t board_id;
    uint32_t descriptor_version;
    uint32_t ready;
};
/* scratch remains owned by caller and must live until the last load/save.
 * A store and its scratch must not be used concurrently. board_id is the
 * detected hardware identity, never the user-selectable menu device. */
int kshim_config_open(struct kshim_config_store *, const struct kshim_blockdev *,
                      const struct kshim_partition *, uint32_t board_id,
                      void *scratch, size_t scratch_bytes);
/* EMPTY is an unprovisioned settings pair, not a successful read of defaults. */
int kshim_config_load(struct kshim_config_store *, struct kshim_config_values *,
                      uint64_t *sequence);
/* Writes exactly one 4096-byte slot, then flushes and compares readback.
 * Failure does not promise that no bytes changed; the preceding valid slot
 * is never overwritten. sequence is updated only on verified success. */
int kshim_config_save(struct kshim_config_store *, const struct kshim_config_values *,
                      uint64_t *sequence);
#endif
