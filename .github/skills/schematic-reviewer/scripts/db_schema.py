"""
db_schema.py — Canonical schema and migration system for review.db.

Usage:
    python db_schema.py <db_path>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS nets (
    name                TEXT PRIMARY KEY,
    voltage             REAL,
    voltage_min         REAL,
    voltage_max         REAL,
    confidence          TEXT DEFAULT 'unknown',
    type                TEXT DEFAULT 'unknown',
    voltage_source_ref  TEXT,
    voltage_source_pin  TEXT,
    voltage_driver_type TEXT,
    propagation_hops    INTEGER
);

CREATE TABLE IF NOT EXISTS components (
    ref             TEXT PRIMARY KEY,
    comp_type       TEXT,
    value           TEXT,
    package         TEXT,
    mfg_part_number TEXT,
    part_number     TEXT,
    role            TEXT,
    dnp             INTEGER DEFAULT 0,
    verified        INTEGER DEFAULT 0,
    func_des        TEXT,
    sheet           TEXT,
    rated_voltage   REAL,
    tolerance       TEXT,
    power_rating    TEXT,
    dielectric      TEXT,
    temp_min_c      REAL,
    temp_max_c      REAL
);

CREATE TABLE IF NOT EXISTS pins (
    ref       TEXT NOT NULL,
    pin       TEXT NOT NULL,
    net       TEXT,
    direction TEXT,
    pin_name  TEXT,
    PRIMARY KEY (ref, pin),
    FOREIGN KEY (ref) REFERENCES components(ref),
    FOREIGN KEY (net) REFERENCES nets(name)
);

CREATE TABLE IF NOT EXISTS net_connections (
    net TEXT NOT NULL,
    ref TEXT NOT NULL,
    pin TEXT,
    FOREIGN KEY (net) REFERENCES nets(name),
    FOREIGN KEY (ref) REFERENCES components(ref)
);

CREATE TABLE IF NOT EXISTS net_drivers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    net        TEXT NOT NULL,
    ref        TEXT NOT NULL,
    pin        TEXT,
    drive_type TEXT,
    confidence TEXT
);

CREATE TABLE IF NOT EXISTS issues (
    id               TEXT PRIMARY KEY,
    severity         TEXT NOT NULL,
    type             TEXT,
    summary          TEXT NOT NULL,
    description      TEXT NOT NULL,
    resolution       TEXT,
    refs             TEXT,
    ref              TEXT,
    pin              TEXT,
    net              TEXT,
    sheet            TEXT,
    source           TEXT,
    also_reported_by TEXT,
    components       TEXT,
    created_at       TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    stage        TEXT PRIMARY KEY,
    status       TEXT DEFAULT 'pending',
    completed_at TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_deps (
    stage      TEXT,
    depends_on TEXT,
    PRIMARY KEY (stage, depends_on)
);
"""

# ---------------------------------------------------------------------------
# Default pipeline configuration
# ---------------------------------------------------------------------------

_DEFAULT_STAGES = [
    "build",
    "datasheets",
    "ic_review",
    "enrichment",
    "verification",
    "temp_check",
    "supply_check",
    "bom_check",
    "power_estimate",
    "report",
]

_DEFAULT_DEPS: list[tuple[str, str]] = [
    ("datasheets",        "build"),
    ("ic_review",         "datasheets"),
    ("enrichment",        "ic_review"),
    ("verification",      "enrichment"),
    ("temp_check",        "datasheets"),
    ("supply_check",      "build"),
    ("bom_check",         "build"),
    ("power_estimate",    "enrichment"),
    ("report",            "verification"),
    ("report",            "temp_check"),
    ("report",            "supply_check"),
    ("report",            "bom_check"),
    ("report",            "power_estimate"),
]

# ---------------------------------------------------------------------------
# Future migrations — add tuples of (target_version_int, sql_string) here
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str]] = [
    (1, "ALTER TABLE pins ADD COLUMN pin_name TEXT"),
    (2, "ALTER TABLE nets ADD COLUMN voltage_min REAL"),
    (3, "ALTER TABLE nets ADD COLUMN voltage_max REAL"),
    (4, "UPDATE nets SET type = 'unknown' WHERE type = 'signal' AND confidence IN ('unknown', 'hint')"),
    (5, "ALTER TABLE components ADD COLUMN part_number TEXT"),
]

_LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_db(db_path: Path) -> None:
    """Create (or open) the review.db at *db_path* and initialise the schema.

    Applies all CREATE TABLE IF NOT EXISTS statements, inserts the latest
    schema version into ``meta`` if absent, and populates the default
    pipeline stages and their dependency edges.

    WAL journal mode and foreign-key enforcement are enabled on every open.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        conn.executescript(_DDL)

        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(_LATEST_SCHEMA_VERSION),),
            )

            for stage in _DEFAULT_STAGES:
                conn.execute(
                    "INSERT OR IGNORE INTO pipeline_stages (stage) VALUES (?)",
                    (stage,),
                )

            for stage, dep in _DEFAULT_DEPS:
                conn.execute(
                    "INSERT OR IGNORE INTO pipeline_deps (stage, depends_on) VALUES (?, ?)",
                    (stage, dep),
                )
    finally:
        conn.close()


def migrate_db(db_path: Path) -> int:
    """Apply any pending schema migrations to the DB at *db_path*.

    Reads ``schema_version`` from ``meta``, runs each entry in
    :data:`MIGRATIONS` whose target version is greater than the current one,
    and updates ``schema_version`` after each step.

    Returns the number of migrations applied.
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0

        applied = 0
        for target_version, sql in MIGRATIONS:
            if target_version <= current:
                continue
            with conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(target_version),),
                )
            current = target_version
            applied += 1

        return applied
    finally:
        conn.close()


def get_schema_version(db_path: Path) -> int:
    """Return the current ``schema_version`` stored in the ``meta`` table.

    Returns 0 if the meta table or key does not exist.
    """
    db_path = Path(db_path)
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Create or migrate a schematic review SQLite database.",
    )
    parser.add_argument("db_path", help="Path to review.db")
    args = parser.parse_args()

    path = Path(args.db_path)
    create_db(path)
    migrate_db(path)
    version = get_schema_version(path)
    print(f"schema_version={version}")


if __name__ == "__main__":
    main()
