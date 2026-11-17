# Bluetooth controller hidraw access

Steam and SDL can read controller reports through a dedicated HIDAPI driver.
The active local session needs read/write access to that controller's hidraw
node. Access to `/dev/input/event*` alone does not enable this path.

Armada adds `60-armada-xbox-hidraw.rules` for the Xbox Bluetooth IDs recognized
by SDL: `045e:02e0`, `02fd`, `0b20`, `0b05`, `0b22`, `0b13`, `0b0c` and
`0b21`. Compatible controllers presenting those IDs receive the same access.
The rule runs before `73-seat-late.rules`, allowing systemd's `uaccess` builtin
to grant the active seat user an ACL. It does not change button mappings.
It skips xpadneo-bound devices and precedes xpadneo's rules, preserving that
driver's policy of using evdev and disabling hidraw access.

## Verified behavior

On 2026-11-17, a Leadjoy Blade Plus in Xbox Bluetooth mode (`045e:02e0`) on a
Y700 Gen4 exposed a hidraw node owned by `root:root` with mode `0600` and no
user ACL. Steam logged `Permission denied`, then selected an evdev mapping
that mismatched X/Y, the shoulder buttons and the menu buttons.

Using the same Steam SDL library (`SDL-release-3.4.0-1576-gab5541ec5`), granting
the ordinary session user access selected `SDL_JOYSTICK_HIDAPI_XBOX_ONE`
without extra SDL hints. The user confirmed that the button mapping recovered.
The packaged rule was also checked on the device with `udevadm verify` and
confirmed to grant the active user's ACL automatically.
After a Bluetooth disconnect/reconnect, the newly created HID device received
the same ACL and the running Steam client logged `Controller using HIDAPI
driver`. Its input handle changed from evdev to hidraw without restarting
Steam or the tablet. Other Xbox models have not been tested on hardware.

## Sony and Nintendo coverage

The installed `steam-devices` package already grants Bluetooth hidraw access
to these devices:

| Family | Vendor/product IDs | Existing rule |
| --- | --- | --- |
| DualShock 3 | `054c:0268` | `60-steam-input.rules` |
| DualShock 4 / revision 2 | `054c:05c4`, `054c:09cc` | `60-steam-input.rules` |
| DualSense / Edge | `054c:0ce6`, `054c:0df2` | `60-steam-input.rules` |
| Joy-Con left / right | `057e:2006`, `057e:2007` | `60-steam-input.rules` |
| Switch Pro Controller | `057e:2009` | `60-steam-input.rules` |

InputPlumber's packaged `60-inputplumber-uaccess.rules` additionally covers
Sony HID devices. Rule inspection found no equivalent access gap for the IDs
above. Sony and Joy-Con controllers were not connected during this check;
their actual mappings and reconnect behavior have not been tested here.
This coverage does not establish support for third-party devices using other
IDs or for Joy-Con 2.

## Checking a connection

Find the controller with `udevadm info --attribute-walk --name=/dev/hidrawN`
and inspect its ACL with `getfacl /dev/hidrawN`. The active local user should
have read/write access. After adding a rule, reload udev rules and reconnect
the controller so Steam opens it again; a running client can retain its old
evdev connection until then.

Confirm the selected driver in Steam's `logs/console_log.txt` and check its
`logs/controller.txt`. Test all buttons, sticks, triggers and the D-pad, then
reconnect once. hidraw access enables the application parser; it does not
repair the kernel's evdev mapping for applications that use evdev directly.

The Xbox ID list follows [SDL's Bluetooth Xbox detection](https://github.com/libsdl-org/SDL/blob/ab5541ec5/src/joystick/SDL_joystick.c#L3392).
Sony and Nintendo access rules come from [Valve's steam-devices package](https://github.com/ValveSoftware/steam-devices/blob/master/60-steam-input.rules).
