# AltData (QuiverQuant-like MVP)

FastAPI + SQLAlchemy webapp that can ingest alternative market data from n8n into a database and render it in a QuiverQuant-like UI (original branding/content).

## Local dev

Requirements: Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data
python3 scripts/seed_demo.py
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open:
- `http://localhost:8000` (landing)
- `http://localhost:8000/app` (dashboard)

### Enable login (recommended)

Set in `.env`:
- `AUTH_DISABLED=false`
- `SESSION_SECRET=...` (long random string)

Then create an account at `http://localhost:8000/signup` and log in at `http://localhost:8000/login`.

Optional (admin login via shared password):
- `APP_PASSWORD=...`
Admin can log in by leaving the email empty on `/login`.

## Ingest from n8n

The app exposes `POST /api/ingest/trades` protected by `INGEST_SECRET` (send it as the `x-ingest-secret` header).

Example payload (single object or an array of objects):

```json
{
  "source": "insider",
  "external_id": "sec:123456",
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "person_name": "Jane Doe",
  "transaction_type": "BUY",
  "transaction_date": "2025-12-29",
  "filed_at": "2025-12-29T12:34:56+00:00",
  "shares": 250,
  "price_usd": 189.12,
  "url": "https://example.com/detail"
}
```

### Payload fields

Headers:
- `content-type: application/json`
- `x-ingest-secret: <INGEST_SECRET>`

Body:
- Single object or array of objects.
- Required: `source` (`insider`, `congress`, etc)
- Recommended: `external_id` (or `externalId`) for idempotency/upserts.
- Optional fields (with common aliases):
  - `ticker` (or `symbol`), `company_name` (or `companyName`), `person_name` (or `personName`)
  - `transaction_type` (or `type`), `form` (or `issuerForm` / `reportingForm`)
  - `transaction_date` (or `transactionDate`, `YYYY-MM-DD`), `filed_at` (or `filedAt`, ISO datetime)
  - Amount (USD):
    - Insider trades (`source=insider`): calculated as `shares * price_usd` (overrides any amount fields)
    - Range (e.g. congress trades): `amount_usd_low` (or `amountUsdLow`) + `amount_usd_high` (or `amountUsdHigh`)
  - `shares`, `price_usd` (or `priceUsd`)
  - `url`
- Any extra fields will be stored in `raw`.

In n8n, use an **HTTP Request** node:
- Method: `POST`
- URL: `https://YOUR_DOMAIN/api/ingest/trades`
- Header: `x-ingest-secret: <INGEST_SECRET>`
- Body: JSON

Quick test (local):

```bash
curl -sS -X POST "http://localhost:8000/api/ingest/trades" \
  -H "content-type: application/json" \
  -H "x-ingest-secret: $(grep '^INGEST_SECRET=' .env | cut -d= -f2- | tr -d '\"')" \
  -d '{"source":"insider","external_id":"demo:curl:1","ticker":"TSLA","person_name":"Test Person","transaction_type":"BUY","transaction_date":"2025-12-29","shares":10,"price_usd":250}'
```

### Delete trades

The app also exposes `DELETE /api/ingest/trades` protected by `INGEST_SECRET`.

Safety: add `?confirm=true` or the request will be rejected.

```bash
curl -sS -X DELETE "http://localhost:8000/api/ingest/trades?confirm=true" \
  -H "x-ingest-secret: $(grep '^INGEST_SECRET=' .env | cut -d= -f2- | tr -d '\"')"

# Only delete a single source ("insider" / "congress" / etc)
curl -sS -X DELETE "http://localhost:8000/api/ingest/trades?source=insider&confirm=true" \
  -H "x-ingest-secret: $(grep '^INGEST_SECRET=' .env | cut -d= -f2- | tr -d '\"')"
```

The UI pages `/app/insiders`, `/app/congress`, `/app/search`, `/app/watchlist` will read from the database.

## Deploy (Linux server)

