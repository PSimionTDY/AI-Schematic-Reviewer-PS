"""
Highstage BOM Extractor Utility

Extracts Bill of Materials (BOM) information from Highstage part views.
Supports fetching, parsing, and exporting BOM data.

CLI usage:
    python highstage_bom_extractor.py bom.csv
    python highstage_bom_extractor.py bom.csv --update-schematic reviews/SCH25678-1E
    python highstage_bom_extractor.py bom.csv --update-schematic reviews/SCH25678-1E --out-csv out.csv
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml
from bs4 import BeautifulSoup
from filelock import FileLock

# ---------------------------------------------------------------------------
# DNP detection helpers
# ---------------------------------------------------------------------------

# Column names that directly signal DNP when their value is truthy/yes
_DNP_TRUE_COLS = {"DNP", "DO_NOT_POPULATE", "DO NOT POPULATE", "NO_POP", "NOPOP", "NOFIT"}

# Column names where "No" / "N" / "FALSE" means DNP (inverted sense)
_DNP_INVERTED_COLS = {"POPULATE", "FITTED", "FIT", "POPULATED"}

_YES = {"yes", "y", "true", "1", "x", "dnp"}
_NO = {"no", "n", "false", "0", ""}


def _normalise_col(name: str) -> str:
    """Uppercase and strip a column header for comparison."""
    return re.sub(r'[\s_\-]+', '_', name.strip().upper())


def detect_dnp(row: dict) -> Optional[bool]:
    """
    Inspect a BOM row dict and return True (DNP), False (populated), or None (unknown).

    Recognises these column patterns:
      Direct:   DNP, DO_NOT_POPULATE, NO_POP, NOFIT           → truthy value ⇒ DNP
      Inverted: POPULATE, FITTED, FIT, POPULATED              → falsy value ⇒ DNP
    """
    for raw_col, raw_val in row.items():
        col = _normalise_col(raw_col)
        val = raw_val.strip().upper() if raw_val else ""
        if col in _DNP_TRUE_COLS:
            return val in _YES
        if col in _DNP_INVERTED_COLS:
            return val in _NO
    return None


def read_bom_csv(csv_path: Path) -> List[Dict[str, str]]:
    """Read a BOM CSV and return a list of row dicts."""
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        return list(reader)


def extract_refdes_list(value: str) -> List[str]:
    """Split a REFDES cell that may contain comma/space-separated values."""
    return [r.strip() for r in re.split(r'[,;\s]+', value) if r.strip()]


def update_schematic_yaml(schematic_yaml: Path, bom_rows: List[Dict[str, str]]) -> int:
    """
    Cross-reference BOM rows into schematic.yaml.

    For each BOM row:
      - Splits REFDES into individual ref-designators
      - Sets component['highstage_id'] from the first matching ID column
      - Sets component['dnp'] from DNP column detection

    Returns the number of components updated.
    """
    # Identify the REFDES column (case-insensitive) before acquiring the lock
    refdes_col = None
    highstage_id_col = None
    if bom_rows:
        for col in bom_rows[0]:
            norm = _normalise_col(col)
            if norm in {"REFDES", "REF_DES", "REF_DESIGNATOR", "REFERENCE"}:
                refdes_col = col
            if norm in {"HIGHSTAGE_ID", "HIGHSTAGE", "PART_ID", "PART_NUMBER", "PN", "PART_NO"}:
                highstage_id_col = col

    _lock = FileLock(str(schematic_yaml) + ".lock", timeout=60)
    with _lock:
        with open(schematic_yaml, encoding='utf-8') as f:
            schematic = yaml.safe_load(f)

        components = schematic.setdefault('components', {})

        updated = 0
        for row in bom_rows:
            if not refdes_col:
                continue
            refs = extract_refdes_list(row.get(refdes_col, ''))
            dnp = detect_dnp(row)
            hid = row.get(highstage_id_col, '').strip() if highstage_id_col else ''

            for ref in refs:
                if ref not in components:
                    components[ref] = {}
                if hid:
                    components[ref]['highstage_id'] = hid
                if dnp is not None:
                    components[ref]['dnp'] = dnp
                updated += 1

        schematic.setdefault('meta', {})['bom_updated'] = datetime.utcnow().isoformat()
        with open(schematic_yaml, 'w', encoding='utf-8') as f:
            yaml.dump(schematic, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return updated


class HighstageBOMExtractor:
    """Extracts BOM data from Highstage part viewer."""
    
    def __init__(self, base_url: str, session: Optional[requests.Session] = None):
        """
        Initialize the extractor.
        
        Args:
            base_url: Base URL for Highstage instance (e.g., 'https://highstage')
            session: Optional requests Session for handling cookies/auth
        """
        self.base_url = base_url.rstrip('/')
        self.session = session or requests.Session()
    
    def fetch_part(self, part_id: str, verify_ssl: bool = False) -> Optional[str]:
        """
        Fetch a part view from Highstage.
        
        Args:
            part_id: Part object identifier (e.g., 'PCB_ASSY1017453-3')
            verify_ssl: Whether to verify SSL certificate (default: False for internal systems)
            
        Returns:
            HTML content of the part view, or None if fetch failed
        """
        url = f"{self.base_url}/ts/ts/view.aspx?t=part&o={part_id}"
        try:
            response = self.session.get(url, timeout=10, verify=verify_ssl)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            print(f"Error fetching part {part_id}: {e}")
            return None
    
    def extract_bom_from_html(self, html_content: str) -> List[Dict[str, str]]:
        """
        Extract BOM table data from HTML content.
        
        Args:
            html_content: HTML string from Highstage part view
            
        Returns:
            List of dictionaries representing BOM line items
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        bom_items = []
        
        # Look for tables that might contain BOM data
        tables = soup.find_all('table')
        
        for table in tables:
            # Check if this looks like a BOM table (contains relevant headers)
            headers = []
            header_row = table.find('tr')
            
            if header_row:
                for th in header_row.find_all(['th', 'td']):
                    text = th.get_text(strip=True).lower()
                    if any(keyword in text for keyword in ['part', 'qty', 'quantity', 'description', 'refdes']):
                        headers = [th.get_text(strip=True) for th in header_row.find_all(['th', 'td'])]
                        break
            
            if headers:
                # Extract rows
                rows = table.find_all('tr')[1:]  # Skip header row
                for row in rows:
                    cells = row.find_all(['td', 'th'])
                    if cells:
                        item = {}
                        for i, cell in enumerate(cells):
                            if i < len(headers):
                                item[headers[i]] = cell.get_text(strip=True)
                        if item:
                            bom_items.append(item)
        
        return bom_items
    
    def get_bom(self, part_id: str, verify_ssl: bool = False) -> Optional[List[Dict[str, str]]]:
        """
        Fetch and extract BOM for a given part.
        
        Args:
            part_id: Part object identifier
            verify_ssl: Whether to verify SSL certificate
            
        Returns:
            List of BOM items, or None if extraction failed
        """
        html = self.fetch_part(part_id, verify_ssl=verify_ssl)
        if html:
            return self.extract_bom_from_html(html)
        return None
    
    def export_to_csv(self, bom_items: List[Dict[str, str]], output_file: str) -> bool:
        """
        Export BOM items to CSV file.
        
        Args:
            bom_items: List of BOM dictionaries
            output_file: Path to output CSV file
            
        Returns:
            True if successful, False otherwise
        """
        if not bom_items:
            print("No BOM items to export")
            return False
        
        try:
            fieldnames = set()
            for item in bom_items:
                fieldnames.update(item.keys())
            fieldnames = sorted(list(fieldnames))
            
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(bom_items)
            
            print(f"BOM exported to {output_file}")
            return True
        except IOError as e:
            print(f"Error writing CSV: {e}")
            return False
    
    def export_to_json(self, bom_items: List[Dict[str, str]], output_file: str) -> bool:
        """
        Export BOM items to JSON file.
        
        Args:
            bom_items: List of BOM dictionaries
            output_file: Path to output JSON file
            
        Returns:
            True if successful, False otherwise
        """
        if not bom_items:
            print("No BOM items to export")
            return False
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(bom_items, f, indent=2)
            
            print(f"BOM exported to {output_file}")
            return True
        except IOError as e:
            print(f"Error writing JSON: {e}")
            return False


