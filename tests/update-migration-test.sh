#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/system_files/usr/lib/armada/update-lib"

assert_registry() {
    local expected="$1" ref="$2" actual
    actual="$(armada_registry_from_ref "$ref")"
    if [[ "$actual" != "$expected" ]]; then
        printf 'registry parse failed: expected %q, got %q for %q\n' \
            "$expected" "$actual" "$ref" >&2
        exit 1
    fi
}

assert_registry "$ARMADA_LEGACY_REGISTRY" \
    "ostree-image-signed:docker://${ARMADA_LEGACY_REGISTRY}:stable"
assert_registry "$ARMADA_CANONICAL_REGISTRY" \
    "ostree-image-signed:docker://${ARMADA_CANONICAL_REGISTRY}@sha256:deadbeef"
assert_registry "$ARMADA_CANONICAL_REGISTRY" \
    "docker://${ARMADA_CANONICAL_REGISTRY}:testing"
assert_registry "$ARMADA_CANONICAL_REGISTRY" \
    "ostree-unverified-registry:${ARMADA_CANONICAL_REGISTRY}:testing"
assert_registry "" \
    "ostree-image-signed:docker://example.invalid/${ARMADA_LEGACY_REGISTRY}:stable"

assert_repository() {
    local expected="$1" ref="$2" actual
    actual="$(armada_repository_from_ref "$ref" || true)"
    if [[ "$actual" != "$expected" ]]; then
        printf 'repository parse failed: expected %q, got %q for %q\n' \
            "$expected" "$actual" "$ref" >&2
        exit 1
    fi
}

assert_repository "local.armadaos.dev/armada" \
    "ostree-unverified-registry:docker://local.armadaos.dev/armada:feature-a"
assert_repository "$ARMADA_CANONICAL_REGISTRY" \
    "ostree-unverified-registry:${ARMADA_CANONICAL_REGISTRY}:testing"
assert_repository "example.invalid/${ARMADA_LEGACY_REGISTRY}" \
    "ostree-image-signed:docker://example.invalid/${ARMADA_LEGACY_REGISTRY}:stable"
assert_repository "" "containers-storage:localhost/armada:latest"
assert_repository "" "ostree-unverified-registry:docker://invalid"

armada_steam_channels_apply "$ARMADA_CANONICAL_REGISTRY" stable
armada_steam_channels_apply "$ARMADA_LEGACY_REGISTRY" beta
if armada_steam_channels_apply "$ARMADA_CANONICAL_REGISTRY" feature-a; then
    echo "non-channel tag unexpectedly uses Steam channels" >&2
    exit 1
fi
if armada_steam_channels_apply "local.armadaos.dev/armada" stable; then
    echo "external registry unexpectedly uses Steam channels" >&2
    exit 1
fi

TEST_TMP=$(mktemp -d)
trap 'rm -rf "$TEST_TMP"' EXIT
ARMADA_CHANNEL_STATE="$TEST_TMP/channel"

# Literal Freedom target: the expected repository must not be derived from the
# production constants, or a half-migrated implementation would pass by agreement.
FREEDOM=ghcr.io/armadaos-freedom/armadaos-freedom
UPSTREAM_CANONICAL=ghcr.io/armada-os/armada
UPSTREAM_LEGACY=ghcr.io/virtudude/armada

assert_channel_token() {
    local channel="$1" token="$2" actual
    actual="$(armada_channel_to_token "$channel" || true)"
    if [[ "$actual" != "$token" ]]; then
        printf 'channel token failed: expected %q, got %q for %q\n' \
            "$token" "$actual" "$channel" >&2
        exit 1
    fi
}

# The Steam UI preview token is the testing channel.
assert_channel_token stable rel
assert_channel_token beta beta
assert_channel_token testing preview

assert_target() {
    local expected_repository="$1" expected_tag="$2" state="$3" ref="$4" actual
    if [[ "$state" == - ]]; then
        rm -f "$ARMADA_CHANNEL_STATE"
    else
        printf '%s\n' "$state" > "$ARMADA_CHANNEL_STATE"
    fi
    actual="$(armada_update_target "$ref" || true)"
    if [[ "$actual" != "$expected_repository $expected_tag" ]]; then
        printf 'target selection failed: expected %q, got %q for %q\n' \
            "$expected_repository $expected_tag" "$actual" "$ref" >&2
        exit 1
    fi
}

# A normal Freedom origin resolves to Freedom only, on every supported transport.
assert_target "$FREEDOM" stable - \
    "ostree-image-signed:docker://${FREEDOM}:stable"
assert_target "$FREEDOM" beta beta \
    "ostree-image-signed:docker://${FREEDOM}:stable"
assert_target "$FREEDOM" testing - \
    "ostree-image-signed:docker://${FREEDOM}:testing"
assert_target "$FREEDOM" testing - \
    "docker://${FREEDOM}:testing"
assert_target "$FREEDOM" testing - \
    "ostree-unverified-registry:${FREEDOM}:testing"
assert_target "$FREEDOM" testing - \
    "ostree-unverified-registry:docker://${FREEDOM}:testing"

# A digest-only origin resolves only from a valid stored channel.
assert_target "$FREEDOM" beta beta \
    "ostree-image-signed:docker://${FREEDOM}@sha256:deadbeef"
