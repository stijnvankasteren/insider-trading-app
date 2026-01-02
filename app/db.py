from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.models import Base
from app.settings import get_settings

settings = get_settings()

connect_args: dict[str, object] = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _ensure_sqlite_dir_exists(database_url: str) -> None:
    if not database_url.startswith("sqlite"):
        return

    # sqlite:///./data/dev.db -> ./data/dev.db
    parsed = urlparse(database_url)
    sqlite_path = parsed.path
    if database_url.startswith("sqlite:////"):
        # Absolute path: keep leading slash.
        pass
    else:
        sqlite_path = sqlite_path.lstrip("/")
    if not sqlite_path:
        return

    directory = os.path.dirname(sqlite_path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def init_db() -> None:
    _ensure_sqlite_dir_exists(settings.database_url)
    Base.metadata.create_all(bind=engine)
    _migrate_trade_form_column()


def _migrate_trade_form_column() -> None:
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("trades")}
    if "form" not in columns:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE trades ADD COLUMN form VARCHAR(32)"))
        except (OperationalError, ProgrammingError) as exc:
            message = str(exc).lower()
            if "duplicate column" not in message and "already exists" not in message:
                raise

    is_form_clause = (
        "transaction_type ILIKE 'FORM %'"
        if engine.dialect.name == "postgresql"
        else "upper(transaction_type) LIKE 'FORM %'"
    )

    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                UPDATE trades
                SET form = transaction_type
                WHERE form IS NULL
                  AND transaction_type IS NOT NULL
                  AND {is_form_clause}
                """
            )
        )
        conn.execute(
            text(
                f"""
                UPDATE trades
                SET transaction_type = NULL
                WHERE transaction_type IS NOT NULL
                  AND form IS NOT NULL
                  AND {is_form_clause}
                """
            )
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
