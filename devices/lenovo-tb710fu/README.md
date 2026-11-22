# 联想小新 Pad Pro GT

ArmadaOS Freedom 统一内核与 kShimC 启动菜单对小新 Pad Pro GT 的适配。本页记录 TB710FU 的安装前提与步骤、功能状态、已知问题、所需固件和开发入口。

| 项目 | 配置 |
|---|---|
| 型号 | TB710FU（小新 Pad Pro GT，3200×2000 120 Hz） |
| SoC | Qualcomm SM8650（骁龙 8 Gen 3，`qcom,pineapple`） |
| 设备树标识 | `lenovo,tb710fu`、`qcom,sm8650` |
| 内核 | ArmadaOS Freedom 统一 Linux 7.2.6（默认构建），DTB `sm8650-lenovo-tb710fu.dtb` |
| 引导 | kShimC 菜单放在 boot_a，与原厂 Android 共存；Bootloader 未解锁 |
| 已验证的用户空间 | ArmadaOS Freedom（用安装器装到机身存储，与 HyperOS 共存）；此前也在 SteamOS-ARM-Port 的 sm8650 镜像上验证过 |

## 安装

### 前提

- **启动链：** 平板要先刷好 ZUI 1.5.04.495 的免解锁补充包（A/B 槽的 `abl`、`vbmeta` 等）。这条启动链里 vbmeta 的顶层密钥是 AOSP `testkey_rsa4096`，ABL 信任它，所以 Bootloader 保持**锁定**也能启动用同一测试密钥签名的 boot（`verifiedbootstate=green`）。其它启动链的机器不适用。
- **Android 一侧：** 机身上的 Android（原厂 ZUI 或基于 495 启动链的移植系统）能正常开机，并打开 USB 调试。安装会清空 Android 的个人数据；照片、应用数据等请先自行备份，小米／联想账号请先退出。
- **分区布局：** LUN0 的 `userdata` 要一直延伸到盘尾（原厂布局）。之前手动分过区的，要先恢复原厂分区表。
- **安装器：** 使用带 TB710FU 机型配置的 ArmadaOS Freedom 安装器（Windows 版或 macOS 版），以及包含 TB710FU 的 9008 合集。

### 步骤

安装器的“安装到内部存储”按 4 步进行，与其它机型相同：

1. **检查平板：** 通过 adb 读取机型、序列号、锁定状态和当前槽位。TB710FU **不进入 fastboot**（本机从 Android 重启到 fastboot 后会崩溃进 900E 转储模式），锁定状态和槽位改从 Android 属性读取。
2. **备份数据：** 平板重启到 9008，安装器按 USB 序列号核对是同一台平板，读出 6 个 LUN 的分区表和恢复所需的分区，保存到电脑上。
3. **分配空间：** 导入 9008 合集，选择留给 Android 的容量。剩余空间给 Linux（启动区 1 GiB、根分区、2 GiB 板端恢复区）。
4. **确认安装：** 写入分区表、Linux 启动区与根分区，再写入 `boot_b`、`boot_a`。boot 用 AOSP 测试密钥签名，保留原 footer 的 rollback index；**不改动 vbmeta、vbmeta_system 和槽位属性**，也不写入 `persist`、`modemst1/2`、`devinfo` 等本机唯一的分区。每次写入后都会读回校验。

装完后平板经 kShimC 菜单默认启动 ArmadaOS。菜单里也保留 Android 条目；安装后从菜单回到 Android（数据已清空，会重新初始化）还没有实测。

### 进入 9008

- 安装器会自动让平板从 Android 重启到 9008（`adb reboot edl`）。
- 需要手动进入时：拔线，长按电源键约 15–20 秒关机，按住音量＋和音量－再插线。
- kShimC 菜单“高级”里的 9008 入口在本机上进入后无法建立 firehose 连接，请用按键方式。

## 功能状态

以下为本机在 ArmadaOS Freedom（统一内核 7.2.6，用安装器装机）下的实测结果；最早一轮测试在 SteamOS 用户空间上进行，结果相同的项不再分开写。

