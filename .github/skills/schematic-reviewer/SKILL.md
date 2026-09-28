---
name: schematic-reviewer
description: Perform a structured AI-assisted review of a Cadence Allegro schematic. Use this skill whenever the user wants to review, verify, or audit a schematic design. Trigger on phrases like "review this schematic", "check the design", "verify the components", "look for issues", "check I2C addresses", "verify power rails", or any request to perform a quality check on schematic files. This skill orchestrates the full review workflow — parsing, datasheet lookup, and systematic checking — using the review checklist.
---

# Schematic Reviewer Skill

Guides Claude through a complete, systematic schematic review using the Allegro parser, DOKARKIV datasheet lookups, and the structured checklist.

Read `references/review_checklist.md` now — it defines every check to perform and in what order.

## Python Environment

**Always use the repo venv — never system Python or bare `pip`:**

```
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\<script>.py
venv\Scripts\pip.exe install <package>   # only if a package is truly missing
```

All required packages are already installed in the venv. Do not run `python` without the venv path.

## Use of Subagents

**Always run heavy tasks as subagents** to keep the main conversation context clean and allow parallel execution:

- Parsing the netlist → `task` subagent
- Running verification scripts (capacitor, resistor checks) → `task` subagents, launched in parallel
- Downloading multiple datasheets → `task` subagent
- Generating `review.html` → `task` subagent

Use the main conversation only for reasoning, asking the user questions, and presenting results. Reserve `general-purpose` subagents for complex multi-step operations that require decision-making.

## PDF Extraction Backend: LiteParse

This branch replaces `pdfplumber`/`PyMuPDF` with **LiteParse** (`lit` CLI) in `extract_pdf_text.py`.

### OCR Setup (one-time)

LiteParse's Tesseract OCR requires `eng.traineddata` locally — **without it, OCR hangs indefinitely**
because 31 workers (one per CPU) all try to download the 5 MB file from jsdelivr CDN simultaneously.

**Pre-download tessdata once:**

```powershell
$dir = "$env:USERPROFILE\.tessdata"; New-Item -Force -ItemType Directory $dir | Out-Null
$gz = "$dir\eng.traineddata.gz"
Invoke-WebRequest "https://cdn.jsdelivr.net/npm/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz" -OutFile $gz
$in = [IO.File]::OpenRead($gz); $out = [IO.File]::Create("$dir\eng.traineddata")
[IO.Compression.GZipStream]::new($in,[IO.Compression.CompressionMode]::Decompress).CopyTo($out)
$out.Close(); $in.Close(); Remove-Item $gz
```

**Set env var** (add to your shell profile for persistence):

```powershell
$env:TESSDATA_PREFIX = "$env:USERPROFILE\.tessdata"
```

### OCR vs no-OCR

| Mode | Flag | Speed | Best for |
|------|------|-------|---------|
| No OCR (default) | `--no-ocr` (default) | ~500ms | Text-layer PDFs (datasheets, schematics) |
| OCR | `--ocr` | ~6s | Scanned/image PDFs only |

**Default is `--no-ocr`** — all datasheets in this repo have embedded text layers. Use `--ocr` only for scanned PDFs where `--no-ocr` produces empty or garbled pages.


A full review follows a **multi-pass, parallel-flow pipeline** backed by `review.db` (SQLite). Each stage writes results to `review.db` and marks its pipeline status when complete. Stages with no dependency on each other run in parallel.

## Pipeline overview

