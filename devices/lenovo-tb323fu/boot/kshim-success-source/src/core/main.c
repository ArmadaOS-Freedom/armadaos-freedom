#include <kshim.h>
#include <pl011_uart.h>
#include <kshim_console.h>
#include <timer.h>
#include <gicv3.h>
#include <arm64/boot.h>
#include <manifest.h>
#include <platform.h>
#include <menu_text.h>
#include <device_info.h>
#include <boot_metrics.h>
#include <boot_witness.h>
#include <kshim_mmu.h>
#if defined(CONFIG_KSHIM_STORAGE_ENTRY_PROBE) && CONFIG_KSHIM_STORAGE_ENTRY_PROBE
#include <storage_probe_io.h>
#include <storage_probe_export.h>
static struct storage_probe_snapshot StorageEntrySnapshot;
const struct storage_probe_snapshot *storage_probe_entry_snapshot(void)
{
    return &StorageEntrySnapshot;
}
#endif
#if defined(CONFIG_KSHIM_UFS_READ_PROBE) && CONFIG_KSHIM_UFS_READ_PROBE
#include <storage_ufs_read_native.h>
#include <storage_ufs_read_export.h>
#endif
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
#include <storage_settings_native.h>
#include <psci.h>
#include <update_trial_boot.h>
#if CONFIG_KSHIM_QCOM_REBOOT
#include <qcom_reboot.h>
static uint32_t EdlRequestSeen;
static int EdlRequestStatus;
#if CONFIG_KSHIM_EDL_PROBE
static struct kshim_qcom_edl_probe EdlProbe;
#endif
#endif
#endif
#include <dt.h>
#include <tb323fu_android_touch.h>
#include <initrd.h>
#include <qcom_dt.h>
#include <dt_handoff.h>
#include <dt_simplefb.h>
#include <tb323fu_ramoops.h>
#include <tb323fu_ramoops_export.h>
#if defined(KSHIM_DIAGNOSTIC_MEMORY_ONLY) && (KSHIM_DIAGNOSTIC_MEMORY_ONLY != 0)
#include <diag_placement.h>
void arm64_diag_head_tail(uint64_t kernel_base, uint64_t dtb, uint64_t android_entry, uint64_t android_dtb, uint64_t flag_address) __attribute__((noreturn));
#endif
extern unsigned char __image_start_address__[];
extern unsigned char __runtime_end_address__[];
static uint64_t HandoffDtb[256 * 1024 / sizeof(uint64_t)]; /* bounded private DT scratch; 4 MiB linker assert applies */
/* Early, independent packed runtime-memory snapshot (32 KiB) and the DT
 * overlay/apply scratch (64 KiB); both bounded, aligned for FDT access, and
 * consumed only by the new dt_memory_capture/dt_memory_apply API. */
static __attribute__((aligned(64))) uint64_t
    RuntimeMemorySnapshot[DT_MEMORY_SNAPSHOT_BYTES / sizeof(uint64_t)];
static __attribute__((aligned(64))) uint64_t
    MemoryOverlay[DT_MEMORY_OVERLAY_BYTES / sizeof(uint64_t)];

#define PL011_UART_BASE 0x09000000ULL
#ifndef CONFIG_KSHIM_QEMU_PLATFORM
#define CONFIG_KSHIM_QEMU_PLATFORM 0
#endif

typedef struct {
    const MANIFEST *manifest;
    const MANIFEST_ENTRY *entries[MANIFEST_MAX_ENTRIES];
} MANIFEST_VIEW;

static bool is_executable_type(uint32_t type)
{
    return type == MF_ENTRY_TYPE_LINUX ||
        type == MF_ENTRY_TYPE_FREE_EXEC ||
        type == MF_ENTRY_TYPE_SHIM;
}

static bool is_flattened_device_tree(uint64_t address, uint64_t size)
{
    return address && size <= SIZE_MAX && dt_validate((const void *)(uintptr_t)address, (size_t)size) == 0;
}

static bool parse_manifest_entries(uint64_t manifest_address,
                                   uint64_t manifest_size,
                                   MANIFEST_VIEW *view)
{
    const MANIFEST *manifest = (const MANIFEST *)manifest_address;
    uint64_t cursor = sizeof(MANIFEST_HEADER);
    for (uint32_t index = 0; index < manifest->header.entry_count; index++) {
        if (cursor > manifest_size ||
            sizeof(MANIFEST_ENTRY) > manifest_size - cursor)
            return false;
        const MANIFEST_ENTRY *entry =
            (const MANIFEST_ENTRY *)(manifest_address + cursor);
        if (entry->name_len == 0)
            return false;
        uint64_t record_size = sizeof(MANIFEST_ENTRY) + entry->name_len;
        record_size = (record_size + MANIFEST_ENTRY_ALIGNMENT - 1) &
            ~(uint64_t)(MANIFEST_ENTRY_ALIGNMENT - 1);
        if (record_size > manifest_size - cursor)
            return false;
        for (uint32_t name_index = 0; name_index < entry->name_len;
             name_index++) {
            uint8_t character = (uint8_t)entry->name[name_index];
            if (character < 0x20 || character == 0x7f || character == ';')
                return false;
        }
        view->entries[index] = entry;
        cursor += record_size;
    }
    return cursor == manifest_size;
}

static bool get_manifest(uint64_t linux_base,
                         uint64_t manifest_address,
                         uint64_t manifest_size,
                         MANIFEST_VIEW *view)
{
    if (manifest_address < linux_base ||
        manifest_size < sizeof(MANIFEST_HEADER))
        return false;

    const MANIFEST *manifest = (const MANIFEST *)manifest_address;
    if (manifest->header.magic != MANIFEST_MAGIC ||
        manifest->header.version.major != MANIFEST_VERSION_MAJOR ||
        manifest->header.version.minor != MANIFEST_VERSION_MINOR ||
        manifest->header.entry_count < 2 ||
        manifest->header.entry_count > MANIFEST_MAX_ENTRIES ||
        manifest->header.default_entry >= manifest->header.entry_count)
        return false;

    uint64_t manifest_offset = manifest_address - linux_base;
    if (manifest_address > UINT64_MAX - manifest_size ||
        !parse_manifest_entries(manifest_address, manifest_size, view))
        return false;
    view->manifest = manifest;

    uint32_t attached_shim_index = manifest->header.entry_count - 1;
    const MANIFEST_ENTRY *attached_shim = view->entries[attached_shim_index];
    uint64_t attached_shim_offset =
        *(const uint64_t *)(linux_base + 0x30);
    if (manifest->header.default_entry == attached_shim_index ||
        attached_shim->type != MF_ENTRY_TYPE_SHIM ||
        attached_shim->flags != MF_ENTRY_FLAG_EXECUTABLE ||
        attached_shim->offset != attached_shim_offset ||
        attached_shim->size == 0 || attached_shim->entry_offset != 0)
        return false;

    const MANIFEST_ENTRY *base_entry = view->entries[0];
    if (base_entry->type != MF_ENTRY_TYPE_LINUX ||
        (base_entry->flags &
         (MF_ENTRY_FLAG_BASE_IMAGE | MF_ENTRY_FLAG_EXECUTABLE)) !=
            (MF_ENTRY_FLAG_BASE_IMAGE | MF_ENTRY_FLAG_EXECUTABLE) ||
        (base_entry->flags & ~(MF_ENTRY_FLAG_BASE_IMAGE |
                               MF_ENTRY_FLAG_EXECUTABLE |
                               MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB)) !=
            0 ||
        base_entry->offset != 0)
        return false;

    uint64_t runtime_image_size = *(const uint64_t *)(linux_base + 0x10);
    if (linux_base > UINT64_MAX - runtime_image_size)
        return false;
    uint64_t packed_image_end = linux_base + runtime_image_size;

    for (uint32_t index = 0; index < manifest->header.entry_count; index++) {
        const MANIFEST_ENTRY *entry = view->entries[index];
        bool executable =
            (entry->flags & MF_ENTRY_FLAG_EXECUTABLE) != 0;
        if (executable != is_executable_type(entry->type))
            return false;
        bool copy_image = (entry->flags & MF_ENTRY_FLAG_COPY) != 0;
        bool has_dtb =
            (entry->flags & MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB) != 0;
        uint32_t allowed_flags = index == 0
            ? MF_ENTRY_FLAG_BASE_IMAGE | MF_ENTRY_FLAG_EXECUTABLE |
              MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB
            : MF_ENTRY_FLAG_COPY | MF_ENTRY_FLAG_EXECUTABLE |
              MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB |
              MF_ENTRY_FLAG_HAS_QC_ABOOT_HEADER;
        if ((entry->flags & ~allowed_flags) != 0 ||
            entry->offset > manifest_offset ||
            entry->size > manifest_offset - entry->offset)
            return false;
        if (entry->alignment < MANIFEST_ALIGNMENT_SHIFT_MIN ||
            entry->alignment > MANIFEST_ALIGNMENT_SHIFT_MAX)
            return false;
        if ((entry->flags & MF_ENTRY_FLAG_HAS_QC_ABOOT_HEADER) &&
            entry->type != MF_ENTRY_TYPE_LINUX)
            return false;
        if (executable &&
            (entry->entry_offset > entry->size ||
             entry->size - entry->entry_offset < 4 ||
             (entry->entry_offset & 3) != 0))
            return false;
        if (!has_dtb && entry->device_tree_blob_entry != 0)
            return false;
        if (!executable &&
            (copy_image || entry->entry_offset != 0 || has_dtb))
            return false;
        if (copy_image) {
            if (!executable || entry->load_address == 0 ||
                (entry->load_address & 3) != 0 ||
                entry->copy_size_max == 0 ||
                entry->size > entry->copy_size_max)
                return false;
            if (entry->load_address > UINT64_MAX - entry->size)
                return false;
            uint64_t copy_end = entry->load_address + entry->size;
            if (entry->load_address < packed_image_end &&
                copy_end > linux_base)
                return false;
        } else if (entry->load_address != 0 || entry->copy_size_max != 0) {
            return false;
        }
        if (entry->type == MF_ENTRY_TYPE_DTB &&
            !is_flattened_device_tree(linux_base + entry->offset,
                                      entry->size)) {
            return false;
        }
        if (has_dtb) {
            if (!executable ||
                entry->device_tree_blob_entry >=
                    manifest->header.entry_count) {
                return false;
            }
            const MANIFEST_ENTRY *dtb =
                view->entries[entry->device_tree_blob_entry];
            if (dtb->type != MF_ENTRY_TYPE_DTB || dtb->flags != 0 ||
                !is_flattened_device_tree(linux_base + dtb->offset,
                                          dtb->size)) {
                return false;
            }
        }
    }

    if ((view->entries[manifest->header.default_entry]->flags &
            MF_ENTRY_FLAG_EXECUTABLE) == 0)
        return false;

    return true;
}

