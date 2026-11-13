#!/usr/bin/env bash
# Build one EDL runtime per device profile selected from the devices config.
# Runs inside the Containerfile.edl-runtime image; the source tree is never modified.
set -euo pipefail
umask 022

readonly CRDK_REPO="https://github.com/Project-Aloha/CrDK"
readonly CRDK_COMMIT="b6c3335136b7f6b28271917de11e8f11982975fc"
readonly PICOLIBC_REPO="https://github.com/picolibc/picolibc"
readonly PICOLIBC_COMMIT="f7edb0869e64b9d687f46961adeef7154921fe92"
readonly LVGL_REPO="https://github.com/lvgl/lvgl"
readonly LVGL_COMMIT="7f07a129e8d77f4984fff8e623fd5be18ff42e74"
readonly KSHIMC_REPO="https://github.com/sunflower2333/kShimC"
readonly KSHIMC_COMMIT="82ce6eaa10c36e64cfd3f16799172d854f9a955b"
readonly LIBFDT_ENV_SHA256="132cb14b8f0b323e5701d4bfcb0ebba4b1f35726bfdfa6739f18ad638d20e582"
readonly RUNTIME_BYTES=4194304

usage() {
    cat <<'EOF'
Usage: build-edl-runtime.sh --source REPO --work ABSOLUTE_EMPTY_OR_NEW_DIR [options]

Build one 4 MiB EDL runtime per selected device profile.

  --source REPO           repository root containing build_files/edl_device_config.py
  --work DIR              absolute empty (or not yet existing) directory for copies, deps and outputs
  --jobs N                parallel build jobs, positive integer (default: 4)
  --devices-config PATH   device profile config (default: REPO/build_files/edl-devices.json)
  --model ID              build only this profile; repeatable (default: every profile)
  -h, --help              show this help

The device config is validated before any work directory, snapshot copy or
dependency download happens. The source tree is read-only; dependencies, build
trees and outputs are created only under DIR, which receives one 4 MiB image per
selected profile in DIR/runtimes plus SHA256SUMS.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '==> %s\n' "$*"
}

source_dir=""
work_dir=""
devices_config=""
jobs=4
model_ids=()
selected_models=()