```
build → datasheets ┬→ T1(power-gating) ─┐
                   │                                          ├→ enrich → T2(DC-DC) ─┐
                   │                                          │                       ├→ enrich → T3(LDO) ─┐
                   │                                          │                       │                     ├→ enrich → T4(seq) ─┐
                   │                                          │                       │                     │                    ├→ enrich → T5(clk) ─┐
                   │                                          │                       │                     │                    │                    ├→ enrich → T6(compute) ─┐
                   │                                          │                       │                     │                    │                    │                        ├→ enrich → T7(intf) ─┐
                   │                                          │                       │                     │                    │                    │                        │                      ├→ enrich → T8(stor) ─┐
                   │                                          │                       │                     │                    │                    │                        │                      │                      └→ T9(glue) → enrichment → verification ─┐
                   ├→ temp_check ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
                   ├→ supply_check ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤→ report
                   └→ bom_check ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
                                                                                                                                                                                                                                      │
                                                                                                                              enrichment → power_estimate ────────────────────────────────────────────────────────────────────────────┘
```

**IC review runs in 9 sequential tiers** (T1→T9), each tier fully parallel within itself.
After each tier: run `collect_ic_results.py` + `collect_enrichments.py` to propagate confirmed
voltages before the next tier starts.

| Stage | Script(s) | Writes to review.db | Order |
|-------|-----------|---------------------|-------|
| `build` | `schematic_builder.py` | nets, components, pins | first |
| `datasheets` | `find_datasheet.py` (dokarkiv skill) | datasheet_fields on components | after build |
| `ic_review T1` | power-gating agents | issues, enrichments (output voltages) | after datasheets |
| `ic_review T2` | DC-DC converter agents | issues, enrichments | after T1 enrichment |
| `ic_review T3` | LDO agents | issues, enrichments | after T2 enrichment |
| `ic_review T4` | Sequencer/supervisor agents | issues, enrichments | after T3 enrichment |
| `ic_review T5` | Clock/oscillator agents | issues, enrichments | after T4 enrichment |
| `ic_review T6` | Compute agents (FPGA, CPU) | issues, enrichments | after T5 enrichment |
| `ic_review T7` | Interface agents (PHY, USB) | issues | after T6 enrichment |
| `ic_review T8` | Storage & sensor agents | issues | after T7 enrichment |
| `ic_review T9` | Glue logic & level shifters | issues | after T8 enrichment |
| `enrichment` | `collect_ic_results.py`, `collect_enrichments.py` | verified flags, propagated voltages | after T9 |
| `verification` | `verify_capacitor_voltage.py`, `verify_resistor_power.py` | issues | after final enrichment |
| `temp_check` | `verify_temperature_ratings.py` | issues | parallel with ic_review tiers |
| `supply_check` | `check_price_availability.py`, `check_obsolescence.py` | issues | parallel from build |
| `bom_check` | `verify_bom.py` | issues | parallel from build |
| `power_estimate` | `estimate_power_consumption.py` | issues | after final enrichment |
| `report` | `validate_issues.py` → `generate_review_html.py` | — | after all above |

**Do not write REVIEW_REPORT.md until `validate_issues.py` exits 0 and `review.html` exists.**

## Checking pipeline status

```bash
python .github/skills/schematic-reviewer/scripts/pipeline_status.py reviews/<SCH_ID>/REVIEW/review.db
```

Prints a topologically-sorted table showing `[ ]` pending, `[→]` running, `[✓]` done, `[!]` failed, and `[-]` skipped for every stage. Use `--json` for machine-readable output.

## Voltage confidence levels

Net voltages in `review.db` carry a `confidence` field using this ordered scale:

| Level | Meaning |
|-------|---------|
| `unknown` | No voltage information at all |
| `hint` | Name-based inference only (e.g. net named `VCC_3V3`) |
| `doubtful` | Inferred but unreliable (e.g. shared bus, multiple drivers) |
| `inferred` | Reasonable guess from context |
| `deduced` | Traced from the actual circuit driver by an IC/inductor review agent |
| `calculated` | Computed analytically (e.g. resistor divider formula) |
| `confirmed` | Explicitly confirmed by user or IC agent via `add_enrichment.py` |
| `certain` | From a reference source (e.g. GND = 0 V by definition) |

