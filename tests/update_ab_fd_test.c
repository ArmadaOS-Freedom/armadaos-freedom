/* SPDX-License-Identifier: GPL-3.0-only */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <sys/stat.h>
#include <unistd.h>

#define main existing_ab_selftests_main
#include "../devices/lenovo-tb323fu/boot/kshim-success-source/tests/storage_ab_test.c"
#undef main

int armada_update_ab_inspect_fds(
    const int fds[6],
    const uint32_t sizes[6],
    const uint64_t counts[6],
    const uint8_t ids[6],
    uint32_t enabled,
    uint32_t result[3]
);
int armada_update_ab_partition_fds(
    const int fds[6],
    const uint32_t sizes[6],
    const uint64_t counts[6],
    const uint8_t ids[6],
    uint32_t enabled,
    const char *label,
    uint64_t result[3],
    uint8_t uuid[16]
);

enum scenario {
    SC_VALID = 0,
    SC_CORRUPT_PRIMARY_GPT_CRC,
    SC_RECOVERY_MISC_CMD,
    SC_NONZERO_AOSP_MISC,
    SC_VALID_VAB_MERGE,
    SC_BOOT_LUN_MISMATCH,
    SC_EMPTY_INIT_BOOT_B,
    SC_DUP_ARGS_FD,
    SC_DUP_ALIAS_EXTRA_FD,
    SC_RW_EXTRA_FD,
    SC_INVALID_SIZE,
    SC_COUNT_MAX,
    SC_BAD_IDS0,
    SC_ENABLED_ZERO,
    SC_CLOSED_EXTRA_FD,
    SC_WRONG_SUPPLIED_LEN,
    SC_DIR_EXTRA_FD,
    SC_COUNT
};

static void write_all(int fd, const uint8_t *buf, size_t len)
{
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(fd, buf + off, len - off);
        assert(w > 0);
        off += (size_t)w;
    }
}

static void verify_file_matches(const char *path, const uint8_t *exp, size_t len)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    assert(fd >= 0);
    uint8_t buf[4096];
    size_t off = 0;
    while (off < len) {
        size_t want = len - off < sizeof(buf) ? len - off : sizeof(buf);
        ssize_t r = read(fd, buf, want);
        assert(r > 0);
        assert(!memcmp(buf, exp + off, (size_t)r));
        off += (size_t)r;
    }
    assert(read(fd, buf, 1) == 0);
    assert(close(fd) == 0);
}

static void canonical_uuid(uint8_t out[16], const uint8_t raw[16])
{
    out[0] = raw[3];
    out[1] = raw[2];
    out[2] = raw[1];
    out[3] = raw[0];
    out[4] = raw[5];
    out[5] = raw[4];
    out[6] = raw[7];
    out[7] = raw[6];
    memcpy(out + 8, raw + 8, 8);
}

static void locator_fails(const int fds[6], const uint32_t sizes[6],
                          const uint64_t counts[6], const uint8_t ids[6],
                          uint32_t enabled, const char *label)
{
    uint64_t result[3] = {
        UINT64_C(0xdeadbeef01234567),
        UINT64_C(0xfeedface89abcdef),
        UINT64_C(0xcafebabe55aa33cc)
    };
    uint8_t uuid[16], before[16];
    memset(uuid, 0xa5, sizeof(uuid));
    memcpy(before, uuid, sizeof(before));
    assert(armada_update_ab_partition_fds(fds, sizes, counts, ids, enabled,
                                          label, result, uuid) != 0);
    assert(result[0] == UINT64_C(0xdeadbeef01234567));
    assert(result[1] == UINT64_C(0xfeedface89abcdef));
    assert(result[2] == UINT64_C(0xcafebabe55aa33cc));
    assert(!memcmp(uuid, before, sizeof(uuid)));
}

static void locator_succeeds(const int fds[6], const uint32_t sizes[6],
                             const uint64_t counts[6], const uint8_t ids[6],
                             uint32_t enabled, const char *label,
                             uint64_t first, const uint8_t expected_uuid[16])
{
    uint64_t result[3] = {0, 0, 0};
    uint8_t uuid[16] = {0};
    assert(armada_update_ab_partition_fds(fds, sizes, counts, ids, enabled,
                                          label, result, uuid) == 0);
    assert(result[0] == 4 && result[1] == first && result[2] == 8);
    assert(!memcmp(uuid, expected_uuid, sizeof(uuid)));
}

