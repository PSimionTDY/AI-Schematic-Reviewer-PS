---
name: highstage
description: Access the Highstage internal parts database to download component datasheets or extract BOM data. This skill is NOT the default datasheet source for this project (that is the `dokarkiv` skill) — only use this skill when the user EXPLICITLY asks for Highstage (e.g. says "check Highstage", "use Highstage", or references a Highstage-style part ID like `IC1008360` directly). Do not trigger this skill from generic phrases like "get the datasheet" or "look up part specs" — those should use `dokarkiv` instead.
---

# Highstage Skill

> **Not the default datasheet source.** For general datasheet/part-info lookups, use the
> `dokarkiv` skill instead (`\\SE-ARN-FS2\Vol1\DOKARKIV`). Only use this skill when the user
> explicitly asks for Highstage.

Provides access to Teledyne's internal Highstage parts database. Use the bundled scripts to download schematics, probe datasheet access, and extract/enrich BOM data without needing a browser.

## Python Environment

**Always use the repo venv — never system Python or bare `pip`:**

```
venv\Scripts\python.exe .github\skills\highstage\scripts\<script>.py
venv\Scripts\pip.exe install <package>   # only if a package is truly missing
```

All required packages are already installed in the venv. Do not run `python` without the venv path.

## Use of Subagents

**Run downloads as `task` subagents**, especially for batch BOM downloads which can take minutes:

```
task subagent: python .github/skills/highstage/scripts/highstage_downloader.py --bom bom.csv --filter "U,IC"
```

For multiple individual parts, launch parallel `task` subagents — one per part — so downloads happen simultaneously.

---

## Downloading a Schematic from Highstage

Use `schematic_downloader.py` to fetch a schematic folder from Highstage and set up a local workspace.

```powershell
# List available revisions without copying anything
python .github/skills/highstage/scripts/schematic_downloader.py SCH25678-1E --list-only

# Download latest revision to reviews/ (default dest)
python .github/skills/highstage/scripts/schematic_downloader.py SCH25678

# Download a specific revision to a custom folder
python .github/skills/highstage/scripts/schematic_downloader.py SCH25678-1E --dest C:\reviews\
```

The script:
1. Searches Highstage via the document API (Windows auth / SSPI)
2. Resolves the UNC path `\\highstage\files\{folderrelativepath}`
3. Creates the workspace folder structure: `allegro/`, `REVIEW/IC/`, `REVIEW/RES/`, `REVIEW/CAP/`, `REVIEW/history/`
4. Copies all `.dat` files and the schematic PDF

| Option | Description |
|--------|-------------|
| `schematic_id` | Schematic number, e.g. `SCH25678-1E` or `SCH25678` (base = latest rev) |
| `--dest` | Destination parent folder (default: `reviews/`) |
| `--list-only` | List available revisions without downloading |

---

## Probing Datasheet Accessibility

Use `probe_datasheet_access.py` to check which ICs have datasheets reachable via UNC, and record results in the workspace.

```powershell
# Check all ICs in schematic.yaml against \\highstage\files\PURCHASE_SPEC\IC\
python .github/skills/highstage/scripts/probe_datasheet_access.py reviews/SCH25678-1E
```

Output files written to `REVIEW/`:
- `datasheet_access.yaml` — per-part probe results (accessible, pdf list, UNC path)
- Updates `REVIEW/schematic.yaml` with `meta.datasheet_access` = `unc` / `local` / `mixed`

| Option | Description |
|--------|-------------|
| `schematic_folder` | Path to schematic workspace (must contain `REVIEW/schematic.yaml`) |
| `--download` | Also download inaccessible datasheets locally (future: not yet implemented) |

Requires `REVIEW/schematic.yaml` to be populated with `highstage_id` fields — run the BOM extractor first.

---

## Finding and Downloading Schematics (Manual)

Search for a schematic by number using the Highstage document API (requires Windows auth):