**IC review agents must not rely on `hint`-confidence voltages** for any safety-relevant check. `hint` = name-based inference only; `deduced` or higher = traced from a real circuit driver by an agent. If a net's confidence is `hint` or lower, the agent should flag the uncertainty as a `question` issue rather than silently assuming the voltage.

**Voltages are set exclusively by review agents** — there is no automatic deduction step. Agents read datasheets, trace circuits, and write voltages via `add_enrichment.py` with `confirmed` or `deduced` confidence. The `deduce_net_voltages.py` script is deprecated and must not be run.

**Net typing and driver rules:**
- Nets start as `type='unknown'`. Only IC reviewer agents may promote a net to `power`, `signal`, `i2c`, etc. after verified circuit analysis.
- Resistors are never voltage drivers. If a voltage propagates through a low-value resistor, the original upstream IC or rail remains the `voltage_source_ref`.
- When the tolerance band is known, IC agents should write `voltage_min` and `voltage_max` alongside nominal `voltage`.
- When an agent confirms a rail driver, it should also write `voltage_source_ref` (and `voltage_source_pin` when known) so passive propagation preserves the true source.

## YAML snapshots (debug)

The primary store is `review.db`. For human inspection, generate YAML snapshots:

```bash
python .github/skills/schematic-reviewer/scripts/dump_yaml.py reviews/<SCH_ID>/REVIEW/review.db
```

Writes `schematic.yaml` and `issues.yaml` alongside the DB. These are **read-only debug outputs** — never modify them directly; write back to `review.db` only.

You don't have to do a full review every time. If the user asks for a targeted review (e.g. "just check the I2C buses" or "verify U400"), jump directly to that section of the checklist.

## Starting a Review

Ask the user for the schematic number (e.g. `SCH25678-1E`) if not already provided.

### Step 0 — Locate or fetch the schematic

Check whether the review workspace already exists locally:

```
reviews/<SCH_ID>/allegro/   ← Allegro export files (pstxnet.dat or pinView.dat etc.)
```

**If it exists** → proceed to Step 1.

**If it does not exist** → ask the user to provide the schematic export files (Allegro `pstxnet.dat`/`pstchip.dat` or `View.dat`) and the schematic PDF, and place them under `reviews/<SCH_ID>/allegro/`. This repository no longer fetches schematics automatically from any external system.

### Step 1 — Parse and build review.db

```bash
python .github/skills/schematic-parser/scripts/schematic_builder.py reviews/<SCH_ID>
```

This auto-detects the Allegro format (View.dat or pst*.dat), parses all netlist data, enriches with PDF annotations (voltage ratings, MPN, tolerance), and writes `reviews/<SCH_ID>/REVIEW/review.db` (marks pipeline stage `build` done). Initialises all pipeline stages in the DB.

### Step 1c — Load BOM (part numbers)

**Do this before downloading datasheets** — it populates `part_number` on every component, which the DOKARKIV datasheet finder needs.

`schematic_builder.py` (Step 1) already reads any BOM CSV present in `reviews/<SCH_ID>/REVIEW/` and populates `part_number` on matching components automatically (see `load_bom_ids` in `schematic_builder.py`). If no BOM CSV is present, ask the user to provide one with ref-designator and internal part number columns (e.g. `Ref Des`, `Part ID`), or to fill in part numbers directly on the components in `review.db`.

### Step 1b — Confirm voltage rails (interactive)

**Do this before IC review — IC agents need accurate voltage context.**

Query `review.db` for power nets still at `unknown` or `hint` confidence:

```sql
SELECT name, voltage, confidence FROM nets
WHERE type IN ('power','gnd') AND confidence IN ('unknown','hint','doubtful')
ORDER BY name;
```

Present a table to the user:

| Net | Current voltage | Inferred from |
|-----|----------------|---------------|
| VCC_5V | null | — |
| ISO_VCC_+5V_DIG | null | — |

Ask: *"Please confirm the voltage for each of these nets (or say 'skip' if unknown)."*

