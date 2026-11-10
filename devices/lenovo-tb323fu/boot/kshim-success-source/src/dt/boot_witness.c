#include <boot_witness.h>

#include <libfdt.h>
#include <stdint.h>
#include <string.h>

#define BOOT_WITNESS_MAX_BOOTARGS 65536

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

int kshim_boot_slot_read(const void *firmware_fdt)
{
    static const char bare_key[] = "androidboot.slot_suffix";
    static const size_t bare_len = sizeof(bare_key) - 1;

    const char *args;
    int args_len;
    size_t content_len;
    int found = -1;
    size_t i;

    if (!firmware_fdt || fdt_check_header(firmware_fdt) != 0) {
        return -1;
    }

    args = boot_witness_find_chosen_bootargs(firmware_fdt, &args_len);
    if (!args) {
        return -1;
    }

    /* Bound: at most 65536 bytes including terminating NUL. */
    if (args_len > BOOT_WITNESS_MAX_BOOTARGS) {
        return -1;
    }

    /* Require exactly one C string: final NUL, no embedded NUL. */
    if (args[args_len - 1] != '\0') {
        return -1;
    }

    content_len = (size_t)args_len - 1;
    if (memchr(args, '\0', content_len) != NULL) {
        return -1;
    }

    /* Tokenize over ASCII whitespace excluding the terminating NUL. */
    i = 0;
    while (i < content_len) {
        size_t start;
        size_t tok_len;
        const char *val;
        size_t val_len;
        int this_slot;

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
        tok_len = i - start;

        if (tok_len < bare_len ||
            memcmp(args + start, bare_key, bare_len) != 0) {
            continue;
        }

        if (tok_len == bare_len) {
            /* Bare exact key without '=' invalidates evidence. */
            return -1;
        }

        if (args[start + bare_len] != '=') {
            /* Prefix lookalike (e.g. androidboot.slot_suffix_extra=_a). */
            continue;
        }

        val = args + start + bare_len + 1;
        val_len = tok_len - (bare_len + 1);

        if (val_len == 2 && val[0] == '_' && val[1] == 'a') {
            this_slot = 0;
        } else if (val_len == 2 && val[0] == '_' && val[1] == 'b') {
            this_slot = 1;
        } else {
            /* Empty, quoted, or unrecognized slot suffix value. */
            return -1;
        }

        if (found != -1) {
            /* Any duplicate (even equal) is rejected as ambiguous/conflicting. */
            return -1;
        }

        found = this_slot;
    }

    return found;
}

