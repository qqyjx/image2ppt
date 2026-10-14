"""Layout schema shared by extract, tidy, render and review, plus the model prompts.

All coordinates are pixels of the source image (origin at the top-left corner).

    layout = {
      "image_size": [W, H],
      "background": "RRGGBB",
      "elements": [
        {"id": "b1", "type": "rect", "box": [x, y, w, h], "fill": "RRGGBB" | null,
         "line": "RRGGBB" | null, "rounded": bool, "dash": bool,
         "text": str | null, "bold": bool, "italic": bool, "line_px": int,
         "align": "left" | "center" | "right", "parent": "b0" | null},
        {"id": "t1", "type": "text", "box": [...], "text": str, "bold": bool,
         "italic": bool, "line_px": int, "align": ..., "parent": ...},
        {"id": "a1", "type": "arrow" | "line", "points": [[x, y], [x, y], ...],
         "dash": bool, "from_id": "b1" | null, "to_id": "b2" | null},
        {"id": "c1", "type": "crop", "box": [...], "key_bg": bool, "desc": str}
      ]
    }

The model reports "box" as [x1, y1, x2, y2] (corners). `pipeline.normalize_raw`
converts it to [x, y, w, h], the convention used from tidy onward.

tidy adds to line elements an "anchors" entry
({"begin": {"id", "side", "cxn"}, "end": {...}}) and to text-bearing elements a
"size" (font size in pt) computed to fit the box.
"""
from __future__ import annotations

from typing import Any

JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "image_size": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        "background": {"type": "string"},
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string", "enum": ["rect", "text", "arrow", "line", "crop"]},
                    "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}},
                    "fill": {"type": ["string", "null"]},
                    "line": {"type": ["string", "null"]},
                    "rounded": {"type": "boolean"},
                    "dash": {"type": "boolean"},
                    "text": {"type": ["string", "null"]},
                    "bold": {"type": "boolean"},
                    "italic": {"type": "boolean"},
                    "line_px": {"type": "number"},
                    "align": {"type": "string", "enum": ["left", "center", "right"]},
                    "parent": {"type": ["string", "null"]},
                    "from_id": {"type": ["string", "null"]},
                    "to_id": {"type": ["string", "null"]},
                    "key_bg": {"type": "boolean"},
                    "desc": {"type": "string"},
                },
                "required": ["id", "type"],
            },
        },
    },
    "required": ["image_size", "background", "elements"],
}

PATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["move", "set", "delete", "add"]},
                    "id": {"type": "string"},
                    "box": {"type": "array", "items": {"type": "number"}},
                    "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
                    "fields": {"type": "object"},
                    "element": {"type": "object"},
                    "why": {"type": "string"},
                },
                "required": ["op"],
            },
        },
        "verdict": {"type": "string"},
    },
    "required": ["ops", "verdict"],
}

EXTRACT_PROMPT = """You are converting one slide image into an editable PowerPoint layout.
The attached image is the slide. Its pixel size is {w}x{h}. Report every element in SOURCE PIXEL coordinates.
"box" is always [x1, y1, x2, y2] = left, top, right, bottom corners in pixels (not width/height).

Rules:
- Every visible piece of text must appear exactly once, either as the "text" of the rect that
  encloses it, or as a standalone "text" element. Copy text exactly (keep Greek letters, hats,
  subscripts as unicode). Use "\\n" between lines only where the original breaks lines.
- Boxes with a border or fill are "rect" (set fill/line hex, rounded, dash). A rect that visually
  contains other boxes gets listed BEFORE its children and children set "parent" to its id.
- Arrows and lines are "arrow"/"line" with "points": start point first, end point (arrowhead) last.
  Use 3 or more points for elbowed paths. Start and end points must lie ON the edge of the boxes they
  connect; set from_id/to_id to those box ids when they connect boxes. Dashed -> dash=true.
- Icons, photos, charts, screenshots, stacks of cards, anything not made of text/boxes/lines is
  "crop": give the tight box, a short "desc", and key_bg=true when it sits on the page background.
  Do NOT include a box's own border or text inside a crop. Small bullet icons are crops too.
- "line_px": pixel height of one text line (cap height + descender, i.e. the height of one
  line of the text as printed). Estimate carefully; it decides the font size.
- Boxes that are visually the same width or height should get identical numbers; boxes in a row
  with equal spacing should get equal spacing. Use exact multiples where you can.
- Background: hex of the dominant page background. Do not emit a rect for the page itself.
- Give ids b1,b2,... for rects, t1,... for text, a1,... for arrows/lines, c1,... for crops.
Return only the structured output."""

REVIEW_PROMPT = """You are checking a rebuilt PowerPoint slide against the original slide image.
Two images are attached: first the original, then the rebuilt slide (rendered from the current layout).
The current layout JSON (source-pixel coordinates, size {w}x{h}) is:
{layout}
In that JSON "box" is [x, y, width, height]; use the same convention in your ops.

List patch ops that would make the rebuilt slide match the original. Only report real defects:
text overlapping a line or another box, text cut off or wrapped differently, a box clearly at the
wrong place/size, an arrow not touching the box it should, missing or duplicated elements, wrong
colors. Ignore font differences and sub-1% offsets.
Ops: {{"op":"move","id":..,"box":[x,y,w,h]}} or {{"op":"move","id":..,"points":[[x,y],...]}};
{{"op":"set","id":..,"fields":{{...}}}}; {{"op":"delete","id":..}}; {{"op":"add","element":{{...}}}}.
Give each op a short "why". Set "verdict" to "ok" if nothing material remains, else "fix".
Return only the structured output."""


# ---------------------------------------------------------------- validation

_PY_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def _type_ok(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _PY_TYPES[name])


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Check `value` against the JSON-Schema subset used in this module.

    Supports type (string or list), enum, required, properties, items,
    minItems and maxItems. Returns a list of error strings, empty when valid.
    """
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(value, n) for n in names):
            return [f"{path}: expected {'|'.join(names)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def validate_layout(layout: dict) -> list[str]:
    """Schema check plus the cross-element rules the pipeline relies on.

    Extra rules: unique ids; rect/text/crop carry a "box"; arrow/line carry at
    least two "points"; text elements carry "text"; parent / from_id / to_id
    point at existing ids.
    """
    errors = validate(layout, JSON_SCHEMA)
    if errors:
        return errors
    ids = [e["id"] for e in layout["elements"]]
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            errors.append(f"duplicate id {i!r}")
        seen.add(i)
    for e in layout["elements"]:
        eid, kind = e["id"], e["type"]
        if kind in ("rect", "text", "crop") and "box" not in e:
            errors.append(f"{eid}: {kind} needs a box")
        if kind in ("arrow", "line") and len(e.get("points", [])) < 2:
            errors.append(f"{eid}: {kind} needs at least two points")
        if kind == "text" and not e.get("text"):
            errors.append(f"{eid}: text element needs text")
        for ref in ("parent", "from_id", "to_id"):
            if e.get(ref) and e[ref] not in seen:
                errors.append(f"{eid}: {ref} {e[ref]!r} is not an element id")
    return errors
