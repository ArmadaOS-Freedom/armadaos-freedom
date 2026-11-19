#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/.github/scripts/split-disk-image.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -x "$HELPER" ] || { echo "helper not executable: $HELPER" >&2; exit 1; }

make_fixture() {
    local size="$1" path="$2"
    head -c "$size" /dev/zero | openssl enc -aes-128-ctr \
        -K 000102030405060708090a0b0c0d0e0f \
        -iv 00000000000000000000000000000000 2>/dev/null > "$path"
}

for spec in "1000:400" "1024:400" "777:400"; do
    size="${spec%%:*}"
    chunk="${spec##*:}"
    case_dir="$WORK/case-$size"
    mkdir -p "$case_dir"
    image="$case_dir/armada-$size.img.gz"
    make_fixture "$size" "$image"

    count="$("$HELPER" "$image" "$chunk" 8)"
    expected=$(( (size + chunk - 1) / chunk ))
    [ "$count" -eq "$expected" ] ||
        { echo "FAIL: size=$size count=$count expected=$expected" >&2; exit 1; }

    mapfile -t listed < <(sed -n '/^parts:$/,/^reassemble_command=/s/^  //p' \
        "$image.parts.manifest" | sed '/^reassemble_command=/d')
    [ "${#listed[@]}" -eq "$expected" ] ||
        { echo "FAIL: size=$size manifest entries=${#listed[@]}" >&2; exit 1; }
    [ "${listed[0]}" = "${image##*/}.part-01" ] ||
        { echo "FAIL: size=$size first part is not part-01" >&2; exit 1; }

    reassembled="$case_dir/reassembled.img.gz"
    : > "$reassembled"
    for name in "${listed[@]}"; do
        cat "$case_dir/$name" >> "$reassembled"
    done
    cmp -s "$image" "$reassembled" ||
        { echo "FAIL: size=$size bytes differ after reassembly" >&2; exit 1; }
    [ "$(sha256sum "$image" | awk '{print $1}')" = \
      "$(sha256sum "$reassembled" | awk '{print $1}')" ] ||
        { echo "FAIL: size=$size SHA-256 differs after reassembly" >&2; exit 1; }
done

exceed_dir="$WORK/exceed"
mkdir -p "$exceed_dir"
exceed_image="$exceed_dir/armada.img.gz"
make_fixture 1000 "$exceed_image"
if "$HELPER" "$exceed_image" 100 3 >"$exceed_dir/out" 2>"$exceed_dir/err"; then
    echo "FAIL: helper accepted chunk count above max_parts" >&2
    exit 1
fi
compgen -G "$exceed_image.part-*" >/dev/null &&
    { echo "FAIL: failed over-limit split left parts" >&2; exit 1; }
[ ! -e "$exceed_image.parts.manifest" ] ||
    { echo "FAIL: failed over-limit split left manifest" >&2; exit 1; }

for bad_chunk in 0 abc -5 ""; do
    bad_dir="$WORK/bad-$(printf '%s' "$bad_chunk" | tr -c 'A-Za-z0-9' '_')"
    mkdir -p "$bad_dir"
    bad_image="$bad_dir/armada.img.gz"
    make_fixture 100 "$bad_image"
    if "$HELPER" "$bad_image" "$bad_chunk" 8 >"$bad_dir/out" 2>"$bad_dir/err"; then
        echo "FAIL: helper accepted chunk_bytes='$bad_chunk'" >&2
        exit 1
    fi
    compgen -G "$bad_image.part-*" >/dev/null &&
        { echo "FAIL: invalid chunk left parts" >&2; exit 1; }
done

echo "Disk image split test passed"
