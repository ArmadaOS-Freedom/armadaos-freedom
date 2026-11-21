/* boot_disk_load.h — 只读 boot 分区 loader 接口（无 malloc，无设备写） */
#ifndef KSHIM_BOOT_DISK_LOAD_H
#define KSHIM_BOOT_DISK_LOAD_H

#include <stddef.h>
#include <stdint.h>

#include "boot_disk_manifest.h"  /* struct kshim_disk_manifest, kshim_disk_manifest_verify */

/* 由 kshim storage 层提供的前向声明（见 boot_ext4.h / kshim_blockdev.h） */
struct kshim_blockdev;
struct kshim_partition;

/* 调用方提供的目标缓冲（顺序固定，不得重排）。返回 0 后才有意义。 */
struct kshim_disk_boot_buffers {
    void   *kernel;
    size_t  kernel_capacity;
    void   *initrd;
    size_t  initrd_capacity;
    void   *dtb;
    size_t  dtb_capacity;
};

/*
 * 只读地从 part 上固定绝对路径读取并校验 boot 载荷，<model> 为
 * TB322FC 或 TB323FU：
 *   /armada/boot/<model>/manifest.bin (160 字节)
 *   /armada/boot/<model>/manifest.sig (8..72 字节)
 *   /armada/boot/kernel.img   (共享；校验签名 size + SHA-256)
 *   /armada/boot/initramfs.img (共享)
 *   /armada/boot/<model>/board.dtb
 *
 * 契约：
 *  - 绝不 malloc / free，绝不写设备，仅使用 caller 提供的 scratch。
 *  - 只有在三个 signed size 都能放进对应 caller buffer 时才读本体；
 *    任何一步失败都返回非 0，且不填充 *out_manifest（返回 0 时才有效）。
 *  - 读取/校验按块进行，中断（短读、scratch 不足）不允许 handoff。
 *  - 不启动内核、不改分区号。
 *
 * 返回 0 成功；非 0 为 KSHIM_STORAGE_* 或本层负错误码。
 */
int kshim_disk_boot_read(const struct kshim_blockdev   *dev,
                         const struct kshim_partition  *part,
                         const uint8_t                  public_key[65],
                         uint32_t                       board,
                         const struct kshim_disk_boot_buffers *buffers,
                         struct kshim_disk_manifest    *out_manifest,
                         void                          *scratch,
                         size_t                         scratch_bytes);

#endif /* KSHIM_BOOT_DISK_LOAD_H */
