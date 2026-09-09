"""
validate_issues.py — Pre-report validation gate for review.db.

Checks the ``issues`` table for incomplete or malformed rows and reports
problems before the final review report is generated.

Usage:
    python validate_issues.py reviews/<SCH_ID>/REVIEW/review.db
    python validate_issues.py reviews/<SCH_ID>/REVIEW/review.db --errors-only
    python validate_issues.py reviews/<SCH_ID>/REVIEW/review.db --json

Exit codes:
    0  no ERRORs (warnings are fine)
    1  one or more ERRORs found
    2  DB file not found or unreadable
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SEVERITIES = {"critical", "major", "minor", "question", "info"}


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------

def validate_issues(db_path: Path | str) -> dict:
    """Validate all rows in the ``issues`` table of *db_path*.

    Args:
        db_path: Path to a ``review.db`` SQLite file.

    Returns:
        A dict with keys:
        - ``"errors"``: list of ``{"id": str, "message": str}``
        - ``"warnings"``: list of ``{"id": str, "message": str}``
        - ``"checked"``: int — total number of issue rows examined
        - ``"sch_id"``: str — value of ``sch_id`` from ``meta``, or ``""``
    """
    import sqlite3

    db_path = Path(db_path)

    # Import here so callers can use this module without db_io on the path
    try:
        from db_io import get_connection
    except ImportError:
        _scripts = Path(__file__).parent
        sys.path.insert(0, str(_scripts))
        from db_io import get_connection

    conn = get_connection(db_path)

    errors: list[dict] = []
    warnings: list[dict] = []

    def _err(issue_id: str, msg: str) -> None:
        errors.append({"id": issue_id, "message": msg})

    def _warn(issue_id: str, msg: str) -> None:
        warnings.append({"id": issue_id, "message": msg})

    # ---- load reference sets ------------------------------------------------

    known_components = {
        row["ref"]
        for row in conn.execute("SELECT ref FROM components").fetchall()
    }
    known_nets = {
        row["name"]
        for row in conn.execute("SELECT name FROM nets").fetchall()
    }

    # ---- load all issues ----------------------------------------------------

    rows = conn.execute(
        "SELECT id, severity, summary, description, resolution, refs, components, ref, net "
        "FROM issues"
    ).fetchall()

    # ---- per-row checks -------------------------------------------------------

    summary_counts: Counter = Counter()

    for row in rows:
        issue_id = row["id"] or "<no-id>"
        summary = row["summary"]
        description = row["description"]
        severity = row["severity"]
        resolution = row["resolution"]
        refs_raw = row["refs"]
        components_raw = row["components"]
        ref_val = row["ref"]
        net_val = row["net"]

        # Missing summary
        if not summary:
            _err(issue_id, "missing summary")

        # Missing description
        if not description:
            _err(issue_id, "missing description")

        # Missing severity
        if severity is None:
            _err(issue_id, "missing severity")
        elif severity not in VALID_SEVERITIES:
            _err(issue_id, f'invalid severity: "{severity}"')

        # Missing resolution
        if not resolution:
            _warn(issue_id, "missing resolution")

        # Invalid refs JSON
        if refs_raw is not None:
            try:
                json.loads(refs_raw)
            except (json.JSONDecodeError, TypeError):
                _err(issue_id, "refs is not valid JSON")

        # Invalid components JSON
        if components_raw is not None:
            try:
                json.loads(components_raw)
            except (json.JSONDecodeError, TypeError):
                _err(issue_id, "components is not valid JSON")

        # Dangling ref
        if ref_val and ref_val not in known_components:
            _warn(issue_id, f'ref "{ref_val}" not found in components table')

        # Dangling net
        if net_val and net_val not in known_nets:
            _warn(issue_id, f'net "{net_val}" not found in nets table')

        # Collect summaries for duplicate check
        if summary:
            summary_counts[summary] += 1

    # ---- duplicate summary check (cross-row) --------------------------------

    duplicate_summaries = {s for s, n in summary_counts.items() if n > 1}
    if duplicate_summaries:
        for row in rows:
            if row["summary"] in duplicate_summaries:
                issue_id = row["id"] or "<no-id>"
                _warn(issue_id, f'duplicate summary: "{row["summary"]}"')

    # ---- meta ---------------------------------------------------------------

    sch_id_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'sch_id'"
    ).fetchone()
    sch_id = sch_id_row["value"] if sch_id_row else ""

    conn.close()

    return {
        "errors": errors,
        "warnings": warnings,
        "checked": len(rows),
        "sch_id": sch_id,
    }


# ---------------------------------------------------------------------------
# Formatted report
# ---------------------------------------------------------------------------

def _print_report(result: dict, errors_only: bool = False) -> None:
    sch_id = result["sch_id"] or Path(sys.argv[1]).stem if len(sys.argv) > 1 else "unknown"
    checked = result["checked"]
    errors = result["errors"]
    warnings = result["warnings"]

    print(f"validate_issues: {sch_id}  ({checked} issues checked)")

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    {e['id']}  {e['message']}")

    if warnings and not errors_only:
        print(f"  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    {w['id']}  {w['message']}")

    ok_count = checked - len({e["id"] for e in errors} | {w["id"] for w in warnings})
    print(f"  OK: {ok_count} issues passed all checks")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate issues table in review.db before report generation."
    )
    parser.add_argument("db_path", help="Path to review.db")
    parser.add_argument(
        "--errors-only",
        action="store_true",
        help="Suppress warnings; only show errors",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output result as JSON instead of formatted text",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"ERROR: DB file not found: {db_path}", file=sys.stderr)
        sys.exit(2)

    try:
        result = validate_issues(db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not read DB: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.output_json:
        print(json.dumps(result, indent=2))
    else:
        _print_report(result, errors_only=args.errors_only)

    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
