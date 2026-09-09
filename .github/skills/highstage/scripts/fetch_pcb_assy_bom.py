"""
Fetch BOM from a Highstage PCB_ASSY and write highstage_id / dnp flags to review.db.

Workflow:
  1. Find the PCB_ASSY(s) linked to a schematic via the Highstage "references" API.
  2. Fetch the PCB_ASSY BOM (structured line items with ts_ref.pos ref-des positions).
  3. Build a refdes → Highstage ID map.
  4. Sanity-check: every non-passive, non-test-point schematic ref-des must appear in
     the BOM; XPCB1007755 entries are no-mounts (DNP=1).
  5. Write highstage_id and dnp flags back into review.db.
  6. If the BOM is absent, stale (missing >5% of schematic refs), or the sanity check
     fails badly, print a warning and ask the caller to supply a valid BOM.

Usage:
    python fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db
    python fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db --assy PCB_ASSY1020244-1A
    python fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db --list-assys

XPCB1007755 is the Highstage "no-mount" placeholder part.  Any ref-des that appears
in the BOM under XPCB1007755 is a DNP (Do Not Populate) component.
"""

import argparse
import os
import sqlite3
import sys
import urllib3
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from requests_negotiate_sspi import HttpNegotiateAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HIGHSTAGE_HOST = os.environ.get("HIGHSTAGE_HOST", "https://highstage.tdy.teledyne.com")
HIGHSTAGE_SEARCH = f"{_HIGHSTAGE_HOST}/ts/ts/search.aspx"
NO_MOUNT_PART = "XPCB1007755"

# Component types that are mechanical / test-only and may not appear in the BOM
_SKIP_TYPES = {"test_point", "mounting_hole", "other", "fiducial"}


