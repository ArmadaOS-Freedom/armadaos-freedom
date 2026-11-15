<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/armada-mark-white.svg">
    <img src=".github/assets/armada-mark-black.svg" alt="Armada" width="112">
  </picture>
</p>

<h1 align="center">ArmadaOS Freedom</h1>

<p align="center"><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a></p>

<p align="center"><strong>Turn your Android tablet into a portable gaming device—with a Steam Deck-style gaming mode, a full Linux desktop, and optional multi-boot.</strong></p>

<p align="center">
  A community-maintained operating system for ARM Android tablets,
  built on <a href="https://github.com/armada-os/armada">ArmadaOS</a>.
</p>

<p align="center">
  <a href="https://github.com/armada-os/armada"><img alt="Based on ArmadaOS" src="https://img.shields.io/badge/based_on-ArmadaOS-18181a?style=flat"></a>
  <a href="#device-support"><img alt="Y700 Gen 4, Gen 5 and Xiaoxin Pad Pro GT adaptation" src="https://img.shields.io/badge/devices-Y700_Gen_4_%26_5_%2B_Pad_Pro_GT-285580?style=flat"></a>
  <a href="LICENSE"><img alt="GPL-2.0-or-later license" src="https://img.shields.io/badge/license-GPL--2.0--or--later-18181a?style=flat"></a>
</p>

<p align="center">
  <a href="#getting-started"><strong>Getting started</strong></a>
  · <a href="#device-support">Supported devices</a>
  · <a href="#companion-tools">Companion tools</a>
  · <a href="#community-and-feedback">Community</a>
</p>

## About ArmadaOS Freedom

