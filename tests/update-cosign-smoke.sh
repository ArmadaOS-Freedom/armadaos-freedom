#!/usr/bin/env bash
set -euo pipefail

COSIGN_BIN="${COSIGN_BIN:-cosign}"
# cosign is invoked after a cd, so resolve the selected binary to an absolute
# path first. `type -P` handles a bare command name, a relative path and an
# absolute path uniformly, searches PATH only (never an alias or function),
# and returns a name containing a slash unchanged, so a relative path with
# spaces survives the quoted dirname/basename canonicalisation below.
cosign_exe="$(type -P -- "$COSIGN_BIN" || true)"
if [[ -n "$cosign_exe" ]]; then
    cosign_exe="$(cd -P -- "$(dirname -- "$cosign_exe")" && pwd -P)/$(basename -- "$cosign_exe")"
fi
if [[ -z "$cosign_exe" || ! -f "$cosign_exe" || ! -x "$cosign_exe" ]]; then
    echo "SKIP: cosign not available at $COSIGN_BIN" >&2
    exit 77
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/tests:$REPO_ROOT/system_files/usr/lib/armada${PYTHONPATH:+:$PYTHONPATH}"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/cosign-smoke.XXXXXX")"
cleanup() {
    if [[ -n "${tmp:-}" && -d "$tmp" ]]; then
        rm -rf -- "$tmp"
    fi
}
trap cleanup EXIT

python3 -c 'import sys; from pathlib import Path; import test_update_package_builder as fixture; fixture.UpdatePackageBuilderTests()._make_bundle(Path(sys.argv[1]), models=("TB322FC","TB323FU"))' "$tmp"

CLI="$REPO_ROOT/build_files/make-universal-update-descriptor.py"
python3 "$CLI" make --archive "$tmp/update-bundle.zip" --release-id cosign-smoke --output "$tmp/descriptor.json"

(
    cd "$tmp"
    export COSIGN_PASSWORD=''
    "$cosign_exe" generate-key-pair --output-key-prefix "$tmp/synthetic" >/dev/null 2>&1
    "$cosign_exe" sign-blob --key "$tmp/synthetic.key" --tlog-upload=false --output-signature "$tmp/descriptor.sig" "$tmp/descriptor.json" >/dev/null 2>&1
)

cli_out="$(python3 "$CLI" verify --descriptor "$tmp/descriptor.json" --signature "$tmp/descriptor.sig" --trusted-key "$tmp/synthetic.pub")"
[[ "$cli_out" == *"cosign-smoke"* ]] || { echo "FAIL: expected cosign-smoke in output" >&2; exit 1; }

printf ' ' >> "$tmp/descriptor.json"
if python3 "$CLI" verify --descriptor "$tmp/descriptor.json" --signature "$tmp/descriptor.sig" --trusted-key "$tmp/synthetic.pub" >/dev/null 2>&1; then
    echo "FAIL: tampered descriptor verification succeeded" >&2
    exit 1
fi

echo "PASS: cosign smoke roundtrip succeeded"
