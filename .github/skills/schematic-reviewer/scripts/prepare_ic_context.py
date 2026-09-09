#!/usr/bin/env python3
"""
Generate per-component context JSONs for AI-assisted schematic review.

For each reviewable component in REVIEW/review.db, writes:
  REVIEW/ic_contexts/<ref>.json  — full pin/net context for one sub-agent
  REVIEW/ic_contexts/_index.json — list of all components (for orchestrating agent)

Reviewable component types: ic, oscillator, clock, diode, zener, led, inductor, transistor,
  mosfet, transistor_bjt, transistor_jfet, gan_fet

Usage:
    python prepare_ic_context.py reviews/SCH26913-1A
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Import shared helpers from verify_ic_pins.py (same directory)
# ---------------------------------------------------------------------------
# Component types that warrant a datasheet-driven agent review
_REVIEWABLE_TYPES = frozenset({
    'ic', 'oscillator', 'clock', 'diode', 'zener', 'led', 'inductor', 'transistor',
    'mosfet', 'transistor_bjt', 'transistor_jfet', 'gan_fet',
})

_HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(_HERE))
from verify_ic_pins import (  # noqa: E402
    filename_matches_mpn,
    get_ic_part_id,
)
from db_io import get_connection  # noqa: E402


# ---------------------------------------------------------------------------
# Repo root detection — robust across main worktree and git worktrees
# ---------------------------------------------------------------------------

def find_repo_root() -> Path:
    """Return the main git repo root.

    Uses ``git rev-parse --git-common-dir`` so that the result is the main
    worktree root even when the script runs from a linked worktree (e.g.
    worktrees/ic-review-agents/).  Falls back to ``parents[4]`` relative to
    this script file if git is not available.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=_HERE,
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = (_HERE / common_dir).resolve()
            # common_dir is the .git directory of the main worktree
            return common_dir.parent
    except Exception:
        pass
    # Fallback: script is at .github/skills/schematic-reviewer/scripts/
    return Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# Datasheet helpers
# ---------------------------------------------------------------------------

def find_datasheet_path(highstage_id: str, mpn: str, repo_root: Path) -> str | None:
    """Return path (relative to repo root, forward slashes) to the best matching
    datasheet PDF, or None if no datasheet folder or no PDFs are found."""
    if not highstage_id:
        return None
    ds_dir = repo_root / "datasheets" / highstage_id
    if not ds_dir.exists():
        return None
    pdfs = list(ds_dir.glob("*.pdf"))
    if not pdfs:
        return None

    # Prefer PDFs whose filename matches the MPN; fall back to largest file
    mpn_matches = [p for p in pdfs if filename_matches_mpn(p, mpn)]
    candidates = mpn_matches if mpn_matches else pdfs
    best = max(candidates, key=lambda p: p.stat().st_size, default=None)
    if best is None:
        return None
    return str(best.relative_to(repo_root)).replace("\\", "/")


# ---------------------------------------------------------------------------
# Pin table cache: datasheets/<HIGHSTAGE_ID>/datasheet.json (AI-extracted)
# ---------------------------------------------------------------------------

def find_datasheet_json_path(highstage_id: str, repo_root: Path) -> Path | None:
    """Return the Path to datasheet.json if it exists, else None."""
    if not highstage_id:
        return None
    p = repo_root / "datasheets" / highstage_id / "datasheet.json"
    return p if p.exists() else None


def load_or_extract_pins(
    highstage_id: str,
    mpn: str,
    repo_root: Path,
) -> tuple[dict[str, dict], str | None, str | None]:
    """Return (pin_map, source_label, extraction_method) with caching.

    pin_map maps pin number strings to {"name": ..., "type": ...}.

    Only uses datasheets/<id>/datasheet.json (AI-extracted, high-confidence).
    Heuristic extraction is intentionally removed — it produces garbage output.
    If no datasheet.json exists, the IC agent (Step 4c) will read the PDF and
    write datasheet.json as part of its review.

    Returns ({}, None, None) when no datasheet.json is available.
    """
    if not highstage_id:
        return {}, None, None

    ds_dir = repo_root / "datasheets" / highstage_id
    if not ds_dir.exists():
        return {}, None, None

    datasheet_json_path = ds_dir / "datasheet.json"
    if datasheet_json_path.exists():
        try:
            with open(datasheet_json_path, encoding="utf-8") as f:
                ds = json.load(f)
            pins_section = ds.get("pins") or {}
            pin_map: dict[str, dict] = {}
            for pnum, pdata in pins_section.items():
                pin_type = pdata.get("direction") or pdata.get("type") or "unknown"
                pin_map[str(pnum)] = {"name": pdata.get("name", ""), "type": pin_type}
            print(f"  [{highstage_id}] using datasheet.json ({len(pin_map)} pins, AI-extracted)")
            return pin_map, str(datasheet_json_path.relative_to(repo_root)).replace("\\", "/"), "ai_extracted"
        except Exception as exc:
            print(f"  [warn] could not read {datasheet_json_path}: {exc}")

    # No datasheet.json — IC agent will extract on first review
    pdfs = list(ds_dir.glob("*.pdf"))
    label = f"datasheets/{highstage_id}/{pdfs[0].name}" if pdfs else None
    print(f"  [{highstage_id}] no datasheet.json yet — IC agent will extract from PDF")
    return {}, label, None


