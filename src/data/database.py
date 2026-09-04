from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from config import DB_PATH
from data.models import Base

engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    _migrate_company_list_owner()
    Base.metadata.create_all(engine)


def _migrate_company_list_owner() -> None:
    """One-time migration: add owner_id to company_list and replace the old global-unique name
    constraint with (owner_id, name). SQLite can't ALTER away a column constraint, so we rebuild
    the table (rename → recreate with the new schema → copy rows → drop old). Existing lists get
    owner_id = NULL (unassigned); the app assigns them to the connected user on first refresh.
    No-op once migrated (or on a fresh DB, where create_all builds the new schema directly)."""
    insp = inspect(engine)
    if "company_list" not in insp.get_table_names():
        return                                        # fresh DB — create_all makes the new schema
    if any(c["name"] == "owner_id" for c in insp.get_columns("company_list")):
        return                                        # already migrated
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE company_list RENAME TO company_list_old"))
        Base.metadata.tables["company_list"].create(conn)      # new schema (owner_id + uq)
        conn.execute(text("INSERT INTO company_list (id, owner_id, name) "
                          "SELECT id, NULL, name FROM company_list_old"))
        conn.execute(text("DROP TABLE company_list_old"))