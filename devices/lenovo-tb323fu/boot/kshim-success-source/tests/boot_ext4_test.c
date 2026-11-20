/* SPDX-License-Identifier: GPL-3.0-only */
/*
 * Host test for kshim_ext4_read_file().
 *
 * argv[1] = path to a 4096-byte-block ext4 image (whole image == partition,
 *           first_lba = 0) produced externally by mke2fs.
 * argv[2] = path to the expected payload for /ostree/default-test/kernel.bin.
 *
 * Expects the image to contain:
 *   /ostree/default-test/kernel.bin   regular file, payload > 64 KiB so it
 *                                     spans multiple extents / blocks.
 *   /loader                           short symlink -> "/loader.0"
 *   /loader.0/entries/probe.bin       regular file with same payload.
 *
 * Production under test (header added by next round):
 *   int kshim_ext4_read_file(const struct kshim_blockdev *,
 *                            const struct kshim_partition *,
 *                            const char *path, void *output,
 *                            size_t capacity, size_t *bytes_out,
 *                            void *scratch, size_t scratch_bytes);
 *   KSHIM_EXT4_SCRATCH_BYTES == 16384
 *
 * Success: returns KSHIM_STORAGE_OK (0) and sets *bytes_out.
 * Failure: returns nonzero and *bytes_out == 0.
 *
 * No writes are performed; read-only test.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "storage_block.h"

/* ---- Declarations for the not-yet-existing production interface --------- */
#define KSHIM_EXT4_SCRATCH_BYTES 16384u

int kshim_ext4_read_file(const struct kshim_blockdev *bd,
                         const struct kshim_partition *part,
                         const char *path,
                         void *output,
                         size_t capacity,
                         size_t *bytes_out,
                         void *scratch,
                         size_t scratch_bytes);

/* ---- Test helpers ------------------------------------------------------- */

#define EXPECT(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        return 1; \
    } \
} while (0)

static int read_whole_file(const char *path, uint8_t **out, size_t *out_len)
{
    FILE *f = NULL;
    long sz = 0;
    uint8_t *buf = NULL;

    *out = NULL;
    *out_len = 0;
    f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        return 1;
    }
    if (fseek(f, 0, SEEK_END) != 0) { fclose(f); return 1; }
    sz = ftell(f);
    if (sz < 0) { fclose(f); return 1; }
    if (fseek(f, 0, SEEK_SET) != 0) { fclose(f); return 1; }
    buf = (uint8_t *)malloc((size_t)sz ? (size_t)sz : 1u);
    if (!buf) { fclose(f); return 1; }
    if ((size_t)sz > 0 && fread(buf, 1, (size_t)sz, f) != (size_t)sz) {
        free(buf);
        fclose(f);
        return 1;
    }
    fclose(f);
    *out = buf;
    *out_len = (size_t)sz;
    return 0;
}

/* ---- kshim_blockdev callbacks backed by an in-memory image ------------- */

struct file_dev {
    const uint8_t *data;
    size_t         len;
    uint64_t       sectors;      /* number of device sectors */
    uint32_t       sector_size;  /* 512 or 4096 */
    uint64_t       fail_lba;     /* injected I/O error at this LBA */
    int            has_fail;     /* whether fail_lba is active */
};

static int fd_read(void *ctx, uint64_t lba, uint32_t count, void *buffer)
{
    struct file_dev *fd = (struct file_dev *)ctx;
    uint64_t byte_off;
    uint64_t byte_len;

    if (!fd || !buffer) return -1;
    if (count == 0) return 0;
    if (fd->has_fail && lba == fd->fail_lba) return -1;

    byte_off = lba * (uint64_t)fd->sector_size;
    byte_len = (uint64_t)count * (uint64_t)fd->sector_size;

    if (byte_off > (uint64_t)fd->len) return -1;
    if (byte_len > (uint64_t)fd->len - byte_off) return -1;

    memcpy(buffer, fd->data + byte_off, (size_t)byte_len);
    return 0;
}

static int fd_write(void *ctx, uint64_t lba, uint32_t count, const void *buffer)
{
    (void)ctx; (void)lba; (void)count; (void)buffer;
    return -1; /* test is read-only */
}

static int fd_flush(void *ctx)
{
    (void)ctx;
    return 0;
}

static void fd_init(struct file_dev *fd,
                    const uint8_t *data, size_t len,
                    uint32_t sector_size)
{
    memset(fd, 0, sizeof(*fd));
    fd->data = data;
    fd->len = len;
    fd->sector_size = sector_size;
    fd->sectors = (uint64_t)len / (uint64_t)sector_size;
    fd->has_fail = 0;
}

static void fd_bind(struct kshim_blockdev *bd, struct file_dev *fd)
{
    memset(bd, 0, sizeof(*bd));
    bd->ctx = fd;
    bd->block_size = fd->sector_size;
    bd->block_count = fd->sectors;
    bd->read = fd_read;
    bd->write = fd_write;
    bd->flush = fd_flush;
}

