"""Standalone bounded local Wayland socket roundtrip probe."""

from __future__ import annotations

import ctypes
import os
import sys

WAYLAND_CLIENT_SONAME = "libwayland-client.so.0"


def _load_client() -> ctypes.CDLL:
    lib = ctypes.CDLL(WAYLAND_CLIENT_SONAME, use_errno=True)
    lib.wl_display_connect.argtypes = [ctypes.c_char_p]
    lib.wl_display_connect.restype = ctypes.c_void_p
    lib.wl_display_roundtrip.argtypes = [ctypes.c_void_p]
    lib.wl_display_roundtrip.restype = ctypes.c_int
    lib.wl_display_disconnect.argtypes = [ctypes.c_void_p]
    lib.wl_display_disconnect.restype = None
    return lib


def probe_socket(socket_path: str) -> int:
    if not socket_path or not os.path.isabs(socket_path) or "\x00" in socket_path:
        sys.stderr.write("invalid wayland socket path\n")
        return 1
    try:
        lib = _load_client()
    except OSError as exc:
        sys.stderr.write(f"wayland client library error: {exc}\n")
        return 1

    display = lib.wl_display_connect(os.fsencode(socket_path))
    if not display:
        err = ctypes.get_errno()
        sys.stderr.write(f"wl_display_connect failed: errno={err}\n")
        return 1
    try:
        rc = int(lib.wl_display_roundtrip(display))
        if rc < 0:
            err = ctypes.get_errno()
            sys.stderr.write(f"wl_display_roundtrip failed: errno={err}\n")
            return 1
        return 0
    finally:
        lib.wl_display_disconnect(display)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: wayland_probe.py <absolute-socket-path>\n")
        return 1
    return probe_socket(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
