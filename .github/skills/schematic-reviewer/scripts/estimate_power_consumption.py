#!/usr/bin/env python3
"""
estimate_power_consumption.py — Best-effort per-component power estimation.

Reads component, pin, and net data from review.db and estimates power
dissipation for ICs, resistors, LEDs, and LDOs.  High-dissipation
components generate issues written back to the DB.

Usage:
    python estimate_power_consumption.py reviews/<SCH_ID>/REVIEW/review.db [--top N]

Public API:
    from estimate_power_consumption import estimate_power_consumption
    result = estimate_power_consumption(db_path)
    # Returns {"analysed": N, "estimated": N, "total_mw": float, "top": [...], "issues_written": N}
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

# Ensure Unicode output works on Windows consoles (cp1252 → utf-8)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Path setup: allow running from any cwd
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# Confidence ordering (mirrors collect_enrichments.py)
# ---------------------------------------------------------------------------

_CONFIDENCE_ORDER = [
    "unknown", "hint", "doubtful", "inferred", "deduced",
    "calculated", "confirmed", "certain",
]
_MIN_CONFIDENCE = "inferred"   # voltages below this confidence are ignored


def _conf_index(val: str | None) -> int:
    try:
        return _CONFIDENCE_ORDER.index(val or "unknown")
    except ValueError:
        return 0


def _voltage_ok(confidence: str | None) -> bool:
    """Return True if *confidence* meets the minimum threshold."""
    return _conf_index(confidence) >= _conf_index(_MIN_CONFIDENCE)


# ---------------------------------------------------------------------------
# _parse_r — copied from collect_enrichments.py to avoid circular imports
# ---------------------------------------------------------------------------

def _parse_r(value_str: str) -> float | None:
    """Parse a resistance string to ohms; return None if unparseable."""
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


# ---------------------------------------------------------------------------
# Component-type identification
# ---------------------------------------------------------------------------

_IC_TYPES = {
    "ic", "ldo", "voltage_regulator", "dc_dc_converter",
    "oscillator", "microcontroller", "fpga",
}

_LDO_TYPES = {"ldo", "voltage_regulator"}


def _is_ic(comp_type: str, ref: str) -> bool:
    return (comp_type or "").lower() in _IC_TYPES


def _is_ldo(comp_type: str) -> bool:
    return (comp_type or "").lower() in _LDO_TYPES


def _is_resistor(comp_type: str, ref: str) -> bool:
    ct = (comp_type or "").lower()
    return ct == "resistor" or (not ct and ref.upper().startswith("R"))


def _is_led(comp_type: str, ref: str) -> bool:
    ct = (comp_type or "").lower()
    return ct == "led" or ref.upper().startswith("LED")


# ---------------------------------------------------------------------------
# DB column helpers
# ---------------------------------------------------------------------------

def _has_column(conn, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _ensure_datasheet_fields_column(conn) -> None:
    """Add the datasheet_fields TEXT column to components if it's absent."""
    if not _has_column(conn, "components", "datasheet_fields"):
        with conn:
            conn.execute(
                "ALTER TABLE components ADD COLUMN datasheet_fields TEXT"
            )


def _get_dsf(comp_row) -> dict:
    """Parse the datasheet_fields JSON for a component row (dict or Row)."""
    raw = None
    if hasattr(comp_row, "keys"):
        keys = comp_row.keys() if callable(comp_row.keys) else list(comp_row)
        if "datasheet_fields" in (comp_row.keys() if callable(comp_row.keys) else keys):
            raw = comp_row["datasheet_fields"]
    elif isinstance(comp_row, dict):
        raw = comp_row.get("datasheet_fields")
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_nets(conn) -> dict[str, dict]:
    """Return {net_name: {voltage, confidence}} for all nets."""
    nets: dict[str, dict] = {}
    for row in conn.execute("SELECT name, voltage, confidence FROM nets"):
        nets[row["name"]] = {
            "voltage":    row["voltage"],
            "confidence": row["confidence"] or "unknown",
        }
    return nets


