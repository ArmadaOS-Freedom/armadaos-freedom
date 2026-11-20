/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_STORAGE_CONFIG_H
#define KSHIM_STORAGE_CONFIG_H
#include "storage_block.h"
#define KSHIM_CONFIG_PARTITION_BYTES UINT64_C(2147483648)
#define KSHIM_CONFIG_REGION_BYTES 65536u
#define KSHIM_CONFIG_SLOT_BYTES 4096u

/* next_action carries a packed action (bits 0..7) plus flag bits (bit 8+).
 * Use the helpers below rather than comparing next_action to a bare value. */
enum kshim_action {
    KSHIM_ACTION_NONE = 0,
    KSHIM_ACTION_EDL = 1,
    KSHIM_ACTION_ANDROID = 2,
    KSHIM_ACTION_LINUX = 3,
    KSHIM_ACTION_LINUX_DIAG = 4,
    KSHIM_ACTION_MAX = KSHIM_ACTION_LINUX_DIAG,
};
#define KSHIM_ACTION_MASK 0xffu
#define KSHIM_FLAG_NO_BOOT_LOG (1u << 8)
#define KSHIM_FLAGS_KNOWN KSHIM_FLAG_NO_BOOT_LOG
#define KSHIM_FLAGS_MASK 0xffffff00u

static inline uint32_t kshim_next_action_pack(uint32_t action, uint32_t flags)
{ return (action & KSHIM_ACTION_MASK) | (flags & KSHIM_FLAGS_MASK); }
static inline uint32_t kshim_next_action_flags(uint32_t packed)
{ return packed & KSHIM_FLAGS_MASK; }
static inline uint32_t kshim_next_action_value(uint32_t packed)
{ return packed & KSHIM_ACTION_MASK; }

struct kshim_config_values {
    uint32_t device_choice; /* 0..2 */
    uint32_t default_target; /* 0..4; 4 remembers the last system */
    uint32_t language;       /* 0..1 */
    uint32_t last_target;    /* 0 unknown, 1 Android, 2 internal, 3 TF */
    uint32_t next_action;    /* packed: bits 0..7 action, bit 8 no-boot-log flag */
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
int kshim_config_log_boot(struct kshim_config_store *s, uint32_t target,
                          uint32_t phase, int32_t status);
#endif
