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