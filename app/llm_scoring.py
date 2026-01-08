from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from typing import Optional

import httpx
from sqlalchemy import or_, select

from app.db import SessionLocal
from app.forms import form_prefix
from app.models import Trade
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

MASTER_PROMPT = """MASTER PROMPT - "Elite Trade Intelligence Analyst"

Role & Expertise
You are an elite investment analyst with decades of experience across public equities, insider
trading analysis, political finance, corporate fundamentals, and behavioral finance. You think
like a hedge fund CIO and forensic researcher.

Your goal is not to hype trades, but to assess whether copying a specific insider or politician
trade is rational, risk-aware, and evidence-based.

You are analytical, skeptical, data-driven, and conservative with confidence.

Inputs You Will Receive
One or more PDF documents containing:
- Politician or insider trade disclosures
- Dates, transaction size, security, and trade type (buy/sell/options)
- Optional user notes or questions

Your Core Objective
Determine whether the disclosed trade is smart to copy, based on:
- The credibility and historical skill of the trader
- The context and structure of the trade
- The company's fundamentals and narrative
- The timing and incentives behind the trade
- Risks, uncertainty, and alternative explanations

You must then return:
- A Confidence Score (0-100)
- A clear, structured explanation justifying that score

Required Analysis Framework (Follow in Order)

1. Trader Assessment
Evaluate the trader's credibility:
- Are they a politician, executive, or insider with direct informational advantage?

History of prior trades:
- Frequency
- Pattern consistency
- Past outcomes (if inferable)

Possible non-alpha motives:
- Hedging
- Optics
- Scheduled or rule-based trades
- Diversification

Classify the trader as:
- High-signal
- Medium-signal
- Low-signal

2. Trade Quality & Structure
Analyze:
- Buy vs sell (buys usually stronger than sells)
- Trade size relative to:
  - Trader's net worth
  - Typical trade size
- Timing:
  - Proximity to earnings, legislation, contracts, or major events
- Instrument used:
  - Common stock
  - Options (calls/puts, expiry, strike)
- Whether this appears conviction-based or routine

3. Company & Sector Analysis
Research and reason about:
- Business model and revenue drivers
- Financial health (profitability, debt, growth trajectory)
- Valuation context (cheap, fair, stretched)
- Sector tailwinds or headwinds

Exposure to:
- Regulation
- Government spending
- Macroeconomic trends

Focus on why this specific trader would care about this specific company.

4. Information Advantage & Timing Edge
Assess whether:
- The trader could plausibly have material insight (policy, contracts, regulation, industry knowledge)
- The trade timing suggests anticipation of:
  - Legislation
  - Regulatory change
  - Government funding
  - Strategic announcements

Clearly separate signal from speculation.

5. Risk & Counterarguments
Explicitly list:
- Reasons this trade might fail
- Reasons copying could be misleading
- What information is unknown or delayed
- Market conditions that could negate the thesis

Output Format (STRICT)

Copy-Trade Confidence Score: XX / 100
Score Explanation:
(Structured, concise, evidence-based justification)
"""

_SCORE_RE = re.compile(r"Score\\s*:\\s*([0-9]{1,3})\\s*/\\s*100", re.IGNORECASE)
_SCORE_FALLBACK_RE = re.compile(r"([0-9]{1,3})\\s*/\\s*100")

_scoring_started = False


def _normalized_tx_type(trade: Trade) -> str:
    raw = (trade.transaction_type or "").strip()
    if form_prefix(trade.form) == "FORM 4":
        code = raw.upper()
        if code == "A":
            return "BUY"
        if code == "D":
            return "SELL"
    return raw or "UNKNOWN"


def _trade_amount_mid(trade: Trade) -> Optional[float]:
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


