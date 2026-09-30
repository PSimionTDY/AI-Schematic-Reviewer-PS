---
name: dokarkiv
description: Access component datasheets and part information from the internal DOKARKIV network file share. This is the DEFAULT datasheet source for this project. Use this skill whenever the user wants to look up a part, download a datasheet PDF, or batch-download datasheets for components in a design. Trigger even when the user doesn't say "DOKARKIV" explicitly — phrases like "get the datasheet for T19200", "download all IC datasheets", or "find the datasheet on the network drive" should all trigger this skill.
---

# DOKARKIV Skill

Provides access to component datasheets stored on the internal `DOKARKIV` network file share. Use the bundled script to find and copy datasheet PDFs without needing a browser or any external API.

> **This is the default datasheet source.**

## Python Environment

**Always use the repo venv — never system Python or bare `pip`:**

```
venv\Scripts\python.exe .github\skills\dokarkiv\scripts\find_datasheet.py <PART_NUMBER>
```

## File Share Location

Datasheets and component information live at:

```
\\SE-ARN-FS2\Vol1\DOKARKIV
```

## Part Number Mapping

Internal part numbers use a letter prefix followed by digits, e.g. `T19200`. On the file
share, the same part is stored in a folder named with **only the numeric portion** — the
leading letter is stripped. For example:

| Internal part number | DOKARKIV folder |
|-----------------------|-------------------------------------------|
| `T19200`              | `\\SE-ARN-FS2\Vol1\DOKARKIV\19200\`        |

## Downloading a Datasheet

```powershell
# Copy datasheet(s) for a part to datasheets/<PART_NUMBER>/
python .github/skills/dokarkiv/scripts/find_datasheet.py T19200

# List available files without copying
python .github/skills/dokarkiv/scripts/find_datasheet.py T19200 --list

# Custom output directory
python .github/skills/dokarkiv/scripts/find_datasheet.py T19200 --output datasheets/
```

## Batch Download from a BOM

```powershell
python .github/skills/dokarkiv/scripts/find_datasheet.py --bom bom.csv --filter "U,IC" --limit 20 --output datasheets/
```

The BOM CSV must have a ref-designator column (`Pos`, `Ref`, or `RefDes`) and a part-number
column (`Part Number`, `Part_Number`, `PN`, or `MPN`).

## Options

| Option     | Description                                                |
|------------|-------------------------------------------------------------|
| `part_id`  | Internal part number (e.g. `T19200`)                        |
| `--list`   | List datasheets without downloading                          |
| `--output` | Output directory (default: `datasheets/`)                    |
| `--bom`    | BOM CSV file for batch download                               |
| `--filter` | Filter BOM by ref designator prefix, e.g. `"U,IC"`           |
| `--limit`  | Maximum number of parts to process from BOM                   |

## Dependencies

No extra Python packages required beyond the standard library. Requires network access to
`\\SE-ARN-FS2\Vol1\DOKARKIV` (Teledyne network or VPN). Windows handles authentication
automatically — no credentials needed.