[ArmadaOS Freedom](https://github.com/ArmadaOS-Freedom/armadaos-freedom) is a community-maintained operating system based on [ArmadaOS](https://github.com/armada-os/armada), focused on hardware support and gaming on ARM Android tablets.

We build on ArmadaOS to create a tablet operating system with Steam Gaming Mode, FEX, Proton, KDE Plasma, and more. Our work also includes multi-boot support, hardware drivers, installation and maintenance tools, and gaming controls.

The system and its companion tools focus on:

- **Gaming and desktop modes**: Steam Gaming Mode and KDE Plasma, with FEX and Proton to run compatible PC games.
- **Optional multi-boot**: Use the kShimC launcher to keep Android and Linux side by side and choose which system to start.
- **Tablet hardware support**: A shared kernel with device-specific device trees, drivers, display modes, touch, audio, networking, power and USB configuration, plus battery and temperature reporting.
- **Touch and controller input**: Tablet touch input, G9 / G9 2026 controller mapping, RGB control, and double-Home quick access.
- **Installation and maintenance**: A companion installer for installation, backup, recovery and device maintenance, with support varying by model and platform.
- **System tools**: [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) for global and per-game compatibility, performance settings and TF card game-library setup, alongside other [companion tools](#companion-tools).

This repository maintains the Freedom system sources, kernel and drivers, boot support, and build configuration. Feature availability, tested behavior and update support vary by device; see the device documentation below.

## Getting started

1. **Check your device.** Open the matching [device guide](#device-support) and review its firmware, boot-chain, installation requirements and known issues.
2. **Prepare the installer and system package.** Follow the [Freedom Installer documentation](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer) for environment checks, backups and the package required by your model.
3. **Install to internal storage.** Follow the assistant's steps and the device guide, then use the system's gaming mode, desktop and companion tools as supported by your device.

The current installer provides installation to internal storage; installation of the system to a TF card is not yet available through the assistant. Windows and Apple silicon macOS versions have different feature coverage; see the installer documentation. Installation and repartitioning erase the affected personal data, so back up your files first.

To use a TF card as a game library, see [Companion tools](#companion-tools).

## Device support

Current work covers Lenovo Legion Y700 Gen 4, Y700 Gen 5, and Xiaoxin Pad Pro GT. They share the Linux **7.2.6** kernel, with separate device trees, boot payloads and hardware configuration for each model. Check the matching guide for installation requirements, available features, and known issues.

| Device | Model / SoC | Documentation | Known issues |
|---|---|---|---|
| Lenovo Legion Y700 Gen 4 | TB322FC / SM8750 | [Gen 4 documentation](devices/lenovo-tb322fc/README.md) | [Gen 4 known issues](devices/lenovo-tb322fc/README.md#已知问题与待完成项) |
| Lenovo Legion Tab Gen 5 / Y700 Gen 5 | TB323FU / SM8850P | [Gen 5 documentation](devices/lenovo-tb323fu/README.md) | [Gen 5 known issues](devices/lenovo-tb323fu/README.md#已知限制) |
| Lenovo Xiaoxin Pad Pro GT | TB710FU / SM8650 | [TB710FU documentation](devices/lenovo-tb710fu/README.md) | [TB710FU known issues](devices/lenovo-tb710fu/README.md#已知问题与待完成项) |

Boot images remain model-specific and must not be cross-flashed.

## kShimC multi-boot

The **kShimC** menu lets you choose Android or ArmadaOS Freedom. Available entries and boot support depend on your device:

- Android.
- ArmadaOS Freedom installed in internal storage.
- ArmadaOS Freedom on a TF / microSD card, where supported.

The menu also provides device settings, restart and shutdown, and advanced boot entries.

A **TF card game library** stores games for the running system; it is separate from installing or booting Linux on the card. See [Companion tools](#companion-tools).

<p align="center">
  <img src=".github/assets/kshimc-star-chart-en.png" alt="KShimC launcher — STAR_CHART theme, English menu" width="100%">
</p>

*STAR_CHART menu rendered from the current implementation; this is a software preview, not a photograph of the tablet.*

## Companion tools

| Project | Purpose |
|---|---|
| [Freedom Installer](https://github.com/ArmadaOS-Freedom/armadaos-freedom-installer) | Windows installation and maintenance, plus installation to internal storage on Apple silicon macOS. Features vary by model and platform. |
| [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) | Global and per-game compatibility and performance settings, tablet controls, and TF card game-library setup. |
| [Smart TF Card Allocator](https://github.com/ArmadaOS-Freedom/smart-tf-allocator) | An Android app for allocating microSD space between Android app storage and a Linux game library, and moving supported apps. Linux game-library setup is handled by Freedom Controller. |
| [UU Accelerator Plugin](https://github.com/ArmadaOS-Freedom/decky-uu-accelerator) | A third-party Decky plugin for official core installation, phone pairing via QR code, and acceleration status monitoring. |

For a TF game library, first allocate the card's space on Android with Smart TF Card Allocator, then configure the reserved Linux partition through Freedom Controller's TF card game-library page. Initial allocation repartitions and erases the selected card; back it up first. App migration depends on Android, device firmware and app support. These tools configure storage and do not install Linux onto the card.

## Development

<details>
<summary>Build instructions and implementation details</summary>

System images use ArmadaOS's bootc build foundation. The shared kernel lives in
[`packages/kernel/`](packages/kernel/), hardware profiles in
[`system_files/usr/lib/armada/devices/`](system_files/usr/lib/armada/devices/),
and device-specific resources and boot guides in [`devices/`](devices/).
Package development, cache, and publication setup are documented in
[`packages/`](packages/README.md). Recipes use [just](https://just.systems/) and
[Podman](https://podman.io/); published packages are resolved with
[skopeo](https://github.com/containers/skopeo), so it needs to be installed.

```console
$ git submodule update --init --recursive
$ just check     # Run the test suite and check recipe formatting
$ just build     # Build the bootc container; not a ready-to-flash tablet image
$ just --list    # List development recipes
```

`just build` reuses prebuilt packages from the registries when the content hash
matches, so you normally do not need to build packages first. Run
`just package NAME` only when you change that package or it is missing; it builds
one local package. `just packages` intentionally rebuilds every package.

### Freedom Control

The control plugin ships from the pinned
[Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control) dependency at
`decky/freedom-control`. This repository no longer maintains an inline copy of
the plugin source, so a fresh clone needs the pinned revision:

```console
$ git submodule update --init --recursive
```

To move to a newer plugin revision, update the dependency deliberately and
commit the new pointer:

```console
$ git submodule update --remote decky/freedom-control
$ just check
$ git add decky/freedom-control
$ git commit -m "Update Freedom Control"
```

The plugin keeps its installed directory and its service and socket interfaces,
so existing settings and integrations continue to work. A few SM8850 companion
files — the power daemon, the TB323FU board profile, and the SM8850 power-profile
layer — come from the same dependency; the rest of the host OS remains here.

Follow the configuration and build guide for the target device. System updates
use Freedom's own repository; see each device page for boot synchronization
and OTA support status.

### kShimC boot implementation

The launcher, Linux kernel and device tree are packaged as boot payloads in the Android boot image. Linux mounts the root filesystem selected by the internal-storage or TF-card entry. These are separate system installation targets.

The supported models share the [kShimC launcher sources](devices/lenovo-tb323fu/boot/kshim-success-source/README.md), with separate device profiles and boot payloads. Build paths are documented in the [Gen 4](devices/lenovo-tb322fc/boot/README.md), [Gen 5](devices/lenovo-tb323fu/boot/star-chart-build/README.md), and [Xiaoxin Pad Pro GT](devices/lenovo-tb710fu/boot/README.md) boot guides. For system package builds, see the [9008 / EDL package guide](docs/edl-package.md).

</details>

## Community and feedback

Report bugs and feature requests through [Issues](https://github.com/ArmadaOS-Freedom/armadaos-freedom/issues), with your device model, system and kernel versions, steps to reproduce the problem, and relevant logs.

- **Chinese-speaking community**: [Danyi's Bilibili channel](https://space.bilibili.com/258786) for updates, tutorials and demos.
- **Main QQ fan group**: 487945399
- **Second QQ fan group**: 332038419
- **International contact**: [mydanyi@qq.com](mailto:mydanyi@qq.com)

See the [ArmadaOS Freedom organization](https://github.com/ArmadaOS-Freedom) for all projects and updates.

## Credits

- System foundation: [ArmadaOS](https://github.com/armada-os/armada), built on
  [Fedora bootc](https://github.com/bootc-dev/bootc).
- Boot menu: [kShimC](https://github.com/sunflower2333/kShimC/tree/feature/2610).
- Control plugin: [Freedom Control](https://github.com/ArmadaOS-Freedom/freedom-control).
- Armada logo: [Rax](https://github.com/Raxcoms).

See the [upstream credits](https://armadaos.dev/project/credits/) for more
contributors and the [device documentation](#device-support) for component
origins and pinned versions. Individual components retain their source
attribution and licenses.

## License

Original armadaos-freedom software and scripts are licensed under **GPL-2.0-or-later**, unless a file or component specifies otherwise. Copyright (C) 2026 danyi and the armadaos-freedom contributors, for their original contributions. Inherited ArmadaOS code and other components retain their copyrights and applicable licenses. See [LICENSE](LICENSE) and [licensing and copyright](LICENSING.md) ([简体中文](LICENSING.zh-CN.md)).
