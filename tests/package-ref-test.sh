#!/usr/bin/env bash
# Regression tests for packages/resolve-package-ref.sh.
# Uses a temporary PATH skopeo stub; no real registry or network is touched.

set -u

project_root=$(cd "$(dirname "$0")/.." && pwd)
resolver="$project_root/packages/resolve-package-ref.sh"

if [[ ! -f $resolver ]]; then
    printf 'FAIL: resolver not found: %s\n' "$resolver" >&2
    exit 1
fi

workdir=$(mktemp -d "${TMPDIR:-/tmp}/package-ref-test.XXXXXX")
cleanup() {
    rm -rf "$workdir"
}
trap cleanup EXIT

stub_dir="$workdir/bin"
mkdir -p "$stub_dir"
call_log="$workdir/calls.log"
behavior_file="$workdir/behavior"

cat >"$stub_dir/skopeo" <<'STUB'
#!/usr/bin/env bash
set -u
log="${STUB_CALL_LOG:?}"
behavior_file="${STUB_BEHAVIOR:?}"
ref=""
prev=""
for arg in "$@"; do
    if [[ $prev == "--raw" ]]; then
        ref=$arg
    fi
    prev=$arg
done
printf '%s\n' "$ref" >>"$log"
if [[ -f $behavior_file ]]; then
    while IFS= read -r line; do
        [[ -z $line ]] && continue
        pattern=${line%%|*}
        action=${line#*|}
        if [[ $ref == $pattern ]]; then
            case $action in
                ok)
                    printf '{"manifest":"ok"}\n'
                    exit 0
                    ;;
                miss)
                    printf 'time="x" level=fatal msg="reading manifest: manifest unknown: manifest unknown"\n' >&2
                    exit 1
                    ;;
                miss_code)
                    printf 'time="x" level=fatal msg="NAME_UNKNOWN: repository name not known to registry"\n' >&2
                    exit 1
                    ;;
                denied)
                    printf 'time="x" level=fatal msg="reading manifest: denied: requested access to the resource is denied"\n' >&2
                    exit 1
                    ;;
                unauthorized)
                    printf 'time="x" level=fatal msg="unauthorized: authentication required"\n' >&2
                    exit 1
                    ;;
                timeout)
                    printf 'time="x" level=fatal msg="Get https://example: dial tcp: i/o timeout"\n' >&2
                    exit 1
                    ;;
                server_error)
                    printf 'time="x" level=fatal msg="received unexpected HTTP status: 500 Internal Server Error"\n' >&2
                    exit 1
                    ;;
                notfound_html)
                    printf '<html><body>404 Not Found</body></html>\n' >&2
                    exit 1
                    ;;
                *)
                    printf 'stub: unknown action %s\n' "$action" >&2
                    exit 99
                    ;;
            esac
        fi
    done <"$behavior_file"
fi
printf 'stub: no behavior for %s\n' "$ref" >&2
exit 98
STUB
chmod +x "$stub_dir/skopeo"

unset ARMADA_PKG_REGISTRY ARMADA_PKG_FALLBACK_REGISTRY

export PATH="$stub_dir:$PATH"
export STUB_CALL_LOG="$call_log"
export STUB_BEHAVIOR="$behavior_file"

failures=0

reset_calls() {
    : >"$call_log"
}

write_behavior() {
    : >"$behavior_file"
    for entry in "$@"; do
        printf '%s\n' "$entry" >>"$behavior_file"
    done
}

run_case() {
    local name=$1 expected_status=$2 expected_stdout=$3
    shift 3
    local out err status
    out=$(bash "$resolver" "$@" 2>"$workdir/stderr.log")
    status=$?
    err=$(cat "$workdir/stderr.log")
    if [[ $status -ne $expected_status ]]; then
        printf 'FAIL [%s]: exit %s, expected %s\n' "$name" "$status" "$expected_status" >&2
        printf '  stdout: %s\n  stderr: %s\n' "$out" "$err" >&2
        failures=$((failures + 1))
        return
    fi
    if [[ $out != "$expected_stdout" ]]; then
        printf 'FAIL [%s]: stdout %q, expected %q\n' "$name" "$out" "$expected_stdout" >&2
        failures=$((failures + 1))
        return
    fi
    printf 'PASS [%s]\n' "$name"
}

assert_calls() {
    local name=$1 expected=$2
    local actual
    actual=$(cat "$call_log")
    if [[ $actual != "$expected" ]]; then
        printf 'FAIL [%s calls]: got:\n%s\nexpected:\n%s\n' "$name" "$actual" "$expected" >&2
        failures=$((failures + 1))
    fi
}

pkg=armada/pkg
hash=abc123
primary=ghcr.io/armadaos-freedom/armada/pkg
fallback=ghcr.io/armada-os/armada/pkg

# 1. Primary hit stops lookup.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|ok"
run_case "primary hit" 0 "$primary/$pkg:$hash" "$pkg" "$hash"
assert_calls "primary hit" "docker://$primary/$pkg:$hash"

