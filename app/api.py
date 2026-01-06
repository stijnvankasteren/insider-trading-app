from __future__ import annotations

import datetime as dt
import io
import csv

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import form_prefix, normalize_form
from app.ingest import router as ingest_router
from app.models import Trade
from app.sanitization import sql_like_contains
from app.settings import get_settings

router = APIRouter()

router.include_router(ingest_router, prefix="/ingest")


@router.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


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

    items = [
        {
            "external_id": t.external_id,
            "ticker": t.ticker,
            "company_name": t.company_name,
            "person_name": t.person_name,
            "person_slug": t.person_slug,
            "transaction_type": t.transaction_type,
            "form": t.form,
            "transaction_date": t.transaction_date.isoformat()
            if t.transaction_date
            else None,
            "filed_at": t.filed_at.isoformat() if t.filed_at else None,
            "amount_usd_low": t.amount_usd_low,
            "amount_usd_high": t.amount_usd_high,
            "shares": t.shares,
            "price_usd": str(t.price_usd) if t.price_usd is not None else None,
            "url": t.url,
        }
        for t in trades
    ]

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
