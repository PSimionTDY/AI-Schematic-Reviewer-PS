# Cadence OrCAD Capture .DSN File Format — Investigation Findings

**Investigated by**: Copilot  
**Date**: 2025-07  
**Files examined**: `SCH25678-1E-1.DSN` (3.2 MB), `SCH23729-3.DSN` (3.9 MB)  
**Source**: `\\highstage\files\RP3\SCH\SCH25678\SCH25678-1E\` and `\\highstage\files\PURCHASE_SPEC\SCH\SCH23729\SCH23729-3\`

---

## Summary

| Property | Value |
|----------|-------|
| Container format | **OLE2 Compound Document** (Microsoft Structured Storage) |
| Magic bytes | `D0 CF 11 E0 A1 B1 1A E1` |
| Content format | **Proprietary Cadence binary** — not XML, not ASCII |
| OrCAD version | 17.4-2019 (both files; from `DsnStream` JSON metadata) |
| Python library | `olefile` — reads the OLE container; stream content requires binary parsing |

---

## Container Structure

The DSN file is an OLE2 Compound Document file (same container format as old `.doc`/`.xls`). Inside are named streams organized in a hierarchy:

```
Root
├── DsnStream           ← JSON: OrCAD version & license info
├── AdminData           ← 6 bytes, opaque flags
├── Cache               ← 700–924 KB: component symbol cache (binary)
├── Library             ← 68–156 KB: library path references (binary)
├── NetBundleMapData    ← Net bundle definitions (binary)
├── Packages/
│   ├── <PartName>_0   ← One entry per package type; contains pin names (partly readable)
│   └── ...
├── Symbols/
│   ├── TEMPOBJ_0      ← Symbol graphics (binary)
│   └── TEMPOBJ_1      ← 627 KB: all component instances (binary + embedded strings)
├── Views/
│   ├── Top/
│   │   ├── Pages/Top           ← Net names, bus names (partly readable strings)
│   │   ├── Hierarchy/Hierarchy ← 314–323 KB: all ref des (readable via string scan)
│   │   ├── Schematic           ← Schematic metadata (binary)
│   │   └── Constraint/DCF      ← 1 MB: design constraints (binary)
│   ├── <SheetName>/
│   │   ├── Pages/<SheetName>   ← Net & bus names for this sheet
│   │   └── Schematic           ← Sheet metadata
│   └── ...
└── CIS/
    ├── VariantStore/
    │   ├── VariantNames        ← Variant names (readable: "No Mount", "Variant1", etc.)
    │   ├── BOM/<PN>/           ← BOM part data per assembly variant
    │   └── Groups/
    │       ├── No Mount/       ← List of DNP component IDs (numeric object IDs, not ref des)
    │       └── <VariantGroup>/ ← Variant configuration groups
    └── CISSchematicStore/      ← CIS link metadata
