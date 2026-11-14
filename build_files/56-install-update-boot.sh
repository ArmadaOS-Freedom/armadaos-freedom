#!/bin/bash
set -euo pipefail

umask 022
root=/
context=/ctx

while (($#)); do
    case "$1" in
        --root)
            root=$2
            shift 2
            ;;
        --context)
            context=$2
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

root=$(realpath "$root")
context=$(realpath "$context")

share="$root/usr/share/armada/update-boot"
updates="$root/usr/lib/armada/updates"
[[ -f "$updates/boot_payload.py" ]]

(cd "$context/armada-runtimes" && sha256sum -c SHA256SUMS)

install -d -m 0755 "$share/runtimes" "$updates"
install -m 0644 "$context/build_files/edl-devices.json" "$share/edl-devices.json"
install -m 0644 "$context/build_files/edl_device_config.py" "$updates/edl_device_config.py"
install -m 0644 "$context/build_files/edl_boot_requirements.py" "$updates/edl_boot_requirements.py"

while IFS= read -r model; do
    [[ -n "$model" ]]
    install -m 0644 "$context/armada-runtimes/$model.bin" "$share/runtimes/$model.bin"
done < <(python3 "$updates/edl_device_config.py" --config "$context/build_files/edl-devices.json" --format models)

python3 - "$root" <<'PY'
import importlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "usr/lib/armada"))
manifest = importlib.import_module("updates.boot_payload").create_manifest(str(root))
(root / "usr/share/armada/update-boot/manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n"
)
PY
