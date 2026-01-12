from __future__ import annotations

import datetime as dt
import io
import csv
import secrets

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, status
from fastapi import UploadFile
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import form_prefix, normalize_form
from app.ingest import router as ingest_router
from app.models import BrokerConnection, Trade
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
