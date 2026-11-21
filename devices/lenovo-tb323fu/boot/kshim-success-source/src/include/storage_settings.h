/* SPDX-License-Identifier: GPL-3.0-only */
#pragma once
#include <storage_config.h>
enum kshim_settings_source {
    KSHIM_SETTINGS_NONE=0, KSHIM_SETTINGS_INTERNAL=1, KSHIM_SETTINGS_TF=2
};
struct kshim_settings_medium {
    struct kshim_config_store config;
    uint8_t scratch[KSHIM_STORAGE_SCRATCH_BYTES];
    int status;
};
struct kshim_settings_session {
    struct kshim_settings_medium medium[2]; /* Internal, then TF. */
    struct kshim_config_values values;
    uint64_t sequence;
    int save_uncertain; /* A failed write/flush/readback may still have reached media. */
    enum kshim_settings_source source;
    /* Optional transaction ownership. Both callbacks must be supplied together.
     * A failed begin cleans its own partial claim. After successful begin,
     * end always runs, including write failure. No UI state is published until
     * end also succeeds; ctx and callbacks must outlive the session. */
    void *io_context;
    int (*io_begin)(void *);
    int (*io_end)(void *);
};
/* Read-only discovery. Valid internal settings take precedence over valid TF
 * settings. If neither exists, select a provisioned empty internal store,
 * otherwise an empty TF store. Missing/v2/unprovisioned media are never
 * formatted. Errors remain visible independently for each medium.
 * All blockdevs and this session must outlive every save. */
int kshim_settings_open(struct kshim_settings_session *,
                        const struct kshim_blockdev *internal,
                        const struct kshim_blockdev *tf,uint32_t actual_board,
                        const struct kshim_config_values *defaults);
/* Save only to the selected source. Never silently switch media on failure.
 * Session values/sequence change only after flush and exact readback succeed. */
int kshim_settings_save(struct kshim_settings_session *,
                        const struct kshim_config_values *);
/* Called for an actual boot target (1..3), not menu focus or power actions.
 * Same confirmed history is a no-op, even in fixed-default mode. */
int kshim_settings_note_boot(struct kshim_settings_session *,uint32_t target);
/* Consume a one-shot request through the normal verified transaction. Output
 * remains0 on failure; successful consumption never changes boot defaults. */
int kshim_settings_take_action(struct kshim_settings_session *,uint32_t *action,int diagnostic_target_valid);
