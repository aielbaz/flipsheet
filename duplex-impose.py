"""Impose pages of a PDF N-up onto portrait A4 sheets (for duplex-print).

usage: duplex-impose.py IN.pdf OUT.pdf --range 64-90 [--per-side 2] [--dir rtl|ltr] [--back rotated|same]

Each output page is one *side* of a sheet. With --per-side 2 the two source pages sit side by
side in landscape and the whole layout is rotated onto a portrait A4 page, so the output is
already in the printer's paper orientation (no printer-side rotation guessing).
  --dir rtl   first page of each pair goes on the RIGHT (Arabic books); ltr puts it on the left.
  --back      rotated: every second side (the backs) is rotated 180 deg in the file. A long-edge
                       duplex printer puts the back's top at the opposite physical edge, so this makes
                       the top of the content land on the same edge on both sides (default).
              same   : no extra rotation; use it if a test sheet shows the backs the wrong way up.
"""
import argparse

import pymupdf

A4_W, A4_H = 595.276, 841.89
MARGIN = 20  # keep clear of the printer's unprintable edge


def parse_range(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        lo, hi = int(a or 1), int(b) if b else (int(a) if a else total)
        pages += range(lo, hi + 1)
    bad = [p for p in pages if not 1 <= p <= total]
    if bad:
        raise SystemExit(f"error: pages out of range 1-{total}: {bad[:5]}")
    return pages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--range", default="all")
    ap.add_argument("--per-side", type=int, default=2)
    ap.add_argument("--dir", choices=["rtl", "ltr"], default="rtl")
    ap.add_argument("--back", choices=["rotated", "same"], default="rotated")
    a = ap.parse_args()

    src = pymupdf.open(a.src)
    pages = list(range(1, len(src) + 1)) if a.range == "all" else parse_range(a.range, len(src))
    n = a.per_side
    if n not in (1, 2):
        raise SystemExit("error: --per-side must be 1 or 2")

    # Landscape layout area (before rotating onto the portrait sheet)
    lw, lh = A4_H, A4_W
    out = pymupdf.open()
    for i in range(0, len(pages), n):
        group = pages[i:i + n]
        side = i // n  # 0-based index of this sheet side
        page = out.new_page(width=A4_W, height=A4_H)
        if n == 1:
            slots = [pymupdf.Rect(MARGIN, MARGIN, A4_W - MARGIN, A4_H - MARGIN)]
            for pno, rect in zip(group, slots):
                page.show_pdf_page(rect, src, pno - 1)
            continue

        # fit two source pages side by side, keeping aspect ratio, centred
        sw, sh = src[group[0] - 1].rect.width, src[group[0] - 1].rect.height
        scale = min((lw - 2 * MARGIN) / (2 * sw), (lh - 2 * MARGIN) / sh)
        w, h = sw * scale, sh * scale
        x0 = (lw - 2 * w) / 2
        y0 = (lh - h) / 2
        left, right = (x0, y0, x0 + w, y0 + h), (x0 + w, y0, x0 + 2 * w, y0 + h)
        order = [right, left] if a.dir == "rtl" else [left, right]

        ccw = not (a.back == "rotated" and side % 2 == 1)
        for pno, (ax0, ay0, ax1, ay1) in zip(group, order):
            if ccw:   # landscape top edge -> portrait left edge
                rect, rot = pymupdf.Rect(ay0, lw - ax1, ay1, lw - ax0), 90
            else:     # landscape top edge -> portrait right edge
                rect, rot = pymupdf.Rect(lh - ay1, ax0, lh - ay0, ax1), 270
            page.show_pdf_page(rect, src, pno - 1, rotate=rot)

    out.save(a.out, deflate=True, garbage=3)
    print(f"{len(pages)} source pages -> {len(out)} sheet sides", flush=True)


if __name__ == "__main__":
    main()
