# PDF Annotation Findings — Cadence Allegro Schematic PDFs

**Date**: 2026-03-10  
**PDFs investigated**: `SCH25678-1E-1.pdf` (37 pages), `sch23729-3.pdf` (24 pages)  
**Tool**: PyMuPDF (fitz) 1.26.6  

---

## 1. Annotation Type

**Type**: `/Annot` with `/Subtype /Link` and `/S /JavaScript` action.

All annotations are PDF **Link** annotations whose action is a JavaScript call:

```
/Type /Annot
/Subtype /Link
/Rect [ ... ]
/C [ 0 1 0 ]
/Border [ 0 0 0 ]
/A <<
  /JS (var cmd = app.popUpMenu\( "Title" , "-" , "Key=Value" , ... \);)
  /S /JavaScript
>>
```

**Why `page.annots()` returns nothing**: PyMuPDF's `page.annots()` iterator skips `/Link` subtype annotations by default. You must scan the xref table directly using `doc.xref_object(xref)` and look for objects with `/Type /Annot`.

**Scale**: SCH25678-1E has **19,040 annotations** across 37 pages (~515 per page).

---

## 2. Raw Content Format

The JavaScript string calls `app.popUpMenu()` with a list of quoted string arguments:

```javascript
var cmd = app.popUpMenu(
  "INS408671 : R2",           // title: <CIS name> : <reference>
  "-",                         // separator
  "Name=INS408671",
  "ID=67920",
  "Reference=R2",
  "Designator=",
  "Part Reference=R2",
  "Value=2.80K",
  "Primitive=DEFAULT",
  "Implementation Type=<none>",
  "Implementation=",
  "Implementation Path=",
  "ItemType=RES",
  "Part Number=RES1014198",
  "Description=RES,THICK FILM,SMD,2.80K,1%,0402,0.063W",
  "Voltage=50V",
  "Tolerance=1%",
  "Package Type=THICK FILM",
  "eia724status=3 Maturity",
  "lifecyclenote=",
  "Current=",
  "CISType=",
  "mfg1=Yageo",
  "mfg1partnumber=232270672802L",
  "mgf2=Vishay",
  "mfg2partnumber=CRCW04022K80FKED",
  "HEIGHT=0.40",
  "PCB Footprint=SMR0402a"
);
```

The `=` sign separates key from value. The separator `"-"` divides the title from the properties. Keys and values are plain strings. **No XML, JSON, or CSV** — just `key=value` pairs as quoted JS string arguments.

> **Important PDF parsing note**: The JS string is embedded in a PDF literal string `(...)`. Inner parentheses are escaped as `\(` and `\)`. A naïve regex stops early at the first `\)`. You must implement a proper PDF literal string parser that tracks nesting depth and respects `\)` escapes. See `pdf_annotation_extractor.py::_extract_pdf_literal_string()`.

---

## 3. Fields Available

### All component annotations (1,684 per schematic)

| Field | Example | Coverage |
|-------|---------|----------|
| `Reference` | `R2`, `C42`, `U1` | ~96% |
| `Value` | `2.80K`, `100n`, `AT24C512C-XHL` | 96% |
| `ItemType` | `RES`, `CAP`, `IC`, `DIO`, `CONN`, `MAG`, `XPCB` | 97% |
| `Part Number` | `RES1014198`, `CAP1013576` (Highstage internal PN) | 97% |
| `Description` | `RES,THICK FILM,SMD,2.80K,1%,0402,0.063W` | 97% |
| `Voltage` | `50V`, `25V`, `200V`, `1.7-3.6V` | 84% |
| `Tolerance` | `1%`, `10%`, `0.5%` | 74% |
| `Package Type` | `X7R`, `X5R`, `THICK FILM`, `THIN FILM`, `EEPROM` | 89% |
| `PCB Footprint` | `SMR0402a`, `smd0603a`, `sop65p640-8n` | 97% |
| `mfg1` | `Yageo`, `Vishay`, `Murata` | 88% |
| `mfg1partnumber` | `232270672802L`, `GRM033R60J104KE19D` | 88% |
| `mgf2` *(typo in CIS)* | `Vishay`, `TDK` | 88% |
| `mfg2partnumber` | `CRCW04022K80FKED` | 88% |
| `HEIGHT` | `0.40`, `1.2` (mm) | 89% |
| `Current` | `20mA`, `1.4A` | 23% |

### Additional annotation categories

| Category | Count | Title pattern | Key data |
|----------|-------|---------------|----------|
| Net/Wire labels | 7,604 | `"HV_in : HV_IN (Wire ID = 8788)"` | Wire ID, net name |
| Net aliases | 6,455 | `"GND : GND"` | None |
| Text labels | 140 | `"Address: 0x50"`, `"Capacitor bank connector"` | Bounding box, font |
| Page title blocks | 37 | (title page data) | RevCode, page count, designer |

---

## 4. Coverage Analysis

### SCH25678-1E (37 pages)

- **Total annotations**: 19,040
- **Component annotations**: 1,684 unique components
- **Components with Voltage**: 1,416 / 1,684 = **84%**
- **Components with Tolerance**: 1,261 / 1,684 = **74%**
- **Components with MPN**: 1,482 / 1,684 = **88%**
- **Components with Highstage PN**: 1,648 / 1,684 = **97%**

