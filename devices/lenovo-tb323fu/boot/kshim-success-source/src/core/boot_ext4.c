/* boot_ext4_file.c — kShimC ext4 read backend, lowest-level file-side helpers.
 * Freestanding, NOT executed here. Compiled only on V100 Docker build host.
 * No public wrapper / readdir / extent yet (appended later to same TU).
 */
#include <string.h>
#include "boot_ext4.h"
#include "storage_block.h"

struct boot_volume {
    const struct kshim_blockdev   *dev;
    const struct kshim_partition  *part;
    uint8_t                       *io;      /* caller-provided >= 4096 scratch */
    uint64_t                       blocks;  /* fs block count (lo|hi) */
    uint32_t                       blocks_per_group;
    uint32_t                       inodes_per_group;
    uint32_t                       inodes;
    uint32_t                       groups;
    uint32_t                       sectors_per_block; /* 4096 / dev.block_size */
    uint16_t                       descriptor_size;   /* 32 or 64 */
    uint8_t                        uuid[16];          /* s_uuid @ sb+0x68 */
};

static inline uint16_t le16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

/* ---- volume_open -------------------------------------------------------
 * Validate partition geometry, read the first 4096 bytes of the fs, parse
 * the superblock at offset 1024, fill *v. Read-only; no journal replay.
 * Returns KSHIM_STORAGE_OK or ARG/FORMAT/RANGE/IO.
 */
static int volume_open(struct boot_volume *v,
                       const struct kshim_blockdev *dev,
                       const struct kshim_partition *part,
                       uint8_t *io)
{
    uint64_t dev_bs, part_first, part_last, part_sectors, fs_sectors;
    uint32_t incompat, rocompat;
    const uint8_t *sb;
    int rc;

    if (!v || !dev || !part || !io)
        return KSHIM_STORAGE_ARG;
    if (dev->block_size != 512 && dev->block_size != 4096)
        return KSHIM_STORAGE_ARG;

    dev_bs       = dev->block_size;
    part_first   = part->first_lba;
    part_last    = part->last_lba;
    if (part_first > part_last)
        return KSHIM_STORAGE_RANGE;
    if (part_last >= dev->block_count)
        return KSHIM_STORAGE_RANGE;

    /* partition must hold at least one full 4096 fs block */
    part_sectors = part_last - part_first + 1;
    if (part_sectors < 4096u / dev_bs)
        return KSHIM_STORAGE_RANGE;

    /* initial read: one whole FS block worth of device sectors */
    memset(v, 0, sizeof(*v));
    v->dev     = dev;
    v->part    = part;
    v->io      = io;
    v->sectors_per_block = (uint32_t)(4096u / dev_bs);

    rc = kshim_block_read(dev, part_first, v->sectors_per_block, io);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    sb = io + 1024; /* superblock within the first fs block */

    if (le16(sb + 0x38) != 0xEF53u)              /* s_magic */
        return KSHIM_STORAGE_FORMAT;
    if (le32(sb + 0x18) != 2u)                   /* 1024 << log_block_size */
        return KSHIM_STORAGE_FORMAT;
    if (le16(sb + 0x58) != 256u)                 /* s_inode_size */
        return KSHIM_STORAGE_FORMAT;
    if (le32(sb + 0x14) != 0u)                   /* s_first_data_block */
        return KSHIM_STORAGE_FORMAT;

    incompat = le32(sb + 0x60);
    rocompat = le32(sb + 0x64);

    /* required: FILETYPE(0x2) + EXTENTS(0x40); allowed extras only. */
    if ((incompat & 0x2u) == 0u || (incompat & 0x40u) == 0u)
        return KSHIM_STORAGE_FORMAT;
    {
        uint32_t allowed = 0x2u | 0x40u | 0x4u | 0x80u | 0x200u | 0x2000u;
        if (incompat & ~allowed)
            return KSHIM_STORAGE_FORMAT;
    }
    if (rocompat & 0x200u)                       /* bigalloc */
        return KSHIM_STORAGE_FORMAT;

    v->inodes = le32(sb + 0x00);                 /* s_inodes_count */
    v->blocks = (uint64_t)le32(sb + 0x04);       /* s_blocks_count_lo */
    if (incompat & 0x80u)                        /* 64BIT descriptor */
        v->blocks |= (uint64_t)le32(sb + 0x150) << 32;

    v->blocks_per_group = le32(sb + 0x20);
    v->inodes_per_group = le32(sb + 0x28);
    if (v->blocks_per_group == 0u || v->inodes_per_group == 0u)
        return KSHIM_STORAGE_FORMAT;
    if (v->inodes == 0u || v->blocks == 0u)
        return KSHIM_STORAGE_FORMAT;

    /* fs blocks must fit inside partition sectors / sectors_per_block */
    fs_sectors = part_sectors / v->sectors_per_block;
    if (v->blocks > fs_sectors)
        return KSHIM_STORAGE_RANGE;

    /* group count without overflow */
    uint64_t group_count = 1ull + ((uint64_t)v->blocks - 1ull) / v->blocks_per_group;
    if (group_count > UINT32_MAX)
        return KSHIM_STORAGE_FORMAT;
    v->groups = (uint32_t)group_count;

    if (incompat & 0x80u) {
        uint16_t ds = le16(sb + 0xFE);
        if (ds != 64u)
            return KSHIM_STORAGE_FORMAT;
        v->descriptor_size = 64u;
    } else {
        v->descriptor_size = 32u;
    }

    memcpy(v->uuid, sb + 0x68, 16u);   /* s_uuid */
    return KSHIM_STORAGE_OK;
}

