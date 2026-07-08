"""Manual overrides for a company's fit score, persisted in SQLite.

Service layer over the FitOverride table. The UI talks to THIS, never to the
ORM directly. A stored override always wins over the computed heuristic.
"""

from datetime import datetime, timezone

from data.database import SessionLocal
from data.models import FitOverride


def get_override(company_id: int) -> int | None:
    with SessionLocal() as s:
        row = s.query(FitOverride).filter_by(company_id=company_id).one_or_none()
        return row.score if row else None


def get_all_overrides() -> dict[int, int]:
    """All overrides as {company_id: score}, for bulk lookup while rendering lists."""
    with SessionLocal() as s:
        return {r.company_id: r.score for r in s.query(FitOverride).all()}


def set_override(company_id: int, score: int) -> None:
    with SessionLocal() as s:
        row = s.query(FitOverride).filter_by(company_id=company_id).one_or_none()
        now = datetime.now(timezone.utc).isoformat()
        if row is None:
            s.add(FitOverride(company_id=company_id, score=score, created_at=now))
        else:
            row.score = score
            row.created_at = now
        s.commit()


def clear_override(company_id: int) -> None:
    with SessionLocal() as s:
        s.query(FitOverride).filter_by(company_id=company_id).delete()
        s.commit()
