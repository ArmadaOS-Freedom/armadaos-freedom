#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kshim="$root/devices/lenovo-tb323fu/boot/kshim-success-source"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/update-boot-runtime-test.XXXXXX")"
trap 'rm -rf -- "$tmp"' EXIT

gcc_common=(-std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -g -I "$kshim/src/include")
core_sources=(
  "$kshim/src/core/storage_block.c"
  "$kshim/src/core/storage_config.c"
  "$kshim/src/core/update_trial.c"
  "$kshim/src/core/update_trial_store.c"
)

build_and_run() {
  local name="$1"
  shift
  gcc "${gcc_common[@]}" "$@" "$kshim/tests/$name.c" -o "$tmp/$name"
  "$tmp/$name"
}

for name in storage_config_test update_trial_test update_trial_store_test update_trial_policy_test; do
  build_and_run "$name" "${core_sources[@]}"
done

build_and_run storage_ab_test \
  "$kshim/src/core/storage_ab.c" \
  "$kshim/src/core/storage_block.c"
build_and_run update_binding_test \
  "$kshim/src/core/update_binding.c"
build_and_run update_trial_boot_test \
  "$kshim/src/core/update_trial_boot.c" \
  "$kshim/src/core/update_binding.c" \
  "$kshim/src/core/update_trial.c" \
  "$kshim/src/core/storage_block.c"
build_and_run storage_ufs_settings_test \
  "$kshim/src/drivers/storage_ufs_settings.c" \
  "$kshim/src/core/storage_settings.c" \
  "${core_sources[@]}"
build_and_run storage_settings_native_test \
  -DKSHIM_HOST_TEST=1 -DCONFIG_KSHIM_SETTINGS_SAVE_TEST=1 \
  "$kshim/src/arch/arm64/storage_settings_native.c"
