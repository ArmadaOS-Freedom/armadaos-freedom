#include "update_binding.h"
#include "storage_block.h"
#include <string.h>

static int is_ws(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

static int is_alnum_ascii(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
}

static int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

int kshim_update_binding_parse(const char *cmdline, size_t bytes, struct kshim_update_binding *out) {
    static const char key[] = "armada.deployment=";
    const size_t key_len = sizeof(key) - 1;
    const size_t bare_len = key_len - 1;
    if (!cmdline || !out || bytes < 1 || bytes > 65536) return KSHIM_STORAGE_ARG;
    const size_t len = bytes - 1;
    for (size_t i = 0; i < len; ++i) {
        if (cmdline[i] == '\0') return KSHIM_STORAGE_FORMAT;
    }
    if (cmdline[len] != '\0') return KSHIM_STORAGE_FORMAT;

    struct kshim_update_binding tmp;
    memset(&tmp, 0, sizeof(tmp));
    int found = 0;
    size_t i = 0;
    while (i < len) {
        while (i < len && is_ws(cmdline[i])) ++i;
        if (i == len) break;
        size_t start = i;
        while (i < len && !is_ws(cmdline[i])) ++i;
        size_t tok_len = i - start;
        const char *tok = cmdline + start;

        if (tok_len == bare_len && memcmp(tok, key, bare_len) == 0) return KSHIM_STORAGE_FORMAT;
        if (tok_len >= key_len && memcmp(tok, key, key_len) == 0) {
            if (found) return KSHIM_STORAGE_FORMAT;
            found = 1;
            const char *val = tok + key_len;
            size_t val_len = tok_len - key_len;
            if (val_len == 0) return KSHIM_STORAGE_FORMAT;

            size_t slash = 0;
            while (slash < val_len && val[slash] != '/') ++slash;
            if (slash == 0 || slash > 64 || slash == val_len) return KSHIM_STORAGE_FORMAT;
            if (!is_alnum_ascii(val[0])) return KSHIM_STORAGE_FORMAT;
            for (size_t j = 1; j < slash; ++j) {
                char c = val[j];
                if (!is_alnum_ascii(c) && c != '_' && c != '.' && c != '-') return KSHIM_STORAGE_FORMAT;
            }
            memcpy(tmp.os_name, val, slash);
            tmp.os_name[slash] = '\0';

            const char *rest = val + slash + 1;
            size_t rest_len = val_len - slash - 1;
            if (rest_len < 66 || rest[64] != '.') return KSHIM_STORAGE_FORMAT;
            for (size_t j = 0; j < 32; ++j) {
                int hi = hex_val(rest[j * 2]);
                int lo = hex_val(rest[j * 2 + 1]);
                if (hi < 0 || lo < 0) return KSHIM_STORAGE_FORMAT;
                tmp.commit[j] = (uint8_t)((hi << 4) | lo);
            }

            const char *ser = rest + 65;
            size_t ser_len = rest_len - 65;
            if (ser_len > 1 && ser[0] == '0') return KSHIM_STORAGE_FORMAT;
            uint32_t v = 0;
            for (size_t j = 0; j < ser_len; ++j) {
                char c = ser[j];
                if (c < '0' || c > '9') return KSHIM_STORAGE_FORMAT;
                uint32_t d = (uint32_t)(c - '0');
                if (v > 429496729U || (v == 429496729U && d > 5U)) return KSHIM_STORAGE_FORMAT;
                v = v * 10U + d;
            }
            tmp.serial = v;
        }
    }
    if (!found) return KSHIM_STORAGE_NOT_FOUND;
    memcpy(out, &tmp, sizeof(*out));
    return KSHIM_STORAGE_OK;
}
