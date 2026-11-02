"""Command-line entry point for the merged ClearRead OCR/UI module."""
from __future__ import annotations

import argparse

from ui import run


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indra OCR: save the best OCR result in one TXT")
    parser.add_argument("source", help="Local PDF/image path or HTTP(S) URL")
    parser.add_argument("--output", help="Output directory (default: document name in current folder)")
    parser.add_argument("--dpi", type=int, default=250)
    parser.add_argument("--min-score", type=int, default=80)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--columns", type=int, default=120)
    parser.add_argument("--keep-images", action="store_true")
    args = parser.parse_args()
    run(args.source, args.output, args.dpi, args.min_score, args.timeout,
        args.keep_images, args.columns)
