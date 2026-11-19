#!/usr/bin/env bash
# Split one disk image into independently downloadable parts.
#
# Usage: split-disk-image.sh <image> <chunk_bytes> <max_parts>
#
# Success prints only the part count. Diagnostics go to stderr. On failure,
# no partial parts or manifest remain.

set -euo pipefail

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

[ "$#" -eq 3 ] || die "usage: $0 <image> <chunk_bytes> <max_parts>"

image="$1"
chunk_bytes="$2"
max_parts="$3"

[ -f "$image" ] || die "image not found: $image"
[ -s "$image" ] || die "image is empty: $image"

case "$chunk_bytes" in
    ''|*[!0-9]*) die "chunk_bytes must be a positive integer, got: '$chunk_bytes'" ;;
esac
case "$max_parts" in
    ''|*[!0-9]*) die "max_parts must be a positive integer, got: '$max_parts'" ;;
esac

chunk_bytes=$((10#$chunk_bytes))
max_parts=$((10#$max_parts))
[ "$chunk_bytes" -gt 0 ] || die "chunk_bytes must be greater than zero"
[ "$max_parts" -gt 0 ] || die "max_parts must be greater than zero"
[ "$max_parts" -le 99 ] || die "max_parts must not exceed 99"

parts=()
reassembled=""
manifest="${image}.parts.manifest"
cleanup_failed() {
    rm -f -- "${parts[@]}" "$manifest"
    [ -z "$reassembled" ] || rm -f -- "$reassembled"
}
trap cleanup_failed EXIT

size="$(wc -c < "$image")"
size="${size//[[:space:]]/}"
chunk_count=$(( (size + chunk_bytes - 1) / chunk_bytes ))
if [ "$chunk_count" -lt 1 ] || [ "$chunk_count" -gt "$max_parts" ]; then
    die "chunk_count=$chunk_count outside 1..$max_parts (size=$size chunk_bytes=$chunk_bytes)"
fi

probe_dir="$(mktemp -d)"
trap 'rm -rf -- "$probe_dir"; cleanup_failed' EXIT
printf x > "${probe_dir}/probe"
split -b 1 -d -a 2 --numeric-suffixes=1 \
    "${probe_dir}/probe" "${probe_dir}/probe.part-"
[ -f "${probe_dir}/probe.part-01" ] ||
    die "split does not support two-digit numeric suffixes starting at 01"

rm -f -- "${image}".part-*
split -b "$chunk_bytes" -d -a 2 --numeric-suffixes=1 \
    "$image" "${image}.part-"
parts=( "${image}".part-* )
[ "${#parts[@]}" -eq "$chunk_count" ] ||
    die "expected $chunk_count parts, found ${#parts[@]}"

reassembled="$(mktemp)"
cat "${parts[@]}" > "$reassembled"
cmp -s "$image" "$reassembled" ||
    die "reassembled file does not match original"
orig_sha="$(sha256sum "$image" | awk '{print $1}')"
reassembled_sha="$(sha256sum "$reassembled" | awk '{print $1}')"
[ "$orig_sha" = "$reassembled_sha" ] ||
    die "SHA-256 mismatch after reassembly"
rm -f -- "$reassembled"
reassembled=""

image_basename="${image##*/}"
{
    printf 'image=%s\n' "$image_basename"
    printf 'chunk_bytes=%s\n' "$chunk_bytes"
    printf 'chunk_count=%s\n' "$chunk_count"
    printf 'sha256=%s\n' "$orig_sha"
    printf 'parts:\n'
    for part in "${parts[@]}"; do
        printf '  %s\n' "${part##*/}"
    done
    printf 'reassemble_command=cat %s.part-* > %s\n' \
        "$image_basename" "$image_basename"
    printf 'sha256_check_command=sha256sum -c %s.sha256\n' "$image_basename"
} > "$manifest"

rm -rf -- "$probe_dir"
trap - EXIT
printf '%s\n' "$chunk_count"