static void sync_image_cache(uint64_t address, uint64_t size)
{
    uint64_t ctr_el0 = 0;
    asm volatile("mrs %0, ctr_el0" : "=r"(ctr_el0));

    uint64_t data_line_size = 4ULL << ((ctr_el0 >> 16) & 0xf);
    uint64_t instruction_line_size = 4ULL << (ctr_el0 & 0xf);
    uint64_t end = address + size;

    for (uint64_t current = address & ~(data_line_size - 1);
         current < end; current += data_line_size)
        asm volatile("dc cvau, %0" : : "r"(current) : "memory");
    asm volatile("dsb ish" : : : "memory");

    for (uint64_t current = address & ~(instruction_line_size - 1);
         current < end; current += instruction_line_size)
        asm volatile("ic ivau, %0" : : "r"(current) : "memory");
    asm volatile("dsb ish\nisb" : : : "memory");
}

#if (defined(CONFIG_KSHIM_TB323FU_ANDROID_TOUCH_4M8) && \
     CONFIG_KSHIM_TB323FU_ANDROID_TOUCH_4M8) || KSHIM_TB323FU_ANDROID_TOUCH_4M8_TRIAL
/* The Android tree stays at ABL's original address. Make the one-cell update
 * visible at PoC before the original kernel changes its cache/MMU state. */
static void clean_android_trial_fdt(const void *fdt)
{
    uint64_t ctr;
    asm volatile("mrs %0, ctr_el0" : "=r"(ctr));
    const uint64_t line = 4ULL << ((ctr >> 16) & 0xf);
    const uint64_t base = (uint64_t)(uintptr_t)fdt;
    const uint64_t end = base + fdt_totalsize(fdt);
    for (uint64_t p = base & ~(line - 1); p < end; p += line)
        asm volatile("dc cvac, %0" : : "r"(p) : "memory");
    asm volatile("dsb sy" : : : "memory");
}
#endif

static void print_entry_name(const MANIFEST_ENTRY *entry)
{
    uint32_t remaining = entry->name_len;
    const char *current = entry->name;
    while (remaining != 0) {
        int chunk = remaining > 1024 ? 1024 : (int)remaining;
        kshim_console_write(current, chunk);
        current += chunk;
        remaining -= (uint32_t)chunk;
    }
}

