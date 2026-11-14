#!/bin/bash
# Installs the stale-ostree-karg rescue; see armada-ostree-fallback.sh.

check() {
    # 255 = explicit --add only, matching the other armada modules.
    return 255
}

depends() {
    echo systemd bash ostree
    return 0
}

install() {
    inst_multiple mkdir mount umount rm readlink

    inst_script "$moddir/armada-ostree-fallback.sh" \
        /usr/libexec/armada/armada-ostree-fallback
    inst_script "$moddir/armada-bound-deployment.sh" \
        /usr/libexec/armada/armada-bound-deployment

    # Inside prepare-root's start job: ordered after sysroot.mount by its own unit.
    mkdir -p "$initdir/$systemdsystemunitdir/ostree-prepare-root.service.d"
    printf '[Service]\nExecStartPre=/usr/libexec/armada/armada-ostree-fallback\nExecStartPost=/usr/libexec/armada/armada-ostree-fallback --restore\n' \
        > "$initdir/$systemdsystemunitdir/ostree-prepare-root.service.d/armada-fallback.conf"
}
