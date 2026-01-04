from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _database_url() -> str:
    raw = os.environ.get("DATABASE_URL")
    if raw:
        return raw

    password = os.environ.get("POSTGRES_PASSWORD", "").strip()
    if not password:
        return "sqlite:///./data/dev.db"

    from urllib.parse import quote

    user = os.environ.get("POSTGRES_USER", "postgres")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    database_name = os.environ.get("POSTGRES_DB", user)

    user_enc = quote(user, safe="")
    password_enc = quote(password, safe="")
    database_enc = quote(database_name, safe="")

    return f"postgresql+psycopg://{user_enc}:{password_enc}@{host}:{port}/{database_enc}"


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    ingest_secret: str
    public_base_url: str
    auth_disabled: bool
    app_password: str
    session_secret: str
    cookie_secure: bool


@lru_cache
def get_settings() -> Settings:
    return Settings(
        app_name=os.environ.get("APP_NAME", "AltData"),
        database_url=_database_url(),
        ingest_secret=os.environ.get("INGEST_SECRET", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        auth_disabled=_env_bool("AUTH_DISABLED", True),
        app_password=os.environ.get("APP_PASSWORD", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
    )
