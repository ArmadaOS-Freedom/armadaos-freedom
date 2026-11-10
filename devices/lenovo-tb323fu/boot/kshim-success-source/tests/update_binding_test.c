#include "update_binding.h"
#include "storage_block.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define KEY "armada.deployment="
#define HASH "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
#define BIND KEY "Linux/" HASH ".7"
#define OS64 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
#define OS65 "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

static void expect_n(const char *s, size_t bytes, int rc)
{
    struct kshim_update_binding out, before, after;
    memset(&out, 0xa5, sizeof(out));
    memcpy(&before, &out, sizeof(before));
    assert(kshim_update_binding_parse(s, bytes, &out) == rc);
    if (rc != KSHIM_STORAGE_OK) {
        memcpy(&after, &out, sizeof(after));
        assert(memcmp(&before, &after, sizeof(before)) == 0);
    }
}

static void expect(const char *s, int rc)
{
    expect_n(s, strlen(s) + 1, rc);
}

int main(void)
{
    static const char key[] = KEY;
    static char large[65536];
    struct kshim_update_binding out;
    char embedded[] = BIND;
    char unterminated[] = BIND;

    assert(sizeof(key) - 1 == 18);
    expect(BIND, KSHIM_STORAGE_OK);
    assert(kshim_update_binding_parse(BIND, sizeof(BIND), &out) == KSHIM_STORAGE_OK);
    assert(strcmp(out.os_name, "Linux") == 0 && out.serial == 7);
    for (unsigned i = 0; i < 32; ++i) assert(out.commit[i] == i);

    expect(KEY "zero/" HASH ".0", KSHIM_STORAGE_OK);
    expect(KEY "max/" HASH ".4294967295", KSHIM_STORAGE_OK);
    expect(KEY OS64 "/" HASH ".1", KSHIM_STORAGE_OK);
    expect(" \t\n\r\f\v" BIND "\v\f\r\n\t ", KSHIM_STORAGE_OK);

    expect(BIND " " BIND, KSHIM_STORAGE_FORMAT);
    expect(BIND " " KEY "Other/" HASH ".8", KSHIM_STORAGE_FORMAT);
    expect("armada.deployment", KSHIM_STORAGE_FORMAT);
    expect(KEY, KSHIM_STORAGE_FORMAT);
    expect("other.value=1", KSHIM_STORAGE_NOT_FOUND);

    expect(KEY "Linux/000102030405060708090A0B0C0D0E0F101112131415161718191A1B1C1D1E1F.7", KSHIM_STORAGE_FORMAT);
    expect(KEY "Linux/g00102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f.7", KSHIM_STORAGE_FORMAT);
    expect(KEY "Linux/00.7", KSHIM_STORAGE_FORMAT);
    expect(BIND "/extra", KSHIM_STORAGE_FORMAT);
    expect(KEY "Lin\xc3\xa9ux/" HASH ".7", KSHIM_STORAGE_FORMAT);
    expect(KEY "_Linux/" HASH ".7", KSHIM_STORAGE_FORMAT);
    expect(KEY OS65 "/" HASH ".7", KSHIM_STORAGE_FORMAT);
    expect(KEY "Linux/" HASH ".4294967296", KSHIM_STORAGE_FORMAT);
    expect(KEY "Linux/" HASH ".01", KSHIM_STORAGE_FORMAT);
    expect(KEY "Linux/" HASH ".-1", KSHIM_STORAGE_FORMAT);

    embedded[5] = '\0';
    expect_n(embedded, sizeof(embedded), KSHIM_STORAGE_FORMAT);
    expect_n(unterminated, sizeof(unterminated) - 1, KSHIM_STORAGE_FORMAT);
    expect_n(BIND, 0, KSHIM_STORAGE_ARG);
    expect_n(large, sizeof(large) + 1, KSHIM_STORAGE_ARG);
    assert(kshim_update_binding_parse(NULL, 1, &out) == KSHIM_STORAGE_ARG);
    assert(kshim_update_binding_parse(BIND, sizeof(BIND), NULL) == KSHIM_STORAGE_ARG);

    puts("PASS");
    return 0;
}
