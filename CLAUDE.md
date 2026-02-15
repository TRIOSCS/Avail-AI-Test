# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

AvailAI is an electronic component sourcing engine. It searches multiple supplier APIs in parallel, tracks vendor intelligence, automates RFQ workflows, mines email inboxes for vendor offers, and includes CRM features for managing companies, quotes, and buyer routing.

## Commands

### Run (Docker)
```bash
docker compose up -d                # Start all containers (app, db, caddy)
docker compose up -d --build        # Rebuild and start
docker compose logs -f app          # Tail app logs
```

### Run Locally (no Docker)
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Tests
```bash
pytest tests/                           # All tests
pytest tests/test_routers_rfq.py        # Single file
pytest tests/test_routers_rfq.py -k "test_send" # Single test by name
pytest -v                               # Verbose output
```

Tests use in-memory SQLite with auth overrides (no real DB or M365 tokens needed). PostgreSQL-only features (ARRAY columns, e.g. `buyer_profiles` table) are excluded from the SQLite test DB.

### Database Migrations
```bash
alembic upgrade head                              # Apply migrations
alembic revision --autogenerate -m "description"  # Generate new migration
```

Note: `app/startup.py` also runs idempotent DDL (`IF NOT EXISTS`) at boot — Alembic is for schema evolution during development.

### Deploy / Update
```bash
bash scripts/deploy.sh    # Full DigitalOcean setup (first time)
bash scripts/update.sh    # Pull, rebuild, migrate
```

## Architecture

**Stack**: FastAPI (async) + SQLAlchemy 2.0 + PostgreSQL 16 + Jinja2 templates + vanilla JS frontend. Deployed via Docker Compose (app, db, caddy) on DigitalOcean.

### Layer Structure

- **`app/routers/`** — HTTP endpoints grouped by domain (auth, requisitions, vendors, rfq, crm, sources, ai, v13_features). Each router uses FastAPI dependency injection for DB sessions and auth.
- **`app/services/`** — Business logic decoupled from HTTP (response_parser, ai_service, attachment_parser, engagement_scorer, routing_service, ownership_service, activity_service, webhook_service, buyer_service).
- **`app/connectors/`** — External API integrations (Nexar/Octopart, BrokerBin, DigiKey, Mouser, eBay, OEMSecrets, Sourcengine, email_mining). All extend `BaseConnector` ABC with automatic retry and unified error handling. Connector failures never break search — results from working connectors are returned.
- **`app/schemas/`** — Pydantic request/response models.
- **`app/models.py`** — All 37 SQLAlchemy ORM models in one file.
- **`app/config.py`** — Settings class reading from env vars.
- **`app/dependencies.py`** — Auth middleware: `require_user`, `require_buyer`, `require_fresh_token`. Role-based access (buyer vs sales vs admin).
- **`app/startup.py`** — Idempotent DDL migrations run at app boot.
- **`app/scheduler.py`** — Background tasks (token refresh, inbox scan, contacts sync, ownership sweep) via `asyncio.create_task`.

### Key Request Flows

**Search**: User submits part numbers → `search_service.search_requirement()` fires all connectors via `asyncio.gather()` → results deduped and scored by `scoring.py` (6 weighted factors: recency, quantity, vendor reliability, data completeness, source credibility, price) → material cards auto-upserted.

**RFQ**: `email_service.send_batch_rfq()` sends via Graph API, tagged with `[AVAIL-{id}]` → scheduler polls inbox every 30min → `response_parser.py` uses Claude to extract structured data → confidence ≥0.8 auto-creates Offer, 0.5–0.8 flags for review.

**Email Mining**: Scheduler scans Outlook for vendor offers/stock lists → `attachment_parser` detects Excel columns (cached by vendor domain + fingerprint) → creates Sighting and VendorCard records.

### Auth & Sessions

Azure AD OAuth2 via Microsoft Graph API. Session middleware stores `user_id` in cookie. Three dependency levels: `require_user` (any logged-in), `require_buyer` (buyer role), admin check via `ADMIN_EMAILS` env var. Tokens auto-refreshed by scheduler.

### Frontend

Two vanilla JS files serve the entire UI: `app/static/app.js` (search, requisitions, vendors, upload) and `app/static/crm.js` (companies, quotes, activity). Single Jinja2 template at `app/templates/index.html`.

## Configuration

All config via `.env` (see `.env.example`). Key groups:
- Azure OAuth: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`
- AI: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`
- Data sources: individual API keys for each connector
- Feature flags: `EMAIL_MINING_ENABLED`, `ACTIVITY_TRACKING_ENABLED`, `CONTACTS_SYNC_ENABLED`
- Scoring weights: `WEIGHT_RECENCY`, `WEIGHT_QUANTITY`, etc. (defaults in config.py)
- DB: `DATABASE_URL=postgresql://availai:availai@db:5432/availai`

## Conventions

- Database models use `created_at` with UTC timezone. The `UTCTimestamp` type decorator in `database.py` enforces this.
- Vendor names are normalized to lowercase for deduplication (`normalized_name` field on `VendorCard`).
- All connector search methods return a list of dicts with keys: `vendor_name`, `mpn`, `qty`, `price`, `source_type`, etc.
- API versioning middleware rewrites `/api/v1/...` to `/api/...` internally; `X-API-Version: v1` header on all responses.
- Structured logging via loguru with request_id context.
- `TESTING=1` env var disables scheduler and real API calls in test mode.
