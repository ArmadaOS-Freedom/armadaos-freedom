#!/usr/bin/env bash
# Host wrapper for the Freedom EDL raw package build.
#
# Builds the tools task image, resolves the OCI input and its embedded EDL
# runtimes, runs the raw producer with only the loop devices it needs, then
# splits the archive and publishes the part checksums plus a short bilingual
# readme. The source tree is only ever mounted read-only. The tools image tag is
# task-temporary and is removed again when the wrapper exits.
set -euo pipefail
umask 022

readonly TASK_PREFIX="freedom-edl-package"
readonly SIGNED_ORIGIN_RE='^ostree-image-signed:docker://ghcr\.io/armadaos-freedom/armadaos-freedom:(stable|beta|testing)(@sha256:[0-9a-f]{64})?$'
readonly GUID_RE='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '==> %s\n' "$*"
}

usage() {
    cat <<'EOF'
Usage: build-edl-package.sh --oci OCI --manifest-digest DIGEST --source-commit COMMIT \
           --version VERSION --output ABS_DIR --work ABS_DIR [options]

Build the Freedom EDL raw package and split it for distribution.

Required:
  --oci OCI_INPUT           docker://IMAGE[:TAG][@DIGEST], a bare registry image
                            reference, or an absolute OCI layout directory
  --manifest-digest DIGEST  exact image manifest digest (sha256:<64 lowercase hex>)
  --source-commit COMMIT    exact 40-character lowercase source revision
  --version VERSION         package version; must not be "latest"
  --output ABS_DIR          empty or new directory for the split distribution
  --work ABS_DIR            empty or new directory for all build state

Options:
  --source REPO             git checkout to build from (default: parent of build_files)
  --devices-config PATH     device profile config
                            (default: SOURCE/build_files/edl-devices.json)
  --model ID                package only this device profile; repeatable
                            (default: every profile registered in the config)
  --origin ORIGIN           ostree-image-signed origin; required unless the OCI
                            input is ghcr.io/armadaos-freedom/armadaos-freedom:stable|beta|testing
  --boot-uuid GUID          boot filesystem UUID (default: generated)
  --root-uuid GUID          root filesystem UUID (default: generated)
  --root-image-gib 32|48    root image size in GiB (default: 32)
  --runtime MODEL=ABS_FILE  optional exact-match cross-check runtime for a
                              selected profile; when given, provide exactly one
                              for every selected profile; never overrides the
                              runtimes embedded in the OCI input
  -h, --help                show this help

The full ZIP stays in WORK/archive. Only the split parts, their checksums and a
short readme are written to OUTPUT.
EOF
}

# --- command line -----------------------------------------------------------

oci_input=""
manifest_digest=""
source_commit=""
version=""
output_arg=""
work_arg=""
source_arg=""
devices_config=""
boot_uuid=""
root_uuid=""
root_image_gib=32
origin_arg=""
runtime_args=()
model_ids=()
selected_models=()

declare -A seen_flags=()

record_once() {
    [[ -z ${seen_flags[$1]:-} ]] || die "duplicate option: $1"
    seen_flags[$1]=1
}

need_value() {
    (($# >= 2)) || die "$1 requires a value"
}

while (($# > 0)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --oci) need_value "$@"; record_once --oci; oci_input="$2"; shift 2 ;;
        --manifest-digest) need_value "$@"; record_once --manifest-digest; manifest_digest="$2"; shift 2 ;;
        --source-commit) need_value "$@"; record_once --source-commit; source_commit="$2"; shift 2 ;;
        --version) need_value "$@"; record_once --version; version="$2"; shift 2 ;;
        --output) need_value "$@"; record_once --output; output_arg="$2"; shift 2 ;;
        --work) need_value "$@"; record_once --work; work_arg="$2"; shift 2 ;;
        --source) need_value "$@"; record_once --source; source_arg="$2"; shift 2 ;;
        --devices-config) need_value "$@"; record_once --devices-config; devices_config="$2"; shift 2 ;;
        --model) need_value "$@"; model_ids+=("$2"); shift 2 ;;
        --origin) need_value "$@"; record_once --origin; origin_arg="$2"; shift 2 ;;
        --boot-uuid) need_value "$@"; record_once --boot-uuid; boot_uuid="$2"; shift 2 ;;
        --root-uuid) need_value "$@"; record_once --root-uuid; root_uuid="$2"; shift 2 ;;
        --root-image-gib) need_value "$@"; record_once --root-image-gib; root_image_gib="$2"; shift 2 ;;
        --runtime) need_value "$@"; runtime_args+=("$2"); shift 2 ;;
        -*) die "unknown option: $1" ;;
        *) die "unexpected argument: $1" ;;
    esac
