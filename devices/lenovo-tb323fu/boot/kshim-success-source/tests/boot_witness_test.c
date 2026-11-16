#include <boot_witness.h>

#include <assert.h>
#include <libfdt.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TEST_FDT_BUF_SIZE 4096

static void init_fdt_with_chosen(void *buf, size_t size, int with_chosen)
{
    int err;

    memset(buf, 0, size);
    err = fdt_create_empty_tree(buf, (int)size);
    assert(err == 0);

    if (with_chosen) {
        int node = fdt_add_subnode(buf, 0, "chosen");
        assert(node >= 0);
    }
}

static void make_fdt_with_raw_bootargs(void *buf, size_t size,
                                       const void *raw, int raw_len)
{
    int node;
    int err;

    init_fdt_with_chosen(buf, size, 1);
    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);

    if (raw != NULL) {
        err = fdt_setprop(buf, node, "bootargs", raw, raw_len);
        assert(err == 0);
    }
}

static void make_fdt_with_bootargs_str(void *buf, size_t size, const char *str)
{
    make_fdt_with_raw_bootargs(buf, size, str, (int)strlen(str) + 1);
}

static int check_read_pure(const void *fdt, size_t size)
{
    void *snapshot = malloc(size);
    int res;

    assert(snapshot != NULL);
    memcpy(snapshot, fdt, size);
    res = kshim_boot_slot_read(fdt);
    assert(memcmp(snapshot, fdt, size) == 0);
    free(snapshot);
    return res;
}

static void test_read_valid_positions_and_whitespace(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];

    /* Exact single token */
    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == 1);

    /* Start of bootargs */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_a console=ttyS0 quiet");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_b console=ttyS0 quiet");
    assert(check_read_pure(buf, sizeof(buf)) == 1);

    /* Middle of bootargs */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "console=ttyS0 androidboot.slot_suffix=_a root=/dev/mmcblk0p1");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "console=ttyS0 androidboot.slot_suffix=_b root=/dev/mmcblk0p1");
    assert(check_read_pure(buf, sizeof(buf)) == 1);

    /* End of bootargs (regression test for NUL included in final token) */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "console=ttyS0 quiet androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "console=ttyS0 quiet androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == 1);

    /* All 6 allowed ASCII whitespace characters: ' ', '\t', '\r', '\n', '\f', '\v' */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               " \t\r\n\f\vfoo=1\t\r\n\f\vandroidboot.slot_suffix=_a \t\r\n\f\vbar=2\v\f\n\r\t ");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "\v\f\n\r\t androidboot.slot_suffix=_b\t\r\n\f\v");
    assert(check_read_pure(buf, sizeof(buf)) == 1);
}

static void test_read_missing_and_structural_errors(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];
    int node;
    int err;

    /* NULL pointer */
    assert(kshim_boot_slot_read(NULL) == -1);

    /* Bad FDT header */
    memset(buf, 0x5a, sizeof(buf));
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Missing /chosen node */
    init_fdt_with_chosen(buf, sizeof(buf), 0);
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Missing bootargs property under /chosen */
    init_fdt_with_chosen(buf, sizeof(buf), 1);
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Zero-length bootargs property */
    init_fdt_with_chosen(buf, sizeof(buf), 1);
    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);
    err = fdt_setprop(buf, node, "bootargs", "", 0);
    assert(err == 0);
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Empty string (1 byte NUL) and whitespace-only string */
    make_fdt_with_bootargs_str(buf, sizeof(buf), "");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), " \t\r\n\f\v");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Unterminated string (no trailing NUL) */
    {
        const char raw[] = "androidboot.slot_suffix=_a";
        make_fdt_with_raw_bootargs(buf, sizeof(buf), raw, (int)(sizeof(raw) - 1));
        assert(check_read_pure(buf, sizeof(buf)) == -1);
    }

    /* Embedded NUL (multi-string property) */
    {
        const char raw[] = "androidboot.slot_suffix=_a\0console=ttyS0";
        make_fdt_with_raw_bootargs(buf, sizeof(buf), raw, (int)sizeof(raw));
        assert(check_read_pure(buf, sizeof(buf)) == -1);
    }

    {
        const char raw[] = "console=ttyS0\0androidboot.slot_suffix=_b";
        make_fdt_with_raw_bootargs(buf, sizeof(buf), raw, (int)sizeof(raw));
        assert(check_read_pure(buf, sizeof(buf)) == -1);
    }
}

