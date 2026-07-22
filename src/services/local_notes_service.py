"""Local-only company notes, persisted in SQLite — never sent to Affinity.

Service layer over the LocalNote table. Returns Affinity-shaped Note objects (with
local=True) so the UI can merge them into the notes pane alongside shared notes.
"""

from datetime import datetime, timezone

from data.database import SessionLocal
from data.models import LocalNote
from services.affinity_service import Note


def add_local_note(company_id: int, content: str) -> None:
    """Store a note for a company on this machine only."""
    with SessionLocal() as s:
        s.add(LocalNote(company_id=company_id, content=content,
                        created_at=datetime.now(timezone.utc).isoformat()))
        s.commit()


def delete_local_note(note_id: int) -> None:
    """Delete one locally-stored note by its id (no-op if it doesn't exist)."""
    with SessionLocal() as s:
        s.query(LocalNote).filter_by(id=note_id).delete()
        s.commit()


def get_local_notes(company_id: int) -> list[Note]:
    """This machine's notes for a company, newest first, as local-flagged Notes."""
    with SessionLocal() as s:
        rows = (s.query(LocalNote)
                .filter_by(company_id=company_id)
                .order_by(LocalNote.created_at.desc())
                .all())
        return [Note(id=r.id, content=r.content, created_at=r.created_at,
                     creator_id=None, is_meeting=False, local=True) for r in rows]
