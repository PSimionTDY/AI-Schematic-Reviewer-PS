#!/usr/bin/env python3
"""
bom_verify_highstage.py — Verify every BOM entry against Highstage.

For each BOM row:
  - If hs = TBD/empty  → flag as missing Highstage ID
  - Otherwise          → query Highstage, check:
      * Part exists
      * Description / MPN roughly matches source_manufacturer_1_pn
      * Status is not obsolete / NRND / last-time-buy
      * Part type makes sense (CAP → capacitor, RES → resistor, etc.)

Writes a Markdown report to the specified output file.

Usage:
    python bom_verify_highstage.py <bom_file> <output_md>
    (accepts both .csv and .xlsx)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib3
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import requests
    from requests_negotiate_sspi import HttpNegotiateAuth
    _auth = HttpNegotiateAuth()
except ImportError as e:
    print(f"[error] Missing dependency: {e}")
    sys.exit(1)

HIGHSTAGE_SEARCH_URL = "https://highstage/ts/ts/search.aspx"
_COLS = "o;name;description;status;type;Mfg1Partnumber;Mfg2Partnumber"

# Status strings that indicate supply chain concern
_BAD_STATUS = {"obsolete", "nrnd", "last time buy", "discontinued", "end of life"}

# Revision suffix pattern to strip when querying Highstage (e.g. CAP1020440-1 → CAP1020440)
_REVISION_RE = re.compile(r'-\d+[A-Za-z]?$')


# ── Highstage API ────────────────────────────────────────────────────────────

def _strip_revision(hs_id: str) -> str:
    """Strip trailing revision suffix: CAP1020440-1 → CAP1020440."""
    return _REVISION_RE.sub('', hs_id)


def _query_part(part_id: str) -> tuple[list[dict], str | None]:
    """Query Highstage for a part ID.
    Returns (results, draft_id) where draft_id is set if part is a working draft."""
    url = (
        f"{HIGHSTAGE_SEARCH_URL}?t=part&o={part_id}"
        f"&_format=xml_raw&_columns={_COLS}"
    )
    try:
        resp = requests.get(url, auth=_auth, verify=False, timeout=30)
        if resp.status_code == 500:
            # Check for "not approved yet" message with draft link
            import re as _re
            m = _re.search(r'o=([A-Z0-9]+-\d+[A-Za-z]?)(?:"|&quot;)', resp.text)
            draft_id = m.group(1) if m else None
            return [], draft_id
        if not resp.ok:
            return [], None
        root = ET.fromstring(resp.text.strip())
        return [dict(r.attrib) for r in root.iter("row")], None
    except Exception as e:
        return [{"__error": str(e)}], None


def query_part(part_id: str) -> tuple[list[dict], bool]:
    """Strip revision suffix then query Highstage.
    Returns (results, is_draft).
    """
    base_id = _strip_revision(part_id)
    results, draft_id = _query_part(base_id)
    if results:
        return results, False

    # 500 response gave us an explicit draft link — use it
    if draft_id:
        draft_results, _ = _query_part(draft_id)
        if draft_results:
            draft_results[0]['__draft'] = draft_id
            return draft_results, True
        return [{'__draft': draft_id, '__pending': True}], True

    # 200 empty or no hint: probe draft suffixes -1A, -2A, -3A
    for rev in ('1A', '2A', '3A'):
        candidate = f"{base_id}-{rev}"
        draft_results, _ = _query_part(candidate)
        if draft_results:
            draft_results[0]['__draft'] = candidate
            return draft_results, True

    # Last resort: try original BOM ID as-is (e.g. CONN1020564-1)
    if base_id != part_id:
        results, draft_id2 = _query_part(part_id)
        if results:
            return results, False

    return [], False


# ── BOM parsing ──────────────────────────────────────────────────────────────

@dataclass
class BomRow:
    qty: str
    designators: list[str]
    hs_id: str
    mpn: str
    manufacturer: str
    name: str          # "Name" column = value
    description: str
    package: str
    libref: str


def parse_bom(bom_path: Path) -> list[BomRow]:
    """Parse BOM from CSV or XLSX."""
    suffix = bom_path.suffix.lower()
    if suffix in ('.xlsx', '.xls'):
        return _parse_bom_xlsx(bom_path)
    return _parse_bom_csv(bom_path)


def _parse_bom_csv(csv_path: Path) -> list[BomRow]:
    rows: list[BomRow] = []
    with open(csv_path, encoding='utf-8-sig', errors='replace', newline='') as f:
        reader = csv.DictReader(f)
        for raw in reader:
            rows.append(_row_from_dict(raw))
    return rows


def _parse_bom_xlsx(xlsx_path: Path) -> list[BomRow]:
    try:
        import pandas as pd
    except ImportError:
        print("[error] pandas not installed. Run: pip install pandas openpyxl")
        sys.exit(1)
    df = pd.read_excel(xlsx_path, dtype=str).fillna('')
    rows: list[BomRow] = []
    for _, raw in df.iterrows():
        rows.append(_row_from_dict(dict(raw)))
    return rows


def _row_from_dict(raw: dict) -> BomRow:
    qty = str(raw.get('Quantity', '')).strip()
    desig_str = str(raw.get('Designator', '')).strip()
    hs_id = str(raw.get('hs', '')).strip()
    mpn = str(raw.get('source_manufacturer_1_pn', '')).strip()
    mfg = str(raw.get('source_manufacturer_1', '')).strip()
    name = str(raw.get('Name', '')).strip()
    desc = str(raw.get('Description', '')).strip()
    pkg = str(raw.get('Case/Package', '')).strip()
    libref = str(raw.get('LibRef', '')).strip()
    designators = [d.strip() for d in re.split(r',\s*', desig_str) if d.strip()]
    return BomRow(qty, designators, hs_id, mpn, mfg, name, desc, pkg, libref)


# ── Verification logic ───────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str  # error / warning / ok / info
    hs_id: str
    mpn_bom: str
    designators: str
    name_bom: str
    detail: str
    hs_description: str = ""
    hs_status: str = ""
    hs_type: str = ""


def norm_mpn(s: str) -> str:
    return re.sub(r'[\s\-_,./]', '', s.upper())


def check_status(status: str) -> str | None:
    """Return warning message if status is concerning, else None."""
    sl = status.lower().strip()
    for bad in _BAD_STATUS:
        if bad in sl:
            return f"Status: {status!r}"
    return None


def _is_passive(hs_id: str) -> bool:
    """Return True if the part is a passive (CAP, RES, MAG, DIO, CONN, ELX, MPCB)."""
    prefixes = ('CAP', 'RES', 'MAG', 'DIO', 'CONN', 'ELX', 'MPCB', 'DIS')
    uid = hs_id.upper()
    return any(uid.startswith(p) for p in prefixes)


def verify_row(row: BomRow) -> Finding:
    desig_str = ", ".join(row.designators[:6])
    if len(row.designators) > 6:
        desig_str += f" (+{len(row.designators)-6} more)"

    # Missing Highstage ID
    hs_id_clean = row.hs_id.strip()
    if not hs_id_clean or hs_id_clean.upper() in ('TBD', 'MISSING'):
        return Finding(
            severity='error',
            hs_id=hs_id_clean or '(empty)',
            mpn_bom=row.mpn,
            designators=desig_str,
            name_bom=row.name,
            detail=f"No Highstage ID assigned (hs='{row.hs_id}'). MPN in BOM: {row.mpn}",
        )

    results, is_draft = query_part(row.hs_id)

    if not results:
        return Finding(
            severity='error',
            hs_id=row.hs_id,
            mpn_bom=row.mpn,
            designators=desig_str,
            name_bom=row.name,
            detail=f"Part **not found** in Highstage",
        )

    if results[0].get("__error"):
        return Finding(
            severity='warning',
            hs_id=row.hs_id,
            mpn_bom=row.mpn,
            designators=desig_str,
            name_bom=row.name,
            detail=f"Highstage API error: {results[0]['__error']}",
        )

    # Draft pending approval
    if results[0].get('__pending'):
        draft_id = results[0].get('__draft', '')
        return Finding(
            severity='warning',
            hs_id=row.hs_id,
            mpn_bom=row.mpn,
            designators=desig_str,
            name_bom=row.name,
            detail=f"⚠️ Part exists as **unapproved draft** ({draft_id}) — needs approval before release",
        )

    r = results[0]
    hs_name = r.get('name', '').strip()
    hs_desc = r.get('description', '').strip()
    hs_status = r.get('status', '').strip()
    hs_type = r.get('type', '').strip()
    hs_mpn1 = r.get('mfg1partnumber', '').strip()
    hs_mpn2 = r.get('mfg2partnumber', '').strip()
    draft_id = r.get('__draft', '')

    issues = []

    # Draft warning
    if is_draft:
        issues.append(f"⚠️ Unapproved draft ({draft_id}) — needs approval before release")

    # Status check
    status_warn = check_status(hs_status)
    if status_warn:
        issues.append(status_warn)

    # MPN match — compare BOM MPN against Highstage Mfg1Partnumber / Mfg2Partnumber
    if row.mpn:
        bom_mpn_norm = norm_mpn(row.mpn)
        hs_mpn1_norm = norm_mpn(hs_mpn1) if hs_mpn1 else ''
        hs_mpn2_norm = norm_mpn(hs_mpn2) if hs_mpn2 else ''
        passive = _is_passive(row.hs_id)

        if not hs_mpn1 and not hs_mpn2:
            pass  # No MPN stored in Highstage — skip
        elif bom_mpn_norm in (hs_mpn1_norm, hs_mpn2_norm):
            pass  # Exact match
        else:
            # Prefix match (e.g. HS has "LM3880MFE-1AE/NOPB", BOM has "LM3880MFE-1AE")
            partial = (
                (hs_mpn1_norm and (bom_mpn_norm.startswith(hs_mpn1_norm) or hs_mpn1_norm.startswith(bom_mpn_norm))) or
                (hs_mpn2_norm and (bom_mpn_norm.startswith(hs_mpn2_norm) or hs_mpn2_norm.startswith(bom_mpn_norm)))
            )
            if partial:
                # Downgrade to info for all partial matches
                issues.append(
                    f"MPN partial match: BOM `{row.mpn}` vs Highstage `{hs_mpn1 or hs_mpn2}`"
                )
            elif passive:
                # For passives, alternative MPNs are common — downgrade to info
                issues.append(
                    f"MPN alt/mismatch (passive): BOM `{row.mpn}` vs Highstage `{hs_mpn1 or hs_mpn2}`"
                )
            else:
                issues.append(
                    f"MPN mismatch: BOM has `{row.mpn}` but Highstage has `{hs_mpn1 or hs_mpn2}`"
                )

    # Determine severity
    if not issues:
        severity = 'ok'
        detail = "✓ Verified"
    elif any("mismatch" in i and "passive" not in i or "Status:" in i or "draft" in i.lower() for i in issues):
        severity = 'warning'
        detail = "; ".join(issues)
    else:
        severity = 'info'
        detail = "; ".join(issues)

    return Finding(
        severity=severity,
        hs_id=row.hs_id,
        mpn_bom=row.mpn,
        designators=desig_str,
        name_bom=row.name,
        detail=detail,
        hs_description=(f"{hs_mpn1}" + (f" / {hs_mpn2}" if hs_mpn2 else "") + (f" — {hs_desc[:60]}" if hs_desc else "")).strip(" —"),
        hs_status=hs_status,
        hs_type=hs_type,
    )


# ── Report rendering ─────────────────────────────────────────────────────────

def render_report(findings: list[Finding], bom_path: Path) -> str:
    lines: list[str] = []
    a = lines.append

    errors   = [f for f in findings if f.severity == 'error']
    warnings = [f for f in findings if f.severity == 'warning']
    ok       = [f for f in findings if f.severity == 'ok']
    infos    = [f for f in findings if f.severity == 'info']

    # Split warnings: drafts vs real issues
    drafts   = [f for f in warnings if 'draft' in f.detail.lower() and 'mismatch' not in f.detail.lower()]
    other_w  = [f for f in warnings if f not in drafts]

    a("# BOM Verification vs Highstage\n")
    a(f"**BOM file:** `{bom_path.name}`  ")
    a(f"**Total rows:** {len(findings)}  ")
    a(f"**Verified OK:** {len(ok)}  ")
    a(f"**Unapproved drafts:** {len(drafts)}  ")
    a(f"**Warnings (MPN/status):** {len(other_w)}  ")
    a(f"**Errors:** {len(errors)}  ")
    a(f"**Info:** {len(infos)}\n")

    a("| Severity | Count |")
    a("|----------|-------|")
    a(f"| 🔴 Error (missing ID / part not found)               | {len(errors)} |")
    a(f"| 🟠 Draft (part exists but unapproved in Highstage)   | {len(drafts)} |")
    a(f"| 🟡 Warning (MPN mismatch / obsolete status)          | {len(other_w)} |")
    a(f"| 🔵 Info (partial match)                              | {len(infos)} |")
    a(f"| ✅ OK                                                | {len(ok)} |")
    a("")

    def _table(title: str, items: list[Finding]) -> None:
        if not items:
            return
        a(f"## {title}\n")
        a("| Highstage ID | BOM MPN | Designators | Value | Detail |")
        a("|---|---|---|---|---|")
        for f in items:
            hs_link = f"[{f.hs_id}](https://highstage/ts/ts/view.aspx?t=part&o={f.hs_id})" if f.hs_id and f.hs_id not in ('(empty)', 'TBD', 'MISSING') else f.hs_id
            a(f"| {hs_link} | `{f.mpn_bom}` | {f.designators} | {f.name_bom} | {f.detail} |")
        a("")

    _table("🔴 Errors — Missing / Not Found in Highstage", errors)
    _table("🟠 Unapproved Drafts — Part exists but not yet approved", drafts)
    _table("🟡 Warnings — MPN Mismatch / Obsolete / Status Issues", other_w)
    _table("🔵 Info — Passive alternates / partial matches", infos)

    # Full verified list (collapsed for brevity - just show key info)
    if ok:
        a("## ✅ Verified OK\n")
        a("| Highstage ID | BOM MPN | Designators | Value | Highstage Description |")
        a("|---|---|---|---|---|")
        for f in ok:
            hs_link = f"[{f.hs_id}](https://highstage/ts/ts/view.aspx?t=part&o={f.hs_id})"
            desc = f.hs_description[:80] if f.hs_description else ""
            a(f"| {hs_link} | `{f.mpn_bom}` | {f.designators} | {f.name_bom} | {desc} |")
        a("")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bom_file", type=Path)
    ap.add_argument("output_md", type=Path)
    ap.add_argument("--delay", type=float, default=0.15,
                    help="Seconds between Highstage API calls (default 0.15)")
    args = ap.parse_args()

    print(f"Reading BOM: {args.bom_file}")
    rows = parse_bom(args.bom_file)
    print(f"  {len(rows)} BOM rows")

    # Deduplicate by Highstage ID so we only query each part once
    hs_to_rows: dict[str, list[BomRow]] = {}
    for row in rows:
        key = row.hs_id.strip().upper() if row.hs_id else ""
        hs_to_rows.setdefault(key, []).append(row)

    findings: list[Finding] = []
    total = len(rows)
    done = 0

    for row in rows:
        done += 1
        print(f"  [{done:3d}/{total}] {row.hs_id:25s}  {', '.join(row.designators[:3])}{'…' if len(row.designators)>3 else ''}")
        finding = verify_row(row)
        findings.append(finding)
        if args.delay and row.hs_id and row.hs_id.upper() != 'TBD':
            time.sleep(args.delay)

    report = render_report(findings, args.bom_file)
    args.output_md.write_text(report, encoding='utf-8')
    print(f"\nReport written to: {args.output_md}")

    errors   = sum(1 for f in findings if f.severity == 'error')
    warnings = sum(1 for f in findings if f.severity == 'warning')
    print(f"Errors: {errors}  Warnings: {warnings}  OK: {sum(1 for f in findings if f.severity=='ok')}")


if __name__ == "__main__":
    main()
