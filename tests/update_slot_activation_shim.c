// SPDX-License-Identifier: GPL-3.0-only
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static char *attribute_path;
static int fixture_mode, get_count, set_count, have_bsg;
static dev_t bsg_dev;
static ino_t bsg_ino;

static int read_value(uint32_t *value)
{
    char buf[64], *end;
    unsigned long parsed;
    int fd;
    ssize_t used;
    if (!attribute_path || !value)
        return -EIO;
    fd = open(attribute_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -EIO;
    used = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (used <= 0)
        return -EIO;
    buf[used] = '\0';
    errno = 0;
    parsed = strtoul(buf, &end, 10);
    if (errno || end == buf || parsed > UINT32_MAX)
        return -EIO;
    *value = (uint32_t)parsed;
    return 0;
}

int armada_ufs_get_boot_lun(int fd, uint32_t *value)
{
    (void)fd;
    get_count++;
    return read_value(value);
}

int armada_ufs_set_boot_lun(int fd, uint32_t expected, uint32_t target)
{
    uint32_t current;
    char buf[64];
    int fd_out, len, rc = 0;
    (void)fd;
    set_count++;
    if (read_value(&current) || current != expected || target > 2)
        return -EIO;
    fd_out = open(attribute_path, O_WRONLY | O_CLOEXEC);
    if (fd_out < 0)
        return -EIO;
    len = snprintf(buf, sizeof(buf), "%u\n", target);
    if (pwrite(fd_out, buf, (size_t)len, 0) != len || fsync(fd_out))
        rc = -EIO;
    close(fd_out);
    if (rc)
        return rc;
    if (read_value(&current) || current != target)
        return -EIO;
    if ((fixture_mode == 1 && set_count == 1) || fixture_mode == 2)
        return -EIO;
    return 0;
}

int fixture_configure(const char *path, int mode)
{
    char *copy = strdup(path);
    if (!copy)
        return -1;
    free(attribute_path);
    attribute_path = copy;
    fixture_mode = mode;
    get_count = set_count = 0;
    return 0;
}

int fixture_set_bsg_fd(int fd)
{
    struct stat64 st;
    if (fstat64(fd, &st))
        return -1;
    bsg_dev = st.st_dev;
    bsg_ino = st.st_ino;
    have_bsg = 1;
    return 0;
}

int fixture_calls(void) { return get_count + set_count; }
int fixture_set_calls(void) { return set_count; }

int __real_fstat64(int fd, struct stat64 *st);
int __wrap_fstat64(int fd, struct stat64 *st)
{
    int rc = __real_fstat64(fd, st);
    if (!rc && have_bsg && st->st_dev == bsg_dev && st->st_ino == bsg_ino) {
        st->st_mode = (st->st_mode & ~S_IFMT) | S_IFCHR;
        st->st_rdev = makedev(240, 7);
    }
    return rc;
}
