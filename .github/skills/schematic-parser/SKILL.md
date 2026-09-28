---
name: schematic-parser
description: Parse Cadence Allegro schematic exports to extract component connections, search nets and components, trace signals, and read engineering notes from schematic PDFs. Use this skill whenever the user wants to load a schematic, find what's connected to a pin or net, trace a signal path, or extract notes from a schematic PDF. Trigger on phrases like "parse the schematic", "what is connected to U400", "find the I2C bus components", "trace this signal", "show me all connections for this IC", or "read the engineering notes in the schematic PDF".
---

# Schematic Parser Skill

Parses Cadence Allegro CAD export files and schematic PDFs to extract connection data and engineering notes.

## Python Environment

**Always use the repo venv — never system Python or bare `pip`:**

```
venv\Scripts\python.exe .github\skills\schematic-parser\scripts\<script>.py
venv\Scripts\pip.exe install <package>   # only if a package is truly missing
```

All required packages are already installed in the venv. Do not run `python` without the venv path.

## Use of Subagents

**Run the parser script as a `task` subagent** — netlists for large boards can have thousands of components and take several seconds to parse. This keeps the main context clean:

```
task subagent: python .github/skills/schematic-parser/scripts/schematic_parser.py <schematic_folder>
```

Use `explore` subagents for targeted searches across the generated `schematic.yaml` or `CONNECTIONS_REPORT.md` when you need to answer specific questions without polluting the main context.

## Input Files

Two Allegro export formats are supported — the parser detects automatically:

**Format A (newer): `*View.dat`**
```
<schematic_folder>/allegro/
├── pinView.dat     ← connections: NET_NAME, REFDES, PIN_NUMBER, FUNC_DES
├── netView.dat     ← net list
├── funcView.dat    ← component instances + sheet location (FUNC_DES)
├── compView.dat    ← component list
└── chipsView.dat   ← pin directions (PINUSE) — same format as pstchip.dat
```

**Format B (older): `pst*.dat`**
```
<schematic_folder>/allegro/
├── pstxnet.dat     ← netlist (NET_NAME / NODE_NAME blocks)
├── pstchip.dat     ← component library with PINUSE pin directions
└── pstxprt.dat     ← part placement with XY coordinates
```

See `references/allegro_formats.md` (in the schematic-reviewer skill) for full format details.

## Parsing the Netlist

Run the parser to generate `CONNECTIONS_REPORT.md` in the schematic folder:

```
python skills/schematic-parser/scripts/schematic_parser.py <schematic_folder>
```

The report has two sections:
- **By component** — each component with all its pin-to-net connections
- **By net** — each net with all connected component pins

## Searching Connections

Use the connection finder after the report is generated:

```
# Find all connections for a component
python skills/schematic-parser/scripts/connection_finder.py -c U400

# Find all components on a net
python skills/schematic-parser/scripts/connection_finder.py -n SDA

# Trace from a specific pin through its net
python skills/schematic-parser/scripts/connection_finder.py -t U400 1

# List power nets
python skills/schematic-parser/scripts/connection_finder.py -p

# Interactive menu
python skills/schematic-parser/scripts/connection_finder.py
```

## Reading Engineering Notes from PDF

Claude can read schematic PDFs directly. When asked to find engineering notes:

1. Open the schematic PDF (e.g. `SCH26782-1/SCH26782-1-1.pdf`).
2. Look for text-based notes, title block notes, revision history, and assembly notes on each page.
3. Flag anything marked as a warning, caution, or "do not populate" (DNP).
4. Summarize all notes found and their page locations.

PDF reading does not require a script — use Claude's built-in PDF capability.

## Schematic Text Annotation Extraction

Schematic PDFs contain text annotations placed by engineers near components — e.g. `"Address: 0x50"` next to an EEPROM, or `"PHY addr = 0x02"` next to an Ethernet PHY. These carry critical design intent that is **not present in any Allegro export file** and must be extracted from the PDF.

