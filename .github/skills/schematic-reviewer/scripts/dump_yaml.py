"""
dump_yaml.py — Generate human-readable YAML snapshots from review.db.

Writes two files next to the DB:
  schematic.yaml  — nets, components, pins, pipeline, meta
  issues.yaml     — all issues sorted by severity then id

Usage:
    python dump_yaml.py <db_path>
    python dump_yaml.py <db_path> --no-schematic
    python dump_yaml.py <db_path> --no-issues
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Severity sort order
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "question": 3, "info": 4}


def _severity_key(issue: dict) -> tuple:
    return (_SEVERITY_ORDER.get(issue.get("severity", ""), 99), issue.get("id", ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, dropping null values."""
    return {k: row[k] for k in row.keys() if row[k] is not None}


def _parse_json_field(value):
    """Parse a JSON string field to a Python list/dict, or return as-is."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
    return value


# ---------------------------------------------------------------------------
# schematic.yaml builder
# ---------------------------------------------------------------------------

def _build_schematic_data(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    # meta
    meta = {}
    for row in conn.execute("SELECT key, value FROM meta"):
        meta[row["key"]] = row["value"]

    # nets
    nets = {}
    for row in conn.execute("SELECT * FROM nets"):
        d = _row_to_dict(row)
        name = d.pop("name")
        nets[name] = d

    # pins indexed by ref
    pins_by_ref: dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM pins"):
        d = _row_to_dict(row)
        ref = d.pop("ref")
        pin_num = d.pop("pin")
        pins_by_ref.setdefault(ref, {})[pin_num] = d

    # components
    components = {}
    for row in conn.execute("SELECT * FROM components"):
        d = _row_to_dict(row)
        ref = d.pop("ref")

        # Normalise boolean-ish integers
        if "verified" in d:
            d["verified"] = bool(d["verified"])
        if "dnp" in d:
            if d["dnp"]:
                d["dnp"] = True
            else:
                del d["dnp"]  # omit dnp: false

        # Attach pins sub-dict
        if ref in pins_by_ref:
            d["pins"] = pins_by_ref[ref]

        components[ref] = d

    # pipeline
    stages = {}
    for row in conn.execute("SELECT * FROM pipeline_stages"):
        d = _row_to_dict(row)
        stage = d.pop("stage")
        stages[stage] = d

    pipeline = {"stages": stages} if stages else {}

    data: dict = {}
    if meta:
        data["meta"] = meta
    if nets:
        data["nets"] = nets
    if components:
        data["components"] = components
    if pipeline:
        data["pipeline"] = pipeline

    return data


# ---------------------------------------------------------------------------
# issues.yaml builder
# ---------------------------------------------------------------------------

_JSON_ISSUE_FIELDS = ("refs", "also_reported_by", "components")


def _build_issues_data(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    issues = []
    for row in conn.execute("SELECT * FROM issues"):
        d = _row_to_dict(row)
        for field in _JSON_ISSUE_FIELDS:
            if field in d:
                d[field] = _parse_json_field(d[field])
        issues.append(d)

    issues.sort(key=_severity_key)
    return {"issues": issues}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dump_yaml(
    db_path: Path | str,
    write_schematic: bool = True,
    write_issues: bool = True,
) -> dict[str, Path]:
    """Dump review.db to YAML snapshot files next to the database.

    Args:
        db_path: Path to the SQLite review.db file.
        write_schematic: Whether to write schematic.yaml (default True).
        write_issues: Whether to write issues.yaml (default True).

    Returns:
        Dict with keys ``"schematic_yaml"`` and/or ``"issues_yaml"`` mapping
        to the :class:`~pathlib.Path` of each written file.
    """
    db_path = Path(db_path)
    out_dir = db_path.parent

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        written: dict[str, Path] = {}

        if write_schematic:
            schematic_data = _build_schematic_data(conn)
            schematic_path = out_dir / "schematic.yaml"
            schematic_path.write_text(
                yaml.dump(schematic_data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False),
                encoding="utf-8",
            )
            written["schematic_yaml"] = schematic_path

        if write_issues:
            issues_data = _build_issues_data(conn)
            issues_path = out_dir / "issues.yaml"
            issues_path.write_text(
                yaml.dump(issues_data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False),
                encoding="utf-8",
            )
            written["issues_yaml"] = issues_path

    finally:
        conn.close()

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump review.db to YAML snapshot files."
    )
    parser.add_argument("db_path", help="Path to review.db")
    parser.add_argument(
        "--no-schematic", action="store_true", help="Skip writing schematic.yaml"
    )
    parser.add_argument(
        "--no-issues", action="store_true", help="Skip writing issues.yaml"
    )
    args = parser.parse_args()

    written = dump_yaml(
        args.db_path,
        write_schematic=not args.no_schematic,
        write_issues=not args.no_issues,
    )

    for path in written.values():
        print(path)


if __name__ == "__main__":
    _main()
