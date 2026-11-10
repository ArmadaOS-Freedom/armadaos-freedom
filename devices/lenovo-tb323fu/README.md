# 联想拯救者 Y700 五代

ArmadaOS Freedom 为 Y700 五代提供 kShimC 多系统启动、Steam 游戏模式和面向平板的显示、输入与外设支持。本页记录 TB323FU 的功能状态、已知问题和开发入口。

| 项目 | 配置 |
|---|---|
| 型号 | TB323FU |
| SoC | Qualcomm SM8850P；驱动配置使用 SM8850 |
| 设备树标识 | `lenovo,baldur`、`qcom,kaanapali` |
| 内核 | ArmadaOS Freedom 统一 Linux 7.2.6 |
| Armada Control 配置 | `lenovo-tb323fu`，无风扇平板 |

## 功能状态

| 功能 | 当前状态 |
|---|---|
| 机身 Linux | 统一 Linux 7.2.6 已启动到 Steam，原生 DRM／Adreno 840 显示已运行 |
| 内屏显示 | 30／60／90／120／165 Hz 档位按机型匹配，真实 165 Hz 时序已接入 |
| 硬件旋转 | SM8850 DPU 旋转与游戏/HUD 两层直出已在动态 PV 验证，详见[硬件旋转说明](docs/dpu-rotation.md) |
| kShimC 启动菜单 | STAR_CHART 中英文菜单，提供 Android、机身／TF 卡 Linux、设置和高级启动入口 |
| 输入与性能配置 | 已接入设备识别、CPU 拓扑、性能配置及桌面触摸校准 |
| 音频与网络 | 已有扬声器保护、双 USB 路由和 5 GHz Wi-Fi 扫描适配 |
| 状态显示 | 已接入 MangoHud 电池和温度采集 |
| G9 2026 | 基础按键、单按／双击 Home 及 RGB 控制已确认正常 |
| Armada Control | 使用五代独立配置，无风扇设备隐藏风扇相关入口 |

统一内核已确认机身启动及实体显示；触屏、游戏、TF 卡启动和长期显示稳定性仍需继续复测。高级启动中的操作尚未全部完成逐项验证。

## G9 手柄

五代适配 G9 2026（USB `3537:1136`）：

- 基础按键可正常操作。
- 单按 Home 保留 Steam 原菜单，双击 Home 打开右侧快捷菜单。
- 在 Armada Control 的 RGB 页面控制灯光开关、颜色和亮度。

按键映射、RGB 和双击辅助程序同时匹配平板型号与手柄 ID，与[四代旧 G9](../lenovo-tb322fc/README.md#g9-手柄)分开维护。双击辅助程序使用 InputPlumber 的共享输入支持，监听主输入及同一设备的 Consumer Control 接口。

## 已知限制

- **显示与游戏稳定性：** 90 Hz 偶发物理花屏、Steam 主界面低帧、复杂场景帧时间波动，以及游戏期间死机／自动重启仍需继续排查。SGSR 高刷、面板时序和补帧分别跟踪。
- **安装与更新：** 公共安装及五代引导同步尚待完成完整验证，不能直接使用四代的引导打包／同步流程。
- **充电设置：** 硬件充电固件可能沿用 Android 的充电上限，Linux 调整入口尚未接入。
- **启动器设置：** 机型选择和默认启动项仅在本次启动内生效，持久化暂缓。
- **手柄扩展功能：** 全部背键、IMU、震动、多模式和插拔／休眠后的 RGB 恢复尚未完整验证。

## 内核与启动

五代与[四代](../lenovo-tb322fc/README.md)共用 [`packages/kernel/`](../../packages/kernel/) 中的 Linux 7.2.6 基础内核和 Freedom 补丁集，五代使用 Baldur 设备树、对应驱动补丁与功放模块。旧 7.3 板级源码保留为移植来源，当前构建入口是统一内核目录。

引导构建见[五代引导说明](boot/star-chart-build/README.md)，外置 initramfs 见[配套说明](boot/star-chart-build/EXTERNAL_INITRAMFS.md)。统一内核使用 `tb323fu_unified_defconfig` 启动器配置和五代专用启动载荷；**两代 boot 镜像不能互刷**。

系统更新目标为 Freedom 的 `ghcr.io/armadaos-freedom/armadaos-freedom`，支持 `stable`、`beta`、`testing` 渠道。现有 `armada-kshim-sync` 按四代 Elden 匹配，不能将它当作五代已完成的 OTA 支持。

## 源码与来源

| 入口 | 内容 |
|---|---|
| [设备配置](../../system_files/usr/lib/armada/devices/lenovo-tb323fu.conf) | 机型识别与显示旋转配置 |
| [统一内核](../../packages/kernel/) | 当前内核、设备树、驱动补丁及模块构建 |
| [启动器源码](boot/kshim-success-source/README.md) | 四代与五代共用的 kShimC 源码快照 |
| [引导构建](boot/star-chart-build/README.md) | 五代启动载荷与打包约定 |
| [设备系统文件](rootfs/) | 双 USB、音频、Wi-Fi 等设备专用资源 |
| [扬声器部署](docs/audio-deployment.md) | DSP 保护和功放增益服务 |
| [165 Hz 补丁说明](kernel/runtime-fixes/165hz.md) | 旧内核参考补丁及统一内核对应入口 |
| [组件来源](docs/SOURCES.md) | 上游来源、固定版本与许可记录 |

`kernel/` 保存历史板级源码，`patches/boot/` 保存引导及打包器的来源补丁。历史补丁不能重复叠加到当前统一内核或启动器源码。`diagnostics/` 和 `tests/` 提供诊断与回归工具。

小黄鸭插件在独立仓库维护，不包含在本目录。保留上游及各组件的许可证、字体授权和来源记录。