def _load_components(conn, has_dsf: bool) -> dict[str, dict]:
    """Return {ref: component_dict} for all non-DNP components."""
    cols = "ref, comp_type, value, dnp"
    if has_dsf:
        cols += ", datasheet_fields"
    comps: dict[str, dict] = {}
    for row in conn.execute(f"SELECT {cols} FROM components"):
        if row["dnp"]:
            continue
        comps[row["ref"]] = dict(row)
    return comps


def _load_pins(conn) -> dict[str, list[dict]]:
    """Return {ref: [{pin, net, direction}, ...]} for all pins."""
    pins: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT ref, pin, net, direction FROM pins"):
        pins.setdefault(row["ref"], []).append({
            "pin":       row["pin"],
            "net":       row["net"],
            "direction": row["direction"],
        })
    return pins


def _net_voltage(nets: dict, net_name: str | None) -> tuple[float | None, str]:
    """Return (voltage, confidence) for *net_name*; (None, 'unknown') if absent."""
    if not net_name or net_name not in nets:
        return None, "unknown"
    info = nets[net_name]
    return info["voltage"], info["confidence"] or "unknown"


# ---------------------------------------------------------------------------
# Per-component estimators
# ---------------------------------------------------------------------------

def _estimate_ic(
    ref: str,
    comp: dict,
    comp_type: str,
    pins: list[dict],
    nets: dict,
) -> tuple[float | None, str]:
    """
    Estimate IC power from datasheet_fields supply_current_max_ma / quiescent_current_ua
    and the supply rail voltage.  Returns (power_mw, detail_str) or (None, '').
    """
    dsf = _get_dsf(comp)

    # Determine current (mA)
    current_ma: float | None = None
    if dsf.get("supply_current_max_ma") is not None:
        try:
            current_ma = float(dsf["supply_current_max_ma"])
        except (TypeError, ValueError):
            pass
    if current_ma is None and dsf.get("quiescent_current_ua") is not None:
        try:
            current_ma = float(dsf["quiescent_current_ua"]) / 1000.0
        except (TypeError, ValueError):
            pass
    if current_ma is None:
        return None, ""

    # Find supply rail: first power_in pin with a known-voltage net
    supply_v: float | None = None
    supply_net: str = ""
    for p in pins:
        if (p.get("direction") or "").lower() != "power_in":
            continue
        v, conf = _net_voltage(nets, p.get("net"))
        if v is not None and _voltage_ok(conf):
            supply_v = v
            supply_net = p.get("net") or ""
            break

    if supply_v is None:
        return None, ""

    power_mw = supply_v * current_ma
    detail = f"{supply_net}={supply_v}V × {current_ma:.1f}mA"
    return power_mw, detail


def _estimate_ldo(
    ref: str,
    comp: dict,
    pins: list[dict],
    nets: dict,
) -> tuple[float | None, str]:
    """
    Estimate LDO dropout power: P = (V_in - V_out) × I_out_max.
    Returns (power_mw, detail_str) or (None, '').
    """
    dsf = _get_dsf(comp)

    # Output current
    i_out_a: float | None = None
    if dsf.get("output_current_max_a") is not None:
        try:
            i_out_a = float(dsf["output_current_max_a"])
        except (TypeError, ValueError):
            pass
    if i_out_a is None:
        return None, ""

    # V_in: power_in pin net
    v_in: float | None = None
    for p in pins:
        if (p.get("direction") or "").lower() == "power_in":
            v, conf = _net_voltage(nets, p.get("net"))
            if v is not None and _voltage_ok(conf):
                v_in = v
                break

    if v_in is None:
        return None, ""

    # V_out: prefer datasheet_fields.output_voltage_v, then power_out pin net
    v_out: float | None = None
    if dsf.get("output_voltage_v") is not None:
        try:
            v_out = float(dsf["output_voltage_v"])
        except (TypeError, ValueError):
            pass

    if v_out is None:
        for p in pins:
            if (p.get("direction") or "").lower() in ("power_out", "output"):
                v, conf = _net_voltage(nets, p.get("net"))
                if v is not None and _voltage_ok(conf):
                    v_out = v
                    break

    if v_out is None:
        return None, ""

    dropout_v = v_in - v_out
    if dropout_v < 0:
        return None, ""

    power_mw = dropout_v * i_out_a * 1000.0  # W → mW
    i_out_ma = i_out_a * 1000.0
    detail = f"dropout: {dropout_v:.2g}V × {i_out_ma:.0f}mA"
    return power_mw, detail


