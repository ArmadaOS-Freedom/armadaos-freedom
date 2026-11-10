/* SPDX-License-Identifier: GPL-3.0-only */
#include "update_trial.h"

#include <string.h>

static const uint8_t kshim_trial_magic[8] = {
    'A', 'R', 'M', 'U', 'P', 'D', '1', 0
};

static uint32_t read_le32(const uint8_t *p)
{
    return (uint32_t)p[0] |
           ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint64_t read_le64(const uint8_t *p)
{
    return (uint64_t)read_le32(p) | ((uint64_t)read_le32(p + 4) << 32);
}

static void write_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xffu);
    p[1] = (uint8_t)((v >> 8) & 0xffu);
    p[2] = (uint8_t)((v >> 16) & 0xffu);
    p[3] = (uint8_t)((v >> 24) & 0xffu);
}

static void write_le64(uint8_t *p, uint64_t v)
{
    write_le32(p, (uint32_t)(v & 0xffffffffu));
    write_le32(p + 4, (uint32_t)(v >> 32));
}

static int bytes_all_zero(const uint8_t *p, size_t n)
{
    size_t i;
    uint8_t acc = 0;

    for (i = 0; i < n; ++i)
        acc |= p[i];
    return acc == 0;
}

static int is_ascii_alnum(uint8_t c)
{
    return (c >= '0' && c <= '9') ||
           (c >= 'A' && c <= 'Z') ||
           (c >= 'a' && c <= 'z');
}

static int is_os_name_rest(uint8_t c)
{
    return is_ascii_alnum(c) || c == '_' || c == '.' || c == '-';
}

static int validate_os_name_field(const uint8_t *field, size_t *out_len)
{
    size_t len = 0;
    size_t i;

    if (!is_ascii_alnum(field[0]))
        return 0;
    while (len < KSHIM_UPDATE_TRIAL_OS_NAME_MAX + 1u && field[len] != 0) {
        if (len > 0 && !is_os_name_rest(field[len]))
            return 0;
        ++len;
    }
    if (len == 0 || len > KSHIM_UPDATE_TRIAL_OS_NAME_MAX)
        return 0;
    for (i = len; i < KSHIM_UPDATE_TRIAL_OS_NAME_MAX + 1u; ++i) {
        if (field[i] != 0)
            return 0;
    }
    if (out_len)
        *out_len = len;
    return 1;
}

static int validate_logical_record(const struct kshim_update_trial *r,
                                   size_t *os_len)
{
    size_t len = 0;

    if (r->sequence == 0)
        return 0;
    if (bytes_all_zero(r->partition_uuid, sizeof(r->partition_uuid)) ||
        bytes_all_zero(r->installation_uuid, sizeof(r->installation_uuid)) ||
        bytes_all_zero(r->transaction_uuid, sizeof(r->transaction_uuid)))
        return 0;

    if (r->from_slot > 1u || r->target_slot > 1u ||
        r->from_slot == r->target_slot)
        return 0;

    if (r->attempt_limit < 1u ||
        r->attempt_limit > KSHIM_UPDATE_TRIAL_ATTEMPT_MAX ||
        r->remaining > r->attempt_limit)
        return 0;

    switch (r->state) {
    case KSHIM_UPDATE_TRIAL_PREPARED:
        if (r->remaining != r->attempt_limit)
            return 0;
        break;
    case KSHIM_UPDATE_TRIAL_TRIAL:
    case KSHIM_UPDATE_TRIAL_CONFIRMED:
        if (r->remaining >= r->attempt_limit)
            return 0;
        break;
    case KSHIM_UPDATE_TRIAL_ROLLED_BACK:
        break;
    default:
        return 0;
    }

    if (!is_ascii_alnum((uint8_t)r->os_name[0]))
        return 0;
    while (len < sizeof(r->os_name) && r->os_name[len] != '\0') {
        if (len > 0 && !is_os_name_rest((uint8_t)r->os_name[len]))
            return 0;
        ++len;
    }
    if (len == 0 || len > KSHIM_UPDATE_TRIAL_OS_NAME_MAX)
        return 0;

    if (bytes_all_zero(r->old_commit, sizeof(r->old_commit)) ||
        bytes_all_zero(r->new_commit, sizeof(r->new_commit)) ||
        bytes_all_zero(r->old_boot_sha256, sizeof(r->old_boot_sha256)) ||
        bytes_all_zero(r->new_boot_sha256, sizeof(r->new_boot_sha256)) ||
        bytes_all_zero(r->plan_sha256, sizeof(r->plan_sha256)))
        return 0;

    switch (r->policy) {
    case KSHIM_UPDATE_POLICY_ONLINE_SINGLE:
        if (!bytes_all_zero(r->other_boot_sha256,
                            sizeof(r->other_boot_sha256)))
            return 0;
        break;
    case KSHIM_UPDATE_POLICY_OFFLINE_DUAL:
        if (r->state == KSHIM_UPDATE_TRIAL_ROLLED_BACK ||
            bytes_all_zero(r->other_boot_sha256,
                           sizeof(r->other_boot_sha256)))
            return 0;
        break;
    default:
        return 0;
    }

    if (os_len)
        *os_len = len;
    return 1;
}

