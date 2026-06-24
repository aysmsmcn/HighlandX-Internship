from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DB_PATH
from data.models import Base

engine = create_engine(f"sqlite:///{DB_PATH}")
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    Base.metadata.create_all(engine)