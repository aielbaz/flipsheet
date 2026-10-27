"""flipsheet — Qt6 front end for the manual duplex workflow.

Pick a PDF (or drop one on the window), set the options, and check the live preview of
exactly what will go on each side of each sheet. Ranges and 2-up are built by impose.py.

Run it with:  flipsheet [file.pdf]   (from a checkout: uv run flipsheet [file.pdf])
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QToolButton, QVBoxLayout,
    QWidget,
)

PYMUPDF_LOCK = threading.Lock()  # MuPDF isn't thread-safe, so serialise all pymupdf work
CONFIG = (Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation))
          / "flipsheet.json")
OLD_CONFIG = CONFIG.with_name("duplex-print-gui.json")
RANGE_RE = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")
PREVIEW_DPI = 90
PAPERS = ["A4", "A3", "A5", "Letter", "Legal"]  # CUPS media names; lowercased for impose.py
MARGINS = [("None", 0), ("Small", 4), ("Normal", 7), ("Large", 12), ("Extra large", 20)]  # mm
DEFAULT_MARGIN_MM = 7
SCALES = [("Fit to page", "fit"), ("100% (actual size)", 100), ("90%", 90), ("80%", 80),
          ("70%", 70), ("50%", 50), ("Custom…", "custom")]
DEFAULT_CUSTOM_SCALE = 95


def load_impose():
    """Import the imposition module (and with it pymupdf) the first time it is needed."""
    from . import impose
    return impose


def list_printers() -> list[str]:
    """Printer names from CUPS. `ipptool` answers in ~20 ms; `lpstat -e` waits about a second
    for network discovery, so it is only the fallback."""
    try:
        out = subprocess.run(["ipptool", "-tv", "ipp://localhost/", "get-printers.test"],
                             capture_output=True, text=True, timeout=5).stdout
        names = re.findall(r"^\s*printer-name \(nameWithoutLanguage\) = (\S+)\s*$", out, re.M)
        if names:
            return names
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        return subprocess.run(["lpstat", "-e"], capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        return []


def page_count(path: str) -> int:
    try:
        with PYMUPDF_LOCK:
            import pymupdf  # deferred: only loaded once a PDF is actually opened
            with pymupdf.open(path) as doc:
                return len(doc)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Could not read that PDF. Is it a valid PDF file?") from e


def render_side(path: str, index: int) -> bytes:
    with PYMUPDF_LOCK:
        import pymupdf
        with pymupdf.open(path) as doc:
            return doc[index].get_pixmap(dpi=PREVIEW_DPI).tobytes("png")


def printable_areas(printer: str) -> dict[str, tuple[float, ...]]:
    """Per paper size, the sheet size and the unprintable border of this printer, from its PPD.

    Returns {paper: (page_w, page_h, left, bottom, right, top)} in points, where the last four
    are the border the printer cannot print on. Empty if CUPS or the PPD can't be reached
    (driverless printers have no PPD), in which case no overlay is drawn.
    """
    url = f"http://localhost:631/printers/{urllib.parse.quote(printer)}.ppd"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            ppd = r.read().decode("utf-8", "replace")
    except OSError:
        return {}
    dims = {}
    for m in re.finditer(r'^\*PaperDimension\s+([^/\s:]+)[^:]*:\s*"([^"]+)"', ppd, re.M):
        dims[m[1]] = [float(v) for v in m[2].split()]
    areas = {}
    for m in re.finditer(r'^\*ImageableArea\s+([^/\s:]+)[^:]*:\s*"([^"]+)"', ppd, re.M):
        if m[1] not in dims:
            continue
        (pw, ph), (x0, y0, x1, y1) = dims[m[1]], [float(v) for v in m[2].split()]
        areas[m[1]] = (pw, ph, x0, y0, pw - x1, ph - y1)
    return areas


def check_range(spec: str, total: int) -> str | None:
    """Return an error message, or None if the range is fine."""
    if not spec:
        return None
    if not RANGE_RE.match(spec):
        return "Use a form like 64-90 or 1-5,9"
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo > hi:
            return f"'{part.strip()}' is backwards"
        if lo < 1 or hi > total:
            return f"The PDF has only {total} pages"
    return None


def section(title: str, first: bool = False) -> QWidget:
    """A section heading: bigger bold title with a divider line above it (except the first)."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0 if first else 12, 0, 4)
    lay.setSpacing(10)
    if not first:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setForegroundRole(QPalette.ColorRole.Mid)
        lay.addWidget(line)
    lb = QLabel(title)
    f = lb.font()
    f.setBold(True)
    f.setPointSizeF(f.pointSizeF() * 1.2)
    lb.setFont(f)
    lay.addWidget(lb)
    return box


