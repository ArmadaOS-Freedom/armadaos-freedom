/* boot_disk_load.h — 只读 boot 分区 loader 接口（无 malloc，无设备写） */
#ifndef KSHIM_BOOT_DISK_LOAD_H
#define KSHIM_BOOT_DISK_LOAD_H

#include <stdbool.h>
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
 * Placement callback. Invoked exactly once, after the signed manifest has
 * been verified and exactly one read-and-verify pass has authenticated the
 * board DTB into dtb_scratch. The callback receives the verified DTB bytes
 * and MUST fill all final buffers (kernel/initrd/dtb pointers) in *buffers
 * before returning 0. It must perform no I/O and must not mutate
 * verified_dtb or dtb_bytes. Nonzero return aborts the load.
 */
typedef int (*kshim_disk_boot_place_fn)(void *ctx,
                                        const void *verified_dtb,
                                        size_t dtb_bytes,
                                        struct kshim_disk_boot_buffers *buffers);

/*
 * 只读地从 part 上固定绝对路径读取并校验 boot 载荷，<model> 为
 * TB322FC 或 TB323FU：
 *   /armada/boot/<model>/manifest.bin (160 字节)
 *   /armada/boot/<model>/manifest.sig (8..72 字节)
 *   /armada/boot/<model>/board.dtb (经签名 size + SHA-256 认证到 dtb_scratch)
 *   /armada/boot/kernel.img   (共享；校验签名 size + SHA-256 到 place 选定的 buffer)
 *   /armada/boot/initramfs.img (共享)
 *
 * 契约：
 *  - 绝不 malloc / free，绝不写设备，仅使用 caller 提供的 scratch。
 *  - 先校验签名 manifest；随后把 board DTB 认证读入 dtb_scratch（每次调用只读一次），
 *    再调用 place() 让调用方在已认证 DTB 基础上决定最终 buffer 布局。
 *    buffers 的 kernel/initrd/dtb 指针允许在调用 place() 之前为未设置。
 *  - 只有在三个 signed size 都能放进对应 place 之后的 capacity/range
 *    时才读 kernel/initrd 本体；已认证 DTB 通过 memmove 复制到最终 DTB，不重读。
 *  - 任何一步失败都返回非 0，且不填充 *out_manifest（返回 0 时才有效）。
 *  - 读取/校验按块进行，中断（短读、scratch 不足）不允许 handoff。
 *  - 不启动内核、不改分区号。
 *
 * 返回 0 成功；非 0 为 KSHIM_STORAGE_* 或本层负错误码。
 */
int kshim_disk_boot_read(const struct kshim_blockdev   *dev,
                         const struct kshim_partition  *part,
                         const uint8_t                  public_key[65],
                         uint32_t                       board,
                         bool                           recovery,
                         void                          *dtb_scratch,
                         size_t                         dtb_scratch_bytes,
                         kshim_disk_boot_place_fn       place,
                         void                          *place_ctx,
                         struct kshim_disk_boot_buffers *buffers,
                         struct kshim_disk_manifest    *out_manifest,
                         void                          *scratch,
                         size_t                         scratch_bytes);

#endif /* KSHIM_BOOT_DISK_LOAD_H */