> **Investigated formats:** Neither View.dat (compView, funcView, pinView, netView, chipsView) nor pst*.dat (pstxnet, pstchip, pstxprt) export any text annotation or free-text field. The Allegro exporter only outputs netlist and placement data. See `references/text_extraction_findings.md` for full investigation details.

### Sources (in order of preference)

1. **Schematic PDF** — only source that contains text annotations. Both tested PDFs have a fully selectable text layer (no OCR needed). Use `pdfplumber` to extract words with pixel-precise bounding box coordinates.
2. ~~Allegro export files~~ — do not contain annotations (confirmed by investigation).
3. OCR on rendered PDF images — fallback only if the PDF has no text layer.

### Extraction approach

```python
import pdfplumber, re

def extract_annotations(pdf_path):
    results = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            # Group same-baseline tokens into phrases
            phrases = group_words_into_phrases(words)  # join if dy<2pt, dx<30pt
            for phrase in phrases:
                ann = classify_annotation(phrase["text"])
                if ann["type"] != "unknown":
                    ref = nearest_refdes(phrase, words)  # closest [A-Z]+[0-9]+ token
                    results.append({
                        "text": phrase["text"],
                        "type": ann["type"],
                        "value": ann.get("value"),
                        "ref": ref,
                        "source": f"pdf_page_{page_num}",
                        "x_norm": phrase["x0"] / page.width,
                        "y_norm": phrase["top"] / page.height,
                    })
    return results
```

### Classification rules

| Type | Patterns | Example |
|------|----------|---------|
| `i2c_address` | `Address:\s*0x[0-9A-Fa-f]+`, `addr\s*=\s*0x...`, `Address:\s*b[01]+` | `Address: 0x50`, `PHY addr = 0x02` |
| `dnp` | `\bDNP\b`, `Do Not Populate`, `NO POP`, `Not Fitted` | `DNP` |
| `net_voltage` | `\d+\.?\d*V`, `VCC=\d+`, `\d+V=\d+\.?\d*V` | `100V=1.89V`, `3.3V` |
| `note` | `routing note`, `fly-by`, `termination`, `Note:` | `Fly-by topology` |

### Confirmed real examples

| Schematic | Page | Annotation | Associated component |
|-----------|------|------------|----------------------|
| SCH25678-1E | 1 | `Address: 0x50` at (300, 397) | U1 (AT24C512C-XHL EEPROM) at (321, 406) |
| SCH25678-1E | 2 | `Address: 0x48` at (408, 479) | I2C device (page 2) |
| SCH25678-1E | 36 | `100V=1.89V` at (322, 99) | Voltage calibration note |
| SCH23729-3 | 4 | `PHY addr = 0x02` | ADIN1300 PHY instance 1 |
| SCH23729-3 | 4 | `PHY addr = 0x04` | ADIN1300 PHY instance 2 |
| SCH23729-3 | 15 | `Address: b1010 [A2][A1][A0][R/W]` | U600 (I2C EEPROM) |
| SCH23729-3 | 15 | `Address: b1000 10[ADDR][R/W]` | U601 (temp sensor) |

### Structured output in schematic.yaml

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
        x_norm: 0.379   # x0 / page_width
        y_norm: 0.649   # top / page_height
        confidence: high
  U600:
    annotations:
      - text: "Address: b1010 [A2][A1][A0][R/W]"
        type: i2c_address
        value: "0b1010xxx"
        source: pdf_page_15
        confidence: high