static void test_read_oversize_bootargs(void)
{
    const size_t max_len = 65536;
    const size_t fdt_size = 131072;
    const char token[] = "androidboot.slot_suffix=_a";
    const size_t token_len = sizeof(token) - 1;
    void *fdt = malloc(fdt_size);
    char *args = malloc(max_len + 16);

    assert(fdt != NULL);
    assert(args != NULL);

    /* Exact max boundary (65536 bytes including NUL) must succeed */
    memset(args, ' ', max_len - 1);
    memcpy(args + (max_len - 1 - token_len), token, token_len);
    args[max_len - 1] = '\0';
    make_fdt_with_raw_bootargs(fdt, fdt_size, args, (int)max_len);
    assert(check_read_pure(fdt, fdt_size) == 0);

    /* Oversize (65537 bytes including NUL) must be rejected */
    memset(args, ' ', max_len);
    memcpy(args + (max_len - token_len), token, token_len);
    args[max_len] = '\0';
    make_fdt_with_raw_bootargs(fdt, fdt_size, args, (int)(max_len + 1));
    assert(check_read_pure(fdt, fdt_size) == -1);

    free(args);
    free(fdt);
}

static void test_read_duplicates_malformed_and_lookalikes(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];

    /* Duplicate equal values -> rejected (-1) */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_a androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_b foo=1 androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Duplicate conflicting values -> rejected (-1) */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_a androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_b androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Invalid, empty, and quoted values */
    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix= androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=_c");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=a");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=0");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=_a1");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=\"_a\"");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix=\"_b\"");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Bare key without '=' invalidates even if valid key is also present */
    make_fdt_with_bootargs_str(buf, sizeof(buf), "androidboot.slot_suffix");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix=_b androidboot.slot_suffix");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    /* Prefix lookalikes (regression test for missing '=' check) */
    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix_extra=_a androidboot.slot_suffix.bak=_b androidboot.slot_suffix2");
    assert(check_read_pure(buf, sizeof(buf)) == -1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix_extra=bad androidboot.slot_suffix=_a");
    assert(check_read_pure(buf, sizeof(buf)) == 0);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "androidboot.slot_suffix.foo=_a androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == 1);

    make_fdt_with_bootargs_str(buf, sizeof(buf),
                               "my_androidboot.slot_suffix=_a androidboot.slot_suffix=_b");
    assert(check_read_pure(buf, sizeof(buf)) == 1);
}

