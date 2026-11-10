#define _GNU_SOURCE
#define main prior_ab_test_main
#include "storage_ab_test.c"
#undef main
#define main prior_ufs_test_main
#include "update_ufs_boot_test.c"
#undef main

#include <sys/types.h>

extern int armada_update_ab_select_fds(const int[6], const uint32_t[6],
    const uint64_t[6], const uint8_t[6], int, uint32_t, uint32_t, uint32_t[3]);
extern int armada_update_ab_mark_success_fds(const int[6], const uint32_t[6],
    const uint64_t[6], const uint8_t[6], int, uint32_t, uint32_t[3]);

enum kind { NORMAL, STALE, RO_LUN, RO_BSG, DUP_FD, PWRITE_ONCE,
    FSYNC_ONCE, FSYNC_ALWAYS, PARTIAL, BSG_WRITE, POST_INSPECT };
enum { NONE, WRITE_ONCE, FLUSH_ONCE, FLUSH_ALWAYS, WRITE_HALF };
struct testfix {
    struct fixture mem, old, exp;
    char dir[4096], path[6][4096];
    int fd[6], bsg;
    unsigned bs;
    uint32_t sizes[6];
    uint64_t counts[6];
    uint8_t ids[6];
};
static int fault_mode, pwrite_calls, fsync_calls, cases_run, mark_cases;

ssize_t __real_pwrite64(int, const void *, size_t, off64_t);
ssize_t __wrap_pwrite64(int fd, const void *buf, size_t bytes, off64_t off)
{
    ssize_t rc;
    ++pwrite_calls;
    if (fault_mode == WRITE_HALF && pwrite_calls == 1) {
        size_t half = bytes / 2;
        return __real_pwrite64(fd, buf, half ? half : 1, off);
    }
    rc = __real_pwrite64(fd, buf, bytes, off);
    if (fault_mode == WRITE_ONCE && pwrite_calls == 1 && rc >= 0) {
        errno = EIO;
        return -1;
    }
    return rc;
}
int __real_fsync(int);
int __wrap_fsync(int fd)
{
    ++fsync_calls;
    if (fault_mode == FLUSH_ALWAYS ||
        (fault_mode == FLUSH_ONCE && fsync_calls == 1)) {
        errno = EIO;
        return -1;
    }
    return __real_fsync(fd);
}

