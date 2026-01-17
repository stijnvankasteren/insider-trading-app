from __future__ import annotations

import datetime as dt
import io
import csv
import secrets
import base64
import hashlib
import hmac
import math
import re

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, status
from fastapi import UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import FORM_LABELS, FORM_PREFIX_ORDER, form_prefix, normalize_form
from app.ingest import router as ingest_router
from app.market_data import MarketDataError, fetch_stooq_daily_prices
from app.models import (
    BrokerConnection,
    PersonSummary,
    PortfolioImport,
    PortfolioTransaction,
    Trade,
    User,
    WatchlistItem,
)
from app.portfolio import (
    BROKER_CATALOG,
    CSV_TEMPLATE_HEADERS,
    add_portfolio_import,
    broker_label,
    decode_upload,
    normalize_broker_slug,
    parse_portfolio_csv,
    upsert_broker_connection,
    upsert_portfolio_transactions,
)
from app.sanitization import sql_like_contains
from app.settings import get_settings

router = APIRouter()

router.include_router(ingest_router, prefix="/ingest")

_EMAIL_RE = re.compile(r"^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _validate_email(value: str) -> Optional[str]:
    value = _normalize_email(value)
    if not value or len(value) > 320 or not _EMAIL_RE.match(value):
        return None
    return value


def _hash_password(password: str, salt: bytes) -> str:
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return base64.urlsafe_b64encode(derived).decode("ascii").rstrip("=")


def _new_salt() -> bytes:
    return secrets.token_bytes(16)


def _salt_to_str(salt: bytes) -> str:
    return base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")


def _salt_from_str(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _display_tx_type(form: Optional[str], tx_type: Optional[str]) -> str:
    tx_value = (tx_type or "").strip()
    if form_prefix(form) == "FORM 4":
        code = tx_value.upper()
        if code == "A":
            return "BUY"
        if code == "D":
            return "SELL"
    return tx_value or "—"


def _trade_amount_usd(trade: Trade) -> Optional[float]:
    low = trade.amount_usd_low
    high = trade.amount_usd_high
    if low is not None or high is not None:
        if low is not None and high is not None:
            if low == high:
                return float(low)
            return float((low + high) / 2)
        return float(low if low is not None else high)
    if trade.shares is not None and trade.price_usd is not None:
        try:
            return float(trade.shares) * float(trade.price_usd)
        except (TypeError, ValueError):
            return None
    return None


def _score_trade_heuristic(trade: Trade) -> int:
    score = 50

    prefix = form_prefix(trade.form)
    if prefix == "FORM 4":
        score += 15
    elif prefix == "CONGRESS":
        score += 10
    elif prefix == "FORM 3":
        score += 5

    tx_label = _display_tx_type(trade.form, trade.transaction_type).upper()
    if tx_label == "BUY":
        score += 20
    elif tx_label == "SELL":
        score -= 20

    amount = _trade_amount_usd(trade)
    if amount is not None and amount > 0:
        if amount >= 2_000_000:
            score += 25
        elif amount >= 500_000:
            score += 20
        elif amount >= 100_000:
            score += 15
        elif amount >= 25_000:
            score += 10
        elif amount >= 5_000:
            score += 5
        else:
            score += 2

    trade_date = trade.transaction_date
    if trade_date is None and trade.filed_at is not None:
        trade_date = trade.filed_at.date()
    if trade_date is not None:
        days_ago = (dt.date.today() - trade_date).days
        if days_ago >= 0:
            if days_ago <= 7:
                score += 10
            elif days_ago <= 30:
                score += 6
            elif days_ago <= 90:
                score += 3
            elif days_ago <= 180:
                score += 0
            elif days_ago <= 365:
                score -= 5
            else:
                score -= 10

    return max(0, min(100, int(score)))


def _display_trade_score(trade: Trade) -> Optional[int]:
    if trade.score is not None:
        try:
            return int(trade.score)
        except (TypeError, ValueError):
            return None

    settings = get_settings()
    if not settings.llm_score_enabled:
        return _score_trade_heuristic(trade)
    return None


def _trade_delay_days(trade: Trade) -> Optional[int]:
    if trade.transaction_date and trade.filed_at:
        try:
            return (trade.filed_at.date() - trade.transaction_date).days
        except (TypeError, ValueError):
            return None
    return None


def _serialize_trade(trade: Trade) -> dict[str, Any]:
    amount_usd = _trade_amount_usd(trade)
    price_value = None
    if trade.price_usd is not None:
        try:
            price_value = float(trade.price_usd)
        except (TypeError, ValueError):
            price_value = None
    tx_label = _display_tx_type(trade.form, trade.transaction_type)
    return {
        "id": trade.id,
        "external_id": trade.external_id,
        "ticker": trade.ticker,
        "company_name": trade.company_name,
        "person_name": trade.person_name,
        "person_slug": trade.person_slug,
        "transaction_type": trade.transaction_type,
        "transaction_label": tx_label,
        "form": trade.form,
        "transaction_date": trade.transaction_date.isoformat() if trade.transaction_date else None,
        "filed_at": trade.filed_at.isoformat() if trade.filed_at else None,
        "amount_usd_low": trade.amount_usd_low,
        "amount_usd_high": trade.amount_usd_high,
        "amount_usd": amount_usd,
        "shares": trade.shares,
        "price_usd": str(trade.price_usd) if trade.price_usd is not None else None,
        "price_usd_value": price_value,
        "url": trade.url,
        "score": _display_trade_score(trade),
        "delay_days": _trade_delay_days(trade),
        "is_buy": tx_label.upper() == "BUY",
        "is_sell": tx_label.upper() == "SELL",
    }


def _form_counts_from_rows(rows: list[tuple[Optional[str], int]]) -> dict[str, int]:
    counts = {prefix: 0 for prefix in FORM_PREFIX_ORDER}
    for form_value, count in rows:
        prefix = form_prefix(form_value)
        if prefix:
            counts[prefix] = counts.get(prefix, 0) + int(count)
    return counts


def _forms_payload(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {
            "prefix": prefix,
            "label": FORM_LABELS.get(prefix, prefix),
            "count": int(counts.get(prefix, 0)),
        }
        for prefix in FORM_PREFIX_ORDER
    ]


def _serialize_watchlist_item(item: WatchlistItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "value": item.value,
        "label": item.label,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def _serialize_portfolio_tx(tx: PortfolioTransaction) -> dict[str, Any]:
    return {
        "id": tx.id,
        "external_id": tx.external_id,
        "broker": tx.broker,
        "broker_label": broker_label(tx.broker) if tx.broker else None,
        "account": tx.account,
        "activity_type": tx.activity_type,
        "symbol": tx.symbol,
        "name": tx.name,
        "trade_date": tx.trade_date.isoformat() if tx.trade_date else None,
        "settlement_date": tx.settlement_date.isoformat() if tx.settlement_date else None,
        "quantity": float(tx.quantity) if tx.quantity is not None else None,
        "price": float(tx.price) if tx.price is not None else None,
        "fees": float(tx.fees) if tx.fees is not None else None,
        "amount": float(tx.amount) if tx.amount is not None else None,
        "currency": tx.currency,
        "notes": tx.notes,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
    }


def _serialize_portfolio_import(item: PortfolioImport) -> dict[str, Any]:
    return {
        "id": item.id,
        "source": item.source,
        "broker": item.broker,
        "broker_label": broker_label(item.broker) if item.broker else None,
        "status": item.status,
        "file_name": item.file_name,
        "file_size_bytes": item.file_size_bytes,
        "inserted": item.inserted,
        "updated": item.updated,
        "error_count": item.error_count,
        "message": item.message,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }

@router.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@router.get("/me")
def api_me(request: Request) -> dict[str, Any]:
    settings = get_settings()
    user = None
    if "session" in request.scope:
        user = request.session.get("user")
    return {"auth_disabled": settings.auth_disabled, "user": user}


@router.post("/login")
async def api_login(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    if settings.auth_disabled:
        if "session" in request.scope:
            request.session.clear()
            request.session["user"] = "public"
            request.session["csrf"] = secrets.token_urlsafe(32)
        return {"ok": True, "user": "public", "auth_disabled": True}

    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session middleware not configured",
        )

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    if not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password required")

    normalized_email = _validate_email(email) if email else None
    if normalized_email:
        user = db.scalar(select(User).where(User.email == normalized_email))
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        salt = _salt_from_str(user.password_salt)
        expected = _hash_password(password, salt)
        if not hmac.compare_digest(expected, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        request.session.clear()
        request.session["user"] = normalized_email
        request.session["csrf"] = secrets.token_urlsafe(32)
        return {"ok": True, "user": normalized_email}

    if not settings.app_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin password not configured",
        )
    if password != settings.app_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")

    request.session.clear()
    request.session["user"] = "admin"
    request.session["csrf"] = secrets.token_urlsafe(32)
    return {"ok": True, "user": "admin"}


@router.post("/logout")
def api_logout(request: Request) -> dict[str, bool]:
    if "session" in request.scope:
        request.session.clear()
    return {"ok": True}


@router.post("/signup")
async def api_signup(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    if settings.auth_disabled:
        if "session" in request.scope:
            request.session.clear()
            request.session["user"] = "public"
            request.session["csrf"] = secrets.token_urlsafe(32)
        return {"ok": True, "user": "public", "auth_disabled": True}

    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session middleware not configured",
        )

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    password_confirm = payload.get("password_confirm") or ""

    normalized_email = _validate_email(email)
    if not normalized_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid email address.",
        )
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters.",
        )
    if password != password_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match.",
        )

    existing = db.scalar(select(User).where(User.email == normalized_email))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    salt = _new_salt()
    user = User(
        email=normalized_email,
        password_salt=_salt_to_str(salt),
        password_hash=_hash_password(password, salt),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not create account. Try a different email.",
        ) from None

    request.session.clear()
    request.session["user"] = normalized_email
    request.session["csrf"] = secrets.token_urlsafe(32)
    return {"ok": True, "user": normalized_email}