while (($# > 0)); do
    case "$1" in
        --source)
            (($# >= 2)) || die "--source requires a value"
            source_dir="$2"
            shift 2
            ;;
        --work)
            (($# >= 2)) || die "--work requires a value"
            work_dir="$2"
            shift 2
            ;;
        --jobs)
            (($# >= 2)) || die "--jobs requires a value"
            jobs="$2"
            shift 2
            ;;
        --devices-config)
            (($# >= 2)) || die "--devices-config requires a value"
            devices_config="$2"
            shift 2
            ;;
        --model)
            (($# >= 2)) || die "--model requires a value"
            model_ids+=("$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
done

[[ -n "$source_dir" ]] || die "--source is required"
[[ -n "$work_dir" ]] || die "--work is required"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer"

[[ -d "$source_dir" ]] || die "source is not a directory: $source_dir"
source_dir="$(cd "$source_dir" && pwd -P)"

[[ "$work_dir" == /* ]] || die "--work must be an absolute path"
work_dir="$(realpath -m -- "$work_dir")"

case "$work_dir" in
    "$source_dir"|"$source_dir"/*) die "work directory must not be inside the source tree" ;;
esac
case "$source_dir" in
    "$work_dir"/*) die "source tree must not be inside the work directory" ;;
esac

# --- device config ----------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 is required to read the devices config"
helper="$source_dir/build_files/edl_device_config.py"
[[ -f "$helper" ]] || die "source is missing build_files/edl_device_config.py"

[[ -n "$devices_config" ]] || devices_config="$source_dir/build_files/edl-devices.json"
[[ -e "$devices_config" ]] || die "devices config does not exist: $devices_config"
[[ -f "$devices_config" ]] || die "devices config is not a regular file: $devices_config"
devices_config="$(realpath -- "$devices_config")"

select_args=()
for model_id in "${model_ids[@]+"${model_ids[@]}"}"; do
    select_args+=(--model "$model_id")
done

declare -A selected_model_set=()

models_output="$(python3 -B "$helper" --config "$devices_config" --format models \
    ${select_args[@]+"${select_args[@]}"})" \
    || die "the devices config helper rejected $devices_config"
[[ -n "$models_output" ]] || die "no device profile is selected"
mapfile -t selected_models <<< "$models_output"
for model_id in "${selected_models[@]}"; do
    [[ -n "$model_id" ]] || die "the devices config helper returned an empty model id"
    [[ "$model_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
        || die "unusable model id in the devices config: $model_id"
    [[ -z "${selected_model_set[$model_id]:-}" ]] || die "duplicate model id: $model_id"
    selected_model_set[$model_id]=1
done

snapshot_rel="$(python3 -B "$helper" --config "$devices_config" --format runtime-source \
    ${select_args[@]+"${select_args[@]}"})" \
    || die "the devices config helper could not resolve runtimeSource"
[[ -n "$snapshot_rel" ]] || die "the devices config has no runtimeSource"
[[ "$snapshot_rel" != /* ]] || die "runtimeSource must be a relative path: $snapshot_rel"
case "/$snapshot_rel/" in
    */../*) die "runtimeSource must not contain '..': $snapshot_rel" ;;
    */./*) die "runtimeSource must be a clean relative path: $snapshot_rel" ;;
esac

plan_output="$(python3 -B "$helper" --config "$devices_config" --format runtime-plan \
    ${select_args[@]+"${select_args[@]}"})" \
    || die "the devices config helper could not produce a runtime plan"
[[ -n "$plan_output" ]] || die "the devices config produced an empty runtime plan"
mapfile -t plan_lines <<< "$plan_output"

plan_models=()
plan_configs=()
for line in "${plan_lines[@]}"; do
    [[ -n "$line" ]] || die "the runtime plan contains an empty line"
    [[ "$line" == *$'\t'* ]] || die "malformed runtime plan line: $line"
    model_id="${line%%$'\t'*}"
    config_rel="${line#*$'\t'}"
    [[ -n "$model_id" && -n "$config_rel" ]] || die "malformed runtime plan line: $line"
    [[ "$config_rel" != *$'\t'* ]] || die "malformed runtime plan line: $line"
    [[ -n "${selected_model_set[$model_id]:-}" ]] \
        || die "the runtime plan lists an unselected model: $model_id"
    [[ "$config_rel" != /* ]] || die "runtime config must be relative: $config_rel"
    case "/$config_rel/" in
        */../*) die "runtime config must not contain '..': $config_rel" ;;
    esac
    plan_models+=("$model_id")
    plan_configs+=("$config_rel")
done
((${#plan_models[@]} == ${#selected_models[@]})) \
    || die "the runtime plan does not cover every selected model"

snapshot="$source_dir/$snapshot_rel"
[[ -d "$snapshot" ]] || die "runtime snapshot not found: $snapshot"
[[ -f "$snapshot/tools/build_runtime.py" ]] || die "missing tools/build_runtime.py in snapshot"
[[ -f "$snapshot/CMakeLists.txt" ]] || die "missing CMakeLists.txt in snapshot"
[[ -d "$snapshot/ports/crdk/patches" ]] || die "missing ports/crdk/patches in snapshot"
[[ -d "$snapshot/src/ui/fonts" ]] || die "missing shipped menu fonts in snapshot"

for config_rel in "${plan_configs[@]}"; do
    [[ -f "$snapshot/$config_rel" ]] || die "missing runtime config in snapshot: $config_rel"
done

# --- work directory and tools ----------------------------------------------

if [[ -e "$work_dir" ]]; then
    [[ -d "$work_dir" ]] || die "work path exists and is not a directory: $work_dir"
    first_entry="$(find "$work_dir" -mindepth 1 -maxdepth 1 -print -quit)" \
        || die "cannot inspect work directory: $work_dir"
    [[ -z "$first_entry" ]] || die "work directory is not empty: $work_dir"
else
    mkdir -p -- "$work_dir"
fi

for tool in python3 cmake ninja git genconfig aarch64-linux-gnu-gcc \
            aarch64-linux-gnu-objcopy aarch64-linux-gnu-nm sha256sum realpath; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not in PATH: $tool"
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "Python 3.12 or newer is required"

# --- runtime snapshot and pinned dependencies -------------------------------

runtime="$work_dir/runtime-source"
note "copying runtime snapshot $snapshot_rel to $runtime"
mkdir -p -- "$runtime"
cp -a -- "$snapshot/." "$runtime/"

for dep in lib/CrDK lib/picolibc third_party/lvgl lib/libfdt; do
    [[ ! -e "$runtime/$dep" ]] || die "snapshot unexpectedly already contains $dep"
done

fetch_pinned() {
    local url="$1" commit="$2" dest="$3" actual
    mkdir -p -- "$(dirname -- "$dest")"
    git init -q -- "$dest"
    git -C "$dest" remote add origin "$url"
    git -C "$dest" fetch -q --depth=1 origin "$commit"
    git -C "$dest" checkout -q --detach FETCH_HEAD
    actual="$(git -C "$dest" rev-parse HEAD)"
    [[ "$actual" == "$commit" ]] || die "pin mismatch at $dest: expected $commit, got $actual"
}

note "restoring pinned CrDK, picolibc and LVGL"
fetch_pinned "$CRDK_REPO" "$CRDK_COMMIT" "$runtime/lib/CrDK"
fetch_pinned "$PICOLIBC_REPO" "$PICOLIBC_COMMIT" "$runtime/lib/picolibc"
fetch_pinned "$LVGL_REPO" "$LVGL_COMMIT" "$runtime/third_party/lvgl"

libfdt_source="$work_dir/dependency-sources/kShimC"
note "restoring libfdt from the pinned kShimC dependency source"
mkdir -p -- "$(dirname -- "$libfdt_source")"
git init -q -- "$libfdt_source"
git -C "$libfdt_source" remote add origin "$KSHIMC_REPO"
git -C "$libfdt_source" config remote.origin.promisor true
git -C "$libfdt_source" config remote.origin.partialclonefilter blob:none
git -C "$libfdt_source" fetch -q --depth=1 --filter=blob:none origin "$KSHIMC_COMMIT"
libfdt_fetched="$(git -C "$libfdt_source" rev-parse --verify FETCH_HEAD)"
[[ "$libfdt_fetched" == "$KSHIMC_COMMIT" ]] \
    || die "pin mismatch at $libfdt_source: expected $KSHIMC_COMMIT, got $libfdt_fetched"
git -C "$libfdt_source" archive "$KSHIMC_COMMIT" lib/libfdt | tar -x -C "$runtime"
[[ -f "$runtime/lib/libfdt/include/libfdt_env.h" ]] || die "libfdt export is incomplete"

sed -i '/^#include <stdint.h>$/a #include <stdlib.h>' \
    "$runtime/lib/libfdt/include/libfdt_env.h"
( cd "$runtime" && printf '%s  lib/libfdt/include/libfdt_env.h\n' "$LIBFDT_ENV_SHA256" \
    | sha256sum -c - >/dev/null ) || die "libfdt_env.h SHA256 mismatch after adaptation"

note "normalizing CrDK patch line endings to LF"
shopt -s nullglob
patches=("$runtime"/ports/crdk/patches/*.patch)
shopt -u nullglob
for patch in "${patches[@]}"; do
    sed -i 's/\r$//' "$patch"
done

# --- per-profile builds -----------------------------------------------------

check_cache() {
    local cache="$1" key
    [[ -f "$cache" ]] || die "missing CMakeCache.txt: $cache"
    for key in KSHIM_STANDALONE KSHIM_DIAGNOSTIC_BASE_ONLY KSHIM_DIAGNOSTIC_DT_ONLY \
               KSHIM_DIAGNOSTIC_RESOURCES_ONLY KSHIM_RAMOOPS_RECOVERY_ONLY; do
        grep -qx "${key}:BOOL=OFF" "$cache" || die "${key} is not BOOL=OFF in $cache"
    done
}

runtimes="$work_dir/runtimes"
mkdir -p -- "$runtimes"
staged=()

build_profile() {
    local model="$1" config_rel="$2"
    local out_rel="build/$model"
    local build_dir="$runtime/build/$model"
    local binary size symbols
    local -a profile_args=()
    local arg_output

    note "building $model from $config_rel"

    # Pre-configure the isolated output directory: the profile-specific cache
    # entries have to be set before the original tool runs its own configure.
    mkdir -p -- "$build_dir"
    sed -e 's/^CONFIG_KSHIM_UEFI_TEST_MENU=.*$/# CONFIG_KSHIM_UEFI_TEST_MENU is not set/' \
        -- "$runtime/$config_rel" > "$build_dir/.config"

    arg_output="$(python3 -B "$helper" --config "$devices_config" --format runtime-args \
        --model "$model")" \
        || die "the devices config helper could not resolve runtimeCmakeArgs for $model"
    if [[ -n "$arg_output" ]]; then
        mapfile -t profile_args <<< "$arg_output"
    fi

    cmake -S "$runtime" -B "$build_dir" -G Ninja \
        -DKSHIM_STANDALONE=OFF \
        -DKSHIM_RUNTIME_SLOT_SIZE=0x400000 \
        -DKSHIM_UEFI_PAYLOAD= \
        -DCMAKE_BUILD_TYPE=Release \
        ${profile_args[@]+"${profile_args[@]}"}

    ( cd "$runtime" && python3 tools/build_runtime.py \
        --config "$config_rel" --out "$out_rel" --jobs "$jobs" )

    binary="$build_dir/kShimC.bin"
    [[ -f "$binary" ]] || die "missing binary: $binary"
    size="$(stat -c %s -- "$binary")"
    [[ "$size" == "$RUNTIME_BYTES" ]] || die "$binary is $size bytes, expected $RUNTIME_BYTES"
    if [[ "$model" == TB322FC || "$model" == TB323FU ]]; then
        grep -aqF 'ARMADA_LINUX_MIRROR_V1' "$binary" \
            || die "$binary is missing the ARMADA_LINUX_MIRROR_V1 runtime contract marker"
    fi
    check_cache "$build_dir/CMakeCache.txt"
    symbols="$(aarch64-linux-gnu-nm -u "$build_dir/kShimC.elf")"
    [[ -z "${symbols//[[:space:]]/}" ]] || die "undefined symbols in $build_dir/kShimC.elf"
    [[ -f "$build_dir/validation.json" ]] || die "missing validation.json in $build_dir"
    grep -q '"undefined_symbols": \[\]' "$build_dir/validation.json" \
        || die "builder reported undefined symbols in $build_dir/validation.json"
    grep -q '"hardware_tested": false' "$build_dir/validation.json" \
        || die "unexpected hardware_tested value in $build_dir/validation.json"

    cp -- "$binary" "$runtimes/$model.bin"
    staged+=("$model.bin")
}

for index in "${!plan_models[@]}"; do
    build_profile "${plan_models[$index]}" "${plan_configs[$index]}"
done

for image in "${staged[@]}"; do
    size="$(stat -c %s -- "$runtimes/$image")"
    [[ "$size" == "$RUNTIME_BYTES" ]] || die "staged $image is $size bytes, expected $RUNTIME_BYTES"
done

( cd "$runtimes" && sha256sum "${staged[@]}" > SHA256SUMS )
( cd "$runtimes" && sha256sum -c --quiet SHA256SUMS )

note "done: ${#staged[@]} runtime image(s) in $runtimes (offline build only; no hardware test)"
