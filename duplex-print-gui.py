#!/usr/bin/python3
"""duplex-print-gui — GTK4 front end for the manual duplex workflow.

Pick a PDF (or drop one on the window), set the options, and check the live preview of
exactly what will go on each side of each sheet. Ranges and 2-up are built by duplex-impose.py via `uv`.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

SCRIPT_DIR = Path(os.path.realpath(__file__)).parent
IMPOSE = SCRIPT_DIR / "duplex-impose.py"
DEFAULT_PRINTER = "HP_LaserJet_Professional_P1102"
CONFIG = Path(GLib.get_user_config_dir()) / "duplex-print-gui.json"
RANGE_RE = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")
PREVIEW_DPI = 90


def list_printers() -> list[str]:
    try:
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    return out.split()


def page_count(path: str) -> int:
    out = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise RuntimeError("Could not read that PDF. Is it a valid PDF file?")


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


def section(title: str) -> Gtk.Label:
    lb = Gtk.Label(label=title, xalign=0, margin_top=8)
    lb.add_css_class("heading")
    return lb


class Win(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Duplex Print")
        self.set_default_size(980, 700)
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
        self.png_cache: dict[tuple, str] = {}
        self.debounce_id = 0
        self.cfg = self.load_cfg()

        self.build_ui()
        self.apply_cfg()
        self.update_state()

    # ── config ───────────────────────────────────────────────────────────────
    def load_cfg(self) -> dict:
        try:
            return json.loads(CONFIG.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def save_cfg(self):
        o = self.opts()
        data = {k: o[k] for k in ("printer", "per_side", "dir", "auto", "single", "flip")}
        data["invert"] = self.same.get_active()
        data["folder"] = str(Path(self.file).parent) if self.file else self.cfg.get("folder")
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(data))
        except OSError:
            pass

    def apply_cfg(self):
        c = self.cfg
        if c.get("printer") in self.printers:
            self.printer.set_selected(self.printers.index(c["printer"]))
        (self.per1 if c.get("per_side") == 1 else self.per2).set_active(True)
        (self.ltr if c.get("dir") == "ltr" else self.rtl).set_active(True)
        self.same.set_active(bool(c.get("invert")))
        (self.short_edge if c.get("flip") == "short" else self.long_edge).set_active(True)
        (self.single if c.get("single") else self.auto if c.get("auto")
         else self.manual).set_active(True)
        self.sync_sensitivity()

    # ── UI ───────────────────────────────────────────────────────────────────
    def build_ui(self):
        # header bar
        hb = Gtk.HeaderBar()
        self.set_titlebar(hb)
        self.print_btn = Gtk.Button(label="Print")
        self.print_btn.add_css_class("suggested-action")
        self.print_btn.connect("clicked", self.on_print)
        hb.pack_end(self.print_btn)
        self.save_btn = Gtk.Button(label="Save PDF…")
        self.save_btn.connect("clicked", self.on_save)
        hb.pack_end(self.save_btn)
        open_btn = Gtk.Button(label="Open PDF…")
        open_btn.connect("clicked", self.on_browse)
        hb.pack_start(open_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)
        paned = Gtk.Box(spacing=0, vexpand=True)
        outer.append(paned)

        # ── left: settings ──
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, width_request=310)
        for side in ("top", "bottom", "start", "end"):
            getattr(left, f"set_margin_{side}")(14)
        paned.append(left)
        paned.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.file_label = Gtk.Label(label="Drop a PDF here, or click Open PDF…", xalign=0,
                                    wrap=True, margin_bottom=4)
        self.file_label.add_css_class("title-4")
        left.append(self.file_label)
        self.info_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.info_label.add_css_class("dim-label")
        left.append(self.info_label)

        left.append(section("Printer"))
        self.printers = list_printers() or [DEFAULT_PRINTER]
        self.printer = Gtk.DropDown.new_from_strings(self.printers)
        if DEFAULT_PRINTER in self.printers:
            self.printer.set_selected(self.printers.index(DEFAULT_PRINTER))
        left.append(self.printer)

        left.append(section("Pages to print"))
        self.range = Gtk.Entry(placeholder_text="All pages   (or e.g. 64-90, 1-5,9)")
        self.range.connect("changed", self.on_option_changed)
        left.append(self.range)
        self.range_msg = Gtk.Label(label="", xalign=0, wrap=True)
        self.range_msg.add_css_class("error")
        left.append(self.range_msg)

        left.append(section("Layout"))
        self.per1 = Gtk.CheckButton(label="One page per side")
        self.per2 = Gtk.CheckButton(label="Two pages per side (side by side)", group=self.per1)
        self.per2.set_active(True)
        left.append(self.per1)
        left.append(self.per2)

        self.order_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.order_box.append(section("Reading order"))
        self.rtl = Gtk.CheckButton(label="Right to left (Arabic): first page on the right")
        self.ltr = Gtk.CheckButton(label="Left to right: first page on the left", group=self.rtl)
        self.rtl.set_active(True)
        self.order_box.append(self.rtl)
        self.order_box.append(self.ltr)
        left.append(self.order_box)

        left.append(section("Paper"))
        self.manual = Gtk.CheckButton(label="Manual: print fronts, reload paper, print backs")
        self.auto = Gtk.CheckButton(label="Automatic: printer flips the paper itself", group=self.manual)
        self.auto.set_tooltip_text("For printers with a built-in duplex unit. Sends one job "
                                   "with sides=two-sided-long-edge or -short-edge.")
        self.single = Gtk.CheckButton(label="One side only: one sheet per side, no flipping",
                                      group=self.manual)
        self.manual.set_active(True)
        left.append(self.manual)
        left.append(self.auto)
        left.append(self.single)

        self.flip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.flip_box.append(section("Flip pages on"))
        self.long_edge = Gtk.CheckButton(label="Long edge (like a book)")
        self.short_edge = Gtk.CheckButton(label="Short edge (like a notepad)", group=self.long_edge)
        self.long_edge.set_active(True)
        self.flip_box.append(self.long_edge)
        self.flip_box.append(self.short_edge)
        left.append(self.flip_box)

        adv = Gtk.Expander(label="Advanced", margin_top=10)
        advbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6)
        self.same = Gtk.CheckButton(label="Backs come out upside down? Turn them 180°")
        self.same.set_tooltip_text(
            "The backs are rotated automatically to suit the flip edge. Tick this if a\n"
            "test sheet still comes out with the backs upside down (2 per side only).")
        self.dry = Gtk.CheckButton(label="Dry run: show commands, print nothing")
        advbox.append(self.same)
        advbox.append(self.dry)
        adv.set_child(advbox)
        left.append(adv)

        for w in (self.per1, self.per2, self.rtl, self.ltr, self.same, self.manual, self.auto,
                  self.single, self.long_edge, self.short_edge):
            w.connect("toggled", self.on_option_changed)

        # ── right: preview ──
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        for side in ("top", "bottom", "start", "end"):
            getattr(right, f"set_margin_{side}")(14)
        paned.append(right)

        self.stack = Gtk.Stack(vexpand=True, hexpand=True)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        self.picture.add_css_class("card")
        self.stack.add_named(self.picture, "pic")
        empty = Gtk.Label(label="The preview appears here once you open a PDF.")
        empty.add_css_class("dim-label")
        self.stack.add_named(empty, "empty")
        spin = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                           width_request=32, height_request=32)
        self.stack.add_named(spin, "busy")
        self.err_label = Gtk.Label(wrap=True)
        self.err_label.add_css_class("error")
        self.stack.add_named(self.err_label, "err")
        self.stack.set_visible_child_name("empty")
        right.append(self.stack)

        nav = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
        self.next_btn = Gtk.Button(icon_name="go-next-symbolic")
        self.prev_btn.connect("clicked", lambda *_: self.goto(self.side - 1))
        self.next_btn.connect("clicked", lambda *_: self.goto(self.side + 1))
        self.nav_label = Gtk.Label(label="", width_chars=34)
        nav.append(self.prev_btn)
        nav.append(self.nav_label)
        nav.append(self.next_btn)
        right.append(nav)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        # ── bottom: status + log ──
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.append(Gtk.Separator())
        self.status = Gtk.Label(label="Ready", xalign=0, margin_start=14, margin_end=14,
                                margin_top=6, margin_bottom=6, ellipsize=3)
        bottom.append(self.status)
        exp = Gtk.Expander(label="Details", margin_start=14, margin_end=14, margin_bottom=8)
        self.buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.buf, editable=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sw = Gtk.ScrolledWindow(min_content_height=110, child=self.log_view)
        sw.add_css_class("card")
        exp.set_child(sw)
        bottom.append(exp)
        outer.append(bottom)

        # drag & drop a PDF onto the window
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", lambda _t, f, *_: self.set_file(f.get_path()) or True)
        self.add_controller(drop)

        self.connect("close-request", self.on_close)

    # ── state ────────────────────────────────────────────────────────────────
    def opts(self):
        return dict(
            printer=self.printers[self.printer.get_selected()],
            range=self.range.get_text().strip(),
            per_side=2 if self.per2.get_active() else 1,
            dir="rtl" if self.rtl.get_active() else "ltr",
            flip="short" if self.short_edge.get_active() else "long",
            # 2-up pages sit sideways on the sheet, so the backs need turning for a long-edge
            # flip but not for a short-edge one; the Advanced box inverts that.
            back="rotated" if (self.long_edge.get_active() != self.same.get_active()
                               and not self.single.get_active()) else "same",
            dry=self.dry.get_active(),
            auto=self.auto.get_active(),
            single=self.single.get_active(),
        )

    def sync_sensitivity(self):
        self.order_box.set_visible(self.per2.get_active())
        self.same.set_sensitive(self.per2.get_active() and not self.single.get_active())
        self.flip_box.set_visible(not self.single.get_active())

    def set_status(self, text: str):
        self.status.set_text(text)

    def log(self, text: str):
        def _do():
            self.buf.insert(self.buf.get_end_iter(), text + "\n")
            mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
            self.log_view.scroll_mark_onscreen(mark)
        GLib.idle_add(_do)

    def update_state(self):
        ok = bool(self.file) and not self.busy and self.ready is not None
        self.print_btn.set_sensitive(ok)
        self.save_btn.set_sensitive(ok)

    def set_file(self, path: str):
        if not path or not path.lower().endswith(".pdf"):
            self.set_status("That is not a PDF file.")
            return
        try:
            self.total = page_count(path)
        except Exception as e:  # noqa: BLE001
            self.set_status(str(e))
            return
        self.file = path
        self.cfg["folder"] = str(Path(path).parent)
        self.file_label.set_text(Path(path).name)
        self.file_label.set_tooltip_text(path)
        self.info_label.set_text(f"{self.total} pages")
        self.on_option_changed()

    def on_option_changed(self, *_):
        self.sync_sensitivity()
        if not self.file:
            return
        spec = self.range.get_text().strip()
        err = check_range(spec, self.total)
        if err:
            self.range.add_css_class("error")
            self.range_msg.set_text(err)
        else:
            self.range.remove_css_class("error")
            self.range_msg.set_text("")
        self.gen += 1
        self.ready = None
        self.update_state()
        if self.debounce_id:
            GLib.source_remove(self.debounce_id)
            self.debounce_id = 0
        if err:
            self.stack.set_visible_child_name("empty")
            self.nav_label.set_text("")
            self.prev_btn.set_sensitive(False)
            self.next_btn.set_sensitive(False)
            return
        self.stack.set_visible_child_name("busy")
        self.debounce_id = GLib.timeout_add(350, self.rebuild_preview)

    # ── building the ready-to-print PDF ──────────────────────────────────────
    def get_ready(self, o) -> str:
        """Build (or reuse) the PDF with range / 2-up applied. Safe to call from a worker."""
        src, rng = self.file, o["range"]
        if o["per_side"] == 1 and not rng:
            return src
        key = (src, os.path.getmtime(src), rng, o["per_side"],
               o["dir"] if o["per_side"] == 2 else "", o["back"] if o["per_side"] == 2 else "")
        with self.lock:
            if key in self.ready_cache and os.path.exists(self.ready_cache[key]):
                return self.ready_cache[key]
            d = tempfile.mkdtemp(dir=self.root_tmp)
            ready = os.path.join(d, "ready.pdf")
            uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
            if not os.path.exists(uv) or not IMPOSE.exists():
                raise RuntimeError(f"Preparing the PDF needs 'uv' and {IMPOSE}")
            cmd = [uv, "run", "--quiet", "--project", str(SCRIPT_DIR), str(IMPOSE), src, ready,
                   "--range", rng or "all", "--per-side", str(o["per_side"]),
                   "--dir", o["dir"], "--back", o["back"]]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "Preparing the PDF failed")
            self.ready_cache[key] = ready
            return ready

    def rebuild_preview(self):
        self.debounce_id = 0
        gen, o = self.gen, self.opts()
        self.set_status("Building preview…")

        def work():
            try:
                ready = self.get_ready(o)
                n = page_count(ready)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.preview_failed, gen, str(e))
                return
            GLib.idle_add(self.preview_ready, gen, ready, n)
        threading.Thread(target=work, daemon=True).start()
        return False

    def preview_failed(self, gen, msg):
        if gen != self.gen:
            return
        self.err_label.set_text(msg)
        self.stack.set_visible_child_name("err")
        self.set_status("Could not build the preview.")

    def preview_ready(self, gen, ready, n):
        if gen != self.gen:
            return
        self.ready, self.sides = ready, n
        self.side = min(self.side, n - 1)
        sheets = n if self.single.get_active() else (n + 1) // 2
        self.info_label.set_text(f"{self.total} pages in the file  ·  printing {n} "
                                 f"side{'s' if n != 1 else ''} on {sheets} sheet"
                                 f"{'s' if sheets != 1 else ''}")
        self.set_status("Ready")
        self.update_state()
        self.goto(self.side)

    # ── preview navigation ───────────────────────────────────────────────────
    def on_key(self, _c, keyval, *_):
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up):
            self.goto(self.side - 1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down):
            self.goto(self.side + 1)
            return True
        return False

    def goto(self, i: int):
        if not self.ready or not self.sides:
            return
        i = max(0, min(self.sides - 1, i))
        self.side = i
        o = self.opts()
        if o["single"]:
            self.nav_label.set_text(f"Sheet {i + 1} of {self.sides}")
            sheet = None
        else:
            sheet, back = i // 2 + 1, i % 2 == 1
        if sheet is not None:
            what = "back" if back else "front"
            if back and o["per_side"] == 2 and o["back"] == "rotated":
                what += " (turned 180°)"
            self.nav_label.set_text(f"Sheet {sheet} of {(self.sides + 1) // 2}  ·  {what}")
        self.prev_btn.set_sensitive(i > 0)
        self.next_btn.set_sensitive(i < self.sides - 1)
        key = (self.ready, i)
        if key in self.png_cache and os.path.exists(self.png_cache[key]):
            self.show_png(self.png_cache[key])
            return
        gen, ready = self.gen, self.ready
        self.stack.set_visible_child_name("busy")

        def work():
            prefix = os.path.join(self.root_tmp, f"{abs(hash(ready))}-side{i}")
            r = subprocess.run(["pdftoppm", "-png", "-r", str(PREVIEW_DPI), "-f", str(i + 1),
                                "-l", str(i + 1), "-singlefile", ready, prefix],
                               capture_output=True, text=True)
            if r.returncode != 0:
                GLib.idle_add(self.preview_failed, gen, r.stderr.strip() or "pdftoppm failed")
                return

            def done():
                self.png_cache[key] = prefix + ".png"
                if gen == self.gen and self.side == i:
                    self.show_png(prefix + ".png")
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def show_png(self, path):
        self.picture.set_filename(path)
        self.stack.set_visible_child_name("pic")

    # ── file dialogs ─────────────────────────────────────────────────────────
    def on_browse(self, _b):
        flt = Gtk.FileFilter()
        flt.set_name("PDF files")
        flt.add_mime_type("application/pdf")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(flt)
        dlg = Gtk.FileDialog(title="Choose a PDF", filters=filters)
        folder = self.cfg.get("folder") or str(SCRIPT_DIR)
        if os.path.isdir(folder):
            dlg.set_initial_folder(Gio.File.new_for_path(folder))

        def done(d, res):
            try:
                self.set_file(d.open_finish(res).get_path())
            except GLib.Error:
                pass
        dlg.open(self, None, done)

    def on_save(self, _b):
        dlg = Gtk.FileDialog(title="Save ready-to-print PDF", initial_name="ready-to-print.pdf")

        def done(d, res):
            try:
                dest = d.save_finish(res).get_path()
            except GLib.Error:
                return
            shutil.copyfile(self.ready, dest)
            self.log(f"Saved: {dest} (not printed)")
            self.set_status(f"Saved {Path(dest).name}")
        dlg.save(self, None, done)

    # ── printing ─────────────────────────────────────────────────────────────
    def on_print(self, _b):
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
                GLib.idle_add(self.set_status, "Printing failed.")
                GLib.idle_add(lambda: Gtk.AlertDialog(message="Printing failed",
                                                      detail=str(e)).show(self))
            finally:
                def fin():
                    self.busy = False
                    self.update_state()
                GLib.idle_add(fin)
        threading.Thread(target=go, daemon=True).start()

    def print_job(self, o, ready):
        if not shutil.which("lp"):
            raise RuntimeError("'lp' not found. Is CUPS installed?")
        n = page_count(ready)
        odd, even = (n + 1) // 2, n // 2
        self.log(f"File: {self.file}   Printer: {o['printer']}   Sides: {n} ({n if o['single'] else odd} sheet(s))")
        if o["dry"]:
            self.log("Dry run: nothing will be printed.")
        if o["single"]:
            cmd = ["lp", "-d", o["printer"], "-o", "sides=one-sided", ready]
            GLib.idle_add(self.set_status, f"Printing {n} sheet(s), one side only…")
            self.log("One-sided: " + " ".join(cmd))
            if not o["dry"]:
                self.run_lp(cmd)
            self.log("Done. Job sent to the printer.")
            GLib.idle_add(self.set_status, "Done. Job sent to the printer.")
            return
        if o["auto"]:
            cmd = ["lp", "-d", o["printer"], "-o", f"sides=two-sided-{o['flip']}-edge", ready]
            GLib.idle_add(self.set_status, f"Printing both sides ({odd} sheets)…")
            self.log("Automatic duplex: " + " ".join(cmd))
            if not o["dry"]:
                self.run_lp(cmd)
            msg = "Done. Job sent to the printer."
            if o["per_side"] == 2:
                msg += " Backs upside down? See Advanced."
            self.log(msg)
            GLib.idle_add(self.set_status, msg)
            return
        cmd_odd = ["lp", "-d", o["printer"], "-o", "page-set=odd", ready]
        cmd_even = ["lp", "-d", o["printer"], "-o", "page-set=even"]
        if o["flip"] == "long":
            cmd_even += ["-o", "orientation-requested=6"]   # as the CLI does
        cmd_even += ["-o", "outputorder=reverse", ready]

        GLib.idle_add(self.set_status, f"Step 1 of 2: printing the fronts ({odd} sheets)…")
        self.log("Step 1: " + " ".join(cmd_odd))
        if not o["dry"]:
            self.run_lp(cmd_odd)
        if even == 0:
            GLib.idle_add(self.set_status, "Done. Only one page; nothing for the back.")
            return

        ev, answer = threading.Event(), {"go": False}

        def ask():
            dlg = Gtk.AlertDialog(
                message="Reload the paper",
                detail="1. Wait until the printer has finished.\n"
                       "2. Put the printed pages back in the tray exactly as they came out "
                       "(don't flip or rotate them).\n"
                       "3. Click “Print the backs”.",
                buttons=["Cancel", "Print the backs"], cancel_button=0, default_button=1)

            def done(d, res):
                try:
                    answer["go"] = d.choose_finish(res) == 1
                except GLib.Error:
                    pass
                ev.set()
            dlg.choose(self, None, done)
        GLib.idle_add(self.set_status, "Waiting for you to reload the paper…")
        GLib.idle_add(ask)
        ev.wait()
        if not answer["go"]:
            self.log("Cancelled before printing the backs.")
            GLib.idle_add(self.set_status, "Cancelled before printing the backs.")
            return

        GLib.idle_add(self.set_status, f"Step 2 of 2: printing the backs ({even} sheets)…")
        self.log("Step 2: " + " ".join(cmd_even))
        if not o["dry"]:
            self.run_lp(cmd_even)
        msg = "Done. Both sides sent to the printer."
        if o["per_side"] == 2:
            msg += " Backs upside down? See Advanced."
        self.log(msg)
        GLib.idle_add(self.set_status, msg)

    def run_lp(self, cmd):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "lp failed")
        self.log(r.stdout.strip())

    def on_close(self, *_):
        self.save_cfg()
        shutil.rmtree(self.root_tmp, ignore_errors=True)
        return False


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.duplexprint.gui",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_activate(self):
        (self.props.active_window or Win(self)).present()

    def do_open(self, files, n_files, hint):
        win = self.props.active_window or Win(self)
        win.present()
        win.set_file(files[0].get_path())


if __name__ == "__main__":
    App().run()
