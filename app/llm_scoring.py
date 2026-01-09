from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from typing import Optional

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.forms import FORM_PREFIX_ORDER, form_prefix
from app.models import PersonSummary, Trade
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SCORE_SYSTEM_PROMPT = """MASTER PROMPT - "Elite Trade Intelligence Analyst"

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

PERSON_SUMMARY_SYSTEM_PROMPT = """You write short, factual summaries about a person based only on the provided trade data.
Use a neutral tone, avoid speculation, and do not add facts that are not present.
If something is unknown, say it is unknown.
Output 2 to 4 sentences in plain text.
"""

_SCORE_RE = re.compile(r"Score\\s*:\\s*([0-9]{1,3})\\s*/\\s*100", re.IGNORECASE)
_SCORE_FALLBACK_RE = re.compile(r"([0-9]{1,3})\\s*/\\s*100")

_scoring_started = False


def _normalize_tx_type(form: Optional[str], tx_type: Optional[str]) -> str:
    raw = (tx_type or "").strip()
    if form_prefix(form) == "FORM 4":
        code = raw.upper()
        if code == "A":
            return "BUY"
        if code == "D":
            return "SELL"
    return raw or "UNKNOWN"


def _normalized_tx_type(trade: Trade) -> str:
    return _normalize_tx_type(trade.form, trade.transaction_type)


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


def _format_form_counts(rows: list[tuple[Optional[str], int]]) -> str:
    counts: dict[str, int] = {}
    for form, count in rows:
        if count is None:
            continue
        prefix = form_prefix(form)
        if prefix:
            key = prefix
        elif form and isinstance(form, str) and form.strip():
            key = form.strip().upper()
        else:
            key = "UNKNOWN"
        counts[key] = counts.get(key, 0) + int(count)

    if not counts:
        return "unknown"

    parts: list[str] = []
    for prefix in FORM_PREFIX_ORDER:
        if prefix in counts:
            parts.append(f"{prefix}={counts.pop(prefix)}")
    for key in sorted(counts):
        parts.append(f"{key}={counts[key]}")
    return ", ".join(parts)


def _format_tx_counts(rows: list[tuple[Optional[str], Optional[str], int]]) -> str:
    counts = {"BUY": 0, "SELL": 0, "OTHER": 0}
    for form, tx_type, count in rows:
        if count is None:
            continue
        normalized = _normalize_tx_type(form, tx_type)
        if normalized in ("BUY", "SELL"):
            counts[normalized] += int(count)
        else:
            counts["OTHER"] += int(count)
    return f"BUY={counts['BUY']}, SELL={counts['SELL']}, OTHER={counts['OTHER']}"


def _amount_text(trade: Trade) -> str:
    low = trade.amount_usd_low
    high = trade.amount_usd_high
    if low is not None or high is not None:
        if low is not None and high is not None:
            if low == high:
                return f"{low}"
            return f"{low}-{high}"
        return f"{low if low is not None else high}"
    if trade.shares is not None and trade.price_usd is not None:
        try:
            value = float(trade.shares) * float(trade.price_usd)
        except (TypeError, ValueError):
            return "unknown"
        return f"{value:.2f}"
    return "unknown"


def _person_summary_prompt(
    db: Session, slug: str, *, max_trades: int
) -> tuple[str, Optional[str]]:
    person_name = db.scalar(
        select(func.max(Trade.person_name)).where(Trade.person_slug == slug)
    )
    total = int(
        db.scalar(
            select(func.count()).select_from(Trade).where(Trade.person_slug == slug)
        )
        or 0
    )
    first_trade = db.scalar(
        select(func.min(Trade.transaction_date)).where(Trade.person_slug == slug)
    )
    last_trade = db.scalar(
        select(func.max(Trade.transaction_date)).where(Trade.person_slug == slug)
    )
    if first_trade is None:
        first_trade = db.scalar(
            select(func.min(Trade.filed_at)).where(Trade.person_slug == slug)
        )
        if isinstance(first_trade, dt.datetime):
            first_trade = first_trade.date()
    if last_trade is None:
        last_trade = db.scalar(
            select(func.max(Trade.filed_at)).where(Trade.person_slug == slug)
        )
        if isinstance(last_trade, dt.datetime):
            last_trade = last_trade.date()

    form_rows = db.execute(
        select(Trade.form, func.count(Trade.id))
        .where(Trade.person_slug == slug)
        .group_by(Trade.form)
    ).all()
    tx_rows = db.execute(
        select(Trade.form, Trade.transaction_type, func.count(Trade.id))
        .where(Trade.person_slug == slug)
        .group_by(Trade.form, Trade.transaction_type)
    ).all()

    trades = db.scalars(
        select(Trade)
        .where(Trade.person_slug == slug)
        .order_by(
            Trade.filed_at.is_(None),
            Trade.filed_at.desc(),
            Trade.created_at.desc(),
        )
        .limit(max_trades)
    ).all()

    lines = [
        "Person trade summary (use only the data below):",
        f"- Today: {dt.date.today().isoformat()}",
        f"- Person name: {person_name or 'unknown'}",
        f"- Person slug: {slug}",
        f"- Total trades: {total}",
        f"- First trade date: {first_trade.isoformat() if first_trade else 'unknown'}",
        f"- Latest trade date: {last_trade.isoformat() if last_trade else 'unknown'}",
        f"- Form counts: {_format_form_counts(form_rows)}",
        f"- Transaction type counts (normalized): {_format_tx_counts(tx_rows)}",
        "Recent trades (most recent first):",
    ]
    if not trades:
        lines.append("- none")
    else:
        for idx, trade in enumerate(trades, start=1):
            trade_date = trade.transaction_date or (trade.filed_at.date() if trade.filed_at else None)
            trade_date_text = trade_date.isoformat() if trade_date else "unknown"
            lines.append(
                f"{idx}) {trade_date_text} | {trade.ticker or 'unknown'} | "
                f"{trade.form or 'unknown'} | {_normalize_tx_type(trade.form, trade.transaction_type)} | "
                f"Amount USD: {_amount_text(trade)}"
            )
    lines.append("")
    lines.append("Write 2 to 4 sentences. Do not speculate.")
    return "\n".join(lines), person_name


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


def _call_llm(
    settings: Settings,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> str:
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
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
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
    content = _call_llm(
        settings,
        system_prompt=SCORE_SYSTEM_PROMPT,
        user_prompt=prompt,
        max_tokens=700,
    )
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


def summarize_people_once() -> dict[str, int]:
    settings = get_settings()
    if not settings.llm_person_summary_enabled or not settings.llm_api_key:
        return {"summarized": 0, "failed": 0}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=settings.llm_person_summary_stale_hours)
    max_items = settings.llm_person_summary_max_per_run
    sleep_seconds = (
        settings.llm_person_summary_sleep_ms / 1000 if settings.llm_person_summary_sleep_ms else 0
    )

    summarized = 0
    failed = 0

    with SessionLocal() as db:
        slugs = db.execute(
            select(Trade.person_slug)
            .where(Trade.person_slug.is_not(None))
            .group_by(Trade.person_slug)
            .order_by(Trade.person_slug)
        ).scalars().all()

        for slug in slugs:
            summary = db.scalar(select(PersonSummary).where(PersonSummary.person_slug == slug))
            if summary and summary.summary_updated_at and summary.summary_updated_at >= cutoff:
                continue

            prompt, person_name = _person_summary_prompt(
                db,
                slug,
                max_trades=settings.llm_person_summary_max_trades,
            )
            try:
                content = _call_llm(
                    settings,
                    system_prompt=PERSON_SUMMARY_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    max_tokens=settings.llm_person_summary_max_tokens,
                )
            except Exception as exc:
                failed += 1
                logger.warning("LLM person summary failed for %s: %s", slug, exc)
                continue

            if summary is None:
                summary = PersonSummary(person_slug=slug)
            summary.person_name = person_name
            summary.summary = content.strip()
            summary.summary_model = settings.llm_model
            summary.summary_updated_at = dt.datetime.utcnow()
            db.add(summary)
            db.commit()
            summarized += 1

            if max_items > 0 and summarized >= max_items:
                break
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

    return {"summarized": summarized, "failed": failed}


def _llm_jobs_enabled(settings: Settings) -> bool:
    if not settings.llm_api_key:
        return False
    return settings.llm_score_enabled or settings.llm_person_summary_enabled


def _run_llm_jobs() -> None:
    settings = get_settings()
    if settings.llm_score_enabled:
        score_trades_once()
    if settings.llm_person_summary_enabled:
        summarize_people_once()


def _scoring_loop() -> None:
    settings = get_settings()
    logger.info(
        "LLM scheduler started (model=%s, score=%s, person_summary=%s, interval=%sm).",
        settings.llm_model,
        settings.llm_score_enabled,
        settings.llm_person_summary_enabled,
        settings.llm_schedule_interval_minutes,
    )

    _run_llm_jobs()

    while True:
        interval_seconds = max(60, int(settings.llm_schedule_interval_minutes) * 60)
        time.sleep(interval_seconds)
        _run_llm_jobs()


def start_llm_scoring() -> None:
    settings = get_settings()
    if not _llm_jobs_enabled(settings):
        return

    global _scoring_started
    if _scoring_started:
        return
    _scoring_started = True

    thread = threading.Thread(target=_scoring_loop, name="llm-scoring", daemon=True)
    thread.start()
