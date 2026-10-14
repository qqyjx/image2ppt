"""Command line: python -m image2ppt slide.png -o out.pptx [options]."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import llm, pipeline


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="image2ppt",
        description="Convert slide images or framework figures into an editable PPTX with native shapes.")
    ap.add_argument("images", nargs="+", help="input image(s), one slide per image")
    ap.add_argument("-o", "--out", required=True, help="output .pptx path")
    ap.add_argument("--rounds", type=int, default=1,
                    help="model review rounds after the first render (default 1; 0 = none)")
    ap.add_argument("--layout", default=None,
                    help="reuse a layout JSON instead of calling a model for extraction (single image)")
    ap.add_argument("--backend", choices=["claude", "anthropic"], default="claude",
                    help="model backend: local `claude` CLI (default) or the Anthropic API "
                         "(key from ANTHROPIC_API_KEY)")
    ap.add_argument("--model", default=None, help="model name passed to the backend")
    ap.add_argument("--no-tidy", action="store_true", help="skip geometry cleanup")
    ap.add_argument("--preview", action="store_true",
                    help="also render PNG previews with LibreOffice (needs soffice and pdftoppm)")
    ap.add_argument("--workdir", default=None, help="directory for intermediate files (default: <out>.work)")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    needs_model = args.layout is None or args.rounds > 0
    client = llm.make_client(args.backend, args.model) if needs_model else None
    try:
        pipeline.convert(args.images, args.out, client=client, rounds=args.rounds, layout=args.layout,
                         do_tidy=not args.no_tidy, workdir=args.workdir, preview=args.preview)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
