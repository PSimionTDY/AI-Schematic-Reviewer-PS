---
name: inductor-reviewer
description: Review a single power inductor against its datasheet and the surrounding DC-DC converter circuit. Checks current rating, saturation current margin, DCR, inductance value vs design equations, and voltage rating. Invoked by the schematic-reviewer skill for inductor-type components.
---

# Inductor Reviewer Skill

This skill is invoked by the schematic-reviewer orchestrator — one agent instance per power inductor, running in parallel alongside the IC reviewer agents.

---

You are a hardware engineer reviewing power inductor {ref} ({value}, {package}, MPN: {mfg_part_number}) in schematic {schematic_id}.

Context file: {context_json_path}
Datasheet PDF: {datasheet_path}
Datasheet JSON: {datasheet_json_path}   ← null if not yet extracted

**If `datasheet_json_path` is null**:
  1. Extract the PDF text:
     ```
     venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\extract_pdf_text.py {datasheet_path}
     ```
     Use `--info` first to check page count. Add `--no-ocr` for text-layer datasheets.
  2. Write `datasheets/{part_number}/datasheet.json` following `.github/skills/schematic-reviewer/references/datasheet_schema.md`.
     Key fields to extract: `inductance`, `i_sat`, `i_rms`, `dcr`, `voltage_rating`, `package`.

**If `datasheet_json_path` is not null**: read it with the `view` tool.

Use the `view` tool to read the context JSON.

## Inductor Check List

### 1. Identify the converter

From the inductor's net connections, find the switching regulator IC it belongs to — the IC with a SW, LX, or PHASE pin on the same net as one side of the inductor. Read that IC's `datasheet.json` for:
- Switching frequency (`f_sw`)
- Design equation for inductance: `L = V_out × (1 - D) / (f_sw × ΔI_L)` where D = V_out / V_in and ΔI_L is typically 20–40% of I_out
- Recommended inductance range (if stated)

If no switching IC is found on either net, flag as a question.

### 2. Inductance value

Compare the actual inductance value (from `schematic.yaml` `value` field, e.g. `4.7uH`) against the design equation result using the converter IC's parameters. Flag as **major** if the value is more than 20% outside the IC's recommended range or the design equation result. Flag as **info** if within range, noting the calculated value.

### 3. Saturation current (I_sat)

From the inductor's datasheet, confirm:
> I_sat > I_peak = I_out + ΔI_L / 2

Where ΔI_L = (V_out × (1 - D)) / (L × f_sw).

Flag as **major** if I_sat ≤ I_peak (no margin). Flag as **question** if no datasheet is available or I_sat is not stated.

Saturation causes the inductor to lose inductance under load, potentially damaging the converter or downstream circuitry.

### 4. RMS current rating

Confirm the inductor's I_rms rating is greater than the expected continuous load current (I_out). If I_rms is not stated in the datasheet, flag as question. Flag as major if I_rms < I_out.

### 5. DCR (DC resistance)

Note the DCR from the datasheet. If the copper loss is significant:
> DCR × I_out² > 5% of P_out  (where P_out = V_out × I_out)

Flag as **info** with the calculated efficiency impact. This is an observation, not necessarily an error — high DCR may be acceptable for low-power designs.

### 6. Voltage rating

Confirm the inductor's voltage rating exceeds V_in (the supply rail feeding the converter switch node). Flag as **major** if the voltage rating ≤ V_in.

### 7. Ferrite bead vs proper inductor

For converters switching above 1 MHz: a wire-wound power inductor is required. If the component appears to be a ferrite bead (high impedance at MHz frequencies, very low DCR, part number suggests ferrite), flag as **major** — ferrite beads are not suitable as power inductors in switching converters.

## YAML Output Format

Return ONLY a YAML block (empty list if no issues):

```yaml
issues:
  - severity: major
    type: inductor_isat_marginal
    description: "{ref} ({value}): I_sat={i_sat}A from datasheet; I_peak={i_peak}A calculated (I_out={i_out}A + ΔI_L/2={di_half}A) — no saturation margin"
    pin: ""
    net: "{sw_net}"
    components:
      - ref: "{ref}"
      - ref: "{regulator_ref}"
  - severity: info
    type: inductor_dcr_loss
    description: "{ref}: DCR={dcr}mΩ, copper loss at {i_out}A = {loss}mW ({pct}% of P_out={p_out}W)"
    pin: ""
    net: ""
    components:
      - ref: "{ref}"
  - severity: info
    type: inductor_value_ok
    description: "{ref}: L={value} — design equation gives {l_calc}µH (D={d:.2f}, f_sw={f_sw}kHz, ΔI_L={di}A); value within range"
    pin: ""
    net: ""
    components:
      - ref: "{ref}"
      - ref: "{regulator_ref}"
```

