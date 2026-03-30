# CLAUDE.md — AvailAI Documentation

**PROJECT:** AvailAI — Electronic component sourcing platform and CRM for Trio Supply Chain Solutions
**VERSION:** See `app/config.py` → `APP_VERSION`
**STACK:** FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind CSS
**DEPLOY:** Docker Compose (app, db, redis, caddy, enrichment-worker [disabled], db-backup) on DigitalOcean
**LEVEL:** Intermediate

---

## What This Is

AvailAI is an **electronic component sourcing engine** that automates vendor discovery and RFQ workflows. It:
- **Searches** 10 supplier APIs in parallel (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14, Sourcengine, eBay, AI web search, email mining)
- **Tracks** vendor intelligence via material cards and proactive matching
- **Automates** RFQ workflows via Microsoft Graph API
- **Mines** email inboxes for vendor offers using Claude AI
- **Manages** full CRM for companies, quotes, buy plans, and customer matching

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Runtime** | Python 3.11+ | Backend runtime |
| **Framework** | FastAPI 0.100+ | API and HTMX route server |
| **Database** | PostgreSQL 16 | Primary data store (see `alembic/versions/`) |
| **Cache** | Redis (optional) | Endpoint caching, scheduler coordination |
| **ORM** | SQLAlchemy 2.0 | Type-safe database access (see `app/models/`) |
| **Frontend** | HTMX 2.x + Alpine.js 3.x | Progressive enhancement, no SPA |
| **Templates** | Jinja2 | Server-side rendering (see `app/templates/`) |
| **Styling** | Tailwind CSS + custom | Responsive, dark-mode ready |
| **Build** | Vite 6.x | Frontend asset bundling |
| **Auth** | Azure AD OAuth2 | Microsoft Graph API integration |
| **AI** | Anthropic Claude API | Email parsing, query enrichment |
| **Scheduling** | APScheduler | Background jobs (see `app/jobs/`) |
| **Linting** | Ruff + mypy + pre-commit | Code quality enforcement |
| **Testing** | pytest + Playwright | Comprehensive test suite, E2E coverage |

---

## Quick Start

### Prerequisites
- **Docker & Docker Compose** (v2.20+)
- **Git**
- **Python 3.11+** (for local development)
- **Node.js / Bun** (for frontend work)

### Local Development (Docker)

```bash
# Clone and navigate
git clone https://github.com/YOUR_REPO/availai.git
cd availai

# Copy and configure environment
cp .env.example .env
# Edit .env with your API keys and database URL

# Start all services
docker compose up -d

# Initialize database (runs automatically at startup)
docker compose exec app alembic upgrade head

# View logs
docker compose logs -f app

# Open in browser
# https://app.yourdomain.com (or http://localhost:8000 for local)
```

### Frontend Development (Hot Reload)

```bash
# Terminal 1: Start Vite dev server (watches src, rebuilds on change)
npm run dev

# Terminal 2: Run the FastAPI server (in Docker or locally)
docker compose up app

# Browser: http://localhost:5173 (Vite dev proxy)
```

---

## CODE RULES

- Always write tests with any new code. Don't ask, just include them.
- Give exact file paths for everything.
- Never use placeholder comments like "# rest of code here" — give complete code.
- Keep responses under 150 lines. Break big tasks into steps.
- Simple beats clever. 20 readable lines > 10 clever lines.
- Use Loguru for logging, never print().
- Use Ruff for linting.
- Use Alembic for database migrations. Always include rollback steps.

## Standing Workflow Rules

### Execution Model
- Always use subagent-driven execution for multi-step tasks — never ask, never offer inline
- Maximize parallel subagents for all independent work — never serialize what can parallelize
- Run the full skill pipeline on every task: brainstorm → plan → TDD → execute → simplify → review → verify (this order is canonical)
- Never skip a step because it seems "overkill" — use every available tool and skill aggressively
- Fix ALL review findings immediately — never defer as "lower priority" or "MVP acceptable"

### UI Guardrails
- Never add, remove, or rearrange UI elements without explicit user approval
- Follow existing codebase patterns — find a working example before creating new UI conventions

### Code Anti-Patterns (never introduce — in addition to Coding Conventions section)
- `innerHTML` → use `htmx.ajax()` or Alpine reactive binding
- Pydantic `class Config` → use `model_config = ConfigDict()`
- Alpine `_x_dataStack` → use `Alpine.store()`
- `db.query(Model).get(id)` → use `db.get(Model, id)`

