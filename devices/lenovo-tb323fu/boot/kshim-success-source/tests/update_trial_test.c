// SPDX-License-Identifier: GPL-3.0-only
#include "update_trial.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static void seal(uint8_t *wire)
{
    put32(wire + 4092, kshim_crc32(wire, 4092));
}

static void fixture(struct kshim_update_trial *r)
{
    size_t i;

    memset(r, 0, sizeof(*r));
    r->sequence = UINT64_C(0x0102030405060708);
    for (i = 0; i < 16; ++i) {
        r->partition_uuid[i] = (uint8_t)(0x10u + i);
        r->installation_uuid[i] = (uint8_t)(0x40u + i);
        r->transaction_uuid[i] = (uint8_t)(0x70u + i);
    }
    r->state = KSHIM_UPDATE_TRIAL_PREPARED;
    r->from_slot = 0;
    r->target_slot = 1;
    r->remaining = 4;
    r->attempt_limit = 4;
    r->old_serial = UINT32_MAX;
    r->new_serial = UINT32_MAX;
    memcpy(r->os_name, "host-01", 8);
    for (i = 0; i < 32; ++i) {
        r->old_commit[i] = (uint8_t)(0x81u + i);
        r->new_commit[i] = (uint8_t)(0xa1u + i);
        r->old_boot_sha256[i] = (uint8_t)(0xc1u + i);
        r->new_boot_sha256[i] = (uint8_t)(0xe1u + i);
        r->plan_sha256[i] = (uint8_t)(0x21u + i);
    }
}

static void encode_ok(const struct kshim_update_trial *r, uint8_t *wire)
{
    assert(kshim_update_trial_encode(r, wire, 4096) == KSHIM_STORAGE_OK);
}

static void decode_is(const uint8_t *wire, int expected,
                      const struct kshim_update_trial *r)
{
    struct kshim_update_trial out, before;

    memset(&out, 0xa5, sizeof(out));
    memcpy(&before, &out, sizeof(out));
    assert(kshim_update_trial_decode(wire, 4096, r->partition_uuid,
                                     r->installation_uuid, &out) == expected);
    if (expected != KSHIM_STORAGE_OK)
        assert(memcmp(&out, &before, sizeof(out)) == 0);
}

static void test_roundtrip(void)
{
    static const uint8_t sequence[8] = {8, 7, 6, 5, 4, 3, 2, 1};
    struct kshim_update_trial r, out;
    uint8_t wire[4096];
    size_t i;

    fixture(&r);
    encode_ok(&r, wire);
    assert(memcmp(wire + 16, sequence, 8) == 0);
    assert(wire[92] == 255 && wire[95] == 255 &&
           wire[96] == 255 && wire[99] == 255);
    for (i = 108; i < 168; ++i)
        assert(wire[i] == 0);
    for (i = 328; i < 4092; ++i)
        assert(wire[i] == 0);
    assert(kshim_update_trial_decode(wire, 4096, r.partition_uuid,
                                     r.installation_uuid, &out) == KSHIM_STORAGE_OK);
    assert(out.sequence == r.sequence && out.state == r.state &&
           out.from_slot == r.from_slot && out.target_slot == r.target_slot &&
           out.remaining == r.remaining && out.attempt_limit == r.attempt_limit &&
           out.old_serial == UINT32_MAX && out.new_serial == UINT32_MAX &&
           strcmp(out.os_name, r.os_name) == 0);
    assert(memcmp(out.partition_uuid, r.partition_uuid, 16) == 0 &&
           memcmp(out.installation_uuid, r.installation_uuid, 16) == 0 &&
           memcmp(out.transaction_uuid, r.transaction_uuid, 16) == 0 &&
           memcmp(out.old_commit, r.old_commit, 32) == 0 &&
           memcmp(out.new_commit, r.new_commit, 32) == 0 &&
           memcmp(out.old_boot_sha256, r.old_boot_sha256, 32) == 0 &&
           memcmp(out.new_boot_sha256, r.new_boot_sha256, 32) == 0 &&
           memcmp(out.plan_sha256, r.plan_sha256, 32) == 0);
    memset(r.os_name, 'a', 64);
    r.os_name[64] = '\0';
    encode_ok(&r, wire);
    for (i = 100; i < 164; ++i)
        assert(wire[i] == 'a');
    assert(wire[164] == 0);
    decode_is(wire, KSHIM_STORAGE_OK, &r);
}

