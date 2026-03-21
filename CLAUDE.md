# CLAUDE.md

PROJECT: AvailAI — Electronic component sourcing platform and CRM for Trio Supply Chain Solutions
VERSION: 3.1.0 (single source of truth: `app/config.py` → `APP_VERSION`)
STACK: FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind CSS
DEPLOY: Docker Compose (app, db, redis, caddy) on DigitalOcean
DEVELOPER LEVEL: Beginner — explain things simply, use examples from this project

## What This Is

AvailAI is an electronic component sourcing engine. It searches 6 supplier APIs in parallel (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14), tracks vendor intelligence via material cards, automates RFQ workflows via Microsoft Graph API, mines email inboxes for vendor offers using Claude AI, and includes full CRM for companies, quotes, buy plans, and proactive matching.

## CODE RULES

- Always write tests with any new code. Don't ask, just include them.
- Give exact file paths for everything.
- Never use placeholder comments like "# rest of code here" — give complete code.
- Keep responses under 150 lines. Break big tasks into steps.
- Simple beats clever. 20 readable lines > 10 clever lines.
- Use Loguru for logging, never print().
- Use Ruff for linting.
- Use Alembic for database migrations. Always include rollback steps.

## PROJECT STRUCTURE

```
app/
├── main.py                    # FastAPI app, router registration (34 routers), lifespan, middleware stack
├── config.py                  # Pydantic Settings (env vars, APP_VERSION, MVP_MODE)
├── database.py                # SQLAlchemy engine, SessionLocal, UTCDateTime type
├── dependencies.py            # Auth: require_user, require_admin, require_buyer, require_fresh_token, get_db
├── constants.py               # StrEnum status enums (19 enums — ALWAYS use these, never raw strings)
├── shared_constants.py        # JUNK_DOMAINS (68), JUNK_EMAIL_PREFIXES (17) — don't duplicate
├── startup.py                 # Runtime DB ops: triggers, seeds, backfills, ANALYZE (NO DDL)
├── scheduler.py               # APScheduler coordinator
├── scoring.py                 # Sighting/lead/vendor scoring functions
├── vendor_utils.py            # fuzzy_score_vendor() — don't inline rapidfuzz calls
├── search_service.py          # Requirement search coordinator (all 6 sources)
├── email_service.py           # Graph API: batch RFQ, inbox monitor, AI parse
├── enrichment_service.py      # Customer/vendor enrichment orchestrator
├── rate_limit.py              # Slowapi rate limiting
│
├── models/                    # SQLAlchemy models (47 models across domain modules)
├── schemas/                   # Pydantic request/response schemas (26 files)
├── routers/                   # API route handlers (34 routers, 200+ endpoints)
│   ├── auth.py                # /auth/* — OAuth2 login/callback/logout
│   ├── htmx_views.py          # /v2/* — Main HTMX frontend (all page/partial routes)
│   ├── ai.py                  # /api/ai/* — AI features
│   ├── requisitions/          # /api/requisitions/*, /v2/partials/* (core.py, requirements.py, attachments.py)
│   ├── crm/                   # /api/companies/*, sites, offers, quotes, buy_plans, enrichment, clone
│   ├── excess.py              # /api/excess-*, /v2/partials/excess/*
│   ├── materials.py           # /api/materials/*
│   ├── proactive.py           # /api/proactive/*
│   └── ...                    # 25+ more router files
│
├── services/                  # Business logic (120+ service files, decoupled from HTTP)
│   ├── search_worker_base/    # Search connector base + mpn_normalizer.py
│   ├── ics_worker/            # ICS search worker
│   ├── nc_worker/             # NC search worker
│   └── ...                    # AI, enrichment, proactive, tagging, scoring, etc.
│
├── connectors/                # External API integrations (DigiKey, Mouser, Nexar, etc.)
├── jobs/                      # APScheduler job definitions (14 job modules)
├── cache/                     # Redis caching: @cached_endpoint(prefix, ttl_hours, key_params)
├── utils/                     # Shared utilities (claude_client, graph_client, normalization, etc.)
│
├── templates/                 # Jinja2 templates (164 files)
│   ├── base.html              # App shell (topbar, mobile nav, toast, modal)
│   ├── htmx/base_page.html    # Lazy-loader: spinner → hx-get partial
│   ├── htmx/partials/         # 158 HTMX partials across 29 subdirectories
│   └── documents/             # PDF templates (quote_report, rfq_summary)
│
└── static/                    # Frontend assets
    ├── htmx_app.js            # Alpine.js + HTMX bootstrap, stores, components (19KB)
    ├── styles.css             # Tailwind + component styles (16KB)
    ├── htmx_mobile.css        # Mobile overrides (8KB)
    └── dist/                  # Vite build output (minified, content-hashed)
```

## Frontend

**HTMX 2.x + Alpine.js 3.x + Jinja2 partials + Tailwind CSS.** No React, no SPA framework.

- All navigation is HTMX-driven: links fire `hx-get`, server returns HTML partials, HTMX swaps into `#main-content`
- Alpine.js handles component state (stores: sidebar, toast, preferences, shortlist)
- 9 Alpine plugins loaded: focus, persist, intersect, collapse, morph, mask, sort, anchor, resize
- 14 HTMX extensions active: alpine-morph, preload, sse, loading-states, multi-swap, etc.
- Tailwind config: DM Sans font, brand color palette (50-900)
- Build: Vite → `app/static/dist/`

### Template Routing — CRITICAL

**ALWAYS trace route → view function → template_response() before editing any template.**

Key gotcha: Requisitions parts tab loads `parts/list.html`, NOT `requisitions/list.html`.

## Key Request Flows

