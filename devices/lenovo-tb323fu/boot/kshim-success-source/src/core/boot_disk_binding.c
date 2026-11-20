#include "boot_disk_binding.h"
#include <string.h>

static int hex_lower(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
}

static int decode_hex64(char dst[65], const uint8_t *src)
{
    size_t i;
    int nonzero = 0;
    for (i = 0; i < 64; i++) {
        char c = (char)src[i];
        if (!hex_lower(c))
            return -1;
        if (c != '0')
            nonzero = 1;
        dst[i] = c;
    }
    dst[64] = '\0';
    return nonzero ? 0 : -1;
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

int kshim_disk_binding_decode(const void *data, size_t bytes,
                              struct kshim_disk_binding *out)
{
    const uint8_t *p = (const uint8_t *)data;
    static const uint8_t magic[8] = { 'A', 'R', 'M', 'D', 'E', 'P', '1', '\0' };
    struct kshim_disk_binding tmp;
    uint32_t serial, index;
    size_t i;

    if (data == NULL || out == NULL)
        return -1;
    if (bytes != KSHIM_DISK_BINDING_BYTES)
        return -1;

    for (i = 0; i < 8; i++) {
        if (p[i] != magic[i])
            return -1;
    }

    if (p[8] != 0 && p[8] != 1)
        return -1;

    for (i = 9; i < 16; i++) {
        if (p[i] != 0)
            return -1;
    }

    memset(&tmp, 0, sizeof(tmp));
    tmp.version = p[8];

    if (decode_hex64(tmp.boot_checksum, p + 16) != 0)
        return -1;
    if (decode_hex64(tmp.commit, p + 80) != 0)
        return -1;

    serial = le32(p + 144);
    index = le32(p + 148);

    if (serial > (uint32_t)INT32_MAX)
        return -1;
    if (index > (uint32_t)INT32_MAX)
        return -1;

    tmp.serial = serial;
    tmp.index = index;

    *out = tmp;
    return 0;
}

/* append to boot_disk_binding.c */
#include <stdio.h>
#include <string.h>

/* Scan one quote-aware token at args[*i..len). Whitespace inside double
 * quotes does not end the token. Returns 0 on success and sets the start and end of
 * the token (including any surrounding quotes). Returns -1 if a double quote
 * is left unbalanced. Mirrors the boot_witness scan for Linux quoting. */
static int kshim_signed_next_token(const char *args, size_t len, size_t *i,
                                   size_t *start, size_t *end)
{
    int quoted = 0;

    while (*i < len && args[*i] == ' ')
        (*i)++;
    if (*i >= len) {
        *start = *i;
        *end = *i;
        return 0;
    }
    *start = *i;
    while (*i < len && (quoted || args[*i] != ' ')) {
        if (args[*i] == '"')
            quoted = !quoted;
        (*i)++;
    }
    if (quoted)
        return -1;
    *end = *i;
    return 0;
}

/* Strip one leading double quote and, if the token was quoted and the
 * remaining text still ends in one, a single trailing double quote. Every
 * double quote toggles the scan; there are no escape rules. */
static void kshim_signed_token_unquote(const char **tok, size_t *len)
{
    int quoted = 0;

    if (*len > 0 && (*tok)[0] == '"') {
        (*tok)++;
        (*len)--;
        quoted = 1;
    }
    if (quoted && *len > 0 && (*tok)[*len - 1] == '"')
        (*len)--;
}

/* Reject reserved signed_args tokens exactly root / boot / ostree /
 * armada.deployment, either bare or immediately followed by '=', after
 * handling surrounding double quotes. Values quoted inside are preserved. */
static int kshim_signed_token_reserved(const char *tok, size_t len)
{
    static const char *const reserved[] = {
        "root", "boot", "ostree", "armada.deployment"
    };
    size_t i;

    kshim_signed_token_unquote(&tok, &len);

    for (i = 0; i < sizeof(reserved) / sizeof(reserved[0]); i++) {
        size_t rlen = strlen(reserved[i]);
        if (len == rlen && memcmp(tok, reserved[i], rlen) == 0)
            return 1;
        if (len > rlen && tok[rlen] == '=' &&
            memcmp(tok, reserved[i], rlen) == 0)
            return 1;
    }
    return 0;
}

static int kshim_signed_token_is(const char *tok, size_t len, const char *s)
{
    size_t slen = strlen(s);
    return len == slen && memcmp(tok, s, slen) == 0;
}

/* Return 1 if token begins with prefix and either equals it or continues
 * with '='. Used for loglevel=* and rd.break[=...]. */
static int kshim_signed_token_prefix_eq(const char *tok, size_t len,
                                        const char *prefix)
{
    size_t plen = strlen(prefix);
    if (len < plen)
        return 0;
    if (memcmp(tok, prefix, plen) != 0)
        return 0;
    if (len == plen)
        return 1;
    return tok[plen] == '=';
}

static int kshim_token_board2_diag_filtered(const char *tok, size_t len)
{
    kshim_signed_token_unquote(&tok, &len);
    if (kshim_signed_token_is(tok, len, "quiet"))
        return 1;
    if (kshim_signed_token_prefix_eq(tok, len, "loglevel"))
        return 1;
    if (kshim_signed_token_prefix_eq(tok, len, "rd.break"))
        return 1;
    return 0;
}

static int kshim_append(char *out, size_t cap, size_t *used,
                        const char *s, size_t n)
{
    if (*used >= cap || n >= cap - *used)
        return -1;
    memcpy(out + *used, s, n);
    *used += n;
    out[*used] = '\0';
    return 0;
}

static int kshim_append_cstr(char *out, size_t cap, size_t *used, const char *s)
{
    return kshim_append(out, cap, used, s, strlen(s));
}

static int kshim_append_uuid(char *out, size_t cap, size_t *used,
                             const uint8_t uuid[16])
{
    static const char hex[] = "0123456789abcdef";
    char buf[37];
    size_t i, o = 0;

    for (i = 0; i < 16; i++) {
        if (i == 4 || i == 6 || i == 8 || i == 10)
            buf[o++] = '-';
        buf[o++] = hex[(uuid[i] >> 4) & 0xf];
        buf[o++] = hex[uuid[i] & 0xf];
    }
    buf[o] = '\0';
    return kshim_append(out, cap, used, buf, o);
}

int kshim_disk_bootargs(char out[2048], const char *signed_args,
                        const struct kshim_disk_entry *entry,
                        const struct kshim_disk_binding *binding,
                        uint32_t board)
{
    size_t used = 0;
    size_t i, n;
    int any = 0;
    int rc;

    if (out == NULL || signed_args == NULL || entry == NULL || binding == NULL)
        return -1;

    out[0] = '\0';

    /* Validate signed_args tokens before emitting anything. */
    n = strlen(signed_args);
    if (n >= 2048)
        return -1;
    for (i = 0; i < n; i++) {
        unsigned char c = (unsigned char)signed_args[i];
        if (c < 0x20 || c > 0x7e)
            return -1;
    }
    i = 0;
    while (i < n) {
        size_t start, end, len;
        const char *tok;
        if (kshim_signed_next_token(signed_args, n, &i, &start, &end) != 0)
            return -1;
        if (end == start)
            break;
        tok = signed_args + start;
        len = end - start;
        if (kshim_signed_token_reserved(tok, len))
            return -1;
        kshim_signed_token_unquote(&tok, &len);
        if (len > 0 && kshim_signed_token_is(tok, len, "--"))
            return -1;
    }

    /* Copy signed statics, applying board-specific filtering. */
    n = strlen(signed_args);
    i = 0;
    while (i < n) {
        size_t start, end, len;
        if (kshim_signed_next_token(signed_args, n, &i, &start, &end) != 0)
            return -1;
        if (end == start)
            break;
        len = end - start;

        if (board == 2 && entry->diagnostic && kshim_token_board2_diag_filtered(
                signed_args + start, len))
            continue;

        if (used != 0) {
            rc = kshim_append_cstr(out, 2048, &used, " ");
            if (rc != 0)
                return -1;
        }
        rc = kshim_append(out, 2048, &used, signed_args + start, len);
        if (rc != 0)
            return -1;
        any = 1;
    }

    /* Board-specific diagnostics. */
    if (board == 1 && entry->diagnostic) {
        /* TB322FC */
        if (any) {
            if (kshim_append_cstr(out, 2048, &used, " ") != 0)
                return -1;
        } else {
            any = 1;
        }
        if (kshim_append_cstr(out, 2048, &used, "rd.break=pre-mount") != 0)
            return -1;
    } else if (board == 2 && entry->diagnostic) {
        /* TB323FU */
        if (any) {
            if (kshim_append_cstr(out, 2048, &used, " ") != 0)
                return -1;
        } else {
            any = 1;
        }
        if (kshim_append_cstr(out, 2048, &used,
                "loglevel=8 ignore_loglevel printk.time=1 "
                "initcall_debug rd.debug") != 0)
            return -1;
    }

    /* root=UUID */
    if (any) {
        if (kshim_append_cstr(out, 2048, &used, " ") != 0)
            return -1;
    } else {
        any = 1;
    }
    if (kshim_append_cstr(out, 2048, &used, "root=UUID=") != 0)
        return -1;
    if (kshim_append_uuid(out, 2048, &used, entry->root_uuid) != 0)
        return -1;

    /* boot=UUID */
    if (kshim_append_cstr(out, 2048, &used, " boot=UUID=") != 0)
        return -1;
    if (kshim_append_uuid(out, 2048, &used, entry->boot_uuid) != 0)
        return -1;

    /* ostree=/ostree/armada/<commit>.<serial> */
    if (kshim_append_cstr(out, 2048, &used, " ostree=/ostree/armada/") != 0)
        return -1;
    if (kshim_append_cstr(out, 2048, &used, binding->commit) != 0)
        return -1;
    if (kshim_append_cstr(out, 2048, &used, ".") != 0)
        return -1;
    {
        char buf[32];
        int blen;
        blen = snprintf(buf, sizeof(buf), "%lu", (unsigned long)binding->serial);
        if (blen < 0 || (size_t)blen >= sizeof(buf))
            return -1;
        if (kshim_append(out, 2048, &used, buf, (size_t)blen) != 0)
            return -1;
    }

    /* armada.deployment=default/<commit>.<serial> */
    if (kshim_append_cstr(out, 2048, &used, " armada.deployment=default/") != 0)
        return -1;
    if (kshim_append_cstr(out, 2048, &used, binding->commit) != 0)
        return -1;
    if (kshim_append_cstr(out, 2048, &used, ".") != 0)
        return -1;
    {
        char buf[32];
        int blen;
        blen = snprintf(buf, sizeof(buf), "%lu", (unsigned long)binding->serial);
        if (blen < 0 || (size_t)blen >= sizeof(buf))
            return -1;
        if (kshim_append(out, 2048, &used, buf, (size_t)blen) != 0)
            return -1;
    }

    return 0;
}
