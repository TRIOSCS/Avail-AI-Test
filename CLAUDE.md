# CLAUDE.md

PROJECT: AVAIL AI - Electronic component sourcing platform and CRM for Trio Supply Chain Solutions
STACK: Python, FastAPI, PostgreSQL, hosted on DigitalOcean
DEVELOPER LEVEL: Beginner - explain things simply, use examples from this project

## What This Is

AvailAI is an electronic component sourcing engine. It searches multiple supplier APIs in parallel, tracks vendor intelligence, automates RFQ workflows, mines email inboxes for vendor offers, and includes CRM features for managing companies, quotes, and buyer routing.

## CODE RULES

- Always write tests with any new code. Don't ask, just include them.
- Give exact file paths for everything.
- Never use placeholder comments like "# rest of code here" - give complete code.
- Keep responses under 150 lines. Break big tasks into steps.
- Simple beats clever. 20 readable lines > 10 clever lines.
- Use Loguru for logging, never print().
- Use Ruff for linting.
- Use Alembic for database migrations. Always include rollback steps.

## PROJECT STRUCTURE

- `app/routers/` = thin route handlers (HTTP endpoints grouped by domain)
- `app/services/` = business logic goes here (decoupled from HTTP)
- `app/models/` = SQLAlchemy models (42 models split into domain modules)
- `app/schemas/` = Pydantic schemas (request/response models)
- `app/connectors/` = external API integrations (Nexar, BrokerBin, DigiKey, Mouser, eBay, OEMSecrets, Sourcengine, email_mining)
- `app/config.py` = Settings class reading from env vars
- `app/dependencies.py` = auth middleware (require_user, require_buyer, require_fresh_token)
- `app/startup.py` = boot-time setup (FTS triggers, seed data, backfills — NO schema DDL)
- `app/scheduler.py` = background tasks (token refresh, inbox scan, contacts sync)
- `specs/` = business rules (if present)
- `tests/` = pytest tests

## FILE RULES

- Every new file needs a header comment explaining: what it does, what calls it, what it depends on.

## SAFETY

- WARN before any destructive operation (DROP, DELETE, production changes). Include backup and rollback steps.
- Flag security issues, missing error handling, N+1 queries, missing indexes.

## SESSION RULES

- End sessions with: what changed, git commands, what to test, any tech debt.
- After 15-20 messages, suggest starting fresh to avoid context problems.

## TRIGGERS

- "new feature" = make a plan first, don't just start coding
- "bug" or "error" = ask for the full error message before trying to fix
- "refactor" = check what's stable first
- "quick" or "just" = warn about hidden complexity

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

Schema is defined in `app/models/` (split into domain modules). Alembic manages all schema evolution. The entrypoint runs `alembic upgrade head` before app start. Production DB is at migration `048`.

## Database & Migration Rules

### ABSOLUTE RULES — NEVER VIOLATE
1. **ALL schema changes go through Alembic.** Never use raw DDL (ALTER TABLE, CREATE INDEX, ADD COLUMN, ADD CONSTRAINT, DROP anything) in:
   - startup.py
   - any service file
   - any router file
   - any ad-hoc script
   The ONLY place DDL belongs is inside `alembic/versions/` migration files.

2. **Never use Base.metadata.create_all() for schema changes.** It exists only as a legacy safety net with logging. If a table is missing, write a migration — don't rely on create_all().

3. **Never run raw SQL against production** outside of a migration. No `docker compose exec db psql` schema changes. No scripts that ALTER tables.

4. **Migration workflow — every time:**
   a. Make your model change in `app/models/`
   b. Run: `alembic revision --autogenerate -m "description"` (inside Docker)
   c. REVIEW the generated migration (autogenerate is not perfect)
   d. Test: `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head`
   e. Commit the migration file with the model change
   f. Deploy: entrypoint runs `alembic upgrade head` automatically

5. **If `alembic revision --autogenerate` generates a non-empty migration when you haven't changed models, there is schema drift.** Stop and investigate before proceeding.

6. **startup.py is for runtime operations ONLY:**
   - FTS triggers (PostgreSQL-specific, can't be expressed in Alembic cleanly)
   - Seed data (system_config defaults, initial admin user)
   - ANALYZE on hot tables
   - Backfill queries that run on NULL values (idempotent)
   - Count triggers (PG-specific, recreated idempotently)
   - NOTHING that creates, alters, or drops tables/columns/indexes/constraints

### Deploy / Update
```bash
bash scripts/deploy.sh    # Full DigitalOcean setup (first time)
bash scripts/update.sh    # Pull, rebuild, migrate
```

## Architecture

**Stack**: FastAPI (async) + SQLAlchemy 2.0 + PostgreSQL 16 + Jinja2 templates + vanilla JS frontend. Deployed via Docker Compose (app, db, caddy) on DigitalOcean.

### Key Request Flows

**Search**: User submits part numbers -> `search_service.search_requirement()` fires all connectors via `asyncio.gather()` -> results deduped and scored by `scoring.py` (6 weighted factors: recency, quantity, vendor reliability, data completeness, source credibility, price) -> material cards auto-upserted.

**RFQ**: `email_service.send_batch_rfq()` sends via Graph API, tagged with `[AVAIL-{id}]` -> scheduler polls inbox every 30min -> `response_parser.py` uses Claude to extract structured data -> confidence >=0.8 auto-creates Offer, 0.5-0.8 flags for review.

**Email Mining**: Scheduler scans Outlook for vendor offers/stock lists -> `attachment_parser` detects Excel columns (cached by vendor domain + fingerprint) -> creates Sighting and VendorCard records.

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
- DB: `DATABASE_URL=postgresql://availai:availai@db:5432/availai`

## Conventions

- Database models use `created_at` with UTC timezone. The `UTCTimestamp` type decorator in `database.py` enforces this.
- Vendor names are normalized to lowercase for deduplication (`normalized_name` field on `VendorCard`).
- All connector search methods return a list of dicts with keys: `vendor_name`, `mpn`, `qty`, `price`, `source_type`, etc.
- API versioning middleware rewrites `/api/v1/...` to `/api/...` internally; `X-API-Version: v1` header on all responses.
- Structured logging via loguru with request_id context.
- `TESTING=1` env var disables scheduler and real API calls in test mode.