@router.get("/dashboard")
def api_dashboard(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=24)

    total_24h = db.scalar(select(func.count()).select_from(Trade).where(Trade.created_at >= since))

    top_ticker_row = db.execute(
        select(Trade.ticker, func.count(Trade.id))
        .where(Trade.ticker.is_not(None))
        .group_by(Trade.ticker)
        .order_by(func.count(Trade.id).desc())
        .limit(1)
    ).first()

    latest_trades = db.scalars(
        select(Trade)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(20)
    ).all()

    return {
        "stats": {
            "total_24h": int(total_24h or 0),
            "top_ticker": top_ticker_row[0] if top_ticker_row else None,
            "latest_form": latest_trades[0].form if latest_trades else None,
        },
        "latest_trades": [_serialize_trade(t) for t in latest_trades],
    }


@router.get("/search")
def api_search(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    q: Optional[str] = Query(default=None, max_length=80),
) -> dict[str, Any]:
    query = (q or "").strip()

    ticker_results: list[dict[str, Any]] = []
    people_results: list[dict[str, Any]] = []

    if query:
        like = sql_like_contains(query.lower())
        tickers = db.execute(
            select(
                Trade.ticker,
                func.max(Trade.company_name),
                func.count(Trade.id),
            )
            .where(Trade.ticker.is_not(None))
            .where(
                or_(
                    func.lower(Trade.ticker).like(like, escape="\\"),
                    func.lower(Trade.company_name).like(like, escape="\\"),
                )
            )
            .group_by(Trade.ticker)
            .order_by(func.count(Trade.id).desc())
            .limit(25)
        ).all()

        ticker_results = [
            {"ticker": row[0], "company_name": row[1], "count": int(row[2])}
            for row in tickers
            if row[0]
        ]

        people = db.execute(
            select(
                Trade.person_slug,
                func.max(Trade.person_name),
                func.count(Trade.id),
            )
            .where(Trade.person_slug.is_not(None))
            .where(func.lower(Trade.person_name).like(like, escape="\\"))
            .group_by(Trade.person_slug)
            .order_by(func.count(Trade.id).desc())
            .limit(25)
        ).all()

        people_results = [
            {"slug": row[0], "name": row[1] or row[0], "count": int(row[2])}
            for row in people
            if row[0]
        ]

    user_id = _get_user_id(request)
    watchlist_rows = db.execute(
        select(WatchlistItem.kind, WatchlistItem.value).where(WatchlistItem.user_id == user_id)
    ).all()
    watchlist_tickers = {r[1] for r in watchlist_rows if r[0] == "ticker"}
    watchlist_people = {r[1] for r in watchlist_rows if r[0] == "person"}

    return {
        "query": query,
        "tickers": ticker_results,
        "people": people_results,
        "watchlist_tickers": sorted(watchlist_tickers),
        "watchlist_people": sorted(watchlist_people),
    }


