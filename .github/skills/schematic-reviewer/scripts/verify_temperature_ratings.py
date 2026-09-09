"""
verify_temperature_ratings.py — Check component temperature ratings against the board's
required operating temperature range.

Reads board_temp_min_c / board_temp_max_c from the meta table (defaulting to −40 / 85 if
absent), then checks every non-DNP component whose datasheet_fields JSON contains
temp_rating_min_c or temp_rating_max_c.  Issues are written to the issues table via
add_issue.write_issues.

Public API
----------
    from verify_temperature_ratings import verify_temperature_ratings
    result = verify_temperature_ratings(db_path)
    # Returns {"checked": N, "skipped_no_data": N, "violations": N, "issues_written": N}

CLI
---
    python verify_temperature_ratings.py reviews/<SCH_ID>/REVIEW/review.db
    python verify_temperature_ratings.py reviews/<SCH_ID>/REVIEW/review.db --board-min -40 --board-max 85
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from any cwd
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from db_io import get_connection, get_meta
from add_issue import write_issues
from pipeline_status import mark_stage

# ---------------------------------------------------------------------------
# Grade inference helpers
# ---------------------------------------------------------------------------

_DEFAULT_BOARD_MIN = -40.0
_DEFAULT_BOARD_MAX = 85.0


def _infer_board_grade(temp_min: float, temp_max: float) -> str:
    """Return a human-readable grade label for the board's required range."""
    if temp_min <= -55:
        return "military"
    if temp_min <= -40:
        return "industrial"
    if temp_min >= 0:
        return "commercial"
    return "extended"


def _infer_component_grade(temp_min: float | None, temp_max: float | None) -> str | None:
    """Infer a grade string from numeric temp limits when temp_grade is absent."""
    if temp_min is None or temp_max is None:
        return None
    if temp_min <= -55:
        return "military"
    if temp_min <= -40:
        return "industrial"
    if temp_min >= 0:
        return "commercial"
    return "extended"


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

