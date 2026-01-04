from __future__ import annotations

import datetime as dt
import math
import re
import secrets
import base64
import hashlib
import hmac
from urllib.parse import urlencode

from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.market_data import MarketDataError, PricePoint, fetch_stooq_daily_prices
from app.models import Subscriber, Trade, User, WatchlistItem
from app.settings import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_EMAIL_RE = re.compile(r"^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")


def _parse_iso_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _build_url(path: str, params: dict[str, Any]) -> str:
    clean: dict[str, object] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        clean[key] = value
    if not clean:
        return path
    return f"{path}?{urlencode(clean)}"


def _attach_trade_price_changes(trades: list[Trade]) -> None:
    tickers = sorted({t.ticker for t in trades if t.ticker})
    series_by_ticker: dict[str, list[PricePoint]] = {}
    for ticker in tickers:
        try:
            _, points = fetch_stooq_daily_prices(ticker)
        except MarketDataError:
            continue
        series_by_ticker[ticker] = points

    for trade in trades:
        pct_text: Optional[str] = None
        pct_positive = True

        ticker = trade.ticker
        tx_date = trade.transaction_date or (trade.filed_at.date() if trade.filed_at else None)
        if ticker:
            points = series_by_ticker.get(ticker)
            if points:
                latest_close = points[-1].close
                baseline: Optional[float] = None
                if trade.price_usd is not None:
                    try:
                        baseline = float(trade.price_usd)
                    except (TypeError, ValueError):
                        baseline = None

                if baseline is None and tx_date is not None:
                    for p in reversed(points):
                        if p.date <= tx_date:
                            baseline = p.close
                            break

                if baseline and baseline > 0:
                    pct = ((latest_close - baseline) / baseline) * 100
                    pct_text = f"{pct:+.1f}%"
                    pct_positive = pct >= 0

        setattr(trade, "price_change_pct", pct_text)
        setattr(trade, "price_change_positive", pct_positive)


def _base_context(request: Request) -> dict[str, Any]:
    settings = get_settings()

    current_user = None
    csrf_token = None
    if "session" in request.scope:
        current_user = request.session.get("user")
        csrf_token = request.session.get("csrf")
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(32)
            request.session["csrf"] = csrf_token

    return {
        "app_name": settings.app_name,
        "auth_disabled": settings.auth_disabled,
        "current_user": current_user,
        "csrf_token": csrf_token,
    }


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={**_base_context(request), **context},
    )


def _require_login(request: Request) -> str:
    settings = get_settings()
    if settings.auth_disabled:
        return "public"

    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session middleware not configured",
        )

    user = request.session.get("user")
    if user:
        return str(user)

    next_url = request.url.path
    if request.url.query:
        next_url = f"{next_url}?{request.url.query}"

    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        detail="Login required",
        headers={"Location": f"/login?next={next_url}"},
    )


def _require_csrf(request: Request, token: Optional[str]) -> None:
    settings = get_settings()
    if settings.auth_disabled:
        return
    if "session" not in request.scope:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Session middleware not configured",
        )
    expected = request.session.get("csrf")
    if not expected or not token or token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )


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


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return _render(
        request,
        "index.html",
        {"page_title": "Alternative market data"},
    )


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return _render(request, "pricing.html", {"page_title": "Pricing"})


@router.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return _render(request, "about.html", {"page_title": "About"})


