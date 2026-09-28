#!/usr/bin/env python3
"""
Read BOM data from "flat" ERP-style BOM export XLSX files.

Expected columns (header row detected by presence of 'Ref.Des'):
    Pos | Antal | Part Label | Artikelnummer | Ref.Des | Comp Status | Mount Qty

Returns a component_map matching the structure produced by
pdf_annotation_extractor.build_component_map() / altium_bom_reader.read_altium_bom(),
keyed by refdes.
"""

from pathlib import Path


def _find_file(folder: Path, pattern: str) -> Path | None:
    matches = list(folder.glob(pattern))
    return matches[0] if matches else None


def _split_designators(raw) -> list[str]:
    """Split a comma/space/semicolon-separated designator string into individual refdes strings."""
    if raw is None:
        return []
    text = str(raw)
    for sep in (';', '/'):
        text = text.replace(sep, ',')
    return [r.strip() for r in text.split(',') if r.strip()]


def _read_flat_bom(bom_path: Path, component_map: dict) -> None:
    """Populate component_map from a flat ERP-style BOM XLSX."""
    import openpyxl
    wb = openpyxl.load_workbook(bom_path, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    # Find header row — the first row containing 'Ref.Des'
    header_idx = None
    for i, row in enumerate(rows):
        row_strs = [str(c).strip() if c is not None else '' for c in row]
        if 'Ref.Des' in row_strs:
            header_idx = i
            break

    if header_idx is None:
        return

    headers = [str(c).strip() if c is not None else '' for c in rows[header_idx]]
    col = {h: i for i, h in enumerate(headers) if h}

    des_col    = col.get('Ref.Des')
    label_col  = col.get('Part Label')
    pn_col     = col.get('Artikelnummer')
    status_col = col.get('Comp Status')

    if des_col is None:
        return

    for row in rows[header_idx + 1:]:
        des_raw = row[des_col] if des_col < len(row) else None
        if des_raw is None:
            continue

        label  = str(row[label_col]).strip()  if label_col  is not None and row[label_col]  is not None else ''
        pn     = str(row[pn_col]).strip()     if pn_col     is not None and row[pn_col]     is not None else ''
        status = str(row[status_col]).strip() if status_col is not None and row[status_col] is not None else ''

        entry: dict = {}
        if label:
            entry['value'] = label
            entry['description'] = label
        if pn:
            entry['part_number'] = pn
        if status:
            entry['comp_status'] = status
            if status.strip().upper() in ('DNP', 'DO NOT POPULATE', 'NOT FITTED', 'NO POP'):
                entry['dnp'] = True

        if not entry:
            continue

        for ref in _split_designators(des_raw):
            if ref not in component_map:
                component_map[ref] = {}
            component_map[ref].update(entry)


def read_flat_bom(export_folder: Path) -> dict[str, dict]:
    """Read BOM data from a folder containing a flat ERP-style BOM XLSX.
    Returns component_map keyed by refdes."""
    component_map: dict[str, dict] = {}

    for bom_path in sorted(export_folder.glob('*.xlsx')):
        if bom_path.name.startswith('~$'):
            continue
        if is_flat_bom(bom_path):
            _read_flat_bom(bom_path, component_map)

    return component_map


def is_flat_bom(bom_path: Path) -> bool:
    """Return True if the given XLSX file looks like a flat ERP-style BOM (has a 'Ref.Des' header)."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(bom_path, read_only=True, data_only=True)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > 10:
                break
            row_strs = [str(c).strip() if c is not None else '' for c in row]
            if 'Ref.Des' in row_strs:
                wb.close()
                return True
        wb.close()
    except Exception:
        return False
    return False


def find_flat_bom(folder: Path) -> Path | None:
    """Return the first flat ERP-style BOM XLSX file found in folder, or None."""
    for bom_path in sorted(folder.glob('*.xlsx')):
        if bom_path.name.startswith('~$'):
            continue
        if is_flat_bom(bom_path):
            return bom_path
    return None