def subheading(title: str) -> QLabel:
    """A smaller heading for a group of options inside a section."""
    lb = QLabel(title)
    f = lb.font()
    f.setBold(True)
    lb.setFont(f)
    lb.setContentsMargins(0, 8, 0, 0)
    return lb


def radios(*labels: str) -> tuple[QWidget, list[QRadioButton]]:
    """A group of mutually exclusive radio buttons in their own container."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    group = QButtonGroup(box)
    buttons = []
    for text in labels:
        b = QRadioButton(text)
        group.addButton(b)
        lay.addWidget(b)
        buttons.append(b)
    buttons[0].setChecked(True)
    return box, buttons


class NoWheelComboBox(QComboBox):
    """Ignores the mouse wheel so scrolling the options column never changes a value by mistake."""

    def wheelEvent(self, e):
        e.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, e):
        e.ignore()


class PreviewLabel(QLabel):
    """Shows a pixmap scaled to fit, keeping its aspect ratio."""

    def __init__(self):
        super().__init__()
        self._pix: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setFrameShape(QFrame.Shape.StyledPanel)

    def set_pixmap(self, pix: QPixmap):
        self._pix = pix
        self._rescale()

    def _rescale(self):
        if self._pix:
            self.setPixmap(self._pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


class Runner(QObject):
    """Lets worker threads run a callable on the GUI thread."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda f: f())


