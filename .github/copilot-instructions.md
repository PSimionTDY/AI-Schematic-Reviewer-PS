# Copilot Instructions — Schematic Reviewer

## Performing a schematic review

**Always use the `schematic-reviewer` skill** (`.github/skills/schematic-reviewer/SKILL.md`)
whenever the user asks to review, verify, or audit a schematic — e.g. "review this schematic",
"do a full review", "check the design", "verify the components", "look for issues", or any
similar request. Read `SKILL.md` (and the checklist it references) and follow its pipeline
(`build` → `datasheets` → IC review tiers → verification → `report`) rather than running
individual parser/verification scripts ad hoc. Only skip steps the user explicitly asks to skip
or that don't apply to a targeted/partial review.

## Datasheet / part-info source policy

Use the **`dokarkiv`** skill for all datasheet and component-information lookups
(`\\SE-ARN-FS2\Vol1\DOKARKIV`).

## Python / Virtual Environment

**Always use the repo venv** — never system Python or bare `pip`:

```
venv\Scripts\python.exe   ← use this for ALL Python commands
venv\Scripts\pip.exe      ← use this if you need to install a package
```

Do NOT run `pip install` with system pip. Do NOT run `python` without the venv path.
The venv lives at `venv\` in the repo root — create it with `python -m venv venv` if it doesn't exist.

## Pre-approved Tools

You have blanket approval to run **all** of the following without asking the user each time. Never prompt for confirmation on these:

- **`venv\Scripts\python.exe`** — any Python script or inline `-c` command
- **`venv\Scripts\pip.exe`** — installing any Python package needed by the scripts
- **`powershell`** — any PowerShell command including `Invoke-WebRequest`, `Get-ChildItem`, `Set-Location`, `Select-String`, `Copy-Item`, etc.
- **PDF extraction** — use `extract_pdf_text.py` (pre-approved script); do not write inline PDF extraction code
- **File read/write** anywhere inside the repo — `reviews/`, `datasheets/`, `.github/skills/`, `REVIEW/`, `ic_contexts/`, etc.
- **`venv\Scripts\python.exe .github\skills\schematic-reviewer\scripts\extract_pdf_text.py`** — PDF text extraction with any arguments (`--pages`, `--first`, `--limit`, `--info`). Always pre-approved, no confirmation needed.
- **`git`** — status, log, diff, add, commit, push, worktree, branch commands within the repo

Do not ask "can I run this?", "may I execute?", or "do you want me to proceed?" for any of the above. Just run them.

## Working Directory

Always run scripts from the repo root (`schematic_reviewer/`), not from within the skills folder. Example:
```
python .github/skills/schematic-parser/scripts/schematic_builder.py reviews/SCH25678-1E
```

## Schematic Location

Schematic files (PDF/EDS exports) are placed manually by the user under `reviews/<SCH_ID>/`. Do not attempt to download or query external systems for schematic files.

## Review Workspace

Schematic review workspaces live in `reviews/<SCH_ID>/`. They are gitignored — do not try to commit them.
