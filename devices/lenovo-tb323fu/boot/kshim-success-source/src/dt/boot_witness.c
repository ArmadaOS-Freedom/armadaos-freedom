#include <boot_witness.h>

#include <libfdt.h>
#include <stdint.h>
#include <string.h>

#define BOOT_WITNESS_MAX_BOOTARGS 65536

/* AOSP bootconfig trailer appended by the bootloader to the end of the
 * initrd: <params><u32 le size><u32 le checksum>"#BOOTCONFIG\n". The kernel
 * (init/main.c) accepts up to three bytes of alignment padding after the
 * magic and bounds the parameter data to XBC_DATA_MAX. */
#define BOOT_WITNESS_BOOTCONFIG_MAGIC "#BOOTCONFIG\n"
#define BOOT_WITNESS_BOOTCONFIG_MAGIC_LEN 12U
#define BOOT_WITNESS_BOOTCONFIG_MAX 32767U
#define BOOT_WITNESS_INITRD_MAX (UINT64_C(1) << 30)
/* Longest normalized "key=value" token examined for a witness key. */
#define BOOT_WITNESS_TOKEN_MAX 128U

/* ASCII whitespace per boot requirements; no locale, no heap. */
static int boot_witness_is_space(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' ||
           c == '\f' || c == '\v';
}

static const char *boot_witness_find_chosen_bootargs(const void *fdt,
                                                     int *len_out)
{
    int node = fdt_path_offset(fdt, "/chosen");
    const char *args;

    if (node < 0) {
        return NULL;
    }

    args = fdt_getprop(fdt, node, "bootargs", len_out);
    if (!args || *len_out <= 0) {
        return NULL;
    }

    return args;
}

/*
 * Return the bootargs content (without the terminating NUL) when /chosen
 * carries exactly one bounded C string; NULL otherwise.
 */
static const char *boot_witness_bootargs(const void *firmware_fdt,
                                         size_t *content_len)
{
    const char *args;
    int args_len;

    if (!firmware_fdt || fdt_check_header(firmware_fdt) != 0) {
        return NULL;
    }

    args = boot_witness_find_chosen_bootargs(firmware_fdt, &args_len);
    if (!args) {
        return NULL;
    }

    /* Bound: at most 65536 bytes including terminating NUL. */
    if (args_len > BOOT_WITNESS_MAX_BOOTARGS) {
        return NULL;
    }

    /* Require exactly one C string: final NUL, no embedded NUL. */
    if (args[args_len - 1] != '\0') {
        return NULL;
    }

    *content_len = (size_t)args_len - 1;
    if (memchr(args, '\0', *content_len) != NULL) {
        return NULL;
    }

    return args;
}

typedef void (*boot_witness_token_fn)(void *ctx, const char *token,
                                      size_t len);

/* Tokenize over ASCII whitespace excluding the terminating NUL. */
static void boot_witness_for_each_token(const char *args, size_t content_len,
                                        boot_witness_token_fn fn, void *ctx)
{
    size_t i = 0;

    while (i < content_len) {
        size_t start;

        while (i < content_len && boot_witness_is_space(args[i])) {
            i++;
        }
        if (i >= content_len) {
            break;
        }

        start = i;
        while (i < content_len && !boot_witness_is_space(args[i])) {
            i++;
        }
        fn(ctx, args + start, i - start);
    }
}

/*
 * Match one "key=value" token against key. Returns 0 when the token is not
 * about key (including prefix lookalikes such as key_extra=...), 1 with the
 * value when it is, and -1 for the bare exact key without '='.
 */
static int boot_witness_match(const char *token, size_t len, const char *key,
                              const char **val, size_t *val_len)
{
    size_t key_len = strlen(key);

    if (len < key_len || memcmp(token, key, key_len) != 0) {
        return 0;
    }
    if (len == key_len) {
        return -1;
    }
    if (token[key_len] != '=') {
        return 0;
    }
    *val = token + key_len + 1;
    *val_len = len - (key_len + 1);
    return 1;
}

struct boot_witness_slot_acc {
    int seen;
    int invalid;
    int slot;
};

