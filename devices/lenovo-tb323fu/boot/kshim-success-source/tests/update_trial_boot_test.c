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
static struct kshim_ab_status ab;
static unsigned char tx[16];
static int load_rc, save_rc, ab_rc, select_rc, mark_rc, loads, saves, selects, marks;
static unsigned mark_last_slot;
static uint64_t save_expected;

struct kshim_settings_session *storage_settings_native_session(void) { return &session; }
int storage_settings_native_trial_load(struct kshim_update_trial *r, unsigned *s)
{ loads++; *s = 0; if (!load_rc) *r = record; return load_rc; }
int storage_settings_native_trial_save(uint64_t expected,
    const struct kshim_update_trial *next, unsigned *written)
{ saves++; assert(expected == save_expected); *written = 0; if (!save_rc) { record = *next; *written = 1; } return save_rc; }
int storage_settings_native_ab_status(struct kshim_ab_status *s)
{ if (!ab_rc) *s = ab; return ab_rc; }
int storage_settings_native_ab_select(unsigned target, unsigned current)
{ selects++; assert(target == record.from_slot); assert(current == ab.current); if (!select_rc) ab.current = target; return select_rc; }
int storage_settings_native_ab_mark_success(unsigned expected_current)
{ marks++; mark_last_slot = expected_current; assert(expected_current == ab.current); return mark_rc; }

