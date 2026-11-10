/* SPDX-License-Identifier: GPL-3.0-only */
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "update_trial.h"

static void init_fixture(struct kshim_update_trial *r, uint32_t from, uint32_t target)
{
    memset(r, 0, sizeof(*r));
    r->sequence = 10u;
    memset(r->partition_uuid, 0x11, sizeof(r->partition_uuid));
    memset(r->installation_uuid, 0x22, sizeof(r->installation_uuid));
    memset(r->transaction_uuid, 0x33, sizeof(r->transaction_uuid));
    r->state = KSHIM_UPDATE_TRIAL_PREPARED;
    r->from_slot = from;
    r->target_slot = target;
    r->remaining = 3u;
    r->attempt_limit = 3u;
    r->old_serial = 0u;
    r->new_serial = 1u;
    memcpy(r->os_name, "default", 8u);
    memset(r->old_commit, 0x44, sizeof(r->old_commit));
    memset(r->new_commit, 0x55, sizeof(r->new_commit));
    memset(r->old_boot_sha256, 0x66, sizeof(r->old_boot_sha256));
    memset(r->new_boot_sha256, 0x77, sizeof(r->new_boot_sha256));
    memset(r->plan_sha256, 0x88, sizeof(r->plan_sha256));
}

static void assert_wire_eq(const struct kshim_update_trial *a,
                           const struct kshim_update_trial *b)
{
    uint8_t wa[KSHIM_UPDATE_TRIAL_RECORD_BYTES];
    uint8_t wb[KSHIM_UPDATE_TRIAL_RECORD_BYTES];

    assert(kshim_update_trial_encode(a, wa, sizeof(wa)) == KSHIM_STORAGE_OK);
    assert(kshim_update_trial_encode(b, wb, sizeof(wb)) == KSHIM_STORAGE_OK);
    assert(memcmp(wa, wb, sizeof(wa)) == 0);
}

