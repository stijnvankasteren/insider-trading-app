from __future__ import annotations

import datetime as dt
import os
import sys

from sqlalchemy import select

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app.db import SessionLocal, init_db
from app.models import Trade


def main() -> None:
    init_db()
    now = dt.datetime.now(dt.timezone.utc)

    demo_trades = [
        Trade(
            source="insider",
            external_id="demo:insider:aapl:1",
            ticker="AAPL",
            company_name="Apple Inc.",
            person_name="Jane Doe",
            person_slug="jane-doe",
            transaction_type="BUY",
            transaction_date=(now.date() - dt.timedelta(days=1)),
            filed_at=now - dt.timedelta(hours=20),
            amount_usd_low=50000,
            amount_usd_high=100000,
            shares=250,
            price_usd=189.12,
            url="https://example.com",
            raw={"demo": True},
        ),
        Trade(
            source="congress",
            external_id="demo:congress:msft:1",
            ticker="MSFT",
            company_name="Microsoft Corporation",
            person_name="John Smith",
            person_slug="john-smith",
            transaction_type="SELL",
            transaction_date=(now.date() - dt.timedelta(days=3)),
            filed_at=now - dt.timedelta(hours=10),
            amount_usd_low=15000,
            amount_usd_high=50000,
            shares=50,
            price_usd=412.55,
            url="https://example.com",
            raw={"demo": True},
        ),
    ]

    with SessionLocal() as db:
        for trade in demo_trades:
            exists = db.scalar(
                select(Trade.id).where(Trade.external_id == trade.external_id)
            )
            if exists:
                continue
            db.add(trade)
        db.commit()

    print("Seeded demo trades.")


if __name__ == "__main__":
    main()
