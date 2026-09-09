#!/usr/bin/env python3
"""
check_price_availability.py — Supply-chain availability check using Highstage.

Queries the internal Highstage parts database to detect components whose
lifecycle status indicates a supply risk (NRND, discontinued, last-time-buy).

For each active (non-DNP) component that has a ``highstage_id``, the script
queries the Highstage search API and scans the result's status / description
fields for lifecycle keywords.  All Highstage access is wrapped in try/except
so that an unreachable server never fails the overall review pipeline.

Usage:
    python check_price_availability.py reviews/<SCH_ID>/REVIEW/review.db

Public API:
    from check_price_availability import check_price_availability
    result = check_price_availability(db_path)
    # Returns {"checked": N, "unreachable": bool, "issues_written": N}
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# Ensure the scripts directory is on sys.path for sibling imports
_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from db_io import get_connection
from add_issue import write_issues

HIGHSTAGE_SEARCH_URL = "https://highstage/ts/ts/search.aspx"

# ---------------------------------------------------------------------------
# Lifecycle keyword detection table
# Order matters: more-severe patterns are listed first.
# ---------------------------------------------------------------------------

_LIFECYCLE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\blast[\s_-]time[\s_-]buy\b", re.I), "critical", "last-time-buy"),
    (re.compile(r"\bltb\b",                            re.I), "critical", "last-time-buy"),
    (re.compile(r"\bdiscontinued\b",                   re.I), "major",    "discontinued"),
    (re.compile(r"\bnrnd\b",                           re.I), "major",    "NRND"),
    (re.compile(r"\bnot\s+recommended\s+for\s+new\s+design", re.I), "major", "NRND"),
]

# Human-readable labels and resolutions keyed by lifecycle label
_LIFECYCLE_META: dict[str, tuple[str, str]] = {
    "last-time-buy": (
        "last-time-buy (LTB)",
        "This part is in last-time-buy status. Place a procurement order immediately "
        "if it is still required. Check Highstage for suggested replacements.",
    ),
    "discontinued": (
        "discontinued",
        "Find an alternative part. Check Highstage for suggested replacements.",
    ),
    "NRND": (
        "not recommended for new designs (NRND)",
        "Find an alternative part. Check Highstage for suggested replacements.",
    ),
}


# ---------------------------------------------------------------------------
# Highstage query helpers
# ---------------------------------------------------------------------------

def _query_highstage(part_id: str) -> Optional[dict]:
    """Query the Highstage search API and return the first result dict, or None.

    Retries with the ``-1A`` working-draft suffix if the first attempt returns
    no rows (mirrors the behaviour in highstage_downloader.py).

    Raises:
        RuntimeError: if ``requests`` or ``requests_negotiate_sspi`` are absent.
        Any ``requests`` exception propagates to the caller for graceful handling.
    """
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        import requests
        from requests_negotiate_sspi import HttpNegotiateAuth
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for Highstage query: {exc}") from exc

    cols = "o%3Btitle%3Bstatus%3Bdescription%3Bworkspace"

    def _fetch(pid: str) -> list[dict]:
        url = f"{HIGHSTAGE_SEARCH_URL}?t=part&o={pid}&_format=xml_raw&_columns={cols}"
        resp = requests.get(url, auth=HttpNegotiateAuth(), verify=False, timeout=20)
        if not resp.ok:
            return []
        try:
            root = ET.fromstring(resp.text.strip())
            return [dict(row.attrib) for row in root.iter("row")]
        except ET.ParseError:
            return []

    results = _fetch(part_id)

    # Retry with working-draft suffix when Highstage returns nothing
    if not results and not part_id.endswith("-1A"):
        results = _fetch(part_id + "-1A")
        if results:
            print(f"  [info] {part_id} is a working draft — found via {part_id}-1A")

    return results[0] if results else None


def _detect_lifecycle(result: dict) -> Optional[tuple[str, str]]:
    """Scan a Highstage result dict for lifecycle-concern keywords.

    Returns:
        ``(severity, label)`` on the first match, or ``None`` if no concern.
    """
    # Combine all text fields so partial matches in any field are caught
    combined = " ".join(filter(None, [
        result.get("status", ""),
        result.get("description", ""),
        result.get("title", ""),
    ]))
    for pattern, severity, label in _LIFECYCLE_PATTERNS:
        if pattern.search(combined):
            return severity, label
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_price_availability(db_path: str | Path) -> dict:
    """Check supply-chain lifecycle status for all active components that have
    a ``highstage_id``.

    * Components with ``dnp = 1`` are skipped.
    * If the Highstage server is unreachable, a single ``info`` issue is written
      and the function returns ``unreachable=True`` (exit code is still 0).
    * Marks the ``supply_check`` pipeline stage as ``done`` on completion.

    Args:
        db_path: Path to the review SQLite database.

    Returns:
        ``{"checked": int, "unreachable": bool, "issues_written": int}``
    """
    db_path = Path(db_path)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT ref, highstage_id FROM components "
            "WHERE highstage_id IS NOT NULL AND highstage_id != '' AND dnp = 0"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No components with highstage_id found — nothing to check.")
        _mark_supply_check(db_path)
        return {"checked": 0, "unreachable": False, "issues_written": 0}

    issues: list[dict] = []
    checked = 0
    unreachable = False

    for row in rows:
        ref = row["ref"]
        highstage_id = row["highstage_id"]

        try:
            result = _query_highstage(highstage_id)
        except Exception as exc:
            print(
                f"WARNING: Highstage unreachable for {ref} ({highstage_id}): {exc}",
                file=sys.stderr,
            )
            unreachable = True
            break

        checked += 1

        if result is None:
            print(f"  [{ref}] {highstage_id}: not found in Highstage — skipping")
            continue

        concern = _detect_lifecycle(result)
        if concern is None:
            print(f"  [{ref}] {highstage_id}: OK")
            continue

        severity, label = concern
        human_label, resolution = _LIFECYCLE_META[label]
        status_text = result.get("status", "")
        description_text = result.get("description", "")

        summary = f"{ref} ({highstage_id}): {human_label}"
        description = (
            f"{ref} uses Highstage part {highstage_id}, "
            f"which has lifecycle status: {human_label}."
        )
        if status_text:
            description += f" Highstage status field: '{status_text}'."
        if description_text:
            description += f" Description: {description_text}."

        issues.append({
            "severity": severity,
            "type": "supply_chain",
            "summary": summary,
            "description": description,
            "resolution": resolution,
            "ref": ref,
            "source": "check_price_availability",
            "refs": [{"type": "datasheet", "highstage_id": highstage_id}],
        })
        print(f"  [{ref}] {highstage_id}: {label} → {severity}")

    if unreachable:
        print("WARNING: Highstage unavailable — supply chain check skipped.", file=sys.stderr)
        skip_issue = [{
            "severity": "info",
            "type": "supply_chain",
            "summary": "Highstage unavailable — supply chain check skipped",
            "description": (
                "The Highstage parts database was unreachable during this review. "
                "Lifecycle / supply-chain status could not be verified for any component. "
                "Re-run check_price_availability.py when Highstage is accessible."
            ),
            "source": "check_price_availability",
        }]
        written = write_issues(db_path, skip_issue)
        _mark_supply_check(db_path)
        return {"checked": 0, "unreachable": True, "issues_written": len(written)}

    issues_written = 0
    if issues:
        written = write_issues(db_path, issues)
        issues_written = len(written)

    _mark_supply_check(db_path)
    return {"checked": checked, "unreachable": False, "issues_written": issues_written}


def _mark_supply_check(db_path: Path) -> None:
    """Mark the ``supply_check`` pipeline stage as done (best-effort)."""
    try:
        from pipeline_status import mark_stage
        mark_stage(db_path, "supply_check", "done")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python check_price_availability.py <db_path>",
            file=sys.stderr,
        )
        return 1

    db_path = Path(sys.argv[1])
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Supply chain check: {db_path}")
    result = check_price_availability(db_path)
    print(
        f"Done — checked={result['checked']}, "
        f"unreachable={result['unreachable']}, "
        f"issues_written={result['issues_written']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