static void test_export_behavior_and_security(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];
    int node;
    int len;
    int err;
    const uint32_t *cells;
    const char *keep;
    const uint32_t malicious[4] = {
        cpu_to_fdt32(99),
        cpu_to_fdt32(99),
        cpu_to_fdt32(1),
        cpu_to_fdt32(0xffffffffu),
    };

    /* NULL or bad header */
    assert(kshim_boot_witness_export(NULL, 0) < 0);
    memset(buf, 0x5a, sizeof(buf));
    assert(kshim_boot_witness_export(buf, 0) < 0);

    /* Missing /chosen node must return error and must NOT create /chosen */
    init_fdt_with_chosen(buf, sizeof(buf), 0);
    assert(kshim_boot_witness_export(buf, 0) < 0);
    assert(fdt_path_offset(buf, "/chosen") < 0);

    /* Replace malicious pre-baked witness with known slot 0, preserving other props */
    init_fdt_with_chosen(buf, sizeof(buf), 1);
    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);
    assert(fdt_setprop_string(buf, node, "stdout-path", "serial0:115200n8") == 0);
    assert(fdt_setprop(buf, node, "armada,boot-witness", malicious, sizeof(malicious)) == 0);

    err = kshim_boot_witness_export(buf, 0);
    assert(err == 0);

    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);
    cells = fdt_getprop(buf, node, "armada,boot-witness", &len);
    assert(cells != NULL);
    assert(len == 16);
    assert(fdt32_to_cpu(cells[0]) == 1u);
    assert(fdt32_to_cpu(cells[1]) == 1u);
    assert(fdt32_to_cpu(cells[2]) == 0u);
    assert(fdt32_to_cpu(cells[3]) == 0u);

    keep = fdt_getprop(buf, node, "stdout-path", &len);
    assert(keep != NULL);
    assert(strcmp(keep, "serial0:115200n8") == 0);

    /* Export known slot 1 */
    err = kshim_boot_witness_export(buf, 1);
    assert(err == 0);
    node = fdt_path_offset(buf, "/chosen");
    cells = fdt_getprop(buf, node, "armada,boot-witness", &len);
    assert(cells != NULL);
    assert(len == 16);
    assert(fdt32_to_cpu(cells[0]) == 1u);
    assert(fdt32_to_cpu(cells[1]) == 1u);
    assert(fdt32_to_cpu(cells[2]) == 1u);
    assert(fdt32_to_cpu(cells[3]) == 0u);

    /* Unknown slot (-1 or invalid) removes stale witness and leaves no property */
    err = kshim_boot_witness_export(buf, -1);
    assert(err == 0);
    node = fdt_path_offset(buf, "/chosen");
    cells = fdt_getprop(buf, node, "armada,boot-witness", &len);
    assert(cells == NULL);
    assert(len == -FDT_ERR_NOTFOUND);
    keep = fdt_getprop(buf, node, "stdout-path", &len);
    assert(keep != NULL && strcmp(keep, "serial0:115200n8") == 0);

    /* Unknown slot when property is already absent still succeeds cleanly */
    err = kshim_boot_witness_export(buf, -1);
    assert(err == 0);
    node = fdt_path_offset(buf, "/chosen");
    assert(fdt_getprop(buf, node, "armada,boot-witness", &len) == NULL);
}

static void test_export_nospace_clears_forged_witness(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];
    int node;
    int len;
    int err;
    const uint8_t forged[4] = { 0xde, 0xad, 0xbe, 0xef };

    /*
     * Build a tree with a small 4-byte forged witness and pack the DTB.
     * Deleting the 4-byte property frees only 16 bytes in the struct block,
     * whereas setting the 16-byte witness requires 28 bytes (12-byte header +
     * 16-byte payload), forcing fdt_setprop() to fail with -FDT_ERR_NOSPACE.
     */
    init_fdt_with_chosen(buf, sizeof(buf), 1);
    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);
    assert(fdt_setprop(buf, node, "armada,boot-witness", forged, sizeof(forged)) == 0);
    assert(fdt_pack(buf) == 0);

    err = kshim_boot_witness_export(buf, 0);
    assert(err == -FDT_ERR_NOSPACE);

    /* Forged witness must have been removed before the failed setprop */
    node = fdt_path_offset(buf, "/chosen");
    assert(node >= 0);
    assert(fdt_getprop(buf, node, "armada,boot-witness", &len) == NULL);
    assert(len == -FDT_ERR_NOTFOUND);

    /* Also test packed DTB without prior witness failing with -FDT_ERR_NOSPACE */
    init_fdt_with_chosen(buf, sizeof(buf), 1);
    assert(fdt_pack(buf) == 0);
    err = kshim_boot_witness_export(buf, 1);
    assert(err == -FDT_ERR_NOSPACE);
    node = fdt_path_offset(buf, "/chosen");
    assert(fdt_getprop(buf, node, "armada,boot-witness", &len) == NULL);
}

