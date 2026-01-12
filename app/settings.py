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


def _env_int(name: str, default: int, *, min_value: int = 0, max_value: int = 1_000_000) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


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


def _env_csv(name: str) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    ingest_secret: str
    ingest_secrets: tuple[str, ...]
    public_base_url: str
    auth_disabled: bool
    app_password: str
    session_secret: str
    cookie_secure: bool
    trust_proxy_headers: bool
    rate_limit_enabled: bool
    rate_limit_window_seconds: int
    rate_limit_default_ip: int
    rate_limit_default_principal: int
    rate_limit_auth_ip: int
    rate_limit_auth_principal: int
    rate_limit_ingest_ip: int
    rate_limit_ingest_principal: int
    rate_limit_health_ip: int
    rate_limit_health_principal: int
    ingest_reject_extra_fields: bool
    ingest_max_items: int
    ingest_max_raw_bytes: int
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_score_enabled: bool
    llm_score_stale_hours: int
    llm_score_max_per_run: int
    llm_score_timeout_seconds: int
    llm_score_sleep_ms: int
    llm_schedule_interval_minutes: int
    llm_person_summary_enabled: bool
    llm_person_summary_stale_hours: int
    llm_person_summary_max_per_run: int
    llm_person_summary_max_trades: int
    llm_person_summary_max_tokens: int
    llm_person_summary_sleep_ms: int
    ocr_service_url: str
    portfolio_max_items: int
    portfolio_upload_max_mb: int


@lru_cache
def get_settings() -> Settings:
    ingest_secrets: list[str] = []
    # Support key rotation: accept multiple secrets while keeping INGEST_SECRET as the primary.
    ingest_secrets.extend(_env_csv("INGEST_SECRETS"))
    primary_ingest_secret = os.environ.get("INGEST_SECRET", "").strip()
    if primary_ingest_secret:
        ingest_secrets.insert(0, primary_ingest_secret)
    previous_ingest_secret = os.environ.get("INGEST_SECRET_PREVIOUS", "").strip()
    if previous_ingest_secret:
        ingest_secrets.append(previous_ingest_secret)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ingest_secrets = [s for s in ingest_secrets if not (s in seen or seen.add(s))]
    ingest_secret = ingest_secrets[0] if ingest_secrets else ""

    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    llm_base_url = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip()
    llm_model = os.environ.get("LLM_MODEL", "xiaomi/mimo-v2-flash:free").strip()
    llm_score_enabled = _env_bool("LLM_SCORE_ENABLED", bool(llm_api_key))
    llm_person_summary_enabled = _env_bool("LLM_PERSON_SUMMARY_ENABLED", bool(llm_api_key))
    ocr_service_url = os.environ.get("OCR_SERVICE_URL", "").strip()

    return Settings(
        app_name=os.environ.get("APP_NAME", "AltData"),
        database_url=_database_url(),
        ingest_secret=ingest_secret,
        ingest_secrets=tuple(ingest_secrets),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        auth_disabled=_env_bool("AUTH_DISABLED", True),
        app_password=os.environ.get("APP_PASSWORD", ""),
        session_secret=os.environ.get("SESSION_SECRET", ""),
        cookie_secure=_env_bool("COOKIE_SECURE", False),
        trust_proxy_headers=_env_bool("TRUST_PROXY_HEADERS", False),
        rate_limit_enabled=_env_bool("RATE_LIMIT_ENABLED", True),
        rate_limit_window_seconds=_env_int("RATE_LIMIT_WINDOW_SECONDS", 60, min_value=1, max_value=3600),
        rate_limit_default_ip=_env_int("RATE_LIMIT_DEFAULT_IP", 120, min_value=1, max_value=100_000),
        rate_limit_default_principal=_env_int(
            "RATE_LIMIT_DEFAULT_PRINCIPAL", 240, min_value=1, max_value=100_000
        ),
        # Stricter limits for login/signup/subscribe to slow credential stuffing / spam.
        rate_limit_auth_ip=_env_int("RATE_LIMIT_AUTH_IP", 20, min_value=1, max_value=100_000),
        rate_limit_auth_principal=_env_int(
            "RATE_LIMIT_AUTH_PRINCIPAL", 40, min_value=1, max_value=100_000
        ),
        # Ingest endpoints are protected by a secret header but can be expensive; keep them bounded.
        rate_limit_ingest_ip=_env_int("RATE_LIMIT_INGEST_IP", 60, min_value=1, max_value=100_000),
        rate_limit_ingest_principal=_env_int(
            "RATE_LIMIT_INGEST_PRINCIPAL", 120, min_value=1, max_value=100_000
        ),
        # Health is often polled by load balancers/monitors.
        rate_limit_health_ip=_env_int("RATE_LIMIT_HEALTH_IP", 300, min_value=1, max_value=100_000),
        rate_limit_health_principal=_env_int(
            "RATE_LIMIT_HEALTH_PRINCIPAL", 600, min_value=1, max_value=100_000
        ),
        ingest_reject_extra_fields=_env_bool("INGEST_REJECT_EXTRA_FIELDS", False),
        ingest_max_items=_env_int("INGEST_MAX_ITEMS", 5000, min_value=1, max_value=50_000),
        ingest_max_raw_bytes=_env_int(
            "INGEST_MAX_RAW_BYTES", 50_000, min_value=1_000, max_value=5_000_000
        ),
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url or "https://openrouter.ai/api/v1",
        llm_model=llm_model or "xiaomi/mimo-v2-flash:free",
        llm_score_enabled=llm_score_enabled,
        llm_score_stale_hours=_env_int("LLM_SCORE_STALE_HOURS", 24, min_value=1, max_value=168),
        llm_score_max_per_run=_env_int("LLM_SCORE_MAX_PER_RUN", 0, min_value=0, max_value=100_000),
        llm_score_timeout_seconds=_env_int(
            "LLM_SCORE_TIMEOUT_SECONDS", 30, min_value=5, max_value=300
        ),
        llm_score_sleep_ms=_env_int("LLM_SCORE_SLEEP_MS", 0, min_value=0, max_value=10_000),
        llm_schedule_interval_minutes=_env_int(
            "LLM_SCHEDULE_INTERVAL_MINUTES", 15, min_value=1, max_value=10_000
        ),
        llm_person_summary_enabled=llm_person_summary_enabled,
        llm_person_summary_stale_hours=_env_int(
            "LLM_PERSON_SUMMARY_STALE_HOURS", 24, min_value=1, max_value=168
        ),
        llm_person_summary_max_per_run=_env_int(
            "LLM_PERSON_SUMMARY_MAX_PER_RUN", 0, min_value=0, max_value=100_000
        ),
        llm_person_summary_max_trades=_env_int(
            "LLM_PERSON_SUMMARY_MAX_TRADES", 30, min_value=5, max_value=500
        ),
        llm_person_summary_max_tokens=_env_int(
            "LLM_PERSON_SUMMARY_MAX_TOKENS", 300, min_value=50, max_value=2000
        ),
        llm_person_summary_sleep_ms=_env_int(
            "LLM_PERSON_SUMMARY_SLEEP_MS", 0, min_value=0, max_value=10_000
        ),
        ocr_service_url=ocr_service_url,
        portfolio_max_items=_env_int("PORTFOLIO_MAX_ITEMS", 5000, min_value=1, max_value=50_000),
        portfolio_upload_max_mb=_env_int("PORTFOLIO_UPLOAD_MAX_MB", 25, min_value=1, max_value=500),
    )