Once confirmed, write them back to `review.db` using `db_io.update_net` or via the `enrich_schematic.py` patch mechanism:

```bash
# Write patch.yaml with confirmed voltages, then:
python .github/skills/schematic-reviewer/scripts/enrich_schematic.py reviews/<SCH_ID> --patch patch.yaml
```

Patch format:
```yaml
nets:
  VCC_5V:
    voltage: 5.0
    confidence: confirmed
    type: power
  ISO_GND_0V:
    voltage: 0.0
    confidence: confirmed
    type: gnd
```

Delete `patch.yaml` after applying. If the user cannot confirm a voltage, leave it as-is — IC agents may resolve it during review.

### Step 2 — Extract variant DNP info (if multiple PDFs present)

```bash
python .github/skills/schematic-parser/scripts/variant_dnp_extractor.py reviews/<SCH_ID>
```

Only needed if the schematic has multiple PCB assembly variants. Detects DNP components and component substitutions per variant, writes `REVIEW/variants.yaml`.

### Step 3 — Read the PDF

Open the schematic PDF (`reviews/<SCH_ID>/<SCH_ID>.pdf`) and extract engineering notes (see checklist Section 7).

### Step 4 — Find datasheets (ICs, transistors, diodes)

Datasheets are cached at **`datasheets/<PART_NUMBER>/`** in the repo root — shared across all schematic reviews. Look up datasheets for all reviewable component types: ICs, transistors (GaN FETs, MOSFETs, BJTs), and diodes (including bootstrap and TVS diodes). Internal part numbers use a letter prefix followed by digits (e.g. `T19200`); the DOKARKIV file share folder uses only the numeric portion (`19200`).

Batch-copy all reviewable parts in one call:
```bash
python .github/skills/dokarkiv/scripts/find_datasheet.py \
    --bom reviews/<SCH_ID>/REVIEW/bom.csv \
    --filter "U,IC" \
    --output datasheets/
```

Or copy a single part's datasheet (any type):
```bash
python .github/skills/dokarkiv/scripts/find_datasheet.py <PART_NUMBER> --output datasheets/
# Example:
#   T19200
```

**Do NOT use `PINUSE` from the Allegro `chipsview.dat` — these values are unreliable.** The datasheet is the authoritative source for pin types (VCC, GND, input, output, open-drain, etc.).

For each component datasheet, extract the key parameters and cross-reference against the schematic connections to verify:
- All VCC/VDD/AVDD/DVDD pins connected to an appropriate power net
- All GND/AGND/PGND/DGND pins connected to a GND net  
- Unused digital inputs not floating (tied to rail or GND per datasheet)
- Open-drain / open-collector outputs have a pull-up resistor
- Enable, shutdown, and reset pins are driven (not floating)
- For transistors: Vds/Vce/Vgs ratings vs. actual operating voltages
- For diodes: reverse voltage rating vs. actual blocking voltage (e.g. bootstrap diodes must exceed full switch node swing)

**`datasheet.json` — persistent AI extraction cache**

When a datasheet is copied to `datasheets/<PART_NUMBER>/`, check whether `datasheets/<PART_NUMBER>/datasheet.json` already exists:

- **If `datasheet.json` exists** → use it directly. All scripts (`verify_ic_pins.py`, `prepare_ic_context.py`) read from it automatically. Skip re-extraction.
- **If `datasheet.json` does not exist** → the per-component review agent (Step 4c) will write it after reading the PDF. Once written, all subsequent reviews for that part use the cached file instead of re-reading the PDF.

`datasheet.json` follows the canonical schema defined in `references/datasheet_schema.md`. It contains pins, required external components, design equations, and notes — all of which are used by the automated verification scripts.

### Step 4b — Prepare IC review contexts

```bash
python .github/skills/schematic-reviewer/scripts/prepare_ic_context.py reviews/<SCH_ID>
```

