/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_UPDATE_TRIAL_H
#define KSHIM_UPDATE_TRIAL_H

#include <stddef.h>
#include <stdint.h>

#include "storage_block.h"

/*
 * Linux update trial journal wire layout (4096 bytes, little-endian integers):
 *    0..7    magic "ARMUPD1\0"
 *    8..11   version (1 for ONLINE_SINGLE, 2 for OFFLINE_DUAL)
 *   12..15   record_bytes (4096)
 *   16..23   sequence (nonzero)
 *   24..39   partition_uuid (RFC 4122 raw bytes, nonzero)
 *   40..55   installation_uuid (RFC 4122 raw bytes, nonzero)
 *   56..71   transaction_uuid (RFC 4122 raw bytes, nonzero)
 *   72..75   state (enum kshim_update_trial_state)
 *   76..79   from_slot (0 or 1)
 *   80..83   target_slot (0 or 1, != from_slot)
 *   84..87   remaining_attempts
 *   88..91   attempt_limit (1..7)
 *   92..95   old_serial
 *   96..99   new_serial
 *  100..164  os_name (1..64 ASCII chars, NUL-terminated, zero-padded)
 *  165..167  reserved (zero)
 *  168..199  old_commit (nonzero)
 *  200..231  new_commit (nonzero)
 *  232..263  old_boot_sha256 (nonzero)
 *  264..295  new_boot_sha256 (nonzero)
 *  296..327  plan_sha256 (nonzero)
 *  328..331  v1: reserved (zero); v2: policy (1 = OFFLINE_DUAL)
 *  332..363  v1: reserved (zero); v2: other_boot_sha256 (nonzero)
 *  364..4091 reserved (zero)
 * 4092..4095 crc32 of bytes 0..4091 (integrity check only, not authentication)
 *
 * Logical-empty sentinel.  Before the first real record is written into copy
 * A, the writer durably stores an all-zero copy B with exactly one bit set:
 * byte EMPTY_MARKER_OFFSET == EMPTY_MARKER_BYTE.  The marker can never be
 * decoded as a record (its stored CRC stays zero while the computed CRC over
 * the marked body is not) and is only recognised in copy B, only after a real
 * record failed to select, and only when copy A is blank or CRC-damaged.  A
 * complete CRC-valid image, a foreign/incompatible record, a conflicting pair
 * or any other non-marker pair stays an error.  The marker exists only in the
 * window between the initial anchor write and the second journal write, so it
 * requires a reader that knows it; older readers fail closed on it (they never
 * boot a Linux candidate from a marker pair).  This also limits producers:
 * only the current writer anchors an initial journal, and a journal created by
 * an older writer keeps the old, unanchored behaviour.  Once a real record is
 * durable it always wins and the next write replaces the marker.
 *
 * Journal offsets below are relative to the 64 KiB mutable tail, not partition
 * start. Callers must verify descriptor version/capabilities and manage I/O
 * durability; this codec never reads, writes, or activates storage.
 */

#define KSHIM_UPDATE_TRIAL_VERSION 1u
#define KSHIM_UPDATE_TRIAL_OFFLINE_VERSION 2u
#define KSHIM_UPDATE_TRIAL_RECORD_BYTES 4096u
#define KSHIM_UPDATE_TRIAL_DESCRIPTOR_MIN_VERSION 3u
#define KSHIM_UPDATE_TRIAL_TAIL_OFFSET_A 28672u
#define KSHIM_UPDATE_TRIAL_TAIL_OFFSET_B 32768u
#define KSHIM_UPDATE_TRIAL_ATTEMPT_MAX 7u
#define KSHIM_UPDATE_TRIAL_OS_NAME_MAX 64u
#define KSHIM_UPDATE_TRIAL_EMPTY_MARKER_COPY 1u
#define KSHIM_UPDATE_TRIAL_EMPTY_MARKER_OFFSET \
    (KSHIM_UPDATE_TRIAL_RECORD_BYTES - 5u)