def _search(params: dict) -> list[dict]:
    """Run a Highstage search API call and return rows as dicts."""
    url = HIGHSTAGE_SEARCH
    try:
        resp = requests.get(
            url, params=params,
            auth=HttpNegotiateAuth(), verify=False, timeout=120,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Highstage API error: {e}")
    try:
        root = ET.fromstring(resp.text.strip())
        return [dict(r.attrib) for r in root.iter("row")]
    except ET.ParseError as e:
        raise RuntimeError(f"Could not parse Highstage response: {e}\n{resp.text[:200]}")


def find_pcb_assys(sch_id: str) -> list[dict]:
    """Return PCB_ASSY items that reference this schematic."""
    rows = _search({
        "t": "part",
        "_references": sch_id,
        "_format": "xml_raw",
        "_columns": "o;name;type;description;item;workspace;folderrelativepath;status",
        "_max": "*",
    })
    return [r for r in rows if r.get("type") == "PCB_ASSY"]


def fetch_bom(assy_id: str) -> tuple[dict[str, str], set[str]]:
    """
    Fetch BOM for a PCB_ASSY.

    Returns:
        ref_to_id   dict[refdes -> highstage_id]   (excluding no-mounts)
        dnp_refs    set[refdes]                     (XPCB1007755 no-mounts)
    """
    rows = _search({
        "t": "part",
        "_parenttype": "part",
        "_parent": assy_id,
        "_format": "xml_raw",
        "_columns": "name;ts_ref.pos;ts_ref.qty;description;type;item",
        "_max": "*",
    })
    ref_to_id: dict[str, str] = {}
    dnp_refs: set[str] = set()
    for row in rows:
        name = row.get("name", "")
        pos_str = row.get("ts_ref.pos", "").strip()
        if not pos_str:
            continue
        refs = pos_str.split()
        if name == NO_MOUNT_PART:
            dnp_refs.update(refs)
        else:
            for ref in refs:
                ref_to_id[ref] = name
    return ref_to_id, dnp_refs


def sanity_check(
    ref_to_id: dict[str, str],
    dnp_refs: set[str],
    db_components: list[dict],
) -> tuple[list[str], list[str]]:
    """
    Compare BOM ref-des against schematic components.

    Returns:
        missing_in_bom   refs present in schematic but absent from BOM (and not DNP)
        extra_in_bom     refs present in BOM but absent from schematic
    """
    sch_refs = {
        c["ref"] for c in db_components
        if c.get("comp_type") not in _SKIP_TYPES and not c.get("dnp")
    }
    bom_refs = set(ref_to_id.keys()) | dnp_refs

    missing_in_bom = sorted(sch_refs - bom_refs)
    extra_in_bom = sorted(bom_refs - set(c["ref"] for c in db_components))
    return missing_in_bom, extra_in_bom


def apply_to_db(
    db_path: Path,
    ref_to_id: dict[str, str],
    dnp_refs: set[str],
) -> int:
    """Write highstage_id and dnp flags into review.db. Returns number of rows updated."""
    conn = sqlite3.connect(str(db_path))
    updated = 0
    try:
        for ref, hid in ref_to_id.items():
            cur = conn.execute(
                "UPDATE components SET highstage_id=? WHERE ref=?", (hid, ref)
            )
            updated += cur.rowcount
        for ref in dnp_refs:
            conn.execute("UPDATE components SET dnp=1 WHERE ref=?", (ref,))
        conn.commit()
    finally:
        conn.close()
    return updated


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch PCB_ASSY BOM and populate review.db")
    ap.add_argument("db", help="Path to review.db")
    ap.add_argument("--assy", help="Specific PCB_ASSY ID to use (e.g. PCB_ASSY1020244-1A)")
    ap.add_argument("--list-assys", action="store_true", help="List linked PCB_ASSYs and exit")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be written, don't modify DB")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: DB not found: {db_path}")
        return 2

    # Read meta + components from DB
    conn = sqlite3.connect(str(db_path))
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    db_comps = [
        {"ref": r[0], "comp_type": r[1], "dnp": r[2]}
        for r in conn.execute("SELECT ref, comp_type, dnp FROM components").fetchall()
    ]
    conn.close()

    sch_id = meta.get("schematic_id", "")
    if not sch_id:
        print("Error: schematic_id not found in review.db meta table.")
        return 2

    # Find PCB_ASSYs linked to this schematic
    print(f"Searching Highstage for PCB_ASSYs referencing {sch_id}...")
    try:
        assys = find_pcb_assys(sch_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1

    if not assys:
        print(f"No PCB_ASSY found referencing {sch_id} on Highstage.")
        print("Please provide a BOM CSV manually (refdes,highstage_id columns) and re-run.")
        return 1

    if args.list_assys:
        print(f"PCB_ASSYs referencing {sch_id}:")
        for a in assys:
            status_map = {"1": "draft", "2": "review", "3": "approved", "5": "released"}
            st = status_map.get(a.get("status", ""), a.get("status", "?"))
            print(f"  {a.get('item','?'):30} {st:10} {a.get('description','')[:60]}")
        return 0

    # Select which assembly to use
    if args.assy:
        assy_id = args.assy.upper()
        if not any(a.get("item", "").upper() == assy_id for a in assys):
            print(f"Warning: {assy_id} not found in references for {sch_id}.")
    else:
        # Prefer the assembly whose description best matches the schematic description.
        # Fall back to approved/released status, then first result.
        sch_desc = (meta.get("description") or "").lower()
        def _score(a: dict) -> tuple:
            desc = a.get("description", "").lower()
            # Word overlap score
            sch_words = set(sch_desc.split())
            assy_words = set(desc.split())
            overlap = len(sch_words & assy_words) / max(len(sch_words | assy_words), 1)
            status_rank = {"5": 2, "3": 1}.get(a.get("status", ""), 0)
            return (overlap, status_rank, a.get("date1", ""))

        assys_sorted = sorted(assys, key=_score, reverse=True)
        chosen = assys_sorted[0]
        assy_id = chosen.get("item", "")
        if len(assys) > 1:
            print(f"Multiple PCB_ASSYs found. Using best match: {assy_id} ({chosen.get('description','')[:50]})")
            print("  Use --list-assys to see all options, --assy <ID> to override.")

    print(f"Fetching BOM from {assy_id}...")
    try:
        ref_to_id, dnp_refs = fetch_bom(assy_id)
    except RuntimeError as e:
        print(f"Error fetching BOM: {e}")
        return 1

    if not ref_to_id and not dnp_refs:
        print(f"BOM for {assy_id} returned no ref-des entries.")
        print("The PCB_ASSY may not have been frozen/released yet.")
        print("Please ask the designer to freeze the BOM, or provide a BOM CSV manually.")
        return 1

    print(f"BOM: {len(ref_to_id)} populated refs, {len(dnp_refs)} DNP (no-mount) refs")

    # Sanity check
    missing, extra = sanity_check(ref_to_id, dnp_refs, db_comps)
    coverage = (len(ref_to_id) + len(dnp_refs)) / max(len(db_comps), 1)

    if missing:
        print(f"\nWARNING: {len(missing)} schematic refs missing from BOM:")
        for r in missing[:20]:
            print(f"  {r}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")

    if extra:
        print(f"\nWARNING: {len(extra)} BOM refs not in schematic:")
        for r in extra[:10]:
            print(f"  {r}")

    if coverage < 0.85:
        print(f"\nERROR: BOM coverage is only {coverage:.0%} of schematic components.")
        print("The BOM may be outdated or from the wrong revision.")
        print("Please verify the correct PCB_ASSY is linked, or supply a BOM manually.")
        return 1

    if missing and len(missing) / max(len(db_comps), 1) > 0.05:
        print(f"\nWARNING: More than 5% of schematic refs are missing from BOM.")
        print("Proceeding, but please review the missing refs above.")

    if args.dry_run:
        print("\nDry run — no changes written to DB.")
        return 0

    # Apply to DB
    updated = apply_to_db(db_path, ref_to_id, dnp_refs)
    print(f"\nUpdated {updated} components in review.db with Highstage IDs.")
    if dnp_refs:
        print(f"Marked {len(dnp_refs)} components as DNP: {sorted(dnp_refs)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
