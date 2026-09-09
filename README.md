# Schematic Reviewer

An AI-assisted schematic review system for Cadence Allegro designs, built as a set of Claude skills. Anyone with access to GitHub Copilot CLI can use it to review any schematic on file.

## Skills

Three skills work together. You can use them individually or let the master skill orchestrate everything.

| Skill | What it does |
|-------|-------------|
| `schematic-reviewer` | Full review workflow — parses the schematic, checks every section of the review checklist, and produces a report |
| `schematic-parser` | Parse Allegro netlist files, search connections, trace signals, read engineering notes from PDF |
| `highstage` | Download component datasheets and BOM data from the internal Highstage parts database |

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

Open Copilot CLI from the repo root and describe what you want:

```
# Full review of a schematic (fetches from Highstage automatically)
"Review schematic SCH26782-1"

# Targeted checks
"Check all I2C addresses in SCH26782-1"
"Verify the decoupling on U5 in SCH26782-1"

# Individual skill usage
"Download the datasheet for IC1008360"
"Find everything connected to the SDA net in SCH26782-1"
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

File share access to `\\highstage\` requires being on the Teledyne network (or VPN). No extra setup needed for the file share — Windows handles authentication automatically.

## Local Cache

Two folders are created locally and gitignored:

- **`reviews/<SCH_ID>/`** — schematic files and REVIEW output per design
- **`datasheets/<HIGHSTAGE_ID>/`** — downloaded PDFs and AI-extracted `datasheet.json` per part

These persist between sessions. On a fresh clone they start empty and are populated on first use.

## Repository Layout

```
.github/skills/
├── highstage/              ← Highstage datasheet & BOM access
├── schematic-parser/       ← Allegro netlist parser + connection search
└── schematic-reviewer/     ← Master review orchestrator + checklist
requirements.txt
```

