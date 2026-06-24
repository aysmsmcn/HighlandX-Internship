"""Application-wide constants and configuration."""

from pathlib import Path

APP_NAME = "HighlandX"

# Project root (one level above src/).
ROOT_DIR = Path(__file__).resolve().parent.parent

# Local SQLite database location (used from Phase 1 onward).
DB_PATH = ROOT_DIR / "highlandx.db"

MS_CLIENT_ID = "9359bdda-d20d-4341-8c73-686f3b9870b8"
MS_TENANT_ID = "d1247142-0090-45d0-97b1-7a7d8e0a6766"
GRAPH_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read"]