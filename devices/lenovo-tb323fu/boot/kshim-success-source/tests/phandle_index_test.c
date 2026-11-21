/* SPDX-License-Identifier: MIT */
/* Standalone regression for the indexed dt_phandle() lookup. Inside a
 * dt_parent_index_begin()/end() scope the sorted phandle index must answer
 * exactly like the full libfdt walk it replaces, keep every strict validation
 * rule, and never rescan the tree for bulk lookups. The fallback paths (scope
 * closed, other blob, deeper or larger tree) are compared against the same
 * independent reference walk. */

#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <libfdt.h>

int dt_validate(const void *fdt, size_t available);
void dt_parent_index_begin(const void *fdt);
void dt_parent_index_end(void);
int dt_phandle(const void *fdt, uint32_t handle);

/* The full walk in dt_phandle() is the only path that calls fdt_next_node(), so
 * this counter observes tree walking without looking at the implementation. */
extern int __real_fdt_next_node(const void *fdt, int offset, int *depth);
extern int __wrap_fdt_next_node(const void *fdt, int offset, int *depth);
static long g_walks;
int __wrap_fdt_next_node(const void *fdt, int offset, int *depth)
{
    ++g_walks;
    return __real_fdt_next_node(fdt, offset, depth);
}

#define BLOB_CAP (1u << 21)
#define MAX_NODES 16384

static _Alignas(64) uint8_t g_blob_a[BLOB_CAP];
static _Alignas(64) uint8_t g_blob_b[BLOB_CAP];
static _Alignas(64) uint8_t g_struct[BLOB_CAP];
static char g_strings[1 << 16];
static int g_nodes[MAX_NODES];

struct builder { uint8_t *st; size_t so; char *names; size_t nl; };

static void b_reset(struct builder *b)
{
    b->st = g_struct;
    b->so = 0;
    b->names = g_strings;
    b->nl = 0;
}

static void be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static void b_u32(struct builder *b, uint32_t v)
{
    assert(b->so + 4 <= BLOB_CAP);
    be32(b->st + b->so, v);
    b->so += 4;
}

static void b_align(struct builder *b)
{
    while (b->so & 3u) {
        assert(b->so + 1 <= BLOB_CAP);
        b->st[b->so++] = 0;
    }
}

static void b_begin(struct builder *b, const char *name)
{
    size_t len = strlen(name) + 1;
    b_u32(b, FDT_BEGIN_NODE);
    assert(b->so + len <= BLOB_CAP);
    memcpy(b->st + b->so, name, len);
    b->so += len;
    b_align(b);
}

static void b_end(struct builder *b)
{
    b_u32(b, FDT_END_NODE);
}

static uint32_t b_name(struct builder *b, const char *name)
{
    for (size_t at = 0; at < b->nl;) {
        if (!strcmp(b->names + at, name)) return (uint32_t)at;
        at += strlen(b->names + at) + 1;
    }
    size_t len = strlen(name) + 1;
    assert(b->nl + len <= sizeof(g_strings));
    memcpy(b->names + b->nl, name, len);
    uint32_t offset = (uint32_t)b->nl;
    b->nl += len;
    return offset;
}

static void b_prop(struct builder *b, const char *name, const void *data, size_t len)
{
    uint32_t nameoff = b_name(b, name);
    b_u32(b, FDT_PROP);
    b_u32(b, (uint32_t)len);
    b_u32(b, nameoff);
    assert(b->so + len <= BLOB_CAP);
    if (len) memcpy(b->st + b->so, data, len);
    b->so += len;
    b_align(b);
}

static void b_prop_u32(struct builder *b, const char *name, uint32_t value)
{
    uint8_t raw[4];
    be32(raw, value);
    b_prop(b, name, raw, sizeof(raw));
}

static void b_prop_string(struct builder *b, const char *name, const char *value)
{
    b_prop(b, name, value, strlen(value) + 1);
}

