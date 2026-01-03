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
  "amount_usd_low": 50000,
  "amount_usd_high": 100000,
  "shares": 250,
  "price_usd": 189.12,
  "url": "https://example.com/detail"
}
```

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
  -d '{"source":"insider","external_id":"demo:curl:1","ticker":"TSLA","person_name":"Test Person","transaction_type":"BUY","transaction_date":"2025-12-29","amount_usd_low":1000,"amount_usd_high":5000}'
```

The UI pages `/app/insiders`, `/app/congress`, `/app/search`, `/app/watchlist` will read from the database.

## Deploy (Linux server)

Recommended:
- Run Postgres (managed or Docker).
- Run this app behind Nginx/Caddy with HTTPS.
- Configure `DATABASE_URL`, `INGEST_SECRET`, and (recommended) auth vars (`AUTH_DISABLED=false`, `APP_PASSWORD`, `SESSION_SECRET`).

### Docker (app + Postgres)

```bash
docker compose up --build
```

Then open `http://localhost:8000` and set a strong `INGEST_SECRET` (via `.env` or Portainer stack variables).

### Portainer (stack + Postgres in dezelfde stack)

1) Build de app image op de server (1x):

```bash
docker build -t alldata-web:latest .
```

2) Portainer → **Stacks** → **Add stack** → plak `portainer-stack-image.yaml` in de editor.

3) Zet stack variables (minimaal):
- `POSTGRES_PASSWORD` (sterk)
- `INGEST_SECRET` (sterk)
- `AUTH_DISABLED=false` + `APP_PASSWORD` + `SESSION_SECRET` (voor login)
- `PUBLIC_BASE_URL` (bijv. `https://jouwdomein.nl`)

4) Deploy (zet **Pull images** uit als je de lokale image `alldata-web:latest` gebruikt; anders probeert Portainer te pullen van een registry).

Open `http://SERVER_IP:8000` (of via je reverse proxy).

Tip: je hoeft Postgres niet te exposen naar buiten; laat `alldata-db` zonder `ports` (in `portainer-stack-image.yaml` zit dat al zo).

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
3) Deploy (zet **Pull images** uit; `alldata-web` wordt built, niet gepulled).
4) Update na een push: Portainer stack → **Update the stack** → **Pull latest changes** + redeploy (of webhook/auto-update).

Optioneel (prebuilt image i.p.v. build op de server): gebruik GHCR via `.github/workflows/publish-image.yml` en pull de image `ghcr.io/stijnvankasteren/insider-trading-app:latest` met registry credentials in Portainer.
