#!/usr/bin/env python3
"""
Schematic Builder — extends parsed schematic data with:
  - Net voltage and type inference from net names
  - Component role detection (pull_up, pull_down, decoupling, etc.)
  - Full schematic.yaml output

Usage:
    python schematic_builder.py <schematic_folder> [--output REVIEW/schematic.yaml]

The schematic_folder must contain an allegro/ subdirectory (or directly contain
Allegro export files). Output defaults to <schematic_folder>/REVIEW/schematic.yaml.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Path to schematic-reviewer scripts (for db_schema, pipeline_status)
_REVIEWER_SCRIPTS = Path(__file__).parents[2] / 'schematic-reviewer' / 'scripts'

# Add script directory to path so we can import the parser and extractor
sys.path.insert(0, str(Path(__file__).parent))
from schematic_parser import parse, summary
from pdf_annotation_extractor import extract_annotations, build_component_map


# ---------------------------------------------------------------------------
# Net voltage / type inference
# ---------------------------------------------------------------------------

CONFIDENCE_ORDER = ["unknown", "hint", "doubtful", "inferred", "deduced", "calculated", "confirmed", "certain"]

# Component ref-designator prefixes → semantic type
REF_PREFIX_TYPE = {
    'U':  'ic',
    'IC': 'ic',
    'R':  'resistor',
    'C':  'capacitor',
    'L':  'inductor',
    'D':  'diode',
    'Q':  'transistor',
    'T':  'transformer',
    'J':  'connector',
    'P':  'connector',
    'SW': 'switch',
    'F':  'fuse',
    'Y':  'crystal',
    'X':  'crystal',
    'FB': 'ferrite_bead',
    'TP': 'test_point',
    'BT': 'battery',
    'RL': 'relay',
    'FL': 'filter',
    'VR': 'voltage_regulator',
    'Z':  'zener',
    'LED': 'led',
    'OSC': 'oscillator',
}

# GND net name variants
GND_NAMES = re.compile(
    r'^(GND|AGND|DGND|PGND|CGND|SGND|CHASSIS|EARTH|PE|SHIELD|RTN)',
    re.IGNORECASE
)

# Voltage patterns in net names — ordered from most specific to least
# Each entry: (regex, voltage_value, confidence)
_VOLT_PATTERNS = [
    # Decimal with dot: +1.8V, 3.3V, 2.5V — must come before integer pattern
    (re.compile(r'(?<![0-9])(\d+\.\d+)V(?!\d)', re.IGNORECASE), None, 'hint'),
    # Exact decimal: 3V3 -> 3.3, 1V8 -> 1.8, 2V5 -> 2.5, 1V2 -> 1.2, 0V9 -> 0.9
    (re.compile(r'(?<![0-9])(\d+)V(\d+)', re.IGNORECASE), None, 'hint'),
    # Simple integer: 5V, 12V, 24V, 48V (word boundary or end)
    (re.compile(r'(?<![0-9.])(\d+)V(?!\d)', re.IGNORECASE), None, 'hint'),
    # VBUS -> 5V (USB)
    (re.compile(r'\bVBUS\b', re.IGNORECASE), 5.0, 'doubtful'),
    # VIN, VMAIN, VSUPPLY (unknown voltage, power rail)
    (re.compile(r'\b(VIN|VMAIN|VSUPPLY|VPWR|VPOWER|V_IN)\b', re.IGNORECASE), None, 'doubtful'),
    # VCC, VDD, AVDD, DVDD (unknown voltage, power rail)
    (re.compile(r'\b(VCC|VDD|AVDD|DVDD|AVCC|DVCC|IOVCC|IOVDD|VCORE|VANA|VDIG)\b', re.IGNORECASE), None, 'doubtful'),
    # VREF (reference voltage, unknown)
    (re.compile(r'\bVREF\b', re.IGNORECASE), None, 'doubtful'),
]

# Net type keywords (checked in order; first match wins)
_TYPE_PATTERNS = [
    (re.compile(r'\bI2C\b', re.IGNORECASE),           'i2c'),
    (re.compile(r'\bSPI\b', re.IGNORECASE),            'spi'),
    (re.compile(r'\bUART|USART|RS232|RS485\b', re.IGNORECASE), 'uart'),
    (re.compile(r'\bCAN\b', re.IGNORECASE),            'can'),
    (re.compile(r'\bUSB\b', re.IGNORECASE),            'usb'),
    (re.compile(r'\bETH|RGMII|RMII|SGMII\b', re.IGNORECASE), 'ethernet'),
    (re.compile(r'\bCLK|OSC\b', re.IGNORECASE),        'clock'),
    (re.compile(r'\bRST|RESET\b', re.IGNORECASE),      'reset'),
    (re.compile(r'\bIRQ|INT\b', re.IGNORECASE),        'interrupt'),
    (re.compile(r'\bPWM\b', re.IGNORECASE),            'pwm'),
    (re.compile(r'\bADC|AIN|ANA\b', re.IGNORECASE),   'analog'),
    (re.compile(r'\bDAC|AOUT\b', re.IGNORECASE),       'analog'),
    (re.compile(r'\bDIFF|_P$|_N$|_DP|_DN\b', re.IGNORECASE), 'differential'),
    (re.compile(r'\bPWR|PWR|VCC|VDD|VIN|VOUT\b', re.IGNORECASE), 'power'),
]


def infer_net_voltage(net_name: str) -> dict:
    """
    Infer voltage and type from a net name.
    Returns: {voltage: float|None, type: str, confidence: str}
    """
    # GND family
    if GND_NAMES.match(net_name):
        return {'voltage': 0.0, 'type': 'power', 'confidence': 'certain'}

    voltage = None
    confidence = 'unknown'

    for pat, fixed_v, conf in _VOLT_PATTERNS:
        m = pat.search(net_name)
        if m:
            confidence = conf
            if fixed_v is not None:
                voltage = fixed_v
            elif m.lastindex == 2:
                # e.g. 3V3 -> groups 3,3 -> 3.3
                voltage = float(f"{m.group(1)}.{m.group(2)}")
            elif m.lastindex == 1:
                # e.g. 5V -> group 5 -> 5.0
                voltage = float(m.group(1))
            break

    # Net type
    net_type = 'signal'
    if voltage is not None or GND_NAMES.match(net_name):
        net_type = 'power'
    else:
        for pat, t in _TYPE_PATTERNS:
            if pat.search(net_name):
                net_type = t
                break

    return {'voltage': voltage, 'type': net_type, 'confidence': confidence}


# ---------------------------------------------------------------------------
# Component type from ref designator
# ---------------------------------------------------------------------------

def ref_type(ref: str) -> str:
    """Return component semantic type from ref designator prefix."""
    for prefix in sorted(REF_PREFIX_TYPE, key=len, reverse=True):
        if ref.upper().startswith(prefix):
            return REF_PREFIX_TYPE[prefix]
    return 'other'


def is_passive_two_pin(comp: dict) -> bool:
    return len(comp['pins']) == 2


def nets_of(comp: dict) -> list[str]:
    return [p['net'] for p in comp['pins'].values()]


# ---------------------------------------------------------------------------
# Driver inference
# ---------------------------------------------------------------------------

# Component types that cannot actively drive a net
_PASSIVE_COMP_TYPES = frozenset({
    'resistor', 'capacitor', 'inductor', 'ferrite_bead',
    'test_point', 'filter', 'crystal', 'fuse',
})


def compute_drivers(nets_out: dict, comps_out: dict) -> int:
    """Pass-1 structural driver inference.

    Populates net['drivers'] with candidate driving components:
    - voltage_regulator / transistor on a power net → power_supply (inferred)
    - connector on any non-GND net → external (inferred)
    - pull-up resistor on a signal net → passive_pullup (inferred)
    - pull-down resistor on a signal net → passive_pulldown (inferred)

    Active ICs on power nets are loads — omitted here to avoid flooding
    every power net with every consumer. IC agents emit confirmed pin_directions
    in pass 2 (collect_enrichments.py) which supersede these inferred entries.

    Returns the total number of driver entries added.
    """
    total = 0
    for net_name, net_data in nets_out.items():
        net_data.setdefault('drivers', [])
        net_type = net_data.get('type', '')
        net_voltage = net_data.get('voltage')

        # GND is the system reference — no active driver to annotate
        if net_voltage == 0.0 and net_type == 'power':
            continue

        for conn in net_data.get('connections', []):
            ref = conn['ref']
            pin = conn['pin']
            comp = comps_out.get(ref, {})
            ctype = comp.get('comp_type', '')
            role = comp.get('role', '')

            if ctype in _PASSIVE_COMP_TYPES:
                # Pull-up/pull-down resistors passively condition signal nets
                if ctype == 'resistor' and net_type != 'power':
                    if role == 'pull_up':
                        net_data['drivers'].append({
                            'ref': ref, 'pin': pin,
                            'drive_type': 'passive_pullup',
                            'confidence': 'inferred',
                        })
                        total += 1
                    elif role == 'pull_down':
                        net_data['drivers'].append({
                            'ref': ref, 'pin': pin,
                            'drive_type': 'passive_pulldown',
                            'confidence': 'inferred',
                        })
                        total += 1
                continue

            if ctype == 'connector':
                net_data['drivers'].append({
                    'ref': ref, 'pin': pin,
                    'drive_type': 'external',
                    'confidence': 'inferred',
                })
                total += 1

            elif net_type == 'power' and ctype in ('voltage_regulator', 'transistor'):
                # VRs and pass transistors are candidate power-plane drivers.
                # (Note: this includes their input pin too; IC agents will prune
                # to the correct output pin in pass 2.)
                net_data['drivers'].append({
                    'ref': ref, 'pin': pin,
                    'drive_type': 'power_supply',
                    'confidence': 'inferred',
                })
                total += 1
            # All other active ICs on power nets are loads — omitted.
            # Signal-net IC drivers require pin_directions from IC agents (pass 2).

    return total


# ---------------------------------------------------------------------------
# Component role detection
# ---------------------------------------------------------------------------

def detect_roles(data: dict) -> dict:
    """
    Detect roles for all components.
    Returns: {ref: role_string}

    Uses stored net voltages/types (from propagation or enrichment) first,
    falling back to name-based inference. This means propagate_voltages()
    should be called on the nets dict before this function.
    """
    nets = data['nets']
    components = data['components']

    def _stored_voltage(net_name: str) -> float | None:
        return nets.get(net_name, {}).get('voltage')

    def _net_voltage(net_name: str) -> float | None:
        v = _stored_voltage(net_name)
        if v is not None:
            return v
        return infer_net_voltage(net_name).get('voltage')

    def is_power(net_name: str) -> bool:
        """True if net is a power/supply rail (stored type takes priority over name inference)."""
        stored = nets.get(net_name, {})
        if stored.get('type') in ('power', 'gnd'):
            return True
        return infer_net_voltage(net_name).get('type') == 'power'

    def is_gnd(net_name: str) -> bool:
        v = _net_voltage(net_name)
        return v == 0.0

    def is_hv_power(net_name: str) -> bool:
        v = _net_voltage(net_name)
        return v is not None and v > 0.0

    roles = {}
    for ref, comp in components.items():
        ctype = ref_type(ref)
        role = None

        if ctype == 'resistor' and is_passive_two_pin(comp):
            pin_nets = nets_of(comp)
            if len(pin_nets) == 2:
                a, b = pin_nets
                if is_hv_power(a) and not is_power(b):
                    role = 'pull_up'
                elif is_hv_power(b) and not is_power(a):
                    role = 'pull_up'
                elif is_gnd(a) and not is_power(b):
                    role = 'pull_down'
                elif is_gnd(b) and not is_power(a):
                    role = 'pull_down'
                else:
                    role = 'series'

        elif ctype == 'capacitor' and is_passive_two_pin(comp):
            pin_nets = nets_of(comp)
            if len(pin_nets) == 2:
                a, b = pin_nets
                if (is_hv_power(a) and is_gnd(b)) or (is_gnd(a) and is_hv_power(b)):
                    role = 'decoupling'
                elif is_power(a) and is_gnd(b) or is_gnd(a) and is_power(b):
                    role = 'decoupling'
                else:
                    role = 'ac_coupling'

        elif ctype == 'inductor' and is_passive_two_pin(comp):
            pin_nets = nets_of(comp)
            if len(pin_nets) == 2:
                a, b = pin_nets
                if (is_power(a) and is_power(b)):
                    role = 'power_filter'
                else:
                    role = 'series'

        elif ctype == 'ferrite_bead':
            role = 'power_filter'

        roles[ref] = role

    return roles


# ---------------------------------------------------------------------------
# Variant / DNP loading
# ---------------------------------------------------------------------------

def load_variants(review_dir: Path) -> dict:
    """
    Load REVIEW/variants.yaml if it exists.

    Returns a dict like:
    {
        'PCB_ASSY1017453-3': {'dnp_refs': {'R45', 'C102', ...}},
        'PCB_ASSY1017636-2': {'dnp_refs': {'R12', 'C44', ...}},
        '_meta': [
            {'assembly': 'PCB_ASSY1017453-3', 'pdf': '...', 'dnp_count': N},
            ...
        ]
    }
    or {} if no variants.yaml.
    """
    variants_path = review_dir / 'variants.yaml'
    if not variants_path.exists():
        return {}
    with open(variants_path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    result: dict = {}
    meta_list = []
    for v in data.get('variants', []):
        assy = v.get('assembly', '')
        dnp_refs = set(v.get('dnp_refs', []))
        # Build substitution lookup: root_ref → {variant_value, variant_part_number}
        subs: dict[str, dict] = {}
        for s in v.get('substitutions', []):
            ref = s.get('ref', '')
            if ref:
                subs[ref] = {
                    'value':       s.get('variant_value', ''),
                    'part_number': s.get('variant_part_number', ''),
                }
        result[assy] = {'dnp_refs': dnp_refs, 'substitutions': subs}
        meta_list.append({
            'assembly':          assy,
            'pdf':               v.get('pdf', ''),
            'dnp_count':         len(dnp_refs),
            'substitution_count': len(subs),
        })
    result['_meta'] = meta_list
    return result


# ---------------------------------------------------------------------------
# Internal part number lookup (from BOM if available)
# ---------------------------------------------------------------------------

def load_bom_ids(review_dir: Path) -> dict:
    """Try to find the internal part_number for components from BOM files in REVIEW/."""
    ids = {}
    for bom_file in review_dir.glob('*.csv'):
        try:
            import csv
            with open(bom_file, encoding='utf-8', errors='replace', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ref = row.get('Ref Des', row.get('REFDES', row.get('Reference', ''))).strip()
                    part_id = row.get('Part ID', row.get('PART_ID', row.get('Part Number', ''))).strip()
                    if ref and part_id:
                        ids[ref] = part_id
        except Exception:
            pass
    return ids


# ---------------------------------------------------------------------------
# Net voltage resolver
# ---------------------------------------------------------------------------

def _parse_resistance_simple(value_str: str) -> float | None:
    """Parse a resistance value string to ohms. Returns None if unparseable.

    Handles:
      - Standard SI: 10k, 4K7, 1R5, 100, 100R, 100Ω
      - Milli-ohm:   10m, 100m  (lowercase m = milli = 0.001)
      - Mega-ohm:    1M          (uppercase M = Mega = 1e6)
      - Altium symbolic zero-ohm: RES_x0_*, *_x0, *_0R_*  → 0.0
    """
    if not value_str:
        return None
    s = value_str.strip()

    # Altium symbolic zero-ohm footprint names (e.g. RES_x0_Nettie_WFCP0612)
    if re.search(r'[_\-]x0[_\-]|[_\-]x0$|^x0[_\-]', s, re.IGNORECASE):
        return 0.0

    m = re.match(r'^(\d+(?:\.\d+)?)[Kk](\d+)$', s)
    if m:
        return float(m.group(1)) * 1000 + float(m.group(2)) * 100
    m = re.match(r'^(\d+(?:\.\d+)?)[Rr](\d+)$', s)
    if m:
        return float(m.group(1)) + float(m.group(2)) * 0.1
    # Note: lowercase 'm' = milli (0.001); uppercase 'M' = Mega (1e6)
    m = re.match(r'^(\d+(?:\.\d+)?)([KkMmRrΩ]?)$', s)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2)
    if suffix in ('K', 'k'):
        return val * 1000
    if suffix == 'M':
        return val * 1e6
    if suffix == 'm':
        return val * 0.001
    return val  # R, Ω, or bare number = ohms


def propagate_voltages(nets_out: dict, components: dict) -> int:
    """
    Propagate known net voltages to adjacent unknown nets through DC-coupled passives:
      - Inductors and ferrite beads (DC short circuit)
      - Resistors with value ≤ 10Ω (RC inrush filters, current-sense shunts)

    Iterates to convergence (handles chains of passives). Returns total nets updated.
    This is the first iteration of voltage resolution — runs on name-inferred voltages
    at build time. A second pass runs in collect_enrichments.py after IC agent enrichment.
    """
    total = 0
    while True:
        changed = 0
        for ref, comp in components.items():
            ctype = ref_type(ref)
            if ctype not in ('inductor', 'ferrite_bead', 'resistor'):
                continue

            if ctype == 'resistor':
                r = _parse_resistance_simple(comp.get('value', ''))
                if r is None or r > 10.0:
                    continue

            pins = comp.get('pins', {})
            if len(pins) != 2:
                continue

            pin_list = list(pins.values())
            net_a = pin_list[0].get('net', '')
            net_b = pin_list[1].get('net', '')

            v_a = nets_out.get(net_a, {}).get('voltage')
            v_b = nets_out.get(net_b, {}).get('voltage')

            if v_a is not None and v_b is None and net_b in nets_out:
                nets_out[net_b]['voltage'] = v_a
                nets_out[net_b]['confidence'] = 'inferred'
                nets_out[net_b]['type'] = 'power'
                changed += 1
            elif v_b is not None and v_a is None and net_a in nets_out:
                nets_out[net_a]['voltage'] = v_b
                nets_out[net_a]['confidence'] = 'inferred'
                nets_out[net_a]['type'] = 'power'
                changed += 1

        if not changed:
            break
        total += changed

    return total


def resolve_net_voltages(data: dict) -> int:
    """
    Resolve unknown net voltages by tracing pull-up resistors.
    Returns count of nets resolved.

    Algorithm:
    For each net with voltage=None (unknown/doubtful):
      Look at connected components with role='pull_up'
      For each pull_up resistor on the net:
        Find the resistor's other pin's net
        If that net has a known voltage (not None):
          Set this net's voltage to that voltage
          Set confidence to 'doubtful'
          Add comment: "Inferred from pull-up {ref} via net {other_net}"
    """
    nets = data.get('nets', {})
    components = data.get('components', {})
    resolved = 0

    for net_name, net_data in nets.items():
        if net_data.get('voltage') is not None:
            continue

        for ref, comp in components.items():
            if comp.get('role') != 'pull_up':
                continue

            pins = comp.get('pins', {})
            if len(pins) != 2:
                continue

            pin_nets = [p.get('net', '') for p in pins.values()]
            if net_name not in pin_nets:
                continue

            other_nets = [n for n in pin_nets if n != net_name]
            if not other_nets:
                continue
            other_net = other_nets[0]

            other_voltage = nets.get(other_net, {}).get('voltage')
            if other_voltage is not None:
                net_data['voltage'] = other_voltage
                net_data['confidence'] = 'doubtful'
                comment = f"Inferred from pull-up {ref} via net {other_net}"
                review = net_data.setdefault('review', {'status': 'pending', 'comments': []})
                review.setdefault('comments', []).append(comment)
                resolved += 1
                break

    return resolved


# ---------------------------------------------------------------------------
# PDF annotation enrichment
# ---------------------------------------------------------------------------

def find_schematic_pdf(schematic_folder: Path) -> Path | None:
    """
    Auto-detect the base schematic PDF in a review folder.
    Excludes variant PDFs (those containing '_PCB_ASSY' in the name).
    Prefers a PDF whose stem matches the folder name (case-insensitive).
    Also searches parent folder if no PDF found in the folder itself.
    """
    def _candidates(search_dir: Path) -> list[Path]:
        # Use both patterns to handle case-insensitive .PDF on all platforms
        found = list(search_dir.glob('*.pdf')) + list(search_dir.glob('*.PDF'))
        seen = set()
        result = []
        for p in found:
            key = str(p).lower()
            if key not in seen and '_PCB_ASSY' not in p.name.upper() and not p.name.startswith('.'):
                seen.add(key)
                result.append(p)
        return result

    pdfs = _candidates(schematic_folder)
    if not pdfs:
        pdfs = _candidates(schematic_folder.parent)
    if not pdfs:
        return None
    if len(pdfs) == 1:
        return pdfs[0]
    # Multiple candidates — prefer one matching folder name
    folder_stem = schematic_folder.name.lower()
    for p in pdfs:
        if p.stem.lower() == folder_stem:
            return p
    return pdfs[0]


def _parse_voltage_str(v_str: str) -> float | None:
    """Parse a voltage string like '50V', '6V3', '16V' into a float. Returns None for '0V' or empty."""
    if not v_str:
        return None
    m = re.match(r'^(\d+(?:\.\d+)?)V(\d+)?$', v_str.strip(), re.IGNORECASE)
    if not m:
        return None
    integer_part = m.group(1)   # e.g. "6" or "50"
    frac_part = m.group(2)      # e.g. "3" for 6V3, or None
    try:
        if frac_part and '.' not in integer_part:
            # European notation: 6V3 → 6.3V
            voltage = float(f"{integer_part}.{frac_part}")
        else:
            voltage = float(integer_part)
    except ValueError:
        return None
    return voltage if voltage > 0.0 else None


def _parse_tolerance_str(t_str: str) -> float | None:
    """Parse a tolerance string like '10%', '5%' into a float (percent). Returns None if unparseable."""
    if not t_str:
        return None
    m = re.match(r'^(\d+(?:\.\d+)?)%$', t_str.strip())
    return float(m.group(1)) if m else None


def enrich_from_pdf(comps_out: dict, pdf_path: Path) -> int:
    """
    Enrich comps_out in-place with PDF annotation data.
    Sets: rated_voltage, tolerance, part_number, mfg_part_number, manufacturer.
    Returns the number of components enriched.
    """
    print(f"Extracting PDF annotations from {pdf_path.name} (this may take 1-2 min)...")
    annots = extract_annotations(pdf_path, verbose=False)
    print(f"  Found {len(annots)} annotations")
    component_map = build_component_map(annots)
    print(f"  Parsed {len(component_map)} unique components with data")

    enriched = 0
    for ref, props in component_map.items():
        if ref not in comps_out:
            continue
        comp = comps_out[ref]
        changed = False

        try:
            rated_v = _parse_voltage_str(props.get('voltage_rating', ''))
            if rated_v is not None and not comp.get('rated_voltage'):
                comp['rated_voltage'] = rated_v
                changed = True

            tol = _parse_tolerance_str(props.get('tolerance', ''))
            if tol is not None and not comp.get('tolerance'):
                comp['tolerance'] = tol
                changed = True

            pn = props.get('highstage_pn', '')
            if pn and not comp.get('part_number'):
                comp['part_number'] = pn
                changed = True

            mpn = props.get('mpn1', '') or props.get('mpn2', '')
            if mpn and not comp.get('mfg_part_number'):
                comp['mfg_part_number'] = mpn
                changed = True

            mfg = props.get('mfg1', '') or props.get('mfg2', '')
            if mfg and not comp.get('manufacturer'):
                comp['manufacturer'] = mfg
                changed = True
        except Exception:
            pass

        if changed:
            enriched += 1

    return enriched


def enrich_from_flat_bom(comps_out: dict, export_folder: Path) -> int:
    """
    Enrich comps_out in-place from a flat ERP-style BOM XLSX (Ref.Des/Part Label/Artikelnummer).
    Sets: part_number, value/description, dnp.
    Returns the number of components enriched.
    """
    from flat_bom_reader import read_flat_bom
    print(f"Reading flat BOM from {export_folder.name}...")
    component_map = read_flat_bom(export_folder)
    print(f"  Parsed {len(component_map)} BOM entries")

    enriched = 0
    for ref, props in component_map.items():
        if ref not in comps_out:
            continue
        comp = comps_out[ref]
        changed = False

        try:
            pn = props.get('part_number', '')
            if pn and not comp.get('part_number'):
                comp['part_number'] = pn
                changed = True

            value = props.get('value', '')
            if value and not comp.get('value'):
                comp['value'] = value
                changed = True

            desc = props.get('description', '')
            if desc and not comp.get('description'):
                comp['description'] = desc
                changed = True

            if props.get('dnp'):
                comp['dnp'] = True
                changed = True
        except Exception:
            pass

        if changed:
            enriched += 1

    return enriched



    """
    Enrich comps_out in-place from Altium XLSX BOMs.
    Sets: part_number (highstage), mfg_part_number, manufacturer, mfg, mpn.
    Returns the number of components enriched.
    """
    from altium_bom_reader import read_altium_bom
    print(f"Reading Altium BOM from {export_folder.name}...")
    component_map = read_altium_bom(export_folder)
    print(f"  Parsed {len(component_map)} BOM entries")

    enriched = 0
    for ref, props in component_map.items():
        if ref not in comps_out:
            continue
        comp = comps_out[ref]
        changed = False

        try:
            pn = props.get('highstage_pn', '')
            if pn and not comp.get('part_number'):
                comp['part_number'] = pn
                changed = True

            mpn = props.get('mpn1', '')
            if mpn:
                if not comp.get('mfg_part_number'):
                    comp['mfg_part_number'] = mpn
                    changed = True
                if not comp.get('mpn'):
                    comp['mpn'] = mpn
                    changed = True

            mfg = props.get('mfg1', '')
            if mfg:
                if not comp.get('manufacturer'):
                    comp['manufacturer'] = mfg
                    changed = True
                if not comp.get('mfg'):
                    comp['mfg'] = mfg
                    changed = True
        except Exception:
            pass

        if changed:
            enriched += 1

    return enriched




# ---------------------------------------------------------------------------
# Xilinx package file parser
# ---------------------------------------------------------------------------

def _parse_xilinx_pkg_file(pkg_path: Path) -> dict:
    """Parse a Xilinx package pin file.

    Returns a dict mapping pin_number -> {'name': str, 'bank': str, 'io_type': str}.
    Rows where bank is 'NA' (pure power/GND pins) are skipped.
    """
    result = {}
    in_data = False
    with open(pkg_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip()
            if not in_data:
                # The header line starts with 'Pin' and contains 'Pin Name'
                if line.startswith('Pin') and 'Pin Name' in line:
                    in_data = True
                continue

            parts = line.split()
            if len(parts) < 4:
                continue

            pin      = parts[0]
            name     = parts[1]
            # parts[2] = Memory Byte Group (single token: "0U", "1L", "NA", ...)
            bank     = parts[3]
            io_type  = parts[4] if len(parts) > 4 else 'NA'

            if bank == 'NA':
                continue

            result[pin] = {'name': name, 'bank': bank, 'io_type': io_type}

    return result


# ---------------------------------------------------------------------------
# FPGA pin enrichment (Xilinx package file)
# ---------------------------------------------------------------------------

# Voltage patches for signal nets whose voltage is known from FPGA bank information.
# These are applied to nets_out after all other enrichment so that the confidence
# is preserved and later agents can use an accurate voltage.
_VOLTAGE_PATCH: dict[str, tuple[float, str]] = {
    'SI.DVL.PWR.EN':      (3.3, 'U1 pin AB15, IO_L7N_T1L_N1_QBC_AD13N_64, bank 64 HR'),
    'SI.IMU.PWR.EN':      (1.8, 'U1 pin J13,  IO_L9P_T1L_N4_AD12P_66,     bank 66 HP'),
    'SI.Pressure.PWR.EN': (3.3, 'U1 pin AC12, IO_L13P_T2L_N0_GC_QBC_64,   bank 64 HR'),
    'SI.SVP.PWR.EN':      (3.3, 'U1 pin AD14, IO_L6P_T0U_N10_AD6P_64,     bank 64 HR'),
}


def _enrich_fpga_pins(comps_out: dict, nets_out: dict, repo_root: Path) -> int:
    """Enrich FPGA component pins using Xilinx package pin files.

    For each component whose mpn (or value) matches a sub-folder under
    datasheets/<mpn>/*pkg.txt, parse the file and annotate every pin with:
      - name     : full Xilinx pin name (e.g. IO_L6P_T0U_N10_AD6P_64)
      - bank     : I/O bank number string (e.g. '64')
      - io_type  : HR / HP / NA

    After annotating pins, derives bank_voltages from the VCCO_xx pins already
    present in the schematic and stores them on the component dict.  Each pin
    then gets a bank_voltage field if its bank is known.

    Returns total number of pins enriched.
    """
    datasheets_dir = repo_root / 'datasheets'
    total_enriched = 0

    for ref, comp in comps_out.items():
        mpn = comp.get('mpn', '') or comp.get('value', '')
        if not mpn:
            continue

        pkg_dir = datasheets_dir / mpn
        if not pkg_dir.is_dir():
            continue
        pkg_files = list(pkg_dir.glob('*pkg.txt'))
        if not pkg_files:
            continue

        pkg_path = pkg_files[0]
        print(f"  Enriching {ref} ({mpn}) pins from {pkg_path.name}...")

        try:
            pin_info = _parse_xilinx_pkg_file(pkg_path)
        except Exception as exc:
            print(f"  WARNING: Failed to parse {pkg_path}: {exc}")
            continue

        pins = comp.get('pins', {})

        # --- Step 1: annotate each pin with name / bank / io_type ---
        enriched_count = 0
        for pin_num, pin_data in pins.items():
            info = pin_info.get(pin_num)
            if not info:
                continue
            pin_data['name'] = info['name']
            pin_data['bank'] = info['bank']
            if info['io_type'] != 'NA':
                pin_data['io_type'] = info['io_type']
            enriched_count += 1

        # --- Step 2: derive bank voltages from VCCO_xx pin nets ---
        bank_voltages: dict[str, float] = {}
        for pin_num, pin_data in pins.items():
            info = pin_info.get(pin_num)
            if not info:
                continue
            if not info['name'].startswith('VCCO_'):
                continue
            net = pin_data.get('net', '')
            if not net:
                continue
            v = nets_out.get(net, {}).get('voltage')
            if v is not None and info['bank'] not in bank_voltages:
                bank_voltages[info['bank']] = v

        # --- Step 3: store bank_voltages on component, annotate each pin ---
        if bank_voltages:
            comp['bank_voltages'] = dict(sorted(bank_voltages.items()))
            for pin_data in pins.values():
                b = pin_data.get('bank')
                if b and b in bank_voltages:
                    pin_data['bank_voltage'] = bank_voltages[b]

        print(f"    {enriched_count}/{len(pins)} pins enriched; "
              f"{len(bank_voltages)} bank voltages: {bank_voltages}")
        total_enriched += enriched_count

    return total_enriched


def build_yaml(schematic_folder: Path, output_path: Path, pdf_path: Path | None = None) -> None:
    """Parse schematic and write schematic.yaml to output_path."""
    allegro_dir = schematic_folder / 'allegro' if (schematic_folder / 'allegro').exists() else schematic_folder
    source_hash = _hash_dir(allegro_dir)

    print(f"Parsing {schematic_folder}...")
    data = parse(schematic_folder)
    print(summary(data))

    review_dir = output_path.parent
    bom_ids = load_bom_ids(review_dir)
    variants = load_variants(review_dir)

    # --- nets (built first so propagation can run before role detection) ---
    nets_out = {}
    for net_name, net_data in sorted(data['nets'].items()):
        info = infer_net_voltage(net_name)
        nets_out[net_name] = {
            'voltage':     info['voltage'],
            'type':        info['type'],
            'confidence':  info['confidence'],
            'connections': [
                {
                    'ref':       c['ref'],
                    'pin':       c['pin'],
                    'direction': c['direction'],
                    **(({'func_des': c['func_des']} if c.get('func_des') else {})),
                }
                for c in net_data['connections']
            ],
            'review': {'status': 'pending', 'comments': []},
        }

    # Iteration 1: propagate voltages through inductors/ferrite beads/low-R resistors
    # before role detection so RC filters and LC filter outputs get correct voltage/type.
    propagated = propagate_voltages(nets_out, data['components'])
    if propagated:
        print(f"  Voltage propagation (pass 1): {propagated} nets resolved via inductors/low-R resistors")

    # Role detection now uses the propagated net voltages
    roles = detect_roles({'nets': nets_out, 'components': data['components']})

    # --- components ---
    comps_out = {}
    for ref, comp in sorted(data['components'].items()):
        pins_out = {}
        for pin_name, pin_data in comp['pins'].items():
            pins_out[pin_name] = {
                'direction': pin_data['direction'],
                'net':       pin_data['net'],
            }

        schematic_ref = {}
        if comp.get('func_des'):
            schematic_ref['func_des'] = comp['func_des']
        if comp.get('sheet'):
            schematic_ref['sheet'] = comp['sheet']

        ctype = ref_type(ref)
        # test_points have no meaningful electrical stress — auto-verified.
        # Everything else starts unverified until a check script or IC agent clears it.
        auto_verified = ctype == 'test_point'

        comps_out[ref] = {
            'device_type':    comp['device_type'],
            'part_name':      comp['part_name'],
            'value':          comp['value'],
            'mfg':            comp['mfg'],
            'mpn':            comp['mpn'],
            'package':        comp['package'],
            'part_number':    bom_ids.get(ref, ''),
            'comp_type':      ctype,
            'role':           roles.get(ref),
            'verified':       auto_verified,
            'schematic_ref':  schematic_ref,
            'pins':           pins_out,
            'review':         {'status': 'pending', 'comments': []},
        }

        # Add variants section if variants.yaml was loaded
        variant_assemblies = [k for k in variants if k != '_meta']
        if variant_assemblies:
            comp_variants = {}
            for assy in variant_assemblies:
                dnp = ref in variants[assy]['dnp_refs']
                v_entry: dict = {'dnp': dnp}
                # Include substitution overrides only when they differ from base
                sub = variants[assy].get('substitutions', {}).get(ref)
                if sub:
                    base_value = comp.get('value', '')
                    if sub.get('value') and sub['value'] != base_value:
                        v_entry['value'] = sub['value']
                    if sub.get('part_number'):
                        base_pn = bom_ids.get(ref, '')
                        if sub['part_number'] != base_pn:
                            v_entry['part_number'] = sub['part_number']
                comp_variants[assy] = v_entry
            comps_out[ref]['variants'] = comp_variants

    resolved = resolve_net_voltages({'nets': nets_out, 'components': comps_out})
    if resolved:
        print(f"  Net voltages resolved via pull-up tracing: {resolved}")

    # --- PDF annotation enrichment (Allegro) or BOM enrichment (Altium/FlatNet) ---
    if data['format'] == 'altium':
        try:
            enriched = enrich_from_altium_bom(comps_out, schematic_folder)
            print(f"  Altium BOM enrichment: {enriched} components got MPN/manufacturer/part number data")
        except Exception as exc:
            print(f"  WARNING: Altium BOM enrichment failed: {exc}")
    elif data['format'] == 'flatnet':
        try:
            enriched = enrich_from_flat_bom(comps_out, schematic_folder)
            print(f"  Flat BOM enrichment: {enriched} components got part number/value data")
        except Exception as exc:
            print(f"  WARNING: Flat BOM enrichment failed: {exc}")
    else:
        effective_pdf = pdf_path or find_schematic_pdf(schematic_folder)
        if effective_pdf and effective_pdf.exists():
            try:
                enriched = enrich_from_pdf(comps_out, effective_pdf)
                print(f"  PDF enrichment: {enriched} components got rated_voltage/tolerance/MPN data")
            except Exception as exc:
                print(f"  WARNING: PDF enrichment failed: {exc}")
        else:
            print("  No schematic PDF found — skipping PDF annotation enrichment")

    # --- FPGA pin enrichment from Xilinx package files ---
    repo_root = Path(__file__).parents[4]
    try:
        fpga_pins = _enrich_fpga_pins(comps_out, nets_out, repo_root)
        if fpga_pins:
            print(f"  FPGA pin enrichment: {fpga_pins} pins annotated with full name/bank/io_type")
    except Exception as exc:
        print(f"  WARNING: FPGA pin enrichment failed: {exc}")

    # --- Apply manual voltage patch for known FPGA I/O nets ---
    patch_applied = 0
    for net_name, (voltage, source) in _VOLTAGE_PATCH.items():
        if net_name in nets_out:
            nets_out[net_name]['voltage'] = voltage
            nets_out[net_name]['confidence'] = 'manual'
            nets_out[net_name]['voltage_source'] = source
            patch_applied += 1
    if patch_applied:
        print(f"  Voltage patch: {patch_applied} net voltages set from FPGA bank knowledge")

    # Pass-1 driver inference: structural heuristics (VRs, connectors, pull resistors).
    # IC agents will confirm/extend this via pin_directions in collect_enrichments.py.
    driver_entries = compute_drivers(nets_out, comps_out)
    if driver_entries:
        print(f"  Driver inference (pass 1): {driver_entries} entries across power/signal nets")

    doc = {
        'meta': {
            'schematic_id': schematic_folder.name,
            'format':       data['format'],
            'generated':    datetime.now(timezone.utc).isoformat(),
            'source_hash':  source_hash,
            'source_dir':   str(allegro_dir),
            **({'variants': variants['_meta']} if variants.get('_meta') else {}),
        },
        'nets':       nets_out,
        'components': comps_out,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        yaml.dump(doc, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    print(f"\nWrote {output_path}")
    print(f"  Nets:       {len(nets_out)}")
    print(f"  Components: {len(comps_out)}")

    # Role summary
    from collections import Counter
    role_counts = Counter(r for r in roles.values() if r)
    if role_counts:
        print("  Roles detected:")
        for role, count in role_counts.most_common():
            print(f"    {role}: {count}")


def build_db(schematic_folder: Path, db_path: Path, pdf_path: Path | None = None) -> None:
    """Parse schematic and write review.db to db_path."""
    if str(_REVIEWER_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_REVIEWER_SCRIPTS))

    from db_schema import create_db
    from pipeline_status import mark_stage

    allegro_dir = schematic_folder / 'allegro' if (schematic_folder / 'allegro').exists() else schematic_folder
    source_hash = _hash_dir(allegro_dir)

    print(f"Parsing {schematic_folder}...")
    data = parse(schematic_folder)
    print(summary(data))

    review_dir = db_path.parent
    bom_ids = load_bom_ids(review_dir)
    variants = load_variants(review_dir)

    # --- nets (built first so propagation runs before role detection) ---
    nets_out = {}
    for net_name, net_data in sorted(data['nets'].items()):
        info = infer_net_voltage(net_name)
        nets_out[net_name] = {
            'voltage':     info['voltage'],
            'type':        info['type'],
            'confidence':  info['confidence'],
            'connections': [
                {
                    'ref':       c['ref'],
                    'pin':       c['pin'],
                    'direction': c['direction'],
                    **(({'func_des': c['func_des']} if c.get('func_des') else {})),
                }
                for c in net_data['connections']
            ],
        }

    propagated = propagate_voltages(nets_out, data['components'])
    if propagated:
        print(f"  Voltage propagation (pass 1): {propagated} nets resolved via inductors/low-R resistors")

    roles = detect_roles({'nets': nets_out, 'components': data['components']})

    # --- components ---
    comps_out = {}
    for ref, comp in sorted(data['components'].items()):
        pins_out = {}
        for pin_name, pin_data in comp['pins'].items():
            pins_out[pin_name] = {
                'direction': pin_data['direction'],
                'net':       pin_data['net'],
            }

        schematic_ref = {}
        if comp.get('func_des'):
            schematic_ref['func_des'] = comp['func_des']
        if comp.get('sheet'):
            schematic_ref['sheet'] = comp['sheet']

        ctype = ref_type(ref)
        auto_verified = ctype == 'test_point'

        comps_out[ref] = {
            'device_type':   comp['device_type'],
            'part_name':     comp['part_name'],
            'value':         comp['value'],
            'mfg':           comp['mfg'],
            'mpn':           comp['mpn'],
            'package':       comp['package'],
            'part_number':   bom_ids.get(ref, ''),
            'comp_type':     ctype,
            'role':          roles.get(ref),
            'verified':      auto_verified,
            'schematic_ref': schematic_ref,
            'func_des':      comp.get('func_des'),
            'sheet':         comp.get('sheet'),
            'pins':          pins_out,
        }

        variant_assemblies = [k for k in variants if k != '_meta']
        if variant_assemblies:
            comp_variants = {}
            for assy in variant_assemblies:
                dnp = ref in variants[assy]['dnp_refs']
                v_entry: dict = {'dnp': dnp}
                sub = variants[assy].get('substitutions', {}).get(ref)
                if sub:
                    base_value = comp.get('value', '')
                    if sub.get('value') and sub['value'] != base_value:
                        v_entry['value'] = sub['value']
                    if sub.get('part_number'):
                        base_pn = bom_ids.get(ref, '')
                        if sub['part_number'] != base_pn:
                            v_entry['part_number'] = sub['part_number']
                comp_variants[assy] = v_entry
            comps_out[ref]['variants'] = comp_variants

    resolved = resolve_net_voltages({'nets': nets_out, 'components': comps_out})
    if resolved:
        print(f"  Net voltages resolved via pull-up tracing: {resolved}")

    # --- PDF annotation enrichment (Allegro) or BOM enrichment (Altium/FlatNet) ---
    if data['format'] == 'altium':
        try:
            enriched = enrich_from_altium_bom(comps_out, schematic_folder)
            print(f"  Altium BOM enrichment: {enriched} components got MPN/manufacturer/part number data")
        except Exception as exc:
            print(f"  WARNING: Altium BOM enrichment failed: {exc}")
    elif data['format'] == 'flatnet':
        try:
            enriched = enrich_from_flat_bom(comps_out, schematic_folder)
            print(f"  Flat BOM enrichment: {enriched} components got part number/value data")
        except Exception as exc:
            print(f"  WARNING: Flat BOM enrichment failed: {exc}")
    else:
        effective_pdf = pdf_path or find_schematic_pdf(schematic_folder)
        if effective_pdf and effective_pdf.exists():
            try:
                enriched = enrich_from_pdf(comps_out, effective_pdf)
                print(f"  PDF enrichment: {enriched} components got rated_voltage/tolerance/MPN data")
            except Exception as exc:
                print(f"  WARNING: PDF enrichment failed: {exc}")
        else:
            print("  No schematic PDF found — skipping PDF annotation enrichment")

    # --- FPGA pin enrichment ---
    repo_root = Path(__file__).parents[4]
    try:
        fpga_pins = _enrich_fpga_pins(comps_out, nets_out, repo_root)
        if fpga_pins:
            print(f"  FPGA pin enrichment: {fpga_pins} pins annotated with full name/bank/io_type")
    except Exception as exc:
        print(f"  WARNING: FPGA pin enrichment failed: {exc}")

    # --- Apply manual voltage patch ---
    patch_applied = 0
    for net_name, (voltage, source) in _VOLTAGE_PATCH.items():
        if net_name in nets_out:
            nets_out[net_name]['voltage'] = voltage
            nets_out[net_name]['confidence'] = 'manual'
            nets_out[net_name]['voltage_source'] = source
            patch_applied += 1
    if patch_applied:
        print(f"  Voltage patch: {patch_applied} net voltages set from FPGA bank knowledge")

    driver_entries = compute_drivers(nets_out, comps_out)
    if driver_entries:
        print(f"  Driver inference (pass 1): {driver_entries} entries across power/signal nets")

    # --- Write to review.db ---
    db_path.parent.mkdir(parents=True, exist_ok=True)
    create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # off during bulk import

    # meta
    for key, val in {
        'schematic_id': schematic_folder.name,
        'format':       data['format'],
        'generated':    datetime.now(timezone.utc).isoformat(),
        'source_hash':  source_hash,
        'source_dir':   str(allegro_dir),
    }.items():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(val)))

    if variants.get('_meta'):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     ('variants', json.dumps(variants['_meta'])))

    # nets
    for net_name, n in nets_out.items():
        conn.execute("""INSERT OR REPLACE INTO nets
            (name, voltage, voltage_min, voltage_max, confidence, type,
             voltage_source_ref, voltage_source_pin, voltage_driver_type, propagation_hops)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (net_name, n.get('voltage'), n.get('voltage_min'), n.get('voltage_max'),
             n.get('confidence', 'unknown'), n.get('type', 'unknown'),
             n.get('voltage_source_ref'), n.get('voltage_source_pin'),
             n.get('voltage_driver_type'), n.get('propagation_hops')))

    # net_connections
    for net_name, n in nets_out.items():
        for c in n.get('connections', []):
            conn.execute("INSERT OR IGNORE INTO net_connections (net, ref, pin) VALUES (?, ?, ?)",
                         (net_name, c['ref'], c['pin']))

    # components
    for ref, comp in comps_out.items():
        schematic_ref = comp.get('schematic_ref', {})
        func_des = comp.get('func_des') or schematic_ref.get('func_des')
        sheet = comp.get('sheet') or schematic_ref.get('sheet')
        conn.execute("""INSERT OR REPLACE INTO components
            (ref, comp_type, value, package, mfg_part_number, part_number,
             role, dnp, verified, func_des, sheet,
             rated_voltage, tolerance, power_rating, dielectric, temp_min_c, temp_max_c)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ref, comp.get('comp_type'), comp.get('value'), comp.get('package'),
             comp.get('mpn') or comp.get('mfg_part_number'),
             comp.get('part_number'),
             comp.get('role'),
             1 if comp.get('dnp') else 0,
             1 if comp.get('verified') else 0,
             func_des, sheet,
             comp.get('rated_voltage'), comp.get('tolerance'),
             comp.get('power_rating'), comp.get('dielectric'),
             comp.get('temp_min_c'), comp.get('temp_max_c')))

    # pins
    for ref, comp in comps_out.items():
        for pin_name, pin_data in comp.get('pins', {}).items():
            conn.execute("INSERT OR IGNORE INTO pins (ref, pin, net, direction) VALUES (?, ?, ?, ?)",
                         (ref, pin_name, pin_data.get('net'), pin_data.get('direction')))

    # net_drivers
    for net_name, n in nets_out.items():
        for drv in n.get('drivers', []):
            conn.execute("""INSERT INTO net_drivers (net, ref, pin, drive_type, confidence)
                VALUES (?, ?, ?, ?, ?)""",
                (net_name, drv['ref'], drv.get('pin'), drv['drive_type'], drv['confidence']))

    conn.commit()
    conn.close()

    mark_stage(db_path, 'build', 'done')

    print(f"\nWrote {db_path}")
    print(f"  Nets:       {len(nets_out)}")
    print(f"  Components: {len(comps_out)}")

    from collections import Counter
    role_counts = Counter(r for r in roles.values() if r)
    if role_counts:
        print("  Roles detected:")
        for role, count in role_counts.most_common():
            print(f"    {role}: {count}")


def _hash_dir(directory: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(directory.glob('*.dat')):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    # Altium: hash .NET files from OrCadPCB2Netlist subfolders and directly in directory
    for nets_dir in sorted(directory.glob('Nets *')):
        orcad_dir = nets_dir / 'OrCadPCB2Netlist'
        if orcad_dir.is_dir():
            for f in sorted(orcad_dir.glob('*.NET')):
                h.update(f.name.encode())
                h.update(f.read_bytes())
    for f in sorted(directory.glob('*.NET')):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build review.db from Allegro/Altium schematic exports")
    ap.add_argument("schematic_folder", help="Path to schematic folder (containing allegro/)")
    ap.add_argument("--output", "-o", help="Output path for review.db (default: REVIEW/review.db)")
    ap.add_argument("--pdf", type=Path, default=None,
                    help="Path to schematic PDF for annotation enrichment (auto-detected if omitted)")
    ap.add_argument("--yaml", action="store_true",
                    help="Write schematic.yaml instead of review.db (legacy mode)")
    args = ap.parse_args()

    folder = Path(args.schematic_folder)
    if args.yaml:
        output_path = Path(args.output) if args.output else folder / 'REVIEW' / 'schematic.yaml'
        build_yaml(folder, output_path, pdf_path=args.pdf)
    else:
        output_path = Path(args.output) if args.output else folder / 'REVIEW' / 'review.db'
        build_db(folder, output_path, pdf_path=args.pdf)


if __name__ == "__main__":
    main()
