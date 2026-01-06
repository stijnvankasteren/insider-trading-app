from __future__ import annotations

import datetime as dt
import io
import csv

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.ingest import router as ingest_router
from app.models import Trade
from app.sources import normalize_source
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


def _parse_iso_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/trades")
def list_trades(
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    source: Optional[str] = None,
    ticker: Optional[str] = None,
    person: Optional[str] = None,
    tx_type: Optional[str] = Query(default=None, alias="type"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    date_from = _parse_iso_date(from_date)
    date_to = _parse_iso_date(to_date)

    conditions = []
    if source is not None:
        source_norm = normalize_source(source)
        if source_norm:
            conditions.append(Trade.source == source_norm)
        elif source.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid source: {source}",
            )
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
            "source": t.source,
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
    _: None = Depends(_require_api_login),
    db: Session = Depends(get_db),
    source: Optional[str] = None,
    ticker: Optional[str] = None,
    person: Optional[str] = None,
    tx_type: Optional[str] = Query(default=None, alias="type"),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    limit: int = 5000,
) -> Response:
    limit = max(1, min(int(limit or 5000), 5000))
    date_from = _parse_iso_date(from_date)
    date_to = _parse_iso_date(to_date)

    conditions = []
    if source is not None:
        source_norm = normalize_source(source)
        if source_norm:
            conditions.append(Trade.source == source_norm)
        elif source.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid source: {source}",
            )
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
            "source",
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
                t.source,
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
