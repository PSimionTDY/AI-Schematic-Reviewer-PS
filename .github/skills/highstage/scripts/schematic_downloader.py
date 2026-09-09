#!/usr/bin/env python3
"""
Download a schematic from Highstage to a local workspace.

Search strategy:
  1. Strip revision suffix and search Highstage by base number (e.g. SCH26782).
  2. Highstage returns a UNC path (typically pointing to the latest revision folder).
  3. Swap the revision folder name in that UNC path to match the requested revision.

Supported formats:
    SCH25678        -> latest revision (whatever Highstage returns)
    SCH25678-1      -> major revision 1
    SCH25678-1A     -> major revision 1, minor letter A
"""
import argparse
import os
import re
import shutil
import sys
import urllib3
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from requests_negotiate_sspi import HttpNegotiateAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HIGHSTAGE_HOST = os.environ.get("HIGHSTAGE_HOST", "https://highstage.tdy.teledyne.com")
HIGHSTAGE_SEARCH_URL = f"{_HIGHSTAGE_HOST}/ts/ts/search.aspx"
HIGHSTAGE_UNC_ROOT = r"\\highstage\files"

_REVISION_RE = re.compile(r"^(.+?)-(\d+)([A-Z]?)$", re.IGNORECASE)


def parse_schematic_id(sch_id: str):
    """Return (base, major, minor) — major is None if no revision given."""
    m = _REVISION_RE.match(sch_id)
    if m:
        return m.group(1).upper(), m.group(2), m.group(3).upper()
    return sch_id.upper(), None, ""