#define KSHIM_UPDATE_TRIAL_EMPTY_MARKER_BYTE 0x01u

enum kshim_update_policy {
    KSHIM_UPDATE_POLICY_ONLINE_SINGLE = 0,
    KSHIM_UPDATE_POLICY_OFFLINE_DUAL = 1
};

enum kshim_update_trial_state {
    KSHIM_UPDATE_TRIAL_PREPARED = 1,
    KSHIM_UPDATE_TRIAL_TRIAL = 2,
    KSHIM_UPDATE_TRIAL_CONFIRMED = 3,
    KSHIM_UPDATE_TRIAL_ROLLED_BACK = 4
};

enum kshim_update_trial_action {
    KSHIM_UPDATE_TRIAL_CONTINUE = 0,
    KSHIM_UPDATE_TRIAL_PERSIST = 1,
    KSHIM_UPDATE_TRIAL_SWITCH_OLD = 2
};

struct kshim_update_trial {
    uint64_t sequence;
    uint8_t partition_uuid[16];
    uint8_t installation_uuid[16];
    uint8_t transaction_uuid[16];
    uint32_t state;
    uint32_t from_slot;
    uint32_t target_slot;
    uint32_t remaining;
    uint32_t attempt_limit;
    uint32_t old_serial;
    uint32_t new_serial;
    char os_name[KSHIM_UPDATE_TRIAL_OS_NAME_MAX + 1u];
    uint8_t old_commit[32];
    uint8_t new_commit[32];
    uint8_t old_boot_sha256[32];
    uint8_t new_boot_sha256[32];
    uint8_t plan_sha256[32];
    uint32_t policy;
    uint8_t other_boot_sha256[32];
};

struct kshim_update_trial_decision {
    uint32_t action;
    struct kshim_update_trial next;
};

int kshim_update_trial_encode(const struct kshim_update_trial *record,
                              void *buffer, size_t size);

/* Build the exact logical-empty sentinel image (all zero except one fixed
 * byte) or test a record-sized buffer for that exact image.  The test is a
 * full-image comparison, never a "nonzero and short" heuristic. */
int kshim_update_trial_empty_marker(void *buffer, size_t size);
int kshim_update_trial_is_empty_marker(const void *buffer, size_t size);

int kshim_update_trial_decode(const void *buffer, size_t size,
                              const uint8_t partition_uuid[16],
                              const uint8_t installation_uuid[16],
                              struct kshim_update_trial *record);

int kshim_update_trial_select(const void *a, const void *b, size_t size,
                              const uint8_t partition_uuid[16],
                              const uint8_t installation_uuid[16],
                              struct kshim_update_trial *record,
                              unsigned *selected);

/* PERSIST must be flushed and read back before trial Linux handoff. */
int kshim_update_trial_before_boot(const struct kshim_update_trial *record,
                                   uint32_t actual_slot,
                                   uint32_t internal_linux,
                                   const char *os_name,
                                   const uint8_t commit[32], uint32_t serial,
                                   struct kshim_update_trial_decision *decision);

/*
 * Confirmation requires the boot SHA-256 observed on actual_slot.  An
 * OFFLINE_DUAL record confirms on target_slot with new_boot_sha256 and on
 * from_slot with other_boot_sha256 (the mirrored copy of the new deployment).
 * A legacy single-slot record confirms only on target_slot with
 * new_boot_sha256.  A digest that belongs to the other slot is refused without
 * touching the journal.  Callers must establish real system health before
 * requesting confirmation.
 */
int kshim_update_trial_confirm(const struct kshim_update_trial *record,
                               uint32_t actual_slot,
                               const uint8_t boot_sha256[32],
                               const char *os_name,
                               const uint8_t commit[32], uint32_t serial,
                               struct kshim_update_trial *next);

int kshim_update_trial_rollback(const struct kshim_update_trial *record,
                                uint32_t actual_slot,
                                struct kshim_update_trial *next);

#endif