### Linear Development
- Memory references specific code (line numbers, function names)? Verify against current files before acting
- Plans or specs with line numbers? Verify those lines are still correct before editing
- Never mix old patterns with new — if the codebase has moved to a new pattern, follow the new one
- Always read the actual codebase before making changes — never rely on cached assumptions

### PR Reviews
- Run ALL pr-review-toolkit agents on every PR: comment-analyzer, pr-test-analyzer, type-design-analyzer, silent-failure-hunter, code-simplifier, code-reviewer
- Also run feature-dev:code-reviewer

## Project Structure

```
app/
├── main.py                    # FastAPI app, router registration, middleware stack, lifespan
├── config.py                  # Pydantic Settings (env vars, APP_VERSION, MVP_MODE)
├── database.py                # SQLAlchemy engine, SessionLocal, UTCDateTime type
├── dependencies.py            # Auth middleware: require_user, require_admin, require_buyer, require_fresh_token
├── constants.py               # StrEnum status enums — ALWAYS use, never raw strings
├── shared_constants.py        # JUNK_DOMAINS, JUNK_EMAIL_PREFIXES
├── startup.py                 # Runtime operations: triggers, seeds, ANALYZE (NO DDL)
├── scheduler.py               # APScheduler coordinator (see app/jobs/)
├── scoring.py                 # Sighting/lead/vendor scoring (6-factor weighted algorithm)
├── vendor_utils.py            # fuzzy_score_vendor() — vendor matching utility
├── search_service.py          # Requirement search orchestrator (all 10 sources)
├── email_service.py           # Graph API: batch RFQ send, inbox monitor, AI parse replies
├── enrichment_service.py      # Customer/vendor enrichment orchestrator
├── rate_limit.py              # Slowapi rate limiting configuration
│
├── models/                    # SQLAlchemy ORM models
├── schemas/                   # Pydantic request/response schemas
├── routers/                   # API route handlers (see main.py for registration)
│   ├── auth.py                # /auth/* — Azure AD OAuth2 flow
│   ├── htmx_views.py          # /v2/* — Main HTMX frontend (page + partial routes)
│   ├── ai.py                  # /api/ai/* — AI features (parsing, enrichment)
│   ├── requisitions/          # Requisition core workflow
│   ├── crm/                   # Company, vendor, quote, buy plan management
│   ├── excess.py              # Excess inventory management
│   ├── materials.py           # Material card storage and retrieval
│   ├── proactive.py           # Vendor offer matching to purchase history
│   └── ...                    # Additional routers (vendors, contacts, activity, etc.)
│
├── services/                  # Business logic (decoupled from HTTP)
│   ├── search_worker_base/    # Search connector base + MPN normalizer
│   ├── ics_worker/            # ICS (In-stock capability) search worker
│   ├── nc_worker/             # NC (Normally closable) search worker
│   ├── response_parser.py      # Claude AI email reply parser
│   └── ...                    # AI, enrichment, proactive, tagging, scoring
│
├── connectors/                # External API integrations (DigiKey, Mouser, Nexar, etc.)
├── jobs/                      # APScheduler job definitions
│   ├── inbox_monitor.py       # Check for RFQ replies every 30min
│   ├── requirement_refresh.py # Re-search stale requirements
│   └── ...
│
├── cache/                     # Redis caching utilities
│   └── decorators.py          # @cached_endpoint(prefix, ttl_hours, key_params)
│
├── utils/                     # Shared utilities
│   ├── claude_client.py       # Anthropic API client wrapper
│   ├── graph_client.py        # Microsoft Graph API client
│   ├── normalization.py       # MPN/part number normalization
│   └── ...
│
├── templates/                 # Jinja2 templates
│   ├── base.html              # App shell (topbar, mobile nav, modal, toast)
│   ├── htmx/base_page.html    # Lazy-loader: spinner → hx-get partial
│   ├── htmx/partials/         # HTMX partials
│   └── documents/             # PDF templates (quote_report, rfq_summary)
│
├── static/                    # Frontend assets
│   ├── htmx_app.js            # Alpine.js + HTMX bootstrap, stores, components
│   ├── styles.css             # Tailwind + component styles
│   ├── htmx_mobile.css        # Mobile-specific overrides
│   └── dist/                  # Vite build output (minified, content-hashed)
│
├── migrations/                # Alembic migration files
└── logs/                      # Loguru output (structured, request_id context)

tests/
├── test_models.py             # ORM model tests
├── test_routers.py            # HTTP endpoint tests
├── test_services.py           # Business logic tests
├── conftest.py                # pytest fixtures, in-memory SQLite engine
└── e2e/                       # End-to-end Playwright tests
```