static void *b_finish(struct builder *b, uint8_t *blob)
{
    b_u32(b, FDT_END);
    size_t off_struct = 56, off_strings = off_struct + b->so;
    assert(off_strings + b->nl <= BLOB_CAP);
    memset(blob, 0, BLOB_CAP);
    memcpy(blob + off_struct, b->st, b->so);
    memcpy(blob + off_strings, b->names, b->nl);
    be32(blob + 0, 0xd00dfeedu);
    be32(blob + 4, BLOB_CAP);
    be32(blob + 8, (uint32_t)off_struct);
    be32(blob + 12, (uint32_t)off_strings);
    be32(blob + 16, 40);                     /* off_mem_rsvmap */
    be32(blob + 20, 17);                     /* version */
    be32(blob + 24, 16);                     /* last_comp_version */
    be32(blob + 28, 0);                      /* boot_cpuid_phys */
    be32(blob + 32, (uint32_t)b->nl);        /* size_dt_strings */
    be32(blob + 36, (uint32_t)b->so);        /* size_dt_struct */
    return blob;
}

static int b_nodes(const void *f)
{
    int offset = -1, depth = -1, n = 0;
    while ((offset = fdt_next_node(f, offset, &depth)) >= 0) {
        if (depth < 0) break;
        assert(n < MAX_NODES);
        g_nodes[n++] = offset;
    }
    return n;
}

/* The pre-index dt_available() chain, independent of dt_parent_offset(). */
static bool reference_available(const void *f, int node)
{
    while (node >= 0) {
        int len, slen;
        const void *status = fdt_getprop(f, node, "status", &len);
        if (status) {
            const char *s = fdt_getprop(f, node, "status", &slen);
            if (!s || slen <= 0 || s[slen - 1] || memchr(s, 0, (size_t)slen - 1) ||
                (strcmp(s, "okay") && strcmp(s, "ok")))
                return false;
        } else if (len != -FDT_ERR_NOTFOUND) {
            return false;
        }
        if (!node) return true;
        node = fdt_parent_offset(f, node);
    }
    return false;
}

/* The exact pre-index dt_phandle() walk: the behavioral contract. */
static int reference_phandle(const void *f, uint32_t handle)
{
    if (!handle || handle == UINT32_MAX) return -1;
    int found = -1;
    for (int n = fdt_next_node(f, -1, NULL); n >= 0; n = fdt_next_node(f, n, NULL)) {
        uint32_t h = 0, legacy = 0;
        int alen, blen;
        const fdt32_t *ap = fdt_getprop(f, n, "phandle", &alen);
        const fdt32_t *bp = fdt_getprop(f, n, "linux,phandle", &blen);
        if ((ap && alen != 4) || (bp && blen != 4)) return -1;
        if (ap) h = fdt32_to_cpu(*ap);
        if (bp) legacy = fdt32_to_cpu(*bp);
        if (ap && bp && h != legacy) return -1;
        if ((ap && h == handle) || (bp && legacy == handle)) {
            if (found >= 0) return -1;
            found = n;
        }
    }
    return found >= 0 && reference_available(f, found) ? found : -1;
}

/* Same answers with the scope open and closed, always against the walk. */
static void compare_both(const void *f, const uint32_t *handles, size_t count)
{
    dt_parent_index_begin(f);
    for (size_t i = 0; i < count; ++i)
        assert(dt_phandle(f, handles[i]) == reference_phandle(f, handles[i]));
    dt_parent_index_end();
    for (size_t i = 0; i < count; ++i)
        assert(dt_phandle(f, handles[i]) == reference_phandle(f, handles[i]));
}

/* root(1), /dev1(2), /legacy(linux,phandle 3), /off(4, disabled),
 * /parent(disabled)/kid(5), /dup1,/dup2(6), /mix1(7)/mix2(linux,phandle 7),
 * /both(8 + agreeing linux,phandle 8), /plain(none). */
static void *build_main(uint8_t *blob)
{
    struct builder b;
    b_reset(&b);
    b_begin(&b, "");
    b_prop_u32(&b, "phandle", 1);
    b_begin(&b, "dev1");
    b_prop_u32(&b, "phandle", 2);
    b_end(&b);
    b_begin(&b, "legacy");
    b_prop_u32(&b, "linux,phandle", 3);
    b_end(&b);
    b_begin(&b, "off");
    b_prop_string(&b, "status", "disabled");
    b_prop_u32(&b, "phandle", 4);
    b_end(&b);
    b_begin(&b, "parent");
    b_prop_string(&b, "status", "disabled");
    b_begin(&b, "kid");
    b_prop_u32(&b, "phandle", 5);
    b_end(&b);
    b_end(&b);
    b_begin(&b, "dup1");
    b_prop_u32(&b, "phandle", 6);
    b_end(&b);
    b_begin(&b, "dup2");
    b_prop_u32(&b, "phandle", 6);
    b_end(&b);
    b_begin(&b, "mix1");
    b_prop_u32(&b, "phandle", 7);
    b_end(&b);
    b_begin(&b, "mix2");
    b_prop_u32(&b, "linux,phandle", 7);
    b_end(&b);
    b_begin(&b, "both");
    b_prop_u32(&b, "phandle", 8);
    b_prop_u32(&b, "linux,phandle", 8);
    b_end(&b);
    b_begin(&b, "plain");
    b_end(&b);
    b_end(&b);
    return b_finish(&b, blob);
}

