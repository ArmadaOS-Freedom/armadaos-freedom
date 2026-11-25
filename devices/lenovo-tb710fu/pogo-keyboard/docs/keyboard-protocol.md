# 磁吸键盘协议（Lenovo Keyboard Pack，TB710FU）

> 逆向来源：Android 侧 `lenovo_keyboard.ko`（`lenovo_scram`/`lenovo_disscram`/`PPPFrameDecode`）
> + 驱动 `debug_log` 抓到的真实帧。样本与二进制在 `work/keyboard/`。

## 硬件通路

```
pogo 键盘 ── UART(Geni) ── /dev/ttyHS1
   DT: /soc/lenovo_kb  compatible = "lenovo,lenovo_keyboard"
   uart-tx-gpios  = GPIO 62      uart-rx-gpios = GPIO 63
   tx-en-gpios    = GPIO 100     plug-gpios   = GPIO 81
   uart-wake-gpios= GPIO 137     vdd_3p3 / vdd_1p8
```

Android 侧分两层：
- 内核模块 `lenovo_keyboard.ko`：帧同步、解扰、上电时序、插拔检测、LED、输入注入
- 用户态 HAL `vendor.lenovo.hardware.keyboard-service`：命令下发 + `keyboard_status`

## 帧格式（PPP 风格）

```
7E | FF | seed | enc[0..n-1] | chk | 7E
```

- `7E`：帧头/帧尾；`FF`：PPP 地址字节
- `seed`：解扰种子，**取低 6 位**参与索引
- `chk = (seed + Σ enc) & 0xFF`
- 明文：`plain[i] = enc[i] ^ keyDict[(seed + i) & 0x3F]`
- 帧内出现 `7E`/`7D` 时按 PPP 规则转义（`7D` + 字节^0x20）

## keyDict（64 字节）

```
b8 02 d9 57 c6 48 c2 13 d7 a6 30 bf 40 d3 07 ce
c3 ab fa 51 62 18 bd 84 7d ca e0 cf f2 ac bc 12
f5 14 d0 72 dc 1a 50 00 00 32 d1 7b 33 b6 10 dd
6a 21 eb 7e 52 5b 3d 9f 85 cb 1f 66 73 96 a1 60
```

（`lenovo_keyboard.ko` 的 `.data+0x230`，符号名 `keyDict`）

## 明文包（cmd, len, data...）

| cmd | 含义 | 例子 |
|---|---|---|
| `0x11` | 键盘报告（HID 风格） | `11 08 00 00 05 00 00 00 00 00` = 'b' |
| `0x13` | 触摸板 | `13 06 00 07 8b 04 2e 06` |
| `0x16` | 状态/保活（约 1 s） | `16 04 04 02 bb 00` |

键盘报告布局（实测）：

```
11 08 | mod(1) | 00 | key1..key6 (HID usage)
        0x28 = Enter, 0x05 = 'b'
```

## 解码脚本

`work/keyboard/decode.py`：喂一行 dmesg 原始帧，输出明文。
（也可作为 Linux 侧用户态守护进程的解析核心。）

## Linux 侧待办

1. **设备树**：补 `/soc/lenovo_kb` 节点 + 打开对应 Geni UART（`/dev/ttyHS1` 在 Android 上对应
   `msm_geni_serial`；需确认 QUP 实例号）+ pinctrl（GPIO 62/63 复用为 UART）+ 两路电源
2. **上电时序**：`tx-en`（GPIO100）半双工方向、`plug`（GPIO81）插入检测、`wake`（GPIO137）
3. **用户态**：读串口 → PPP 解帧 → 校验和 → 解扰 → 按 cmd 分发：
   - `0x11` → uinput 键盘事件
   - `0x13` → uinput 相对/绝对指针
   - 回写 LED（大写/静音/麦克风）用同样的帧格式
4. 可选：写一个 serdev 内核驱动替代用户态守护进程
