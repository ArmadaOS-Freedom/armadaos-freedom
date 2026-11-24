/* tests/boot_disk_entry_test.c */
#include "boot_disk_entry.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures = 0;

static void check(int cond, const char *msg)
{
    if (!cond) {
        fprintf(stderr, "FAIL: %s\n", msg);
        failures++;
    }
}

/* Fixed fixture: version 1, diagnostic 0, non-zero boot and root UUIDs,
 * reserved bytes zero. */
static const uint8_t fixture[64] = {
    'A', 'R', 'M', 'D', 'S', 'K', '1', 0x00,
    0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
    0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x01,
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
    0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x02,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
};

static const uint8_t boot_uuid[16] = {
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
    0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x01,
};

static const uint8_t root_uuid[16] = {
    0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
    0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x02,
};

static void test_fixture(void)
{
    struct kshim_disk_entry entry;
    uint8_t buf[64];

    memset(&entry, 0xaa, sizeof(entry));
    check(kshim_disk_entry_decode(fixture, sizeof(fixture), &entry) == 0,
          "fixture decodes");
    check(entry.diagnostic == false, "fixture diagnostic false");
    check(memcmp(entry.boot_uuid, boot_uuid, 16) == 0, "fixture boot uuid");
    check(memcmp(entry.root_uuid, root_uuid, 16) == 0, "fixture root uuid");

    /* Diagnostic flag toggled to 1. */
    memcpy(buf, fixture, sizeof(buf));
    buf[12] = 0x01;
    memset(&entry, 0xaa, sizeof(entry));
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == 0,
          "fixture diagnostic true decodes");
    check(entry.diagnostic == true, "fixture diagnostic true");

    /* Both flags checked above. */
}

static void test_rejections(void)
{
    struct kshim_disk_entry entry;
    uint8_t buf[64];

    /* NULL data. */
    check(kshim_disk_entry_decode(NULL, sizeof(fixture), &entry) == -1,
          "reject null data");

    /* NULL out. */
    check(kshim_disk_entry_decode(fixture, sizeof(fixture), NULL) == -1,
          "reject null out");

    /* Wrong length: shorter. */
    check(kshim_disk_entry_decode(fixture, 63, &entry) == -1,
          "reject short length");

    /* Wrong length: longer. */
    {
        uint8_t big[65];
        memcpy(big, fixture, 64);
        big[64] = 0;
        check(kshim_disk_entry_decode(big, sizeof(big), &entry) == -1,
              "reject long length");
    }

    /* Bad magic. */
    memcpy(buf, fixture, sizeof(buf));
    buf[0] = 'X';
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject bad magic");

    /* Bad version. */
    memcpy(buf, fixture, sizeof(buf));
    buf[8] = 0x02;
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject bad version");

    /* Bad flag (not 0 or 1). */
    memcpy(buf, fixture, sizeof(buf));
    buf[12] = 0x02;
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject bad flag");

    /* Non-zero reserved. */
    memcpy(buf, fixture, sizeof(buf));
    buf[48] = 0x01;
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject nonzero reserved");

    /* Zero boot UUID. */
    memcpy(buf, fixture, sizeof(buf));
    memset(buf + 16, 0, 16);
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject zero boot uuid");

    /* Zero root UUID. */
    memcpy(buf, fixture, sizeof(buf));
    memset(buf + 32, 0, 16);
    check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
          "reject zero root uuid");

    /* On failure out must not be written. */
    memset(&entry, 0x5a, sizeof(entry));
    {
        uint8_t sentinel[sizeof(entry)];
        memcpy(sentinel, &entry, sizeof(entry));
        check(kshim_disk_entry_decode(buf, sizeof(buf), &entry) == -1,
              "reject keeps out untouched (root zero)");
        check(memcmp(&entry, sentinel, sizeof(entry)) == 0,
              "out not filled on failure");
    }
}

static int test_real_descriptor(const char *path)
{
    FILE *f = NULL;
    long size;
    uint8_t *buf = NULL;
    struct kshim_disk_entry entry;
    int rc = 0;

    f = fopen(path, "rb");
    if (f == NULL) {
        fprintf(stderr, "FAIL: cannot open %s\n", path);
        return 1;
    }

    if (fseek(f, 0, SEEK_END) != 0) {
        fprintf(stderr, "FAIL: fseek end\n");
        rc = 1;
        goto done;
    }
    size = ftell(f);
    if (size < 0) {
        fprintf(stderr, "FAIL: ftell\n");
        rc = 1;
        goto done;
    }
    if (fseek(f, 0, SEEK_SET) != 0) {
        fprintf(stderr, "FAIL: fseek start\n");
        rc = 1;
        goto done;
    }
    if ((unsigned long)size != 64ul) {
        fprintf(stderr, "FAIL: real descriptor length %ld != 64\n", size);
        rc = 1;
        goto done;
    }

    buf = (uint8_t *)malloc((size_t)size);
    if (buf == NULL) {
        fprintf(stderr, "FAIL: malloc\n");
        rc = 1;
        goto done;
    }
    if (fread(buf, 1, (size_t)size, f) != (size_t)size) {
        fprintf(stderr, "FAIL: fread\n");
        rc = 1;
        goto done;
    }

    memset(&entry, 0, sizeof(entry));
    if (kshim_disk_entry_decode(buf, (size_t)size, &entry) != 0) {
        fprintf(stderr, "FAIL: real descriptor does not decode\n");
        rc = 1;
        goto done;
    }

    /* Real descriptor must produce identical known UUIDs to the fixture. */
    if (memcmp(entry.boot_uuid, boot_uuid, 16) != 0) {
        fprintf(stderr, "FAIL: real descriptor boot uuid mismatch\n");
        rc = 1;
        goto done;
    }
    if (memcmp(entry.root_uuid, root_uuid, 16) != 0) {
        fprintf(stderr, "FAIL: real descriptor root uuid mismatch\n");
        rc = 1;
        goto done;
    }

    /* Mode toggles: flip diagnostic bit in-memory and re-decode. */
    buf[12] = 0x01;
    memset(&entry, 0, sizeof(entry));
    if (kshim_disk_entry_decode(buf, (size_t)size, &entry) != 0) {
        fprintf(stderr, "FAIL: real descriptor diagnostic toggle\n");
        rc = 1;
        goto done;
    }
    if (!entry.diagnostic) {
        fprintf(stderr, "FAIL: real descriptor diagnostic true expected\n");
        rc = 1;
        goto done;
    }
    buf[12] = 0x00;
    memset(&entry, 0, sizeof(entry));
    if (kshim_disk_entry_decode(buf, (size_t)size, &entry) != 0) {
        fprintf(stderr, "FAIL: real descriptor diagnostic toggle back\n");
        rc = 1;
        goto done;
    }
    if (entry.diagnostic) {
        fprintf(stderr, "FAIL: real descriptor diagnostic false expected\n");
        rc = 1;
        goto done;
    }

done:
    free(buf);
    fclose(f);
    return rc;
}

int main(int argc, char **argv)
{
    test_fixture();
    test_rejections();

    if (argc >= 2) {
        if (test_real_descriptor(argv[1]) != 0) {
            failures++;
        }
    }

    if (failures != 0) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    printf("boot_disk_entry_test: OK\n");
    return 0;
}