@router.get("/legal/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _render(request, "legal/privacy.html", {"page_title": "Privacy"})


@router.get("/legal/terms", response_class=HTMLResponse)
def terms(request: Request):
    return _render(request, "legal/terms.html", {"page_title": "Terms"})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = "/app"):
    settings = get_settings()
    if settings.auth_disabled:
        return RedirectResponse(url=next or "/app", status_code=status.HTTP_303_SEE_OTHER)
    return _render(
        request,
        "login.html",
        {
            "page_title": "Login",
            "next": next,
            "error": None,
            "email": "",
            "signup_url": _build_url("/signup", {"next": next}),
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    csrf: Optional[str] = Form(None),
    email: str = Form(""),
    password: str = Form(...),
    next: str = Form("/app"),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if settings.auth_disabled:
        return RedirectResponse(url=next or "/app", status_code=status.HTTP_303_SEE_OTHER)
    _require_csrf(request, csrf)
    if not settings.session_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SESSION_SECRET not configured",
        )

    normalized_email = _validate_email(email) if email else None
    if normalized_email:
        user = db.scalar(select(User).where(User.email == normalized_email))
        if not user:
            return _render(
                request,
                "login.html",
                {
                    "page_title": "Login",
                    "next": next,
                    "email": normalized_email,
                    "signup_url": _build_url("/signup", {"next": next}),
                    "error": "Invalid email or password.",
                },
            )
        salt = _salt_from_str(user.password_salt)
        expected = _hash_password(password, salt)
        if not hmac.compare_digest(expected, user.password_hash):
            return _render(
                request,
                "login.html",
                {
                    "page_title": "Login",
                    "next": next,
                    "email": normalized_email,
                    "signup_url": _build_url("/signup", {"next": next}),
                    "error": "Invalid email or password.",
                },
            )

        request.session.clear()
        request.session["user"] = normalized_email
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse(url=next or "/app", status_code=status.HTTP_303_SEE_OTHER)

    if not settings.app_password:
        return _render(
            request,
            "login.html",
            {
                "page_title": "Login",
                "next": next,
                "email": "",
                "signup_url": _build_url("/signup", {"next": next}),
                "error": "No account email given, and APP_PASSWORD is not configured for admin login.",
            },
        )

    if password != settings.app_password:
        return _render(
            request,
            "login.html",
            {
                "page_title": "Login",
                "next": next,
                "email": "",
                "signup_url": _build_url("/signup", {"next": next}),
                "error": "Invalid password.",
            },
        )

    request.session.clear()
    request.session["user"] = "admin"
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse(url=next or "/app", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
def logout(request: Request):
    if "session" in request.scope:
        request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/subscribe", response_class=HTMLResponse)
def subscribe(request: Request, ok: int = 0):
    return _render(
        request,
        "subscribe.html",
        {
            "page_title": "Subscribe",
            "ok": bool(ok),
            "error": "",
            "email": "",
        },
    )


@router.post("/subscribe", response_class=HTMLResponse)
def subscribe_submit(
    request: Request,
    email: str = Form(...),
    csrf: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if "session" in request.scope:
        _require_csrf(request, csrf)
    normalized_email = _validate_email(email)
    if not normalized_email:
        return _render(
            request,
            "subscribe.html",
            {
                "page_title": "Subscribe",
                "ok": False,
                "error": "Please enter a valid email address.",
                "email": email,
            },
        )
    try:
        db.add(Subscriber(email=normalized_email))
        db.commit()
    except IntegrityError:
        db.rollback()
    return RedirectResponse(url="/subscribe?ok=1", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/signup", response_class=HTMLResponse)
def signup(request: Request, next: str = "/app"):
    return _render(
        request,
        "signup.html",
        {
            "page_title": "Create account",
            "next": next,
            "error": None,
            "email": "",
            "login_url": _build_url("/login", {"next": next}),
        },
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    subscribe_updates: Optional[str] = Form(None),
    next: str = Form("/app"),
    csrf: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    if "session" in request.scope:
        _require_csrf(request, csrf)

    normalized_email = _validate_email(email)
    if not normalized_email:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next,
                "email": email,
                "login_url": _build_url("/login", {"next": next}),
                "error": "Please enter a valid email address.",
            },
        )
    if len(password) < 8:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next}),
                "error": "Password must be at least 8 characters.",
            },
        )
    if password != password_confirm:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next}),
                "error": "Passwords do not match.",
            },
        )

    existing = db.scalar(select(User).where(User.email == normalized_email))
    if existing:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next}),
                "error": "An account with this email already exists. Try logging in.",
            },
        )

    salt = _new_salt()
    user = User(
        email=normalized_email,
        password_salt=_salt_to_str(salt),
        password_hash=_hash_password(password, salt),
    )
    db.add(user)
    if subscribe_updates:
        db.add(Subscriber(email=normalized_email))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next}),
                "error": "Could not create account. Try a different email.",
            },
        )

    if "session" in request.scope:
        request.session.clear()
        request.session["user"] = normalized_email
        request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse(url=next or "/app", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app", response_class=HTMLResponse)
def app_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=24)

    total_24h = db.scalar(
        select(func.count()).select_from(Trade).where(Trade.created_at >= since)
    )

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

    return _render(
        request,
        "app/dashboard.html",
        {
            "page_title": "Dashboard",
            "total_24h": int(total_24h or 0),
            "top_ticker": top_ticker_row[0] if top_ticker_row else None,
            "latest_source": latest_trades[0].source if latest_trades else None,
            "latest_trades": latest_trades,
        },
    )


