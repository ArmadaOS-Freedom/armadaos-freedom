%global forgeurl https://github.com/linux-msm/hexagonrpc
%global shortcommit %(c=%{commit}; echo ${c:0:7})
%global source_date_epoch_from_changelog 0

Name:           hexagonrpc
Version:        %{upstream_version}^%{snapshot}git%{shortcommit}
Release:        1%{?dist}.armada
Summary:        FastRPC reverse tunnel daemon for Qualcomm Hexagon DSPs

License:        GPL-3.0-or-later
URL:            %{forgeurl}
Source0:        %{forgeurl}/archive/%{commit}/%{name}-%{commit}.tar.gz

Patch1:         0001-rpcd-listener-fetch-large-input-buffers-with-get_in_.patch
Patch2:         0002-rpcd-apps_std-optional-writable-sensor-registry.patch

BuildRequires:  gcc
BuildRequires:  meson

%description
hexagonrpcd serves the files and calls that a Qualcomm Hexagon DSP requests
from the application processor over FastRPC, such as the sensor hub's
configuration and registry. This build adds large input buffers and an
optional writable sensor registry (HEXAGONRPCD_WRITABLE_REGISTRY).

%prep
%autosetup -n %{name}-%{commit} -p1

%build
%meson
%meson_build

%install
%meson_install
# Upstream's units start hexagonrpcd on any device with a non-secure ADSP or
# SDSP node, as a user this image does not create. Boards that need the daemon
# ship their own unit, so none is started by the package.
rm -f %{buildroot}%{_libdir}/systemd/system/hexagonrpcd-*.service
# No headers are installed, so the unversioned development link is unused.
rm -f %{buildroot}%{_libdir}/libhexagonrpc.so

%check
%meson_test

%files
%license COPYING
%doc README.md
%{_bindir}/hexagonrpcd
%{_libdir}/libhexagonrpc.so.0.5
%{_libexecdir}/hexagonrpc/chrecd
%{_mandir}/man1/hexagonrpcd.1*

%changelog
