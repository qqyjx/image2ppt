"""Offline demo: no model, no network, no LibreOffice.

1. Draw a synthetic framework figure with Pillow (examples/demo_slide.png).
2. Load examples/demo_layout.raw.json, a hand-written, slightly noisy stand-in
   for what a vision model would return for that image (coordinates jittered
   by roughly 1 % of the width, one word split across lines).
3. Run tidy + render with zero review rounds and write examples/out/demo.pptx.
4. Print counts of what the PPTX contains and a before/after list of coordinates.

Run from the repository root:  python -m image2ppt.demo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import pipeline
from .render import pptx_stats
from .tidy import tidy

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
W, H = 1200, 640
BG = "FFFFFF"


def _font(px: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    for d in ("/usr/share/fonts/truetype/liberation2", "/usr/share/fonts/truetype/liberation"):
        if (Path(d) / name).exists():
            return ImageFont.truetype(str(Path(d) / name), px)
    return ImageFont.load_default(px)


def _hex(s: str) -> tuple[int, int, int]:
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def draw_slide(path: Path) -> None:
    """Draw the synthetic framework figure (clean geometry, no jitter)."""
    im = Image.new("RGB", (W, H), _hex(BG))
    d = ImageDraw.Draw(im)

    def box(xy, fill, line, text=None, rounded=False, bold=False, px=22):
        (d.rounded_rectangle if rounded else d.rectangle)(xy, fill=_hex(fill), outline=_hex(line), width=2,
                                                          **({"radius": 12} if rounded else {}))
        if text:
            d.multiline_text(((xy[0] + xy[2]) / 2, (xy[1] + xy[3]) / 2), text, font=_font(px, bold),
                             fill=_hex("1E1E1E"), anchor="mm", align="center")

    def label(xy, text, px, bold=False, anchor="lm"):
        x = xy[0] if anchor == "lm" else (xy[0] + xy[2]) / 2
        d.text((x, (xy[1] + xy[3]) / 2), text, font=_font(px, bold), fill=_hex("1E1E1E"), anchor=anchor)

    def head(x, y, dx, dy):  # arrowhead pointing along (dx, dy)
        px, py = -dy, dx
        d.polygon([(x, y), (x - 12 * dx + 5 * px, y - 12 * dy + 5 * py),
                   (x - 12 * dx - 5 * px, y - 12 * dy - 5 * py)], fill=_hex("1E1E1E"))

    label((395, 18, 810, 62), "Toy Framework (synthetic)", 30, bold=True, anchor="mm")
    box((290, 140, 910, 460), "F7F7F7", "7F7F7F")
    label((310, 150, 410, 180), "Encoder", 20, bold=True)
    box((110, 250, 250, 350), "DCE9F7", "2F5597", "Input")
    box((320, 250, 460, 350), "FFF2CC", "BF9000", "Embed")
    box((530, 250, 670, 350), "FFF2CC", "BF9000", "Lightweight\nAttention")
    box((740, 250, 880, 350), "FFF2CC", "BF9000", "Project")
    box((950, 250, 1090, 350), "E2F0D9", "548235", "Output")
    box((500, 520, 720, 590), "FCE4D6", "C55A11", "Feedback", rounded=True)
    for x0, x1 in ((250, 320), (460, 530), (670, 740), (880, 950)):
        d.line([(x0, 300), (x1 - 8, 300)], fill=_hex("1E1E1E"), width=3)
        head(x1, 300, 1, 0)
    label((468, 260, 522, 286), "tokens", 16, anchor="mm")
    for y in range(350, 555, 16):  # dashed feedback path: down, then left
        d.line([(1020, y), (1020, min(y + 9, 555))], fill=_hex("1E1E1E"), width=3)
    for x in range(1020, 728, -16):
        d.line([(x, 555), (max(x - 9, 728), 555)], fill=_hex("1E1E1E"), width=3)
    head(720, 555, -1, 0)
    # small bar-chart icon, drawn as pixels so it can only be cropped, not rebuilt
    d.line([(110, 520), (210, 520)], fill=_hex("595959"), width=3)
    d.line([(110, 430), (110, 520)], fill=_hex("595959"), width=3)
    for x, top, col in ((125, 480, "4472C4"), (153, 455, "ED7D31"), (181, 470, "70AD47")):
        d.rectangle((x, top, x + 18, 518), fill=_hex(col))
    label((108, 524, 216, 552), "Score", 18, anchor="mm")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _find(lay: dict, eid: str) -> dict:
    return next(e for e in lay["elements"] if e["id"] == eid)


def before_after(before: dict, after: dict) -> list[str]:
    """A few coordinates before tidy (model output) and after tidy, in source pixels."""
    rows = []
    for eid, name in (("b3", "Embed"), ("b4", "Attention"), ("b5", "Project")):
        b, a = _find(before, eid)["box"], _find(after, eid)["box"]
        rows.append(f"{eid} {name:<10} x {b[0]:7.1f} -> {a[0]:7.1f}   w {b[2]:6.1f} -> {a[2]:6.1f}   h {b[3]:6.1f} -> {a[3]:6.1f}")
    gap = lambda lay: _find(lay, "b4")["box"][0] - sum(_find(lay, "b3")["box"][0::2])
    rows.append(f"gap Embed|Attention {gap(before):.1f} -> {gap(after):.1f}")
    for eid, end, label in (("a1", 0, "a1 begin"), ("a4", -1, "a4 end  "), ("a5", -1, "a5 end  ")):
        p0, p1 = _find(before, eid)["points"][end], _find(after, eid)["points"][end]
        anc = _find(after, eid).get("anchors", {}).get("begin" if end == 0 else "end")
        note = f"  anchored to {anc['id']}.{anc['side']}, connection point={anc['cxn']}" if anc else "  free"
        rows.append(f"{label}      ({p0[0]:6.1f}, {p0[1]:6.1f}) -> ({p1[0]:6.1f}, {p1[1]:6.1f}){note}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline image2ppt demo (replays a recorded layout).")
    ap.add_argument("--out-dir", default=str(EXAMPLES / "out"), help="where demo.pptx is written")
    args = ap.parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = EXAMPLES / "demo_slide.png"
    raw_path = EXAMPLES / "demo_layout.raw.json"
    pptx = out_dir / "demo.pptx"

    print("image2ppt offline demo: replays a recorded layout; no model is called.")
    draw_slide(png)
    print(f"[1] drew synthetic slide {png.name} ({W}x{H})")
    before = pipeline.load_layout(raw_path, png)
    kinds = {}
    for e in before["elements"]:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print(f"[2] loaded recorded model output {raw_path.name}: {len(before['elements'])} elements "
          + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    pipeline.convert([png], pptx, layout=raw_path, rounds=0, workdir=out_dir / "demo.work", log=lambda m: None)
    after = json.loads((out_dir / "demo.work" / "p1.r0.json").read_text(encoding="utf-8"))
    print(f"[3] tidy + render (rounds=0) -> {pptx.relative_to(ROOT) if pptx.is_relative_to(ROOT) else pptx}")
    log = tidy(before)[1]
    print("    tidy steps: " + "; ".join(log))

    st = pptx_stats(pptx)
    print("[4] contents of the PPTX (counted from the slide XML)")
    print(f"    native shapes (boxes + text boxes): {st['shapes']}  (text boxes: {st['text_boxes']})")
    print(f"    connectors: {st['connectors']}  attached at both ends: {st['connectors_both_ends']}  "
          f"attached at one end: {st['connectors_one_end']}")
    print(f"    cropped pictures: {st['pictures']}")
    print("[5] before (model output) -> after (tidy), source pixels")
    for row in before_after(before, after):
        print("    " + row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