Recommended:
- Run Postgres (managed or Docker).
- Run this app behind Nginx/Caddy with HTTPS.
- Configure `DATABASE_URL`, `INGEST_SECRET`, and (recommended) auth vars (`AUTH_DISABLED=false`, `APP_PASSWORD`, `SESSION_SECRET`).

### Docker (app + Postgres)

```bash
cp .env.example .env
docker compose up --build
```

Then open `http://localhost:8000` and set a strong `INGEST_SECRET` (via `.env` or Portainer stack variables).

Note: Postgres gebruikt `POSTGRES_USER`/`POSTGRES_PASSWORD` alleen bij de allereerste init van de data directory. Als je deze waarden aanpast nadat de `db_data` volume al bestaat, krijg je vaak `password authentication failed`. Fix: `docker compose down -v` (wist DB data) of wijzig het wachtwoord in Postgres en redeploy.

Wachtwoord wijzigen in Postgres (zonder data te wissen):
- Docker Compose (lokaal): `docker compose exec db psql -U postgres -d postgres -c "ALTER USER postgres WITH PASSWORD 'nieuw_wachtwoord';"`
- Portainer: **Containers** → selecteer je Postgres container → **Console/Exec** → run `psql -U postgres -d postgres -c "ALTER USER postgres WITH PASSWORD 'nieuw_wachtwoord';"`, update daarna de stack env `POSTGRES_PASSWORD` en redeploy/restart.
  - Gebruik je een andere DB user (bijv. `alldata` in `portainer-stack.yaml`), vervang dan `postgres` in het `ALTER USER ...` commando.
  - DB-user bepalen:
    - In Compose/stack: kijk naar `POSTGRES_USER` bij de Postgres service.
    - In de app: kijk naar de user in `DATABASE_URL` (het stuk vóór `:`). In `portainer-stack.yaml` is dat standaard `alldata`.
    - In een draaiende container: `printenv POSTGRES_USER` (Docker Compose: `docker compose exec db printenv POSTGRES_USER`; Portainer: container → **Console/Exec** → `printenv POSTGRES_USER`).
  - App (website) updaten naar het nieuwe wachtwoord:
    - Docker Compose: zet `POSTGRES_PASSWORD=nieuw_wachtwoord` in `.env` en doe `docker compose up -d --force-recreate web` (of `docker compose up -d --build`).
    - Portainer: Stack → **Environment variables** → update `POSTGRES_PASSWORD` → redeploy/restart.

### GitHub + Portainer (aanrader)

Doel: Portainer deployt vanuit deze GitHub repo (Portainer pulled de repo en build de app image op de server) en draait de stack met Postgres.

1) Push deze repo naar GitHub (branch `main`).
2) Portainer → **Stacks** → **Add stack** → **Git repository**:
   - Repository URL: `https://github.com/stijnvankasteren/insider-trading-app`
   - Reference: `main`
   - Compose path: `portainer-stack.yaml`
   - Environment variables:
     - `POSTGRES_PASSWORD=...`
     - `INGEST_SECRET=...`
     - (aanrader) `AUTH_DISABLED=false`, `APP_PASSWORD=...`, `SESSION_SECRET=...`, `COOKIE_SECURE=true`, `PUBLIC_BASE_URL=https://...`
     - Tip: deze variabelen worden door Portainer per stack opgeslagen. Je hoeft ze maar **1x** in te vullen; bij **Update the stack** → **Pull latest changes** + redeploy blijven ze staan (tenzij je ze zelf verwijdert/overschrijft). Voor secrets: zet **Hide value** aan.
       - Let op: als een env var op **Hide value** staat, toont Portainer de waarde later niet meer in de UI, maar hij wordt wel gebruikt.
3) Deploy (zet **Pull images** uit; `alldata-web` wordt built, niet gepulled).
4) Update na een push: Portainer stack → **Update the stack** → **Pull latest changes** + redeploy (of webhook/auto-update).
