# Text Annotation Extraction — Research Findings

**Date:** 2026-06-13  
**Schematics tested:**
- `SCH25678-1E` — View.dat format (newer), 37-page PDF (`SCH25678-1E-1.pdf`)
- `SCH23729-3` — pst*.dat format (older), 24-page PDF (`sch23729-3.pdf`)

---

## 1. What's in Allegro Export Files

### Files checked — SCH25678-1E (View.dat format)

| File | Size | Content | Text annotations? |
|------|------|---------|-------------------|
| `pinView.dat` | 1 MB | NET_NAME, REFDES, PIN_NUMBER, FUNC_LOGICAL_PATH, COMP_DEVICE_TYPE, PIN_NET_SHORT | ❌ No |
| `funcView.dat` | 388 KB | FUNC_LOGICAL_PATH, COMP_DEVICE_TYPE, REFDES, FUNC_DES (sheet/function) | ❌ No |
| `compView.dat` | 34 KB | REFDES, COMP_VOLTAGE, model name, COMP_ROOM, COMP_GROUP | ❌ No |
| `netView.dat` | 72 KB | NET_NAME, NET_LOGICAL_PATH, NET_VOLTAGE_LAYER, NET_BUS_NAME | ❌ No |
| `chipsView.dat` | 85 KB | Library part primitives with pin PINUSE directions | ❌ No |
| `pxlBA.txt` | 3 KB | Back-annotation property list (metadata only, lists which columns to extract) | ❌ No |

**Header columns confirmed (A rows):**
- `compView.dat`: `REFDES | COMP_VOLTAGE | COMP_CDS_FSP_LIB_PART_MODEL | COMP_CDS_FSP_INSTANCE_NAME | COMP_ROOM | COMP_GROUP | ...`
- `funcView.dat`: `FUNC_LOGICAL_PATH | COMP_DEVICE_TYPE | REFDES | FUNC_PRIM_FILE | FUNC_SCH_SIZE | FUNC_DES | ...`
- `netView.dat`: `NET_NAME | NET_LOGICAL_PATH | NET_VOLTAGE_LAYER | NET_BUS_NAME | ...`
- `pinView.dat`: `NET_NAME | REFDES | PIN_NUMBER | FUNC_LOGICAL_PATH | COMP_DEVICE_TYPE | FUNC_DES | PIN_NET_SHORT | ...`

### Files checked — SCH23729-3 (pst*.dat format)

| File | Size | Content | Text annotations? |
|------|------|---------|-------------------|
| `pstxnet.dat` | 748 KB | Net-to-node connections (NET_NAME / NODE_NAME blocks) | ❌ No |
| `pstchip.dat` | 244 KB | Library part primitives: pin numbers, PINUSE, JEDEC_TYPE, MFG1, VALUE | ❌ No |
| `pstxprt.dat` | 649 KB | Part placement: REFDES, instance paths, XY coordinates (mils), SECTION | ❌ No |
| `pxlBA.txt` | 4 KB | Back-annotation property list | ❌ No |

**pstxprt.dat provides schematic XY coordinates** (in mils) for every component:
```
C1 'GEN_C_SMC0402A_MURATA_0.7_GRM155R61C225KE11D_2.2U':
  XY='(520,870)',
```
These mil-unit schematic coordinates are in a different coordinate space from the PDF pixel coordinates and cannot be directly correlated without a coordinate mapping step.

### Conclusion on Allegro files
**Neither export format contains any text annotation, comment, note, or free-text field.**  
The exports are strictly netlist/placement data. Text annotations placed by engineers in Cadence Allegro's schematic editor are not exported to any of the standard netlist formats (View.dat or pst*.dat). They exist only in the native Cadence design database (`.cdsz`) and the rendered PDF.

---

## 2. PDF Text Layer

**Both PDFs have a fully selectable text layer** — no OCR required.

Tested with `pdfplumber` (`page.extract_words()`):
- Returns word strings with bounding box coordinates (x0, top, x1, bottom) in PDF points
- Coordinates are consistent and precise — annotations appear at the correct schematic locations
- Page dimensions: SCH25678-1E = 792×612 pt (landscape Letter), SCH23729-3 = 842×595 pt (landscape A3)

### Annotation examples found

#### SCH25678-1E (37 pages)

