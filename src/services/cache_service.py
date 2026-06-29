"""Local SQLite cache for slow API results (key → JSON blob + timestamp).

Service layer over the CacheEntry table. Stores opaque JSON strings; callers
decide what to serialize. Lets the app render instantly from the last fetch
instead of waiting on a multi-minute Affinity re-download every launch.
"""

from datetime import datetime, timezone

from data.database import SessionLocal
from data.models import CacheEntry


def write_cache(key: str, value: str) -> str:
    """Upsert a cached value; stamps and returns the UTC write time (ISO)."""
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as s:
        row = s.get(CacheEntry, key)
        if row:
            row.value = value
            row.updated_at = now
        else:
            s.add(CacheEntry(key=key, value=value, updated_at=now))
        s.commit()
    return now


def read_cache(key: str) -> tuple[str | None, str | None]:
    """Return (value, updated_at) for a key, or (None, None) if not cached."""
    with SessionLocal() as s:
        row = s.get(CacheEntry, key)
        if row:
            return row.value, row.updated_at
        return None, None
