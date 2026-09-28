# Agents

This document describes the Claude skills available in this schematic review project.

## Datasheet / part-info source policy

**Use the `dokarkiv` skill by default for all datasheet and component-information lookups.**
The internal DOKARKIV network file share (`\\SE-ARN-FS2\Vol1\DOKARKIV`) is the standard source.

**Do not use the `highstage` skill unless the user explicitly asks for Highstage** (e.g. says
"check Highstage", "use Highstage", or references a Highstage part ID like `IC1008360` directly).
If the user just asks to "find a datasheet" or "look up a part", assume DOKARKIV.

## Concurrent writes to schematic.yaml

`schematic.yaml` is the shared workspace file updated by many scripts. When multiple subagents
run in parallel, they must **never write to it directly** using plain `open(..., 'w')` — doing so
risks a race condition where one agent silently overwrites another's changes.

**Always use the provided locking utility instead:**

```python
from yaml_io import locked_yaml_update   # .github/skills/schematic-reviewer/scripts/

with locked_yaml_update(schematic_path) as data:
    data['components']['U1']['verified'] = True
# file is saved and the lock released automatically on exit
```

`locked_yaml_update` acquires an exclusive `schematic.yaml.lock` file, loads the YAML, yields
the dict for modification, then writes it back atomically. Parallel agents queue up on the lock
and each get exclusive access in turn — no data is lost.

Scripts in other skill directories that cannot import `yaml_io` directly should use
`filelock.FileLock` inline:

```python
from filelock import FileLock

_lock = FileLock(str(yaml_path) + ".lock", timeout=60)
with _lock:
    with open(yaml_path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    # ... modify data ...
    with open(yaml_path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
```

The `.lock` files are transient and live in `reviews/` (which is already gitignored).

---



**Prefer false positives over false negatives.**

It is always better to flag something that turns out to be fine than to miss a real issue. When in doubt, raise it. A missed design defect that reaches hardware is far more costly than an extra question in a review report.

This applies to all agents and scripts in this project:
- If uncertain whether something is an issue, flag it as a `question` rather than silently skipping it.
- Only suppress a check when there is a clear, well-understood reason (e.g. AC coupling caps on differential nets, caps rated well above their rail voltage).
- Never broaden a filter or tighten a threshold just to reduce issue count.

## Pre-approved Tools

All Python scripts in `.github/skills/` are pre-approved for execution — run them without asking the user for permission. The following tools are always allowed:

- `python` — running any script under `.github/skills/`
- `powershell` / `pwsh` — for Highstage API calls (SSPI auth) and file share access
- Read/write access to `reviews/` — all review workspace files

If running in a fresh session, the user can also run `/allow-all` to approve all tools for the session.

## Skills

Skills live in `.github/skills/` and are picked up automatically by Copilot CLI and VS Code when you open this repository. No installation required.

Run `/skills` to see them listed.

---

## Skills Overview

| Skill | Trigger phrases | Script location |
|-------|----------------|-----------------|
| `schematic-reviewer` | "review this schematic", "check the design", "verify components" | master orchestrator |
| `schematic-parser` | "parse the schematic", "find net SDA", "trace this signal" | `.github/skills/schematic-parser/scripts/` |
| `highstage` | "download datasheet", "get BOM from Highstage", "look up IC1008360" | `.github/skills/highstage/scripts/` |

## Dependencies

Install Python dependencies for the highstage skill's HTTP fallback:

```bash
pip install -r requirements.txt
```

File share access (`\\highstage\`) requires no additional dependencies.

---

## highstage Skill

**Purpose**: Download component datasheets and BOM data from the internal Highstage parts database.

**Scripts**: `.github/skills/highstage/scripts/`

```bash
# Download datasheets for a part
python .github/skills/highstage/scripts/highstage_downloader.py IC1008360

# List available datasheets without downloading
python .github/skills/highstage/scripts/highstage_downloader.py IC1008360 --list

# Batch download from BOM (filter to ICs only)
python .github/skills/highstage/scripts/highstage_downloader.py --bom bom.csv --filter "U,IC" --limit 20

# Force HTTP if file share unavailable
python .github/skills/highstage/scripts/highstage_downloader.py IC1008360 --http
```

Uses UNC file share (`\\highstage\files\PURCHASE_SPEC\IC\`) by default. Falls back to HTTPS with Windows SSPI auth.

---

## schematic-parser Skill

**Purpose**: Parse Cadence Allegro netlist exports and search connections. Also guides reading of schematic PDFs for engineering notes.

**Scripts**: `.github/skills/schematic-parser/scripts/`

```bash
# Parse netlist → generates CONNECTIONS_REPORT.md
python .github/skills/schematic-parser/scripts/schematic_parser.py SCH26782-1/

# Search connections
python .github/skills/schematic-parser/scripts/connection_finder.py -c U400   # by component
python .github/skills/schematic-parser/scripts/connection_finder.py -n SDA    # by net
python .github/skills/schematic-parser/scripts/connection_finder.py -t U5 1   # trace pin
python .github/skills/schematic-parser/scripts/connection_finder.py -p        # power nets
```

Input files: `<schematic_folder>/allegro/pstxnet.dat` and `pstchip.dat`

---

## schematic-reviewer Skill

**Purpose**: Full structured schematic review. Orchestrates parser, datasheet downloads, and the review checklist, then produces a `REVIEW_REPORT.md`.

The review checklist is at: `.github/skills/schematic-reviewer/references/review_checklist.md`

**Checks performed**:
1. Power & decoupling caps
2. I2C buses — address uniqueness, pull-ups, voltage levels
3. SPI buses — chip selects, mode, clock
4. Component ratings (cap voltage, resistor power)
5. IC pin verification
6. Passive components
7. Engineering notes from schematic PDF
8. High-speed signals & clocks