| Page | Text | Coordinates (x, y) | Nearby component |
|------|------|--------------------|-----------------|
| 1 | `Address: 0x50` | (300, 397) | **U1** AT24C512C-XHL EEPROM at (321, 406) — 9 pt below |
| 2 | `Address: 0x48` | (408, 479) | I2C device (page 2) |
| 3–35 | `Note` (×2) | (659, 211) and (581, 246) | Title block notes (repeated header on every page) |
| 36 | `100V=1.89V` | (322, 99) | Voltage calibration note |
| 37 | `10x15x0.01` | (262, 150) | PCB label component value |

#### SCH23729-3 (24 pages)

| Page | Text | Coordinates (x, y) | Nearby component |
|------|------|--------------------|-----------------|
| 1 | `Routing notes: Place all TPs on bottom side! All TPs should have via in them.` | (59, 26) | General routing note |
| 2–3 | `Termination resistor Fly-by topology decoupling caps` | (570, 233) | DDR4 design notes |
| 4 | `PHY addr = 0x02` | (268–296, 192) | Ethernet PHY (ADIN1300) first instance |
| 4 | `PHY addr = 0x04` | (266–291, 459) | Ethernet PHY (ADIN1300) second instance |
| 15 | `Address: b1010 [A2][A1][A0][R/W]` | (335, 247–251) | **U600** I2C EEPROM at (354, 257) |
| 15 | `Address: b1000 10[ADDR][R/W]` | (354, 376–380) | **U601** temp sensor at (354, 386) |
| 19 | `Address` (partial) | (596, 59) | Another I2C device |
| 23–24 | `Termination` | (404, 155) | Signal termination notes |

### "Address: 0x50" — exact extraction path

```python
import pdfplumber

with pdfplumber.open("SCH25678-1E-1.pdf") as pdf:
    page = pdf.pages[0]                     # Page 1 (0-indexed)
    words = page.extract_words()
    # Result includes:
    # {'text': 'Address:', 'x0': 300.2, 'top': 396.7, 'x1': 327.8, 'bottom': 401.4}
    # {'text': '0x50',     'x0': 330.5, 'top': 396.7, 'x1': 349.1, 'bottom': 401.4}
    # {'text': 'U1',       'x0': 321.0, 'top': 406.0, ...}   ← closest ref desig: 9pt below
    # {'text': 'AT24C512C-XHL', 'x0': 295.9, 'top': 431.9, ...}  ← part number 35pt below
```

Component association: the ref designator `U1` appears 9 points (≈3 mm at 72 dpi) below and 9 points right of the annotation — the closest ref designator to the annotation text.

---

## 3. Recommendation: PDF-first approach

| Approach | Feasibility | Quality |
|----------|-------------|---------|
| **pdfplumber word extraction** | ✅ Works now, no setup | ★★★★★ Exact coordinates, selectable text |
| Allegro export files | ❌ Not feasible | Text annotations not exported |
| Native Cadence `.cdsz` database | ❌ Requires Cadence license | Not available |
| OCR on PDF images | ⚠️ Fallback only | Lower quality, error-prone |

**Recommended approach:** Extract annotations from schematic PDFs using `pdfplumber`.

### Algorithm

1. **Extract all words** with bounding boxes from each PDF page via `pdfplumber`
2. **Group consecutive words** into text phrases using y-proximity (words within 2pt vertically, 30pt horizontally)
3. **Classify phrases** by regex pattern (see section 6)
4. **Associate with components** using nearest-ref-designator:
   - For each annotation phrase, find all ref designator tokens (matching `[A-Z]+[0-9]+`) on the same page
   - The ref designator with minimum Euclidean distance to the annotation centroid is the associated component
   - Reject association if distance > 60 pt (≈21 mm) — too far to be an annotation
5. **Normalise coordinates** to `x_norm = x / page_width`, `y_norm = y / page_height` for page-size independence
6. **Filter noise** — skip annotations in the title block region (bottom 15% of page, or `y > 0.85 * page_height`)

### Component association quality check
- SCH25678-1E page 1: `Address: 0x50` → `U1` (distance 9pt) ✅
- SCH23729-3 page 4: `PHY addr = 0x02` → Ethernet PHY instance (distance ~10pt) ✅  
- SCH23729-3 page 15: `Address: b1010...` → `U600` (distance 10pt) ✅

---

## 4. Proposed `schematic.yaml` Structure