def search_highstage(base_id: str) -> list:
    """Search Highstage by base schematic number, returns list of result dicts."""
    url = (
        f"{HIGHSTAGE_SEARCH_URL}?t=doc&o={base_id}&_format=xml_raw&_max=*"
        f"&_columns=o%3Btitle%3Bfilename%3Bfolderrelativepath%3Bstatus%3Btype%3Bdescription%3Bworkspace"
    )
    try:
        resp = requests.get(url, auth=HttpNegotiateAuth(), verify=False, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Highstage search failed. Are you on the company network?\n{e}")
    try:
        root = ET.fromstring(resp.text.strip())
        return [dict(row.attrib) for row in root.iter("row")]
    except ET.ParseError as e:
        raise RuntimeError(f"Could not parse Highstage response: {e}\n{resp.text[:200]}")


def get_unc_path(folder_relative_path: str) -> Path:
    return Path(HIGHSTAGE_UNC_ROOT) / folder_relative_path.replace("/", "\\").lstrip("\\")


def resolve_unc(unc_from_search: Path, base_id: str, major: str, minor: str) -> Path:
    """
    Highstage returns the UNC path of the latest revision folder, e.g.
    \\\\highstage\\files\\...\\SCH26782\\SCH26782-2

    If a specific revision was requested, swap the last segment for the
    requested one (e.g. SCH26782-1 or SCH26782-1A).

    Note: Highstage tracks the minor revision letter (e.g. the 'C' in SCH26913-1C)
    in its database, but the file share folder is typically just the major revision
    (e.g. SCH26913-1).  When the exact folder is not found, fall back to the
    major-revision folder that Highstage already pointed to.
    """
    if major is None:
        return unc_from_search  # no revision requested, use as-is

    parent = unc_from_search.parent
    requested_suffix = f"-{major}{minor}"  # e.g. "-1" or "-1A" or "-1C"

    # Build the expected folder name: base + suffix  (e.g. SCH26782-1A)
    target_name = f"{base_id}{requested_suffix}"
    target = parent / target_name

    if target.exists():
        if target != unc_from_search:
            print(f"Using revision: {target.name} (latest on Highstage: {unc_from_search.name})")
        return target

    # Try case-insensitive scan of parent for a matching suffix
    if parent.exists():
        siblings = sorted([d for d in parent.iterdir() if d.is_dir()])
        match = next((d for d in siblings if d.name.upper().endswith(requested_suffix.upper())), None)
        if match:
            print(f"Using revision: {match.name}")
            return match

        # The revision letter may be tracked in the Highstage DB only (not as a folder).
        # If the search already pointed to the correct major-revision folder, use it.
        if minor and unc_from_search.exists():
            major_suffix = f"-{major}"
            if unc_from_search.name.upper().endswith(major_suffix.upper()):
                print(f"Note: revision letter '{minor}' is tracked in Highstage only — "
                      f"folder on file share is '{unc_from_search.name}' (rev {minor} of that folder).")
                return unc_from_search

        available = ", ".join(d.name for d in siblings)
        print(f"Revision '{target_name}' not found. Available: {available}")
        print(f"Using: {unc_from_search.name}")

    return unc_from_search


def list_revisions(unc_folder: Path) -> list:
    parent = unc_folder.parent
    if parent.exists():
        return sorted([d.name for d in parent.iterdir() if d.is_dir()])
    return []


def setup_workspace(dest: Path, schematic_id: str) -> Path:
    workspace = dest / schematic_id
    (workspace / "allegro").mkdir(parents=True, exist_ok=True)
    for sub in ("IC", "RES", "CAP", "history"):
        (workspace / "REVIEW" / sub).mkdir(parents=True, exist_ok=True)
    return workspace


def copy_schematic(source_unc: Path, workspace: Path) -> None:
    allegro_dst = workspace / "allegro"

    copied_dat = 0
    for src_dir in [source_unc / "allegro", source_unc]:
        if src_dir.exists():
            for f in src_dir.glob("*.dat"):
                shutil.copy2(f, allegro_dst / f.name)
                copied_dat += 1
        if copied_dat:
            break
    if copied_dat == 0:
        for sub in (d for d in source_unc.iterdir() if d.is_dir()):
            if sub.name.lower() == "allegro":
                for f in sub.glob("*.dat"):
                    shutil.copy2(f, allegro_dst / f.name)
                    copied_dat += 1
    print(f"  Copied {copied_dat} Allegro .dat files")

    for pdf in source_unc.glob("*.pdf"):
        shutil.copy2(pdf, workspace / pdf.name)
        print(f"  Copied PDF: {pdf.name}")

    for pattern in ("*.dsn", "*.DSN", "*.opj", "*.OPJ"):
        for f in source_unc.glob(pattern):
            shutil.copy2(f, workspace / f.name)
            print(f"  Copied: {f.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Download schematic from Highstage")
    ap.add_argument("schematic_id", help="e.g. SCH25678  SCH25678-1  SCH25678-1A")
    ap.add_argument("--dest", default="reviews", help="Destination parent folder (default: reviews/)")
    ap.add_argument("--list-only", action="store_true", help="List available revisions without downloading")
    args = ap.parse_args()

    dest = Path(args.dest)
    sch_id = args.schematic_id.upper()
    base_id, major, minor = parse_schematic_id(sch_id)

    # Search by full revision ID when one is specified (e.g. SCH26913-1B for
    # unapproved/working-draft documents), or by base ID to get the latest revision.
    search_id = sch_id if major is not None else base_id
    print(f"Searching Highstage for '{search_id}'...")
    try:
        results = search_highstage(search_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        print(f"Copy the schematic folder manually to: {dest / sch_id}")
        sys.exit(1)

    if not results:
        print(f"No results found for '{search_id}' on Highstage.")
        sys.exit(1)

    if len(results) > 1:
        print(f"Found {len(results)} results:")
        for r in results:
            print(f"  {r.get('o','?'):20} {r.get('title',''):40} [{r.get('workspace','')}]")

    result = results[0]
    folder_path = result.get("folderrelativepath", "")
    unc_from_search = get_unc_path(folder_path)
    print(f"Found: {result.get('title', base_id)}")
    print(f"UNC:   {unc_from_search}")

    if not unc_from_search.exists():
        print(f"Error: UNC path not accessible: {unc_from_search}")
        print("Check you are on the company network.")
        sys.exit(1)

    if args.list_only:
        revisions = list_revisions(unc_from_search)
        print(f"Revisions: {', '.join(revisions) if revisions else '(none)'}")
        return

    source_unc = resolve_unc(unc_from_search, base_id, major, minor)
    # Use the requested ID as the workspace name so SCH26913-1C → reviews/SCH26913-1C
    # (not the bare folder name SCH26913-1, which could be any revision).
    final_id = sch_id  # keep what the user asked for

    print(f"\nSetting up workspace: {dest / final_id}")
    workspace = setup_workspace(dest, final_id)
    print("Copying files...")
    copy_schematic(source_unc, workspace)
    print(f"\nDone! Workspace ready at: {workspace.resolve()}")
    print(f"Next: venv\\Scripts\\python.exe .github\\skills\\schematic-parser\\scripts\\schematic_builder.py {workspace}")


if __name__ == "__main__":
    main()
