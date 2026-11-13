#!/usr/bin/env bash
# Offline test for .github/scripts/verify-signing-key.sh.
#
# Generates throwaway key pairs and proves the helper accepts the public key
# of the matching pair while rejecting the public key of a different pair and
# a malformed trusted key. No network, registry, transparency log, or real
# secret is used.
set -euo pipefail

COSIGN_BIN="${COSIGN_BIN:-cosign}"
if ! command -v "$COSIGN_BIN" >/dev/null 2>&1; then
    echo "SKIP: cosign not available at $COSIGN_BIN" >&2
    exit 77
fi
if ! command -v openssl >/dev/null 2>&1; then
    echo "SKIP: openssl not available" >&2
    exit 77
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$REPO_ROOT/.github/scripts/verify-signing-key.sh"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/signing-key-test.XXXXXX")"
cleanup() {
    if [[ -n "${tmp:-}" && -d "$tmp" ]]; then
        rm -rf -- "$tmp"
    fi
}
trap cleanup EXIT

# Throwaway password for the throwaway keys below. The helper deliberately
# requires a nonempty COSIGN_PASSWORD, so the same nonempty value must both
# create these keys and unlock them; it protects nothing and is never secret.
throwaway_password="throwaway-signing-key-password"

(
    cd "$tmp"
    export COSIGN_PASSWORD="$throwaway_password"
    "$COSIGN_BIN" generate-key-pair --output-key-prefix "$tmp/signing" >/dev/null 2>&1
    "$COSIGN_BIN" generate-key-pair --output-key-prefix "$tmp/other" >/dev/null 2>&1
)

private_key="$(cat "$tmp/signing.key")"

run_helper() {
    env -u RUNNER_TEMP \
        COSIGN_PRIVATE_KEY="$private_key" \
        COSIGN_PASSWORD="$throwaway_password" \
        ARMADA_SIGNING_PUBLIC_KEY="$1" \
        TMPDIR="$tmp" \
        bash "$HELPER"
}

output="$(run_helper "$tmp/signing.pub")"
[[ "$output" == *"Signing key matches"* ]] || {
    echo "FAIL: matching key pair was not reported as a match: $output" >&2
    exit 1
}

if mismatch="$(run_helper "$tmp/other.pub" 2>&1)"; then
    echo "FAIL: helper accepted a public key from a different key pair" >&2
    exit 1
fi
[[ "$mismatch" == *"is not the counterpart"* ]] || {
    echo "FAIL: mismatched key failed for an unexpected reason: $mismatch" >&2
    exit 1
}

printf 'not a public key\n' > "$tmp/malformed.pub"
if malformed="$(run_helper "$tmp/malformed.pub" 2>&1)"; then
    echo "FAIL: helper accepted a malformed trusted public key" >&2
    exit 1
fi
[[ "$malformed" == *"not a PEM public key"* ]] || {
    echo "FAIL: malformed key failed for an unexpected reason: $malformed" >&2
    exit 1
}

echo "PASS: signing key proof accepts the matching pair and rejects mismatches"