static void check_disks(struct testfix *t, const struct fixture *expected)
{
    uint8_t buf[8192];
    size_t total = BLOCKS * t->bs;
    for (unsigned l = 0; l < 6; ++l) {
        int fd = open(t->path[l], O_RDONLY);
        size_t at = 0;
        CHECK(fd >= 0);
        while (at < total) {
            size_t want = total - at < sizeof(buf) ? total - at : sizeof(buf);
            ssize_t got;
            do got = pread(fd, buf, want, (off_t)at);
            while (got < 0 && errno == EINTR);
            CHECK(got == (ssize_t)want);
            CHECK(!memcmp(buf, expected->disk[l].bytes + at, want));
            at += want;
        }
        CHECK(pread(fd, buf, 1, (off_t)total) == 0);
        CHECK(close(fd) == 0);
    }
}
static void check_owned(struct testfix *t)
{
    for (unsigned l = 0; l < 6; ++l) {
        CHECK(fcntl(t->fd[l], F_GETFD) >= 0);
        CHECK(lseek(t->fd[l], 0, SEEK_CUR) == 7);
    }
    CHECK(fcntl(t->bsg, F_GETFD) >= 0);
}
static void expect_selected(struct testfix *t, unsigned target, unsigned old)
{
    CHECK(kshim_ab_select(&t->exp.media, target, old) == 0);
    check_disks(t, &t->exp);
}
static struct testfix *open_case(unsigned bs, enum kind kind)
{
    struct testfix *t = calloc(1, sizeof(*t));
    const char *tmp = getenv("TMPDIR");
    int n;
    CHECK(t && tmp);
    t->bs = bs;
    init(&t->mem, bs);
    init(&t->old, bs);
    init(&t->exp, bs);
    n = snprintf(t->dir, sizeof(t->dir), "%s/ab-fd-XXXXXX", tmp);
    CHECK(n > 0 && (size_t)n < sizeof(t->dir));
    CHECK(mkdtemp(t->dir));
    for (unsigned l = 0; l < 6; ++l) {
        size_t at = 0, total = BLOCKS * bs;
        n = snprintf(t->path[l], sizeof(t->path[l]), "%s/lun%u", t->dir, l);
        CHECK(n > 0 && (size_t)n < sizeof(t->path[l]));
        t->fd[l] = open(t->path[l], O_CREAT | O_EXCL | O_RDWR, 0600);
        CHECK(t->fd[l] >= 0);
        while (at < total) {
            ssize_t put = write(t->fd[l], t->mem.disk[l].bytes + at, total - at);
            CHECK(put > 0);
            at += (size_t)put;
        }
        CHECK(lseek(t->fd[l], 7, SEEK_SET) == 7);
        t->sizes[l] = bs;
        t->counts[l] = BLOCKS;
        t->ids[l] = (uint8_t[]){0, 1, 2, 0, 0, 0}[l];
    }
    t->bsg = open("/dev/null", O_RDWR);
    CHECK(t->bsg >= 0);
    if (kind == RO_LUN) {
        close(t->fd[0]);
        t->fd[0] = open(t->path[0], O_RDONLY);
        CHECK(t->fd[0] >= 0 && lseek(t->fd[0], 7, SEEK_SET) == 7);
    } else if (kind == RO_BSG) {
        close(t->bsg);
        t->bsg = open("/dev/null", O_RDONLY);
        CHECK(t->bsg >= 0);
    } else if (kind == DUP_FD) {
        close(t->fd[5]);
        t->fd[5] = dup(t->fd[4]);
        CHECK(t->fd[5] >= 0);
    }
    reset_fake(1);
    fault_mode = NONE;
    pwrite_calls = fsync_calls = 0;
    return t;
}
static void close_case(struct testfix *t)
{
    for (unsigned l = 0; l < 6; ++l) {
        CHECK(close(t->fd[l]) == 0);
        CHECK(unlink(t->path[l]) == 0);
    }
    CHECK(close(t->bsg) == 0 && rmdir(t->dir) == 0);
    dispose(&t->mem);
    dispose(&t->old);
    dispose(&t->exp);
    free(t);
    fault_mode = NONE;
}
static int select_native(struct testfix *t, uint32_t target, uint32_t expected,
                         uint32_t result[3])
{
    pwrite_calls = fsync_calls = 0;
    return armada_update_ab_select_fds(t->fd, t->sizes, t->counts, t->ids,
        t->bsg, target, expected, result);
}
static void success_case(unsigned bs)
{
    struct testfix *t = open_case(bs, NORMAL);
    uint32_t result[3] = {7, 8, 9};
    CHECK(select_native(t, 1, 0, result) == 0);
    CHECK(result[0] == 1 && result[1] == 1 && result[2] == 1 && fake.current == 2);
    expect_selected(t, 1, 0);
    check_owned(t);
    CHECK(select_native(t, 0, 1, result) == 0);
    CHECK(result[0] == 0 && result[1] == 1 && result[2] == 1 && fake.current == 1);
    expect_selected(t, 0, 1);
    check_owned(t);
    int bsg_writes = fake.writes;
    CHECK(select_native(t, 0, 0, result) == 0);
    CHECK(result[0] == 0 && result[1] == 1 && result[2] == 1 && fake.current == 1);
    CHECK(pwrite_calls == 0 && fsync_calls == 0 && fake.writes == bsg_writes);
    check_disks(t, &t->exp);
    check_owned(t);
    close_case(t);
    ++cases_run;
}
static void rejection_case(enum kind kind)
{
    struct testfix *t = open_case(512, kind);
    uint32_t result[3] = {7, 8, 9};
    uint32_t expected = kind == STALE ? 1 : 0;
    CHECK(select_native(t, 1, expected, result) != 0);
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    CHECK(pwrite_calls == 0 && fake.writes == 0);
    check_disks(t, &t->old);
    close_case(t);
    ++cases_run;
}
static void rollback_case(enum kind kind)
{
    struct testfix *t = open_case(512, kind);
    uint32_t result[3] = {7, 8, 9};
    int rc;
    fault_mode = kind == PWRITE_ONCE ? WRITE_ONCE :
        kind == FSYNC_ONCE ? FLUSH_ONCE : FLUSH_ALWAYS;
    rc = select_native(t, 1, 0, result);
    fault_mode = NONE;
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    if (kind == FSYNC_ALWAYS) {
        CHECK(rc == KSHIM_AB_UNCERTAIN);
    } else {
        CHECK(rc != 0 && rc != KSHIM_AB_UNCERTAIN && fake.current == 1);
        check_disks(t, &t->old);
    }
    close_case(t);
    ++cases_run;
}
static void partial_case(void)
{
    struct testfix *t = open_case(512, PARTIAL);
    uint32_t result[3] = {7, 8, 9};
    fault_mode = WRITE_HALF;
    CHECK(select_native(t, 1, 0, result) == 0);
    fault_mode = NONE;
    CHECK(result[0] == 1 && result[1] == 1 && result[2] == 1 && fake.current == 2);
    expect_selected(t, 1, 0);
    check_owned(t);
    close_case(t);
    ++cases_run;
}
static void bsg_failure_case(void)
{
    struct testfix *t = open_case(512, BSG_WRITE);
    uint32_t result[3] = {7, 8, 9};
    fake.fault = F_IOCTL;
    fake.fault_errno = EINTR;
    fake.fault_at = 6;
    int rc = select_native(t, 1, 0, result);
    CHECK(rc != 0 && rc != KSHIM_AB_UNCERTAIN);
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    CHECK(fake.writes == 1 && fake.current == 1);
    check_disks(t, &t->old);
    close_case(t);
    ++cases_run;
}
static void post_inspect_case(void)
{
    struct testfix *t = open_case(512, POST_INSPECT);
    uint32_t result[3] = {7, 8, 9};
    fake.fault = F_IOCTL;
    fake.fault_errno = EINTR;
    fake.fault_at = 9;
    CHECK(select_native(t, 1, 0, result) == KSHIM_AB_UNCERTAIN);
    CHECK(fake.current == 2);
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    expect_selected(t, 1, 0);
    close_case(t);
    ++cases_run;
}
static int mark_native(struct testfix *t, uint32_t expected,
                       uint32_t result[3])
{
    pwrite_calls = fsync_calls = 0;
    return armada_update_ab_mark_success_fds(t->fd, t->sizes, t->counts,
        t->ids, t->bsg, expected, result);
}
static void expect_marked(struct testfix *t, unsigned current)
{
    uint8_t *e = entry(&t->exp, 4, current);
    p64(e + 48, (u64(e + 48) | SUCCESS) & ~(RETRIES | UNBOOTABLE));
    tables(&t->exp, 4);
    check_disks(t, &t->exp);
}
static void mark_success_case(unsigned bs, unsigned current)
{
    struct testfix *t = open_case(bs, NORMAL);
    uint32_t result[3] = {7, 8, 9};
    int bsg_writes;
    if (current) {
        CHECK(select_native(t, 1, 0, result) == 0);
        expect_selected(t, 1, 0);
    }
    bsg_writes = fake.writes;
    CHECK(mark_native(t, current, result) == 0);
    CHECK(result[0] == current && result[current + 1] == 1);
    CHECK(fake.current == current + 1 && fake.writes == bsg_writes);
    expect_marked(t, current);
    check_owned(t);
    CHECK(mark_native(t, current, result) == 0);
    CHECK(result[0] == current && result[current + 1] == 1);
    CHECK(pwrite_calls == 0 && fsync_calls == 0);
    CHECK(fake.current == current + 1 && fake.writes == bsg_writes);
    check_disks(t, &t->exp);
    check_owned(t);
    close_case(t);
    ++mark_cases;
}
static void mark_rejection_case(enum kind kind)
{
    struct testfix *t = open_case(512, kind);
    uint32_t result[3] = {7, 8, 9};
    uint32_t expected = kind == STALE ? 1 : 0;
    CHECK(mark_native(t, expected, result) != 0);
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    CHECK(pwrite_calls == 0 && fake.writes == 0);
    check_disks(t, &t->old);
    close_case(t);
    ++mark_cases;
}
static void mark_rollback_case(enum kind kind)
{
    struct testfix *t = open_case(512, kind);
    uint32_t result[3] = {7, 8, 9};
    int rc;
    fault_mode = kind == PWRITE_ONCE ? WRITE_ONCE :
        kind == FSYNC_ONCE ? FLUSH_ONCE : FLUSH_ALWAYS;
    rc = mark_native(t, 0, result);
    fault_mode = NONE;
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    CHECK(fake.writes == 0);
    if (kind == FSYNC_ALWAYS) {
        CHECK(rc == KSHIM_AB_UNCERTAIN);
    } else {
        CHECK(rc != 0 && rc != KSHIM_AB_UNCERTAIN);
        check_disks(t, &t->old);
    }
    check_owned(t);
    close_case(t);
    ++mark_cases;
}
static void mark_post_inspect_case(void)
{
    struct testfix *t = open_case(512, POST_INSPECT);
    uint32_t result[3] = {7, 8, 9};
    fake.fault = F_IOCTL;
    fake.fault_errno = EINTR;
    fake.fault_at = 4;
    CHECK(mark_native(t, 0, result) == KSHIM_AB_UNCERTAIN);
    CHECK(result[0] == 7 && result[1] == 8 && result[2] == 9);
    CHECK(fake.writes == 0 && fake.current == 1);
    expect_marked(t, 0);
    check_owned(t);
    close_case(t);
    ++mark_cases;
}
int main(void)
{
    success_case(512);
    success_case(4096);
    rejection_case(STALE);
    rejection_case(RO_LUN);
    rejection_case(RO_BSG);
    rejection_case(DUP_FD);
    rollback_case(PWRITE_ONCE);
    rollback_case(FSYNC_ONCE);
    rollback_case(FSYNC_ALWAYS);
    partial_case();
    bsg_failure_case();
    post_inspect_case();
    mark_success_case(512, 0);
    mark_success_case(512, 1);
    mark_success_case(4096, 0);
    mark_success_case(4096, 1);
    mark_rejection_case(STALE);
    mark_rejection_case(RO_LUN);
    mark_rejection_case(RO_BSG);
    mark_rejection_case(DUP_FD);
    mark_rollback_case(PWRITE_ONCE);
    mark_rollback_case(FSYNC_ONCE);
    mark_rollback_case(FSYNC_ALWAYS);
    mark_post_inspect_case();
    CHECK(cases_run == 12);
    CHECK(mark_cases == 12);
    puts("PASS native A/B fd selection tests (12 cases)");
    puts("PASS native A/B fd mark-success tests (12 cases)");
    return 0;
}
