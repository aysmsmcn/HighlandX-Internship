"""Application-wide constants and configuration."""

import os
import sys
from pathlib import Path

APP_NAME = "HighlandX"

# Project root (one level above src/).
ROOT_DIR = Path(__file__).resolve().parent.parent

# Where the local SQLite DB (settings + cache) lives. In a packaged PyInstaller
# build __file__ points inside the read-only/ephemeral bundle, so write to a
# per-user data directory instead; in dev it stays in the project root.
if getattr(sys, "frozen", False):
    _base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = Path(_base) / APP_NAME
else:
    DATA_DIR = ROOT_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Local SQLite database location (used from Phase 1 onward).
DB_PATH = DATA_DIR / "highlandx.db"

# On-disk cache of fetched company logos (keyed by domain), so they're only
# fetched from the network once per company across the app's lifetime.
LOGOS_DIR = DATA_DIR / "logos"
LOGOS_DIR.mkdir(parents=True, exist_ok=True)

MS_CLIENT_ID = "9359bdda-d20d-4341-8c73-686f3b9870b8"
MS_TENANT_ID = "d1247142-0090-45d0-97b1-7a7d8e0a6766"
GRAPH_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read"]