| 功能 | 当前状态 |
|---|---|
| kShimC 启动菜单 | 显示、音量／电源键选择、超时默认项正常；gzip 压缩的 boot（解压后约 109 MB）被 ABL 接受 |
| Android 启动 | 菜单进入机身 Android（HyperOS 移植系统）：`verifiedbootstate=green`、`locked`（安装器装机前的手动打包 boot 上验证） |
| 显示 | NT36532 双 DSI + DSC，3200×2000@120，msm 原生驱动 |
| GPU | Adreno 750，Mesa freedreno／Turnip；频率 231–903 MHz；IFPC 关闭 |
| 触屏与手写笔 | NT36532 触摸正常；Lenovo Tab Pen Plus 在桌面下压感、倾斜、按键正常 |
| 扬声器 | 4 颗 AW882xx 功放经 TDM 出声，左右声道正常 |
| WiFi | WCN7850（ath12k），5 GHz 下载实测约 14 MB/s |
| 蓝牙 | 使用本机地址（来自 DT `local-bd-address`） |
| 电池与充电 | pmic-glink／battmgr 正常上报；墙充 PD 9 V 可快充（约 18 W），但到 80% 停止 |
| 亮度 | SY7758 主从两颗背光同步，0–4080 |
| 休眠 | s2idle 睡眠与唤醒后显示、触摸、笔、WiFi、声音恢复正常 |
| USB | 作为设备端正常；OTG 主机模式未测试 |
| 传感器 | 加速度计、光线经 ADSP 传感器中枢（SSC）读出，供桌面自动旋转和 Steam 自适应亮度使用（见下文“传感器”；原型已在本机验证，镜像集成待新镜像复测） |
| 游戏 | Steam 游戏模式正常；游戏兼容性与其它机型相同（FEX／Proton ARM64）。实测 Two Point Campus（带 Denuvo）无法运行 |

## 已知问题与待完成项

- **系统更新：** 系统更新后重新生成并写入 boot 的流程目前不支持 TB710FU（本机要求 boot 用测试密钥签名并写入 `boot_a`）。在支持之前，**装好后不要做系统更新**，否则可能无法启动。
- **桌面模式：** 当前镜像里 KWin 与 Plasma 组件版本不一致，切换到桌面会退回游戏模式。这个问题与机型无关，所有设备都受影响。
- **手写笔（游戏模式）：** gamescope 不处理手写笔（tablet-tool）事件，游戏模式下需要另外把笔转成触摸。
- **充电上限：** 充到 80% 后停止，与 sysfs 的 `charge_control_*_threshold` 无关，推测是 ADSP 一侧保存的上限（可能来自 Android 下的“智能充电”设置），尚未查清。
- **传感器：** 加速度计与光线的读取在本机原型上验证过（方向四个角度都对）；镜像集成后的桌面自动旋转、Steam 自适应亮度、休眠时停止传感器流还要在新镜像上复测。接近传感器、磁力计、陀螺仪未验证。
- **CPU：** TB710FU 是六核版骁龙 8 Gen 3，设备树仍按 8 核描述，cpu1、cpu2 启动失败（`psci: failed to boot`）后保持离线。机型配置按实际在线的核（cpu0、cpu3–7）划分；设备树还没改。
- **fastboot：** 本机从 Android 重启到 fastboot 后会崩溃进 900E 转储模式（触发条件未查清），所以安装器不使用 fastboot。
- **音频：** 只验证了扬声器。耳机、麦克风未测试。前端必须使用 S16：24 位输入会让 ADSP 的音频处理出错。
- **WiFi／显示的板级变通：** 部分补丁来自原移植项目的调试过程（ath12k 的 CE 轮询、DSI 控制器寄存器、PHY 时序等），按 `lenovo,tb710fu` 隔离，只在本机生效。是否都必要尚未逐项验证，后续可以继续精简。
- **视频编解码（iris）、相机、外接显示（DP）：** 未启用。
- **固件：** 设备专用固件随镜像提供（[`firmware/`](firmware/README.md)），见下文。

## 内核与启动

TB710FU 的内核支持在统一内核的默认构建里，与其它机型共用同一个内核包：

```sh
bash packages/kernel/scripts/build-kernel.sh
```