static void test_snapshot_freeze_flow(void)
{
    uint8_t fw_fdt[TEST_FDT_BUF_SIZE];
    uint8_t fw_snapshot[TEST_FDT_BUF_SIZE];
    uint8_t linux_fdt[TEST_FDT_BUF_SIZE];
    int captured_slot;
    int node;
    int len;
    const uint32_t *cells;
    const uint32_t malicious[4] = {
        cpu_to_fdt32(1),
        cpu_to_fdt32(1),
        cpu_to_fdt32(0),
        cpu_to_fdt32(0),
    };

    /* 1. Firmware FDT has slot _b; Linux payload FDT has malicious slot _a witness */
    make_fdt_with_bootargs_str(fw_fdt, sizeof(fw_fdt),
                               "console=ttyS0 androidboot.slot_suffix=_b quiet");
    init_fdt_with_chosen(linux_fdt, sizeof(linux_fdt), 1);
    node = fdt_path_offset(linux_fdt, "/chosen");
    assert(node >= 0);
    assert(fdt_setprop(linux_fdt, node, "armada,boot-witness",
                       malicious, sizeof(malicious)) == 0);
    assert(fdt_setprop_string(linux_fdt, node, "bootargs",
                              "console=ttyS0 androidboot.slot_suffix=_a") == 0);

    /* 2. Capture slot once from firmware FDT before any menu/mutation */
    captured_slot = kshim_boot_slot_read(fw_fdt);
    assert(captured_slot == 1);

    /* 3. Mutate firmware FDT afterwards (simulating later bootargs edits) */
    make_fdt_with_bootargs_str(fw_fdt, sizeof(fw_fdt),
                               "console=ttyS0 androidboot.slot_suffix=_a quiet");
    memcpy(fw_snapshot, fw_fdt, sizeof(fw_fdt));

    /* 4. Export previously captured slot into separate Linux FDT */
    assert(kshim_boot_witness_export(linux_fdt, captured_slot) == 0);

    /* Firmware FDT must not be modified by export, and Linux FDT must reflect captured slot 1 */
    assert(memcmp(fw_fdt, fw_snapshot, sizeof(fw_fdt)) == 0);
    node = fdt_path_offset(linux_fdt, "/chosen");
    assert(node >= 0);
    cells = fdt_getprop(linux_fdt, node, "armada,boot-witness", &len);
    assert(cells != NULL);
    assert(len == 16);
    assert(fdt32_to_cpu(cells[0]) == 1u);
    assert(fdt32_to_cpu(cells[1]) == 1u);
    assert(fdt32_to_cpu(cells[2]) == 1u);
    assert(fdt32_to_cpu(cells[3]) == 0u);
}

/* ---- firmware bootconfig fallback ---------------------------------------- */

/* A synthetic firmware initrd: arbitrary ramdisk bytes followed by the AOSP
 * bootconfig trailer. The firmware DT points linux,initrd-start/end at it and
 * declares a memory node covering exactly that host buffer. */
#define BC_RAMDISK 64u
#define BC_MAX 4096u

static uint8_t g_initrd[BC_RAMDISK + BC_MAX + 32u] __attribute__((aligned(8)));

struct bc_opts {
    uint32_t pad;          /* bytes after the magic (kernel accepts 0..3) */
    int bad_checksum;
    int bad_magic;
    uint32_t size_delta;   /* added to the declared size */
    uint32_t nul_pad;      /* NUL bytes appended inside the declared size */
    int memory;            /* 0 none, 1 covering, 2 not covering */
    int cells32;           /* 4-byte linux,initrd-* cells */
};

