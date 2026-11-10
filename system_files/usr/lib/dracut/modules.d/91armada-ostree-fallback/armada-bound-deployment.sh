#!/bin/bash
# Select only a bootlink explicitly bound to an existing deployment.

set -u
set -f

die() { echo "armada-bound-deployment: $*" >&2; exit 1; }

[ "$#" -eq 2 ] || die "expected cmdline and sysroot"
cmdline=$1

[ -d "$2" ] || die "sysroot is not an existing directory"
case "$2" in /*) ;; *) die "sysroot is not absolute" ;; esac
sysroot=$(readlink -f -- "$2") || die "cannot canonicalize sysroot"
[ -d "${sysroot}" ] || die "canonical sysroot is not a directory"
root=${sysroot%/}

deployments=0
ostree_args=0
binding=""
karg=""
IFS=$' \t\n\v\f\r'
for t in ${cmdline}; do
    case "${t}" in
        armada.deployment|ostree)
            die "bare deployment or ostree argument"
            ;;
        armada.deployment=*)
            deployments=$((deployments + 1))
            binding=${t#armada.deployment=}
            ;;
        ostree=*)
            ostree_args=$((ostree_args + 1))
            karg=${t#ostree=}
            ;;
    esac
done
[ "${deployments}" -eq 1 ] || die "expected exactly one deployment binding"
[ "${ostree_args}" -eq 1 ] || die "expected exactly one ostree argument"

if [[ "${binding}" =~ ^([A-Za-z0-9][A-Za-z0-9_.-]{0,63})/([0-9a-f]{64})\.(0|[1-9][0-9]*)$ ]]; then
    osname=${BASH_REMATCH[1]}
    commit=${BASH_REMATCH[2]}
    serial=${BASH_REMATCH[3]}
else
    die "invalid deployment binding"
fi

if [[ "${karg}" =~ ^/ostree/boot\.([01])/([^/]+)/([^/]+)/([^/]+)$ ]]; then
    [ "${BASH_REMATCH[2]}" = "${osname}" ] || die "ostree osname does not match binding"
    bootcsum=${BASH_REMATCH[3]}
    original_index=${BASH_REMATCH[4]}
else
    die "invalid ostree path"
fi
[[ "${bootcsum}" =~ ^[0-9a-f]{64}$ ]] || die "invalid boot checksum"
[[ "${original_index}" =~ ^(0|[1-9][0-9]*)$ ]] || die "invalid boot index"

expected="${root}/ostree/deploy/${osname}/deploy/${commit}.${serial}"
[ -d "${expected}" ] || die "bound deployment is missing"
resolved=$(readlink -e -- "${expected}") || die "cannot resolve bound deployment"
[ "${resolved}" = "${expected}" ] || die "bound deployment is not its canonical path"

matches() {
    local link=$1 target
    [ -L "${link}" ] || return 1
    target=$(readlink -e -- "${link}" 2>/dev/null) || return 1
    [ "${target}" = "${expected}" ]
}

if matches "${root}${karg}"; then
    printf '%s\n' "${karg}"
    exit 0
fi

for b in boot.0 boot.1; do
    candidate="/ostree/${b}/${osname}/${bootcsum}/${original_index}"
    if matches "${root}${candidate}"; then
        printf '%s\n' "${candidate}"
        exit 0
    fi
done

set +f
for b in boot.0 boot.1; do
    for e in "${root}/ostree/${b}/${osname}/${bootcsum}"/*; do
        [ -e "${e}" ] || [ -L "${e}" ] || continue
        index=${e##*/}
        [[ "${index}" =~ ^(0|[1-9][0-9]*)$ ]] || continue
        candidate="/ostree/${b}/${osname}/${bootcsum}/${index}"
        if matches "${e}"; then
            printf '%s\n' "${candidate}"
            exit 0
        fi
    done
done

die "no bootlink resolves to the bound deployment"
