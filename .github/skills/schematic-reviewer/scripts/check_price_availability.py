#!/usr/bin/env python3
"""
check_price_availability.py — Supply-chain availability check (disabled).

This check previously queried the internal Highstage parts database for
lifecycle status (NRND, discontinued, last-time-buy). Highstage is no longer
used by this repository — component datasheets and part information are now
sourced from the DOKARKIV network file share, which does not expose lifecycle
or pricing data.

This script is kept as a no-op stub so the pipeline's `supply_check` stage
can still be marked complete without failing the review. It writes a single
`info` issue noting that the check was skipped.

Usage:
    python check_price_availability.py reviews/<SCH_ID>/REVIEW/review.db

Public API:
    from check_price_availability import check_price_availability
    result = check_price_availability(db_path)
    # Returns {"checked": 0, "unreachable": False, "issues_written": N}
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from add_issue import write_issues


def check_price_availability(db_path: str | Path) -> dict:
    """No-op supply-chain check — Highstage lifecycle lookups are no longer available.

    Writes a single ``info`` issue noting the check was skipped, and marks the
    ``supply_check`` pipeline stage as done.

    Args:
        db_path: Path to the review SQLite database.

    Returns:
        ``{"checked": 0, "unreachable": False, "issues_written": int}``
    """
    db_path = Path(db_path)

    skip_issue = [{
        "severity": "info",
        "type": "supply_chain",
        "summary": "Supply-chain lifecycle check skipped (Highstage no longer used)",
        "description": (
            "This project no longer queries Highstage for part lifecycle status. "
            "Datasheets and component information are sourced from the DOKARKIV "
            "network file share, which does not expose lifecycle/pricing data. "
            "Lifecycle / supply-chain status was not verified for any component."
        ),
        "source": "check_price_availability",
    }]
    written = write_issues(db_path, skip_issue)

    _mark_supply_check(db_path)
    return {"checked": 0, "unreachable": False, "issues_written": len(written)}


def _mark_supply_check(db_path: Path) -> None:
    """Mark the ``supply_check`` pipeline stage as done (best-effort)."""
    try:
        from pipeline_status import mark_stage
        mark_stage(db_path, "supply_check", "done")
    except Exception:
        pass


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

    print(f"Supply chain check: {db_path} (skipped — Highstage no longer used)")
    result = check_price_availability(db_path)
    print(
        f"Done — checked={result['checked']}, "
        f"unreachable={result['unreachable']}, "
        f"issues_written={result['issues_written']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
