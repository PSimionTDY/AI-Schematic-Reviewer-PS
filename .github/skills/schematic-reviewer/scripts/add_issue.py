#!/usr/bin/env python3
"""
Canonical issue writer for schematic review agents.

Enforces the required field schema and normalises common field-name variants
so that all issues are consistent regardless of which agent wrote them.

## Usage as a Python module (preferred for IC review agents)

    import sys
    sys.path.insert(0, r'C:\\work\\bitbucket\\schematic_reviewer\\.github\\skills\\schematic-reviewer\\scripts')
    from add_issue import write_issues

    write_issues('reviews/SCH25678-1F/review.db', [
        {
            'severity': 'critical',
            'type': 'ic_shoot_through',
            'summary': 'U400 pin 18 shoot-through risk in IIM mode.',
            'description': 'U400 pin 18 (EN/HI): permanently HIGH in IIM mode — shoot-through risk.',
            'ref': 'U400',
            'pin': '18',
            'net': 'N148821_TRANSMITTERS_TX1',
            'resolution': 'DNP R401 (0R), populate R408 (267kΩ) to GND → PWM mode.',
        },
        {
            'severity': 'minor',
            'type': 'ic_decoupling',
            'summary': 'U400 VDD pin 4: no local 100nF bypass cap.',
            'description': 'U400 VDD (pin 4): no local 100nF HF bypass cap confirmed.',
            'ref': 'U400',
            'pin': '4',
        },
    ])

## Usage from the command line

    # Write from a YAML file
    python add_issue.py review.db --from-yaml my_issues.yaml

    # Append a single issue inline (JSON string)
    python add_issue.py review.db --json '{"severity":"minor","type":"ic_decoupling","summary":"...","description":"..."}'

## Schema

Required fields:
    severity     : critical | major | minor | question | info
    summary      : one-line title for the issue (≤ 120 chars)
    description  : human-readable plain-English description of the issue

Strongly recommended:
    type         : short snake_case identifier, e.g. ic_shoot_through
    ref          : primary component reference, e.g. U400
    resolution   : what the designer should do to fix it

Optional:
    pin          : pin number as string
    net          : net name
    sheet        : sheet name or number
    refs         : list of supporting citations / datasheet references
    components   : list of {ref: X} dicts (auto-built from `ref` if omitted)
    also_reported_by : list of other refs that flagged the same issue
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_SEVERITIES = {"critical", "major", "minor", "question", "info"}

# Field-name aliases: map non-canonical → canonical name.
# Applied before validation so agents can use any of these names.
# NOTE: "summary" → "description" fallback is handled in _normalise(), not here,
# to avoid clobbering a real summary field.
_FIELD_ALIASES: dict[str, str] = {
    "title": "summary",
    "issue": "description",
    "text": "description",
    "message": "description",
    "component": "ref",
    "reference": "ref",
    "designator": "ref",
    "recommendation": "resolution",
    "fix": "resolution",
    "action": "resolution",
    "action_required": "resolution",
    "category": "type",
    "issue_type": "type",
}

# Fields that belong in the canonical output, in preferred display order.
_CANONICAL_ORDER = [
    "id",
    "severity",
    "type",
    "summary",
    "description",
    "resolution",
    "refs",
    "ref",
    "pin",
    "net",
    "sheet",
    "source",
    "also_reported_by",
    "components",
    "created_at",
]

_SUMMARY_MAX_CHARS = 120


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _auto_summary(description: str) -> str:
    """Generate a summary from the first sentence of *description* (≤ 120 chars)."""
    # Take the first sentence (up to first '.', '!', or '?')
    for sep in (".", "!", "?"):
        idx = description.find(sep)
        if idx != -1:
            candidate = description[: idx + 1].strip()
            break
    else:
        candidate = description.strip()

    if len(candidate) <= _SUMMARY_MAX_CHARS:
        return candidate

    # Truncate at last word boundary before the limit
    truncated = candidate[: _SUMMARY_MAX_CHARS]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated + "…"


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised copy of *raw*, applying aliases and defaults."""
    issue: dict[str, Any] = {}

    for key, value in raw.items():
        canonical_key = _FIELD_ALIASES.get(key, key)
        # If both the alias and the canonical name are present, canonical wins.
        if canonical_key not in issue:
            issue[canonical_key] = value
        elif canonical_key == key:
            issue[canonical_key] = value

    # ------------------------------------------------------------------
    # summary / description cross-population
    # ------------------------------------------------------------------
    has_summary = bool(issue.get("summary", "").strip() if isinstance(issue.get("summary"), str) else issue.get("summary"))
    has_description = bool(issue.get("description", "").strip() if isinstance(issue.get("description"), str) else issue.get("description"))

    if not has_summary and not has_description:
        # Last-resort: pull from detail or the raw "summary" key used as description
        if "detail" in issue:
            issue["description"] = str(issue["detail"]).strip().splitlines()[0]
            has_description = True

    if has_description and not has_summary:
        # Auto-generate summary from description
        issue["summary"] = _auto_summary(str(issue["description"]).strip())

    elif has_summary and not has_description:
        # Copy summary to description with a warning
        print(
            f"WARNING: issue has 'summary' but no 'description' — copying summary to description. "
            f"Provide both for best results. summary={issue['summary']!r}",
            file=sys.stderr,
        )
        issue["description"] = issue["summary"]

    # ------------------------------------------------------------------
    # severity normalisation
    # ------------------------------------------------------------------
    sev = str(issue.get("severity", "info")).lower().strip()
    sev = {"warn": "minor", "warning": "minor", "error": "critical",
           "critical_error": "critical", "info": "info", "information": "info",
           "note": "info", "high": "critical", "medium": "major",
           "low": "minor"}.get(sev, sev)
    if sev not in VALID_SEVERITIES:
        sev = "info"
    issue["severity"] = sev

    # ------------------------------------------------------------------
    # components: build from ref if not already a list
    # ------------------------------------------------------------------
    ref = issue.get("ref", "")
    if "components" not in issue:
        if ref:
            issue["components"] = [{"ref": ref}]
    else:
        comps = issue["components"]
        if isinstance(comps, str):
            issue["components"] = [{"ref": comps}]
        elif isinstance(comps, list):
            normalised_comps = []
            for c in comps:
                if isinstance(c, str):
                    normalised_comps.append({"ref": c})
                elif isinstance(c, dict) and "ref" not in c and ref:
                    normalised_comps.append({"ref": ref, **c})
                else:
                    normalised_comps.append(c)
            issue["components"] = normalised_comps

    # Ensure primary ref is first in the components list
    if ref and issue.get("components"):
        refs_in_list = [c.get("ref") for c in issue["components"] if isinstance(c, dict)]
        if ref not in refs_in_list:
            issue["components"].insert(0, {"ref": ref})

    # ------------------------------------------------------------------
    # pin: coerce to string
    # ------------------------------------------------------------------
    if "pin" in issue and issue["pin"] is not None:
        issue["pin"] = str(issue["pin"])

    return issue


