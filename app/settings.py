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
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./data/dev.db"),
        ingest_secret=os.environ.get("INGEST_SECRET", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        auth_disabled=_env_bool("AUTH_DISABLED", True),
        app_password=os.environ.get("APP_PASSWORD", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
    )
