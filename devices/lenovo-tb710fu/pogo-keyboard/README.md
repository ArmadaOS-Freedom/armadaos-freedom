# TB710FU magnetic keyboard (pogo keyboard + touchpad)

参考实现：在真机上跑通的磁吸键盘支持。**这是可直接工作的原始实现**，不是按仓库布局重写过的版本——如果方向认可，我可以按 `packages/` / `system_files/` 的规矩重新组织。

## 硬件

| 项 | 值 |
|---|---|
| UART | `qupv3_se7` = `serial@a9c000`（`qcom,geni-uart`），921600 8N1 |
| 引脚 | `tlmm` GPIO62 (TX) / GPIO63 (RX)，function `qup1_se7` |
| `tx-en` | **GPIO100，必须由主机输出低电平**，否则键盘不发任何字节 |
| 供电 | `vreg_l1b_1p8`（pogo 逻辑 1.8 V，默认**从未开启**）+ `vreg_l8b_1p8`（键盘主供电） |
| L8B 电压 | PMIC 上限 **3.296 V**；请求 3.3 V 会被拒（`unsupportable voltage range: 3300000-3296000uV`）。2.96 V 能用但不稳，3.296 V 稳定 |
| 吸附检测 | GPIO81，DTS 里被描述为 `SW_LID`（`gpio-keys/event-hall-sensor`） |

## 组成

- `dts/` —— 不属于本目录，实际改动在 `packages/kernel/dts/sm8650-lenovo-tb710fu.dts.patch`（新增 `uart7` 节点 + `qup_uart7_default` pinctrl）。
- `kernel/pogo_power.c` —— 可加载模块（323 行）：按全局名取两路 LDO（`regulator_get(NULL, "vreg_l1b_1p8"/"vreg_l8b_1p8")`）、用 gpiod lookup table 绑 GPIO100，导出 `/sys/class/pogo_power/power/{enable,txen}`。
- `userspace/tbkb.py` —— 用户态守护进程（约 1000 行）：PPP 解帧 + 解扰 + 校验、uinput 注入键盘与触摸板、0x12 consumer 功能键、CapsLock 灯闭环（合成器 `EV_LED` + 键盘状态帧回报）。
- `systemd/*.service` —— 先上电（`tb-pogo-power`），再起守护进程（`tb-pogo-keyboard`）。
- `docs/keyboard-protocol.md` —— 协议细节（帧格式、扰码表、包类型、LED 命令）。

## 安装（ArmadaOS：`/usr` 只读、`/var` 可写）

```sh
# 1. 编译模块（需要与运行内核完全一致的源码树/配置）
make -C <kernel-tree> M=$PWD/kernel modules
# 2. 部署
install -D -m640 kernel/pogo_power.ko /var/lib/pogo/pogo_power.ko
install -D -m640 userspace/tbkb.py     /var/lib/pogo/tbkb.py
install -m644 systemd/*.service        /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tb-pogo-power.service tb-pogo-keyboard.service
```

## 已在真机验证

打字、Delete/方向键、触摸板（移动/轻点/双指滚动/双指轻点右键/三指拖）、0x12 功能键（静音/音量±/麦克风/亮度±/截屏/放大/分屏/收藏）、CapsLock 灯闭环（`keys=39 sends=39 mismatch=0`，延迟 45–130 ms）。

## 需要注意 / 待商量

1. **模块 vs 纯用户态**：`/sys/class/regulator/*` 的 `microvolts`/`state` 都是 `DEVICE_ATTR_RO`（`drivers/regulator/core.c:623/694`），用户态改不了；所以要么 DTS 里把两路写成 `regulator-boot-on`/`always-on` + 固定电压（**开机即 3.296 V 是否波及 SDHCI/OCP 共用该轨尚未验证**），要么保留一个小模块。`tx-en` 那边不需要模块，`gpioset` 即可。
2. **`SW_LID`**：GPIO81 现在被当成合盖开关，键盘一吸上就"合盖"。我目前是 `unbind gpio-keys` 绕过的（代价：机身音量键失效）。是否改 DTS 拆分更合适，想听你的意见。
3. 守护进程里还有几块"整机定制"（KWin 分屏、自研截图、会话切换），与本功能无关，PR 里可以删掉。