/* ---- volume_block ------------------------------------------------------
 * Read one 4096 fs block into *out. block must be < v->blocks.
 * Multiplier cannot overflow because volume_open bounded blocks by
 * partition sectors / sectors_per_block.
 */
static int volume_block(const struct boot_volume *v, uint64_t block, void *out)
{
    uint64_t begin;
    int rc;

    if (!v || !out || !v->dev)
        return KSHIM_STORAGE_ARG;
    if (block >= v->blocks)
        return KSHIM_STORAGE_RANGE;

    begin = v->part->first_lba + block * (uint64_t)v->sectors_per_block;
    if (begin < v->part->first_lba || begin > v->part->last_lba)
        return KSHIM_STORAGE_RANGE;

    rc = kshim_block_read(v->dev, begin, v->sectors_per_block, out);
    if (rc != KSHIM_STORAGE_OK)
        return rc;
    return KSHIM_STORAGE_OK;
}

/* ---- volume_inode ------------------------------------------------------
 * Fetch inode_nr (1-based) as 256 raw bytes. Uses group descriptor table
 * starting at fs block 1 (first_data_block == 0); descriptors never cross
 * a 4K block boundary (16 x 256 for 64B desc). No checksum verification
 * here — that is a higher layer's job.
 */
static int volume_inode(const struct boot_volume *v, uint32_t inode_nr,
                        uint8_t inode[256])
{
    uint32_t group, index;
    uint64_t desc_off, inode_off;
    uint32_t table_lo, table_hi = 0;
    uint64_t itable;
    const uint8_t *d;
    int rc;

    if (!v || !inode || !v->io)
        return KSHIM_STORAGE_ARG;
    if (inode_nr == 0u || inode_nr > v->inodes)
        return KSHIM_STORAGE_RANGE;

    group = (inode_nr - 1u) / v->inodes_per_group;
    if (group >= v->groups)
        return KSHIM_STORAGE_RANGE;
    index = (inode_nr - 1u) % v->inodes_per_group;

    /* group descriptors live from fs block 1, packed, no cross-block */
    desc_off = (uint64_t)group * (uint64_t)v->descriptor_size;
    rc = volume_block(v, 1u + (uint64_t)(desc_off / 4096u), v->io);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    d = v->io + (desc_off % 4096u);

    table_lo = le32(d + 8);                     /* bg_inode_table_lo */
    if (v->descriptor_size == 64u)
        table_hi = le32(d + 40);                /* bg_inode_table_hi */
    itable = (uint64_t)table_lo | ((uint64_t)table_hi << 32);
    if (itable == 0u || itable >= v->blocks)
        return KSHIM_STORAGE_FORMAT;

    /* 256-byte inodes, 16 per 4K block. inode_off cannot overflow: index
     * < inodes_per_group, and 16*256 < 4096 ensures index/16 < 4K. */
    inode_off = (uint64_t)index * 256ull;
    rc = volume_block(v, itable + (uint64_t)(inode_off / 4096u), v->io);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    memcpy(inode, v->io + (inode_off % 4096u), 256u);
    return KSHIM_STORAGE_OK;
}

