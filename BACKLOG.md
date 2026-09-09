# Feature Backlog

Track feature requests and ideas here. Implemented items move to the relevant skill or plan.md.

---

## Active Features (being built now)

| ID | Feature | Branch |
|----|---------|--------|
| track-viewer | Interactive review.html viewer (graph + PDF split pane) | feat/viewer |
| track-highstage | Schematic downloader, DNP detection, datasheet access probe | feat/highstage |
| track-verification | Cap/resistor verification, issues.yaml | feat/verification |
| track-dsn | DSN format investigation + PDF consistency checks | feat/dsn-investigation |

---

## DSN-001 — Cadence OrCAD .DSN Format Investigation

**Status**: Investigated ✅ (see `skills/schematic-parser/references/dsn_format_findings.md`)  
**Branch**: feat/dsn-investigation

### Findings

The `.DSN` file is a **Cadence OrCAD Capture proprietary binary** using the OLE2 Compound Document container (magic bytes `D0 CF 11 E0 A1 B1 1A E1`).

**Extractable without reverse engineering**:
- OrCAD version & license — from `DsnStream` (JSON): `{"InstalledVersionBase":"17.4-2019",...}`
- Sheet/page names — from OLE stream paths: `Views/<SheetName>/Pages/<SheetName>`
- Component ref des (~1400 in SCH25678-1E, ~2000+ in SCH23729-3) — string scan on `Views/Top/Hierarchy/Hierarchy`
- Net names and bus names — string scan on `Views/*/Pages/*` streams
- Variant group names (No Mount, Variant1, etc.) — from `CIS/VariantStore/VariantNames`
- DNP component object IDs — from `CIS/VariantStore/Groups/No Mount/*` (numeric IDs, not ref des)

**Not extractable without reverse engineering**:
- Component values, voltage ratings, tolerances — in binary structs
- MPN (manufacturer part number) — in `CIS/VariantStore/BOM/*/BOMPartData` binary blob
- Pin-to-net connectivity — binary wiring topology format
- XY coordinates — binary format

**Recommendation**: Use Allegro export files + PDF for all current review work. Only parse DSN for version/sheet-name metadata. Full property extraction requires Cadence OrCAD COM/Tcl scripting API.

---

## Backlog

### TEXT-001 — Schematic text block extraction
**Priority**: High

Read text annotations from schematics and use them as structured verification inputs.

**Example**: In SCH25678-1E, there is text above AT24C512C that reads `"Address: 0x50"`. This should be:
1. Extracted and stored in `schematic.yaml` under the relevant component or net
2. Verified against the datasheet (does the actual configured address match 0x50?)
3. Cross-referenced in I2C bus checking (is this address unique on the bus?)

**Sources to investigate**:
- PDF text layer (PyMuPDF / pdfplumber) — schematic PDFs often have selectable text
- Allegro export files — some tools embed text/annotation attributes in export files
- `pstxprt.dat` or `funcView.dat` — may carry schematic text objects

**Structured output in schematic.yaml**:
```yaml
components:
  U42:                          # AT24C512C
    ...
    annotations:
      - text: "Address: 0x50"
        type: i2c_address       # auto-classified
        value: "0x50"
        source: pdf_page_4      # or allegro_file
```

**Verification use**:
- Check `0x50 == datasheet address for configured A0/A1/A2 pins`
- Check `0x50` is unique on the I2C bus it belongs to

**Todos**: `feat-text-extraction` → `verify-i2c-addresses`

---

### TEXT-003 — Netlist func_des as PDF anchor for component location
**Priority**: Medium  
**Branch**: `liteparse` (extend when working on PDF location improvements)

Currently `build_comp_page_map()` scans the PDF for refdes text ("R20", "U39B") using pdfplumber.
A more reliable alternative: use the `func_des` labels (e.g. "F67", "F7", "F8") from the Allegro
netlist (`pinview.dat` / `schematic.yaml`) to anchor component positions in the PDF.

**Why this could be better**:
- `func_des` labels are always present in the PDF as schematic gate labels (consistent placement)
- Each gate/sub-part has its own `func_des` — F7 = U39A, F8 = U39B — so sub-part selection is exact
- `pinview.dat` gives `func_des` **per pin**, so we'd know exactly which gate each pin lives on
- No need for the multi-part suffix scan ("U39B" → "U39") or PIN_POS_MAP heuristics
- Would replace the current PDF text scan + PIN_POS_MAP approach with a single clean lookup

**Approach**:
1. Parse `pinview.dat` to build `{ref → {pin → func_des}}` (already has it per row)
2. Scan PDF for `func_des` tokens (F67, F7, F355, etc.) to get `{func_des → (page, x_pct, y_pct)}`
3. In `generate_review_html.py`: inject this as `FUNC_DES_MAP`
4. In `review.html`: for any issue with a pin, look up `FUNC_DES_MAP[comp.func_des]` first

