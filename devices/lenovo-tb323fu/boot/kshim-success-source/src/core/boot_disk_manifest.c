/* boot_disk_manifest.c */
#include "boot_disk_manifest.h"

#include <string.h>

#include "bearssl_hash.h"
#include "bearssl_ec.h"

#define KSHIM_MAGIC0 0x41u /* 'A' */
#define KSHIM_MAGIC1 0x52u /* 'R' */
#define KSHIM_MAGIC2 0x4Du /* 'M' */
#define KSHIM_MAGIC3 0x42u /* 'B' */
#define KSHIM_MAGIC4 0x54u /* 'T' */
#define KSHIM_MAGIC5 0x56u /* 'V' */
#define KSHIM_MAGIC6 0x31u /* '1' */
#define KSHIM_MAGIC7 0x00u

#define KSHIM_VERSION 1u

#define KSHIM_MAX_KERNEL (128ull * 1024ull * 1024ull)
#define KSHIM_MAX_INITRD (128ull * 1024ull * 1024ull)
#define KSHIM_MAX_DTB    (256ull * 1024ull)

static uint32_t kshim_le32(const uint8_t *p)
{
    return (uint32_t)p[0]
         | ((uint32_t)p[1] << 8)
         | ((uint32_t)p[2] << 16)
         | ((uint32_t)p[3] << 24);
}

static uint64_t kshim_le64(const uint8_t *p)
{
    return (uint64_t)kshim_le32(p)
         | ((uint64_t)kshim_le32(p + 4) << 32);
}

int kshim_disk_manifest_verify(const uint8_t data[160],
                               const uint8_t *signature,
                               size_t sig_bytes,
                               const uint8_t public_key[65],
                               uint32_t expected_board,
                               struct kshim_disk_manifest *out)
{
    struct kshim_disk_manifest tmp;
    uint8_t hash[32];
    uint32_t version;
    uint32_t board;
    uint64_t kernel_bytes;
    uint64_t initrd_bytes;
    uint64_t dtb_bytes;
    size_t i;
    br_sha256_context sha;
    br_ec_public_key key;

    if (data == NULL || signature == NULL || public_key == NULL || out == NULL) {
        return 1;
    }

    /* ARMADA_BOOT boards: TB322FC 1, TB323FU 2, TB710FU 3 (device_board_has_disk_boot). */
    if (expected_board != 1u && expected_board != 2u && expected_board != 3u) {
        return 2;
    }

    /* Magic: "ARMBTV1\0" */
    if (data[0] != KSHIM_MAGIC0 || data[1] != KSHIM_MAGIC1 ||
        data[2] != KSHIM_MAGIC2 || data[3] != KSHIM_MAGIC3 ||
        data[4] != KSHIM_MAGIC4 || data[5] != KSHIM_MAGIC5 ||
        data[6] != KSHIM_MAGIC6 || data[7] != KSHIM_MAGIC7) {
        return 3;
    }

    version = kshim_le32(data + 8);
    if (version != KSHIM_VERSION) {
        return 4;
    }

    board = kshim_le32(data + 12);
    if (board != expected_board) {
        return 5;
    }

    kernel_bytes = kshim_le64(data + 16);
    initrd_bytes = kshim_le64(data + 24);
    dtb_bytes    = kshim_le64(data + 32);

    if (kernel_bytes == 0 || kernel_bytes > KSHIM_MAX_KERNEL) {
        return 6;
    }
    if (initrd_bytes == 0 || initrd_bytes > KSHIM_MAX_INITRD) {
        return 7;
    }
    if (dtb_bytes == 0 || dtb_bytes > KSHIM_MAX_DTB) {
        return 8;
    }

    /* Reserved trailing 24 bytes must be zero. */
    for (i = 136u; i < 160u; i++) {
        if (data[i] != 0u) {
            return 9;
        }
    }

    /* Raw public key prefix. Curve validity is enforced by BearSSL. */
    if (public_key[0] != 0x04u) {
        return 10;
    }

    /* Signature DER framing checks. */
    if (sig_bytes < 8u || sig_bytes > 72u) {
        return 11;
    }
    if (signature[0] != 0x30u) {
        return 12;
    }
    if ((size_t)signature[1] != sig_bytes - 2u) {
        return 13;
    }

    /* Hash the 160-byte wire image. */
    br_sha256_init(&sha);
    br_sha256_update(&sha, data, KSHIM_DISK_MANIFEST_WIRE_BYTES);
    br_sha256_out(&sha, hash);

    /* Verify ECDSA P-256 signature over hash. */
    key.curve = BR_EC_secp256r1;
    key.q = (unsigned char *)public_key;
    key.qlen = 65u;

    if (br_ecdsa_i31_vrfy_asn1(&br_ec_p256_m31,
                               hash, sizeof(hash),
                               &key,
                               signature, sig_bytes) != 1) {
        return 14;
    }

    /* Only now populate the output. */
    tmp.kernel_bytes = kernel_bytes;
    tmp.initrd_bytes = initrd_bytes;
    tmp.dtb_bytes    = dtb_bytes;
    memcpy(tmp.kernel_sha256, data + 40, 32);
    memcpy(tmp.initrd_sha256, data + 72, 32);
    memcpy(tmp.dtb_sha256,    data + 104, 32);

    *out = tmp;
    return 0;
}
