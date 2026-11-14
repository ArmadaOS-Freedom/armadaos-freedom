/* SPDX-License-Identifier: GPL-3.0-only */
#ifndef KSHIM_UPDATE_TRIAL_STORE_H
#define KSHIM_UPDATE_TRIAL_STORE_H

#include "storage_config.h"
#include "update_trial.h"

/* The caller owns the exclusive block-device session and must revalidate the
 * GPT and descriptor after every new claim. Stores and their scratch buffers
 * must not be used concurrently or aliased by another store/scratch user. */
int kshim_update_trial_load(struct kshim_config_store *s,
                            struct kshim_update_trial *record,
                            unsigned *selected);

/* Compare-and-swap append. On verified success only, *written is the slot
 * written. Failure may change target-slot bytes but never overwrites the
 * preceding valid slot. Runtime policy and health decisions are separate. */
int kshim_update_trial_save(struct kshim_config_store *s,
                            uint64_t expected_sequence,
                            const struct kshim_update_trial *next,
                            unsigned *written);
#endif
