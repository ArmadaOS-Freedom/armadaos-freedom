# 游戏模式电源键

默认短按切换屏幕亮灭，长按仍打开 Steam 电源菜单。若希望短按触发 Steam 的睡眠流程，可以在 KDE 桌面终端为当前用户增加 systemd 配置：

```bash
mkdir -p ~/.config/systemd/user/armada-powerbuttond.service.d
cat > ~/.config/systemd/user/armada-powerbuttond.service.d/70-short-action.conf <<'EOF'
[Service]
Environment=ARMADA_POWERBUTTON_SHORT_ACTION=sleep
EOF
systemctl --user daemon-reload
systemctl --user try-restart armada-powerbuttond.service
```

切回游戏模式后，短按会使用当前选择的睡眠模式。系统在真正挂起后会短暂过滤唤醒键事件，避免同一个按键再次触发睡眠。

回到默认操作时，删除上面的配置文件，再执行最后两条命令。
