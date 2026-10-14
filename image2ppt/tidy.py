"""Layout normalization: turns the vision model's estimated coordinates into a
layout a person would have drawn (aligned edges, equal sizes, equal gaps, lines
anchored to box edges, text sized to fit). Pure geometry, no model calls.

Steps (all tolerances scale with image width W):
  0. rejoin words split across lines; merge loose text into the box it sits on
  1. cluster left/right/top/bottom edges of boxes and unify each cluster
  2. cluster widths/heights of rects and unify (keep left/top edge)
  3. rows/columns with >=3 members: equalize gaps
  4. containment: keep children inside parents with a pad
  5. snap boxes to a grid
  6. lines: merge shared endpoints, orthogonalize, anchor endpoints to box edges
  7. labels sitting on a line: move them clear of the line
  8. font size to fit the box
"""
import copy
import math
import os
from statistics import median

from PIL import ImageFont

_FONT_FILES = {
    (False, False): "LiberationSans-Regular.ttf", (True, False): "LiberationSans-Bold.ttf",
    (False, True): "LiberationSans-Italic.ttf", (True, True): "LiberationSans-BoldItalic.ttf"}
_FONT_DIRS = [d for d in [os.environ.get("IMAGE2PPT_FONT_DIR"),
                          "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/liberation2",
                          "/usr/share/fonts"] if d]
_font_cache = {}


def text_width_pt(text, size_pt, bold=False, italic=False):
    """Width of one line in points using Liberation Sans (metric-compatible with Arial).
    Looks in IMAGE2PPT_FONT_DIR and the usual system font directories. Falls back to a
    per-glyph estimate when the font file is missing, so font sizes can differ
    slightly on machines without Liberation Sans."""
    key = (bool(bold), bool(italic))
    if key not in _font_cache:
        path = None
        for d in _FONT_DIRS:
            cand = os.path.join(d, _FONT_FILES[key])
            if os.path.exists(cand):
                path = cand
                break
        _font_cache[key] = path
    path = _font_cache[key]
    if not path:
        return len(text) * size_pt * (0.6 if bold else 0.53)
    f = ImageFont.truetype(path, 100)
    return f.getlength(text) * size_pt / 100

BOX_TYPES = ("rect", "text", "crop")
LINE_TYPES = ("arrow", "line")
FONT_STEPS = [6, 7, 8, 9, 10, 10.5, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32, 36, 44]


def _cluster(values, tol):
    """1-D clustering of (key, value) pairs; returns {key: cluster_median}."""
    items = sorted(values, key=lambda kv: kv[1])
    out, group = {}, []
    for k, v in items:
        if group and v - group[-1][1] > tol:
            m = median(x for _, x in group)
            out.update({kk: m for kk, _ in group})
            group = []
        group.append((k, v))
    if group:
        m = median(x for _, x in group)
        out.update({kk: m for kk, _ in group})
    return out


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


