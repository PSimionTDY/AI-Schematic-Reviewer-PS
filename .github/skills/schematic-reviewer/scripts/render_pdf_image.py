#!/usr/bin/env python3
"""
Render a PDF page (optionally cropped) to a PNG image, for cases where the
information needed is graphical rather than textual -- e.g. a transistor/IC
pinout diagram, a package top-view drawing, or a truth table rendered as a
figure rather than real text.

Text extraction (extract_pdf_text.py / LiteParse OCR) is unreliable for these
cases: pinout diagrams are drawings with pin numbers and pin names placed as
short disconnected text fragments scattered around a package outline, and OCR
frequently mangles them or loses the pin-to-position association entirely.
Rendering the page (or just the diagram region) as an image lets the agent's
own vision read the diagram directly, the same way a human engineer would.

Usage:
    # Render a full page to PNG (1-based page number)
    python render_pdf_image.py <pdf> --page 5

    # Render a full page at higher resolution (default dpi=200)
    python render_pdf_image.py <pdf> --page 5 --dpi 300

    # Render only a cropped region of the page -- coordinates are fractions of
    # page width/height (0.0-1.0), so you don't need to know the page size in
    # points. Use this once you know roughly where the diagram sits (e.g. from
    # extract_pdf_text.py output or a first full-page render).
    python render_pdf_image.py <pdf> --page 5 --crop 0.5,0.0,1.0,0.4

    # Explicit output path (default: alongside the PDF, auto-named)
    python render_pdf_image.py <pdf> --page 5 --output out.png

After running, use the `view`/image-reading tool on the printed output path to
actually read the diagram -- this script only produces the image.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def render_page(
    pdf_path: Path,
    page_num: int,
    dpi: int,
    crop: tuple[float, float, float, float] | None,
    output_path: Path | None,
) -> Path:
    """Render 1-based *page_num* of *pdf_path* to a PNG, returning the output path."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(
            "ERROR: PyMuPDF (fitz) not installed. Run: venv\\Scripts\\pip.exe install PyMuPDF",
            file=sys.stderr,
        )
        sys.exit(1)

    doc = fitz.open(str(pdf_path))
    try:
        if page_num < 1 or page_num > len(doc):
            print(
                f"ERROR: page {page_num} out of range (PDF has {len(doc)} pages)",
                file=sys.stderr,
            )
            sys.exit(1)

        page = doc[page_num - 1]
        rect = page.rect

        clip = None
        if crop:
            x0f, y0f, x1f, y1f = crop
            clip = fitz.Rect(
                rect.x0 + x0f * rect.width,
                rect.y0 + y0f * rect.height,
                rect.x0 + x1f * rect.width,
                rect.y0 + y1f * rect.height,
            )

        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, clip=clip)

        if output_path is None:
            suffix = f"_p{page_num}"
            if crop:
                suffix += "_crop"
            output_path = pdf_path.with_name(f"{pdf_path.stem}{suffix}.png")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(output_path))
        return output_path
    finally:
        doc.close()


def parse_crop(spec: str) -> tuple[float, float, float, float]:
    parts = [float(p.strip()) for p in spec.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop must be 'x0,y0,x1,y1' (fractions 0.0-1.0)")
    x0, y0, x1, y1 = parts
    for v in parts:
        if not (0.0 <= v <= 1.0):
            raise ValueError("--crop values must be between 0.0 and 1.0")
    if x0 >= x1 or y0 >= y1:
        raise ValueError("--crop requires x0 < x1 and y0 < y1")
    return x0, y0, x1, y1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a PDF page (optionally cropped) to a PNG image for visual reading.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--page", type=int, required=True, metavar="N", help="1-based page number")
    parser.add_argument(
        "--dpi", type=int, default=200, metavar="N",
        help="Rendering DPI (default 200; use 300+ for dense pinout diagrams)",
    )
    parser.add_argument(
        "--crop", metavar="x0,y0,x1,y1",
        help="Crop region as fractions of page width/height, e.g. 0.5,0.0,1.0,0.4",
    )
    parser.add_argument("--output", metavar="PATH", help="Output PNG path (default: auto-named next to the PDF)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    crop = parse_crop(args.crop) if args.crop else None
    output_path = Path(args.output) if args.output else None

    result_path = render_page(pdf_path, args.page, args.dpi, crop, output_path)
    print(f"Rendered page {args.page} of {pdf_path.name} -> {result_path}")
    print("Use the image-viewing tool on this path to read the diagram.")


if __name__ == "__main__":
    main()