def _trade_summary(trade: Trade) -> str:
    prefix = form_prefix(trade.form)
    if prefix == "CONGRESS":
        trader_kind = "Politician disclosure"
    elif prefix in {"FORM 3", "FORM 4"}:
        trader_kind = "Insider disclosure"
    elif prefix:
        trader_kind = f"Other filing ({prefix})"
    else:
        trader_kind = "Unknown filing"

    transaction_date = trade.transaction_date.isoformat() if trade.transaction_date else "unknown"
    filed_date = trade.filed_at.date().isoformat() if trade.filed_at else "unknown"
    amount_mid = _trade_amount_mid(trade)
    amount_mid_text = f"{amount_mid:.2f}" if amount_mid is not None else "unknown"

    lines = [
        "Trade disclosure summary (no external data available):",
        f"- Today: {dt.date.today().isoformat()}",
        f"- Trader name: {trade.person_name or 'unknown'}",
        f"- Trader category: {trader_kind}",
        f"- Form: {trade.form or 'unknown'}",
        f"- Ticker: {trade.ticker or 'unknown'}",
        f"- Company: {trade.company_name or 'unknown'}",
        f"- Transaction type (raw): {trade.transaction_type or 'unknown'}",
        f"- Transaction type (normalized): {_normalized_tx_type(trade)}",
        f"- Transaction date: {transaction_date}",
        f"- Filed date: {filed_date}",
        f"- Amount USD low: {trade.amount_usd_low if trade.amount_usd_low is not None else 'unknown'}",
        f"- Amount USD high: {trade.amount_usd_high if trade.amount_usd_high is not None else 'unknown'}",
        f"- Amount USD midpoint: {amount_mid_text}",
        f"- Shares: {trade.shares if trade.shares is not None else 'unknown'}",
        f"- Price USD: {trade.price_usd if trade.price_usd is not None else 'unknown'}",
        f"- URL: {trade.url or 'unknown'}",
        "",
        "Use only the information above. If something is missing, say it is unknown.",
    ]
    return "\n".join(lines)


def _extract_score(text: str) -> Optional[int]:
    match = _SCORE_RE.search(text)
    if not match:
        match = _SCORE_FALLBACK_RE.search(text)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except ValueError:
        return None
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


def _call_llm(settings: Settings, *, prompt: str) -> str:
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY not configured")

    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    if settings.public_base_url:
        headers["HTTP-Referer"] = settings.public_base_url
    if settings.app_name:
        headers["X-Title"] = settings.app_name

    payload = {
        "model": settings.llm_model,
        "temperature": 0.2,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": MASTER_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }

    timeout = httpx.Timeout(settings.llm_score_timeout_seconds)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM response missing choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("LLM response missing content")
    return str(content)


def score_trade_with_llm(trade: Trade, settings: Settings) -> tuple[int, str]:
    prompt = _trade_summary(trade)
    content = _call_llm(settings, prompt=prompt)
    score = _extract_score(content)
    if score is None:
        raise RuntimeError("Could not parse score from LLM response")
    return score, content


def score_trades_once() -> dict[str, int]:
    settings = get_settings()
    if not settings.llm_score_enabled or not settings.llm_api_key:
        return {"scored": 0, "failed": 0}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=settings.llm_score_stale_hours)
    max_items = settings.llm_score_max_per_run
    sleep_seconds = settings.llm_score_sleep_ms / 1000 if settings.llm_score_sleep_ms else 0

    scored = 0
    failed = 0

    with SessionLocal() as db:
        stmt = (
            select(Trade)
            .where(or_(Trade.score_updated_at.is_(None), Trade.score_updated_at < cutoff))
            .order_by(Trade.score_updated_at.is_(None).desc(), Trade.created_at.desc())
        )
        if max_items > 0:
            stmt = stmt.limit(max_items)

        trades = db.scalars(stmt).all()
        for trade in trades:
            try:
                score, explanation = score_trade_with_llm(trade, settings)
            except Exception as exc:
                failed += 1
                logger.warning("LLM scoring failed for trade %s: %s", trade.id, exc)
                continue

            trade.score = score
            trade.score_model = settings.llm_model
            trade.score_explanation = explanation
            trade.score_updated_at = dt.datetime.utcnow()
            db.add(trade)
            db.commit()
            scored += 1

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    return {"scored": scored, "failed": failed}


def _seconds_until_next_run(hour: int, minute: int) -> float:
    now = dt.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return max(0, (target - now).total_seconds())


def _scoring_loop() -> None:
    settings = get_settings()
    logger.info("LLM scoring scheduler started (model=%s).", settings.llm_model)

    score_trades_once()

    while True:
        delay = _seconds_until_next_run(settings.llm_score_daily_hour, settings.llm_score_daily_minute)
        time.sleep(delay)
        score_trades_once()


def start_llm_scoring() -> None:
    settings = get_settings()
    if not settings.llm_score_enabled or not settings.llm_api_key:
        return

    global _scoring_started
    if _scoring_started:
        return
    _scoring_started = True

    thread = threading.Thread(target=_scoring_loop, name="llm-scoring", daemon=True)
    thread.start()
