Name:           flipsheet
Version:        0.1.0
Release:        1%{?dist}
Summary:        Manual duplex printing and booklet layout for single-sided printers
License:        AGPL-3.0-or-later
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils

# PySide6 is not a pip dependency of the project (it must match the desktop's Qt)
Requires:       python3-pyside6
Requires:       cups-client
Requires:       cups-ipptool

%description
Flipsheet prints PDFs on both sides even on printers without a duplex unit: it prints
the fronts, waits while you reload the paper, then prints the backs. It can also lay out two
pages per side (booklet style, with right-to-left reading order), with paper size, margin and
scale options and a live preview that shows the printer's unprintable edge.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files flipsheet
install -Dpm 0644 data/flipsheet.desktop \
    %{buildroot}%{_datadir}/applications/flipsheet.desktop
install -Dpm 0644 data/flipsheet-servicemenu.desktop \
    %{buildroot}%{_datadir}/kio/servicemenus/flipsheet.desktop
install -Dpm 0644 src/flipsheet/flipsheet.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/flipsheet.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/flipsheet.desktop
%pyproject_check_import

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/flipsheet
%{_bindir}/flipsheet-impose
%{_datadir}/applications/flipsheet.desktop
%{_datadir}/kio/servicemenus/flipsheet.desktop
%{_datadir}/icons/hicolor/scalable/apps/flipsheet.svg

%changelog
* Tue Oct 27 2026 Flipsheet contributors - 0.1.0-1
- Initial package