@router.get("/people")
def api_people(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    q: Optional[str] = Query(default=None, max_length=80),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
) -> dict[str, Any]:
    query = (q or "").strip()
    page_size = max(10, min(int(page_size or 50), 200))
    page = max(int(page or 1), 1)

    conditions = [Trade.person_slug.is_not(None)]
    if query:
        like = sql_like_contains(query.lower())
        conditions.append(
            or_(
                func.lower(Trade.person_name).like(like, escape="\\"),
                func.lower(Trade.person_slug).like(like, escape="\\"),
            )
        )

    where_clause = and_(*conditions)

    total = int(
        db.scalar(select(func.count(func.distinct(Trade.person_slug))).where(where_clause))
        or 0
    )
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(page, total_pages)

    offset = (page - 1) * page_size
    people = db.execute(
        select(
            Trade.person_slug,
            func.max(Trade.person_name),
            func.count(Trade.id),
        )
        .where(where_clause)
        .group_by(Trade.person_slug)
        .order_by(func.lower(func.max(Trade.person_name)))
        .limit(page_size)
        .offset(offset)
    ).all()

    user_id = _get_user_id(request)
    watchlist_people = {
        r[0]
        for r in db.execute(
            select(WatchlistItem.value).where(
                WatchlistItem.user_id == user_id, WatchlistItem.kind == "person"
            )
        ).all()
    }

    items = [
        {
            "slug": row[0],
            "name": row[1] or row[0],
            "count": int(row[2]),
            "watchlisted": row[0] in watchlist_people,
        }
        for row in people
        if row[0]
    ]

    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


