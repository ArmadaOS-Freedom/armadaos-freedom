# Y700 五代扬声器保护部署

本配置适用于 TB323FU 的声卡和功放，使用 WirePlumber 软件 DSP 保护扬声器，并将功放增益服务绑定到 PipeWire 和 WirePlumber 的生命周期。四代使用自己的音频配置，不能直接套用这些声卡名称和增益值。设备状态见[五代设备文档](../README.md)。

## 文件映射

| 本仓库源文件 | 目标部署路径 | 属主/权限 |
| --- | --- | --- |
| [DSP 保护配置](../rootfs/usr/share/tb323fu/pipewire/tb323fu-speakers-protection.json) | `/usr/share/tb323fu/pipewire/tb323fu-speakers-protection.json` | root:root / mode 0644 |
| [扬声器路由脚本](../../../system_files/usr/share/wireplumber/scripts/device/tb323fu-speaker-volume.lua) | `/usr/share/wireplumber/scripts/device/tb323fu-speaker-volume.lua` | root:root / mode 0644 |
| [WirePlumber 规则](../rootfs/usr/share/wireplumber/wireplumber.conf.d/61-tb323fu-speakers-dsp.conf) | `/usr/share/wireplumber/wireplumber.conf.d/61-tb323fu-speakers-dsp.conf` | root:root / mode 0644 |
| [功放增益服务](../rootfs/usr/lib/systemd/user/tb323fu-speaker-gain.service) | `/usr/lib/systemd/user/tb323fu-speaker-gain.service` | root:root / mode 0644 |
| [功放增益程序](../rootfs/usr/libexec/tb323fu/tb323fu-speaker-gain) | `/usr/libexec/tb323fu/tb323fu-speaker-gain` | root:root / mode 0755 |

`61-tb323fu-speakers-dsp.conf` 中 `filter-path` 写死为上面的绝对部署路径。

增益程序需要 `amixer`、`pactl` 和当前用户对 ALSA 控制设备的访问权限；可选读取 `/etc/tb323fu/audio.conf`，未提供时使用程序内的默认衰减值。DSP 还依赖 `swh-plugins` 中的 `sc4_1882` 和 `fast_lookahead_limiter_1913`。

## 从旧配置迁移

1. 备份并**停用**旧的手动加载配置 `/usr/share/pipewire/pipewire.conf.d/60-tb323fu-speakers.conf`——
   必须把它**移出所有会被扫描的 `*.conf` 目录**；仅改 basename 但仍以 `.conf` 结尾无效。
   目的是确保不再有原手动 `context.modules` 实例，避免出现两个同名保护 sink。
2. 按上表部署五个文件。先安装扬声器路由脚本，再部署引用该脚本的 WirePlumber 规则。
3. 由 root 在**实际的 armada 用户会话**中（而非 root 自身的 user 会话）替换该 unit 后执行
   `systemctl --user daemon-reload` 与 `systemctl --user enable tb323fu-speaker-gain.service`。
   `enable` 依据 `WantedBy=pipewire.service wireplumber.service` 同时创建
   `pipewire.service.wants/tb323fu-speaker-gain.service` 与
   `wireplumber.service.wants/tb323fu-speaker-gain.service`。unit 现为
   `After=pipewire.service wireplumber.service` 且 `BindsTo=pipewire.service wireplumber.service`，
   因此 WirePlumber 的停止/重启一并参与 gain 生命周期：`ExecStop` 会在被绑定的 WP 服务停止前停下 gain；
   WP 由用户 `Wants` 启动时可一并启动 gain，并让既有 helper 等待保护 filter。
4. 由 root 重启/重载音频服务完成切换。

说明：
- 真实 RAW node 仍保留给 WirePlumber 连接使用；但对普通 PipeWire/Pulse 客户端不再可见
  （`hide-parent` 把匹配硬件 node 的所有非 wireplumber.daemon 客户端权限设为 `-`，WP 自身仍可链接）。
- 规则只按精确 `node.name` 匹配该 HiFi speaker sink；耳机 / Bluetooth / 其他 sink 不匹配。
- 保留原有两个 LADSPA 插件依赖（swh-plugins：`sc4_1882`、`fast_lookahead_limiter_1913`），未引入新依赖。

## 回滚

1. 先在实际用户会话中停止并禁用 `tb323fu-speaker-gain.service`，使功放恢复安全衰减，再停用新增的 WP fragment（移出被扫描的 `*.conf` 目录）。
2. 恢复原手动 `60-tb323fu-speakers.conf`。
3. 由 root 重启音频服务。
