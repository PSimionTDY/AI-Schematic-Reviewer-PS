"""
Highstage Datasheet Downloader

Downloads component datasheets from Highstage.
Queries the Highstage search API to locate the part folder, then copies
files directly from the UNC file share (fast, no browser needed).
Falls back to HTTP-based scraping if the file share is unavailable.

Usage:
    # Download datasheet for a part
    python highstage_downloader.py IC1008360
    python highstage_downloader.py DIS1019629    # transistors (DIS prefix)
    python highstage_downloader.py DIO1019495    # diodes (DIO prefix)

    # List available datasheets without downloading
    python highstage_downloader.py IC1008360 --list

    # Force HTTP-based download
    python highstage_downloader.py IC1008360 --http

    # Download from BOM
    python highstage_downloader.py --bom bom.csv --filter "U,IC"
"""

import os
import sys
import csv
import shutil
import argparse
import urllib3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass

import requests
from requests_negotiate_sspi import HttpNegotiateAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_HIGHSTAGE_HOST = os.environ.get("HIGHSTAGE_HOST", "https://highstage.tdy.teledyne.com")
HIGHSTAGE_SEARCH_URL = f"{_HIGHSTAGE_HOST}/ts/ts/search.aspx"
HIGHSTAGE_UNC_ROOT = r"\\highstage\files"
HIGHSTAGE_URL = _HIGHSTAGE_HOST


@dataclass
class DownloadResult:
    """Result of a download operation."""
    success: bool
    filename: str = ""
    filepath: Optional[str] = None
    error: Optional[str] = None
    size_bytes: int = 0


# =============================================================================
# HIGHSTAGE SEARCH API
# =============================================================================

def search_highstage_part(part_id: str) -> list:
    """Query the Highstage search API for a part, returning a list of result dicts.

    Each dict may contain: o, title, filename, folderrelativepath, status, type,
    description, workspace.

    If the part is a "working draft" (not yet approved), automatically retries
    with the ``-1A`` minor-version suffix that Highstage uses for draft records.

    Raises RuntimeError if the network request fails.
    """
    cols = "o%3Btitle%3Bfilename%3Bfolderrelativepath%3Bstatus%3Btype%3Bdescription%3Bworkspace"

    def _query(pid: str) -> list:
        url = f"{HIGHSTAGE_SEARCH_URL}?t=part&o={pid}&_format=xml_raw&_max=*&_columns={cols}"
        resp = requests.get(url, auth=HttpNegotiateAuth(), verify=False, timeout=120)
        if not resp.ok:
            return []
        try:
            root = ET.fromstring(resp.text.strip())
            return [dict(row.attrib) for row in root.iter("row")]
        except ET.ParseError:
            return []

    results = _query(part_id)
    if results:
        return results

    # Highstage returns an error page (not XML) for unapproved parts when
    # queried without a minor-version suffix.  Retry with "-1A".
    if not part_id.endswith("-1A"):
        results = _query(part_id + "-1A")
        if results:
            print(f"  [info] {part_id} is a working draft — found via {part_id}-1A")
            return results

    return []


def get_part_unc_path(part_id: str) -> Optional[Path]:
    """Return the UNC path for a part by querying the Highstage search API.

    Returns None if the part is not found or the API call fails.
    """
    try:
        results = search_highstage_part(part_id)
    except Exception as e:
        print(f"  [warn] Highstage API error for {part_id}: {e}")
        return None

    if not results:
        return None

    folder_path = results[0].get("folderrelativepath", "")
    if not folder_path:
        return None

    return Path(HIGHSTAGE_UNC_ROOT) / folder_path.replace("/", "\\").lstrip("\\")


# =============================================================================
# FILE-BASED DOWNLOADER (Primary - No browser needed)
# =============================================================================

