"""
db_io.py — Shared SQLite connection helpers for the schematic review tool.

All other scripts should obtain connections through :func:`get_connection`
so that WAL mode, foreign keys, and the row factory are always configured
consistently.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Column sets (used for field-filtering in update helpers)
# ---------------------------------------------------------------------------

_NETS_COLUMNS = {
    "name", "voltage", "voltage_min", "voltage_max", "confidence", "type",
    "voltage_source_ref", "voltage_source_pin",
    "voltage_driver_type", "propagation_hops",
}

_COMPONENTS_COLUMNS = {
    "ref", "comp_type", "value", "package",
    "mfg_part_number", "highstage_id", "role",
    "dnp", "verified", "func_des", "sheet",
    "rated_voltage", "tolerance", "power_rating",
    "dielectric", "temp_min_c", "temp_max_c",
}

_PINS_COLUMNS = {"ref", "pin", "net", "direction", "pin_name"}


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def get_connection(db_path: Path | str, timeout: int = 30) -> sqlite3.Connection:
    """Open and configure a SQLite connection to *db_path*.

    The connection is configured with:
    - ``isolation_level = None`` (autocommit) — callers use explicit
      ``with conn:`` transaction blocks.
    - ``PRAGMA journal_mode=WAL`` for concurrent read access.
    - ``PRAGMA foreign_keys=ON`` to enforce referential integrity.
    - ``row_factory = sqlite3.Row`` so rows are accessible by column name.

    Args:
        db_path: Path to the SQLite database file.
        timeout: Seconds to wait when the database is locked (default 30).

    Returns:
        A configured :class:`sqlite3.Connection`.
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Issue helpers
# ---------------------------------------------------------------------------

def _next_issue_id(conn: sqlite3.Connection) -> str:
    """Return the next available ISS_NNN identifier."""
    row = conn.execute("SELECT MAX(id) FROM issues").fetchone()
    current = row[0] if row and row[0] else None
    if current and current.startswith("ISS_"):
        try:
            n = int(current[4:])
            return f"ISS_{n + 1:03d}"
        except ValueError:
            pass
    return "ISS_001"


def _serialise(value: Any) -> Any:
    """Serialise lists/dicts to JSON strings; pass other types through."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def insert_issue(conn: sqlite3.Connection, issue: dict) -> str:
    """Insert a normalised issue dict into the ``issues`` table.

    - Assigns the next ``ISS_NNN`` id if ``issue`` does not already contain
      one.
    - Sets ``created_at`` to the current UTC ISO timestamp if absent.
    - Serialises ``refs``, ``also_reported_by``, and ``components`` to JSON
      strings when supplied as Python lists or dicts.

    Args:
        conn: An open database connection (from :func:`get_connection`).
        issue: Mapping of column names to values.

    Returns:
        The ``id`` assigned to the inserted row.
    """
    issue = dict(issue)  # work on a copy

    if not issue.get("id"):
        issue["id"] = _next_issue_id(conn)

    if not issue.get("created_at"):
        issue["created_at"] = datetime.now(timezone.utc).isoformat()

    for field in ("refs", "also_reported_by", "components"):
        if field in issue:
            issue[field] = _serialise(issue[field])

    columns = list(issue.keys())
    placeholders = ", ".join("?" * len(columns))
    col_clause = ", ".join(columns)
    values = [issue[c] for c in columns]

    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO issues ({col_clause}) VALUES ({placeholders})",
            values,
        )

    return issue["id"]


# ---------------------------------------------------------------------------
# Net / component / pin update helpers
# ---------------------------------------------------------------------------

def update_net(conn: sqlite3.Connection, name: str, **fields) -> bool:
    """Update the named net row with the supplied *fields*.

    Only fields whose key exists in the ``nets`` table schema are written;
    unknown keys are silently ignored.

    Args:
        conn: An open database connection.
        name: The net name (primary key).
        **fields: Column name → value pairs to update.

    Returns:
        ``True`` if exactly one row was updated, ``False`` otherwise.
    """
    valid = {k: v for k, v in fields.items() if k in _NETS_COLUMNS and k != "name"}
    if not valid:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in valid)
    values = list(valid.values()) + [name]
    with conn:
        cur = conn.execute(f"UPDATE nets SET {set_clause} WHERE name = ?", values)
    return cur.rowcount == 1


def update_component(conn: sqlite3.Connection, ref: str, **fields) -> bool:
    """Update the named component row with the supplied *fields*.

    Only fields whose key exists in the ``components`` table schema are
    written; unknown keys are silently ignored.

    Args:
        conn: An open database connection.
        ref: The component designator (primary key).
        **fields: Column name → value pairs to update.

    Returns:
        ``True`` if exactly one row was updated, ``False`` otherwise.
    """
    valid = {k: v for k, v in fields.items() if k in _COMPONENTS_COLUMNS and k != "ref"}
    if not valid:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in valid)
    values = list(valid.values()) + [ref]
    with conn:
        cur = conn.execute(
            f"UPDATE components SET {set_clause} WHERE ref = ?", values
        )
    return cur.rowcount == 1


def update_pin(conn: sqlite3.Connection, ref: str, pin: str, **fields) -> bool:
    """Update a specific pin row with the supplied *fields*.

    Matches first by (ref, pin) — where pin is the Allegro pin number.
    If no row is found, falls back to matching by (ref, pin_name) so that
    IC-review agents can supply pin names from the datasheet instead of
    numeric Allegro IDs.

    Only fields whose key exists in the ``pins`` table schema are written;
    unknown keys are silently ignored.

    Args:
        conn: An open database connection.
        ref: Component designator.
        pin: Pin identifier — either the Allegro numeric pin ID or a pin name.
        **fields: Column name → value pairs to update.

    Returns:
        ``True`` if exactly one row was updated, ``False`` otherwise.
    """
    valid = {
        k: v for k, v in fields.items()
        if k in _PINS_COLUMNS and k not in ("ref", "pin")
    }
    if not valid:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in valid)
    values = list(valid.values()) + [ref, pin]
    with conn:
        cur = conn.execute(
            f"UPDATE pins SET {set_clause} WHERE ref = ? AND pin = ?", values
        )
    if cur.rowcount == 1:
        return True
    # Fallback: try matching by pin_name (IC agents use datasheet pin names)
    values_by_name = list(valid.values()) + [ref, pin]
    with conn:
        cur = conn.execute(
            f"UPDATE pins SET {set_clause} WHERE ref = ? AND pin_name = ?",
            values_by_name,
        )
    return cur.rowcount >= 1


# ---------------------------------------------------------------------------
# Meta key-value helpers
# ---------------------------------------------------------------------------

def get_meta(conn: sqlite3.Connection, key: str, default=None):
    """Read a value from the ``meta`` key-value table.

    Args:
        conn: An open database connection.
        key: The meta key to look up.
        default: Value returned when the key is absent (default ``None``).

    Returns:
        The stored string value, or *default* if the key does not exist.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Write a value to the ``meta`` key-value table (upsert).

    Args:
        conn: An open database connection.
        key: The meta key to set.
        value: The string value to store.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
