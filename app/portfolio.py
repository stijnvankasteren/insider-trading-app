from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BrokerConnection, PortfolioImport, PortfolioTransaction
from app.settings import get_settings

BROKER_CATALOG: dict[str, str] = {
    "degiro": "DeGiro",
    "trading212": "Trading 212",
    "etoro": "eToro",
}

CSV_TEMPLATE_HEADERS: list[str] = [
    "date",
    "type",
    "ticker",
    "name",
    "quantity",
    "price",
    "fees",
    "amount",
    "currency",
    "broker",
    "account",
    "external_id",
    "notes",
]

_HEADER_ALIASES: dict[str, set[str]] = {
    "date": {
        "date",
        "trade_date",
        "transaction_date",
        "executed_date",
        "execution_date",
    },
    "settlement_date": {"settlement_date", "settle_date", "settled_date"},
    "type": {"type", "activity", "side", "transaction_type", "action"},
    "ticker": {"ticker", "symbol", "isin"},
    "name": {"name", "security_name", "instrument_name"},
    "quantity": {"quantity", "qty", "shares", "units"},
    "price": {"price", "price_per_share", "price_per_unit", "rate"},
    "fees": {"fees", "fee", "commission", "commissions"},
    "amount": {"amount", "total", "value", "gross", "net"},
    "currency": {"currency", "ccy"},
    "broker": {"broker", "provider"},
    "account": {"account", "account_id", "account_name"},
    "external_id": {"external_id", "id", "trade_id", "order_id", "execution_id"},
    "notes": {"notes", "note", "memo", "description", "details"},
}

_HEADER_TO_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _HEADER_ALIASES.items()
    for alias in aliases
}

@dataclass
class PortfolioCsvResult:
    items: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    skipped_empty: int