---

## Architecture Overview

### Request Flow

**HTMX Page Request:**
```
Browser → HTMX Link (hx-get) → FastAPI Router → HTTP Response (HTML partial)
→ HTMX swaps into #main-content → Alpine.js updates state
```

**API Request:**
```
Client → FastAPI endpoint → Service layer → Database/External API
→ JSON response
```

**Background Job:**
```
APScheduler fires at interval → Job function → Service layer → Database/External API
```

### Key Workflows

**Search Pipeline** (`search_service.py`):
1. User submits part numbers with target quantity
2. `search_requirement()` fires all 10 connectors via `asyncio.gather()`
3. Results deduplicated (by MPN + vendor)
4. Scored by 6 weighted factors (recency, qty match, vendor reliability, data completeness, source credibility, price)
5. Material cards auto-upserted, sightings created

**RFQ Workflow** (`email_service.py`):
1. User selects vendors → Click "Send RFQ"
2. `send_batch_rfq()` sends via Microsoft Graph API, tagged with `[AVAIL-{id}]`
3. APScheduler polls inbox every 30 minutes (`inbox_monitor.py`)
4. New replies forwarded to Claude via `response_parser.py`
5. Claude extracts: price, quantity, lead time, condition, date code
6. Confidence ≥0.8 → auto-create Offer, 0.5-0.8 → flag for manual review
7. Vendor reliability scores update based on reply speed and accuracy

**Proactive Matching** (`proactive_service.py`):
1. New vendor offers compared to customer purchase history
2. SQL scorecard (0-100): part match, quantity fit, price vs. historical, vendor reliability
3. Batch prepare/send workflow
4. Grouped by customer, ready for RFQ approval

### Data Model Layers

```
Requisitions (search + RFQ workflow)
  ├── Requirements (parts to find)
  ├── Sightings (vendor quotes, auto-created from search)
  └── Responses (RFQ replies)

CRM (vendor intelligence + customer relationships)
  ├── Companies (customers + vendors)
  ├── Contacts
  ├── Offers (vendor proposals)
  ├── Quotes (customer orders)
  └── BuyPlans (fulfillment tracking)

Materials (inventory + search cache)
  ├── MaterialCards (deduplicated parts)
  ├── Vendors (supplier info + scores)
  └── SourceStocks (external supplier stock levels)
```

---

## Frontend

### Technology Stack
- **HTMX 2.x** — HTTP-driven frontend, no SPA
- **Alpine.js 3.x** — Component state, reactivity
- **Jinja2** — Server-side HTML rendering
- **Tailwind CSS** — Utility-first styling
- **Vite 6.x** — Build tool and dev server

### Core Concepts

**Navigation is HTMX-driven:**
- Link (`<a>`) fires `hx-get="/partial/path"`
- Server returns HTML fragment
- HTMX swaps into `#main-content`
- **No page reloads, no JSON, no client-side routing**

**State is Alpine.js:**
```javascript
// stores/sidebar.js - persisted via @persist plugin
export default {
  open: false,
  toggle() { this.open = !this.open }
}

// In template:
<button @click="$store.sidebar.toggle()">Menu</button>
<div x-show="$store.sidebar.open">...</div>
```

**Styling uses Tailwind + custom CSS:**
- DM Sans font family
- Brand color palette (50-900 shades)
- Dark mode via `dark:` prefix
- Component classes in `styles.css`

**Toast System:**
- `$store.toast` has properties: `message`, `type`, `show` (boolean)
- Set properties directly: `$store.toast.message='msg'; $store.toast.type='success'; $store.toast.show=true`
- Do NOT call `$store.toast.show()` as a function — it is a boolean, not a method
- `_oob_toast()` in `sightings.py` returns OOB HTML that sets these properties

### Plugins & Extensions

**Alpine.js Plugins:** See `htmx_app.js` for current plugin list
**HTMX Extensions:** See `htmx_app.js` and `base.html` for current extension list

### Build & Deployment

```bash
# Development (watch mode)
npm run dev              # Vite dev server on localhost:5173

# Production
npm run build            # Minify → app/static/dist/
npm run lint             # ESLint check
```