/* A second blob that reuses handle 2 on a different node. */
static void *build_second(uint8_t *blob)
{
    struct builder b;
    b_reset(&b);
    b_begin(&b, "");
    b_begin(&b, "other");
    b_prop_u32(&b, "phandle", 2);
    b_end(&b);
    b_end(&b);
    return b_finish(&b, blob);
}

/* kind 0: 8-byte phandle; kind 1: phandle/linux,phandle disagree;
 * kind 2: 3-byte linux,phandle. Each one must refuse every lookup. */
static void *build_malformed(uint8_t *blob, int kind)
{
    struct builder b;
    b_reset(&b);
    b_begin(&b, "");
    b_prop_u32(&b, "phandle", 1);
    b_begin(&b, "good");
    b_prop_u32(&b, "phandle", 2);
    b_end(&b);
    b_begin(&b, "bad");
    if (kind == 0) {
        uint8_t raw[8] = {0, 0, 0, 3, 0, 0, 0, 3};
        b_prop(&b, "phandle", raw, sizeof(raw));
    } else if (kind == 1) {
        b_prop_u32(&b, "phandle", 4);
        b_prop_u32(&b, "linux,phandle", 3);
    } else {
        b_prop(&b, "linux,phandle", "\0\0\3", 3);
    }
    b_end(&b);
    b_end(&b);
    return b_finish(&b, blob);
}

