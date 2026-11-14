// SPDX-License-Identifier: GPL-3.0-only
#ifndef ARMADA_UPDATE_UFS_BOOT_H
#define ARMADA_UPDATE_UFS_BOOT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Low-level UFS BSG bBootLunEn helper for offline A/B update integration.
 * Caller (kshim_ab_select) must separately verify that fd belongs to the exact
 * UFS controller anchored by the recovery partition and manage GPT/rollback.
 * Not ready for hardware deployment until caller transaction integration is verified.
 */

__attribute__((visibility("default")))
int armada_ufs_get_boot_lun(int fd, uint32_t *value);

__attribute__((visibility("default")))
int armada_ufs_set_boot_lun(int fd, uint32_t expected, uint32_t target);

#ifdef __cplusplus
}
#endif

#endif /* ARMADA_UPDATE_UFS_BOOT_H */
