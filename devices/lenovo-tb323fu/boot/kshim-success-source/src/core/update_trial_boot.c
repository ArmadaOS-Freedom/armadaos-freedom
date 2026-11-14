/* SPDX-License-Identifier: GPL-3.0-only */
#include "update_trial_boot.h"

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "storage_settings_native.h"
#include "update_binding.h"
#include "update_trial.h"

/*
 * Stable runtime compatibility marker for the new-system TB322FC/TB323FU
 * mirrored policy. Package tooling requires this exact literal in the runtime
 * bytes before accepting an online, offline or rollback target. The marker is
 * read below through a volatile lvalue from the used gate path, so the linked
 * binary retains it even with -ffunction-sections/-fdata-sections/
 * --gc-sections. It carries no boot decision and does not control Android.
 */
const char kshim_update_trial_boot_compat_marker[] =
    "ARMADA_LINUX_MIRROR_V1";

static bool uuid_all_zero(const uint8_t *uuid, size_t bytes)
{
    size_t i;

    for (i = 0; i < bytes; i++) {
        if (uuid[i] != 0u) {
            return false;
        }
    }
    return true;
}

static bool state_pending(uint32_t state)
{
    return state == KSHIM_UPDATE_TRIAL_PREPARED ||
           state == KSHIM_UPDATE_TRIAL_TRIAL;
}

int kshim_update_trial_boot(int actual_slot, bool internal_linux,
                            const char *cmdline, size_t bytes,
                            const void *transaction, size_t transaction_bytes)
{
    struct kshim_settings_session *session;
    struct kshim_update_trial record;
    unsigned selected = 0u;
    bool required;
    int rc;

    /* Retain the compatibility marker in every runtime that links this gate. */
    (void)*(const volatile char *)kshim_update_trial_boot_compat_marker;

    if (!internal_linux) {
        return 0;
    }
    if (transaction == NULL) {
        if (transaction_bytes != 0u) {
            return KSHIM_STORAGE_FORMAT;
        }
        required = false;
    } else {
        if (transaction_bytes != 16u || uuid_all_zero(transaction, 16u)) {
            return KSHIM_STORAGE_FORMAT;
        }
        required = true;
    }

    session = storage_settings_native_session();
    if (session == NULL || session->source != KSHIM_SETTINGS_INTERNAL ||
        session->medium[0].config.descriptor_version !=
            KSHIM_UPDATE_TRIAL_DESCRIPTOR_MIN_VERSION) {
        return required ? KSHIM_STORAGE_FORMAT : 0;
    }

    rc = storage_settings_native_trial_load(&record, &selected);
    if (rc == KSHIM_STORAGE_EMPTY) {
        return required ? KSHIM_STORAGE_CONFLICT : 0;
    }
    if (rc != 0) {
        return rc;
    }
    if (actual_slot != 0 && actual_slot != 1) {
        return KSHIM_STORAGE_CONFLICT;
    }

    if (record.policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL) {
        struct kshim_update_binding binding;
        struct kshim_update_trial_decision decision;
        unsigned written = 0u;

        if (!required ||
            memcmp(transaction, record.transaction_uuid, 16u) != 0)
            return KSHIM_STORAGE_CONFLICT;
        rc = kshim_update_binding_parse(cmdline, bytes, &binding);
        if (rc != 0) {
            return rc;
        }
        rc = kshim_update_trial_before_boot(&record, (uint32_t)actual_slot,
                                            1u, binding.os_name,
                                            binding.commit, binding.serial,
                                            &decision);
        if (rc != 0) {
            return rc;
        }
        if (decision.action == KSHIM_UPDATE_TRIAL_PERSIST) {
            return storage_settings_native_trial_save(record.sequence,
                                                      &decision.next,
                                                      &written);
        }
        if (decision.action == KSHIM_UPDATE_TRIAL_CONTINUE) {
            return 0;
        }
        return KSHIM_STORAGE_CONFLICT;
    }

    /*
     * Legacy single-slot records: never issue Android slot controls from the
     * Linux update hook.  Refuse an in-flight old transaction instead of
     * fabricating a rollback; terminal records are inert.
     */
    return state_pending(record.state) ? KSHIM_STORAGE_CONFLICT : 0;
}