# 2. Primary miss, fallback hit.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|miss" "docker://$fallback/$pkg:$hash|ok"
run_case "primary miss fallback hit" 0 "$fallback/$pkg:$hash" "$pkg" "$hash"
assert_calls "primary miss fallback hit" "docker://$primary/$pkg:$hash
docker://$fallback/$pkg:$hash"

# 3. All definite misses exit 3.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|miss" "docker://$fallback/$pkg:$hash|miss_code"
run_case "all misses" 3 "" "$pkg" "$hash"
assert_calls "all misses" "docker://$primary/$pkg:$hash
docker://$fallback/$pkg:$hash"

# 4. Permission errors with no fallback success exit 1 (not 3).
reset_calls
write_behavior "docker://$primary/$pkg:$hash|denied" "docker://$fallback/$pkg:$hash|unauthorized"
run_case "permission errors" 1 "" "$pkg" "$hash"
if ! grep -q "lookup error (status 1)" "$workdir/stderr.log"; then
    printf 'FAIL [permission errors stderr]: expected status 1 in stderr\n' >&2
    failures=$((failures + 1))
fi
assert_calls "permission errors" "docker://$primary/$pkg:$hash
docker://$fallback/$pkg:$hash"

# 5. Timeout with no fallback success exits 1.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|timeout" "docker://$fallback/$pkg:$hash|timeout"
run_case "timeouts" 1 "" "$pkg" "$hash"

# 6. Generic 404 html is not a definite miss; exit 1.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|notfound_html" "docker://$fallback/$pkg:$hash|notfound_html"
run_case "generic 404" 1 "" "$pkg" "$hash"

# 7. 5xx is a lookup error, not a miss; exit 1.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|server_error" "docker://$fallback/$pkg:$hash|server_error"
run_case "server error" 1 "" "$pkg" "$hash"

# 8. Primary error plus fallback hit succeeds and warns.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|denied" "docker://$fallback/$pkg:$hash|ok"
run_case "primary error fallback hit" 0 "$fallback/$pkg:$hash" "$pkg" "$hash"
assert_calls "primary error fallback hit" "docker://$primary/$pkg:$hash
docker://$fallback/$pkg:$hash"
if ! grep -q "lookup error" "$workdir/stderr.log"; then
    printf 'FAIL [primary error fallback hit warn]: expected lookup error warning\n' >&2
    failures=$((failures + 1))
fi

# 9. Both registry overrides respected (primary env and fallback env).
reset_calls
write_behavior "docker://reg-one.example/armada/pkg/$pkg:$hash|miss" "docker://reg-two.example/armada/pkg/$pkg:$hash|ok"
out=$(ARMADA_PKG_REGISTRY="reg-one.example/armada/pkg/" ARMADA_PKG_FALLBACK_REGISTRY="reg-two.example/armada/pkg" bash "$resolver" "$pkg" "$hash" 2>"$workdir/stderr.log")
status=$?
if [[ $status -ne 0 || $out != "reg-two.example/armada/pkg/$pkg:$hash" ]]; then
    printf 'FAIL [registry overrides]: status=%s out=%q\n' "$status" "$out" >&2
    failures=$((failures + 1))
else
    printf 'PASS [registry overrides]\n'
fi
assert_calls "registry overrides" "docker://reg-one.example/armada/pkg/$pkg:$hash
docker://reg-two.example/armada/pkg/$pkg:$hash"

# 10. Explicitly empty fallback disables fallback.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|miss"
out=$(ARMADA_PKG_FALLBACK_REGISTRY="" bash "$resolver" "$pkg" "$hash" 2>"$workdir/stderr.log")
status=$?
if [[ $status -ne 3 || -n $out ]]; then
    printf 'FAIL [disabled fallback]: status=%s out=%q expected 3/empty\n' "$status" "$out" >&2
    failures=$((failures + 1))
else
    printf 'PASS [disabled fallback]\n'
fi
assert_calls "disabled fallback" "docker://$primary/$pkg:$hash"

# 11. Duplicate registries are not queried twice.
reset_calls
write_behavior "docker://$primary/$pkg:$hash|miss" "docker://$fallback/$pkg:$hash|miss"
run_case "duplicate registry" 3 "" "$pkg" "$hash" "$primary" "$primary/" "$fallback" "$fallback/"
assert_calls "duplicate registry" "docker://$primary/$pkg:$hash
docker://$fallback/$pkg:$hash"

# 12. Usage errors exit 2.
reset_calls
out=$(bash "$resolver" 2>"$workdir/stderr.log")
status=$?
if [[ $status -ne 2 ]]; then
    printf 'FAIL [usage]: exit %s, expected 2\n' "$status" >&2
    failures=$((failures + 1))
else
    printf 'PASS [usage]\n'
fi
if [[ -s $call_log ]]; then
    printf 'FAIL [usage]: skopeo should not be called\n' >&2
    failures=$((failures + 1))
fi

if [[ $failures -ne 0 ]]; then
    printf '\n%d failure(s)\n' "$failures" >&2
    exit 1
fi

printf '\nAll package-ref tests passed.\n'