Generates `REVIEW/ic_contexts/<ref>.json` for every IC — one file per IC containing its full pin/net context and datasheet path. Also writes `REVIEW/ic_contexts/_index.json` listing all ICs.

### Step 4c — IC review: hierarchical tier order

IC review follows a **power-first** order. Each tier's agents run in parallel within the tier, but
tiers run sequentially — Tier N must complete and have enrichments collected before Tier N+1 starts.
This ensures each downstream tier has accurate, agent-confirmed voltages from the stages above it.

After **each tier** completes, run the collection scripts before starting the next:

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\collect_ic_results.py reviews/<SCH_ID>
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\collect_enrichments.py reviews/<SCH_ID>
```

`collect_enrichments.py` propagates agent-confirmed net voltages back into `review.db`, so the next
tier's agents see accurate supply voltages instead of `hint`-confidence guesses.

#### Tier classification

| Tier | Function | Typical parts |
|------|----------|---------------|
| **T1** | Input protection & power gating | eFuses (TPS25961, TPS25970), TVS diodes, load switches on the main input rail |
| **T2** | DC-DC converters & inductors | Switching regulators (LT8625, LT8627, LTC3634, TPS5x), coupled inductors |
| **T3** | LDOs & linear regulators | LDO post-regulators (TLV758, TPS7A26, TPS7A10) |
| **T4** | Power sequencing & supervision | Sequencers (LM3880), supervisors (TPS3430), voltage references (REF30xx), small load switches |
| **T5** | Clocks & oscillators | Clock generators (SI5332), active oscillators (SiT16xx), passive crystals |
| **T6** | Main compute | FPGAs (XCKU, XC7), processors (QCS8550), DDR memory |
| **T7** | High-speed interfaces | Ethernet PHYs (DP83867, GPY215), USB bridges (FT2232, PTN3222, TS3USB30), LVDS (SN65LVDS4), RS-485/232 |
| **T8** | Storage & sensors | Flash (MX25V), EEPROM (AT24C, 93LC46), temperature sensors (TMP110, SHT30) |
| **T9** | Glue logic & level shifters | Buffers, gates, muxes (SN74AUC1G126, SN74LVC1G3157, 74LVC1G08, TXU0304, TXS0102, SN74AVC4T245) |

**Classify by function, not just type prefix.** A load switch IC is Tier 1, not Tier 7 just because
it's an IC. A DC-DC controller drives Tier 2. When uncertain, assign the earlier (lower-numbered)
tier — it is always safe to review power before signal.

#### Grouping agents

**Launch one agent per unique part number** (not one per instance). For example, if `TPS25961DRVR`
(T1020431) appears as U5, U6, U12, U14, U15, U4300, launch a single agent with all six context
JSONs. The agent reviews all instances, writing one issue per ref where relevant.

#### Choosing the agent skill

- `ic`, `transistor`, `diode`, `zener`, `led` → **`.github/skills/ic-reviewer/SKILL.md`**
- `oscillator`, `clock` → **`.github/skills/oscillator-reviewer/SKILL.md`**
- `inductor` → **`.github/skills/inductor-reviewer/SKILL.md`**

Provide each agent with:
- The full contents of `.github/skills/<reviewer>/SKILL.md` as its system prompt
- All context JSON paths for that part number: `REVIEW/ic_contexts/{ref}.json`
- The schematic ID, refs, value, package, MPN from `_index.json`

### Step 4d — Crystal agent review (passive crystals only)

> **Note**: Active oscillators (`comp_type: oscillator` or `clock`, typically OSC* refs with a VCC power pin) are handled in step 4c via **`.github/skills/oscillator-reviewer/SKILL.md`**. Step 4d covers **passive crystals only** (XTAL refs with no power pin).

Passive crystals are treated like ICs — one `general-purpose` sub-agent per component, run in parallel with `mode=background`.

For each crystal (XTAL) in `review.db`:
- Read the component's context JSON (pins, nets, connected components)
- If a datasheet is available (from `datasheets/<part_number>/`), read it
- Check:
  1. Load capacitors present on both XTAL pins, value matches datasheet spec (C_L formula)
  2. Oscillator type correct (XTAL vs active oscillator — check if power pin present)
  3. Frequency consumer (MCU/FPGA clock pin) connected to correct net
  4. Series resistor present if specified (for drive level limiting)
  5. Ground guard ring noted (schematic note or layout note present)

Write results to `REVIEW/review.db` using `add_issue.py` (never write YAML directly).

### Step 4e — SPI bus verification (agent-based)

For each SPI bus identified in `review.db`, spawn one `general-purpose` agent:
- List all devices on the bus (those sharing MOSI/MISO/SCK nets)
- For each device: read its `datasheet.json` SPI interface section
- Check:
  1. Each device has a unique, dedicated CS line (not shared)
  2. CPOL/CPHA mode compatible across all devices on the bus
  3. Maximum clock frequency: use the lowest limit across all devices
  4. MISO line has pull-up or pull-down if any device can tri-state it
  5. Voltage levels compatible across all devices

Write results to `REVIEW/review.db` using `add_issue.py` (never write YAML directly).

### Step 5 — Run verification scripts (parallel tracks)

**`verification` track — run AFTER enrichment.** The scripts now see agent-confirmed voltages and propagated net types, which eliminates the majority of false positives. Launch as parallel `task` subagents:

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\verify_capacitor_voltage.py reviews/<SCH_ID>
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\verify_resistor_power.py reviews/<SCH_ID>
```

