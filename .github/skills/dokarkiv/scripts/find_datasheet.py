#!/usr/bin/env python3
"""
DOKARKIV Datasheet Finder

Finds and copies component datasheets from the internal DOKARKIV network
file share (\\SE-ARN-FS2\Vol1\DOKARKIV).

Internal part numbers are of the form "T19200" (a leading "T" followed by
digits). On the file share, the leading letter is stripped, and files for a
given part are stored inside a "thousand-range" folder rather than one
folder per part. For example, part "T119200" is grouped under the range
folder "119000" (parts 119000-119999), with the datasheet file named
something like "119200_B.pdf" (a revision-lettered filename that starts
with the numeric part id).

Usage:
    # Find datasheet PDFs for a part and copy them locally
    python find_datasheet.py T19200

    # List available files without copying
    python find_datasheet.py T19200 --list

    # Copy to a custom output directory (default: datasheets/)
    python find_datasheet.py T19200 --output datasheets/

    # Batch: read part numbers from a BOM CSV
    python find_datasheet.py --bom bom.csv --filter "U,IC" --output datasheets/
"""

import argparse
import csv
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DOKARKIV_ROOT = r"\\SE-ARN-FS2\Vol1\DOKARKIV"

_PART_RE = re.compile(r"^([A-Za-z]*)(\d+)$")


@dataclass
class DownloadResult:
    """Result of a single-file copy operation."""
    success: bool
    filename: str = ""
    filepath: Optional[str] = None
    error: Optional[str] = None
    size_bytes: int = 0


def is_share_available() -> bool:
    """Check if the DOKARKIV UNC root is accessible."""
    return Path(DOKARKIV_ROOT).exists()


def normalize_part_id(part_id: str) -> str:
    """Normalize a part id (strip whitespace, uppercase)."""
    return part_id.strip().upper()


def numeric_id_for_part(part_id: str) -> str:
    """Return the numeric portion of an internal part number.

    Internal part numbers are prefixed with a letter (e.g. "T19200"). The
    file share drops the leading letter. If the part id is already purely
    numeric, it is returned unchanged.
    """
    part_id = normalize_part_id(part_id)
    m = _PART_RE.match(part_id)
    if not m:
        return part_id
    return m.group(2)