static void boot_witness_slot_token(void *ctx, const char *token, size_t len)
{
    struct boot_witness_slot_acc *acc = ctx;
    const char *val = NULL;
    size_t val_len = 0;
    int this_slot;
    int m = boot_witness_match(token, len, "androidboot.slot_suffix",
                               &val, &val_len);

    if (m == 0) {
        return;
    }
    acc->seen = 1;
    if (m < 0) {
        /* Bare exact key without '=' invalidates evidence. */
        acc->invalid = 1;
        return;
    }

    if (val_len == 2 && val[0] == '_' && val[1] == 'a') {
        this_slot = 0;
    } else if (val_len == 2 && val[0] == '_' && val[1] == 'b') {
        this_slot = 1;
    } else {
        /* Empty, quoted, or unrecognized slot suffix value. */
        acc->invalid = 1;
        return;
    }

    if (acc->slot != -1) {
        /* Any duplicate (even equal) is rejected as ambiguous/conflicting. */
        acc->invalid = 1;
        return;
    }
    acc->slot = this_slot;
}

struct boot_witness_lock_acc {
    int seen;
    int invalid;
    int verified;
    int vbmeta;
    int flash;
};

static void boot_witness_lock_token(void *ctx, const char *token, size_t len)
{
    struct boot_witness_lock_acc *acc = ctx;
    const char *val = NULL;
    size_t val_len = 0;
    int this_state = 0;
    int *slot = NULL;
    int m;

    m = boot_witness_match(token, len, "androidboot.verifiedbootstate",
                           &val, &val_len);
    if (m != 0) {
        slot = &acc->verified;
        if (m > 0) {
            if ((val_len == 5 && memcmp(val, "green", 5) == 0) ||
                (val_len == 6 && memcmp(val, "yellow", 6) == 0)) {
                this_state = 1;
            } else if (val_len == 6 && memcmp(val, "orange", 6) == 0) {
                this_state = 2;
            } else {
                this_state = 0;
            }
        }
        goto record;
    }

    m = boot_witness_match(token, len, "androidboot.vbmeta.device_state",
                           &val, &val_len);
    if (m != 0) {
        slot = &acc->vbmeta;
        if (m > 0) {
            if (val_len == 6 && memcmp(val, "locked", 6) == 0) {
                this_state = 1;
            } else if (val_len == 8 && memcmp(val, "unlocked", 8) == 0) {
                this_state = 2;
            } else {
                this_state = 0;
            }
        }
        goto record;
    }

    m = boot_witness_match(token, len, "androidboot.flash.locked",
                           &val, &val_len);
    if (m != 0) {
        slot = &acc->flash;
        if (m > 0) {
            if (val_len == 1 && val[0] == '1') {
                this_state = 1;
            } else if (val_len == 1 && val[0] == '0') {
                this_state = 2;
            } else {
                this_state = 0;
            }
        }
        goto record;
    }
    return;

record:
    acc->seen = 1;
    if (m < 0 || this_state == 0 || *slot != 0) {
        /* Bare key, malformed value or duplicate. */
        acc->invalid = 1;
        return;
    }
    *slot = this_state;
}

static int boot_witness_lock_result(const struct boot_witness_lock_acc *acc)
{
    /* verifiedbootstate is mandatory; the other keys only corroborate it. */
    if (acc->invalid || acc->verified == 0 ||
        (acc->vbmeta != 0 && acc->vbmeta != acc->verified) ||
        (acc->flash != 0 && acc->flash != acc->verified)) {
        return 0;
    }
    return acc->verified;
}

static int boot_witness_read_cells(const void *prop, int len, uint64_t *value)
{
    const uint8_t *p = prop;
    uint64_t v = 0;
    int i;

    if (!prop || (len != 4 && len != 8)) {
        return -1;
    }
    for (i = 0; i < len; i++) {
        v = (v << 8) | p[i];
    }
    *value = v;
    return 0;
}

static int boot_witness_root_cells(const void *fdt, const char *name, int dflt)
{
    int len;
    const fdt32_t *prop = fdt_getprop(fdt, 0, name, &len);

    if (!prop) {
        return dflt;
    }
    if (len != 4) {
        return -1;
    }
    return (int)fdt32_to_cpu(*prop);
}