**Vite config:** Bundles JS/CSS, fingerprints assets (`[hash].js`), configured in `vite.config.js`

### Template Routing (CRITICAL)

**Golden Rule:** Always trace `router → view function → template_response()` before editing any template.

**Key gotcha:** Requisitions parts tab loads `app/templates/htmx/partials/parts/list.html`, NOT `requisitions/list.html`. Follow the router!

## Authentication & Authorization

**OAuth2 via Azure AD:**
- `app/routers/auth.py` handles login/callback/logout
- Session middleware stores `user_id` in HTTP-only cookie (15-minute expiry)
- Fresh token validation via `require_fresh_token` dependency (15-min buffer)

**Permission Levels:**
- `require_user` — Any logged-in user
- `require_buyer` — Buyer role (can search, send RFQ)
- `require_admin` — Admin role (settings, user management)

## Response Format Standards

**JSON errors**: `{"error": "message", "status_code": 400, "request_id": "abc123"}`
- Tests check `response.json()["error"]`, NOT `["detail"]`

**List responses**: `{"items": [...], "total": 100, "limit": 50, "offset": 0}`
- Companies list returns this format — NOT a plain array

**HTMX responses**: HTMLResponse from Jinja2 templates

**Schemas**: All in `app/schemas/responses.py`, use `extra="allow"` on Pydantic models

## Coding Conventions

### Database
- Use `db.get(Model, id)` NOT `db.query(Model).get(id)` (SQLAlchemy 2.0 style)
- Status values: **Always** use StrEnum constants from `app/constants.py`, never raw strings
- Status enum example: `RequisitionStatus.OPEN`, `RequirementStatus.FOUND`

### Search & Matching
- Vendor matching: use `fuzzy_score_vendor()` from `app/vendor_utils.py` (rapidfuzz wrapper)
- MPN dedup: use `strip_packaging_suffixes()` from `app/services/search_worker_base/mpn_normalizer.py`
- Never inline rapidfuzz or fuzzy matching logic
- `search_requirement()` uses a separate write session — caller's ORM objects are stale after it returns. Call `db.expire(requirement)` before rendering templates.

### MPN Normalization
- `normalize_mpn()` uppercases, strips noise, returns `None` for MPNs < 3 chars
- `@validates` on Requirement auto-uppercases `primary_mpn`, `customer_pn`, `oem_pn` on every save
- CSS `uppercase` class on MPN display cells as belt-and-suspenders
- Use `|sub_mpns` Jinja2 filter for displaying substitutes (handles both string and dict formats)

### Substitutes Format
- Canonical format: `[{"mpn": "ABC123", "manufacturer": "TI"}, ...]` (list of dicts)
- Legacy rows may contain plain strings `["ABC123"]` — always handle both formats
- Use `parse_substitute_mpns()` from `app/utils/normalization.py` for write paths

### Shared Constants
- Junk email domains: use `JUNK_DOMAINS` from `app/shared_constants.py`
- Junk email prefixes: use `JUNK_EMAIL_PREFIXES` from `app/shared_constants.py`
- Don't duplicate; import and reuse

### Caching
```python
from app.cache.decorators import cached_endpoint

@cached_endpoint("vendor_list", ttl_hours=24, key_params=["supplier"])
async def get_vendors(supplier: str):
    ...
```

### Logging
- **Always** use Loguru: `from loguru import logger`
- Never use `print()`
- Structured logging with request_id context (auto-injected)
- Example: `logger.info("RFQ sent", extra={"requisition_id": 123})`

### Testing
- `TESTING=1` env var disables scheduler and real API calls
- Tests use in-memory SQLite (no real DB)
- `conftest.py` sets `RATE_LIMIT_ENABLED=false` in tests
- Import test engine: `from tests.conftest import engine`
- Mock lazy imports at source module, not at import site
- Target: 100% coverage, no commit reduces it

### Code Quality
- Ruff for linting: `ruff check app/`
- Mypy for type checking (enabled in pre-commit)
- Pyright LSP plugin active — stage only intentionally changed files
- Pre-commit hooks: ruff, ruff-format, mypy, docformatter, detect-private-key
- Keep routers thin (HTTP only), put business logic in `app/services/`
- New files must have header comment: what it does, what calls it, what it depends on
- Simple beats clever: 20 readable lines > 10 clever lines

## MVP Mode

`config.py: mvp_mode = True` — gates Dashboard, Enrichment, Teams, Task Manager.
Core MVP: Requisitions, Customers, Vendors, Sourcing Engine.

