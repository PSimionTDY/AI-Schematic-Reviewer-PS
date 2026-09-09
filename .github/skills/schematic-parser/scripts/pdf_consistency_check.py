#!/usr/bin/env python3
"""
pdf_consistency_check.py — Verify the schematic PDF is consistent with the folder name,
has sequential page numbering, and is not older than the Allegro export files.

Usage:
    python pdf_consistency_check.py reviews/SCH25678-1E
    python pdf_consistency_check.py reviews/SCH23729-3

Output:
    - Prints issues to stdout
    - Writes REVIEW/issues_pdf.yaml inside the schematic folder
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import yaml


def is_altium_folder(folder: Path) -> bool:
    """Return True if folder contains an Altium design export."""
    if list(folder.glob('*.PrjPcb')):
        return True
    for nets_dir in folder.glob('Nets *'):
        if (nets_dir / 'OrCadPCB2Netlist').is_dir():
            return True
    return False


# ---------------------------------------------------------------------------
# Pattern sets
# ---------------------------------------------------------------------------

REV_PATTERNS = [
    # Only match on the same line (use [^\n] instead of \s for the separator)
    re.compile(r'\bRev(?:ision)?[.:\s]*([A-Z][A-Z0-9]?)\b'),
    re.compile(r'\bREV[.:\s]*([A-Z][A-Z0-9]?)\b'),
]

# Paper sizes to exclude from revision detection
PAPER_SIZES = {"A0", "A1", "A2", "A3", "A4", "A5", "B0", "B1", "B2", "B3", "B4", "B5"}

SHEET_PATTERNS = [
    re.compile(r'[Ss]heet\s+(\d+)\s+of\s+(\d+)'),
    re.compile(r'[Pp]age\s+(\d+)\s+of\s+(\d+)'),
    re.compile(r'\b(\d+)\s*/\s*(\d+)\b'),
]

SCH_ID_PATTERN = re.compile(r'SCH\d{5}', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_tripled(text: str) -> str:
    """Collapse triple-character rendering artifact common in OrCAD title blocks.

    OrCAD Capture PDFs often render title block text with each character repeated
    three times for a bold effect (e.g. 'SSSiiizzzeee' → 'Size', '333777' → '37').
    This function collapses runs of 3 identical characters to a single character.
    """
    result = []
    i = 0
    while i < len(text):
        c = text[i]
        # Consume a run of this character
        run_end = i
        while run_end < len(text) and text[run_end] == c:
            run_end += 1
        run_len = run_end - i
        # Collapse runs that are multiples of 3 (typical artifact)
        if run_len >= 3:
            result.append(c * (run_len // 3))
        else:
            result.append(text[i:run_end])
        i = run_end
    return "".join(result)


def _page_text(page) -> str:
    """Extract all text from a pdfplumber page, with tripled-char normalization."""
    raw = page.extract_text() or ""
    return _normalize_tripled(raw)


def _find_pdf(folder: Path) -> Path | None:
    """Find the primary schematic PDF in a review folder."""
    # Collect PDFs case-insensitively (handles .PDF extension on case-sensitive systems)
    seen: set[str] = set()
    pdfs: list[Path] = []
    for p in list(folder.glob("*.pdf")) + list(folder.glob("*.PDF")):
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            pdfs.append(p)
    # Prefer the one whose stem matches the folder name (case-insensitive)
    folder_stem = folder.name.lower()
    for pdf in pdfs:
        if pdf.stem.lower().startswith(folder_stem.replace("-", "")):
            return pdf
        if folder_stem in pdf.stem.lower():
            return pdf
    # Fall back to the single pdf, or the largest one
    if len(pdfs) == 1:
        return pdfs[0]
    if pdfs:
        return max(pdfs, key=lambda p: p.stat().st_size)
    return None


def _extract_revision_from_folder(folder_name: str) -> str | None:
    """Extract expected revision letter from a folder name like 'SCH25678-1E' → 'E'."""
    m = re.search(r'SCH\d{5}-\d+([A-Z])', folder_name, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def _read_opj_data(folder: Path) -> dict:
    """Parse the .opj OrCAD project file for sheet names and software version.

    Returns a dict with optional keys:
      - software_version (str)
      - sheets (list[str])  — sheets visible in project tree (may be incomplete)
      - pcb_board (str)     — linked PCB board file path
      - opj_path (Path)
    """
    opj_files = list(folder.glob("*.opj"))
    if not opj_files:
        return {}
    opj_path = opj_files[0]
    try:
        content = opj_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    result: dict = {"opj_path": opj_path}

    m = re.search(r'\(SoftwareVersion\s+"([^"]+)"\)', content)
    if m:
        result["software_version"] = m.group(1)

    # 3-element Path entries inside FileView list sheets the user expanded in the tree
    sheets = re.findall(
        r'\(Path\s+"[^"]+"\s+"[^"]+\.dsn"\s+"([^"]+)"\)',
        content, re.IGNORECASE,
    )
    result["sheets"] = sheets  # may not be exhaustive

    m = re.search(r'"Allegro Netlist Output Board File"\s+"([^"]+)"', content)
    if m:
        result["pcb_board"] = m.group(1)

    return result


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_revision_match(pdf_path: Path, expected_rev: str | None) -> list[dict]:
    """Check 1: Revision string on title block matches folder revision."""
    issues = []
    if expected_rev is None:
        return issues

    found_revs = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for text in [_page_text(page), page.extract_text() or ""]:
                for line in text.splitlines():
                    for pat in REV_PATTERNS:
                        for m in pat.finditer(line):
                            candidate = m.group(1).upper()
                            if re.match(r'^[A-Z0-9]{1,3}$', candidate) and candidate not in PAPER_SIZES:
                                found_revs.add(candidate)

    # Remove numeric-only false positives when letter revision is expected
    if expected_rev and expected_rev.isalpha():
        found_revs = {r for r in found_revs if not r.isdigit()}

    if not found_revs:
        issues.append({
            "severity": "info",
            "type": "pdf_revision_not_found",
            "description": (
                f"Revision string not found in PDF text layer "
                f"(expected Rev {expected_rev} — may be a raster/vector element in title block)"
            ),
            "components": [],
        })
    elif expected_rev not in found_revs:
        revs_str = ", ".join(sorted(found_revs))
        issues.append({
            "severity": "major",
            "type": "pdf_revision_mismatch",
            "description": (
                f"PDF title block shows Rev {revs_str} "
                f"but folder revision is {expected_rev} "
                f"(folder: '{pdf_path.parent.name}') — PDF may be outdated"
            ),
            "components": [],
        })
    return issues


def check_page_numbering(pdf_path: Path) -> list[dict]:
    """Check 2: Sheet X of N numbering is sequential and consistent."""
    issues = []
    page_entries: list[tuple[int, int, int]] = []  # (pdf_page_num, sheet_num, total)

    with pdfplumber.open(pdf_path) as pdf:
        for pdf_page_num, page in enumerate(pdf.pages, 1):
            text = _page_text(page)
            found = False
            for pat in SHEET_PATTERNS:
                m = pat.search(text)
                if m:
                    sheet_num = int(m.group(1))
                    total = int(m.group(2))
                    page_entries.append((pdf_page_num, sheet_num, total))
                    found = True
                    break
            if not found:
                issues.append({
                    "severity": "minor",
                    "type": "page_number_missing",
                    "description": (
                        f"PDF page {pdf_page_num} has no 'Sheet X of N' numbering"
                    ),
                    "components": [],
                })

    if not page_entries:
        return issues

    # Check totals are consistent
    totals = {e[2] for e in page_entries}
    if len(totals) > 1:
        issues.append({
            "severity": "major",
            "type": "page_total_inconsistent",
            "description": (
                f"'Sheet X of N' total is inconsistent across pages: "
                f"found totals {sorted(totals)}"
            ),
            "components": [],
        })

    # Check sheet numbers are sequential (no gaps, no duplicates)
    sheet_nums = [e[1] for e in page_entries]
    if len(sheet_nums) != len(set(sheet_nums)):
        dupes = [n for n in sheet_nums if sheet_nums.count(n) > 1]
        issues.append({
            "severity": "major",
            "type": "page_number_duplicate",
            "description": (
                f"Duplicate sheet numbers found: {sorted(set(dupes))}"
            ),
            "components": [],
        })

    sorted_sheets = sorted(sheet_nums)
    expected = list(range(sorted_sheets[0], sorted_sheets[-1] + 1))
    if sorted_sheets != expected:
        missing = sorted(set(expected) - set(sorted_sheets))
        issues.append({
            "severity": "major",
            "type": "page_number_gap",
            "description": (
                f"Sheet numbering has gaps — missing sheet numbers: {missing}"
            ),
            "components": [],
        })

    return issues


def check_pdf_staleness(pdf_path: Path) -> list[dict]:
    """Check 3: PDF is not older than export source files."""
    issues = []
    folder = pdf_path.parent
    pdf_mtime = pdf_path.stat().st_mtime

    # Altium: compare against .NET file(s) in Nets */OrCadPCB2Netlist/
    source_files: list[tuple[str, float]] = []
    allegro_dir = folder / "allegro"
    if allegro_dir.exists():
        for dat_file in allegro_dir.glob("*.dat"):
            source_files.append((f"allegro/{dat_file.name}", dat_file.stat().st_mtime))
    else:
        for nets_dir in folder.glob('Nets *'):
            orcad_dir = nets_dir / 'OrCadPCB2Netlist'
            if orcad_dir.is_dir():
                for net_file in orcad_dir.glob('*.NET'):
                    rel = f"{nets_dir.name}/OrCadPCB2Netlist/{net_file.name}"
                    source_files.append((rel, net_file.stat().st_mtime))
        for net_file in folder.glob('*.NET'):
            source_files.append((net_file.name, net_file.stat().st_mtime))

    stale_files = []
    for name, mtime in source_files:
        if mtime > pdf_mtime:
            delta_days = (mtime - pdf_mtime) / 86400
            stale_files.append((name, delta_days, mtime))

    if stale_files:
        stale_files.sort(key=lambda x: -x[1])
        worst_name, worst_days, worst_mtime = stale_files[0]
        description = (
            f"PDF exported {_fmt_dt(pdf_mtime)}, "
            f"but {worst_name} was modified {_fmt_dt(worst_mtime)} "
            f"({worst_days:.1f} days later) — schematic may have changed since PDF was exported"
        )
        if len(stale_files) > 1:
            description += f". ({len(stale_files)} source files are newer than the PDF)"
        issues.append({
            "severity": "minor",
            "type": "pdf_staleness",
            "description": description,
            "components": [],
        })

    return issues


def check_schematic_id(pdf_path: Path, folder_name: str) -> list[dict]:
    """Check 4: Schematic ID (e.g. SCH25678-1E or SCH25678) appears on at least one page."""
    issues = []
    found = False

    # Extract the base numeric ID without revision: "SCH25678-1E" → "SCH25678"
    base_id_m = re.search(r'(SCH\d{5})', folder_name, re.IGNORECASE)
    base_id = base_id_m.group(1).upper() if base_id_m else None
    folder_id = folder_name.upper()

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = (_page_text(page) or "").upper()
            # Match full folder ID or base numeric ID
            if folder_id in text or (base_id and base_id in text):
                found = True
                break

    if not found:
        issues.append({
            "severity": "minor",
            "type": "schematic_id_missing",
            "description": (
                f"Schematic ID '{folder_name}' not found on any PDF page title block"
            ),
            "components": [],
        })

    return issues


def check_opj_sheet_count(pdf_path: Path, opj_data: dict) -> list[dict]:
    """Check 5: PDF page count vs sheet count extracted from .opj FileView."""
    issues = []
    opj_sheets = opj_data.get("sheets", [])
    if not opj_sheets:
        return issues

    with pdfplumber.open(pdf_path) as pdf:
        pdf_page_count = len(pdf.pages)

    if pdf_page_count < len(opj_sheets):
        issues.append({
            "severity": "minor",
            "type": "pdf_sheet_count_low",
            "description": (
                f"PDF has {pdf_page_count} page(s) but .opj lists "
                f"{len(opj_sheets)} sheet(s) that were open in the project tree "
                f"({', '.join(sorted(opj_sheets))}) — PDF may be missing sheets or was exported partially"
            ),
            "components": [],
        })

    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_checks(schematic_folder: Path) -> list[dict]:
    """Run all PDF consistency checks. Returns list of issues."""
    pdf_path = _find_pdf(schematic_folder)
    if pdf_path is None:
        return [{
            "severity": "major",
            "type": "pdf_missing",
            "description": f"No PDF found in {schematic_folder}",
            "components": [],
        }]

    folder_name = schematic_folder.name
    altium = is_altium_folder(schematic_folder)
    expected_rev = None if altium else _extract_revision_from_folder(folder_name)
    opj_data = {} if altium else _read_opj_data(schematic_folder)

    print(f"Checking: {pdf_path.relative_to(schematic_folder.parent)}")
    if altium:
        print("  Format: Altium (skipping SCH revision and .opj checks)")
    else:
        print(f"  Expected revision: {expected_rev or '(none — no revision letter in folder name)'}")
        if opj_data.get("software_version"):
            print(f"  OrCAD version (from .opj): {opj_data['software_version']}")
        if opj_data.get("sheets"):
            print(f"  Sheets in .opj ({len(opj_data['sheets'])}): {', '.join(opj_data['sheets'])}")
        if opj_data.get("pcb_board"):
            print(f"  Linked PCB board: {opj_data['pcb_board']}")

    issues = []
    issues += check_revision_match(pdf_path, expected_rev)
    issues += check_page_numbering(pdf_path)
    issues += check_pdf_staleness(pdf_path)
    if not altium:
        issues += check_schematic_id(pdf_path, folder_name)
        issues += check_opj_sheet_count(pdf_path, opj_data)

    return issues


def write_yaml(schematic_folder: Path, issues: list[dict]) -> Path:
    """Write issues to REVIEW/issues_pdf.yaml."""
    review_dir = schematic_folder / "REVIEW"
    review_dir.mkdir(exist_ok=True)
    out_path = review_dir / "issues_pdf.yaml"

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": "pdf_consistency_check",
        "issues": issues,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(payload, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Check PDF consistency for a schematic review folder."
    )
    parser.add_argument(
        "schematic_folder",
        help="Path to the schematic folder, e.g. reviews/SCH25678-1E",
    )
    args = parser.parse_args()

    folder = Path(args.schematic_folder).resolve()
    if not folder.is_dir():
        print(f"ERROR: '{folder}' is not a directory", file=sys.stderr)
        sys.exit(1)

    issues = run_checks(folder)
    out_path = write_yaml(folder, issues)

    # Print summary
    print()
    if not issues:
        print("✅  No PDF consistency issues found.")
    else:
        majors = [i for i in issues if i["severity"] == "major"]
        minors = [i for i in issues if i["severity"] == "minor"]
        print(f"Found {len(issues)} issue(s): {len(majors)} major, {len(minors)} minor")
        print()
        for issue in issues:
            badge = "🔴" if issue["severity"] == "major" else "🟡"
            print(f"  {badge} [{issue['type']}]")
            print(f"     {issue['description']}")
            print()

    print(f"Written: {out_path}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