int main(void)
{
    struct builder b;
    int i;

    /* ---- tree A: strict phandle rules, the root included ---- */
    void *a = build_main(g_blob_a);
    assert(dt_validate(a, BLOB_CAP) == 0);
    int dev1 = fdt_path_offset(a, "/dev1");
    int legacy = fdt_path_offset(a, "/legacy");
    int both = fdt_path_offset(a, "/both");
    assert(dev1 > 0 && legacy > 0 && both > 0);

    static const uint32_t handles[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                                       0x7ffffff0u, UINT32_MAX};
    compare_both(a, handles, sizeof(handles) / sizeof(handles[0]));

    dt_parent_index_begin(a);
    assert(dt_phandle(a, 1) == 0);        /* the root is indexed too */
    assert(dt_phandle(a, 2) == dev1);
    assert(dt_phandle(a, 3) == legacy);   /* linux,phandle only */
    assert(dt_phandle(a, 4) == -1);       /* node itself disabled */
    assert(dt_phandle(a, 5) == -1);       /* disabled ancestor */
    assert(dt_phandle(a, 6) == -1);       /* duplicated phandle */
    assert(dt_phandle(a, 7) == -1);       /* duplicate across property kinds */
    assert(dt_phandle(a, 8) == both);     /* agreeing pair on one node */
    assert(dt_phandle(a, 0) == -1 && dt_phandle(a, UINT32_MAX) == -1);
    /* A second begin is a clean reset, not a second layer. */
    dt_parent_index_begin(a);
    g_walks = 0;
    assert(dt_phandle(a, 2) == dev1);
    assert(g_walks == 0);
    dt_parent_index_end();
    g_walks = 0;
    assert(dt_phandle(a, 2) == dev1);
    assert(g_walks > 0);                  /* scope closed: full walk */

    /* ---- tree B: another blob must not see A's index ---- */
    void *s = build_second(g_blob_b);
    assert(dt_validate(s, BLOB_CAP) == 0);
    int other = fdt_path_offset(s, "/other");
    assert(other > 0);

    dt_parent_index_begin(a);
    dt_parent_index_begin(s);             /* the scope follows the blob */
    g_walks = 0;
    assert(dt_phandle(s, 2) == other);
    assert(g_walks == 0);
    g_walks = 0;
    assert(dt_phandle(a, 2) == dev1);     /* A's scope is gone */
    assert(g_walks > 0);
    dt_parent_index_end();
    dt_parent_index_begin(a);
    g_walks = 0;
    assert(dt_phandle(s, 2) == other);    /* unscoped blob: full walk */
    assert(g_walks > 0);
    dt_parent_index_end();

    /* ---- strict validation: one bad property refuses every lookup ---- */
    for (int kind = 0; kind < 3; ++kind) {
        static const uint32_t bad_handles[] = {1, 2, 3, 4, 5};
        void *bad = build_malformed(g_blob_b, kind);
        assert(dt_validate(bad, BLOB_CAP) == 0);
        compare_both(bad, bad_handles, sizeof(bad_handles) / sizeof(bad_handles[0]));
        for (size_t k = 0; k < sizeof(bad_handles) / sizeof(bad_handles[0]); ++k)
            assert(dt_phandle(bad, bad_handles[k]) == -1);
    }

    /* ---- mutate, re-begin: the scope is rebuilt, never reused ---- */
    assert(fdt_open_into(a, a, (int)BLOB_CAP) == 0);
    int root = fdt_path_offset(a, "/");
    assert(root >= 0);
    int late = fdt_add_subnode(a, root, "late");
    assert(late > 0);
    assert(fdt_setprop_u32(a, late, "phandle", 9) == 0);
    assert(dt_validate(a, BLOB_CAP) == 0);
    late = fdt_path_offset(a, "/late");
    assert(late > 0 && dt_phandle(a, 9) == late);
    dt_parent_index_begin(a);
    assert(dt_phandle(a, 9) == late);
    assert(dt_phandle(a, 2) == fdt_path_offset(a, "/dev1"));
    dt_parent_index_end();

    /* ---- a tree deeper than the 64-level cap falls back ---- */
    b_reset(&b);
    b_begin(&b, "");
    b_prop_u32(&b, "phandle", 1);
    for (i = 0; i < 70; ++i) {
        char name[16];
        snprintf(name, sizeof(name), "d%d", i);
        b_begin(&b, name);
    }
    b_prop_u32(&b, "phandle", 42);
    for (i = 0; i < 70; ++i) b_end(&b);
    b_end(&b);
    void *deep = b_finish(&b, g_blob_a);
    assert(dt_validate(deep, BLOB_CAP) == 0);
    int deepest = reference_phandle(deep, 42);
    assert(deepest > 0);
    dt_parent_index_begin(deep);
    g_walks = 0;
    assert(dt_phandle(deep, 42) == deepest);
    assert(g_walks > 0);
    dt_parent_index_end();

    /* ---- more nodes than the 8192 cap falls back ---- */
    b_reset(&b);
    b_begin(&b, "");
    for (i = 0; i < 9000; ++i) {
        char name[16];
        snprintf(name, sizeof(name), "w%d", i);
        b_begin(&b, name);
        if (i == 4500) b_prop_u32(&b, "phandle", 7);
        b_end(&b);
    }
    b_end(&b);
    void *wide = b_finish(&b, g_blob_b);
    assert(dt_validate(wide, BLOB_CAP) == 0);
    int wide_node = reference_phandle(wide, 7);
    assert(wide_node > 0);
    dt_parent_index_begin(wide);
    g_walks = 0;
    assert(dt_phandle(wide, 7) == wide_node);
    assert(g_walks > 0);
    dt_parent_index_end();

    /* ---- bulk indexed lookups never rescan the tree ---- */
    b_reset(&b);
    b_begin(&b, "");
    for (i = 0; i < 3000; ++i) {
        char name[16];
        snprintf(name, sizeof(name), "n%d", i);
        b_begin(&b, name);
        b_prop_u32(&b, "phandle", (uint32_t)i + 2);
        b_end(&b);
    }
    b_end(&b);
    void *bulk = b_finish(&b, g_blob_a);
    assert(dt_validate(bulk, BLOB_CAP) == 0);
    int nodes = b_nodes(bulk);
    assert(nodes == 3001);
    dt_parent_index_begin(bulk);
    g_walks = 0;
    for (i = 0; i < 3000; ++i)
        assert(dt_phandle(bulk, (uint32_t)i + 2) == g_nodes[i + 1]);
    assert(g_walks == 0);                 /* 3000 lookups, zero tree walks */
    dt_parent_index_end();
    g_walks = 0;
    assert(dt_phandle(bulk, 2) == g_nodes[1]);
    assert(g_walks > 0);                  /* outside the scope: full walk */

    puts("dt_phandle_index_test: ok");
    return 0;
}