static void put_le32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static size_t build_initrd(const char *params, const struct bc_opts *o)
{
    size_t len = strlen(params) + o->nul_pad;
    uint8_t *p = g_initrd + BC_RAMDISK;
    uint32_t sum = 0;
    size_t i;

    assert(len <= BC_MAX);
    memset(g_initrd, 0xa5, sizeof(g_initrd));
    memset(p, 0, len);
    memcpy(p, params, strlen(params));
    for (i = 0; i < len; i++) {
        sum += p[i];
    }
    put_le32(p + len, (uint32_t)len + o->size_delta);
    put_le32(p + len + 4, o->bad_checksum ? sum + 1u : sum);
    memcpy(p + len + 8, o->bad_magic ? "#BOOTCONFIg\n" : "#BOOTCONFIG\n", 12);
    memset(p + len + 20, 0, o->pad);
    return BC_RAMDISK + len + 20 + o->pad;
}

static void set_cells(void *fdt, int node, const char *name, uint64_t v,
                      int cells32)
{
    if (cells32) {
        assert(fdt_setprop_u32(fdt, node, name, (uint32_t)v) == 0);
    } else {
        assert(fdt_setprop_u64(fdt, node, name, v) == 0);
    }
}

static void make_bootconfig_fdt(void *buf, size_t size, const char *bootargs,
                                const char *params, const struct bc_opts *o)
{
    uint64_t start = (uint64_t)(uintptr_t)g_initrd;
    uint64_t end = start + build_initrd(params, o);
    int chosen;

    make_fdt_with_bootargs_str(buf, size, bootargs);
    assert(fdt_setprop_u32(buf, 0, "#address-cells", 2) == 0);
    assert(fdt_setprop_u32(buf, 0, "#size-cells", 2) == 0);
    if (o->memory) {
        int mem = fdt_add_subnode(buf, 0, "memory@0");
        fdt64_t reg[2];

        assert(mem >= 0);
        assert(fdt_setprop_string(buf, mem, "device_type", "memory") == 0);
        reg[0] = cpu_to_fdt64(o->memory == 1 ? start : end);
        reg[1] = cpu_to_fdt64(sizeof(g_initrd));
        assert(fdt_setprop(buf, mem, "reg", reg, sizeof(reg)) == 0);
    }
    chosen = fdt_path_offset(buf, "/chosen");
    assert(chosen >= 0);
    set_cells(buf, chosen, "linux,initrd-start", start, o->cells32);
    set_cells(buf, chosen, "linux,initrd-end", end, o->cells32);
}

static const struct bc_opts BC_OK = { 0, 0, 0, 0, 0, 1, 0 };

static int bc_slot(const char *bootargs, const char *params,
                   const struct bc_opts *o, int *source)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];
    uint8_t initrd_before[sizeof(g_initrd)];
    int slot;

    make_bootconfig_fdt(buf, sizeof(buf), bootargs, params, o);
    memcpy(initrd_before, g_initrd, sizeof(g_initrd));
    slot = check_read_pure(buf, sizeof(buf));
    assert(kshim_boot_slot_read_source(buf, source) == slot);
    assert(memcmp(initrd_before, g_initrd, sizeof(g_initrd)) == 0);
    return slot;
}

static int bc_lock(const char *bootargs, const char *params, int *source)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];

    make_bootconfig_fdt(buf, sizeof(buf), bootargs, params, &BC_OK);
    return kshim_boot_lock_read_source(buf, source);
}

/* Shape measured on TB710FU (ZUXOS 495 ABL): bootargs only carry the
 * "bootconfig" switch; slot and lock state exist only in bootconfig. */
#define TB710FU_PARAMS \
    "androidboot.hardware=qcom\nandroidboot.memcg=1\n" \
    "androidboot.usbcontroller=a600000.dwc3\n" \
    "androidboot.verifiedbootstate=green\n" \
    "androidboot.vbmeta.device_state=locked\n" \
    "androidboot.vbmeta.size=12288\n" \
    "androidboot.slot_suffix=_a\n"

