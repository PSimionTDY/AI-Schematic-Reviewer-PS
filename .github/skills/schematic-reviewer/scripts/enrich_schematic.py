#!/usr/bin/env python3
"""
Apply net voltage/type enrichments to schematic.yaml.

Used in two ways:

1. Interactive (Step 1b) — reviewer agent calls this after confirming
   power rail voltages with the user:

     python enrich_schematic.py reviews/SCH26782-1 --patch my_patch.yaml

2. Automated (Step 4f) — called by collect_enrichments.py to merge IC
   agent discoveries back into schematic.yaml.

Patch YAML format:
    nets:
      VCC_5V:
        voltage: 5.0
        confidence: confirmed
        type: power
      ISO_GND_0V:
        voltage: 0.0
        confidence: confirmed
        type: gnd
"""
import argparse
import sys
from pathlib import Path

import yaml

from yaml_io import locked_yaml_update


CONFIDENCE_ORDER = ["unknown", "doubtful", "inferred", "confirmed"]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def apply_patch(schematic: dict, patch: dict) -> int:
    """Apply net patches to schematic. Returns count of nets updated."""
    updated = 0
    nets = schematic.setdefault("nets", {})
    for net_name, updates in patch.get("nets", {}).items():
        if net_name not in nets:
            continue  # don't add nets that don't exist in the schematic
        net = nets[net_name]
        existing_conf = net.get("confidence", "unknown")
        new_conf = updates.get("confidence", existing_conf)
        # Only update if new confidence is >= existing (never downgrade)
        if CONFIDENCE_ORDER.index(new_conf) >= CONFIDENCE_ORDER.index(existing_conf):
            for key, value in updates.items():
                net[key] = value
            updated += 1
    return updated


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich schematic.yaml with confirmed net data")
    ap.add_argument("workspace", help="Path to schematic workspace (reviews/<SCH_ID>)")
    ap.add_argument("--patch", required=True, help="YAML file with net enrichments to apply")
    ap.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = ap.parse_args()

    workspace = Path(args.workspace)
    schematic_path = workspace / "REVIEW" / "schematic.yaml"
    patch_path = Path(args.patch)

    if not schematic_path.exists():
        print(f"Error: schematic.yaml not found at {schematic_path}", file=sys.stderr)
        sys.exit(1)
    if not patch_path.exists():
        print(f"Error: patch file not found at {patch_path}", file=sys.stderr)
        sys.exit(1)

    schematic = load_yaml(schematic_path)
    patch = load_yaml(patch_path)

    if args.dry_run:
        nets = schematic.get("nets", {})
        for net_name, updates in patch.get("nets", {}).items():
            if net_name in nets:
                print(f"  {net_name}: {nets[net_name].get('voltage')} → {updates.get('voltage', '(unchanged)')}")
        return

    with locked_yaml_update(schematic_path) as schematic:
        updated = apply_patch(schematic, patch)
    print(f"Enriched {updated} nets in {schematic_path}")


if __name__ == "__main__":
    main()