启动用的 DTB 为 `dtb/qcom/sm8650-lenovo-tb710fu.dtb`。补丁在 `patches/series` 末尾的 TB710FU 一节，涉及公共驱动的代码都按 `lenovo,tb710fu`（或专用 compatible）判断，其它机型不受影响：

| 部分 | 补丁 | 设备树（`dts/sm8650-lenovo-tb710fu.dts.patch`） |
|---|---|---|
| 板级、显示、GPU、背光 | `071x`：SY7758 跟随芯片、NT36532 面板、msm DSI 命令触发口与 ABL 控制器/PHY 状态、DSC 宽度取整、A750 不启用 IFPC | kShimC framebuffer 模板、GPU 903 MHz、ADSP/CDSP 固件路径、ADSP FastRPC 只用 secure 会话、触摸 compatible |
| 触摸与手写笔 | `072x`：NT36532 SPI 厂商驱动（`novatek,nt36532-ts-spi`）、开机打开笔扫描 | 笔 |
| 蓝牙与 WiFi | `073x`：DT 蓝牙地址、WCN7850 复位脉冲、ath12k 变通 | — |
| 扬声器 | `074x`：上游 TDM 时隙辅助函数（回移植）、q6apm TDM DAI、TDM 输出端点、4 声道上混、`lenovo,tb710fu-sndcard` | 4 颗 AW882xx 与声卡 |

板级 DTS 取自 MeteoricmeteorLL/tb710fu-linux 的固定提交 `16e800d4f6e5cf4a8517900a9e11eec0ebc91bd3`（该仓库 LICENSE 第 3 节：`kernel/sources/` 适用 GPL-2.0），只加了 SPDX 与来源注释；本项目的改动都在 `dts/sm8650-lenovo-tb710fu.dts.patch` 里。扬声器功放使用统一构建的 out-of-tree [`aw882xx`](../../packages/kernel/out-of-tree/aw882xx/README.md)（与五代相同的 0x2308 芯片）。

引导构建见 [TB710FU 引导说明](boot/README.md)。kShimC 的 TB710FU 支持在共享源码中，全部只在 `qcom,pineapple` 上生效：SM8650 的启动画面读取（ABL 用 4 个 VIG 管线拼接扫描输出）、跳过 size 为 0 的保留内存节点、重启白名单，以及交接前清理只读的 WB 映射；菜单配置为 `configs/tb710fu_android_defconfig`。**TB710FU 的 boot 镜像不能刷到其它机型，反之亦然。**

## 固件

