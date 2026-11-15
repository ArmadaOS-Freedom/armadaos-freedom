<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/armada-mark-white.svg">
    <img src=".github/assets/armada-mark-black.svg" alt="Armada 标志" width="112">
  </picture>
</p>

<h1 align="center">ArmadaOS Freedom</h1>

<p align="center"><a href="README.md">English</a> · <strong>简体中文</strong></p>

<p align="center"><strong>让 ARM/安卓 平板拥有类 SteamDeck 的游戏模式、完整的 Linux 桌面与可选的多系统启动，让你拥有随身游戏机的体验</strong></p>

<p align="center">
  基于 <a href="https://github.com/armada-os/armada">ArmadaOS</a>、由社区维护的 ARM 安卓平板游戏系统。
</p>

<p align="center">
  <a href="https://github.com/armada-os/armada"><img alt="基于 ArmadaOS" src="https://img.shields.io/badge/基于-ArmadaOS-18181a?style=flat"></a>
  <a href="#设备支持"><img alt="Y700 四代、五代与小新 Pad Pro GT 适配" src="https://img.shields.io/badge/当前适配-Y700_四代与五代_及_Pad_Pro_GT-285580?style=flat"></a>
  <a href="LICENSE"><img alt="GPL-2.0-or-later 许可证" src="https://img.shields.io/badge/许可证-GPL--2.0--or--later-18181a?style=flat"></a>
</p>

<p align="center">
  <a href="#安装与使用"><strong>安装与使用</strong></a>
  · <a href="#设备支持">支持设备</a>
  · <a href="#配套工具">配套工具</a>
  · <a href="#交流与反馈">交流反馈</a>
</p>

## 关于 ArmadaOS Freedom

