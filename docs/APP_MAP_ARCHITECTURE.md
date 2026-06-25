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
    │       Inner of Session (registered before it so Session is outer): AuditUserMiddleware
    │       (sets current_user_id contextvar) and ModuleAccessMiddleware (per-user MODULE
    │       access chokepoint on module-exclusive HTMX sub-partials — see INTERACTIONS
    │       "Module SUB-partial chokepoint"; reads scope["session"] so Session must run first).
    ├── 3. CSRFMiddleware (double-submit cookie on mutations)
    ├── 4. PrometheusMiddleware (request count + duration histogram, app/prometheus_metrics.py)
    │       Note: fastapi 0.137 (PR #15745) made `app.routes` a tree — `include_router`'d
    │       routes hide behind opaque `_IncludedRouter` wrappers — so `_handler_for` reads
    │       the templated label straight off `scope["route"].path` instead of walking the
    │       route table (nesting-agnostic, correct on 0.136.x and 0.137.x).
    ├── 5. CSP Middleware (Content-Security-Policy header)
    ├── 6. Request ID Middleware (UUID tracking, timing, logging)
    │       Also owns Cache-Control (set HERE, the OUTERMOST @app.middleware, because
    │       inner response processing drops headers set on the TemplateResponse itself):
    │       /static/assets/* (Vite-hashed) -> immutable 1yr; other /static -> 1hr; EVERY
    │       text/html response — full-page shell AND /v2/partials/* HTMX fragments ->
    │       no-store,no-cache,must-revalidate + Pragma:no-cache (so a redeploy's markup is
    │       fetched fresh, not heuristically cached stale). Guard is the response
    │       content-type ONLY (starts "text/html"), so JSON, SSE (text/event-stream), and
    │       file downloads (Content-Disposition) are untouched and streaming bodies unread.
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

## Authorization & Access Control

Three layers: **role gates** (who may reach an endpoint), **ownership scoping**
(which records a user may act on), and **per-feature access** (which nav modules +
capabilities a user is granted).

- **Role gates** — FastAPI dependencies in `app/dependencies.py`: `require_user`
  (any authenticated active user), `require_buyer` (BUYER_ROLES = buyer/sales/trader/
  manager/admin), `require_admin`, `require_manager`. The non-interactive `agent`
  account is excluded from buyer-tier actions.
- **Ownership scoping (role-scoped model)** — `RESTRICTED_ROLES = {SALES, TRADER}`
  (single source of truth in `app/constants.py`): sales/trader users may act only on
  requisitions they created (`Requisition.created_by`); buyer/manager/admin are
  unrestricted. Enforced through ONE chokepoint, not per-endpoint logic:
  - `require_requisition_access(db, req_id, user, *, owner_id=None, label=...)` — pure
    guard, raises 404 for a restricted non-owner. Used after loading a requisition or a
    requisition-scoped child (Offer/Requirement/Contact/VendorResponse/SourcingLead;
    `owner_id` covers scratch resources with a null `requisition_id`).
  - `get_req_for_user` / `get_quote_for_user` — load-and-authorize helpers that return
    the owned record or 404.

  Every mutating or email-sending endpoint that touches a requisition-scoped resource
  routes through one of these. Regression tests live in `tests/test_authz_*.py`
  (a non-owner sales/trader user must get 404). 404 (not 403) is used so resource
  existence isn't leaked.

- **Per-feature access (user-management feature)** — an access registry gates both
  nav-module visibility and discrete capabilities, administered from Settings > Users.
  - **Access registry (`app/constants.py`)** — `AccessKey` StrEnum is the closed
    vocabulary of grantable access: 10 module keys (`MODULE_ACCESS_KEYS` — requisitions/
    sightings/materials/search/buy_plans/resell/crm/proactive/prospecting/my_day) + 5
    capability keys (`CAPABILITY_ACCESS_KEYS` — send_rfq/approve_offers/export_data/
    manage_connectors/ops_verification). `ROLE_ACCESS_DEFAULTS` maps each `UserRole` to
    its default key set; defaults deliberately preserve prior behavior (every interactive
    role gets all modules + all capabilities except `ops_verification`; admin → all; agent
    → none), so turning the layer on is a no-op until an admin sets an override.
    `UserAuditAction` StrEnum is the closed vocabulary for the audit trail.
  - **Effective-access resolution (`app/dependencies.py`)** — `user_has_access(user,
    key, db)`: admin → always True; `ops_verification` delegates to
    `VerificationGroupMember` (single source of truth); otherwise an explicit per-user
    override (`User.access_overrides`) wins, else the role default. `require_access(key)`
    is a dependency factory (depends on `require_user`) that raises 403 unless the user
    has `key` — applied to the 10 nav-module partial entry routes and the capability
    actions (RFQ-send, offer approve/reject/reconfirm, CSV/quote exports, source-test).
  - **Admin Users surface (`app/routers/admin/users.py`)** — admin-only CRUD: invite,
    change role, activate/deactivate, a per-user access editor (module + capability
    toggles; `ops_verification` writes `VerificationGroupMember`), and an audit-log
    viewer. Self-protection invariants: no self-demote/deactivate, the last active admin
    is row-locked-protected, the agent account is uneditable. Every mutation appends a
    `UserAdminAudit` row via `services.user_admin.record_user_audit`. `module_access_map`
    (here) builds the `{nav-id: bool}` map consumed by the nav gate.
  - **Templates** — Settings > Users tab (`partials/settings/users.html` +
    `user_access_panel.html` access editor + `users_audit.html` log; tab wired in
    `settings/index.html`, GET tab `htmx_views.settings_users_tab`). Nav gating:
    `_base_ctx` exposes the `{nav-id: bool}` `access` map and `shared/mobile_nav.html`
    hides revoked sections; `v2_page` redirects a denied module to the first allowed one.
  - **Config** — `ENABLE_USER_ALLOWLIST` (`app/config.py`, default True): when on, an
    OAuth login by an unknown email (no pre-provisioned row) is rejected at the callback
    unless the email is in `ADMIN_EMAILS`. See APP_MAP_INTERACTIONS for the allowlist +
    invite-adoption flow.

## Frontend Architecture

```
base.html (app shell: topbar, mobile nav, modal, toast, SSE)
└── base_page.html (spinner -> hx-get lazy load)
    └── partials/ (182 HTML files across 24 feature dirs)
```

- **Navigation:** HTMX `hx-get` swaps into `#main-content` (no SPA routing)
- **State:** Alpine.js `x-data` + `$store.toast`, `$store.sidebar` with `@persist`
- **Forms:** `hx-post` with `data-loading-disable` and `hx-indicator` spinners
- **Tabs:** Each tab is a separate partial loaded on click
- **Real-time:** SSE via `hx-ext="sse"` for notifications
- **Build:** Vite bundles `htmx_app.js` + `styles.css` -> content-hashed dist/
- **Tailwind safelist:** Broadened to cover all color families (slate, red, amber, emerald, etc.) + Python content scanning so dynamic classes survive tree-shaking
- **Modals:** One global wrapper in `base.html` driven by the `resizableModal()` Alpine
  component (`htmx_app.js`); every dialog loads into `#modal-content` via
  `$dispatch('open-modal', {url, wide})`. The panel (`.modal-shell`) is a flex column
  capped to the viewport so it stays on-screen and scrolls internally — responsive on
  every screen. On desktop (≥1024px) it is drag-to-move (top strip) + drag-to-resize
  (8 edge/corner handles), with size/position remembered per `lg`/`wide` bucket in
  `localStorage['avail_modal_geom']` (double-click any handle to reset); on phones
  (<640px) it renders as a full-width bottom sheet (handles hidden). Pure geometry math
  is isolated in `app/static/modal_geometry.js` (unit-tested, no DOM). Content fetches
  use a `#modal-loading` spinner as the htmx indicator. Modal bodies that need to fill a
  resized panel use a `flex flex-col h-full min-h-0` root with a `flex-1 min-h-0` scroll
  region (e.g. `unified_modal.html` parts table, `vendor_modal.html` preview).

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
| Customers | 14 | partials/customers/ |
| Materials | 13 | partials/materials/ |
| Resell | 11 | partials/resell/ — resell-brokerage workspace (replaced the removed `partials/excess/`; router `routers/resell.py`) |
| Parts | 13 | partials/parts/ |
| Quotes | 5 | partials/quotes/ — `list.html` removed (standalone Quotes tab retired); detail/macros/line_row/preview/pricing_history remain |
| Sightings | 7 | partials/sightings/ |
| Search | 13 | partials/search/ — incl. the Part Dossier ("Bench") at `/v2/search?mpn=`: `dossier_shell/hero/specs/recent/market.html` (routes in `routers/part_dossier.py`). |
| Prospecting | 8 | partials/prospecting/ — list/_card/_macros/detail/stats/add_result/enrich_status/_action_oob; buyer-ready ranking via `services/prospect_priority.build_priority_snapshot` (single source of truth); background enrich polls `/enrich-status` (HTTP 286 stops); grid actions OOB-remove cards + refresh `#prospect-stats` |
| Proactive | 4 | partials/proactive/ |
| Emails | 4 | partials/emails/ |
| Tickets | 4 | partials/tickets/ |
| Settings | 8 | partials/settings/ — tabs: **Connectors** (unified, replaces Sources + API Keys; admin-only), Profile, System, Data Ops, Ops Group, **Users** (admin-only); legacy `/sources` + `/api-keys` routes 302 → Connectors. Users tab = `users.html` (invite/role/activate table) + `user_access_panel.html` (per-user access editor modal) + `users_audit.html` (audit-log viewer); see Authorization & Access Control. |
| Shared | 18 | partials/shared/ |
| Buy Plans | 6 | partials/buy_plans/ — the **Deal Hub**, a role-lens shell at `/v2/buy-plans` (own primary-nav tab). `hub.html` is the shell (lens switcher + lazy `#bp-hub-body`); `_board.html` (sales "My Deals" stage board), `_orders_queue.html` (buyer "My Orders" PO-cut queue), `_supervise.html` (manager/ops "Supervise" triage strip + all-scope board) are the three lens bodies; `detail.html`/`_macros.html` are the single-plan view. Lens partial routes: `GET /v2/partials/buy-plans` (shell, `lens=` param → role-derived default), `/orders`, `/board?scope=`, `/supervise` (all in `routers/htmx_views.py`). Read models in `services/buyplan_hub.py` (`buyer_line_queue` / `deals_board` / `supervise_overview`). The retired `/v2/reporting` page folded its analytics in here (supervise strip) + the Sales Hub pipeline chip + the CRM coverage chip — `partials/reporting/` and the `reporting_dashboard` route are gone. |

### Shared Template Components

| Component | File | Purpose |
|-----------|------|---------|
| `_mpn_chips.html` | partials/shared/ | Renders all MPNs (primary + substitutes) as equal inline pill chips with overflow toggle; clickable chips open material card modal when a `link_map` entry exists |
| `status_badge` macro | partials/shared/_macros.html | Unified status badge rendering used by all pages (requisitions, sightings, parts, etc.). Thin wrappers over it apply entity-specific color maps: `req_status_badge` (Requisition.status), `quote_status_badge` (Quote.status + RFQ Contact.status — one canonical map so "sent" is brand everywhere), `account_type_badge` (Company.account_type). |
| `activity_icon` / `activity_row` macros | partials/shared/_macros.html | Canonical activity-timeline icon + row. Every entity timeline (requisitions, parts, sightings, vendors, customers) renders through these — the customer activity tab calls `activity_icon` directly rather than re-declaring an inline icon map. |
| `cadence_hero` / `cadence_clocks` macros | partials/shared/_macros.html | Shared cadence card. `cadence_clocks(entity, now_utc)` is the dual-clock (Last Out / Last Reply) render used by both `cadence_hero` (vendor) and the customer Account Cadence card in `customers/detail.html` + `customers/header.html`. |
| `suggested_contact_row` macro | partials/shared/_contact_row.html | Single source of truth for a discovered/suggested contact card + Add form. Consumed by the Contacts-tab `customers/tabs/_suggested_contacts.html` (Add → `#contacts-tab-list`, innerHTML) and the Enrich result panel `shared/_enrich_result.html` (Add → `closest li`, outerHTML, `from_enrich=1`). Target/swap are macro params. |
| `_enrich_result.html` | partials/shared/ | Result panel swapped into `#enrich-results-{id}` by `enrich_company`/`enrich_vendor_card` (HTMX path). Firmographics grid (Updated/Current pills) + source/freshness + discovered contacts (companies). Replaced the old raw-JSON dump. |
| `list.html` | partials/customers/ | CDM account workspace: split-panel layout (left = scrollable account list, right = `#cdm-detail`), resizable divider via the `splitPanel` Alpine component (panel id `cdm`). Modeled on the requisitions2 workspace. |
| `_account_list.html` | partials/customers/ | Left-panel account list only — swapped in on filter/sort/pagination refreshes by `GET /v2/partials/customers/account-list`. |
| `_detail_empty.html` | partials/customers/ | Right-panel placeholder shown before any account is selected in the CDM workspace. |
| `tabs/contacts_tab.html` | partials/customers/tabs/ | Contacts tab partial for company detail — default tab on `company_detail_partial`. Displays `contact_rows` (active SiteContacts across the company's active sites + legacy site-level contacts on active sites) and renders click-to-contact links (tel:/mailto:/Teams deep link/weixin://) with `data-outreach-log` attributes. |
| `tabs/quotes_tab.html` | partials/customers/tabs/ | CRM account Quotes tab — Alpine status filter (all/draft/sent/won/lost). Quote set = union of site-linked and requisition-linked quotes via `_company_quotes_query`. Served at `GET /v2/partials/customers/{id}/tab/quotes`. |

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
   Graph API         BrokerBin            Clay MCP
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
| po_verification | 15 min | Scan buyer sent-mail for PO confirmations on active buy plans |
| stock_autocomplete | Daily | Auto-complete stuck stock-sale buy plans (case report + notification) |
| buyplan_nudge | 30 min | Remind buyer (PO unconfirmed >4h) / ops (PO unverified >2h); idempotent via `buy_plan_lines.last_nudge_at` |

## Management Commands (`app/management/`)

| Module | Invocation | Purpose |
|--------|-----------|---------|
| `reenrich.py` | `python -m app.management.reenrich` | Re-run first-pass card enrichment (description/category/lifecycle) on existing cards |
| `enrich_specs.py` | `python -m app.management.enrich_specs --limit N` | One-time / on-demand backfill of structured-spec extraction for cards missing `specs_enriched_at` |
| `ingest_source_data.py` | `python -m app.management.ingest_source_data [--files GLOB] [--ai-correct] [--apply] [--limit N]` | SP-Ingest CLI: parse → clean → consolidate → (ai_correct) → ingest TRIO source files (SFDC part master + inventory sheets) into `material_cards` via the SP2 tier ladder. DRY RUN by default; `--apply` writes. |
| `reconcile_decoded_facets.py` | `python -m app.management.reconcile_decoded_facets [--apply] [--limit N]` | Facet-accuracy reconcile: re-run the fixed MPN decoder + desc extractor over cards with mpn_decode/desc_parse facet rows for capacity_gb/gpu_family/memory_gb; corrects changed values (same source, newer ladder timestamp) and DELETES keys the fixed extractor no longer yields. DRY RUN by default with per-failure-class tallies; `--apply` writes. |
| `backfill_vendor_specs.py` | `python -m app.management.backfill_vendor_specs [--apply] [--limit N] [--daily-cap N] [--source mouser\|element14]` | Vendor-API parametric enrichment: select uncategorized cards demand-first (`sourced_qty_90d DESC NULLS LAST`), search the source for each within a date-keyed per-day call cap (`vendor_api:{source}:calls:{date}`), then the per-source writer enriches through the F1 ladder. `--source mouser` (default, cap 800) → `vendor_spec_enrich.enrich_card_from_mouser` (Mouser's rich DESCRIPTION → desc grammar at connector_desc/84; Mouser carries no structured parametrics). `--source element14` (cap 100 — Element14 rate-limits hard) → `enrich_card_from_element14` (Element14's structured `attributes` ARE parametrics; the connector maps them to seeded keys via `_vendor_spec_map`, written at element14_api/90). DRY RUN by default (counts/searches/writes nothing); `--apply` writes. |
| `seed_sample_data.py` | `python -m app.management.seed_sample_data [--owner EMAIL] [--wipe]` | Populate staging with a realistic, interconnected sample dataset (companies/contacts/vendors, requisitions+requirements, offers across statuses, quotes incl. revised/won + chosen offers, buy plans, resell/excess lists with competing per-line + take-all broker offers and a customer bid-back, sightings, dated activities, account/contact tasks, outreach + buyer scores, material cards via the F1 ladder) so every workflow can be exercised end-to-end. Idempotent-additive (re-run creates 0 rows; get-or-create on natural keys), every sample row carries the `AVSAMPLE`/`avsample` marker, and `--wipe` deletes ONLY tagged sample rows (FK-safe) — never real data. `--owner EMAIL` assigns the deals to that user (redirecting the seeder/sales/buyer/trader roles) so they show in that user's own-work lenses (buy-plans "orders"/"deals", resell "Open to Me") not just admin "supervise"; re-owning needs `--wipe` first (rows are never UPDATEd), and an unknown email pre-provisions a real, never-wiped account. ORM-only, zero outbound effects. |

## TRIO Source Ingest (`app/services/source_ingest/`)

AUGMENT-only pipeline that ingests TRIO's own SFDC part master + inventory sheets as
top-tier enrichment input (trio_source:95 / trio_source_ai:88 on the F1 ladder):

| Module | Purpose |
|--------|---------|
| `models.py` | `SourceRecord` / `ConsolidatedPart` dataclasses + `SOURCE_KIND_PRIORITY` (sfdc_master > inventory_sheet). |
| `parsers.py` | `parse_sfdc_material_master` (streams the LSC1__Material__c CSV) + `parse_inventory_sheet` (csv/xlsx/txt operational captures) → raw `SourceRecord`s. |
| `clean.py` | MPN suffix strip + `normalize_mpn_key` dedup key, `_x000D_`/control-char scrub, condition canon via `constants.MaterialCondition` (None when the source carries none — never a synthetic "Unknown"), trailing-OEM extraction, category via `normalize_trio_category` (TRIO-scoped vocabulary, e.g. bare "Memory"→dram); drops <3-char MPNs and "DO NOT USE" rows. |
| `consolidate.py` | Groups cleaned records by `normalized_mpn` → one `ConsolidatedPart` per MPN with per-field provenance (description=longest, manufacturer=modal, condition=modal, quantity=sum, specs merged with master-wins). |
| `ai_correct.py` | Optional Claude (smart tier) standardization/inference pass — output tagged `trio_source_ai` (tier 88, below vendor APIs). Per-part failure isolation; fail-fast on ClaudeUnavailable/Auth; returns `{corrected, failed}` for the report. |
| `ingest.py` | AUGMENTs `material_cards` (creates when absent; never clobbers an existing description), category via `spec_tiers.set_category`, specs via `record_spec`; per-card SAVEPOINTs with tallies merged only after a clean release; failed parts counted + sampled in the report; `apply=False` (default) is a true dry run through the SAME ladder/schema gates (`set_category(write=False)` + `spec_would_write`) so the report matches `--apply`. |

## Offer Qualification Service (`app/services/offer_qualification.py`)

Pure-function library that drives the condition-spine qualification capture for buyer-entered
offers. Zero I/O except `apply_qualification` (writes onto an Offer ORM object) and
`prefill_from_vendor` (one DB read). All other functions are pure Python — safe to call from
templates, tests, or background jobs without a DB session.

| Export | Role |
|--------|------|
| `validate_essentials` | Per-condition gate (new/new_no_pkg/pulls/refurb); returns error strings |
| `compose_note` | System-composed standardized note for `offers.qualification_note` |
| `meter` | `(filled, total)` qualification item counts |
| `compute_status` | → `QualificationStatus` string |
| `apply_qualification` | Composes note+status onto Offer ORM; never raises (gate is in buyer handlers) |
| `normalize_offer_condition` | Normalizes raw condition incl. legacy `used`→`pulls` |
| `prefill_from_vendor` | Vendor-memory (#8): stable answer prefill from the vendor's last offer |
| `request_template` | RFQ-back request text for `images`/`fpq`/`cert`/`pkg_qty` |
| `essentials_data` | Canonical `data` dict builder (keeps key-set in sync across callers) |
| `PACKAGING_CHIPS` | Display strings for the packaging chip selector |
| `REQUEST_KINDS` | Tuple of valid request kind tokens |

Frontend: `partials/offers/_qualification_fields.html` (condition-spine partial) and
the `offerQualification` Alpine.js factory in `htmx_app.js` (live note preview + meter).
`_offer_row.html` renders the qualification badge and standardized note/request list on each
offer row.

## Cross-App Alerts (`app/services/alerts/`)

Reusable framework behind the emerald nav-count badges and the in-tab fluid spotlight.
Each nav tab registers one or more `AlertSource`s; a badge count is the SUM of its
sources' counts. See APP_MAP_INTERACTIONS § Cross-app alerts.

| Module | Purpose |
|--------|---------|
| `base.py` | `AlertSource` ABC (`count_for_user` + `new_items_for_user`) with two `Temperament`s — **FYI** (count excludes `alert_seen` rows; seeing drains the badge) and **ACTION** (count from work-state; `alert_seen` only gates the one-time pulse). `AlertItem` (ref_id + row anchor); `recency_floor` (rolling `alert_recency_days` window floored at `ALERTS_EPOCH` so the launch backlog never lights up); `record_seen` (idempotent) / `seen_ref_ids`. |
| `registry.py` | tab→sources registry — `register`, `sources_for_tab`, `source_for_kind`, `tab_for_kind`, `count_for_tab` (sum, fail-quiet per source), `markers_for_tab` (per-anchor spotlight markers for the list partials). |
| `sources/` | Concrete sources, registered centrally on import: `OfferConfirmedSource`→`requisitions` (Sales Hub, FYI), `BuyplanActionSource`→`buy-plans` (ACTION), `InboundCustomerSource`→`crm` (FYI). Tab keys match the `mobile_nav.html` nav ids. |

Router `app/routers/alerts.py` (registered in `main.py`): `GET /v2/partials/alerts/{tab_key}/badge` (emerald nav pill, fail-quiet) + `POST /v2/partials/alerts/{kind}/seen` (idempotent; returns the owning tab's refreshed nav badge as an OOB swap). Constants: `AlertKind` StrEnum (`app/constants.py`). Config: `alert_recency_days` (30) + `alerts_epoch` (`app/config.py`). Frontend: emerald count badges in `mobile_nav.html` (Sales Hub / Buy Plans / CRM, polled every 60s — same pattern as Proactive); the shared spotlight module + `.alert-rail`/`.tab-alert-pill` styles in `htmx_app.js` / `styles.css`; rows stamped by the `alert_row_attrs` macro in `partials/shared/_alert_macros.html` (fed by `markers_for_tab` via the parts list, buy_plans list, and CDM account list).

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
| `__init__.py` | `extract_desc` pure router: TRIO `<Label>,` lead / comma-less first token / whole-word body tokens route to a commodity; foreign labels ("Other,"/"Tray,"…), cross-family conflicts, and degenerate descriptions return None. Under a commodity hint, a contradiction guard returns None when the lead OR all strong body tokens belong to a different family than the hint (a motherboard FRU in the SFDC CPU bucket never takes cpu facets) — with a subordinate-vocabulary exemption (`_SUBORDINATE_UNDER`): dram tokens under a cpu hint refine, never contradict (CPU descs state their supported memory). CPU specifics: the `IC,uP` full-prefix gate (bare `IC,` stays the foreign general-components bin), the `PROC,` lead, and SUBORDINATE cpu body tokens (`_CPU_WEAK`: XEON/EPYC/RYZEN/PENTIUM/ATHLON/model strings) that route only when no lead matched and no other body token fired — boards naming their CPUs stay boards. |
| `_common.py` | `DescResult` dataclass + `DESC_SOURCE`/`DESC_CONFIDENCE`/`SPEC_COMMODITIES` constants shared by the router and the writer. |
| `storage.py` | hdd/ssd token grammar: capacity (link-speed tokens excluded), rpm (hdd-only), form factor, interface — per-commodity seeded vocabularies. |
| `memory.py` | DRAM token grammar: capacity, ddr_type, speed_mhz, ecc (incl. Non-ECC negation), form_factor, rank — seeded enums + the only numeric_range gate. |
| `cpu.py` | CPU grammars (wave 3B — steps 0/1/1b of `docs/CPU_DECODE_FEASIBILITY.md`): HP `IC,uP,<codename>,<model>,<GHz>,<W>,<MB>` + `SPS-CPU/SPS-PROC` spares forms (underscore decimals `1_7GHZ`, glued `E52650Lv2`, `Xeon-G/-S/-P/-B`), generic model strings (E3/E5/E7 vN, Scalable, Core iN, EPYC, Ryzen), core-count/GHz/TDP tokens (turbo/"up to" clocks dropped; TDP emits `tdp_watts`, never `wattage`; digit-range/sign markers block glued cores so "0-70C" temp ranges never read as 70 cores), HP codename→architecture map (CFL/KBL/BDW/SKL/HSW/CLX/ICL/SNB) → full names → vN map. Pentium Gold / Athlon Gold-Silver suppress the Scalable metal-word interpretation (no Xeon family/model from "PENTIUM GOLD 7505"); a dangling slash-alternate after a model ("E5-2620 V3/V4", "GOLD 6230R/6240R") expands to a second model so unique-or-omit skips the table merge. `is_cpu_pollution` step-0 deny-list (Murata/Panasonic/EPCOS B32-B88 clusters/AVX/TI/TVS/TE 6-7-digit/StorageTek shapes — the report's false-positive MPN classes, not its full ≥5.6k re-bucket sweep) makes polluted rows return None outright. Curated model→spec table `app/data/cpu_model_specs.json` (~280 entries: E5 v1-v4, Scalable gen1/2, E3/E7, EPYC 7001/7002, Core desktop) fills missing facets, merged UNDER desc tokens; socket is table-only; the drift guard pins every key as parser-reachable and vN-arch-coherent. Bare cores/TDP tokens AND codename-only architecture require a CPU-context signal (MPN-echo descs and "SPS-BASE ENCLOSURE KBL-R" chassis rows emit nothing). |
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
| HTML templates | 197 |
| Database tables | 50+ |
| API endpoints | 400+ |
| Service modules | 120 |
| Supplier connectors | 12 |
| Background jobs | 15 modules |
| Test files | 100+ |
| Alembic migrations | 95+ |