```yaml
components:
  U1:
    ref: U1
    device: AT24C512C-XHL
    annotations:
      - text: "Address: 0x50"
        type: i2c_address
        value: "0x50"
        source: pdf_page_1
        x_norm: 0.379      # x0 / page_width  (300.2 / 792)
        y_norm: 0.649      # top / page_height (396.7 / 612)
        confidence: high   # exact match to i2c_address pattern

  U600:
    ref: U600
    device: "<from netlist>"
    annotations:
      - text: "Address: b1010 [A2][A1][A0][R/W]"
        type: i2c_address
        value: "0b1010xxx"
        source: pdf_page_15
        x_norm: 0.398
        y_norm: 0.415
        confidence: high

  # Example with routing/design note
  U400:
    ref: U400
    annotations:
      - text: "OR AC termination to GND; depends on layout!"
        type: note
        source: pdf_page_2
        x_norm: 0.502
        y_norm: 0.361
        confidence: high
```

---

## 5. Classification Rules

```python
import re

I2C_ADDRESS_PATTERNS = [
    r'Address:\s*(0x[0-9A-Fa-f]{2,3})',           # "Address: 0x50"
    r'addr\s*=\s*(0x[0-9A-Fa-f]{2,3})',           # "PHY addr = 0x02"
    r'Address:\s*(b[01]{4,8})',                    # "Address: b1010..."
    r'I2C\s+addr\w*\s*[=:]\s*(0x[0-9A-Fa-f]+)',  # "I2C address: 0x48"
    r'\b(0x[0-9A-Fa-f]{2})\b',                    # bare hex near I2C component
]

DNP_PATTERNS = [
    r'\bDNP\b',
    r'\bDo\s+Not\s+Populate\b',
    r'\bNO\s+POP\b',
    r'\bNot\s+Fitted\b',
]

NET_VOLTAGE_PATTERNS = [
    r'(\d+(?:\.\d+)?V)',           # "3.3V", "100V"
    r'(\d+(?:\.\d+)?)\s*V\s*=',   # "100V=1.89V"
    r'VCC\s*=\s*(\d+(?:\.\d+)?)', # "VCC=3.3"
]

NOTE_PATTERNS = [
    r'(?i)(routing\s+note|place\s+all|fly.by|termination|decoupling)',
    r'(?i)note\s*\d*\s*[:\-]',
]

def classify_annotation(text: str) -> dict:
    for pat in I2C_ADDRESS_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return {"type": "i2c_address", "value": m.group(1)}
    for pat in DNP_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return {"type": "dnp", "value": True}
    for pat in NET_VOLTAGE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return {"type": "net_voltage", "value": m.group(1)}
    for pat in NOTE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return {"type": "note", "value": text}
    return {"type": "note", "value": text}
```

---

## 6. Verification Use Cases

| Annotation type | Verification check |
|-----------------|-------------------|
| `i2c_address` | Compare stated address against datasheet address for configured A0/A1/A2 pin states; flag mismatch as ERROR |
| `i2c_address` (multiple devices) | Verify all I2C addresses on same bus are unique; flag duplicates as ERROR |
| `dnp` | Set `component.dnp = true` in schematic.yaml; skip electrical checks for that component |
| `net_voltage` | Use as `confidence: certain` override for net voltage inference |
| `note` | Surface to reviewer for manual inspection; include in REVIEW_REPORT.md |

---

## 7. Implementation Notes

- **Word grouping:** `pdfplumber`'s `extract_words()` splits on whitespace. Tokens like `Address:` and `0x50` on the same baseline should be joined into one phrase. Group tokens where `abs(w1.top - w2.top) < 2` and `w2.x0 - w1.x1 < 30`.
- **Title block noise:** Repeated "Note" words at (659, 211) and (581, 246) across all 35 pages of SCH25678-1E are title block labels, not component annotations. Filter by checking if the annotation y-coordinate is in the border/title block region (typically `y > page_height * 0.85` for bottom title blocks, or compare to a known title block bounding box).
- **Char-fragmented text:** SCH23729-3 title block text is fragmented (`F a a b r i k...`), but component annotation text is clean and readable. The fragmentation only appears in the mirrored/decorative title block font — actual engineering annotations are fine.
- **Coordinate system:** pdfplumber uses PDF points with origin at top-left. `y=0` is the top of the page.