# ---------------------------------------------------------------------------
# Schema migration helper
# ---------------------------------------------------------------------------

def _migrate_db(db_path: Path) -> None:
    """Add pin_name column to pins table if not already present."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(db_path))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(pins)").fetchall()}
        if "pin_name" not in cols:
            conn.execute("ALTER TABLE pins ADD COLUMN pin_name TEXT")
            conn.commit()
            print("  Migrated: added pin_name column to pins table")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

MAX_CONNECTIONS = 20

# Passive component types through which graph traversal is performed (one hop)
_PASSIVE_TYPES = frozenset({
    "resistor", "capacitor", "inductor", "ferrite", "ferrite_bead",
})

# Net types whose far-side connections are skipped during passive traversal
# (GND and power rails fan out to hundreds of components — not useful to include)
_SKIP_NET_TYPES = frozenset({"gnd", "power"})


def _one_hop_through_passive(
    passive_ref: str,
    arrived_from_net: str,
    nets: dict,
    components: dict,
    origin_ref: str,
    dnp_refs: set | None = None,
) -> list[dict]:
    """Follow a passive to its other pin(s) and return what's connected there.

    Skips power/GND nets on the far side (they fan out to the whole board).
    Skips DNP components on the far side (open-circuit — not populated).
    Returns entries tagged with ``via`` so the agent knows the topology.
    """
    passive_comp = components.get(passive_ref, {})
    passive_pins = passive_comp.get("pins", {})
    result: list[dict] = []

    for pin_data in passive_pins.values():
        other_net = pin_data.get("net", "")
        if not other_net or other_net == arrived_from_net:
            continue  # same net we came from — skip

        other_net_info = nets.get(other_net, {})
        if other_net_info.get("type") in _SKIP_NET_TYPES:
            # Power/GND: just report the net name, not the hundreds of caps on it
            result.append({
                "ref": None,
                "comp_type": "net",
                "value": other_net,
                "role": other_net_info.get("type"),
                "pin": None,
                "via": f"{passive_ref}({passive_comp.get('value', '')})",
                "via_net": other_net,
                "net_voltage": other_net_info.get("voltage"),
            })
            continue

        for conn in (other_net_info.get("connections") or []):
            conn_ref = conn.get("ref", "")
            if conn_ref in (passive_ref, origin_ref):
                continue
            if dnp_refs and conn_ref in dnp_refs:
                continue  # DNP component on far side — open-circuit, skip
            conn_comp = components.get(conn_ref, {})
            result.append({
                "ref": conn_ref,
                "comp_type": conn_comp.get("comp_type") or None,
                "value": conn_comp.get("value") or None,
                "role": conn_comp.get("role") or None,
                "pin": conn.get("pin") or None,
                "via": f"{passive_ref}({passive_comp.get('value', '')})",
                "via_net": other_net,
            })

    return result


def build_pin_context(
    ref: str,
    pin_num: str,
    pin_data: dict,
    nets: dict,
    components: dict,
    dnp_refs: set | None = None,
) -> dict:
    """Return the context dict for a single pin.

    Direct connections on the pin's net are listed first.  For each passive
    (R/C/L/ferrite) in the direct list, one-hop traversal follows through to
    the passive's other pin(s) so that compensation networks, RC filters, and
    series-resistor chains are fully visible to the reviewing agent.
    """
    net_name = pin_data.get("net", "") or ""
    net_info = nets.get(net_name, {}) if net_name else {}

    net_connections = net_info.get("connections", []) or []
    connected_to: list[dict] = []
    total_others = sum(1 for c in net_connections if c.get("ref") != ref)
    truncated_count = 0

    direct_passives: list[str] = []  # passive refs seen on direct net

    for conn in net_connections:
        conn_ref = conn.get("ref", "")
        if conn_ref == ref:
            continue
        if len(connected_to) >= MAX_CONNECTIONS:
            truncated_count = total_others - MAX_CONNECTIONS
            break
        conn_comp = components.get(conn_ref, {})
        conn_type = conn_comp.get("comp_type") or None
        entry: dict = {
            "ref": conn_ref,
            "comp_type": conn_type,
            "value": conn_comp.get("value") or None,
            "role": conn_comp.get("role") or None,
            "pin": conn.get("pin") or None,
        }
        if dnp_refs and conn_ref in dnp_refs:
            entry["dnp"] = True
        connected_to.append(entry)
        # Only traverse through populated passives — a DNP passive is open-circuit
        if conn_type in _PASSIVE_TYPES and not (dnp_refs and conn_ref in dnp_refs):
            direct_passives.append(conn_ref)

    if truncated_count > 0:
        connected_to.append({"note": f"truncated — {truncated_count} more connections not shown"})

    # One-hop passive traversal: follow each passive to its other side
    hop_seen: set[str] = set()  # avoid duplicates across multiple passives
    for passive_ref in direct_passives:
        hops = _one_hop_through_passive(
            passive_ref, net_name, nets, components, origin_ref=ref, dnp_refs=dnp_refs
        )
        for hop in hops:
            key = f"{hop.get('ref')}:{hop.get('via_net')}"
            if key not in hop_seen:
                hop_seen.add(key)
                connected_to.append(hop)

    return {
        "net": net_name,
        "net_voltage": net_info.get("voltage"),
        "net_type": net_info.get("type"),
        "net_confidence": net_info.get("confidence"),
        "connected_to": connected_to,
    }


def _load_db_into_memory(db_path: Path) -> tuple[str, dict, dict]:
    """Load components, nets, and schematic_id from review.db into memory dicts.

    Returns (schematic_id, components_dict, nets_dict) matching the YAML structure
    used by build_pin_context().
    """
    conn = get_connection(db_path)
    try:
        # meta
        row = conn.execute("SELECT value FROM meta WHERE key='schematic_id'").fetchone()
        schematic_id = row["value"] if row else db_path.parent.parent.name

        # components
        components: dict = {}
        for row in conn.execute(
            "SELECT ref, comp_type, value, package, mfg_part_number, highstage_id, "
            "role, dnp, verified, func_des, sheet FROM components WHERE dnp=0"
        ).fetchall():
            ref = row["ref"]
            components[ref] = {
                "comp_type":       row["comp_type"],
                "value":           row["value"],
                "package":         row["package"],
                "mfg_part_number": row["mfg_part_number"],
                "highstage_id":    row["highstage_id"],
                "role":            row["role"],
                "dnp":             bool(row["dnp"]),
                "verified":        bool(row["verified"]),
                "func_des":        row["func_des"],
                "sheet":           row["sheet"],
                "schematic_ref":   {
                    "func_des": row["func_des"],
                    "sheet":    row["sheet"],
                },
                "pins": {},
            }

        # track DNP refs separately so connected_to entries can be flagged
        dnp_refs: set[str] = set(
                row[0] for row in conn.execute("SELECT ref FROM components WHERE dnp=1").fetchall()
        )

        # pins
        for row in conn.execute("SELECT ref, pin, net, direction FROM pins").fetchall():
            ref = row["ref"]
            if ref in components:
                components[ref]["pins"][row["pin"]] = {
                    "net":       row["net"],
                    "direction": row["direction"],
                }

        # nets — build connections list from net_connections joined with components
        nets: dict = {}
        for row in conn.execute(
            "SELECT name, voltage, confidence, type FROM nets"
        ).fetchall():
            nets[row["name"]] = {
                "voltage":     row["voltage"],
                "confidence":  row["confidence"],
                "type":        row["type"],
                "connections": [],
            }

        for row in conn.execute(
            "SELECT nc.net, nc.ref, nc.pin, c.comp_type, c.value "
            "FROM net_connections nc JOIN components c ON nc.ref=c.ref"
        ).fetchall():
            net_name = row["net"]
            if net_name in nets:
                nets[net_name]["connections"].append({
                    "ref":       row["ref"],
                    "pin":       row["pin"],
                    "comp_type": row["comp_type"],
                    "value":     row["value"],
                })

    finally:
        conn.close()

    return schematic_id, components, nets, dnp_refs


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate IC context JSONs for review")
    ap.add_argument("schematic_folder", help="Path to schematic review folder (contains REVIEW/review.db)")
    args = ap.parse_args()

    folder = Path(args.schematic_folder).resolve()
    # Accept either the review folder or the REVIEW/ subdirectory
    if (folder / "REVIEW" / "review.db").exists():
        db_path = folder / "REVIEW" / "review.db"
        review_dir = folder / "REVIEW"
    elif folder.name == "review.db" and folder.exists():
        db_path = folder
        review_dir = folder.parent
    elif (folder / "review.db").exists():
        db_path = folder / "review.db"
        review_dir = folder
    else:
        db_path = folder / "REVIEW" / "review.db"
        review_dir = folder / "REVIEW"

    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {db_path} …")
    schematic_id, components, nets, dnp_refs = _load_db_into_memory(db_path)

    # Apply schema migration: add pin_name column if missing
    _migrate_db(db_path)

    repo_root = find_repo_root()

    out_dir = review_dir / "ic_contexts"
    out_dir.mkdir(parents=True, exist_ok=True)

    index: list[dict] = []
    n_with_datasheet = 0
    n_without_datasheet = 0

    ic_refs = sorted(
        ref for ref, comp in components.items()
        if comp.get("comp_type") in _REVIEWABLE_TYPES and not comp.get("dnp", False)
    )

    for ref in ic_refs:
        comp = components[ref]
        highstage_id = get_ic_part_id(comp) or ""
        mpn = comp.get("mfg_part_number") or ""
        value = comp.get("value") or ""
        package = comp.get("package") or ""
        schematic_ref = comp.get("schematic_ref") or {}

        datasheet_path = find_datasheet_path(highstage_id, mpn, repo_root)
        if datasheet_path:
            n_with_datasheet += 1
        else:
            n_without_datasheet += 1

        ds_json_path_obj = find_datasheet_json_path(highstage_id, repo_root)
        datasheet_json_path = (
            str(ds_json_path_obj.relative_to(repo_root)).replace("\\", "/")
            if ds_json_path_obj else None
        )

        ds_pin_map, cached_pins_source, extraction_method = load_or_extract_pins(
            highstage_id, mpn, repo_root
        )
        cached_pins_ref: str | None
        if datasheet_json_path and ds_pin_map:
            cached_pins_ref = datasheet_json_path
        elif ds_pin_map:
            cached_pins_ref = f"datasheets/{highstage_id}/pins.json"
        else:
            cached_pins_ref = None

        pins_out: dict[str, dict] = {}
        pin_name_updates: list[tuple[str, str, str]] = []  # (ref, pin_num, pin_name)
        for pin_num, pin_data in (comp.get("pins") or {}).items():
            pin_ctx = build_pin_context(ref, str(pin_num), pin_data, nets, components, dnp_refs)
            ds_pin = ds_pin_map.get(str(pin_num))
            if ds_pin:
                pin_name = ds_pin.get("name") or ""
                pin_ctx["name"] = pin_name
                pin_ctx["type"] = ds_pin.get("type")
                if extraction_method:
                    pin_ctx["pin_data_confidence"] = (
                        "high" if extraction_method == "ai_extracted" else "low"
                    )
                if pin_name:
                    pin_name_updates.append((ref, str(pin_num), pin_name))
            pins_out[str(pin_num)] = pin_ctx

        # Write pin names back to DB so update_pin() can match by name
        if pin_name_updates:
            import sqlite3 as _sqlite3
            _conn = _sqlite3.connect(str(db_path))
            try:
                for _ref, _pin, _name in pin_name_updates:
                    _conn.execute(
                        "UPDATE pins SET pin_name=? WHERE ref=? AND pin=?",
                        (_name, _ref, _pin),
                    )
                _conn.commit()
            finally:
                _conn.close()

        context_json_path = out_dir / f"{ref}.json"
        context = {
            "ref": ref,
            "value": value,
            "package": package,
            "mfg_part_number": mpn,
            "highstage_id": highstage_id,
            "datasheet_path": datasheet_path,
            "datasheet_json_path": datasheet_json_path,
            "cached_pins": cached_pins_ref,
            "pin_extraction_method": extraction_method,
            "schematic_id": schematic_id,
            "schematic_ref": schematic_ref,
            "pins": pins_out,
        }

        with open(context_json_path, "w", encoding="utf-8") as f:
            json.dump(context, f, indent=2, ensure_ascii=False)

        index.append({
            "ref": ref,
            "comp_type": comp.get("comp_type"),
            "highstage_id": highstage_id,
            "sheet": comp.get("sheet"),
            "verified": bool(comp.get("verified")),
            "value": value,
            "mpn": mpn,
            "datasheet_path": datasheet_path,
            "datasheet_json_path": datasheet_json_path,
            "context_json_path": str(context_json_path),
            "schematic_ref": schematic_ref,
        })

    index_path = out_dir / "_index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    n_total = len(ic_refs)
    print(f"Generated contexts for {n_total} ICs ({n_with_datasheet} with datasheets, {n_without_datasheet} without)")
    print(f"Output: {out_dir}")


if __name__ == "__main__":
    main()
