/* SPDX-License-Identifier: GPL-3.0-only */
#define _FILE_OFFSET_BITS 64
#define _POSIX_C_SOURCE 200809L

#if !defined(__linux__)
#error "This adapter is Linux-only"
#endif

#include "update_ufs_boot.h"
#include <storage_ab.h>
#include <storage_block.h>

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/fs.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

struct update_ab_fd_ctx {
    int fd;
    uint32_t block_size;
    uint64_t block_count;
};

struct update_ab_media_ctx {
    uint32_t enabled_boot_id;
    int bsg_fd;
};

static int update_ab_validate_range(const struct update_ab_fd_ctx *ctx,
                                    uint64_t lba, uint32_t count,
                                    const void *buffer, uint64_t *offset_out,
                                    size_t *bytes_out)
{
    uint64_t offset;
    uint64_t bytes;

    if (ctx == NULL || (count != 0 && buffer == NULL))
        return KSHIM_STORAGE_ARG;
    if (count == 0) {
        *offset_out = 0;
        *bytes_out = 0;
        return KSHIM_STORAGE_OK;
    }
    if (lba > ctx->block_count ||
        (uint64_t)count > ctx->block_count - lba)
        return KSHIM_STORAGE_RANGE;
    if (lba > (uint64_t)INT64_MAX / ctx->block_size)
        return KSHIM_STORAGE_OVERFLOW;

    offset = lba * ctx->block_size;
    bytes = (uint64_t)count * ctx->block_size;
    if (bytes > (uint64_t)INT64_MAX - offset ||
        bytes > (uint64_t)SIZE_MAX)
        return KSHIM_STORAGE_OVERFLOW;

    *offset_out = offset;
    *bytes_out = (size_t)bytes;
    return KSHIM_STORAGE_OK;
}

static int update_ab_read(void *opaque, uint64_t lba, uint32_t count,
                          void *buffer)
{
    struct update_ab_fd_ctx *ctx = opaque;
    uint64_t offset = 0;
    size_t bytes = 0;
    size_t done = 0;
    int rc;

    rc = update_ab_validate_range(ctx, lba, count, buffer, &offset, &bytes);
    if (rc != KSHIM_STORAGE_OK || bytes == 0)
        return rc;

    while (done < bytes) {
        size_t request = bytes - done;
        ssize_t got;

        if (request > (size_t)SSIZE_MAX)
            request = (size_t)SSIZE_MAX;
        got = pread(ctx->fd, (unsigned char *)buffer + done, request,
                    (off_t)(offset + done));
        if (got < 0) {
            if (errno == EINTR)
                continue;
            return KSHIM_STORAGE_IO;
        }
        if (got == 0)
            return KSHIM_STORAGE_IO;
        done += (size_t)got;
    }
    return KSHIM_STORAGE_OK;
}

static int update_ab_write(void *opaque, uint64_t lba, uint32_t count,
                           const void *buffer)
{
    struct update_ab_fd_ctx *ctx = opaque;
    uint64_t offset = 0;
    size_t bytes = 0;
    size_t done = 0;
    int rc;

    rc = update_ab_validate_range(ctx, lba, count, buffer, &offset, &bytes);
    if (rc != KSHIM_STORAGE_OK || bytes == 0)
        return rc;

    while (done < bytes) {
        size_t request = bytes - done;
        ssize_t put;

        if (request > (size_t)SSIZE_MAX)
            request = (size_t)SSIZE_MAX;
        put = pwrite(ctx->fd, (const unsigned char *)buffer + done, request,
                     (off_t)(offset + done));
        if (put < 0) {
            if (errno == EINTR)
                continue;
            return KSHIM_STORAGE_IO;
        }
        if (put == 0)
            return KSHIM_STORAGE_IO;
        done += (size_t)put;
    }
    return KSHIM_STORAGE_OK;
}

static int update_ab_flush(void *opaque)
{
    struct update_ab_fd_ctx *ctx = opaque;

    if (ctx == NULL || ctx->fd < 0)
        return KSHIM_STORAGE_ARG;

    while (fsync(ctx->fd) != 0) {
        if (errno == EINTR)
            continue;
        return KSHIM_STORAGE_IO;
    }
    return KSHIM_STORAGE_OK;
}

static int update_ab_get_boot_lun(void *opaque, uint32_t *boot_lun)
{
    const struct update_ab_media_ctx *ctx = opaque;

    if (ctx == NULL || boot_lun == NULL)
        return KSHIM_STORAGE_ARG;
    if (ctx->bsg_fd >= 0) {
        if (armada_ufs_get_boot_lun(ctx->bsg_fd, boot_lun) != 0)
            return KSHIM_STORAGE_IO;
        return KSHIM_STORAGE_OK;
    }
    *boot_lun = ctx->enabled_boot_id;
    return KSHIM_STORAGE_OK;
}

