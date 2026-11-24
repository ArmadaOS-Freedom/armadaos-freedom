#!/bin/bash
# Auto-return from a one-shot diagnostic boot.  Runs in pre-mount
# before pivot; when the handoff requested auto-return (TB322FC one-shot
# diagnostic converted to plain Linux), reboot into normal boot instead of
# dropping to an interactive shell.

if [ -e /proc/device-tree/chosen/armada,diagnostic-auto-return ]; then
    systemctl --no-block --force reboot
fi
