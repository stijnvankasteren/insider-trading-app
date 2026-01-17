from __future__ import annotations

import datetime as dt
import math
import re
import secrets
import base64
import hashlib
import hmac
import httpx
from urllib.parse import urlencode, urlsplit

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, status
from fastapi import UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import FORM_LABELS, FORM_PREFIX_ORDER, form_prefix, normalize_form
from app.market_data import MarketDataError, PricePoint, fetch_stooq_daily_prices
from app.models import (
    BrokerConnection,
    PersonSummary,
    PortfolioImport,
    PortfolioTransaction,
    Subscriber,
    Trade,
    User,
    WatchlistItem,
)
from app.portfolio import (
    BROKER_CATALOG,
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
templates = Jinja2Templates(directory="app/templates")

_EMAIL_RE = re.compile(r"^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")


def _build_url(path: str, params: dict[str, Any]) -> str:
    clean: dict[str, object] = {}
    for key, value in params.items():
        if value is None or value == "":
            continue
        clean[key] = value
    if not clean:
        return path
    return f"{path}?{urlencode(clean)}"


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


def _safe_next_path(value: Optional[str], *, default: str = "/app") -> str:
    """
    Prevent open redirects by only allowing local absolute paths ("/...").

    This is applied to all `next` parameters used in redirects.
    """

    if not value:
        return default

    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return default

    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return default

    path = parts.path or ""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default

    out = path
    if parts.query:
        out = f"{out}?{parts.query}"
    if parts.fragment:
        out = f"{out}#{parts.fragment}"
    return out


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
    is_app_client = request.cookies.get("app_client") == "1"

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
        "app_only_mode": settings.app_only_mode,
        "is_app_client": is_app_client,
        "auth_disabled": settings.auth_disabled,
        "current_user": current_user,
        "csrf_token": csrf_token,
        "form_labels": FORM_LABELS,
        "form_prefix_order": FORM_PREFIX_ORDER,
        "tx_label": _display_tx_type,
        "score_trade": _display_trade_score,
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
def login(request: Request, next: str = Query(default="/app", max_length=2048)):
    settings = get_settings()
    next_url = _safe_next_path(next, default="/app")
    if settings.auth_disabled:
        return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    return _render(
        request,
        "login.html",
        {
            "page_title": "Login",
            "next": next_url,
            "error": None,
            "email": "",
            "signup_url": _build_url("/signup", {"next": next_url}),
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    csrf: Optional[str] = Form(None, max_length=256),
    email: str = Form("", max_length=320),
    password: str = Form(..., max_length=1024),
    next: str = Form("/app", max_length=2048),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    next_url = _safe_next_path(next, default="/app")
    if settings.auth_disabled:
        return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
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
                    "next": next_url,
                    "email": normalized_email,
                    "signup_url": _build_url("/signup", {"next": next_url}),
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
                    "next": next_url,
                    "email": normalized_email,
                    "signup_url": _build_url("/signup", {"next": next_url}),
                    "error": "Invalid email or password.",
                },
            )

        request.session.clear()
        request.session["user"] = normalized_email
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)

    if not settings.app_password:
        return _render(
            request,
            "login.html",
            {
                "page_title": "Login",
                "next": next_url,
                "email": "",
                "signup_url": _build_url("/signup", {"next": next_url}),
                "error": "No account email given, and APP_PASSWORD is not configured for admin login.",
            },
        )

    if password != settings.app_password:
        return _render(
            request,
            "login.html",
            {
                "page_title": "Login",
                "next": next_url,
                "email": "",
                "signup_url": _build_url("/signup", {"next": next_url}),
                "error": "Invalid password.",
            },
        )

    request.session.clear()
    request.session["user"] = "admin"
    request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)


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
    email: str = Form(..., max_length=320),
    csrf: Optional[str] = Form(None, max_length=256),
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
def signup(request: Request, next: str = Query(default="/app", max_length=2048)):
    next_url = _safe_next_path(next, default="/app")
    return _render(
        request,
        "signup.html",
        {
            "page_title": "Create account",
            "next": next_url,
            "error": None,
            "email": "",
            "login_url": _build_url("/login", {"next": next_url}),
        },
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    email: str = Form(..., max_length=320),
    password: str = Form(..., max_length=1024),
    password_confirm: str = Form(..., max_length=1024),
    subscribe_updates: Optional[str] = Form(None, max_length=16),
    next: str = Form("/app", max_length=2048),
    csrf: Optional[str] = Form(None, max_length=256),
    db: Session = Depends(get_db),
):
    if "session" in request.scope:
        _require_csrf(request, csrf)
    next_url = _safe_next_path(next, default="/app")

    normalized_email = _validate_email(email)
    if not normalized_email:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next_url,
                "email": email,
                "login_url": _build_url("/login", {"next": next_url}),
                "error": "Please enter a valid email address.",
            },
        )
    if len(password) < 8:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next_url,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next_url}),
                "error": "Password must be at least 8 characters.",
            },
        )
    if password != password_confirm:
        return _render(
            request,
            "signup.html",
            {
                "page_title": "Create account",
                "next": next_url,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next_url}),
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
                "next": next_url,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next_url}),
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
                "next": next_url,
                "email": normalized_email,
                "login_url": _build_url("/login", {"next": next_url}),
                "error": "Could not create account. Try a different email.",
            },
        )

    if "session" in request.scope:
        request.session.clear()
        request.session["user"] = normalized_email
        request.session["csrf"] = secrets.token_urlsafe(32)
    return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app/launch")
