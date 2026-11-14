#!/bin/bash
set -euxo pipefail

# Copy dist from the image build stage, not the source tree. The destination
# keeps the historical armada-control directory name; the source defaults to the
# in-tree plugin and can be overridden for dependency-owned sources.
install_plugin() {
    local name=$1 dist=$2 src=${3:-/ctx/decky/$1} dest=/usr/share/decky-plugins/$1
    install -d -m 0755 "${dest}"
    cp -a "${src}/plugin.json" "${src}/package.json" "${src}/main.py" "${dest}/"
    cp -a "${src}/py_modules" "${dest}/"
    [[ ! -f "${src}/catalog.json" ]] || cp -a "${src}/catalog.json" "${dest}/"
    [[ ! -d "${src}/templates" ]] || cp -a "${src}/templates" "${dest}/"
    cp -a "${dist}" "${dest}/dist"
    rm -f "${dest}/dist/"*.map
    find "${dest}" -name __pycache__ -type d -prune -exec rm -rf {} +
}
install_plugin armada-control /packages/decky-dist /ctx/decky/freedom-control/decky/armada-control
install_plugin armada-store /packages/decky-store-dist
chmod 0755 /usr/lib/decky-loader/armada-decky-sync

decky_tmp_dir="$(mktemp -d)"
cleanup_decky_tmp() {
    rm -rf -- "${decky_tmp_dir}"
}
trap cleanup_decky_tmp EXIT

decky_releases_json="${decky_tmp_dir}/releases.json"
decky_release_json="${decky_tmp_dir}/release.json"
decky_loader_tmp="${decky_tmp_dir}/PluginLoader-arm64"
decky_service_tmp="${decky_tmp_dir}/plugin_loader.service"

if [[ -e /run/secrets/decky_releases ]]; then
    cp /run/secrets/decky_releases "${decky_releases_json}"
else
    curl --retry 12 --retry-delay 10 --proto '=https' --proto-redir '=https' -fsSL \
        -o "${decky_releases_json}" \
        'https://api.github.com/repos/SteamDeckHomebrew/decky-loader/releases?per_page=100'
fi

PYTHONPATH=/usr/lib/armada python3 - "${decky_releases_json}" "${decky_release_json}" <<'PY'
from dataclasses import asdict
import json
import sys

from updates.decky_release import select_preview

with open(sys.argv[1], encoding="utf-8") as handle:
    release = select_preview(json.load(handle))
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(asdict(release), handle)
    handle.write("\n")
PY

decky_version="$(jq -er '.tag' "${decky_release_json}")"
decky_url="$(jq -er '.url' "${decky_release_json}")"
decky_service_url="https://raw.githubusercontent.com/SteamDeckHomebrew/decky-loader/${decky_version}/dist/plugin_loader-prerelease.service"

install -d -m 0755 /usr/share/decky-loader

curl --retry 12 --retry-delay 10 --proto '=https' --proto-redir '=https' -fL \
    -o "${decky_loader_tmp}" "${decky_url}"
PYTHONPATH=/usr/lib/armada python3 - "${decky_release_json}" "${decky_loader_tmp}" <<'PY'
import json
import sys

from updates.decky_release import DeckyRelease, verify_payload

with open(sys.argv[1], encoding="utf-8") as handle:
    release = DeckyRelease(**json.load(handle))
verify_payload(release, sys.argv[2])
PY

install -m 0755 "${decky_loader_tmp}" /usr/share/decky-loader/PluginLoader
printf '%s\n' "${decky_version}" > /usr/share/decky-loader/.loader.version
install -m 0644 "${decky_release_json}" /usr/share/decky-loader/release.json

curl --retry 12 --retry-delay 10 --proto '=https' --proto-redir '=https' -fsSL "${decky_service_url}" |
    sed 's#${HOMEBREW_FOLDER}#/var/home/armada/homebrew#g' \
        >"${decky_service_tmp}"
install -D -m 0644 "${decky_service_tmp}" /etc/systemd/system/plugin_loader.service

systemctl enable armada-decky-sync.service
systemctl enable plugin_loader.service