int kshim_update_trial_encode(const struct kshim_update_trial *record,
                              void *buffer, size_t size)
{
    uint8_t *out = (uint8_t *)buffer;
    size_t os_len = 0;
    uint32_t version;
    uint32_t crc;

    if (!record || !buffer || size != KSHIM_UPDATE_TRIAL_RECORD_BYTES)
        return KSHIM_STORAGE_ARG;

    if (!validate_logical_record(record, &os_len))
        return KSHIM_STORAGE_FORMAT;

    version = (record->policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL)
                  ? KSHIM_UPDATE_TRIAL_OFFLINE_VERSION
                  : KSHIM_UPDATE_TRIAL_VERSION;

    memset(out, 0, KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    memcpy(out + 0, kshim_trial_magic, sizeof(kshim_trial_magic));
    write_le32(out + 8, version);
    write_le32(out + 12, KSHIM_UPDATE_TRIAL_RECORD_BYTES);
    write_le64(out + 16, record->sequence);
    memcpy(out + 24, record->partition_uuid, 16);
    memcpy(out + 40, record->installation_uuid, 16);
    memcpy(out + 56, record->transaction_uuid, 16);
    write_le32(out + 72, record->state);
    write_le32(out + 76, record->from_slot);
    write_le32(out + 80, record->target_slot);
    write_le32(out + 84, record->remaining);
    write_le32(out + 88, record->attempt_limit);
    write_le32(out + 92, record->old_serial);
    write_le32(out + 96, record->new_serial);
    memcpy(out + 100, record->os_name, os_len);
    memcpy(out + 168, record->old_commit, 32);
    memcpy(out + 200, record->new_commit, 32);
    memcpy(out + 232, record->old_boot_sha256, 32);
    memcpy(out + 264, record->new_boot_sha256, 32);
    memcpy(out + 296, record->plan_sha256, 32);
    if (record->policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL) {
        write_le32(out + 328, record->policy);
        memcpy(out + 332, record->other_boot_sha256, 32);
    }

    crc = kshim_crc32(out, 4092u);
    write_le32(out + 4092, crc);
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_decode(const void *buffer, size_t size,
                              const uint8_t partition_uuid[16],
                              const uint8_t installation_uuid[16],
                              struct kshim_update_trial *record)
{
    const uint8_t *in = (const uint8_t *)buffer;
    struct kshim_update_trial tmp;
    size_t os_len = 0;
    uint32_t version;
    uint32_t expected_crc;

    if (!buffer || !partition_uuid || !installation_uuid || !record ||
        size != KSHIM_UPDATE_TRIAL_RECORD_BYTES)
        return KSHIM_STORAGE_ARG;

    if (bytes_all_zero(in, KSHIM_UPDATE_TRIAL_RECORD_BYTES))
        return KSHIM_STORAGE_EMPTY;

    expected_crc = kshim_crc32(in, 4092u);
    if (read_le32(in + 4092) != expected_crc)
        return KSHIM_STORAGE_CRC;

    version = read_le32(in + 8);
    if (memcmp(in + 0, kshim_trial_magic, sizeof(kshim_trial_magic)) != 0 ||
        (version != KSHIM_UPDATE_TRIAL_VERSION &&
         version != KSHIM_UPDATE_TRIAL_OFFLINE_VERSION) ||
        read_le32(in + 12) != KSHIM_UPDATE_TRIAL_RECORD_BYTES)
        return KSHIM_STORAGE_FORMAT;

    if (!bytes_all_zero(in + 165, 3u) ||
        !validate_os_name_field(in + 100, &os_len))
        return KSHIM_STORAGE_FORMAT;

    memset(&tmp, 0, sizeof(tmp));
    if (version == KSHIM_UPDATE_TRIAL_VERSION) {
        if (!bytes_all_zero(in + 328, 4092u - 328u))
            return KSHIM_STORAGE_FORMAT;
        tmp.policy = KSHIM_UPDATE_POLICY_ONLINE_SINGLE;
    } else {
        if (!bytes_all_zero(in + 364, 4092u - 364u))
            return KSHIM_STORAGE_FORMAT;
        tmp.policy = read_le32(in + 328);
        if (tmp.policy != KSHIM_UPDATE_POLICY_OFFLINE_DUAL)
            return KSHIM_STORAGE_FORMAT;
        memcpy(tmp.other_boot_sha256, in + 332, 32);
    }
    tmp.sequence = read_le64(in + 16);
    memcpy(tmp.partition_uuid, in + 24, 16);
    memcpy(tmp.installation_uuid, in + 40, 16);
    memcpy(tmp.transaction_uuid, in + 56, 16);
    tmp.state = read_le32(in + 72);
    tmp.from_slot = read_le32(in + 76);
    tmp.target_slot = read_le32(in + 80);
    tmp.remaining = read_le32(in + 84);
    tmp.attempt_limit = read_le32(in + 88);
    tmp.old_serial = read_le32(in + 92);
    tmp.new_serial = read_le32(in + 96);
    memcpy(tmp.os_name, in + 100, os_len);
    memcpy(tmp.old_commit, in + 168, 32);
    memcpy(tmp.new_commit, in + 200, 32);
    memcpy(tmp.old_boot_sha256, in + 232, 32);
    memcpy(tmp.new_boot_sha256, in + 264, 32);
    memcpy(tmp.plan_sha256, in + 296, 32);

    if (!validate_logical_record(&tmp, NULL))
        return KSHIM_STORAGE_FORMAT;

    if (memcmp(tmp.partition_uuid, partition_uuid, 16) != 0 ||
        memcmp(tmp.installation_uuid, installation_uuid, 16) != 0)
        return KSHIM_STORAGE_CONFLICT;

    *record = tmp;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_select(const void *a, const void *b, size_t size,
                              const uint8_t partition_uuid[16],
                              const uint8_t installation_uuid[16],
                              struct kshim_update_trial *record,
                              unsigned *selected)
{
    struct kshim_update_trial rec_a;
    struct kshim_update_trial rec_b;
    int rc_a;
    int rc_b;

    if (!a || !b || !partition_uuid || !installation_uuid || !record ||
        !selected || size != KSHIM_UPDATE_TRIAL_RECORD_BYTES)
        return KSHIM_STORAGE_ARG;

    rc_a = kshim_update_trial_decode(a, size, partition_uuid,
                                     installation_uuid, &rec_a);
    rc_b = kshim_update_trial_decode(b, size, partition_uuid,
                                     installation_uuid, &rec_b);

    if (rc_a == KSHIM_STORAGE_OK && rc_b == KSHIM_STORAGE_OK) {
        if (rec_a.sequence > rec_b.sequence) {
            *record = rec_a;
            *selected = 0u;
            return KSHIM_STORAGE_OK;
        }
        if (rec_b.sequence > rec_a.sequence) {
            *record = rec_b;
            *selected = 1u;
            return KSHIM_STORAGE_OK;
        }
        if (memcmp(a, b, KSHIM_UPDATE_TRIAL_RECORD_BYTES) == 0) {
            *record = rec_a;
            *selected = 0u;
            return KSHIM_STORAGE_OK;
        }
        return KSHIM_STORAGE_CONFLICT;
    }

    if (rc_a == KSHIM_STORAGE_OK) {
        *record = rec_a;
        *selected = 0u;
        return KSHIM_STORAGE_OK;
    }

    if (rc_b == KSHIM_STORAGE_OK) {
        *record = rec_b;
        *selected = 1u;
        return KSHIM_STORAGE_OK;
    }

    if (rc_a == KSHIM_STORAGE_EMPTY && rc_b == KSHIM_STORAGE_EMPTY)
        return KSHIM_STORAGE_EMPTY;

    return KSHIM_STORAGE_FORMAT;
}

static int new_deployment_matches(const struct kshim_update_trial *record,
                                  const char *os_name,
                                  const uint8_t commit[32], uint32_t serial)
{
    size_t i;

    if (!os_name || !commit || serial != record->new_serial ||
        memcmp(commit, record->new_commit, sizeof(record->new_commit)) != 0)
        return 0;

    for (i = 0; i < sizeof(record->os_name); ++i) {
        if (os_name[i] != record->os_name[i])
            return 0;
        if (record->os_name[i] == '\0')
            return 1;
    }
    return 0;
}

static int start_mutation(const struct kshim_update_trial *record,
                          struct kshim_update_trial *next)
{
    if (record->sequence == UINT64_MAX)
        return KSHIM_STORAGE_OVERFLOW;
    *next = *record;
    ++next->sequence;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_before_boot(const struct kshim_update_trial *record,
                                   uint32_t actual_slot,
                                   uint32_t internal_linux,
                                   const char *os_name,
                                   const uint8_t commit[32], uint32_t serial,
                                   struct kshim_update_trial_decision *decision)
{
    struct kshim_update_trial next;
    int rc;

    if (!record || !decision || actual_slot > 1u || internal_linux > 1u)
        return KSHIM_STORAGE_ARG;
    if (!validate_logical_record(record, NULL))
        return KSHIM_STORAGE_FORMAT;

    if (record->policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL) {
        if (!internal_linux) {
            decision->action = KSHIM_UPDATE_TRIAL_CONTINUE;
            decision->next = *record;
            return KSHIM_STORAGE_OK;
        }
        if (!new_deployment_matches(record, os_name, commit, serial))
            return KSHIM_STORAGE_CONFLICT;
        if (record->state == KSHIM_UPDATE_TRIAL_CONFIRMED) {
            if (actual_slot != record->from_slot &&
                actual_slot != record->target_slot)
                return KSHIM_STORAGE_CONFLICT;
            decision->action = KSHIM_UPDATE_TRIAL_CONTINUE;
            decision->next = *record;
            return KSHIM_STORAGE_OK;
        }
        if (actual_slot != record->target_slot || record->remaining == 0u)
            return KSHIM_STORAGE_CONFLICT;
        rc = start_mutation(record, &next);
        if (rc != KSHIM_STORAGE_OK)
            return rc;
        next.state = KSHIM_UPDATE_TRIAL_TRIAL;
        --next.remaining;
        decision->action = KSHIM_UPDATE_TRIAL_PERSIST;
        decision->next = next;
        return KSHIM_STORAGE_OK;
    }

    if (record->state == KSHIM_UPDATE_TRIAL_CONFIRMED ||
        record->state == KSHIM_UPDATE_TRIAL_ROLLED_BACK) {
        decision->action = KSHIM_UPDATE_TRIAL_CONTINUE;
        decision->next = *record;
        return KSHIM_STORAGE_OK;
    }

    if (actual_slot == record->from_slot) {
        if (record->state == KSHIM_UPDATE_TRIAL_PREPARED) {
            decision->action = KSHIM_UPDATE_TRIAL_CONTINUE;
            decision->next = *record;
            return KSHIM_STORAGE_OK;
        }
        rc = start_mutation(record, &next);
        if (rc != KSHIM_STORAGE_OK)
            return rc;
        next.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
        decision->action = KSHIM_UPDATE_TRIAL_PERSIST;
        decision->next = next;
        return KSHIM_STORAGE_OK;
    }

    if (!internal_linux) {
        decision->action = KSHIM_UPDATE_TRIAL_CONTINUE;
        decision->next = *record;
        return KSHIM_STORAGE_OK;
    }

    if (record->remaining == 0u) {
        decision->action = KSHIM_UPDATE_TRIAL_SWITCH_OLD;
        decision->next = *record;
        return KSHIM_STORAGE_OK;
    }

    if (!new_deployment_matches(record, os_name, commit, serial))
        return KSHIM_STORAGE_CONFLICT;

    rc = start_mutation(record, &next);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    next.state = KSHIM_UPDATE_TRIAL_TRIAL;
    --next.remaining;
    decision->action = KSHIM_UPDATE_TRIAL_PERSIST;
    decision->next = next;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_confirm(const struct kshim_update_trial *record,
                               uint32_t actual_slot, const char *os_name,
                               const uint8_t commit[32], uint32_t serial,
                               struct kshim_update_trial *next)
{
    struct kshim_update_trial tmp;
    int rc;

    if (!record || !os_name || !commit || !next || actual_slot > 1u)
        return KSHIM_STORAGE_ARG;
    if (!validate_logical_record(record, NULL))
        return KSHIM_STORAGE_FORMAT;
    if (record->state != KSHIM_UPDATE_TRIAL_TRIAL ||
        actual_slot != record->target_slot ||
        !new_deployment_matches(record, os_name, commit, serial))
        return KSHIM_STORAGE_CONFLICT;

    rc = start_mutation(record, &tmp);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    tmp.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    *next = tmp;
    return KSHIM_STORAGE_OK;
}

int kshim_update_trial_rollback(const struct kshim_update_trial *record,
                                uint32_t actual_slot,
                                struct kshim_update_trial *next)
{
    struct kshim_update_trial tmp;
    int rc;

    if (!record || !next || actual_slot > 1u)
        return KSHIM_STORAGE_ARG;
    if (!validate_logical_record(record, NULL))
        return KSHIM_STORAGE_FORMAT;
    if (record->policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL)
        return KSHIM_STORAGE_CONFLICT;
    if ((record->state != KSHIM_UPDATE_TRIAL_PREPARED &&
         record->state != KSHIM_UPDATE_TRIAL_TRIAL) ||
        actual_slot != record->from_slot)
        return KSHIM_STORAGE_CONFLICT;

    rc = start_mutation(record, &tmp);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    tmp.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    *next = tmp;
    return KSHIM_STORAGE_OK;
}