@router.get("/people/{slug}")
def api_person_detail(
    request: Request,
    slug: str,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    person_slug = slug.strip().lower()
    if not person_slug:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")

    total = int(
        db.scalar(select(func.count(Trade.id)).where(Trade.person_slug == person_slug)) or 0
    )

    name_row = db.execute(
        select(func.max(Trade.person_name)).where(Trade.person_slug == person_slug)
    ).first()
    person_name = name_row[0] if name_row else None

    form_rows = db.execute(
        select(Trade.form, func.count(Trade.id))
        .where(Trade.person_slug == person_slug)
        .group_by(Trade.form)
    ).all()
    form_counts = _form_counts_from_rows([(row[0], int(row[1])) for row in form_rows])

    summary = db.scalar(
        select(PersonSummary).where(PersonSummary.person_slug == person_slug)
    )

    trades = db.scalars(
        select(Trade)
        .where(Trade.person_slug == person_slug)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(20)
    ).all()

    user_id = _get_user_id(request)
    watchlisted = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == "person",
            WatchlistItem.value == person_slug,
        )
    )

    return {
        "slug": person_slug,
        "name": person_name or person_slug,
        "total": total,
        "forms": _forms_payload(form_counts),
        "summary": summary.summary if summary else None,
        "summary_updated_at": summary.summary_updated_at.isoformat()
        if summary and summary.summary_updated_at
        else None,
        "watchlisted": bool(watchlisted),
        "watchlist_item_id": watchlisted.id if watchlisted else None,
        "trades": [_serialize_trade(t) for t in trades],
    }


@router.get("/companies/{ticker}")
def api_company_detail(
    request: Request,
    ticker: str,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw_ticker = ticker.strip().upper()
    if not raw_ticker:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    total = int(db.scalar(select(func.count(Trade.id)).where(Trade.ticker == raw_ticker)) or 0)
    name_row = db.execute(
        select(func.max(Trade.company_name)).where(Trade.ticker == raw_ticker)
    ).first()
    company_name = name_row[0] if name_row else None

    form_rows = db.execute(
        select(Trade.form, func.count(Trade.id))
        .where(Trade.ticker == raw_ticker)
        .group_by(Trade.form)
    ).all()
    form_counts = _form_counts_from_rows([(row[0], int(row[1])) for row in form_rows])

    latest_price = None
    latest_price_date = None
    try:
        _, points = fetch_stooq_daily_prices(raw_ticker)
        if points:
            latest_price = points[-1].close
            latest_price_date = points[-1].date.isoformat()
    except MarketDataError:
        pass

    trades = db.scalars(
        select(Trade)
        .where(Trade.ticker == raw_ticker)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(20)
    ).all()

    user_id = _get_user_id(request)
    watchlisted = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == "ticker",
            WatchlistItem.value == raw_ticker,
        )
    )

    return {
        "ticker": raw_ticker,
        "company_name": company_name,
        "total": total,
        "forms": _forms_payload(form_counts),
        "latest_price": latest_price,
        "latest_price_date": latest_price_date,
        "watchlisted": bool(watchlisted),
        "watchlist_item_id": watchlisted.id if watchlisted else None,
        "trades": [_serialize_trade(t) for t in trades],
    }


@router.get("/watchlist")
def api_watchlist(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user_id = _get_user_id(request)
    items = db.scalars(
        select(WatchlistItem)
        .where(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.created_at.desc())
    ).all()

    tickers = [i for i in items if i.kind == "ticker"]
    people = [i for i in items if i.kind == "person"]

    conditions = []
    if tickers:
        conditions.append(Trade.ticker.in_([i.value for i in tickers]))
    if people:
        conditions.append(Trade.person_slug.in_([i.value for i in people]))

    watchlist_trades = []
    if conditions:
        watchlist_trades = db.scalars(
            select(Trade)
            .where(or_(*conditions))
            .order_by(
                Trade.filed_at.is_(None),
                Trade.filed_at.desc(),
                Trade.created_at.desc(),
            )
            .limit(50)
        ).all()

    return {
        "items": [_serialize_watchlist_item(i) for i in items],
        "trades": [_serialize_trade(t) for t in watchlist_trades],
    }


@router.post("/watchlist")
async def api_watchlist_add(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    kind = (payload.get("kind") or "").strip().lower()
    value = (payload.get("value") or "").strip()
    label = (payload.get("label") or "").strip() or None

    if kind not in {"ticker", "person"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid kind")
    if not value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing value")

    if kind == "ticker":
        value = value.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,15}", value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid ticker format",
            )
    else:
        value = _slugify(value)
        if not value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid person name",
            )
    if not label:
        label = value

    user_id = _get_user_id(request)
    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == kind,
            WatchlistItem.value == value,
        )
    )
    if existing:
        return _serialize_watchlist_item(existing)

    item = WatchlistItem(user_id=user_id, kind=kind, value=value, label=label)
    db.add(item)
    db.commit()
    db.refresh(item)
    return _serialize_watchlist_item(item)


