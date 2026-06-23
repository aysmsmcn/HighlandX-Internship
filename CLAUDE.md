# CLAUDE.md — HighlandX

Persistent context for Claude Code. See `ROADMAP.md` for the full plan.

## What this is
Cross-platform desktop app that (1) surfaces Outlook/M365 data and (2) logs into
external websites to extract authenticated data, unified in one PySide6 UI.

## Working agreement
- **The user writes the code.** Claude's role is to give reminders, surface
  next steps/gaps, and review code — **not** to edit or create files unless the
  user explicitly says so for that specific task.

## Architecture rule (do not violate)
UI → services → data → auth. The `ui/` layer NEVER imports `msgraph` or
`playwright` directly; it only calls the `services/` layer.

## Stack
Python 3.14 + PySide6 · msgraph-sdk · azure-identity · playwright · keyring ·
SQLAlchemy (SQLite) · qasync (asyncio↔Qt bridge) · PyInstaller.

## Run
```bash
python src/main.py
```

## Layout
- `src/main.py` — entry point; boots Qt + qasync
- `src/ui/` — PySide6 windows/views/widgets
- `src/services/` — outlook_service, scraper_service
- `src/auth/` — ms_auth (OAuth), secrets (keyring)
- `src/data/` — database, models (SQLAlchemy)
- `src/config.py` — constants
- `tests/`

## Conventions
- Secrets go in `keyring` only — never plaintext or source.
- Commit at the end of each phase.