```powershell
$sch = "SCH25678-1E"
$cols = "o%3Btitle%3Bfilename%3Bfolderrelativepath%3Bstatus%3Btype%3Bdescription%3Bworkspace"
$resp = Invoke-WebRequest -Uri "https://highstage/ts/ts/search.aspx?t=doc&o=$sch&_format=xml_raw&_columns=$cols" -UseDefaultCredentials -UseBasicParsing
$resp.Content
```

Key response fields:
- `folderrelativepath`: e.g. `RP3\SCH\SCH25678\SCH25678-1E`
- `workspace`: e.g. `RP3`
- `description`: design description

The full UNC path is `\\highstage\files\{folderrelativepath}`. Copy the folder contents (allegro/ subdir + PDF) to the local workspace.

---

The downloader first tries the UNC file share (`\\highstage\files\PURCHASE_SPEC\IC\{part_id}\`) — this is fast and requires no authentication setup. If the share is unreachable, it falls back to HTTPS with Windows SSPI (Kerberos/NTLM) authentication.

## Downloading Datasheets

### Single part by Highstage part ID
```
python skills/highstage/scripts/highstage_downloader.py IC1008360
```

### List available datasheets without downloading
```
python skills/highstage/scripts/highstage_downloader.py IC1008360 --list
```

### Batch download from a BOM CSV
```
python skills/highstage/scripts/highstage_downloader.py --bom bom.csv --filter "U,IC" --limit 20
```

Output is saved to `datasheets/` by default. Use `--output <dir>` to change it.

## Options

| Option     | Description                                              |
|------------|----------------------------------------------------------|
| `part_id`  | Highstage part ID (e.g. `IC1008360`)                    |
| `--list`   | List datasheets without downloading                      |
| `--output` | Output directory (default: `datasheets/`)                |
| `--http`   | Force HTTP download (skip file share attempt)           |
| `--bom`    | BOM CSV file for batch download                          |
| `--filter` | Filter BOM by ref designator prefix, e.g. `"U,IC"`      |
| `--limit`  | Maximum number of parts to process from BOM             |

---

## Fetching BOM from PCB_ASSY (Preferred Method)

Use `fetch_pcb_assy_bom.py` to fetch the authoritative BOM directly from Highstage via the PCB_ASSY linked to the schematic. This maps every ref-des to its Highstage part ID and marks DNP (no-mount) components.

### How Highstage revision letters work

Highstage tracks the revision letter (e.g. the `C` in `SCH26913-1C`) in its database, but the
**file share folder** uses only the major revision (`SCH26913-1`). There is no separate folder per
letter revision. The same logic applies to PCB_ASSY — the file share has `PCB_ASSY1020244-1`, the
current letter revision (e.g. `-1A`) is tracked in the Highstage database only.

### Finding the linked PCB_ASSY

When you have a schematic ID (e.g. `SCH26913-1C`), find which PCB_ASSY(s) reference it:

```
GET https://highstage/ts/ts/search.aspx
    ?t=part&_references=SCH26913-1&_format=xml_raw
    &_columns=o;name;type;description;item;workspace;folderrelativepath;status
```

Filter results to `type=PCB_ASSY`. Prefer entries with `status=3` (approved) or `status=5` (released).

> **Note:** Use the **major revision** as the search key (e.g. `SCH26913-1`, not `SCH26913-1C`).
> The letter suffix is tracked in the DB only; the `_references` lookup uses the folder name.

### Fetching the BOM

```
GET https://highstage/ts/ts/search.aspx
    ?t=part&_parenttype=part&_parent=PCB_ASSY1020244-1A&_format=xml_raw
    &_columns=name;ts_ref.pos;ts_ref.qty;description;type;item
```

Each row contains:
- `name` — Highstage part ID (e.g. `CAP1000196`, `IC1008360`)
- `ts_ref.pos` — space-separated ref-des list (e.g. `C10 C115 C120`)
- `ts_ref.qty` — quantity (float string)

### No-mount (DNP) handling

**`XPCB1007755`** is the Highstage placeholder part for no-mount positions. Any ref-des appearing
in the BOM under `name=XPCB1007755` is a DNP (Do Not Populate) component. Mark these as `dnp=1`
in `review.db`.

### Sanity check before accepting the BOM

Before writing to `review.db`, verify:
1. **Coverage ≥ 85%**: count `(populated refs + DNP refs) / total schematic components`. If < 85%,
   the BOM is likely from the wrong revision — warn and ask the user.
2. **Missing refs**: schematic refs not in BOM (and not mechanical/test-point types). List them.
3. **Extra refs**: BOM positions not present in schematic. List them (may indicate substitutions).

If coverage is < 85% or the BOM is empty, print:
> *"The BOM for PCB_ASSY... does not match the schematic. Please verify the correct assembly is
> linked, or supply a BOM CSV manually."*

### Using the script

```bash
# Auto-detect PCB_ASSY, fetch BOM, write to review.db
python .github/skills/highstage/scripts/fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db

# List linked PCB_ASSYs without writing anything
python .github/skills/highstage/scripts/fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db --list-assys

# Use a specific assembly
python .github/skills/highstage/scripts/fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db --assy PCB_ASSY1020244-1A

# Dry run — show diff without writing
python .github/skills/highstage/scripts/fetch_pcb_assy_bom.py reviews/SCH26913-1C/REVIEW/review.db --dry-run
```

After running, all components with a matching BOM entry will have `highstage_id` populated in
`review.db`, enabling datasheet downloads via `batch_downloader.py`.

---

## Extracting and Enriching BOM Data (Legacy — CSV only)

Use `highstage_bom_extractor.py` to read a BOM CSV, detect DNP components, and cross-reference data into `schematic.yaml`.

```powershell
# Read a BOM CSV and print DNP summary
python .github/skills/highstage/scripts/highstage_bom_extractor.py bom.csv

# Annotate schematic.yaml with highstage_id and dnp flag from BOM
python .github/skills/highstage/scripts/highstage_bom_extractor.py bom.csv --update-schematic reviews/SCH25678-1E

# Export annotated BOM to CSV
python .github/skills/highstage/scripts/highstage_bom_extractor.py bom.csv --out-csv annotated_bom.csv

# Fetch BOM from Highstage directly by part number
python .github/skills/highstage/scripts/highstage_bom_extractor.py --part-id PCB_ASSY1017453-3
```

### DNP Detection

The extractor recognises these BOM column patterns automatically:

| Column name | Logic |
|-------------|-------|
| `DNP`, `Do Not Populate`, `NO_POP`, `NOFIT` | Truthy value (`YES`, `Y`, `TRUE`, `X`, `1`) → DNP |
| `Populate`, `Fitted`, `Fit`, `Populated` | Falsy value (`NO`, `N`, `FALSE`, `0`, empty) → DNP |

When `--update-schematic` is used, each component in `REVIEW/schematic.yaml` receives:
- `highstage_id`: from the matching BOM ID column
- `dnp: true/false`: from the DNP detection above

| Option | Description |
|--------|-------------|
| `bom_csv` | BOM CSV file to process |
| `--update-schematic` | Workspace folder; updates `REVIEW/schematic.yaml` |
| `--out-csv` | Write annotated BOM to this CSV |
| `--out-json` | Write annotated BOM to this JSON |
| `--part-id` | Fetch BOM from Highstage directly (no CSV needed) |

---

## Workflow Guidance

1. **Get the schematic**: `schematic_downloader.py SCH25678-1E` → workspace in `reviews/SCH25678-1E/`
2. **Build schematic.yaml**: run `schematic_builder.py reviews/SCH25678-1E`
3. **Enrich with BOM**: `highstage_bom_extractor.py bom.csv --update-schematic reviews/SCH25678-1E`
4. **Probe datasheet access**: `probe_datasheet_access.py reviews/SCH25678-1E`
5. **Download datasheets**: `highstage_downloader.py --bom bom.csv --filter "U,IC"`

## Dependencies

Already installed in the repo venv. If something is missing: `venv\Scripts\pip.exe install -r requirements.txt`

Requires network access to `\\highstage\` (file share) or `https://highstage/` (HTTPS).

