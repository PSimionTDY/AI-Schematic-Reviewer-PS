#!/usr/bin/env python3
"""
Extract text from a PDF using LiteParse (lit CLI) — single pre-approved command for agent use.

Uses `@llamaindex/liteparse` (`lit` CLI) instead of pdfplumber/PyMuPDF.
LiteParse runs fully locally, uses OCR by default, and supports structured JSON output.

Usage:
    extract_pdf_text.py <pdf>                    # all pages
    extract_pdf_text.py <pdf> --pages 1-10       # page range (1-based)
    extract_pdf_text.py <pdf> --pages 5          # single page
    extract_pdf_text.py <pdf> --pages 1-5,20-25  # multiple ranges
    extract_pdf_text.py <pdf> --first 20         # first N pages
    extract_pdf_text.py <pdf> --limit 500        # cap output lines
    extract_pdf_text.py <pdf> --info             # page count + metadata only
    extract_pdf_text.py <pdf> --no-ocr           # skip OCR (text-layer PDFs only)
    extract_pdf_text.py <pdf> --dpi 300          # higher DPI for better quality

This script exists so agents can extract PDF content in one pre-approved
command instead of generating ad-hoc inline -c snippets per page range.
Backend: LiteParse (lit CLI), requires `npm i -g @llamaindex/liteparse`.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# On Windows, npm global scripts are .cmd wrappers — use shell=True or the .cmd path
_LIT_CMD = ["lit.cmd"] if sys.platform == "win32" else ["lit"]


def run_lit(args: list[str]) -> subprocess.CompletedProcess:
    """Run the lit CLI and return the completed process."""
    return subprocess.run(
        [*_LIT_CMD, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def get_page_count(path: Path) -> int:
    """Get total page count by parsing with JSON output and no OCR (fast)."""
    result = run_lit(["parse", str(path), "--format", "json", "--no-ocr"])
    try:
        data = json.loads(result.stdout)
        pages = data.get("pages", [])
        return len(pages)
    except (json.JSONDecodeError, AttributeError):
        return 0


def parse_pages(spec: str, total: int) -> list[int]:
    """Parse page spec like '1-5,20,25-30' into 0-based page indices."""
    indices: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i = max(0, int(lo) - 1)
            hi_i = min(total - 1, int(hi) - 1)
            indices.extend(range(lo_i, hi_i + 1))
        else:
            i = int(part) - 1
            if 0 <= i < total:
                indices.append(i)
    # deduplicate, preserve order
    seen: set[int] = set()
    result: list[int] = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            result.append(i)
    return result


def pages_to_lit_spec(indices: list[int]) -> str:
    """Convert 0-based page indices to lit --target-pages spec (1-based)."""
    one_based = [i + 1 for i in indices]
    return ",".join(str(p) for p in one_based)


def extract_with_liteparse(
    path: Path,
    page_indices: list[int],
    limit: int,
    no_ocr: bool,
    dpi: int,
) -> None:
    """Extract text from the given pages using lit CLI (JSON mode)."""
    lit_args = ["parse", str(path), "--format", "json"]
    if page_indices:
        lit_args += ["--target-pages", pages_to_lit_spec(page_indices)]
    if no_ocr:
        lit_args.append("--no-ocr")
    if dpi != 150:
        lit_args += ["--dpi", str(dpi)]

    result = run_lit(lit_args)
    if result.returncode != 0:
        print(f"ERROR: lit exited with code {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Fallback: lit output is plain text
        lines = result.stdout.splitlines()
        lines_out = 0
        for line in lines:
            print(line)
            lines_out += 1
            if limit and lines_out >= limit:
                print(f"\n[output truncated at {limit} lines — use --limit to increase]")
                return
        return

    pages = data.get("pages", [])
    lines_out = 0
    for page_data in pages:
        page_num = page_data.get("page", "?")
        print(f"\n=== PAGE {page_num} ===")
        # LiteParse JSON: pages[].text or pages[].items[].text
        text = page_data.get("text", "")
        if not text:
            items = page_data.get("items", [])
            text = "\n".join(item.get("text", "") for item in items if item.get("text"))
        if text:
            for line in text.splitlines():
                print(line)
                lines_out += 1
                if limit and lines_out >= limit:
                    print(f"\n[output truncated at {limit} lines — use --limit to increase]")
                    return
        else:
            print("[no text extracted from this page]")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Extract text from a PDF using LiteParse — one pre-approved command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument(
        "--pages",
        metavar="SPEC",
        help="Page range spec: '1-10', '5', '1-5,20-25' (1-based)",
    )
    parser.add_argument(
        "--first",
        type=int,
        metavar="N",
        help="Extract only the first N pages",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Cap output at N lines (0 = unlimited)",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print page count and metadata only, no text",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        default=True,
        help="Disable OCR — default ON (text-layer PDFs). Use --ocr to enable Tesseract.js OCR.",
    )
    parser.add_argument(
        "--ocr",
        dest="no_ocr",
        action="store_false",
        help="Enable Tesseract.js OCR (slow, use for scanned/image PDFs only)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        metavar="N",
        help="Rendering DPI (default 150; use 300 for higher quality)",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Verify lit is available
    check = run_lit(["--version"])
    if check.returncode != 0:
        print(
            "ERROR: lit CLI not found. Install with: npm i -g @llamaindex/liteparse",
            file=sys.stderr,
        )
        sys.exit(1)

    total = get_page_count(pdf_path)
    if total == 0:
        print("ERROR: could not open PDF or PDF has no pages.", file=sys.stderr)
        sys.exit(1)

    if args.info:
        print(f"File   : {pdf_path}")
        print(f"Pages  : {total}")
        print(f"Backend: LiteParse (lit {check.stdout.strip()})")
        return

    # Determine which pages to extract
    if args.pages:
        indices = parse_pages(args.pages, total)
    elif args.first:
        indices = list(range(min(args.first, total)))
    else:
        indices = list(range(total))

    print(f"[{pdf_path.name} — {total} pages total — extracting pages: "
          f"{indices[0]+1}–{indices[-1]+1} ({len(indices)} pages)]")
    print(f"[backend: LiteParse {check.stdout.strip()} — OCR: {'off' if args.no_ocr else 'on'} — DPI: {args.dpi}]")

    extract_with_liteparse(pdf_path, indices, args.limit, args.no_ocr, args.dpi)


if __name__ == "__main__":
    main()
