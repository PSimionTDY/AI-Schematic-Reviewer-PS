#!/usr/bin/env python3
"""
Flag components that have not been verified by any check script or IC agent.

Every component in schematic.yaml starts with verified=false (set by schematic_builder).
Check scripts (verify_resistor_power, verify_capacitor_voltage) and IC agents
(via collect_enrichments) mark components verified=true when they reach a verdict.

This script runs last (before build_issues) and emits one grouped question per
component type for anything still unverified — meaning no script had enough data
to assess it (e.g. voltage levels unknown) and no IC agent reviewed it.

Usage:
    python verify_unverified.py reviews/<SCH_ID>
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

# These types carry no meaningful electrical stress — skip them.
_SKIP_TYPES = {'test_point'}

# Human-readable descriptions per type, explaining why it might be unverified.
_TYPE_DESCRIPTIONS = {
    'resistor':        'voltage levels or role unknown — cannot assess power dissipation',
    'capacitor':       'supply voltage unknown — cannot assess voltage rating',
    'inductor':        'operating current unknown — cannot assess saturation/thermal rating',
    'ferrite_bead':    'operating current unknown — cannot assess impedance and saturation',
    'ic':              'not reviewed by any IC agent — pinout and operating conditions unverified',
    'transistor':      'operating conditions unknown — Vce, Vgs, Id not verified',
    'diode':           'operating conditions unknown — forward voltage, reverse voltage, current not verified',
    'crystal':         'load capacitance and drive level not verified',
    'fuse':            'rated current not verified against load',
    'voltage_regulator': 'output voltage and load current not verified',
    'zener':           'clamping voltage and power dissipation not verified',
    'led':             'forward current and limiting resistor not verified',
    'relay':           'coil voltage and contact ratings not verified',
    'connector':       'pin signals and voltage levels not verified',
}


def _is_dnp_everywhere(comp: dict) -> bool:
    """Return True if the component is DNP in every assembly variant."""
    variants = comp.get('variants')
    if not variants:
        return comp.get('dnp', False)
    return all(v.get('dnp', False) for v in variants.values())


def verify_unverified(schematic_path: Path) -> list[dict]:
    """Return question issues for all components still marked verified=false."""
    with open(schematic_path, encoding='utf-8') as f:
        sch = yaml.safe_load(f)

    components = sch.get('components', {})

    # Group unverified refs by comp_type
    by_type: dict[str, list[str]] = defaultdict(list)
    for ref, comp in sorted(components.items()):
        if comp.get('verified', True):  # default True = backward-compat with old schematics
            continue
        if _is_dnp_everywhere(comp):
            continue
        ctype = comp.get('comp_type', 'unknown')
        if ctype in _SKIP_TYPES:
            continue
        by_type[ctype].append(ref)

    issues = []
    for ctype in sorted(by_type):
        refs = by_type[ctype]
        reason = _TYPE_DESCRIPTIONS.get(ctype, 'not assessed by any check script or IC agent')
        ref_list = ', '.join(refs)
        issues.append({
            'severity': 'question',
            'type': 'component_unverified',
            'description': (
                f"{len(refs)} {ctype}(s) not yet verified: {ref_list} — {reason}"
            ),
            'components': [{'ref': r} for r in refs],
        })

    return issues


def main():
    ap = argparse.ArgumentParser(description="Flag unverified components")
    ap.add_argument("schematic_folder", help="Schematic workspace folder (contains REVIEW/schematic.yaml)")
    ap.add_argument("--output", "-o", help="Output file (default: REVIEW/issues_unverified.yaml)")
    args = ap.parse_args()

    folder = Path(args.schematic_folder)
    yaml_path = folder / "REVIEW" / "schematic.yaml"

    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found")
        sys.exit(1)

    issues = verify_unverified(yaml_path)

    total = sum(len(i['components']) for i in issues)
    print(f"Unverified components: {total} across {len(issues)} type group(s)")
    for issue in issues:
        n = len(issue['components'])
        ctype = issue['description'].split()[1]
        print(f"  {ctype}: {n}")

    out = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'source': 'verify_unverified',
        'issues': issues,
    }

    out_path = Path(args.output) if args.output else folder / "REVIEW" / "issues_unverified.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        yaml.dump(out, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