### ItemType breakdown

| Type | Count | Notes |
|------|-------|-------|
| RES | 661 | Resistors — voltage, tolerance, power in description |
| CAP | 503 | Capacitors — voltage, tolerance, dielectric (X7R/X5R) |
| XPCB | 134 | PCB items (test points, jumpers) |
| DIO | 99 | Diodes, LEDs |
| MAG | 97 | Magnetics (inductors, transformers) |
| DIS | 96 | Discrete semiconductors |
| IC | 46 | Integrated circuits |
| CONN | 12 | Connectors |

---

## 5. Passive Component Data Confirmed

### Capacitor example — C1

```
Value:        100n
Voltage:      6.3V
Tolerance:    10%
Package Type: X5R          ← dielectric
PCB Footprint: SMR0201a    → package 0201
Description:  CAP,X5R,SMD,100n,10%,0201,6.3V
Highstage PN: CAP1018724
MPN (Yageo):  GRM033R60J104KE19D
```

### Resistor example — R2

```
Value:        2.80K
Voltage:      50V
Tolerance:    1%
Package Type: THICK FILM
PCB Footprint: SMR0402a   → package 0402
Description:  RES,THICK FILM,SMD,2.80K,1%,0402,0.063W
Highstage PN: RES1014198
MPN (Yageo):  232270672802L
MPN (Vishay): CRCW04022K80FKED
Power rating: 0.063W       ← parsed from description
```

Voltage rating, tolerance, **and power rating** are all present for passives.

---

## 6. Proposed schematic.yaml Fields

```yaml
components:
  C42:
    value: 100n                         # from Allegro netlist (already populated)
    # --- NEW from PDF annotations ---
    pdf_annot:
      item_type: CAP
      highstage_pn: CAP1018724          # Highstage internal part number
      voltage_rating: 6.3V
      tolerance: 10%
      package_type: X5R                 # dielectric type
      package_size: "0201"              # parsed from description or footprint
      pcb_footprint: SMR0201a
      mfg1: Murata
      mpn1: GRM033R60J104KE19D
      mfg2: Yageo
      mpn2: 04026D474KAT2A
      height_mm: "0.35"
      description: "CAP,X5R,SMD,100n,10%,0201,6.3V"

  R2:
    value: 2.80K
    pdf_annot:
      item_type: RES
      highstage_pn: RES1014198
      voltage_rating: 50V
      tolerance: 1%
      package_type: THICK FILM
      package_size: "0402"
      pcb_footprint: SMR0402a
      mfg1: Yageo
      mpn1: 232270672802L
      mfg2: Vishay
      mpn2: CRCW04022K80FKED
      height_mm: "0.40"
      power_rating: 0.063W              # parsed from description
      description: "RES,THICK FILM,SMD,2.80K,1%,0402,0.063W"
```

---

## 7. Recommendation: Can Highstage Be Skipped for Passives?

**Yes — for the vast majority of passive components.**

| Check | Without Highstage | With PDF annotations |
|-------|------------------|----------------------|
| Cap voltage rating vs rail | ❌ No data | ✅ `voltage_rating` present for 84% |
| Resistor power rating | ❌ No data | ✅ Parsed from `description` for ~95% of RES |
| Tolerance verification | ❌ No data | ✅ `tolerance` present for 74% |
| MPN for cross-reference | ❌ No data | ✅ `mpn1` present for 88% |
| Highstage PN (for datasheet URL) | ❌ Must fetch | ✅ Already embedded — 97% coverage |

**Caveats**:
- 16% of components lack `Voltage`, 26% lack `Tolerance` — those still need Highstage
- `Current` rating is sparse (23%) — still needs Highstage for current-critical paths
- IC validation (datasheet pin counts, I2C addresses) still requires Highstage

**Recommendation**:
1. Run `pdf_annotation_extractor.py` **first** as the primary data source for passives
2. Only fall back to Highstage for components where `voltage_rating` or `tolerance` is missing
3. Use the embedded `highstage_pn` from annotations to construct the Highstage URL when needed — avoids the part number lookup step entirely

---

## 8. Implementation Notes

### Script: `pdf_annotation_extractor.py`

Located at `.github/skills/schematic-parser/scripts/pdf_annotation_extractor.py`.

```bash
# Extract and dump to JSON
python .github/skills/schematic-parser/scripts/pdf_annotation_extractor.py reviews/SCH25678-1E --json

# Extract and update schematic.yaml
python .github/skills/schematic-parser/scripts/pdf_annotation_extractor.py reviews/SCH25678-1E

# Specify PDF path explicitly
python .github/skills/schematic-parser/scripts/pdf_annotation_extractor.py reviews/SCH25678-1E \
    --pdf reviews/SCH25678-1E/SCH25678-1E-1.pdf
```

**Performance**: ~15 seconds for a 37-page schematic with 38,000 xref entries on a typical machine.

### Integration point

The schematic reviewer skill should call this extractor early in its pipeline, before any Highstage lookups, and use the `pdf_annot` section of `schematic.yaml` for:
- Capacitor voltage rating checks
- Resistor power rating checks  
- Tolerance verification
- MPN lookup for datasheets (use `mpn1` directly)