static int update_ab_set_boot_lun(void *opaque, uint32_t requested)
{
    const struct update_ab_media_ctx *ctx = opaque;
    uint32_t actual = 0;

    if (ctx == NULL || ctx->bsg_fd < 0 ||
        (requested != 1u && requested != 2u))
        return KSHIM_STORAGE_ARG;
    if (armada_ufs_get_boot_lun(ctx->bsg_fd, &actual) != 0)
        return KSHIM_STORAGE_IO;
    if (armada_ufs_set_boot_lun(ctx->bsg_fd, actual, requested) != 0)
        return KSHIM_STORAGE_IO;
    return KSHIM_STORAGE_OK;
}

static int update_ab_geometry(uint32_t block_size, uint64_t block_count,
                              uint64_t *bytes)
{
    if ((block_size != 512u && block_size != 4096u) || block_count < 10u ||
        block_count > (uint64_t)INT64_MAX / block_size)
        return KSHIM_STORAGE_ARG;
    *bytes = block_count * block_size;
    return KSHIM_STORAGE_OK;
}

static int update_ab_same_identity(const struct stat *a,
                                   const struct stat *b)
{
    if (S_ISBLK(a->st_mode) && S_ISBLK(b->st_mode))
        return a->st_rdev == b->st_rdev;
    if (S_ISREG(a->st_mode) && S_ISREG(b->st_mode))
        return a->st_dev == b->st_dev && a->st_ino == b->st_ino;
    return 0;
}

static int update_ab_init_media(const int fds[KSHIM_AB_LUNS],
                                const uint32_t block_sizes[KSHIM_AB_LUNS],
                                const uint64_t block_counts[KSHIM_AB_LUNS],
                                const uint8_t boot_ids[KSHIM_AB_LUNS],
                                int rw,
                                uint32_t enabled_boot_id,
                                int bsg_fd,
                                struct update_ab_fd_ctx fd_ctx[KSHIM_AB_LUNS],
                                struct update_ab_media_ctx *media_ctx,
                                struct kshim_ab_media *media)
{
    struct stat stats[KSHIM_AB_LUNS];
    int expected_accmode = rw ? O_RDWR : O_RDONLY;
    int rc;
    unsigned i;

    if (fds == NULL || block_sizes == NULL || block_counts == NULL ||
        boot_ids == NULL || fd_ctx == NULL || media_ctx == NULL ||
        media == NULL || (enabled_boot_id != 1u && enabled_boot_id != 2u))
        return KSHIM_STORAGE_ARG;

    for (i = 0; i < KSHIM_AB_LUNS; ++i) {
        uint64_t bytes;
        int flags;
        unsigned j;

        if (fds[i] < 0 || boot_ids[i] > 2u)
            return KSHIM_STORAGE_ARG;
        rc = update_ab_geometry(block_sizes[i], block_counts[i], &bytes);
        if (rc != KSHIM_STORAGE_OK)
            return rc;

        flags = fcntl(fds[i], F_GETFL);
        if (flags < 0)
            return KSHIM_STORAGE_IO;
#ifdef O_PATH
        if ((flags & O_PATH) != 0)
            return KSHIM_STORAGE_ARG;
#endif
        if ((flags & O_ACCMODE) != expected_accmode)
            return KSHIM_STORAGE_ARG;
        if (fstat(fds[i], &stats[i]) != 0)
            return KSHIM_STORAGE_IO;

        if (S_ISREG(stats[i].st_mode)) {
            if (stats[i].st_size < 0 ||
                (uint64_t)stats[i].st_size != bytes)
                return KSHIM_STORAGE_ARG;
        } else if (S_ISBLK(stats[i].st_mode)) {
            int logical_size = 0;
            uint64_t device_bytes = 0;

            if (ioctl(fds[i], BLKSSZGET, &logical_size) != 0 ||
                ioctl(fds[i], BLKGETSIZE64, &device_bytes) != 0)
                return KSHIM_STORAGE_IO;
            if (logical_size < 0 ||
                (uint32_t)logical_size != block_sizes[i] ||
                device_bytes != bytes)
                return KSHIM_STORAGE_ARG;
        } else {
            return KSHIM_STORAGE_ARG;
        }

        for (j = 0; j < i; ++j) {
            if (update_ab_same_identity(&stats[i], &stats[j]))
                return KSHIM_STORAGE_ARG;
        }

        fd_ctx[i].fd = fds[i];
        fd_ctx[i].block_size = block_sizes[i];
        fd_ctx[i].block_count = block_counts[i];
        media->lun[i].ctx = &fd_ctx[i];
        media->lun[i].block_size = block_sizes[i];
        media->lun[i].block_count = block_counts[i];
        media->lun[i].read = update_ab_read;
        media->lun[i].write = rw ? update_ab_write : NULL;
        media->lun[i].flush = rw ? update_ab_flush : NULL;
        media->boot_id[i] = boot_ids[i];
    }

