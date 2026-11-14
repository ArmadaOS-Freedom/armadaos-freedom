/* SPDX-License-Identifier: GPL-3.0-only */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "update_trial_boot.h"
#include "storage_settings_native.h"

#define OLD_ARGS "armada.deployment=default/1111111111111111111111111111111111111111111111111111111111111111.10"
#define NEW_ARGS "armada.deployment=default/2222222222222222222222222222222222222222222222222222222222222222.20"

static struct kshim_settings_session session;
static struct kshim_update_trial record;
static unsigned char tx[16];
static int load_rc, save_rc, select_rc, mark_rc;
static int loads, saves, selects, marks;
static uint64_t save_expected;

struct kshim_settings_session *storage_settings_native_session(void) { return &session; }
int storage_settings_native_trial_load(struct kshim_update_trial *r, unsigned *s)
{ loads++; *s = 0; if (!load_rc) *r = record; return load_rc; }
int storage_settings_native_trial_save(uint64_t expected,
    const struct kshim_update_trial *next, unsigned *written)
{ saves++; assert(expected == save_expected); *written = 0; if (!save_rc) { record = *next; *written = 1; } return save_rc; }

/* Android slot controls must never be reached from the update gate. */
int storage_settings_native_ab_select(unsigned target, unsigned current)
{ (void)target; (void)current; selects++; return select_rc; }
int storage_settings_native_ab_mark_success(unsigned expected_current)
{ (void)expected_current; marks++; return mark_rc; }

static void fixture(unsigned from, unsigned target)
{
    memset(&session, 0, sizeof(session));
    memset(&record, 0, sizeof(record));
    session.source = KSHIM_SETTINGS_INTERNAL;
    session.medium[0].config.descriptor_version = 3;
    record.sequence = 5;
    record.state = KSHIM_UPDATE_TRIAL_PREPARED;
    record.from_slot = from;
    record.target_slot = target;
    record.remaining = record.attempt_limit = 3;
    record.old_serial = 10;
    record.new_serial = 20;
    strcpy(record.os_name, "default");
    memset(record.partition_uuid, 1, 16);
    memset(record.installation_uuid, 2, 16);
    memset(record.transaction_uuid, 3, 16);
    memset(tx, 3, 16);
    memset(record.old_commit, 0x11, 32);
    memset(record.new_commit, 0x22, 32);
    memset(record.old_boot_sha256, 4, 32);
    memset(record.new_boot_sha256, 5, 32);
    memset(record.plan_sha256, 6, 32);
    load_rc = save_rc = select_rc = mark_rc = 0;
    loads = saves = selects = marks = 0;
}

static void dual_fixture(unsigned from, unsigned target)
{
    fixture(from, target);
    record.policy = KSHIM_UPDATE_POLICY_OFFLINE_DUAL;
    memset(record.other_boot_sha256, 7, 32);
    record.remaining = record.attempt_limit = 4;
}

static int gate(int slot, const char *args, const void *marker, size_t bytes)
{
    return kshim_update_trial_boot(slot, true, args,
                                   args ? strlen(args) + 1 : 0, marker, bytes);
}