static int boot_witness_is_memory_node(const void *fdt, int node)
{
    int len;
    const char *type = fdt_getprop(fdt, node, "device_type", &len);
    const char *name;

    if (type) {
        return len == 7 && memcmp(type, "memory", 7) == 0;
    }
    name = fdt_get_name(fdt, node, NULL);
    return name && (strcmp(name, "memory") == 0 ||
                    strncmp(name, "memory@", 7) == 0);
}

static int boot_witness_node_available(const void *fdt, int node)
{
    int len;
    const char *status = fdt_getprop(fdt, node, "status", &len);

    if (!status) {
        return 1;
    }
    return (len == 5 && memcmp(status, "okay", 5) == 0) ||
           (len == 3 && memcmp(status, "ok", 3) == 0);
}

/*
 * True only when [start, end) lies inside one reg range of an enabled
 * root-level memory node. Evidence is read before translation is enabled,
 * so the trailer is never read from an address the firmware DT does not
 * declare as memory.
 */
static int boot_witness_range_in_memory(const void *fdt, uint64_t start,
                                        uint64_t end)
{
    int ac = boot_witness_root_cells(fdt, "#address-cells", 2);
    int sc = boot_witness_root_cells(fdt, "#size-cells", 1);
    int node;

    if (ac < 1 || ac > 2 || sc < 1 || sc > 2) {
        return 0;
    }

    fdt_for_each_subnode(node, fdt, 0) {
        const uint8_t *reg;
        int reg_len;
        int tuple = (ac + sc) * 4;
        int off;

        if (!boot_witness_is_memory_node(fdt, node) ||
            !boot_witness_node_available(fdt, node)) {
            continue;
        }
        reg = fdt_getprop(fdt, node, "reg", &reg_len);
        if (!reg || reg_len <= 0 || reg_len % tuple != 0) {
            continue;
        }
        for (off = 0; off < reg_len; off += tuple) {
            uint64_t base;
            uint64_t size;

            if (boot_witness_read_cells(reg + off, ac * 4, &base) != 0 ||
                boot_witness_read_cells(reg + off + ac * 4, sc * 4,
                                        &size) != 0) {
                return 0;
            }
            if (size == 0 || base > UINT64_MAX - size) {
                continue;
            }
            if (start >= base && end <= base + size) {
                return 1;
            }
        }
    }
    return 0;
}

static uint32_t boot_witness_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/*
 * Locate the AOSP bootconfig parameters appended to the firmware initrd.
 * Returns the parameter text (up to its first NUL; any remaining bytes of the
 * declared size must be NUL padding) or NULL when the trailer is absent,
 * out of bounds, or fails its size or checksum.
 */
static const char *boot_witness_bootconfig(const void *fdt, size_t *text_len)
{
    int node = fdt_path_offset(fdt, "/chosen");
    const void *prop;
    int len;
    uint64_t start;
    uint64_t end;
    const uint8_t *magic = NULL;
    const uint8_t *data;
    uint32_t size;
    uint32_t csum;
    uint32_t sum = 0;
    size_t text;
    uint32_t i;
    int pad;

    if (node < 0) {
        return NULL;
    }
    prop = fdt_getprop(fdt, node, "linux,initrd-start", &len);
    if (boot_witness_read_cells(prop, len, &start) != 0) {
        return NULL;
    }
    prop = fdt_getprop(fdt, node, "linux,initrd-end", &len);
    if (boot_witness_read_cells(prop, len, &end) != 0) {
        return NULL;
    }
    if (start == 0 || end <= start || end - start > BOOT_WITNESS_INITRD_MAX ||
        end - start < BOOT_WITNESS_BOOTCONFIG_MAGIC_LEN + 8U ||
        (uint64_t)(uintptr_t)end != end ||
        !boot_witness_range_in_memory(fdt, start, end)) {
        return NULL;
    }

    for (pad = 0; pad < 4; pad++) {
        const uint8_t *p = (const uint8_t *)(uintptr_t)
            (end - BOOT_WITNESS_BOOTCONFIG_MAGIC_LEN - (uint64_t)pad);

        if ((uint64_t)(uintptr_t)p < start + 8U) {
            return NULL;
        }
        if (memcmp(p, BOOT_WITNESS_BOOTCONFIG_MAGIC,
                   BOOT_WITNESS_BOOTCONFIG_MAGIC_LEN) == 0) {
            magic = p;
            break;
        }
    }
    if (!magic) {
        return NULL;
    }

    size = boot_witness_le32(magic - 8);
    csum = boot_witness_le32(magic - 4);
    if (size == 0 || size > BOOT_WITNESS_BOOTCONFIG_MAX ||
        (uint64_t)(uintptr_t)(magic - 8) - start < size) {
        return NULL;
    }
    data = magic - 8 - size;
    for (i = 0; i < size; i++) {
        sum += data[i];
    }
    if (sum != csum) {
        return NULL;
    }

    text = 0;
    while (text < size && data[text] != '\0') {
        text++;
    }
    for (i = (uint32_t)text; i < size; i++) {
        if (data[i] != '\0') {
            return NULL;
        }
    }
    *text_len = text;
    return (const char *)data;
}