def app_launch(request: Request) -> RedirectResponse:
    settings = get_settings()
    response = RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        "app_client",
        "1",
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


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
            "latest_form": latest_trades[0].form if latest_trades else None,
            "latest_trades": latest_trades,
        },
    )


def _render_form_trades(
    request: Request,
    db: Session,
    *,
    form_prefix_value: str,
    page_title: str,
    page_subtitle: str,
    template_name: str,
    base_path: str,
    ticker: Optional[str],
    person: Optional[str],
    tx_type: Optional[str],
    from_date: Optional[dt.date],
    to_date: Optional[dt.date],
    page: int,
    page_size: int,
) -> HTMLResponse:
    page_size = max(10, min(int(page_size or 50), 200))
    page = max(int(page or 1), 1)

    conditions = [func.lower(Trade.form).like(f"{form_prefix_value.lower()}%")]
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
        "from": from_date.isoformat() if from_date else "",
        "to": to_date.isoformat() if to_date else "",
    }
    base_params = {**filters, "page_size": page_size}
    prev_url = (
        _build_url(base_path, {**base_params, "page": page - 1}) if page > 1 else None
    )
    next_url = (
        _build_url(base_path, {**base_params, "page": page + 1}) if page < total_pages else None
    )
    export_url = _build_url("/api/trades.csv", {**filters, "form": form_prefix_value})

    start = offset + 1 if total > 0 else 0
    end = min(offset + len(trades), total)

    return _render(
        request,
        template_name,
        {
            "page_title": page_title,
            "page_subtitle": page_subtitle,
            "trades": trades,
            "filters": filters,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "reset_url": base_path,
            "export_url": export_url,
            "showing_start": start,
            "showing_end": end,
        },
    )


