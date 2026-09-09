#!/usr/bin/env python3
"""
Variant DNP Extractor — extract DNP component refs from multi-variant schematic PDFs.

For multi-variant schematics (e.g. SCH23729-3), Highstage contains:
  - <sch>.pdf                  — base schematic (Core Design / all components)
  - <sch>_PCB_ASSY<num>.pdf   — assembly variant PDF

In Cadence OrCAD CIS variant exports, DNP (Do Not Populate) components are
indicated by a non-empty `NC` (Not Configured) field in the JS popup annotation.
Variant PDFs differ from the base in their `Variant Name` field and which
components carry a non-empty `NC` field.

DNP detection strategy:
  1. Primary:  NC field — if NC != "" in a variant PDF, the component is DNP.
  2. Secondary (fallback): missing ref — if a ref appears in the base but not
     in the variant, it is DNP (handles simpler PDF exports without NC field).

Component substitution detection:
  - Same ref des, different Value or Part Number between base and variant.
  - Split ICs (U1A, U1B, ...) are grouped under the root ref (U1).

Variant PDF discovery (priority order):
  1. Highstage refby API → PCB_ASSY IDs → UNC paths for fresh PDFs
  2. Local folder scan (fallback when Highstage unreachable)

Usage:
    python variant_dnp_extractor.py reviews/SCH23729-3
    python variant_dnp_extractor.py reviews/SCH23729-3 --dry-run
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Reuse annotation extraction from the existing extractor
sys.path.insert(0, str(Path(__file__).parent))
from pdf_annotation_extractor import extract_annotations


# ---------------------------------------------------------------------------
# Highstage variant discovery
# ---------------------------------------------------------------------------

def find_variants_via_highstage(schematic_id: str) -> list[str]:
    """Find PCB_ASSY variants linked to this schematic via Highstage refby API."""
    ps_cmd = (
        f'$r = Invoke-WebRequest "https://highstage/ts/ts/ref/refby.exe.aspx?t=doc&o={schematic_id}"'
        f' -UseDefaultCredentials -SkipCertificateCheck -ErrorAction SilentlyContinue; $r.Content'
    )
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    html = result.stdout
    assemblies = list(set(re.findall(r'PCB_ASSY[\w\-]+', html)))
    versioned = [a for a in assemblies if re.match(r'PCB_ASSY\d+-\d+$', a)]
    return sorted(versioned)


def find_variant_pdf_on_highstage(assembly_id: str, schematic_id: str) -> str | None:
    """Find the variant PDF for a given PCB_ASSY on Highstage UNC share."""
    base_id = assembly_id.split('-')[0]
    unc_path = f"\\\\highstage\\files\\PURCHASE_SPEC\\PCB_ASSY\\{base_id}\\{assembly_id}\\"
    try:
        if not os.path.exists(unc_path):
            return None
        files = os.listdir(unc_path)
    except Exception:
        return None
    # Prefer PDF matching the schematic name
    for f in files:
        if f.lower().endswith('.pdf') and schematic_id.lower() in f.lower():
            return os.path.join(unc_path, f)
    # Fall back: any PDF in the folder
    for f in files:
        if f.lower().endswith('.pdf'):
            return os.path.join(unc_path, f)
    return None


# ---------------------------------------------------------------------------
# PDF discovery
# ---------------------------------------------------------------------------

def find_pdfs(review_dir: Path) -> tuple[Path | None, list[Path]]:
    """
    Find base PDF and variant PDFs.

    Priority:
      1. Highstage refby API → PCB_ASSY IDs → UNC paths for fresh PDFs
      2. Local folder scan (fallback when Highstage unreachable)

    Naming convention (local):
      Base:    <schematic_name>.pdf           (no ASSY suffix)
      Variant: <schematic_name>_PCB_ASSY<num>.pdf

    Returns (base_pdf, [variant_pdf, ...]).
    """
    # Determine base PDF from local scan first (always needed)
    all_pdfs = sorted(p for p in review_dir.glob('*.pdf') if not p.name.startswith('.'))
    variant_pattern = re.compile(r'_PCB_ASSY[\d-]+\.pdf$', re.IGNORECASE)

    base_pdf: Path | None = None
    local_variant_pdfs: list[Path] = []

    for pdf in all_pdfs:
        if variant_pattern.search(pdf.name):
            local_variant_pdfs.append(pdf)
        else:
            if base_pdf is None:
                base_pdf = pdf
            elif pdf.stem.lower() == review_dir.name.lower():
                base_pdf = pdf

    local_variant_pdfs.sort(key=lambda p: p.name.lower())

    # Attempt Highstage discovery
    schematic_id = review_dir.name.upper()
    print(f"Querying Highstage refby API for {schematic_id}...")
    hs_assemblies = find_variants_via_highstage(schematic_id)

    if hs_assemblies:
        print(f"  Highstage found {len(hs_assemblies)} PCB_ASSY: {hs_assemblies}")
        highstage_pdfs: list[Path] = []
        for assy in hs_assemblies:
            unc = find_variant_pdf_on_highstage(assy, schematic_id.lower())
            if unc:
                highstage_pdfs.append(Path(unc))
                print(f"  {assy} -> {unc}")
            else:
                print(f"  {assy} -> UNC not accessible, falling back to local")
        if highstage_pdfs:
            return base_pdf, highstage_pdfs

    if not hs_assemblies:
        print("  Highstage unreachable or no results — using local folder scan")

    return base_pdf, local_variant_pdfs


def assembly_name(variant_pdf: Path) -> str:
    """Extract PCB_ASSY<num> from variant PDF filename."""
    m = re.search(r'(PCB_ASSY[\d-]+)', variant_pdf.name, re.IGNORECASE)
    return m.group(1).upper() if m else variant_pdf.stem


# ---------------------------------------------------------------------------
# Ref extraction
# ---------------------------------------------------------------------------

def _component_annots(annots: list[dict]) -> list[dict]:
    """Filter to component-only annotations (those with a Reference field)."""
    return [
        a for a in annots
        if a.get('Reference', '').strip()
        and not a.get('Reference', '').startswith('Warning')
    ]


def extract_refs(annots: list[dict]) -> set[str]:
    """Extract all component ref-designators from already-extracted annotations."""
    return {a['Reference'].strip() for a in _component_annots(annots)}


def extract_dnp_refs_nc(annots: list[dict]) -> set[str]:
    """
    Extract DNP refs using the NC (Not Configured) field.

    In Cadence OrCAD CIS variant exports, a non-empty NC field means the
    component is marked 'Not Configured' (DNP) for this variant.
    NC values observed: '1' (single instance NC), '2' (multi-instance NC).
    """
    return {
        a['Reference'].strip()
        for a in _component_annots(annots)
        if a.get('NC', '').strip()
    }


def _root_ref(ref: str) -> str:
    """
    Return root ref designator by stripping trailing letter suffixes from split ICs.

    U1A, U1B, U1C → U1
    R45 → R45  (no change — resistors don't split this way)
    U400 → U400 (no suffix letter)
    """
    return re.sub(r'([A-Z]+\d+)[A-Z]$', r'\1', ref)


def extract_component_values(annots: list[dict]) -> dict[str, dict]:
    """
    Build a map of root-ref → {value, part_number} from annotations.

    Split ICs (U1A, U1B…) are grouped under their root ref (U1).
    When multiple sub-parts exist, the first non-empty value/part_number wins.
    """
    root_map: dict[str, dict] = {}
    for a in _component_annots(annots):
        ref = a.get('Reference', '').strip()
        root = _root_ref(ref)
        value = a.get('Value', '').strip()
        part_number = a.get('Part Number', '').strip()
        if root not in root_map:
            root_map[root] = {'value': value, 'part_number': part_number}
        else:
            # Fill in missing fields from later sub-parts
            if not root_map[root]['value'] and value:
                root_map[root]['value'] = value
            if not root_map[root]['part_number'] and part_number:
                root_map[root]['part_number'] = part_number
    return root_map


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def compute_variants(review_dir: Path, verbose: bool = False) -> dict:
    """
    Extract variant DNP and substitution information from base and variant PDFs.

    DNP detection strategy (in priority order):
    1. NC field: refs where NC != "" in the variant PDF.
    2. Missing-ref fallback: refs present in base but absent from variant.

    Substitution detection:
    - Same root ref, but Value or Part Number differs between base and variant.

    Returns:
        {
            'base_pdf': str,
            'base_refs': set[str],
            'base_nc_refs': set[str],
            'base_values': dict[str, dict],   # root_ref → {value, part_number}
            'variants': [
                {
                    'assembly': str,
                    'pdf': str,
                    'variant_refs': set[str],
                    'dnp_refs': sorted list[str],
                    'dnp_method': 'nc_field' | 'missing_ref',
                    'only_in_variant': sorted list[str],
                    'substitutions': list[dict],
                },
                ...
            ]
        }
    """
    base_pdf, variant_pdfs = find_pdfs(review_dir)

    if not base_pdf:
        print(f"ERROR: No base PDF found in {review_dir}", file=sys.stderr)
        sys.exit(1)

    if not variant_pdfs:
        print(f"WARNING: No variant PDFs found in {review_dir} — nothing to compare")
        return {}

    print(f"\nBase PDF:   {base_pdf.name}")
    print(f"Variants:   {len(variant_pdfs)}")
    for v in variant_pdfs:
        print(f"  {v.name}")

    print(f"\nExtracting annotations from base PDF...")
    base_annots = extract_annotations(base_pdf, verbose=verbose)
    base_refs = extract_refs(base_annots)
    base_nc_refs = extract_dnp_refs_nc(base_annots)
    base_values = extract_component_values(base_annots)
    print(f"  {len(base_refs)} refs in base, {len(base_nc_refs)} NC in base (core design)")

    variants_out = []
    for variant_pdf in variant_pdfs:
        assy = assembly_name(variant_pdf)
        print(f"\nExtracting annotations from {variant_pdf.name}...")
        var_annots = extract_annotations(variant_pdf, verbose=verbose)
        variant_refs = extract_refs(var_annots)
        variant_nc_refs = extract_dnp_refs_nc(var_annots)
        variant_values = extract_component_values(var_annots)

        print(f"  {len(variant_refs)} refs in {assy}, {len(variant_nc_refs)} NC")

        # --- DNP detection ---
        all_variant_nc = variant_nc_refs
        missing_refs = base_refs - variant_refs
        variant_specific_nc = variant_nc_refs - base_nc_refs

        if all_variant_nc:
            dnp_refs = sorted(all_variant_nc)
            dnp_method = 'nc_field'
            if variant_specific_nc:
                print(f"  Variant-specific NC (not in base): {sorted(variant_specific_nc)}")
            print(f"  DNP (NC field, total): {len(dnp_refs)}")
        elif missing_refs:
            dnp_refs = sorted(missing_refs)
            dnp_method = 'missing_ref'
            print(f"  DNP (missing from variant): {len(dnp_refs)}")
        else:
            dnp_refs = []
            dnp_method = 'nc_field'
            print(f"  No DNP refs detected")

        only_in_variant = sorted(variant_refs - base_refs)
        if only_in_variant:
            print(f"  WARNING: {len(only_in_variant)} refs in variant but NOT in base: "
                  f"{only_in_variant[:5]}{'...' if len(only_in_variant) > 5 else ''}")

        # --- Substitution detection ---
        substitutions = []
        common_roots = set(base_values) & set(variant_values)
        for root in sorted(common_roots):
            bv = base_values[root]
            vv = variant_values[root]
            if bv['value'] != vv['value'] or bv['part_number'] != vv['part_number']:
                # Skip self-referential placeholders where variant has no real CIS data
                # (value == part_number in variant means the PDF carries a null-part marker)
                if vv['value'] and vv['part_number'] and vv['value'] == vv['part_number']:
                    continue
                substitutions.append({
                    'ref':               root,
                    'base_value':        bv['value'],
                    'base_part_number':  bv['part_number'],
                    'variant_value':     vv['value'],
                    'variant_part_number': vv['part_number'],
                })
        if substitutions:
            print(f"  Substitutions detected: {len(substitutions)}")
            for s in substitutions:
                print(f"    {s['ref']}: {s['base_value']} ({s['base_part_number']}) "
                      f"-> {s['variant_value']} ({s['variant_part_number']})")

        variants_out.append({
            'assembly':        assy,
            'pdf':             variant_pdf.name,
            'variant_refs':    variant_refs,
            'dnp_refs':        dnp_refs,
            'dnp_method':      dnp_method,
            'only_in_variant': only_in_variant,
            'substitutions':   substitutions,
        })

    return {
        'base_pdf':     base_pdf.name,
        'base_refs':    base_refs,
        'base_nc_refs': base_nc_refs,
        'base_values':  base_values,
        'variants':     variants_out,
    }


def write_variants_yaml(result: dict, output_path: Path) -> None:
    """Write variants.yaml to output_path."""
    variants_section = []
    for v in result['variants']:
        entry: dict = {
            'assembly':   v['assembly'],
            'pdf':        v['pdf'],
            'dnp_method': v['dnp_method'],
            'dnp_refs':   v['dnp_refs'],
        }
        if v['substitutions']:
            entry['substitutions'] = v['substitutions']
        if v['only_in_variant']:
            entry['only_in_variant_warning'] = v['only_in_variant']
        variants_section.append(entry)

    doc = {
        'base_pdf':     result['base_pdf'],
        'base_nc_refs': sorted(result.get('base_nc_refs', set())),
        'variants':     variants_section,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"\nWrote {output_path}")


def print_stats(result: dict) -> None:
    """Print summary statistics."""
    print(f"\n{'='*60}")
    print(f"VARIANT SUMMARY")
    print(f"{'='*60}")
    print(f"Base PDF:      {result['base_pdf']}")
    print(f"Base refs:     {len(result['base_refs'])}")
    print(f"Base NC refs:  {len(result.get('base_nc_refs', set()))} (core design DNP)")
    print(f"Variants:      {len(result['variants'])}")
    for v in result['variants']:
        print(f"\n  [{v['assembly']}]")
        print(f"    PDF:           {v['pdf']}")
        print(f"    Populated:     {len(v['variant_refs'])}")
        print(f"    DNP method:    {v['dnp_method']}")
        print(f"    DNP refs:      {len(v['dnp_refs'])}")
        if v['dnp_refs']:
            sample = v['dnp_refs'][:10]
            print(f"    Sample DNPs:   {', '.join(sample)}"
                  + (f"  (+ {len(v['dnp_refs'])-10} more)" if len(v['dnp_refs']) > 10 else ""))
        if v['substitutions']:
            print(f"    Substitutions: {len(v['substitutions'])}")
            for s in v['substitutions']:
                print(f"      {s['ref']}: {s['base_value']} ({s['base_part_number']}) "
                      f"-> {s['variant_value']} ({s['variant_part_number']})")
        if v['only_in_variant']:
            print(f"    *** ONLY IN VARIANT (unusual): {v['only_in_variant'][:5]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('review_dir', type=Path,
                        help='Review directory (e.g. reviews/SCH23729-3)')
    parser.add_argument('--output', '-o', type=Path, default=None,
                        help='Output path for variants.yaml (default: REVIEW/variants.yaml)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print stats but do not write variants.yaml')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    review_dir = args.review_dir.resolve()
    if not review_dir.exists():
        print(f"ERROR: {review_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    result = compute_variants(review_dir, verbose=args.verbose)
    if not result:
        sys.exit(0)

    print_stats(result)

    if not args.dry_run:
        output_path = args.output or (review_dir / 'REVIEW' / 'variants.yaml')
        write_variants_yaml(result, output_path)
    else:
        print("\n[dry-run] variants.yaml not written")


if __name__ == '__main__':
    main()
