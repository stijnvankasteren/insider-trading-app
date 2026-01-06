from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import form_prefix, normalize_form
from app.models import Trade
from app.settings import get_settings

router = APIRouter(tags=["ingest"])


def _clean_str(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        value = str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    return v or None


def _has_trade_data(payload: dict[str, Any]) -> bool:
    for key in (
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
    ):
        value = payload.get(key)
        if isinstance(value, str):
            if value.strip():
                return True
        elif value is not None:
            return True
    return False


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
    skipped_empty = 0
    errors: list[dict[str, Any]] = []

    for idx, raw in enumerate(items):
        ticker_value = _clean_str(raw.get("ticker") or raw.get("symbol"))
        if ticker_value:
            ticker_value = ticker_value.upper()

        company_name_value = _clean_str(raw.get("company_name") or raw.get("companyName"))
        person_name_value = _clean_str(raw.get("person_name") or raw.get("personName"))

        tx_type_value = _clean_str(raw.get("transaction_type") or raw.get("type"))

        form_value = normalize_form(
            raw.get("form") or raw.get("issuerForm") or raw.get("reportingForm")
        )
        if not form_value and tx_type_value:
            maybe_form = normalize_form(tx_type_value)
            if form_prefix(maybe_form):
                form_value = maybe_form
                tx_type_value = None

        prefix = form_prefix(form_value)
        if not prefix:
            errors.append({"index": idx, "error": "Missing or invalid 'form'"})
            continue

        shares_value = _parse_int(raw.get("shares"))
        price_usd_value = _parse_decimal(raw.get("price_usd") or raw.get("priceUsd"))

        amount_usd_low = _parse_int(raw.get("amount_usd_low") or raw.get("amountUsdLow"))
        amount_usd_high = _parse_int(raw.get("amount_usd_high") or raw.get("amountUsdHigh"))
        amount_usd = _parse_int(raw.get("amount_usd") or raw.get("amountUsd"))

        url_value = _clean_str(raw.get("url"))
        external_id_value = _clean_str(raw.get("external_id") or raw.get("externalId"))

        if prefix in ("FORM 3", "FORM 4") and shares_value is not None and price_usd_value is not None:
            computed_amount = (price_usd_value * Decimal(shares_value)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
            amount_usd_low = int(computed_amount)
            amount_usd_high = int(computed_amount)
        else:
            if amount_usd_low is None and amount_usd_high is None and amount_usd is not None:
                amount_usd_low = amount_usd
                amount_usd_high = amount_usd
            if prefix in ("FORM 3", "FORM 4"):
                if amount_usd_low is None and amount_usd_high is not None:
                    amount_usd_low = amount_usd_high
                elif amount_usd_high is None and amount_usd_low is not None:
                    amount_usd_high = amount_usd_low
            elif prefix == "CONGRESS":
                if amount_usd_low is None or amount_usd_high is None:
                    errors.append(
                        {
                            "index": idx,
                            "error": "For form=CONGRESS, provide amount_usd_low and amount_usd_high",
                        }
                    )
                    continue

        payload: dict[str, Any] = {
            "external_id": external_id_value,
            "ticker": ticker_value,
            "company_name": company_name_value,
            "person_name": person_name_value,
            "transaction_type": tx_type_value,
            "form": form_value,
            "transaction_date": _parse_date(
                raw.get("transaction_date") or raw.get("transactionDate")
            ),
            "filed_at": _parse_datetime(raw.get("filed_at") or raw.get("filedAt")),
            "amount_usd_low": amount_usd_low,
            "amount_usd_high": amount_usd_high,
            "shares": shares_value,
            "price_usd": price_usd_value,
            "url": url_value,
            "raw": raw,
        }

        if not payload["external_id"]:
            payload["external_id"] = _make_external_id(payload)

        if payload["person_name"] and not payload.get("person_slug"):
            payload["person_slug"] = _slugify(str(payload["person_name"]))

        if not _has_trade_data(payload):
            skipped_empty += 1
            continue

        existing = db.scalar(select(Trade).where(Trade.external_id == payload["external_id"]))
        if existing:
            for key in (
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
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_empty": skipped_empty,
        "errors": errors[:50],
    }


@router.delete("/trades")
def delete_trades(
    confirm: bool = Query(default=False),
    form: Optional[str] = Query(default=None),
    _: None = Depends(_require_ingest_secret),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add ?confirm=true to delete trades",
        )

    form_value: Optional[str] = None
    stmt = delete(Trade)
    if form is not None:
        normalized_form = normalize_form(form)
        prefix = form_prefix(normalized_form)
        if form.strip() and not prefix:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid form: {form}",
            )
        if prefix:
            form_value = prefix
            stmt = stmt.where(func.lower(Trade.form).like(f"{prefix.lower()}%"))

    result = db.execute(stmt)
    db.commit()
    return {"deleted": int(result.rowcount or 0), "form": form_value}
