#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libfdt.h>

#include "boot_witness.h"

#define BUFSZ 147456 /* 144 KiB: fits 65537-byte bootargs plus FDT overhead */

static uint64_t fw_align[BUFSZ / 8];
static uint64_t lw_align[BUFSZ / 8];

static int failures;

#define CHECK(cond)                                                        \
    do {                                                                   \
        if (!(cond)) {                                                     \
            fprintf(stderr, "CHECK failed at line %d: %s\n", __LINE__,    \
                    #cond);                                                \
            failures++;                                                    \
        }                                                                  \
    } while (0)

static void *fw_build(const char *args, int len)
{
    void *fdt = fw_align;
    int off;

    CHECK(fdt_create_empty_tree(fdt, BUFSZ) == 0);
    off = fdt_add_subnode(fdt, 0, "chosen");
    CHECK(off >= 0);
    if (args != NULL)
        CHECK(fdt_setprop(fdt, off, "bootargs", args, len) == 0);
    return fdt;
}

static void *fw_str(const char *args)
{
    return fw_build(args, (int)strlen(args) + 1);
}

static void *lw_build(void)
{
    void *fdt = lw_align;
    int off;

    CHECK(fdt_create_empty_tree(fdt, BUFSZ) == 0);
    off = fdt_add_subnode(fdt, 0, "chosen");
    CHECK(off >= 0);
    CHECK(fdt_setprop(fdt, off, "armada,other", "keep", 5) == 0);
    return fdt;
}

static void expect_lock(const char *args, int want)
{
    static uint64_t snapshot[BUFSZ / 8];
    void *fdt;

    fdt = fw_str(args);
    memcpy(snapshot, fdt, BUFSZ);
    CHECK(kshim_boot_lock_read(fdt) == want);
    CHECK(memcmp(snapshot, fdt, BUFSZ) == 0);
    CHECK(fdt_check_header(fdt) == 0);
}

static void check_wire(const void *fdt, uint32_t want_version,
                       uint32_t want_state)
{
    int off = fdt_path_offset(fdt, "/chosen");
    const void *prop;
    int len;
    const uint32_t *cells;

    CHECK(off >= 0);
    prop = fdt_getprop((void *)fdt, off, "armada,boot-lock", &len);
    CHECK(prop != NULL && len == 16);
    if (!prop || len != 16)
        return;
    cells = prop;
    CHECK(fdt32_to_cpu(cells[0]) == want_version);
    CHECK(fdt32_to_cpu(cells[1]) == 1);
    CHECK(fdt32_to_cpu(cells[2]) == want_state);
    CHECK(fdt32_to_cpu(cells[3]) == 0);
    /* unrelated property untouched */
    prop = fdt_getprop((void *)fdt, off, "armada,other", &len);
    CHECK(prop != NULL && len == 5 && memcmp(prop, "keep", 5) == 0);
}

static void test_lock_states(void)
{
    expect_lock("androidboot.verifiedbootstate=green", 1);
    expect_lock("androidboot.verifiedbootstate=yellow", 1);
    expect_lock("androidboot.verifiedbootstate=orange", 2);
    /* corroborating keys */
    expect_lock("androidboot.verifiedbootstate=orange "
                "androidboot.vbmeta.device_state=unlocked "
                "androidboot.flash.locked=0", 2);
    expect_lock("androidboot.flash.locked=1 "
                "androidboot.vbmeta.device_state=locked "
                "androidboot.verifiedbootstate=green", 1);
    /* key order / whitespace variants */
    expect_lock("\tandroidboot.verifiedbootstate=orange  ", 2);
    expect_lock("quiet androidboot.verifiedbootstate=green rw", 1);
    expect_lock("androidboot.verifiedbootstate=green\n"
                "androidboot.verifiedbootstate=green", 0); /* duplicate */
    /* missing verifiedbootstate => unknown even with others */
    expect_lock("androidboot.vbmeta.device_state=unlocked "
                "androidboot.flash.locked=0", 0);
    expect_lock("", 0);
}

static void test_bad_fdts(void)
{
    static const char nul_args[] = "androidboot.verifiedbootstate=\0green";
    char longargs[65537];

    CHECK(kshim_boot_lock_read(NULL) == 0);
    {
        uint8_t junk[64];
        memset(junk, 0xab, sizeof(junk));
        CHECK(kshim_boot_lock_read(junk) == 0);
    }
    {
        /* valid empty FDT without /chosen */
        CHECK(fdt_create_empty_tree(fw_align, BUFSZ) == 0);
        CHECK(kshim_boot_lock_read(fw_align) == 0);
    }
    {
        void *fdt = fw_build(NULL, 0); /* chosen without bootargs */
        CHECK(kshim_boot_lock_read(fdt) == 0);
    }
    {
        /* unterminated bootargs: build with strlen (omitting NUL) */
        void *fdt = fw_build("androidboot.verifiedbootstate=green",
                             (int)strlen("androidboot.verifiedbootstate=green"));
        CHECK(kshim_boot_lock_read(fdt) == 0);
    }
    /* embedded NUL: value truncated before terminator */
    CHECK(kshim_boot_lock_read(fw_build(nul_args, (int)sizeof(nul_args))) == 0);
    /* 65537-byte args (including trailing NUL) exceeds 65536 max */
    memset(longargs, 'x', sizeof(longargs));
    memcpy(longargs, "androidboot.verifiedbootstate=green ", 36);
    longargs[sizeof(longargs) - 1] = '\0';
    CHECK(kshim_boot_lock_read(fw_build(longargs, (int)sizeof(longargs))) == 0);
}

static void test_duplicates(void)
{
    /* duplicates return unknown even when values are equal */
    expect_lock("androidboot.verifiedbootstate=green "
                "androidboot.verifiedbootstate=green", 0);
    expect_lock("androidboot.verifiedbootstate=orange "
                "androidboot.verifiedbootstate=orange", 0);
    expect_lock("androidboot.vbmeta.device_state=unlocked "
                "androidboot.vbmeta.device_state=unlocked "
                "androidboot.verifiedbootstate=orange", 0);
    expect_lock("androidboot.flash.locked=1 androidboot.flash.locked=1 "
                "androidboot.verifiedbootstate=green", 0);
}

static void test_conflicts(void)
{
    expect_lock("androidboot.verifiedbootstate=green "
                "androidboot.verifiedbootstate=orange", 0);
    expect_lock("androidboot.verifiedbootstate=green "
                "androidboot.vbmeta.device_state=unlocked", 0);
    expect_lock("androidboot.verifiedbootstate=orange "
                "androidboot.flash.locked=1", 0);
    expect_lock("androidboot.vbmeta.device_state=locked "
                "androidboot.flash.locked=0 "
                "androidboot.verifiedbootstate=orange", 0);
}

static void test_malformed_and_lookalikes(void)
{
    expect_lock("androidboot.verifiedbootstate=teal", 0);
    expect_lock("androidboot.verifiedbootstate=GREEN", 0);
    expect_lock("androidboot.vbmeta.device_state=maybe "
                "androidboot.verifiedbootstate=green", 0);
    expect_lock("androidboot.flash.locked=maybe "
                "androidboot.verifiedbootstate=orange", 0);
    /* bare keys */
    expect_lock("androidboot.verifiedbootstate", 0);
    expect_lock("androidboot.verifiedbootstate green", 0);
    /* prefix lookalikes are ignored, real key decides */
    expect_lock("xandroidboot.verifiedbootstate=orange "
                "androidboot.verifiedbootstate=green", 1);
    expect_lock("androidboot.verifiedbootstatex=orange "
                "androidboot.verifiedbootstate=green", 1);
    expect_lock("androidboot.verifiedbootstate.extra=orange "
                "androidboot.verifiedbootstate=green", 1);
}

static void test_export(void)
{
    void *fdt;

    /* valid locked export: 4 BE u32 cells */
    fdt = lw_build();
    CHECK(kshim_boot_lock_export(fdt, 1) == 0);
    check_wire(fdt, 1, 1);

    /* valid unlocked export, clear-first replaces stale cells */
    CHECK(kshim_boot_lock_export(fdt, 2) == 0);
    check_wire(fdt, 1, 2);

    /* invalid state clears stale evidence including prior invalid state */
    CHECK(kshim_boot_lock_export(fdt, 7) == 0);
    CHECK(fdt_getprop(fdt, fdt_path_offset(fdt, "/chosen"),
                      "armada,boot-lock", NULL) == NULL);
    CHECK(kshim_boot_lock_export(fdt, 7) == 0); /* idempotent on absent */
    CHECK(kshim_boot_lock_export(fdt, 0) == 0);
    CHECK(fdt_getprop(fdt, fdt_path_offset(fdt, "/chosen"),
                      "armada,boot-lock", NULL) == NULL);

    /* missing /chosen: returns -FDT_ERR_NOTFOUND, no creation, no crash */
    CHECK(fdt_create_empty_tree(lw_align, BUFSZ) == 0);
    CHECK(kshim_boot_lock_export(lw_align, 1) == -FDT_ERR_NOTFOUND);
    CHECK(fdt_path_offset(lw_align, "/chosen") == -FDT_ERR_NOTFOUND);
}

static void emit_witness(int state)
{
    void *fdt = lw_build();
    const void *prop;
    int len, i;
    const uint8_t *p;

    if (kshim_boot_lock_export(fdt, state) != 0)
        exit(2);
    prop = fdt_getprop(fdt, fdt_path_offset(fdt, "/chosen"),
                       "armada,boot-lock", &len);
    if (!prop || len != 16)
        exit(2);
    p = prop;
    for (i = 0; i < 16; i++)
        printf("%02x", p[i]);
    printf("\n");
}

int main(void)
{
    test_lock_states();
    test_bad_fdts();
    test_duplicates();
    test_conflicts();
    test_malformed_and_lookalikes();
    test_export();
    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    emit_witness(1);
    emit_witness(2);
    return 0;
}