def main():
    ap = argparse.ArgumentParser(description="Highstage BOM extractor and schematic updater")
    ap.add_argument("bom_csv", nargs='?', help="BOM CSV file to process")
    ap.add_argument("--update-schematic", metavar="SCHEMATIC_FOLDER",
                    help="Cross-reference BOM into REVIEW/schematic.yaml in this workspace folder")
    ap.add_argument("--out-csv", metavar="PATH", help="Export processed BOM to this CSV file")
    ap.add_argument("--out-json", metavar="PATH", help="Export processed BOM to this JSON file")
    ap.add_argument("--part-id", metavar="PART_ID",
                    help="Fetch a single part BOM from Highstage (e.g. PCB_ASSY1017453-3)")
    args = ap.parse_args()

    # ------ Highstage fetch mode ------
    if args.part_id:
        HIGHSTAGE_URL = os.environ.get("HIGHSTAGE_HOST", "https://highstage.tdy.teledyne.com")
        output_dir = Path("bom_exports")
        output_dir.mkdir(exist_ok=True)
        extractor = HighstageBOMExtractor(HIGHSTAGE_URL)
        print(f"Fetching BOM for {args.part_id}...")
        bom_items = extractor.get_bom(args.part_id, verify_ssl=False)
        if bom_items:
            print(f"Found {len(bom_items)} BOM items")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            if args.out_csv:
                extractor.export_to_csv(bom_items, args.out_csv)
            else:
                extractor.export_to_csv(bom_items, str(output_dir / f"bom_{args.part_id}_{ts}.csv"))
            if args.out_json:
                extractor.export_to_json(bom_items, args.out_json)
            print("\nFirst few BOM items:")
            for item in bom_items[:5]:
                print(f"  {item}")
        else:
            print("Failed to extract BOM")
            sys.exit(1)
        return

    # ------ CSV processing mode ------
    if not args.bom_csv:
        ap.print_help()
        sys.exit(1)

    bom_path = Path(args.bom_csv)
    if not bom_path.exists():
        print(f"Error: BOM file not found: {bom_path}")
        sys.exit(1)

    print(f"Reading BOM: {bom_path}")
    bom_rows = read_bom_csv(bom_path)
    print(f"  {len(bom_rows)} rows loaded")

    # Annotate DNP flags in output
    for row in bom_rows:
        dnp = detect_dnp(row)
        row['_dnp'] = '' if dnp is None else ('YES' if dnp else 'NO')

    dnp_count = sum(1 for r in bom_rows if r.get('_dnp') == 'YES')
    print(f"  DNP components detected: {dnp_count}")

    if args.out_csv:
        out = Path(args.out_csv)
        fieldnames = list(bom_rows[0].keys()) if bom_rows else []
        with open(out, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(bom_rows)
        print(f"  Written: {out}")

    if args.out_json:
        with open(args.out_json, 'w', encoding='utf-8') as f:
            json.dump(bom_rows, f, indent=2)
        print(f"  Written: {args.out_json}")

    if args.update_schematic:
        sch_folder = Path(args.update_schematic)
        yaml_path = sch_folder / "REVIEW" / "schematic.yaml"
        if not yaml_path.exists():
            print(f"Error: {yaml_path} not found. Run schematic_builder.py first.")
            sys.exit(1)
        updated = update_schematic_yaml(yaml_path, bom_rows)
        print(f"  Updated {updated} component entries in {yaml_path}")


if __name__ == "__main__":
    main()