@router.delete("/watchlist/{item_id}")
def api_watchlist_remove(
    request: Request,
    item_id: int,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    user_id = _get_user_id(request)
    item = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.id == item_id, WatchlistItem.user_id == user_id
        )
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


@router.get("/portfolio")
def api_portfolio(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user_id = _get_user_id(request)

    transactions = db.scalars(
        select(PortfolioTransaction)
        .where(PortfolioTransaction.user_id == user_id)
        .order_by(
            PortfolioTransaction.trade_date.is_(None),
            PortfolioTransaction.trade_date.desc(),
            PortfolioTransaction.created_at.desc(),
        )
        .limit(20)
    ).all()

    imports = db.scalars(
        select(PortfolioImport)
        .where(PortfolioImport.user_id == user_id)
        .order_by(PortfolioImport.created_at.desc())
        .limit(8)
    ).all()

    connections = db.scalars(
        select(BrokerConnection)
        .where(BrokerConnection.user_id == user_id)
        .order_by(BrokerConnection.created_at.desc())
    ).all()

    brokers = [
        {"slug": slug, "label": label} for slug, label in sorted(BROKER_CATALOG.items())
    ]

    return {
        "transactions": [_serialize_portfolio_tx(tx) for tx in transactions],
        "imports": [_serialize_portfolio_import(item) for item in imports],
        "connections": [
            {
                "broker": conn.broker,
                "broker_label": broker_label(conn.broker),
                "account": conn.account,
                "status": conn.status,
                "last_synced_at": conn.last_synced_at.isoformat()
                if conn.last_synced_at
                else None,
                "error_message": conn.error_message,
            }
            for conn in connections
        ],
        "brokers": brokers,
    }


@router.post("/portfolio/connect")
async def api_portfolio_connect(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    broker = (payload.get("broker") or "").strip()
    account = (payload.get("account") or "").strip() or None
    slug = normalize_broker_slug(broker)
    if not slug or slug not in BROKER_CATALOG:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown broker")

    user_id = _get_user_id(request)
    connection = upsert_broker_connection(
        db,
        user_id=user_id,
        broker=slug,
        account=account,
        status="pending",
        raw={"source": "manual"},
    )
    db.commit()
    return {
        "broker": connection.broker,
        "label": broker_label(connection.broker),
        "account": connection.account,
        "status": connection.status,
    }


@router.post("/portfolio/disconnect")
async def api_portfolio_disconnect(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    broker = (payload.get("broker") or "").strip()
    account = (payload.get("account") or "").strip() or None
    slug = normalize_broker_slug(broker)
    if not slug:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown broker")

    user_id = _get_user_id(request)
    connection = upsert_broker_connection(
        db,
        user_id=user_id,
        broker=slug,
        account=account,
        status="disconnected",
    )
    db.commit()
    return {
        "broker": connection.broker,
        "label": broker_label(connection.broker),
        "account": connection.account,
        "status": connection.status,
    }


@router.get("/prices")
def api_prices(
    request: Request,
    _: None = Depends(_require_api_login),
    ticker: Optional[str] = Query(default=None, max_length=16),
    range: str = Query(default="1m", max_length=8),
) -> dict[str, Any]:
    raw_ticker = (ticker or "").strip()
    selected_range = (range or "1m").strip().lower()

    ranges: list[tuple[str, str, Optional[dt.timedelta]]] = [
        ("1m", "1M", dt.timedelta(days=31)),
        ("3m", "3M", dt.timedelta(days=93)),
        ("1y", "1Y", dt.timedelta(days=366)),
        ("5y", "5Y", dt.timedelta(days=5 * 366)),
        ("max", "Max", None),
    ]
    valid_ranges = {code for code, _, _ in ranges}
    if selected_range not in valid_ranges:
        selected_range = "1m"

    error: Optional[str] = None
    chart_labels: list[str] = []
    chart_values: list[float] = []
    stats: dict[str, Any] = {}
    resolved_symbol: Optional[str] = None

    if raw_ticker:
        try:
            resolved_symbol, points = fetch_stooq_daily_prices(raw_ticker)

            end_date = points[-1].date
            start_date = points[0].date
            for code, _, delta in ranges:
                if code != selected_range:
                    continue
                if delta is not None:
                    start_date = end_date - delta

            filtered = [p for p in points if p.date >= start_date]
            if not filtered:
                filtered = points[-1:]

            max_points = 900
            if len(filtered) > max_points:
                step = math.ceil(len(filtered) / max_points)
                filtered = filtered[::step]
                if filtered[-1].date != points[-1].date:
                    filtered.append(points[-1])

            chart_labels = [p.date.isoformat() for p in filtered]
            chart_values = [round(p.close, 6) for p in filtered]

            first = filtered[0].close
            last = filtered[-1].close
            change_abs = last - first
            change_pct = (change_abs / first) * 100 if first else 0.0

            stats = {
                "first_date": filtered[0].date.isoformat(),
                "last_date": filtered[-1].date.isoformat(),
                "first_close": round(first, 2),
                "last_close": round(last, 2),
                "change_abs": round(change_abs, 2),
                "change_pct": round(change_pct, 2),
                "change_positive": change_abs >= 0,
            }
        except MarketDataError as exc:
            error = str(exc)

    return {
        "ticker": raw_ticker.upper(),
        "range": selected_range,
        "ranges": [{"code": code, "label": label} for code, label, _ in ranges],
        "resolved_symbol": resolved_symbol,
        "labels": chart_labels,
        "values": chart_values,
        "stats": stats,
        "error": error,
    }


@router.get("/settings")
def api_settings(
    request: Request,
    _: None = Depends(_require_api_login),
) -> dict[str, Any]:
    settings = get_settings()
    db_kind = "sqlite" if settings.database_url.startswith("sqlite") else "postgres"
    return {
        "app_name": settings.app_name,
        "public_base_url": settings.public_base_url,
        "db_kind": db_kind,
        "ingest_configured": bool(settings.ingest_secret),
        "auth_disabled": settings.auth_disabled,
        "web_ui_enabled": settings.web_ui_enabled,
    }


def _require_api_login(request: Request) -> None:
    settings = get_settings()
    if settings.auth_disabled:
        return
    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session middleware not configured",
        )
    if not request.session.get("user"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
        )


def _get_user_id(request: Request) -> str:
    if "session" in request.scope:
        user = request.session.get("user")
        if user:
            return str(user)
    return "public"


@router.get("/trades")
def list_trades(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    form: Optional[str] = Query(default=None, max_length=32),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> dict[str, object]:
    allowed_params = {"form", "ticker", "person", "type", "from", "to", "limit", "offset"}
    unexpected = sorted(set(request.query_params.keys()) - allowed_params)
    if unexpected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unexpected query parameter(s): {', '.join(unexpected)}",
        )

    conditions = []
    if form:
        normalized_form = normalize_form(form)
        prefix = form_prefix(normalized_form)
        if prefix:
            conditions.append(func.lower(Trade.form).like(f"{prefix.lower()}%"))
        elif normalized_form:
            conditions.append(func.lower(Trade.form) == normalized_form.lower())
    if ticker:
        pattern = sql_like_contains(ticker.strip().lower())
        conditions.append(func.lower(Trade.ticker).like(pattern, escape="\\"))
    if person:
        pattern = sql_like_contains(person.strip().lower())
        conditions.append(func.lower(Trade.person_name).like(pattern, escape="\\"))
    if tx_type:
        tx_value = tx_type.strip().lower()
        base = tx_value
        if base.startswith("form"):
            without_prefix = base.removeprefix("form").strip()
            if without_prefix:
                base = without_prefix
        elif base.startswith("schedule"):
            without_prefix = base.removeprefix("schedule").strip()
            if without_prefix:
                base = without_prefix
        candidates = {tx_value, base, f"form {base}".strip(), f"schedule {base}".strip()}
        conditions.append(
            or_(
                func.lower(Trade.transaction_type).in_(candidates),
                func.lower(Trade.form).in_(candidates),
            )
        )
    if from_date:
        conditions.append(Trade.transaction_date >= from_date)
    if to_date:
        conditions.append(Trade.transaction_date <= to_date)

    where_clause = and_(*conditions) if conditions else None

    total_stmt = select(func.count()).select_from(Trade)
    items_stmt = select(Trade)
    if where_clause is not None:
        total_stmt = total_stmt.where(where_clause)
        items_stmt = items_stmt.where(where_clause)

    total = int(db.scalar(total_stmt) or 0)
    trades = db.scalars(
        items_stmt.order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()

    items = [_serialize_trade(t) for t in trades]

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/trades.csv")
def export_trades_csv(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    form: Optional[str] = Query(default=None, max_length=32),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> Response:
    allowed_params = {"form", "ticker", "person", "type", "from", "to", "limit"}
    unexpected = sorted(set(request.query_params.keys()) - allowed_params)
    if unexpected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unexpected query parameter(s): {', '.join(unexpected)}",
        )

    conditions = []
    if form:
        normalized_form = normalize_form(form)
        prefix = form_prefix(normalized_form)
        if prefix:
            conditions.append(func.lower(Trade.form).like(f"{prefix.lower()}%"))
        elif normalized_form:
            conditions.append(func.lower(Trade.form) == normalized_form.lower())
    if ticker:
        pattern = sql_like_contains(ticker.strip().lower())
        conditions.append(func.lower(Trade.ticker).like(pattern, escape="\\"))
    if person:
        pattern = sql_like_contains(person.strip().lower())
        conditions.append(func.lower(Trade.person_name).like(pattern, escape="\\"))
    if tx_type:
        tx_value = tx_type.strip().lower()
        base = tx_value
        if base.startswith("form"):
            without_prefix = base.removeprefix("form").strip()
            if without_prefix:
                base = without_prefix
        elif base.startswith("schedule"):
            without_prefix = base.removeprefix("schedule").strip()
            if without_prefix:
                base = without_prefix
        candidates = {tx_value, base, f"form {base}".strip(), f"schedule {base}".strip()}
        conditions.append(
            or_(
                func.lower(Trade.transaction_type).in_(candidates),
                func.lower(Trade.form).in_(candidates),
            )
        )
    if from_date:
        conditions.append(Trade.transaction_date >= from_date)
    if to_date:
        conditions.append(Trade.transaction_date <= to_date)

    stmt = select(Trade)
    if conditions:
        stmt = stmt.where(and_(*conditions))

    trades = db.scalars(
        stmt.order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        ).limit(limit)
    ).all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "ticker",
            "company_name",
            "person_name",
            "transaction_type",
            "form",
            "transaction_date",
            "filed_at",
            "amount_usd_low",
            "amount_usd_high",
            "shares",
            "price_usd",
            "url",
            "external_id",
        ]
    )
    for t in trades:
        writer.writerow(
            [
                t.ticker or "",
                t.company_name or "",
                t.person_name or "",
                t.transaction_type or "",
                t.form or "",
                t.transaction_date.isoformat() if t.transaction_date else "",
                t.filed_at.isoformat() if t.filed_at else "",
                t.amount_usd_low or "",
                t.amount_usd_high or "",
                t.shares or "",
                str(t.price_usd) if t.price_usd is not None else "",
                t.url or "",
                t.external_id,
            ]
        )

    csv_text = out.getvalue()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="trades.csv"'},
    )


