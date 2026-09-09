#!/usr/bin/env python3
"""
Verify IC pin assignments against manufacturer datasheets.

For each unique IC in the schematic (deduplicated by highstage_id / part_number):
  1. Downloads the datasheet PDF from the Highstage file share
  2. Extracts the pin table using PyMuPDF heuristics
  3. Cross-references each schematic pin against the datasheet type
  4. Flags mismatches (VCC on signal net, GND on power net, floating inputs, etc.)

DNP components are skipped. ICs without a Highstage ID emit a question issue.
PINUSE values from Allegro's chipsview.dat are intentionally ignored — use
datasheets as the authoritative source for pin types.

Datasheets are stored at the repo root under datasheets/<HIGHSTAGE_ID>/
so they are reused across all schematic reviews.

Usage:
    python verify_ic_pins.py reviews/SCH26913-1A
"""
import argparse
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Highstage file share
# ---------------------------------------------------------------------------
HIGHSTAGE_FILE_SHARE = r"\\highstage\files\PURCHASE_SPEC\IC"


def is_file_share_available() -> bool:
    return Path(HIGHSTAGE_FILE_SHARE).exists()


def find_datasheets_file(part_id: str) -> list[Path]:
    """Return all PDF files for *part_id* on the file share."""
    part_dir = Path(HIGHSTAGE_FILE_SHARE) / part_id
    if not part_dir.exists():
        return []
    return list(part_dir.rglob("*.pdf"))