```

---

## What Is Extractable (without reverse engineering)

### ✅ Easily extractable via string scanning

| Data | How | Quality |
|------|-----|---------|
| **Component ref des** (R217, C204, U42, Q2800, …) | Regex `[RCULDQJTPFY]\d+` scan on `Views/*/Hierarchy/Hierarchy` | High — 1420 refs found in SCH25678-1E |
| **Net names** | String scan on `Views/*/Pages/*` streams | High — bus notation preserved (e.g. `FB_SEL[0..4]`, `I2C.SDA`) |
| **Sheet/page names** | OLE stream path: `Views/<SheetName>/Pages/<SheetName>` | High — exact |
| **OrCAD version & license** | `DsnStream` stream — JSON format | Exact |
| **Variant names** | `CIS/VariantStore/VariantNames` — readable text | High |
| **Component symbol names** | `Cache` stream string scan | Medium — e.g. `TMUX1208RSVR`, `LTC3626EUDC` |
| **Library paths** | `Cache` and `Library` streams | High — full UNC paths |
| **Title block type** | `Library` stream — e.g. `TitleBlock5` | High |
| **Company address strings** | `Library` stream | High — "Teledyne Reson A/S", "Fabriksvangen 13" |

### ⚠️ Partially extractable (requires heuristic binary parsing)

| Data | Notes |
|------|-------|
| **Component values** | Embedded in binary struct fields. Adjacent to ref des in page streams but not in simple string form. Requires understanding the fixed-size binary records. |
| **DNP component list** | `CIS/VariantStore/Groups/No Mount/*` contains numeric object IDs — needs cross-reference to ref des mapping (not a simple string scan). |
| **BOM part data** | `CIS/VariantStore/BOM/*/BOMPartData` — binary; ~5 KB per BOM, likely structured part property tables. |
| **Pin-to-net connectivity** | Page streams contain net and ref data but the wiring topology is in binary coordinate/pointer structures. |

### ❌ Not extractable without full reverse engineering

| Data | Reason |
|------|--------|
| **Voltage/tolerance/rating** | Stored as component properties in binary structs — no published format spec |
| **MPN (manufacturer part number)** | In BOMPartData binary blob |
| **Schematic XY coordinates** | Binary format |
| **Wire routing data** | Binary format |

---

## Key Discovery: DsnStream (JSON Metadata)

The `DsnStream` stream contains a JSON object — the only structured, human-readable metadata in the file:

```json
{
  "InstallMode": "0",
  "License": "OrCAD_X_Capture+OrCAD_Capture_CIS_option",
  "InstalledVersionBase": "17.4-2019",
  "InstalledVersionISR": "P001"
}
```

SCH23729-3 uses `"License": "OrCAD_Capture_CIS_option+Capture"` and `"InstalledVersionISR": "S001"`, indicating slightly different build/license configurations.

---

## Variant / DNP Data Structure

The `CIS/VariantStore` tree stores:
- **VariantNames**: Human-readable variant group names (No Mount, Variant-Variant1, bom-XXXXXX-Common, etc.)
- **Groups/No Mount/**: Numeric object IDs (e.g. `1540~0`, `5084~0`) for DNP components — these are internal OrCAD object identifiers, not ref des
- **Groups/<Variant>/<SubVariant>/**: Similar numeric ID lists for variant-specific population

The No Mount group in SCH25678-1E has ~44 DNP component IDs. In SCH23729-3 the `NoMount` group contains 1590 bytes (~100+ entries), and multiple subgroups handle different board configurations (FAN mount/noMount, FPGA KCU040/KCU060, SFP x4, etc.).

---

## Recommended Extraction Approach

For production use, the best approach is **not** to parse the DSN directly. Instead:

1. **Use Allegro export files** (`pinView.dat`, `pstxnet.dat`, etc.) for netlist and component data — these are already parseable and well-understood.
2. **Use the schematic PDF** for ref des values, annotations, and title block data.
3. **Use the DSN only for**:
   - Confirming OrCAD version (from `DsnStream` JSON)
   - Extracting schematic/page names (from OLE stream paths)
   - Listing component ref des (string scan on `Hierarchy` stream) as a cross-check

If full component property extraction from the DSN is needed, the correct approach is to use the **Cadence OrCAD Capture scripting API** (via the OrCAD tool itself, using `ORCAD_CAPTURE_TCL_API` or COM automation), not direct file parsing.

---

## Python Code to Extract Available Data

```python
import olefile, re, json

def inspect_dsn(dsn_path):
    ole = olefile.OleFileIO(dsn_path)
    
    # OrCAD version
    version_json = json.loads(ole.openstream("DsnStream").read())
    print("OrCAD version:", version_json)
    
    # Sheet names from OLE stream structure
    pages = [e for e in ole.listdir() if len(e) >= 3 and e[0] == "Views" and e[1] != "Directory" and e[-2] == "Pages"]
    for p in pages:
        print("Sheet:", p[-1])
    
    # Component ref des from Hierarchy stream
    hier = ole.openstream("Views/Top/Hierarchy/Hierarchy").read()
    refs = re.findall(rb'(?<!\w)[RCULDQJTPFY]\d{3,5}(?!\w)', hier)
    print(f"Found {len(set(refs))} unique ref des")
    
    # Variant names
    vnames_data = ole.openstream("CIS/VariantStore/VariantNames").read()
    vstrings = re.findall(rb'[\x20-\x7E]{3,}', vnames_data)
    print("Variants:", [s.decode() for s in vstrings])
    
    ole.close()
