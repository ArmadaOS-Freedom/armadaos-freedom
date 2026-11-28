#!/usr/bin/env python3
"""TB710FU 磁吸键盘 Linux 守护进程。

- 打开 pogo UART (/dev/ttyHS2, 921600 8N1)，tx-en 拉低（否则收不到数据）
- 解 PPP 帧 7E 7E FF seed enc... chk 7E → 去转义 → 校验 → 解扰
- 0x11: HID 键盘报告 → /dev/uinput 键盘设备
- 0x13: 触摸板绝对坐标（屏幕像素空间）→ REL_X/REL_Y 增量；pkt[2] = 物理按键状态
  （1 = 按下，可做真正的按下拖动）；多指帧 = 滚动/右键
- 0x16: 状态帧，忽略
"""
import os
import select
import subprocess
import queue
import struct
import sys
import threading
import time

TTY = "/dev/ttyHS2"
TXEN = "/sys/class/pogo_power/power/txen"
DEBUG_KBD = os.path.exists("/run/tbkb-debug")

# The Lenovo shortcut column sends its keys as HID consumer-page reports
# (packet type 0x12), not as keyboard usages.  Captured on hardware:
#   0x0E2 mute   0x0EA vol-   0x0E9 vol+   0x06F brightness+  0x070 brightness-
#   0x391 mic mute   0x394 zoom   0x395 split   0x398 favourite
CONSUMER = {
    0x0E2: 113,   # KEY_MUTE
    0x0EA: 114,   # KEY_VOLUMEDOWN
    0x0E9: 115,   # KEY_VOLUMEUP
    0x06F: 225,   # KEY_BRIGHTNESSUP
    0x070: 224,   # KEY_BRIGHTNESSDOWN
    0x391: 248,   # KEY_MICMUTE
}

# Keys with no useful evdev code get synthesised as shortcuts instead.
KEY_CTRL, KEY_LEFTMETA, KEY_EQUAL, KEY_D, KEY_LEFT = 29, 125, 13, 32, 105
CONSUMER_COMBO = {
    0x394: ([KEY_CTRL], KEY_EQUAL),           # 放大页面  -> Ctrl + =
    0x395: ([KEY_LEFTMETA], KEY_LEFT),        # 分屏      -> Meta + Left (quick tile)
    0x398: ([KEY_CTRL], KEY_D),               # 收藏      -> Ctrl + D
}

