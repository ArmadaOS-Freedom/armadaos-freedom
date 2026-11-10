/* SPDX-License-Identifier: GPL-3.0-only */
#include "update_trial_boot.h"

#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "storage_settings_native.h"
#include "update_binding.h"
#include "update_trial.h"

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

static bool state_terminal(uint32_t state)
{
    return state == KSHIM_UPDATE_TRIAL_CONFIRMED ||
           state == KSHIM_UPDATE_TRIAL_ROLLED_BACK;
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
    struct kshim_update_binding binding;
    const uint8_t *expected_commit;
    uint32_t expected_serial;
    unsigned selected = 0u;
    bool required;
    bool from;
    int rc;

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
        if (decision.action == KSHIM_UPDATE_TRIAL_CONTINUE &&
            record.state == KSHIM_UPDATE_TRIAL_CONFIRMED) {
            return storage_settings_native_ab_mark_success(
                (unsigned)actual_slot);
        }
        return KSHIM_STORAGE_CONFLICT;
    }

    if (actual_slot == (int)record.target_slot && state_pending(record.state) &&
        record.remaining == 0u) {
        struct kshim_ab_status status;

        rc = storage_settings_native_ab_status(&status);
        if (rc != 0) {
            return rc;
        }
        if (status.current == record.from_slot) {
            return KSHIM_UPDATE_BOOT_RESTART_REQUIRED;
        }
        if (status.current != (unsigned)actual_slot) {
            return KSHIM_STORAGE_CONFLICT;
        }
        rc = storage_settings_native_ab_select(record.from_slot,
                                               (unsigned)actual_slot);
        if (rc != 0) {
            return rc;
        }
        return KSHIM_UPDATE_BOOT_RESTART_REQUIRED;
    }

    rc = kshim_update_binding_parse(cmdline, bytes, &binding);
    if (rc != 0) {
        return rc;
    }
    from = actual_slot == (int)record.from_slot;
    expected_commit = from ? record.old_commit : record.new_commit;
    expected_serial = from ? record.old_serial : record.new_serial;
    if (strcmp(binding.os_name, record.os_name) != 0 ||
        memcmp(binding.commit, expected_commit, 32u) != 0 ||
        binding.serial != expected_serial) {
        return KSHIM_STORAGE_CONFLICT;
    }

    if (actual_slot == (int)record.target_slot) {
        if (!required ||
            memcmp(transaction, record.transaction_uuid, 16u) != 0) {
            return KSHIM_STORAGE_CONFLICT;
        }
        if (record.state == KSHIM_UPDATE_TRIAL_ROLLED_BACK) {
            return KSHIM_STORAGE_CONFLICT;
        }
        if (record.state == KSHIM_UPDATE_TRIAL_CONFIRMED) {
            return storage_settings_native_ab_mark_success(
                (unsigned)actual_slot);
        }
    } else if (from && state_terminal(record.state)) {
        if (record.state == KSHIM_UPDATE_TRIAL_ROLLED_BACK) {
            return storage_settings_native_ab_mark_success(
                (unsigned)actual_slot);
        }
        return 0;
    }

    if (state_pending(record.state)) {
        struct kshim_update_trial_decision decision;
        unsigned written = 0u;

        rc = kshim_update_trial_before_boot(&record, (uint32_t)actual_slot, 1u,
                                            binding.os_name, binding.commit,
                                            binding.serial, &decision);
        if (rc != 0) {
            return rc;
        }
        if (decision.action == KSHIM_UPDATE_TRIAL_PERSIST) {
            rc = storage_settings_native_trial_save(record.sequence,
                                                    &decision.next, &written);
            if (rc != 0) {
                return rc;
            }
            if (from &&
                decision.next.state == KSHIM_UPDATE_TRIAL_ROLLED_BACK) {
                return storage_settings_native_ab_mark_success(
                    (unsigned)actual_slot);
            }
            return 0;
        }
        if (decision.action == KSHIM_UPDATE_TRIAL_CONTINUE) {
            return 0;
        }
        return KSHIM_STORAGE_FORMAT;
    }

    return KSHIM_STORAGE_FORMAT;
}