@router.get("/portfolio/template.csv")
def portfolio_template_csv(
    request: Request,
    _: None = Depends(_require_api_login),
) -> Response:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_TEMPLATE_HEADERS)
    writer.writerow(
        [
            "2024-01-15",
            "BUY",
            "AAPL",
            "Apple Inc.",
            "10",
            "187.25",
            "1.50",
            "1872.50",
            "USD",
            "degiro",
            "main",
            "your-id-123",
            "Optional notes",
        ]
    )
    csv_text = out.getvalue()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="portfolio_template.csv"'},
    )


@router.post("/portfolio/import/csv")
def portfolio_import_csv(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    broker: Optional[str] = Form(default=None, max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
    currency: Optional[str] = Form(default=None, max_length=8),
) -> dict[str, Any]:
    settings = get_settings()
    user_id = _get_user_id(request)

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty CSV file")

    max_bytes = int(settings.portfolio_upload_max_mb) * 1_048_576
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {settings.portfolio_upload_max_mb} MB)",
        )

    text = decode_upload(data)
    default_broker = normalize_broker_slug(broker)
    account_value = account.strip() if account else None
    result = parse_portfolio_csv(
        text,
        default_broker=default_broker,
        default_account=account_value,
        default_currency=currency,
        max_items=settings.portfolio_max_items,
    )

    import_batch = secrets.token_urlsafe(8)
    inserted, updated = upsert_portfolio_transactions(
        db,
        user_id=user_id,
        items=result.items,
        import_batch=import_batch,
    )

    error_count = len(result.errors)
    if inserted or updated:
        status_label = "completed" if error_count == 0 else "partial"
    else:
        status_label = "failed" if error_count else "completed"

    broker_name = broker_label(default_broker) if default_broker else None
    summary = f"CSV import: {inserted} inserted, {updated} updated, {error_count} errors."
    if broker_name:
        summary = f"{summary} Broker: {broker_name}."

    add_portfolio_import(
        db,
        user_id=user_id,
        source="csv",
        status=status_label,
        broker=default_broker,
        file_name=file.filename,
        file_size_bytes=len(data),
        inserted=inserted,
        updated=updated,
        error_count=error_count,
        message=summary,
        raw={"errors": result.errors[:50], "skipped_empty": result.skipped_empty},
    )

    db.commit()
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_empty": result.skipped_empty,
        "errors": result.errors[:50],
    }