    media_ctx->enabled_boot_id = enabled_boot_id;
    media_ctx->bsg_fd = rw ? bsg_fd : -1;
    media->ctx = media_ctx;
    media->get_boot_lun = update_ab_get_boot_lun;
    media->set_boot_lun = rw ? update_ab_set_boot_lun : NULL;
    return KSHIM_STORAGE_OK;
}

__attribute__((visibility("default")))
int armada_update_ab_inspect_fds(const int fds[6],
                                 const uint32_t block_sizes[6],
                                 const uint64_t block_counts[6],
                                 const uint8_t boot_ids[6],
                                 uint32_t enabled_boot_id,
                                 uint32_t result[3])
{
    struct update_ab_fd_ctx fd_ctx[KSHIM_AB_LUNS] = {{0}};
    struct update_ab_media_ctx media_ctx = {0, -1};
    struct kshim_ab_media media = {0};
    struct kshim_ab_status status;
    int rc;

    if (result == NULL)
        return KSHIM_STORAGE_ARG;

    rc = update_ab_init_media(fds, block_sizes, block_counts, boot_ids,
                              0, enabled_boot_id, -1,
                              fd_ctx, &media_ctx, &media);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    rc = kshim_ab_inspect(&media, &status);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    result[0] = (uint32_t)status.current;
    result[1] = (uint32_t)status.bootable[0];
    result[2] = (uint32_t)status.bootable[1];
    return KSHIM_STORAGE_OK;
}

/* Read-only partition extent/UUID lookup by label: result is
 * [lun, first_lba, block_count] with block_count = last_lba-first_lba+1,
 * plus the partition GUID in canonical RFC 4122 byte order. Reuses the
 * validated six-LUN GPT parser via kshim_ab_find_partition; never writes.
 * Outputs are left unchanged on any error. */
__attribute__((visibility("default")))
int armada_update_ab_partition_fds(const int fds[6],
                                   const uint32_t block_sizes[6],
                                   const uint64_t block_counts[6],
                                   const uint8_t boot_ids[6],
                                   uint32_t enabled_boot_id,
                                   const char *label,
                                   uint64_t result[3],
                                   uint8_t uuid[16])
{
    struct update_ab_fd_ctx fd_ctx[KSHIM_AB_LUNS] = {{0}};
    struct update_ab_media_ctx media_ctx = {0, -1};
    struct kshim_ab_media media = {0};
    struct kshim_partition part;
    unsigned lun = 0;
    int rc;

    if (fds == NULL || block_sizes == NULL || block_counts == NULL ||
        boot_ids == NULL || label == NULL || result == NULL || uuid == NULL)
        return KSHIM_STORAGE_ARG;

    rc = update_ab_init_media(fds, block_sizes, block_counts, boot_ids,
                              0, enabled_boot_id, -1,
                              fd_ctx, &media_ctx, &media);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    rc = kshim_ab_find_partition(&media, label, &lun, &part);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (part.last_lba < part.first_lba ||
        part.last_lba - part.first_lba == UINT64_MAX)
        return KSHIM_STORAGE_OVERFLOW;

    result[0] = (uint64_t)lun;
    result[1] = part.first_lba;
    result[2] = part.last_lba - part.first_lba + 1;
    memcpy(uuid, part.uuid, 16);
    return KSHIM_STORAGE_OK;
}

