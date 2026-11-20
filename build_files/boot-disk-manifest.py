#!/usr/bin/env python3
import argparse, hashlib, os, struct, sys

BOARDS = {"TB322FC": 1, "TB323FU": 2}
CHUNK = 1 << 20
LIMITS = {"kernel": 128 << 20, "initramfs": 128 << 20, "dtb": 256 << 10}

def read_hash(path, limit, label):
    total = 0
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            total += len(b)
            if total > limit:
                raise SystemExit(f"{label} exceeds {limit} bytes: {path}")
            h.update(b)
    if total <= 0:
        raise SystemExit(f"{label} is empty: {path}")
    return total, h.digest()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(BOARDS))
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--initramfs", required=True)
    ap.add_argument("--dtb", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    ks, kh = read_hash(a.kernel, LIMITS["kernel"], "kernel")
    rs, rh = read_hash(a.initramfs, LIMITS["initramfs"], "initramfs")
    ds, dh = read_hash(a.dtb, LIMITS["dtb"], "dtb")
    blob = struct.pack("<8sIIQQQ32s32s32s24x", b"ARMBTV1\x00", 1, BOARDS[a.model], ks, rs, ds, kh, rh, dh)
    assert len(blob) == 160
    with open(a.output, "xb") as f:
        f.write(blob)
    print(f"wrote {len(blob)} bytes to {a.output} (unsigned record)")

if __name__ == "__main__":
    main()
