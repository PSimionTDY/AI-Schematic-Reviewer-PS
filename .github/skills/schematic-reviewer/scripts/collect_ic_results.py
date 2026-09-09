#!/usr/bin/env python3
"""
Collect IC agent YAML results and write issues_ic_review.yaml.

Each IC agent writes its result to REVIEW/ic_contexts/<ref>_issues.yaml.
This script merges all of them, adds ref/schematic_ref fields, and
writes the combined REVIEW/issues_ic_review.yaml.

Usage:
    collect_ic_results.py reviews/<SCH_ID>

IC agents write their output to:
    REVIEW/ic_contexts/<REF>_issues.yaml

Format expected from each agent:
    issues:
      - severity: major
        type: ic_vcc_wrong_rail
        description: "..."
        pin: "16"
        net: "+5V"
"""
import argparse
import sys
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect IC agent YAML results.")
    parser.add_argument("review_dir", help="Path to reviews/<SCH_ID>")
    args = parser.parse_args()

    review_root = Path(args.review_dir)
    ic_contexts_dir = review_root / "REVIEW" / "ic_contexts"
    output_path = review_root / "REVIEW" / "issues_ic_review.yaml"

    if not ic_contexts_dir.exists():
        print(f"ERROR: {ic_contexts_dir} does not exist. Run prepare_ic_context.py first.")
        sys.exit(1)

    # Load index for ref→schematic_ref mapping
    index_path = ic_contexts_dir / "_index.json"
    ref_meta: dict[str, dict] = {}
    if index_path.exists():
        import json
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in (index if isinstance(index, list) else index.get("ics", [])):
            ref_meta[entry["ref"]] = entry

    # Build an ID registry from the existing issues_ic_review.yaml so that
    # stable ISS_NNN IDs are preserved across rebuilds.
    # Key: (ref, position_within_ref) → id  — position is stable as long as
    # the individual ic_contexts/*_issues.yaml files don't change order.
    id_registry: dict[tuple, str] = {}
    if output_path.exists():
        try:
            existing = yaml.safe_load(output_path.read_text(encoding="utf-8")) or {}
            ref_counters: dict[str, int] = {}
            for iss in existing.get("issues", []):
                existing_id = iss.get("id") or ""
                if not existing_id.startswith("ISS_"):
                    continue
                ref = iss.get("ref", "")
                pos = ref_counters.get(ref, 0)
                id_registry[(ref, pos)] = existing_id
                ref_counters[ref] = pos + 1
        except Exception:
            pass  # corrupted file — start fresh

    all_issues: list[dict] = []
    result_files = sorted(ic_contexts_dir.glob("*_issues.yaml"))

    if not result_files:
        print("No *_issues.yaml files found in ic_contexts/. Have the IC agents run yet?")
        sys.exit(0)

    for result_file in result_files:
        ref = result_file.stem.replace("_issues", "")
        try:
            data = yaml.safe_load(result_file.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"  [warn] could not parse {result_file.name}: {exc}")
            continue

        issues = data.get("issues") or []
        meta = ref_meta.get(ref, {})
        schematic_ref = meta.get("schematic_ref", {})

        for pos, issue in enumerate(issues):
            issue["ref"] = ref
            if schematic_ref.get("func_des"):
                issue["func_des"] = schematic_ref["func_des"]
            if schematic_ref.get("sheet"):
                issue["sheet"] = schematic_ref["sheet"]
            # Restore stable ID from registry if not already set
            if not (issue.get("id") or "").startswith("ISS_"):
                existing_id = id_registry.get((ref, pos))
                if existing_id:
                    issue["id"] = existing_id
            all_issues.append(issue)

        print(f"  {ref}: {len(issues)} issue(s)")

    output = {"issues": all_issues}
    output_path.write_text(
        yaml.dump(output, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"\nWrote {len(all_issues)} total issues → {output_path}")
    print("Run build_issues.py to merge with other issue files.")


if __name__ == "__main__":
    main()
