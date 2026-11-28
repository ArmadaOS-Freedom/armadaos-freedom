#!/usr/bin/bash
# Runs inside the builder container. See ../build-local.sh for the contract.
# Packages the pinned upstream liblhdcv5 release binary + headers first,
# installs them in the builder, then rebuilds Fedora's pipewire SRPM with
# LHDC v5 enabled.
set -euxo pipefail

source ./BASE.env

REST="${SRPM#pipewire-}"
PW_VER="${REST%%-*}"
PW_REL="${REST#*-}"
PW_REL="${PW_REL%.fc*}"
DIST=".fc44.armada"

rm -rf out
mkdir -p out

export HOME=/tmp
dnf -y install rpm-build rpmdevtools koji "dnf-command(builddep)" git-core curl
rpmdev-setuptree
cat >/etc/rpm/macros.armada <<EOF
%_buildhost armada-builder
%packager Armada
%vendor Armada
EOF

SRC_TARBALL="liblhdcv5-${LHDCV5_VERSION}.tar.gz"
SO_BIN="liblhdcv5-v${LHDCV5_VERSION}-linux-aarch64.so"
curl -fL --retry 3 \
    -o "$HOME/rpmbuild/SOURCES/${SRC_TARBALL}" \
    "https://github.com/DBeidachazi/liblhdcv5/archive/refs/tags/v${LHDCV5_VERSION}.tar.gz"
curl -fL --retry 3 \
    -o "$HOME/rpmbuild/SOURCES/${SO_BIN}" \
    "https://github.com/DBeidachazi/liblhdcv5/releases/download/v${LHDCV5_VERSION}/${SO_BIN}"
(
    cd "$HOME/rpmbuild/SOURCES"
    printf '%s  %s\n' "${LHDCV5_SRC_SHA256}" "${SRC_TARBALL}" | sha256sum -c -
    printf '%s  %s\n' "${LHDCV5_SO_AARCH64_SHA256}" "${SO_BIN}" | sha256sum -c -
)

cp /work/liblhdcv5.spec "$HOME/rpmbuild/SPECS/"
rpmbuild -bb --define "upstream_version ${LHDCV5_VERSION}" --define "dist ${DIST}" \
    "$HOME/rpmbuild/SPECS/liblhdcv5.spec"
dnf -y install \
    "$HOME"/rpmbuild/RPMS/*/liblhdcv5-[0-9]*.aarch64.rpm \
    "$HOME"/rpmbuild/RPMS/*/liblhdcv5-devel-[0-9]*.aarch64.rpm

cd /tmp
koji download-build --arch=src "${SRPM}"
rpm -i "${SRPM}.src.rpm"
SPEC="$HOME/rpmbuild/SPECS/pipewire.spec"

# Force the stock numeric release so .fc44.armada sorts just above the base
# build, whatever release macro the spec uses.
sed -i "s/^Release:.*/Release:        ${PW_REL}%{?dist}/" "$SPEC"
sed -i "/^%autochangelog/d" "$SPEC"

LAST=$(grep -nE "^(Patch|Source)[0-9]*:" "$SPEC" | tail -1 | cut -d: -f1)
[ -n "$LAST" ] || { echo "ERROR: no Source/Patch line to anchor on"; exit 1; }
n=9000
for patch in /work/patches/*.patch; do
    n=$((n + 1))
    cp "$patch" "$HOME/rpmbuild/SOURCES/"
    sed -i "${LAST}a Patch${n}:       ${patch##*/}" "$SPEC"
    LAST=$((LAST + 1))
done

grep -q "^BuildRequires:  pkgconfig(fdk-aac)" "$SPEC" \
    || { echo "ERROR: pipewire.spec missing pkgconfig(fdk-aac) BuildRequires"; exit 1; }
sed -i "/^BuildRequires:  pkgconfig(fdk-aac)/a BuildRequires:  pkgconfig(lhdcv5)" "$SPEC"

grep -q -- "-D bluez5-codec-ldac-dec=disabled" "$SPEC" \
    || { echo "ERROR: pipewire.spec missing -D bluez5-codec-ldac-dec=disabled"; exit 1; }
sed -i "/-D bluez5-codec-ldac-dec=disabled/a \\    -D bluez5-codec-lhdc=enabled \\\\" "$SPEC"

grep -qE "^[[:space:]]*%(autosetup|autopatch)" "$SPEC" \
    || { echo "ERROR: pipewire.spec does not auto-apply patches; adjust build.sh"; exit 1; }

dnf -y builddep "$SPEC"
rpmbuild -bb --define "dist ${DIST}" "$SPEC"

cp "$HOME"/rpmbuild/RPMS/*/liblhdcv5-[0-9]*.aarch64.rpm /work/out/
for rpm in "$HOME"/rpmbuild/RPMS/*/pipewire*-"${PW_VER}-${PW_REL}${DIST}".*.rpm; do
    case "${rpm##*/}" in
        pipewire-devel-*|pipewire-doc-*|pipewire-jack-audio-connection-kit-devel-*|*-debuginfo-*|*-debugsource-*)
            continue
            ;;
    esac
    cp "$rpm" /work/out/
done