## Commands

### Docker (Full Stack)
```bash
docker compose up -d                # Start all containers
docker compose up -d --build        # Rebuild and start
docker compose logs -f app          # Tail app logs
docker compose restart              # Restart all containers
docker compose down                 # Stop everything
docker compose ps                   # Show status
```

### Frontend
```bash
npm run dev                   # Start Vite dev server (localhost:5173)
npm run build                 # Production build to app/static/dist/
npm run lint                  # ESLint check
npm run lint:fix              # Auto-fix ESLint issues
```

### Python Linting & Type Checking
```bash
ruff check app/               # Lint Python code
ruff format app/              # Auto-format Python code
mypy app/                     # Type check
```

### Database Migrations
```bash
alembic upgrade head                              # Apply all pending migrations
alembic downgrade -1                              # Rollback one revision
alembic revision --autogenerate -m "description"  # Generate new migration
alembic current                                   # Show current revision
alembic history                                   # Show all revisions
```

**Workflow:** Edit models → `alembic revision --autogenerate` → review → `alembic upgrade head` → test

## Testing

### Strategy: Tiered Approach

**During development: Run only changed module tests**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_<module>.py -v
```

**Before commit: Full test suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v
```

**Coverage check: Before PR only**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

**Fast subset (skip slow tests):**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest -m "not slow" -v
```
- Slowest tests are marked `@pytest.mark.slow` — skip with `-m "not slow"` for ~1:10 runtime
- NEVER add `--cov` to iterative dev runs — only before PR

### Test Configuration
- **Parallel execution:** pytest-xdist (`-n auto` in pytest.ini) for faster runs
- **Timeout:** 30 seconds per test
- **Async mode:** `asyncio_mode = auto`
- **Database:** In-memory SQLite (no real DB needed)
- **Fixtures:** In `tests/conftest.py` — import `engine` from there

### Focused Test Runs
pytest.ini configures `-n auto` (xdist parallel). For single-file runs, disable with:
`TESTING=1 PYTHONPATH=/root/availai pytest tests/test_foo.py -v --override-ini="addopts="`

### E2E Tests (Playwright)
```bash
npx playwright test --project=workflows    # Workflow E2E tests
npx playwright test --project=dead-ends    # Dead-end / error path tests
pytest tests/e2e/ --headed                 # Run with browser visible
```

### Test Types
| Type | Location | Pattern | When |
|------|----------|---------|------|
| Unit | tests/ | test_<module>.py | Changed module logic |
| Integration | tests/ | test_<module>.py | Router/service interaction |
| E2E | tests/e2e/, playwright | .spec.ts | Before PR, full workflows |
| Smoke | scripts/ | smoke-test-bundles.mjs | After npm build |

### Database Backup & Restore
- **Automated:** db-backup service runs `pg_dump` every 6 hours
- **Manual restore:** `scripts/restore.sh`
- **Current production:** Run `alembic current` to check

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

5. **After creating a migration, ALWAYS run `alembic heads`** to verify a single head. If multiple heads: `alembic merge heads -m "merge_description"`. Data-only migrations (no schema changes) use `op.get_bind()` + raw SQL via `text()`.

6. **startup.py is for runtime operations ONLY:**
   - FTS triggers (PostgreSQL-specific)
   - Seed data (system_config defaults)
   - ANALYZE on hot tables
   - Idempotent backfill queries
   - Count triggers (PG-specific)
   - NOTHING that creates, alters, or drops tables/columns/indexes/constraints

## Deployment

**Full deployment (preferred):**
```bash
./deploy.sh    # Commits, pushes, rebuilds, verifies logs with health checks
```

**Manual fallback:**
```bash
cd /root/availai
git pull origin main
docker compose up -d --build
docker compose logs -f app
```

**What "deploy" means:** Commit + push + rebuild + verify logs. No questions asked.

**IMPORTANT:** `deploy.sh` uses `--no-cache` on build (prevents stale cached layers) and `--force-recreate` on up (prevents reusing old containers). Never use bare `docker compose up -d --build` — it causes "code didn't update" bugs. For rebuild without commit: `./deploy.sh --no-commit`.

### Pre-Deploy Checklist
- [ ] All tests pass: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
- [ ] Linting passes: `ruff check app/`
- [ ] No migrations pending
- [ ] `.env` configured for target environment
- [ ] Docker images built locally (no surprises at deploy time)

---

## Safety & Quality

### Before Destructive Operations
- **WARN** before DROP, DELETE, or bulk data changes
- Include backup procedure and rollback steps
- For production: verify backup exists before executing

### Code Review Checklist
- Security: SQL injection, XSS, auth bypass, exposed secrets
- Performance: N+1 queries, missing indexes, inefficient loops
- Error handling: All exceptions caught, user-facing errors clear
- Tests: New code has tests, coverage not reduced
- Types: No `type: ignore` without explanation

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

**Azure OAuth:**
```
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
AZURE_TENANT_ID=...
AZURE_REDIRECT_URI=https://app.yourdomain.com/auth/callback
```

**AI (Anthropic):**
```
ANTHROPIC_API_KEY=sk-...
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
```

**Database:**
```
DATABASE_URL=postgresql://availai:availai@db:5432/availai
```

**Suppliers (Optional — feature disabled if not set):**
```
OCTOPART_API_KEY=...
BROKERBIN_API_KEY=...
DIGIKEY_CLIENT_ID=...
# ... (other API keys)
```

**Feature Flags:**
```
MVP_MODE=false
EMAIL_MINING_ENABLED=true
ACTIVITY_TRACKING_ENABLED=true
CONTACTS_SYNC_ENABLED=true
```

**Email:**
```
MICROSOFT_GRAPH_ENDPOINT=https://graph.microsoft.com/v1.0
SMTP_FROM=noreply@yourdomain.com
```

---

## Debugging Tips

**Can't find a route?**
Trace the HTMX link → search routers/ → find `@router.get()` or `@router.post()` → check `template_response()`

**Search returns empty?**
Check `.env` for API keys (Octopart, BrokerBin, etc.). No keys = no results. You can upload vendor stock lists to build a local database.

**"Check Inbox" finds nothing?**
- Verify replies are in the same email thread as the RFQ you sent
- Check that `Mail.Read` permission is granted in Azure
- Look at `app/jobs/inbox_monitor.py` logs

**Tests fail with "TESTING not set"?**
Run with: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`