__attribute__((visibility("default")))
int armada_update_ab_select_fds(const int fds[6],
                                const uint32_t sizes[6],
                                const uint64_t counts[6],
                                const uint8_t boot_ids[6],
                                int bsg_fd,
                                uint32_t target,
                                uint32_t expected_current,
                                uint32_t result[3])
{
    struct update_ab_fd_ctx fd_ctx[KSHIM_AB_LUNS] = {{0}};
    struct update_ab_media_ctx media_ctx = {0, -1};
    struct kshim_ab_media media = {0};
    struct kshim_ab_status status;
    struct stat bsg_stat;
    uint32_t enabled_boot_id = 0;
    int bsg_flags;
    int rc;

    if (fds == NULL || sizes == NULL || counts == NULL ||
        boot_ids == NULL || result == NULL || bsg_fd < 0 ||
        target > 1u || expected_current > 1u)
        return KSHIM_STORAGE_ARG;

    bsg_flags = fcntl(bsg_fd, F_GETFL);
    if (bsg_flags < 0)
        return KSHIM_STORAGE_IO;
#ifdef O_PATH
    if ((bsg_flags & O_PATH) != 0)
        return KSHIM_STORAGE_ARG;
#endif
    if ((bsg_flags & O_ACCMODE) != O_RDWR)
        return KSHIM_STORAGE_ARG;
    if (fstat(bsg_fd, &bsg_stat) != 0)
        return KSHIM_STORAGE_IO;
    if (!S_ISCHR(bsg_stat.st_mode))
        return KSHIM_STORAGE_ARG;

    if (armada_ufs_get_boot_lun(bsg_fd, &enabled_boot_id) != 0)
        return KSHIM_STORAGE_IO;
    if (enabled_boot_id != 1u && enabled_boot_id != 2u)
        return KSHIM_STORAGE_FORMAT;

    rc = update_ab_init_media(fds, sizes, counts, boot_ids,
                              1, enabled_boot_id, bsg_fd,
                              fd_ctx, &media_ctx, &media);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    rc = kshim_ab_inspect(&media, &status);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (status.current != expected_current ||
        !status.bootable[0] || !status.bootable[1])
        return KSHIM_STORAGE_CONFLICT;

    rc = kshim_ab_select(&media, target, expected_current);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    rc = kshim_ab_inspect(&media, &status);
    if (rc != KSHIM_STORAGE_OK ||
        status.current != target ||
        !status.bootable[0] || !status.bootable[1])
        return KSHIM_AB_UNCERTAIN;

    result[0] = (uint32_t)status.current;
    result[1] = (uint32_t)status.bootable[0];
    result[2] = (uint32_t)status.bootable[1];
    return KSHIM_STORAGE_OK;
}

__attribute__((visibility("default")))
int armada_update_ab_mark_success_fds(const int fds[6],
                                      const uint32_t sizes[6],
                                      const uint64_t counts[6],
                                      const uint8_t boot_ids[6],
                                      int bsg_fd,
                                      uint32_t expected_current,
                                      uint32_t result[3])
{
    struct update_ab_fd_ctx fd_ctx[KSHIM_AB_LUNS] = {{0}};
    struct update_ab_media_ctx media_ctx = {0, -1};
    struct kshim_ab_media media = {0};
    struct kshim_ab_status status;
    struct stat bsg_stat;
    uint32_t enabled_boot_id = 0;
    int bsg_flags;
    int rc;

    if (fds == NULL || sizes == NULL || counts == NULL ||
        boot_ids == NULL || result == NULL || bsg_fd < 0 ||
        expected_current > 1u)
        return KSHIM_STORAGE_ARG;

    bsg_flags = fcntl(bsg_fd, F_GETFL);
    if (bsg_flags < 0)
        return KSHIM_STORAGE_IO;
#ifdef O_PATH
    if ((bsg_flags & O_PATH) != 0)
        return KSHIM_STORAGE_ARG;
#endif
    if ((bsg_flags & O_ACCMODE) != O_RDWR)
        return KSHIM_STORAGE_ARG;
    if (fstat(bsg_fd, &bsg_stat) != 0)
        return KSHIM_STORAGE_IO;
    if (!S_ISCHR(bsg_stat.st_mode))
        return KSHIM_STORAGE_ARG;

    if (armada_ufs_get_boot_lun(bsg_fd, &enabled_boot_id) != 0)
        return KSHIM_STORAGE_IO;
    if (enabled_boot_id != 1u && enabled_boot_id != 2u)
        return KSHIM_STORAGE_FORMAT;

    rc = update_ab_init_media(fds, sizes, counts, boot_ids,
                              1, enabled_boot_id, bsg_fd,
                              fd_ctx, &media_ctx, &media);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    media.set_boot_lun = NULL;

    rc = kshim_ab_inspect(&media, &status);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    if (status.current != expected_current ||
        !status.bootable[expected_current])
        return KSHIM_STORAGE_CONFLICT;

    rc = kshim_ab_mark_success(&media, expected_current);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    rc = kshim_ab_inspect(&media, &status);
    if (rc != KSHIM_STORAGE_OK ||
        status.current != expected_current ||
        !status.bootable[expected_current])
        return KSHIM_AB_UNCERTAIN;

    result[0] = (uint32_t)status.current;
    result[1] = (uint32_t)status.bootable[0];
    result[2] = (uint32_t)status.bootable[1];
    return KSHIM_STORAGE_OK;
}