**`temp_check` track — run in parallel with ic_review (depends only on `datasheets`):**

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\verify_temperature_ratings.py reviews/<SCH_ID>/REVIEW/review.db
```

Checks every non-DNP component's `temp_rating_min_c` / `temp_rating_max_c` (from `datasheet_fields`) against `board_temp_min_c` / `board_temp_max_c` stored in the `meta` table (defaults: −40 / 85 °C). Writes `major` issues for out-of-range parts.

**`supply_check` track — run in parallel from `build` (no datasheet dependency):**

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\check_price_availability.py reviews/<SCH_ID>/REVIEW/review.db
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\check_obsolescence.py reviews/<SCH_ID>/REVIEW/review.db
```

`check_price_availability.py` is a no-op stub — it previously queried Highstage for lifecycle status (NRND, discontinued, last-time-buy) but that data source is no longer used and is not available from DOKARKIV. `check_obsolescence.py` applies offline heuristics (known bad part-number patterns, through-hole indicators, missing MPN). Both write issues to `review.db`.

**`bom_check` track — run in parallel from `build`:**

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\verify_bom.py reviews/<SCH_ID>/REVIEW/review.db
```

Flags missing MPNs, missing internal part numbers, missing values on passives, missing packages, and unknown component types. Writes `minor` / `info` issues to `review.db`.

**`power_estimate` track — run AFTER enrichment:**

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\estimate_power_consumption.py reviews/<SCH_ID>/REVIEW/review.db
```

Estimates per-component power dissipation for ICs, resistors, LEDs, and LDOs using net voltages from `review.db`. High-dissipation components generate issues.

### Step 5b — Net voltage compatibility check (agent-based)

**Requires Step 4c to have completed** (per-IC `datasheet.json` files with `logic_levels` fields).

Spawn a single `general-purpose` agent with access to `review.db` and all available `datasheets/<ID>/datasheet.json` files. The agent:

1. For every net that connects an output pin of one IC to an input pin of another:
   - Look up the driving IC's output voltage (VOH from `logic_levels` or supply domain)
   - Look up the receiving IC's VIH threshold (from `logic_levels`)
   - Flag if VOH < VIH with no level-shifter on the net
2. Flag 3.3V → 1.8V, 5V → 3.3V, and similar mismatches
3. Also check open-drain nets: if an open-drain output pulls to a voltage higher than the receiver's absolute max input voltage