**Docker container won't start?**
```bash
docker compose logs app          # See the error
docker compose restart app       # Try again
docker compose up -d --build     # Rebuild from scratch
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Start everything | `docker compose up -d` |
| Start frontend dev | `npm run dev` |
| Run tests (single) | `TESTING=1 pytest tests/test_<module>.py -v` |
| Run all tests | `TESTING=1 pytest tests/ -v` |
| Check coverage | `TESTING=1 pytest tests/ --cov=app --cov-report=term-missing` |
| Create migration | `alembic revision --autogenerate -m "..."` |
| Deploy | `./deploy.sh` |
| View logs | `docker compose logs -f app` |
| Lint code | `ruff check app/` |
| Type check | `mypy app/` |
| Stop everything | `docker compose down` |

---

**Maintained by:** Development team
**Questions?** Check existing tests (`tests/`) or browse `app/services/` for similar patterns.


## Skill Usage Guide

When working on tasks involving these technologies, invoke the corresponding skill:

| Skill | Invoke When |
|-------|-------------|
| fastapi | Builds FastAPI routes, dependency injection, and middleware |
| htmx | Implements HTMX attributes for server-driven UI updates |
| jinja2 | Renders server-side templates with Jinja2 syntax and inheritance |
| vite | Configures Vite build system, asset bundling, and dev server |
| frontend-design | Designs UI with HTMX, Alpine.js, and Tailwind CSS styling |
| pytest | Writes pytest tests with fixtures and async support |
| redis | Configures Redis caching with decorators and TTL |
| mypy | Enforces mypy strict type checking on Python code |
| playwright | Implements end-to-end tests with Playwright |
| mapping-user-journeys | Maps in-app journeys and identifies friction points in code |
| clarifying-market-fit | Aligns ICP, positioning, and value narrative for on-page messaging |
| designing-onboarding-paths | Designs onboarding paths, checklists, and first-run UI |
| instrumenting-product-metrics | Defines product events, funnels, and activation metrics |
| crafting-page-messaging | Writes conversion-focused messaging for pages and key CTAs |
| tuning-landing-journeys | Improves landing page flow, hierarchy, and conversion paths |
| mapping-conversion-events | Defines funnel events, tracking, and success signals |
| structuring-offer-ladders | Frames plan tiers, value ladders, and upgrade logic |
| inspecting-search-coverage | Audits technical and on-page search coverage |
| adding-structured-signals | Adds structured data for rich results |
