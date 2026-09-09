"""
check_obsolescence.py — Flag potentially obsolete components using static
heuristics and known-bad part-number patterns.

Complements check_price_availability.py: that script checks live Highstage
data; this script applies offline pattern matching to every active component
in the DB.

Checks performed
----------------
1. Known obsolete part-number patterns (LM7805*, MAX232*, large SOIC 74HC*)
2. Through-hole / large-SOIC package indicators
3. ICs with no manufacturer part number (cannot verify obsolescence)
4. lifecycle_status column already set to nrnd / last_time_buy / discontinued

Usage
-----
    python check_obsolescence.py reviews/<SCH_ID>/REVIEW/review.db
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Known obsolete part-number patterns
# (pattern, human description, suggested replacement)
# ---------------------------------------------------------------------------

_OBSOLETE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"^LM78\d{2}", re.IGNORECASE),
        "older linear regulator family with poor efficiency and high dropout",
        "Consider replacing with a modern LDO such as TLV1117, AP2112, or MCP1700.",
    ),
    (
        re.compile(r"^MAX232", re.IGNORECASE),
        "older RS-232 level-shifter; modern equivalents require less external circuitry",
        "Consider SP3232, MAX3232, or SP232 which work at 3.3 V and need smaller caps.",
    ),
]

# 74HC* in SOIC packages wider than 16 pins is a soft indicator only
_74HC_PATTERN = re.compile(r"^74HC", re.IGNORECASE)
_LARGE_SOIC_PKG = re.compile(r"SOIC-(\d+)", re.IGNORECASE)

# lifecycle values that warrant an issue (written by check_price_availability
# or manually)
_BAD_LIFECYCLE = {"nrnd", "last_time_buy", "discontinued"}

_LIFECYCLE_SEVERITY = {
    "nrnd": "minor",
    "last_time_buy": "major",
    "discontinued": "major",
}
_LIFECYCLE_LABEL = {
    "nrnd": "Not Recommended for New Designs (NRND)",
    "last_time_buy": "last-time-buy",
    "discontinued": "discontinued",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_lifecycle_column(conn) -> bool:
    """Return True if the components table has a lifecycle_status column."""
    try:
        pragma = conn.execute("PRAGMA table_info(components)").fetchall()
        return any(row["name"] == "lifecycle_status" for row in pragma)
    except Exception:
        return False


def _build_issues(row, has_lifecycle: bool) -> list[dict]:
    """Return zero or more issue dicts for a single component row."""
    ref = row["ref"]
    mpn = row["mfg_part_number"] or ""
    pkg = row["package"] or ""
    ctype = (row["comp_type"] or "").lower()
    issues: list[dict] = []

    # ------------------------------------------------------------------
    # 1. Known obsolete part-number patterns
    # ------------------------------------------------------------------
    for pattern, reason, resolution in _OBSOLETE_PATTERNS:
        if mpn and pattern.search(mpn):
            issues.append({
                "severity": "minor",
                "type": "obsolescence",
                "summary": f"{ref} ({mpn}): potentially obsolete part",
                "description": (
                    f"{mpn} is a {reason}. "
                    "Verify this choice is intentional for a legacy-compatible design."
                ),
                "resolution": resolution,
                "ref": ref,
                "source": "check_obsolescence",
            })
            break  # one pattern match per component is enough

    # 74HC* in large SOIC package
    if mpn and _74HC_PATTERN.search(mpn):
        m = _LARGE_SOIC_PKG.search(pkg)
        if m and int(m.group(1)) > 16:
            issues.append({
                "severity": "minor",
                "type": "obsolescence",
                "summary": f"{ref} ({mpn}): large SOIC 74HC — modern alternatives available",
                "description": (
                    f"{mpn} in {pkg} is a wide SOIC 74-series part. "
                    "Modern logic families (74LVC, 74AUC) offer smaller packages and "
                    "better electrical performance."
                ),
                "resolution": (
                    "Evaluate whether a 74LVC or 74AUC equivalent in a smaller package "
                    "can be used."
                ),
                "ref": ref,
                "source": "check_obsolescence",
            })

    # ------------------------------------------------------------------
    # 2. Through-hole / large-package indicators
    # ------------------------------------------------------------------
    pkg_upper = pkg.upper()
    if "DIP" in pkg_upper or "PDIP" in pkg_upper:
        issues.append({
            "severity": "info",
            "type": "obsolescence",
            "summary": f"{ref}: through-hole package ({pkg})",
            "description": (
                f"{ref} uses a through-hole package ({pkg}). "
                "Confirm this is intentional — DIP packages are unusual on modern SMD boards."
            ),
            "resolution": (
                "If the board is surface-mount, consider an SMD equivalent. "
                "If through-hole is required (e.g. for ruggedness), document the rationale."
            ),
            "ref": ref,
            "source": "check_obsolescence",
        })
    else:
        m = _LARGE_SOIC_PKG.search(pkg)
        if m and int(m.group(1)) >= 28:
            issues.append({
                "severity": "info",
                "type": "obsolescence",
                "summary": f"{ref}: large SOIC package ({pkg})",
                "description": (
                    f"{ref} uses {pkg}, a wide SOIC footprint. "
                    "Modern replacements often come in smaller QFP or QFN packages."
                ),
                "resolution": (
                    "Check whether a more compact SMD package is available for this part."
                ),
                "ref": ref,
                "source": "check_obsolescence",
            })

    # ------------------------------------------------------------------
    # 3. IC with no manufacturer part number
    # ------------------------------------------------------------------
    if ctype == "ic" and not mpn:
        issues.append({
            "severity": "info",
            "type": "obsolescence",
            "summary": f"{ref}: no manufacturer part number — cannot check obsolescence",
            "description": (
                f"{ref} is an IC with no mfg_part_number in the database. "
                "Obsolescence cannot be assessed without a part number."
            ),
            "resolution": "Add the manufacturer part number to the BOM.",
            "ref": ref,
            "source": "check_obsolescence",
        })

    # ------------------------------------------------------------------
    # 4. lifecycle_status already set in DB
    # ------------------------------------------------------------------
    if has_lifecycle:
        lc = (row["lifecycle_status"] or "").lower().strip()
        if lc in _BAD_LIFECYCLE:
            sev = _LIFECYCLE_SEVERITY.get(lc, "minor")
            label = _LIFECYCLE_LABEL.get(lc, lc)
            mpn_label = f" ({mpn})" if mpn else ""
            issues.append({
                "severity": sev,
                "type": "obsolescence",
                "summary": f"{ref}{mpn_label}: lifecycle status is {label}",
                "description": (
                    f"{ref}{mpn_label} has a lifecycle status of '{lc}' "
                    "as recorded in the parts database."
                ),
                "resolution": (
                    "Find an approved alternate part or obtain a lifetime-buy quantity "
                    "before the part becomes unavailable."
                ),
                "ref": ref,
                "source": "check_obsolescence",
            })

    return issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_obsolescence(db_path: str | Path) -> dict:
    """Run obsolescence checks on all active components in *db_path*.

    Args:
        db_path: Path to review.db.

    Returns:
        ``{"checked": N, "issues_written": N}``
    """
    db_path = Path(db_path)
    scripts_dir = Path(__file__).parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from db_io import get_connection, insert_issue

    conn = get_connection(db_path)

    has_lifecycle = _has_lifecycle_column(conn)

    if has_lifecycle:
        query = "SELECT ref, comp_type, mfg_part_number, package, lifecycle_status FROM components WHERE dnp = 0"
    else:
        query = "SELECT ref, comp_type, mfg_part_number, package FROM components WHERE dnp = 0"

    rows = conn.execute(query).fetchall()

    all_issues: list[dict] = []
    for row in rows:
        all_issues.extend(_build_issues(row, has_lifecycle))

    for issue in all_issues:
        insert_issue(conn, issue)

    conn.close()

    # ------------------------------------------------------------------
    # Pipeline stage
    # ------------------------------------------------------------------
    try:
        from pipeline_status import mark_stage, is_stage_done
        if not is_stage_done(db_path, "supply_check"):
            mark_stage(db_path, "supply_check", "done")
    except ImportError:
        pass

    return {"checked": len(rows), "issues_written": len(all_issues)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python check_obsolescence.py <db_path>", file=sys.stderr)
        sys.exit(1)

    result = check_obsolescence(sys.argv[1])
    print(
        f"check_obsolescence: checked={result['checked']}  "
        f"issues_written={result['issues_written']}"
    )
