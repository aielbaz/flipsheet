# Flipsheet

Print PDFs on both sides, even on a printer without a duplex unit, and lay out two pages per
sheet (booklet style, with right-to-left reading order for Arabic books).

- Manual duplex: prints the fronts, asks you to reload the paper, then prints the backs.
- One or two pages per side, paper size, margin and scale options.
- Live preview of every sheet side, with the printer's unprintable edge shown on it.
- Qt6 interface that follows your desktop theme (KDE Plasma/Breeze, and others).
- Right-click a PDF in Dolphin, then choose "Print with Flipsheet".

## Install (Fedora)

```
sudo dnf install ./flipsheet-0.1.0-1.*.noarch.rpm
```

Build the package yourself with `packaging/build-rpm.sh` (see below).

## Run from a checkout

PySide6 comes from your distribution so it matches the desktop's Qt and theme:

```
sudo dnf install python3-pyside6 cups-client cups-ipptool
uv venv --system-site-packages
uv sync
uv run flipsheet [file.pdf]
```

## Command line

`flipsheet-impose` builds the ready-to-print PDF without the GUI:

```
flipsheet-impose IN.pdf OUT.pdf --range 64-90 --per-side 2 --dir rtl --paper a4 --margin 7 --scale fit
```

## Build the RPM

```
sudo dnf install rpm-build pyproject-rpm-macros python3-devel desktop-file-utils
packaging/build-rpm.sh
```

The package lands in `~/rpmbuild/RPMS/noarch/`.

## License

AGPL-3.0-or-later, to match [PyMuPDF](https://pymupdf.readthedocs.io/), which it depends on.
See `LICENSE`.