```

### Verification use cases

- **I2C address stated in schematic → verify against datasheet** address for configured A0/A1/A2 pin states. Mismatch = ERROR.
- **Multiple devices on same I2C bus → verify all addresses are unique.** Duplicate = ERROR.
- **DNP annotation → set `component.dnp = true`** in schematic.yaml; skip electrical checks.
- **Voltage note → use as `confidence: certain`** override for net voltage inference.
- **Note → surface to reviewer** in REVIEW_REPORT.md for manual inspection.

## Workflow Guidance

1. Always parse the netlist first if `CONNECTIONS_REPORT.md` doesn't exist yet.
2. **If the schematic has multiple PCB assembly variants**, run `variant_dnp_extractor.py` to identify DNP components before building `schematic.yaml`:
   ```
   python .github/skills/schematic-parser/scripts/variant_dnp_extractor.py reviews/SCH23729-3
   ```
   This produces `REVIEW/variants.yaml`, which `schematic_builder.py` picks up automatically.
3. **After parsing, re-run `schematic_builder.py`** to build `schematic.yaml`. If a base schematic PDF is present in the review folder it is auto-detected and its JavaScript popup annotations are extracted to enrich component data (rated voltage, tolerance, MPN, manufacturer). This typically takes 1–2 minutes per PDF and populates `rated_voltage` for ~84% of capacitors, reducing `question` issues in the voltage-rating check by ~80%.

   ```bash
   python .github/skills/schematic-parser/scripts/schematic_builder.py reviews/SCH25678-1E
   # Pass --pdf explicitly if auto-detection picks the wrong file:
   python .github/skills/schematic-parser/scripts/schematic_builder.py reviews/SCH25678-1E --pdf reviews/SCH25678-1E/SCH25678-1E-1.pdf
   ```

   **PDF auto-detection rules** (applied to the schematic folder):
   1. Exclude any PDF containing `_PCB_ASSY` in the name (variant assembly drawings).
   2. If multiple non-variant PDFs exist, prefer the one whose stem matches the folder name (case-insensitive).
   3. If no PDF is found, enrichment is skipped silently (non-fatal).

   **Fields added per component** (when available in PDF annotations):

   | Field | Source annotation key | Example |
   |---|---|---|
   | `rated_voltage` | `Voltage` | `50V` → `50.0` |
   | `tolerance` | `Tolerance` | `10%` → `10.0` |
   | `part_number` | `Part Number` | `IC1008360` |
   | `mfg_part_number` | `mfg1partnumber` / `mfg2partnumber` | `GRM188R71H104KA93D` |
   | `manufacturer` | `mfg1` | `Murata` |

   Voltage strings of `"0V"` or empty are ignored (treated as unrated). European notation (`6V3` → 6.3 V) is supported.

4. **Before trusting the PDF as ground truth, run `pdf_consistency_check.py`** (see PDF Consistency Checks below).
5. After parsing, use the connection finder for targeted searches.
6. For a full review, read the PDF alongside the netlist data.
7. When a component appears in connections but is unfamiliar, note its part number for datasheet lookup via the `dokarkiv` skill.

## Variant DNP Extraction

Multi-variant schematics (e.g. SCH23729-3) have one base PDF plus one variant PDF per PCB assembly:

```
reviews/SCH23729-3/
├── sch23729-3.pdf                           ← base (Core Design, all refs)
├── sch23729-3_PCB_ASSY1017453-3.pdf         ← variant for assembly 1017453-3
└── sch23729-3_PCB_ASSY1017636-2.pdf         ← variant for assembly 1017636-2
```

**Naming convention**: `{schematic_name}_PCB_ASSY{number}.pdf`

### DNP detection

Cadence OrCAD CIS exports mark DNP components in variant PDFs using the `NC` (Not Configured) field in JavaScript popup annotations. A non-empty `NC` value (`"1"` or `"2"`) means the component is DNP for that variant.

Run the extractor to generate `REVIEW/variants.yaml`:

```bash
python .github/skills/schematic-parser/scripts/variant_dnp_extractor.py reviews/SCH23729-3
```

Output `REVIEW/variants.yaml` format:

```yaml
base_pdf: sch23729-3.pdf
base_nc_refs:                      # DNP refs common to all variants (core design)
  - D350
  - U350
