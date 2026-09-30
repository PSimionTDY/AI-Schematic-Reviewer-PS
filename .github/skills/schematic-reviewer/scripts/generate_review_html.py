#!/usr/bin/env python3
"""
Generate review.html from review.db (or legacy schematic.yaml + issues.yaml).

Usage:
    python generate_review_html.py reviews/SCH25678-1E/REVIEW/review.db
    python generate_review_html.py reviews/SCH25678-1E              # auto-detects REVIEW/review.db
    python generate_review_html.py reviews/SCH25678-1E --output path/to/review.html
    python generate_review_html.py reviews/SCH25678-1E --pdf reviews/SCH25678-1E/SCH25678-1E.pdf
    python generate_review_html.py --schematic sample/schematic.yaml --issues sample/issues.yaml

Primary data source is review.db (SQLite). Legacy YAML fallback is used when the
folder argument resolves to a directory with no REVIEW/review.db present.

The schematic PDF is auto-detected (*.pdf in the folder) and embedded as base64
so the Issues tab loads it automatically without a file picker. Use --pdf to
specify an explicit PDF path, or omit for a placeholder message in the panes.

Template markers used:
    // %%SCHEMATIC_DATA%%
    const SCHEMATIC_DATA = { ... };
    // %%SCHEMATIC_DATA_END%%

    // %%ISSUES_DATA%%
    const ISSUES_DATA = { ... };
    // %%ISSUES_DATA_END%%

    // %%PDF_DATA%%
    const PDF_DATA = "...base64..." | null;
    // %%PDF_DATA_END%%

    // %%COMP_PAGE_MAP%%
    const COMP_PAGE_MAP = { ... } | null;
    // %%COMP_PAGE_MAP_END%%
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Ensure sibling scripts (db_io, validate_issues) are importable
_SCRIPTS_DIR = Path(__file__).parent.resolve()
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from db_io import get_connection as _get_db_connection
    _DB_IO_AVAILABLE = True
except ImportError:
    _DB_IO_AVAILABLE = False


TEMPLATE_NAME = "review.html"


def find_template(script_dir: Path) -> Path:
    """Locate the review.html template relative to this script."""
    candidates = [
        script_dir / TEMPLATE_NAME,
        script_dir / "templates" / TEMPLATE_NAME,
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"Template '{TEMPLATE_NAME}' not found. Looked in:\n" +
        "\n".join(f"  {p}" for p in candidates)
    )


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Source-label mapping (DB path)
# ---------------------------------------------------------------------------

_SOURCE_LABELS: dict[str, str] = {
    'verify_bom':                  'BOM',
    'verify_temperature_ratings':  'Temperature',
    'check_price_availability':    'Supply Chain',
    'check_obsolescence':          'Supply Chain',
    'estimate_power_consumption':  'Power',
    'verify_capacitor_voltage':    'Passives',
    'verify_resistor_power':       'Passives',
    'verify_ic_pins':              'IC Review',
}


def load_from_db(db_path: Path) -> tuple[dict, dict]:
    """Read schematic and issues data from review.db.

    Returns ``(schematic_dict, issues_dict)`` in the same structure
    expected by :func:`inject_data`.
    """
    if not _DB_IO_AVAILABLE:
        raise ImportError("db_io not found; cannot read from review.db")

    conn = _get_db_connection(db_path)

    # --- meta ---
    meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM meta')}

    # --- nets ---
    nets: dict = {}
    for r in conn.execute('SELECT * FROM nets'):
        d = {k: r[k] for k in r.keys() if r[k] is not None and k != 'name'}
        nets[r['name']] = d

    # --- components ---
    components: dict = {}
    for row in conn.execute('SELECT * FROM components'):
        comp = {k: row[k] for k in row.keys() if row[k] is not None}
        ref = comp.pop('ref')
        comp['pins'] = {}
        components[ref] = comp

    # --- pins (attach to components) ---
    for row in conn.execute('SELECT * FROM pins'):
        ref = row['ref']
        if ref in components:
            pin_data = {
                k: row[k] for k in row.keys()
                if row[k] is not None and k not in ('ref', 'pin')
            }
            components[ref]['pins'][row['pin']] = pin_data

    # --- pipeline ---
    stages: dict = {}
    for r in conn.execute('SELECT * FROM pipeline_stages'):
        d = {k: r[k] for k in r.keys() if r[k] is not None and k != 'stage'}
        stages[r['stage']] = d
    pipeline = {'stages': stages} if stages else {}

    schematic: dict = {}
    if meta:
        schematic['meta'] = meta
    if nets:
        schematic['nets'] = nets
    if components:
        schematic['components'] = components
    if pipeline:
        schematic['pipeline'] = pipeline

    # --- issues ---
    # Order by severity rank (critical first, info last), not alphabetically —
    # plain `ORDER BY severity` would sort as critical, info, major, minor,
    # question, which is not decreasing severity.
    _SEVERITY_RANK = {
        "critical": 0,
        "major": 1,
        "minor": 2,
        "question": 3,
        "info": 4,
    }
    issues_list: list[dict] = []
    for row in conn.execute('SELECT * FROM issues'):
        issue = {k: row[k] for k in row.keys()}
        for json_field in ('refs', 'components', 'also_reported_by'):
            if issue.get(json_field):
                try:
                    issue[json_field] = json.loads(issue[json_field])
                except (json.JSONDecodeError, TypeError):
                    pass
        issue['source_label'] = _SOURCE_LABELS.get(issue.get('source') or '', 'General')
        issues_list.append(issue)

    issues_list.sort(
        key=lambda i: (
            _SEVERITY_RANK.get((i.get('severity') or '').lower(), 99),
            i.get('id') or '',
        )
    )

    conn.close()

    return schematic, {'issues': issues_list}


def find_pdf(folder: Path) -> Path | None:
    """Find the first PDF in the given folder (non-recursive, case-insensitive extension)."""
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    return pdfs[0] if pdfs else None


def load_pdf_base64(pdf_path: Path) -> str:
    """Read a PDF and return it as a base64 string."""
    return base64.b64encode(pdf_path.read_bytes()).decode("ascii")


def build_comp_page_map(pdf_path: Path, schematic: dict) -> dict:
    """
    Extract the PDF page and position of each component refdes using pdfplumber.

    Returns {ref: {"page": int, "x_pct": float, "y_pct": float}} where
    x_pct/y_pct are 0-1 fractions of page width/height with top-left origin
    (matching the HTML canvas coordinate system directly, no Y-flip needed).

    Components whose refdes text pdfplumber cannot find are omitted; the
    viewer falls back to sheet-based page navigation for those.
    """
    import re

    try:
        import pdfplumber
    except ImportError:
        print("  pdfplumber not installed — skipping component position map", file=sys.stderr)
        return {}

    known_refs = set(schematic.get("components", {}).keys())
    if not known_refs:
        return {}

    # Regex mirrors buildRefdesPageMap in review.html: match component-like tokens
    # anywhere inside a text item (e.g. "R20" inside "R20 470R").
    ref_pattern = re.compile(r'\b([A-Z]{1,4}\d+)\b')

    # Also match multi-part suffixes like "U39B" → base ref "U39"
    multipart_pattern = re.compile(r'\b([A-Z]{1,4}\d+)([A-F])\b')

    result: dict = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                pw = float(page.width)
                ph = float(page.height)

                # Pass 1: loose horizontal word extraction (catches most refs)
                words = page.extract_words(x_tolerance=5, y_tolerance=3) or []
                for w in words:
                    tok = w["text"].strip()
                    cx = (w["x0"] + w["x1"]) / 2.0
                    cy = (w["top"] + w["bottom"]) / 2.0
                    entry = {"page": page_num, "x_pct": round(cx / pw, 4), "y_pct": round(cy / ph, 4)}
                    if tok in known_refs and tok not in result:
                        result[tok] = entry
                    # Multi-part: "U39B" → also record under base "U39" (part A takes priority)
                    m = multipart_pattern.fullmatch(tok)
                    if m:
                        base = m.group(1)
                        suffix = m.group(2)
                        if base in known_refs and (base not in result or suffix == 'A'):
                            result[base] = entry

                # Pass 2: scan char runs — handles both horizontal fragments AND
                # vertical text (common for component labels in Allegro schematics).
                # Chars are sorted by (x0, y0); group by same x0 column (vertical)
                # or same y0 row (horizontal) with small gap tolerance.
                chars = page.chars or []

                # Horizontal groups: same y0, consecutive x
                _scan_runs(chars, known_refs, ref_pattern, result, page_num, pw, ph,
                           primary="y0", secondary="x0", gap_key="x0", prev_key="x1",
                           primary_tol=3, gap_tol=8,
                           multipart_pattern=multipart_pattern)

                # Vertical groups: same x0, consecutive y (rotated text like "R20")
                chars_by_x = sorted(chars, key=lambda c: (round(c["x0"]), c["y0"]))
                _scan_runs(chars_by_x, known_refs, ref_pattern, result, page_num, pw, ph,
                           primary="x0", secondary="y0", gap_key="y0", prev_key="y0",
                           primary_tol=3, gap_tol=8,
                           multipart_pattern=multipart_pattern)

    except Exception as exc:
        print(f"  Warning: pdfplumber extraction failed: {exc}", file=sys.stderr)

    print(f"  Component positions extracted: {len(result)}/{len(known_refs)} found in PDF")
    return result


def _scan_runs(chars: list, known_refs: set, pattern, result: dict,
               page_num: int, pw: float, ph: float,
               primary: str, secondary: str, gap_key: str, prev_key: str,
               primary_tol: float, gap_tol: float,
               multipart_pattern=None) -> None:
    """Group chars into runs along one axis and regex-scan each run."""
    run: list = []
    for ch in chars:
        gap = ch[gap_key] - run[-1][prev_key] if run else None
        if (run and
                abs(ch[primary] - run[-1][primary]) < primary_tol and
                gap is not None and 0 <= gap < gap_tol):
            run.append(ch)
        else:
            if run:
                _scan_char_group(run, known_refs, pattern, result, page_num, pw, ph,
                                 multipart_pattern=multipart_pattern)
            run = [ch]
    if run:
        _scan_char_group(run, known_refs, pattern, result, page_num, pw, ph,
                         multipart_pattern=multipart_pattern)


def _scan_char_group(chars: list, known_refs: set, pattern, result: dict,
                     page_num: int, pw: float, ph: float,
                     multipart_pattern=None) -> None:
    """Regex-scan a group of consecutive chars and record any known refdes hits."""
    # Build a parallel list of (cumulative_start, char) so regex match positions
    # map back to chars even when a char's text is more than one character.
    offsets = []
    parts = []
    pos = 0
    for ch in chars:
        t = ch["text"]
        offsets.append(pos)
        parts.append(t)
        pos += len(t)
    text = "".join(parts)
    offsets.append(pos)  # sentinel

    def _record(tok: str, s: int, e: int, suffix: str = '') -> None:
        matched = [ch for i, ch in enumerate(chars)
                   if offsets[i] < e and offsets[i+1] > s]
        if not matched:
            return
        x0 = min(c["x0"] for c in matched)
        x1 = max(c["x1"] for c in matched)
        ct = min(c["top"] for c in matched)
        cb = max(c["bottom"] for c in matched)
        entry = {
            "page": page_num,
            "x_pct": round((x0 + x1) / 2.0 / pw, 4),
            "y_pct": round((ct + cb) / 2.0 / ph, 4),
        }
        if tok in known_refs and tok not in result:
            result[tok] = entry
        # Multi-part: "U39B" → also store under base "U39" (part A wins)
        if multipart_pattern and suffix:
            m2 = multipart_pattern.fullmatch(tok + suffix)
            if m2:
                base = m2.group(1)
                if base in known_refs and (base not in result or suffix == 'A'):
                    result[base] = entry

    for m in pattern.finditer(text):
        tok = m.group(1)
        # Check what follows the match in the text (possible multi-part suffix)
        suffix_char = text[m.end()] if m.end() < len(text) else ''
        if suffix_char.isalpha() and suffix_char.isupper():
            _record(tok, m.start(), m.end() + 1, suffix=suffix_char)
        else:
            _record(tok, m.start(), m.end())


def build_pin_pos_map(pdf_path: Path, schematic: dict, comp_page_map: dict) -> dict:
    """
    For each component that has sub-parts (U39A, U39B…), extract the pin numbers
    printed near each sub-part in the PDF and return a mapping:

        {ref: {pin_str: {"page": int, "x_pct": float, "y_pct": float}}}

    This lets the viewer navigate to the exact sub-part that carries the pin
    referenced by an issue, rather than always using the base-ref position.

    Strategy: for each sub-part found in the PDF (via comp_page_map entries that
    were recorded from multi-part text like "U39B"), scan chars on the same page
    within a neighbourhood and collect digit-run tokens (pin numbers).  Associate
    each pin number with the sub-part's position.
    """
    try:
        import pdfplumber
    except ImportError:
        return {}

    # Build a map: base_ref → list of (sub_ref, entry) from comp_page_map
    # We detect sub-parts by re-scanning the PDF for "U39A"/"U39B" style tokens
    import re as _re
    multipart_pat = _re.compile(r'\b([A-Z]{1,4}\d+)([A-F])\b')
    digit_pat = _re.compile(r'^\d+$')

    # Collect sub-part positions: sub_ref → entry
    sub_part_positions: dict = {}  # "U39B" → {page, x_pct, y_pct}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                pw = float(page.width)
                ph = float(page.height)
                words = page.extract_words(x_tolerance=5, y_tolerance=3) or []
                for w in words:
                    tok = w["text"].strip()
                    m = multipart_pat.fullmatch(tok)
                    if m:
                        cx = (w["x0"] + w["x1"]) / 2.0
                        cy = (w["top"] + w["bottom"]) / 2.0
                        sub_part_positions[tok] = {
                            "page": page_num,
                            "x_pct": round(cx / pw, 4),
                            "y_pct": round(cy / ph, 4),
                            "_x0": w["x0"], "_y0": w["top"], "_pw": pw, "_ph": ph,
                        }
    except Exception:
        return {}

    if not sub_part_positions:
        return {}

    # For each sub-part, scan nearby chars on the same page for pin numbers
    result: dict = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = list(pdf.pages)
            for sub_ref, sp in sub_part_positions.items():
                m = multipart_pat.fullmatch(sub_ref)
                if not m:
                    continue
                base_ref = m.group(1)
                page = pages[sp["page"] - 1]
                pw = sp["_pw"]
                ph = sp["_ph"]
                cx0 = sp["_x0"]
                cy0 = sp["_y0"]
                # Neighbourhood: wide horizontally (pins appear on both sides), tight vertically
                x_radius = 70
                y_radius = 30
                chars = page.chars or []
                nearby = [c for c in chars
                          if abs(c["x0"] - cx0) < x_radius and abs(c["top"] - cy0) < y_radius]
                # Use word extraction to get proper pin number tokens (avoids char concatenation issues)
                words_on_page = page.extract_words(x_tolerance=5, y_tolerance=3) or []
                for w in words_on_page:
                    tok = w["text"].strip()
                    if not digit_pat.match(tok) or len(tok) < 2:
                        continue
                    wx = (w["x0"] + w["x1"]) / 2.0
                    wy = (w["top"] + w["bottom"]) / 2.0
                    if abs(wx - cx0) < x_radius and abs(wy - cy0) < y_radius:
                        entry = {
                            "page": sp["page"],
                            "x_pct": sp["x_pct"],
                            "y_pct": sp["y_pct"],
                        }
                        result.setdefault(base_ref, {})[tok] = entry
    except Exception as exc:
        print(f"  Warning: PIN_POS_MAP extraction failed: {exc}", file=sys.stderr)

    total_pins = sum(len(v) for v in result.values())
    print(f"  Pin positions extracted: {total_pins} pins across {len(result)} multi-part components")
    return result


def inject_data(template: str, schematic: dict, issues: dict, pdf_b64: str | None = None,
                comp_page_map: dict | None = None, pin_pos_map: dict | None = None) -> str:
    """Replace the marker blocks in the template with real JSON data."""
    schematic_json = json.dumps(schematic, ensure_ascii=False, indent=None)
    issues_json    = json.dumps(issues,    ensure_ascii=False, indent=None)

    def replace_block(text: str, marker: str, js_var: str, json_data: str) -> str:
        pattern = (
            r"// %%" + re.escape(marker) + r"%%\n"
            r"const " + re.escape(js_var) + r" = .*?;\n"
            r"// %%" + re.escape(marker) + r"_END%%"
        )
        replacement = (
            f"// %%{marker}%%\n"
            f"const {js_var} = {json_data};\n"
            f"// %%{marker}_END%%"
        )
        # Use a lambda so re.sub does not interpret backslashes in the replacement
        # string (important: JSON paths on Windows contain backslashes that would
        # otherwise be treated as regex escape sequences, corrupting the output).
        found = [False]
        def _repl(m: re.Match) -> str:
            found[0] = True
            return replacement
        result = re.sub(pattern, _repl, text, flags=re.DOTALL)
        if not found[0]:
            raise ValueError(
                f"Marker block '%%{marker}%%' … '%%{marker}_END%%' not found in template."
            )
        return result

    template = replace_block(template, "SCHEMATIC_DATA", "SCHEMATIC_DATA", schematic_json)
    template = replace_block(template, "ISSUES_DATA",    "ISSUES_DATA",    issues_json)

    # Inject PDF data (base64 string or null)
    pdf_value = f'"{pdf_b64}"' if pdf_b64 else "null"
    template = replace_block(template, "PDF_DATA", "PDF_DATA", pdf_value)

    # Inject pre-computed component→page+position map
    map_json = json.dumps(comp_page_map or {}, ensure_ascii=False)
    template = replace_block(template, "COMP_PAGE_MAP", "COMP_PAGE_MAP", map_json)

    # Inject pin→position map for multi-part components
    pin_map_json = json.dumps(pin_pos_map or {}, ensure_ascii=False)
    template = replace_block(template, "PIN_POS_MAP", "PIN_POS_MAP", pin_map_json)

    return template


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate review.html from review.db (or legacy schematic.yaml + issues.yaml)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Path to review.db, or folder containing REVIEW/review.db (or legacy schematic.yaml)",
    )
    parser.add_argument(
        "--schematic",
        help="Explicit path to schematic.yaml (legacy fallback; overrides folder lookup)",
    )
    parser.add_argument(
        "--issues",
        help="Explicit path to issues.yaml (legacy fallback; overrides folder lookup)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output path for review.html (default: <folder>/review.html or ./review.html)",
    )
    parser.add_argument(
        "--pdf",
        help="Explicit path to schematic PDF to embed (default: auto-detect *.pdf in schematic folder)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()

    # ------------------------------------------------------------------
    # Resolve data source
    # ------------------------------------------------------------------
    use_db = False
    db_path: Path | None = None
    schematic_path: Path | None = None
    issues_path: Path | None = None
    output_dir: Path | None = None

    if args.schematic and args.issues:
        # Explicit YAML paths — legacy mode
        schematic_path = Path(args.schematic)
        issues_path    = Path(args.issues)
        output_dir     = schematic_path.parent

    elif args.folder:
        folder = Path(args.folder)

        # Determine whether the argument is a .db file or a directory
        if folder.suffix.lower() == '.db' or (folder.is_file() and not folder.is_dir()):
            # Direct DB file path
            db_path    = folder
            output_dir = db_path.parent
            use_db     = True
        else:
            # Directory — prefer REVIEW/review.db; fall back to YAML
            candidate_db = folder / "REVIEW" / "review.db"
            if candidate_db.exists():
                db_path    = candidate_db
                output_dir = db_path.parent
                use_db     = True
            else:
                # Legacy YAML fallback
                review_dir = folder / "REVIEW" if (folder / "REVIEW" / "schematic.yaml").exists() else folder
                schematic_path = review_dir / "schematic.yaml"
                issues_path    = review_dir / "issues.yaml"
                output_dir     = review_dir

    else:
        parser.error("Provide a folder/db argument or both --schematic and --issues.")

    # ------------------------------------------------------------------
    # Validate resolved paths
    # ------------------------------------------------------------------
    if use_db:
        if not db_path.exists():
            print(f"ERROR: {db_path} not found", file=sys.stderr)
            sys.exit(1)
    else:
        if not schematic_path.exists():
            print(f"ERROR: {schematic_path} not found", file=sys.stderr)
            sys.exit(1)
        if not issues_path.exists():
            print(f"ERROR: {issues_path} not found", file=sys.stderr)
            sys.exit(1)

    # Output path
    output_path = Path(args.output) if args.output else output_dir / "review.html"

    # ------------------------------------------------------------------
    # Resolve PDF to embed
    # ------------------------------------------------------------------
    pdf_b64: str | None = None
    detected_pdf: Path | None = None
    if args.pdf:
        detected_pdf = Path(args.pdf)
        if not detected_pdf.exists():
            print(f"ERROR: PDF not found: {detected_pdf}", file=sys.stderr)
            sys.exit(1)
        print(f"Embedding PDF {detected_pdf} …")
        pdf_b64 = load_pdf_base64(detected_pdf)
    else:
        # Auto-detect: search the folder arg (or db parent/grandparent for db paths)
        if args.folder:
            candidate_dirs = [Path(args.folder), Path(args.folder).parent]
        elif args.db:
            db_path = Path(args.db)
            candidate_dirs = [db_path.parent, db_path.parent.parent]
        else:
            candidate_dirs = []
        for search_dir in candidate_dirs:
            detected_pdf = find_pdf(search_dir)
            if detected_pdf:
                print(f"Auto-detected PDF {detected_pdf} …")
                pdf_b64 = load_pdf_base64(detected_pdf)
                break
        else:
            print("No PDF found — panes will show placeholder message.")

    # ------------------------------------------------------------------
    # Pre-flight validation (DB path only; advisory — never blocks HTML)
    # ------------------------------------------------------------------
    if use_db:
        try:
            from validate_issues import validate_issues as _validate
            print("Running pre-flight issue validation …")
            val_result = _validate(db_path)
            if val_result.get("errors"):
                print(
                    f"  WARNING: {len(val_result['errors'])} validation error(s) found "
                    f"(HTML will still be generated):",
                    file=sys.stderr,
                )
                for err in val_result["errors"]:
                    print(f"    {err['id']}  {err['message']}", file=sys.stderr)
            else:
                print(f"  Validation OK ({val_result.get('checked', 0)} issues checked)")
        except ImportError:
            print("  validate_issues not available — skipping pre-flight check", file=sys.stderr)
        except Exception as exc:
            print(f"  WARNING: pre-flight validation failed: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------
    if use_db:
        print(f"Loading data from {db_path} …")
        schematic, issues = load_from_db(db_path)
    else:
        print(f"Loading {schematic_path} …")
        schematic = load_yaml(schematic_path)
        print(f"Loading {issues_path} …")
        issues = load_yaml(issues_path)

    # ------------------------------------------------------------------
    # Build component→page+position map from PDF text extraction
    # ------------------------------------------------------------------
    comp_page_map: dict = {}
    pin_pos_map: dict = {}
    if detected_pdf:
        print("Extracting component positions from PDF …")
        comp_page_map = build_comp_page_map(detected_pdf, schematic)
        print("Extracting pin positions from PDF …")
        pin_pos_map = build_pin_pos_map(detected_pdf, schematic, comp_page_map)

    # ------------------------------------------------------------------
    # Load template and inject
    # ------------------------------------------------------------------
    template_path = find_template(script_dir)
    print(f"Using template {template_path} …")
    template_text = template_path.read_text(encoding="utf-8")

    print("Injecting data …")
    output_text = inject_data(template_text, schematic, issues, pdf_b64, comp_page_map, pin_pos_map)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")
    print(f"Done. Written to {output_path}")


if __name__ == "__main__":
    main()