```

---

## File Sizes and Page Counts

| File | Size | Sheets | Component refs (approx) |
|------|------|--------|------------------------|
| SCH25678-1E-1.DSN | 3.2 MB | 8 (Top, Feedback_mux, Transmitter, Transmitters, EPC2019_half_bridge, EPC2207_half_bridge, PWR_MEASUREMENT, Mechanical) | ~1420 |
| SCH23729-3.DSN | 3.9 MB | 18+ (DDR4, Ethernet, FPGA_BANK44-48, FPGA_BANK64-68, FPGA_Config, FPGA_MGT, FPGA_Power, Fan, Flash, GPIO, I2C, IO_EXPANDER, Mechanical, PCIe, Power_core, Power_monitor, Power_others, Power_top, SFP+, Sys_clock, Top) | >2000 estimated |

---

## .opj File Format

**File examined**: `SCH25678-1E-1.opj` (11 077 bytes)

### Format

**Plain ASCII text** — S-expression format (Lisp-like parenthesized syntax), no binary content. Opens in any text editor.

```
(ExpressProject ""
  (ProjectVersion "19981106")
  (SoftwareVersion "25.1 P001 (4349869)  [9/14/2025]-[03/10/26]")
  (ProjectType "PCB")
  (Folder "Design Resources"
    ...
    (File ".\sch25678-1.dsn" (Type "Schematic Design"))
    ...
  )
  (GlobalState
    (FileView
      (Path "Design Resources" "\\server\...\sch25678-1.dsn" "Top")
      (Path "Design Resources" "\\server\...\sch25678-1.dsn" "EPC2019_half_bridge")
      ...
    )
    (Doc (Type "COrSchematicDoc") (Schematic "PWR_MEASUREMENT") ...)
  )
)
```

### What Is Readable

| Field | Regex / Location | Example value |
|-------|-----------------|---------------|
| **OrCAD software version** | `\(SoftwareVersion\s+"([^"]+)"\)` | `25.1 P001 (4349869) [9/14/2025]-[03/10/26]` |
| **DSN file reference** | `\(File\s+"([^"]+\.dsn)"` | `.\sch25678-1.dsn` |
| **Sheet names** (expanded in tree) | 3-element `(Path "Design Resources" "<dsn>" "<sheet>")` in `GlobalState/FileView` | Top, EPC2019_half_bridge, Feedback_mux, Mechanical, PWR_MEASUREMENT, Transmitters |
| **Open sheets at save** | `\(Schematic\s+"([^"]+)"\)` in `Doc` blocks | PWR_MEASUREMENT, Top, Transmitters |
| **PCB board path** | `"Allegro Netlist Output Board File"\s+"([^"]+)"` | `//sla-vpapp-hst02/Files/RP3/PCB/PCB1018900/PCB1018900-1/Layout/PCB1018900-1.brd` |
| **Netlist directory** | `"Allegro Netlist Directory"\s+"([^"]+)"` | `allegro` |
| **Library OLB paths** | `\(LibraryName\s+"([^"]+)"\)` in `CacheData` | `S:\UDV\ORCAD\RESON\CAPTURE\SYMBOL\IC.OLB` |
| **MPS session name** | `\(MPSSessionName\s+"([^"]+)"\)` | `hebbekaer` |

### What Is NOT in the .opj

| Data | Notes |
|------|-------|
| **Revision letter** | Not stored in .opj. The DSN file internally has a `RevCode` title block field, but it was left as `?` (default placeholder, 0x3F byte) in the binary. The folder name is the authoritative source for revision. |
| **Schematic title / document number** | Not in .opj; in the DSN title block binary data |
| **Date of last schematic modification** | Not in .opj |
| **Component data / BOM** | Not in .opj |
| **Complete sheet list** | The FileView only contains sheets the user expanded in the project tree — not guaranteed to be exhaustive (6 of 8 sheets shown for SCH25678-1E). Use DSN OLE stream structure for the canonical list. |

### Revision: Can We Extract It?

**No — the .opj does not contain the schematic revision letter.** The DSN binary has a `RevCode` field at byte offset ~11737, but its value is `0x3F` (`?`) — the default placeholder. The revision was never written to the binary property by the designer.

**The folder name remains the most reliable revision source**, e.g. `SCH25678-1E` → Rev `E`.

### Python Parsing

```python
import re
from pathlib import Path

def parse_opj(folder: Path) -> dict:
    opj_files = list(folder.glob("*.opj"))
    if not opj_files:
        return {}
    content = opj_files[0].read_text(encoding="utf-8", errors="replace")

    result = {}
    m = re.search(r'\(SoftwareVersion\s+"([^"]+)"\)', content)
    if m:
        result["software_version"] = m.group(1)

    # Sheets visible in project tree (3-element Path entries in FileView)
    sheets = re.findall(
        r'\(Path\s+"[^"]+"\s+"[^"]+\.dsn"\s+"([^"]+)"\)',
        content, re.IGNORECASE
    )
    result["sheets"] = sheets  # may be incomplete — only tree-expanded sheets

    m = re.search(r'"Allegro Netlist Output Board File"\s+"([^"]+)"', content)
    if m:
        result["pcb_board"] = m.group(1)

    return result
```