Severity levels: `critical` / `major` / `minor` / `question` / `info`

**`components` list rules**: Include {ref} as the first entry. Always include the switching regulator IC ref. Keep to 6 maximum.

If no switching IC is found or no datasheet is available:
```yaml
issues:
  - severity: question
    type: inductor_no_context
    description: "{ref} ({value}): could not identify associated switching regulator — manual review required"
    pin: ""
    net: ""
    components:
      - ref: "{ref}"
```

## DNP (Do Not Populate) Components

**Always check the `dnp` field before reasoning about a connected component.**

Each entry in `connected_to` may carry `"dnp": true` if that component is not populated on the board. Treat DNP components as open-circuit — a DNP pull resistor means the pin is floating, a DNP decoupling cap is absent, a DNP series resistor means the path is open.

**Never raise an issue based on the value of a DNP component.** Flag floating pins or absent paths instead.

## Writing Issues

Use `add_issue.py` — the ONLY way to write issues. Never write YAML directly.

**Python API** (preferred when writing multiple issues at once):
```python
import sys
sys.path.insert(0, r'C:\work\bitbucket\schematic_reviewer\.github\skills\schematic-reviewer\scripts')
from add_issue import write_issues

write_issues('reviews/<SCH_ID>/REVIEW/review.db', [
    {
        'severity': 'major',
        'type': 'inductor_isat_marginal',
        'summary': 'L1 (4.7µH): I_sat=3.2A — no margin over I_peak=3.1A',
        'description': 'Calculated I_peak = I_out (2.8A) + ΔI_L/2 (0.3A) = 3.1A. Datasheet I_sat=3.2A gives only 3% margin — saturation under load peaks will cause inductance collapse.',
        'resolution': 'Replace with a part rated I_sat ≥ 4.0A (e.g. Würth 744043004).',
        'ref': 'L1',
        'pin': '',
        'refs': [
            {'type': 'component', 'ref': 'L1'},
            {'type': 'component', 'ref': 'U5'},
        ],
    },
])
```

**CLI** (for single issues or when Python import is not convenient):
```powershell
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\add_issue.py reviews/<SCH_ID>/REVIEW/review.db --json '{
  "severity": "info",
  "type": "inductor_dcr_loss",
  "summary": "L1: DCR=85mΩ — copper loss at 2.8A = 666mW (4.8% of P_out=14W)",
  "description": "DCR from datasheet is 85mΩ. At I_out=2.8A, copper loss = 85e-3 * 2.8^2 = 666mW, which is 4.8% of P_out=14W. Within acceptable range.",
  "ref": "L1"
}'
```

**Field aliases** (handled automatically — use any of these):
- `title` → `summary`
- `action` or `recommendation` → `resolution`
- `component` or `designator` → `ref`
- `category` or `issue_type` → `type`

**Required fields**: `severity`, `summary`, `description`. `resolution` is strongly recommended.

For an **empty** result (no problems found), call `write_issues(path, [])`.

## Writing Enrichments

Use `add_enrichment.py` — the ONLY way to update net voltages, component roles, and pin directions. Never write YAML or SQL directly.

```powershell
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\add_enrichment.py reviews/<SCH_ID>/REVIEW/review.db --json '{
  "nets": {
    "SW_NODE": {"voltage": null, "confidence": "confirmed", "type": "switching"},
    "VOUT_1V8": {"voltage": 1.8, "confidence": "confirmed", "type": "power"}
  }
}'
```

**Field aliases** (handled automatically):
- `voltage_level` or `rail_voltage` → `voltage`
- `certainty` or `conf` → `confidence`
- `net_type` or `signal_type` → `type`

Omit enrichment calls entirely if you have nothing to confirm.

## Marking the component as verified

**Always do this as the final step**, after writing issues and enrichments.

```python
import sys
sys.path.insert(0, r'C:\work\bitbucket\schematic_reviewer\.github\skills\schematic-reviewer\scripts')
from collect_enrichments import mark_verified_refs
from db_io import get_connection
from pathlib import Path

conn = get_connection(Path('reviews/<SCH_ID>/REVIEW/review.db'))
mark_verified_refs(conn, ['{ref}'])
```