static void test_bootconfig_slot_fallback(void)
{
    struct bc_opts o;
    int source = -1;

    assert(bc_slot("console=tty0 bootconfig", TB710FU_PARAMS, &BC_OK,
                   &source) == 0);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTCONFIG);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_b\n", &BC_OK,
                   &source) == 1);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTCONFIG);

    /* Spaced and quoted forms as written by bootconfig tools. */
    assert(bc_slot("bootconfig", "androidboot.slot_suffix = \"_b\"\n",
                   &BC_OK, &source) == 1);
    assert(bc_slot("bootconfig", "\tandroidboot.slot_suffix=_a  \r\n",
                   &BC_OK, &source) == 0);
    /* No terminating newline; trailing NUL padding inside the size. */
    o = BC_OK;
    o.nul_pad = 5;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_b", &o,
                   &source) == 1);
    /* Kernel-compatible alignment padding after the magic, 4-byte cells. */
    for (o = BC_OK, o.pad = 1; o.pad <= 3; o.pad++) {
        assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                       &source) == 0);
    }
    o = BC_OK;
    o.cells32 = 1;
    if ((uint64_t)(uintptr_t)g_initrd + sizeof(g_initrd) <= UINT32_MAX) {
        assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                       &source) == 0);
    }
    /* Statements after ';', and ';' or braces inside quotes or comments. */
    assert(bc_slot("bootconfig", "androidboot.x=1; androidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == 1);
    assert(bc_slot("bootconfig",
                   "androidboot.x=\"a;b{c}\"\nandroidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == 1);
    assert(bc_slot("bootconfig", "# { ; }\nandroidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == 0);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix = '_b'\n",
                   &BC_OK, &source) == 1);
    /* Unrelated arrays do not affect the witness keys. */
    assert(bc_slot("bootconfig",
                   "androidboot.x=a, b\nandroidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == 1);
    assert(bc_slot("bootconfig",
                   "androidboot.x=a,\n b\nandroidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == 0);
    assert(bc_slot("bootconfig",
                   "androidboot.x=a,\n b;androidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == 0);
    assert(bc_slot("bootconfig",
                   "androidboot.x=\"a,b\";androidboot.y='c,d'\n"
                   "androidboot.slot_suffix=_a\n", &BC_OK, &source) == 0);
    /* Comments and lookalike keys are ignored. */
    assert(bc_slot("bootconfig",
                   "# androidboot.slot_suffix=_b\n"
                   "androidboot.slot_suffix_extra=_b\n"
                   "androidboot.slot_suffix=_a\n", &BC_OK, &source) == 0);
}

static void test_bootconfig_slot_rejections(void)
{
    struct bc_opts o;
    int source = -1;

    /* The kernel switch is required. */
    assert(bc_slot("console=tty0", TB710FU_PARAMS, &BC_OK, &source) == -1);
    assert(source == 0);
    assert(bc_slot("bootconfig=1", TB710FU_PARAMS, &BC_OK, &source) == -1);
    assert(bc_slot("xbootconfig", TB710FU_PARAMS, &BC_OK, &source) == -1);

    /* Bootargs evidence wins and is never mixed with bootconfig. */
    assert(bc_slot("androidboot.slot_suffix=_b bootconfig", TB710FU_PARAMS,
                   &BC_OK, &source) == 1);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTARGS);
    assert(bc_slot("androidboot.slot_suffix bootconfig", TB710FU_PARAMS,
                   &BC_OK, &source) == -1);
    assert(bc_slot("androidboot.slot_suffix=_c bootconfig", TB710FU_PARAMS,
                   &BC_OK, &source) == -1);

    /* Duplicates, bad values and unsupported syntax. */
    assert(bc_slot("bootconfig",
                   "androidboot.slot_suffix=_a\nandroidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix += _a\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix = \"_a\", \"_b\"\n",
                   &BC_OK, &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix = \"_a\n", &BC_OK,
                   &source) == -1);
    /* A trailing comment is valid bootconfig syntax. */
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a # x\n", &BC_OK,
                   &source) == 0);
    assert(bc_slot("bootconfig", "androidboot.hardware=qcom\n", &BC_OK,
                   &source) == -1);
    assert(source == 0);

    /* Adjacent operators other than '=' invalidate the evidence. */
    assert(bc_slot("bootconfig", "androidboot.slot_suffix:=_b\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig", "androidboot.slot_suffix+=_b\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot.slot_suffix=_a\nandroidboot.slot_suffix:=_b\n",
                   &BC_OK, &source) == -1);

    /* Any brace block makes the bootconfig source unsupported: under the
     * tree syntax its keys gain a prefix. */
    assert(bc_slot("bootconfig",
                   "metadata {\nandroidboot.slot_suffix=_b\n}\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot.slot_suffix=_a\nmetadata { x = 1 }\n", &BC_OK,
                   &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot { slot_suffix = _b }\n", &BC_OK,
                   &source) == -1);
    assert(source == 0);

    /* ';' separates statements outside quotes, so a duplicate after it is
     * still seen. */
    assert(bc_slot("bootconfig",
                   "androidboot.slot_suffix=_a; androidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == -1);
    /* Single quotes are quotes too: nothing inside them is a statement. */
    assert(bc_slot("bootconfig",
                   "androidboot.x='ignored;androidboot.slot_suffix=_b;tail'\n",
                   &BC_OK, &source) == -1);
    /* Arrays are unsupported: a value list may continue on the next line. */
    assert(bc_slot("bootconfig",
                   "androidboot.x=first,\nandroidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot.slot_suffix=_a, _b\n", &BC_OK, &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot.x=first, # note\n  androidboot.slot_suffix=_b\n",
                   &BC_OK, &source) == -1);
    assert(bc_slot("bootconfig",
                   "androidboot.x='open\nandroidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == -1);
    /* An unterminated quote anywhere rejects the source. */
    assert(bc_slot("bootconfig",
                   "androidboot.x=\"open\nandroidboot.slot_suffix=_a\n",
                   &BC_OK, &source) == -1);

    /* Trailer integrity. */
    o = BC_OK;
    o.bad_checksum = 1;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);
    o = BC_OK;
    o.bad_magic = 1;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);
    o = BC_OK;
    o.pad = 4;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);
    o = BC_OK;
    o.size_delta = BC_RAMDISK + 1u; /* points before linux,initrd-start */
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);
    o = BC_OK;
    o.size_delta = 1u;              /* checksum no longer covers the data */
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);

    /* The initrd must be inside a declared memory range. */
    o = BC_OK;
    o.memory = 0;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);
    o = BC_OK;
    o.memory = 2;
    assert(bc_slot("bootconfig", "androidboot.slot_suffix=_a\n", &o,
                   &source) == -1);

    /* Non-NUL bytes after the first NUL inside the declared size. */
    {
        static const char params[] = "androidboot.slot_suffix=_a\n";
        uint8_t buf[TEST_FDT_BUF_SIZE];
        uint8_t *data = g_initrd + BC_RAMDISK;
        size_t len = strlen(params) + 2u;
        uint32_t sum = 0;
        size_t i;

        o = BC_OK;
        o.nul_pad = 2;
        make_bootconfig_fdt(buf, sizeof(buf), "bootconfig", params, &o);
        assert(kshim_boot_slot_read(buf) == 0);
        data[len - 1] = 'x';
        for (i = 0; i < len; i++) {
            sum += data[i];
        }
        put_le32(data + len + 4, sum);
        assert(kshim_boot_slot_read(buf) == -1);
    }

    /* Empty parameter data. */
    assert(bc_slot("bootconfig", "", &BC_OK, &source) == -1);
}

static void test_bootconfig_lock(void)
{
    int source = -1;

    assert(bc_lock("console=tty0 bootconfig", TB710FU_PARAMS, &source) == 1);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTCONFIG);
    assert(bc_lock("bootconfig",
                   "androidboot.verifiedbootstate = \"orange\"\n"
                   "androidboot.vbmeta.device_state = \"unlocked\"\n",
                   &source) == 2);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTCONFIG);

    /* verifiedbootstate is mandatory; corroboration must agree. */
    assert(bc_lock("bootconfig", "androidboot.vbmeta.device_state=locked\n",
                   &source) == 0);
    assert(source == 0);
    assert(bc_lock("bootconfig",
                   "androidboot.verifiedbootstate=green\n"
                   "androidboot.vbmeta.device_state=unlocked\n",
                   &source) == 0);
    assert(bc_lock("bootconfig",
                   "androidboot.verifiedbootstate=green\n"
                   "androidboot.verifiedbootstate=green\n",
                   &source) == 0);

    /* Any lock key in bootargs keeps the bootargs decision. */
    assert(bc_lock("androidboot.verifiedbootstate=orange bootconfig",
                   TB710FU_PARAMS, &source) == 2);
    assert(source == KSHIM_BOOT_EVIDENCE_BOOTARGS);
    assert(bc_lock("androidboot.vbmeta.device_state=locked bootconfig",
                   TB710FU_PARAMS, &source) == 0);
    assert(bc_lock("console=tty0", TB710FU_PARAMS, &source) == 0);
    assert(bc_lock("bootconfig",
                   "androidboot { verifiedbootstate = green }\n", &source) == 0);
}

static void test_export_source(void)
{
    uint8_t buf[TEST_FDT_BUF_SIZE];
    const uint32_t *cells;
    int node;
    int len;

    init_fdt_with_chosen(buf, sizeof(buf), 1);
    node = fdt_path_offset(buf, "/chosen");
    assert(kshim_boot_witness_export_source(buf, 1,
                                            KSHIM_BOOT_EVIDENCE_BOOTCONFIG) == 0);
    node = fdt_path_offset(buf, "/chosen");
    cells = fdt_getprop(buf, node, "armada,boot-witness", &len);
    assert(cells && len == 16);
    assert(fdt32_to_cpu(cells[0]) == 1u && fdt32_to_cpu(cells[1]) == 2u &&
           fdt32_to_cpu(cells[2]) == 1u && fdt32_to_cpu(cells[3]) == 0u);

    assert(kshim_boot_lock_export_source(buf, 1,
                                         KSHIM_BOOT_EVIDENCE_BOOTCONFIG) == 0);
    node = fdt_path_offset(buf, "/chosen");
    cells = fdt_getprop(buf, node, "armada,boot-lock", &len);
    assert(cells && len == 16);
    assert(fdt32_to_cpu(cells[1]) == 2u && fdt32_to_cpu(cells[2]) == 1u);

    /* An unknown source clears stale evidence and publishes nothing. */
    assert(kshim_boot_witness_export_source(buf, 0, 3) == 0);
    assert(kshim_boot_lock_export_source(buf, 1, 0) == 0);
    node = fdt_path_offset(buf, "/chosen");
    assert(fdt_getprop(buf, node, "armada,boot-witness", &len) == NULL);
    assert(fdt_getprop(buf, node, "armada,boot-lock", &len) == NULL);
}

int main(void)
{
    test_read_valid_positions_and_whitespace();
    test_read_missing_and_structural_errors();
    test_read_oversize_bootargs();
    test_read_duplicates_malformed_and_lookalikes();
    test_export_behavior_and_security();
    test_export_nospace_clears_forged_witness();
    test_snapshot_freeze_flow();
    test_bootconfig_slot_fallback();
    test_bootconfig_slot_rejections();
    test_bootconfig_lock();
    test_export_source();
    {
        uint8_t packed[TEST_FDT_BUF_SIZE];
        init_fdt_with_chosen(packed, sizeof(packed), 1);
        assert(fdt_pack(packed) == 0);
        assert(fdt_open_into(packed, packed, sizeof(packed)) == 0);
        assert(kshim_boot_witness_export(packed, 1) == 0);
        assert(fdt_pack(packed) == 0);
        assert(fdt_check_header(packed) == 0);
    }

    printf("PASS: boot_witness_test\n");
    return 0;
}
