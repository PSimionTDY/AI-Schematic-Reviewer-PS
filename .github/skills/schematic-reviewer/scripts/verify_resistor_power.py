#!/usr/bin/env python3
"""
Verify resistor power dissipation against package ratings.

Rules:
  - Shunt resistors only (pull_up, pull_down, power_filter, or one net GND + other > 0.5V):
      pull_up:  P = V_high² / R  (V_high = voltage of power net)
      pull_down: P = V_signal² / R  (V_signal = voltage of signal net)
  - Series resistors (role=series/termination/ac_coupling, or both nets are signal nets):
      Skip silently. Emit 'info' only for R < 5Ω (potential high-current path).
  - Flag if P > max_power * 0.5  (50% derating)
  - DNP components are skipped

Usage:
    python verify_resistor_power.py reviews/SCH25678-1E
    python verify_resistor_power.py reviews/SCH25678-1E/REVIEW/review.db
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_io import get_connection, update_component
from add_issue import write_issues
from pipeline_status import mark_stage

PACKAGE_POWER_RATING = {
    '0201': 0.050,
    '0402': 0.063,
    '0603': 0.100,
    '0805': 0.125,
    '1206': 0.250,
    '1210': 0.500,
    '2010': 0.750,
    '2512': 1.000,
    'R0201': 0.050,
    'R0402': 0.063,
    'R0603': 0.100,
    'R0805': 0.125,
    'R1206': 0.250,
}


def parse_resistance(value_str: str) -> float | None:
    """
    Parse resistance value to ohms.
    Handles: 10K, 4.7K, 100R, 0R, 1M, 33.2K, 4K7, 1R5, 47, 10k, 1m
    Returns ohms as float, or None if unparseable.
    Returns 0.0 for jumpers (0R, 0Ω).
    """
    if not value_str:
        return None
    s = value_str.strip()

    # European notation: 4K7 -> 4700, 1R5 -> 1.5, 2M2 -> 2200000
    m = re.match(r'^(\d+(?:\.\d+)?)[Kk](\d+)$', s)
    if m:
        return float(m.group(1)) * 1000 + float(m.group(2)) * 100
    m = re.match(r'^(\d+(?:\.\d+)?)[Rr](\d+)$', s)
    if m:
        return float(m.group(1)) + float(m.group(2)) * 0.1
    m = re.match(r'^(\d+(?:\.\d+)?)[Mm](\d+)$', s)
    if m:
        return float(m.group(1)) * 1e6 + float(m.group(2)) * 1e5

    # Standard: 10K, 4.7K, 100R, 0R, 1M, 47
    m = re.match(r'^(\d+(?:\.\d+)?)([KkMmRrΩ]?)$', s)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2).upper()
    if suffix in ('K',):
        return val * 1000
    elif suffix in ('M',):
        return val * 1e6
    else:
        return val  # R, Ω, or no suffix = ohms


def is_shunt_resistor(role: str, pin_voltages: list[float]) -> bool:
    """Return True if this resistor carries continuous DC current (shunt)."""
    if role in ('pull_up', 'pull_down', 'power_filter'):
        return True
    if role in ('series', 'series_termination', 'termination', 'ac_coupling'):
        return False
    # Unknown role: infer from net voltages
    if len(pin_voltages) == 2:
        has_gnd = 0.0 in pin_voltages
        has_power = any(v > 0.5 for v in pin_voltages)
        return has_gnd and has_power
    return False


def verify_resistors(db_path: Path) -> list[dict]:
    """Run resistor power verification against review.db. Returns list of issue dicts."""
    conn = get_connection(db_path)
    try:
        resistors = conn.execute(
            "SELECT ref, value, power_rating, role, package, func_des, sheet "
            "FROM components WHERE comp_type='resistor' AND dnp=0"
        ).fetchall()

        issues = []
        verified_refs: list[str] = []

        for res in resistors:
            ref = res["ref"]
            value = res["value"] or ""
            package = (res["package"] or "").strip()
            role = res["role"] or ""
            sheet = res["sheet"] or "sch_1"

            resistance = parse_resistance(value)
            if resistance is None:
                continue  # Unparseable — cannot assess

            if resistance == 0.0:
                verified_refs.append(ref)  # 0R jumper — trivially safe
                continue

            pkg_norm = re.sub(r'[^0-9]', '', package)
            package_key = pkg_norm if len(pkg_norm) == 4 else package
            max_power = PACKAGE_POWER_RATING.get(package_key) or PACKAGE_POWER_RATING.get(package)

            # Read pins and net voltages
            pins = conn.execute(
                "SELECT p.pin, p.net, n.voltage, n.confidence "
                "FROM pins p LEFT JOIN nets n ON p.net=n.name "
                "WHERE p.ref=?", (ref,)
            ).fetchall()

            if len(pins) != 2:
                continue  # Only handle 2-pin resistors

            pin_a, pin_b = pins[0], pins[1]
            net_a = pin_a["net"] or ""
            net_b = pin_b["net"] or ""
            v_a = pin_a["voltage"]
            v_b = pin_b["voltage"]

            pin_voltages = [v for v in [v_a, v_b] if v is not None]

            comp_entry = {
                "ref": ref,
                "func_des": res["func_des"] or "",
                "sheet": sheet,
                "net": net_a,
            }

            if not is_shunt_resistor(role, pin_voltages):
                if resistance < 5.0:
                    issues.append({
                        "severity": "info",
                        "type": "res_series_unverified",
                        "description": (
                            f"{ref} ({value}, {package}): series resistor — power dissipation "
                            f"depends on load current, verify if high-current path"
                        ),
                        "ref": ref,
                        "sheet": sheet,
                        "source": "verify_resistor_power",
                    })
                verified_refs.append(ref)
                continue

            # Confirmed shunt — calculate power
            power = None

            if role == "pull_up":
                if v_a is not None and v_b is not None:
                    power = (v_a - v_b) ** 2 / resistance
                elif v_a is not None and v_a > 0 and v_b is None:
                    power = v_a ** 2 / resistance
                elif v_b is not None and v_b > 0 and v_a is None:
                    power = v_b ** 2 / resistance

            elif role in ("pull_down", "power_filter"):
                if v_a is not None and v_b is not None:
                    power = (v_a - v_b) ** 2 / resistance
                elif v_a is not None and v_a > 0:
                    power = v_a ** 2 / resistance
                elif v_b is not None and v_b > 0:
                    power = v_b ** 2 / resistance

            else:
                # Unknown role confirmed as shunt (one net GND, other has voltage)
                if v_a is not None and v_b is not None:
                    power = (v_a - v_b) ** 2 / resistance

            if power is None:
                continue  # Voltages unknown — cannot assess

            verified_refs.append(ref)

            if max_power is None:
                if package and role in ("pull_up", "pull_down", "power_filter"):
                    issues.append({
                        "severity": "question",
                        "type": "res_package_unknown",
                        "description": f"{ref} ({value}, {package}): Unknown package power rating — estimated {power*1000:.1f}mW",
                        "ref": ref,
                        "sheet": sheet,
                        "source": "verify_resistor_power",
                    })
                continue

            derated = max_power * 0.5
            if power > max_power:
                sev = "major"
                desc = f"{ref} ({value}, {package}): {power*1000:.1f}mW EXCEEDS rated {max_power*1000:.0f}mW"
            elif power > derated:
                sev = "minor"
                desc = f"{ref} ({value}, {package}): {power*1000:.1f}mW exceeds 50% derating of {derated*1000:.0f}mW"
            else:
                continue  # OK

            issues.append({
                "severity": sev,
                "type": "res_overpower",
                "description": desc,
                "ref": ref,
                "sheet": sheet,
                "source": "verify_resistor_power",
            })

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
    ap = argparse.ArgumentParser(description="Verify resistor power ratings")
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

    issues = verify_resistors(db_path)

    from collections import Counter
    counts = Counter(i["severity"] for i in issues)
    print(f"Resistor issues: {len(issues)} total")
    for sev in ["critical", "major", "minor", "question", "info"]:
        if counts[sev]:
            print(f"  {sev}: {counts[sev]}")

    if issues:
        write_issues(db_path, issues)
        print(f"Written {len(issues)} issue(s) to {db_path}")

    mark_stage(db_path, "verification", "done")
    print("Pipeline stage 'verification' marked done")


if __name__ == "__main__":
    main()