def download_datasheet(part_id: str, output_dir: Path) -> list[Path]:
    """
    Copy datasheets for *part_id* from the Highstage file share to *output_dir*.
    Returns list of local Paths for successfully obtained PDFs.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pdfs = find_datasheets_file(part_id)
    if not pdfs:
        return []

    import shutil
    local_paths = []
    seen_names: set[str] = set()
    for pdf in pdfs:
        if pdf.name in seen_names:
            continue
        seen_names.add(pdf.name)
        dest = output_dir / pdf.name
        if not dest.exists():
            try:
                shutil.copy2(pdf, dest)
            except Exception as exc:
                print(f"  [warn] copy {pdf.name}: {exc}")
                continue
        local_paths.append(dest)
    return local_paths


# ---------------------------------------------------------------------------
# Pin-type normalisation
# ---------------------------------------------------------------------------
PIN_TYPE_MAP = {
    # power supply / VCC
    "vcc": "power", "vdd": "power", "vss_p": "power",
    "power": "power", "supply": "power", "pwr": "power",
    "avcc": "power", "avdd": "power", "dvcc": "power", "dvdd": "power",
    "iovcc": "power", "iovdd": "power", "vbat": "power",
    # ground
    "gnd": "ground", "ground": "ground", "vss": "ground",
    "agnd": "ground", "dgnd": "ground", "pgnd": "ground",
    "ep": "ground",  # exposed pad
    # digital directions
    "i": "input", "in": "input", "input": "input", "di": "input",
    "o": "output", "out": "output", "output": "output", "do": "output",
    "i/o": "bidir", "io": "bidir", "bidir": "bidir", "inout": "bidir",
    # open-drain / open-collector
    "od": "open_drain", "oc": "open_drain",
    "open-drain": "open_drain", "open_drain": "open_drain",
    "open-collector": "open_drain",
    # analogue / reference
    "analog": "analog", "analogue": "analog",
    "ref": "reference", "reference": "reference",
    # no-connect
    "nc": "no_connect", "n/c": "no_connect", "no connect": "no_connect",
}


def normalise_pin_type(raw: str) -> str:
    """Map a raw type string from the datasheet to a canonical type."""
    s = raw.strip().lower()
    if s in PIN_TYPE_MAP:
        return PIN_TYPE_MAP[s]
    # Partial matches
    for key, val in PIN_TYPE_MAP.items():
        if key in s:
            return val
    return "unknown"


# ---------------------------------------------------------------------------
# PDF pin-table extraction
# ---------------------------------------------------------------------------
def extract_pin_table(pdf_path: Path) -> list[dict]:
    """
    Extract a pin table from a datasheet PDF using PyMuPDF heuristics.

    Returns a list of dicts:
        [{"pin": "1", "name": "VCC", "type": "power"}, ...]

    Returns an empty list if extraction fails or the table is not found.
    The caller should emit a 'question' issue when the list is empty.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("  [warn] PyMuPDF (fitz) not installed — skipping PDF parse")
        return []

    # Patterns
    # A "pin row" typically looks like:  1   VCC   Power  or  A1  CLK  I
    PIN_ROW_RE = re.compile(
        r'^\s*(\w[\w/]*)\s+'          # pin number / alphanumeric
        r'([A-Za-z_][\w#/+\-\(\)]*)\s+'  # pin name
        r'([A-Za-z][A-Za-z/\- _]*)'   # type / I/O column
        r'\s*',
        re.IGNORECASE,
    )
    # Simpler two-column pattern: "1  VCC"  (type inferred from name)
    TWO_COL_RE = re.compile(
        r'^\s*(\d+[A-Z]?|[A-Z]\d+)\s+([A-Za-z_][\w#/+\-]*)\s*$',
        re.IGNORECASE,
    )

    results: list[dict] = []
    seen_pins: set[str] = set()

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        print(f"  [warn] fitz.open({pdf_path.name}): {exc}")
        return []

    # Score each page for "pin table" likelihood
    best_pages: list[tuple[int, int]] = []  # (score, page_index)
    for page_idx in range(len(doc)):
        text = doc[page_idx].get_text("text")
        score = 0
        ltext = text.lower()
        for kw in ("pin no", "pin number", "pin name", "pin description",
                   "signal name", "i/o", "function", "description"):
            if kw in ltext:
                score += 1
        if score >= 2:
            best_pages.append((score, page_idx))

    # Fall back to scanning all pages if no strong candidates
    if not best_pages:
        best_pages = [(0, i) for i in range(len(doc))]

    best_pages.sort(key=lambda x: -x[0])

    for _, page_idx in best_pages[:10]:  # limit search to top-10 pages
        text = doc[page_idx].get_text("text")
        lines = text.splitlines()
        page_hits = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            m = PIN_ROW_RE.match(line)
            if m:
                pin_num = m.group(1).strip()
                pin_name = m.group(2).strip()
                raw_type = m.group(3).strip()

                # Reject obvious header rows
                if pin_num.lower() in ("pin", "no", "num", "number", "#", "ball"):
                    continue

                pin_type = normalise_pin_type(raw_type)

                # If type is unknown, try to infer from pin name
                if pin_type == "unknown":
                    pin_type = _infer_type_from_name(pin_name)

                if pin_num not in seen_pins:
                    seen_pins.add(pin_num)
                    results.append({"pin": pin_num, "name": pin_name, "type": pin_type})
                    page_hits += 1
                continue

            # Try simpler two-column
            m2 = TWO_COL_RE.match(line)
            if m2:
                pin_num = m2.group(1).strip()
                pin_name = m2.group(2).strip()
                if pin_num.lower() in ("pin", "no", "num", "number", "#"):
                    continue
                pin_type = _infer_type_from_name(pin_name)
                if pin_num not in seen_pins:
                    seen_pins.add(pin_num)
                    results.append({"pin": pin_num, "name": pin_name, "type": pin_type})
                    page_hits += 1

        if page_hits > 5:
            # Found a real pin table on this page — no need to continue
            break

    doc.close()
    return results


