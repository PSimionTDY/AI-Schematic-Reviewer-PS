"""
Batch Downloader for Highstage Datasheets

Downloads multiple IC datasheets from Highstage, skipping any that already exist.
Useful for downloading a list of part IDs while avoiding redundant downloads.

Usage:
    python batch_downloader.py IC1008360 IC1008052 IC1011559 --output datasheets\
    
    # Or with file containing part IDs (one per line):
    python batch_downloader.py --file parts.txt --output datasheets\
    
    # With schematic.yaml (extracts part_number field):
    python batch_downloader.py --schematic schematic.yaml --output datasheets\
"""

import os
import sys
import argparse
import subprocess
from pathlib import Path
from typing import List, Tuple, Dict


def get_existing_parts(output_dir: str) -> set:
    """Get set of part IDs that already have folders in the output directory."""
    output_path = Path(output_dir)
    if not output_path.exists():
        return set()

    existing = set()
    for item in output_path.iterdir():
        if item.is_dir():
            existing.add(item.name.upper())

    return existing


def extract_parts_from_schematic(schematic_file: str, filter_types: List[str] = None) -> List[str]:
    """Extract part IDs from schematic.yaml file.

    Args:
        schematic_file: Path to schematic.yaml
        filter_types: Optional list of comp_type values to include
                      (e.g. ['ic', 'transistor', 'diode', 'zener']).
                      If None, all components with a part_number are included.
    """
    import yaml

    parts = []
    try:
        with open(schematic_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        if not data:
            return parts

        # Look for components list
        components = data.get('components', [])
        if isinstance(components, dict):
            for comp_id, comp_data in components.items():
                if isinstance(comp_data, dict):
                    if filter_types and comp_data.get('comp_type') not in filter_types:
                        continue
                    part_num = comp_data.get('part_number') or comp_data.get('partNumber')
                    if part_num:
                        parts.append(part_num)
        elif isinstance(components, list):
            for comp in components:
                if isinstance(comp, dict):
                    if filter_types and comp.get('comp_type') not in filter_types:
                        continue
                    part_num = comp.get('part_number') or comp.get('partNumber')
                    if part_num:
                        parts.append(part_num)

    except ImportError:
        print("Error: PyYAML not installed. Install with: pip install pyyaml")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading schematic file: {e}")
        sys.exit(1)

    return parts


def normalize_part_id(part_id: str) -> str:
    """Normalize part ID to uppercase. Preserves the existing prefix (IC, DIO, DIS, etc.)."""
    return part_id.strip().upper()


def download_part(part_id: str, output_dir: str, downloader_script: str) -> Tuple[bool, str]:
    """
    Download a single part using the highstage_downloader.py script.
    
    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        # Run the downloader script
        cmd = [
            sys.executable,
            downloader_script,
            part_id,
            "--output", output_dir
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode == 0:
            # Check if folder exists and has content
            part_dir = Path(output_dir) / part_id
            if part_dir.exists() and any(part_dir.iterdir()):
                return True, "Downloaded successfully"
            else:
                return False, "No files found"
        else:
            error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
            return False, error_msg or "Unknown error"
    
    except subprocess.TimeoutExpired:
        return False, "Timeout (>120s)"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(
        description="Batch download datasheets from Highstage"
    )
    
    # Input sources (mutually exclusive)
    input_group = parser.add_argument_group('input sources')
    input_group.add_argument(
        "part_ids",
        nargs="*",
        help="Part IDs to download (e.g., IC1008360 IC1008052)"
    )
    input_group.add_argument(
        "--file", "-f",
        help="File containing part IDs (one per line)"
    )
    input_group.add_argument(
        "--schematic", "-s",
        help="Extract part IDs from schematic.yaml"
    )

    parser.add_argument(
        "--filter-type",
        dest="filter_type",
        help=(
            "Only download parts whose comp_type matches one of these values "
            "(comma-separated, e.g. 'ic,transistor,diode,zener'). "
            "Only applies when --schematic is used."
        )
    )
    
    parser.add_argument(
        "--output", "-o",
        default="datasheets",
        help="Output directory (default: datasheets)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if folder already exists"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output from each download"
    )
    
    args = parser.parse_args()
    
    # Get list of parts to download
    parts_to_process = []
    
    if args.schematic:
        filter_types = [t.strip() for t in args.filter_type.split(',')] if args.filter_type else None
        parts_to_process = extract_parts_from_schematic(args.schematic, filter_types)
        type_label = f" (filter: {args.filter_type})" if filter_types else ""
        print(f"Extracted {len(parts_to_process)} parts from {args.schematic}{type_label}")
    elif args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            parts_to_process = [line.strip() for line in f if line.strip()]
        print(f"Loaded {len(parts_to_process)} parts from {args.file}")
    elif args.part_ids:
        parts_to_process = args.part_ids
    else:
        parser.print_help()
        return 1
    
    # Normalize part IDs
    parts_to_process = [normalize_part_id(p) for p in parts_to_process]
    
    # Remove duplicates while preserving order
    seen = set()
    parts_to_process = [p for p in parts_to_process if not (p in seen or seen.add(p))]
    
    print(f"\nProcessing {len(parts_to_process)} unique parts\n")
    
    # Get existing parts
    existing_parts = get_existing_parts(args.output)
    
    # Determine which parts need downloading
    parts_to_download = []
    parts_to_skip = []
    
    for part_id in parts_to_process:
        if part_id in existing_parts and not args.force:
            parts_to_skip.append(part_id)
        else:
            parts_to_download.append(part_id)
    
    # Find downloader script
    script_dir = Path(__file__).parent
    downloader_script = script_dir / "highstage_downloader.py"
    
    if not downloader_script.exists():
        print(f"Error: Could not find {downloader_script}")
        return 1
    
    # Download parts
    results: Dict[str, Tuple[bool, str]] = {}
    
    for i, part_id in enumerate(parts_to_download, 1):
        print(f"[{i}/{len(parts_to_download)}] Downloading {part_id}...", end=" ", flush=True)
        success, message = download_part(part_id, args.output, str(downloader_script))
        results[part_id] = (success, message)
        
        if success:
            print(f"[OK] {message}")
        else:
            print(f"[FAIL] {message}")
    
    # Print summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    downloaded = sum(1 for s, _ in results.values() if s)
    failed = sum(1 for s, _ in results.values() if not s)
    
    print(f"\nSkipped (already exists):  {len(parts_to_skip)}")
    if parts_to_skip and args.verbose:
        for part_id in parts_to_skip:
            print(f"  - {part_id}")
    
    print(f"Downloaded:                {downloaded}")
    print(f"Failed:                    {failed}")
    
    if results:
        print(f"\nResults by part:")
        for part_id in parts_to_download:
            if part_id in results:
                success, message = results[part_id]
                status = "[OK]" if success else "[FAIL]"
                print(f"  {status} {part_id}: {message}")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