@router.get("/app/3", response_class=HTMLResponse)
def app_form3(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="FORM 3",
        page_title="Form 3",
        page_subtitle="Initial beneficial ownership statements (SEC Form 3).",
        template_name="app/form3.html",
        base_path="/app/3",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/insiders", response_class=HTMLResponse)
def app_insiders(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="FORM 4",
        page_title="Form 4",
        page_subtitle="Insider trading transactions (SEC Form 4).",
        template_name="app/insiders.html",
        base_path="/app/insiders",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/congress", response_class=HTMLResponse)
def app_congress(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="CONGRESS",
        page_title="Congress",
        page_subtitle="Trades reported by members of congress.",
        template_name="app/congress.html",
        base_path="/app/congress",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/13d", response_class=HTMLResponse)
def app_schedule13d(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="SCHEDULE 13D",
        page_title="Schedule 13D",
        page_subtitle="Whale moves (SEC Schedule 13D filings; typically >5% ownership).",
        template_name="app/schedule13d.html",
        base_path="/app/13d",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/13f", response_class=HTMLResponse)
def app_form13f(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="FORM 13F",
        page_title="Form 13F",
        page_subtitle="Institutional holdings (portfolio updates from SEC Form 13F).",
        template_name="app/form13f.html",
        base_path="/app/13f",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/8k", response_class=HTMLResponse)
def app_form8k(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="FORM 8-K",
        page_title="Form 8-K",
        page_subtitle="Stock splits and other events surfaced from SEC Form 8-K filings.",
        template_name="app/form8k.html",
        base_path="/app/8k",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/10k", response_class=HTMLResponse)
def app_form10k(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(
        default=None,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
    person: Optional[str] = Query(default=None, max_length=256),
    tx_type: Optional[str] = Query(default=None, alias="type", max_length=32),
    from_date: Optional[dt.date] = Query(default=None, alias="from"),
    to_date: Optional[dt.date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
    return _render_form_trades(
        request,
        db,
        form_prefix_value="FORM 10-K",
        page_title="Form 10-K",
        page_subtitle="Risk factor changes highlighted from SEC Form 10-K filings.",
        template_name="app/form10k.html",
        base_path="/app/10k",
        ticker=ticker,
        person=person,
        tx_type=tx_type,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )


@router.get("/app/search", response_class=HTMLResponse)
def app_search(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    q: Optional[str] = Query(default=None, max_length=80),
):
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
            .where(func.lower(Trade.person_name).like(like, escape="\\"))
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


@router.get("/app/people", response_class=HTMLResponse)
def app_people(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    q: Optional[str] = Query(default=None, max_length=80),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=10, le=200),
):
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

    people_results = [
        {"slug": row[0], "name": row[1] or row[0], "count": int(row[2])}
        for row in people
        if row[0]
    ]

    watchlist_people = {
        r[0]
        for r in db.execute(
            select(WatchlistItem.value).where(
                WatchlistItem.user_id == user_id, WatchlistItem.kind == "person"
            )
        ).all()
    }

    base_params = {"q": query, "page_size": page_size}
    prev_url = (
        _build_url("/app/people", {**base_params, "page": page - 1}) if page > 1 else None
    )
    next_url = (
        _build_url("/app/people", {**base_params, "page": page + 1})
        if page < total_pages
        else None
    )

    start = offset + 1 if total > 0 else 0
    end = min(offset + len(people_results), total)

    return _render(
        request,
        "app/people.html",
        {
            "page_title": "People",
            "page_subtitle": "Browse people in your database.",
            "query": query,
            "people_results": people_results,
            "watchlist_people": watchlist_people,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "prev_url": prev_url,
            "next_url": next_url,
            "reset_url": "/app/people",
            "showing_start": start,
            "showing_end": end,
        },
    )


@router.get("/app/prices", response_class=HTMLResponse)
def app_prices(
    request: Request,
    _: str = Depends(_require_login),
    ticker: Optional[str] = Query(default=None, max_length=16),
    range: str = Query(default="1m", max_length=8),
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


def _portfolio_notice(request: Request) -> Optional[dict[str, str]]:
    notice = request.query_params.get("notice")
    if not notice:
        return None

    if notice == "csv":
        inserted = request.query_params.get("inserted") or "0"
        updated = request.query_params.get("updated") or "0"
        errors = request.query_params.get("errors") or "0"
        status_label = request.query_params.get("status") or "completed"
        kind = "success" if status_label == "completed" else "warning"
        return {
            "kind": kind,
            "message": f"CSV import: {inserted} inserted, {updated} updated, {errors} errors.",
        }
    if notice == "ocr":
        status_label = request.query_params.get("status") or "completed"
        kind = "success" if status_label == "completed" else "error"
        message = "OCR import completed."
        if status_label != "completed":
            message = "OCR import failed. Check OCR service configuration."
        return {"kind": kind, "message": message}
    if notice == "broker":
        broker = request.query_params.get("broker") or "broker"
        status_label = request.query_params.get("status") or "pending"
        kind = "success" if status_label == "pending" else "warning"
        return {
            "kind": kind,
            "message": f"{broker} connection status: {status_label}.",
        }
    return None


def _portfolio_context(
    request: Request,
    db: Session,
    user_id: str,
) -> dict[str, Any]:
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
        "page_title": "Portfolio tracker",
        "page_subtitle": "Import all transactions via OCR, CSV, or broker APIs.",
        "portfolio_notice": _portfolio_notice(request),
        "portfolio_transactions": transactions,
        "portfolio_imports": imports,
        "broker_connections": connections,
        "broker_catalog": brokers,
        "broker_label": broker_label,
    }


@router.get("/app/portfolio", response_class=HTMLResponse)
def app_portfolio(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    return _render(request, "app/portfolio.html", _portfolio_context(request, db, user_id))


@router.post("/app/portfolio/import/csv", response_class=HTMLResponse)
def app_portfolio_import_csv(
    request: Request,
    csv_file: UploadFile = File(...),
    broker: Optional[str] = Form(default=None, max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
    currency: Optional[str] = Form(default=None, max_length=8),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    _require_csrf(request, csrf_token)
    settings = get_settings()

    data = csv_file.file.read()
    if not data:
        return RedirectResponse(url="/app/portfolio?notice=csv&status=failed", status_code=303)

    max_bytes = int(settings.portfolio_upload_max_mb) * 1_048_576
    if len(data) > max_bytes:
        return RedirectResponse(url="/app/portfolio?notice=csv&status=failed", status_code=303)

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
        file_name=csv_file.filename,
        file_size_bytes=len(data),
        inserted=inserted,
        updated=updated,
        error_count=error_count,
        message=summary,
        raw={"errors": result.errors[:50], "skipped_empty": result.skipped_empty},
    )
    db.commit()

    return RedirectResponse(
        url=_build_url(
            "/app/portfolio",
            {
                "notice": "csv",
                "inserted": inserted,
                "updated": updated,
                "errors": error_count,
                "status": status_label,
            },
        ),
        status_code=303,
    )


@router.post("/app/portfolio/import/ocr", response_class=HTMLResponse)
def app_portfolio_import_ocr(
    request: Request,
    ocr_file: UploadFile = File(...),
    broker: Optional[str] = Form(default=None, max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    _require_csrf(request, csrf_token)
    settings = get_settings()

    data = ocr_file.file.read()
    if not data:
        return RedirectResponse(url="/app/portfolio?notice=ocr&status=failed", status_code=303)

    max_bytes = int(settings.portfolio_upload_max_mb) * 1_048_576
    if len(data) > max_bytes:
        return RedirectResponse(url="/app/portfolio?notice=ocr&status=failed", status_code=303)

    ocr_url = settings.ocr_service_url.rstrip("/")
    broker_slug = normalize_broker_slug(broker)
    if not ocr_url:
        add_portfolio_import(
            db,
            user_id=user_id,
            source="ocr",
            status="failed",
            broker=broker_slug,
            file_name=ocr_file.filename,
            file_size_bytes=len(data),
            error_count=1,
            message="OCR_SERVICE_URL is not configured.",
        )
        db.commit()
        return RedirectResponse(url="/app/portfolio?notice=ocr&status=failed", status_code=303)

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{ocr_url}/ocr/file",
                files={
                    "file": (
                        ocr_file.filename or "document.pdf",
                        data,
                        ocr_file.content_type or "application/pdf",
                    )
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        add_portfolio_import(
            db,
            user_id=user_id,
            source="ocr",
            status="failed",
            broker=broker_slug,
            file_name=ocr_file.filename,
            file_size_bytes=len(data),
            error_count=1,
            message="OCR request failed.",
        )
        db.commit()
        return RedirectResponse(url="/app/portfolio?notice=ocr&status=failed", status_code=303)

    text = payload.get("text") or ""
    text_excerpt = text[:8000]
    if len(text) > 8000:
        text_excerpt = f"{text_excerpt}..."

    add_portfolio_import(
        db,
        user_id=user_id,
        source="ocr",
        status="completed",
        broker=broker_slug,
        file_name=ocr_file.filename,
        file_size_bytes=len(data),
        message="OCR extracted text. Review and map to transactions.",
        raw={
            "source": payload.get("source"),
            "stats": payload.get("stats"),
            "text_excerpt": text_excerpt,
        },
    )
    db.commit()

    return RedirectResponse(url="/app/portfolio?notice=ocr&status=completed", status_code=303)


@router.post("/app/portfolio/connect", response_class=HTMLResponse)
def app_portfolio_connect(
    request: Request,
    broker: str = Form(..., max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    _require_csrf(request, csrf_token)
    broker_slug = normalize_broker_slug(broker)
    account_value = account.strip() if account else None
    if not broker_slug or broker_slug not in BROKER_CATALOG:
        return RedirectResponse(url="/app/portfolio?notice=broker&status=failed", status_code=303)

    upsert_broker_connection(
        db,
        user_id=user_id,
        broker=broker_slug,
        account=account_value,
        status="pending",
        raw={"source": "manual"},
    )
    db.commit()
    return RedirectResponse(
        url=_build_url(
            "/app/portfolio",
            {"notice": "broker", "status": "pending", "broker": broker_label(broker_slug)},
        ),
        status_code=303,
    )


@router.post("/app/portfolio/disconnect", response_class=HTMLResponse)
def app_portfolio_disconnect(
    request: Request,
    broker: str = Form(..., max_length=64),
    account: Optional[str] = Form(default=None, max_length=128),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
):
    _require_csrf(request, csrf_token)
    broker_slug = normalize_broker_slug(broker)
    account_value = account.strip() if account else None
    if not broker_slug:
        return RedirectResponse(url="/app/portfolio?notice=broker&status=failed", status_code=303)

    upsert_broker_connection(
        db,
        user_id=user_id,
        broker=broker_slug,
        account=account_value,
        status="disconnected",
    )
    db.commit()
    return RedirectResponse(
        url=_build_url(
            "/app/portfolio",
            {"notice": "broker", "status": "disconnected", "broker": broker_label(broker_slug)},
        ),
        status_code=303,
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
    kind: str = Form(..., max_length=16),
    value: str = Form(..., max_length=256),
    label: Optional[str] = Form(default=None, max_length=256),
    next: str = Form("/app/watchlist", max_length=2048),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
):
    _require_csrf(request, csrf_token)
    next_url = _safe_next_path(next, default="/app/watchlist")

    kind_norm = kind.strip().lower()
    if kind_norm not in {"ticker", "person"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid kind")

    value_norm = value.strip()
    if not value_norm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing value")

    if kind_norm == "ticker":
        value_norm = value_norm.upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,15}", value_norm):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid ticker format",
            )
    else:
        value_norm = _slugify(value_norm)
        if not value_norm:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid person name",
            )

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

    return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/app/watchlist/remove")
def watchlist_remove(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(_require_login),
    item_id: int = Form(..., ge=1),
    next: str = Form("/app/watchlist", max_length=2048),
    csrf_token: Optional[str] = Form(default=None, max_length=256),
):
    _require_csrf(request, csrf_token)
    next_url = _safe_next_path(next, default="/app/watchlist")

    item = db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
    if not item or item.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    db.delete(item)
    db.commit()
    return RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/app/companies/{ticker}", response_class=HTMLResponse)
def app_company(
    request: Request,
    ticker: str = Path(
        ...,
        min_length=1,
        max_length=16,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$",
    ),
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
    by_form_prefix: dict[str, int] = {}
    for form_value, count in db.execute(
        select(Trade.form, func.count(Trade.id))
        .where(Trade.ticker == ticker_norm)
        .where(Trade.form.is_not(None))
        .group_by(Trade.form)
    ).all():
        prefix = form_prefix(form_value)
        if prefix:
            by_form_prefix[prefix] = by_form_prefix.get(prefix, 0) + int(count)

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
            "by_form_prefix": by_form_prefix,
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
    slug: str = Path(
        ...,
        min_length=1,
        max_length=256,
        pattern=r"^[a-z0-9][a-z0-9-]{0,255}$",
    ),
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
    by_form_prefix: dict[str, int] = {}
    for form_value, count in db.execute(
        select(Trade.form, func.count(Trade.id))
        .where(Trade.person_slug == slug_norm)
        .where(Trade.form.is_not(None))
        .group_by(Trade.form)
    ).all():
        prefix = form_prefix(form_value)
        if prefix:
            by_form_prefix[prefix] = by_form_prefix.get(prefix, 0) + int(count)

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

    summary = db.scalar(select(PersonSummary).where(PersonSummary.person_slug == slug_norm))
    summary_text = summary.summary if summary else None
    summary_updated_at = (
        summary.summary_updated_at.date().isoformat()
        if summary and summary.summary_updated_at
        else None
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
            "by_form_prefix": by_form_prefix,
            "trades": trades,
            "watchlisted": watchlisted,
            "person_summary": summary_text,
            "person_summary_updated_at": summary_updated_at,
        },
    )
