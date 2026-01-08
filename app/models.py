from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from typing import Optional

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_salt: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Idempotency key from n8n (recommended), or generated server-side.
    external_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)

    ticker: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(256))

    person_name: Mapped[Optional[str]] = mapped_column(String(256), index=True)
    person_slug: Mapped[Optional[str]] = mapped_column(String(256), index=True)

    # "BUY" | "SELL" | etc (string for flexibility)
    transaction_type: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # SEC form number ("4", "144", etc) or free-form label.
    form: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    transaction_date: Mapped[Optional[dt.date]] = mapped_column(Date, index=True)
    filed_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )

    amount_usd_low: Mapped[Optional[int]] = mapped_column(Integer)
    amount_usd_high: Mapped[Optional[int]] = mapped_column(Integer)
    shares: Mapped[Optional[int]] = mapped_column(Integer)
    price_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4))

    url: Mapped[Optional[str]] = mapped_column(String(1024))

    score: Mapped[Optional[int]] = mapped_column(Integer)
    score_model: Mapped[Optional[str]] = mapped_column(String(64))
    score_explanation: Mapped[Optional[str]] = mapped_column(Text)
    score_updated_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


Index("ix_trades_form_date", Trade.form, Trade.transaction_date)


class CikCompany(Base):
    __tablename__ = "cik_companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    cik: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(256), index=True)

    raw: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value", name="uq_watchlist_user_kind_value"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Single-user MVP: this is "admin" (or "public" when AUTH_DISABLED=true).
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    # "ticker" | "person"
    kind: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[str] = mapped_column(String(256), index=True)
    label: Mapped[Optional[str]] = mapped_column(String(256))

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


Index("ix_watchlist_user_kind", WatchlistItem.user_id, WatchlistItem.kind)
