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
    _migrate_trade_form_values()
    _migrate_trade_score_columns()
    _drop_trade_source_column()
    _cleanup_empty_trades()


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

    if engine.dialect.name == "postgresql":
        is_form_clause = "transaction_type ILIKE 'FORM %' OR transaction_type ILIKE 'SCHEDULE %'"
    else:
        is_form_clause = (
            "upper(transaction_type) LIKE 'FORM %' OR upper(transaction_type) LIKE 'SCHEDULE %'"
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


def _migrate_trade_form_values() -> None:
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("trades")}
    if "form" not in columns:
        return

    with engine.begin() as conn:
        if "source" in columns:
            conn.execute(
                text(
                    """
                    UPDATE trades
                    SET form = CASE lower(source)
                      WHEN 'insider' THEN 'FORM 4'
                      WHEN 'form3' THEN 'FORM 3'
                      WHEN 'form4' THEN 'FORM 4'
                      WHEN 'schedule13d' THEN 'SCHEDULE 13D'
                      WHEN 'form13f' THEN 'FORM 13F'
                      WHEN 'form8k' THEN 'FORM 8-K'
                      WHEN 'form10k' THEN 'FORM 10-K'
                      WHEN 'congress' THEN 'CONGRESS'
                      ELSE form
                    END
                    WHERE form IS NULL
                      AND source IS NOT NULL
                    """
                )
            )

        conn.execute(text("UPDATE trades SET form = UPPER(form) WHERE form IS NOT NULL"))
        conn.execute(
            text(
                """
                UPDATE trades
                SET form = CASE
                  WHEN form = '3' THEN 'FORM 3'
                  WHEN form = '4' THEN 'FORM 4'
                  WHEN form = '13D' THEN 'SCHEDULE 13D'
                  WHEN form = '13F' THEN 'FORM 13F'
                  WHEN form = '8K' THEN 'FORM 8-K'
                  WHEN form = '10K' THEN 'FORM 10-K'
                  ELSE form
                END
                WHERE form IS NOT NULL
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE trades
                SET form = REPLACE(form, 'FORM 8K', 'FORM 8-K')
                WHERE form LIKE 'FORM 8K%'
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE trades
                SET form = REPLACE(form, 'FORM 10K', 'FORM 10-K')
                WHERE form LIKE 'FORM 10K%'
                """
            )
        )
        conn.execute(
            text(
                """
                UPDATE trades
                SET form = REPLACE(form, 'FORM 13D', 'SCHEDULE 13D')
                WHERE form LIKE 'FORM 13D%'
                """
            )
        )


def _migrate_trade_score_columns() -> None:
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("trades")}
    with engine.begin() as conn:
        if "score" not in columns:
            try:
                conn.execute(text("ALTER TABLE trades ADD COLUMN score INTEGER"))
            except (OperationalError, ProgrammingError) as exc:
                message = str(exc).lower()
                if "duplicate column" not in message and "already exists" not in message:
                    raise
        if "score_model" not in columns:
            try:
                conn.execute(text("ALTER TABLE trades ADD COLUMN score_model VARCHAR(64)"))
            except (OperationalError, ProgrammingError) as exc:
                message = str(exc).lower()
                if "duplicate column" not in message and "already exists" not in message:
                    raise
        if "score_explanation" not in columns:
            try:
                conn.execute(text("ALTER TABLE trades ADD COLUMN score_explanation TEXT"))
            except (OperationalError, ProgrammingError) as exc:
                message = str(exc).lower()
                if "duplicate column" not in message and "already exists" not in message:
                    raise
        if "score_updated_at" not in columns:
            col_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "DATETIME"
            try:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN score_updated_at {col_type}"))
            except (OperationalError, ProgrammingError) as exc:
                message = str(exc).lower()
                if "duplicate column" not in message and "already exists" not in message:
                    raise


def _drop_trade_source_column() -> None:
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("trades")}
    if "source" not in columns:
        return

    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE trades RENAME TO trades_old"))
            index_names = [
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'index'
                          AND tbl_name = 'trades_old'
                        """
                    )
                ).all()
            ]
            for name in index_names:
                if name.startswith("sqlite_autoindex"):
                    continue
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))

            Base.metadata.create_all(bind=conn)
            conn.execute(
                text(
                    """
                    INSERT INTO trades (
                      id,
                      external_id,
                      ticker,
                      company_name,
                      person_name,
                      person_slug,
                      transaction_type,
                      form,
                      transaction_date,
                      filed_at,
                      amount_usd_low,
                      amount_usd_high,
                      shares,
                      price_usd,
                      url,
                      raw,
                      created_at
                    )
                    SELECT
                      id,
                      external_id,
                      ticker,
                      company_name,
                      person_name,
                      person_slug,
                      transaction_type,
                      form,
                      transaction_date,
                      filed_at,
                      amount_usd_low,
                      amount_usd_high,
                      shares,
                      price_usd,
                      url,
                      raw,
                      created_at
                    FROM trades_old
                    """
                )
            )
            conn.execute(text("DROP TABLE trades_old"))
        return

    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_trades_source"))
        conn.execute(text("DROP INDEX IF EXISTS ix_trades_source_date"))
        conn.execute(text("ALTER TABLE trades DROP COLUMN IF EXISTS source"))


def _cleanup_empty_trades() -> None:
    inspector = inspect(engine)
    if "trades" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("trades")}
    required = {
        "ticker",
        "company_name",
        "person_name",
        "person_slug",
        "transaction_type",
        "form",
        "transaction_date",
        "filed_at",
        "amount_usd_low",
        "amount_usd_high",
        "shares",
        "price_usd",
        "url",
    }
    if not required.issubset(columns):
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE trades
                SET
                  ticker = NULLIF(TRIM(ticker), ''),
                  company_name = NULLIF(TRIM(company_name), ''),
                  person_name = NULLIF(TRIM(person_name), ''),
                  person_slug = NULLIF(TRIM(person_slug), ''),
                  transaction_type = NULLIF(TRIM(transaction_type), ''),
                  form = NULLIF(TRIM(form), ''),
                  url = NULLIF(TRIM(url), '')
                WHERE
                  ticker IS NOT NULL
                  OR company_name IS NOT NULL
                  OR person_name IS NOT NULL
                  OR person_slug IS NOT NULL
                  OR transaction_type IS NOT NULL
                  OR form IS NOT NULL
                  OR url IS NOT NULL
                """
            )
        )
        conn.execute(
            text(
                """
                DELETE FROM trades
                WHERE ticker IS NULL
                  AND company_name IS NULL
                  AND person_name IS NULL
                  AND person_slug IS NULL
                  AND transaction_type IS NULL
                  AND form IS NULL
                  AND transaction_date IS NULL
                  AND filed_at IS NULL
                  AND amount_usd_low IS NULL
                  AND amount_usd_high IS NULL
                  AND shares IS NULL
                  AND price_usd IS NULL
                  AND url IS NULL
                """
            )
        )


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