def _estimate_resistor(
    ref: str,
    comp: dict,
    pins: list[dict],
    nets: dict,
) -> tuple[float | None, str]:
    """
    Estimate resistor power: P = (V1 - V2)² / R.
    Both nets must have voltage confidence ≥ 'inferred'.
    """
    r_ohm = _parse_r(comp.get("value") or "")
    if r_ohm is None or r_ohm <= 0:
        return None, ""

    net_pins = [p for p in pins if p.get("net")]
    if len(net_pins) < 2:
        return None, ""

    voltages: list[tuple[float, str]] = []
    for p in net_pins[:2]:
        v, conf = _net_voltage(nets, p["net"])
        if v is None or not _voltage_ok(conf):
            return None, ""
        voltages.append((v, p["net"]))

    v1, net1 = voltages[0]
    v2, net2 = voltages[1]
    delta_v = abs(v1 - v2)
    power_mw = (delta_v ** 2 / r_ohm) * 1000.0  # W → mW
    v_high = max(v1, v2)
    detail = f"{v_high:.3g}V across {r_ohm:g}Ω"
    return power_mw, detail


def _estimate_led(
    ref: str,
    comp: dict,
    pins: list[dict],
    all_comps: dict,
    all_pins: dict,
    nets: dict,
) -> tuple[float | None, str]:
    """
    Estimate LED power using forward voltage and a series resistor.
    P_led ≈ V_fwd × I, where I is estimated from the series resistor.
    """
    dsf = _get_dsf(comp)
    v_fwd: float = 2.0
    if dsf.get("forward_voltage_v") is not None:
        try:
            v_fwd = float(dsf["forward_voltage_v"])
        except (TypeError, ValueError):
            pass

    # Find a net connected to this LED (anode or any pin)
    led_nets = {p["net"] for p in pins if p.get("net")}
    if not led_nets:
        return None, ""

    # Search for a series resistor: a resistor sharing one of the LED's nets
    for anode_net in led_nets:
        # Find resistors connected to this same net
        for r_ref, r_pins in all_pins.items():
            r_comp = all_comps.get(r_ref, {})
            if not _is_resistor(r_comp.get("comp_type") or "", r_ref):
                continue
            r_pin_nets = {p["net"] for p in r_pins if p.get("net")}
            if anode_net not in r_pin_nets:
                continue

            # This resistor is on the same net as the LED — use it
            r_ohm = _parse_r(r_comp.get("value") or "")
            if r_ohm is None or r_ohm <= 0:
                continue

            # Find the other net of the resistor (supply side)
            other_nets = r_pin_nets - {anode_net}
            if not other_nets:
                continue
            supply_net = next(iter(other_nets))
            v_supply, conf = _net_voltage(nets, supply_net)
            if v_supply is None or not _voltage_ok(conf):
                continue

            # Current through resistor: I = (V_supply - V_fwd) / R
            v_drive = v_supply - v_fwd
            if v_drive <= 0:
                continue
            i_a = v_drive / r_ohm
            power_mw = v_fwd * i_a * 1000.0
            detail = f"V_fwd={v_fwd}V, I≈{i_a*1000:.1f}mA via {r_ref}"
            return power_mw, detail

    return None, ""


# ---------------------------------------------------------------------------
# DB write-back
# ---------------------------------------------------------------------------

