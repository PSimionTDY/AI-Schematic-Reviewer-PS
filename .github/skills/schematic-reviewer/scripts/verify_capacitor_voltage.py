#!/usr/bin/env python3
"""
Verify capacitor voltage ratings against net voltages from review.db.

Rules:
  - Rated voltage must be >= net voltage * 1.5 (50% derating)
  - If no rated voltage found: question issue
  - If net voltage unknown or confidence in {unknown, hint}: skip
  - DNP components are skipped

Usage:
    python verify_capacitor_voltage.py reviews/SCH25678-1E
    python verify_capacitor_voltage.py reviews/SCH25678-1E/REVIEW/review.db
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_io import get_connection, update_component
from add_issue import write_issues
from pipeline_status import mark_stage


def parse_cap_value(value_str: str) -> tuple[str | None, float | None]:
    """
    Parse capacitor value string. Returns (capacitance_str, rated_voltage_float_or_None).

    Handles: 100n, 10u/25V, 22u 16V, 4.7uF 50V, 100nF, 6V3, 220u/35V, 10u/6V3
    """
    if not value_str:
        return None, None

    s = value_str.strip()

    # European voltage notation: 6V3 -> 6.3, 16V -> 16
    def parse_voltage_token(tok: str) -> float | None:
        # Match patterns: 16V, 6V3, 50V, 6.3V
        m = re.match(r'^(\d+(?:\.\d+)?)V(\d+)?$', tok, re.IGNORECASE)
        if m:
            integer_part = m.group(1)
            frac_part = m.group(2)
            if frac_part:
                return float(f"{integer_part}.{frac_part}")
            return float(integer_part)
        return None

    # Split on whitespace and / to find tokens
    tokens = re.split(r'[\s/]+', s)

    cap_tok = None
    volt_tok = None

    for tok in tokens:
        v = parse_voltage_token(tok)
        if v is not None:
            volt_tok = v
        elif re.match(r'^\d', tok):
            cap_tok = tok

    return cap_tok, volt_tok


_LOW_CONFIDENCE = {'unknown', 'hint'}
_NON_POWER_TYPES = {
    'signal', 'differential', 'clock', 'uart', 'spi',
    'i2c', 'can', 'usb', 'ethernet', 'interrupt',
    'pwm', 'analog', 'reset',
}


def verify_caps(db_path: Path) -> list[dict]:
    """Run capacitor voltage verification against review.db. Returns list of issue dicts."""
    conn = get_connection(db_path)
    try:
        caps = conn.execute(
            "SELECT ref, value, rated_voltage, role, func_des, sheet "
            "FROM components WHERE comp_type='capacitor' AND dnp=0"
        ).fetchall()

        issues = []
        verified_refs: set[str] = []

        for cap in caps:
            ref = cap["ref"]
            value = cap["value"] or ""
            func_des = cap["func_des"] or ""
            sheet = cap["sheet"] or "sch_1"

            _, rated_v = parse_cap_value(value)
            if rated_v is None and cap["rated_voltage"] is not None:
                try:
                    rated_v = float(cap["rated_voltage"])
                except (TypeError, ValueError):
                    pass

            # Read pins and their net voltages
            pins = conn.execute(
                "SELECT p.pin, p.net, n.voltage, n.confidence, n.type "
                "FROM pins p LEFT JOIN nets n ON p.net=n.name "
                "WHERE p.ref=?", (ref,)
            ).fetchall()

            pin_voltages = []
            pin_net_types = []
            for pin in pins:
                net_name = pin["net"] or ""
                v = pin["voltage"]
                conf = pin["confidence"] or "unknown"
                ntype = pin["type"] or ""
                if ntype:
                    pin_net_types.append(ntype)
                if v is not None and conf not in _LOW_CONFIDENCE:
                    pin_voltages.append((v, conf, net_name))

            cap_role = cap["role"] or ""
            if cap_role == "ac_coupling":
                verified_refs.append(ref)
                continue

            if not pin_voltages:
                # Skip if any connected net is explicitly a non-power type
                if any(t in _NON_POWER_TYPES for t in pin_net_types):
                    verified_refs.append(ref)
                    continue
                issues.append({
                    "severity": "question",
                    "type": "cap_net_unknown",
                    "description": f"{ref} ({value}): Net voltages unknown — cannot verify voltage rating",
                    "ref": ref,
                    "sheet": sheet,
                    "source": "verify_capacitor_voltage",
                })
                verified_refs.append(ref)
                continue

            max_voltage, max_conf, max_net = max(pin_voltages, key=lambda x: x[0])

            if max_voltage == 0.0:
                verified_refs.append(ref)
                continue

            if rated_v is None:
                issues.append({
                    "severity": "question",
                    "type": "cap_rating_unknown",
                    "description": f"{ref}: No voltage rating found in value '{value}' (on {max_net} = {max_voltage}V)",
                    "ref": ref,
                    "net": max_net,
                    "sheet": sheet,
                    "source": "verify_capacitor_voltage",
                })
                verified_refs.append(ref)
                continue

            min_required = max_voltage * 1.5

            if rated_v < max_voltage:
                sev = "critical"
                desc = f"{ref} ({value}): rated {rated_v}V BELOW operating voltage {max_voltage}V on {max_net} — REPLACE IMMEDIATELY"
            elif rated_v < max_voltage * 1.25:
                sev = "major"
                desc = f"{ref} ({value}): rated {rated_v}V is below 1.25× {max_voltage}V = {max_voltage*1.25:.1f}V on {max_net}"
            elif rated_v < min_required:
                sev = "minor"
                desc = f"{ref} ({value}): rated {rated_v}V below recommended {min_required:.0f}V (1.5× {max_voltage}V) on {max_net}"
            else:
                verified_refs.append(ref)
                continue

            issues.append({
                "severity": sev,
                "type": "cap_voltage_low",
                "description": desc,
                "ref": ref,
                "net": max_net,
                "sheet": sheet,
                "source": "verify_capacitor_voltage",
            })
            verified_refs.append(ref)

    finally:
        conn.close()

    # Mark verified in DB
    conn = get_connection(db_path)
    try:
        for ref in verified_refs:
            update_component(conn, ref, verified=1)
    finally:
        conn.close()

    return issues


def main():
    ap = argparse.ArgumentParser(description="Verify capacitor voltage ratings")
    ap.add_argument("schematic_folder", help="Schematic workspace folder or path to review.db")
    args = ap.parse_args()

    p = Path(args.schematic_folder)
    if p.suffix == ".db" and p.exists():
        db_path = p
    elif (p / "REVIEW" / "review.db").exists():
        db_path = p / "REVIEW" / "review.db"
    elif (p / "review.db").exists():
        db_path = p / "review.db"
    else:
        db_path = p / "REVIEW" / "review.db"

    if not db_path.exists():
        print(f"Error: {db_path} not found")
        sys.exit(1)

    issues = verify_caps(db_path)

    from collections import Counter
    counts = Counter(i["severity"] for i in issues)
    print(f"Capacitor issues: {len(issues)} total")
    for sev in ["critical", "major", "minor", "question"]:
        if counts[sev]:
            print(f"  {sev}: {counts[sev]}")

    if issues:
        write_issues(db_path, issues)
        print(f"Written {len(issues)} issue(s) to {db_path}")

    mark_stage(db_path, "verification", "done")
    print("Pipeline stage 'verification' marked done")


if __name__ == "__main__":
    main()
