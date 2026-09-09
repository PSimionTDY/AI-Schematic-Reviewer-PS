#!/usr/bin/env python3
"""
Extract component properties from Cadence Allegro schematic PDF annotations.

Cadence OrCAD/Allegro exports JavaScript /Link annotations using app.popUpMenu()
calls that carry full CIS (Component Information System) data for every component:
voltage rating, tolerance, MPN, package, PCB footprint, Highstage part number, etc.

Usage:
    python pdf_annotation_extractor.py reviews/SCH25678-1E
    python pdf_annotation_extractor.py reviews/SCH25678-1E --pdf path/to/schematic.pdf
    python pdf_annotation_extractor.py reviews/SCH25678-1E --json   # dump to JSON only
    python pdf_annotation_extractor.py reviews/SCH25678-1E --no-yaml  # skip YAML update
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    yaml = None

try:
    from filelock import FileLock as _FileLock
except ImportError:
    _FileLock = None


# ---------------------------------------------------------------------------
# PDF string / JS parsing
# ---------------------------------------------------------------------------

def _extract_pdf_literal_string(text: str, pos: int) -> str:
    """Extract a PDF literal string (...) starting at the opening '(' at pos.
    Handles nested parentheses and PDF escape sequences (\\n, \\(, \\), etc.)."""
    assert text[pos] == '('
    depth = 1
    i = pos + 1
    chars: list[str] = []
    esc = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
           '(': '(', ')': ')', '\\': '\\'}
    while i < len(text) and depth > 0:
        c = text[i]
        if c == '\\' and i + 1 < len(text):
            chars.append(esc.get(text[i + 1], text[i + 1]))
            i += 2
        elif c == '(':
            depth += 1
            chars.append(c)
            i += 1
        elif c == ')':
            depth -= 1
            if depth > 0:
                chars.append(c)
            i += 1
        else:
            chars.append(c)
            i += 1
    return ''.join(chars)


def _parse_popup_args(js: str) -> dict | None:
    """Parse app.popUpMenu( "Title", "-", "Key=Value", ... ) into a dict."""
    m = re.search(r'app\.popUpMenu\s*\(', js)
    if not m:
        return None
    args = re.findall(r'"((?:[^"\\]|\\.)*)"', js[m.end():])
    if not args:
        return None
    result: dict[str, str] = {'_title': args[0]}
    for arg in args[1:]:
        if arg == '-':
            continue
        k, _, v = arg.partition('=')
        if _:
            result[k.strip()] = v.strip()
    return result


# ---------------------------------------------------------------------------
# Annotation extraction
# ---------------------------------------------------------------------------

def extract_annotations(pdf_path: Path, verbose: bool = False) -> list[dict]:
    """Extract all JS /Link annotations from the PDF as parsed dicts."""
    doc = fitz.open(pdf_path)
    results: list[dict] = []
    xref_count = doc.xref_length()

    if verbose:
        print(f"  Scanning {xref_count} xrefs in {pdf_path.name}...")

    for xref in range(1, xref_count):
        try:
            obj = doc.xref_object(xref, compressed=False)
            if '/Type /Annot' not in obj and '/Type/Annot' not in obj:
                continue
            js_pos = obj.find('/JS (')
            if js_pos == -1:
                continue
            paren_pos = obj.index('(', js_pos + 3)
            js_str = _extract_pdf_literal_string(obj, paren_pos)
            parsed = _parse_popup_args(js_str)
            if parsed:
                parsed['_xref'] = xref
                results.append(parsed)
        except Exception:
            pass

    doc.close()
    return results


# ---------------------------------------------------------------------------
# Component data extraction
# ---------------------------------------------------------------------------

def _parse_power_rating(description: str) -> str:
    """Extract power rating (e.g. '0.063W', '2W') from description string.
    Format: RES,...,...,<value>,<tol>,<pkg>,<power>"""
    if not description:
        return ''
    parts = [p.strip() for p in description.split(',')]
    for p in reversed(parts):
        if p.endswith('W') and re.match(r'^\d', p):
            return p
    return ''


def _parse_pcb_package(footprint: str, description: str) -> str:
    """Infer PCB package size (0402, 0603, 1210, etc.) from footprint or description."""
    # Try description first (most reliable)
    if description:
        parts = [p.strip() for p in description.split(',')]
        for p in parts:
            if re.match(r'^\d{4}$', p):  # 4-digit package code
                return p
    # Fallback: parse footprint name
    if footprint:
        m = re.search(r'\b(\d{4})\b', footprint)
        if m:
            return m.group(1)
    return ''


def build_component_map(annots: list[dict]) -> dict[str, dict]:
    """Build a map of Reference → component properties from PDF annotations."""
    components: dict[str, dict] = {}

    for a in annots:
        ref = a.get('Reference', '').strip()
        if not ref or ref.startswith('Warning'):
            continue

        item_type = a.get('ItemType', '').strip()
        description = a.get('Description', '').strip()
        footprint = a.get('PCB Footprint', '').strip()
        voltage = a.get('Voltage', '').strip()
        tolerance = a.get('Tolerance', '').strip()
        pkg_type = a.get('Package Type', '').strip()  # X7R, THICK FILM, etc.
        value = a.get('Value', '').strip()
        part_number = a.get('Part Number', '').strip()
        mfg1 = a.get('mfg1', '').strip()
        mpn1 = a.get('mfg1partnumber', '').strip()
        mfg2 = a.get('mgf2', '').strip()       # note: CIS typo "mgf2"
        mpn2 = a.get('mfg2partnumber', '').strip()
        height = a.get('HEIGHT', '').strip()
        current = a.get('Current', '').strip()

        comp: dict[str, str] = {}

        if item_type:
            comp['item_type'] = item_type
        if value:
            comp['value'] = value
        if part_number:
            comp['highstage_pn'] = part_number
        if description:
            comp['description'] = description
        if voltage:
            comp['voltage_rating'] = voltage
        if tolerance:
            comp['tolerance'] = tolerance
        if pkg_type:
            comp['package_type'] = pkg_type  # dielectric for caps (X7R), film for resistors
        if footprint:
            comp['pcb_footprint'] = footprint
        pkg_size = _parse_pcb_package(footprint, description)
        if pkg_size:
            comp['package_size'] = pkg_size
        if mfg1 and mpn1:
            comp['mfg1'] = mfg1
            comp['mpn1'] = mpn1
        if mfg2 and mpn2:
            comp['mfg2'] = mfg2
            comp['mpn2'] = mpn2
        if height:
            comp['height_mm'] = height
        if current:
            comp['current_rating'] = current

        # Power rating from description (resistors)
        power = _parse_power_rating(description)
        if power:
            comp['power_rating'] = power

        if comp:
            # Keep the last annotation if duplicate (later pages may have more detail)
            if ref not in components or len(comp) > len(components[ref]):
                components[ref] = comp

    return components


# ---------------------------------------------------------------------------
# YAML update
# ---------------------------------------------------------------------------

def _load_yaml_safe(path: Path) -> dict:
    if yaml is None:
        raise ImportError("PyYAML not installed. Run: pip install pyyaml")
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _save_yaml(data: dict, path: Path):
    if yaml is None:
        raise ImportError("PyYAML not installed")
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def update_schematic_yaml(yaml_path: Path, component_map: dict[str, dict]) -> tuple[int, int]:
    """Merge PDF annotation data into schematic.yaml. Returns (updated, new) counts."""
    lock_ctx = _FileLock(str(yaml_path) + ".lock", timeout=60) if _FileLock else None

    def _do_update():
        data = _load_yaml_safe(yaml_path)
        components = data.setdefault('components', {})

        updated = 0
        new_fields_total = 0

        for ref, props in component_map.items():
            if ref not in components:
                continue  # Don't add components not already in YAML
            existing = components[ref]
            if not isinstance(existing, dict):
                existing = {}
                components[ref] = existing

            new_fields = 0
            pdf_section = existing.setdefault('pdf_annot', {})
            for k, v in props.items():
                if k not in pdf_section or not pdf_section[k]:
                    pdf_section[k] = v
                    new_fields += 1

            if new_fields:
                updated += 1
                new_fields_total += new_fields

        _save_yaml(data, yaml_path)
        return updated, new_fields_total

    if lock_ctx is not None:
        with lock_ctx:
            return _do_update()
    return _do_update()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_pdf(review_dir: Path) -> Path | None:
    pdfs = [p for p in review_dir.rglob('*.pdf')
            if not p.name.startswith('.')]
    if not pdfs:
        return None
    # Prefer non-PCB-ASSY PDFs (those are assembly drawings, not schematics)
    sch_pdfs = [p for p in pdfs if 'ASSY' not in p.name.upper()]
    return (sch_pdfs or pdfs)[0]


def find_yaml(review_dir: Path) -> Path | None:
    candidates = list(review_dir.rglob('schematic.yaml'))
    return candidates[0] if candidates else None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('review_dir', type=Path,
                        help='Review directory (e.g. reviews/SCH25678-1E)')
    parser.add_argument('--pdf', type=Path, default=None,
                        help='Path to schematic PDF (auto-detected if omitted)')
    parser.add_argument('--yaml', dest='yaml_path', type=Path, default=None,
                        help='Path to schematic.yaml (auto-detected if omitted)')
    parser.add_argument('--json', action='store_true',
                        help='Dump extracted component map to JSON and exit')
    parser.add_argument('--no-yaml', action='store_true',
                        help='Skip schematic.yaml update')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    review_dir = args.review_dir.resolve()
    if not review_dir.exists():
        print(f"ERROR: {review_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    # Find PDF
    pdf_path = args.pdf or find_pdf(review_dir)
    if not pdf_path or not pdf_path.exists():
        print(f"ERROR: No schematic PDF found in {review_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"PDF: {pdf_path}")

    # Extract annotations
    print("Extracting PDF annotations...")
    annots = extract_annotations(pdf_path, verbose=args.verbose)
    print(f"  Found {len(annots)} annotations")

    # Build component map
    component_map = build_component_map(annots)
    print(f"  Parsed {len(component_map)} unique components with data")

    # Count field coverage
    has_voltage = sum(1 for c in component_map.values() if c.get('voltage_rating'))
    has_tolerance = sum(1 for c in component_map.values() if c.get('tolerance'))
    has_mpn = sum(1 for c in component_map.values() if c.get('mpn1'))
    has_pn = sum(1 for c in component_map.values() if c.get('highstage_pn'))
    n = len(component_map)
    print(f"  Coverage: voltage={has_voltage}/{n} ({100*has_voltage//n if n else 0}%),",
          f"tolerance={has_tolerance}/{n} ({100*has_tolerance//n if n else 0}%),",
          f"MPN={has_mpn}/{n} ({100*has_mpn//n if n else 0}%),",
          f"Highstage PN={has_pn}/{n} ({100*has_pn//n if n else 0}%)")

    # JSON dump mode
    if args.json:
        out = review_dir / 'pdf_component_data.json'
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(component_map, f, indent=2, ensure_ascii=False)
        print(f"Saved component data to {out}")
        return

    # Update schematic.yaml
    if not args.no_yaml:
        yaml_path = args.yaml_path or find_yaml(review_dir)
        if not yaml_path or not yaml_path.exists():
            print("No schematic.yaml found — skipping YAML update "
                  "(use --no-yaml to suppress this message)")
        else:
            if yaml is None:
                print("WARNING: PyYAML not installed, skipping YAML update. "
                      "Run: pip install pyyaml")
            else:
                print(f"Updating {yaml_path}...")
                updated, fields = update_schematic_yaml(yaml_path, component_map)
                print(f"  Updated {updated} components, added {fields} fields")

    print("Done.")


if __name__ == '__main__':
    main()