@router.post("/portfolio/import/ocr")
def portfolio_import_ocr(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    broker: Optional[str] = Form(default=None, max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
) -> dict[str, Any]:
    settings = get_settings()
    user_id = _get_user_id(request)

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty OCR file")

    max_bytes = int(settings.portfolio_upload_max_mb) * 1_048_576
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large (max {settings.portfolio_upload_max_mb} MB)",
        )

    ocr_url = settings.ocr_service_url.rstrip("/")
    if not ocr_url:
        add_portfolio_import(
            db,
            user_id=user_id,
            source="ocr",
            status="failed",
            broker=normalize_broker_slug(broker),
            file_name=file.filename,
            file_size_bytes=len(data),
            error_count=1,
            message="OCR_SERVICE_URL is not configured.",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OCR_SERVICE_URL not configured",
        )

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{ocr_url}/ocr/file",
                files={
                    "file": (
                        file.filename or "document.pdf",
                        data,
                        file.content_type or "application/pdf",
                    )
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        add_portfolio_import(
            db,
            user_id=user_id,
            source="ocr",
            status="failed",
            broker=normalize_broker_slug(broker),
            file_name=file.filename,
            file_size_bytes=len(data),
            error_count=1,
            message=f"OCR request failed: {exc}",
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OCR service error",
        ) from exc

    text = payload.get("text") or ""
    text_excerpt = text[:8000]
    if len(text) > 8000:
        text_excerpt = f"{text_excerpt}..."

    add_portfolio_import(
        db,
        user_id=user_id,
        source="ocr",
        status="completed",
        broker=normalize_broker_slug(broker),
        file_name=file.filename,
        file_size_bytes=len(data),
        message="OCR extracted text. Review and map to transactions.",
        raw={
            "source": payload.get("source"),
            "stats": payload.get("stats"),
            "text_excerpt": text_excerpt,
        },
    )
    db.commit()

    stats = payload.get("stats") or {}
    return {
        "status": "completed",
        "text_chars": len(text),
        "pages": stats.get("pages"),
        "ocr_pages": stats.get("ocr_pages"),
    }


@router.get("/portfolio/brokers")
def portfolio_brokers(
    request: Request,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user_id = _get_user_id(request)
    connections = db.scalars(
        select(BrokerConnection).where(BrokerConnection.user_id == user_id)
    ).all()

    return {
        "catalog": [
            {"slug": slug, "label": label} for slug, label in sorted(BROKER_CATALOG.items())
        ],
        "connections": [
            {
                "broker": c.broker,
                "account": c.account,
                "status": c.status,
                "last_synced_at": c.last_synced_at.isoformat() if c.last_synced_at else None,
                "error_message": c.error_message,
            }
            for c in connections
        ],
    }


@router.post("/portfolio/brokers/{broker}/connect")
def portfolio_broker_connect(
    request: Request,
    broker: str,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    account: Optional[str] = Form(default=None, max_length=128),
) -> dict[str, Any]:
    user_id = _get_user_id(request)
    slug = normalize_broker_slug(broker)
    account_value = account.strip() if account else None
    if not slug or slug not in BROKER_CATALOG:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown broker")

    connection = upsert_broker_connection(
        db,
        user_id=user_id,
        broker=slug,
        account=account_value,
        status="pending",
        raw={"source": "manual"},
    )
    db.commit()

    return {
        "broker": connection.broker,
        "label": broker_label(connection.broker),
        "account": connection.account,
        "status": connection.status,
    }


@router.post("/portfolio/brokers/{broker}/disconnect")
def portfolio_broker_disconnect(
    request: Request,
    broker: str,
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    account: Optional[str] = Form(default=None, max_length=128),
) -> dict[str, Any]:
    user_id = _get_user_id(request)
    slug = normalize_broker_slug(broker)
    account_value = account.strip() if account else None
    if not slug:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown broker")

    connection = upsert_broker_connection(
        db,
        user_id=user_id,
        broker=slug,
        account=account_value,
        status="disconnected",
    )
    db.commit()

    return {
        "broker": connection.broker,
        "label": broker_label(connection.broker),
        "account": connection.account,
        "status": connection.status,
    }
