#!/usr/bin/env python3
"""
Schematic Parser - Parses Cadence Allegro CAD export files.

Supports two export formats:
  - *View.dat  (newer): pinView.dat, netView.dat, funcView.dat, compView.dat, chipsView.dat
  - pst*.dat   (older): pstxnet.dat, pstchip.dat, pstxprt.dat

Outputs structured data suitable for schematic.yaml generation.
"""

import os
import re
from collections import defaultdict
from pathlib import Path

import altium_parser


# Map Allegro PINUSE values to canonical pin directions
PINUSE_MAP = {
    'IN':     'input',
    'OUT':    'output',
    'BI':     'bidir',
    'OC':     'open_drain_out',
    'OE':     'open_drain_out',
    'PWRIN':  'power_in',
    'PWROUT': 'power_out',
    'PWR':    'power_in',
    'UNSPEC': 'passive',
    'NC':     'passive',
    '':       'passive',
}


def is_altium_folder(folder: Path) -> bool:
    """Return True if folder contains an Altium export (PrjPcb or OrCadPCB2Netlist .NET)."""
    if list(folder.glob('*.PrjPcb')):
        return True
    return altium_parser.find_altium_net(folder) is not None


def detect_format(allegro_dir: Path) -> str:
    """Return 'view' for *View.dat format, 'pst' for pst*.dat format."""
    if (allegro_dir / 'pinView.dat').exists():
        return 'view'
    if (allegro_dir / 'pstxnet.dat').exists():
        return 'pst'
    raise ValueError(f"No recognised Allegro export files in {allegro_dir}")


