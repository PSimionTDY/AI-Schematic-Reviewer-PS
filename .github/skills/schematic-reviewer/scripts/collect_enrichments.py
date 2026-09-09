#!/usr/bin/env python3
"""
collect_enrichments.py — Post-IC-review enrichment collector (SQLite edition).

Responsibilities:
  1. Voltage propagation (iteration 2): propagate known net voltages through
     inductors, ferrite beads, and low-value resistors (≤ 10 Ω) reading from
     and writing to review.db.  Runs to convergence.
  2. Mark reviewed ICs as verified in review.db.

Enrichments written by IC agents are already in the DB (via add_enrichment.py),
so no YAML reading is needed.

Usage:
    python collect_enrichments.py reviews/<SCH_ID>/REVIEW/review.db [--refs U1,U2,U3]

Backward compat: if the argument is a directory, REVIEW/review.db is appended.
"""
import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Confidence ordering
# ---------------------------------------------------------------------------

CONFIDENCE_ORDER = [
    "unknown", "hint", "doubtful", "inferred", "deduced",
    "calculated", "confirmed", "certain",
]


def _conf_index(val: str) -> int:
    """Return sort index for a confidence string; unknown values map to 0."""
    try:
        return CONFIDENCE_ORDER.index(val)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Component-type helpers (unchanged from old file)
# ---------------------------------------------------------------------------

def _parse_r(value_str: str) -> float | None:
    """Parse resistance string to ohms, returning None if unparseable."""
    if not value_str:
        return None
    s = value_str.strip()
    m = re.match(r'^(\d+(?:\.\d+)?)[Kk](\d+)$', s)
    if m:
        return float(m.group(1)) * 1000 + float(m.group(2)) * 100
    m = re.match(r'^(\d+(?:\.\d+)?)[Rr](\d+)$', s)
    if m:
        return float(m.group(1)) + float(m.group(2)) * 0.1
    m = re.match(r'^(\d+(?:\.\d+)?)([KkMmRrΩ]?)$', s)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix == 'K':
        return val * 1000
    if suffix == 'M':
        return val * 1e6
    return val


_PASSIVE_TYPES = {'inductor', 'ferrite_bead', 'resistor'}
_REF_PREFIX_TYPE = {
    'L': 'inductor', 'FB': 'ferrite_bead', 'FL': 'ferrite_bead',
    'R': 'resistor',
}


def _comp_type(ref: str) -> str:
    for prefix in sorted(_REF_PREFIX_TYPE, key=len, reverse=True):
        if ref.upper().startswith(prefix):
            return _REF_PREFIX_TYPE[prefix]
    return 'other'


# ---------------------------------------------------------------------------
# Voltage propagation — reads/writes review.db
# ---------------------------------------------------------------------------

def propagate_voltages(conn) -> int:
    """
    Iteration 2 of voltage resolution: propagate known net voltages through
    inductors, ferrite beads, and low-value resistors (≤ 10 Ω).

    Reads nets, components, and pins from the SQLite connection *conn* and
    writes updated voltage/confidence/type back via db_io.update_net().
    Runs to convergence.  Returns the total number of nets updated.
    """
    from db_io import update_net  # local import keeps top-level dependency-free

    # Load nets: {name: {voltage, confidence, type}}
    nets: dict[str, dict] = {}
    for row in conn.execute("SELECT name, voltage, confidence, type FROM nets"):
        nets[row["name"]] = {
            "voltage":    row["voltage"],
            "confidence": row["confidence"] or "unknown",
            "type":       row["type"] or "signal",
        }

    # Load components relevant to propagation: {ref: {comp_type, value}}
    components: dict[str, dict] = {}
    for row in conn.execute("SELECT ref, comp_type, value FROM components"):
        ctype = row["comp_type"] or _comp_type(row["ref"])
        if ctype in _PASSIVE_TYPES:
            components[row["ref"]] = {"comp_type": ctype, "value": row["value"] or ""}

    # Load pins for those components: {ref: [net_name, ...]}
    comp_refs = list(components.keys())
    if not comp_refs:
        return 0

    placeholders = ",".join("?" * len(comp_refs))
    comp_pins: dict[str, list[str]] = {r: [] for r in comp_refs}
    for row in conn.execute(
        f"SELECT ref, net FROM pins WHERE ref IN ({placeholders})", comp_refs
    ):
        if row["net"]:
            comp_pins[row["ref"]].append(row["net"])

    total = 0

    while True:
        changed = 0
        for ref, comp in components.items():
            ctype = comp["comp_type"]

            if ctype == "resistor":
                r = _parse_r(comp["value"])
                if r is None or r > 10.0:
                    continue

            pin_nets = comp_pins[ref]
            if len(pin_nets) != 2:
                continue

            net_a, net_b = pin_nets[0], pin_nets[1]
            info_a = nets.get(net_a)
            info_b = nets.get(net_b)
            if info_a is None or info_b is None:
                continue

            v_a = info_a["voltage"]
            v_b = info_b["voltage"]

            if v_a is not None and v_b is None:
                update_net(conn, net_b, voltage=v_a, confidence="inferred", type="power")
                nets[net_b]["voltage"] = v_a
                nets[net_b]["confidence"] = "inferred"
                nets[net_b]["type"] = "power"
                changed += 1
            elif v_b is not None and v_a is None:
                update_net(conn, net_a, voltage=v_b, confidence="inferred", type="power")
                nets[net_a]["voltage"] = v_b
                nets[net_a]["confidence"] = "inferred"
                nets[net_a]["type"] = "power"
                changed += 1

        if not changed:
            break
        total += changed

    return total


# ---------------------------------------------------------------------------
# Mark reviewed ICs as verified
# ---------------------------------------------------------------------------

def mark_verified_refs(conn, refs: list[str]) -> list[str]:
    """Mark each ref in *refs* as verified=1 in the components table.

    Returns the list of refs that were actually updated (i.e. existed in the DB
    and were not already verified).
    """
    from db_io import update_component

    updated = []
    for ref in refs:
        row = conn.execute(
            "SELECT verified FROM components WHERE ref = ?", (ref,)
        ).fetchone()
        if row is not None and not row["verified"]:
            update_component(conn, ref, verified=1)
            updated.append(ref)
    return updated


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _resolve_db_path(arg: str) -> Path:
    """Accept either a review.db path or a workspace directory."""
    p = Path(arg)
    if p.is_dir():
        return p / "REVIEW" / "review.db"
    return p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Voltage propagation (iteration 2) + mark reviewed ICs verified."
    )
    parser.add_argument(
        "db",
        help="Path to review.db, or workspace directory (REVIEW/review.db appended).",
    )
    parser.add_argument(
        "--refs",
        default="",
        help="Comma-separated list of IC refs to mark as verified (e.g. U1,U2,U3).",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    # Add scripts dir to path so db_io is importable regardless of cwd
    scripts_dir = str(Path(__file__).parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from db_io import get_connection

    conn = get_connection(db_path)

    propagated = propagate_voltages(conn)

    verified_refs: list[str] = []
    if args.refs:
        refs = [r.strip() for r in args.refs.split(",") if r.strip()]
        verified_refs = mark_verified_refs(conn, refs)

    print(f"collect_enrichments: {db_path}")
    if propagated:
        print(f"  {propagated} additional nets resolved via voltage propagation (pass 2)")
    else:
        print("  Voltage propagation: no new nets resolved")
    if verified_refs:
        print(f"  {len(verified_refs)} ICs marked verified: {', '.join(verified_refs)}")


if __name__ == "__main__":
    main()

