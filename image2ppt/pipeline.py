"""End-to-end pipeline: extract -> tidy -> render -> optional review rounds.

Work files for each page go to the work directory:
  pN.raw.json            model output (only when extraction ran)
  pN.rK.json             layout after tidy, round K
  rK-N.png               LibreOffice rendering of round K (only when rendered)
  pN.rK.compare.png      original on top, rebuilt below
  pN.patchK.json         review patch from the model
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image

from . import render, schema
from .llm import LLMClient
from .tidy import tidy


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def normalize_raw(lay: dict) -> dict:
    """Model output -> internal convention.

    Boxes become [x, y, w, h] (the model reports corners), a rect that covers
    the whole page is dropped, degenerate boxes are dropped, and a `parent`
    that points at a missing element is cleared.
    """
    W, H = lay["image_size"]
    out = []
    for e in lay["elements"]:
        if "box" in e:
            x1, y1, x2, y2 = e["box"]
            if x2 > x1 and y2 > y1:
                e["box"] = [x1, y1, x2 - x1, y2 - y1]
            x, y, w, h = e["box"]
            if e["type"] == "rect" and w * h > 0.85 * W * H:
                continue  # the page itself
            if w <= 0 or h <= 0:
                continue
        out.append(e)
    ids = {e["id"] for e in out}
    for e in out:
        if e.get("parent") not in ids:
            e["parent"] = None
    lay["elements"] = out
    return lay


def extract(img_path: str | Path, client: LLMClient) -> dict:
    """Ask the vision model for the layout of one image and return it normalized."""
    W, H = Image.open(img_path).size
    lay = client.complete_json(schema.EXTRACT_PROMPT.format(w=W, h=H), schema.JSON_SCHEMA, images=[img_path])
    lay["image_size"] = [W, H]
    errors = schema.validate_layout(lay)
    if errors:
        raise ValueError("model output does not match the layout schema: " + "; ".join(errors[:5]))
    return normalize_raw(lay)


def load_layout(path: str | Path, img_path: str | Path) -> dict:
    """Load a layout JSON. A `*.raw.json` file is treated as model output and normalized."""
    lay = json.loads(Path(path).read_text(encoding="utf-8"))
    if "image_size" not in lay:
        lay["image_size"] = list(Image.open(img_path).size)
    errors = schema.validate_layout(lay)
    if errors:
        raise ValueError(f"{path}: invalid layout: " + "; ".join(errors[:5]))
    if str(path).endswith(".raw.json"):
        lay = normalize_raw(lay)
    return lay


def apply_patch(lay: dict, patch: dict) -> int:
    """Apply review ops (move / set / delete / add) in place. Returns the number applied."""
    by_id = {e["id"]: e for e in lay["elements"]}
    n = 0
    for op in patch.get("ops", []):
        k = op.get("op")
        if k == "move" and op.get("id") in by_id:
            e = by_id[op["id"]]
            if op.get("box") and "box" in e:
                e["box"] = list(op["box"])
            if op.get("points") and e["type"] in ("arrow", "line"):
                e["points"] = [list(p) for p in op["points"]]
            e.pop("anchors", None)
            n += 1
        elif k == "set" and op.get("id") in by_id:
            by_id[op["id"]].update(op.get("fields") or {})
            n += 1
        elif k == "delete" and op.get("id") in by_id:
            lay["elements"] = [e for e in lay["elements"] if e["id"] != op["id"]]
            by_id.pop(op["id"])
            n += 1
        elif k == "add" and op.get("element"):
            e = op["element"]
            e.setdefault("id", f"x{len(lay['elements'])}")
            lay["elements"].append(e)
            n += 1
    return n


def render_png(pptx_path: str | Path, workdir: str | Path, tag: str) -> list[str]:
    """PPTX -> PDF (LibreOffice) -> one PNG per slide (poppler). Returns the PNG paths."""
    for tool in ("soffice", "pdftoppm"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found; it is needed to render slides for review or --preview")
    workdir = str(workdir)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", workdir, str(pptx_path)],
                   check=True, capture_output=True, timeout=300)
    pdf = os.path.join(workdir, os.path.splitext(os.path.basename(str(pptx_path)))[0] + ".pdf")
    prefix = os.path.join(workdir, tag)
    subprocess.run(["pdftoppm", "-r", "120", "-png", pdf, prefix], check=True, capture_output=True, timeout=300)
    names = (os.path.join(workdir, f) for f in os.listdir(workdir))
    return sorted(p for p in names if os.path.basename(p).startswith(tag + "-") and p.endswith(".png"))


def side_by_side(orig: str | Path, rebuilt: str | Path, out: str | Path) -> None:
    """Write the original (top) and the rebuilt slide (bottom) as one PNG."""
    a = Image.open(orig).convert("RGB")
    b = Image.open(rebuilt).convert("RGB").resize(a.size)
    c = Image.new("RGB", (a.width, a.height * 2 + 12), "white")
    c.paste(a, (0, 0))
    c.paste(b, (0, a.height + 12))
    c.save(out)


def _dump(obj: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def convert(images: Sequence[str | Path], out_path: str | Path, *, client: LLMClient | None = None,
            rounds: int = 1, layout: str | Path | None = None, do_tidy: bool = True,
            workdir: str | Path | None = None, preview: bool = False,
            log: Callable[[str], None] = _stderr) -> list[dict]:
    """Convert images to one PPTX (one slide per image). Returns the final layouts.

    `layout` skips extraction (single image only). `client` is required when
    extraction or review rounds run. `preview` renders PNGs even with rounds=0.
    """
    out_path = str(out_path)
    workdir = str(workdir or os.path.splitext(out_path)[0] + ".work")
    os.makedirs(workdir, exist_ok=True)
    if layout and len(images) != 1:
        raise ValueError("--layout works with exactly one image")
    if (not layout or rounds > 0) and client is None:
        raise ValueError("a model backend is needed for extraction and review rounds")

    pages = []  # [layout, image path, stem]
    for i, img in enumerate(images):
        stem = f"p{i + 1}"
        if layout:
            lay = load_layout(layout, img)
        else:
            log(f"[{stem}] extracting layout from {img} ...")
            lay = extract(img, client)
            _dump(lay, os.path.join(workdir, f"{stem}.raw.json"))
        log(f"[{stem}] {len(lay['elements'])} elements")
        pages.append([lay, img, stem])

    want_png = rounds > 0 or preview

    def build(tag: str) -> list[str]:
        rendered = []
        for page in pages:
            lay, img, stem = page
            needs_sizes = any(e["type"] in ("rect", "text") and "size" not in e for e in lay["elements"])
            cur = tidy(lay)[0] if (do_tidy or needs_sizes) else lay  # font sizes are needed even without tidy
            _dump(cur, os.path.join(workdir, f"{stem}.{tag}.json"))
            page[0] = cur  # later patches address the tidied coordinates
            rendered.append((cur, Image.open(img)))
        render.render(rendered, out_path)
        if not want_png:
            return []
        pngs = render_png(out_path, workdir, tag)
        for (_, img, stem), png in zip(pages, pngs):
            side_by_side(img, png, os.path.join(workdir, f"{stem}.{tag}.compare.png"))
        return pngs

    pngs = build("r0")
    for r in range(1, rounds + 1):
        for (lay, img, stem), png in zip(pages, pngs):
            W, H = lay["image_size"]
            log(f"[{stem}] review round {r} ...")
            prompt = schema.REVIEW_PROMPT.format(w=W, h=H, layout=json.dumps(lay, ensure_ascii=False, separators=(",", ":")))
            patch = client.complete_json(prompt, schema.PATCH_SCHEMA, images=[img, png])
            _dump(patch, os.path.join(workdir, f"{stem}.patch{r}.json"))
            n = apply_patch(lay, patch)
            log(f"[{stem}] verdict={patch.get('verdict')} ops applied={n}")
        pngs = build(f"r{r}")
    return [p[0] for p in pages]
