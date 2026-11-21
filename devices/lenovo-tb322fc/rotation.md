# TB322FC 90° DPU 旋转修复与验证

记录日期：2026-11-19。此修复在原固定270°方案（[PR #115](https://github.com/ArmadaOS-Freedom/armadaos-freedom/pull/115)）上补齐90°反射取像方向，默认仍为 `right`／270°，`ARMADA_GAMESCOPE_USE_ROTATION_SHADER=0`。加速度计硬件暂不可用；方向覆盖使用已有的 Freedom Control 接口，应用方向会重启整个游戏模式。

## 问题与修复

DRM 逆时针90°经既有转换后，DPU 使用顺时针 ROT90 加水平／垂直反射。QSEED V11 原先从未反射的 source／extended rectangle 计算方向性 pixel-extension fetch，却直接把结果写到最终反射后的管线。先前90°半屏异常时记录到 UBWC 错误 `0x00ff0001`。

[`0234`](../../packages/kernel/patches/0234-drm-msm-dpu-qseed-v11-reflected-fetch.patch) 在 `dpu_plane_setup_qseed3_v11()` 计算 fetch 后按最终反射交换方向：

```c
if (pipe_cfg->rotation & DRM_MODE_REFLECT_X)
    swap(fetch_left, fetch_right);
if (pipe_cfg->rotation & DRM_MODE_REFLECT_Y)
    swap(fetch_top, fetch_btm);
```

改动限于 QSEED V11 inline quarter-turn 路径的方向性 fetch。source extent、total、phase、preload、ROI、UBWC配置、source-split顺序和带宽检查保持原有算法。270°没有最终反射，两项交换均不触发。0°直通和180°双翻转是既有能力，此补丁没有新增180°算法。当前 `0228` 已包含历史 extent 修正，不再叠加旧 `0233`。

## Host 回归

[`test_dpu_rotation.py`](../../packages/kernel/tests/test_dpu_rotation.py) 从被测内核提取生产 `dpu_plane_setup_qseed3_v11()`、`dpu_plane_check_qseed3_v11()` 和 `dpu_hw_sspp_setup_pe_config()` 编译执行。新增断言检查四路已捕获的90°split-pipe几何、无反射／X／Y／XY反射、负值crop及实际PE寄存器打包，并保留原有split／性能测试。

- GCC `-Wall -Wextra -Werror`，ASan／UBSan：135 cases，0 failed assertions。
- 同一测试对仅撤回四行交换的负对照：135 cases，190 failed assertions，退出1；无反射270°控制项保持通过。
- 2026-11-19按下面的CI入口全新解包并严格应用323个shared／Infinity补丁，0失败；全部kernel host source checks及4项patch catalogue检查通过。提取的四份显示／矩形源码与实机候选构建树逐字节一致。
- 用仓库构建入口检查完整补丁集及 Infinity overlay：

```sh
KERNEL_HOST_CHECKS_ONLY=1 KERNEL_OVERLAYS=infinity \
WORK_DIR=/path/to/fresh-work \
bash packages/kernel/scripts/build-kernel.sh
python3 -m unittest discover -s tests -p 'test_patch_catalogue.py' -v
```

## 实机技术检查

实机为 TB322FC／SM8750。测试镜像由已验证的 camera2 内核树仅增加精确 `0234` 后构建；不是本旋转分支 HEAD 的独立全量构建。相机修复另立 [PR #134](https://github.com/ArmadaOS-Freedom/armadaos-freedom/pull/134)，不包含在旋转补丁中。

| 来源 | 标识 |
| --- | --- |
| Kernel release | `7.2.6-y700-camera2` |
| GNU build ID | `38e5d591699a995b335df726d9436580e2da4b37` |
| Image SHA256 | `207a15d2cd032b6a2f010fbb4992dcb70243da8bf8451330568d31ca8dbbd037` |
| 0234 SHA256 | `c4ed53451cdc78c68c3d6617ee2ce03a4a5aa4b5ce9e73ef754c7ffbcaaabcde` |
| 完整签名 A/B boot SHA256 | `62446cff946ddee7e92abaa9b2df382edff3761773951c661f6fa4e13ffe67a1` |
| 最后核验 boot ID | `fad0b36b-64d1-484e-a8a2-f6499e7fad73` |
| Freedom Control | 上游0.4.3，commit `8f23c8b1d3583c7820e9cf11c36b9ccbd3e9e1c7` |

90°普通游戏模式保持两个 KMS plane `rotation=2` 约55.6秒；在观察到的165Hz→60Hz主页节能切换中保持硬件旋转。另用独立 DRM Gamescope 运行30秒全屏 `glxgears`，3040×1904@60Hz，未传 `--force-composition-rotation`，约59–60 FPS，两个UBWC plane均为90°；四路活动SSPP的错误寄存器均0，测试区间未见内核DPU／UBWC／vblank故障。

四方向核验同时读取两个实际KMS plane和四路活动SSPP的开始／结束寄存器，各约20秒，均为60Hz、UBWC modifier `0x500000000000001`：

| 角度 | Control | KMS rotation | DPU ROT90 | 水平翻转 | 垂直翻转 |
| --- | --- | --- | --- | --- | --- |
| 0° | normal | 1／1 | 关 | 关 | 关 |
| 90° | left | 2／2 | 开 | 开 | 开 |
| 180° | upsidedown | 4／4 | 关 | 开 | 开 |
| 270° | right | 8／8 | 开 | 关 | 关 |

寄存器依据为 DPU12 `SSPP_SRC_FORMAT +0x30` bit11（ROT90）、`SSPP_SRC_OP_MODE +0x38` bit13／14（水平／垂直反射），以及 `SSPP_UBWC_ERROR_STATUS +0x138`。所采开始／结束UBWC错误均0。source-split的bit4未被误读为旋转位。

前三方向在同批次完成；切到270°时，Decky对一分钟内密集Steam／CEF重启累计报 `webhelper crashed` 并进入shutdown，服务停止超时。原批次失败结果保留。恢复 `plugin_loader` 后，在不再重启的条件下独立补完270°约21秒。四方向硬件状态正确不代表一次连续切换批次无故障。

## 仍待验证的边界

- 用户在四方向测试前反馈当前画面正常；没有本轮逐方向的物理目视和触控验收。逻辑 `SCREEN_BUFFER` 截图已检查，不能证明DPU输出后的实屏连续性。
- 齿轮退出有三条 `drmModeRmFB failed: Bad file descriptor`。密集重启时Decky shutdown／停止超时及早期Steam恢复半屏闪烁继续单独排查，不能据无内核显示故障宣布已解决。
- 未新增游戏、全部刷新率、休眠恢复或其它QSEED V11设备的实机验收；共享驱动的其它设备仍需回归。
- 测试结束恢复默认270°，清空方向override，相关服务正常、恢复timer停止。此次PR整理不再次操作设备或刷写分区。
