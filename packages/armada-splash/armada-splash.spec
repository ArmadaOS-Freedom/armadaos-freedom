%global debug_package %{nil}

Name:           armada-splash
# Always update the version when you update the package
Version:        2
Release:        1%{?dist}.armada
Summary:        Boot splash for Armada
License:        GPL-2.0-or-later AND OFL-1.1
URL:            https://github.com/armada-os/armada-packages

Source0:        armada-splash.tar.gz

BuildRequires:  gcc
BuildRequires:  make
BuildRequires:  libX11-devel
BuildRequires:  libdrm-devel
BuildRequires:  python3-fonttools
BuildRequires:  google-noto-sans-cjk-fonts

%description
%{name} is the boot splash that Armada uses instead of Plymouth

%prep
%autosetup -n work

%build
%make_build

%install
%make_install

%files
%license LICENSE.md
%doc README.md
%{_datadir}/armada/splash/font.ttf
%{_prefix}/lib/dracut/modules.d/91armada-splash/*
%{_prefix}/lib/systemd/system/sddm.service.d/armada*.conf
%{_prefix}/lib/systemd/system/armada*.service
%{_libexecdir}/armada/armada*

%changelog
* Mon Aug 03 2026 Radical <radical@radical.fun> - 1-1
- Initial package