class Win(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Flipsheet")
        self.resize(1000, 720)
        self.setAcceptDrops(True)
        self.runner = Runner()
        self.file: str | None = None
        self.total = 0                       # pages in the source PDF
        self.busy = False                    # a print job is running
        self.ready_cache: dict[tuple, str] = {}
        self.lock = threading.Lock()
        self.root_tmp = tempfile.mkdtemp(prefix="duplex-gui-")
        self.gen = 0                         # bumped whenever the preview must be rebuilt
        self.ready: str | None = None        # PDF currently previewed
        self.sides = 0
        self.side = 0                        # 0-based index of the previewed side
        self.png_cache: dict[tuple, bytes] = {}
        self.areas: dict[str, dict[str, tuple[float, ...]]] = {}  # printer -> printable_areas()
        self.debounce = QTimer(self, singleShot=True, interval=350)
        self.debounce.timeout.connect(self.rebuild_preview)
        self.cfg = self.load_cfg()

        self.build_ui()
        self.apply_cfg()
        self.fit_sidebar()
        threading.Thread(target=lambda: self.ui(self.set_printers, list_printers()),
                         daemon=True).start()
        self.update_state()

    def ui(self, fn, *args):
        """Run fn(*args) on the GUI thread (safe from worker threads)."""
        self.runner.call.emit(lambda: fn(*args))

    # ── config ───────────────────────────────────────────────────────────────
    def load_cfg(self) -> dict:
        for path in (CONFIG, OLD_CONFIG):  # OLD_CONFIG: settings saved under the old name
            try:
                return json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                continue
        return {}

    def save_cfg(self):
        o = self.opts()
        data: dict = {k: o[k] for k in ("per_side", "dir", "auto", "single", "flip",
                                  "paper", "margin")}
        data["show_edges"] = self.show_edges.isChecked()
        data["scale"] = self.scale.currentData()
        data["custom_scale"] = self.custom_scale.value()
        data["printer"] = o["printer"] or self.cfg.get("printer")  # keep it if never loaded
        data["invert"] = self.same.isChecked()
        data["folder"] = str(Path(self.file).parent) if self.file else self.cfg.get("folder")
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(data))
        except OSError:
            pass

    def apply_cfg(self):
        c = self.cfg
        (self.per1 if c.get("per_side") == 1 else self.per2).setChecked(True)
        (self.ltr if c.get("dir") == "ltr" else self.rtl).setChecked(True)
        self.same.setChecked(bool(c.get("invert")))
        if c.get("paper") in PAPERS:
            self.paper.setCurrentText(c["paper"])
        if isinstance(c.get("margin"), (int, float)):
            i = self.margin.findData(c["margin"])
            if i >= 0:
                self.margin.setCurrentIndex(i)
        self.show_edges.setChecked(c.get("show_edges", True))
        i = self.scale.findData(c.get("scale"))
        if i >= 0:
            self.scale.setCurrentIndex(i)
        if isinstance(c.get("custom_scale"), (int, float)):
            self.custom_scale.setValue(int(c["custom_scale"]))
        (self.short_edge if c.get("flip") == "short" else self.long_edge).setChecked(True)
        (self.single if c.get("single") else self.auto if c.get("auto")
         else self.manual).setChecked(True)
        self.sync_sensitivity()

    # ── UI ───────────────────────────────────────────────────────────────────
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # top bar
        header = QWidget()
        header.setAutoFillBackground(True)
        pal = header.palette()
        pal.setColor(QPalette.ColorRole.Window, pal.color(QPalette.ColorRole.AlternateBase))
        header.setPalette(pal)
        bar = QHBoxLayout(header)
        bar.setContentsMargins(14, 10, 14, 10)
        bar.setSpacing(12)
        open_btn = QPushButton("Open PDF…")
        open_btn.clicked.connect(self.on_browse)
        self.save_btn = QPushButton("Save PDF…")
        self.save_btn.clicked.connect(self.on_save)
        self.print_btn = QPushButton("Print")
        self.print_btn.setDefault(True)
        self.print_btn.clicked.connect(self.on_print)
        bar.addWidget(open_btn)
        bar.addStretch()
        bar.addWidget(self.save_btn)
        bar.addWidget(self.print_btn)
        outer.addWidget(header)
        header_line = QFrame()
        header_line.setFrameShape(QFrame.Shape.HLine)
        header_line.setFrameShadow(QFrame.Shadow.Plain)
        header_line.setForegroundRole(QPalette.ColorRole.Mid)
        outer.addWidget(header_line)

        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # ── left: options ──
        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(12, 4, 12, 12)
        left.setSpacing(4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(left_w)
        split.addWidget(scroll)
        split.setChildrenCollapsible(False)
        self.side_scroll, self.side_content, self.side_layout = scroll, left_w, left

        # ── Printer ──
        left.addWidget(section("Printer", first=True))
        self.printers: list[str] = []
        self.printers_loaded = False         # filled in the background after start-up
        self.printer = NoWheelComboBox()
        self.printer.setPlaceholderText("Loading printers…")
        self.printer.setEnabled(False)
        left.addWidget(self.printer)
        self.printer.currentIndexChanged.connect(self.on_printer_changed)

        left.addWidget(subheading("Duplex mode"))
        box, (self.manual, self.auto, self.single) = radios(
            "Manual: print fronts, reload paper, print backs",
            "Automatic: printer flips the paper itself",
            "One side only: one sheet per side, no flipping")
        self.auto.setToolTip("For printers with a built-in duplex unit. Sends one job "
                             "with sides=two-sided-long-edge or -short-edge.")
        left.addWidget(box)

        self.flip_box = QWidget()
        fb = QVBoxLayout(self.flip_box)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.addWidget(subheading("Flip pages on"))
        box, (self.long_edge, self.short_edge) = radios("Long edge (like a book)",
                                                        "Short edge (like a notepad)")
        fb.addWidget(box)
        left.addWidget(self.flip_box)

        # ── Pages ──
        left.addWidget(section("Pages"))
        self.range = QLineEdit()
        self.range.setPlaceholderText("All pages   (or e.g. 64-90, 1-5,9)")
        self.range.setClearButtonEnabled(True)
        self.range.textChanged.connect(self.on_option_changed)
        left.addWidget(self.range)
        self.range_msg = QLabel("")
        self.range_msg.setStyleSheet("color: #da4453;")
        self.range_msg.setWordWrap(True)
        self.range_msg.setVisible(False)
        left.addWidget(self.range_msg)

        left.addWidget(subheading("Pages per side"))
        box, (self.per1, self.per2) = radios("One page per side",
                                             "Two pages per side (side by side)")
        self.per2.setChecked(True)
        left.addWidget(box)

        self.order_box = QWidget()
        ob = QVBoxLayout(self.order_box)
        ob.setContentsMargins(0, 0, 0, 0)
        ob.addWidget(subheading("Reading order"))
        box, (self.rtl, self.ltr) = radios("Right to left (Arabic): first page on the right",
                                           "Left to right: first page on the left")
        ob.addWidget(box)
        left.addWidget(self.order_box)

        # ── Page setup ──
        left.addWidget(section("Page setup"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)  # label above its field
        form.setVerticalSpacing(4)
        self.paper = NoWheelComboBox()
        self.paper.addItems(PAPERS)
        form.addRow("Paper size", self.paper)
        self.margin = NoWheelComboBox()
        for name, mm in MARGINS:
            self.margin.addItem(f"{name} ({mm} mm)", mm)
        self.margin.setCurrentIndex(self.margin.findData(DEFAULT_MARGIN_MM))
        self.margin.setToolTip("Blank border on every edge. Keep it clear of the printer's "
                               "unprintable edge.")
        form.addRow("Margin", self.margin)
        self.scale = NoWheelComboBox()
        for name, value in SCALES:
            self.scale.addItem(name, value)
        self.scale.setToolTip("Fit to page fills the space inside the margins. A percentage is "
                              "relative to the page's own size; anything too big is cut off.")
        form.addRow("Scale", self.scale)
        self.custom_scale = NoWheelSpinBox()
        self.custom_scale.setRange(10, 400)
        self.custom_scale.setValue(DEFAULT_CUSTOM_SCALE)
        self.custom_scale.setSuffix(" %")
        self.custom_label = QLabel("Custom scale")
        form.addRow(self.custom_label, self.custom_scale)
        left.addLayout(form)
        self.show_edges = QCheckBox("Show the printer's unprintable edge")
        self.show_edges.setChecked(True)
        self.show_edges.toggled.connect(lambda *_: self.goto(self.side))
        left.addWidget(self.show_edges)
        self.edge_label = QLabel("")
        self.edge_label.setWordWrap(True)
        self.edge_label.setEnabled(False)
        left.addWidget(self.edge_label)
        for w in (self.paper, self.margin, self.scale):
            w.currentIndexChanged.connect(self.on_option_changed)
        self.paper.currentIndexChanged.connect(lambda *_: self.update_edge_info(self.opts()))
        self.custom_scale.valueChanged.connect(self.on_option_changed)

        # ── Advanced ──
        left.addWidget(section("Advanced"))
        self.same = QCheckBox("Backs come out upside down? Turn them 180°")
        self.same.setToolTip(
            "The backs are rotated automatically to suit the flip edge. Tick this if a\n"
            "test sheet still comes out with the backs upside down (2 per side only).")
        self.dry = QCheckBox("Dry run: show commands, print nothing")
        left.addWidget(self.same)
        left.addWidget(self.dry)
        left.addStretch()

        for w in (self.per1, self.per2, self.rtl, self.ltr, self.same, self.manual, self.auto,
                  self.single, self.long_edge, self.short_edge):
            w.toggled.connect(self.on_option_changed)

        # ── right: preview ──
        right_w = QWidget()
        right = QVBoxLayout(right_w)
        right.setContentsMargins(12, 4, 12, 8)
        split.addWidget(right_w)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([360, 640])

        self.stack = QStackedWidget()
        self.picture = PreviewLabel()
        self.empty = QLabel("The preview appears here once you open a PDF.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setEnabled(False)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(240)
        busy_page = QWidget()
        bl = QVBoxLayout(busy_page)
        bl.addStretch()
        bl.addWidget(self.busy_bar, alignment=Qt.AlignmentFlag.AlignCenter)
        bl.addStretch()
        self.err_label = QLabel()
        self.err_label.setWordWrap(True)
        self.err_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.err_label.setStyleSheet("color: #da4453;")
        self.pages = {"pic": self.picture, "empty": self.empty, "busy": busy_page,
                      "err": self.err_label}
        for w in self.pages.values():
            self.stack.addWidget(w)
        self.show_page("empty")
        right.addWidget(self.stack, 1)

        self.info_label = QLabel("")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setEnabled(False)
        self.info_label.setVisible(False)    # shown once a PDF is open
        right.addWidget(self.info_label)

        self.nav_box = QWidget()
        self.nav_box.setVisible(False)       # shown once a PDF is open
        nav = QHBoxLayout(self.nav_box)
        nav.setContentsMargins(0, 0, 0, 0)
        self.prev_btn = QPushButton("◀")
        self.next_btn = QPushButton("▶")
        self.prev_btn.clicked.connect(lambda: self.goto(self.side - 1))
        self.next_btn.clicked.connect(lambda: self.goto(self.side + 1))
        self.nav_label = QLabel("")
        self.nav_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.nav_label.setMinimumWidth(260)
        nav.addStretch()
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.nav_label)
        nav.addWidget(self.next_btn)
        nav.addStretch()
        right.addWidget(self.nav_box)

        # ── bottom: status + log ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(sep)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(12, 4, 12, 4)
        self.status = QLabel("Ready")
        self.status.setMinimumWidth(100)
        status_row.addWidget(self.status, 1)
        details = QToolButton()
        details.setText("Details")
        details.setCheckable(True)
        details.setArrowType(Qt.ArrowType.RightArrow)
        details.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details.setAutoRaise(True)
        status_row.addWidget(details)
        outer.addLayout(status_row)
        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumHeight(130)
        self.log_view.setVisible(False)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)

        def toggle_details(on):
            self.log_view.setVisible(on)
            details.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        details.toggled.connect(toggle_details)
        outer.addWidget(self.log_view)

    def fit_sidebar(self):
        """Make the options column wide enough for its content so it never scrolls sideways."""
        self.side_layout.activate()
        need = self.side_content.minimumSizeHint().width()
        bar = self.side_scroll.verticalScrollBar().sizeHint().width()
        self.side_scroll.setMinimumWidth(max(340, need + bar + 4))

    def show_page(self, name: str):
        self.stack.setCurrentWidget(self.pages[name])

    # ── state ────────────────────────────────────────────────────────────────
    def opts(self) -> dict[str, Any]:
        return dict(
            printer=self.printer.currentText(),
            range=self.range.text().strip(),
            paper=self.paper.currentText(),
            margin=self.margin.currentData(),
            scale=self.scale_value(),
            per_side=2 if self.per2.isChecked() else 1,
            dir="rtl" if self.rtl.isChecked() else "ltr",
            flip="short" if self.short_edge.isChecked() else "long",
            # 2-up pages sit sideways on the sheet, so the backs need turning for a long-edge
            # flip but not for a short-edge one; the Advanced box inverts that.
            back="rotated" if (self.long_edge.isChecked() != self.same.isChecked()
                               and not self.single.isChecked()) else "same",
            dry=self.dry.isChecked(),
            auto=self.auto.isChecked(),
            single=self.single.isChecked(),
        )

    def scale_value(self) -> str:
        """'fit' or a percentage, as impose.py's --scale wants it."""
        v = self.scale.currentData()
        return str(self.custom_scale.value() if v == "custom" else v)

    def sync_sensitivity(self):
        custom = self.scale.currentData() == "custom"
        self.custom_scale.setVisible(custom)
        self.custom_label.setVisible(custom)
        self.order_box.setVisible(self.per2.isChecked())
        self.same.setEnabled(self.per2.isChecked() and not self.single.isChecked())
        self.flip_box.setVisible(not self.single.isChecked())

    def set_status(self, text: str):
        self.status.setText(text)

    def log(self, text: str):
        self.ui(self.log_view.appendPlainText, text)

    def update_state(self):
        ok = bool(self.file) and not self.busy and self.ready is not None
        self.print_btn.setEnabled(ok and bool(self.printers))
        self.save_btn.setEnabled(ok)

    def set_file(self, path: str | None):
        if not path or not path.lower().endswith(".pdf"):
            self.set_status("That is not a PDF file.")
            return
        try:
            self.total = page_count(path)
        except Exception as e:  # noqa: BLE001
            self.set_status(str(e))
            return
        self.file = path
        self.nav_box.setVisible(True)
        self.cfg["folder"] = str(Path(path).parent)
        self.setWindowTitle(f"{Path(path).name} — Flipsheet")
        self.info_label.setToolTip(path)
        self.info_label.setText(f"{Path(path).name}  ·  {self.total} pages")
        self.info_label.setVisible(True)
        self.on_option_changed()
        if self.debounce.isActive():         # a new file needs no typing delay
            self.debounce.stop()
            self.rebuild_preview()

    def on_option_changed(self, *_):
        self.sync_sensitivity()
        if not self.file:
            return
        err = check_range(self.range.text().strip(), self.total)
        self.range.setStyleSheet("border: 1px solid #da4453;" if err else "")
        self.range_msg.setText(err or "")
        self.range_msg.setVisible(bool(err))
        self.gen += 1
        self.ready = None
        self.update_state()
        self.debounce.stop()
        if err:
            self.show_page("empty")
            self.nav_label.setText("")
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            return
        self.show_page("busy")
        self.debounce.start()

    # ── building the ready-to-print PDF ──────────────────────────────────────
    def get_ready(self, o) -> str:
        """Build (or reuse) the PDF with range / 2-up applied. Safe to call from a worker."""
        src, rng = self.file, o["range"]
        assert src
        key = (src, os.path.getmtime(src), rng, o["per_side"], o["paper"], o["margin"], o["scale"],
               o["dir"] if o["per_side"] == 2 else "", o["back"] if o["per_side"] == 2 else "")
        with self.lock:
            if key in self.ready_cache and os.path.exists(self.ready_cache[key]):
                return self.ready_cache[key]
            d = tempfile.mkdtemp(dir=self.root_tmp)
            ready = os.path.join(d, "ready.pdf")
            try:
                with PYMUPDF_LOCK:
                    load_impose().impose(
                        src, ready, page_range=rng or "all", per_side=o["per_side"],
                        direction=o["dir"], back=o["back"], paper=o["paper"].lower(),
                        margin_mm=o["margin"], scale=o["scale"])
            except ValueError as e:
                raise RuntimeError(str(e)) from None
            self.ready_cache[key] = ready
            return ready

    def rebuild_preview(self):
        gen, o = self.gen, self.opts()
        self.set_status("Building preview…")

        def work():
            try:
                if o["printer"]:
                    self.load_areas(o["printer"])
                ready = self.get_ready(o)
                n = page_count(ready)
            except Exception as e:  # noqa: BLE001
                self.ui(self.preview_failed, gen, str(e))
                return
            self.ui(self.preview_ready, gen, ready, n)
        threading.Thread(target=work, daemon=True).start()

    def preview_failed(self, gen, msg):
        if gen != self.gen:
            return
        self.err_label.setText(msg)
        self.show_page("err")
        self.set_status("Could not build the preview.")

    def preview_ready(self, gen, ready, n):
        if gen != self.gen:
            return
        self.ready, self.sides = ready, n
        self.side = min(self.side, n - 1)
        sheets = n if self.single.isChecked() else (n + 1) // 2
        self.info_label.setText(f"{Path(self.file or '').name}  ·  {self.total} pages  ·  printing {n} "
                                f"side{'s' if n != 1 else ''} on {sheets} sheet"
                                f"{'s' if sheets != 1 else ''}")
        self.set_status("Ready")
        self.update_state()
        self.goto(self.side)

    # ── preview navigation ───────────────────────────────────────────────────
    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.goto(self.side - 1)
        elif e.key() in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.goto(self.side + 1)
        else:
            super().keyPressEvent(e)

    def goto(self, i: int):
        if not self.ready or not self.sides:
            return
        i = max(0, min(self.sides - 1, i))
        self.side = i
        o = self.opts()
        if o["single"]:
            self.nav_label.setText(f"Sheet {i + 1} of {self.sides}")
        else:
            back = i % 2 == 1
            what = "back" if back else "front"
            if back and o["per_side"] == 2 and o["back"] == "rotated":
                what += " (turned 180°)"
            self.nav_label.setText(f"Sheet {i // 2 + 1} of {(self.sides + 1) // 2}  ·  {what}")
        self.prev_btn.setEnabled(i > 0)
        self.next_btn.setEnabled(i < self.sides - 1)
        key = (self.ready, i)
        if key in self.png_cache:
            self.show_png(self.png_cache[key])
            return
        gen, ready = self.gen, self.ready
        self.show_page("busy")

        def work():
            try:
                data = render_side(ready, i)
            except Exception as e:  # noqa: BLE001
                self.ui(self.preview_failed, gen, str(e))
                return

            def done():
                self.png_cache[key] = data
                if gen == self.gen and self.side == i:
                    self.show_png(data)
            self.ui(done)
        threading.Thread(target=work, daemon=True).start()

    def load_areas(self, printer: str):
        if printer not in self.areas:
            self.areas[printer] = printable_areas(printer)

    def set_printers(self, names: list[str]):
        """Fill the printer selector once CUPS has answered: the saved printer if it still
        exists, otherwise the first one."""
        self.printers, self.printers_loaded = names, True
        self.printer.blockSignals(True)
        self.printer.clear()
        self.printer.addItems(names)
        saved = self.cfg.get("printer")
        self.printer.setCurrentIndex(names.index(saved) if saved in names else (0 if names else -1))
        self.printer.blockSignals(False)
        self.printer.setEnabled(bool(names))
        if not names:
            self.printer.setPlaceholderText("No printers found (is CUPS running?)")
        self.update_state()
        self.refresh_edges()
        if self.ready:
            self.goto(self.side)

    def on_printer_changed(self, *_):
        printer = self.printer.currentText()
        if not printer:
            return

        def work():
            self.load_areas(printer)
            self.ui(self.goto, self.side)
        threading.Thread(target=work, daemon=True).start()
        self.refresh_edges()

    def refresh_edges(self):
        """Look up the printer's unprintable edge and update the description (no PDF needed)."""
        printer = self.printer.currentText()

        def work():
            if printer:
                self.load_areas(printer)
            self.ui(lambda: self.update_edge_info(self.opts()))
        threading.Thread(target=work, daemon=True).start()

    def edge_widths(self, o) -> tuple[float, float, float, float] | None:
        """Unprintable border (left, top, right, bottom) in points as the printer sees this
        side, or None if unknown."""
        area = self.areas.get(o["printer"], {}).get(o["paper"])
        if not area:
            return None
        _pw, _ph, left, bottom, right, top = area
        # the manual-duplex back pass is sent rotated 180°, which swaps opposite borders
        if (not o["single"] and not o["auto"] and o["flip"] == "long" and self.side % 2 == 1):
            left, right, top, bottom = right, left, bottom, top
        return left, top, right, bottom

    def update_edge_info(self, o):
        if not self.printers_loaded:
            self.edge_label.setText("")
            return
        area = self.areas.get(o["printer"], {}).get(o["paper"])
        if not area:
            known = o["printer"] in self.areas
            self.edge_label.setText(
                f"{o['printer']} does not list {o['paper']} paper." if known and self.areas[o["printer"]]
                else "The printer's unprintable edge is not available.")
            return
        mm = 25.4 / 72
        _pw, _ph, left, bottom, right, top = area
        self.edge_label.setText(f"The printer can't print within {max(left, right) * mm:.1f} mm "
                                f"of the sides or {max(top, bottom) * mm:.1f} mm of the top and "
                                f"bottom ({o['paper']}).")

    def show_png(self, data: bytes):
        pix = QPixmap()
        pix.loadFromData(data)
        o = self.opts()
        self.update_edge_info(o)
        edges = self.edge_widths(o) if self.show_edges.isChecked() else None
        if edges:
            area = self.areas[o["printer"]][o["paper"]]
            sx, sy = pix.width() / area[0], pix.height() / area[1]
            l, t, r, b = edges[0] * sx, edges[1] * sy, edges[2] * sx, edges[3] * sy
            w, h = pix.width(), pix.height()
            p = QPainter(pix)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(218, 68, 83, 90))
            for x, y, bw, bh in ((0, 0, w, t), (0, h - b, w, b), (0, t, l, h - t - b),
                                 (w - r, t, r, h - t - b)):
                p.drawRect(int(x), int(y), int(bw + 0.999), int(bh + 0.999))
            pen = QPen(QColor(218, 68, 83))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(int(l), int(t), int(w - l - r), int(h - t - b))
            p.end()
        self.picture.set_pixmap(pix)
        self.show_page("pic")

    # ── file dialogs ─────────────────────────────────────────────────────────
    def on_browse(self):
        folder = self.cfg.get("folder") or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a PDF", folder if os.path.isdir(folder) else "", "PDF files (*.pdf)")
        if path:
            self.set_file(path)

    def on_save(self):
        dest, _ = QFileDialog.getSaveFileName(self, "Save ready-to-print PDF",
                                              "ready-to-print.pdf", "PDF files (*.pdf)")
        if not dest or not self.ready:
            return
        shutil.copyfile(self.ready, dest)
        self.log(f"Saved: {dest} (not printed)")
        self.set_status(f"Saved {Path(dest).name}")

    # ── drag & drop a PDF onto the window ────────────────────────────────────
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if urls:
            self.set_file(urls[0].toLocalFile())

    # ── printing ─────────────────────────────────────────────────────────────
    def on_print(self):
        o, ready = self.opts(), self.ready
        if not ready or self.busy:
            return
        self.busy = True
        self.update_state()
        self.save_cfg()

        def go():
            try:
                self.print_job(o, ready)
            except Exception as e:  # noqa: BLE001
                self.log(f"Error: {e}")
                self.ui(self.set_status, "Printing failed.")
                self.ui(QMessageBox.critical, self, "Printing failed", str(e))
            finally:
                def fin():
                    self.busy = False
                    self.update_state()
                self.ui(fin)
        threading.Thread(target=go, daemon=True).start()

    def print_job(self, o, ready):
        if not shutil.which("lp"):
            raise RuntimeError("'lp' not found. Is CUPS installed?")
        n = page_count(ready)
        odd, even = (n + 1) // 2, n // 2
        self.log(f"File: {self.file}   Printer: {o['printer']}   "
                 f"Sides: {n} ({n if o['single'] else odd} sheet(s))")
        if o["dry"]:
            self.log("Dry run: nothing will be printed.")
        lp = ["lp", "-d", o["printer"], "-o", f"media={o['paper']}"]
        if o["single"]:
            cmd = lp + ["-o", "sides=one-sided", ready]
            self.ui(self.set_status, f"Printing {n} sheet(s), one side only…")
            self.log("One-sided: " + " ".join(cmd))
            if not o["dry"]:
                self.run_lp(cmd)
            self.log("Done. Job sent to the printer.")
            self.ui(self.set_status, "Done. Job sent to the printer.")
            return
        if o["auto"]:
            cmd = lp + ["-o", f"sides=two-sided-{o['flip']}-edge", ready]
            self.ui(self.set_status, f"Printing both sides ({odd} sheets)…")
            self.log("Automatic duplex: " + " ".join(cmd))
            if not o["dry"]:
                self.run_lp(cmd)
            msg = "Done. Job sent to the printer."
            if o["per_side"] == 2:
                msg += " Backs upside down? See Advanced."
            self.log(msg)
            self.ui(self.set_status, msg)
            return
        cmd_odd = lp + ["-o", "page-set=odd", ready]
        cmd_even = lp + ["-o", "page-set=even"]
        if o["flip"] == "long":
            cmd_even += ["-o", "orientation-requested=6"]
        cmd_even += ["-o", "outputorder=reverse", ready]

        self.ui(self.set_status, f"Step 1 of 2: printing the fronts ({odd} sheets)…")
        self.log("Step 1: " + " ".join(cmd_odd))
        if not o["dry"]:
            self.run_lp(cmd_odd)
        if even == 0:
            self.ui(self.set_status, "Done. Only one page; nothing for the back.")
            return

        ev, answer = threading.Event(), {"go": False}

        def ask():
            box = QMessageBox(self)
            box.setWindowTitle("Reload the paper")
            box.setText("Reload the paper")
            box.setInformativeText(
                "1. Wait until the printer has finished.\n"
                "2. Put the printed pages back in the tray exactly as they came out "
                "(don't flip or rotate them).\n"
                "3. Click “Print the backs”.")
            go_btn = box.addButton("Print the backs", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(go_btn)
            box.exec()
            answer["go"] = box.clickedButton() is go_btn
            ev.set()
        self.ui(self.set_status, "Waiting for you to reload the paper…")
        self.ui(ask)
        ev.wait()
        if not answer["go"]:
            self.log("Cancelled before printing the backs.")
            self.ui(self.set_status, "Cancelled before printing the backs.")
            return

        self.ui(self.set_status, f"Step 2 of 2: printing the backs ({even} sheets)…")
        self.log("Step 2: " + " ".join(cmd_even))
        if not o["dry"]:
            self.run_lp(cmd_even)
        msg = "Done. Both sides sent to the printer."
        if o["per_side"] == 2:
            msg += " Backs upside down? See Advanced."
        self.log(msg)
        self.ui(self.set_status, msg)

    def run_lp(self, cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "lp failed")
        self.log(r.stdout.strip())

    def closeEvent(self, e):
        self.save_cfg()
        shutil.rmtree(self.root_tmp, ignore_errors=True)
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("flipsheet")
    # Lets the desktop match the window to flipsheet.desktop, which gives the taskbar its icon
    QGuiApplication.setDesktopFileName("flipsheet")
    # The installed icon if there is one, otherwise the copy bundled with the package
    app.setWindowIcon(QIcon.fromTheme("flipsheet", QIcon(str(Path(__file__).with_name("flipsheet.svg")))))
    win = Win()
    win.show()
    if len(sys.argv) > 1:
        win.set_file(os.path.abspath(sys.argv[1]))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
