#!/usr/bin/env python3
"""
Altium / OrCadPCB2Netlist parser.

Parses .NET files exported from Altium Designer in OrCAD PCB II Netlist format.
Returns the same data structure as schematic_parser.parse() so it can be used
interchangeably throughout the review pipeline.
"""

import re
from pathlib import Path


# Pin line:        "  ( PIN_NUM NET_NAME )"
_PIN_RE = re.compile(r'^  \(\s+(\S+)\s+(.+?)\s*\)')


def _parse_comp_line(line: str) -> tuple[str, str, str] | None:
    """Parse a component header line: ( INDEX [footprint tokens...] REFDES VALUE
    
    The footprint can contain spaces (e.g. '20-LQFN 4x3mm'), so REFDES is always
    the second-to-last token and VALUE is always the last.
    Returns (footprint, refdes, value) or None.
    """
    # Strip leading ' ( ' and trailing content
    stripped = line.strip()
    if not stripped.startswith('('):
        return None
    tokens = stripped[1:].split()  # drop opening '('
    # Need at least: INDEX, FOOTPRINT, REFDES, VALUE = 4 tokens
    if len(tokens) < 4:
        return None
    # tokens[0] = INDEX, tokens[-2] = REFDES, tokens[-1] = VALUE
    # tokens[1:-2] = footprint (may be multiple tokens)
    footprint = ' '.join(tokens[1:-2])
    refdes = tokens[-2]
    value = tokens[-1]
    return footprint, refdes, value


def find_altium_net(folder: Path) -> Path | None:
    """Return path to the OrCadPCB2Netlist .NET file in an Altium export folder, or None."""
    # Case 1: Altium timestamped output folder  →  <folder>/Nets <timestamp>/OrCadPCB2Netlist/*.NET
    for nets_dir in sorted(folder.glob('Nets *')):
        if not nets_dir.is_dir():
            continue
        orcad_dir = nets_dir / 'OrCadPCB2Netlist'
        if orcad_dir.is_dir():
            nets = list(orcad_dir.glob('*.NET'))
            if nets:
                return nets[0]
    # Case 2: OrCadPCB2Netlist folder directly under the export root
    orcad_dir = folder / 'OrCadPCB2Netlist'
    if orcad_dir.is_dir():
        nets = list(orcad_dir.glob('*.NET'))
        if nets:
            return nets[0]
    # Case 3: .NET file directly in the export folder
    nets = list(folder.glob('*.NET'))
    return nets[0] if nets else None


def _parse_net_file(net_file: Path) -> dict:
    nets: dict = {}
    components: dict = {}
    current_ref: str | None = None

    for line in net_file.read_text(encoding='utf-8', errors='replace').splitlines():
        # Component start: exactly one leading space before '('
        if line.startswith(' (') and not line.startswith('  ('):
            parsed = _parse_comp_line(line)
            if parsed:
                footprint, ref, value = parsed
                current_ref = ref
                components[ref] = {
                    'device_type': footprint,
                    'part_name':   '',
                    'value':       value,
                    'mfg':         '',
                    'mpn':         '',
                    'package':     footprint,
                    'func_des':    '',
                    'sheet':       'sch_1',
                    'pins':        {},
                }
            continue

        # Pin line: exactly two leading spaces before '('
        if line.startswith('  (') and current_ref is not None:
            m = _PIN_RE.match(line)
            if m:
                pin, net = m.group(1), m.group(2).strip()
                components[current_ref]['pins'][pin] = {'net': net, 'direction': 'passive'}
                nets.setdefault(net, {'connections': []})['connections'].append({
                    'ref': current_ref, 'pin': pin, 'direction': 'passive', 'func_des': '',
                })
            continue

        # End of component block
        if line == ' )':
            current_ref = None

    return {'format': 'altium', 'nets': nets, 'components': components}


def parse(folder: Path) -> dict:
    """Parse an Altium export folder. Returns the same structure as schematic_parser.parse()."""
    net_file = find_altium_net(folder)
    if net_file is None:
        raise FileNotFoundError(f"No OrCadPCB2Netlist .NET file found in {folder}")
    return _parse_net_file(net_file)