done

# --- basic validation -------------------------------------------------------

[[ -n $oci_input ]] || die "--oci is required"
[[ -n $manifest_digest ]] || die "--manifest-digest is required"
[[ -n $source_commit ]] || die "--source-commit is required"
[[ -n $version ]] || die "--version is required"
[[ -n $output_arg ]] || die "--output is required"
[[ -n $work_arg ]] || die "--work is required"

[[ $manifest_digest =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "--manifest-digest must be sha256:<64 lowercase hex>"
[[ $source_commit =~ ^[0-9a-f]{40}$ ]] \
    || die "--source-commit must be 40 lowercase hex characters"
[[ $version != latest ]] || die "--version must not be latest"
case "$version" in
    *[[:space:]]*|*/*|*\\*) die "--version must not contain whitespace, '/' or '\\'" ;;
esac

case "$root_image_gib" in
    32|48) ;;
    *) die "--root-image-gib must be 32 or 48" ;;
esac

if [[ -z $boot_uuid || -z $root_uuid ]]; then
    command -v uuidgen >/dev/null 2>&1 || die "uuidgen is required to generate filesystem UUIDs"
fi
[[ -n $boot_uuid ]] || boot_uuid="$(uuidgen)"
[[ -n $root_uuid ]] || root_uuid="$(uuidgen)"
[[ $boot_uuid =~ $GUID_RE ]] || die "--boot-uuid must be a canonical GUID"
[[ $root_uuid =~ $GUID_RE ]] || die "--root-uuid must be a canonical GUID"
[[ ${boot_uuid,,} != "${root_uuid,,}" ]] || die "boot and root UUIDs must differ"

# --- source tree ------------------------------------------------------------

if [[ -z $source_arg ]]; then
    source_arg="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
[[ -d $source_arg ]] || die "source is not a directory: $source_arg"
source_dir="$(cd -- "$source_arg" && pwd -P)"

preflight_sources=(
    build_files/make-edl-raw-package.py
    build_files/split-package-archive.py
    build_files/edl_device_config.py
    build_files/edl_boot_requirements.py
    build_files/make-universal-update-descriptor.py
    build_files/universal_package.py
    build_files/Containerfile.edl-tools
    tests/update-oci-pull-test.py
    system_files/usr/lib/armada/updates/__init__.py
    system_files/usr/lib/armada/updates/signature.py
    system_files/usr/lib/armada/updates/package_contract.py
    system_files/usr/lib/armada/updates/package_media.py
    system_files/usr/lib/armada/updates/recovery_manifest.py
    system_files/usr/lib/armada/updates/boot_payload.py
    system_files/usr/libexec/armada/armada-installer
)
for relative in "${preflight_sources[@]}"; do
    [[ -f $source_dir/$relative ]] || die "source is missing $relative"
done
[[ -e $source_dir/.git ]] || die "source must be a git checkout with .git: $source_dir"

# --- device profiles --------------------------------------------------------

command -v python3 >/dev/null 2>&1 \
    || die "python3 is required on the host to read the devices config"
helper="$source_dir/build_files/edl_device_config.py"

[[ -n $devices_config ]] || devices_config="$source_dir/build_files/edl-devices.json"
[[ -e $devices_config ]] || die "devices config does not exist: $devices_config"
[[ -f $devices_config ]] || die "devices config is not a regular file: $devices_config"
devices_config="$(realpath -- "$devices_config")"

model_select_args=()
for model_id in "${model_ids[@]+"${model_ids[@]}"}"; do
    model_select_args+=(--model "$model_id")
done

declare -A selected_model_set=()

models_output="$(python3 -B "$helper" --config "$devices_config" --format models \
    ${model_select_args[@]+"${model_select_args[@]}"})" \
    || die "the devices config helper rejected $devices_config"
[[ -n $models_output ]] || die "no device profile is selected"
mapfile -t selected_models <<< "$models_output"
for model_id in "${selected_models[@]}"; do
    [[ -n $model_id ]] || die "the devices config helper returned an empty model id"
    [[ $model_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
        || die "unusable model id in the devices config: $model_id"
    [[ -z ${selected_model_set[$model_id]:-} ]] || die "duplicate model id: $model_id"
    selected_model_set[$model_id]=1
done

readme_output="$(python3 -B "$helper" --config "$devices_config" --format readme \
    ${model_select_args[@]+"${model_select_args[@]}"})" \
    || die "the devices config helper could not produce the readme"
[[ -n $readme_output ]] || die "the devices config helper returned an empty readme"

# --- prebuilt runtimes ------------------------------------------------------

declare -A runtime_files=()
if ((${#runtime_args[@]} > 0)); then
    for spec in "${runtime_args[@]}"; do
        [[ $spec == *=* ]] || die "--runtime must be MODEL=ABSOLUTE_FILE: $spec"
        model="${spec%%=*}"
        file="${spec#*=}"
        [[ -n $model ]] || die "--runtime MODEL=ABSOLUTE_FILE requires a non-empty MODEL: $spec"
        [[ -n ${selected_model_set[$model]:-} ]] \
            || die "--runtime model is not selected by the devices config: $model"
        [[ -z ${runtime_files[$model]:-} ]] || die "duplicate runtime for $model"
        [[ $file == /* ]] || die "--runtime $model path must be absolute: $file"
        [[ -f $file ]] || die "--runtime $model is not a regular file: $file"
        runtime_files[$model]="$(realpath -- "$file")"
    done
    ((${#runtime_files[@]} == ${#selected_models[@]})) \
        || die "--runtime must be given exactly once for every selected model: ${selected_models[*]}"
fi

# --- work and output directories -------------------------------------------

[[ $work_arg == /* ]] || die "--work must be an absolute path"
[[ $output_arg == /* ]] || die "--output must be an absolute path"
work_dir="$(realpath -m -- "$work_arg")"
output_dir="$(realpath -m -- "$output_arg")"

[[ $work_dir != "$output_dir" ]] || die "--work and --output must be different directories"
case "$work_dir" in "$output_dir"/*) die "--work must not be inside --output" ;; esac
case "$output_dir" in "$work_dir"/*) die "--output must not be inside --work" ;; esac
[[ $work_dir != "$source_dir" ]] || die "--work must not be the source tree"
[[ $output_dir != "$source_dir" ]] || die "--output must not be the source tree"
case "$source_dir" in "$work_dir"/*) die "the source tree must not be inside --work" ;; esac
case "$source_dir" in "$output_dir"/*) die "the source tree must not be inside --output" ;; esac

prepare_empty_dir() {
    local path="$1" label="$2"
    local -a entries=()
    if [[ -e $path ]]; then
        [[ -d $path ]] || die "$label exists and is not a directory: $path"
        shopt -s nullglob dotglob
        entries=("$path"/*)
        shopt -u nullglob dotglob
        ((${#entries[@]} == 0)) || die "$label is not empty: $path"
    else
        mkdir -p -- "$path"
    fi
}

prepare_empty_dir "$work_dir" "--work"
prepare_empty_dir "$output_dir" "--output"

# --- OCI input and origin ---------------------------------------------------

local_oci=""
input_ref=""
case "$oci_input" in
    docker://*) input_ref="${oci_input#docker://}" ;;
    *://*) die "--oci must be docker://, a registry reference or an absolute OCI layout directory" ;;
    /*)
        [[ -d $oci_input ]] || die "OCI layout directory does not exist: $oci_input"
        local_oci="$(realpath -- "$oci_input")"
        ;;
    *[[:space:]]*) die "--oci must not contain whitespace: $oci_input" ;;
    *) input_ref="$oci_input" ;;
esac

origin=""
if [[ -n $origin_arg ]]; then
    [[ $origin_arg =~ $SIGNED_ORIGIN_RE ]] \
        || die "--origin must be ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:(stable|beta|testing)[@sha256:<hex>]"
    origin="$origin_arg"
fi

registry_repo=""
registry_tag=""
if [[ -n $input_ref ]]; then
    rest="$input_ref"
    case "$rest" in
        *@*)
            registry_digest="${rest##*@}"
            [[ $registry_digest =~ ^sha256:[0-9a-f]{64}$ ]] \
                || die "the digest in --oci must be sha256:<64 lowercase hex>: $registry_digest"
            [[ $registry_digest == "$manifest_digest" ]] \
                || die "the digest in --oci conflicts with --manifest-digest"
            rest="${rest%@*}"
            ;;
    esac
    case "$rest" in
        */*)
            head="${rest%/*}"
            base="${rest##*/}"
            case "$base" in
                *:*) registry_tag="${base##*:}"; base="${base%:*}" ;;
            esac
            registry_repo="$head/$base"
            ;;
        *:*)
            registry_tag="${rest##*:}"
            registry_repo="${rest%:*}"
            ;;
        *) die "--oci is not a registry image reference: $input_ref" ;;
    esac
    [[ -n $registry_repo && -n ${registry_repo##*/} ]] || die "--oci has no image name: $input_ref"
    case "$registry_tag" in
        *[!A-Za-z0-9_.-]*) die "--oci has an invalid image tag: $registry_tag" ;;
    esac
    if [[ -z $origin ]] \
        && [[ $registry_repo == "ghcr.io/armadaos-freedom/armadaos-freedom" ]]; then
        case "$registry_tag" in
            stable|beta|testing)
                origin="ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos-freedom:$registry_tag"
                ;;
        esac
    fi
fi

if [[ -n $local_oci ]]; then
    [[ -n $origin ]] || die "--origin is required for a local OCI layout directory"
else
    [[ -n $origin ]] \
        || die "--origin is required unless --oci is exactly ghcr.io/armadaos-freedom/armadaos-freedom:(stable|beta|testing)"
fi

# --- task bookkeeping -------------------------------------------------------

host_uid="$(id -u)"
host_gid="$(id -g)"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
tools_image="$TASK_PREFIX-tools:$run_id"
task_containers=()
built_images=()

cleanup() {
    local status=$?
    local name image failed=0
    trap - EXIT INT TERM
    for name in "${task_containers[@]+"${task_containers[@]}"}"; do
        docker rm -f -- "$name" >/dev/null 2>&1 || true
    done
    for image in "${built_images[@]+"${built_images[@]}"}"; do
        if ! docker image rm -- "$image" >/dev/null 2>&1; then
            printf 'warning: could not remove temporary image %s\n' "$image" >&2
            failed=1
        fi
    done
    if ((failed)); then
        printf 'warning: temporary image cleanup incomplete\n' >&2
    fi
    exit "$status"
}
trap 'exit 130' INT TERM
trap cleanup EXIT

run_container() {
    local name="$1"
    shift
    task_containers+=("$name")
    docker run --rm --name "$name" "$@"
}

# --- preflight --------------------------------------------------------------

command -v docker >/dev/null 2>&1 || die "docker is required on the host PATH"
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"

note "building the tools image $tools_image"
docker build \
    --file "$source_dir/build_files/Containerfile.edl-tools" \
    --tag "$tools_image" \
    "$source_dir/build_files"
built_images+=("$tools_image")

# --- OCI input --------------------------------------------------------------

oci_mount=()
if [[ -n $local_oci ]]; then
    note "using the local OCI layout $local_oci read-only at /oci"
    producer_oci="/oci"
    oci_mount=(-v "$local_oci:/oci:ro")
else
    note "copying $registry_repo@$manifest_digest for linux/arm64"
    if [[ -n ${DOCKER_CONFIG:-} ]]; then
        auth_config="$DOCKER_CONFIG/config.json"
    else
        auth_config="$HOME/.docker/config.json"
    fi
    skopeo_mount=()
    skopeo_auth=()
    if [[ -e $auth_config ]]; then
        [[ -f $auth_config && -r $auth_config ]] \
            || die "Docker credential config is not a readable file: $auth_config"
        skopeo_mount=(-v "$auth_config:/auth/config.json:ro")
        skopeo_auth=(--authfile /auth/config.json)
    fi
    run_container "$TASK_PREFIX-skopeo-$run_id" \
        --cpus 4 \
        --memory 6g \
        --user "$host_uid:$host_gid" \
        --env HOME=/tmp \
        -v "$work_dir:/work" \
        ${skopeo_mount[@]+"${skopeo_mount[@]}"} \
        "$tools_image" \
        skopeo copy \
            ${skopeo_auth[@]+"${skopeo_auth[@]}"} \
            --override-arch arm64 \
            --override-os linux \
            "docker://$registry_repo@$manifest_digest" \
            "oci:/work/oci:latest"
    [[ -d $work_dir/oci ]] || die "skopeo did not produce $work_dir/oci"
    producer_oci="/work/oci"
fi

# --- runtimes ---------------------------------------------------------------

config_mount=(-v "$devices_config:/config/edl-devices.json:ro")
runtime_mounts=()
producer_runtime_args=()

if ((${#runtime_files[@]} > 0)); then
    note "cross-checking the supplied runtimes for ${selected_models[*]}"
    for model_id in "${selected_models[@]}"; do
        runtime_mounts+=(-v "${runtime_files[$model_id]}:/runtime/$model_id.bin:ro")
        producer_runtime_args+=(--runtime "$model_id=/runtime/$model_id.bin")
    done
else
    note "taking the runtimes from the OCI input for ${selected_models[*]}"
fi

# --- loop devices -----------------------------------------------------------

[[ -c /dev/loop-control ]] || die "/dev/loop-control is required for the raw producer"

free_loops=()
for ((index = 0; index <= 255; index++)); do
    sys="/sys/block/loop$index"
    [[ -d $sys ]] || continue
    [[ -b /dev/loop$index ]] || continue
    if [[ -e $sys/loop/backing_file ]]; then
        continue
    fi
    size="$(cat "$sys/size" 2>/dev/null || printf '1')"
    if [[ $size != 0 ]]; then
        continue
    fi
    free_loops+=("$index")
    if ((${#free_loops[@]} == 2)); then
        break
    fi
done
((${#free_loops[@]} == 2)) \
    || die "two unused /dev/loopN devices are required for the raw producer; found ${#free_loops[@]}"
loop_a="/dev/loop${free_loops[0]}"
loop_b="/dev/loop${free_loops[1]}"
note "using $loop_a and $loop_b for the file-backed images"

# --- raw producer -----------------------------------------------------------

mkdir -p -- "$work_dir/tmp"

note "running the real bootc/OSTree OCI protocol regression tests"
run_container "$TASK_PREFIX-oci-protocol-$run_id" \
    --cpus 2 \
    --memory 1g \
    --network none \
    -v "$source_dir:/source:ro" \
    -v "$work_dir:/work" \
    -e TMPDIR=/work/tmp \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$tools_image" \
    python3 -B /source/tests/update-oci-pull-test.py

producer_args=(
    python3 -B /source/build_files/make-edl-raw-package.py
    --source /source
    --oci "$producer_oci"
    --manifest-digest "$manifest_digest"
    --source-commit "$source_commit"
    --origin "$origin"
    --devices-config /config/edl-devices.json
    --boot-uuid "$boot_uuid"
    --root-uuid "$root_uuid"
    --root-image-gib "$root_image_gib"
    --version "$version"
    --offline-upgrade
    --work /work/producer
    --out /work/archive
)
for model_id in "${selected_models[@]}"; do
    producer_args+=(--model "$model_id")
done
producer_args+=("${producer_runtime_args[@]}")

note "producing the raw EDL package for ${selected_models[*]}"
run_container "$TASK_PREFIX-producer-$run_id" \
    --cpus 4 \
    --memory 6g \
    --network none \
    --cap-add SYS_ADMIN \
    --security-opt apparmor=unconfined \
    --device /dev/loop-control \
    --device "$loop_a" \
    --device "$loop_b" \
    -v "$source_dir:/source:ro" \
    -v "$work_dir:/work" \
    "${config_mount[@]}" \
    ${oci_mount[@]+"${oci_mount[@]}"} \
    "${runtime_mounts[@]}" \
    -e TMPDIR=/work/tmp \
    -e TMP=/work/tmp \
    -e TEMP=/work/tmp \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e GIT_CONFIG_COUNT=1 \
    -e GIT_CONFIG_KEY_0=safe.directory \
    -e GIT_CONFIG_VALUE_0=/source \
    "$tools_image" \
    "${producer_args[@]}"

zip_name="ArmadaOS-Freedom-$version.zip"
[[ -f $work_dir/archive/$zip_name ]] \
    || die "the producer did not write $work_dir/archive/$zip_name"

# --- split ------------------------------------------------------------------

note "splitting $zip_name into 1992294400-byte volumes"
run_container "$TASK_PREFIX-split-$run_id" \
    --cpus 4 \
    --memory 6g \
    --network none \
    --user "$host_uid:$host_gid" \
    -v "$source_dir:/source:ro" \
    -v "$work_dir:/work:ro" \
    -v "$output_dir:/output" \
    "$tools_image" \
    python3 -B /source/build_files/split-package-archive.py \
        --input "/work/archive/$zip_name" \
        --output /output

parts_manifest="$output_dir/$zip_name.parts.json"
[[ -f $parts_manifest ]] || die "the splitter did not write $parts_manifest"

# --- update descriptor -----------------------------------------------------

update_descriptor="$output_dir/$zip_name.update.json"
note "producing the unsigned update descriptor $zip_name.update.json"
# The producer already verified the deployed image carries the normal-Linux
# updater, so the descriptor may declare both install and offline-upgrade. A
# package built from an image without that updater fails before this point and
# can never be relabeled as upgrade-capable here.
run_container "$TASK_PREFIX-descriptor-$run_id" --cpus 4 --memory 6g --network none --user "$host_uid:$host_gid" -v "$source_dir:/source:ro" -v "$work_dir:/work:ro" -v "$output_dir:/output" "$tools_image" python3 -B /source/build_files/make-universal-update-descriptor.py make --archive "/work/archive/$zip_name" --parts-manifest "/output/$zip_name.parts.json" --release-id "$version" --offline-upgrade --output "/output/$zip_name.update.json"
[[ -f $update_descriptor ]] || die "the descriptor tool did not write $update_descriptor"

command -v python3 >/dev/null 2>&1 \
    || die "python3 is required on the host to read the parts manifest"

python3 - "$parts_manifest" > "$output_dir/.SHA256SUMS.part" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)
parts = manifest.get("parts") if isinstance(manifest, dict) else None
if not isinstance(parts, list) or not parts:
    raise SystemExit("the parts manifest carries no parts")
lines = []
for part in parts:
    name = part.get("fileName") if isinstance(part, dict) else None
    digest = part.get("sha256") if isinstance(part, dict) else None
    if not isinstance(name, str) or not name or "/" in name:
        raise SystemExit("the parts manifest has an invalid fileName")
    if not isinstance(digest, str) or len(digest) != 64:
        raise SystemExit("the parts manifest has an invalid sha256")
    lines.append("%s  %s\n" % (digest, name))
sys.stdout.write("".join(lines))
PY
mv -f -- "$output_dir/.SHA256SUMS.part" "$output_dir/SHA256SUMS"

cat > "$output_dir/README.zh-en.txt" <<EOF
ArmadaOS Freedom - EDL raw package $version
============================================

Device profiles in this package / 本包包含的机型：

$readme_output

Files:
  ArmadaOS-Freedom-$version.zip.001 ...      split parts of one archive
  ArmadaOS-Freedom-$version.zip.parts.json   split metadata
  ArmadaOS-Freedom-$version.zip.update.json  unsigned install and upgrade identity
  SHA256SUMS                                 checksums of the split parts
  README.zh-en.txt                           this file

The .update.json file is unsigned and only describes this package. Published
packages additionally carry a .update.json.sig that installers verify against
the trusted release public key; without that signature these files carry no
authenticity.

The descriptor declares first installation and a normal-Linux offline upgrade
that keeps user data and both boot slots. That upgrade runs from an installed
system; memory-rescue tools are not part of this package.

Keep all parts in the same folder. SHA256SUMS checks the parts:
  sha256sum -c SHA256SUMS        (Linux)
  shasum -a 256 -c SHA256SUMS    (macOS)

Select the ".001" part directly in an ArmadaOS Freedom installer that supports
this package. The installer uses the files that match the detected device.
No manual extraction, merging or recompression is needed.

[中文]
本包包含上方列出的机型配置。请将所有分卷放在同一文件夹内，SHA256SUMS 用于校验各分卷。
在支持本包的 ArmadaOS Freedom 安装器中直接选择 .001 分卷，安装器会自动使用与所识别机型匹配的文件，无需手动解压、合并或重新压缩。
描述文件同时声明首次安装与保留用户数据及两个启动槽的正常 Linux 离线升级；该升级在已安装系统中执行，本包不包含内存救援工具。
EOF

# --- summary ----------------------------------------------------------------

note "package complete"
printf 'output: %s\n' "$output_dir"
printf 'parts manifest: %s\n' "$parts_manifest"
printf 'full archive: %s\n' "$work_dir/archive/$zip_name"
printf 'tools image (removed on exit): %s\n' "$tools_image"
