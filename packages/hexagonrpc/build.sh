#!/usr/bin/bash
# Runs inside the builder container. See ../build-local.sh for the contract.
set -euxo pipefail

source ./BASE.env

rm -rf out
mkdir -p out

export HOME=/tmp
dnf -y install rpm-build rpmdevtools spectool "dnf-command(builddep)"
rpmdev-setuptree
cat >/etc/rpm/macros.armada <<MACROS
%_buildhost armada-builder
%packager Armada
%vendor Armada
MACROS

defines=(
    --define "commit ${COMMIT}"
    --define "upstream_version ${VERSION}"
    --define "snapshot ${SNAPSHOT}"
)
cp /work/hexagonrpc.spec ~/rpmbuild/SPECS/
cp /work/patches/*.patch ~/rpmbuild/SOURCES/
spectool -g -R "${defines[@]}" ~/rpmbuild/SPECS/hexagonrpc.spec
dnf -y builddep "${defines[@]}" ~/rpmbuild/SPECS/hexagonrpc.spec
rpmbuild -bb "${defines[@]}" ~/rpmbuild/SPECS/hexagonrpc.spec
cp ~/rpmbuild/RPMS/*/hexagonrpc-[0-9]*.armada.*.rpm /work/out/
