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

int main(void)
{
    test_read_valid_positions_and_whitespace();
    test_read_missing_and_structural_errors();
    test_read_oversize_bootargs();
    test_read_duplicates_malformed_and_lookalikes();
    test_export_behavior_and_security();
    test_export_nospace_clears_forged_witness();
    test_snapshot_freeze_flow();

    printf("PASS: boot_witness_test\n");
    return 0;
}