Write results to `REVIEW/review.db` using `add_issue.py` (never write YAML directly).

### Step 5c — Power sequencing check (agent-based)

**Requires Step 4c to have completed** (enable pins, UVLO thresholds, power-good pins extracted from `datasheet.json`).

Spawn a single `general-purpose` agent with `review.db` and all `datasheet.json` files:

1. Trace enable/power-good chains: which IC's PG output drives another IC's EN input?
2. Identify ICs with UVLO pins (from `datasheet.json power_path` or `switching` sections) — verify a resistor divider is present
3. Flag enable pins that are floating or tied directly to rail without sequencing logic when the IC's datasheet specifies a ramp-up sequence
4. Note any obvious sequencing violations (e.g. load powered before its regulator)

Write results to `REVIEW/review.db` using `add_issue.py` (never write YAML directly).

### Step 5d — Issue deduplication (agent-based)

Independent IC agents often report the same cross-component issue from different perspectives
(e.g. a shared net topology seen by two agents). Before merging, run a single dedup agent.

Spawn one `general-purpose` agent with this prompt:

---

> You are deduplicating issues from a schematic review.
>
> Query `reviews/{schematic_id}/REVIEW/review.db` for all IC-review issues (source = ic_review).
>
> Identify groups of issues that are **semantically equivalent** — same root cause, same
> net or component pair, described from different IC perspectives. Common patterns:
> - The same net cross-coupling flagged by both ICs that share the net
> - The same open-drain pull-up missing, reported by the driving IC and by the receiving IC
> - The same voltage mismatch on a net, reported by both the source and sink IC
>
> For each duplicate group: keep the **most complete** description, add a note listing
> which other refs also reported it (e.g. `also_reported_by: [U22, U23]`), and discard
> the rest.
>
> Do NOT merge issues that share only a component reference but have different root causes.
> Do NOT merge issues of different types even if they reference the same net.
>
> Update `review.db` — delete the duplicate rows and update the kept issue's `also_reported_by`
> field with the merged refs. Use `add_issue.py` for any rewrites; never write SQL directly.
> Preserve all fields of the kept issue; add `also_reported_by` only when merging.

---

### Step 6 — Validate and generate report assets

**Pre-report validation gate** — must pass (exit 0) before generating the report:

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\validate_issues.py reviews/<SCH_ID>/REVIEW/review.db
```

Checks every row in the `issues` table for missing or malformed fields. Exit code 0 = no errors (warnings are acceptable); exit code 1 = one or more errors that must be fixed before reporting; exit code 2 = DB not readable.

Then generate the interactive viewer:

```bash
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\generate_review_html.py reviews/<SCH_ID>/REVIEW
```
Generates a self-contained interactive viewer at `REVIEW/review.html`. Reads directly from `review.db`. Open directly in a browser — no server needed.

Add `--pdf <path>` to explicitly specify the PDF if auto-detection fails.

### Step 7 — Work through the checklist

Go section by section through `references/review_checklist.md`, querying `review.db` directly (or YAML snapshots generated by `dump_yaml.py`).

## Producing the Review Report

> **Prerequisite:** `validate_issues.py` must exit 0 and `REVIEW/review.html` must exist before writing this file.
> If not, run steps 5d → 6 (validate + generate_review_html) first.

After completing the checklist (or the requested sections), write a `REVIEW_REPORT.md` in the schematic folder with:

```markdown
# Schematic Review — <schematic name>
Date: <date>

## Summary
<one-paragraph overview of findings>

## Issues Found
| # | Section | Component / Net | Issue | Severity |
|---|---------|-----------------|-------|----------|
| 1 | I2C     | U5, U12         | Address conflict: both at 0x48 | Critical |
| 2 | Power   | C42             | Voltage rating 6.3V on 5V rail (< 1.5× margin) | Major |
...

## Notes from PDF
<any engineering notes, DNP components, revision history>

