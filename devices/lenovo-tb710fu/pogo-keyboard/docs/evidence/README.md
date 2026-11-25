# 实机运行证据（TB710FU / ArmadaOS 20261125.d944532 / kernel 7.2.6）

- `live-status.png` —— 设备上直接打开的运行状态：`pogo_power` 已加载、`/sys/class/pogo_power/power/{enable,txen}`、两路供电轨
  （`vreg_l8b_1p8` 3.296 V / `vreg_l1b_1p8` 1.8 V 均 enabled）、`/dev/ttyHS2`、两个 unit active、
  `dd if=/dev/ttyHS2` 的原始帧、以及守护进程解码出来的 `status 16040402bb00` / `action usage=…` / `leds caps=… mute=… mic=…`。
- `frames-12s.bin` —— 同一时刻 `/dev/ttyHS2` 上抓到的原始字节流（12 秒）。

抓包时键盘空闲，所以流里绝大多数是 0x16 状态帧（约 1 Hz）与偶发按键帧；帧格式与校验规则见 `../docs/keyboard-protocol.md`。