/*
 * ext4 extent-mapped read-only helpers.
 * Appended to boot_ext4.c.  NOT RUN as part of any build/test here.
 * Read-only; no writes to dev/part, no machine or release access.
 */

/* ---- 1) private extent_map ------------------------------------------- */

static int extent_map(struct boot_volume *v, const uint8_t inode[256],
                      uint64_t logical, uint64_t *physical_out)
{
    const uint32_t EXTENTS_FL = 0x00080000u;
    const uint32_t INLINE_FL  = 0x10000000u;
    const uint16_t EXT_MAGIC  = 0xF30Au;
    const int MAX_DEPTH = 4;

    uint32_t i_flags = (uint32_t)inode[32] | ((uint32_t)inode[33] << 8) |
                       ((uint32_t)inode[34] << 16) | ((uint32_t)inode[35] << 24);

    if (!(i_flags & EXTENTS_FL))
        return KSHIM_STORAGE_NOT_FOUND;      /* not extent mapped */
    if (i_flags & INLINE_FL)
        return KSHIM_STORAGE_NOT_FOUND;      /* inline data unsupported */

    /* extent root header at inode+40, 60 bytes total: 12 hdr + 4*12 */
    const uint8_t *root = inode + 40;
    uint16_t magic = (uint16_t)(root[0] | (root[1] << 8));
    uint16_t ents  = (uint16_t)(root[2] | (root[3] << 8));
    uint16_t max   = (uint16_t)(root[4] | (root[5] << 8));
    uint16_t depth = (uint16_t)(root[6] | (root[7] << 8));

    if (magic != EXT_MAGIC)
        return KSHIM_STORAGE_FORMAT;
    if (max > 4 || ents > max)
        return KSHIM_STORAGE_FORMAT;
    if (depth > (uint16_t)MAX_DEPTH)
        return KSHIM_STORAGE_FORMAT;

    const uint8_t *node = root;

    for (int level = 0; ; level++) {
        /* node points at a 12-byte header followed by `max` records */
        uint16_t nmagic = (uint16_t)(node[0] | (node[1] << 8));
        uint16_t nents  = (uint16_t)(node[2] | (node[3] << 8));
        uint16_t nmax   = (uint16_t)(node[4] | (node[5] << 8));
        uint16_t ndepth = (uint16_t)(node[6] | (node[7] << 8));

        if (nmagic != EXT_MAGIC)
            return KSHIM_STORAGE_FORMAT;
        if ((level == 0 && nmax > 4) || (level != 0 && nmax > 340) ||
            nents > nmax)
            return KSHIM_STORAGE_FORMAT;
        if (ndepth != (uint16_t)(depth - (uint16_t)level))
            return KSHIM_STORAGE_FORMAT;

        if (nents == 0)
            return (level == 0) ? KSHIM_STORAGE_EMPTY : KSHIM_STORAGE_FORMAT;

        const uint8_t *recs = node + 12;

        /* pick entry with greatest ei_block <= logical */
        int chosen = -1;
        for (uint16_t i = 0; i < nents; i++) {
            const uint8_t *r = recs + (size_t)i * 12;
            uint32_t ei_block = (uint32_t)r[0] | ((uint32_t)r[1] << 8) |
                                ((uint32_t)r[2] << 16) | ((uint32_t)r[3] << 24);
            if ((uint64_t)ei_block <= logical)
                chosen = (int)i;
        }
        if (chosen < 0)
            return KSHIM_STORAGE_EMPTY;

        const uint8_t *r = recs + (size_t)chosen * 12;

        if (ndepth == 0) {
            /* leaf record: ee_block, ee_len, ee_start_hi, ee_start_lo */
            uint32_t ee_block = (uint32_t)r[0] | ((uint32_t)r[1] << 8) |
                                ((uint32_t)r[2] << 16) | ((uint32_t)r[3] << 24);
            uint16_t ee_len   = (uint16_t)(r[4] | (r[5] << 8));
            uint16_t ee_hi    = (uint16_t)(r[6] | (r[7] << 8));
            uint32_t ee_lo    = (uint32_t)r[8] | ((uint32_t)r[9] << 8) |
                                ((uint32_t)r[10] << 16) | ((uint32_t)r[11] << 24);

            /* Linux ext4: ee_len 0 invalid; 1..32768 initialized;
               >32768 uninitialized, with count = raw - 32768. */
            if (ee_len == 0)
                return KSHIM_STORAGE_FORMAT;

            int unwritten = 0;
            uint32_t run_len;
            if (ee_len <= 32768) {
                run_len = (uint32_t)ee_len;
            } else {
                unwritten = 1;
                run_len = (uint32_t)ee_len - 32768u;
            }
            if (run_len == 0)
                return KSHIM_STORAGE_FORMAT;

            uint64_t end = (uint64_t)ee_block + (uint64_t)run_len; /* exclusive */
            if (logical >= end)
                return KSHIM_STORAGE_EMPTY;   /* hole beyond this extent */

            uint64_t phys = ((uint64_t)ee_hi << 32) | (uint64_t)ee_lo;
            uint64_t off  = logical - (uint64_t)ee_block;
            if (phys + off < phys)                /* overflow */
                return KSHIM_STORAGE_RANGE;
            uint64_t p = phys + off;
            if (p >= v->blocks)
                return KSHIM_STORAGE_RANGE;

            if (unwritten)
                return KSHIM_STORAGE_EMPTY;

            *physical_out = p;
            return KSHIM_STORAGE_OK;
        }

        /* index record: ei_block, ei_leaf_lo(+4), ei_leaf_hi(+8) */
        uint32_t ei_block = (uint32_t)r[0] | ((uint32_t)r[1] << 8) |
                            ((uint32_t)r[2] << 16) | ((uint32_t)r[3] << 24);
        uint32_t leaf_lo  = (uint32_t)r[4] | ((uint32_t)r[5] << 8) |
                            ((uint32_t)r[6] << 16) | ((uint32_t)r[7] << 24);
        uint16_t leaf_hi  = (uint16_t)(r[8] | (r[9] << 8));

        (void)ei_block; /* bounded by selection above */
        uint64_t child = ((uint64_t)leaf_hi << 32) | (uint64_t)leaf_lo;
        if (child >= v->blocks)
            return KSHIM_STORAGE_RANGE;

        int rc = volume_block(v, child, v->io);
        if (rc != KSHIM_STORAGE_OK)
            return rc;


        node = v->io;
    }
}

