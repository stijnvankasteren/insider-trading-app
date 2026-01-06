from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.aliases import AliasChoices
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.forms import form_prefix, normalize_form
from app.models import Trade
from app.settings import get_settings

router = APIRouter(tags=["ingest"])


class IngestTradeItem(BaseModel):
    """
    Strict-ish schema for ingest payloads.

    We validate and normalize common aliases while keeping unknown fields isolated in `raw`.
    Set `INGEST_REJECT_EXTRA_FIELDS=true` to fully reject unknown fields.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    external_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("external_id", "externalId"),
        max_length=160,
    )
    ticker: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ticker", "symbol"),
        max_length=16,
    )
    company_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "companyName"),
        max_length=256,
    )
    person_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("person_name", "personName"),
        max_length=256,
    )
    person_slug: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("person_slug", "personSlug"),
        max_length=256,
    )
    transaction_type: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("transaction_type", "type"),
        max_length=32,
    )
    form: str = Field(
        validation_alias=AliasChoices("form", "issuerForm", "reportingForm"),
        max_length=32,
    )
    transaction_date: Optional[dt.date] = Field(
        default=None,
        validation_alias=AliasChoices("transaction_date", "transactionDate"),
    )
    filed_at: Optional[dt.datetime] = Field(
        default=None,
        validation_alias=AliasChoices("filed_at", "filedAt"),
    )
    amount_usd_low: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("amount_usd_low", "amountUsdLow"),
    )
    amount_usd_high: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("amount_usd_high", "amountUsdHigh"),
    )
    amount_usd: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices("amount_usd", "amountUsd"),
    )
    shares: Optional[int] = None
    price_usd: Optional[Decimal] = Field(
        default=None,
        validation_alias=AliasChoices("price_usd", "priceUsd"),
    )
    url: Optional[str] = Field(default=None, max_length=1024)
    raw: Optional[dict[str, Any]] = None

    @field_validator(
        "external_id",
        "ticker",
        "company_name",
        "person_name",
        "person_slug",
        "transaction_type",
        "form",
        "url",
        mode="before",
    )
    @classmethod
    def _clean_strings(cls, value: object) -> Optional[str]:
        return _clean_str(value)

    @field_validator("ticker", mode="after")
    @classmethod
    def _normalize_ticker(cls, value: Optional[str]) -> Optional[str]:
        return value.upper() if value else None

    @field_validator("person_slug", mode="after")
    @classmethod
    def _normalize_person_slug(cls, value: Optional[str]) -> Optional[str]:
        return _slugify(value) if value else None

    @field_validator("transaction_date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> Optional[dt.date]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        parsed = _parse_date(value)
        if parsed is None:
            raise ValueError("Invalid transaction_date (expected YYYY-MM-DD)")
        return parsed

    @field_validator("filed_at", mode="before")
    @classmethod
    def _validate_datetime(cls, value: object) -> Optional[dt.datetime]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        parsed = _parse_datetime(value)
        if parsed is None:
            raise ValueError("Invalid filed_at (expected ISO datetime)")
        return parsed

    @field_validator("shares", "amount_usd_low", "amount_usd_high", "amount_usd", mode="before")
    @classmethod
    def _validate_int(cls, value: object) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        parsed = _parse_int(value)
        if parsed is None:
            raise ValueError("Invalid integer value")
        return parsed

    @field_validator("price_usd", mode="before")
    @classmethod
    def _validate_decimal(cls, value: object) -> Optional[Decimal]:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        parsed = _parse_decimal(value)
        if parsed is None:
            raise ValueError("Invalid decimal value")
        return parsed


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


def _cap_raw_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Prevent unbounded growth of the `Trade.raw` JSON column.

    The ingest endpoint is exposed to the network (protected by a secret header),
    so we cap stored raw payload size to reduce DoS risk from huge JSON blobs.
    """

    settings = get_settings()
    max_bytes = max(1_000, int(getattr(settings, "ingest_max_raw_bytes", 50_000)))
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    except Exception:
        return {"truncated": True}

    if len(encoded) <= max_bytes:
        return payload
    return {"truncated": True, "keys": list(payload.keys())[:50]}


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
    secrets = tuple(getattr(settings, "ingest_secrets", ())) or (
        (settings.ingest_secret,) if settings.ingest_secret else ()
    )
    if not secrets:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INGEST_SECRET not configured",
        )
    if not x_ingest_secret or not any(hmac.compare_digest(x_ingest_secret, s) for s in secrets):
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
    inserted = 0
    updated = 0
    skipped_empty = 0
    errors: list[dict[str, Any]] = []

    items: list[tuple[int, dict[str, Any]]] = []
    if isinstance(body, list):
        for idx, item in enumerate(body):
            if not isinstance(item, dict):
                errors.append({"index": idx, "error": "Each item must be an object"})
                continue
            items.append((idx, item))
    elif isinstance(body, dict):
        items.append((0, body))
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must be an object or an array of objects",
        )

    settings = get_settings()
    max_items = max(1, int(getattr(settings, "ingest_max_items", 5000)))
    if len(items) > max_items:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Too many items (max {max_items})",
        )

    for idx, raw in items:
        try:
            item = IngestTradeItem.model_validate(raw)
        except ValidationError as exc:
            summary = "; ".join(f"{e.get('loc')}: {e.get('msg')}" for e in exc.errors()[:5])
            errors.append({"index": idx, "error": f"Invalid item: {summary or 'validation error'}"})
            continue

        extra = item.model_extra or {}
        if settings.ingest_reject_extra_fields and extra:
            errors.append(
                {
                    "index": idx,
                    "error": f"Unexpected field(s): {', '.join(sorted(extra.keys()))}",
                }
            )
            continue

        ticker_value = item.ticker
        company_name_value = item.company_name
        person_name_value = item.person_name
        person_slug_value = item.person_slug
        tx_type_value = item.transaction_type

        form_value = normalize_form(item.form)
        if not form_value and tx_type_value:
            maybe_form = normalize_form(tx_type_value)
            if form_prefix(maybe_form):
                form_value = maybe_form
                tx_type_value = None

        prefix = form_prefix(form_value)
        if not prefix:
            errors.append({"index": idx, "error": "Missing or invalid 'form'"})
            continue

        shares_value = item.shares
        price_usd_value = item.price_usd

        amount_usd_low = item.amount_usd_low
        amount_usd_high = item.amount_usd_high
        amount_usd = item.amount_usd

        url_value = item.url
        external_id_value = item.external_id

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
            "person_slug": person_slug_value,
            "transaction_type": tx_type_value,
            "form": form_value,
            "transaction_date": item.transaction_date,
            "filed_at": item.filed_at,
            "amount_usd_low": amount_usd_low,
            "amount_usd_high": amount_usd_high,
            "shares": shares_value,
            "price_usd": price_usd_value,
            "url": url_value,
            "raw": _cap_raw_payload(raw),
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
    request: Request,
    confirm: bool = Query(default=False),
    form: Optional[str] = Query(default=None, max_length=32),
    _: None = Depends(_require_ingest_secret),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    allowed_params = {"confirm", "form"}
    unexpected = sorted(set(request.query_params.keys()) - allowed_params)
    if unexpected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unexpected query parameter(s): {', '.join(unexpected)}",
        )

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