# --------------------------------------------------------------------------
# Actions that need more than a keystroke: brightness, volume, mute, screenshot
# The daemon runs as root, so it can poke sysfs; for PipeWire / plasmashell it
# uses the freshly logged-in user's session bus.
USER = "armada"
USER_UID = 1000
USER_ENV = dict(os.environ,
                XDG_RUNTIME_DIR=f"/run/user/{USER_UID}",
                DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{USER_UID}/bus")
BACKLIGHT = "/sys/class/backlight/sy7758-backlight"
SCREENSHOT = "/var/lib/pogo/tb-screenshot.py"
BEEP = "/var/lib/pogo/beep.wav"


def run_async(cmd):
    """Fire and forget, in the user session.

    The daemon runs as root, but the session bus only accepts the owning uid,
    so everything user-facing has to be re-executed as the logged-in user.
    """
    cmd = ["runuser", "-u", USER, "--"] + list(cmd)
    try:
        subprocess.Popen(cmd, env=USER_ENV, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as exc:
        log(f"action failed {cmd[0]}: {exc}")


def osd(method, *args):
    cmd = ["busctl", "--user", "call", "org.kde.plasmashell",
           "/org/kde/osdService", "org.kde.osdService", method]
    for a in args:
        cmd.append(f"i{a}" if isinstance(a, int) else f"s{a}")
    run_async(cmd)


def wpctl(*args):
    try:
        res = subprocess.run(["runuser", "-u", USER, "--", "wpctl", *args],
                             env=USER_ENV, timeout=3,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        if DEBUG_KBD:
            log(f"wpctl {' '.join(args)} rc={res.returncode} "
                f"out={res.stdout.strip()} err={res.stderr.strip()[:120]}")
        return res.stdout
    except Exception:
        return ""


def sink_percent():
    """Return (percent, muted) for the default sink."""
    out = wpctl("get-volume", "@DEFAULT_AUDIO_SINK@")
    muted = "MUTED" in out
    val = 0.0
    for tok in out.split():
        try:
            val = float(tok)
            break
        except ValueError:
            continue
    return int(round(val * 100)), muted


def brightness_step(direction):
    try:
        cur = int(open(f"{BACKLIGHT}/brightness").read().strip())
        mx = int(open(f"{BACKLIGHT}/max_brightness").read().strip())
    except Exception:
        return
    step = max(1, mx // 20)
    new = max(1, min(mx, cur + direction * step))
    if new == cur:
        return
    try:
        with open(f"{BACKLIGHT}/brightness", "w") as fh:
            fh.write(str(new))
    except Exception as exc:
        log(f"brightness write failed: {exc}")
        return
    osd("brightnessChanged", int(round(new * 100 / mx)))


def inject(code):
    """把按键注入 uinput，让 gamescope/Plasma 自己处理（这样才有原生 OSD）。"""
    dev = KBD_DEV[0]
    if dev is None:
        return
    try:
        dev.emit(EV_KEY, code, 1)
        dev.syn()
        time.sleep(0.02)
        dev.emit(EV_KEY, code, 0)
        dev.syn()
    except Exception as exc:
        log(f"inject {code} failed: {exc}")


def sync_mute_led():
    time.sleep(0.10)          # 等合成器把静音状态落下去
    pct, muted = sink_percent()
    LED_BITS["mute"] = 1 if muted else 0
    if KBD_FD[0] is not None:
        send_led_bitmap(KBD_FD[0], force=True)


def consumer_action(usage):
    if usage == 0x0E2:
        # 注入 KEY_MUTE 合成器不认（实测音量键认、静音键不认），只能自己调 wpctl。
        # 先乐观翻灯（立刻可见），再真正切静音，最后按真实状态纠错。
        LED_BITS["mute"] = 0 if LED_BITS["mute"] else 1
        if KBD_FD[0] is not None:
            send_led_bitmap(KBD_FD[0], force=True)
        try:
            wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "toggle")
        except Exception as exc:
            log(f"mute toggle failed: {exc}")
        beep()
        sync_mute_led()
        # 键盘自己会在按下/松开时闪一下灯，把我们的状态冲掉；
        # 之后 0.15/0.35/0.60 s 各重发一次位图，保证松手后灯仍是我们想要的状态。
        def _reassert():
            for delay in (0.15, 0.35, 0.60):
                time.sleep(delay if delay == 0.15 else 0.20)
                fd = KBD_FD[0]
                if fd is not None:
                    send_led_bitmap(fd, force=True)
        threading.Thread(target=_reassert, daemon=True).start()
    elif usage == 0x0EA:
        inject(KEY_VOLUMEDOWN)
    elif usage == 0x0E9:
        inject(KEY_VOLUMEUP)
    elif usage == 0x06F:
        brightness_step(+1)
    elif usage == 0x070:
        brightness_step(-1)
    elif usage == 0x391:
        toggle_mic()
    elif usage == 0x395:
        tile_window()
    else:
        combo = CONSUMER_COMBO.get(usage)
        if combo:
            slow_combo(*combo)


def ensure_audible():
    """WirePlumber on this image comes up with sink volume 0, so a mute toggle
    is inaudible until the user raises the volume.  Lift a silent sink once."""
    pct, muted = sink_percent()
    if pct < 5:
        wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", "50%")
        if muted:
            wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")
        return True
    return False


def beep():
    """游戏模式没有 OSD，用一声短促提示音当"静音状态"的反馈。"""
    try:
        subprocess.run(["runuser", "-u", USER, "--", "paplay", BEEP], env=USER_ENV,
                       timeout=1.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def toggle_mute():
    pct, muted = sink_percent()
    if not muted:
        beep()               # 静音之后放不出来，所以先响再静音
        wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "1")
    else:
        wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")
        if pct < 5:
            wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", "50%")
        beep()
    pct, muted = sink_percent()
    LED_BITS["mute"] = 1 if muted else 0
    if KBD_FD[0] is not None:
        send_led_bitmap(KBD_FD[0])
    osd("volumeChanged", pct)
    osd("showText", "Speaker", "Muted" if muted else "Unmuted")


def volume_step(delta):
    # wpctl syntax is "5%+" / "5%-" (magnitude first, sign of the operation last)
    op = f"{abs(delta)}%+" if delta > 0 else f"{abs(delta)}%-"
    wpctl("set-volume", "@DEFAULT_AUDIO_SINK@", op)
    pct, muted = sink_percent()
    if muted:
        wpctl("set-mute", "@DEFAULT_AUDIO_SINK@", "0")
    osd("volumeChanged", pct)


def toggle_mic():
    # 这台机器目前没有任何采集设备（arecord -l 为空），wpctl 切不了麦克风，
    # 所以这里做"本地开关"：灯的状态由我们自己维护，等以后有 capture 设备再挂上去。
    LED_BITS["mic"] = 0 if LED_BITS["mic"] else 1
    if KBD_FD[0] is not None:
        send_led_bitmap(KBD_FD[0], force=True)

    def _reassert():
        for _ in range(3):
            time.sleep(0.22)
            fd = KBD_FD[0]
            if fd is not None:
                send_led_bitmap(fd, force=True)
    threading.Thread(target=_reassert, daemon=True).start()
    beep()


def screenshot():
    run_async(["python3", SCREENSHOT])

# 分屏：不合成快捷键（KWin 在移动会话里会把它还原），而是让 KWin 脚本
# 直接设置活动窗口几何。三态循环：左半 -> 右半 -> 还原。
TILE_STATE = "/var/lib/pogo/tile.state"
TILE_JS = "/var/lib/pogo/tb-tile.js"
TILE_TEMPLATE = """(function () {
    const w = workspace.activeWindow;
    if (!w) { return; }
    const g = workspace.clientArea(KWin.MaximizeArea, w);
    const mode = %d;
    if (mode === 0) {
        w.frameGeometry = {x: g.x, y: g.y, width: Math.round(g.width / 2), height: g.height};
    } else if (mode === 1) {
        w.frameGeometry = {x: g.x + Math.round(g.width / 2), y: g.y, width: Math.round(g.width / 2), height: g.height};
    } else {
        w.frameGeometry = {x: g.x, y: g.y, width: g.width, height: g.height};
    }
})();
"""


def tile_window():
    """Cycle the active window: left half -> right half -> restored."""
    try:
        state = int(open(TILE_STATE).read().strip() or "0")
    except Exception:
        state = 0
    mode = state % 3
    try:
        with open(TILE_JS, "w") as fh:
            fh.write(TILE_TEMPLATE % mode)
        os.chmod(TILE_JS, 0o644)
        os.chown(TILE_JS, USER_UID, USER_UID)
    except Exception as exc:
        log(f"tile: cannot write script: {exc}")
        return
    try:
        out = subprocess.run(
            ["runuser", "-u", USER, "--", "sh", "-c",
             'export XDG_RUNTIME_DIR=/run/user/1000 '
             'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus; '
             'id=$(busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting '
             'loadScript s /var/lib/pogo/tb-tile.js | awk "{print \\$2}"); '
             'busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting start >/dev/null; '
             'sleep 0.2; '
             'busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting '
             'unloadScript s "$id" >/dev/null 2>&1; echo "$id"'],
            env=USER_ENV, timeout=6, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        log(f"tile mode={mode} id={out.stdout.strip()} err={out.stderr.strip()[:100]}")
    except Exception as exc:
        log(f"tile failed: {exc}")
        return
    try:
        with open(TILE_STATE, "w") as fh:
            fh.write(str((mode + 1) % 3))
    except Exception:
        pass


SESSION_CONF = "/etc/sddm.conf.d/zz-tb-session.conf"
TB_MODE = "/var/lib/tb-session/tb-mode"


def session_toggle():
    """在 Steam 大屏和作者的 Plasma 桌面之间切换。

    Steam 自带的“切换到桌面”走的是 steamos-manager 的 D-Bus，在 ArmadaOS 上
    是个空操作（点了会先黑一下又回到 Steam），所以这里给键盘绑一个真按钮。
    """
    try:
        cur = open(SESSION_CONF, encoding="utf-8", errors="replace").read()
    except OSError:
        cur = ""
    target = "desktop" if "gamescope" in cur else "game"
    log(f"session toggle -> {target}")
    # 真正的切换交给 systemd（tb-session-toggle.path -> toggle.sh，以 root 跑）：
    # 守护进程只要写一个触发文件，避免在这里踩权限/环境问题。
    try:
        with open("/run/tb-session-toggle", "w") as fh:
            fh.write(target + "\n")
    except OSError as exc:
        log(f"session toggle failed: {exc}")


def slow_combo(mods, tap):
    """Emit a shortcut with human-ish spacing so kwin registers it reliably.

    注意：合成用的修饰键必须全部松开，但**用户物理上还按着的**修饰键要重新按下，
    否则"按住 Ctrl 再按放大键"会把 Ctrl 卡在松开状态。
    """
    for m in mods:
        kbd.key(m, True)
        time.sleep(0.02)
    kbd.key(tap, True)
    time.sleep(0.03)
    kbd.key(tap, False)
    for m in reversed(mods):
        time.sleep(0.02)
        kbd.key(m, False)
    for m in mods:
        if PHYS_MODS[0] & MOD_BITS.get(m, 0):
            time.sleep(0.01)
            kbd.key(m, True)

ENABLE = "/sys/class/pogo_power/power/enable"

KEYDICT = bytes.fromhex(
    "b802d957c648c213d7a630bf40d307ce"
    "c3abfa516218bd847dcae0cff2acbc12"
    "f514d072dc1a50000032d17b33b610dd"
    "6a21eb7e525b3d9f85cb1f667396a160"
)

# HID usage -> Linux keycode
KEYMAP = {
    0x04: 30, 0x05: 48, 0x06: 46, 0x07: 32, 0x08: 18, 0x09: 33, 0x0A: 34,
    0x0B: 35, 0x0C: 23, 0x0D: 36, 0x0E: 37, 0x0F: 38, 0x10: 50, 0x11: 49,
    0x12: 24, 0x13: 25, 0x14: 16, 0x15: 19, 0x16: 31, 0x17: 20, 0x18: 22,
    0x19: 47, 0x1A: 17, 0x1B: 45, 0x1C: 21, 0x1D: 44,
    0x1E: 2, 0x1F: 3, 0x20: 4, 0x21: 5, 0x22: 6, 0x23: 7, 0x24: 8,
    0x25: 9, 0x26: 10, 0x27: 11,
    0x28: 28, 0x29: 1, 0x2A: 14, 0x2B: 15, 0x2C: 57,
    0x2D: 12, 0x2E: 13, 0x2F: 26, 0x30: 27, 0x31: 43,
    0x33: 39, 0x34: 40, 0x35: 41, 0x36: 51, 0x37: 52, 0x38: 53,
    0x39: 58, 0x3A: 59, 0x3B: 60, 0x3C: 61, 0x3D: 62, 0x3E: 63, 0x3F: 64,
    0x40: 65, 0x41: 66, 0x42: 67, 0x43: 68, 0x44: 87, 0x45: 88,
    0x65: 127, 0x66: 116, 0x67: 13,
    0x6F: 113, 0x70: 115, 0x71: 114,   # mute / vol+ / vol-
    0x46: 99, 0x47: 70, 0x48: 119, 0x49: 110, 0x4A: 102, 0x4B: 104,
    0x4C: 111, 0x4D: 107, 0x4E: 109, 0x4F: 106, 0x50: 105, 0x51: 108,
    0x52: 103, 0x53: 69,
}
MODMAP = {
    0x01: 29, 0x02: 42, 0x04: 56, 0x08: 125,
    0x10: 97, 0x20: 54, 0x40: 100, 0x80: 126,
}

DEBUG = os.environ.get("TBKB_DEBUG") == "1"
# 单击按住时长：太长会被 Qt 当成 long-press，太短个别应用会吞掉
HOLD = float(os.environ.get("TBKB_HOLD", "0.06"))
# 轻点判定：一次接触的帧数与累计位移上限（触摸板不发按键/压力事件，轻点只能按时间+位移合成）
TAP_FRAMES = int(os.environ.get("TBKB_TAP_FRAMES", "26"))   # 26 帧 ≈ 0.48 s @54fps
TAP_MOVE = int(os.environ.get("TBKB_TAP_MOVE", "40"))       # 屏幕像素，|dx|+|dy| 累计

EV_SYN, EV_KEY, EV_REL, EV_LED = 0, 1, 2, 17
LED_CAPSL = 1
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL = 0, 1, 6, 8
BTN_LEFT, BTN_RIGHT = 0x110, 0x111
# 双指滚动：每累计多少屏幕像素发一格滚轮
SCROLL_STEP = int(os.environ.get("TBKB_SCROLL_STEP", "90"))
# 单帧最多发几格，防止快速滑动瞬间刷爆滚轮队列（会拖累合成器）
SCROLL_MAX = int(os.environ.get("TBKB_SCROLL_MAX", "2"))
# 双指轻点（右键）判定：帧数与累计位移上限，以及“两指必须差不多同时落下”的容差
TWO_TAP_FRAMES = int(os.environ.get("TBKB_TWO_TAP_FRAMES", "30"))
# 指针平滑：固件按 ~63 fps 上报、每帧 delta 可达上百像素，且偶发跳变（实测最大 1148 px）。
# 把增量放进队列，按固定节奏分小步发出（低通），指针就不会一格一格地跳。
TICK = float(os.environ.get("TBKB_TICK", "0.006"))
SMOOTH = float(os.environ.get("TBKB_SMOOTH", "0.55"))
# 单帧增量上限：手指最快也就 60 px/帧（~4000 px/s），超过 300 一定是"跳变"
# （新接触点、双指↔单指切换），直接重新校准，不能当成移动，否则光标会飞到别处
MAX_DELTA = int(os.environ.get("TBKB_MAX_DELTA", "300"))

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_LEDBIT = 0x40045569   # _IOW('U', 105, int)；105=0x69（106 是 SNDBIT，103 是 ABSBIT）
UI_DEV_SETUP = 0x405C5503


def log(msg):
    print(f"tbkb: {msg}", flush=True)


class UInput:
    def __init__(self, name, keys=(), rels=(), leds=()):
        # 必须 O_RDWR：EV_LED 等"输出事件"要从这个 fd 读，O_WRONLY 读不了（led 只会亮不会灭）
        self.fd = os.open("/dev/uinput", os.O_RDWR | os.O_NONBLOCK)
        for ev in ([EV_KEY] if keys else []) + ([EV_REL] if rels else []) + ([EV_LED] if leds else []):
            self._ioctl(UI_SET_EVBIT, ev)
        for k in keys:
            self._ioctl(UI_SET_KEYBIT, k)
        for r in rels:
            self._ioctl(UI_SET_RELBIT, r)
        for l in leds:
            self._ioctl(UI_SET_LEDBIT, l)
        ident = struct.pack("HHHH", 0x06, 0x17EF, 0x0001, 1)
        setup = ident + name.encode()[:79].ljust(80, b"\0") + struct.pack("I", 0)
        self._ioctl(UI_DEV_SETUP, setup)
        self._ioctl(UI_DEV_CREATE, 0)
        self.pressed = set()

    def _ioctl(self, req, arg):
        if isinstance(arg, (bytes, bytearray)):
            import fcntl
            fcntl.ioctl(self.fd, req, bytearray(arg))
        else:
            import fcntl
            fcntl.ioctl(self.fd, req, arg)

    def emit(self, etype, code, value):
        os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def syn(self):
        self.emit(EV_SYN, 0, 0)

    def key(self, code, down):
        if down:
            self.pressed.add(code)
        else:
            self.pressed.discard(code)
        self.emit(EV_KEY, code, 1 if down else 0)
        self.syn()

    def rel(self, dx, dy):
        if dx:
            self.emit(EV_REL, REL_X, dx)
        if dy:
            self.emit(EV_REL, REL_Y, dy)
        if dx or dy:
            self.syn()

    def button(self, down, code=BTN_LEFT):
        self.emit(EV_KEY, code, 1 if down else 0)
        self.syn()

    def click(self, code=BTN_LEFT):
        self.button(True, code)
        time.sleep(0.02)
        self.button(False, code)

    def wheel(self, notches):
        if not notches:
            return
        self.emit(EV_REL, REL_WHEEL, notches)
        self.syn()


def unescape(buf):
    out = bytearray()
    i = 0
    while i < len(buf):
        if buf[i] == 0x7D and i + 1 < len(buf):
            out.append(buf[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(buf[i])
            i += 1
    return bytes(out)


def parse_frame(frame):
    # On the wire:  7E 7E FF | seed | esc(enc) | esc(chk) | 7E
    #
    # Two separate escapes bit us before:
    #   * the checksum is computed over the *escaped* payload bytes, not the
    #     unescaped ones, so frames containing 7E/7D used to fail verification
    #     (~6% of all frames -- lost key releases look like "stuck" keys);
    #   * chk itself is escaped too, so a frame can legitimately end in 7D 5D.
    # Try both readings of the tail and accept whichever verifies.
    if len(frame) < 6:
        return None
    raw = frame[2:-1]
    if not raw or raw[0] != 0xFF:
        return None
    p = raw[1:]
    if len(p) < 3:
        return None
    seed, rest = p[0], p[1:]
    cands = [(rest[-1], rest[:-1])]
    if len(rest) >= 3 and rest[-2] == 0x7D:
        cands.append((rest[-1] ^ 0x20, rest[:-2]))
    for chk, enc_esc in cands:
        if ((seed + sum(enc_esc)) & 0xFF) != chk:
            continue
        enc = unescape(enc_esc)
        return bytes(c ^ KEYDICT[(seed + i) & 0x3F] for i, c in enumerate(enc))
    return None


def escape(buf):
    out = bytearray()
    for b in buf:
        if b == 0x7E:
            out += b"\x7d\x5e"
        elif b == 0x7D:
            out += b"\x7d\x5d"
        else:
            out.append(b)
    return bytes(out)


def build_frame(plain, seed=0):
    enc = bytes(c ^ KEYDICT[(seed + i) & 0x3F] for i, c in enumerate(plain))
    chk = (seed + sum(enc)) & 0xFF
    return b"\x7e\x7e\xff" + bytes([seed]) + escape(enc + bytes([chk])) + b"\x7e"


LED_SENT = [0]  # 我们最后一次真正发出去的 LED 状态（唯一真相）


led_retry = [0.0, 0]  # [last_retry_time, retries_done]
LED_LAST = [None, 0.0]  # [最后写入的值, 时间] —— 限速用
class LinkLost(Exception):
    """tx-en 无法恢复成"收"，这条链路已经不可信，必须重开串口。"""


LINK_LOCK = threading.RLock()   # 串口方向切换是互斥资源：主线程点灯 / 动作线程点灯 / 回声
PHYS_MODS = [0]                 # 当前物理按住的 HID 修饰键位图（主循环维护）
MOD_BITS = {29: 0x01, 42: 0x02, 56: 0x04, 125: 0x08,
            97: 0x10, 54: 0x20, 100: 0x40, 126: 0x80}

ACTION_Q = queue.Queue(maxsize=64)


def dispatch(usage):
    """把功能键动作排进队列：动作里可能跑 wpctl/busctl（秒级超时），
    绝不能在读串口的线程里同步执行，否则按键/触摸板帧会被拖住。"""
    try:
        ACTION_Q.put_nowait(usage)
    except queue.Full:
        log(f"action queue full, dropping 0x{usage:03x}")


def action_worker():
    while True:
        usage = ACTION_Q.get()
        try:
            consumer_action(usage)
        except Exception as exc:
            log(f"action 0x{usage:03x} failed: {exc!r}")


def set_txen(on):
    """切 UART 方向，失败重试；仍然失败就抛 LinkLost。"""
    want = 1 if on else 0
    for _ in range(3):
        if write_attr(TXEN, want):
            return
        time.sleep(0.02)
    raise LinkLost(f"cannot set txen={want}")


def release_all(last_keys, last_mods):
    """把虚拟键盘上还按着的键全部松开（拔键盘/串口重开时必须做）。"""
    for code in list(last_keys):
        try:
            kbd.key(code, False)
        except Exception:
            pass
    for code, bit in MOD_BITS.items():
        if last_mods & bit:
            try:
                kbd.key(code, False)
            except Exception:
                pass


REPEATABLE = {0x0EA, 0x0E9, 0x06F, 0x070}  # 长按要连发的功能键（音量/亮度）
held = {}  # usage -> [按下时刻, 上次连发时刻]
caps_key_t = [0.0]  # last physical CapsLock press

# 三灯位图：反汇编 lenovo_kb_restore_leds 得到
#   载荷 6A 06 2B 04 <caps> 00 <mute> <mic>
# caps=bit2(0x4)、mute=bit3(0x8)、mic=bit4(0x10)，真机已确认 bit3=mute、bit4=mic。
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP, KEY_MICMUTE = 113, 114, 115, 248
LED_BITS = {"caps": 0, "mute": 0, "mic": 0}
KBD_FD = [None]  # 主循环打开的 tty fd，供动作函数回写 LED
KBD_DEV = [None]  # uinput 键盘设备，音量/静音键走注入让合成器出 OSD
led_rate = [0.0]  # 上一次真正发位图的时刻


def send_led_bitmap(fd, tries=2, force=False):
    """把三灯位图发给键盘（幂等，可重复发）。"""
    now = time.time()
    if not force and now - led_rate[0] < 0.12:
        return
    led_rate[0] = now
    payload = bytes([0x6A, 0x06, 0x2B, 0x04,
                     LED_BITS["caps"], 0x00, LED_BITS["mute"], LED_BITS["mic"]])
    with LINK_LOCK:
        try:
            for i in range(tries):
                frame = build_frame(payload, seed=(int(time.time() * 1000) + i * 37) & 0xFF)
                set_txen(1)
                try:
                    time.sleep(0.004)
                    os.write(fd, frame)
                    time.sleep(0.008)
                finally:
                    # 无论写成功与否，都必须把方向切回"收"，否则键盘/触摸板的帧全丢
                    set_txen(0)
                if i + 1 < tries:
                    time.sleep(0.030)
            log(f"leds caps={LED_BITS['caps']} mute={LED_BITS['mute']} mic={LED_BITS['mic']}")
        except (OSError, LinkLost) as exc:
            log(f"led bitmap write failed: {exc!r}")


def send_caps_led(fd, on, tries=1):
    # 三灯位图里的 caps 位（bit2）。旧单灯命令 6A 03 27 01 也能用，但统一走位图，
    # 免得和静音/麦克风灯互相覆盖。
    val = 1 if on else 0
    now = time.time()
    if val == LED_BITS["caps"] and now - led_rate[0] < 0.5:
        return
    LED_BITS["caps"] = val
    LED_SENT[0] = val
    send_led_bitmap(fd, tries=max(1, tries), force=True)
    led_retry[0] = time.time()
    led_retry[1] = 0
    log(f"caps led -> {val} t={time.time():.3f} (x{tries})")


def write_attr(path, value):
    """写 sysfs 属性。返回 True/False —— 方向控制(txen)出错必须让上层知道。"""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError as e:
        log(f"write {path}={value} failed: {e}")
        return False


def toggle_session_mode():
    """Ctrl+Alt+D：Steam 游戏模式 <-> KDE 桌面（Steam 自带菜单里没有这一项）。"""
    try:
        # plasmashell 在跑 = 当前是桌面会话；否则当成游戏模式
        desktop = subprocess.run(["pgrep", "-x", "plasmashell"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).returncode == 0
        mode = "switch-to-game-mode" if desktop else "switch-to-desktop-mode"
        # 必须以 steamos 用户 + 用户 bus 执行：root 直接调会在连接用户 session 时
        # 报 "I/O error: Broken pipe (os error 32)" 而不生效
        logfd = open("/tmp/tbkb-session.log", "ab", buffering=0)
        subprocess.Popen(["sudo", "-u", "armada", "env",
                          "XDG_RUNTIME_DIR=/run/user/1000",
                          "tb-mode", mode],
                         stdout=logfd, stderr=logfd,
                         start_new_session=True)
        log("session toggle -> %s" % mode)
    except Exception as exc:
        log("session toggle failed: %s" % exc)


def wait_attr(path, value, timeout=120.0):
    """等节点出现并**确认写入生效**（读回校验）。

    开机早期 pogo_power 可能还没 probe 完（regulator provider 未就绪 → EPROBE_DEFER），
    只"文件存在就写一次"是不够的：写失败会被吞掉，上层拿着 True 去开串口，
    结果是没电或 TX 一直占着方向。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            write_attr(path, value)
            try:
                with open(path) as f:
                    if f.read().strip() == str(value):
                        return True
            except OSError:
                pass
        time.sleep(0.2)
    log(f"timeout/failed waiting for {path}={value}")
    return False


# CapsLock 本地状态（按 key 事件翻转，不依赖 compositor 的 EV_LED）
global_caps = [0]
caps_synced = [False]


def open_tty():
    """Open the pogo UART at 921600 8N1.

    Called again whenever the link goes silent: the keyboard is magnetic, so it
    gets detached and re-attached, and the old fd simply stops producing bytes.
    """
    t0 = time.time()
    while not os.path.exists(TTY) and time.time() - t0 < 60:
        time.sleep(0.2)
    fd = os.open(TTY, os.O_RDWR | os.O_NOCTTY)   # 要回写 LED 帧，必须可写
    import termios
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag = 0
    oflag = 0
    lflag = 0
    cflag = (cflag & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
    cc = list(cc)
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag,
                                            termios.B921600, termios.B921600, cc])
    log("tty open @921600")
    return fd


def main():
    extra = set(CONSUMER.values())
    for mods, code in CONSUMER_COMBO.values():
        extra.update(mods)
        extra.add(code)
    global kbd
    kbd = UInput("TB710FU Keyboard",
                 keys=sorted(set(KEYMAP.values()) | set(MODMAP.values()) | extra),
                 leds=[LED_CAPSL])
    pad = UInput("TB710FU Touchpad", keys=[BTN_LEFT, BTN_RIGHT],
                 rels=[REL_X, REL_Y, REL_WHEEL, REL_HWHEEL])
    KBD_DEV[0] = kbd
    log("uinput devices created")
    threading.Thread(target=action_worker, daemon=True).start()

    wait_attr(ENABLE, 1, 180)
    wait_attr(TXEN, 0, 10)
    log("pogo power on, tx-en low")

    fd = open_tty()
    KBD_FD[0] = fd

    if ensure_audible():
        log("sink was silent, raised to 50%")

    buf = bytearray()
    last_keys = set()
    last_mods = 0
    started = False
    prev_x = prev_y = None
    prev_seq = 7
    STATS = {"frames": 0, "crc": 0, "by_type": {}, "seq": {}, "watchdog": 0}
    stats_at = time.time()
    in_click = False          # 当前 BTN_LEFT 处于按下状态
    cluster = False           # 正处于「压力接触帧簇」内
    click_release_at = 0.0
    tap_frames = 0            # 本次接触收到的触摸板帧数
    tap_moved = 0             # 本次接触累计位移（屏幕像素，|dx|+|dy|）
    pend_x = 0.0              # 待发指针增量（平滑队列）
    pend_y = 0.0
    last_tick = 0.0
    two_prev = None           # 双指上一帧平均坐标 (avg_x, avg_y)
    two_rest = 0.0            # 双指滚动累计余数（像素）
    two_frames = 0            # 双指接触帧数
    two_moved = 0             # 双指接触累计位移
    last_status = 0
    pad_btn = 0               # 触摸板物理按键状态（pkt[2]）
    pad_press_click = False   # 当前 BTN_LEFT 是按物理按键按下的（不是合成轻点）
    contact_dx = 0            # 本次接触累计加到平滑队列的位移（用于轻点后补偿）
    contact_dy = 0
    cur = [0, 0]              # 我们自己累计的指针位置（调试轻点漂移用）
    last_touch_t = 0.0        # 上一帧触摸板数据的时间（判断"新接触"）
    drag_prev = None          # 拖动模式（物理按下 / 三指）的上一帧平均坐标
    drag_3f = False           # 当前拖动是"三指拖动"合成的按下
    three_frames = 0          # 连续三指帧数（要连续 3 帧才认为真的想三指拖动）
    suppress_tap = False      # 本次接触里已经做过多指手势，禁止再合成轻点
    consumer_seen = set()     # 本轮按下的 consumer 键（0x12 包，放开时清空）

    last_any = time.time()      # last time any valid frame arrived

    while True:
        # Magnetic keyboard: detach/re-attach leaves the old fd open but silent.
        if time.time() - last_any > 8.0:
            log("tty silent for 8s, reopening")
            # 先把虚拟键盘上还按着的键松开（否则 Shift/Ctrl 会"卡住"）
            release_all(last_keys, last_mods)
            try:
                os.close(fd)
            except OSError:
                pass
            if not wait_attr(ENABLE, 1, 10):
                log("warning: pogo power not confirmed before reopen")
            if not wait_attr(TXEN, 0, 5):
                log("warning: txen not confirmed before reopen")
            fd = open_tty()
            last_any = time.time()
            last_keys = set()
            last_mods = 0
            PHYS_MODS[0] = 0
        r, _, _ = select.select([fd, kbd.fd], [], [], TICK)
        if kbd.fd in r:
            try:
                ev = os.read(kbd.fd, 24 * 16)
            except OSError:
                ev = b""
            for off in range(0, len(ev) - 23, 24):
                etype, code, value = struct.unpack_from("llHHi", ev, off)[2:]
                if etype == EV_LED and code == LED_CAPSL:
                    want = int(value)
                    global_caps[0] = want
                    log(f"compositor EV_LED={want} t={time.time():.3f} (LED_SENT={LED_SENT[0]})")
                    if want != LED_SENT[0]:
                        send_caps_led(fd, want, tries=2)
        now = time.time()
        if (pend_x or pend_y) and now - last_tick >= TICK:
            last_tick = now
            sx = pend_x * SMOOTH
            sy = pend_y * SMOOTH
            ix = int(sx) if abs(sx) >= 1 else (1 if sx > 0 else (-1 if sx < 0 else 0))
            iy = int(sy) if abs(sy) >= 1 else (1 if sy > 0 else (-1 if sy < 0 else 0))
            if abs(ix) > abs(pend_x):
                ix = pend_x
            if abs(iy) > abs(pend_y):
                iy = pend_y
            pend_x -= ix
            pend_y -= iy
            if abs(pend_x) < 0.5:
                pend_x = 0.0
            if abs(pend_y) < 0.5:
                pend_y = 0.0
            pad.rel(int(ix), int(iy))
            cur[0] += int(ix)
            cur[1] += int(iy)
        if in_click and time.time() >= click_release_at:
            pad.button(False)
            in_click = False
            if DEBUG:
                log("click up (hold elapsed)")
        # ---- 每 15 s 打一行统计：能区分"串口没数据"和"我们丢帧" ----
        # 长按连发：键盘对功能键只发一次按压（release 前不重复），
        # 所以自己起节奏，按住 0.45s 后每 0.12s 触发一次。
        for usage, ts in list(held.items()):
            if now - ts[0] > 0.45 and now - ts[1] > 0.12:
                dispatch(usage)
                ts[1] = now

        if now - stats_at >= 15:
            stats_at = now
            top = sorted(STATS["by_type"].items())
            log(f"stats frames={STATS['frames']} crc={STATS['crc']} "
                f"types={top} seq={sorted(STATS['seq'].items())} "
                f"wd={STATS['watchdog']} age={(now - last_touch_t):.1f}s")
            STATS["frames"] = STATS["crc"] = 0
            STATS["by_type"] = {}
            STATS["seq"] = {}

        # ---- 卡死看门狗：接触中 1.5 s 收不到新帧，强制收尾，避免"指针不动/卡住" ----
        if cluster and (now - last_touch_t) > 1.5:
            STATS["watchdog"] += 1
            log(f"watchdog: contact stalled {(now - last_touch_t)*1000:.0f} ms, forcing release")
            if in_click:
                pad.button(False)
                in_click = False
            pad_press_click = False
            cluster = False
            prev_x = None
            prev_seq = None
            pend_x = pend_y = 0.0
            two_prev = None
            drag_prev = None
            drag_3f = False
            suppress_tap = True
            three_frames = 0

        if fd not in r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError as e:
            log(f"read error {e}, reopening")
            release_all(last_keys, last_mods)
            last_keys = set()
            last_mods = 0
            PHYS_MODS[0] = 0
            try:
                os.close(fd)
            except OSError:
                pass
            time.sleep(1)
            fd = open_tty()
            last_any = time.time()
            continue
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > 8192:
            del buf[:4096]

        while True:
            start = buf.find(b"\x7e\x7e\xff")
            if start < 0:
                if len(buf) > 4:
                    del buf[:-4]
                break
            end = buf.find(b"\x7e", start + 3)
            if end < 0:
                if start:
                    del buf[:start]
                break
            frame = bytes(buf[start:end + 1])
            pkt = parse_frame(frame)
            if not pkt:
                # 只前进 1 字节：这一帧里可能还套着下一个完整帧的前导，
                # 整段丢掉会把紧跟着的正常帧（比如松键报告）一起吃掉。
                STATS["crc"] += 1
                del buf[:start + 1]
                continue
            del buf[:end + 1]
            STATS["frames"] += 1
            last_any = time.time()
            STATS["by_type"][pkt[0]] = STATS["by_type"].get(pkt[0], 0) + 1
            if pkt[0] == 0x13 and len(pkt) >= 4:
                STATS["seq"][pkt[3]] = STATS["seq"].get(pkt[3], 0) + 1
            if DEBUG_KBD and pkt[0] != 0x16:
                log(f"frame {pkt.hex()}")
            if pkt[0] == 0x11 and len(pkt) >= 10:
                mods = pkt[2]
                keys = set()
                for usage in pkt[4:10]:
                    code = KEYMAP.get(usage)
                    if code:
                        keys.add(code)
                changed_mod = mods ^ last_mods
                for bit, code in MODMAP.items():
                    if changed_mod & bit:
                        kbd.key(code, bool(mods & bit))
                for code in last_keys - keys:
                    kbd.key(code, False)
                for code in keys - last_keys:
                    kbd.key(code, True)
                # 截屏键（HID 0x46）：KWin 的 ScreenShot2 直接抓图，比 KEY_SYSRQ
                # 可靠——镜像里没有 spectacle，PrintScreen 没人接。
                if 99 in (keys - last_keys):
                    screenshot()
                    keys.discard(99)
                # Ctrl+Alt+D：在 Steam 游戏模式和 KDE 桌面之间切换
                if (mods & 0x11) and (mods & 0x44) and 32 in (keys - last_keys):
                    toggle_session_mode()
                # CapsLock 自己维护一份灯状态：compositor 不一定给 uinput 设备发 EV_LED
                if 58 in (keys - last_keys):
                    # 只记时间，不再在这里点灯：键盘会重复发同样的 press 报告，
                    # 由它推导灯状态必然来回抵消。灯的状态完全以「合成器发来的
                    # EV_LED」+「键盘状态帧回报的硬件状态」为准。
                    caps_key_t[0] = time.time()
                    log(f"caps key t={time.time():.3f} (LED_SENT={LED_SENT[0]})")
                last_keys, last_mods = keys, mods
                PHYS_MODS[0] = mods
            elif pkt[0] == 0x12 and len(pkt) >= 4:
                usage = pkt[2] | (pkt[3] << 8)
                if usage == 0:
                    consumer_seen.clear()
                    held.clear()
                elif usage not in consumer_seen:
                    consumer_seen.add(usage)
                    log(f"action usage=0x{usage:03x}")
                    dispatch(usage)
                    if usage in REPEATABLE:
                        held[usage] = [time.time(), time.time()]
            elif pkt[0] == 0x13 and len(pkt) >= 8:
                x = pkt[4] | (pkt[5] << 8)
                y = pkt[6] | (pkt[7] << 8)
                seq = pkt[3]
                btn = pkt[2] & 0x01
                # 触点通用解析：第 1 点占 4 字节，之后每个点前有 1 字节分隔（0x17/0x27...）
                pts = [(x, y)]
                idx = 8
                while idx + 4 < len(pkt):
                    idx += 1
                    pts.append((pkt[idx] | (pkt[idx + 1] << 8),
                                pkt[idx + 2] | (pkt[idx + 3] << 8)))
                    idx += 4

                # ---- 帧校验：pogo 接触不良时 UART 会偶发位错，坏帧的坐标是随机值。
                # 不丢掉它们就会被当成"小幅移动"，这正是之前光标自己漂移的原因。
                if seq not in (6, 7) or btn not in (0, 1):
                    if DEBUG:
                        log(f"drop bad header seq={seq} btn={btn} {pkt.hex()}")
                    continue
                if any(px > 3200 or py > 2000 for px, py in pts):
                    if DEBUG:
                        log(f"drop bad point {pts} {pkt.hex()}")
                    continue

                # ---- 新接触检测：手指抬起时固件不一定发 seq==6。
                # 中间隔了 >120 ms（正常 8 ms 一帧）说明是另一次接触，第一帧的坐标和上一段无关，
                # 直接算 delta 会让指针跳几十~几百像素（这就是"时不时漂移"的根源）。
                now_t = time.time()
                gap = now_t - last_touch_t
                last_touch_t = now_t
                cluster = True
                if gap > 0.12 and prev_x is not None:
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    tap_frames = 1
                    tap_moved = 0
                    contact_dx = contact_dy = 0
                    pend_x = pend_y = 0.0
                    two_prev = None
                    two_frames = 0
                    two_moved = 0
                    two_rest = 0.0
                    three_frames = 0
                    suppress_tap = False
                    if DEBUG:
                        log(f"new contact (gap {gap*1000:.0f} ms) at ({x},{y})")
                    continue

                # ---- 物理按键（clickpad）：pkt[2] 就是机械开关状态 ----
                if btn != pad_btn:
                    pad_btn = btn
                    drag_prev = None
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    contact_dx = contact_dy = 0
                    if btn:
                        # 按下：立刻按左键并保持，之后手指移动就是拖动窗口
                        if not in_click:
                            pad.button(True)
                            in_click = True
                        # 物理按键永远接管所有权：即使此时还压着一次合成轻点，
                        # 松手也必须由"物理松开"来释放，不能留下无限期按住的左键。
                        pad_press_click = True
                        click_release_at = float("inf")   # 由物理按键松开决定，不做定时释放
                        pend_x = pend_y = 0.0
                        if DEBUG:
                            log(f"pad press at ({x},{y})")
                    else:
                        if in_click and pad_press_click:
                            pad.button(False)
                            in_click = False
                            pad_press_click = False
                        contact_dx = contact_dy = 0
                        tap_frames = 1
                        tap_moved = 0
                        if DEBUG:
                            log("pad release")
                    continue

                # ---- 拖动模式 ----
                # ① 物理按键按下时：不管几根手指，都按平均坐标移动指针（= 一指按住、另一指拖）
                # ② 三指接触（没按物理键）：macOS 风格三指拖动，自己合成按下
                if pad_btn or len(pts) >= 3:
                    ax = sum(p[0] for p in pts) / float(len(pts))
                    ay = sum(p[1] for p in pts) / float(len(pts))
                    if pad_btn:
                        three_frames = 0
                    else:
                        three_frames += 1
                        # 连续 3 帧（~50 ms）都是三指才合成按下：手指掠过触控板不会误拖
                        if (not in_click and three_frames == 3):
                            pad.button(True)
                            in_click = True
                            pad_press_click = False
                            drag_3f = True
                            suppress_tap = True
                            click_release_at = float("inf")
                            if DEBUG:
                                log(f"three-finger drag start at ({ax:.0f},{ay:.0f})")
                    if drag_prev is None:
                        drag_prev = (ax, ay)
                    else:
                        ddx = ax - drag_prev[0]
                        ddy = ay - drag_prev[1]
                        drag_prev = (ax, ay)
                        if abs(ddx) <= MAX_DELTA and abs(ddy) <= MAX_DELTA:
                            pend_x += ddx
                            pend_y += ddy
                        else:
                            pend_x = pend_y = 0.0
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    continue

                if drag_prev is not None:
                    # 退出拖动：三指拖动要自己松开按键（物理按键由松手帧负责）
                    drag_prev = None
                    three_frames = 0
                    if drag_3f:
                        drag_3f = False
                        if in_click:
                            pend_x = pend_y = 0.0
                            pad.button(False)
                            in_click = False
                        if DEBUG:
                            log("three-finger drag end")

                if len(pts) >= 2:
                    # 双指（或多指）接触：滚动/右键，绝不移指针
                    x2, y2 = pts[1]
                    ax = sum(p[0] for p in pts) / float(len(pts))
                    ay = sum(p[1] for p in pts) / float(len(pts))
                    prev_x, prev_y = x, y
                    cluster = False
                    two_frames += 1
                    if two_prev is None:
                        two_prev = (ax, ay)
                        two_rest = 0.0
                        two_moved = 0
                        suppress_tap = True
                        if DEBUG:
                            log(f"two-finger start at ({ax:.0f},{ay:.0f})")
                        continue
                    ddx = ax - two_prev[0]
                    ddy = ay - two_prev[1]
                    two_prev = (ax, ay)
                    if abs(ddx) < 400 and abs(ddy) < 400:
                        two_moved += abs(ddx) + abs(ddy)
                        two_rest += ddy
                        notches = 0
                        while two_rest >= SCROLL_STEP and notches < SCROLL_MAX:
                            two_rest -= SCROLL_STEP
                            notches += 1
                        while two_rest <= -SCROLL_STEP and notches > -SCROLL_MAX:
                            two_rest += SCROLL_STEP
                            notches -= 1
                        if notches:
                            pad.wheel(notches)     # 手指向下 = 向下滚
                        if abs(two_rest) > SCROLL_STEP * 4:
                            two_rest = 0
                    continue
                if two_prev is not None:
                    # 多指接触结束：短、几乎没动 => 右键
                    if 0 < two_frames <= TWO_TAP_FRAMES and two_moved <= TAP_MOVE:
                        pad.click(BTN_RIGHT)
                        if DEBUG:
                            log(f"two-finger tap -> right click frames={two_frames}")
                    two_prev = None
                    two_frames = 0
                    two_moved = 0
                    two_rest = 0.0
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    tap_frames = 1
                    tap_moved = 0
                    pend_x = pend_y = 0.0
                    contact_dx = contact_dy = 0
                    continue
                if seq == 6:
                    # seq==6 = 本次接触的最后一帧。没有物理按键时，轻点只能自己合成。
                    if (prev_seq != 6 and not in_click and not cluster and not pad_btn
                            and not suppress_tap
                            and 0 < tap_frames <= TAP_FRAMES and tap_moved <= TAP_MOVE):
                        # 轻点时手指的抖动会累积成光标漂移：把这次接触已经移动的量补偿回去
                        comp_x = pend_x - contact_dx
                        comp_y = pend_y - contact_dy
                        pend_x = pend_y = 0.0
                        cx = max(-200, min(200, int(comp_x)))
                        cy = max(-200, min(200, int(comp_y)))
                        before = (cur[0], cur[1])
                        if cx or cy:
                            pad.rel(cx, cy)
                            cur[0] += cx
                            cur[1] += cy
                        if DEBUG:
                            log(f"pos before={before} after={(cur[0], cur[1])}")
                        pad.button(True)
                        in_click = True
                        click_release_at = time.time() + HOLD
                        if DEBUG:
                            log(f"tap click frames={tap_frames} moved={tap_moved} comp=({cx},{cy})")
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    tap_frames = 0
                    tap_moved = 0
                    contact_dx = contact_dy = 0
                    pend_x = pend_y = 0.0
                    continue
                if prev_seq == 6:
                    # 上一帧是"接触结束"标记：这一帧是新接触的起点，不做位移
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    tap_frames = 1
                    tap_moved = 0
                    contact_dx = contact_dy = 0
                    pend_x = pend_y = 0.0
                    if DEBUG:
                        log(f"resync contact at ({x},{y})")
                    continue
                if not started or prev_x is None:
                    started = True
                    prev_x, prev_y = x, y
                    prev_seq = seq
                    tap_frames = 1
                    tap_moved = 0
                    contact_dx = contact_dy = 0
                    continue
                dx = x - prev_x
                dy = y - prev_y
                prev_x, prev_y = x, y
                prev_seq = seq
                tap_frames += 1
                if abs(dx) > MAX_DELTA or abs(dy) > MAX_DELTA:
                    # 跳变：重新校准，不移动，也不把队列里的余量带过去
                    pend_x = pend_y = 0.0
                    contact_dx = contact_dy = 0
                    if DEBUG:
                        log(f"jump resync ({dx},{dy}) at ({x},{y})")
                    continue
                tap_moved += abs(dx) + abs(dy)
                pend_x += dx
                pend_y += dy
                contact_dx += dx
                contact_dy += dy
            elif pkt[0] == 0x16:
                # 状态帧 ... <caps>：最后一个字节就是键盘当前的 CapsLock 灯状态。
                # 用它做闭环校验——发过灯帧后对不上就重发，直到键盘自己确认。
                if len(pkt) >= 7:
                    hw = pkt[6] & 1
                    caps_synced[0] = True
                    if hw != LED_SENT[0] and (time.time() - led_retry[0]) > 1.0:
                        if led_retry[1] < 2:
                            led_retry[0] = time.time()
                            led_retry[1] += 1
                            log(f"led mismatch: hw={hw} want={LED_SENT[0]}, resend #{led_retry[1]}")
                            send_caps_led(fd, LED_SENT[0], tries=1)
                        else:
                            LED_SENT[0] = hw
                            led_retry[0] = time.time()
                            led_retry[1] = 0
                            log(f"led adopt hw state {hw} after retries")
                now = time.time()
                if now - last_status > 60:
                    log(f"status {pkt.hex()}")
                    last_status = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