class Tidy:
    def __init__(self, layout, grid_div=240, tol_frac=0.012):
        self.lay = copy.deepcopy(layout)
        self.W, self.H = self.lay["image_size"]
        self.tol = tol_frac * self.W
        self.grid = self.W / grid_div
        self.els = self.lay["elements"]
        for i, e in enumerate(self.els):
            e.setdefault("id", f"e{i}")
            if e["type"] in LINE_TYPES and "points" not in e:
                e["points"] = [e.pop("from"), e.pop("to")]
        self.by_id = {e["id"]: e for e in self.els}
        self.log = []

    # ---------- helpers ----------
    def boxes(self, types=BOX_TYPES):
        return [e for e in self.els if e["type"] in types and "box" in e]

    def run(self):
        self.clean_text()
        self.align_edges()
        self.unify_sizes()
        self.equalize_gaps()
        self.contain_children()
        self.snap_boxes()
        self.fix_lines()
        self.center_labels()
        self.fit_text()
        return self.lay

    # 0 -------------------------------------------------------------
    def clean_text(self):
        """Rejoin words the model split across lines ("Lightweigh\\nt"), and merge a
        text element into the text block of the rect it sits on when the two overlap."""
        for e in self.boxes(("rect", "text")):
            if not e.get("text"):
                continue
            lines = e["text"].split("\n")
            out = []
            for ln in lines:
                if out and len(ln.strip()) <= 2 and ln.strip().isalpha() and out[-1] and out[-1][-1].isalpha():
                    out[-1] += ln.strip()
                else:
                    out.append(ln)
            e["text"] = "\n".join(out)
        rects = [r for r in self.boxes(("rect",)) if r.get("text")]
        drop = set()
        for t in self.boxes(("text",)):
            if not t.get("text"):
                continue
            x, y, w, h = t["box"]
            for r in rects:
                rx, ry, rw, rh = r["box"]
                if not (rx <= x + w / 2 <= rx + rw and ry <= y + h / 2 <= ry + rh):
                    continue
                n = len(r["text"].split("\n"))
                lp = r.get("line_px") or rh / (n + 1)
                block_top, block_bot = ry + rh / 2 - n * lp * 0.7, ry + rh / 2 + n * lp * 0.7
                gap = max(block_top - (y + h), y - block_bot, 0)
                if _overlap(y, y + h, block_top, block_bot) > 0 or gap < 0.8 * lp:
                    if y >= ry + rh / 2:
                        r["text"] = r["text"] + "\n" + t["text"]
                    else:
                        r["text"] = t["text"] + "\n" + r["text"]
                    drop.add(t["id"])
                    self.log.append(f"merged {t['id']} into {r['id']}")
                    break
        if drop:
            self.els[:] = [e for e in self.els if e["id"] not in drop]
            self.by_id = {e["id"]: e for e in self.els}

    # 1 -------------------------------------------------------------
    def align_edges(self):
        bs = [b for b in self.boxes() if b["type"] != "crop"]
        for idx, name in ((0, "left"), (2, "top")):
            m = _cluster([(b["id"], b["box"][idx // 2]) for b in bs], self.tol)
            for b in bs:
                far = b["box"][idx // 2] + b["box"][idx // 2 + 2]
                b["box"][idx // 2] = m[b["id"]]
                b["box"][idx // 2 + 2] = far - m[b["id"]]
        for idx, name in ((0, "right"), (1, "bottom")):
            m = _cluster([(b["id"], b["box"][idx] + b["box"][idx + 2]) for b in bs], self.tol)
            for b in bs:
                b["box"][idx + 2] = max(self.grid, m[b["id"]] - b["box"][idx])
        self.log.append("edges aligned")

    # 2 -------------------------------------------------------------
    def unify_sizes(self):
        """Cluster widths/heights among rects that share a parent (containers and
        their children are never unified with each other)."""
        rs = [b for b in self.boxes(("rect",))]
        groups = {}
        for b in rs:
            p = self.parent_of(b)
            b["parent"] = p["id"] if p else None
            groups.setdefault(b["parent"], []).append(b)
        for members in groups.values():
            for dim in (2, 3):
                m = _cluster([(b["id"], b["box"][dim]) for b in members], self.tol)
                for b in members:
                    b["box"][dim] = m[b["id"]]
        self.log.append("sizes unified")

    # 3 -------------------------------------------------------------
    def _groups(self, rs, axis):
        """Group rects sharing the same span on `axis` (0 = same row, 1 = same column)."""
        a, s = (1, 3) if axis == 0 else (0, 2)
        groups, used = [], set()
        for b in rs:
            if b["id"] in used:
                continue
            g = [b]
            used.add(b["id"])
            for c in rs:
                if c["id"] in used:
                    continue
                ov = _overlap(b["box"][a], b["box"][a] + b["box"][s], c["box"][a], c["box"][a] + c["box"][s])
                if ov > 0.7 * min(b["box"][s], c["box"][s]) and abs(b["box"][s] - c["box"][s]) <= self.tol:
                    g.append(c)
                    used.add(c["id"])
            if len(g) >= 3:
                groups.append(g)
        return groups

    def equalize_gaps(self):
        rs = [b for b in self.boxes(("rect",))]
        # rects that have the same parent (or none)
        for axis in (0, 1):
            p, s = (0, 2) if axis == 0 else (1, 3)
            for g in self._groups(rs, axis):
                g.sort(key=lambda b: b["box"][p])
                gaps = [g[i + 1]["box"][p] - (g[i]["box"][p] + g[i]["box"][s]) for i in range(len(g) - 1)]
                if max(gaps) - min(gaps) > 3 * self.tol or min(gaps) < 0:
                    continue  # not an evenly spaced row/column
                total = g[-1]["box"][p] + g[-1]["box"][s] - g[0]["box"][p]
                gap = (total - sum(b["box"][s] for b in g)) / (len(g) - 1)
                cur = g[0]["box"][p]
                for b in g:
                    b["box"][p] = cur
                    cur += b["box"][s] + gap
                self.log.append(f"equalized {'row' if axis == 0 else 'column'} of {len(g)}")

    # 4 -------------------------------------------------------------
    def parent_of(self, e):
        pid = e.get("parent")
        if pid and pid in self.by_id and pid != e["id"]:
            return self.by_id[pid]
        x, y, w, h = e["box"]
        best = None
        for r in self.boxes(("rect",)):
            if r is e:
                continue
            rx, ry, rw, rh = r["box"]
            inter = _overlap(x, x + w, rx, rx + rw) * _overlap(y, y + h, ry, ry + rh)
            if inter >= 0.9 * w * h and rw * rh > w * h * 1.2:
                if best is None or rw * rh < best["box"][2] * best["box"][3]:
                    best = r
        return best

    def contain_children(self):
        """Children stay inside their parent. An edge within tol of the parent's
        edge becomes flush with it (header tabs); other edges keep a pad."""
        pad = self.grid
        for e in self.boxes():
            p = self.parent_of(e)
            if not p:
                continue
            e["parent"] = p["id"]
            x, y, w, h = e["box"]
            px, py, pw, ph = p["box"]
            tab = w >= 0.8 * pw or h >= 0.8 * ph
            l = px if tab and abs(x - px) <= self.tol else max(x, px + pad)
            t = py if tab and abs(y - py) <= self.tol else max(y, py + pad)
            r = px + pw if tab and abs(x + w - px - pw) <= self.tol else min(x + w, px + pw - pad)
            b = py + ph if tab and abs(y + h - py - ph) <= self.tol else min(y + h, py + ph - pad)
            e["box"] = [l, t, max(self.grid, r - l), max(self.grid, b - t)]

    # 5 -------------------------------------------------------------
    def snap(self, v):
        return round(v / self.grid) * self.grid

    def snap_boxes(self):
        for b in self.boxes():
            if b["type"] == "crop":
                continue
            x, y, w, h = b["box"]
            b["box"] = [self.snap(x), self.snap(y), max(self.grid, self.snap(w)), max(self.grid, self.snap(h))]

    # 6 -------------------------------------------------------------
    def _nearest_edge(self, pt, direction=None):
        """Box edge nearest to pt within tol. `direction` = (dx, dy) of the segment
        touching pt; only sides perpendicular to it qualify (a horizontal segment
        ends on a left/right side). Ties within half a grid go to the smaller box."""
        px, py = pt
        sides = None
        if direction:
            dx, dy = direction
            if abs(dx) > 2 * abs(dy):
                sides = ("left", "right")
            elif abs(dy) > 2 * abs(dx):
                sides = ("top", "bottom")
        best = None
        for b in self.boxes(("rect",)):  # lines connect boxes, never labels
            x, y, w, h = b["box"]
            cands = [
                ("top", abs(py - y), x <= px <= x + w, (px - x) / w),
                ("bottom", abs(py - (y + h)), x <= px <= x + w, (px - x) / w),
                ("left", abs(px - x), y <= py <= y + h, (py - y) / h),
                ("right", abs(px - (x + w)), y <= py <= y + h, (py - y) / h),
            ]
            for side, d, within, t in cands:
                if sides and side not in sides:
                    continue
                if d <= self.tol and (within or d <= self.tol / 2):
                    area = w * h
                    if best is None or d < best[2] - self.grid / 2 or (abs(d - best[2]) <= self.grid / 2 and area < best[4]):
                        best = (b, side, d, t, area)
        return best[:4] if best else None

    def fix_lines(self):
        lines = [e for e in self.els if e["type"] in LINE_TYPES]
        # merge endpoints that several segments share (elbows drawn as separate segments)
        pts = [(li, pi) for li, l in enumerate(lines) for pi in range(len(l["points"]))]
        r = self.tol * 0.6
        for i, (li, pi) in enumerate(pts):
            a = lines[li]["points"][pi]
            for lj, pj in pts[i + 1:]:
                b = lines[lj]["points"][pj]
                if abs(a[0] - b[0]) <= r and abs(a[1] - b[1]) <= r:
                    m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
                    a[0], a[1] = m
                    b[0], b[1] = m
        for l in lines:
            P = l["points"]
            # orthogonalize each segment
            for i in range(len(P) - 1):
                (x1, y1), (x2, y2) = P[i], P[i + 1]
                if abs(x2 - x1) <= self.tol and abs(y2 - y1) > self.tol:
                    xm = (x1 + x2) / 2
                    P[i][0] = P[i + 1][0] = xm
                elif abs(y2 - y1) <= self.tol and abs(x2 - x1) > self.tol:
                    ym = (y1 + y2) / 2
                    P[i][1] = P[i + 1][1] = ym
            # anchor the two ends to box edges
            anchors = {}
            orient = {}
            for i in range(len(P) - 1):
                dx, dy = P[i + 1][0] - P[i][0], P[i + 1][1] - P[i][1]
                orient[i] = "h" if abs(dy) <= self.tol < abs(dx) else "v" if abs(dx) <= self.tol < abs(dy) else None
            for key, idx, nb in (("begin", 0, 1), ("end", len(P) - 1, len(P) - 2)):
                hint = l.get("from_id" if key == "begin" else "to_id")
                direction = (P[idx][0] - P[nb][0], P[idx][1] - P[nb][1])
                hit = self._nearest_edge(P[idx], direction)
                if hint and hint in self.by_id and "box" in self.by_id[hint]:
                    b = self.by_id[hint]
                    x, y, w, h = b["box"]
                    px, py = P[idx]
                    d = {"top": abs(py - y), "bottom": abs(py - y - h), "left": abs(px - x), "right": abs(px - x - w)}
                    side = min(d, key=d.get)
                    t = (px - x) / w if side in ("top", "bottom") else (py - y) / h
                    hit = (b, side, d[side], t)
                if not hit:
                    continue
                b, side, d, t = hit
                x, y, w, h = b["box"]
                px, py = P[idx]
                if side == "top":
                    py = y
                elif side == "bottom":
                    py = y + h
                elif side == "left":
                    px = x
                else:
                    px = x + w
                edge_len = w if side in ("top", "bottom") else h
                seg = min(idx, nb)
                # a connection point (edge midpoint) only when the line already aims near it and
                # moving there keeps the segment straight
                cxn = abs(t - 0.5) * edge_len <= 1.5 * self.tol and b["type"] == "rect"
                other_key = "end" if key == "begin" else "begin"
                if cxn and len(P) == 2 and other_key in anchors and anchors[other_key]["cxn"] and orient.get(seg):
                    # on a two-point line the far end already fixed the shared coordinate: attach this end too only
                    # when its connection point lies on that line, so the segment stays straight
                    mid = y + h / 2 if side in ("left", "right") else x + w / 2
                    cxn = abs(mid - (py if side in ("left", "right") else px)) < 0.5
                if cxn:
                    if side in ("top", "bottom"):
                        px = x + w / 2
                    else:
                        py = y + h / 2
                P[idx] = [px, py]
                anchors[key] = {"id": b["id"], "side": side, "cxn": cxn}
                # keep the adjacent segment straight by moving its other point
                q = P[nb]
                if orient.get(seg) == "h":
                    q[1] = py
                elif orient.get(seg) == "v":
                    q[0] = px
                if nb == (len(P) - 1 if key == "begin" else 0) and other_key in anchors and not anchors[other_key]["cxn"]:
                    pass  # the far end slid along its edge; it stays anchored to that edge
            # final pass: every near-orthogonal segment becomes exactly orthogonal,
            # taking the coordinate from a connection-point end when there is one
            for i, o in orient.items():
                if not o:
                    continue
                c = 1 if o == "h" else 0
                a_is_cxn = i == 0 and anchors.get("begin", {}).get("cxn")
                b_is_cxn = i + 1 == len(P) - 1 and anchors.get("end", {}).get("cxn")
                v = P[i][c] if a_is_cxn else P[i + 1][c] if b_is_cxn else (P[i][c] + P[i + 1][c]) / 2
                P[i][c] = P[i + 1][c] = v
            l["anchors"] = anchors
            # snap free points to the grid
            for i, p in enumerate(P):
                key = "begin" if i == 0 else "end" if i == len(P) - 1 else None
                if key and key in anchors:
                    continue
                P[i] = [self.snap(p[0]), self.snap(p[1])]
        self.log.append(f"lines fixed: {sum(len(l.get('anchors', {})) for l in lines)} anchored ends")

    # 7 -------------------------------------------------------------
    def center_labels(self):
        """Center a free label on the line it sits next to, moving it off the line if they overlap."""
        lines = [e for e in self.els if e["type"] in LINE_TYPES]
        for t in self.boxes(("text",)):
            if t.get("parent") in self.by_id:
                continue
            x, y, w, h = t["box"]
            cx, cy = x + w / 2, y + h / 2
            for l in lines:
                for i in range(len(l["points"]) - 1):
                    (x1, y1), (x2, y2) = l["points"][i], l["points"][i + 1]
                    if abs(y1 - y2) < 1 and min(x1, x2) <= cx <= max(x1, x2) and abs(cy - y1) <= 2.5 * self.tol:
                        t["box"][0] = self.snap((x1 + x2) / 2 - w / 2)
                        if y - 2 <= y1 <= y + h + 2:  # label overlaps the line: move it clear
                            t["box"][1] = y1 - h - self.grid / 2 if cy <= y1 else y1 + self.grid / 2
                    elif abs(x1 - x2) < 1 and min(y1, y2) <= cy <= max(y1, y2) and abs(cx - x1) <= 2.5 * self.tol:
                        t["box"][1] = self.snap((y1 + y2) / 2 - h / 2)
                        if x - 2 <= x1 <= x + w + 2:
                            t["box"][0] = x1 - w - self.grid / 2 if cx <= x1 else x1 + self.grid / 2

    # 8 -------------------------------------------------------------
    def px_to_pt(self, px):
        return px * (13.3333 * 72) / self.W

    def fit_text(self):
        """Font size per element from the model's line-height estimate, checked
        against the box. A free text box grows to fit; a rect or a child element
        shrinks its font (down to 85%) first, then grows within its parent."""
        for e in self.boxes(("rect", "text")):
            if not e.get("text"):
                continue
            n = len(e["text"].split("\n"))
            if e.get("line_px"):
                base = self.px_to_pt(e["line_px"]) * 0.95  # model reports glyph height, ~1 em
            elif e.get("size"):
                base = float(e["size"])
            else:
                base = self.px_to_pt(e["box"][3] / n / 1.4) / 1.2
            size = max(s for s in FONT_STEPS if s <= max(6.0, base))
            lines = [ln.rstrip() for ln in e["text"].split("\n")]
            widest = lambda sz: max(text_width_pt(ln, sz, e.get("bold"), e.get("italic")) for ln in lines)
            need = lambda sz: (self.pt_to_px(sz * 1.25 * n + 7), self.pt_to_px(widest(sz) + 12))
            need_h, need_w = need(size)
            x, y, w, h = e["box"]
            parent = self.by_id.get(e.get("parent") or "")
            if e["type"] == "rect" or parent:
                while (need_h > h or need_w > w) and size > 0.85 * base and size > 6:
                    size = max(s for s in FONT_STEPS if s < size)
                    need_h, need_w = need(size)
            if need_h > h:
                y -= (need_h - h) / 2
                h = need_h
            if need_w > w:
                if e.get("align") == "left":
                    pass
                elif e.get("align") == "right":
                    x -= need_w - w
                else:
                    x -= (need_w - w) / 2
                w = need_w
            if parent:
                px, py, pw, ph = parent["box"]
                x = min(max(x, px), px + pw - w)
                y = min(max(y, py), py + ph - h)
            e["box"] = [x, y, w, h]
            e["size"] = size

    def pt_to_px(self, pt):
        return pt * self.W / (13.3333 * 72)


def tidy(layout, **kw):
    t = Tidy(layout, **kw)
    out = t.run()
    return out, t.log