def filename_matches_mpn(pdf_path: Path, mpn: str) -> bool:
    """Return True if the PDF filename likely corresponds to *mpn*.

    Normalises both strings (lowercase, strip separators) then checks whether
    any progressively shorter prefix of the normalised MPN appears in the
    normalised filename stem.  Shortest prefix tried is half the MPN length or
    4 characters, whichever is greater, to avoid false positives from very
    short tokens.
    """
    if not mpn:
        return False
    norm_mpn = re.sub(r"[-_/ ]", "", mpn).lower()
    norm_name = re.sub(r"[-_/ ]", "", pdf_path.stem).lower()
    min_len = max(4, len(norm_mpn) // 2)
    for length in range(len(norm_mpn), min_len - 1, -1):
        if norm_mpn[:length] in norm_name:
            return True
    return False


def best_pin_table(pdf_paths: list[Path], mpn: str = "") -> tuple[list[dict], Path | None]:
    """
    Return (pin_table, source_pdf) for the best PDF candidate.

    Selection strategy:
      1. Primary  — prefer PDFs whose filename matches the component's MPN.
      2. Tiebreaker — among matched PDFs (or all PDFs when none match) pick the
                      one that yields the most extracted pins.

    Logs which PDF was selected and whether it won via MPN match or the
    most-pins fallback.
    """
    per_pdf: list[tuple[Path, list[dict], bool]] = []  # (path, pins, mpn_match)
    for pdf in pdf_paths:
        pins = extract_pin_table(pdf)
        matched = filename_matches_mpn(pdf, mpn)
        per_pdf.append((pdf, pins, matched))
        tag = "MPN match" if matched else "no MPN match"
        print(f"    [pdf] {pdf.name}: {len(pins)} pins, {tag}")

    # Candidates that matched the MPN, or all if none matched
    mpn_matches = [(p, pins) for p, pins, m in per_pdf if m]
    candidates = mpn_matches if mpn_matches else [(p, pins) for p, pins, _ in per_pdf]

    best_path: Path | None = None
    best_pins: list[dict] = []
    for path, pins in candidates:
        if len(pins) > len(best_pins):
            best_pins = pins
            best_path = path

    if best_path is not None:
        reason = "matched mfg_part_number" if mpn_matches else "fallback: most pins"
        print(f"    [pdf] selected: {best_path.name} ({reason})")

    return best_pins, best_path


def _infer_type_from_name(name: str) -> str:
    """Guess pin type from the pin name when no explicit type column is present."""
    n = name.lower()
    if any(k in n for k in ("vcc", "vdd", "vbat", "avcc", "avdd", "dvcc",
                             "dvdd", "iovcc", "iovdd", "vsupply")):
        return "power"
    if any(k in n for k in ("gnd", "vss", "agnd", "dgnd", "pgnd", "ep")):
        return "ground"
    if n in ("nc", "n/c"):
        return "no_connect"
    return "unknown"


# ---------------------------------------------------------------------------
# IC deduplication helpers
# ---------------------------------------------------------------------------
def get_ic_part_id(comp: dict) -> str | None:
    """
    Return the Highstage part ID to use for downloading datasheets.

    Preference: highstage_id → part_number (if it starts with 'IC').
    Returns None if no usable ID is found.
    """
    hid = (comp.get("highstage_id") or "").strip()
    if hid:
        return hid
    pn = (comp.get("part_number") or "").strip()
    if pn.upper().startswith("IC"):
        return pn
    return None


def deduplicate_ics(components: dict) -> dict[str, tuple[str, dict]]:
    """
    Return a mapping from part_id → (first_ref, comp) for unique ICs.

    ICs without a usable part ID are returned with part_id = "".

    Second-source handling notes
    ----------------------------
    Each Highstage ID maps to one subfolder under datasheets/<ID>/. When a
    component has a second-source manufacturer, that second source has its own
    distinct Highstage ID and therefore its own datasheets/<ID2>/ subfolder —
    so the caching structure handles second sources naturally.

    However, schematic.yaml currently stores only one `highstage_id` per
    component (the primary source). If a component also carries a
    `second_source_id` field, process_ic will download and try that folder too
    (see process_ic below). When neither field exists for a second-source part,
    the secondary datasheet is silently skipped.

    # TODO: extend schematic.yaml / the BOM parser to populate `second_source_id`
    #       (or a `second_source_ids` list) so that alternative manufacturers are
    #       checked automatically without manual intervention.
    """
    seen: dict[str, tuple[str, dict]] = {}  # part_id → (ref, comp)
    no_id: dict[str, tuple[str, dict]] = {}  # ref → ("", comp)

    for ref, comp in sorted(components.items()):
        if comp.get("comp_type") != "ic":
            continue
        if comp.get("dnp", False):
            continue
        part_id = get_ic_part_id(comp)
        if part_id:
            if part_id not in seen:
                seen[part_id] = (ref, comp)
        else:
            no_id[ref] = ("", comp)

    # Merge no-ID ICs at the end (each ref is its own "unique" entry)
    result: dict[str, tuple[str, dict]] = dict(seen)
    for ref, (_, comp) in no_id.items():
        result[f"__noid__{ref}"] = (ref, comp)
    return result


# ---------------------------------------------------------------------------
# datasheet.json helpers
# ---------------------------------------------------------------------------

def load_datasheet_json(part_id: str, datasheet_dir: Path) -> dict | None:
    """Load datasheets/<part_id>/datasheet.json and return the parsed dict, or None."""
    if not part_id or part_id.startswith("__noid__"):
        return None
    path = datasheet_dir / part_id / "datasheet.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"  [warn] could not read {path}: {exc}")
        return None


def pin_table_from_datasheet_json(ds: dict) -> list[dict]:
    """Convert the pins section of datasheet.json into the same list[dict] format
    as extract_pin_table(): [{"pin": "1", "name": "VCC", "type": "power"}, ...]
    """
    pins_section = ds.get("pins") or {}
    result = []
    for pin_num, pdata in pins_section.items():
        # datasheet.json stores direction; map to canonical pin type
        raw_type = pdata.get("direction") or pdata.get("type") or "unknown"
        result.append({
            "pin": str(pin_num),
            "name": pdata.get("name", ""),
            "type": normalise_pin_type(raw_type),
        })
    return result


def _component_ref_matches_type(ref: str, component_desc: str) -> bool:
    """Return True if the component ref prefix matches the expected type in the description."""
    desc_lower = component_desc.lower()
    r = ref.upper()
    if any(k in desc_lower for k in ("cap", "capacitor", "bypass", "decoupling", "bootstrap")):
        return r.startswith("C")
    if any(k in desc_lower for k in ("resistor", "pull-up", "pull up", "pullup",
                                      "pull-down", "divider", "r_")):
        return r.startswith("R")
    if any(k in desc_lower for k in ("inductor", "ferrite", "choke")):
        return r.startswith("L")
    if any(k in desc_lower for k in ("diode", "zener", "tvs", "schottky")):
        return r.startswith("D")
    return False


def check_required_external_components(
    ref: str,
    comp: dict,
    ds: dict,
    nets: dict,
) -> list[dict]:
    """Check required_external_components from datasheet.json against schematic nets.

    For each entry with required=True, find the net connected to that pin in the
    schematic and verify that at least one connected component matches the expected
    component type. Flags missing required components as major issues.
    """
    issues: list[dict] = []
    req_comps = ds.get("required_external_components") or []
    if not req_comps:
        return issues

    schematic_ref = comp.get("schematic_ref", {})
    base_entry = {
        "ref": ref,
        "func_des": schematic_ref.get("func_des", ""),
        "sheet": schematic_ref.get("sheet", "sch_1"),
    }

    # Build a lookup from pin name → net name for this component instance
    pin_name_to_net: dict[str, str] = {}
    pin_num_to_name: dict[str, str] = {}

    # Use datasheet pin names when available, supplemented by schematic PINUSE
    ds_pins = ds.get("pins") or {}
    for pnum, pdata in ds_pins.items():
        name = (pdata.get("name") or "").strip()
        if name:
            pin_num_to_name[str(pnum)] = name

    for pin_num, pin_data in (comp.get("pins") or {}).items():
        net = pin_data.get("net") or ""
        # Use datasheet name if available, else PINUSE name
        ds_name = pin_num_to_name.get(str(pin_num))
        if ds_name:
            pin_name_to_net[ds_name.upper()] = net
        pinuse_name = (pin_data.get("name") or "").strip()
        if pinuse_name:
            pin_name_to_net[pinuse_name.upper()] = net
        # Also store by pin number for fallback
        pin_name_to_net[str(pin_num)] = net

    for entry in req_comps:
        if not entry.get("required", False):
            continue

        pin_field = (entry.get("pin") or "").strip()
        component_desc = (entry.get("component") or "").strip()
        constraint = (entry.get("constraint") or entry.get("note") or "").strip()

        # Find the net for this pin (try multiple name variants)
        net_name: str | None = None
        for candidate in [pin_field.upper(), pin_field, pin_field.split("/")[0].upper()]:
            if candidate in pin_name_to_net:
                net_name = pin_name_to_net[candidate]
                break

        if net_name is None:
            # Cannot locate the pin in this instance — skip
            continue

        if not net_name:
            # Pin is unconnected in the schematic — the missing component is on a floating pin
            issues.append({
                "severity": "major",
                "type": "ic_missing_required_component",
                "description": (
                    f"{ref} {pin_field} pin: no net connected — required component missing "
                    f"({component_desc}). {constraint}"
                ).rstrip(". "),
                "components": [{**base_entry, "net": ""}],
            })
            continue

        net_info = nets.get(net_name, {})
        connections = net_info.get("connections", []) or []

        # Check if any connected component matches the expected type
        connected_refs = [c.get("ref", "") for c in connections if c.get("ref", "") != ref]
        found = any(_component_ref_matches_type(r, component_desc) for r in connected_refs)

        if not found:
            desc = (
                f"{ref} {pin_field} pin: missing required external component "
                f"({component_desc}) on net {net_name!r}"
            )
            if constraint:
                desc += f". {constraint}"
            issues.append({
                "severity": "major",
                "type": "ic_missing_required_component",
                "description": desc,
                "components": [{**base_entry, "net": net_name}],
            })

    return issues


# ---------------------------------------------------------------------------
# Cross-reference logic
# ---------------------------------------------------------------------------
def cross_reference_ic(
    ref: str,
    comp: dict,
    pin_table: list[dict],
    nets: dict,
) -> list[dict]:
    """
    Compare schematic pins against the pin table.

    Returns a list of issue dicts.
    """
    issues: list[dict] = []
    pin_map = {row["pin"]: row for row in pin_table}

    schematic_ref = comp.get("schematic_ref", {})
    base_entry = {
        "ref": ref,
        "func_des": schematic_ref.get("func_des", ""),
        "sheet": schematic_ref.get("sheet", "sch_1"),
    }

    for pin_num, pin_data in comp.get("pins", {}).items():
        net_name = pin_data.get("net", "") or ""
        net_info = nets.get(net_name, {})
        net_type = net_info.get("type", "signal")
        net_voltage = net_info.get("voltage")

        comp_entry = {**base_entry, "net": net_name}

        ds_pin = pin_map.get(str(pin_num))
        if ds_pin is None:
            # Try without leading zeros etc.
            for k, v in pin_map.items():
                if k.lstrip("0") == str(pin_num).lstrip("0"):
                    ds_pin = v
                    break

        if ds_pin is None:
            continue  # Pin not in extracted table — skip silently

        ds_type = ds_pin.get("type", "unknown")
        ds_name = ds_pin.get("name", "?")

        if ds_type == "power":
            # VCC pin must connect to a power net
            if net_type not in ("power",):
                issues.append({
                    "severity": "major",
                    "type": "ic_vcc_not_on_power",
                    "description": (
                        f"{ref} pin {pin_num} ({ds_name}): connected to net "
                        f"{net_name!r} (type={net_type}) — expected power net"
                    ),
                    "components": [comp_entry],
                })

        elif ds_type == "ground":
            # GND pin must be on GND net (voltage == 0 or type == ground)
            is_gnd = net_type == "ground" or (
                net_voltage is not None and float(net_voltage) == 0.0
            )
            if not is_gnd:
                issues.append({
                    "severity": "major",
                    "type": "ic_gnd_not_on_ground",
                    "description": (
                        f"{ref} pin {pin_num} ({ds_name}): connected to net "
                        f"{net_name!r} (type={net_type}) — expected ground net"
                    ),
                    "components": [comp_entry],
                })

        elif ds_type == "input":
            # Input pins should not be floating (net has no voltage and no connections)
            connections = net_info.get("connections", [])
            if not net_name or (
                net_voltage is None and len(connections) <= 1 and net_type == "signal"
            ):
                issues.append({
                    "severity": "minor",
                    "type": "ic_input_floating",
                    "description": (
                        f"{ref} pin {pin_num} ({ds_name}): input pin connected to "
                        f"net {net_name!r} — may be floating (no voltage assigned)"
                    ),
                    "components": [comp_entry],
                })

        elif ds_type == "open_drain":
            # Open-drain pins should have a pull-up on the net
            connections = net_info.get("connections", [])
            has_pullup = any(
                c.get("ref", "").startswith("R") for c in connections
            )
            if not has_pullup:
                issues.append({
                    "severity": "minor",
                    "type": "ic_od_no_pullup",
                    "description": (
                        f"{ref} pin {pin_num} ({ds_name}): open-drain pin on net "
                        f"{net_name!r} — no pull-up resistor detected on net"
                    ),
                    "components": [comp_entry],
                })

    return issues


# ---------------------------------------------------------------------------
# Per-IC download + parse worker
# ---------------------------------------------------------------------------
def process_ic(
    part_id: str,
    ref: str,
    comp: dict,
    nets: dict,
    datasheet_dir: Path,
    file_share_ok: bool,
) -> tuple[list[dict], list[dict]]:
    """
    Resolve pin data and verify one unique IC, returning (issues, pin_table).

    Priority for pin data:
      1. datasheets/<part_id>/datasheet.json — AI-extracted, high-confidence
         Also checks required_external_components from datasheet.json.
      2. Heuristic PDF extraction from local PDFs (downloaded from file share)

    Returns pin_table so the caller can cross-reference additional schematic
    instances of the same part without re-downloading / re-parsing.

    Designed to run inside a ThreadPoolExecutor thread.
    """
    schematic_ref = comp.get("schematic_ref", {})
    base_entry = {
        "ref": ref,
        "func_des": schematic_ref.get("func_des", ""),
        "sheet": schematic_ref.get("sheet", "sch_1"),
        "net": "",
    }

    # --- ICs without any Highstage ID ---
    if not part_id or part_id.startswith("__noid__"):
        mpn = comp.get("mfg_part_number") or comp.get("value") or ref
        return (
            [{
                "severity": "question",
                "type": "ic_datasheet_unavailable",
                "description": (
                    f"{ref} ({mpn}): no Highstage ID — datasheet check skipped"
                ),
                "components": [base_entry],
            }],
            [],
        )

    # --- Prefer datasheet.json (AI-extracted, high-confidence) ---
    ds = load_datasheet_json(part_id, datasheet_dir)
    if ds:
        pin_table = pin_table_from_datasheet_json(ds)
        print(f"  [{ref}] {part_id}: {len(pin_table)} pins from datasheet.json (AI-extracted)")
        issues = cross_reference_ic(ref, comp, pin_table, nets)
        issues.extend(check_required_external_components(ref, comp, ds, nets))
        return issues, pin_table

    # --- Download primary datasheet (PDF) as fallback ---
    local_pdfs: list[Path] = []
    if file_share_ok:
        local_pdfs = download_datasheet(part_id, datasheet_dir / part_id)
    else:
        print(f"  [info] File share unavailable — skipping download for {part_id}")

    # --- Also download second-source datasheet(s) if recorded in schematic.yaml ---
    # Supports a single `second_source_id` string or a `second_source_ids` list.
    second_source_ids: list[str] = []
    ss_single = (comp.get("second_source_id") or "").strip()
    if ss_single:
        second_source_ids.append(ss_single)
    for ss in comp.get("second_source_ids") or []:
        ss = (ss or "").strip()
        if ss and ss not in second_source_ids:
            second_source_ids.append(ss)

    if file_share_ok:
        for ss_id in second_source_ids:
            ss_pdfs = download_datasheet(ss_id, datasheet_dir / ss_id)
            if ss_pdfs:
                print(f"  [{ref}] also checking second-source {ss_id}: {len(ss_pdfs)} PDF(s)")
                local_pdfs.extend(ss_pdfs)

    if not local_pdfs:
        mpn = comp.get("mfg_part_number") or comp.get("value") or ref
        return (
            [{
                "severity": "question",
                "type": "ic_datasheet_unavailable",
                "description": (
                    f"{ref} ({mpn}, {part_id}): no datasheet found on Highstage "
                    f"— pin check skipped"
                ),
                "components": [base_entry],
            }],
            [],
        )

    # --- Parse all PDFs — prefer MPN filename match, fall back to most pins ---
    mpn = comp.get("mfg_part_number") or comp.get("mpn") or ""
    if len(local_pdfs) > 1:
        print(f"  [{ref}] {part_id}: trying {len(local_pdfs)} PDF(s) to find best pin table (MPN: {mpn or 'n/a'})")
    pin_table, used_pdf = best_pin_table(local_pdfs, mpn=mpn)

    if not pin_table:
        mpn = comp.get("mfg_part_number") or comp.get("value") or ref
        pdf_names = ", ".join(p.name for p in local_pdfs)
        return (
            [{
                "severity": "question",
                "type": "ic_pin_table_not_extracted",
                "description": (
                    f"{ref} ({mpn}): could not extract pin table from datasheet "
                    f"({pdf_names}) — manual check required"
                ),
                "components": [base_entry],
            }],
            [],
        )

    print(
        f"  [{ref}] {part_id}: {len(pin_table)} pins extracted "
        f"from {used_pdf.name}"
    )

    # Cross-reference the representative ref
    issues = cross_reference_ic(ref, comp, pin_table, nets)
    return issues, pin_table


# ---------------------------------------------------------------------------
# Find all schematic refs that share a given part_id
# ---------------------------------------------------------------------------
def refs_for_part(part_id: str, components: dict) -> list[str]:
    """Return all refs whose Highstage ID matches *part_id*."""
    if part_id.startswith("__noid__"):
        return [part_id[len("__noid__"):]]
    refs = []
    for ref, comp in components.items():
        if comp.get("comp_type") != "ic":
            continue
        if comp.get("dnp", False):
            continue
        if get_ic_part_id(comp) == part_id:
            refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# Main verification function
# ---------------------------------------------------------------------------
def verify_ic_pins(schematic_path: Path) -> list[dict]:
    with open(schematic_path, encoding="utf-8") as f:
        sch = yaml.safe_load(f)

    components = sch.get("components", {})
    nets = sch.get("nets", {})

    # Shared datasheet cache at repo root: datasheets/<HIGHSTAGE_ID>/
    repo_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    datasheet_dir = repo_root / "datasheets"
    datasheet_dir.mkdir(parents=True, exist_ok=True)

    file_share_ok = is_file_share_available()
    if not file_share_ok:
        print("[warn] Highstage file share not accessible — downloads skipped")

    # Deduplicate ICs
    unique_ics = deduplicate_ics(components)
    print(
        f"IC verification: {len(unique_ics)} unique parts "
        f"({sum(1 for k in unique_ics if not k.startswith('__noid__'))} with IDs)"
    )

    all_issues: list[dict] = []

    # Download + parse in parallel
    with ThreadPoolExecutor(max_workers=6) as exe:
        futures = {}
        for part_id, (ref, comp) in unique_ics.items():
            fut = exe.submit(
                process_ic, part_id, ref, comp, nets, datasheet_dir, file_share_ok
            )
            futures[fut] = (part_id, ref, comp)

        for fut in as_completed(futures):
            part_id, ref, comp = futures[fut]
            try:
                issues, pin_table = fut.result()
            except Exception as exc:
                schematic_ref = comp.get("schematic_ref", {})
                issues = [{
                    "severity": "question",
                    "type": "ic_verification_error",
                    "description": f"{ref}: unexpected error during IC verification — {exc}",
                    "components": [{
                        "ref": ref,
                        "func_des": schematic_ref.get("func_des", ""),
                        "sheet": schematic_ref.get("sheet", "sch_1"),
                        "net": "",
                    }],
                }]
                pin_table = []

            all_issues.extend(issues)

            # Cross-reference all other schematic instances that share this part
            if pin_table and not part_id.startswith("__noid__"):
                for other_ref in refs_for_part(part_id, components):
                    if other_ref == ref:
                        continue  # already handled by process_ic
                    all_issues.extend(
                        cross_reference_ic(
                            other_ref, components[other_ref], pin_table, nets
                        )
                    )

    return all_issues


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Verify IC pin assignments against Highstage datasheets"
    )
    ap.add_argument("schematic_folder", help="Path to schematic review folder")
    ap.add_argument("--output", "-o", help="Override output YAML path")
    args = ap.parse_args()

    folder = Path(args.schematic_folder)
    yaml_path = folder / "REVIEW" / "schematic.yaml"

    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found")
        sys.exit(1)

    issues = verify_ic_pins(yaml_path)

    counts = Counter(i["severity"] for i in issues)
    print(f"\nIC pin issues: {len(issues)} total")
    for sev in ["critical", "major", "minor", "question", "info"]:
        if counts[sev]:
            print(f"  {sev}: {counts[sev]}")

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "source": "verify_ic_pins",
        # issues_ic.yaml is automatically picked up by build_issues.py via glob("issues_*.yaml")
        "issues": issues,
    }

    out_path = (
        Path(args.output) if args.output else folder / "REVIEW" / "issues_ic.yaml"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(out, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