def _store_estimate(conn, ref: str, power_mw: float, has_dsf: bool) -> None:
    """Write estimated_power_mw into components.datasheet_fields (JSON merge)."""
    row = conn.execute(
        "SELECT datasheet_fields FROM components WHERE ref = ?", (ref,)
    ).fetchone()
    if row is None:
        return
    raw = row[0] if row[0] else None
    try:
        fields: dict = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        fields = {}
    fields["estimated_power_mw"] = round(power_mw, 1)
    with conn:
        conn.execute(
            "UPDATE components SET datasheet_fields = ? WHERE ref = ?",
            (json.dumps(fields), ref),
        )


# ---------------------------------------------------------------------------
# Issue writing
# ---------------------------------------------------------------------------

def _write_high_power_issue(conn, ref: str, comp_type: str, power_mw: float, db_path: Path) -> None:
    from add_issue import write_issues
    summary = (
        f"{ref} ({comp_type}): estimated power dissipation {power_mw:.0f}mW "
        f"— check thermal design"
    )
    description = (
        f"{ref} ({comp_type}) has an estimated power dissipation of {power_mw:.0f} mW "
        f"based on datasheet current and supply rail voltage. "
        f"At this level, thermal management (heat spreading, airflow, or derating) "
        f"should be verified."
    )
    write_issues(db_path, [{
        "severity":    "major",
        "type":        "power",
        "summary":     summary,
        "description": description,
        "resolution":  (
            "Verify thermal management. Check junction temperature at maximum load. "
            "Ensure package and PCB can dissipate this power within operating temperature range."
        ),
        "ref":    ref,
        "source": "estimate_power_consumption",
    }])


def _write_ldo_dropout_issue(conn, ref: str, comp_type: str, power_mw: float, db_path: Path) -> None:
    from add_issue import write_issues
    summary = (
        f"{ref} ({comp_type}): {power_mw:.0f}mW dropout loss — consider switching regulator"
    )
    description = (
        f"{ref} ({comp_type}) dissipates an estimated {power_mw:.0f} mW as dropout loss "
        f"(P = (V_in − V_out) × I_out_max). "
        f"This may cause excessive heating and reduce efficiency."
    )
    write_issues(db_path, [{
        "severity":    "minor",
        "type":        "power",
        "summary":     summary,
        "description": description,
        "resolution":  (
            "Verify thermal dissipation. If efficiency is critical, "
            "consider replacing the LDO with a synchronous buck regulator."
        ),
        "ref":    ref,
        "source": "estimate_power_consumption",
    }])


# ---------------------------------------------------------------------------
# Core estimation engine
# ---------------------------------------------------------------------------

EstimateRow = dict  # {ref, comp_type, power_mw, detail, is_ldo}


