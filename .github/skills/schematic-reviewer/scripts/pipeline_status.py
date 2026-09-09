"""
pipeline_status.py — Human-readable DAG status view for the review pipeline.

Usage:
    python pipeline_status.py reviews/<SCH_ID>/REVIEW/review.db
    python pipeline_status.py reviews/<SCH_ID>/REVIEW/review.db --json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_io import get_connection, get_meta

# ---------------------------------------------------------------------------
# Status icons
# ---------------------------------------------------------------------------

_ICONS = {
    "done":    "[✓]",
    "running": "[→]",
    "failed":  "[!]",
    "skipped": "[-]",
    "pending": "[ ]",
}


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

def _topo_sort(stages: list[str], deps: dict[str, list[str]]) -> list[str]:
    """Return *stages* in topological order (dependencies first).

    Uses Kahn's algorithm.  Any cycle is broken by preserving insertion order.
    """
    in_degree: dict[str, int] = {s: 0 for s in stages}
    for stage, upstreams in deps.items():
        for _ in upstreams:
            if stage in in_degree:
                in_degree[stage] = in_degree.get(stage, 0)

    # Recompute properly
    in_degree = {s: 0 for s in stages}
    for stage in stages:
        for upstream in deps.get(stage, []):
            if upstream in in_degree:
                in_degree[stage] += 1

    queue = [s for s in stages if in_degree[s] == 0]
    result: list[str] = []
    # Build reverse map: stage -> list of dependents
    dependents: dict[str, list[str]] = {s: [] for s in stages}
    for stage in stages:
        for upstream in deps.get(stage, []):
            if upstream in dependents:
                dependents[upstream].append(stage)

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dep in dependents.get(node, []):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Append any remaining (cycle members) in original order
    remaining = [s for s in stages if s not in result]
    result.extend(remaining)
    return result


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

def get_pipeline_status(db_path: str | Path) -> list[dict[str, Any]]:
    """Return pipeline stage info as a list of dicts, sorted in dependency order.

    Each dict contains:
        name        — stage name
        status      — pending / running / done / failed / skipped
        started_at  — ISO string or None
        completed_at— ISO string or None
        elapsed_s   — float seconds (done stages only, if both timestamps set)
        error       — error string or None
        blocked_by  — list of upstream stage names that are not yet done
    """
    conn = get_connection(db_path)
    try:
        # Read all stages
        rows = conn.execute(
            "SELECT stage, status, completed_at, error FROM pipeline_stages"
        ).fetchall()

        # Check if started_at column exists (schema may evolve)
        col_names = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(pipeline_stages)"
            ).fetchall()
        }
        has_started_at = "started_at" in col_names

        if has_started_at:
            rows = conn.execute(
                "SELECT stage, status, started_at, completed_at, error "
                "FROM pipeline_stages"
            ).fetchall()

        # Build status map
        status_map: dict[str, str] = {}
        raw: dict[str, dict] = {}
        for row in rows:
            d = dict(row)
            name = d["stage"]
            status_map[name] = d.get("status", "pending") or "pending"
            raw[name] = d

        # Read deps: stage -> [upstream, ...]
        dep_rows = conn.execute(
            "SELECT stage, depends_on FROM pipeline_deps"
        ).fetchall()
        deps: dict[str, list[str]] = {name: [] for name in raw}
        for dr in dep_rows:
            stage, upstream = dr["stage"], dr["depends_on"]
            if stage in deps:
                deps[stage].append(upstream)

        stage_names = list(raw.keys())
        ordered = _topo_sort(stage_names, deps)

        result = []
        for name in ordered:
            d = raw[name]
            status = status_map[name]

            started_at = d.get("started_at") if has_started_at else None
            completed_at = d.get("completed_at")

            elapsed_s: float | None = None
            if status == "done" and started_at and completed_at:
                try:
                    t0 = datetime.fromisoformat(started_at)
                    t1 = datetime.fromisoformat(completed_at)
                    elapsed_s = (t1 - t0).total_seconds()
                except ValueError:
                    pass

            # blocked_by = upstream stages that are not done
            blocked_by = [
                upstream for upstream in deps.get(name, [])
                if status_map.get(upstream, "pending") not in ("done", "skipped")
            ]

            result.append({
                "name":         name,
                "status":       status,
                "started_at":   started_at,
                "completed_at": completed_at,
                "elapsed_s":    elapsed_s,
                "error":        d.get("error"),
                "blocked_by":   blocked_by,
            })

        return result

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------

def _format_elapsed(elapsed_s: float | None) -> str:
    if elapsed_s is None:
        return ""
    s = int(elapsed_s)
    if s < 60:
        return f"({s}s)"
    m, sec = divmod(s, 60)
    return f"({m}m{sec:02d}s)"


def print_pipeline_status(db_path: str | Path) -> None:
    """Print a human-readable pipeline DAG status to stdout."""
    db_path = Path(db_path)
    conn = get_connection(db_path)
    try:
        sch_id = get_meta(conn, "schematic_id") or db_path.parent.parent.name
    finally:
        conn.close()

    stages = get_pipeline_status(db_path)

    print(f"Pipeline: {sch_id}")
    for s in stages:
        icon   = _ICONS.get(s["status"], "[ ]")
        name   = s["name"]
        status = s["status"]
        parts  = [f"  {icon} {name:<22} {status}"]

        elapsed = _format_elapsed(s["elapsed_s"])
        if elapsed:
            parts.append(f"  {elapsed}")

        if s["blocked_by"]:
            parts.append(f"  (blocked by: {', '.join(s['blocked_by'])})")

        if s["status"] == "failed" and s["error"]:
            err = s["error"]
            if len(err) > 60:
                err = err[:57] + "..."
            parts.append(f'  "{err}"')

        print("".join(parts))


# ---------------------------------------------------------------------------
# Helper functions used by other scripts
# ---------------------------------------------------------------------------

def mark_stage(
    db_path: str | Path,
    stage_name: str,
    status: str,
    error_msg: str | None = None,
) -> None:
    """Set a pipeline stage status.

    Args:
        db_path:    Path to the review SQLite database.
        stage_name: Name of the pipeline stage to update.
        status:     One of: running, done, failed, skipped.
        error_msg:  Optional error message (stored in the ``error`` column).
    """
    valid_statuses = {"running", "done", "failed", "skipped"}
    if status not in valid_statuses:
        raise ValueError(f"status must be one of {valid_statuses}, got {status!r}")

    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection(db_path)
    try:
        # Detect available columns
        col_names = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(pipeline_stages)"
            ).fetchall()
        }
        has_started_at = "started_at" in col_names

        if has_started_at and status == "running":
            with conn:
                conn.execute(
                    "UPDATE pipeline_stages "
                    "SET status = ?, started_at = ?, error = NULL "
                    "WHERE stage = ?",
                    (status, now, stage_name),
                )
        elif status in ("done", "failed", "skipped"):
            with conn:
                conn.execute(
                    "UPDATE pipeline_stages "
                    "SET status = ?, completed_at = ?, error = ? "
                    "WHERE stage = ?",
                    (status, now, error_msg, stage_name),
                )
        else:
            # running, no started_at column
            with conn:
                conn.execute(
                    "UPDATE pipeline_stages SET status = ?, error = NULL WHERE stage = ?",
                    (status, stage_name),
                )
    finally:
        conn.close()


def is_stage_done(db_path: str | Path, stage_name: str) -> bool:
    """Return True if the stage has status='done'."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM pipeline_stages WHERE stage = ?",
            (stage_name,),
        ).fetchone()
        return row is not None and row["status"] == "done"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Print pipeline stage status for a review.db"
    )
    parser.add_argument("db_path", help="Path to review.db")
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON array"
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Error: {db_path} not found", file=sys.stderr)
        sys.exit(1)

    if args.json:
        stages = get_pipeline_status(db_path)
        print(json.dumps(stages, indent=2, default=str))
    else:
        print_pipeline_status(db_path)


if __name__ == "__main__":
    main()