/* Run one group of assertions against a given sector size. */
static int run_suite(uint32_t sector_size,
                     const uint8_t *img, size_t img_len,
                     const uint8_t *payload, size_t payload_len)
{
    struct file_dev fd;
    struct kshim_blockdev bd;
    struct kshim_partition part;
    uint8_t scratch[KSHIM_EXT4_SCRATCH_BYTES];
    uint8_t *out = NULL;
    size_t n = 0;
    int rc;

    /* Buffer large enough for the whole payload (+ slack). */
    out = (uint8_t *)malloc(payload_len + 64u);
    EXPECT(out != NULL);

    fd_init(&fd, img, img_len, sector_size);
    fd_bind(&bd, &fd);

    memset(&part, 0, sizeof(part));
    part.first_lba = 0;
    part.last_lba = fd.sectors ? fd.sectors - 1u : 0;

    /* (1) Successful read of the extent-spanning file (> 64 KiB). */
    memset(out, 0xAA, payload_len + 64u);
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/kernel.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc == KSHIM_STORAGE_OK);
    EXPECT(n == payload_len);
    EXPECT(memcmp(out, payload, payload_len) == 0);

    /* (1b) Exact-capacity read succeeds. */
    memset(out, 0xBB, payload_len + 64u);
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/kernel.bin",
                              out, payload_len,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc == KSHIM_STORAGE_OK);
    EXPECT(n == payload_len);
    EXPECT(memcmp(out, payload, payload_len) == 0);

    /* (2) Following the /loader symlink to /loader.0/entries/probe.bin. */
    memset(out, 0xCC, payload_len + 64u);
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/loader/entries/probe.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc == KSHIM_STORAGE_OK);
    EXPECT(n == payload_len);
    EXPECT(memcmp(out, payload, payload_len) == 0);

    /* (2b) Same via symlinked path spelled with a trailing component only. */
    memset(out, 0xDD, payload_len + 64u);
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "loader/entries/probe.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
EXPECT(rc != KSHIM_STORAGE_OK);
EXPECT(n == 0);
/* NOT RUN: relative path "loader/entries/probe.bin" must be rejected; interface accepts absolute paths only. */


    /* (3) Missing path: nonzero, bytes_out zeroed. */
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/does-not-exist.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    /* (4) Short buffer: nonzero, bytes_out zeroed (no partial data). */
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/kernel.bin",
                              out, payload_len - 1u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    /* (5) Path traversal with '..' must be rejected. */
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/../ostree/default-test/kernel.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/loader/../ostree/default-test/kernel.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    /* (6) Path that is not a regular file (directory and symlink). */
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/loader",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);

    /* (7) Injected I/O error on the first data LBA must surface as a
     *     non-OK result with bytes_out zeroed. Deliberately fail reads
     *     around inode/block start area; use LBA 0 which is the ext4
     *     superblock - the read will likely fail during metadata load. */
    fd.has_fail = 1;
    fd.fail_lba = 0;
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/kernel.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);
    fd.has_fail = 0;

    /* (7b) Injected I/O error on the first block after the superblock. */
    fd.has_fail = 1;
    fd.fail_lba = 4096u / sector_size; /* block 1 of the ext4 filesystem */
    n = 0xDEADBEEF;
    rc = kshim_ext4_read_file(&bd, &part,
                              "/ostree/default-test/kernel.bin",
                              out, payload_len + 64u,
                              &n, scratch, sizeof(scratch));
    EXPECT(rc != KSHIM_STORAGE_OK);
    EXPECT(n == 0);
    fd.has_fail = 0;

    free(out);
    return 0;
}

int main(int argc, char **argv)
{
    uint8_t *img = NULL;
    size_t img_len = 0;
    uint8_t *payload = NULL;
    size_t payload_len = 0;
    int rc = 1;

    if (argc != 3) {
        fprintf(stderr, "usage: %s <ext4-image> <expected-payload>\n",
                argv[0]);
        return 2;
    }

    if (read_whole_file(argv[1], &img, &img_len) != 0) return 2;
    if (read_whole_file(argv[2], &payload, &payload_len) != 0) {
        free(img);
        return 2;
    }

    /* Sanity: the ext4 image must be a multiple of 4096 bytes. */
    if (img_len == 0 || (img_len % 4096u) != 0) {
        fprintf(stderr, "image length %zu is not a multiple of 4096\n",
                img_len);
        goto out;
    }

    /* Payload must span more than 64 KiB to exercise multi-extent reads. */
    if (payload_len <= 64u * 1024u) {
        fprintf(stderr, "payload must be > 64 KiB, got %zu bytes\n",
                payload_len);
        goto out;
    }

    if (run_suite(512u, img, img_len, payload, payload_len) != 0) goto out;
    if (run_suite(4096u, img, img_len, payload, payload_len) != 0) goto out;

    printf("PASS\n");
    rc = 0;

out:
    free(img);
    free(payload);
    return rc;
}
