# Schematic Reviewer

An AI-assisted schematic review system for Cadence Allegro / EDIF schematic exports, built as a set of Claude/Copilot skills. Anyone with access to GitHub Copilot CLI (or VS Code Copilot Chat) can use it to review a schematic design.

## How it works (Täby workflow)

Unlike an earlier version of this tool, **schematics are no longer fetched automatically** from
Highstage or any other external system. Instead, you place the exported schematic files
yourself into a review folder, and the reviewer works entirely from what it finds there.

### 1. Create a review folder and drop in your files

Pick a schematic ID (e.g. the SCH/LUTC number on the drawing) and create a folder for it under
`reviews/`:

```
reviews/<SCH_ID>/
├── <SCH_ID>.pdf        ← the schematic PDF (any single PDF in the folder is auto-detected)
└── <SCH_ID>.eds        ← the EDIF netlist export (or an allegro/ subfolder — see below)
```

- **EDIF export**: drop the single `*.eds` file directly into `reviews/<SCH_ID>/` — no
  subfolder needed. This is the standard export format used for the Täby-designed boards.
- **Allegro export** (older projects): instead of a `.eds` file, place the Allegro export files
  under `reviews/<SCH_ID>/allegro/` (either `pinView.dat`/`netView.dat`/... or
  `pstxnet.dat`/`pstchip.dat`/`pstxprt.dat` — both formats are auto-detected).
- **BOM (optional but recommended)**: if you have a BOM export with ref-designator and internal
  part-number columns, put the `.csv` in `reviews/<SCH_ID>/REVIEW/` (create that subfolder
  yourself, or it will be created for you in step 2) — this lets the reviewer look up
  datasheets automatically instead of asking you for part numbers one by one.

The parser auto-detects whichever format is present (Altium, FlatNet, EDIF `.eds`, or Allegro
`View.dat`/`pst*.dat`) — you don't need to tell it which one you used.

### 2. Ask Copilot to review it

Open Copilot CLI (or VS Code Copilot Chat) from the repo root and say:

```
"Review the schematic in reviews/<SCH_ID>"
```