/* ---- 2) dir_lookup --------------------------------------------------- */

static int dir_lookup(struct boot_volume *v, const uint8_t inode[256],
                      const char *name, uint32_t *inode_out)
{
    const uint32_t DIR_FMT      = 0x00004000u;
    const uint64_t MAX_DIR_SIZE = 1024ull * 1024ull;

    uint32_t mode = (uint32_t)inode[0] | ((uint32_t)inode[1] << 8);
    if ((mode & 0xF000u) != DIR_FMT)
        return KSHIM_STORAGE_NOT_FOUND;

    uint32_t size_lo = (uint32_t)inode[4] | ((uint32_t)inode[5] << 8) |
                       ((uint32_t)inode[6] << 16) | ((uint32_t)inode[7] << 24);
    uint32_t size_hi = (uint32_t)inode[108] | ((uint32_t)inode[109] << 8) |
                       ((uint32_t)inode[110] << 16) | ((uint32_t)inode[111] << 24);
    uint64_t fsize = ((uint64_t)size_hi << 32) | (uint64_t)size_lo;

    if (fsize == 0)
        return KSHIM_STORAGE_NOT_FOUND;
    if (fsize > MAX_DIR_SIZE)
        return KSHIM_STORAGE_FORMAT;

    /* directory entries must sit within whole 4096 blocks */
    uint64_t lblocks = (fsize + 4095ull) / 4096ull;
    size_t nlen = strlen(name);
    if (nlen == 0 || nlen > 255)
        return KSHIM_STORAGE_RANGE;

    for (uint64_t blk = 0; blk < lblocks; blk++) {
        uint64_t phys = 0;
        int rc = extent_map(v, inode, blk, &phys);
        if (rc != KSHIM_STORAGE_OK)
            return rc;             /* hole or unwritten: reject */

        rc = volume_block(v, phys, v->io);
        if (rc != KSHIM_STORAGE_OK)
            return rc;

        uint64_t block_size = 4096;
        /* only bytes actually inside the file for the final block */
        uint64_t block_valid = block_size;
        if (blk + 1 == lblocks) {
            uint64_t tail = fsize - blk * block_size;
            if (tail < block_valid)
                block_valid = tail;
        }

        uint64_t off = 0;
        while (off + 8 <= block_valid) {
            const uint8_t *e = v->io + off;
            uint32_t ino = (uint32_t)e[0] | ((uint32_t)e[1] << 8) |
                           ((uint32_t)e[2] << 16) | ((uint32_t)e[3] << 24);
            uint16_t rl  = (uint16_t)(e[4] | (e[5] << 8));
            uint8_t  nl  = e[6];

            if (rl < 8 || (rl & 3u) != 0)
                return KSHIM_STORAGE_FORMAT;
            if (off + rl > block_valid)
                return KSHIM_STORAGE_FORMAT; /* must not cross block */
            if (8u + (uint32_t)nl > (uint32_t)rl)
                return KSHIM_STORAGE_FORMAT;

            if (ino != 0) {
                /* validate '.' and '..' too, though they are never returned */
                const uint8_t *nm = e + 8;
                if (nl == 1 && nm[0] == '.') {
                    ; /* validated, not a requestable match */
                } else if (nl == 2 && nm[0] == '.' && nm[1] == '.') {
                    ;
                } else if ((size_t)nl == nlen && memcmp(nm, name, nlen) == 0) {
                    *inode_out = ino;
                    return KSHIM_STORAGE_OK;
                }
            }
            off += rl;
        }
    }
    return KSHIM_STORAGE_NOT_FOUND;
}