def search_highstage_doc(doc_id: str) -> list:
    """Query the Highstage search API for a document (e.g. PARTD23863-1A)."""
    cols = "o%3Bfilename%3Bfolderrelativepath%3Btype%3Bdescription%3Bworkspace"
    url = f"{HIGHSTAGE_SEARCH_URL}?t=doc&o={doc_id}&_format=xml_raw&_max=*&_columns={cols}"
    try:
        resp = requests.get(url, auth=HttpNegotiateAuth(), verify=False, timeout=120)
        if not resp.ok:
            return []
        root = ET.fromstring(resp.text.strip())
        return [dict(row.attrib) for row in root.iter("row")]
    except Exception:
        return []


def find_partd_for_part(part_id: str) -> List[Path]:
    """Find PDFs in PARTD documents linked from a part's Highstage page.

    Some components (e.g. FPGAs) store their datasheets in a separate PARTD
    document rather than directly in the IC part folder.  This function scrapes
    the part page for ``PARTD…`` references, resolves each one to a UNC path,
    and returns all PDFs found there.
    """
    part_page_url = f"https://highstage/ts/ts/view.aspx?t=part&o={part_id}"
    try:
        resp = requests.get(part_page_url, auth=HttpNegotiateAuth(), verify=False, timeout=120)
        if not resp.ok:
            return []
    except Exception:
        return []

    import re
    partd_ids = list(set(re.findall(r"PARTD\d+-\d+[A-Z]*", resp.text)))
    if not partd_ids:
        return []

    pdfs: List[Path] = []
    for partd_id in partd_ids:
        rows = search_highstage_doc(partd_id)
        for row in rows:
            folder = row.get("folderrelativepath", "")
            if not folder:
                continue
            unc = Path(HIGHSTAGE_UNC_ROOT) / folder.replace("/", "\\").lstrip("\\")
            if unc.exists():
                pdfs.extend(unc.rglob("*.pdf"))
                print(f"  [info] Found PARTD {partd_id} → {unc} ({len(list(unc.rglob('*.pdf')))} PDFs)")
    return pdfs


def find_datasheets_file(part_id: str) -> List[Path]:
    """Find all PDF datasheets for a part by querying the Highstage API for the UNC path.

    Checks the part's own folder first; if empty, falls back to any linked PARTD
    documents (used for FPGAs and other complex components).

    Args:
        part_id: Highstage part ID (e.g., IC1008360, DIO1019495, DIS1019629)

    Returns:
        List of Path objects to PDF files
    """
    part_dir = get_part_unc_path(part_id)
    if part_dir is not None and part_dir.exists():
        pdfs = list(part_dir.rglob("*.pdf"))
        if pdfs:
            return pdfs

    # Fall back to PARTD documents linked from the part page
    partd_pdfs = find_partd_for_part(part_id)
    if partd_pdfs:
        return partd_pdfs

    return []


def download_via_file_share(part_id: str, output_dir: str = "datasheets") -> List[DownloadResult]:
    """Download datasheets via direct file share access (API lookup → UNC copy).

    Args:
        part_id: Highstage part ID
        output_dir: Directory to save files

    Returns:
        List of DownloadResult objects
    """
    output_path = Path(output_dir) / part_id
    output_path.mkdir(parents=True, exist_ok=True)

    results = []

    print(f"Finding datasheets for {part_id}...")
    pdfs = find_datasheets_file(part_id)

    if not pdfs:
        print(f"No datasheets found for {part_id}")
        return results

    print(f"Found {len(pdfs)} PDF(s)")

    seen_names: set = set()
    for pdf in pdfs:
        filename = pdf.name
        if filename in seen_names:
            continue
        seen_names.add(filename)

        dest = output_path / filename
        if dest.exists():
            print(f"  Skipped (exists): {filename}")
            results.append(DownloadResult(
                success=True, filename=filename, filepath=str(dest),
                size_bytes=dest.stat().st_size,
            ))
            continue

        try:
            shutil.copy2(pdf, dest)
            size_bytes = dest.stat().st_size
            print(f"  Downloaded: {filename} ({size_bytes/1024:.1f} KB)")
            results.append(DownloadResult(
                success=True, filename=filename, filepath=str(dest),
                size_bytes=size_bytes,
            ))
        except Exception as e:
            print(f"  Failed: {filename} - {e}")
            results.append(DownloadResult(success=False, filename=filename, error=str(e)))

    return results