This runs the full pipeline: parses the netlist, looks up datasheets on DOKARKIV, reviews every
IC/transistor/diode tier by tier, runs the automated verification scripts, and produces a report.
You can also ask for a partial/targeted review (e.g. "just check the I2C buses in
reviews/<SCH_ID>") if you don't need the full checklist.

### 3. Find the output

Everything the review produces is written into a `REVIEW/` subfolder next to the files you
provided — it's created automatically the first time you run a review:

```
reviews/<SCH_ID>/
├── <SCH_ID>.pdf
├── <SCH_ID>.eds
└── REVIEW/
    ├── review.db          ← the single source of truth (SQLite): nets, components, issues
    ├── review.html        ← open this in a browser — interactive viewer (PDF + issue list + net graph)
    ├── ic_contexts/        ← per-IC context JSON used during the review (debug/inspection)
    ├── schematic.yaml      ← optional human-readable YAML snapshot (debug only, regenerate with dump_yaml.py)
    └── issues.yaml         ← optional human-readable YAML snapshot of issues (debug only)
```

**`review.html` is the main deliverable** — a self-contained interactive report (no server
needed, just open the file). Issues are listed in decreasing severity (critical → major →
minor → question → info), each linked to its location in the embedded schematic PDF.

`review.db` is the authoritative data store; the YAML files and `REVIEW_REPORT.md` (if
generated) are just human-readable views of what's in the database — never hand-edit them.

### Re-running / updating a review

Because everything lives in `review.db`, you can re-run individual steps at any time without
starting over — e.g. re-generate just the HTML after adding new issues:

```bat
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\generate_review_html.py reviews\<SCH_ID>
```

Or check what pipeline stages have completed so far:

```bat
venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\pipeline_status.py reviews\<SCH_ID>\REVIEW\review.db
```

See `.github/skills/schematic-reviewer/SKILL.md` for the full step-by-step pipeline reference
(what each script does and in what order) if you want to run steps manually instead of just
asking Copilot to "review the schematic".

## Skills

Four skills work together. You can use them individually or let the master skill orchestrate everything.

| Skill | What it does |
|-------|-------------|
| `schematic-reviewer` | Full review workflow — parses the schematic, checks every section of the review checklist, and produces a report |
| `schematic-parser` | Parse Allegro/EDIF/Altium/FlatNet netlist files, search connections, trace signals, read engineering notes from PDF |
| `ic-reviewer` | Per-IC/transistor/diode pin-by-pin review against a datasheet, invoked by `schematic-reviewer` |
| `dokarkiv` | Look up and download component datasheets from the internal DOKARKIV network file share (default datasheet source) |
| `highstage` | Download component datasheets/BOM from the internal Highstage parts database (only when explicitly requested) |

## Setup (first time only)

Clone the repo and create the Python virtual environment:

```bat
git clone <repo-url>
cd schematic_reviewer
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

All scripts must be run with `venv\Scripts\python.exe` — never system Python.

## Quick Start

Once you've placed your schematic files under `reviews/<SCH_ID>/` (see **How it works** above),
open Copilot CLI from the repo root and describe what you want:

```
# Full review of a schematic already placed in reviews/<SCH_ID>
"Review the schematic in reviews/SCH26782-1"

# Targeted checks
"Check all I2C addresses in reviews/SCH26782-1"
"Verify the decoupling on U5 in reviews/SCH26782-1"

# Individual skill usage
"Find everything connected to the SDA net in reviews/SCH26782-1"
"Get the datasheet for T19200"
```

## What Gets Checked

The review checklist covers:

1. **Power & Decoupling** — rails present, decoupling caps on every IC, voltage rating margins
2. **I2C Buses** — unique addresses per bus, pull-up resistors, voltage consistency
3. **SPI Buses** — dedicated chip selects, compatible modes, clock speeds
4. **Component Ratings** — capacitor voltage (≥1.5×), resistor power (≥2×)
5. **IC Pin Verification** — power, ground, enable, unused inputs, open-drain outputs
6. **Passives** — pull-ups, series resistors, ferrites
7. **Engineering Notes** — PDF notes, DNP components, revision history
8. **High-Speed & Clocks** — crystal load caps, differential pairs, clock sources

See the full checklist: [`.github/skills/schematic-reviewer/references/review_checklist.md`](.github/skills/schematic-reviewer/references/review_checklist.md)

## Requirements

Python 3.11+ required. All dependencies installed via the venv setup above.

File share access to `\\SE-ARN-FS2\Vol1\DOKARKIV` (datasheets) requires being on the internal
network (or VPN). No extra setup needed — Windows handles authentication automatically.

## Local Cache

Two folders are used locally and gitignored — nothing here is committed to the repo:

- **`reviews/<SCH_ID>/`** — the schematic files you provide, plus the `REVIEW/` output described above
- **`datasheets/<PART_NUMBER>/`** — downloaded datasheet PDFs and the AI-extracted `datasheet.json` cache, shared across all schematic reviews (so the same part is only looked up once)

Both persist between sessions. On a fresh clone they start empty and are populated as you work.

## Repository Layout

```
.github/skills/
├── dokarkiv/               ← DOKARKIV datasheet lookup (default source)
├── highstage/              ← Highstage datasheet & BOM access (opt-in only)
├── ic-reviewer/            ← per-IC/transistor/diode pin review
├── schematic-parser/       ← netlist parser (Allegro/EDIF/Altium/FlatNet) + connection search
└── schematic-reviewer/     ← master review orchestrator + checklist
reviews/                    ← your schematic files + generated REVIEW/ output (gitignored)
datasheets/                 ← cached datasheets + datasheet.json (gitignored)
requirements.txt
```