static int boot_witness_bc_space(char c)
{
    return c == ' ' || c == '\t' || c == '\r';
}

static int boot_witness_bc_key_char(char c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
}

/*
 * Parse one bootconfig line under the strict flat grammar:
 *   blank | '#' comment | KEY ws* '=' ws* VALUE ws* ['#' comment]
 * KEY is [A-Za-z0-9._-]+. VALUE is a string quoted with '"' or '\'' and closed
 * on the same line, or a non-empty bare value without any of ,;{}"'.
 * Returns 0 for blank/comment lines, 1 with key/value for an assignment, and
 * -1 for anything else.
 */
static int boot_witness_bc_line(const char *text, size_t len,
                                const char **key, size_t *key_len,
                                const char **val, size_t *val_len)
{
    size_t i = 0;
    size_t j;

    while (i < len && boot_witness_bc_space(text[i])) {
        i++;
    }
    if (i == len || text[i] == '#') {
        return 0;
    }

    j = i;
    while (j < len && boot_witness_bc_key_char(text[j])) {
        j++;
    }
    if (j == i) {
        return -1;
    }
    *key = text + i;
    *key_len = j - i;

    while (j < len && boot_witness_bc_space(text[j])) {
        j++;
    }
    if (j == len || text[j] != '=') {
        return -1; /* bare key, "+=", ":=", braces or anything else */
    }
    j++;
    while (j < len && boot_witness_bc_space(text[j])) {
        j++;
    }
    if (j == len || text[j] == '#') {
        return -1; /* an empty value continues on the next line */
    }

    if (text[j] == '"' || text[j] == '\'') {
        const char *close = memchr(text + j + 1, text[j], len - (j + 1));

        if (!close) {
            return -1;
        }
        *val = text + j + 1;
        *val_len = (size_t)(close - *val);
        j = (size_t)(close - text) + 1;
        while (j < len && boot_witness_bc_space(text[j])) {
            j++;
        }
        if (j != len && text[j] != '#') {
            return -1;
        }
        return 1;
    }

    *val = text + j;
    while (j < len && text[j] != '#') {
        char c = text[j];

        if (c == ',' || c == ';' || c == '{' || c == '}' || c == '"' ||
            c == '\'') {
            return -1;
        }
        j++;
    }
    while (j > (size_t)(*val - text) && boot_witness_bc_space(text[j - 1])) {
        j--;
    }
    *val_len = (size_t)(text + j - *val);
    return *val_len ? 1 : -1;
}

/*
 * Feed bootconfig assignments of keys as normalized "key=value" tokens.
 * Android bootconfig is a flat list of single assignments, one per line. The
 * whole bootconfig is first checked against the strict grammar above; any
 * line outside it (arrays, ';', braces, other operators, empty values,
 * embedded quotes, ...) could change which key a later line belongs to, so
 * the source is then unsupported and the first key is passed on bare so no
 * evidence is used.
 */