variants:
  - assembly: PCB_ASSY1017453-3
    pdf: sch23729-3_PCB_ASSY1017453-3.pdf
    dnp_method: nc_field           # detection method used
    dnp_refs:
      - D350
      - U350
      - ...
  - assembly: PCB_ASSY1017636-2
    pdf: sch23729-3_PCB_ASSY1017636-2.pdf
    dnp_method: nc_field
    dnp_refs:
      - ...
```

**DNP detection methods** (tried in order):
1. `nc_field` — primary: uses the `NC` field present in OrCAD CIS variant exports
2. `missing_ref` — fallback: ref present in base PDF but absent from variant PDF

### Variant data in schematic.yaml

After running `variant_dnp_extractor.py`, re-run `schematic_builder.py` to embed variant data:

```bash
python .github/skills/schematic-parser/scripts/schematic_builder.py reviews/SCH23729-3
```

The generated `schematic.yaml` includes variant data at two levels:

**Meta section** — summary per assembly:
```yaml
meta:
  schematic_id: SCH23729-3
  variants:
    - assembly: PCB_ASSY1017453-3
      pdf: sch23729-3_PCB_ASSY1017453-3.pdf
      dnp_count: 10
    - assembly: PCB_ASSY1017636-2
      pdf: sch23729-3_PCB_ASSY1017636-2.pdf
      dnp_count: 10
```

**Per-component** — DNP flag per assembly:
```yaml
components:
  D350:
    value: ZHCS750TA
    comp_type: diode
    variants:
      PCB_ASSY1017453-3:
        dnp: true
      PCB_ASSY1017636-2:
        dnp: true
  R1:
    variants:
      PCB_ASSY1017453-3:
        dnp: false
      PCB_ASSY1017636-2:
        dnp: false
```

If no `variants.yaml` exists, components have no `variants` field (backward compatible).



## PDF Consistency Checks

Before trusting the PDF as ground truth, run `pdf_consistency_check.py` to verify:

```bash
python .github/skills/schematic-parser/scripts/pdf_consistency_check.py reviews/SCH25678-1E
```

This checks:
- **Revision match** — revision on title block matches folder name revision (e.g. `SCH25678-1E` → Rev E)
- **Sheet numbering** — Sheet X of N is sequential and consistent across all pages
- **PDF staleness** — PDF is not older than any Allegro export `.dat` file
- **Schematic ID presence** — the schematic number appears on the title block

Output is printed to stdout and written to `REVIEW/issues_pdf.yaml`.

**Known title block behavior**: Reson/Teledyne schematics use OrCAD's custom title block (`TitleBlock5`) which renders some fields (including Rev) as vector graphics rather than text. If the revision check reports `pdf_revision_not_found`, verify manually that the revision shown in the PDF title block matches the folder name.

## .DSN File Format

The Cadence OrCAD `.DSN` file is a binary OLE2 Compound Document (not XML, not text). See `references/dsn_format_findings.md` for full investigation details.

**Best use of DSN**: extract OrCAD version and sheet names via `olefile`. Do not attempt to parse component properties from it — use Allegro exports instead.

## Net Voltage Resolution Procedure

When `schematic.yaml` contains nets with `confidence: unknown`, Claude should:

1. **Check auto-resolved nets first**: `schematic_builder.py` already traces pull-up resistors automatically. Run it again if needed.

2. **For remaining unknowns**, trace manually:
   - Find the net in `schematic.yaml` and list its `connections`
   - For each connected IC pin with `direction: output` or `power_out`, look up that component's VCC/VDD pin → its net voltage is the logic voltage
   - For open-drain signals (direction: open_drain_out), trace the pull-up resistor's power net
   - For connector pins, ask the user: "What is the voltage range on connector J1 pin 2 (net SENSOR_OUT)?"

3. **Record your reasoning** in the net's `review.comments` field in schematic.yaml

4. **Set confidence**:
   - `certain`: directly driven by a regulator output or labeled power rail
   - `doubtful`: inferred from context (pull-up rail, IC supply voltage)
   - `unknown`: truly cannot be determined — leave as-is and flag as question issue

5. **Never guess silently** — always add a comment explaining the inference or asking the user
