#!/usr/bin/env python3
"""
Merge all REVIEW/issues_*.yaml files into REVIEW/issues.yaml.

Issue ID stability rules:
  - Once an issue is assigned ISS_NNN it NEVER changes, even if severity changes.
  - Issues with an existing id in the source file keep that id.
  - Issues with id=null (new issues) are assigned the next available id.
  - Assigned ids are written back into the source files so they persist on
    the next rebuild.
  - Issues are sorted by severity in the output file for readability, but
    that sort does NOT affect the id.

Usage:
    python build_issues.py reviews/SCH25678-1E
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

try:
    from filelock import FileLock
except ImportError:
    # Minimal fallback — single-process usage only
    class FileLock:  # type: ignore
        def __init__(self, path, timeout=60): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

SEVERITY_ORDER = ['critical', 'major', 'minor', 'question', 'info']


def _load_source(src: Path):
    with open(src, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        return data, data.get('issues', [])
    if isinstance(data, list):
        return {'issues': data}, data
    return {'issues': []}, []


def _save_source(src: Path, data: dict):
    lock = FileLock(str(src) + '.lock', timeout=60)
    with lock:
        with open(src, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False,
                      default_flow_style=False)


def main():
    ap = argparse.ArgumentParser(description="Build merged issues.yaml")
    ap.add_argument("schematic_folder")
    ap.add_argument("--output", "-o")
    args = ap.parse_args()

    folder = Path(args.schematic_folder)
    review_dir = folder / "REVIEW"

    # Load schematic meta
    sch_yaml = review_dir / "schematic.yaml"
    schematic_id = folder.name
    if sch_yaml.exists():
        with open(sch_yaml, encoding='utf-8') as f:
            sch = yaml.safe_load(f)
        schematic_id = sch.get('meta', {}).get('schematic_id', folder.name)

    # ------------------------------------------------------------------ #
    # Pass 1: collect all issues from source files, tracking which file   #
    # each issue came from so we can write back assigned IDs.             #
    # ------------------------------------------------------------------ #
    sources = sorted(review_dir.glob("issues_*.yaml"))
    # Each entry: (issue_dict, source_path, source_data_dict)
    tagged: list[tuple[dict, Path, dict]] = []
    for src in sources:
        data, issues = _load_source(src)
        for iss in issues:
            tagged.append((iss, src, data))

    # Normalize field names: some subagents write summary+detail
    for iss, _, _ in tagged:
        if not iss.get('description'):
            parts = []
            for field in ('summary', 'detail'):
                if iss.get(field):
                    parts.append(iss[field])
            if iss.get('action'):
                parts.append('Action: ' + iss['action'])
            if parts:
                iss['description'] = '\n\n'.join(parts)

    # ------------------------------------------------------------------ #
    # Pass 2: determine the highest already-assigned id so new ids start  #
    # above it.                                                            #
    # ------------------------------------------------------------------ #
    existing_nums = set()
    for iss, _, _ in tagged:
        raw = iss.get('id') or ''
        if isinstance(raw, str) and raw.startswith('ISS_'):
            try:
                existing_nums.add(int(raw[4:]))
            except ValueError:
                pass

    next_id = max(existing_nums, default=0) + 1

    # ------------------------------------------------------------------ #
    # Pass 3: assign ids to new issues and collect dirty source files.    #
    # ------------------------------------------------------------------ #
    dirty: dict[Path, dict] = {}  # src path -> data dict that needs saving

    for iss, src, data in tagged:
        raw = iss.get('id') or ''
        needs_id = not (isinstance(raw, str) and raw.startswith('ISS_'))
        if needs_id:
            iss['id'] = f"ISS_{next_id:03d}"
            next_id += 1
            dirty[src] = data

    # Write back any source files that received new IDs
    for src, data in dirty.items():
        _save_source(src, data)
    if dirty:
        print(f"  Wrote new IDs back to {len(dirty)} source file(s).")

    # ------------------------------------------------------------------ #
    # Pass 4: sort for output (severity order) — ids do NOT change here.  #
    # ------------------------------------------------------------------ #
    all_issues = [iss for iss, _, _ in tagged]

    def sev_key(issue):
        sev = issue.get('severity', 'info')
        return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else 99

    all_issues.sort(key=sev_key)

    counts = Counter(i.get('severity', 'info') for i in all_issues)

    output = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'schematic_id': schematic_id,
        'summary': {sev: counts.get(sev, 0) for sev in SEVERITY_ORDER},
        'issues': all_issues,
    }

    out_path = Path(args.output) if args.output else review_dir / "issues.yaml"
    with open(out_path, 'w', encoding='utf-8') as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False,
                  default_flow_style=False)

    print(f"\nTotal: {len(all_issues)} issues")
    for sev in SEVERITY_ORDER:
        if counts.get(sev):
            print(f"  {sev}: {counts[sev]}")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