static void fixture(unsigned from, unsigned target)
{
    memset(&session, 0, sizeof(session)); memset(&record, 0, sizeof(record));
    session.source = KSHIM_SETTINGS_INTERNAL;
    session.medium[0].config.descriptor_version = 3;
    record.sequence = 5; record.state = KSHIM_UPDATE_TRIAL_PREPARED;
    record.from_slot = from; record.target_slot = target;
    record.remaining = record.attempt_limit = 3;
    record.old_serial = 10; record.new_serial = 20; strcpy(record.os_name, "default");
    memset(record.partition_uuid, 1, 16); memset(record.installation_uuid, 2, 16);
    memset(record.transaction_uuid, 3, 16); memset(tx, 3, 16);
    memset(record.old_commit, 0x11, 32); memset(record.new_commit, 0x22, 32);
    memset(record.old_boot_sha256, 4, 32); memset(record.new_boot_sha256, 5, 32);
    memset(record.plan_sha256, 6, 32); memset(&ab, 0, sizeof(ab)); ab.current = target;
    load_rc = save_rc = ab_rc = select_rc = mark_rc = loads = saves = selects = marks = 0;
    mark_last_slot = 99u;
}
static int gate(int slot, const char *args, const void *marker, size_t bytes)
{ return kshim_update_trial_boot(slot, true, args, args ? strlen(args) + 1 : 0, marker, bytes); }
static void flow(unsigned from, unsigned target)
{
    const char *args = NEW_ARGS;
    fixture(from, target);
    for (unsigned left = 2;; left--) {
        save_expected = record.sequence;
        assert(gate(target, args, tx, 16) == 0); assert(saves == 1 && marks == 0);
        assert(record.state == KSHIM_UPDATE_TRIAL_TRIAL && record.remaining == left);
        saves = 0; if (!left) break;
    }
    assert(gate(target, args, tx, 16) == KSHIM_UPDATE_BOOT_RESTART_REQUIRED);
    assert(selects == 1 && saves == 0 && marks == 0);
    save_expected = record.sequence;
    assert(gate(from, OLD_ARGS, tx, 16) == 0);
    assert(record.state == KSHIM_UPDATE_TRIAL_ROLLED_BACK && record.sequence == 9 && saves == 1);
    assert(selects == 1 && marks == 1 && mark_last_slot == from); saves = 0;
    assert(gate(from, OLD_ARGS, NULL, 0) == 0 && saves == 0 && marks == 2 && mark_last_slot == from);
    assert(gate(target, args, tx, 16) == KSHIM_STORAGE_CONFLICT && saves == 0 && marks == 2);
    fixture(from, target); record.state = KSHIM_UPDATE_TRIAL_CONFIRMED; record.remaining = 2;
    assert(gate(target, args, tx, 16) == 0 && saves == 0 && marks == 1 && mark_last_slot == target);
}
static void offline_flow(unsigned from, unsigned target)
{
    const char *args = NEW_ARGS;
    fixture(from, target);
    record.policy = KSHIM_UPDATE_POLICY_OFFLINE_DUAL;
    memset(record.other_boot_sha256, 7, 32);

    /* from pending internal Linux => CONFLICT, never save/select/mark */
    assert(gate(from, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && saves == 0 && selects == 0 && marks == 0);
    /* missing/wrong transaction => CONFLICT */
    assert(gate(target, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT && saves == 0);
    tx[0] ^= 1; assert(gate(target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && saves == 0); tx[0] ^= 1;
    /* old binding on target => CONFLICT */
    assert(gate(target, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && saves == 0);

    /* three target trials: save each time, no select/mark */
    for (unsigned left = 2;; left--) {
        save_expected = record.sequence;
        assert(gate(target, args, tx, 16) == 0 && saves == 1 && marks == 0 && selects == 0);
        assert(record.state == KSHIM_UPDATE_TRIAL_TRIAL && record.remaining == left);
        saves = 0; if (!left) break;
    }
    /* exhaustion => CONFLICT, never AB-select or fabricated rollback */
    assert(gate(target, args, tx, 16) == KSHIM_STORAGE_CONFLICT && selects == 0 && saves == 0 && marks == 0);

    /* save failure leaves record untouched */
    fixture(from, target);
    record.policy = KSHIM_UPDATE_POLICY_OFFLINE_DUAL;
    memset(record.other_boot_sha256, 7, 32);
    save_rc = KSHIM_STORAGE_IO; save_expected = 5;
    assert(gate(target, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO && saves == 1 && record.state == KSHIM_UPDATE_TRIAL_PREPARED && selects == 0 && marks == 0);

    /* confirmed: both slots require exact new binding + tx and mark actual slot */
    save_rc = 0; saves = 0; record.state = KSHIM_UPDATE_TRIAL_CONFIRMED; record.remaining = 2;
    assert(gate(target, NEW_ARGS, tx, 16) == 0 && saves == 0 && marks == 1 && mark_last_slot == target);
    ab.current = from;
    assert(gate(from, NEW_ARGS, tx, 16) == 0 && marks == 2 && mark_last_slot == from);
    tx[0] ^= 1; assert(gate(from, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && marks == 2); tx[0] ^= 1;
    assert(gate(from, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && marks == 2);

    /* non-Linux top-level return unchanged */
    int prev_loads = loads;
    assert(kshim_update_trial_boot((int)target, false, "bad", 4, NULL, 1) == 0 && loads == prev_loads && saves == 0 && selects == 0 && marks == 2);
}

int main(void)
{
    flow(0, 1); flow(1, 0);
    offline_flow(0, 1); offline_flow(1, 0);
    fixture(0, 1); assert(kshim_update_trial_boot(1, false, "bad", 4, NULL, 1) == 0 && loads == 0 && marks == 0);
    session.medium[0].config.descriptor_version = 2;
    assert(gate(1, NEW_ARGS, NULL, 0) == 0 && loads == 0 && marks == 0);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT && loads == 0);
    session.medium[0].config.descriptor_version = 1;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);
    fixture(0, 1); load_rc = KSHIM_STORAGE_EMPTY;
    assert(gate(1, NEW_ARGS, NULL, 0) == 0);
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    fixture(0, 1); assert(gate(2, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate(1, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && saves == 0);
    assert(gate(1, "armada.deployment=other/2222222222222222222222222222222222222222222222222222222222222222.20", tx, 16) == KSHIM_STORAGE_CONFLICT);
    assert(gate(1, "armada.deployment=default/2222222222222222222222222222222222222222222222222222222222222222.21", tx, 16) == KSHIM_STORAGE_CONFLICT);
    tx[0] ^= 1; assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT); tx[0] ^= 1;
    load_rc = KSHIM_STORAGE_IO; assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO);
    fixture(0, 1); save_rc = KSHIM_STORAGE_IO; save_expected = 5;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO && record.state == KSHIM_UPDATE_TRIAL_PREPARED);
    fixture(0, 1); record.remaining = 0; record.state = KSHIM_UPDATE_TRIAL_TRIAL; ab_rc = KSHIM_STORAGE_IO;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO && selects == 0);
    ab_rc = 0; select_rc = KSHIM_STORAGE_IO;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO && selects == 1);
    fixture(0, 1); record.remaining = 0; record.state = KSHIM_UPDATE_TRIAL_TRIAL; ab.current = 0;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_UPDATE_BOOT_RESTART_REQUIRED && selects == 0);
    fixture(0, 1); assert(gate(1, NEW_ARGS, NULL, 1) == KSHIM_STORAGE_FORMAT);
    memset(tx, 0, 16); assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_FORMAT);
    assert(gate(1, NEW_ARGS, tx, 15) == KSHIM_STORAGE_FORMAT);
    fixture(0, 1); ab.current = 0;
    assert(gate(0, OLD_ARGS, NULL, 0) == 0 && saves == 0 && marks == 0);
    fixture(0, 1); record.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    assert(gate(1, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT && marks == 0);
    tx[0] ^= 1; assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && marks == 0); tx[0] ^= 1;
    assert(gate(1, OLD_ARGS, tx, 16) == KSHIM_STORAGE_CONFLICT && marks == 0);
    assert(gate(1, "", NULL, 0) == KSHIM_STORAGE_NOT_FOUND && marks == 0);
    mark_rc = KSHIM_STORAGE_IO;
    assert(gate(1, NEW_ARGS, tx, 16) == KSHIM_STORAGE_IO && marks == 1 && mark_last_slot == 1);
    mark_rc = 0; ab.current = 0;
    assert(gate(0, OLD_ARGS, NULL, 0) == 0 && marks == 1);
    assert(gate(0, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT && marks == 1);
    fixture(0, 1); record.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK; ab.current = 0;
    assert(gate(0, OLD_ARGS, NULL, 0) == 0 && marks == 1 && mark_last_slot == 0);
    assert(gate(0, NEW_ARGS, NULL, 0) == KSHIM_STORAGE_CONFLICT && marks == 1);
    assert(gate(0, "", NULL, 0) == KSHIM_STORAGE_NOT_FOUND && marks == 1);
    fixture(0, 1); record.remaining = 2; record.state = KSHIM_UPDATE_TRIAL_TRIAL; ab.current = 0;
    save_expected = 5; save_rc = KSHIM_STORAGE_IO;
    assert(gate(0, OLD_ARGS, NULL, 0) == KSHIM_STORAGE_IO && saves == 1 && marks == 0);
    save_rc = 0; mark_rc = KSHIM_STORAGE_IO;
    assert(gate(0, OLD_ARGS, NULL, 0) == KSHIM_STORAGE_IO && saves == 2 && marks == 1 && mark_last_slot == 0);
    assert(record.state == KSHIM_UPDATE_TRIAL_ROLLED_BACK && record.sequence == 6);
    mark_rc = 0;
    assert(gate(0, OLD_ARGS, NULL, 0) == 0 && saves == 2 && marks == 2 && mark_last_slot == 0);
    puts("update_trial_boot_test: ok"); return 0;
}
