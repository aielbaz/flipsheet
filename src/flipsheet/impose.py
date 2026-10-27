"""Impose pages of a PDF N-up onto portrait sheets (for flipsheet).

usage: flipsheet-impose IN.pdf OUT.pdf --range 64-90 [--per-side 2] [--dir rtl|ltr] [--back rotated|same]
             [--paper a4|a3|a5|letter|legal] [--margin MM] [--scale fit|PERCENT]

Each output page is one *side* of a sheet. With --per-side 2 the two source pages sit side by
side in landscape and the whole layout is rotated onto a portrait page, so the output is
already in the printer's paper orientation (no printer-side rotation guessing).
  --dir rtl   first page of each pair goes on the RIGHT (Arabic books); ltr puts it on the left.
  --back      rotated: every second side (the backs) is rotated 180 deg in the file. A long-edge
                       duplex printer puts the back's top at the opposite physical edge, so this makes
                       the top of the content land on the same edge on both sides (default).
              same   : no extra rotation; use it if a test sheet shows the backs the wrong way up.
  --paper     sheet size (portrait), default a4.
  --margin    blank border on every edge in mm, default 7. Keep it clear of the printer's
              unprintable edge.
  --scale     fit (default): enlarge/shrink each page to fill the space inside the margins.
              PERCENT: scale relative to the page's own size (100 = actual size), centred. Pages
              bigger than the space are cut off at the sheet edge, so check the preview.
"""
import argparse

import pymupdf

MM = 72 / 25.4  # PDF points per millimetre
PAPERS = {  # portrait (width, height) in mm
    "a3": (297, 420),
    "a4": (210, 297),
    "a5": (148, 210),
    "letter": (215.9, 279.4),
    "legal": (215.9, 355.6),
}


def parse_range(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a or 1), int(b) if b else (int(a) if a else total)
        pages += range(lo, hi + 1)
    bad = [p for p in pages if not 1 <= p <= total]
    if bad:
        raise ValueError(f"pages out of range 1-{total}: {bad[:5]}")
    return pages


def impose(src_path: str, out_path: str, *, page_range: str = "all", per_side: int = 2,
           direction: str = "rtl", back: str = "rotated", paper: str = "a4",
           margin_mm: float = 7, scale: str = "fit") -> tuple[int, int]:
    """Build OUT from SRC as described in the module docstring.

    Returns (source pages used, sheet sides written). Raises ValueError for bad options.
    """
    n = per_side
    if n not in (1, 2):
        raise ValueError("--per-side must be 1 or 2")
    if paper not in PAPERS:
        raise ValueError(f"--paper must be one of {', '.join(PAPERS)}")
    if not 0 <= margin_mm < min(PAPERS[paper]) / 4:
        raise ValueError("--margin is out of range for this paper size")
    zoom = None  # None = fit to the space inside the margins
    if scale != "fit":
        try:
            zoom = float(scale.rstrip("%")) / 100
        except ValueError:
            raise ValueError("--scale must be 'fit' or a percentage") from None
        if not 0.1 <= zoom <= 4:
            raise ValueError("--scale must be between 10 and 400 percent")
    paper_w, paper_h = (v * MM for v in PAPERS[paper])
    margin = margin_mm * MM

    with pymupdf.open(src_path) as src:
        pages = (list(range(1, len(src) + 1)) if page_range == "all"
                 else parse_range(page_range, len(src)))

        # Landscape layout area (before rotating onto the portrait sheet)
        lw, lh = paper_h, paper_w
        out = pymupdf.open()
        for i in range(0, len(pages), n):
            group = pages[i:i + n]
            side = i // n  # 0-based index of this sheet side
            page = out.new_page(width=paper_w, height=paper_h)
            if n == 1:
                pno = group[0]
                sw, sh = src[pno - 1].rect.width, src[pno - 1].rect.height
                fit = zoom or min((paper_w - 2 * margin) / sw, (paper_h - 2 * margin) / sh)
                w, h = sw * fit, sh * fit
                x0, y0 = (paper_w - w) / 2, (paper_h - h) / 2
                page.show_pdf_page(pymupdf.Rect(x0, y0, x0 + w, y0 + h), src, pno - 1)
                continue

            # two source pages side by side, keeping aspect ratio, centred
            sw, sh = src[group[0] - 1].rect.width, src[group[0] - 1].rect.height
            fit = zoom or min((lw - 2 * margin) / (2 * sw), (lh - 2 * margin) / sh)
            w, h = sw * fit, sh * fit
            x0 = (lw - 2 * w) / 2
            y0 = (lh - h) / 2
            left, right = (x0, y0, x0 + w, y0 + h), (x0 + w, y0, x0 + 2 * w, y0 + h)
            order = [right, left] if direction == "rtl" else [left, right]

            ccw = not (back == "rotated" and side % 2 == 1)
            for pno, (ax0, ay0, ax1, ay1) in zip(group, order):
                if ccw:   # landscape top edge -> portrait left edge
                    rect, rot = pymupdf.Rect(ay0, lw - ax1, ay1, lw - ax0), 90
                else:     # landscape top edge -> portrait right edge
                    rect, rot = pymupdf.Rect(lh - ay1, ax0, lh - ay0, ax1), 270
                page.show_pdf_page(rect, src, pno - 1, rotate=rot)

        out.save(out_path, deflate=True, garbage=3)
        sides = len(out)
        out.close()
    return len(pages), sides


def main() -> None:
    ap = argparse.ArgumentParser(prog="flipsheet-impose")
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--range", default="all")
    ap.add_argument("--per-side", type=int, default=2)
    ap.add_argument("--dir", choices=["rtl", "ltr"], default="rtl")
    ap.add_argument("--back", choices=["rotated", "same"], default="rotated")
    ap.add_argument("--paper", choices=list(PAPERS), default="a4")
    ap.add_argument("--margin", type=float, default=7, help="margin in mm")
    ap.add_argument("--scale", default="fit", help="fit, or a percentage such as 100")
    a = ap.parse_args()
    try:
        used, sides = impose(a.src, a.out, page_range=a.range, per_side=a.per_side,
                             direction=a.dir, back=a.back, paper=a.paper,
                             margin_mm=a.margin, scale=a.scale)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None
    print(f"{used} source pages -> {sides} sheet sides", flush=True)


if __name__ == "__main__":
    main()
