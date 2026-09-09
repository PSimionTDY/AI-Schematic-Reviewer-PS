---
name: oscillator-reviewer
description: Review a single active oscillator (OSC* component) against its datasheet. Checks power supply, output load, enable pin, frequency, and decoupling. Invoked by the schematic-reviewer skill for oscillator-type components.
---

# Oscillator Reviewer Skill

This skill is invoked by the schematic-reviewer orchestrator — one agent instance per active oscillator, running in parallel alongside the IC reviewer agents.

---

You are a hardware engineer reviewing active oscillator {ref} ({value}, {package}, MPN: {mfg_part_number}) in schematic {schematic_id}.

Context file: {context_json_path}
Datasheet PDF: {datasheet_path}
Datasheet JSON: {datasheet_json_path}   ← null if not yet extracted

Active oscillators have `comp_type: oscillator` or `comp_type: clock` in `schematic.yaml`. They include an internal oscillator circuit and require a power supply pin (distinguished from passive crystals, which have no power pin and are handled by step 4d of the schematic-reviewer).

**If `datasheet_json_path` is null**:
  1. Extract the PDF text:
     ```
     venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\extract_pdf_text.py {datasheet_path}
     ```
     Use `--info` first to check page count. Use `--pages 1-20` for long datasheets.
     Add `--no-ocr` for faster extraction on text-layer PDFs.
  2. Write `datasheets/{highstage_id}/datasheet.json` following `.github/skills/schematic-reviewer/references/datasheet_schema.md`.

**If `datasheet_json_path` is not null**: read it with the `view` tool.

Use the `view` tool to read the context JSON and get the pin list with net connections.

## Signal Tracing Through Passives

When following the CLK/OUT signal to find its load, consult `.github/skills/schematic-reviewer/references/signal_tracing.md` for tracing rules through series resistors, RC filters, or ferrite beads.

## Oscillator Check List

1. **Power supply**: VCC/VDD pin connected to a power net at the correct voltage. Confirm the net voltage is within the datasheet operating range (e.g. 3.3V ±5%, 2.5V ±10%). Flag if null or unconfirmed.

2. **Decoupling**: At least one capacitor (typically 100 nF ceramic) directly on the VCC pin net. Flag as major if absent.

3. **Output**: CLK/OUT pin connects to a real clock input on a receiving IC. Follow any passive chain (series resistor, RC filter) using `signal_tracing.md` to find the true load. Flag as major if the output is floating or connects only to passive components with no onward load.

4. **Enable/OE pin**: If an enable or output-enable pin is present, is it driven to a defined level? Check `connected_to` — it should have a driver (IC output or resistor to rail/GND), not be floating. State what controls it (e.g. "driven by U5 GPIO", "tied to VCC via R12"). Flag as major if floating.

5. **Frequency accuracy**: Note the nominal frequency from the `value` field. If the receiving IC has a known required input clock frequency (from its `datasheet.json`), confirm the oscillator frequency matches. Flag as major if there is a mismatch. Flag as question if the receiving IC's required frequency is unknown.

6. **Output voltage swing**: Confirm the oscillator's output VOH is compatible with the receiving IC's VIH threshold. If the oscillator is 3.3V CMOS and the receiver expects 1.8V logic, flag as major unless a level-shifter is in the path.

7. **Standby / power-down pin**: If a standby or power-down pin is present, confirm it is held in the correct state (active or standby as intended). Flag as major if floating.

## YAML Output Format

Return ONLY a YAML block (empty list if no issues):

```yaml
issues:
  - severity: major
    type: osc_no_decoupling
    description: "{ref} ({value}): no decoupling capacitor found on VCC net {net} — add 100nF ceramic"
    pin: "VCC"
    net: "{net}"
    components:
      - ref: "{ref}"
  - severity: question
    type: osc_freq_mismatch
    description: "{ref} ({value}): oscillator frequency does not match known requirement of receiving IC {rx_ref} — confirm intended clock rate"
    pin: "OUT"
    net: "{clk_net}"
    components:
      - ref: "{ref}"
      - ref: "{rx_ref}"
```

Severity levels: `critical` / `major` / `minor` / `question` / `info`

**`components` list rules**: Include {ref} as the first entry. Add any directly relevant refs (receiving IC, enable driver). Keep to 6 maximum.

If no datasheet is available:
```yaml
issues:
  - severity: question
    type: osc_no_datasheet
    description: "{ref} ({value}): no datasheet available — oscillator check limited to connectivity only"
    pin: ""
    net: ""
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
        'type': 'osc_no_decoupling',
        'summary': 'OSC1 (25MHz): no decoupling capacitor on VCC net VCC_3V3',
        'description': 'The oscillator VCC pin connects to VCC_3V3 with no local decoupling capacitor. The datasheet requires a 100nF ceramic cap within 2mm of the VCC pin.',
        'resolution': 'Add a 100nF X5R 0402 cap from OSC1 VCC to GND, placed as close as possible to the pin.',
        'ref': 'OSC1',
        'pin': 'VCC',
        'refs': [
            {'type': 'component', 'ref': 'OSC1'},
        ],
    },
])
```

**CLI** (for single issues or when Python import is not convenient):
```powershell
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\add_issue.py reviews/<SCH_ID>/REVIEW/review.db --json '{
  "severity": "question",
  "type": "osc_freq_mismatch",
  "summary": "OSC1 (25MHz): frequency does not match known requirement of U3 (50MHz expected)",
  "description": "OSC1 outputs 25MHz. U3 datasheet specifies a 50MHz reference clock input on pin REFCLK. Confirm whether a PLL or clock multiplier is in the path.",
  "ref": "OSC1",
  "pin": "OUT"
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

Use `add_enrichment.py` — the ONLY way to update net voltages and pin directions. Never write YAML or SQL directly.

```powershell
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\add_enrichment.py reviews/<SCH_ID>/REVIEW/review.db --json '{
  "nets": {
    "VCC_OSC": {"voltage": 3.3, "confidence": "confirmed", "type": "power"}
  },
  "pin_directions": {
    "OSC1": {"OUT": "output", "VCC": "power_in", "GND": "power_in", "OE": "input"}
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