Also investigate: can LiteParse's JSON output (bounding boxes) give better/faster func_des extraction
than pdfplumber char scanning?

---


**Priority**: Medium

Related to text extraction. Schematics often have "DNP" written next to components as text on the schematic page (not as an attribute in Allegro). This would be a fallback for DNP detection if Allegro attributes don't carry it.

---

### VIEWER-001 — "Open in PDF" split view per issue
**Status**: Being implemented in feat/viewer  
Design agreed: left pane = component A location, right pane = component B, bottom strip = issue + severity.

---

### GRAPH-005 — Pin numbers on graph edges
**Status**: Implemented ✅

- Edges show pin numbers as tooltips (`title`) on hover — always visible, no clutter
- "Pin #" toggle button in graph controls adds visible `label` on each edge
- Helps trace signal paths without leaving the graph view

---

### GRAPH-001 — Standalone graph viewer (future)
**Priority**: Low  
A separate web app (or Electron app) where you can explore the full schematic graph interactively, add comments/questions/errors per node, and export those as issues. Currently planned as a tab in review.html.

---

---

## Viewer Bugs

### VIEWER-002 — Graph node bullseye highlight makes labels unreadable
**Status**: Pending

The vis.js selection ring (concentric rings / "bullseye") drawn on clicked graph nodes makes the node label unreadable. The red border circle highlight is fine — only the extra bullseye rings need to be removed.

---

### VIEWER-003 — PDF component marker off-position
**Status**: Pending

In review.html, the red overlay marker placed on the PDF to highlight a component can be positioned far from the actual component. The COMP_PAGE_MAP-based coordinate system needs to be fixed generically.

---

### VIEWER-004 — "Component B" pane shown when no second component exists
**Status**: Pending

The Issues tab always shows Component A and Component B PDF panes, even when an issue only references a single component. Unused panes should be hidden.

---

### VIEWER-005 — Multi-component panes limited to A/B
**Status**: Pending

Issues that involve 3+ components (e.g. I2C bus checks connecting multiple ICs across pages) can only display two PDF panes. Needs dynamic Component C, D, E tabs.

---

## Graph Features

### GRAPH-002 — Graph traversal only goes one level deep
**Status**: Pending

`computeNeighbourhood()` uses a fixed depth (default 1). Should use unbounded BFS that stops automatically at: (1) power nets, (2) ICs — with an optional "pass through ICs" toggle so level translators and similar can be traversed.

---

### GRAPH-003 — Graph does not fit to visible nodes after depth filter
**Status**: Pending

After clicking a node and applying the depth filter, the graph view doesn't re-fit to the now-visible subgraph. Should call `network.fit()` to visible nodes to eliminate blank space.

---

### GRAPH-004 — Single-connection nets shown as hub nodes instead of labelled edges
**Status**: Pending

Nets with exactly 2 component connections (point-to-point signals) should be collapsed into a single labelled edge between those two components. Nets with 3+ connections remain as hub nodes. This significantly reduces graph clutter.

---

### ANNOT-001 — PDF annotation component data extraction
**Status**: Investigated ✅ / Implemented ✅  
**Finding**: Cadence Allegro schematic PDFs contain 19,040+ JavaScript `/Link` annotations per
schematic. Each component annotation carries the full CIS database record: voltage rating,
tolerance, power rating, dielectric type, PCB footprint, Highstage internal part number, and
primary + secondary MPN.

**Coverage** (SCH25678-1E, 1,684 components):
- Voltage rating: 84% of all components, ~100% of passives
- Tolerance: 74% of all components, ~100% of passives
- MPN: 88% (both Yageo/Vishay alternatives often present)
- Highstage PN: 97% (can construct datasheet URL without a search)

**Implementation**: `pdf_annotation_extractor.py` extracts all annotations and writes a
`pdf_annot:` section into `schematic.yaml` for each component. Takes ~15s per schematic.

**Impact**: Highstage lookups can be **skipped entirely** for resistors and capacitors in
voltage/power/tolerance checks. Only needed for ICs (pin verification) and components
where the annotation fields are empty.

**Script**: `.github/skills/schematic-parser/scripts/pdf_annotation_extractor.py`  
**Docs**: `.github/skills/schematic-parser/references/pdf_annotation_findings.md`

---

## Done / Implemented

- [x] Dual-format Allegro parser (View.dat + pst*.dat)
- [x] schematic.yaml with net voltages, component roles, pin directions
- [x] 3 Claude skills with auto-discovery
- [x] schematic_builder.py with role detection

---

*Add new requests below with a unique ID (TEXT-xxx, VIEWER-xxx, VERIFY-xxx, etc.)*


## Human written input
If multiple ICs/passives near each-other, dont show two PDFs, but mark on same schematic. Mark everything on same pages.