**Search**: User submits part numbers → `search_service.search_requirement()` fires all connectors via `asyncio.gather()` → results deduped and scored by `scoring.py` (6 weighted factors) → material cards auto-upserted.

**RFQ**: `email_service.send_batch_rfq()` sends via Graph API, tagged with `[AVAIL-{id}]` → scheduler polls inbox every 30min → `response_parser.py` uses Claude to extract structured data → confidence >=0.8 auto-creates Offer, 0.5-0.8 flags for review.

**Proactive Matching**: Offers matched to customer purchase history → SQL scorecard (0-100) → batch prepare/send workflow → sent offers grouped by customer.

## Auth & Sessions

Azure AD OAuth2 via Microsoft Graph API. Session middleware stores `user_id` in cookie. Dependency levels:
- `require_user` — any logged-in user
- `require_buyer` — buyer role
- `require_admin` — admin role
- `require_fresh_token` — validates M365 token freshness (15-min buffer)

## Response Format Standards

**JSON errors**: `{"error": "message", "status_code": 400, "request_id": "abc123"}`
- Tests check `response.json()["error"]`, NOT `["detail"]`

**List responses**: `{"items": [...], "total": 100, "limit": 50, "offset": 0}`
- Companies list returns this format — NOT a plain array

**HTMX responses**: HTMLResponse from Jinja2 templates

**Schemas**: All in `app/schemas/responses.py`, use `extra="allow"` on Pydantic models

## Coding Conventions

- `db.get(Model, id)` — NOT `db.query(Model).get(id)` (SQLAlchemy 2.0)
- Status values: use StrEnum constants from `app/constants.py` — never raw strings
- Vendor matching: use `fuzzy_score_vendor()` from `app/vendor_utils.py`
- MPN dedup: use `strip_packaging_suffixes()` from `app/services/search_worker_base/mpn_normalizer.py`
- Shared junk lists: use `JUNK_DOMAINS`/`JUNK_EMAIL_PREFIXES` from `app/shared_constants.py`
- Caching: `@cached_endpoint(prefix, ttl_hours, key_params)` from `app/cache/decorators.py`
- Structured logging via Loguru with request_id context
- `TESTING=1` env var disables scheduler and real API calls in test mode
- Pyright LSP plugin is active — stage only intentionally changed files

## MVP Mode

`config.py: mvp_mode = True` — gates Dashboard, Enrichment, Teams, Task Manager.
Core MVP: Requisitions, Customers, Vendors, Sourcing Engine.

## Commands

### Run (Docker)
```bash
docker compose up -d                # Start all containers (app, db, caddy, redis)
docker compose up -d --build        # Rebuild and start
docker compose logs -f app          # Tail app logs
```

### Tests — Tiered Strategy (8,030 tests)

**During development: run only tests for changed files (fast feedback)**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_<changed_module>.py -v
```

**Before commit: full suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

**Coverage check: only before PR creation**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

**Rules:**
- When editing code, run ONLY the test file(s) related to the changed module
- Run full suite only before committing
- Run coverage only when explicitly asked or before PR
- Never run `--cov` during iterative development — it adds significant overhead
- Tests run in parallel via pytest-xdist (`-n auto` in pytest.ini)
- Target: 100% coverage — no commit should reduce it
- Tests use in-memory SQLite (no real DB or M365 tokens needed)
- `conftest.py` sets `RATE_LIMIT_ENABLED=false`
- Mock lazy imports at source module, not importing module
- Use `from tests.conftest import engine` for test SQLite engine

### Database Migrations
```bash
alembic upgrade head                              # Apply migrations (runs automatically at startup)
alembic revision --autogenerate -m "description"  # Generate new migration
```

80+ migration revisions. Schema defined in `app/models/`. Alembic manages all schema evolution.

## Database & Migration Rules

### ABSOLUTE RULES — NEVER VIOLATE
1. **ALL schema changes go through Alembic.** Never use raw DDL in startup.py, services, routers, or scripts.
2. **Never use Base.metadata.create_all() for schema changes.**
3. **Never run raw SQL against production** outside of a migration.
4. **Migration workflow — every time:**
   a. Make model change in `app/models/`
   b. Run: `alembic revision --autogenerate -m "description"`
   c. REVIEW the generated migration
   d. Test: upgrade → downgrade → upgrade
   e. Commit migration with model change

5. **startup.py is for runtime operations ONLY:**
   - FTS triggers (PostgreSQL-specific)
   - Seed data (system_config defaults)
   - ANALYZE on hot tables
   - Idempotent backfill queries
   - Count triggers (PG-specific)
   - NOTHING that creates, alters, or drops tables/columns/indexes/constraints

## Deploy

When I say "deploy", that means: commit + push + rebuild + verify logs. No questions asked.

```bash
cd /root/availai && git pull origin main && docker compose up -d --build && docker compose logs -f app
```

## Safety

- WARN before any destructive operation (DROP, DELETE, production changes). Include backup and rollback steps.
- Flag security issues, missing error handling, N+1 queries, missing indexes.

## File Rules

- Every new file needs a header comment explaining: what it does, what calls it, what it depends on.

## Session Rules

- End sessions with: what changed, git commands, what to test, any tech debt.

## Triggers

- "new feature" = make a plan first, don't just start coding
- "bug" or "error" = ask for the full error message before trying to fix
- "refactor" = check what's stable first
- "quick" or "just" = warn about hidden complexity

## Configuration

All config via `.env` (see `.env.example`). Key groups:
- Azure OAuth: `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`
- AI: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`
- Data sources: individual API keys per connector
- Feature flags: `EMAIL_MINING_ENABLED`, `ACTIVITY_TRACKING_ENABLED`, `CONTACTS_SYNC_ENABLED`
- DB: `DATABASE_URL=postgresql://availai:availai@db:5432/availai`