## Verified OK
<brief list of what was checked and found correct>
```

Severity levels: **Critical** (will cause malfunction), **Major** (likely problem), **Minor** (best-practice deviation), **Info** (observation only).

## Workflow Guidance

- Be systematic — don't skip checklist sections unless the user explicitly requests a targeted review.
- When you find an issue, note it immediately rather than waiting until the end.
- If a datasheet is unavailable, note the gap in the report rather than skipping the check.
- Cross-reference the PDF notes with the netlist — sometimes a note on the schematic explains an unusual connection.
- For I2C address checks: list every device on each bus with its address. Addresses are often set by A0/A1/A2 pin connections; check those in the connections report and verify against the datasheet.

## Verification Workflow

Run these scripts as `task` subagents. Tracks with no dependency on each other launch in parallel.

### 1. Verify capacitor voltage ratings
```bash
python .github/skills/schematic-reviewer/scripts/verify_capacitor_voltage.py reviews/<SCH_ID>
```
Checks every capacitor's rated voltage against its net voltage with a 1.5× derating rule.
Writes issues to `REVIEW/review.db` via `add_issue.py`. Run after enrichment.

### 2. Verify resistor power dissipation
```bash
python .github/skills/schematic-reviewer/scripts/verify_resistor_power.py reviews/<SCH_ID>
```
Calculates P = V²/R for pull-up, pull-down, and series resistors. Flags dissipation above 50% of package rating.
Writes issues to `REVIEW/review.db` via `add_issue.py`. Run after enrichment.

### 3. Verify temperature ratings (parallel from datasheets)
```bash
python .github/skills/schematic-reviewer/scripts/verify_temperature_ratings.py reviews/<SCH_ID>/REVIEW/review.db
```
Reads `board_temp_min_c` / `board_temp_max_c` from the `meta` table (defaults: −40 / 85 °C) and checks all non-DNP components with temperature specs. Writes `major` issues to `review.db`.

### 4. Supply chain checks (parallel from build)
```bash
python .github/skills/schematic-reviewer/scripts/check_price_availability.py reviews/<SCH_ID>/REVIEW/review.db
python .github/skills/schematic-reviewer/scripts/check_obsolescence.py reviews/<SCH_ID>/REVIEW/review.db
```
Run independently of each other. Both write issues to `review.db`. `check_price_availability.py` is a no-op stub since lifecycle data is no longer available (Highstage removed).

### 5. BOM completeness check (parallel from build)
```bash
python .github/skills/schematic-reviewer/scripts/verify_bom.py reviews/<SCH_ID>/REVIEW/review.db
```
Flags missing MPNs, internal part numbers, values, packages, and unknown component types. Writes `minor` / `info` issues.

### 6. Power consumption estimate (after enrichment)
```bash
python .github/skills/schematic-reviewer/scripts/estimate_power_consumption.py reviews/<SCH_ID>/REVIEW/review.db
```
Estimates per-component power dissipation. Generates issues for high-dissipation components.

### 7. Pre-report validation gate
```bash
python .github/skills/schematic-reviewer/scripts/validate_issues.py reviews/<SCH_ID>/REVIEW/review.db
```
Must exit 0 before generating the report. Checks for missing summaries, invalid severities, and malformed rows.

### 8. Generate review.html
```bash
python .github/skills/schematic-reviewer/scripts/generate_review_html.py reviews/<SCH_ID>/REVIEW
```
Generates a self-contained interactive viewer at `REVIEW/review.html`. Reads directly from `review.db`. Open directly in a browser — no server needed.

Add `--pdf <path>` to explicitly specify the PDF if auto-detection fails.

Severity levels in output: `critical` → `major` → `minor` → `question` → `info`

## Future Work (not yet implemented)

- **Connector pinout cross-board verification**: verify mating connector pinouts agree between two board schematics. Requires both boards to be analyzed. Implement once core IC review is stable.
