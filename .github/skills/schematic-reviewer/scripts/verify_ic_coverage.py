#!/usr/bin/env python3
"""
verify_ic_coverage.py -- Guard against silently-skipped IC/transistor/diode reviews.

Historical bug this catches: in the LUTC-REVA review, Q4/Q5 (T951835 dual
transistors) never had a datasheet fetched and were never pin-reviewed by an
IC-review agent. Per the ic-reviewer skill, a component with no datasheet
should still emit a single `ic_no_datasheet` question issue -- but because the
review step was skipped entirely (not just "no datasheet found"), no issue of
any kind was recorded for those refs, and a reversed transistor orientation
went undetected until Rev B.

This script closes that gap by checking, for every non-DNP component whose
`comp_type` is one the ic-reviewer skill is responsible for (ic, transistor,
diode, zener, led), that ONE of the following is true:

  1. `components.verified` = 1  (an IC-review agent examined it and confirmed
     no issues, or issues were logged and it was marked verified), OR
  2. There is at least one row in `issues` referencing this ref (via `ref`,
     `refs`, or `components`) -- i.e. *some* finding was recorded for it
     (including the `ic_no_datasheet` fallback question).

Any component satisfying neither condition is "silently skipped" -- the
review pipeline never reached a verdict on it, and no trace was left behind.
These are reported as `critical` issues (component_review_skipped) since they
represent an unknown gap in review coverage, not a graded electrical finding.

Usage:
    python verify_ic_coverage.py reviews/<SCH_ID>/REVIEW/review.db
    python verify_ic_coverage.py reviews/<SCH_ID>/REVIEW/review.db --write-issues
    python verify_ic_coverage.py reviews/<SCH_ID>/REVIEW/review.db --json

Exit codes:
    0  no unreviewed components found
    1  one or more components have zero review coverage
    2  DB file not found or unreadable
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Component types the ic-reviewer skill is responsible for reviewing.
_REVIEWABLE_TYPES = {"ic", "transistor", "diode", "zener", "led"}


# Issue types/sources that indicate an actual IC-review (pin-level) pass was
# performed on the component -- NOT generic administrative checks like BOM
# completeness, supply-chain/obsolescence, or capacitor/resistor sweeps, which
# reference a ref without ever inspecting its pinout or orientation.
_IC_REVIEW_SOURCES = {"ic_review"}
_IC_REVIEW_TYPE_PREFIXES = ("ic_",)


def _is_ic_review_issue(row) -> bool:
    source = (row["source"] or "").strip().lower()
    if source in _IC_REVIEW_SOURCES:
        return True
    itype = (row["type"] or "").strip().lower()
    return itype.startswith(_IC_REVIEW_TYPE_PREFIXES)


def _load_ref_sets_from_issues(conn) -> set[str]:
    """Return the set of refs covered by an actual IC-review issue.

    Only issues that originate from the IC-review pass (source='ic_review' or
    a type prefixed with 'ic_', e.g. 'ic_no_datasheet', 'ic_pin_floating')
    count as coverage. Administrative checks (BOM, supply-chain, capacitor/
    resistor sweeps) mentioning a ref do NOT count -- they don't imply anyone
    ever looked at the component's pinout.
    """
    mentioned: set[str] = set()
    rows = conn.execute("SELECT ref, refs, components, source, type FROM issues").fetchall()
    for row in rows:
        if not _is_ic_review_issue(row):
            continue
        if row["ref"]:
            mentioned.add(row["ref"])
        for col in ("refs", "components"):
            raw = row[col]
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(parsed, list):
                continue
            for item in parsed:
                if isinstance(item, str):
                    mentioned.add(item)
                elif isinstance(item, dict) and item.get("ref"):
                    mentioned.add(item["ref"])
    return mentioned


def _is_dnp(row) -> bool:
    val = row["dnp"]
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes")
    return bool(val)


def verify_ic_coverage(db_path: Path | str) -> dict:
    """Check every reviewable, non-DNP component for review coverage.

    Returns a dict with keys:
        - "unreviewed": list of {"ref", "comp_type", "part_number"} dicts
        - "checked": total reviewable non-DNP components examined
        - "sch_id": schematic id from meta, or ""
    """
    try:
        from db_io import get_connection
    except ImportError:
        _scripts = Path(__file__).parent
        sys.path.insert(0, str(_scripts))
        from db_io import get_connection

    conn = get_connection(db_path)

    mentioned_refs = _load_ref_sets_from_issues(conn)

    rows = conn.execute(
        "SELECT ref, comp_type, part_number, verified, dnp FROM components"
    ).fetchall()

    unreviewed = []
    checked = 0
    for row in rows:
        comp_type = (row["comp_type"] or "").strip().lower()
        if comp_type not in _REVIEWABLE_TYPES:
            continue
        if _is_dnp(row):
            continue
        checked += 1

        verified = bool(row["verified"])
        has_issue = row["ref"] in mentioned_refs

        if not verified and not has_issue:
            unreviewed.append({
                "ref": row["ref"],
                "comp_type": comp_type,
                "part_number": row["part_number"] or "",
            })

    sch_id_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'sch_id'"
    ).fetchone()
    sch_id = sch_id_row["value"] if sch_id_row else ""

    conn.close()

    return {
        "unreviewed": sorted(unreviewed, key=lambda d: d["ref"]),
        "checked": checked,
        "sch_id": sch_id,
    }


def _print_report(result: dict) -> None:
    sch_id = result["sch_id"] or "unknown"
    checked = result["checked"]
    unreviewed = result["unreviewed"]

    print(f"verify_ic_coverage: {sch_id}  ({checked} reviewable components checked)")

    if unreviewed:
        print(f"  UNREVIEWED ({len(unreviewed)}):")
        for u in unreviewed:
            pn = f" ({u['part_number']})" if u["part_number"] else ""
            print(f"    {u['ref']}  [{u['comp_type']}]{pn}  -- no verified flag and no issue on record")
    else:
        print("  OK: every reviewable non-DNP component has been verified or has a recorded issue")


def write_coverage_issues(db_path: Path | str, unreviewed: list[dict]) -> list[str]:
    """Write one `component_review_skipped` critical issue per unreviewed component."""
    try:
        from add_issue import write_issues
    except ImportError:
        _scripts = Path(__file__).parent
        sys.path.insert(0, str(_scripts))
        from add_issue import write_issues

    issues = []
    for u in unreviewed:
        pn = u["part_number"] or "unknown part number"
        issues.append({
            "severity": "critical",
            "type": "component_review_skipped",
            "summary": f"{u['ref']}: never reviewed by the IC-review pipeline",
            "description": (
                f"{u['ref']} ({u['comp_type']}, {pn}) has no `verified` flag set and no "
                f"issue of any kind recorded against it. This means the IC-review step was "
                f"never run for this component (e.g. datasheet lookup or agent review was "
                f"skipped) rather than run-and-passed. Re-run the IC/transistor/diode review "
                f"for this component before relying on this review's completeness."
            ),
            "ref": u["ref"],
            "resolution": (
                "Fetch the datasheet and run the ic-reviewer skill for this component, "
                "or confirm via prepare_ic_context.py + collect_enrichments.py that it has "
                "been examined and mark it verified."
            ),
        })

    if not issues:
        return []
    return write_issues(db_path, issues)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify every reviewable IC/transistor/diode/zener/led component "
                    "has been examined by the IC-review pipeline (verified flag or a "
                    "recorded issue), catching silently-skipped components."
    )
    parser.add_argument("db_path", help="Path to review.db")
    parser.add_argument(
        "--write-issues",
        action="store_true",
        help="Write a critical `component_review_skipped` issue for each unreviewed component",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output result as JSON instead of formatted text",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if db_path.is_dir():
        db_path = db_path / "REVIEW" / "review.db"
    if not db_path.exists():
        print(f"ERROR: DB file not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    try:
        result = verify_ic_coverage(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not read DB: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.write_issues and result["unreviewed"]:
        ids = write_coverage_issues(db_path, result["unreviewed"])
        print(f"Wrote {len(ids)} component_review_skipped issue(s) to {db_path}")

    if args.output_json:
        print(json.dumps(result, indent=2))
    else:
        _print_report(result)

    sys.exit(1 if result["unreviewed"] else 0)


if __name__ == "__main__":
    main()