static void test_arguments_and_policy(void)
{
    struct kshim_update_trial r, out;
    uint8_t wire[4096], zero[4096] = {0};
    size_t i;

    fixture(&r);
    assert(kshim_update_trial_encode(NULL, wire, 4096) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_encode(&r, NULL, 4096) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_encode(&r, wire, 4095) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_decode(NULL, 4096, r.partition_uuid,
                                     r.installation_uuid, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_decode(wire, 0, r.partition_uuid,
                                     r.installation_uuid, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_decode(wire, 4096, NULL,
                                     r.installation_uuid, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_decode(wire, 4096, r.partition_uuid,
                                     NULL, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_decode(wire, 4096, r.partition_uuid,
                                     r.installation_uuid, NULL) == KSHIM_STORAGE_ARG);
    decode_is(zero, KSHIM_STORAGE_EMPTY, &r);
    memset(wire, 0xa5, sizeof(wire));
    r.sequence = 0;
    assert(kshim_update_trial_encode(&r, wire, 4096) == KSHIM_STORAGE_FORMAT);
    for (i = 0; i < sizeof(wire); ++i)
        assert(wire[i] == 0xa5);

    {
        static const struct { uint32_t state, remaining, limit; int ok; } cases[] = {
            {KSHIM_UPDATE_TRIAL_PREPARED, 7, 7, 1},
            {KSHIM_UPDATE_TRIAL_PREPARED, 6, 7, 0},
            {KSHIM_UPDATE_TRIAL_TRIAL, 6, 7, 1},
            {KSHIM_UPDATE_TRIAL_TRIAL, 7, 7, 0},
            {KSHIM_UPDATE_TRIAL_CONFIRMED, 0, 1, 1},
            {KSHIM_UPDATE_TRIAL_CONFIRMED, 1, 1, 0},
            {KSHIM_UPDATE_TRIAL_ROLLED_BACK, 0, 1, 1},
            {KSHIM_UPDATE_TRIAL_ROLLED_BACK, 7, 7, 1},
            {KSHIM_UPDATE_TRIAL_PREPARED, 0, 0, 0},
            {KSHIM_UPDATE_TRIAL_PREPARED, 8, 8, 0},
            {KSHIM_UPDATE_TRIAL_ROLLED_BACK, 5, 4, 0}
        };
        for (i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
            fixture(&r);
            r.state = cases[i].state;
            r.remaining = cases[i].remaining;
            r.attempt_limit = cases[i].limit;
            assert(kshim_update_trial_encode(&r, wire, 4096) ==
                   (cases[i].ok ? KSHIM_STORAGE_OK : KSHIM_STORAGE_FORMAT));
        }
    }
    for (i = 0; i < 8; ++i) {
        uint8_t *fields[8];
        fixture(&r);
        fields[0] = r.partition_uuid; fields[1] = r.installation_uuid;
        fields[2] = r.transaction_uuid; fields[3] = r.old_commit;
        fields[4] = r.new_commit; fields[5] = r.old_boot_sha256;
        fields[6] = r.new_boot_sha256; fields[7] = r.plan_sha256;
        memset(fields[i], 0, i < 3 ? 16 : 32);
        assert(kshim_update_trial_encode(&r, wire, 4096) == KSHIM_STORAGE_FORMAT);
    }
}

static void test_corruption(void)
{
    static const struct { size_t at; uint8_t mask; int rc; } cases[] = {
        {0, 1, KSHIM_STORAGE_FORMAT}, {8, 1, KSHIM_STORAGE_FORMAT},
        {12, 1, KSHIM_STORAGE_FORMAT},
        {72, 1, KSHIM_STORAGE_FORMAT}, {76, 1, KSHIM_STORAGE_FORMAT},
        {80, 3, KSHIM_STORAGE_FORMAT}, {84, 1, KSHIM_STORAGE_FORMAT},
        {88, 4, KSHIM_STORAGE_FORMAT}, {100, 0x80, KSHIM_STORAGE_FORMAT},
        {108, 1, KSHIM_STORAGE_FORMAT}, {165, 1, KSHIM_STORAGE_FORMAT},
        {328, 1, KSHIM_STORAGE_FORMAT}, {24, 0x10, KSHIM_STORAGE_CONFLICT},
        {40, 0x40, KSHIM_STORAGE_CONFLICT}
    };
    struct kshim_update_trial r;
    uint8_t good[4096], wire[4096];
    size_t i;

    fixture(&r);
    encode_ok(&r, good);
    memcpy(wire, good, 4096);
    wire[200] ^= 1;
    decode_is(wire, KSHIM_STORAGE_CRC, &r);
    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        memcpy(wire, good, 4096);
        wire[cases[i].at] ^= cases[i].mask;
        seal(wire);
        decode_is(wire, cases[i].rc, &r);
    }
    memcpy(wire, good, 4096);
    memset(wire + 16, 0, 8);
    seal(wire);
    decode_is(wire, KSHIM_STORAGE_FORMAT, &r);
    for (i = 0; i < 8; ++i) {
        static const size_t at[8] = {24, 40, 56, 168, 200, 232, 264, 296};
        memcpy(wire, good, 4096);
        memset(wire + at[i], 0, i < 3 ? 16 : 32);
        seal(wire);
        decode_is(wire, KSHIM_STORAGE_FORMAT, &r);
    }
}

static void test_select(void)
{
    struct kshim_update_trial r, out, before;
    uint8_t a[4096], b[4096], zero[4096] = {0}, torn[4096];
    unsigned selected = 99;
    uint64_t seq;
    int pass;

    fixture(&r);
    encode_ok(&r, a);
    r.sequence++;
    r.remaining--;
    r.state = KSHIM_UPDATE_TRIAL_TRIAL;
    encode_ok(&r, b);
    for (pass = 0; pass < 2; ++pass) {
        assert(kshim_update_trial_select(pass ? b : a, pass ? a : b, 4096,
                    r.partition_uuid, r.installation_uuid, &out, &selected) == KSHIM_STORAGE_OK);
        assert(out.sequence == r.sequence && selected == (unsigned)(1 - pass));
    }
    assert(kshim_update_trial_select(b, b, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_OK && selected == 0);
    memset(&out, 0xa5, sizeof(out)); memcpy(&before, &out, sizeof(out)); selected = 77;
    memcpy(a, b, 4096);
    a[56] ^= 1;
    seal(a);
    assert(kshim_update_trial_select(a, b, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_CONFLICT);
    assert(memcmp(&out, &before, sizeof(out)) == 0 && selected == 77);
    for (seq = 0; seq < 2; ++seq) {
        selected = 99;
        assert(kshim_update_trial_select(seq ? zero : b, seq ? b : zero, 4096,
                    r.partition_uuid, r.installation_uuid, &out, &selected) == KSHIM_STORAGE_OK);
        assert(selected == (unsigned)seq);
    }
    memcpy(torn, b, 4096); torn[300] ^= 1;
    selected = 99;
    assert(kshim_update_trial_select(torn, b, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_OK && selected == 1);
    assert(kshim_update_trial_select(zero, zero, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_EMPTY);
    assert(kshim_update_trial_select(zero, torn, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_FORMAT);
    assert(kshim_update_trial_select(NULL, b, 4096, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_select(a, b, 0, r.partition_uuid,
                r.installation_uuid, &out, &selected) == KSHIM_STORAGE_ARG);
    assert(kshim_update_trial_select(a, b, 4096, r.partition_uuid,
                r.installation_uuid, &out, NULL) == KSHIM_STORAGE_ARG);
}

int main(void)
{
    test_roundtrip();
    test_arguments_and_policy();
    test_corruption();
    test_select();
    puts("update_trial_test: OK");
    return 0;
}