int kshim_boot_lock_read(const void *firmware_fdt)
{
    static const char verified_key[] = "androidboot.verifiedbootstate";
    static const char vbmeta_key[] = "androidboot.vbmeta.device_state";
    static const char flash_key[] = "androidboot.flash.locked";
    static const size_t verified_len = sizeof(verified_key) - 1;
    static const size_t vbmeta_len = sizeof(vbmeta_key) - 1;
    static const size_t flash_len = sizeof(flash_key) - 1;

    const char *args;
    int args_len;
    size_t content_len;
    int verified_state = 0;
    int vbmeta_state = 0;
    int flash_state = 0;
    size_t i;

    if (!firmware_fdt || fdt_check_header(firmware_fdt) != 0) {
        return 0;
    }

    args = boot_witness_find_chosen_bootargs(firmware_fdt, &args_len);
    if (!args || args_len > BOOT_WITNESS_MAX_BOOTARGS) {
        return 0;
    }

    if (args[args_len - 1] != '\0') {
        return 0;
    }

    content_len = (size_t)args_len - 1;
    if (memchr(args, '\0', content_len) != NULL) {
        return 0;
    }

    i = 0;
    while (i < content_len) {
        size_t start;
        size_t tok_len;
        const char *val;
        size_t val_len;

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
        tok_len = i - start;

        if (tok_len >= verified_len &&
            memcmp(args + start, verified_key, verified_len) == 0) {
            int this_state;

            if (tok_len == verified_len) {
                return 0;
            }
            if (args[start + verified_len] != '=') {
                goto next_token;
            }

            val = args + start + verified_len + 1;
            val_len = tok_len - (verified_len + 1);
            if ((val_len == 5 && memcmp(val, "green", 5) == 0) ||
                (val_len == 6 && memcmp(val, "yellow", 6) == 0)) {
                this_state = 1;
            } else if (val_len == 6 && memcmp(val, "orange", 6) == 0) {
                this_state = 2;
            } else {
                return 0;
            }

            if (verified_state != 0) {
                return 0;
            }
            verified_state = this_state;
            continue;
        }

        if (tok_len >= vbmeta_len &&
            memcmp(args + start, vbmeta_key, vbmeta_len) == 0) {
            int this_state;

            if (tok_len == vbmeta_len) {
                return 0;
            }
            if (args[start + vbmeta_len] != '=') {
                goto next_token;
            }

            val = args + start + vbmeta_len + 1;
            val_len = tok_len - (vbmeta_len + 1);
            if (val_len == 6 && memcmp(val, "locked", 6) == 0) {
                this_state = 1;
            } else if (val_len == 8 && memcmp(val, "unlocked", 8) == 0) {
                this_state = 2;
            } else {
                return 0;
            }

            if (vbmeta_state != 0) {
                return 0;
            }
            vbmeta_state = this_state;
            continue;
        }

        if (tok_len >= flash_len &&
            memcmp(args + start, flash_key, flash_len) == 0) {
            int this_state;

            if (tok_len == flash_len) {
                return 0;
            }
            if (args[start + flash_len] != '=') {
                goto next_token;
            }

            val = args + start + flash_len + 1;
            val_len = tok_len - (flash_len + 1);
            if (val_len == 1 && val[0] == '1') {
                this_state = 1;
            } else if (val_len == 1 && val[0] == '0') {
                this_state = 2;
            } else {
                return 0;
            }

            if (flash_state != 0) {
                return 0;
            }
            flash_state = this_state;
            continue;
        }

next_token:
        ;
    }

    /* verifiedbootstate is mandatory; the other keys only corroborate it. */
    if (verified_state == 0 ||
        (vbmeta_state != 0 && vbmeta_state != verified_state) ||
        (flash_state != 0 && flash_state != verified_state)) {
        return 0;
    }

    return verified_state;
}

int kshim_boot_witness_export(void *linux_fdt, int booted_slot)
{
    static const char prop_name[] = "armada,boot-witness";
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

    if (booted_slot != 0 && booted_slot != 1) {
        /* Unknown slot: leave stale witness cleared and publish nothing. */
        return 0;
    }

    {
        const uint32_t cells[4] = {
            cpu_to_fdt32(1),            /* version 1 */
            cpu_to_fdt32(1),            /* source 1: firmware bootargs */
            cpu_to_fdt32((uint32_t)booted_slot),
            cpu_to_fdt32(0),            /* flags 0 */
        };

        err = fdt_setprop(linux_fdt, node, prop_name, cells, sizeof(cells));
        if (err != 0) {
            return err;
        }
    }

    return 0;
}

int kshim_boot_lock_export(void *linux_fdt, int state)
{
    static const char prop_name[] = "armada,boot-lock";
    int node;
    int err;

    if (!linux_fdt || fdt_check_header(linux_fdt) != 0) {
        return -FDT_ERR_BADMAGIC;
    }

    node = fdt_path_offset(linux_fdt, "/chosen");
    if (node < 0) {
        return node; /* do not invent /chosen */
    }

    /* Always clear stale evidence, including for unknown state. */
    err = fdt_delprop(linux_fdt, node, prop_name);
    if (err != 0 && err != -FDT_ERR_NOTFOUND) {
        return err;
    }

    if (state != 1 && state != 2) {
        return 0;
    }

    {
        const uint32_t cells[4] = {
            cpu_to_fdt32(1),            /* version 1 */
            cpu_to_fdt32(1),            /* source 1: firmware bootargs */
            cpu_to_fdt32((uint32_t)state),
            cpu_to_fdt32(0),            /* flags 0 */
        };

        err = fdt_setprop(linux_fdt, node, prop_name, cells, sizeof(cells));
        if (err != 0) {
            return err;
        }
    }

    return 0;
}
