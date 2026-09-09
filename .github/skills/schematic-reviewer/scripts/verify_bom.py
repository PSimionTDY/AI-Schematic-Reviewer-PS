#!/usr/bin/env python3
"""
verify_bom.py — Check the BOM (components table) in review.db for completeness.

Checks performed for each non-DNP component:
  - Missing manufacturer part number  (minor)
  - Missing Highstage ID              (minor)
  - Missing value for passives        (minor)
  - Missing package/footprint         (minor)
  - Unknown component type            (info)
  - Test point with no connected net  (info)

Usage:
    python verify_bom.py reviews/<SCH_ID>/REVIEW/review.db
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Passive component prefixes (value check applies to these)
# ---------------------------------------------------------------------------

_PASSIVE_PREFIXES = ("R", "C", "L", "D")


def _is_passive(ref: str) -> bool:
    return ref.upper().startswith(_PASSIVE_PREFIXES)


def _empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def verify_bom(db_path: str | Path) -> dict:
    """Check the BOM for completeness issues and write them to review.db.

    Args:
        db_path: Path to the SQLite review database.

    Returns:
        Dict with keys: checked, dnp_skipped, issues_written, issues.
    """
    db_path = Path(db_path)
    scripts_dir = Path(__file__).parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from db_io import get_connection

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT ref, comp_type, value, package, mfg_part_number, highstage_id, dnp "
            "FROM components"
        ).fetchall()

        # Load all pins grouped by ref for the test-point check
        pin_rows = conn.execute(
            "SELECT ref, net FROM pins"
        ).fetchall()
    finally:
        conn.close()

    # Build {ref: [net, ...]} — only non-NULL nets count as connected
    pin_nets: dict[str, list[str]] = {}
    for pr in pin_rows:
        ref = pr["ref"]
        net = pr["net"]
        pin_nets.setdefault(ref, [])
        if net is not None:
            pin_nets[ref].append(net)

    issues: list[dict] = []
    dnp_skipped = 0
    checked = 0

    for row in rows:
        ref = row["ref"]
        comp_type = row["comp_type"] or ""
        value = row["value"]
        package = row["package"]
        mfg = row["mfg_part_number"]
        hid = row["highstage_id"]
        dnp = row["dnp"]

        if dnp:
            dnp_skipped += 1
            continue

        checked += 1
        value_str = f"{value}, {package}" if value and package else (value or package or "")
        display = f" ({value_str})" if value_str else ""

        # Missing manufacturer part number
        if _empty(mfg):
            issues.append({
                "severity": "minor",
                "type": "bom",
                "summary": f"{ref}: missing manufacturer part number",
                "description": (
                    f"Component {ref}{display} has no manufacturer part number in the schematic. "
                    "BOM generation will be incomplete."
                ),
                "resolution": "Add the manufacturer part number to the schematic component properties.",
                "ref": ref,
                "source": "verify_bom",
            })

        # Missing Highstage ID
        if _empty(hid):
            issues.append({
                "severity": "minor",
                "type": "bom",
                "summary": f"{ref}: missing Highstage ID",
                "description": (
                    f"Component {ref}{display} has no Highstage part ID in the schematic. "
                    "Datasheet lookup and procurement traceability will be unavailable."
                ),
                "resolution": "Add the Highstage part ID (e.g. IC1001234) to the schematic component properties.",
                "ref": ref,
                "source": "verify_bom",
            })

        # Missing value — passives only
        if _is_passive(ref) and _empty(value):
            issues.append({
                "severity": "minor",
                "type": "bom",
                "summary": f"{ref}: missing value",
                "description": (
                    f"Passive component {ref} has no value specified in the schematic. "
                    "The BOM will be incomplete and the design intent unclear."
                ),
                "resolution": "Add the component value (e.g. 10k, 100nF) to the schematic properties.",
                "ref": ref,
                "source": "verify_bom",
            })

        # Missing package / footprint
        if _empty(package):
            issues.append({
                "severity": "minor",
                "type": "bom",
                "summary": f"{ref}: missing package/footprint",
                "description": (
                    f"Component {ref}{display} has no package or footprint specified. "
                    "Layout and BOM generation require a footprint."
                ),
                "resolution": "Add the package/footprint property to the schematic component.",
                "ref": ref,
                "source": "verify_bom",
            })

        # Unknown component type
        if _empty(comp_type):
            issues.append({
                "severity": "info",
                "type": "bom",
                "summary": f"{ref}: component type unknown",
                "description": (
                    f"Component {ref}{display} has no comp_type set. "
                    "Automated checks that rely on component classification may skip this part."
                ),
                "resolution": "Set the component type property in the schematic.",
                "ref": ref,
                "source": "verify_bom",
            })

        # Test point with no connected net
        if comp_type.lower() == "test_point":
            nets_connected = [n for n in pin_nets.get(ref, []) if n]
            if not nets_connected:
                issues.append({
                    "severity": "info",
                    "type": "bom",
                    "summary": f"{ref}: test point has no connected net",
                    "description": (
                        f"Test point {ref} has no net connected to any of its pins. "
                        "It may be unintentionally floating or missing a net assignment."
                    ),
                    "resolution": "Connect the test point to the intended net, or mark it DNP if unused.",
                    "ref": ref,
                    "source": "verify_bom",
                })

    # Write issues
    ids: list[str] = []
    if issues:
        from add_issue import write_issues
        ids = write_issues(db_path, issues)

    # Mark pipeline stage
    try:
        from pipeline_status import mark_stage
        mark_stage(db_path, "bom_check", "done")
    except (ImportError, Exception):
        pass

    return {
        "checked": checked,
        "dnp_skipped": dnp_skipped,
        "issues_written": len(ids),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(result: dict) -> None:
    issues = result["issues"]
    checked = result["checked"]
    dnp_skipped = result["dnp_skipped"]
    issues_written = result["issues_written"]

    missing_mfg     = sum(1 for i in issues if "missing manufacturer part number" in i["summary"])
    missing_hid     = sum(1 for i in issues if "missing Highstage ID" in i["summary"])
    missing_value   = sum(1 for i in issues if "missing value" in i["summary"])
    missing_pkg     = sum(1 for i in issues if "missing package/footprint" in i["summary"])
    unknown_type    = sum(1 for i in issues if "component type unknown" in i["summary"])
    floating_tp     = sum(1 for i in issues if "test point has no connected net" in i["summary"])

    print(f"BOM check: {checked} components checked ({dnp_skipped} DNP skipped)")
    if missing_mfg:
        print(f"  {missing_mfg} missing mfg_part_number")
    if missing_hid:
        print(f"  {missing_hid} missing highstage_id")
    if missing_value:
        print(f"  {missing_value} missing value (passives)")
    if missing_pkg:
        print(f"  {missing_pkg} missing package/footprint")
    if unknown_type:
        print(f"  {unknown_type} unknown component type")
    if floating_tp:
        print(f"  {floating_tp} test points with no connected net")
    print(f"  {issues_written} issues written to review.db")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python verify_bom.py <db_path>", file=sys.stderr)
        sys.exit(1)

    result = verify_bom(sys.argv[1])
    _print_summary(result)
