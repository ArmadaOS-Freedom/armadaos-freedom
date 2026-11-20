#!/usr/bin/env python3
import argparse, hashlib, os, struct, sys

BOARDS = {"TB322FC": 1, "TB323FU": 2}
CHUNK = 1 << 20
LIMITS = {"kernel": 128 << 20, "initramfs": 128 << 20, "dtb": 256 << 10}
KSHIM_KERNEL_CAP = 130 << 20
ARM64_MAGIC = 0x644d5241

def read_hash(path, limit, label, header=None):
    total = 0
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            if header is not None and len(header) < 64:
                need = 64 - len(header)
                header += b[:need]
            total += len(b)
            if total > limit:
                raise SystemExit(f"{label} exceeds {limit} bytes: {path}")
            h.update(b)
    if total <= 0:
        raise SystemExit(f"{label} is empty: {path}")
    return total, h.digest()

def validate_kernel_header(file_bytes, head):
    if len(head) < 64 or file_bytes < 64:
        raise SystemExit("kernel ARM64 Image header shorter than 64 bytes")
    magic = struct.unpack_from("<I", head, 56)[0]
    if magic != ARM64_MAGIC:
        raise SystemExit(f"kernel ARM64 Image magic {magic:#010x} != {ARM64_MAGIC:#010x}")
    text_offset = struct.unpack_from("<Q", head, 8)[0]
    image_size = struct.unpack_from("<Q", head, 16)[0]
    effective = max(file_bytes, image_size)
    # Match KSHIM_KERNEL_CAP in boot_disk_stage.c; text_offset + effective must fit.
    if text_offset + effective > KSHIM_KERNEL_CAP:
        raise SystemExit(
            f"kernel load footprint {text_offset + effective} exceeds {KSHIM_KERNEL_CAP} bytes"
        )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(BOARDS))
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--initramfs", required=True)
    ap.add_argument("--dtb", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()
    khdr = bytearray()
    ks, kh = read_hash(a.kernel, LIMITS["kernel"], "kernel", khdr)
    validate_kernel_header(ks, khdr)
    rs, rh = read_hash(a.initramfs, LIMITS["initramfs"], "initramfs")
    ds, dh = read_hash(a.dtb, LIMITS["dtb"], "dtb")
    blob = struct.pack("<8sIIQQQ32s32s32s24x", b"ARMBTV1\x00", 1, BOARDS[a.model], ks, rs, ds, kh, rh, dh)
    assert len(blob) == 160
    with open(a.output, "xb") as f:
        f.write(blob)
    print(f"wrote {len(blob)} bytes to {a.output} (unsigned record)")

if __name__ == "__main__":
    main()