镜像已自带本机需要的全部内核固件，不需要从平板上提取。唯一的例外是传感器中枢（SSC）用的文件：它们在开机时从当前 Android 槽的 dsp、vendor、persist 分区只读复制（见[传感器](#传感器)），所以安装 Linux 时这些 Android 分区要保留（安装器本来也不动它们）。

| 路径 | 来源 |
|---|---|
| `qcom/sm8650/xiaoxin/gt/adsp.mbn`、`adsp_dtb.mbn`、`cdsp.mbn`、`cdsp_dtb.mbn`、`gen70900_zap.mbn`、`aw882xx_acf.bin`，`novatek/novatek_nt36532_fw.bin`、`novatek_nt36532_mp.bin` | 本机原厂固件，见 [`firmware/`](firmware/README.md)（构建时按 `SHA256SUMS` 校验） |
| `qcom/gen70900_sqe.fw`、`qcom/gen70900_aqe.fw`、`qcom/gmu_gen70900.bin` | linux-firmware（镜像自带） |
| `qcom/sm8650/Lenovo-TB710FU-tplg.bin` | 镜像自带，由 [`audio_topology/sm8650/Lenovo-TB710FU.m4`](../../audio_topology/sm8650/Lenovo-TB710FU.m4) 生成；扬声器端点为 TDM 输出模块（`0x0700100E`） |
| `ath12k/WCN7850/hw2.0/{amss,board-2,m3}.bin` | 镜像自带；`board-2.bin` 末尾追加了本机一项（`subsystem-device=3378`），数据原样取自 Fedora `atheros-firmware-20261019`（即 linux-firmware），许可为同目录的 [`LICENSE.QualcommAtheros_ath10k`](../../system_files/usr/lib/firmware/ath12k/WCN7850/hw2.0/LICENSE.QualcommAtheros_ath10k)（linux-firmware `WHENCE` 中 `ath12k/WCN7850/hw2.0/board-2.bin` 对应的许可） |
| `qca/hmtbtfw20.tlv` 等 | linux-firmware（蓝牙） |

功放驱动按设备树的 `firmware-name` 请求 `qcom/sm8650/xiaoxin/gt/aw882xx_acf.bin`，不会和其它机型根目录下的 `aw882xx_acf.bin` 冲突。

## 用户空间所需设置

- **WiFi：** 不需要额外设置。驱动按 PCI 子系统 ID 和芯片上报的 board-id（0xff）算出 `bus=pci,vendor=17cb,device=1107,subsystem-vendor=17cb,subsystem-device=3378,qmi-chip-id=2,qmi-board-id=255`，镜像的 `board-2.bin` 里有这一项。
- **扬声器：** 不需要额外设置。UCM（`/usr/share/alsa/ucm2/Lenovo/TB710FU/`）打开 `SECONDARY_TDM_RX_0 Audio Mixer MultiMedia1` 和 4 颗功放，功放音量与 Android 下相同（0x34/0x35 衰减 21 档，0x36/0x37 衰减 11 档）。PipeWire 的输出为 `alsa_output.platform-sound.HiFi__Speaker__sink`。
- **手写笔：** 补丁 0721 让驱动开机即打开笔扫描，并给坐标设好分辨率，桌面（KWin/libinput）可以直接使用；`/proc/support_pen` 仍可手动开关。游戏模式下 gamescope 不处理手写笔（tablet-tool）事件，需要另外把笔转成触摸。
- **PipeWire：** WirePlumber 规则 `53-lenovo-tb710fu-speakers.conf`（只匹配声卡名 `Lenovo-TB710FU`）把输出固定为 `S16LE`，并关闭空闲挂起：24 位输入会让 ADSP 的音频处理出错；每次从挂起恢复都会重新启动 4 颗功放，偶尔会锁不上 TDM 时钟。

## 传感器

加速度计、光线等传感器挂在 ADSP 的传感器中枢（SSC）上，不是 IIO 设备。读取链路：

1. **设备树**：ADSP 的 FastRPC 节点去掉 `qcom,non-secure-domain`，内核只建 `/dev/fastrpc-adsp-secure`。SM8650 的传感器 PD 只在 secure 会话下加载传感器；用普通节点时 ADSP 每秒报 `Handover signaled, but it already happened`，传感器列表为空。这个节点只有 hexagonrpcd 使用，音频走 GPR，CDSP 不变。
2. **传感器文件**：传感器中枢运行时向主机要 DSP 库、传感器配置和注册表（含本机校准）。这些是联想的私有文件，镜像不带；`tb710fu-hexagonrpcd.service` 每次启动前先运行 `tb710fu-sensors-prepare.service`（单独的单元：守护进程的 `DevicePolicy=` 作用于整个控制组，在它里面运行的准备脚本打不开 Android 分区），从当前 Android 槽**只读**复制到 `/var/lib/tb710fu-sensors`：

   | 来源 | 目标 |
   |---|---|
   | `dsp_<槽>` 分区（ext4）的 `/adsp` | `fw/dsp/adsp/` |
   | super 里 `vendor_<槽>`（erofs）的 `/etc/sensors/config/`、`/etc/sensors/sns_reg_config` | `fw/sensors/config/`、`fw/sensors/sns_reg.conf` |
   | `persist` 分区（ext4）的 `/sensors/registry/` | `registry/`（只在第一次复制） |

   当前槽取自 GPT 属性（`boot_a`/`boot_b` 的 active 位）；super 的动态分区表在脚本里解析（主副本读不出或校验不过时用备份副本，同 liblp），ext4 用 `debugfs`（不带 `-w`），erofs 用 `dump.erofs --offset`，都只读打开，不挂载、**不写任何分区**。`fw/` 在槽、dsp 分区或 vendor 里的传感器文件变化时重新提取（`stamp.json` 记录来源）；每次复制都对照源文件系统的清单核对名字和大小，核对通过才替换旧文件；`dump.erofs` 在 stderr 报错（即使退出码为 0）也算失败。提取失败时服务启动失败，由 `Restart=` 连同提取一起重试（3 秒起，逐步退到 1 分钟）。

   **分区布局要求**：当前槽的 `vendor_<槽>` 必须是 erofs，并且在 super 里只占**一个线性区段**。Android 重新分区后如果 vendor 被拆成多个区段，准备脚本会报 `vendor_<槽> is not one extent` 并一直失败，重试不会好转——这是不支持的布局，不是暂时的读取错误；格式不对则报 `vendor is not erofs`。
3. **hexagonrpcd**（`tb710fu-hexagonrpcd.service`）：传感器中枢启动时会写注册表目录（写 `DIR` 探测可写、写临时文件后改名为 `parsed_file_list.csv`），失败就不加载传感器。镜像里的 hexagonrpcd 带两个补丁（[`packages/hexagonrpc`](../../packages/hexagonrpc/PATCHES.md)）：`HEXAGONRPCD_WRITABLE_REGISTRY` 指向的主机目录作为可写注册表（只允许该目录及其 `registry/` 子目录里的普通文件），以及超过 256 字节的输入缓冲。这里指向 `/var/lib/tb710fu-sensors/registry`，persist 本身保持原样。要恢复成 persist 里的原始注册表，删除这个目录后重启即可。
4. **iio-sensor-proxy**：镜像改用带 libssc 后端的 3.9（[`packages/iio-sensor-proxy`](../../packages/iio-sensor-proxy/BASE.env)），其它机型的 IIO 传感器行为不变。`81-armada-tb710fu-sensors.rules` 给 secure 节点设 `IIO_SENSOR_PROXY_TYPE=ssc-accel ssc-light ssc-proximity` 和挂载矩阵 `0, 1, 0; -1, 0, 0; 0, 0, 1`（面板原生横屏；顺时针转一圈依次报 normal → left-up → bottom-up → right-up），并在节点出现时启动上面两个服务。传感器中枢在 hexagonrpcd 启动几秒后才就绪，`tb710fu-sensors-generator` 只在本机给 iio-sensor-proxy 加上找不到传感器时重试。休眠前停止 iio-sensor-proxy、唤醒后恢复（`45-tb710fu-sensors`），避免加速度计流在 s2idle 中反复唤醒 ADSP（五代上测得的现象）。

以上单元和规则都只在 `lenovo,tb710fu` 上生效。光线读数经 `armada-als-bridge` 提供给 Steam 的自适应亮度。

## 源码与来源

| 组件 | 来源 |
|---|---|
| 板级 DTS、面板／触摸／WiFi／DSI 变通 | [MeteoricmeteorLL/tb710fu-linux](https://github.com/MeteoricmeteorLL/tb710fu-linux) `16e800d4f6e5cf4a8517900a9e11eec0ebc91bd3`；面板与触摸的点亮工作来自 [SpendyYT](https://github.com/SpendyYT) |
| NT36532 触摸驱动 | Novatek 厂商驱动（GPL-2.0-or-later），经上述项目移植 |
| TDM 时隙辅助函数 | 上游 Linux `1e844ecbc9cb`、`0a9e00d5ebdf`（原作者署名保留在补丁中） |
| TDM DAI 操作 | 参照上游 Linux `4d0840175893`，按本树（无 MI2S 时钟控制）改写 |
| TDM 端点参数 | 在本机 HyperOS 上抓取 AudioReach 用户空间实际下发的参数 |
| 传感器 | [linux-msm/hexagonrpc](https://github.com/linux-msm/hexagonrpc)（GPL-3.0-or-later）`da7a3742ab8d5273a281b70ee11f16fb2cb6a690` 加本项目补丁；[libssc](https://codeberg.org/DylanVanAssche/libssc)（GPL-3.0-or-later）`033e2a32095c024d6dbdfb1d4ae1db4a94be32f1`；[iio-sensor-proxy](https://gitlab.freedesktop.org/hadess/iio-sensor-proxy) 3.9（GPL-3.0-or-later）；做法参考五代的 tb323fu-linux |
| kShimC、DualBootKernelPatcher | 与五代共用，见[五代组件来源](../lenovo-tb323fu/docs/SOURCES.md) |