[ArmadaOS Freedom](https://github.com/ArmadaOS-Freedom/armadaos-freedom) 是基于 [ArmadaOS](https://github.com/armada-os/armada) 的社区维护项目，专注于 ARM 平板的硬件适配与游戏体验。

我们基于 ArmadaOS 的系统基础，将 Steam 游戏模式、FEX、Proton 和 KDE Plasma 桌面等功能带到平板上，并围绕多系统引导、硬件驱动、安装维护和游戏控制开发配套工具，打造的一个全新系统。

系统功能与配套工具主要围绕以下方面：

- **游戏模式与桌面模式**：提供 Steam 游戏模式与 KDE Plasma 桌面，通过 FEX 和 Proton 运行兼容的 PC 游戏。
- **可选的多系统启动**：通过 kShimC 启动器让 Android 与 Linux 共存，按需选择要启动的系统。
- **平板硬件适配**：共用内核，按机型维护设备树、驱动、显示档位、触屏、音频、网络、电源和 USB 配置，并提供电池与温度数据采集。
- **触屏与手柄操作**：适配平板触屏，以及 G9／G9 2026 手柄按键映射、RGB 控制和双击 Home 打开右侧快捷菜单。
- **安装与维护**：通过配套安装助手完成安装、备份、恢复和设备维护，具体功能因机型与平台而异。
- **系统配套工具**：通过 [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) 管理全局与逐游戏的兼容性、性能设置和 TF 卡游戏库，并提供其他[配套工具](#配套工具)。

本仓库维护 Freedom 系统源码、内核与驱动、引导和构建配置。各项功能的可用范围、已测状态和更新支持因设备而异，具体见下方机型文档。

## 安装与使用

1. **确认机型与条件**：查看对应的[设备文档](#设备支持)，核对固件、启动链、安装要求和已知问题。
2. **准备安装助手与系统包**：按照 [Freedom 安装助手](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer) 的说明完成环境检查、备份，并准备匹配当前机型的系统包。
3. **安装到机身存储**：按安装助手和机型文档的步骤操作，再根据设备支持情况使用游戏模式、桌面和配套工具。

当前安装助手提供机身存储安装，尚未开放 TF 卡系统安装；Windows 与 Apple 芯片 macOS 版的功能范围见安装助手说明。安装与重新分区会清除相关个人数据，请先备份。

使用 TF 卡扩展游戏库，请看[配套工具](#配套工具)。

## 设备支持

目前围绕联想拯救者 Y700 四代、五代和小新 Pad Pro GT 开展适配。三款设备共用 Linux **7.2.6** 内核，分别维护设备树、启动载荷与硬件配置。安装前请查看对应机型的要求、功能状态和已知问题。

| 设备 | 型号 / SoC | 设备文档 | 已知问题 |
|---|---|---|---|
| 联想拯救者 Y700 四代 | TB322FC / SM8750 | [四代设备文档](devices/lenovo-tb322fc/README.md) | [四代已知问题](devices/lenovo-tb322fc/README.md#已知问题与待完成项) |
| 联想拯救者 Y700 五代 | TB323FU / SM8850P | [五代设备文档](devices/lenovo-tb323fu/README.md) | [五代已知问题](devices/lenovo-tb323fu/README.md#已知限制) |
| 联想小新 Pad Pro GT | TB710FU / SM8650 | [TB710FU 设备文档](devices/lenovo-tb710fu/README.md) | [TB710FU 已知问题](devices/lenovo-tb710fu/README.md#已知问题与待完成项) |

各机型的启动镜像仍需匹配各自设备，不能直接互刷。

## kShimC 多系统启动

通过 **kShimC** 菜单，可以选择启动 Android 或 ArmadaOS Freedom。具体入口与支持范围因机型而异：

- Android。
- 安装在机身存储中的 ArmadaOS Freedom。
- 安装在 TF／microSD 卡中的 ArmadaOS Freedom，以机型支持情况为准。

菜单还提供设备设置、重启、关机和高级启动入口。

**TF 卡游戏库**用于给正在运行的系统存放游戏，与“把 Linux 系统安装到 TF 卡并从卡启动”是两件事，使用方法见[配套工具](#配套工具)。

<p align="center">
  <img src=".github/assets/kshimc-star-chart-zh.png" alt="KShimC 启动器：STAR_CHART 主题简体中文菜单" width="100%">
</p>

*上图为当前实现生成的 STAR_CHART 菜单渲染预览，非平板实拍。*

## 配套工具

| 项目 | 用途 |
|---|---|
| [Freedom 安装助手](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer) | Windows 安装与维护，以及 Apple 芯片 macOS 上的机身存储安装；功能因机型与平台而异。 |
| [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) | 管理全局与逐游戏的兼容性、性能设置、平板控制和 TF 卡游戏库。 |
| [智能 TF 卡分配器](https://github.com/ArmadaOS-Freedom/smart-tf-allocator) | Android 端 TF 卡分配工具，划分 Android 应用存储与 Linux 游戏库空间，可以迁移受支持的应用；Linux 游戏库配置由 Freedom Controller 完成。 |
| [UU 加速器插件](https://github.com/ArmadaOS-Freedom/decky-uu-accelerator) | 第三方 Decky 插件，提供官方核心安装、手机扫码绑定和加速状态查看。 |

使用 TF 游戏库时，先在 Android 端通过智能 TF 卡分配器划分空间，再到 Freedom Controller 的 TF 卡游戏库页面配置预留的 Linux 分区。首次分配会重新分区并清空所选整张卡，请先备份。应用迁移范围取决于 Android、设备固件及应用本身。这套工具用于配置存储，不会把 Linux 系统安装到 TF 卡。

## 开发与构建

<details>
<summary>构建说明与实现细节</summary>

系统镜像沿用 ArmadaOS 的 bootc 构建基础；统一内核位于 [`packages/kernel/`](packages/kernel/)，机型配置位于 [`system_files/usr/lib/armada/devices/`](system_files/usr/lib/armada/devices/)，设备专用资源和引导说明位于 [`devices/`](devices/)。软件包开发、缓存与发布配置见 [`packages/`](packages/README.md)。构建命令使用 [just](https://just.systems/) 和 [Podman](https://podman.io/)，并依赖 [skopeo](https://github.com/containers/skopeo) 解析已发布的软件包，请先安装：

```console
$ git submodule update --init --recursive
$ just check     # 运行测试并检查构建配方格式
$ just build     # 构建 bootc 容器镜像；尚非可直接刷入平板的完整镜像
$ just --list    # 查看开发命令
```

`just build` 会在内容哈希一致时复用注册表中已构建的软件包，通常无需先构建软件包。仅在修改了某个软件包或该包缺失时运行 `just package NAME`，它只构建指定的本地软件包；`just packages` 则会主动重建所有软件包。

### Freedom Control

控制插件来自固定版本的 [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) 依赖，位于 `decky/freedom-control`。本仓库不再维护插件源码副本，因此首次检出后需要获取固定版本：

```console
$ git submodule update --init --recursive
```

需要升级插件版本时，请显式更新依赖并提交新的指针：

```console
$ git submodule update --remote decky/freedom-control
$ just check
$ git add decky/freedom-control
$ git commit -m "Update Freedom Control"
```

插件仍安装在原有目录，服务与套接字接口保持不变，已有设置和相关集成可继续使用。少数 SM8850 配套文件（电源守护进程、TB323FU 机型配置和 SM8850 电源档位层）同样取自该依赖，系统其余部分仍由本仓库维护。

设备适配请遵循对应机型的配置与构建说明。系统更新源使用 Freedom 自有仓库；各机型的引导同步和 OTA 支持进度见设备文档。

### kShimC 引导实现

启动器、Linux 内核和设备树作为启动载荷打包到 Android boot 镜像中。Linux 启动后，根据所选条目挂载机身或 TF 卡上的根文件系统，二者是独立的系统安装目标。

各机型共用 [kShimC 启动器源码](devices/lenovo-tb323fu/boot/kshim-success-source/README.md)，分别选择对应的机型配置和启动载荷。构建方法见[四代](devices/lenovo-tb322fc/boot/README.md)、[五代](devices/lenovo-tb323fu/boot/star-chart-build/README.md)和[小新 Pad Pro GT](devices/lenovo-tb710fu/boot/README.md)引导说明；系统包构建见 [9008／EDL 部署包说明](docs/edl-package.zh-CN.md)。

</details>

## 交流与反馈

问题反馈与功能建议请提交到 [Issues](https://github.com/ArmadaOS-Freedom/armadaos-freedom/issues)，附上设备型号、系统与内核版本、复现步骤及相关日志。

- **中文交流**：[单翼的哔哩哔哩](https://space.bilibili.com/258786)，查看项目动态、使用教程与演示。
- **QQ 粉丝大群**：487945399
- **QQ 粉丝二群**：332038419
- **海外联系**：[mydanyi@qq.com](mailto:mydanyi@qq.com)

更多项目与动态见 [ArmadaOS Freedom 组织首页](https://github.com/ArmadaOS-Freedom)。

## 支持项目

ArmadaOS Freedom 目前靠热爱和自费投入持续推进。从启动、显示到供电和外设，每一项平板适配都需要反复测试，也需要长期跟进上游更新。

如果这些工作对你有帮助，欢迎通过支付宝或微信自愿赞助，支持设备测试、系统适配和持续维护。量力而行，谢谢每一份支持。

<p align="center">
  <img src=".github/assets/sponsor-zh.png" alt="支持 ArmadaOS Freedom 平板适配：支付宝和微信赞助二维码，自愿支持，量力而行" width="100%">
</p>

反馈问题、提交代码和完善文档，同样是在支持项目。

## 致谢

- 系统基础：[ArmadaOS](https://github.com/armada-os/armada)，基于 [Fedora bootc](https://github.com/bootc-dev/bootc)。
- 启动菜单：[kShimC](https://github.com/sunflower2333/kShimC/tree/feature/2610)。
- 控制插件：[Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control)。
- Armada 标志：[Rax](https://github.com/Raxcoms)。

更多贡献者见[上游致谢](https://armadaos.dev/project/credits/)，设备适配的组件来源与固定版本见[对应设备文档](#设备支持)。各组件保留原有的来源署名和许可证。

## 许可证

除文件或组件另有声明外，armadaos-freedom 原创软件和脚本采用 **GPL-2.0-or-later** 许可证。我们的原创贡献由 danyi 及 armadaos-freedom 各贡献者享有版权（Copyright (C) 2026）。继承的 ArmadaOS 代码及其他组件保留各自的版权和适用许可。详见 [LICENSE](LICENSE) 和[许可与版权说明](LICENSING.zh-CN.md)（[English](LICENSING.md)）。