def normalize_broker_slug(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip().lower()
    cleaned = cleaned.replace(" ", "").replace("-", "")
    if cleaned in {"degiro", "degiros"}:
        return "degiro"
    if cleaned in {"trading212", "trading212invest", "trading212isa"}:
        return "trading212"
    if cleaned in {"etoro"}:
        return "etoro"
    return cleaned or None


def broker_label(slug: Optional[str]) -> Optional[str]:
    if not slug:
        return None
    return BROKER_CATALOG.get(slug, slug)


def decode_upload(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize_header(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return cleaned.strip("_")


def _parse_decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()")
        raw = raw.replace(",", "")
        raw = re.sub(r"[^0-9.+-]", "", raw)
        try:
            parsed = Decimal(raw)
        except Exception:
            return None
        return -parsed if negative else parsed
    return None


def _parse_date(value: object) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        raw = raw.split("T", 1)[0].split(" ", 1)[0]
        raw = raw.replace("/", "-")
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _has_portfolio_data(payload: dict[str, Any]) -> bool:
    for key in (
        "symbol",
        "name",
        "activity_type",
        "quantity",
        "price",
        "fees",
        "amount",
        "notes",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    return False


def _make_portfolio_external_id(payload: dict[str, Any]) -> str:
    stable = {
        "broker": payload.get("broker"),
        "account": payload.get("account"),
        "activity_type": payload.get("activity_type"),
        "symbol": payload.get("symbol"),
        "name": payload.get("name"),
        "trade_date": payload.get("trade_date"),
        "settlement_date": payload.get("settlement_date"),
        "quantity": payload.get("quantity"),
        "price": payload.get("price"),
        "fees": payload.get("fees"),
        "amount": payload.get("amount"),
        "currency": payload.get("currency"),
        "notes": payload.get("notes"),
    }
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"portfolio:{digest}"


def parse_portfolio_csv(
    text: str,
    *,
    default_broker: Optional[str] = None,
    default_account: Optional[str] = None,
    default_currency: Optional[str] = None,
    max_items: Optional[int] = None,
) -> PortfolioCsvResult:
    settings = get_settings()
    max_items = max_items or max(1, int(getattr(settings, "portfolio_max_items", 5000)))
    default_account = default_account.strip() if default_account else None
    default_currency = default_currency.strip().upper() if default_currency else None

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return PortfolioCsvResult(items=[], errors=[{"row": 0, "error": "Missing headers"}], skipped_empty=0)

    header_map: dict[str, str] = {}
    for header in reader.fieldnames:
        normalized = _normalize_header(header)
        canonical = _HEADER_TO_CANONICAL.get(normalized)
        if canonical:
            header_map[header] = canonical

    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped_empty = 0

    for row_idx, row in enumerate(reader, start=1):
        if row_idx > max_items:
            errors.append(
                {
                    "row": row_idx,
                    "error": f"Too many rows (max {max_items})",
                }
            )
            break

        normalized: dict[str, Optional[str]] = {}
        for raw_key, value in row.items():
            canonical = header_map.get(raw_key)
            if not canonical:
                continue
            if value is None:
                continue
            trimmed = value.strip() if isinstance(value, str) else value
            if trimmed is None or trimmed == "":
                continue
            if not normalized.get(canonical):
                normalized[canonical] = str(trimmed)

        trade_date = _parse_date(normalized.get("date"))
        if normalized.get("date") and trade_date is None:
            errors.append({"row": row_idx, "error": "Invalid date (expected YYYY-MM-DD)"})
            continue

        settlement_date = _parse_date(normalized.get("settlement_date"))
        if normalized.get("settlement_date") and settlement_date is None:
            errors.append(
                {"row": row_idx, "error": "Invalid settlement_date (expected YYYY-MM-DD)"}
            )
            continue

        activity_type = normalized.get("type")
        activity_type = activity_type.upper() if activity_type else None

        symbol = normalized.get("ticker")
        symbol = symbol.upper() if symbol else None

        payload: dict[str, Any] = {
            "broker": normalize_broker_slug(normalized.get("broker") or default_broker),
            "account": normalized.get("account") or default_account,
            "activity_type": activity_type,
            "symbol": symbol,
            "name": normalized.get("name"),
            "trade_date": trade_date,
            "settlement_date": settlement_date,
            "quantity": _parse_decimal(normalized.get("quantity")),
            "price": _parse_decimal(normalized.get("price")),
            "fees": _parse_decimal(normalized.get("fees")),
            "amount": _parse_decimal(normalized.get("amount")),
            "currency": (normalized.get("currency") or default_currency or "").upper() or None,
            "notes": normalized.get("notes"),
        }

        if payload["amount"] is None and payload["quantity"] is not None and payload["price"] is not None:
            payload["amount"] = payload["quantity"] * payload["price"]

        if not _has_portfolio_data(payload):
            skipped_empty += 1
            continue

        payload["external_id"] = normalized.get("external_id") or _make_portfolio_external_id(
            payload
        )
        payload["raw"] = row

        items.append(payload)

    return PortfolioCsvResult(items=items, errors=errors, skipped_empty=skipped_empty)


def upsert_portfolio_transactions(
    db: Session,
    *,
    user_id: str,
    items: list[dict[str, Any]],
    import_batch: Optional[str] = None,
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    for item in items:
        external_id = item.get("external_id")
        if not external_id:
            continue
        existing = db.scalar(
            select(PortfolioTransaction).where(
                PortfolioTransaction.user_id == user_id,
                PortfolioTransaction.external_id == external_id,
            )
        )
        if existing:
            for key in (
                "broker",
                "account",
                "activity_type",
                "symbol",
                "name",
                "trade_date",
                "settlement_date",
                "quantity",
                "price",
                "fees",
                "amount",
                "currency",
                "notes",
                "raw",
            ):
                value = item.get(key)
                if value is not None:
                    setattr(existing, key, value)
            if import_batch:
                existing.import_batch = import_batch
            updated += 1
        else:
            db.add(
                PortfolioTransaction(
                    user_id=user_id,
                    external_id=external_id,
                    broker=item.get("broker"),
                    account=item.get("account"),
                    activity_type=item.get("activity_type"),
                    symbol=item.get("symbol"),
                    name=item.get("name"),
                    trade_date=item.get("trade_date"),
                    settlement_date=item.get("settlement_date"),
                    quantity=item.get("quantity"),
                    price=item.get("price"),
                    fees=item.get("fees"),
                    amount=item.get("amount"),
                    currency=item.get("currency"),
                    notes=item.get("notes"),
                    import_batch=import_batch,
                    raw=item.get("raw"),
                )
            )
            inserted += 1
    return inserted, updated


def add_portfolio_import(
    db: Session,
    *,
    user_id: str,
    source: str,
    status: str,
    broker: Optional[str] = None,
    file_name: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    inserted: Optional[int] = None,
    updated: Optional[int] = None,
    error_count: Optional[int] = None,
    message: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
) -> PortfolioImport:
    record = PortfolioImport(
        user_id=user_id,
        source=source,
        broker=broker,
        status=status,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        inserted=inserted,
        updated=updated,
        error_count=error_count,
        message=message,
        raw=raw,
    )
    db.add(record)
    return record


def upsert_broker_connection(
    db: Session,
    *,
    user_id: str,
    broker: str,
    account: Optional[str] = None,
    status: str = "pending",
    error_message: Optional[str] = None,
    raw: Optional[dict[str, Any]] = None,
) -> BrokerConnection:
    existing = db.scalar(
        select(BrokerConnection).where(
            BrokerConnection.user_id == user_id,
            BrokerConnection.broker == broker,
            BrokerConnection.account == account,
        )
    )
    if existing:
        existing.status = status
        existing.error_message = error_message
        if raw:
            existing.raw = raw
        return existing

    connection = BrokerConnection(
        user_id=user_id,
        broker=broker,
        account=account,
        status=status,
        error_message=error_message,
        raw=raw,
    )
    db.add(connection)
    return connection
