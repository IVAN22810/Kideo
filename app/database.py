from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app import models  # noqa: F401 — registers SQLModel tables with metadata

DB_PATH = Path(settings.database_path).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=settings.env == "development",
    connect_args={"check_same_thread": False},
)


def init_database() -> None:
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session