def _check_component(
    ref: str,
    dsf: dict[str, Any],
    board_min: float,
    board_max: float,
) -> list[dict]:
    """Return a list of issue dicts for *ref* (may be empty)."""
    issues: list[dict] = []

    t_min = dsf.get("temp_rating_min_c")
    t_max = dsf.get("temp_rating_max_c")
    comp_grade = dsf.get("temp_grade") or _infer_component_grade(t_min, t_max)

    # --- Cold-end violation ------------------------------------------------
    if t_min is not None and t_min > board_min:
        gap = t_min - board_min  # positive = shortfall
        severity = "critical" if gap > 20 else "major"
        t_min_str = f"{t_min:g}"
        t_max_str = f"{t_max:g}" if t_max is not None else "?"
        board_grade_label = _infer_board_grade(board_min, board_max)
        issues.append({
            "severity": severity,
            "type": "temperature",
            "summary": (
                f"{ref}: temperature rating ({t_min_str}–{t_max_str}°C) insufficient "
                f"for {board_grade_label} board ({board_min:g}–{board_max:g}°C)"
            ),
            "description": (
                f"{ref} is rated down to {t_min_str}°C but the board requires operation "
                f"from {board_min:g}°C.  At temperatures below {t_min_str}°C, behaviour "
                f"is unspecified.  Cold-end shortfall: {gap:g}°C."
            ),
            "resolution": (
                f"Replace with a component rated to {board_min:g}°C or lower "
                f"({board_grade_label}-grade or better)."
            ),
            "ref": ref,
            "source": "verify_temperature_ratings",
            "refs": [{"type": "component", "ref": ref}],
        })

    # --- Hot-end violation -------------------------------------------------
    if t_max is not None and t_max < board_max:
        gap = board_max - t_max  # positive = shortfall
        severity = "critical" if gap > 20 else "major"
        t_min_str = f"{t_min:g}" if t_min is not None else "?"
        t_max_str = f"{t_max:g}"
        board_grade_label = _infer_board_grade(board_min, board_max)
        issues.append({
            "severity": severity,
            "type": "temperature",
            "summary": (
                f"{ref}: temperature rating ({t_min_str}–{t_max_str}°C) insufficient "
                f"for {board_grade_label} board ({board_min:g}–{board_max:g}°C)"
            ),
            "description": (
                f"{ref} is rated up to {t_max_str}°C but the board requires operation "
                f"up to {board_max:g}°C.  At temperatures above {t_max_str}°C, behaviour "
                f"is unspecified.  Hot-end shortfall: {gap:g}°C."
            ),
            "resolution": (
                f"Replace with a component rated to {board_max:g}°C or higher "
                f"({board_grade_label}-grade or better)."
            ),
            "ref": ref,
            "source": "verify_temperature_ratings",
            "refs": [{"type": "component", "ref": ref}],
        })

    # --- Tight margin (within spec but < 10°C headroom) --------------------
    if not issues:
        # Tight cold margin: component IS within spec (t_min <= board_min) but headroom < 10°C.
        # Only flag when there is positive margin (t_min < board_min); exact boundary (=0) is clean.
        if t_min is not None and t_min < board_min:
            headroom = board_min - t_min  # positive = how much margin below the board limit
            if headroom < 10:
                issues.append({
                    "severity": "minor",
                    "type": "temperature",
                    "summary": f"{ref}: tight cold margin ({headroom:g}°C headroom)",
                    "description": (
                        f"{ref} cold-end margin is only {headroom:g}°C "
                        f"(rated {t_min:g}°C, board min {board_min:g}°C).  "
                        "Component is within spec but has very little margin."
                    ),
                    "resolution": "Consider a component with a wider temperature range for reliability.",
                    "ref": ref,
                    "source": "verify_temperature_ratings",
                    "refs": [{"type": "component", "ref": ref}],
                })
        # Tight hot margin: component IS within spec (t_max >= board_max) but headroom < 10°C.
        if t_max is not None and t_max > board_max:
            headroom = t_max - board_max
            if headroom < 10:
                issues.append({
                    "severity": "minor",
                    "type": "temperature",
                    "summary": f"{ref}: tight hot margin ({headroom:g}°C headroom)",
                    "description": (
                        f"{ref} hot-end margin is only {headroom:g}°C "
                        f"(rated {t_max:g}°C, board max {board_max:g}°C).  "
                        "Component is within spec but has very little margin."
                    ),
                    "resolution": "Consider a component with a wider temperature range for reliability.",
                    "ref": ref,
                    "source": "verify_temperature_ratings",
                    "refs": [{"type": "component", "ref": ref}],
                })

    # --- Grade mismatch ----------------------------------------------------
    board_grade = _infer_board_grade(board_min, board_max)
    _grade_order = {"commercial": 0, "extended": 1, "industrial": 2, "automotive": 3, "military": 4}
    if (
        comp_grade
        and board_grade
        and comp_grade in _grade_order
        and board_grade in _grade_order
        and _grade_order[comp_grade] < _grade_order[board_grade]
    ):
        # Only add a grade-mismatch issue if we haven't already issued a numeric violation
        already_flagged_refs = {i["ref"] for i in issues}
        if ref not in already_flagged_refs:
            issues.append({
                "severity": "major",
                "type": "temperature",
                "summary": (
                    f"{ref}: {comp_grade} grade on {board_grade} board"
                ),
                "description": (
                    f"{ref} is marked as {comp_grade!r} grade, but the board requires "
                    f"{board_grade!r} operation ({board_min:g}°C to {board_max:g}°C).  "
                    "The component temperature grade is insufficient."
                ),
                "resolution": (
                    f"Replace with a {board_grade}-grade (or better) equivalent."
                ),
                "ref": ref,
                "source": "verify_temperature_ratings",
                "refs": [{"type": "component", "ref": ref}],
            })

    return issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_temperature_ratings(
    db_path: str | Path,
    board_min_override: float | None = None,
    board_max_override: float | None = None,
) -> dict[str, int]:
    """Check component temperature ratings against the board operating range.

    Args:
        db_path:            Path to review.db.
        board_min_override: Override board_temp_min_c from meta (CLI use).
        board_max_override: Override board_temp_max_c from meta (CLI use).

    Returns:
        Dict with keys: checked, skipped_no_data, violations, issues_written.
    """
    db_path = Path(db_path)
    conn = get_connection(db_path)

    # --- Detect whether datasheet_fields column exists ---------------------
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(components)").fetchall()}
    has_dsf = "datasheet_fields" in col_names

    # --- Board temperature range ------------------------------------------
    meta_min = get_meta(conn, "board_temp_min_c")
    meta_max = get_meta(conn, "board_temp_max_c")

    if board_min_override is not None:
        board_min = float(board_min_override)
    elif meta_min is not None:
        board_min = float(meta_min)
    else:
        board_min = _DEFAULT_BOARD_MIN
        print(f"  Notice: board_temp_min_c not set in meta — using default {_DEFAULT_BOARD_MIN:g}°C")

    if board_max_override is not None:
        board_max = float(board_max_override)
    elif meta_max is not None:
        board_max = float(meta_max)
    else:
        board_max = _DEFAULT_BOARD_MAX
        print(f"  Notice: board_temp_max_c not set in meta — using default {_DEFAULT_BOARD_MAX:g}°C")

    board_grade_label = _infer_board_grade(board_min, board_max)

    # --- Fetch SCH ID for display -----------------------------------------
    sch_id = get_meta(conn, "sch_id") or db_path.parent.parent.name

    print(f"\nTemperature check: {sch_id}  (board: {board_min:g}°C to {board_max:g}°C = {board_grade_label})")

    # --- Load non-DNP components ------------------------------------------
    if has_dsf:
        rows = conn.execute(
            "SELECT ref, datasheet_fields FROM components WHERE dnp = 0 OR dnp IS NULL"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref FROM components WHERE dnp = 0 OR dnp IS NULL"
        ).fetchall()

    conn.close()

    checked = 0
    skipped_no_data = 0
    all_issues: list[dict] = []

    for row in rows:
        ref = row["ref"]
        dsf: dict[str, Any] = {}

        if has_dsf and row["datasheet_fields"]:
            try:
                dsf = json.loads(row["datasheet_fields"])
            except (json.JSONDecodeError, TypeError):
                dsf = {}

        t_min = dsf.get("temp_rating_min_c")
        t_max = dsf.get("temp_rating_max_c")

        if t_min is None and t_max is None:
            skipped_no_data += 1
            continue

        checked += 1
        component_issues = _check_component(ref, dsf, board_min, board_max)
        all_issues.extend(component_issues)

    # --- Write issues -------------------------------------------------------
    violations = sum(
        1 for i in all_issues if i["severity"] in ("critical", "major")
    )

    issues_written = 0
    if all_issues:
        ids = write_issues(db_path, all_issues)
        issues_written = len(ids)

    # --- Summary ------------------------------------------------------------
    print(f"  {checked + skipped_no_data} components checked, {skipped_no_data} skipped (no temp data)")
    if violations == 0 and not any(i["severity"] == "minor" for i in all_issues):
        print("  No temperature violations found.")
    else:
        minor_count = sum(1 for i in all_issues if i["severity"] == "minor")
        print(f"  {violations} violation(s), {minor_count} minor notice(s):")
        for issue in all_issues:
            sev_label = issue["severity"].upper().ljust(8)
            ref = issue["ref"]
            summary = issue["summary"]
            print(f"    {sev_label}  {ref}  {summary}")

    return {
        "checked": checked,
        "skipped_no_data": skipped_no_data,
        "violations": violations,
        "issues_written": issues_written,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Check component temperature ratings against the board operating range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("db", help="Path to review.db")
    parser.add_argument(
        "--board-min", type=float, default=None,
        metavar="TEMP_C",
        help="Override board minimum temperature (°C).  Defaults to meta.board_temp_min_c or −40.",
    )
    parser.add_argument(
        "--board-max", type=float, default=None,
        metavar="TEMP_C",
        help="Override board maximum temperature (°C).  Defaults to meta.board_temp_max_c or 85.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    mark_stage(db_path, "temp_check", "running")
    try:
        result = verify_temperature_ratings(
            db_path,
            board_min_override=args.board_min,
            board_max_override=args.board_max,
        )
        mark_stage(db_path, "temp_check", "done")
        print(
            f"\nDone. checked={result['checked']}, skipped={result['skipped_no_data']}, "
            f"violations={result['violations']}, issues_written={result['issues_written']}"
        )
    except Exception as exc:
        mark_stage(db_path, "temp_check", "failed", error_msg=str(exc))
        raise


if __name__ == "__main__":
    _cli()
