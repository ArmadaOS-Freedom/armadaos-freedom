/* SPDX-License-Identifier: GPL-3.0-only */
#define main existing_ab_selftests_main
#include "storage_ab_test.c"
#undef main

int main(int argc, char **argv)
{
    struct fixture f;
    unsigned bs;
    int ok = 1;

    if (argc != 4 ||
        (strcmp(argv[2], "512") != 0 && strcmp(argv[2], "4096") != 0) ||
        (strcmp(argv[3], "0") != 0 && strcmp(argv[3], "1") != 0))
        return 2;
    bs = strcmp(argv[2], "4096") == 0 ? 4096u : 512u;
    init(&f, bs);
    partition(&f, 5, 0, "efisp", 8, 15, "EFI-STUB");
    partition(&f, 5, 1, "recovery_a", 16, 23, "ANDROID!");
    partition(&f, 5, 2, "recovery_b", 24, 31, "ANDROID!");
    partition(&f, 5, 3, "vbmeta_system_a", 32, 39, "AVB0");
    partition(&f, 5, 4, "vbmeta_system_b", 40, 47, "AVB0");
    tables(&f, 5);
    if (argv[3][0] == '1' && kshim_ab_select(&f.media, 1, 0) != 0)
        ok = 0;
    for (unsigned lun = 0; ok && lun < 6; ++lun) {
        char path[4096];
        FILE *out;

        int written = snprintf(path, sizeof(path), "%s/lun%u.img", argv[1], lun);
        if (written < 0 || (size_t)written >= sizeof(path)) {
            ok = 0;
            break;
        }
        out = fopen(path, "wb");
        if (out == NULL) {
            ok = 0;
            break;
        }
        if (fwrite(f.disk[lun].bytes, bs, BLOCKS, out) != BLOCKS)
            ok = 0;
        if (fclose(out) != 0)
            ok = 0;
    }
    dispose(&f);
    return ok ? 0 : 1;
}
