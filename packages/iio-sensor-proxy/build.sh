#!/usr/bin/bash
# Runs inside the builder container. See ../build-local.sh for the contract.
# Builds libssc first, installs it in the builder, then builds iio-sensor-proxy
# against it; both runtime RPMs go to ./out.
set -euxo pipefail

source ./BASE.env

rm -rf out
mkdir -p out

export HOME=/tmp
dnf -y install rpm-build rpmdevtools spectool "dnf-command(builddep)" python3-rpm-macros
rpmdev-setuptree
cat >/etc/rpm/macros.armada <<MACROS
%_buildhost armada-builder
%packager Armada
%vendor Armada
MACROS

build() {
    local spec=$1
    shift
    cp "/work/${spec}" ~/rpmbuild/SPECS/
    spectool -g -R "$@" ~/rpmbuild/SPECS/"${spec}"
    dnf -y builddep "$@" ~/rpmbuild/SPECS/"${spec}"
    rpmbuild -bb "$@" ~/rpmbuild/SPECS/"${spec}"
}

build libssc.spec \
    --define "commit ${LIBSSC_COMMIT}" \
    --define "upstream_version ${LIBSSC_VERSION}" \
    --define "snapshot ${LIBSSC_SNAPSHOT}"
dnf -y install ~/rpmbuild/RPMS/*/libssc-[0-9]*.armada.*.rpm \
    ~/rpmbuild/RPMS/*/libssc-devel-[0-9]*.armada.*.rpm

build iio-sensor-proxy.spec \
    --define "commit ${IIO_SENSOR_PROXY_COMMIT}" \
    --define "upstream_version ${IIO_SENSOR_PROXY_VERSION}"

cp ~/rpmbuild/RPMS/*/libssc-[0-9]*.armada.*.rpm \
    ~/rpmbuild/RPMS/*/iio-sensor-proxy-[0-9]*.armada.*.rpm /work/out/
