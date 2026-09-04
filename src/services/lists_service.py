"""Custom company watchlists, persisted in SQLite.

Service layer over the CompanyList / ListMember tables. The UI (Reminders pane)
talks to THIS, never to the ORM directly.
"""

from data.database import SessionLocal
from data.models import CompanyList, ListMember


def get_lists(owner_id: "int | None") -> list[tuple[int, str]]:
    """Lists belonging to owner_id as (id, name), ordered by name. owner_id is an Affinity
    person id; pass None to get the unassigned lists (owner_id IS NULL)."""
    with SessionLocal() as s:
        rows = (s.query(CompanyList)
                .filter_by(owner_id=owner_id)
                .order_by(CompanyList.name).all())
        return [(r.id, r.name) for r in rows]


def get_list(list_id: int) -> "tuple[int, int | None, str] | None":
    """One list as (id, owner_id, name), or None if it doesn't exist."""
    with SessionLocal() as s:
        row = s.get(CompanyList, list_id)
        return (row.id, row.owner_id, row.name) if row else None


def create_list(name: str, owner_id: "int | None" = None) -> int:
    """Create a list for owner_id (or return the existing one with that name for that owner).
    Returns its id. Names are unique per owner, so different owners can reuse a name."""
    name = name.strip()
    with SessionLocal() as s:
        row = s.query(CompanyList).filter_by(name=name, owner_id=owner_id).one_or_none()
        if row is None:
            row = CompanyList(name=name, owner_id=owner_id)
            s.add(row)
            s.commit()
        return row.id


def rename_list(list_id: int, new_name: str) -> bool:
    """Rename a list. Returns False if new_name is blank, the list is gone, or the SAME owner
    already has another list with that name; True on success."""
    new_name = new_name.strip()
    if not new_name:
        return False
    with SessionLocal() as s:
        row = s.get(CompanyList, list_id)
        if row is None:
            return False
        clash = (s.query(CompanyList)
                 .filter(CompanyList.owner_id == row.owner_id,
                         CompanyList.name == new_name,
                         CompanyList.id != list_id).one_or_none())
        if clash is not None:
            return False                       # that owner already has a list with this name
        row.name = new_name
        s.commit()
        return True


def reassign_list(list_id: int, new_owner_id: "int | None", new_name: "str | None" = None) -> bool:
    """Move a list to new_owner_id (optionally renaming it to new_name). Returns False if the
    target owner already has a list with that name (the caller should re-try with a new_name),
    or the list is gone; True on success."""
    with SessionLocal() as s:
        row = s.get(CompanyList, list_id)
        if row is None:
            return False
        target_name = (new_name or row.name).strip()
        if not target_name:
            return False
        clash = (s.query(CompanyList)
                 .filter(CompanyList.owner_id == new_owner_id,
                         CompanyList.name == target_name,
                         CompanyList.id != list_id).one_or_none())
        if clash is not None:
            return False                       # name taken in the target owner
        row.owner_id = new_owner_id
        row.name = target_name
        s.commit()
        return True


def assign_unowned_to(owner_id: int) -> int:
    """Assign every unassigned list (owner_id IS NULL) to owner_id. Returns how many were moved.
    Used once to hand pre-existing lists to the connected user. Skips any whose name would clash
    with a list that owner already has (left unassigned for manual reassignment)."""
    moved = 0
    with SessionLocal() as s:
        taken = {n for (n,) in s.query(CompanyList.name).filter_by(owner_id=owner_id).all()}
        for row in s.query(CompanyList).filter_by(owner_id=None).all():
            if row.name in taken:
                continue                       # would collide — leave it for manual reassignment
            row.owner_id = owner_id
            taken.add(row.name)
            moved += 1
        if moved:
            s.commit()
    return moved


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
