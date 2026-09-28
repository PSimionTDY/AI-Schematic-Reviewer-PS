#!/usr/bin/env python3
"""
FlatNet parser.

Parses simple flat netlist text exports of the form:

    FlatNet: 'NET_NAME' REFDES-PIN REFDES-PIN REFDES-PIN ...

One line per net, listing every REFDES-PIN pair connected to that net.

Additionally supports single-pin "PIN" lines, used for nets with only one
connection (e.g. unconnected/internal pins):

    PIN : 'net_name' REFDES-PIN

Returns the same data structure as schematic_parser.parse() so it can be
used interchangeably throughout the review pipeline.
"""

import re
from pathlib import Path


_FLATNET_LINE_RE = re.compile(r"^FlatNet:\s+'(.*?)'\s+(.*)$")
_PIN_LINE_RE = re.compile(r"^PIN\s*:\s+'(.*?)'\s+(.*)$")


def find_flatnet_file(folder: Path) -> Path | None:
    """Return path to a FlatNet-format .txt file in the given folder, or None."""
    for txt_file in sorted(folder.glob('*.txt')):
        try:
            with open(txt_file, encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith('FlatNet:') or line.startswith('PIN'):
                        return txt_file
                    break  # first non-empty line wasn't a FlatNet/PIN line
        except OSError:
            continue
    return None


def _parse_token(token: str) -> tuple[str, str] | None:
    """Split a REFDES-PIN token into (refdes, pin). Uses the last '-' as separator."""
    if '-' not in token:
        return None
    ref, pin = token.rsplit('-', 1)
    if not ref or not pin:
        return None
    return ref, pin


def _parse_flatnet_file(flatnet_file: Path) -> dict:
    nets: dict = {}
    components: dict = {}

    def _add_connection(net_name: str, ref: str, pin: str) -> None:
        if ref not in components:
            components[ref] = {
                'device_type': '',
                'part_name':   '',
                'value':       '',
                'mfg':         '',
                'mpn':         '',
                'package':     '',
                'func_des':    '',
                'sheet':       'sch_1',
                'pins':        {},
            }

        components[ref]['pins'][pin] = {'net': net_name, 'direction': 'passive'}
        nets.setdefault(net_name, {'connections': []})['connections'].append({
            'ref': ref, 'pin': pin, 'direction': 'passive', 'func_des': '',
        })

    for line in flatnet_file.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith('FlatNet:'):
            m = _FLATNET_LINE_RE.match(line)
            if not m:
                continue

            net_name = m.group(1)
            tokens = m.group(2).split()

            for token in tokens:
                parsed = _parse_token(token)
                if parsed is None:
                    continue
                ref, pin = parsed
                _add_connection(net_name, ref, pin)
            continue

        if line.startswith('PIN'):
            m = _PIN_LINE_RE.match(line)
            if not m:
                continue

            net_name = m.group(1)
            token = m.group(2).strip()
            parsed = _parse_token(token)
            if parsed is None:
                continue
            ref, pin = parsed
            _add_connection(net_name, ref, pin)
            continue

    return {'format': 'flatnet', 'nets': nets, 'components': components}


def parse(folder: Path) -> dict:
    """Parse a folder containing a FlatNet .txt export. Returns the same structure as
    schematic_parser.parse()."""
    flatnet_file = find_flatnet_file(folder)
    if flatnet_file is None:
        raise FileNotFoundError(f"No FlatNet .txt file found in {folder}")
    return _parse_flatnet_file(flatnet_file)
