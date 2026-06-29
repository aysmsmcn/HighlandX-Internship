from data.database import SessionLocal
from data.models import Setting

def get_setting(key: str, default: str | None = None) -> str | None:
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key=key).one_or_none()
        return row.value if row else default

def set_setting(key: str, value: str) -> None:   # upsert: update if exists, else insert
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key=key).one_or_none()
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


