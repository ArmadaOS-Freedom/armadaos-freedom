%global source_date_epoch_from_changelog 0
%global debug_package %{nil}
%global _build_id_links none

Name:           liblhdcv5
Version:        %{upstream_version}
Release:        1%{?dist}.armada
Summary:        AOSP LHDC v5 Bluetooth audio encoder library
License:        Apache-2.0
URL:            https://github.com/DBeidachazi/liblhdcv5
Source0:        https://github.com/DBeidachazi/liblhdcv5/archive/refs/tags/v%{version}.tar.gz#/liblhdcv5-%{version}.tar.gz
Source1:        https://github.com/DBeidachazi/liblhdcv5/releases/download/v%{version}/liblhdcv5-v%{version}-linux-aarch64.so

ExclusiveArch:  aarch64

%description
Prebuilt AOSP LHDC v5 Bluetooth audio encoder shared library from
DBeidachazi/liblhdcv5, packaged for PipeWire's bluez5 LHDC v5 plugin.

%package        devel
Summary:        Development headers and pkg-config metadata for liblhdcv5
Requires:       %{name}%{?_isa} = %{version}-%{release}

%description    devel
Headers and pkg-config metadata for building against liblhdcv5.

%prep
%setup -q -n liblhdcv5-%{version}

%build
sed -e "s|@PKGVER@|%{version}|g" \
    -e "s|@PREFIX@|%{_prefix}|g" \
    -e "s|@LIBDIR@|%{_libdir}|g" \
    -e "s|@INCLUDEDIR@|%{_includedir}|g" \
    lhdcv5.pc.in > lhdcv5.pc

%install
install -Dm755 %{SOURCE1} %{buildroot}%{_libdir}/liblhdcv5.so
install -Dm644 aosp/include/lhdcv5BT.h %{buildroot}%{_includedir}/lhdcv5/lhdcv5BT.h
install -Dm644 aosp/include/lhdcv5_api.h %{buildroot}%{_includedir}/lhdcv5/lhdcv5_api.h
install -Dm644 lhdcv5.pc %{buildroot}%{_libdir}/pkgconfig/lhdcv5.pc

%post -p /sbin/ldconfig
%postun -p /sbin/ldconfig

%files
%license LICENSE-APACHE
%doc README.md
%{_libdir}/liblhdcv5.so

%files devel
%dir %{_includedir}/lhdcv5
%{_includedir}/lhdcv5/lhdcv5BT.h
%{_includedir}/lhdcv5/lhdcv5_api.h
%{_libdir}/pkgconfig/lhdcv5.pc

%changelog