static void boot_witness_for_each_bootconfig(const char *text, size_t len,
                                             const char *const *keys,
                                             boot_witness_token_fn fn,
                                             void *ctx)
{
    const char *key;
    const char *val;
    size_t key_len;
    size_t val_len;
    size_t pos;
    int pass;

    for (pass = 0; pass < 2; pass++) {
        pos = 0;
        while (pos < len) {
            size_t line_end = pos;
            int rc;
            size_t k;

            while (line_end < len && text[line_end] != '\n') {
                line_end++;
            }
            rc = boot_witness_bc_line(text + pos, line_end - pos, &key,
                                      &key_len, &val, &val_len);
            if (rc < 0) {
                fn(ctx, keys[0], strlen(keys[0]));
                return;
            }
            if (rc > 0 && pass == 1) {
                for (k = 0; keys[k] != NULL; k++) {
                    char token[BOOT_WITNESS_TOKEN_MAX];

                    if (strlen(keys[k]) != key_len ||
                        memcmp(keys[k], key, key_len) != 0) {
                        continue;
                    }
                    if (key_len + 1 + val_len > sizeof(token)) {
                        fn(ctx, keys[k], key_len);
                        break;
                    }
                    memcpy(token, key, key_len);
                    token[key_len] = '=';
                    memcpy(token + key_len + 1, val, val_len);
                    fn(ctx, token, key_len + 1 + val_len);
                    break;
                }
            }
            pos = line_end + 1;
        }
    }
}

/*
 * Firmware bootargs carry the exact "bootconfig" switch the kernel honours,
 * among the kernel parameters before a standalone "--". Words are split like
 * the kernel's next_arg(): whitespace inside double quotes does not end a
 * word, so a quoted "a -- b" or "x bootconfig" is part of another argument.
 * The slot/lock token rules above are deliberately left unchanged.
 */
static int boot_witness_bootconfig_enabled(const char *args, size_t len)
{
    size_t i = 0;
    int enabled = 0;

    while (i < len) {
        size_t start;
        int quoted = 0;

        while (i < len && boot_witness_is_space(args[i])) {
            i++;
        }
        if (i >= len) {
            break;
        }
        start = i;
        while (i < len && (quoted || !boot_witness_is_space(args[i]))) {
            if (args[i] == '"') {
                quoted = !quoted;
            }
            i++;
        }
        if (i - start == 2 && memcmp(args + start, "--", 2) == 0) {
            break; /* parse_args() stops here; the rest belongs to init */
        }
        if (i - start == 10 && memcmp(args + start, "bootconfig", 10) == 0) {
            enabled = 1;
        }
    }
    return enabled;
}

int kshim_boot_slot_read_source(const void *firmware_fdt, int *source)
{
    static const char *const keys[] = { "androidboot.slot_suffix", NULL };
    struct boot_witness_slot_acc acc = { 0, 0, -1 };
    const char *args;
    const char *text;
    size_t content_len = 0;
    size_t text_len = 0;

    if (source) {
        *source = 0;
    }

    args = boot_witness_bootargs(firmware_fdt, &content_len);
    if (!args) {
        return -1;
    }

    boot_witness_for_each_token(args, content_len, boot_witness_slot_token,
                                &acc);
    if (acc.seen) {
        /* Bootargs evidence, valid or not, is never mixed with bootconfig. */
        if (acc.invalid || acc.slot < 0) {
            return -1;
        }
        if (source) {
            *source = KSHIM_BOOT_EVIDENCE_BOOTARGS;
        }
        return acc.slot;
    }

    if (!boot_witness_bootconfig_enabled(args, content_len)) {
        return -1;
    }
    text = boot_witness_bootconfig(firmware_fdt, &text_len);
    if (!text) {
        return -1;
    }
    boot_witness_for_each_bootconfig(text, text_len, keys,
                                     boot_witness_slot_token, &acc);
    if (!acc.seen || acc.invalid || acc.slot < 0) {
        return -1;
    }
    if (source) {
        *source = KSHIM_BOOT_EVIDENCE_BOOTCONFIG;
    }
    return acc.slot;
}