def _run_estimates(
    conn,
    comps: dict,
    all_pins: dict,
    nets: dict,
) -> tuple[list[EstimateRow], int]:
    """
    Estimate power for each component.

    Returns:
        (estimates, skipped_count)
        estimates  — list of {ref, comp_type, power_mw, detail, is_ldo}
        skipped    — number of components for which no estimate was possible
    """
    estimates: list[EstimateRow] = []
    skipped = 0

    for ref, comp in comps.items():
        comp_type = (comp.get("comp_type") or "").lower()
        pins = all_pins.get(ref, [])
        power_mw: float | None = None
        detail: str = ""
        is_ldo_comp = False

        if _is_led(comp_type, ref):
            power_mw, detail = _estimate_led(ref, comp, pins, comps, all_pins, nets)

        elif _is_ldo(comp_type):
            is_ldo_comp = True
            power_mw, detail = _estimate_ldo(ref, comp, pins, nets)

        elif _is_ic(comp_type, ref):
            power_mw, detail = _estimate_ic(ref, comp, comp_type, pins, nets)

        elif _is_resistor(comp_type, ref):
            power_mw, detail = _estimate_resistor(ref, comp, pins, nets)

        if power_mw is not None and power_mw >= 0:
            estimates.append({
                "ref":       ref,
                "comp_type": comp_type or ref[0].lower(),
                "power_mw":  power_mw,
                "detail":    detail,
                "is_ldo":    is_ldo_comp,
            })
        else:
            skipped += 1

    return estimates, skipped


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_power_consumption(
    db_path: str | Path,
    top_n: int = 10,
) -> dict[str, Any]:
    """
    Estimate per-component and total board power consumption from review.db.

    Args:
        db_path: Path to the review.db SQLite database.
        top_n:   Number of top consumers to include in the returned 'top' list.

    Returns:
        {
            "analysed":       int,   # components examined
            "estimated":      int,   # components with a power estimate
            "total_mw":       float, # sum of all estimates (mW)
            "top":            list,  # top_n [{ref, comp_type, power_mw, detail}]
            "issues_written": int,   # number of issues added to the DB
        }
    """
    db_path = Path(db_path)
    from db_io import get_connection
    from pipeline_status import mark_stage

    conn = get_connection(db_path)
    _ensure_datasheet_fields_column(conn)
    has_dsf = _has_column(conn, "components", "datasheet_fields")

    nets     = _load_nets(conn)
    comps    = _load_components(conn, has_dsf)
    all_pins = _load_pins(conn)

    estimates, skipped = _run_estimates(conn, comps, all_pins, nets)

    # Sort by descending power
    estimates.sort(key=lambda e: e["power_mw"], reverse=True)

    total_mw = sum(e["power_mw"] for e in estimates)

    # Write power estimates back to DB
    for est in estimates:
        _store_estimate(conn, est["ref"], est["power_mw"], has_dsf)

    # Write issues for concerning dissipation
    issues_written = 0
    for est in estimates:
        ref       = est["ref"]
        pwr       = est["power_mw"]
        ctype     = est["comp_type"]
        is_ldo    = est["is_ldo"]

        if pwr > 500.0:
            _write_high_power_issue(conn, ref, ctype, pwr, db_path)
            issues_written += 1

        if is_ldo and pwr > 250.0:
            _write_ldo_dropout_issue(conn, ref, ctype, pwr, db_path)
            issues_written += 1

    # Mark pipeline stage
    mark_stage(db_path, "power_estimate", "done")

    analysed = len(comps)
    top      = estimates[:top_n]

    return {
        "analysed":       analysed,
        "estimated":      len(estimates),
        "total_mw":       round(total_mw, 1),
        "top":            top,
        "issues_written": issues_written,
    }


# ---------------------------------------------------------------------------
# Formatted output
# ---------------------------------------------------------------------------

def _print_report(
    result: dict[str, Any],
    db_path: Path,
    top_n: int,
) -> None:
    from db_io import get_connection, get_meta
    conn    = get_connection(db_path)
    sch_id  = get_meta(conn, "sch_id") or db_path.parent.parent.name

    analysed  = result["analysed"]
    estimated = result["estimated"]
    total_mw  = result["total_mw"]
    top       = result["top"]
    unest     = analysed - estimated

    print(f"\nPower estimate: {sch_id}  ({analysed} components analysed)")
    print()

    if top:
        print("  Top consumers:")
        for est in top:
            ref    = est["ref"]
            ctype  = est["comp_type"]
            pwr    = est["power_mw"]
            detail = est["detail"]
            print(f"    {ref:<6}  ({ctype:<20})  {pwr:>8.1f} mW   ({detail})")
    else:
        print("  (no power estimates available)")

    print()
    total_w = total_mw / 1000.0
    print(f"  Total estimated:  {total_mw:>10,.1f} mW  ({total_w:.1f} W)")
    print(f"  Unestimated:      {unest:>6} components (no current data)")

    high_diss = [e for e in top if e["power_mw"] > 500.0]
    if high_diss:
        refs_str = ", ".join(e["ref"] for e in high_diss)
        print()
        print(f"  ⚠ High dissipation (>500mW in single component): {refs_str}")

    if result["issues_written"]:
        print()
        print(f"  {result['issues_written']} issue(s) written to DB.")

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate per-component board power consumption from review.db.",
    )
    parser.add_argument(
        "db_path",
        help="Path to review.db (e.g. reviews/SCH26782/REVIEW/review.db)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="Show top N power consumers (default: 10)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    result = estimate_power_consumption(db_path, top_n=args.top)
    _print_report(result, db_path, args.top)


if __name__ == "__main__":
    _cli()
