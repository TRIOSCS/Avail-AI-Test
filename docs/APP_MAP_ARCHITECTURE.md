# AvailAI Application Map — Architecture & Stack

> **Auto-maintained reference.** Update this file whenever the tech stack, infrastructure, or project structure changes.

## What It Is

AvailAI is a production electronic component sourcing platform and CRM. Buyers search 10+ supplier APIs in parallel, send RFQs via email, receive AI-parsed responses, build quotes, and manage buy plans — all through an HTMX-driven web interface.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.13, FastAPI, Uvicorn |
| **Database** | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| **Cache** | Redis 7 (fallback: PG JSONB) |
| **Frontend** | Jinja2 + HTMX 2.0 + Alpine.js 3.15 + Tailwind CSS |
| **Build** | Vite 6, PostCSS, Tailwind CSS (safelisted) |
| **AI** | Anthropic Claude (email parsing, enrichment, tagging) |
| **Auth** | Azure AD OAuth2, Microsoft Graph API |
| **Jobs** | APScheduler |
| **Proxy** | Caddy (auto HTTPS) |
| **Monitoring** | Sentry, Loguru |
| **Deploy** | Docker Compose (6 containers), deploy.sh with build tag + CSS verification |

## Infrastructure (Docker Compose)

| Container | Purpose | Resources |
|-----------|---------|-----------|
| **app** | FastAPI on port 8000 | 2 GB / 2 CPU |
| **db** | PostgreSQL 16 | 1.5 GB |
| **redis** | Cache + coordination | 768 MB |
| **caddy** | Reverse proxy, HTTPS | 512 MB |
| **db-backup** | pg_dump every 6 hours | 256 MB |
| **enrichment-worker** | Paced material-card enrichment — trust chain `verified` (distributor API) → `web_sourced` (Claude web search, authorized domains) → **OEM cross-ref** (grounded web, double-verify against distributors → `verified`) → **OEM description** (`oem_sourced`, single official OEM page) → `ai_inferred` (Opus 4.8, ≥0.95, flagged) → `not_catalogued` (recognised OEM/FRU, no public specs) / `not_found`. OEM tiers gated by a pure regex classifier (`oem_classifier.py`). `web_meter` tracks per-card billable web calls + Claude health for exact budget accounting and circuit-breaker reset. Fast-lane: newest-added parts head the queue (`select_batch` orders `unenriched-first, then search_count DESC, created_at DESC` — never-resolved parts drain before `not_found` re-checks, so old low-demand cards aren't starved by the daily re-check churn); ~60s idle poll | 512 MB |

## Request Flow — Browser to Database

```
Browser (HTMX + Alpine.js)
    │
    │  HTTP GET/POST (HTML partials or JSON)
    ▼
Caddy (reverse proxy, TLS termination, static files)
    │
    ▼
FastAPI Middleware Stack (in order):
    ├── 1. GZipMiddleware (compress >= 500 bytes)
    ├── 2. SessionMiddleware (HTTP-only cookie, 15-min expiry)
    ├── 3. CSRFMiddleware (double-submit cookie on mutations)
    ├── 4. PrometheusMiddleware (request count + duration histogram, app/prometheus_metrics.py)
    ├── 5. CSP Middleware (Content-Security-Policy header)
    ├── 6. Request ID Middleware (UUID tracking, timing, logging)
    └── 7. API Version Middleware (/api/v1/* -> /api/*)
    │
    ▼
Router (27 router modules)
    ├── Dependencies: require_user, require_fresh_token, rate limiter
    ▼
Route Handler
    ├──> Service Layer (118 modules) --> Database (SQLAlchemy)
    ├──> Connectors (12 APIs) --> External services
    ├──> Cache (Redis) --> get/set with TTL
    └──> Template (Jinja2) --> HTML response
    │
    ▼
Response -> Caddy -> Browser -> HTMX swaps into DOM
```

## Frontend Architecture

```
base.html (app shell: topbar, mobile nav, modal, toast, SSE)
└── base_page.html (spinner -> hx-get lazy load)
    └── partials/ (167 HTML files across 15 feature dirs)
```

- **Navigation:** HTMX `hx-get` swaps into `#main-content` (no SPA routing)
- **State:** Alpine.js `x-data` + `$store.toast`, `$store.sidebar` with `@persist`
- **Forms:** `hx-post` with `data-loading-disable` and `hx-indicator` spinners
- **Tabs:** Each tab is a separate partial loaded on click
- **Real-time:** SSE via `hx-ext="sse"` for notifications
- **Build:** Vite bundles `htmx_app.js` + `styles.css` -> content-hashed dist/
- **Tailwind safelist:** Broadened to cover all color families (slate, red, amber, emerald, etc.) + Python content scanning so dynamic classes survive tree-shaking

### HTMX Conventions

HTMX is the primary client/server interaction layer. Sourcing is strictly
user-initiated: clicking the refresh icon on a sightings row or the
detail-panel "Search" button POSTs `/refresh`, gated by a 48h per-MPN
cooldown via `MaterialCard.last_searched_at`. The row click itself is
read-only (`GET /detail`, no connector calls). The `X-Rendered-Req-Id`
correlation header and `?source=user|sse` query-param gate are documented
in `APP_MAP_INTERACTIONS.md`. The do/don't rules for imperative
`htmx.ajax()` calls live in `docs/htmx-conventions.md` and are the
authoritative reference. Static-analysis tests in
`tests/test_static_analysis.py` enforce the conventions in CI:
`broker.publish` source-gating, `htmx.ajax()` indicator coverage, and
`X-Rendered-Req-Id` header coverage on context-sensitive responses.

### Templates by Feature

| Feature | Count | Directory |
|---------|-------|-----------|
| Requisitions | 32 | partials/requisitions/ |
| Vendors | 16 | partials/vendors/ |
| Customers | 11 | partials/customers/ |
| Materials | 13 | partials/materials/ |
| Excess | 10 | partials/excess/ |
| Parts | 13 | partials/parts/ |
| Quotes | 9 | partials/quotes/ |
| Sightings | 7 | partials/sightings/ |
| Search | 8 | partials/search/ |
| Prospecting | 5 | partials/prospecting/ |
| Proactive | 4 | partials/proactive/ |
| Emails | 4 | partials/emails/ |
| Tickets | 4 | partials/tickets/ |
| Settings | 5 | partials/settings/ |
| Shared | 16 | partials/shared/ |
| Buy Plans | 3 | partials/buy_plans/ |

### Shared Template Components

| Component | File | Purpose |
|-----------|------|---------|
| `_mpn_chips.html` | partials/shared/ | Renders all MPNs (primary + substitutes) as equal inline pill chips with overflow toggle; clickable chips open material card modal when a `link_map` entry exists |
| `status_badge` macro | partials/shared/_macros.html | Unified status badge rendering used by all pages (requisitions, sightings, parts, etc.) |

### Inline Editing

- **Part header descriptions:** AI-generated descriptions are inline-editable in the part header partial (`parts/header.html`). Users click to edit, submit via `hx-patch`, and the display swaps back.

## External Service Integration

```
                    ┌──────────────┐
                    │   AvailAI    │
                    │   FastAPI    │
                    └──────┬───────┘
                           │
          ┌────────────────┼────────────────────┐
          │                │                    │
     Auth & Comms    Supplier APIs        AI & Intel
          │                │                    │
   Azure AD          Nexar (Octopart)     Claude API
   Graph API         BrokerBin            Apollo API
   Teams API         DigiKey              Explorium API
   8x8 API           Mouser
                     Element14
                     eBay
                     OEMSecrets
                     SourceEngine
                     ICS/NC Workers (browser)
                     Email Mining (local)
```

## Background Jobs (APScheduler)

| Job | Frequency | Purpose |
|-----|-----------|---------|
| inbox_monitor | 30 min | Poll Graph API for RFQ replies, parse with Claude |
| requirement_refresh | 4 hours | Re-search stale requirements |
| proactive_matcher | Daily | Match vendor offers to customer history |
| vendor_scorer | Daily | Update vendor reliability scores |
| health_check | 5 min | DB, Redis, API connector health |
| backup | 6 hours | pg_dump |
| tagging_auto | Hourly | AI-classify parts by commodity/brand |
| material_enrichment | 2 hours | First pass: enrich pending material cards (Claude Haiku: description, category, lifecycle_status); second pass: structured-spec extraction via `spec_enrichment_service` (Claude Sonnet: `specs_structured` + `material_spec_facets`) |
| task_reminder | 2 hours | Notify overdue tasks |
| teams_sync | 6 hours | Sync Teams call history |
| prospecting_refresh | Daily | Web search for new prospects |
| maintenance | Daily | DB ANALYZE, cache cleanup, integrity checks |
| quality | Daily | Vendor scorecards, engagement scoring |

## Management Commands (`app/management/`)

| Module | Invocation | Purpose |
|--------|-----------|---------|
| `reenrich.py` | `python -m app.management.reenrich` | Re-run first-pass card enrichment (description/category/lifecycle) on existing cards |
| `enrich_specs.py` | `python -m app.management.enrich_specs --limit N` | One-time / on-demand backfill of structured-spec extraction for cards missing `specs_enriched_at` |

## Enrichment Worker Modules (`app/services/enrichment_worker/`)

Key modules added for OEM/FRU enrichment:

| Module | Purpose |
|--------|---------|
| `oem_classifier.py` | Pure regex vendor classifier (`classify_oem_vendor`) — detects Lenovo/IBM, HPE/HP, Dell, Acer, ASUS FRU codes to gate the OEM tiers. Non-OEM parts never incur OEM web calls. |
| `oem_domains.py` | Security allowlists (`is_oem_domain`, `is_crossref_domain`) for OEM-official and distributor/manufacturer pages; mirrors `trusted_domains.py`. All domain checks enforced in Python — LLM claims are never trusted. |
| `oem_extractor.py` | Grounded-web-search extractors: `cross_reference_mpn` resolves an OEM/FRU code to a candidate commodity MPN (four Python gates); `extract_oem_description` fetches an official OEM description (four Python gates). Both raise `ClaudeError` on backend failure. |

## Description→Spec Extractor (`app/services/desc_extractor/`)

Deterministic description→spec token grammar (zero LLM/network) run by the
enrichment worker's second pass between the MPN decode (0.95) and the AI spec
reader (>= 0.85) — see APP_MAP_INTERACTIONS "Worker second-pass ordering":

| Module | Purpose |
|--------|---------|
| `__init__.py` | `extract_desc` pure router: TRIO `<Label>,` lead / comma-less first token / whole-word body tokens route to a commodity; foreign labels ("Other,"/"Tray,"…), cross-family conflicts, and degenerate descriptions return None. |
| `_common.py` | `DescResult` dataclass + `DESC_SOURCE`/`DESC_CONFIDENCE`/`SPEC_COMMODITIES` constants shared by the router and the writer. |
| `storage.py` | hdd/ssd token grammar: capacity (link-speed tokens excluded), rpm (hdd-only), form factor, interface — per-commodity seeded vocabularies. |
| `memory.py` | DRAM token grammar: capacity, ddr_type, speed_mhz, ecc (incl. Non-ECC negation), form_factor, rank — seeded enums + the only numeric_range gate. |
| `writer.py` | Worker adapter `extract_and_record_specs`: writes via `record_spec(source="desc_parse", confidence=0.90)`, gated by `settings.desc_parse_enabled`; skips keys held at strictly higher confidence; never categorizes; per-card SAVEPOINT isolation; returns `{parsed, written, failed}`. |

## Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `backfill_oem_enrichment.py` | Dry-run-first backfill over `not_found` / `not_catalogued` cards through the OEM tiers. Writes a coverage CSV; rolls back unless `--commit`. Shared `web_meter` budget cap (`--max-web-calls`, default 300) halts mid-run to prevent API overspend. The paced worker drains any remainder. |

## Key Numbers

| Metric | Count |
|--------|-------|
| Python files | ~315 |
| Python LOC | ~75,000 |
| HTML templates | 167 |
| Database tables | 50+ |
| API endpoints | 400+ |
| Service modules | 120 |
| Supplier connectors | 12 |
| Background jobs | 15 modules |
| Test files | 100+ |
| Alembic migrations | 81+ |