assert_target "$FREEDOM" stable stable \
    "ostree-image-signed:docker://${FREEDOM}@sha256:deadbeef"

# The two old upstream origins migrate to Freedom, keeping the chosen state or
# the booted (tag) channel; the target repository is never the old upstream.
assert_target "$FREEDOM" stable - \
    "ostree-image-signed:docker://${UPSTREAM_CANONICAL}:stable"
assert_target "$FREEDOM" beta beta \
    "ostree-image-signed:docker://${UPSTREAM_CANONICAL}:stable"
assert_target "$FREEDOM" testing - \
    "ostree-image-signed:docker://${UPSTREAM_LEGACY}:testing"
assert_target "$FREEDOM" stable stable \
    "ostree-image-signed:docker://${UPSTREAM_LEGACY}@sha256:deadbeef"
assert_target "$FREEDOM" beta - \
    "ostree-unverified-registry:${UPSTREAM_CANONICAL}:beta"

assert_no_target() {
    local state="$1" ref="$2" actual
    if [[ "$state" == - ]]; then
        rm -f "$ARMADA_CHANNEL_STATE"
    else
        printf '%s\n' "$state" > "$ARMADA_CHANNEL_STATE"
    fi
    if actual=$(armada_update_target "$ref"); then
        printf 'target unexpectedly accepted: %q for %q\n' "$actual" "$ref" >&2
        exit 1
    fi
}

assert_no_target beta \
    "ostree-unverified-registry:docker://local.armadaos.dev/armada@sha256:deadbeef"
assert_no_target testing \
    "ostree-image-signed:docker://example.invalid/${UPSTREAM_LEGACY}@sha256:deadbeef"
assert_no_target beta \
    "ostree-image-signed:docker://example.invalid/${UPSTREAM_LEGACY}:stable"
assert_no_target beta \
    "ostree-image-signed:docker://example.invalid/${FREEDOM}:stable"
assert_no_target beta \
    "ostree-image-signed:docker://${FREEDOM}-extra:stable"
assert_no_target beta \
    "ostree-image-signed:docker://ghcr.io/armadaos-freedom/armadaos freedom:stable"
assert_no_target beta \
    "containers-storage:localhost/armada:latest"
assert_no_target beta \
    ""

# A known repository with an unsupported feature tag is rejected.
assert_no_target beta \
    "ostree-image-signed:docker://${FREEDOM}:feature-a"
assert_no_target beta \
    "ostree-image-signed:docker://${UPSTREAM_CANONICAL}:feature-a"
assert_no_target beta \
    "ostree-unverified-registry:docker://local.armadaos.dev/armada:feature-a"

# A digest-only origin must never guess its channel by re-querying booted state.
(
    armada_booted_channel() { echo stable; }
    assert_no_target - \
        "ostree-image-signed:docker://${FREEDOM}@sha256:deadbeef"
    assert_no_target - \
        "ostree-image-signed:docker://${UPSTREAM_CANONICAL}@sha256:deadbeef"
)

UPDATE="$ROOT/system_files/usr/libexec/armada/armada-update"
LEGACY_CLI="$ROOT/system_files/usr/lib/armada/updates/legacy_cli.py"
# The shipped entrypoint is a thin launcher that delegates the legacy update
# protocol to the Python compatibility CLI. The retired shell implementation
# and its channel-selection branch must not come back.
if grep -Fq 'armada_steam_channels_apply' "$UPDATE"; then
    echo "armada-update still contains the retired shell implementation" >&2
    exit 1
fi
grep -Fq 'export PYTHONPATH=/usr/lib/armada' "$UPDATE"
grep -Fq 'exec /usr/bin/python3 -m updates.legacy_cli "$@"' "$UPDATE"
if [[ ! -f "$LEGACY_CLI" ]]; then
    echo "delegated update module is missing: $LEGACY_CLI" >&2
    exit 1
fi

python3 - "$ROOT" <<'PY'
import json
from pathlib import Path
import sys
import tomllib

root = Path(sys.argv[1])
canonical = "ghcr.io/armada-os/armada"
legacy = "ghcr.io/virtudude/armada"
mirror_path = (
    root / "system_files/etc/containers/registries.conf.d/50-armada-migration.conf"
)

policy = json.loads(
    (root / "system_files/etc/containers/policy.json").read_text()
)
docker = policy["transports"]["docker"]
assert docker[legacy][0]["signedIdentity"] == {
    "type": "exactRepository",
    "dockerRepository": canonical,
}
assert docker[canonical][0]["signedIdentity"] == {"type": "matchRepository"}

assert mirror_path.is_file()
assert not (
    root
    / "system_files/usr/share/containers/registries.conf.d/50-armada-migration.conf"
).exists()
mirror = tomllib.loads(mirror_path.read_text())
registry = mirror["registry"][0]
assert registry["prefix"] == legacy
assert registry["location"] == legacy
assert registry["mirror"] == [{"location": canonical}]

sigstore = (
    root / "system_files/etc/containers/registries.d/ghcr-armada.yaml"
).read_text()
for repository in (legacy, canonical):
    assert f"  {repository}:\n    use-sigstore-attachments: true" in sigstore
PY

printf 'update repository migration test passed\n'
