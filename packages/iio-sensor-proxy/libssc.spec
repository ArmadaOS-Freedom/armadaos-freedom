%global forgeurl https://codeberg.org/DylanVanAssche/libssc
%global shortcommit %(c=%{commit}; echo ${c:0:7})
%global source_date_epoch_from_changelog 0

Name:           libssc
Version:        %{upstream_version}^%{snapshot}git%{shortcommit}
Release:        1%{?dist}.armada
Summary:        Library to expose Qualcomm Sensor Core (SSC) sensors

License:        GPL-3.0-or-later
URL:            %{forgeurl}
Source0:        %{forgeurl}/archive/%{commit}.tar.gz#/%{name}-%{commit}.tar.gz

BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  pkgconfig(gio-unix-2.0)
BuildRequires:  pkgconfig(glib-2.0)
BuildRequires:  pkgconfig(libprotobuf-c)
BuildRequires:  pkgconfig(qmi-glib)
BuildRequires:  protobuf-c-compiler
BuildRequires:  protobuf-compiler
# The mock SSC server for libssc's tests is configured unconditionally.
BuildRequires:  python3-devel

%description
libssc talks QMI over QRTR to the Qualcomm Sensor Core (SSC) on the DSP and
exposes its accelerometer, gyroscope, magnetometer, light and proximity
sensors. ssccli reads them from the command line.

%package        devel
Summary:        Development files for libssc
Requires:       %{name}%{?_isa} = %{version}-%{release}

%description    devel
Headers and pkg-config files for building against libssc.

%prep
%autosetup -n %{name} -p1

%build
%meson
%meson_build

%install
%meson_install
# The mock SSC server and the installed tests are for libssc's own test suite.
rm -rf %{buildroot}%{_libexecdir}/installed-tests \
       %{buildroot}%{_includedir}/libssc/mocking \
       %{buildroot}%{python3_sitelib} \
       %{buildroot}%{_datadir}/installed-tests

%files
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/ssccli
%{_libdir}/libssc.so.*

%files devel
%{_includedir}/libssc/
%{_libdir}/libssc.so
%{_libdir}/pkgconfig/libssc*.pc

%changelog
