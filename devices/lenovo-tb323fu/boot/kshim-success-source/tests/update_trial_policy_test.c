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

static void init_dual_fixture(struct kshim_update_trial *r, uint32_t from,
                              uint32_t target)
{
    init_fixture(r, from, target);
    r->policy = KSHIM_UPDATE_POLICY_OFFLINE_DUAL;
    memset(r->other_boot_sha256, 0xAA, sizeof(r->other_boot_sha256));
    r->remaining = 4u;
    r->attempt_limit = 4u;
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

static void test_legacy(uint32_t from, uint32_t target)
{
    struct kshim_update_trial rec, exp, out;
    struct kshim_update_trial_decision dec, snap_dec;
    uint8_t wrong_sha[32];

    memset(wrong_sha, 0xa6, sizeof(wrong_sha));
    init_fixture(&rec, from, target);
    exp = rec;

    /* The Linux hook never starts, completes, or rolls back a legacy
     * single-slot transaction. */
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);

    /* Android boots bypass every decision, even with invalid bindings. */
    assert(kshim_update_trial_before_boot(&rec, from, 0u, "bad", NULL, 9u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    /* Explicit rollback remains an operator action for legacy records. */
    out = rec;
    assert(kshim_update_trial_rollback(&out, from, &out) == KSHIM_STORAGE_OK);
    exp.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    exp.sequence = 11u;
    assert_wire_eq(&out, &exp);
    exp = rec;
    out = rec;
    assert(kshim_update_trial_rollback(&out, target,
                                       &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);

    /* TRIAL, including exhausted attempts, is refused without a journal
     * change or a fabricated slot rollback. */
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 2u;
    memset(&dec, 0x5A, sizeof(dec));
    memcpy(&snap_dec, &dec, sizeof(snap_dec));
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    rec.remaining = 0u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(memcmp(&dec, &snap_dec, sizeof(dec)) == 0);

    /* Terminal legacy records are inert on both slots. */
    rec.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    rec.remaining = 0u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &rec);
    assert(kshim_update_trial_before_boot(&rec, from, 1u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &rec);
    rec.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "wrong", NULL, 9u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &rec);

    /* Confirm still only advances a live TRIAL on its target slot, and only
     * for the new deployment's own boot digest. */
    init_fixture(&rec, from, target);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 2u;
    out = rec;
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_OK);
    exp = rec;
    exp.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    exp.sequence = 11u;
    assert_wire_eq(&out, &exp);
    exp = rec;
    assert(kshim_update_trial_confirm(&rec, from, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_CONFLICT);
    out = rec;
    assert(kshim_update_trial_confirm(&rec, target, rec.old_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);
    assert(kshim_update_trial_confirm(&rec, target, wrong_sha, "default",
                                      rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);
    assert(kshim_update_trial_confirm(&rec, target, NULL, "default",
                                      rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_ARG);
    assert_wire_eq(&out, &exp);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "wrong", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 9u,
                                      &out) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", NULL, 1u,
                                      &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_confirm(&rec, 2u, rec.new_boot_sha256, "default",
                                      rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_rollback(&rec, 2u, &out) == KSHIM_STORAGE_ARG);

    /* Structural validation and overflow. */
    rec.state = 99u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_rollback(&rec, from,
                                       &out) == KSHIM_STORAGE_FORMAT);

    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 2u;
    rec.sequence = UINT64_MAX;
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_OVERFLOW);
    assert(kshim_update_trial_rollback(&rec, from,
                                       &out) == KSHIM_STORAGE_OVERFLOW);

    assert(kshim_update_trial_before_boot(&rec, 2u, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_before_boot(&rec, target, 2u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_ARG);
}

static void test_dual(uint32_t from, uint32_t target)
{
    struct kshim_update_trial rec, exp, out;
    struct kshim_update_trial_decision dec, snap_dec;
    uint8_t bad_commit[32];
    uint32_t slots[4];
    unsigned i;

    memset(bad_commit, 0x99, sizeof(bad_commit));
    init_dual_fixture(&rec, from, target);
    exp = rec;

    /* Android owns slot selection; pending dual records are untouched. */
    assert(kshim_update_trial_before_boot(&rec, from, 0u, "bad", NULL, 9u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);

    /* The same transaction must name the mirrored (new) deployment on both
     * physical slots; an old, foreign, or stale binding is refused. */
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "wrong",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.old_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 9u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          bad_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, NULL,
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          NULL, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);

    /* Neither explicit rollback nor premature confirm is possible. */
    out = rec;
    assert(kshim_update_trial_rollback(&out, from,
                                       &out) == KSHIM_STORAGE_CONFLICT);
    assert_wire_eq(&out, &exp);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_CONFLICT);

    /* Both first-boot origins draw from the same journal attempt budget;
     * consecutive and alternating slots behave identically. */
    slots[0] = from;
    slots[1] = from;
    slots[2] = target;
    slots[3] = target;
    for (i = 0; i < 4u; ++i) {
        assert(kshim_update_trial_before_boot(&rec, slots[i], 1u, "default",
                                              rec.new_commit, 1u,
                                              &dec) == KSHIM_STORAGE_OK);
        assert(dec.action == KSHIM_UPDATE_TRIAL_PERSIST);
        exp.state = KSHIM_UPDATE_TRIAL_TRIAL;
        exp.sequence += 1u;
        exp.remaining -= 1u;
        assert(exp.remaining == 3u - i);
        assert_wire_eq(&dec.next, &exp);
        rec = dec.next;
    }

    /* Exhausted attempts refuse handoff on either slot without any action. */
    memset(&dec, 0x5A, sizeof(dec));
    memcpy(&snap_dec, &dec, sizeof(snap_dec));
    assert(kshim_update_trial_before_boot(&rec, from, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(memcmp(&dec, &snap_dec, sizeof(dec)) == 0);
    assert(kshim_update_trial_before_boot(&rec, target, 0u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &rec);

    /* A final attempt that cannot advance its sequence fails cleanly. */
    out = rec;
    out.remaining = 1u;
    out.sequence = UINT64_MAX;
    assert(kshim_update_trial_before_boot(&out, from, 1u, "default",
                                          out.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_OVERFLOW);

    /* Confirmed: both slots continue with the same transaction deployment
     * and never perform an Android success mark. */
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_OK);
    exp = out;
    assert(exp.state == KSHIM_UPDATE_TRIAL_CONFIRMED);
    assert(kshim_update_trial_before_boot(&out, from, 1u, "default",
                                          out.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&out, target, 1u, "default",
                                          out.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);
    assert_wire_eq(&dec.next, &exp);
    assert(kshim_update_trial_before_boot(&out, from, 1u, "default",
                                          bad_commit, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&out, target, 1u, "default",
                                          NULL, 1u,
                                          &dec) == KSHIM_STORAGE_CONFLICT);
    assert(kshim_update_trial_before_boot(&out, target, 0u, NULL, NULL, 0u,
                                          &dec) == KSHIM_STORAGE_OK);
    assert(dec.action == KSHIM_UPDATE_TRIAL_CONTINUE);

    /* Invalid inputs still fail structurally. */
    assert(kshim_update_trial_before_boot(&rec, 2u, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_before_boot(&rec, target, 2u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_ARG);
    rec.state = 99u;
    assert(kshim_update_trial_before_boot(&rec, target, 1u, "default",
                                          rec.new_commit, 1u,
                                          &dec) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &out) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_rollback(&rec, from,
                                       &out) == KSHIM_STORAGE_FORMAT);
}

static void assert_confirm_refused(const struct kshim_update_trial *rec,
                                   uint32_t slot, const uint8_t *boot_sha256,
                                   const char *os_name, const uint8_t *commit,
                                   uint32_t serial, int expected)
{
    struct kshim_update_trial out, snap;

    memset(&out, 0xa5, sizeof(out));
    memcpy(&snap, &out, sizeof(out));
    assert(kshim_update_trial_confirm(rec, slot, boot_sha256, os_name, commit,
                                      serial, &out) == expected);
    assert(memcmp(&out, &snap, sizeof(out)) == 0);
}

/*
 * Confirmation must follow the mirrored policy: a dual record confirms from
 * either physical slot with the mirrored (new) deployment, while a legacy
 * single-slot record still confirms only on its target slot.  No slot is
 * selected or marked successful by this transition.
 *
 * The supplied boot digest must be the one observed on actual_slot, so
 * target_slot requires new_boot_sha256 and a dual from_slot requires
 * other_boot_sha256.  Swapped, stale and missing digests are refused without
 * touching the output.
 */
static void test_confirm_policy(uint32_t from, uint32_t target)
{
    struct kshim_update_trial rec, from_out, target_out;
    uint8_t bad_commit[32];
    uint8_t wrong_sha[32];

    memset(bad_commit, 0x99, sizeof(bad_commit));
    memset(wrong_sha, 0xa6, sizeof(wrong_sha));

    /* Both physical origins confirm the same mirrored deployment when each
     * presents the digest recorded for the slot that actually booted. */
    init_dual_fixture(&rec, from, target);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 3u;
    assert(kshim_update_trial_confirm(&rec, from, rec.other_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &from_out) == KSHIM_STORAGE_OK);
    assert(from_out.state == KSHIM_UPDATE_TRIAL_CONFIRMED);
    assert(from_out.sequence == rec.sequence + 1u);
    assert(from_out.from_slot == from && from_out.target_slot == target);
    assert(from_out.policy == KSHIM_UPDATE_POLICY_OFFLINE_DUAL);
    assert(memcmp(from_out.other_boot_sha256, rec.other_boot_sha256, 32) == 0);
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &target_out) == KSHIM_STORAGE_OK);
    assert(target_out.state == KSHIM_UPDATE_TRIAL_CONFIRMED);
    assert_wire_eq(&from_out, &target_out);
    assert_wire_eq(&target_out, &from_out);

    /* A digest belonging to the other slot, an old version, or an unrelated
     * image is refused on both physical slots with no output mutation. */
    assert_confirm_refused(&rec, from, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.other_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, from, rec.old_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.old_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, from, wrong_sha, "default", rec.new_commit,
                           1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, wrong_sha, "default", rec.new_commit,
                           1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, from, NULL, "default", rec.new_commit, 1u,
                           KSHIM_STORAGE_ARG);
    assert_confirm_refused(&rec, target, NULL, "default", rec.new_commit, 1u,
                           KSHIM_STORAGE_ARG);

    /* Invalid state, deployment and slot are refused without mutation. */
    init_dual_fixture(&rec, from, target);
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 3u;
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, "wrong",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, "default",
                           rec.old_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           bad_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.new_commit, 9u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, NULL,
                           rec.new_commit, 1u, KSHIM_STORAGE_ARG);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default", NULL,
                           1u, KSHIM_STORAGE_ARG);
    assert_confirm_refused(&rec, 2u, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_ARG);
    rec.state = KSHIM_UPDATE_TRIAL_CONFIRMED;
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    rec.state = KSHIM_UPDATE_TRIAL_ROLLED_BACK;
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_FORMAT);

    /* A dual confirmation that cannot advance its sequence fails cleanly. */
    init_dual_fixture(&rec, from, target);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 3u;
    rec.sequence = UINT64_MAX;
    assert_confirm_refused(&rec, from, rec.other_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_OVERFLOW);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_OVERFLOW);

    /* Legacy single-slot records still confirm on the target slot only, and
     * only with the new deployment's own boot digest. */
    init_fixture(&rec, from, target);
    rec.state = KSHIM_UPDATE_TRIAL_TRIAL;
    rec.remaining = 2u;
    assert(kshim_update_trial_confirm(&rec, target, rec.new_boot_sha256,
                                      "default", rec.new_commit, 1u,
                                      &target_out) == KSHIM_STORAGE_OK);
    assert(target_out.state == KSHIM_UPDATE_TRIAL_CONFIRMED);
    assert(target_out.sequence == rec.sequence + 1u);
    assert_confirm_refused(&rec, from, rec.new_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.old_boot_sha256, "default",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, wrong_sha, "default", rec.new_commit,
                           1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, NULL, "default", rec.new_commit, 1u,
                           KSHIM_STORAGE_ARG);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "wrong",
                           rec.new_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.old_commit, 1u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default",
                           rec.new_commit, 9u, KSHIM_STORAGE_CONFLICT);
    assert_confirm_refused(&rec, target, rec.new_boot_sha256, "default", NULL,
                           1u, KSHIM_STORAGE_ARG);
}

int main(void)
{
    test_legacy(0u, 1u);
    test_legacy(1u, 0u);
    test_dual(0u, 1u);
    test_dual(1u, 0u);
    test_confirm_policy(0u, 1u);
    test_confirm_policy(1u, 0u);
    return 0;
}
