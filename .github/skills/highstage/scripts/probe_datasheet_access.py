#!/usr/bin/env python3
"""
Probe Highstage datasheet accessibility for all components in a schematic.

Reads schematic.yaml, queries the Highstage search API for each unique
highstage_id to resolve the UNC path, and writes REVIEW/datasheet_access.yaml.

Usage:
    python probe_datasheet_access.py reviews/SCH25678-1E
    python probe_datasheet_access.py reviews/SCH25678-1E --download
"""
import argparse
import os
import shutil
import sys
import urllib3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from filelock import FileLock
from requests_negotiate_sspi import HttpNegotiateAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HIGHSTAGE_HOST = os.environ.get("HIGHSTAGE_HOST", "https://highstage.tdy.teledyne.com")
HIGHSTAGE_SEARCH_URL = f"{_HIGHSTAGE_HOST}/ts/ts/search.aspx"
HIGHSTAGE_UNC_ROOT = r"\\highstage\files"


def get_part_unc_path(part_id: str, session: requests.Session) -> Path | None:
    """Query the Highstage search API and return the UNC folder path for a part."""
    url = (
        f"{HIGHSTAGE_SEARCH_URL}?t=part&o={part_id}&_format=xml_raw&_max=*"
        f"&_columns=o%3Bfolderrelativepath"
    )
    try:
        resp = session.get(url, verify=False, timeout=120)
        resp.raise_for_status()
        root = ET.fromstring(resp.text.strip())
        rows = [dict(row.attrib) for row in root.iter("row")]
    except Exception as e:
        return None

    if not rows:
        return None
    folder_path = rows[0].get("folderrelativepath", "")
    if not folder_path:
        return None
    return Path(HIGHSTAGE_UNC_ROOT) / folder_path.replace("/", "\\").lstrip("\\")


def probe_part(highstage_id: str, session: requests.Session) -> dict:
    """Check if a part's datasheet folder is accessible via API + UNC."""
    result = {'highstage_id': highstage_id, 'unc_path': None, 'accessible': False, 'pdfs': []}
    folder = get_part_unc_path(highstage_id, session)
    if folder is None:
        result['error'] = 'not found in Highstage'
        return result
    result['unc_path'] = str(folder)
    try:
        if folder.exists():
            pdfs = [f.name for f in folder.rglob("*.pdf")]
            result['accessible'] = True
            result['pdfs'] = pdfs
    except Exception as e:
        result['error'] = str(e)
    return result


def main():
    ap = argparse.ArgumentParser(description="Probe datasheet access for schematic components")
    ap.add_argument("schematic_folder", help="Path to schematic workspace folder")
    ap.add_argument("--download", action="store_true", help="Download inaccessible datasheets locally")
    args = ap.parse_args()

    folder = Path(args.schematic_folder)
    yaml_path = folder / "REVIEW" / "schematic.yaml"

    if not yaml_path.exists():
        print(f"Error: {yaml_path} not found. Run schematic_builder.py first.")
        sys.exit(1)

    with open(yaml_path, encoding='utf-8') as f:
        schematic = yaml.safe_load(f)

    # Collect unique highstage_ids with their refdes
    id_to_refs = {}
    for ref, comp in schematic.get('components', {}).items():
        hid = comp.get('highstage_id', '').strip()
        if hid:
            id_to_refs.setdefault(hid, []).append(ref)

    if not id_to_refs:
        print("No highstage_ids found in schematic.yaml")
        print("Tip: Run the BOM extractor first to populate highstage_ids")
        sys.exit(0)

    print(f"Probing {len(id_to_refs)} unique parts...")

    session = requests.Session()
    session.auth = HttpNegotiateAuth()

    results = {}
    accessible_count = 0
    for hid, refs in sorted(id_to_refs.items()):
        r = probe_part(hid, session)
        r['refs'] = refs
        results[hid] = r
        status = "✓" if r['accessible'] else "✗"
        pdfs = r.get('pdfs', [])
        pdf_name = pdfs[0] if pdfs else "(no PDF found)"
        print(f"  {status} {hid} ({', '.join(refs[:3])}{'...' if len(refs) > 3 else ''}): {pdf_name}")
        if r['accessible']:
            accessible_count += 1

    session.close()

    # Determine access mode
    if accessible_count == len(id_to_refs):
        access_mode = 'unc'
    elif accessible_count == 0:
        access_mode = 'local'
    else:
        access_mode = 'mixed'

    # Write datasheet_access.yaml
    output = {
        'generated': datetime.now(timezone.utc).isoformat(),
        'access_mode': access_mode,
        'accessible': accessible_count,
        'total': len(id_to_refs),
        'parts': results,
    }
    out_path = folder / "REVIEW" / "datasheet_access.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False)

    print(f"\nAccess mode: {access_mode} ({accessible_count}/{len(id_to_refs)} accessible via UNC)")
    print(f"Written: {out_path}")

    # Update schematic.yaml meta (re-read under lock to avoid overwriting concurrent changes)
    _lock = FileLock(str(yaml_path) + ".lock", timeout=60)
    with _lock:
        with open(yaml_path, encoding='utf-8') as f:
            schematic = yaml.safe_load(f)
        schematic['meta']['datasheet_access'] = access_mode
        schematic['meta']['datasheet_probe_time'] = datetime.now(timezone.utc).isoformat()
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(schematic, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print(f"Updated: {yaml_path} (meta.datasheet_access = {access_mode})")


if __name__ == "__main__":
    main()
