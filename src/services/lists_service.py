"""Custom company watchlists, persisted in SQLite.

Service layer over the CompanyList / ListMember tables. The UI (Reminders pane)
talks to THIS, never to the ORM directly.
"""

from data.database import SessionLocal
from data.models import CompanyList, ListMember


def get_lists() -> list[tuple[int, str]]:
    """All custom lists as (id, name), ordered by name."""
    with SessionLocal() as s:
        rows = s.query(CompanyList).order_by(CompanyList.name).all()
        return [(r.id, r.name) for r in rows]


def create_list(name: str) -> int:
    """Create a list (or return the existing one with that name). Returns its id."""
    name = name.strip()
    with SessionLocal() as s:
        row = s.query(CompanyList).filter_by(name=name).one_or_none()
        if row is None:
            row = CompanyList(name=name)
            s.add(row)
            s.commit()
        return row.id


def delete_list(list_id: int) -> None:
    """Delete a list and all of its members."""
    with SessionLocal() as s:
        s.query(ListMember).filter_by(list_id=list_id).delete()
        s.query(CompanyList).filter_by(id=list_id).delete()
        s.commit()


def add_company(list_id: int, company_id: int, company_name: str) -> None:
    """Add a company to a list; no-op if it's already a member."""
    with SessionLocal() as s:
        exists = s.query(ListMember).filter_by(
            list_id=list_id, company_id=company_id).one_or_none()
        if exists is None:
            s.add(ListMember(list_id=list_id, company_id=company_id,
                             company_name=company_name))
            s.commit()


def remove_company(list_id: int, company_id: int) -> None:
    """Remove a company from a list (no-op if it isn't a member)."""
    with SessionLocal() as s:
        s.query(ListMember).filter_by(list_id=list_id, company_id=company_id).delete()
        s.commit()


def get_members(list_id: int) -> list[tuple[int, str]]:
    """Members of a list as (company_id, company_name), ordered by name."""
    with SessionLocal() as s:
        rows = (s.query(ListMember)
                .filter_by(list_id=list_id)
                .order_by(ListMember.company_name)
                .all())
        return [(r.company_id, r.company_name) for r in rows]