static void run_case(unsigned bs, unsigned selected, enum scenario sc)
{
    struct fixture f;
    init(&f, bs);
    uint8_t expected_uuid[2][16];
    for (unsigned slot = 0; slot < 2; ++slot) {
        uint8_t raw[16];
        for (unsigned j = 0; j < 16; ++j)
            raw[j] = (uint8_t)(0x30u + 0x20u * slot + j);
        memcpy(entry(&f, 4, slot) + 16, raw, sizeof(raw));
        canonical_uuid(expected_uuid[slot], raw);
    }
    tables(&f, 4);
    if (selected == 1)
        assert(kshim_ab_select(&f.media, 1, 0) == 0);
    f.writes = f.lun_writes = f.flushes = 0;

    if (sc == SC_CORRUPT_PRIMARY_GPT_CRC) {
        f.disk[4].bytes[bs + 16] ^= 1;
    } else if (sc == SC_RECOVERY_MISC_CMD) {
        f.disk[0].bytes[8 * bs] = 'r';
    } else if (sc == SC_NONZERO_AOSP_MISC) {
        f.disk[0].bytes[8 * bs + 2048] = 1;
    } else if (sc == SC_VALID_VAB_MERGE) {
        uint8_t *m = f.disk[0].bytes + 8 * bs + 32768;
        m[0] = 2;
        p32(m + 1, 0x56740ab0u);
        m[5] = 3;
        m[6] = 0;
    } else if (sc == SC_EMPTY_INIT_BOOT_B) {
        memset(f.disk[4].bytes + 64 * bs, 0, 8);
    }

    const char *tmp = getenv("TMPDIR");
    assert(tmp && tmp[0] && strlen(tmp) < 384);
    char dir[512];
    int dn = snprintf(dir, sizeof(dir), "%s/abfdXXXXXX", tmp);
    assert(dn > 0 && (size_t)dn < sizeof(dir));
    assert(mkdtemp(dir) != NULL);

    char paths[6][544];
    int owners[6];
    int args_fds[6];
    uint32_t sizes[6];
    uint64_t counts[6];
    uint8_t ids[6] = {0, 1, 2, 0, 0, 0};
    uint32_t enabled = selected ? 2u : 1u;
    size_t disk_bytes = (size_t)BLOCKS * bs;

    for (unsigned l = 0; l < 6; ++l) {
        int pn = snprintf(paths[l], sizeof(paths[l]), "%s/lun%u.img", dir, l);
        assert(pn > 0 && (size_t)pn < sizeof(paths[l]));
        int wfd = open(paths[l], O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
        assert(wfd >= 0);
        write_all(wfd, f.disk[l].bytes, disk_bytes);
        assert(close(wfd) == 0);

        owners[l] = open(paths[l], O_RDONLY | O_CLOEXEC);
        assert(owners[l] >= 0);
        assert(lseek(owners[l], 7, SEEK_SET) == 7);
        args_fds[l] = owners[l];
        sizes[l] = bs;
        counts[l] = BLOCKS;
    }

    int extra_fd = -1;
    if (sc == SC_BOOT_LUN_MISMATCH) {
        enabled = 2u;
    } else if (sc == SC_DUP_ARGS_FD) {
        args_fds[5] = owners[0];
    } else if (sc == SC_DUP_ALIAS_EXTRA_FD) {
        extra_fd = dup(owners[0]);
        assert(extra_fd >= 0);
        args_fds[5] = extra_fd;
    } else if (sc == SC_RW_EXTRA_FD) {
        extra_fd = open(paths[2], O_RDWR | O_CLOEXEC);
        assert(extra_fd >= 0);
        args_fds[2] = extra_fd;
    } else if (sc == SC_INVALID_SIZE) {
        sizes[3] = 1024u;
    } else if (sc == SC_COUNT_MAX) {
        counts[1] = UINT64_MAX;
    } else if (sc == SC_BAD_IDS0) {
        ids[0] = 3u;
    } else if (sc == SC_ENABLED_ZERO) {
        enabled = 0u;
    } else if (sc == SC_CLOSED_EXTRA_FD) {
        extra_fd = dup(owners[4]);
        assert(extra_fd >= 0);
        assert(close(extra_fd) == 0);
        args_fds[4] = extra_fd;
        extra_fd = -1;
    } else if (sc == SC_WRONG_SUPPLIED_LEN) {
        counts[5] = BLOCKS - 1u;
    } else if (sc == SC_DIR_EXTRA_FD) {
        extra_fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        assert(extra_fd >= 0);
        args_fds[0] = extra_fd;
    }

    uint32_t result[3] = {0xdeadbeefu, 0xfeedfaceu, 0xcafebabeu};
    int rc = armada_update_ab_inspect_fds(args_fds, sizes, counts, ids, enabled, result);

    if (sc == SC_VALID) {
        assert(rc == 0);
        assert(result[0] == selected && result[1] == 1u && result[2] == 1u);
    } else if (sc == SC_EMPTY_INIT_BOOT_B) {
        assert(rc == 0);
        assert(result[0] == 0u && result[1] == 1u && result[2] == 0u);
    } else {
        assert(rc != 0);
        assert(result[0] == 0xdeadbeefu && result[1] == 0xfeedfaceu && result[2] == 0xcafebabeu);
    }

    if (sc == SC_VALID || sc == SC_EMPTY_INIT_BOOT_B) {
        locator_succeeds(args_fds, sizes, counts, ids, enabled,
                         "boot_a", 8, expected_uuid[0]);
        locator_succeeds(args_fds, sizes, counts, ids, enabled,
                         "boot_b", 16, expected_uuid[1]);
    } else {
        locator_fails(args_fds, sizes, counts, ids, enabled, "boot_a");
        locator_fails(args_fds, sizes, counts, ids, enabled, "boot_b");
    }
    if (sc == SC_VALID) {
        char long_label[38];
        memset(long_label, 'x', 37);
        long_label[37] = '\0';
        locator_fails(args_fds, sizes, counts, ids, enabled, "not_present");
        locator_fails(args_fds, sizes, counts, ids, enabled, "");
        locator_fails(args_fds, sizes, counts, ids, enabled, long_label);
        locator_fails(args_fds, sizes, counts, ids, enabled, "boot_\xc3\xa9");
        locator_fails(args_fds, sizes, counts, ids, enabled, NULL);
    }

    assert(!f.writes && !f.lun_writes && !f.flushes);
    for (unsigned l = 0; l < 6; ++l) {
        assert(fcntl(owners[l], F_GETFD) != -1);
        assert(lseek(owners[l], 0, SEEK_CUR) == 7);
        verify_file_matches(paths[l], f.disk[l].bytes, disk_bytes);
    }

    if (extra_fd >= 0)
        assert(close(extra_fd) == 0);
    for (unsigned l = 0; l < 6; ++l) {
        assert(close(owners[l]) == 0);
        assert(unlink(paths[l]) == 0);
    }
    assert(rmdir(dir) == 0);
    dispose(&f);
}

static void find_partition_checks(unsigned bs)
{
    struct fixture f, original;
    init(&f, bs);
    init(&original, bs);
    for (unsigned lun = 3; lun <= 5; lun += 2) {
        partition(&f, lun, 0, "efisp", 8, 15, NULL);
        partition(&original, lun, 0, "efisp", 8, 15, NULL);
        tables(&f, lun);
        tables(&original, lun);
    }
    f.writes = f.lun_writes = f.flushes = 0;

    unsigned lun = 0xdeadbeefu;
    struct kshim_partition part, before;
    memset(&part, 0xa5, sizeof(part));
    before = part;
    assert(kshim_ab_find_partition(&f.media, "efisp", &lun, &part) != 0);
    assert(lun == 0xdeadbeefu && !memcmp(&part, &before, sizeof(part)));
    assert(kshim_ab_find_partition(&f.media, "boot_a", NULL, &part) != 0);
    assert(kshim_ab_find_partition(&f.media, "boot_a", &lun, NULL) != 0);
    assert(lun == 0xdeadbeefu && !memcmp(&part, &before, sizeof(part)));
    assert(!f.writes && !f.lun_writes && !f.flushes);
    same_disks(&f, &original, 1);
    dispose(&f);
    dispose(&original);
}

int main(void)
{
    const unsigned sizes[] = {512u, 4096u};
    for (unsigned i = 0; i < 2; ++i) {
        unsigned bs = sizes[i];
        find_partition_checks(bs);
        run_case(bs, 0, SC_VALID);
        run_case(bs, 1, SC_VALID);
        for (enum scenario sc = SC_CORRUPT_PRIMARY_GPT_CRC; sc < SC_COUNT; ++sc)
            run_case(bs, 0, sc);
    }
    puts("update_ab_fd_test: PASS");
    return 0;
}