@router.get("/app/insiders", response_class=HTMLResponse)
def app_insiders(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = None,
    person: Optional[str] = None,
    tx_type: Optional[str] = Query(default=None, alias="type"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = 1,
    page_size: int = 50,
):
    page_size = max(10, min(int(page_size or 50), 200))
    page = max(int(page or 1), 1)

    date_from = _parse_iso_date(from_date)
    date_to = _parse_iso_date(to_date)

    conditions = [Trade.source == "insider"]
    if ticker:
        conditions.append(func.lower(Trade.ticker).like(f"%{ticker.lower()}%"))
    if person:
        conditions.append(func.lower(Trade.person_name).like(f"%{person.lower()}%"))
    if tx_type:
        tx_value = tx_type.strip().lower()
        candidates = {tx_value}
        if tx_value.startswith("form"):
            without_prefix = tx_value.removeprefix("form").strip()
            if without_prefix:
                candidates.add(without_prefix)
        else:
            candidates.add(f"form {tx_value}".strip())
        conditions.append(
            or_(
                func.lower(Trade.transaction_type).in_(candidates),
                func.lower(Trade.form).in_(candidates),
            )
        )
    if date_from:
        conditions.append(Trade.transaction_date >= date_from)
    if date_to:
        conditions.append(Trade.transaction_date <= date_to)

    where_clause = and_(*conditions)

    total = int(db.scalar(select(func.count()).select_from(Trade).where(where_clause)) or 0)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(page, total_pages)

    offset = (page - 1) * page_size
    trades = db.scalars(
        select(Trade)
        .where(where_clause)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(page_size)
        .offset(offset)
    ).all()

    _attach_trade_price_changes(trades)

    filters = {
        "ticker": ticker or "",
        "person": person or "",
        "type": tx_type or "",
        "from": from_date or "",
        "to": to_date or "",
    }
    base_params = {**filters, "page_size": page_size}
    prev_url = (
        _build_url("/app/insiders", {**base_params, "page": page - 1}) if page > 1 else None
    )
    next_url = (
        _build_url("/app/insiders", {**base_params, "page": page + 1})
        if page < total_pages
        else None
    )
    export_url = _build_url("/api/trades.csv", {**base_params, "source": "insider"})

    start = offset + 1 if total > 0 else 0
    end = min(offset + len(trades), total)

    return _render(
        request,
        "app/insiders.html",
        {
            "page_title": "Insider Trading",
            "page_subtitle": "Transactions reported by company insiders.",
            "trades": trades,
            "filters": filters,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "reset_url": "/app/insiders",
            "export_url": export_url,
            "showing_start": start,
            "showing_end": end,
        },
    )


@router.get("/app/congress", response_class=HTMLResponse)
def app_congress(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = None,
    person: Optional[str] = None,
    tx_type: Optional[str] = Query(default=None, alias="type"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = 1,
    page_size: int = 50,
):
    page_size = max(10, min(int(page_size or 50), 200))
    page = max(int(page or 1), 1)

    date_from = _parse_iso_date(from_date)
    date_to = _parse_iso_date(to_date)

    conditions = [Trade.source == "congress"]
    if ticker:
        conditions.append(func.lower(Trade.ticker).like(f"%{ticker.lower()}%"))
    if person:
        conditions.append(func.lower(Trade.person_name).like(f"%{person.lower()}%"))
    if tx_type:
        tx_value = tx_type.strip().lower()
        candidates = {tx_value}
        if tx_value.startswith("form"):
            without_prefix = tx_value.removeprefix("form").strip()
            if without_prefix:
                candidates.add(without_prefix)
        else:
            candidates.add(f"form {tx_value}".strip())
        conditions.append(
            or_(
                func.lower(Trade.transaction_type).in_(candidates),
                func.lower(Trade.form).in_(candidates),
            )
        )
    if date_from:
        conditions.append(Trade.transaction_date >= date_from)
    if date_to:
        conditions.append(Trade.transaction_date <= date_to)

    where_clause = and_(*conditions)

    total = int(db.scalar(select(func.count()).select_from(Trade).where(where_clause)) or 0)
    total_pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(page, total_pages)

    offset = (page - 1) * page_size
    trades = db.scalars(
        select(Trade)
        .where(where_clause)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(page_size)
        .offset(offset)
    ).all()

    _attach_trade_price_changes(trades)

    filters = {
        "ticker": ticker or "",
        "person": person or "",
        "type": tx_type or "",
        "from": from_date or "",
        "to": to_date or "",
    }
    base_params = {**filters, "page_size": page_size}
    prev_url = (
        _build_url("/app/congress", {**base_params, "page": page - 1}) if page > 1 else None
    )
    next_url = (
        _build_url("/app/congress", {**base_params, "page": page + 1})
        if page < total_pages
        else None
    )
    export_url = _build_url("/api/trades.csv", {**base_params, "source": "congress"})

    start = offset + 1 if total > 0 else 0
    end = min(offset + len(trades), total)

    return _render(
        request,
        "app/congress.html",
        {
            "page_title": "Congress Trading",
            "page_subtitle": "Trades reported by members of congress.",
            "trades": trades,
            "filters": filters,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "reset_url": "/app/congress",
            "export_url": export_url,
            "showing_start": start,
            "showing_end": end,
        },
    )


@router.get("/app/search", response_class=HTMLResponse)
def app_search(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    q: Optional[str] = None,
):
    query = (q or "").strip()

    ticker_results: list[dict[str, Any]] = []
    people_results: list[dict[str, Any]] = []

    if query:
        like = f"%{query.lower()}%"
        tickers = db.execute(
            select(
                Trade.ticker,
                func.max(Trade.company_name),
                func.count(Trade.id),
            )
            .where(Trade.ticker.is_not(None))
            .where(
                or_(
                    func.lower(Trade.ticker).like(like),
                    func.lower(Trade.company_name).like(like),
                )
            )
            .group_by(Trade.ticker)
            .order_by(func.count(Trade.id).desc())
            .limit(25)
        ).all()

        ticker_results = [
            {
                "ticker": row[0],
                "company_name": row[1],
                "count": int(row[2]),
            }
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
            .where(func.lower(Trade.person_name).like(like))
            .group_by(Trade.person_slug)
            .order_by(func.count(Trade.id).desc())
            .limit(25)
        ).all()

        people_results = [
            {
                "slug": row[0],
                "name": row[1] or row[0],
                "count": int(row[2]),
            }
            for row in people
            if row[0]
        ]

    watchlist_rows = db.execute(
        select(WatchlistItem.kind, WatchlistItem.value).where(
            WatchlistItem.user_id == user_id
        )
    ).all()
    watchlist_tickers = {r[1] for r in watchlist_rows if r[0] == "ticker"}
    watchlist_people = {r[1] for r in watchlist_rows if r[0] == "person"}

    return _render(
        request,
        "app/search.html",
        {
            "page_title": "Search",
            "page_subtitle": "Find tickers and people in your database.",
            "query": query,
            "ticker_results": ticker_results,
            "people_results": people_results,
            "watchlist_tickers": watchlist_tickers,
            "watchlist_people": watchlist_people,
        },
    )


@router.get("/app/prices", response_class=HTMLResponse)
def app_prices(
    request: Request,
    _: str = Depends(_require_login),
    ticker: Optional[str] = None,
    range: str = "1m",
):
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
                "first_close": f"{first:,.2f}",
                "last_close": f"{last:,.2f}",
                "change_abs": f"{change_abs:+,.2f}",
                "change_pct": f"{change_pct:+.2f}%",
                "change_positive": change_abs >= 0,
            }
        except MarketDataError as exc:
            error = str(exc)

    range_links = [
        {
            "code": code,
            "label": label,
            "selected": code == selected_range,
            "url": _build_url(
                "/app/prices",
                {"ticker": raw_ticker, "range": code},
            ),
        }
        for code, label, _ in ranges
    ]

    return _render(
        request,
        "app/prices.html",
        {
            "page_title": "Prices",
            "page_subtitle": "Search a stock and view price history.",
            "ticker": raw_ticker.upper(),
            "range": selected_range,
            "range_links": range_links,
            "resolved_symbol": resolved_symbol,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "stats": stats,
            "error": error,
        },
    )


@router.get("/app/settings", response_class=HTMLResponse)
def app_settings(request: Request, _: str = Depends(_require_login)):
    settings = get_settings()
    db_kind = "sqlite" if settings.database_url.startswith("sqlite") else "postgres"
    return _render(
        request,
        "app/settings.html",
        {
            "page_title": "Settings",
            "page_subtitle": "Environment-backed configuration.",
            "public_base_url": settings.public_base_url,
            "db_kind": db_kind,
            "ingest_configured": bool(settings.ingest_secret),
        },
    )


@router.get("/app/watchlist", response_class=HTMLResponse)
def app_watchlist(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
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

    return _render(
        request,
        "app/watchlist.html",
        {
            "page_title": "Watchlist",
            "page_subtitle": "Tickers and people you’re tracking.",
            "tickers": tickers,
            "people": people,
            "watchlist_trades": watchlist_trades,
        },
    )


@router.post("/app/watchlist/add")
def watchlist_add(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    kind: str = Form(...),
    value: str = Form(...),
    label: Optional[str] = Form(default=None),
    next: str = Form("/app/watchlist"),
    csrf_token: Optional[str] = Form(default=None),
):
    _require_csrf(request, csrf_token)

    kind_norm = kind.strip().lower()
    if kind_norm not in {"ticker", "person"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid kind")

    value_norm = value.strip()
    if not value_norm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing value")

    if kind_norm == "ticker":
        value_norm = value_norm.upper()
    else:
        value_norm = _slugify(value_norm)

    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == kind_norm,
            WatchlistItem.value == value_norm,
        )
    )
    if not existing:
        db.add(
            WatchlistItem(
                user_id=user_id,
                kind=kind_norm,
                value=value_norm,
                label=(label.strip() if label else None),
            )
        )
        db.commit()

    return RedirectResponse(url=next or "/app/watchlist", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/watchlist/remove")
def watchlist_remove(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    item_id: int = Form(...),
    next: str = Form("/app/watchlist"),
    csrf_token: Optional[str] = Form(default=None),
):
    _require_csrf(request, csrf_token)

    item = db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
    if not item or item.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(item)
    db.commit()
    return RedirectResponse(url=next or "/app/watchlist", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app/companies/{ticker}", response_class=HTMLResponse)
def app_company(
    request: Request,
    ticker: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    ticker_norm = ticker.strip().upper()
    if not ticker_norm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    company_name = db.scalar(
        select(func.max(Trade.company_name)).where(Trade.ticker == ticker_norm)
    )
    total = int(
        db.scalar(select(func.count()).select_from(Trade).where(Trade.ticker == ticker_norm))
        or 0
    )
    by_source = dict(
        db.execute(
            select(Trade.source, func.count(Trade.id))
            .where(Trade.ticker == ticker_norm)
            .group_by(Trade.source)
        ).all()
    )

    trades = db.scalars(
        select(Trade)
        .where(Trade.ticker == ticker_norm)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(100)
    ).all()

    prices_url = _build_url("/app/prices", {"ticker": ticker_norm})
    latest_price: Optional[str] = None
    latest_price_date: Optional[str] = None
    try:
        _, points = fetch_stooq_daily_prices(ticker_norm)
        latest_point = points[-1]
        latest_price = f"{latest_point.close:,.2f}"
        latest_price_date = latest_point.date.isoformat()
    except MarketDataError:
        pass

    watchlisted = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == "ticker",
            WatchlistItem.value == ticker_norm,
        )
    )

    return _render(
        request,
        "app/company.html",
        {
            "page_title": ticker_norm,
            "page_subtitle": company_name or "Company",
            "ticker": ticker_norm,
            "company_name": company_name,
            "total": total,
            "by_source": {k: int(v) for k, v in by_source.items()},
            "trades": trades,
            "prices_url": prices_url,
            "latest_price": latest_price,
            "latest_price_date": latest_price_date,
            "watchlisted": watchlisted,
        },
    )


@router.get("/app/people/{slug}", response_class=HTMLResponse)
def app_person(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    slug_norm = slug.strip().lower()
    if not slug_norm:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    person_name = db.scalar(
        select(func.max(Trade.person_name)).where(Trade.person_slug == slug_norm)
    )
    total = int(
        db.scalar(
            select(func.count()).select_from(Trade).where(Trade.person_slug == slug_norm)
        )
        or 0
    )
    by_source = dict(
        db.execute(
            select(Trade.source, func.count(Trade.id))
            .where(Trade.person_slug == slug_norm)
            .group_by(Trade.source)
        ).all()
    )

    trades = db.scalars(
        select(Trade)
        .where(Trade.person_slug == slug_norm)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(100)
    ).all()

    watchlisted = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.kind == "person",
            WatchlistItem.value == slug_norm,
        )
    )

    return _render(
        request,
        "app/person.html",
        {
            "page_title": person_name or slug_norm,
            "page_subtitle": "Person",
            "person_name": person_name,
            "slug": slug_norm,
            "total": total,
            "by_source": {k: int(v) for k, v in by_source.items()},
            "trades": trades,
            "watchlisted": watchlisted,
        },
    )