def _reorder(issue: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *issue* with canonical keys first, extras at end."""
    ordered: dict[str, Any] = {}
    for key in _CANONICAL_ORDER:
        if key in issue:
            ordered[key] = issue[key]
    for key, value in issue.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(issue: dict[str, Any], index: int) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    if not issue.get("summary"):
        errors.append(f"issue[{index}]: 'summary' is required and must not be empty")
    if not issue.get("description"):
        errors.append(f"issue[{index}]: 'description' is required and must not be empty")
    if issue.get("severity") not in VALID_SEVERITIES:
        errors.append(
            f"issue[{index}]: 'severity' must be one of {sorted(VALID_SEVERITIES)}, "
            f"got {issue.get('severity')!r}"
        )
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_issues(db_path: str | Path, issues: list[dict[str, Any]]) -> list[str]:
    """Normalise *issues* and insert them into the SQLite database at *db_path*.

    Creates the database (and schema) if it does not already exist.

    Args:
        db_path: Path to ``review.db``. If the path ends with ``.yaml`` or
                 ``.yml`` a deprecation warning is printed to stderr and the
                 function raises ``ValueError`` — pass a ``.db`` path instead.
        issues:  List of raw issue dicts (field names are normalised
                 automatically before writing).

    Returns:
        List of assigned ``ISS_NNN`` ids (one per inserted issue).

    Raises:
        ValueError: If *db_path* ends in ``.yaml``/``.yml``, or if any issue
                    fails validation after normalisation.
    """
    db_path = Path(db_path)

    if db_path.suffix.lower() in (".yaml", ".yml"):
        print(
            f"DEPRECATION ERROR: '{db_path}' looks like a YAML path. "
            "Pass a .db path (e.g. review.db) instead.",
            file=sys.stderr,
        )
        raise ValueError(
            f"db_path must not end in .yaml/.yml — got '{db_path}'. "
            "Pass a .db path instead."
        )

    normalised = [_normalise(i) for i in issues]

    # Validate all before writing anything
    all_errors: list[str] = []
    for idx, issue in enumerate(normalised):
        all_errors.extend(_validate(issue, idx))
    if all_errors:
        raise ValueError(
            "Issue validation failed — fix these errors before writing:\n"
            + "\n".join(f"  • {e}" for e in all_errors)
        )

    normalised_ordered = [_reorder(i) for i in normalised]

    # Ensure the DB exists with the correct schema
    _ensure_db(db_path)

    from db_io import get_connection, insert_issue

    conn = get_connection(db_path)
    ids: list[str] = []
    with conn:
        for issue in normalised_ordered:
            ids.append(insert_issue(conn, issue))
    return ids


def _ensure_db(db_path: Path) -> None:
    """Create *db_path* with the review schema if it does not yet exist."""
    import importlib, sys as _sys
    scripts_dir = Path(__file__).parent
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    from db_schema import create_db
    create_db(db_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Write canonically-formatted issues to review.db.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("output", help="Path to review.db (SQLite database)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--from-yaml", metavar="FILE",
        help="Read issues from a YAML file containing an 'issues:' list",
    )
    group.add_argument(
        "--json", metavar="JSON",
        help="Single issue as a JSON string",
    )
    group.add_argument(
        "--stdin", action="store_true",
        help="Read a YAML 'issues:' block from stdin",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    if output_path.suffix.lower() in (".yaml", ".yml"):
        print(
            f"ERROR: output path '{args.output}' ends in .yaml/.yml — "
            "pass a .db path (e.g. review.db) instead.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.from_yaml:
        with open(args.from_yaml, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        issues = data.get("issues", data) if isinstance(data, dict) else data

    elif args.json:
        raw = json.loads(args.json)
        issues = [raw] if isinstance(raw, dict) else raw

    else:  # stdin
        data = yaml.safe_load(sys.stdin)
        issues = data.get("issues", data) if isinstance(data, dict) else data

    if not isinstance(issues, list):
        print("ERROR: input must be a YAML list or a dict with an 'issues:' key", file=sys.stderr)
        sys.exit(1)

    ids = write_issues(args.output, issues)
    print(f"Wrote {len(ids)} issue(s) → {args.output}: {', '.join(ids)}")


if __name__ == "__main__":
    _cli()
