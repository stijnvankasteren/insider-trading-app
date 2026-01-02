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

Then open `http://localhost:8000` and set a strong `INGEST_SECRET` in `compose.yaml`.

### Portainer (stack + Postgres in dezelfde stack)

1) Build de app image op de server (1x):

```bash
docker build -t alldata-web:latest .
```

2) Portainer → **Stacks** → **Add stack** → plak `portainer-stack.yaml` in de editor.

3) Zet stack variables (minimaal):
- `POSTGRES_PASSWORD` (sterk)
- `INGEST_SECRET` (sterk)
- `AUTH_DISABLED=false` + `APP_PASSWORD` + `SESSION_SECRET` (voor login)
- `PUBLIC_BASE_URL` (bijv. `https://jouwdomein.nl`)

4) Deploy. Open `http://SERVER_IP:8000` (of via je reverse proxy).

Tip: je hoeft Postgres niet te exposen naar buiten; laat `alldata-db` zonder `ports` (in `portainer-stack.yaml` zit dat al zo).

### GitHub + Portainer (aanrader)

Doel: GitHub bouwt en publiceert automatisch een Docker image naar GHCR, en Portainer draait de stack met Postgres.

1) Push deze repo naar GitHub (branch `main`).
2) Zet GHCR packages aan voor de repo. De workflow staat klaar: `.github/workflows/publish-image.yml`.
   - Elke push naar `main` pusht `ghcr.io/<owner>/<repo>:latest` + een `:sha-...` tag.
3) Portainer → **Registries** → **Add registry** → GitHub Container Registry (`ghcr.io`).
   - Als je image privé is: maak een GitHub PAT met `read:packages` en gebruik die hier.
4) Portainer → **Stacks** → **Add stack** → **Git repository**:
   - Repository URL: je GitHub repo
   - Compose path: `portainer-stack.yaml`
   - Environment variables:
     - `APP_IMAGE=ghcr.io/<owner>/<repo>`
     - `APP_IMAGE_TAG=latest`
     - `POSTGRES_PASSWORD=...`
     - `INGEST_SECRET=...`
     - (aanrader) `AUTH_DISABLED=false`, `APP_PASSWORD=...`, `SESSION_SECRET=...`, `COOKIE_SECURE=true`, `PUBLIC_BASE_URL=https://...`
5) Deploy. Na nieuwe push: redeploy de stack (of zet auto-update aan als je Portainer dat ondersteunt).