def range_folder_for_numeric_id(numeric_id: str) -> str:
    """Return the DOKARKIV "thousand-range" folder name for a numeric part id.

    DOKARKIV groups parts into folders named after the start of a thousand
    range, e.g. part "119200" is stored in folder "119000" (covering
    119000-119999). Non-numeric or short ids fall back to the id itself.
    """
    if not numeric_id.isdigit():
        return numeric_id
    n = int(numeric_id)
    range_start = (n // 1000) * 1000
    return str(range_start)


def get_range_dir(part_id: str) -> Path:
    """Return the Path to the DOKARKIV range folder for *part_id* (may not exist)."""
    numeric_id = numeric_id_for_part(part_id)
    range_folder = range_folder_for_numeric_id(numeric_id)
    return Path(DOKARKIV_ROOT) / range_folder


def find_datasheets(part_id: str) -> List[Path]:
    """Return all PDF files in DOKARKIV matching *part_id*.

    Parts are stored in a per-part subfolder inside the thousand-range folder,
    named with the **original** part id (letter prefix kept, e.g. "T951058",
    or just the numeric id if the part had no letter prefix, e.g. "1950500").
    All PDF files inside that subfolder belong to the part (revision-lettered
    datasheets, e.g. "T951058_10.pdf", "T951058_20.pdf").

    Falls back to matching PDF files named directly in the range folder
    (older/flat layout) for compatibility.
    """
    part_id = normalize_part_id(part_id)
    numeric_id = numeric_id_for_part(part_id)
    range_dir = get_range_dir(part_id)
    if not range_dir.exists():
        return []

    matches: List[Path] = []

    # Preferred layout: per-part subfolder, name matched case-insensitively.
    for entry in range_dir.iterdir():
        try:
            if not entry.is_dir():
                continue
        except (PermissionError, OSError):
            continue
        if entry.name.upper() == part_id or entry.name == numeric_id:
            try:
                sub_entries = list(entry.iterdir())
            except (PermissionError, OSError):
                continue
            for f in sub_entries:
                try:
                    if f.is_file() and f.suffix.lower() == ".pdf":
                        matches.append(f)
                except (PermissionError, OSError):
                    continue
    if matches:
        return matches

    # Fallback: flat files directly in the range folder.
    for entry in range_dir.iterdir():
        try:
            if not entry.is_file():
                continue
        except (PermissionError, OSError):
            continue
        if entry.suffix.lower() != ".pdf":
            continue
        stem = entry.stem
        # Match "119200", "119200_B", "119200-C", etc. -- numeric id followed
        # by end-of-string or a separator, so "1192001" does not match "119200".
        if stem == numeric_id or re.match(rf"^{re.escape(numeric_id)}[_\-]", stem):
            matches.append(entry)
    return matches


def download_datasheets(part_id: str, output_dir: str = "datasheets") -> List[DownloadResult]:
    """Copy all datasheet PDFs for *part_id* from DOKARKIV into *output_dir*/<part_id>/.

    Skips files that already exist locally. Returns a list of DownloadResult.
    """
    part_id = normalize_part_id(part_id)
    output_path = Path(output_dir) / part_id
    results: List[DownloadResult] = []

    print(f"Finding datasheets for {part_id} (DOKARKIV range folder: {get_range_dir(part_id).name})...")
    pdfs = find_datasheets(part_id)

    if not pdfs:
        print(f"No datasheets found for {part_id}")
        return results

    output_path.mkdir(parents=True, exist_ok=True)
    print(f"Found {len(pdfs)} PDF(s)")

    seen_names: set = set()
    for pdf in pdfs:
        filename = pdf.name
        if filename in seen_names:
            continue
        seen_names.add(filename)

        dest = output_path / filename
        if dest.exists():
            print(f"  Skipped (exists): {filename}")
            results.append(DownloadResult(
                success=True, filename=filename, filepath=str(dest),
                size_bytes=dest.stat().st_size,
            ))
            continue

        try:
            shutil.copy2(pdf, dest)
            size_bytes = dest.stat().st_size
            print(f"  Copied: {filename} ({size_bytes / 1024:.1f} KB)")
            results.append(DownloadResult(
                success=True, filename=filename, filepath=str(dest),
                size_bytes=size_bytes,
            ))
        except Exception as e:
            print(f"  Failed: {filename} - {e}")
            results.append(DownloadResult(success=False, filename=filename, error=str(e)))

    return results


def extract_parts_from_bom(bom_file: str, filter_types: Optional[List[str]] = None) -> List[str]:
    """Extract part numbers from a BOM CSV, optionally filtered by ref-des prefix."""
    parts: List[str] = []
    with open(bom_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    for row in rows:
        pos = row.get("Pos") or row.get("Ref") or row.get("RefDes") or ""
        if filter_types and not any(pos.upper().startswith(t.upper()) for t in filter_types):
            continue
        part_id = row.get("Part Number") or row.get("Part_Number") or row.get("PN") or row.get("MPN")
        if part_id and part_id.strip():
            parts.append(part_id.strip())

    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description="Find and copy datasheets from the DOKARKIV file share")
    parser.add_argument("part_id", nargs="?", help="Internal part number (e.g. T19200)")
    parser.add_argument("--list", "-l", action="store_true", help="List available datasheets without copying")
    parser.add_argument("--output", "-o", default="datasheets", help="Output directory (default: datasheets/)")
    parser.add_argument("--bom", help="BOM CSV file for batch lookup")
    parser.add_argument("--filter", help="Filter BOM by ref designator prefix, e.g. 'U,IC'")
    parser.add_argument("--limit", type=int, help="Maximum number of parts to process from BOM")

    args = parser.parse_args()

    if not is_share_available():
        print(f"ERROR: DOKARKIV file share not accessible: {DOKARKIV_ROOT}", file=sys.stderr)
        return 1

    if args.bom:
        filter_types = [t.strip() for t in args.filter.split(",")] if args.filter else None
        parts = extract_parts_from_bom(args.bom, filter_types)
        if args.limit:
            parts = parts[: args.limit]
        print(f"Processing {len(parts)} parts from {args.bom}")
        failures = 0
        for part_id in parts:
            results = download_datasheets(part_id, args.output)
            if not results or not any(r.success for r in results):
                failures += 1
        return 0 if failures == 0 else 1

    if not args.part_id:
        parser.print_help()
        return 1

    if args.list:
        pdfs = find_datasheets(args.part_id)
        folder = get_range_dir(args.part_id)
        if not pdfs:
            print(f"No datasheets found for {args.part_id} in {folder}")
            return 1
        print(f"Datasheets for {args.part_id} in {folder}:")
        for p in pdfs:
            print(f"  {p.name}")
        return 0

    results = download_datasheets(args.part_id, args.output)
    return 0 if results and all(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