static void dual_flow(unsigned from, unsigned target)
{
    uint32_t order[4];
    unsigned left = 4;
    unsigned i;

    dual_fixture(from, target);
    order[0] = from;
    order[1] = from;
    order[2] = target;
    order[3] = target;

    /* Android path is a pure bypass. */
    assert(kshim_update_trial_boot((int)target, false, "bad", 4, NULL, 1) == 0);
    assert(loads == 0 && saves == 0 && selects == 0 && marks == 0);

    /* Missing or foreign transaction marker: no journal or slot mutation. */
    assert(gate((int)target, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT);
    tx[0] ^= 1;
    assert(gate((int)from, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    tx[0] ^= 1;
    assert(saves == 0 && selects == 0 && marks == 0);

    /* The same transaction must match the mirrored (new) deployment on both
     * slots; old, foreign, and stale bindings are refused. */
    assert(gate((int)from, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)from, "armada.deployment=other/2222222222222222222222222222222222222222222222222222222222222222.20", tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, "armada.deployment=default/2222222222222222222222222222222222222222222222222222222222222222.21", tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(saves == 0 && selects == 0 && marks == 0);

    /* Either slot can be the first boot origin; both draw from the same
     * journal attempt budget. */
    for (i = 0; i < 4; ++i) {
        save_expected = record.sequence;
        assert(gate((int)order[i], NEW_ARGS, tx, 16) == 0);
        assert(saves == 1 && selects == 0 && marks == 0);
        assert(record.state == KSHIM_UPDATE_TRIAL_TRIAL);
        assert(record.remaining == left - 1);
        left--;
        saves = 0;
    }
    assert(record.remaining == 0);

    /* Exhausted retries fail on both slots without a slot or journal
     * change. */
    assert(gate((int)from, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(saves == 0 && selects == 0 && marks == 0);

    /* Confirmed records continue on both slots and never mark success. */
    record.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    assert(gate((int)from, NEW_ARGS, tx, 16) == 0);
    assert(gate((int)target, NEW_ARGS, tx, 16) == 0);
    assert(saves == 0 && selects == 0 && marks == 0);
    tx[0] ^= 1;
    assert(gate((int)from, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    tx[0] ^= 1;
    assert(gate((int)target, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(selects == 0 && marks == 0);

    /* Journal write failure leaves the record and slots untouched. */
    dual_fixture(from, target);
    save_rc = KSHIM_STORAGE_IO;
    save_expected = record.sequence;
    assert(gate((int)target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO);
    assert(saves == 1 && selects == 0 && marks == 0);
    assert(record.state == KSHIM_UPDATE_TRIAL_PREPARED);

    assert(selects == 0 && marks == 0);
}

static void legacy_flow(unsigned from, unsigned target)
{
    fixture(from, target);

    /* Pending single-slot transactions are refused, never resumed or rolled
     * back by the Linux hook. */
    assert(gate((int)from, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT);
    assert(saves == 0 && selects == 0 && marks == 0);
    assert(record.state == KSHIM_UPDATE_TRIAL_PREPARED);

    record.state = KSHIM_UPDATE_TRIAL_TRIAL;
    record.remaining = 2;
    assert(gate((int)from, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);

    /* Exhausted legacy retries still refuse instead of selecting a slot. */
    record.remaining = 0;
    assert(gate((int)target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate((int)from, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(saves == 0 && selects == 0 && marks == 0);

    /* Terminal legacy records are inert on both slots. */
    record.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    assert(gate((int)from, OLD_ARGS, tx, 16) == 0);
    assert(gate((int)target, NEW_ARGS, NULL, 0) == 0);
    assert(saves == 0 && selects == 0 && marks == 0);
    record.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    assert(gate((int)from, OLD_ARGS, NULL, 0) == 0);
    assert(gate((int)target, NEW_ARGS, tx, 16) == 0);
    assert(saves == 0 && selects == 0 && marks == 0);

    /* Android bypass leaves even a pending legacy record untouched. */
    record.state = KSHIM_UPDATE_TRIAL_TRIAL;
    record.remaining = 2;
    {
        int prev = loads;
        assert(kshim_update_trial_boot((int)target, false, "bad", 4, NULL, 1) == 0);
        assert(loads == prev && saves == 0 && selects == 0 && marks == 0);
    }
}

static void edge_cases(void)
{
    /* The runtime contract marker is exact and visible to packaging tools. */
    assert(strlen(kshim_update_trial_boot_compat_marker) ==
           strlen("ARMADA_LINUX_MIRROR_V1"));
    assert(strcmp(kshim_update_trial_boot_compat_marker,
                  "ARMADA_LINUX_MIRROR_V1") == 0);

    /* Android never reads storage. */
    fixture(0, 1);
    assert(kshim_update_trial_boot(1, false, "bad", 4, NULL, 1) == 0);
    assert(loads == 0 && saves == 0 && selects == 0 && marks == 0);

    /* Unsupported descriptors do not load a transaction. */
    session.medium[0].config.descriptor_version = 2;
    assert(gate(1, NEW_ARGS, NULL, 0) == 0);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);
    assert(loads == 0 && saves == 0 && selects == 0 && marks == 0);
    session.medium[0].config.descriptor_version = 1;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);

    /* Fresh, unmarked boots stay untouched. */
    fixture(0, 1);
    load_rc = KSHIM_STORAGE_EMPTY;
    assert(gate(1, NEW_ARGS, NULL, 0) == 0);
    assert(gate(0, OLD_ARGS, NULL, 0) == 0);
    assert(saves == 0 && selects == 0 && marks == 0);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);

    /* Marker shape validation. */
    fixture(0, 1);
    assert(gate(1, NEW_ARGS, NULL, 1) == KSHIM_STORAGE_FORMAT);
    memset(tx, 0, 16);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);
    assert(gate(1, NEW_ARGS, tx, 15) == KSHIM_STORAGE_FORMAT);

    /* Invalid actual slot after a successful load. */
    fixture(0, 1);
    assert(gate(2, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);

    /* Load failure surfaces unchanged. */
    fixture(0, 1);
    load_rc = KSHIM_STORAGE_IO;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO);
    assert(saves == 0 && selects == 0 && marks == 0);

    /* Non-internal sessions are bypasses, not transaction owners. */
    fixture(0, 1);
    session.source = KSHIM_SETTINGS_TF;
    assert(gate(1, NEW_ARGS, NULL, 0) == 0);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);
    assert(loads == 0 && saves == 0 && selects == 0 && marks == 0);
}

int main(void)
{
    dual_flow(0, 1);
    dual_flow(1, 0);
    legacy_flow(0, 1);
    legacy_flow(1, 0);
    edge_cases();
    puts("update_trial_boot_test: ok");
    return 0;
}