/* ---- 3) public kshim_ext4_read_file ---------------------------------- */

int kshim_ext4_read_file(const struct kshim_blockdev *dev,
                         const struct kshim_partition *part,
                         const char *absolute_path,
                         void *out, size_t capacity, size_t *bytes_out,
                         void *scratch, size_t scratch_bytes);

int kshim_ext4_read_file(const struct kshim_blockdev *dev,
                         const struct kshim_partition *part,
                         const char *absolute_path,
                         void *out, size_t capacity, size_t *bytes_out,
                         void *scratch, size_t scratch_bytes)
{
    const uint32_t REG_FMT     = 0x00008000u;
    const uint32_t LNK_FMT     = 0x0000A000u;
    const uint32_t INLINE_FL   = 0x10000000u;
    const uint32_t MAX_SYMLINK = 8;
    const size_t   PATH_BUF    = 1024;
    const size_t   IO_RESERVE  = 4096;
    const uint64_t MAX_FILE    = 4096ull * 1024ull * 1024ull; /* 4 GiB-ish */

    if (!bytes_out)
        return KSHIM_STORAGE_ARG;
    *bytes_out = 0;

    if (!dev || !part || !absolute_path || !out || !scratch)
        return KSHIM_STORAGE_ARG;
    if (absolute_path[0] != '/')
        return KSHIM_STORAGE_ARG;
    if (scratch_bytes < IO_RESERVE + 2 * PATH_BUF)
        return KSHIM_STORAGE_ARG;

    uint8_t *io = (uint8_t *)scratch;
    uint8_t *path_a = io + IO_RESERVE;
    uint8_t *path_b = path_a + PATH_BUF;

    size_t plen = strlen(absolute_path);
    if (plen == 0 || plen >= PATH_BUF)
        return KSHIM_STORAGE_RANGE;
    memcpy(path_a, absolute_path, plen + 1);

    /* reject any '..' component in the original request */
    for (size_t i = 0; i < plen; ) {
        while (i < plen && path_a[i] == '/') i++;
        size_t j = i;
        while (j < plen && path_a[j] != '/') j++;
        if (j - i == 2 && path_a[i] == '.' && path_a[i + 1] == '.')
            return KSHIM_STORAGE_ARG;
        i = j;
    }

    struct boot_volume v;
    int rc = volume_open(&v, dev, part, io);
    if (rc != KSHIM_STORAGE_OK)
        return rc;


    const struct boot_volume *vp = &v;

    uint32_t cur_ino = 2;                 /* root, standard ext4 */
    uint8_t  inode[256];
    unsigned symlinks = 0;
    uint8_t *cur_path = path_a;
    uint8_t *nxt_path = path_b;

    size_t pos = 0;                       /* cursor into cur_path */

    for (;;) {
        while (pos < PATH_BUF && cur_path[pos] == '/')
            pos++;

        if (cur_path[pos] == '\0') {
            /* final component resolves to cur_ino: must be regular file */
            rc = volume_inode(vp, cur_ino, inode);
            if (rc != KSHIM_STORAGE_OK)
                return rc;

            uint32_t mode = (uint32_t)inode[0] | ((uint32_t)inode[1] << 8);
            if ((mode & 0xF000u) != REG_FMT)
                return KSHIM_STORAGE_NOT_FOUND;

            uint32_t fl = (uint32_t)inode[32] | ((uint32_t)inode[33] << 8) |
                          ((uint32_t)inode[34] << 16) | ((uint32_t)inode[35] << 24);
            if (fl & INLINE_FL)
                return KSHIM_STORAGE_NOT_FOUND;

            uint32_t s_lo = (uint32_t)inode[4] | ((uint32_t)inode[5] << 8) |
                            ((uint32_t)inode[6] << 16) | ((uint32_t)inode[7] << 24);
            uint32_t s_hi = (uint32_t)inode[108] | ((uint32_t)inode[109] << 8) |
                            ((uint32_t)inode[110] << 16) | ((uint32_t)inode[111] << 24);
            uint64_t fsize = ((uint64_t)s_hi << 32) | (uint64_t)s_lo;

            if (fsize > MAX_FILE)
                return KSHIM_STORAGE_RANGE;
            if (fsize > (uint64_t)capacity)
                return KSHIM_STORAGE_RANGE;

            uint8_t *dst = (uint8_t *)out;
            uint64_t remaining = fsize;
            uint64_t logical = 0;

            while (remaining > 0) {
                uint64_t chunk = 4096;
                if (chunk > remaining)
                    chunk = remaining;

                uint64_t phys = 0;
                rc = extent_map(&v, inode, logical, &phys);
                if (rc == KSHIM_STORAGE_EMPTY) {
                    memset(dst, 0, (size_t)chunk);
                } else if (rc == KSHIM_STORAGE_OK) {
                    rc = volume_block(&v, phys, v.io);
                    if (rc != KSHIM_STORAGE_OK)
                        return rc;
                    memcpy(dst, v.io, (size_t)chunk);
                } else {
                    return rc;
                }

                dst       += chunk;
                logical   += 1;
                remaining -= chunk;
            }

            *bytes_out = (size_t)fsize;
            return KSHIM_STORAGE_OK;
        }

        /* extract next component into name[] */
        char name[256];
        size_t k = 0;
        while (pos < PATH_BUF && cur_path[pos] != '/' &&
               cur_path[pos] != '\0') {
            if (k + 1 >= sizeof(name))
                return KSHIM_STORAGE_RANGE;
            name[k++] = (char)cur_path[pos++];
        }
        name[k] = '\0';
        if (k == 0)
            continue;

        if (k == 1 && name[0] == '.')
            continue;

        if (k == 2 && name[0] == '.' && name[1] == '.')
            return KSHIM_STORAGE_ARG; /* '..' introduced by symlink */

        rc = volume_inode(vp, cur_ino, inode);
        if (rc != KSHIM_STORAGE_OK)
            return rc;

        uint32_t child = 0;
        rc = dir_lookup(&v, inode, name, &child);
        if (rc != KSHIM_STORAGE_OK)
            return rc;

        rc = volume_inode(vp, child, inode);
        if (rc != KSHIM_STORAGE_OK)
            return rc;

        uint32_t mode = (uint32_t)inode[0] | ((uint32_t)inode[1] << 8);
        uint32_t fmt  = mode & 0xF000u;

        if (fmt == LNK_FMT) {
            if (++symlinks > MAX_SYMLINK)
                return KSHIM_STORAGE_FORMAT;

            uint32_t fl = (uint32_t)inode[32] | ((uint32_t)inode[33] << 8) |
                          ((uint32_t)inode[34] << 16) | ((uint32_t)inode[35] << 24);
            if (fl & INLINE_FL)
                return KSHIM_STORAGE_FORMAT;

            uint32_t l_lo = (uint32_t)inode[4] | ((uint32_t)inode[5] << 8) |
                            ((uint32_t)inode[6] << 16) | ((uint32_t)inode[7] << 24);
            if (l_lo == 0 || l_lo > 60)
                return KSHIM_STORAGE_FORMAT;

            uint32_t b_lo = (uint32_t)inode[28] | ((uint32_t)inode[29] << 8) |
                            ((uint32_t)inode[30] << 16) | ((uint32_t)inode[31] << 24);
            if (b_lo != 0)
                return KSHIM_STORAGE_FORMAT;

            /* build new path: target + remaining components */
            const uint8_t *tgt = inode + 40;   /* fast symlink data */
            size_t tlen = (size_t)l_lo;

            size_t remaining_len = 0;
            {
                size_t p = pos;
                if (cur_path[p] == '/')
                    p++;
                while (p < PATH_BUF && cur_path[p] != '\0')
                    remaining_len = ++p - pos;
            }

            size_t tstart_in_target = 0;
            memcpy(nxt_path, tgt, tlen);
            nxt_path[tlen] = '\0';

            /* reject '..' inside the link target itself */
            for (size_t i = 0; i < tlen; ) {
                while (i < tlen && nxt_path[i] == '/') i++;
                size_t j = i;
                while (j < tlen && nxt_path[j] != '/') j++;
                if (j - i == 2 && nxt_path[i] == '.' && nxt_path[i + 1] == '.')
                    return KSHIM_STORAGE_ARG;
                i = j;
            }

            if (tlen > 0 && nxt_path[0] != '/') {
                /* relative symlink: relative to current directory (cur_ino) */
                /* copy current absolute directory path up to the current
                 * component boundary */
                size_t dir_end = 0;
                {
                    size_t last_slash = 0;
                    /* stop before name[] began: pos points after name */
                    size_t name_start = pos - k;
                    /* use cur_path up to name_start */
                    last_slash = name_start;
                    while (last_slash > 0 && cur_path[last_slash - 1] != '/')
                        last_slash--;
                    dir_end = last_slash;
                }
                if (dir_end == 0)
                    dir_end = 1; /* root */

                if (dir_end + tlen + 1 > PATH_BUF)
                    return KSHIM_STORAGE_RANGE;

                uint8_t tmp[PATH_BUF];
                memcpy(tmp, cur_path, dir_end);
                if (tmp[dir_end - 1] != '/')
                    tmp[dir_end++] = '/';
                memcpy(tmp + dir_end, tgt, tlen);
                size_t nlen2 = dir_end + tlen;

                (void)tstart_in_target;
                if (nlen2 >= PATH_BUF)
                    return KSHIM_STORAGE_RANGE;
                memcpy(nxt_path, tmp, nlen2);
                nxt_path[nlen2] = '\0';
            }
            (void)remaining_len;

            /* append remaining components if any */
            {
                size_t nlen2 = strlen((char *)nxt_path);
                size_t p = pos;
                while (p < PATH_BUF) {
                    if (cur_path[p] == '\0')
                        break;
                    if (nlen2 + 1 >= PATH_BUF)
                        return KSHIM_STORAGE_RANGE;
                    nxt_path[nlen2++] = cur_path[p++];
                }
                if (nlen2 >= PATH_BUF)
                    return KSHIM_STORAGE_RANGE;
                nxt_path[nlen2] = '\0';
            }

            if (strlen((char *)nxt_path) == 0 ||
                nxt_path[0] != '/')
                return KSHIM_STORAGE_FORMAT;

            uint8_t *swap = cur_path;
            cur_path = nxt_path;
            nxt_path = swap;
            cur_ino = 2;
            pos = 0;
            continue;
        }

        if (fmt == REG_FMT) {
            /* regular file appearing before end of path -> not a dir */
            /* Peek whether we're at end of path */
            size_t p = pos;
            while (p < PATH_BUF && cur_path[p] == '/')
                p++;
            if (cur_path[p] != '\0')
                return KSHIM_STORAGE_NOT_FOUND;
            /* final component reached */
            cur_ino = child;
            pos = p;
            /* loop will handle termination on next iteration */
            continue;
        }

        if (fmt != 0x00004000u) {
            /* not a directory */
            return KSHIM_STORAGE_NOT_FOUND;
        }

        cur_ino = child;
        /* continue parsing remaining components */
    }
}

int kshim_ext4_read_uuid(const struct kshim_blockdev *dev,
                         const struct kshim_partition *part,
                         uint8_t out[16],
                         void *scratch, size_t scratch_bytes)
{
    const size_t IO_RESERVE = 4096;
    struct boot_volume v;
    int rc;

    if (!dev || !part || !out || !scratch)
        return KSHIM_STORAGE_ARG;
    if (scratch_bytes < IO_RESERVE)
        return KSHIM_STORAGE_ARG;

    rc = volume_open(&v, dev, part, (uint8_t *)scratch);
    if (rc != KSHIM_STORAGE_OK)
        return rc;

    memcpy(out, v.uuid, 16u);
    return KSHIM_STORAGE_OK;
}
