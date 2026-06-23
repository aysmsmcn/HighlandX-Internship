"""Application-wide constants and configuration."""

from pathlib import Path

APP_NAME = "HighlandX"

# Project root (one level above src/).
ROOT_DIR = Path(__file__).resolve().parent.parent

# Local SQLite database location (used from Phase 1 onward).
DB_PATH = ROOT_DIR / "highlandx.db"
