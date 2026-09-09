#!/usr/bin/env python3
"""
netlist_diff.py — Diff two OrCadPCB2Netlist .NET files (or Altium export folders).

Reports:
  - Added / removed components
  - Changed component values or footprints
  - Added / removed nets
  - Net pin-list changes (pins added/removed from a net)

Usage:
    python netlist_diff.py <old_folder> <new_folder> [--output DIFF.md]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ── resolve path to altium_parser ──────────────────────────────────────────
_PARSER_DIR = Path(__file__).parents[2] / "schematic-parser" / "scripts"
sys.path.insert(0, str(_PARSER_DIR))

from altium_parser import find_altium_net, _parse_net_file  # type: ignore


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def load(folder: Path) -> dict:
    net_file = find_altium_net(folder)
    if net_file is None:
        raise FileNotFoundError(f"No .NET file found in {folder}")
    print(f"  Loaded: {net_file.relative_to(folder.parent)}")
    return _parse_net_file(net_file)


def norm(s: str) -> str:
    """Normalise whitespace and unicode µ/u for value comparison."""
    return s.strip().replace('\u00b5', 'u').replace('\u03bc', 'u')


def _pin_set(net_data: dict) -> set[str]:
    """Return set of 'REF:PIN' strings for a net."""
    return {f"{c['ref']}:{c['pin']}" for c in net_data.get('connections', [])}


# ───────────────────────────────────────────────────────────────────────────
# Diff logic
# ───────────────────────────────────────────────────────────────────────────

def diff_netlists(old: dict, new: dict) -> dict:
    old_comps = old['components']
    new_comps = new['components']
    old_nets  = old['nets']
    new_nets  = new['nets']

    old_refs = set(old_comps)
    new_refs = set(new_comps)

    added_refs   = sorted(new_refs - old_refs)
    removed_refs = sorted(old_refs - new_refs)
    common_refs  = old_refs & new_refs

    # Component changes
    value_changes = []
    pkg_changes   = []
    net_changes_comp = []  # net reconnections on a per-pin basis

    for ref in sorted(common_refs):
        oc = old_comps[ref]
        nc = new_comps[ref]
        if norm(oc['value']) != norm(nc['value']):
            value_changes.append((ref, oc['value'], nc['value']))
        if norm(oc.get('package', '')) != norm(nc.get('package', '')):
            pkg_changes.append((ref, oc.get('package', ''), nc.get('package', '')))
        # Pin net changes
        old_pins = oc.get('pins', {})
        new_pins = nc.get('pins', {})
        for pin in sorted(set(old_pins) | set(new_pins)):
            on = old_pins.get(pin, {}).get('net', '<<missing>>')
            nn = new_pins.get(pin, {}).get('net', '<<missing>>')
            if on != nn:
                net_changes_comp.append((ref, pin, on, nn))

    # Net-level diff
    old_net_names = set(old_nets)
    new_net_names = set(new_nets)
    added_nets   = sorted(new_net_names - old_net_names)
    removed_nets = sorted(old_net_names - new_net_names)

    # Pin-list changes on surviving nets
    net_pin_changes = []
    for net in sorted(old_net_names & new_net_names):
        op = _pin_set(old_nets[net])
        np = _pin_set(new_nets[net])
        added_pins   = sorted(np - op)
        removed_pins = sorted(op - np)
        if added_pins or removed_pins:
            net_pin_changes.append((net, added_pins, removed_pins))

    return {
        'added_components':    added_refs,
        'removed_components':  removed_refs,
        'value_changes':       value_changes,
        'package_changes':     pkg_changes,
        'pin_net_changes':     net_changes_comp,
        'added_nets':          added_nets,
        'removed_nets':        removed_nets,
        'net_pin_changes':     net_pin_changes,
    }


# ───────────────────────────────────────────────────────────────────────────
# Report generation
# ───────────────────────────────────────────────────────────────────────────

def render_markdown(d: dict, old_label: str, new_label: str) -> str:
    lines: list[str] = []
    a = lines.append

    a(f"# Netlist Diff\n")
    a(f"**Old:** `{old_label}`  ")
    a(f"**New:** `{new_label}`\n")

    # Summary
    total = (len(d['added_components']) + len(d['removed_components']) +
             len(d['value_changes']) + len(d['package_changes']) +
             len(d['pin_net_changes']) + len(d['added_nets']) +
             len(d['removed_nets']) + len(d['net_pin_changes']))
    a(f"**Total changes:** {total}\n")
    a(f"| Category | Count |")
    a(f"|----------|-------|")
    a(f"| Added components     | {len(d['added_components'])} |")
    a(f"| Removed components   | {len(d['removed_components'])} |")
    a(f"| Value changes        | {len(d['value_changes'])} |")
    a(f"| Package changes      | {len(d['package_changes'])} |")
    a(f"| Pin net changes      | {len(d['pin_net_changes'])} |")
    a(f"| Added nets           | {len(d['added_nets'])} |")
    a(f"| Removed nets         | {len(d['removed_nets'])} |")
    a(f"| Net pin-list changes | {len(d['net_pin_changes'])} |")
    a("")

    # ── Added components ──────────────────────────────────────────────────
    if d['added_components']:
        a("## Added Components\n")
        a("| Ref | Footprint | Value |")
        a("|-----|-----------|-------|")
        # We need new_comps — pass it through d
        for ref in d['added_components']:
            c = d.get('_new_comps', {}).get(ref, {})
            a(f"| {ref} | {c.get('package','')} | {c.get('value','')} |")
        a("")

    # ── Removed components ────────────────────────────────────────────────
    if d['removed_components']:
        a("## Removed Components\n")
        a("| Ref | Footprint | Value |")
        a("|-----|-----------|-------|")
        for ref in d['removed_components']:
            c = d.get('_old_comps', {}).get(ref, {})
            a(f"| {ref} | {c.get('package','')} | {c.get('value','')} |")
        a("")

    # ── Value changes ─────────────────────────────────────────────────────
    if d['value_changes']:
        a("## Value Changes\n")
        a("| Ref | Old Value | New Value |")
        a("|-----|-----------|-----------|")
        for ref, ov, nv in d['value_changes']:
            a(f"| {ref} | {ov} | {nv} |")
        a("")

    # ── Package / footprint changes ───────────────────────────────────────
    if d['package_changes']:
        a("## Package / Footprint Changes\n")
        a("| Ref | Old Package | New Package |")
        a("|-----|-------------|-------------|")
        for ref, op, np_ in d['package_changes']:
            a(f"| {ref} | {op} | {np_} |")
        a("")

    # ── Pin net changes ───────────────────────────────────────────────────
    if d['pin_net_changes']:
        a("## Pin Net Changes (Re-routed Pins)\n")
        a("| Ref | Pin | Old Net | New Net |")
        a("|-----|-----|---------|---------|")
        for ref, pin, on, nn in d['pin_net_changes']:
            a(f"| {ref} | {pin} | `{on}` | `{nn}` |")
        a("")

    # ── Added nets ────────────────────────────────────────────────────────
    if d['added_nets']:
        a("## Added Nets\n")
        a("| Net Name | Pins |")
        a("|----------|------|")
        for net in d['added_nets']:
            pins = ", ".join(sorted(_pin_set(d.get('_new_nets', {}).get(net, {}))))
            a(f"| `{net}` | {pins} |")
        a("")

    # ── Removed nets ──────────────────────────────────────────────────────
    if d['removed_nets']:
        a("## Removed Nets\n")
        a("| Net Name | Pins |")
        a("|----------|------|")
        for net in d['removed_nets']:
            pins = ", ".join(sorted(_pin_set(d.get('_old_nets', {}).get(net, {}))))
            a(f"| `{net}` | {pins} |")
        a("")

    # ── Net pin-list changes ──────────────────────────────────────────────
    if d['net_pin_changes']:
        a("## Net Membership Changes\n")
        a("| Net | Added Pins | Removed Pins |")
        a("|-----|-----------|--------------|")
        for net, ap, rp in d['net_pin_changes']:
            ap_s = ", ".join(ap) if ap else "—"
            rp_s = ", ".join(rp) if rp else "—"
            a(f"| `{net}` | {ap_s} | {rp_s} |")
        a("")

    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Diff two OrCadPCB2 netlist folders")
    ap.add_argument("old_folder", type=Path)
    ap.add_argument("new_folder", type=Path)
    ap.add_argument("--output", type=Path, default=None,
                    help="Output Markdown file (default: print to stdout)")
    args = ap.parse_args()

    print(f"Loading OLD netlist from: {args.old_folder}")
    old = load(args.old_folder)
    print(f"  → {len(old['components'])} components, {len(old['nets'])} nets")

    print(f"Loading NEW netlist from: {args.new_folder}")
    new = load(args.new_folder)
    print(f"  → {len(new['components'])} components, {len(new['nets'])} nets")

    print("Diffing…")
    result = diff_netlists(old, new)
    # Attach raw data for rendering
    result['_old_comps'] = old['components']
    result['_new_comps'] = new['components']
    result['_old_nets']  = old['nets']
    result['_new_nets']  = new['nets']

    old_label = args.old_folder.name
    new_label = args.new_folder.name
    md = render_markdown(result, old_label, new_label)

    if args.output:
        args.output.write_text(md, encoding='utf-8')
        print(f"Diff written to: {args.output}")
    else:
        print("\n" + md)

    # Print summary to stderr
    print(f"\nSummary:")
    print(f"  Added components:    {len(result['added_components'])}")
    print(f"  Removed components:  {len(result['removed_components'])}")
    print(f"  Value changes:       {len(result['value_changes'])}")
    print(f"  Package changes:     {len(result['package_changes'])}")
    print(f"  Pin net changes:     {len(result['pin_net_changes'])}")
    print(f"  Added nets:          {len(result['added_nets'])}")
    print(f"  Removed nets:        {len(result['removed_nets'])}")
    print(f"  Net pin-list changes:{len(result['net_pin_changes'])}")


if __name__ == "__main__":
    main()