static uint32_t select_image(const MANIFEST_VIEW *view, uint64_t fdt, uint64_t linux_base,
                             const struct device_info *info, uint32_t *boot_target)
{
    uint32_t indexes[MANIFEST_MAX_ENTRIES];
    char labels[MANIFEST_MAX_ENTRIES][96];
    const char *names[MANIFEST_MAX_ENTRIES];
    const char *descriptions[MANIFEST_MAX_ENTRIES];
    kshim_menu_role_t roles[MANIFEST_MAX_ENTRIES];
#if defined(CONFIG_KSHIM_MENU_SESSION_SETTINGS) && CONFIG_KSHIM_MENU_SESSION_SETTINGS
    enum device_board payload_boards[MANIFEST_MAX_ENTRIES];
#endif
    uint64_t advanced_mask = 0;
    size_t count = 0, initial = 0, selected = 0;
    for (uint32_t i = 0; i + 1 < view->manifest->header.entry_count; ++i) {
        const MANIFEST_ENTRY *entry = view->entries[i];
        if (!(entry->flags & MF_ENTRY_FLAG_EXECUTABLE)) continue;
#if defined(CONFIG_KSHIM_MENU_SESSION_SETTINGS) && CONFIG_KSHIM_MENU_SESSION_SETTINGS
        payload_boards[count] = DEVICE_BOARD_UNKNOWN;
#endif
        size_t n = entry->name_len < sizeof(labels[0]) ? entry->name_len : sizeof(labels[0]) - 1;
        memcpy(labels[count], entry->name, n); labels[count][n] = 0;
        indexes[count] = i; names[count] = labels[count];
        descriptions[count] = (entry->flags & MF_ENTRY_FLAG_BASE_IMAGE) != 0 ?
            KSHIM_MENU_DESC_BASE : KSHIM_MENU_DESC_GENERIC;
        /* Known roles localise their own label/description; unknown stock images
         * keep the manifest name so they are never assumed to be Android. */
        roles[count] = (entry->flags & MF_ENTRY_FLAG_BASE_IMAGE) != 0 ?
            KSHIM_MENU_ROLE_BASE : KSHIM_MENU_ROLE_UNKNOWN;
#if defined(CONFIG_KSHIM_MENU_BASE_ANDROID) && CONFIG_KSHIM_MENU_BASE_ANDROID
        if ((entry->flags & MF_ENTRY_FLAG_BASE_IMAGE) != 0 &&
            (entry->flags & MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB) == 0) {
            descriptions[count] = KSHIM_MENU_DESC_ANDROID;
            roles[count] = KSHIM_MENU_ROLE_ANDROID;
        }
#endif
        if ((entry->flags & MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB) != 0) {
            const MANIFEST_ENTRY *dtb = view->entries[entry->device_tree_blob_entry];
            const void *blob = (const void *)(uintptr_t)(linux_base + dtb->offset);
#if defined(CONFIG_KSHIM_MENU_SESSION_SETTINGS) && CONFIG_KSHIM_MENU_SESSION_SETTINGS
            struct device_info payload_info;
            device_info_read(blob, &payload_info);
            payload_boards[count] = payload_info.board;
#endif
            int chosen = fdt_path_offset(blob, "/chosen");
            const char *stage = chosen >= 0 ? dt_string(blob, chosen, "armada,boot-stage") : NULL;
            const char *medium = chosen >= 0 ? dt_string(blob, chosen, "armada,root-medium") : NULL;
            descriptions[count] = KSHIM_MENU_DESC_GENERIC;
            roles[count] = KSHIM_MENU_ROLE_UNKNOWN;
            if (count < 64 && stage && strcmp(stage, "maintenance") == 0) {
                advanced_mask |= UINT64_C(1) << count;
                descriptions[count] = KSHIM_MENU_DESC_DIAGNOSTIC;
                roles[count] = KSHIM_MENU_ROLE_DIAGNOSTIC;
            } else if (medium && strcmp(medium, "internal") == 0) {
                descriptions[count] = KSHIM_MENU_DESC_INTERNAL;
                roles[count] = KSHIM_MENU_ROLE_INTERNAL;
            } else if (medium && strcmp(medium, "tf") == 0) {
                descriptions[count] = KSHIM_MENU_DESC_TF;
                roles[count] = KSHIM_MENU_ROLE_TF;
            }
        }
        if (i == view->manifest->header.default_entry) initial = count;
        ++count;
    }
#if defined(CONFIG_KSHIM_MENU_SESSION_SETTINGS) && CONFIG_KSHIM_MENU_SESSION_SETTINGS
    if (kshim_platform_select_localized_settings(fdt, names, descriptions, roles, info,
            payload_boards, count, initial, view->manifest->header.timeout_ms,
            advanced_mask, &selected)) {
#else
    if (kshim_platform_select_localized_info(fdt, names, descriptions, roles, info, count, initial,
            view->manifest->header.timeout_ms, advanced_mask, &selected)) {
#endif
        printf("Unsafe UI shutdown; refusing payload handoff\n");
        for (;;) (void)delay(100000);
    }
    *boot_target=roles[selected]==KSHIM_MENU_ROLE_ANDROID ? 1u :
                 roles[selected]==KSHIM_MENU_ROLE_INTERNAL ? 2u :
                 roles[selected]==KSHIM_MENU_ROLE_TF ? 3u : 0u;
    return indexes[selected];
}

extern void _arm64_mmu_setup(void);

/* Last operation before leaving the runtime, after all boot diagnostics. */
static void prepare_boot(void)
{
    if (kshim_platform_prepare_handoff() || kshim_console_quiesce()) {
        printf("Device shutdown incomplete; refusing payload handoff\n");
        for (;;) (void)delay(100000);
    }
}

#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
static void remember_selected_boot(uint32_t target,uint64_t selected_fdt)
{
    if (target>=1 && target<=3) {
        int rc=storage_settings_native_note_boot(target);
        if (rc && rc!=KSHIM_STORAGE_NOT_FOUND)
            printf("Last boot setting could not be saved: %d\n",rc);
        if (!storage_settings_native_handoff_safe()) {
            __asm__ volatile("msr daifset, #0xf" ::: "memory");
            for (;;) __asm__ volatile("wfe");
        }
    }
    /* Refresh evidence after history changed. Only our private Linux DT is
     * writable; the firmware's Android DT is never edited here. */
    if (selected_fdt==(uint64_t)(uintptr_t)HandoffDtb) {
        void *blob=HandoffDtb;
        if (fdt_open_into(blob,blob,sizeof(HandoffDtb))==0) {
            int status=storage_settings_native_export(blob);
            int chosen=fdt_path_offset(blob,"/chosen");
            if (chosen>=0)
                (void)fdt_setprop_u32(blob,chosen,"kshim,settings-export-status",(uint32_t)status);
            if (fdt_pack(blob) || dt_validate(blob,sizeof(HandoffDtb))) {
                __asm__ volatile("msr daifset, #0xf" ::: "memory");
                for (;;) __asm__ volatile("wfe");
            }
        }
    }
}
#endif

int kshim_main(
    uint64_t device_tree_address,
    uint64_t kernel_arg1,
    uint64_t kernel_arg2,
    uint64_t kernel_arg3,
    uint64_t linux_base,
    uint64_t manifest_address,
    uint64_t manifest_size)
{
    kshim_boot_metrics_record_once(KSHIM_BOOT_STAGE_ENTRY);
#if KSHIM_DIAGNOSTIC_DT_ONLY
    /* DT-only diagnostic: preserve the exact incoming DT pointer before the
     * shared validation below can clear device_tree_address to 0.  The
     * handoff must forward the original pointer even when the blob is not a
     * valid FDT. */
    const uint64_t dt_only_device_tree = device_tree_address;
#endif
#if KSHIM_DIAGNOSTIC_RESOURCES_ONLY
    /* Resource-only diagnostic: preserve the exact incoming DT pointer before
     * the shared validation below can clear device_tree_address to 0, plus an
     * entry timestamp for the millisecond resource-probe delta.  The handoff
     * must forward the original pointer even when the blob is not a valid
     * FDT. */
    const uint64_t resources_only_device_tree = device_tree_address;
    const uint64_t resources_only_entry_time = get_current_time();
#endif
#if KSHIM_DIAGNOSTIC_BASE_ONLY
    /* Diagnostic only: hand off to the packed Linux base image immediately,
     * before any DT, console, or peripheral work.  No menu is shown and no
     * peripherals are probed.  arm64_boot_image is declared noreturn and
     * performs the existing handoff (DAIF/timer masks). */
    if (linux_base)
        arm64_boot_image(linux_base + 4, device_tree_address,
                         kernel_arg1, kernel_arg2, kernel_arg3);
    for (;;)
        asm volatile("wfe");
#endif
    const void *runtime_fdt = (const void *)(uintptr_t)device_tree_address;
    if (!runtime_fdt || dt_validate(runtime_fdt, fdt_totalsize(runtime_fdt))) {
        runtime_fdt = NULL;
        device_tree_address = 0;
    }
    /* Freeze firmware slot and boot-lock evidence before any mutation. */
    const int booted_slot = kshim_boot_slot_read(runtime_fdt);
    const int boot_lock_state = kshim_boot_lock_read(runtime_fdt);
    /* Turn translation on before anything else touches memory.  Up to this
     * point every fetch, load and store runs uncached and strictly ordered,
     * which on this platform costs tens of seconds before the menu appears.
     * kshim_mmu_configure_early() verifies the runtime load base against the
     * image header first, and a rejected configuration leaves the context
     * exactly as it was, so translation simply stays off and the previous
     * behaviour is preserved. */
    const kshim_mmu_early_config_t early_mmu = {
        .fdt = runtime_fdt,
        .payload_base = linux_base,
        .payload_end = (manifest_address != 0U && manifest_size != 0U)
            ? manifest_address + manifest_size : 0U,
    };
    const int early_mmu_status = kshim_mmu_configure_early(&early_mmu);
    kshim_boot_metrics_set_mmu(kshim_mmu_is_enabled() ? 1U : 0U, early_mmu_status);
    /* Read-only device info from the already validated runtime DT, collected
     * once here and handed to the menu. A NULL runtime FDT stays all-unknown;
     * there is no extra FDT probing or validation path. */
    struct device_info device_info = {0};
    if (runtime_fdt)
        device_info_read(runtime_fdt, &device_info);
#if defined(CONFIG_KSHIM_STORAGE_ENTRY_PROBE) && CONFIG_KSHIM_STORAGE_ENTRY_PROBE
#if defined(CONFIG_KSHIM_UFS_READ_PROBE) && CONFIG_KSHIM_UFS_READ_PROBE
#if defined(CONFIG_KSHIM_UFS_READ_SCREEN) && CONFIG_KSHIM_UFS_READ_SCREEN
    /* Screen profile: only the actual TB322FC with a validated runtime FDT,
     * and only the scanout is touched - no resources, menu, IRQ, touch or SMP
     * initialization happens before the capture. An unavailable display
     * leaves the existing IO behaviour byte-for-byte unchanged. */
    if (runtime_fdt && device_info.board == DEVICE_BOARD_TB322FC)
        (void)storage_ufs_read_screen_init(runtime_fdt);
#endif
    /* Actual incoming board identity; the native wrapper proves the CPU/DMA
     * preconditions, claims and releases the host, and records one report. */
    storage_ufs_read_native_capture(device_info.board, &StorageEntrySnapshot);
    if (!storage_ufs_read_entry_report()->handoff_safe) {
        /* The firmware state was not restored. There is no safe handoff, so
         * stop here before resources, menu and any payload jump. */
        __asm__ volatile("msr daifset, #0xf" ::: "memory");
        for (;;)
            __asm__ volatile("wfe");
    }
#else
    /* Actual incoming board identity; no menu selection influences MMIO. */
    storage_probe_native_capture(device_info.board, &StorageEntrySnapshot);
#endif
#endif
    /* Early, read-only ramoops capture from the validated runtime DT, taken
     * exactly once before the memory snapshot/console/resources work.  The
     * layout starts zeroed (valid == false) so a failed capture never leaves a
     * consumable layout behind; the returned status is preserved for the
     * normal-Linux bind decision and the recovery-only entry below. */
    tb323fu_ramoops_layout ramoops_layout = {0};
    int ramoops_capture_status = tb323fu_ramoops_capture(
        runtime_fdt,
        runtime_fdt ? fdt_totalsize(runtime_fdt) : 0,
        &ramoops_layout);
#if KSHIM_RAMOOPS_RECOVERY_ONLY
    /* Recovery-only entry (compile-time): runs immediately after runtime DT
     * validation and the read-only capture and before the memory snapshot,
     * console, resources or UI exist.  No menu, no Linux, no prepare_boot and
     * no new MMIO; the only RAM traffic is the already-validated runtime FDT
     * edited by the export helper plus a physical console read that is issued
     * only when the capture succeeded.  The handoff reuses the BASE_ONLY
     * parameters.  A missing runtime FDT skips the report and never reads RAM;
     * a missing base image wfe-waits. */
    if (runtime_fdt) {
        const volatile uint8_t *region = NULL;
        size_t region_len = 0;
        if (ramoops_capture_status == TB323FU_RAMOOPS_OK) {
            region = (const volatile uint8_t *)(uintptr_t)ramoops_layout.base;
            region_len = (size_t)ramoops_layout.size;
        }
        (void)tb323fu_ramoops_export(
            (void *)runtime_fdt, region, region_len, &ramoops_layout,
            ramoops_capture_status,
            "kshim.diag_candidate=m2-pc1-20261006");
    }
    if (linux_base)
        arm64_boot_image(linux_base + 4, device_tree_address,
                         kernel_arg1, kernel_arg2, kernel_arg3);
    for (;;)
        asm volatile("wfe");
#endif
    /* Capture the packed runtime-memory snapshot exactly once, after runtime
     * DT validation and before console/resources/UI.  A NULL runtime FDT
     * (equivalent to dt_validate(NULL, 0)) is a clean failure: the snapshot is
     * only consumed by dt_memory_apply and no code path uses an invalid
     * snapshot or output. */
    int memory_status = dt_memory_capture(
        runtime_fdt,
        runtime_fdt ? fdt_totalsize(runtime_fdt) : 0,
        RuntimeMemorySnapshot,
        sizeof(RuntimeMemorySnapshot));
#if KSHIM_DIAGNOSTIC_DT_ONLY
    /* DT-only diagnostic entry (compile-time): the runtime DT and the
     * ramoops layout were captured read-only and the memory snapshot was
     * taken once.  Only two ASCII reports are formatted in local buffers and
     * offered to dt_diagnostic_report_slots(); any report failure is ignored
     * for boot, so an unavailable report still returns to Android rather than
     * halting.  The original incoming DT pointer is forwarded unchanged even
     * when DT validation zeroed the local copy, and x1..x3 are preserved. */
    if (runtime_fdt) {
        char probe_report[81];
        char s0_report[81];
        int np = snprintf(probe_report, sizeof(probe_report),
                          "dt1:b=%u:s=%u:sz=%u:mem=%d:ro=%d",
                          (unsigned)device_info.board,
                          (unsigned)device_info.soc,
                          (unsigned)fdt_totalsize(runtime_fdt),
                          memory_status, ramoops_capture_status);
        int ns = snprintf(s0_report, sizeof(s0_report),
                          "dt1:known=%u:ram=%llu",
                          (unsigned)device_info.ram_known,
                          (unsigned long long)device_info.dt_reported_ram_bytes);
        if (np > 0 && np < (int)sizeof(probe_report) &&
            ns > 0 && ns < (int)sizeof(s0_report))
            (void)dt_diagnostic_report_slots((void *)runtime_fdt,
                                             probe_report, s0_report);
    }
    if (linux_base)
        arm64_boot_image(linux_base + 4, dt_only_device_tree,
                         kernel_arg1, kernel_arg2, kernel_arg3);
    for (;;)
        asm volatile("wfe");
#endif
    (void)kshim_console_init(runtime_fdt);
#if KSHIM_DIAGNOSTIC_RESOURCES_ONLY
    /* Resource-only diagnostic checkpoint: capture the millisecond timestamps
     * at the actual resources_init boundary and retain the init status.  A
     * missing or invalid runtime FDT is never probed and records a negative
     * status instead.  This build never consumes the permanent resources
     * failure delay loop; every non-diagnostic build keeps the original call
     * order and error handling below byte-for-byte. */
    const uint64_t resources_only_before_time = get_current_time();
    int resources_only_status = -1;
    if (runtime_fdt)
        resources_only_status = kshim_resources_init(
            runtime_fdt, fdt_totalsize(runtime_fdt));
    const uint64_t resources_only_after_time = get_current_time();
#else
    if (runtime_fdt && kshim_resources_init(runtime_fdt, fdt_totalsize(runtime_fdt))) {
        printf("Invalid runtime device resources\n");
        for (;;) (void)delay(100000);
    }
#endif
    kshim_boot_metrics_record_once(KSHIM_BOOT_STAGE_RESOURCES);
#if KSHIM_DIAGNOSTIC_RESOURCES_ONLY
    /* Diagnostic checkpoint after DT/console/resource probing: optional
     * resources may be absent and no menu is shown.  A valid runtime FDT and
     * base image are required, and the console must quiesce successfully
     * before the noreturn handoff to the packed base image. */
    if (runtime_fdt && linux_base) {
        const struct kshim_resources *r = kshim_resources_get();
        char report[81];
        char res_report[81];
        int n;
        int nres;
#if CONFIG_KSHIM_FRAMEBUFFER
        n = kshim_splash_trace_report(kshim_splash_trace_get(), r->splash_status,
                                      report, sizeof(report));
#else
        n = snprintf(report, sizeof(report),
            "r14:s=%d:n=0:o=0:v=0,0,0,0,0", r->splash_status);
#endif
        /* Second diagnostic value: resources_init status and counts plus the
         * entry->before and before->after millisecond deltas (get_current_time
         * is milliseconds). */
        nres = snprintf(res_report, sizeof(res_report),
                        "res1:rc=%d:sp=%d:n=%u:m=%u:dt=%llu:res=%llu",
                        resources_only_status, r->spmi_status,
                        (unsigned)r->spmi_count, (unsigned)r->mapping_count,
                        (unsigned long long)(resources_only_before_time -
                                             resources_only_entry_time),
                        (unsigned long long)(resources_only_after_time -
                                             resources_only_before_time));
        /* FIRST: atomically convert the two ordinary source placeholders and
         * fill both values in one call; any report failure is ignored for
         * boot. */
        if (n > 0 && n < (int)sizeof(report) &&
            nres > 0 && nres < (int)sizeof(res_report))
            (void)dt_diagnostic_report_slots((void *)runtime_fdt, report,
                                             res_report);
        /* THEN: the prior Gen5 diagnostic write-once path is retained
         * unchanged.  On a new source-slot image it harmlessly refuses the
         * already-written targets; with older androidboot pending slots the
         * legacy report continues as before. */
        if (n > 0 && n < (int)sizeof(report))
            (void)dt_probe_report((void *)runtime_fdt, report);
#if defined(KSHIM_DIAGNOSTIC_MENU_MMU) && KSHIM_DIAGNOSTIC_MENU_MMU
        {
            uint64_t raw_el = 0, sctlr = 0;
            struct dt_range reservations[128];
            bool no_map[128];
            size_t reserved_count = 0;
            int reserved_status;
            int config_status = -4;
#if defined(KSHIM_DIAGNOSTIC_MMU_CYCLE) && KSHIM_DIAGNOSTIC_MMU_CYCLE
            int cycle_enable = -5;
            int cycle_active = -1;
            int cycle_prepare = -5;
            int cycle_after = -1;
            uint64_t cycle_sctlr = 0;
#endif

            __asm__ volatile("mrs %0, CurrentEL" : "=r"(raw_el));
            if (raw_el == 4)
                __asm__ volatile("mrs %0, sctlr_el1" : "=r"(sctlr));
            else if (raw_el == 8)
                __asm__ volatile("mrs %0, sctlr_el2" : "=r"(sctlr));

            reserved_status = dt_reservations(runtime_fdt, reservations,
                                              no_map, 128,
                                              &reserved_count);

            if (raw_el == 4 || raw_el == 8) {
                if (sctlr & 1)
                    config_status = -3;
                else if (r->splash_status)
                    config_status = -2;
                else
                    config_status = kshim_mmu_configure_default(runtime_fdt);
            }

#if defined(KSHIM_DIAGNOSTIC_MMU_CYCLE) && KSHIM_DIAGNOSTIC_MMU_CYCLE
            /* Enable/disable cycle only: no FDT write, no print, no
             * framebuffer/keys/SPMI/menu/interrupt changes between enable
             * and prepare.  Prepare is called even if enable returned an
             * error, since a configured state may still need cleanup. */
            if (config_status == 0) {
                cycle_enable = kshim_mmu_enable_current_cpu();
                cycle_active = kshim_mmu_is_enabled();
                cycle_prepare = kshim_mmu_prepare_handoff();
                cycle_after = kshim_mmu_is_enabled();
                if (raw_el == 4)
                    __asm__ volatile("mrs %0, sctlr_el1" : "=r"(cycle_sctlr));
                else if (raw_el == 8)
                    __asm__ volatile("mrs %0, sctlr_el2" : "=r"(cycle_sctlr));
            }
#endif

            n = snprintf(report, sizeof(report),
                         "r22:el=%u:ctl=%llx:cfg=%d:map=%u",
                         (unsigned)(raw_el >> 2),
                         (unsigned long long)sctlr, config_status,
                         (unsigned)r->mapping_count);
            if (n > 0 && n < (int)sizeof(report))
                (void)dt_probe_report_slot((void *)runtime_fdt, 0, report);

            n = snprintf(report, sizeof(report),
                         "r22:root=%llx:frq=%llu:fb=%llx:size=%llu",
                         (unsigned long long)kshim_mmu_root_table(),
                         (unsigned long long)arm64_read_cntfrq(),
                         (unsigned long long)r->framebuffer.render_address,
                         (unsigned long long)r->framebuffer.buffer_size);
            if (n > 0 && n < (int)sizeof(report))
                (void)dt_probe_report_slot((void *)runtime_fdt, 1, report);

            n = snprintf(report, sizeof(report),
                         "r22:rsv=%d:n=%u:fdt=%llx:sz=%u",
                         reserved_status, (unsigned)reserved_count,
                         (unsigned long long)(uintptr_t)runtime_fdt,
                         (unsigned)fdt_totalsize(runtime_fdt));
            if (n > 0 && n < (int)sizeof(report))
                (void)dt_probe_report_slot((void *)runtime_fdt, 2, report);

            n = snprintf(report, sizeof(report),
                         "r22:mmu=%d:spmi=%d:count=%u:splash=%d",
                         kshim_mmu_is_enabled(), r->spmi_status,
                         (unsigned)r->spmi_count, r->splash_status);
            if (n > 0 && n < (int)sizeof(report))
                (void)dt_probe_report_slot((void *)runtime_fdt, 3, report);

#if defined(KSHIM_DIAGNOSTIC_MMU_CYCLE) && KSHIM_DIAGNOSTIC_MMU_CYCLE
            n = snprintf(report, sizeof(report),
                         "r23:on=%d:m=%d:off=%d:m0=%d:ctl=%llx",
                         cycle_enable, cycle_active, cycle_prepare,
                         cycle_after, (unsigned long long)cycle_sctlr);
            if (n > 0 && n < (int)sizeof(report))
                (void)dt_probe_report_slot((void *)runtime_fdt, 4, report);
#endif
        }
#else
#if CONFIG_KSHIM_FRAMEBUFFER
        {
            unsigned slot;
            for (slot = 0; slot < 9; slot++) {
                n = kshim_splash_snapshot_report(
                    kshim_splash_snapshot_get(), slot, report,
                    sizeof(report));
                if (n > 0 && n < (int)sizeof(report))
                    (void)dt_probe_report_slot((void *)runtime_fdt, slot,
                                               report);
            }
        }
#endif
#endif
    }
    /* Tail: quiesce as before, then hand off to base+4 with the ORIGINAL
     * incoming DT pointer and the original x1..x3 even if FDT validation or
     * report writing failed.  runtime_fdt is not required here; only a missing
     * base image or a quiesce failure keeps the existing terminal wfe.  No
     * prepare_boot and no MMU work. */
    if (linux_base && kshim_console_quiesce() == 0) {
        arm64_boot_image(linux_base + 4, resources_only_device_tree,
                         kernel_arg1, kernel_arg2, kernel_arg3);
    }
    for (;;)
        asm volatile("wfe");
#endif
#if CONFIG_ENABLE_BANNER
    printf(CONFIG_BANNER "\n");
#endif
#if KSHIM_STANDALONE
    return kshim_platform_main(device_tree_address, kernel_arg1, kernel_arg2, kernel_arg3);
#endif
    if (linux_base != 0) {
        /* Activate optional repacker source prefixes while retaining the
         * pending/write-once values for m14/m15 and the legacy APIs. */
        if (runtime_fdt)
            (void)dt_diagnostic_report_slots((void *)runtime_fdt, "pending",
                                             "pending");
        MANIFEST_VIEW view = {0};
        if (!get_manifest(
                linux_base, manifest_address, manifest_size, &view)) {
            dt_probe_report((void *)runtime_fdt, "m14:manifest=bad");
            printf("Manifest not found, booting base image\n");
            prepare_boot();
            arm64_boot_linux(linux_base, device_tree_address, 4);
        }

#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
        const struct kshim_config_values defaults={0,0,(uint32_t)kshim_menu_get_language(), 0, 0};
        (void)storage_settings_native_open(device_info.board,&defaults);
        if (!storage_settings_native_handoff_safe()) {
            __asm__ volatile("msr daifset, #0xf" ::: "memory");
            for (;;) __asm__ volatile("wfe");
        }
#if CONFIG_KSHIM_QCOM_REBOOT
#if CONFIG_KSHIM_EDL_PROBE
        (void)kshim_qcom_reboot_probe_edl(
            (const void *)(uintptr_t)device_tree_address,&EdlProbe);
#endif
        /* Linux requests this through the provisioned settings store. Clear
         * it durably before entering EDL so a later reset boots normally. */
        struct kshim_settings_session *session=storage_settings_native_session();
        uint32_t action=0;
        EdlRequestSeen=session->values.next_action;
        EdlRequestStatus=kshim_settings_take_action(session,&action);
        if (!storage_settings_native_handoff_safe()) {
            __asm__ volatile("msr daifset, #0xf" ::: "memory");
            for (;;) __asm__ volatile("wfe");
        }
        if (!EdlRequestStatus && action==1) {
            printf("Entering EDL from Linux request\n");
            EdlRequestStatus=kshim_qcom_reboot_request(
                (const void *)(uintptr_t)device_tree_address,KSHIM_QCOM_REBOOT_EDL);
            if (EdlRequestStatus==KSHIM_QCOM_REBOOT_ROLLBACK_FAILED) {
                __asm__ volatile("msr daifset, #0xf" ::: "memory");
                for (;;) __asm__ volatile("wfe");
            }
        }
        if (EdlRequestSeen && EdlRequestStatus)
            printf("EDL request failed: %d\n",EdlRequestStatus);
#endif
        kshim_platform_bind_settings(storage_settings_native_session());
#endif
        uint32_t boot_target=0;
        uint32_t selected_index = select_image(&view, device_tree_address, linux_base,
                                               &device_info,&boot_target);
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
        kshim_platform_bind_settings(NULL);
        if (!storage_settings_native_handoff_safe()) {
            __asm__ volatile("msr daifset, #0xf" ::: "memory");
            for (;;) __asm__ volatile("wfe");
        }
#endif
        kshim_boot_metrics_record_once(KSHIM_BOOT_STAGE_HANDOFF);
        const MANIFEST_ENTRY *entry = view.entries[selected_index];
        if ((entry->flags & MF_ENTRY_FLAG_BASE_IMAGE) &&
            !(entry->flags & MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB)) {
            dt_probe_report((void *)runtime_fdt, "m14:base=selected");
#if (defined(CONFIG_KSHIM_TB323FU_ANDROID_TOUCH_4M8) && \
     CONFIG_KSHIM_TB323FU_ANDROID_TOUCH_4M8) || KSHIM_TB323FU_ANDROID_TOUCH_4M8_TRIAL
            if (runtime_fdt && device_info.board == DEVICE_BOARD_TB323FU &&
                entry->type == MF_ENTRY_TYPE_LINUX) {
                int touch_rc = tb323fu_android_touch_spi_trial(
                    (void *)runtime_fdt, fdt_totalsize(runtime_fdt));
                printf("Android touch SPI 4.8 MHz: %d\n", touch_rc);
                if (touch_rc >= 0)
                    clean_android_trial_fdt(runtime_fdt);
            }
#endif
        }
        uint64_t selected_device_tree = device_tree_address;
        if (entry->flags & MF_ENTRY_FLAG_HAS_DEVICE_TREE_BLOB) {
            const MANIFEST_ENTRY *dtb =
                view.entries[entry->device_tree_blob_entry];
            uint64_t payload_device_tree = linux_base + dtb->offset;
            selected_device_tree = payload_device_tree;

            /* Every entry that selects a custom DTB automatically uses the
             * new snapshot/overlay API -- no Linux-only opt-in property and
             * no kshim,inherit-runtime-memory gate.  Entries without a custom
             * DTB keep the original bootloader DT pointer. */
            int status = memory_status;
            const char *handoff_stage = "capture";
            if (status == 0) {
                handoff_stage = "memory";
                status = dt_memory_apply(
                    RuntimeMemorySnapshot,
                    sizeof(RuntimeMemorySnapshot),
                    (const void *)(uintptr_t)payload_device_tree,
                    (size_t)dtb->size,
                    HandoffDtb,
                    sizeof(HandoffDtb),
                    MemoryOverlay,
                    sizeof(MemoryOverlay));
            }

            if (status == 0) {
                /* Use the HandoffDtb selected pointer only after successful apply. */
                selected_device_tree = (uint64_t)(uintptr_t)HandoffDtb;

                if (entry->type == MF_ENTRY_TYPE_LINUX) {
                    /* Only a successful early capture may add a persistent
                     * backend.  bind() is non-transactional, so a failed bind
                     * sets the original failure status and falls through to the
                     * existing Android base fallback instead of consuming the
                     * failed DT.  A failed capture reports the missing backend
                     * once and leaves the original Linux path untouched. */
                    if (ramoops_capture_status == TB323FU_RAMOOPS_OK) {
                        handoff_stage = "ramoops";
                        int bind_status = tb323fu_ramoops_bind(
                            HandoffDtb, sizeof(HandoffDtb),
                            &ramoops_layout);
                        if (bind_status != TB323FU_RAMOOPS_OK)
                            status = bind_status;
                    } else {
                        printf("No persistent console backend (capture %d); "
                               "continuing Linux path\n",
                               ramoops_capture_status);
                    }
                    /* Local handoff blob, reused by the following simplefb
                     * apply, metrics export and pack/validate steps. */
                    void *blob = (void *)(uintptr_t)HandoffDtb;
                    if (status == 0) {
                        /* The selected Linux target may reference an external
                         * BLOB through /chosen/kshim,initrd-entry.  When it
                         * does not, kshim_initrd_apply() retains the prior
                         * builtin deletion of any inherited linux,initrd-start
                         * / linux,initrd-end; a present reference instead
                         * advertises the referenced manifest BLOB interval.
                         * A rejection or mutation failure routes into the
                         * existing base fallback below.  get_manifest() has
                         * already validated the entries and source bounds. */
                        const struct kshim_initrd_context initrd_context = {
                            .entries = view.entries,
                            .entry_count = view.manifest->header.entry_count,
                            .packed_base = linux_base,
                            .packed_payload_size = manifest_address - linux_base,
                            .entry = entry,
                            .header =
                                (const void *)(uintptr_t)(linux_base + entry->offset),
                            .runtime =
                                {
                                    .base = (uint64_t)(uintptr_t)__image_start_address__,
                                    .size =
                                        (uint64_t)(uintptr_t)__runtime_end_address__ -
                                        (uint64_t)(uintptr_t)__image_start_address__,
                                },
                            .runtime_fdt =
                                {
                                    .base = (uint64_t)(uintptr_t)runtime_fdt,
                                    .size = runtime_fdt ? fdt_totalsize(runtime_fdt) : 0,
                                },
                            .payload_fdt =
                                {
                                    .base = payload_device_tree,
                                    .size = dtb->size,
                                },
                        };
                        handoff_stage = "initrd";
                        status = kshim_initrd_apply(blob, sizeof(HandoffDtb),
                                                    &initrd_context);
                    }
                    if (status == 0) {
                        struct kshim_resources *r = kshim_resources_get();
                        handoff_stage = "simplefb";
                        status = dt_simplefb_apply(blob, sizeof(HandoffDtb),
                                                   r && r->splash_status == 0
                                                       ? &r->framebuffer
                                                       : NULL);
                    }
                    if (status == 0) {
                        status = kshim_boot_witness_export(blob, booted_slot);
                    }
                    if (status == 0) {
                        status = kshim_boot_lock_export(blob, boot_lock_state);
                    }
                    if (status == 0) {
                        /* Re-pack the modified DT, then validate the result. */
                        (void)kshim_boot_metrics_export(blob);
#if defined(CONFIG_KSHIM_STORAGE_ENTRY_PROBE) && CONFIG_KSHIM_STORAGE_ENTRY_PROBE
                        /* Failure to fit diagnostics must not stop payload boot. */
                        if (fdt_open_into(blob, blob, sizeof(HandoffDtb)) == 0) {
                            int probe_export_status = storage_probe_export(blob, &StorageEntrySnapshot);
#if defined(CONFIG_KSHIM_UFS_READ_PROBE) && CONFIG_KSHIM_UFS_READ_PROBE
                            /* Separate native read report; a failed insertion is
                             * exported as a status and, after a safe release, does
                             * not block the payload handoff. */
                            int read_export_status = storage_ufs_read_export(blob, storage_ufs_read_entry_report());
#if defined(CONFIG_KSHIM_UFS_LINK_WAKE) && CONFIG_KSHIM_UFS_LINK_WAKE
                            /* Auxiliary record, independent of the frozen read
                             * property: v6 could only report CLAIM -23 after a
                             * cleanup that had already rewritten host->link, so
                             * the saved before/after states are published here.
                             * Its failure is only a status; it never blocks the
                             * payload handoff. */
                            int link_export_status = storage_ufs_link_export(
                                blob, storage_ufs_read_entry_report());
#if defined(CONFIG_KSHIM_UFS_DEVICE_WAKE) && CONFIG_KSHIM_UFS_DEVICE_WAKE
                            int power_export_status = storage_ufs_power_export(
                                blob, storage_ufs_read_entry_report());
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
                            const char *build = "gen4-ufs-settings-v11";
#else
                            const char *build = "gen4-ufs-read-v10";
#endif
#else
                            const char *build = "gen4-ufs-read-v8";
#endif
#elif defined(CONFIG_KSHIM_UFS_READ_SCREEN) && CONFIG_KSHIM_UFS_READ_SCREEN
                            const char *build = "gen4-ufs-read-v5";
#else
                            const char *build = "gen4-ufs-read-v4";
#endif
#else
                            const char *build = "gen4-entry-v3";
#endif
                            int chosen = fdt_path_offset(blob, "/chosen");
                            if (chosen >= 0) {
                                (void)fdt_setprop_string(blob, chosen, "kshim,storage-probe-build", build);
                                (void)fdt_setprop_u32(blob, chosen, "kshim,storage-export-status",
                                                    (uint32_t)probe_export_status);
#if defined(CONFIG_KSHIM_UFS_READ_PROBE) && CONFIG_KSHIM_UFS_READ_PROBE
                                (void)fdt_setprop_u32(blob, chosen, "kshim,ufs-read-export-status",
                                                    (uint32_t)read_export_status);
#if defined(CONFIG_KSHIM_UFS_LINK_WAKE) && CONFIG_KSHIM_UFS_LINK_WAKE
                                /* Separate status for the separate property. */
                                (void)fdt_setprop_u32(blob, chosen, "kshim,ufs-link-export-status",
                                                    (uint32_t)link_export_status);
#if defined(CONFIG_KSHIM_UFS_DEVICE_WAKE) && CONFIG_KSHIM_UFS_DEVICE_WAKE
                                (void)fdt_setprop_u32(blob, chosen, "kshim,ufs-power-export-status",
                                                    (uint32_t)power_export_status);
#endif
#endif
#endif
                            }
                        }
#endif
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
                        if (fdt_open_into(blob,blob,sizeof(HandoffDtb))==0) {
                            int settings_status=storage_settings_native_export(blob);
                            int chosen=fdt_path_offset(blob,"/chosen");
                            if (chosen>=0) {
                                (void)fdt_setprop_u32(blob,chosen,"kshim,settings-export-status",
                                                    (uint32_t)settings_status);
                                (void)fdt_setprop_string(blob,chosen,"kshim,settings-build",
                                                       "native-ufs-settings-v3");
#if CONFIG_KSHIM_QCOM_REBOOT
                                (void)fdt_setprop_u32(blob,chosen,"kshim,edl-request-version",1);
                                (void)fdt_setprop_u32(blob,chosen,"kshim,edl-request-seen",EdlRequestSeen);
                                (void)fdt_setprop_u32(blob,chosen,"kshim,edl-request-status",(uint32_t)EdlRequestStatus);
#if CONFIG_KSHIM_EDL_PROBE
                                const uint32_t fields[]={1,(uint32_t)EdlProbe.status,
                                    EdlProbe.scm_available_mask,EdlProbe.psci_version,
                                    (uint32_t)EdlProbe.reset2_status,(uint32_t)EdlProbe.oem_status,
                                    EdlProbe.oem_available,EdlProbe.route,
                                    (uint32_t)(EdlProbe.dload_address>>32),
                                    (uint32_t)EdlProbe.dload_address,EdlProbe.dload_value};
                                fdt32_t report[11];
                                for (unsigned i=0;i<11;++i) report[i]=cpu_to_fdt32(fields[i]);
                                (void)fdt_setprop(blob,chosen,"kshim,edl-probe",report,sizeof(report));
#endif
#endif
                            }
                        }
#endif
                        handoff_stage = "pack";
                        int packed = fdt_pack(blob);
                        if (packed != 0) {
                            status = packed;
                        } else {
                            handoff_stage = "validate";
                            status = dt_validate(blob, sizeof(HandoffDtb));
                        }
                    }
                }
            }

#if defined(KSHIM_DIAGNOSTIC_MEMORY_ONLY) && KSHIM_DIAGNOSTIC_MEMORY_ONLY
            /* Diagnostic-only kernel probe (M30).
             * (KSHIM_DIAGNOSTIC_MEMORY_ONLY=1; undefined by default => OFF.)
             * Inspects the normalized Handoff DT /memory.reg no-map union,
             * the Linux payload header and the patched Image entry, then
             * actually EXECUTES the kernel through __cpu_setup and
             * __enable_mmu via arm64_diag_head_tail(): it branches into the
             * PATCHED Linux Image and runs the full early kernel CPU path -
             * the feature override, KASLR and, when available, LPA2, plus the
             * map and relocate of TTBR1 - through the actual start_kernel
             * into setup_arch, up to the M30 checkpoint at Image+0x1614c3c,
             * immediately after the inlined setup_machine_fdt() and before
             * jump_label_init / parse_early_param / the MMIO setup and idmap
             * removal.  Success requires the current kernel PC in a high VA
             * (bit 63 set), the captured SCTLR_EL1.M bit set, and nonzero
             * copied memblock count and bytes.  There is no printk return
             * condition in M30.  The inline 128-byte stub there restores the
             * caller's x25/x26 from SP+224 and x27/x28 from SP+240 (the
             * start_kernel 144-byte frame plus the setup_arch 160-byte
             * frame), reads the published kimage_voffset, computes
             * physical_text = VA_text - kimage_voffset, and BLs the real
             * kernel DC-clean helper over the static Image range
             * [_stext 0x10000, _end 0x1c40000) to flush all prior static
             * dirty data.  No writes occur after the clean with the MMU on;
             * it then switches to the physical alias 0x1614ca0 while the MMU
             * is on, followed by the known MMU/cache-off sequence
             * (SCTLR_EL1 0x30500800), barriers and a BR to the physical
             * callback.  No x0 physical assumption is made.  The seed gate
             * still ensures the FDT is only read (and rejects a present
             * rng-seed).  It does NOT run reserved-region processing,
             * allocations, full Linux, USB or tuning proof.  A successful
             * probe uses the MMU-off callback to export counts/bytes before
             * Android; rejected probes fall back through the common report
             * path.  The normal Linux handoff is
             * disabled in this test build.  The fallback below is unchanged. */
            if (status == 0) {
                const void *fdt = (const void *)HandoffDtb;
                char report[81];
                int rn = 0;
                int gate_ok = 0;
                uint64_t exec_kbase = 0;

                if (entry != NULL &&
                    entry->type == MF_ENTRY_TYPE_LINUX &&
                    entry->size >= 64u &&
                    (uint64_t)entry->offset <= (UINT64_MAX - (uint64_t)linux_base)) {
                    uintptr_t source =
                        (uintptr_t)((uint64_t)linux_base + entry->offset);
                    const uint8_t *img = (const uint8_t *)source;
                    uint32_t magic = (uint32_t)img[56]
                                   | ((uint32_t)img[57] << 8)
                                   | ((uint32_t)img[58] << 16)
                                   | ((uint32_t)img[59] << 24);
                    uint64_t image_size = (uint64_t)img[16]
                                        | ((uint64_t)img[17] << 8)
                                        | ((uint64_t)img[18] << 16)
                                        | ((uint64_t)img[19] << 24)
                                        | ((uint64_t)img[20] << 32)
                                        | ((uint64_t)img[21] << 40)
                                        | ((uint64_t)img[22] << 48)
                                        | ((uint64_t)img[23] << 56);

                    if (magic == 0x644d5241u && image_size > 0u) {
                        uint64_t kbase = (entry->flags & MF_ENTRY_FLAG_COPY)
                                       ? (uint64_t)entry->load_address
                                       : (uint64_t)source;
                        uint64_t dsize = (uint64_t)fdt_totalsize(HandoffDtb);
                        struct diag_placement kp, dp;
                        int krc, drc;

                        krc = diag_check_span(fdt, kbase, image_size, &kp);
                        if (dsize > 0u &&
                            dsize <= (uint64_t)sizeof(HandoffDtb)) {
                            drc = diag_check_span(fdt,
                                                  (uint64_t)(uintptr_t)HandoffDtb,
                                                  dsize, &dp);
                        } else {
                            drc = -1;
                        }

                        const char *rej = NULL;
                        if (entry->flags & MF_ENTRY_FLAG_COPY) {
                            rej = "placement";
                        } else if (entry->entry_offset != 0u) {
                            rej = "placement";
                        } else if ((kbase & 0x1fffffull) != 0ull) {
                            rej = "placement";
                        } else if (krc != 0 || drc != 0) {
                            rej = "placement";
                        } else if (!kp.covered || !dp.covered) {
                            rej = "placement";
                        } else if (kp.nomap_overlaps != 0u ||
                                   kp.header_overlaps != 0u ||
                                   dp.nomap_overlaps != 0u ||
                                   dp.header_overlaps != 0u) {
                            rej = "placement";
                        } else if (memcmp(img + 32, "KSHM30DT", 8) != 0) {
                            rej = "marker";
                        } else {
                            uint64_t off = (uint64_t)img[40]
                                         | ((uint64_t)img[41] << 8)
                                         | ((uint64_t)img[42] << 16)
                                         | ((uint64_t)img[43] << 24)
                                         | ((uint64_t)img[44] << 32)
                                         | ((uint64_t)img[45] << 40)
                                         | ((uint64_t)img[46] << 48)
                                         | ((uint64_t)img[47] << 56);
                            if (off != 0x1614c3cull) {
                                rej = "marker";
                            } else if (entry->size < off + 128u || image_size < off + 128u) {
                                rej = "marker";
                            } else {
                                const uint8_t *w = img + off;
                                static const uint8_t expected_stub[128] = {
                                    0x18, 0x10, 0x38, 0xd5, 0xea, 0xff, 0xff, 0x10,
                                    0x5f, 0x01, 0x41, 0xf2, 0x18, 0x13, 0x9f, 0x9a,
                                    0xf9, 0x6b, 0x4e, 0xa9, 0xfb, 0x73, 0x4f, 0xa9,
                                    0x14, 0x25, 0x00, 0xd0, 0x94, 0x62, 0x26, 0x91,
                                    0x95, 0x0a, 0x40, 0xf9, 0x96, 0x12, 0x40, 0xf9,
                                    0xbf, 0x02, 0x00, 0xf1, 0x18, 0x13, 0x9f, 0x9a,
                                    0xdf, 0x02, 0x00, 0xf1, 0x18, 0x13, 0x9f, 0x9a,
                                    0xb3, 0xfa, 0xff, 0x90, 0x73, 0x02, 0x40, 0xf9,
                                    0x69, 0x4f, 0xff, 0x90, 0x33, 0x01, 0x13, 0xcb,
                                    0xe0, 0x4f, 0xff, 0x90, 0x61, 0x31, 0x00, 0x90,
                                    0xf9, 0x7a, 0xa8, 0x97, 0x09, 0x94, 0x89, 0xd2,
                                    0x29, 0x2c, 0xa0, 0xf2, 0x69, 0x02, 0x09, 0x8b,
                                    0x20, 0x01, 0x1f, 0xd6, 0x09, 0x00, 0x81, 0xd2,
                                    0x09, 0x0a, 0xa6, 0xf2, 0x9f, 0x3f, 0x03, 0xd5,
                                    0xdf, 0x3f, 0x03, 0xd5, 0x09, 0x10, 0x18, 0xd5,
                                    0xdf, 0x3f, 0x03, 0xd5, 0x60, 0x03, 0x1f, 0xd6,
                                };
                                if (memcmp(w, expected_stub, 128) != 0) {
                                    rej = "marker";
                                } else {
                                    uint64_t el;
                                    __asm__ volatile("mrs %0, CurrentEL"
                                                     : "=r"(el));
                                    if (el != 4u) {
                                        rej = "cpu";
                                    } else {
                                        uint64_t sctlr;
                                        __asm__ volatile("mrs %0, sctlr_el1"
                                                         : "=r"(sctlr));
                                        if ((sctlr & ((1ull << 0) |
                                                      (1ull << 2) |
                                                      (1ull << 25))) != 0ull) {
                                            rej = "cpu";
                                        } else {
                                            /* Seed gate: the source DT must not
                                             * carry a kaslr-seed, so the target
                                             * FDT stays read-only through
                                             * kaslr_early_init and the memory
                                             * overlay never imports it.  The same
                                             * applies to rng-seed; note that
                                             * early_init_dt_scan_chosen() may NOP
                                             * a rng-seed, so the read-only DT is
                                             * still required for cache cleanup.
                                             * Only absent /chosen kaslr-seed and
                                             * rng-seed (with a /chosen that
                                             * resolves) arms the probe; seed
                                             * rejection is diagnostic-only and
                                             * falls back through the common report
                                             * path. */
                                            int chosen = fdt_path_offset(
                                                (const void *)HandoffDtb,
                                                "/chosen");
                                            int slen = 0;
                                            int rlen = 0;
                                            const void *seed = (chosen >= 0)
                                                ? fdt_getprop(
                                                      (const void *)HandoffDtb,
                                                      chosen, "kaslr-seed",
                                                      &slen)
                                                : NULL;
                                            const void *rseed = (chosen >= 0)
                                                ? fdt_getprop(
                                                      (const void *)HandoffDtb,
                                                      chosen, "rng-seed",
                                                      &rlen)
                                                : NULL;
                                            if (chosen < 0 || seed != NULL ||
                                                slen != -FDT_ERR_NOTFOUND ||
                                                rseed != NULL ||
                                                rlen != -FDT_ERR_NOTFOUND) {
                                                rej = "seed";
                                            } else {
                                                gate_ok = 1;
                                                exec_kbase = kbase;
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        if (rej != NULL) {
                            rn = snprintf(report, sizeof(report),
                                          "m30:reject=%s", rej);
                        }
                    } else {
                        rn = snprintf(report, sizeof(report),
                                      "m30:reject=header");
                    }
                } else {
                    rn = snprintf(report, sizeof(report),
                                  "m30:reject=header");
                }

                if (gate_ok) {
                    /* M30 gate: this branch never falls through to the common
                     * reject report below.  Install the incomplete record, resolve
                     * the single DT flag byte, then either arm the tail
                     * (noreturn) or fall back to the original Android boot.  The
                     * source-DT /chosen lookup was already checked by the seed
                     * gate; the report here records the kernel high-VA entry, and
                     * the callback publishes the copied memblock count/total. */
                    const char *flag = NULL;
                    uint64_t incoming_sp;

                    __asm__ volatile("mrs %0, spsel" : "=r"(incoming_sp));
                    rn = snprintf(report, sizeof(report),
                                  "m30:dt=0:n=0000000000000000:b=0000000000000000:sp=%llu",
                                  (unsigned long long)(incoming_sp & 1ull));

                    if (rn > 0 && rn < (int)sizeof(report) &&
                        dt_probe_report((void *)runtime_fdt, report) == 0) {
                        static const char m30_pre[] =
                            "androidboot.kshim_probe=m30:dt=";
                        static const char m30_tok[] =
                            "androidboot.kshim_probe=m30:dt=0:n=0000000000000000:b=0000000000000000:sp=";
                        int chosen = fdt_path_offset((const void *)runtime_fdt,
                                                     "/chosen");
                        int balen = 0;
                        const char *ba = (chosen >= 0)
                            ? (const char *)fdt_getprop((const void *)runtime_fdt,
                                                        chosen, "bootargs",
                                                        &balen)
                            : NULL;
                        if (ba != NULL && balen > 0) {
                            int toklen = (int)(sizeof(m30_tok) - 1);
                            int i;
                            int cnt = 0;
                            for (i = 0; i + toklen <= balen; i++) {
                                int ve;
                                if (i != 0 && ba[i - 1] != ' ') {
                                    continue;
                                }
                                if (memcmp(ba + i, m30_tok, toklen) != 0) {
                                    continue;
                                }
                                ve = i +
                                     (int)(sizeof("androidboot.kshim_probe=") - 1) +
                                     80;
                                if (ve >= balen) {
                                    continue;
                                }
                                if (ba[ve] == '\0' || ba[ve] == ' ') {
                                    cnt++;
                                    if (cnt == 1) {
                                        flag = ba + i +
                                               (int)(sizeof(m30_pre) - 1);
                                    }
                                }
                            }
                            if (cnt != 1) {
                                flag = NULL;
                            }
                            if (flag != NULL && flag[0] != '0') {
                                flag = NULL;
                            }
                        }
                    }

                    if (flag != NULL) {
                        printf("Memory checkpoint m30:armed\n");
                        prepare_boot();
                        arm64_diag_head_tail(exec_kbase,
                                             (uint64_t)HandoffDtb,
                                             (uint64_t)linux_base + 4,
                                             (uint64_t)device_tree_address,
                                             (uintptr_t)flag);
                        /* arm64_diag_head_tail() is __attribute__((noreturn)). */
                    }

                    printf("Memory checkpoint m30:reject=report\n");
                    prepare_boot();
                    arm64_boot_linux(linux_base, device_tree_address, 4);
                } else {
                    if (rn > 0 && rn < (int)sizeof(report)) {
                        printf("Memory checkpoint %s\n", report);
                        dt_probe_report((void *)runtime_fdt, report);
                    }

                    prepare_boot();
                    arm64_boot_linux(linux_base, device_tree_address, 4);
                }
            }
#endif

            if (status != 0) {
                char report[81];
                int n = snprintf(report, sizeof(report),
                                 "m15:s=%s:c=%d:a=%d:d=%s",
                                 handoff_stage, memory_status, status,
                                 dt_handoff_failure_detail());
                if (n > 0 && n < (int)sizeof(report))
                    dt_probe_report((void *)runtime_fdt, report);
                printf("Memory handoff failed; booting base image\n");
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
                remember_selected_boot(1,0);
#endif
                prepare_boot();
                arm64_boot_linux(linux_base, device_tree_address, 4);
            }
        }
#if defined(CONFIG_KSHIM_UFS_SETTINGS) && CONFIG_KSHIM_UFS_SETTINGS
        if (selected_device_tree == (uint64_t)(uintptr_t)HandoffDtb &&
            entry->type == MF_ENTRY_TYPE_LINUX) {
            int chosen = fdt_path_offset(HandoffDtb, "/chosen");
            int root_len = 0;
            int cmdline_len = 0;
            int tx_len = 0;
            const char *root_medium = chosen >= 0
                ? (const char *)fdt_getprop(HandoffDtb, chosen,
                                            "armada,root-medium", &root_len)
                : NULL;
            bool internal_linux = root_medium != NULL && root_len > 0 &&
                root_medium[root_len - 1] == '\0' &&
                strcmp(root_medium, "internal") == 0;
            const char *cmdline = chosen >= 0
                ? (const char *)fdt_getprop(HandoffDtb, chosen,
                                            "bootargs", &cmdline_len)
                : NULL;
            const void *tx = chosen >= 0
                ? fdt_getprop(HandoffDtb, chosen,
                              "armada,update-transaction", &tx_len)
                : NULL;
            const void *tx_ptr = tx != NULL
                ? (tx_len > 0 ? tx : (const void *)&tx_len)
                : NULL;
            size_t tx_bytes = tx != NULL && tx_len > 0 ? (size_t)tx_len : 0u;
            int gate_rc = kshim_update_trial_boot(
                booted_slot, internal_linux, cmdline,
                cmdline != NULL && cmdline_len > 0 ? (size_t)cmdline_len : 0u,
                tx_ptr, tx_bytes);

            if (!storage_settings_native_handoff_safe()) {
                __asm__ volatile("msr daifset, #0xf" ::: "memory");
                for (;;)
                    __asm__ volatile("wfe");
            }
            if (gate_rc == KSHIM_UPDATE_BOOT_RESTART_REQUIRED) {
                prepare_boot();
                (void)kshim_psci_request_system_reset();
                __asm__ volatile("msr daifset, #0xf" ::: "memory");
                for (;;)
                    __asm__ volatile("wfe");
            }
            if (gate_rc != 0) {
                printf("Update trial gate refused handoff (%d); booting base image\n",
                       gate_rc);
                remember_selected_boot(1, 0);
                prepare_boot();
                arm64_boot_linux(linux_base, device_tree_address, 4);
            }
        }
        remember_selected_boot(boot_target,selected_device_tree);
#endif
        if (entry->flags & MF_ENTRY_FLAG_COPY) {
            uint64_t source_address = linux_base + entry->offset;
            printf("Copying ");
            print_entry_name(entry);
            printf(" from 0x%lx to 0x%lx (0x%lx bytes)\n", source_address,
                   entry->load_address, entry->size);
            memmove((void *)entry->load_address, (const void *)source_address,
                    entry->size);
            sync_image_cache(entry->load_address, entry->size);
            prepare_boot();
            if (entry->type == MF_ENTRY_TYPE_LINUX) {
                arm64_boot_linux(entry->load_address, selected_device_tree,
                                 entry->entry_offset);
            }
            arm64_boot_image(entry->load_address + entry->entry_offset,
                             selected_device_tree, kernel_arg1, kernel_arg2,
                             kernel_arg3);
        }

        if (entry->type == MF_ENTRY_TYPE_LINUX) {
            uint64_t entry_offset = entry->flags & MF_ENTRY_FLAG_BASE_IMAGE
                ? 4 : entry->entry_offset;
            printf("Booting ");
            print_entry_name(entry);
            printf(" at 0x%lx with DTB at 0x%lx\n",
                   linux_base + entry->offset, selected_device_tree);
            prepare_boot();
            arm64_boot_linux(linux_base + entry->offset,
                             selected_device_tree, entry_offset);
        }
        prepare_boot();
        arm64_boot_image(linux_base + entry->offset + entry->entry_offset,
                         selected_device_tree, kernel_arg1, kernel_arg2,
                         kernel_arg3);
    }
    return kshim_platform_main(device_tree_address, kernel_arg1, kernel_arg2, kernel_arg3);
}