static void test_direction(uint32_t from, uint32_t target)
{
    struct kshim_update_trial rec, exp, out, snap_rec;
    struct kshim_update_trial_decision dec, snap_dec;
    uint8_t bad_commit[32];
    int i;

    memset(bad_commit, 0x99, sizeof(bad_commit));
    init_fixture(&rec, from, target);
    exp = rec;

    /* PREPARED: from_slot => CONTINUE unchanged; confirm => CONFLICT */
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    memset(&out, 0xA5, sizeof(out));
    memcpy(&snap_rec, &out, sizeof(out));
    assert(kshim_update_trial_confirm(&rec, target, "default",
                                      rec.new_commit, 1u, &out) == KSHIM_STORAGE_CONFLICT);
    assert(memcmp(&out, &snap_rec, sizeof(out)) == 0);

    /* Explicit rollback from PREPARED on from_slot (aliased out == in) */
    out = rec;
    assert(kshim_update_trial_rollback(&out, from, &out) == KSHIM_STORAGE_OK);
    exp.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    exp.sequence = 11u;
    assert_wire_eq(&out, &exp);
    exp = rec;

    /* Android on target_slot (internal_linux=0) => CONTINUE unchanged */
    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL,
                                          NULL, 0u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    /* Three internal Linux target calls => PERSIST, seq+1, TRIAL, remaining 2..0 */
    for (i = 0; i < 3; ++i) {
        assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                              rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
        assert(dec.action == KSHIM_UPDATE_TRIAL_PERSIST);
        exp.state = KSHIM_UPDATE_TRIAL_TRIAL;
        exp.sequence += 1u;
        exp.remaining -= 1u;
        assert(exp.remaining == (uint32_t)(2 - i));
        assert_wire_eq(&dec.next, &exp);
        rec = dec.next;

        if (exp.remaining == 1u) {
            struct kshim_update_trial rb_exp = exp;
            assert(kshim_update_trial_before_boot(&rec, from, 0u, NULL,
                                                  NULL, 0u, &dec) == KSHIM_STORAGE_OK);
            assert(dec.action == KSHIM_UPDATE_TRIAL_PERSIST);
            rb_exp.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
            rb_exp.sequence += 1u;
            assert_wire_eq(&dec.next, &rb_exp);
        }
    }

    /* Exhausted (remaining 0): internal => SWITCH_OLD unchanged; Android => CONTINUE */
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_SWITCH_OLD);
    assert_wire_eq(&dec.next, &exp);

    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL,
                                          NULL, 0u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    /* Exhausted TRIAL (remaining 0) CAN confirm on target with exact binding (aliased) */
    memset(&out, 0x5A, sizeof(out));
    memcpy(&snap_rec, &out, sizeof(out));
    assert(kshim_update_trial_rollback(&rec, target, &out) == KSHIM_STORAGE_CONFLICT);
    assert(memcmp(&out, &snap_rec, sizeof(out)) == 0);

    out = rec;
    assert(kshim_update_trial_confirm(&out, target, "default",
                                      rec.new_commit, 1u, &out) == KSHIM_STORAGE_OK);
    exp.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    exp.sequence += 1u;
    assert_wire_eq(&out, &exp);

    /* Terminal states: before_boot => CONTINUE unchanged; rollback/confirm => CONFLICT */
    assert(kshim_update_trial_before_boot(&out, target, 1u, "default",
                                          out.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_rollback(&out, from, &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);

    /* Wrong/missing bindings & invalid args/states/overflow */
    init_fixture(&rec, from, target);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 1u;
    memset(&dec, 0x3C, sizeof(dec));
    memcpy(&snap_dec, &dec, sizeof(dec));
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "wrong", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", bad_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", rec.new_commit, 9u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, NULL, rec.new_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", NULL, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, 2u, 1u, "default", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_before_boot(&rec, target, 2u, "default", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_ARG);
    assert(memcmp(&dec, &snap_dec, sizeof(dec)) == 0);

    memset(&out, 0x7E, sizeof(out));
    memcpy(&snap_rec, &out, sizeof(out));
    assert(kshim_update_trial_confirm(&rec, target, "wrong", rec.new_commit, 1u, &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, target, "default", bad_commit, 1u, &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, target, "default", rec.new_commit, 9u, &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, from, "default", rec.new_commit, 1u, &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, target, NULL, rec.new_commit, 1u, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_confirm(&rec, target, "default", NULL, 1u, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_confirm(&rec, 2u, "default", rec.new_commit, 1u, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_rollback(&rec, 2u, &out) == KSHIM_STORAGE_ARG);

    rec.state = 99u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_confirm(&rec, target, "default", rec.new_commit, 1u, &out) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_rollback(&rec, from, &out) == KSHIM_STORAGE_FORMAT);

    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.sequence = UINT64_MAX;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OVERFLOW);
    assert(kshim_update_trial_before_boot(&rec, from, 0u, NULL, NULL, 0u, &dec) == KSHIM_STORAGE_OVERFLOW);
    assert(kshim_update_trial_confirm(&rec, target, "default", rec.new_commit, 1u, &out) == KSHIM_STORAGE_OVERFLOW);
    assert(kshim_update_trial_rollback(&rec, from, &out) == KSHIM_STORAGE_OVERFLOW);
    assert(memcmp(&dec, &snap_dec, sizeof(dec)) == 0);
    assert(memcmp(&out, &snap_rec, sizeof(out)) == 0);

    rec.remaining = 0u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default", rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_SWITCH_OLD);
    assert_wire_eq(&dec.next, &rec);

    rec.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    assert(kshim_update_trial_before_boot(&rec, from, 1u, NULL, NULL, 0u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &rec);
}

static void test_offline(uint32_t from, uint32_t target)
{
    struct kshim_update_trial rec, exp, out;
    struct kshim_update_trial_decision dec;
    uint8_t bad_commit[32];
    int i;

    memset(bad_commit, 0x99, sizeof(bad_commit));
    init_fixture(&rec, from, target);
    rec.policy = KSHIM_UPDATE_POLICY_OFFLINE_DUAL;
    memset(rec.other_boot_sha256, 0xAA, sizeof(rec.other_boot_sha256));
    exp = rec;

    /* from slot pending internal Linux => CONFLICT, never SWITCH_OLD */
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);

    /* Android (internal_linux=0) => CONTINUE unchanged */
    assert(kshim_update_trial_before_boot(&rec, from, 0u, NULL,
                                          NULL, 0u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    /* explicit rollback rejected for offline (aliased out == in) */
    out = rec;
    assert(kshim_update_trial_rollback(&out, from, &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);

    /* confirm before any trial => CONFLICT */
    assert(kshim_update_trial_confirm(&rec, target, "default",
                                      rec.new_commit, 1u, &out) == KSHIM_STORAGE_CONFLICT);

    /* three target trials: PERSIST, seq+1, TRIAL, remaining 2..0 */
    for (i = 0; i < 3; ++i) {
        assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                              rec.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
        assert(dec.action == KSHIM_UPDATE_TRIAL_PERSIST);
        exp.state = KSHIM_UPDATE_TRIAL_TRIAL;
        exp.sequence += 1u;
        exp.remaining -= 1u;
        assert_wire_eq(&dec.next, &exp);
        rec = dec.next;
    }

    /* exhausted internal => CONFLICT, never SWITCH_OLD; Android => CONTINUE */
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL,
                                          NULL, 0u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);

    /* wrong binding on target => CONFLICT; overflow unchanged */
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "wrong", bad_commit, 9u, &dec) == KSHIM_STORAGE_CONFLICT);
    out = rec;
    out.state = KSHIM_UPDATE_TRIAL_TRIAL;
    out.remaining = 1u;
    out.sequence = UINT64_MAX;
    assert(kshim_update_trial_before_boot(&out, target, 1u, "default",
                                          out.new_commit, 1u, &dec) == KSHIM_STORAGE_OVERFLOW);

    /* confirm exhausted TRIAL on target, then both slots need exact new binding */
    out = rec;
    assert(kshim_update_trial_confirm(&out, target, "default",
                                      rec.new_commit, 1u, &out) == KSHIM_STORAGE_OK);
    exp.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    exp.sequence += 1u;
    assert_wire_eq(&out, &exp);
    assert(kshim_update_trial_before_boot(&out, target, 1u, "default",
                                          out.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&out, from, 1u, "default",
                                          out.new_commit, 1u, &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&out, from, 1u, "default",
                                          bad_commit, 1u, &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_rollback(&out, from, &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);
}

int main(void)
{
    test_direction(0u, 1u);
    test_direction(1u, 0u);
    test_offline(0u, 1u);
    test_offline(1u, 0u);
    return 0;
}
