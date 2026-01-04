from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from decimal import Decimal
from typing import Any
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Trade
from app.settings import get_settings

router = APIRouter(tags=["ingest"])


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _parse_date(value: object) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        v = v.replace("/", "-")
        try:
            return dt.date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _parse_datetime(value: object) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        # Python 3.9 doesn't parse trailing "Z"
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        try:
            return dt.datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def _parse_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        v = value.strip().replace(",", "")
        if not v:
            return None
        try:
            return int(Decimal(v))
        except Exception:
            return None
    return None


def _parse_decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        v = value.strip().replace(",", "")
        if not v:
            return None
        try:
            return Decimal(v)
        except Exception:
            return None
    return None


def _make_external_id(payload: dict[str, Any]) -> str:
    stable = {
        "source": payload.get("source"),
        "ticker": payload.get("ticker"),
        "company_name": payload.get("company_name"),
        "person_name": payload.get("person_name"),
        "transaction_type": payload.get("transaction_type"),
        "form": payload.get("form"),
        "transaction_date": payload.get("transaction_date"),
        "filed_at": payload.get("filed_at"),
        "amount_usd_low": payload.get("amount_usd_low"),
        "amount_usd_high": payload.get("amount_usd_high"),
        "shares": payload.get("shares"),
        "price_usd": payload.get("price_usd"),
        "url": payload.get("url"),
    }
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"gen:{digest}"


def _require_ingest_secret(
    x_ingest_secret: Optional[str] = Header(default=None),
) -> None:
    settings = get_settings()
    if not settings.ingest_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INGEST_SECRET not configured",
        )
    if x_ingest_secret != settings.ingest_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid ingest secret",
        )


@router.post("/trades")
def ingest_trades(
    body: Any = Body(...),
    _: None = Depends(_require_ingest_secret),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items: list[dict[str, Any]]
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict)]
    elif isinstance(body, dict):
        items = [body]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be an object or an array of objects",
        )

    inserted = 0
    updated = 0
    errors: list[dict[str, Any]] = []

    for idx, raw in enumerate(items):
        source = (raw.get("source") or "").strip().lower()
        if not source:
            errors.append({"index": idx, "error": "Missing 'source'"})
            continue

        ticker_value = raw.get("ticker") or raw.get("symbol") or None
        if isinstance(ticker_value, str):
            ticker_value = ticker_value.strip().upper()
            if not ticker_value:
                ticker_value = None

        person_name_value = raw.get("person_name") or raw.get("personName")
        if isinstance(person_name_value, str):
            person_name_value = person_name_value.strip()
            if not person_name_value:
                person_name_value = None

        tx_type_value = raw.get("transaction_type") or raw.get("type")
        if isinstance(tx_type_value, str):
            tx_type_value = tx_type_value.strip()
            if not tx_type_value:
                tx_type_value = None

        form_value = raw.get("form") or raw.get("issuerForm") or raw.get("reportingForm")
        if isinstance(form_value, bool):
            form_value = None
        elif isinstance(form_value, (int, float)):
            form_value = str(int(form_value))
        if isinstance(form_value, str):
            form_value = form_value.strip()
            if not form_value:
                form_value = None
            elif not re.match(r"^form\\b", form_value, flags=re.IGNORECASE):
                form_value = f"FORM {form_value}"

        if (
            not form_value
            and isinstance(tx_type_value, str)
            and re.match(r"^form\\b", tx_type_value, flags=re.IGNORECASE)
        ):
            form_value = tx_type_value
            tx_type_value = None

        amount_usd_low = _parse_int(raw.get("amount_usd_low") or raw.get("amountUsdLow"))
        amount_usd_high = _parse_int(
            raw.get("amount_usd_high") or raw.get("amountUsdHigh")
        )
        amount_usd = _parse_int(raw.get("amount_usd") or raw.get("amountUsd"))
        if amount_usd_low is None and amount_usd_high is None and amount_usd is not None:
            amount_usd_low = amount_usd
            amount_usd_high = amount_usd
        elif source == "insider":
            if amount_usd_low is None and amount_usd_high is not None:
                amount_usd_low = amount_usd_high
            elif amount_usd_high is None and amount_usd_low is not None:
                amount_usd_high = amount_usd_low

        payload: dict[str, Any] = {
            "source": source,
            "external_id": raw.get("external_id") or raw.get("externalId"),
            "ticker": ticker_value,
            "company_name": raw.get("company_name") or raw.get("companyName"),
            "person_name": person_name_value,
            "transaction_type": tx_type_value,
            "form": form_value,
            "transaction_date": _parse_date(
                raw.get("transaction_date") or raw.get("transactionDate")
            ),
            "filed_at": _parse_datetime(raw.get("filed_at") or raw.get("filedAt")),
            "amount_usd_low": amount_usd_low,
            "amount_usd_high": amount_usd_high,
            "shares": _parse_int(raw.get("shares")),
            "price_usd": _parse_decimal(raw.get("price_usd") or raw.get("priceUsd")),
            "url": raw.get("url"),
            "raw": raw,
        }

        if not payload["external_id"]:
            payload["external_id"] = _make_external_id(payload)

        if payload["person_name"] and not payload.get("person_slug"):
            payload["person_slug"] = _slugify(str(payload["person_name"]))

        existing = db.scalar(select(Trade).where(Trade.external_id == payload["external_id"]))
        if existing:
            for key in (
                "source",
                "ticker",
                "company_name",
                "person_name",
                "person_slug",
                "transaction_type",
                "form",
                "transaction_date",
                "filed_at",
                "amount_usd_low",
                "amount_usd_high",
                "shares",
                "price_usd",
                "url",
                "raw",
            ):
                value = payload.get(key)
                if value is not None:
                    setattr(existing, key, value)
            updated += 1
        else:
            db.add(
                Trade(
                    source=payload["source"],
                    external_id=payload["external_id"],
                    ticker=payload.get("ticker"),
                    company_name=payload.get("company_name"),
                    person_name=payload.get("person_name"),
                    person_slug=payload.get("person_slug"),
                    transaction_type=payload.get("transaction_type"),
                    form=payload.get("form"),
                    transaction_date=payload.get("transaction_date"),
                    filed_at=payload.get("filed_at"),
                    amount_usd_low=payload.get("amount_usd_low"),
                    amount_usd_high=payload.get("amount_usd_high"),
                    shares=payload.get("shares"),
                    price_usd=payload.get("price_usd"),
                    url=payload.get("url"),
                    raw=payload.get("raw"),
                )
            )
            inserted += 1

    db.commit()
    return {"inserted": inserted, "updated": updated, "errors": errors[:50]}


@router.delete("/trades")
def delete_trades(
    confirm: bool = Query(default=False),
    source: Optional[str] = Query(default=None),
    _: None = Depends(_require_ingest_secret),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add ?confirm=true to delete trades",
        )

    source_value: Optional[str] = None
    stmt = delete(Trade)
    if source is not None:
        normalized = source.strip().lower()
        if normalized:
            source_value = normalized
            stmt = stmt.where(Trade.source == normalized)

    result = db.execute(stmt)
    db.commit()
    return {"deleted": int(result.rowcount or 0), "source": source_value}
