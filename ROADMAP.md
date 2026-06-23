# Project Roadmap

A cross-platform desktop application that surfaces Outlook data in its UI and
pulls data from external websites that require account-based logins.

---

## Overview

**Goal:** Build a desktop app that unifies two data sources in a single UI:

1. **Outlook / Microsoft 365** — read and display mail, calendar, and contacts.
2. **Account-based websites** — log in to external sites and extract authenticated data.

**Stack decision:** Python + PySide6 (Qt for Python), chosen because the
developer already knows Python and C++, avoiding the need to learn JavaScript.
Both core requirements are well-supported by Python libraries.

---

## Tech Stack

| Layer | Tool | Purpose |
|---|---|---|
| Framework / UI | **PySide6** | Desktop UI (windows, views, widgets) |
| Outlook API | **msgraph-sdk** | Access Outlook mail, calendar, contacts via Microsoft Graph |
| Microsoft auth | **azure-identity** | OAuth login flow for Microsoft accounts |
| Web automation | **playwright** | Log in to websites and extract authenticated data |
| Secure secrets | **keyring** | Store tokens/passwords in the OS native credential manager |
| Local database | **SQLAlchemy** | Persist emails, scraped data, and settings (over SQLite) |
| Async bridge | **qasync** | Merge asyncio into Qt's event loop (see note below) |
| Packaging | **PyInstaller** | Bundle the app into a standalone executable |

Install (PyCharm manages the virtual environment automatically):

```bash
pip install PySide6 msgraph-sdk azure-identity playwright keyring SQLAlchemy qasync PyInstaller
python -m playwright install
```

`python -m playwright install` is required — it downloads the actual browser engines.

---

## Architecture

Four layers, with a strict rule: **the UI never calls Graph or Playwright
directly.** It only talks to the service layer. This keeps the app testable and
prevents the UI from freezing during long-running work.

```
┌─────────────────────────────────────────────┐
│                  UI Layer                     │
│            (PySide6 windows/views)            │
└───────────────────┬───────────────────────────┘
                    │
┌───────────────────▼───────────────────────────┐
│              Service Layer                     │
│   Outlook service │ Web-scraper service        │
│   (msgraph)       │ (Playwright)               │
└───────────────────┬───────────────────────────┘
                    │
┌───────────────────▼───────────────────────────┐
│               Data Layer                       │
│   SQLAlchemy models │ local SQLite DB          │
└───────────────────┬───────────────────────────┘
                    │
┌───────────────────▼───────────────────────────┐
│            Security / Auth Layer               │
│   azure-identity (OAuth) │ keyring (secrets)   │
└────────────────────────────────────────────────┘
```

### The async decision (locked in before coding)

PySide6 runs Qt's own event loop, but both `msgraph-sdk` and Playwright are
async (asyncio-based). The two loops don't naturally coexist.

**Decision:** Use **qasync** to merge asyncio into Qt's event loop, allowing
direct `await` of Graph/Playwright calls. (Alternative was QThread workers —
more robust but more boilerplate; revisit only if qasync proves limiting.)

---

## Directory Structure

```
my_app/
├── venv/                       # managed by PyCharm (not committed)
├── requirements.txt
├── README.md
├── CLAUDE.md                   # persistent context for Claude Code
├── ROADMAP.md                  # this file
├── .gitignore
│
├── src/
│   ├── main.py                 # entry point — boots Qt + qasync
│   │
│   ├── ui/                     # all PySide6 UI code (no Graph/Playwright here)
│   │   ├── main_window.py      # main app window
│   │   ├── views/              # individual screens/panels
│   │   │   ├── outlook_view.py
│   │   │   └── web_data_view.py
│   │   └── widgets/            # reusable custom widgets
│   │
│   ├── services/               # business logic (no UI here)
│   │   ├── outlook_service.py  # wraps msgraph calls
│   │   └── scraper_service.py  # wraps Playwright flows
│   │
│   ├── auth/                   # authentication
│   │   ├── ms_auth.py          # azure-identity OAuth flow
│   │   └── secrets.py          # keyring read/write helpers
│   │
│   ├── data/                   # persistence
│   │   ├── database.py         # SQLAlchemy engine/session setup
│   │   └── models.py           # table definitions
│   │
│   └── config.py               # constants, settings, app config
│
└── tests/                      # unit/integration tests
```

**Guiding rule:** `ui/` never imports from Playwright or msgraph directly — it
only talks to `services/`.

---

## Development Phases

Each phase produces something that actually runs. Build in order; don't wire
everything at once.

### Phase 0 — Foundation
Set up packages, create the folder structure, get a blank PySide6 window
opening, initialize Git.
**Done when:** an empty app launches.

### Phase 1 — Local data scaffold
Set up SQLAlchemy, define initial models, confirm read/write to a local SQLite
file.
**Done when:** persistence works before there's any real data.

### Phase 2 — Microsoft authentication
Register an app in the Azure Portal (free), implement the OAuth flow with
`azure-identity`, store the token securely via `keyring`.
**Done when:** you can log in with a Microsoft account and get a valid token.

### Phase 3 — Outlook data
Use the token to pull mail/calendar via `msgraph-sdk`, display it in a simple
list in the UI.
**Done when:** real Outlook data is visible in the app.

### Phase 4 — Web automation
Build one scraper flow with Playwright (single target site), log in, extract
data, store it.
**Done when:** authenticated web data flows into the DB and UI.

### Phase 5 — Integration & polish
Unify both data sources in the UI, add error handling, loading states, settings.
**Done when:** it feels like one coherent app, not two features.

### Phase 6 — Packaging
Bundle with PyInstaller, test the standalone build on a clean machine.
**Done when:** there's a distributable app.

---

## Open Decisions / Prerequisites

These need resolving as their phase approaches:

- **Azure app registration (Phase 2):** Register at https://portal.azure.com to
  get a client ID. Free. Decide single-user vs. multi-tenant — it affects
  permission configuration.
- **Target websites (Phase 4):** Have a concrete list of the sites the scraper
  will target. Their login mechanism (simple form vs. OAuth vs. 2FA) heavily
  affects complexity.
- **Terms of Service:** Confirm each target site permits automated access. If a
  site offers an official API, prefer it over scraping — more stable and avoids
  ToS issues.

---

## Key Principles

- **Start simple** — get each phase running before adding the next.
- **UI stays dumb** — it calls services, never APIs directly.
- **Security first** — secrets go in `keyring`, never plain text or source.
- **Version control everything** — commit at the end of each phase.
- **Prefer official APIs** over scraping wherever one exists.

---

## Current Status

- [x] Stack chosen (Python + PySide6)
- [x] Architecture and structure defined
- [x] Async approach decided (qasync)
- [x] Phase 0 — Foundation
- [ ] Phase 1 — Local data scaffold
- [ ] Phase 2 — Microsoft authentication
- [ ] Phase 3 — Outlook data
- [ ] Phase 4 — Web automation
- [ ] Phase 5 — Integration & polish
- [ ] Phase 6 — Packaging

_Update the checkboxes as you complete each phase so Claude Code can see where
things stand._
