%global forgeurl https://gitlab.freedesktop.org/hadess/iio-sensor-proxy
%global source_date_epoch_from_changelog 0

Name:           iio-sensor-proxy
Version:        %{upstream_version}
Release:        1%{?dist}.armada
Summary:        IIO accelerometer sensor to input device proxy

License:        GPL-3.0-or-later
URL:            %{forgeurl}
Source0:        %{forgeurl}/-/archive/%{commit}/%{name}-%{commit}.tar.gz

BuildRequires:  gcc
BuildRequires:  meson
BuildRequires:  systemd-rpm-macros
BuildRequires:  pkgconfig(gio-2.0)
BuildRequires:  pkgconfig(gudev-1.0)
BuildRequires:  pkgconfig(libssc)
BuildRequires:  pkgconfig(polkit-gobject-1)
BuildRequires:  pkgconfig(systemd)
BuildRequires:  pkgconfig(udev)

%description
iio-sensor-proxy exposes accelerometers, ambient light, proximity and compass
sensors over D-Bus (net.hadess.SensorProxy). This build enables the libssc
backend for sensors on a Qualcomm Sensor Core (SSC); IIO sensors work as in
the distribution package.

%prep
%autosetup -n %{name}-%{commit} -p1

%build
%meson \
    -Dssc-support=enabled \
    -Dgeoclue-user=geoclue \
    -Dtests=false \
    -Dgtk-tests=false \
    -Dgtk_doc=false
%meson_build

%install
%meson_install
# Upstream lists ssc-light and ssc-compass on every ADSP/SDSP FastRPC node,
# which also starts the proxy there. Without hexagonrpcd serving the DSP the
# SSC never answers and each sensor waits 30 s, so boards opt in with their own
# rule instead (TB710FU: 81-armada-tb710fu-sensors.rules).
rules=%{buildroot}%{_udevrulesdir}/80-iio-sensor-proxy.rules
test "$(grep -c '^SUBSYSTEM=="misc", KERNEL=="fastrpc-' "$rules")" -eq 2
sed -i '/^SUBSYSTEM=="misc", KERNEL=="fastrpc-/d' "$rules"
! grep -q 'ssc-' "$rules"

%post
%systemd_post iio-sensor-proxy.service

%preun
%systemd_preun iio-sensor-proxy.service

%postun
%systemd_postun_with_restart iio-sensor-proxy.service

%files
%license COPYING
%doc README.md
%{_bindir}/monitor-sensor
%{_libexecdir}/iio-sensor-proxy
%{_unitdir}/iio-sensor-proxy.service
%{_udevrulesdir}/80-iio-sensor-proxy.rules
%{_datadir}/dbus-1/system.d/net.hadess.SensorProxy.conf
%{_datadir}/polkit-1/actions/net.hadess.SensorProxy.policy

%changelog
