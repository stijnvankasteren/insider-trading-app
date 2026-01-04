from __future__ import annotations

import csv
import datetime as dt
import io
import re
import time
from dataclasses import dataclass

import httpx


class MarketDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class PricePoint:
    date: dt.date
    close: float


_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
_CACHE_TTL_SECONDS = 10 * 60
_CACHE: dict[str, tuple[float, tuple[str, list[PricePoint]]]] = {}


def fetch_stooq_daily_prices(ticker: str) -> tuple[str, list[PricePoint]]:
    """
    Fetch daily OHLC data from Stooq and return (resolved_symbol, close_series).

    Stooq uses symbols like `aapl.us`. If the user passes a plain ticker like `AAPL`,
    we try both `aapl.us` and `aapl`.
    """

    raw = (ticker or "").strip()
    if not raw:
        raise MarketDataError("Enter a ticker.")

    if not _TICKER_RE.match(raw):
        raise MarketDataError("Invalid ticker format.")

    normalized = raw.lower()
    candidates = [normalized] if "." in normalized else [f"{normalized}.us", normalized]

    cache_key = candidates[0]
    cached = _CACHE.get(cache_key)
    now = time.time()
    if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    last_error: Exception | None = None
    for symbol in candidates:
        try:
            points = _fetch_stooq_symbol(symbol)
        except MarketDataError as exc:
            last_error = exc
            continue
        resolved = (symbol, points)
        _CACHE[cache_key] = (now, resolved)
        return resolved

    if last_error:
        raise MarketDataError(str(last_error))
    raise MarketDataError("No data found for this ticker.")


def _fetch_stooq_symbol(symbol: str) -> list[PricePoint]:
    url = "https://stooq.com/q/d/l/"
    params = {"s": symbol, "i": "d"}
    try:
        response = httpx.get(
            url,
            params=params,
            timeout=10.0,
            follow_redirects=True,
            headers={"user-agent": "AltData/1.0 (+https://stooq.com)"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MarketDataError("Could not fetch price data right now.") from exc

    text = response.text.strip()
    if not text:
        raise MarketDataError("No data found for this ticker.")

    reader = csv.DictReader(io.StringIO(text))
    points: list[PricePoint] = []
    for row in reader:
        date_raw = (row.get("Date") or "").strip()
        close_raw = (row.get("Close") or "").strip()
        if not date_raw or not close_raw or close_raw.upper() == "N/A":
            continue
        try:
            date_value = dt.date.fromisoformat(date_raw)
            close_value = float(close_raw)
        except ValueError:
            continue
        if close_value <= 0:
            continue
        points.append(PricePoint(date=date_value, close=close_value))

    points.sort(key=lambda p: p.date)
    if len(points) < 2:
        raise MarketDataError("No data found for this ticker.")

    return points