def is_file_share_available() -> bool:
    """Check if the Highstage UNC root is accessible."""
    return Path(HIGHSTAGE_UNC_ROOT).exists()



# =============================================================================
# HTTP-BASED DOWNLOADER (Fallback - Uses requests with SSPI auth)
# =============================================================================

class HttpDownloader:
    """Downloads datasheets using HTTP requests with Windows authentication."""
    
    def __init__(self, output_dir: str = "datasheets"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = None
    
    def _init_session(self):
        """Initialize authenticated HTTP session."""
        if self.session:
            return
        
        import truststore
        truststore.inject_into_ssl()
        
        from requests_negotiate_sspi import HttpNegotiateAuth
        import requests
        
        self.session = requests.Session()
        self.session.auth = HttpNegotiateAuth()
        
        # Test authentication
        print(f"Connecting to Highstage: {HIGHSTAGE_URL}")
        response = self.session.get(HIGHSTAGE_URL, timeout=120)
        response.raise_for_status()
        print("Authentication successful.")
    
    def _close_session(self):
        """Close the HTTP session."""
        if self.session:
            self.session.close()
            self.session = None
    
    def download(self, part_id: str) -> List[DownloadResult]:
        """Download datasheets for a part using HTTP."""
        self._init_session()
        
        from bs4 import BeautifulSoup
        
        results = []
        
        part_url = f"{HIGHSTAGE_URL}/ts/ts/view.aspx?t=part&o={part_id}"
        print(f"\nFetching part: {part_id}")
        
        response = self.session.get(part_url, timeout=120)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all PDF links
        links = soup.find_all('a')
        pdf_links = []
        
        for link in links:
            href = link.get('href', '')
            text = link.get_text(strip=True)
            
            if '.pdf' in href.lower():
                if href.startswith('/'):
                    href = HIGHSTAGE_URL + href
                elif not href.startswith('http'):
                    href = HIGHSTAGE_URL + '/' + href
                
                filename = text if text else os.path.basename(href)
                if not filename.lower().endswith('.pdf'):
                    filename += '.pdf'
                
                # Sanitize filename
                for char in '<>:"/\\|?*':
                    filename = filename.replace(char, '_')
                
                pdf_links.append((filename, href))
                print(f"  Found: {filename}")
        
        if not pdf_links:
            print(f"  No datasheets found for {part_id}")
            return results
        
        # Download each PDF
        for filename, url in pdf_links:
            dest = self.output_dir / filename
            
            if dest.exists():
                print(f"  Already exists: {filename}")
                results.append(DownloadResult(
                    success=True,
                    filename=filename,
                    filepath=str(dest),
                    size_bytes=dest.stat().st_size
                ))
                continue
            
            print(f"  Downloading: {filename}")
            
            try:
                pdf_response = self.session.get(url, timeout=60)
                pdf_response.raise_for_status()
                
                with open(dest, 'wb') as f:
                    f.write(pdf_response.content)
                
                size_bytes = dest.stat().st_size
                print(f"  Downloaded: {filename} ({size_bytes/1024:.1f} KB)")
                
                results.append(DownloadResult(
                    success=True,
                    filename=filename,
                    filepath=str(dest),
                    size_bytes=size_bytes
                ))
                
            except Exception as e:
                print(f"  Failed: {filename} - {e}")
                results.append(DownloadResult(
                    success=False,
                    filename=filename,
                    error=str(e)
                ))
        
        return results
    
    def download_from_bom(self, bom_file: str, filter_types: List[str] = None, 
                          limit: int = None) -> Dict[str, List[DownloadResult]]:
        """Download datasheets for parts in a BOM."""
        print(f"\nLoading BOM: {bom_file}")
        
        parts = []
        with open(bom_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            parts = list(reader)
        
        if filter_types:
            parts = [p for p in parts if any(
                p.get('Pos', '').upper().startswith(t.upper()) 
                for t in filter_types
            )]
            print(f"Filtered to {len(parts)} parts matching types: {filter_types}")
        
        if limit:
            parts = parts[:limit]
            print(f"Limited to {len(parts)} parts")
        
        # Get unique part IDs
        unique_parts = {}
        for part in parts:
            part_id = part.get('Highstage Part') or part.get('Mfg Part') or part.get('MPN')
            if part_id and part_id not in unique_parts:
                unique_parts[part_id] = part
        
        print(f"\nFound {len(unique_parts)} unique parts to process")
        
        results = {}
        for i, (part_id, _) in enumerate(unique_parts.items(), 1):
            print(f"\n[{i}/{len(unique_parts)}] Processing: {part_id}")
            results[part_id] = self.download(part_id)
        
        return results
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self._close_session()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Download datasheets from Highstage"
    )
    parser.add_argument(
        "part_id",
        nargs="?",
        help="Highstage part ID (e.g., IC1008360)"
    )
    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available datasheets without downloading"
    )
    parser.add_argument(
        "--output", "-o",
        default="datasheets",
        help="Output directory (default: datasheets)"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Force HTTP-based download (works for non-IC parts)"
    )
    parser.add_argument(
        "--bom",
        help="Download datasheets for all parts in BOM file"
    )
    parser.add_argument(
        "--filter", "-f",
        help="Filter BOM by component type (e.g., 'U,IC')"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of parts to process from BOM"
    )
    
    args = parser.parse_args()
    
    # BOM mode
    if args.bom:
        filter_types = args.filter.split(',') if args.filter else None
        
        with HttpDownloader(output_dir=args.output) as dl:
            results = dl.download_from_bom(args.bom, filter_types, args.limit)
            
            # Summary
            total_downloaded = sum(1 for r in results.values() for d in r if d.success and d.filepath)
            total_failed = sum(1 for r in results.values() for d in r if not d.success)
            print(f"\n{'='*60}")
            print(f"Summary: {total_downloaded} downloaded, {total_failed} failed")
        
        return 0
    
    # Single part mode
    if not args.part_id:
        parser.print_help()
        return 1
    
    # Normalize part ID — uppercase only; do NOT force an IC prefix since parts
    # can be transistors (DIS…), diodes (DIO…), resistors (RES…), etc.
    part_id = args.part_id.upper()
    
    # List mode
    if args.list:
        pdfs = find_datasheets_file(part_id)
        if pdfs:
            print(f"Datasheets for {part_id}:")
            seen = set()
            for pdf in pdfs:
                if pdf.name not in seen:
                    seen.add(pdf.name)
                    size_kb = pdf.stat().st_size / 1024
                    print(f"  {pdf.name} ({size_kb:.1f} KB)")
        else:
            print(f"No datasheets found for {part_id}")
        return 0
    
    # Download mode - try file share first, then HTTP
    if not args.http and is_file_share_available():
        results = download_via_file_share(part_id, args.output)
        
        if results:
            downloaded = sum(1 for r in results if r.success and r.size_bytes > 0)
            skipped = sum(1 for r in results if r.success and r.filepath and Path(r.filepath).exists())
            failed = sum(1 for r in results if not r.success)
            
            print(f"\nSummary:")
            print(f"  Downloaded: {downloaded}")
            print(f"  Skipped:    {skipped - downloaded}")
            print(f"  Failed:     {failed}")
            return 0
    
    # Fall back to HTTP
    print("Using HTTP-based download...")
    
    try:
        with HttpDownloader(output_dir=args.output) as dl:
            results = dl.download(part_id)
            
            downloaded = sum(1 for r in results if r.success)
            failed = sum(1 for r in results if not r.success)
            
            print(f"\nSummary: {downloaded} downloaded, {failed} failed")
            
    except ImportError as e:
        print(f"\nError: HTTP mode requires additional packages.")
        print("Install with: pip install requests requests-negotiate-sspi truststore beautifulsoup4")
        print(f"Details: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
