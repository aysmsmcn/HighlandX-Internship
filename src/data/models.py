from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    value: Mapped[str]


class CacheEntry(Base):
    """A JSON blob cached locally to avoid slow API re-fetches (key → value + when)."""
    __tablename__ = "cache"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]                  # JSON payload
    updated_at: Mapped[str]             # ISO-8601 timestamp of the last write


class CompanyList(Base):
    """A user-created watchlist of companies (surfaced in the Reminders pane)."""
    __tablename__ = "company_list"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)


class ListMember(Base):
    """A company belonging to a CompanyList. The name is stored alongside the id so
    a listed company still displays even when it's filtered out of the current load."""
    __tablename__ = "list_member"
    __table_args__ = (UniqueConstraint("list_id", "company_id", name="uq_list_company"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_id: Mapped[int]                # -> company_list.id
    company_id: Mapped[int]             # Affinity organization id
    company_name: Mapped[str]


class FitOverride(Base):
    """A manually-set fit score that overrides the computed heuristic for a company."""
    __tablename__ = "fit_override"

    company_id: Mapped[int] = mapped_column(primary_key=True)   # Affinity organization id
    score: Mapped[int]
    created_at: Mapped[str]             # ISO-8601 timestamp