def parse_view_dat(filepath: Path) -> tuple[list[str], list[dict]]:
    """Parse a *View.dat file. Returns (headers, list-of-row-dicts)."""
    headers = []
    rows = []
    with open(filepath, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line:
                continue
            tag = line[0]
            fields = line[2:].split('!')
            if tag == 'A':
                headers = fields
            elif tag == 'S' and headers:
                row = dict(zip(headers, fields))
                rows.append(row)
    return headers, rows


def parse_chips_file(filepath: Path) -> dict:
    """
    Parse pstchip.dat or chipsView.dat.
    Returns dict: primitive_name -> {pins: {pin_name: direction}, part_name, value, mfg, mpn}
    """
    content = filepath.read_text(encoding='utf-8', errors='replace')
    primitives = {}

    for prim_block in re.split(r'(?=^primitive\s)', content, flags=re.MULTILINE):
        m = re.match(r"primitive\s+'([^']+)'\s*;", prim_block)
        if not m:
            continue
        prim_name = m.group(1)
        pins = {}

        for pin_match in re.finditer(
            r"pin\s+'([^']+)'\s*:.*?PINUSE\s*=\s*'([^']*)'", prim_block
        ):
            pin_name = pin_match.group(1)
            pinuse = pin_match.group(2).strip().upper()
            pins[pin_name] = PINUSE_MAP.get(pinuse, 'passive')

        body_match = re.search(r'body(.*?)end_body', prim_block, re.DOTALL)
        body = {}
        if body_match:
            for kv in re.finditer(r"(\w+)\s*=\s*'([^']*)'", body_match.group(1)):
                body[kv.group(1)] = kv.group(2)

        primitives[prim_name] = {
            'pins': pins,
            'part_name': body.get('PART_NAME', ''),
            'value': body.get('VALUE', ''),
            'mfg': body.get('MFG1', ''),
            'mpn': body.get('MFG1PARTNUMBER', ''),
            'package': body.get('JEDEC_TYPE', ''),
        }

    return primitives


def extract_sheet(logical_path: str) -> str:
    """Extract sheet identifier from a Cadence hierarchical path."""
    # e.g. '@\sch25678-1\.top(sch_1):...' -> 'sch_1'
    # For deeply nested paths, take the last (sch_N) occurrence
    matches = re.findall(r'\(sch_(\d+)\)', logical_path, re.IGNORECASE)
    if matches:
        return f"sch_{matches[-1]}"
    matches = re.findall(r'\((SCH_\d+)\)', logical_path, re.IGNORECASE)
    if matches:
        return matches[-1].lower()
    return 'sch_1'


# ---------------------------------------------------------------------------
# View.dat format parser
# ---------------------------------------------------------------------------

def parse_view_format(allegro_dir: Path) -> dict:
    """Parse the *View.dat format. Returns structured schematic data."""
    result = {'nets': {}, 'components': {}}

    # --- Pin directions from chipsView.dat ---
    chips_file = allegro_dir / 'chipsView.dat'
    primitives = parse_chips_file(chips_file) if chips_file.exists() else {}

    # --- Component instances + sheet from funcView.dat ---
    func_file = allegro_dir / 'funcView.dat'
    comp_info = {}  # refdes -> {device_type, func_des, sheet}
    if func_file.exists():
        _, rows = parse_view_dat(func_file)
        for row in rows:
            ref = row.get('REFDES', '').strip()
            if not ref:
                continue
            comp_info[ref] = {
                'device_type': row.get('COMP_DEVICE_TYPE', '').strip(),
                'func_des':    row.get('FUNC_DES', '').strip(),
                'sheet':       extract_sheet(row.get('FUNC_LOGICAL_PATH', '')),
            }

    # --- Connections from pinView.dat (primary source) ---
    pin_file = allegro_dir / 'pinView.dat'
    if not pin_file.exists():
        raise FileNotFoundError(f"pinView.dat not found in {allegro_dir}")

    _, rows = parse_view_dat(pin_file)
    comp_pins = defaultdict(dict)  # refdes -> {pin_name: {net, direction, func_des}}

    for row in rows:
        net  = row.get('NET_NAME', '').strip()
        ref  = row.get('REFDES', '').strip()
        pin  = row.get('PIN_NUMBER', '').strip()
        func_des = row.get('FUNC_DES', '').strip()
        device_type = row.get('COMP_DEVICE_TYPE', '').strip()
        if not (net and ref and pin):
            continue

        # Resolve pin direction from primitive definition
        prim = primitives.get(device_type, {})
        direction = prim.get('pins', {}).get(pin, 'passive')

        # Net connections
        if net not in result['nets']:
            result['nets'][net] = {'connections': []}
        result['nets'][net]['connections'].append({
            'ref': ref, 'pin': pin, 'direction': direction, 'func_des': func_des
        })

        comp_pins[ref][pin] = {'net': net, 'direction': direction, 'func_des': func_des}

        # Merge device_type into comp_info
        if ref not in comp_info:
            comp_info[ref] = {}
        if not comp_info[ref].get('device_type'):
            comp_info[ref]['device_type'] = device_type

    # --- Build components dict ---
    for ref, info in comp_info.items():
        device_type = info.get('device_type', '')
        prim = primitives.get(device_type, {})
        result['components'][ref] = {
            'device_type': device_type,
            'part_name':   prim.get('part_name', ''),
            'value':       prim.get('value', ''),
            'mfg':         prim.get('mfg', ''),
            'mpn':         prim.get('mpn', ''),
            'package':     prim.get('package', ''),
            'func_des':    info.get('func_des', ''),
            'sheet':       info.get('sheet', 'sch_1'),
            'pins':        comp_pins.get(ref, {}),
        }

    return result


# ---------------------------------------------------------------------------
# pst*.dat format parser
# ---------------------------------------------------------------------------

def parse_pst_format(allegro_dir: Path) -> dict:
    """Parse the pst*.dat format. Returns structured schematic data."""
    result = {'nets': {}, 'components': {}}

    # --- Pin directions + component info from pstchip.dat ---
    chip_file = allegro_dir / 'pstchip.dat'
    primitives = parse_chips_file(chip_file) if chip_file.exists() else {}

    # --- Component instance -> primitive mapping from pstxprt.dat ---
    comp_primitives = {}  # refdes -> primitive_name
    comp_sheets = {}      # refdes -> sheet
    prt_file = allegro_dir / 'pstxprt.dat'
    if prt_file.exists():
        content = prt_file.read_text(encoding='utf-8', errors='replace')
        current_ref = None
        current_prim = None
        for line in content.splitlines():
            line = line.strip()
            if line.startswith('PART_NAME'):
                current_ref = None
                current_prim = None
            elif current_ref is None:
                # Line like: C1 'GEN_C_SMC0805A...':;
                m = re.match(r"(\S+)\s+'([^']+)'\s*:.*$", line)
                if m:
                    current_ref = m.group(1)
                    current_prim = m.group(2)
                    comp_primitives[current_ref] = current_prim
            elif line.startswith('SECTION_NUMBER') and current_ref:
                # Extract sheet from the @path on the following line
                pass
            elif current_ref and line.startswith("'@"):
                sheet = extract_sheet(line)
                if current_ref not in comp_sheets:
                    comp_sheets[current_ref] = sheet

    # --- Connections from pstxnet.dat ---
    net_file = allegro_dir / 'pstxnet.dat'
    if not net_file.exists():
        raise FileNotFoundError(f"pstxnet.dat not found in {allegro_dir}")

    comp_pins = defaultdict(dict)  # refdes -> {pin: {net, direction}}
    content = net_file.read_text(encoding='utf-8', errors='replace')
    current_net = None

    for line in content.splitlines():
        stripped = line.strip()

        if stripped.startswith('NET_NAME'):
            current_net = None  # reset, name on next line
        elif current_net is None and stripped.startswith("'") and not stripped.startswith("'@"):
            m = re.match(r"'([^']+)'", stripped)
            if m:
                current_net = m.group(1)
                if current_net not in result['nets']:
                    result['nets'][current_net] = {'connections': []}
        elif stripped.startswith('NODE_NAME') and current_net:
            parts = stripped.split()
            if len(parts) >= 3:
                ref = parts[1]
                pin = parts[2]
                prim_name = comp_primitives.get(ref, '')
                prim = primitives.get(prim_name, {})
                direction = prim.get('pins', {}).get(pin, 'passive')

                result['nets'][current_net]['connections'].append({
                    'ref': ref, 'pin': pin, 'direction': direction, 'func_des': ''
                })
                comp_pins[ref][pin] = {'net': current_net, 'direction': direction}

    # --- Build components dict ---
    all_refs = set(comp_primitives) | set(comp_pins)
    for ref in all_refs:
        prim_name = comp_primitives.get(ref, '')
        prim = primitives.get(prim_name, {})
        result['components'][ref] = {
            'device_type': prim_name,
            'part_name':   prim.get('part_name', ''),
            'value':       prim.get('value', ''),
            'mfg':         prim.get('mfg', ''),
            'mpn':         prim.get('mpn', ''),
            'package':     prim.get('package', ''),
            'func_des':    '',
            'sheet':       comp_sheets.get(ref, 'sch_1'),
            'pins':        comp_pins.get(ref, {}),
        }

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(schematic_folder: str | Path) -> dict:
    """
    Parse a schematic folder. Auto-detects Altium or Allegro export format.

    Returns:
        {
            'format': 'altium' | 'view' | 'pst',
            'nets': {net_name: {'connections': [{ref, pin, direction, func_des}]}},
            'components': {ref: {device_type, part_name, value, mfg, mpn, package,
                                  func_des, sheet, pins: {pin: {net, direction}}}}
        }
    """
    folder = Path(schematic_folder)

    if is_altium_folder(folder):
        return altium_parser.parse(folder)

    allegro_dir = folder / 'allegro' if (folder / 'allegro').exists() else folder

    fmt = detect_format(allegro_dir)
    if fmt == 'view':
        data = parse_view_format(allegro_dir)
    else:
        data = parse_pst_format(allegro_dir)

    data['format'] = fmt
    return data


def summary(data: dict) -> str:
    nets = len(data['nets'])
    comps = len(data['components'])
    conns = sum(len(n['connections']) for n in data['nets'].values())
    return f"Format: {data['format']} | Nets: {nets} | Components: {comps} | Connections: {conns}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse, json
    ap = argparse.ArgumentParser(description="Parse Cadence Allegro schematic exports")
    ap.add_argument("schematic_folder", help="Path to schematic folder (containing allegro/)")
    ap.add_argument("--json", action="store_true", help="Dump full parsed data as JSON")
    ap.add_argument("--net", help="Show connections for a specific net")
    ap.add_argument("--ref", help="Show pins for a specific component")
    args = ap.parse_args()

    data = parse(args.schematic_folder)
    print(summary(data))

    if args.json:
        print(json.dumps(data, indent=2))
    elif args.net:
        net = data['nets'].get(args.net)
        if net:
            print(f"\nNet {args.net}:")
            for c in net['connections']:
                print(f"  {c['ref']}.{c['pin']} ({c['direction']})")
        else:
            print(f"Net '{args.net}' not found")
    elif args.ref:
        comp = data['components'].get(args.ref)
        if comp:
            print(f"\n{args.ref}: {comp['part_name'] or comp['device_type']} {comp['value']}")
            for pin, info in comp['pins'].items():
                print(f"  pin {pin} ({info['direction']}) -> {info['net']}")
        else:
            print(f"Component '{args.ref}' not found")


if __name__ == "__main__":
    main()

