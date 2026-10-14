# image2ppt

Convert a slide screenshot or a framework-figure PNG into an editable `.pptx` that uses native PowerPoint objects.

Boxes, text and arrows become shapes, text boxes and connectors. Icons, photos and charts are cut out of the source image and pasted as pictures.

> **Notice.** This is a small tool, not a paper artifact. The extraction step needs a vision model. The default backend is the local `claude` CLI in non-interactive mode. An optional backend calls the Anthropic API with a key read from the `ANTHROPIC_API_KEY` environment variable. The offline demo in this repository does not call a model: it replays a recorded layout. This README makes no claim about conversion quality beyond what the demo prints and shows.

## Pipeline

For each input image:

| Step | Module | What it does | Needs a model |
|---|---|---|---|
| extract | `llm.py`, `schema.py` | A vision model reads the image and returns a JSON element list (`rect`, `text`, `arrow`, `line`, `crop`) in pixel coordinates. A JSON schema constrains the output. | yes |
| tidy | `tidy.py` | Pure geometry. Aligns near-equal edges, makes near-equal sizes equal, equalises gaps in a row, snaps child boxes to their parent, snaps to a grid, straightens connectors and snaps their ends to box edges, moves labels off connectors, and picks font sizes from measured glyph widths. | no |
| render | `render.py` | `python-pptx` writes native objects. Connector ends attach to box connection points, so the connector follows a box when you drag it. Crops come from the source image. | no |
| review (optional) | `pipeline.py` | LibreOffice renders the slide to PNG. The model sees the original and the rendering and returns a `move`/`set`/`delete`/`add` patch. The patch is applied, then tidy and render run again. | yes |

## Install

```bash
pip install -r requirements.txt        # python-pptx, Pillow, lxml (+ pytest)
```

Python 3.10 or later. Optional, only for review rounds and `--preview`: LibreOffice (`soffice`) and poppler (`pdftoppm`). Optional, for font sizes that match the rendered result: the Liberation Sans fonts. Without them, tidy estimates glyph widths and font sizes can differ slightly.

## Quickstart

### Offline demo (no model, no network, no LibreOffice)

```bash
python -m image2ppt.demo
```

The demo:

1. draws a synthetic framework figure with Pillow (`examples/demo_slide.png`: labelled boxes, a nested box, arrows, a dashed elbow arrow, a small icon region);
2. loads `examples/demo_layout.raw.json`, a hand-written stand-in for a model's answer on that image. Its coordinates are jittered by up to about 1 % of the image width, and one word is split across lines (`Lightweigh` / `t`);
3. runs tidy and render with `--rounds 0` and writes `examples/out/demo.pptx`;
4. prints the number of native shapes, text boxes, connectors (attached at both ends, at one end), cropped pictures, and a before/after list of snapped coordinates.

The output is deterministic. All numbers it prints come from this toy input.

### Real use

With the local `claude` CLI (logged in; no key handled by this package):

```bash
python -m image2ppt slide.png -o out.pptx              # extract + tidy + render + 1 review round
python -m image2ppt slide.png -o out.pptx --rounds 0   # no review round
python -m image2ppt a.png b.png -o deck.pptx           # one slide per image
```

With the Anthropic API:

```bash
export ANTHROPIC_API_KEY=...            # read from the environment only
python -m image2ppt slide.png -o out.pptx --backend anthropic --model <model-name>
```

The model name can also be set with `IMAGE2PPT_MODEL`. The API backend is covered by a unit test with a stubbed HTTP call. The tests do not contact the live service.

Re-run tidy and render on a saved model answer, without any model call:

```bash
python -m image2ppt slide.png -o out.pptx --layout out.work/p1.raw.json --rounds 0
```

Options: `--rounds N`, `--layout FILE`, `--backend {claude,anthropic}`, `--model NAME`, `--no-tidy`, `--preview` (render PNGs with LibreOffice even when `--rounds 0`), `--workdir DIR`.

Work files go to `<out>.work/`: `p1.raw.json` (model output), `p1.r0.json` (layout after tidy), and, when slides are rendered, `r0-1.png` and `p1.r0.compare.png` (original above, rebuilt below). Review rounds add `p1.patchK.json` and the `rK` files.

### Plug in another model

A backend is any object with this method (`image2ppt.llm.LLMClient`):

```python
def complete_json(self, prompt: str, schema: dict, images: Sequence[str | Path] = ()) -> dict: ...
```

Pass it to `image2ppt.convert(images, out_path, client=my_client)`.

## Tests

```bash
pytest -q
```

The tests cover schema validation, tidy (edge alignment, size normalisation, connector snapping inside and outside the threshold), PPTX reopening with shape and connector counts, byte-reproducible output, and the CLI `--layout` path without a model.

## Repository layout

```
image2ppt/
  cli.py        command line
  pipeline.py   extract -> tidy -> render -> review loop, patch application
  schema.py     layout schema, prompts, validator
  llm.py        LLMClient interface; claude CLI, Anthropic API and replay backends
  tidy.py       geometry cleanup
  render.py     python-pptx writer, PPTX statistics
  demo.py       offline demo
examples/       synthetic slide, recorded layout, demo output
tests/
```

## Known limits

- Model coordinates are off by a percent or two of the page width. Tidy pulls them onto clusters and a grid, so the result looks aligned. It moves to the median of a cluster, not to the true position, so sizes and gaps can differ slightly from the source.
- Icons, photos, charts and card stacks are pasted as cropped pictures. The source resolution sets their sharpness.
- A connector end attaches to a box connection point only when it lies within 1.5 % of the page width of an edge midpoint. Otherwise the end sits on the edge but is not attached. On a straight two-point connector, the second end attaches only if the line stays exactly straight.
- Fonts are Arial. Font sizes are derived from Liberation Sans widths (same metrics as Arial). Free text boxes do not wrap. Text inside a box shrinks to 85 % of the estimate before the box grows.
- The model sometimes splits a word across lines or reports a formula inside a box as separate text. Tidy rejoins short fragments and merges close text into the box. Other errors are left to review rounds.
- The element count is usually lower than in the source. Small bullet icons, dividers and decorations are lost when the model does not report them.
- Equal-gap adjustment groups boxes by vertical or horizontal band over the whole slide, not per parent. A row is adjusted only if every box in the band is evenly spaced.
- Model calls can take minutes per image. Each review round adds one more call and one LibreOffice render.

## License

MIT. Copyright (c) 2026 Yifan Xiao.
