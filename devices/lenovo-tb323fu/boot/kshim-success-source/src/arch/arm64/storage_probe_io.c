/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_probe_io.h>
#include <stddef.h>

extern const unsigned char arm64_storage_probe_read_instruction[];
extern const unsigned char arm64_storage_probe_read_failed[];
#if defined(CONFIG_KSHIM_UFS_NATIVE_IO) && CONFIG_KSHIM_UFS_NATIVE_IO
extern const unsigned char arm64_storage_ufs_write_instruction[];
extern const unsigned char arm64_storage_ufs_write_failed[];
#endif

bool arm64_storage_probe_fixup(ARM64_EXCEPTION_CONTEXT *context,
                               uint64_t exception_type)
{
    if (!context || exception_type != 0 ||
        ((context->esr >> 26) & 0x3f) != 0x25)
        return false;
    if (!(context->esr & (UINT64_C(1) << 6))) {
        if (context->elr != (uintptr_t)arm64_storage_probe_read_instruction)
            return false;
        context->elr = (uintptr_t)arm64_storage_probe_read_failed;
        return true;
    }
#if defined(CONFIG_KSHIM_UFS_NATIVE_IO) && CONFIG_KSHIM_UFS_NATIVE_IO
    /* Only the one exact STR below is recoverable; a broader write abort or an
     * asynchronous system error still belongs to the normal fault path. */
    if (context->elr == (uintptr_t)arm64_storage_ufs_write_instruction) {
        context->elr = (uintptr_t)arm64_storage_ufs_write_failed;
        return true;
    }
#endif
    return false;
}

#if !defined(KSHIM_HOST_TEST) || !KSHIM_HOST_TEST
#include <kshim_mmu.h>
#include <string.h>
extern int arm64_storage_probe_load32(uint64_t address, uint32_t *value);

/* The existing exception frame is EL1-only. Do not enable interrupts or alter
 * the controller to make an inaccessible window readable. Save and restore
 * the caller's vectors/masks around this one load. This can recover precise
 * synchronous read aborts, not a bus lockup or asynchronous system error. */
int storage_probe_native_read32(void *ctx, uint64_t address, uint32_t *value)
{
    (void)ctx;
    if (!value || (address & 3) ||
        !((address >= UINT64_C(0x01d84000) && address < UINT64_C(0x01d87000)) ||
          (address >= UINT64_C(0x15000000) && address < UINT64_C(0x15100000))))
        return -2;
    uint64_t masks, vectors;
    __asm__ volatile("mrs %0, daif\n\tmsr daifset, #0xf\n\tmrs %1, vbar_el1"
                     : "=r"(masks), "=r"(vectors) : : "memory");
    arm64_install_exception_vector();
    int status = arm64_storage_probe_load32(address, value);
    __asm__ volatile("msr vbar_el1, %0\n\tisb\n\tmsr daif, %1"
                     : : "r"(vectors), "r"(masks) : "memory");
    return status;
}

void storage_probe_native_capture(enum device_board actual_board,
                                   struct storage_probe_snapshot *out)
{
    if (!out) return;
    if (actual_board != DEVICE_BOARD_TB323FU && actual_board != DEVICE_BOARD_TB322FC &&
        actual_board != DEVICE_BOARD_TB710FU) {
        (void)storage_probe_collect(actual_board, storage_probe_native_read32, NULL, out);
        return;
    }
    uint64_t current_el, stack_select;
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(current_el));
    __asm__ volatile("mrs %0, SPSel" : "=r"(stack_select));
    if (current_el != 4 || stack_select != 1 || !kshim_mmu_is_enabled()) {
        memset(out, 0, sizeof(*out));
        out->version = STORAGE_PROBE_VERSION;
        out->board = actual_board;
        out->status = STORAGE_PROBE_ENV_UNAVAILABLE;
        return;
    }
    (void)storage_probe_collect(actual_board, storage_probe_native_read32, NULL, out);
}
#endif
