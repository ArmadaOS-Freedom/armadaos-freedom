#!/usr/bin/env bash
# Resolve a shared package reference to a concrete content-addressed ref.
#
# Usage: bash packages/resolve-package-ref.sh PACKAGE HASH [REGISTRY ...]
#
# When no REGISTRY args are given, the primary registry defaults to
# ${ARMADA_PKG_REGISTRY:-ghcr.io/armadaos-freedom/armada/pkg} and the fallback to
# ${ARMADA_PKG_FALLBACK_REGISTRY-ghcr.io/armada-os/armada/pkg}. An explicitly
# empty fallback disables fallback lookup.
#
# Exit codes:
#   0  a ref was resolved (printed to stdout)
#   1  no ref resolved and at least one candidate had a lookup error
#   2  usage error
#   3  every candidate was definitively absent (cache miss)

set -u

if [[ $# -lt 2 ]]; then
    printf 'usage: %s PACKAGE HASH [REGISTRY ...]\n' "${0##*/}" >&2
    exit 2
fi

package=$1
hash=$2
shift 2

if [[ -z $package || -z $hash ]]; then
    printf 'error: PACKAGE and HASH must be non-empty\n' >&2
    exit 2
fi

normalize_registry() {
    local r=$1
    while [[ $r == */ ]]; do
        r=${r%/}
    done
    printf '%s' "$r"
}

registries=()
if [[ $# -gt 0 ]]; then
    for r in "$@"; do
        n=$(normalize_registry "$r")
        [[ -n $n ]] && registries+=("$n")
    done
else
    primary=${ARMADA_PKG_REGISTRY:-ghcr.io/armadaos-freedom/armada/pkg}
    fallback=${ARMADA_PKG_FALLBACK_REGISTRY-ghcr.io/armada-os/armada/pkg}
    n=$(normalize_registry "$primary")
    [[ -n $n ]] && registries+=("$n")
    n=$(normalize_registry "$fallback")
    [[ -n $n ]] && registries+=("$n")
fi

if [[ ${#registries[@]} -eq 0 ]]; then
    printf 'error: no registries to query\n' >&2
    exit 2
fi

# Deduplicate while preserving order.
unique=()
for r in "${registries[@]}"; do
    dup=0
    for u in "${unique[@]+${unique[@]}}"; do
        if [[ $u == "$r" ]]; then
            dup=1
            break
        fi
    done
    [[ $dup -eq 0 ]] && unique+=("$r")
done
registries=("${unique[@]}")

had_error=0

is_definite_miss() {
    local msg=$1
    # Explicit registry codes.
    if [[ $msg == *NAME_UNKNOWN* || $msg == *MANIFEST_UNKNOWN* ]]; then
        return 0
    fi
    # English text.
    local lower
    lower=$(printf '%s' "$msg" | tr '[:upper:]' '[:lower:]')
    if [[ $lower == *"name unknown"* || $lower == *"manifest unknown"* ]]; then
        return 0
    fi
    return 1
}

for registry in "${registries[@]}"; do
    candidate="${registry}/${package}:${hash}"
    err_file=$(mktemp "${TMPDIR:-/tmp}/resolve-package-ref.XXXXXX") || {
        printf 'error: cannot create temp file\n' >&2
        exit 1
    }
    if LC_ALL=C skopeo inspect --raw "docker://${candidate}" >/dev/null 2>"$err_file"; then
        rm -f "$err_file"
        printf '%s\n' "$candidate"
        exit 0
    else
        status=$?
    fi
    err_msg=$(cat "$err_file" 2>/dev/null || true)
    rm -f "$err_file"

    if is_definite_miss "$err_msg"; then
        printf 'cache miss: %s\n' "$candidate" >&2
    else
        had_error=1
        printf 'lookup error (status %s): %s\n' "$status" "$candidate" >&2
        if [[ -n $err_msg ]]; then
            printf '%s\n' "$err_msg" >&2
        fi
    fi
done

if [[ $had_error -eq 1 ]]; then
    printf 'error: no registry resolved %s:%s and at least one lookup failed\n' "$package" "$hash" >&2
    exit 1
fi

printf 'error: %s:%s not found in any registry\n' "$package" "$hash" >&2
exit 3