int kshim_boot_slot_read(const void *firmware_fdt)
{
    return kshim_boot_slot_read_source(firmware_fdt, NULL);
}

int kshim_boot_lock_read_source(const void *firmware_fdt, int *source)
{
    static const char *const keys[] = {
        "androidboot.verifiedbootstate",
        "androidboot.vbmeta.device_state",
        "androidboot.flash.locked",
        NULL,
    };
    struct boot_witness_lock_acc acc = { 0, 0, 0, 0, 0 };
    const char *args;
    const char *text;
    size_t content_len = 0;
    size_t text_len = 0;
    int state;

    if (source) {
        *source = 0;
    }

    args = boot_witness_bootargs(firmware_fdt, &content_len);
    if (!args) {
        return 0;
    }

    boot_witness_for_each_token(args, content_len, boot_witness_lock_token,
                                &acc);
    if (acc.seen) {
        state = boot_witness_lock_result(&acc);
        if (state != 0 && source) {
            *source = KSHIM_BOOT_EVIDENCE_BOOTARGS;
        }
        return state;
    }

    if (!boot_witness_bootconfig_enabled(args, content_len)) {
        return 0;
    }
    text = boot_witness_bootconfig(firmware_fdt, &text_len);
    if (!text) {
        return 0;
    }
    boot_witness_for_each_bootconfig(text, text_len, keys,
                                     boot_witness_lock_token, &acc);
    state = boot_witness_lock_result(&acc);
    if (state != 0 && source) {
        *source = KSHIM_BOOT_EVIDENCE_BOOTCONFIG;
    }
    return state;
}

int kshim_boot_lock_read(const void *firmware_fdt)
{
    return kshim_boot_lock_read_source(firmware_fdt, NULL);
}

static int boot_witness_export_cells(void *linux_fdt, const char *prop_name,
                                     int value, int valid, int source)
{
    int node;
    int err;

    if (!linux_fdt || fdt_check_header(linux_fdt) != 0) {
        return -FDT_ERR_BADMAGIC;
    }

    node = fdt_path_offset(linux_fdt, "/chosen");
    if (node < 0) {
        return node; /* do not invent /chosen */
    }

    /* Remove any stale witness first; NOTFOUND is acceptable. */
    err = fdt_delprop(linux_fdt, node, prop_name);
    if (err != 0 && err != -FDT_ERR_NOTFOUND) {
        return err;
    }

    if (!valid || (source != KSHIM_BOOT_EVIDENCE_BOOTARGS &&
                   source != KSHIM_BOOT_EVIDENCE_BOOTCONFIG)) {
        /* Unknown value or source: leave stale evidence cleared. */
        return 0;
    }

    {
        const uint32_t cells[4] = {
            cpu_to_fdt32(1),            /* version 1 */
            cpu_to_fdt32((uint32_t)source),
            cpu_to_fdt32((uint32_t)value),
            cpu_to_fdt32(0),            /* flags 0 */
        };

        err = fdt_setprop(linux_fdt, node, prop_name, cells, sizeof(cells));
        if (err != 0) {
            return err;
        }
    }

    return 0;
}

int kshim_boot_witness_export_source(void *linux_fdt, int booted_slot,
                                     int source)
{
    return boot_witness_export_cells(linux_fdt, "armada,boot-witness",
                                     booted_slot,
                                     booted_slot == 0 || booted_slot == 1,
                                     source);
}

int kshim_boot_witness_export(void *linux_fdt, int booted_slot)
{
    return kshim_boot_witness_export_source(linux_fdt, booted_slot,
                                            KSHIM_BOOT_EVIDENCE_BOOTARGS);
}

int kshim_boot_lock_export_source(void *linux_fdt, int state, int source)
{
    return boot_witness_export_cells(linux_fdt, "armada,boot-lock", state,
                                     state == 1 || state == 2, source);
}

int kshim_boot_lock_export(void *linux_fdt, int state)
{
    return kshim_boot_lock_export_source(linux_fdt, state,
                                         KSHIM_BOOT_EVIDENCE_BOOTARGS);
}
