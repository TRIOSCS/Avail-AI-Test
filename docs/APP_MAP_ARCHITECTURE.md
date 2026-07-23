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
| **db** | PostgreSQL 16 | 2 GB / 1.5 CPU |
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
    │       Edge `@blocked` matcher returns 403 for /metrics, /docs, /redoc,
    │       /openapi.json (Caddyfile). Defense-in-depth on top of the app-layer
    │       gate: FastAPI registers the Swagger/ReDoc/OpenAPI routes only when
    │       `EXPOSE_API_DOCS=true` (settings.expose_api_docs, default False), so
    │       by default those three paths 404 at the app even without Caddy.
    │
    ▼
FastAPI Middleware Stack (in order):
    ├── 1. GZipMiddleware (compress >= 500 bytes)
    ├── 2. SessionMiddleware (HTTP-only cookie, 15-min expiry)
    │       Inner of Session (registered before it so Session is outer): AuditUserMiddleware
    │       (sets current_user_id contextvar) and ModuleAccessMiddleware (per-user MODULE
    │       access chokepoint on module-exclusive HTMX sub-partials — see INTERACTIONS
    │       "Module SUB-partial chokepoint"; reads scope["session"] so Session must run first).
    ├── 3. CSRFMiddleware (double-submit cookie on mutations; exempt set is the
    │       module-level `CSRF_EXEMPT_URLS` in main.py — anchor patterns with `$` since
    │       starlette_csrf matches `re.match(url.path)`. Only auth/health/webhook/read
    │       endpoints and the requisition import PREVIEW are exempt: `import-parse`
    │       (multipart upload, browser form can't add the x-csrftoken header) + the
    │       `import-form` GET. `import-save` (the DB write) is NOT exempt — it stays under
    │       token enforcement like every mutation; htmx supplies the header via the global
    │       `htmx:configRequest` listener in htmx_app.js)
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
    ├── 7. API Version Middleware (/api/v1/* -> /api/*)
    └── 8. SlowAPIMiddleware (INNERMOST — added first, in the `if settings.rate_limit_enabled`
    │       block ~main.py:250, so it wraps closest to the route handler). This is what
    │       actually ENFORCES the per-IP global default `rate_limit_default` (600/min,
    │       config.py) on every route lacking its own `@limiter.limit`; without it the
    │       decorators still work but the default is never applied. key_func is
    │       get_remote_address — correct because uvicorn runs `--proxy-headers
    │       --forwarded-allow-ips` (docker-compose) and fixes scope["client"] to the real
    │       client IP before any Starlette middleware runs. Exempt (`@limiter.exempt`): the
    │       two SSE streams (/api/events/stream, /v2/partials/search/stream — a throttled
    │       stream would be killed) and infra probes (/health, /health/ready, /metrics).
    │       @limiter.limit-decorated routes and static Mounts are auto-exempt. Only present
    │       when rate_limit_enabled (config default True; tests set it False).
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
- **Per-user buy-plan approval right** — `require_buyplan_approver(request, db)` 403s
  unless `User.can_approve_buy_plans` is set (predicate: `can_approve_buy_plans(user)`).
  Role-independent (admins do NOT auto-qualify); the column is the single source of truth,
  admin-toggled in the Users settings tab. Wired on all three layers of the buy-plan
  approve/reject action: the `POST /v2/partials/buy-plans/{id}/approve` route depends on
  `require_buyplan_approver`, the `approve_buy_plan` service re-checks the predicate, and
  the detail/supervise templates hide the approve/reject UI via the `can_approve_buy_plans`
  Jinja global. Reject requires a reason (service-enforced); approve/reject both write a
  `BUYPLAN_APPROVED`/`BUYPLAN_REJECTED` ActivityLog scoped to the plan.
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
  - **Password-login fail-boot guard** — `ENABLE_PASSWORD_LOGIN=true` (the local password
    form, an auth bypass relative to Azure SSO) now **hard-fails boot** on any real
    (non-`TESTING`) start unless the operator acknowledges the risk with
    `ALLOW_PASSWORD_LOGIN_RISK=true`. The guard lives in `app/startup.py`
    (`run_startup_migrations`, before the TESTING short-circuit) and reads `os.getenv` at
    runtime via `auth.password_login_env_enabled` / `auth.password_login_risk_acknowledged`
    (not the import-frozen `settings.*`). With the ack it logs a CRITICAL "bypass
    acknowledged" line and boots; without it, it raises `RuntimeError`. `deploy.sh` mirrors
    this as a Step 1.5 preflight (exit 5) so a missing ack fails fast instead of timing out
    the health check. Staging sets the ack; leave it false everywhere else.

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
- **Design system (single source of truth):** the canonical component layer lives in
  `app/static/styles.css` (`@layer components`: `.card`/`.card-sm`/`.card-lg`, `.btn-*`,
  `.badge*`, `.input`/`.input-focus`, `.h1`–`.h4`, `.table-wrapper`+`.data-table`,
  `.font-data`) + the macro library `partials/shared/_macros.html`. `brand-*` is the neutral
  gray text/surface/border ramp; the single interactive accent is `accent-*` azure
  (`--accent #007DBD`) — focus rings, active nav, and primary buttons use accent, never
  `brand-*`. Page/detail titles render through the `page_header(title, subtitle=None)` macro
  (`.h1` + optional `.text-secondary` + right-aligned action slot via `{% call %}`). Status
  pills route through the shared `badge()`/`status_badge()`/`req_status_badge()`/
  `quote_status_badge()` macros (which preserve semantic status colors) rather than inline
  color-map dicts. Form-control focus rings are the `.input-focus` mixin (accent) app-wide —
  the legacy gray `focus:ring-brand-*` rings are fully retired. `tests/test_design_system_drift.py`
  guards these invariants (page_header present, no gray-brand focus rings anywhere in
  `app/templates/`, canonical classes defined, no dark mode).
- **Lazy-load wrapper:** `lazy_body` macro (`partials/shared/_macros.html`) is the mandated
  wrapper for any faceted/lazy-load sub-container (spinner/skeleton `caller()` block ->
  `hx-get` on `trigger` swapped into an explicit `hx-target`, defaulting to `this`) — used by
  `approvals/approvals_hub.html`, `settings/index.html`,
  `sightings/list.html`, `quotes/detail.html`, `resell/detail.html`, `resell/workspace.html`.
  Never hand-roll an inner `hx-get` container without it.
- **Typeahead is server-rendered, not client-fetch:** the customer picker
  (`requisitions/_customer_typeahead_results.html`) and the vendor search dropdown
  (`sightings/_vendor_search_results.html`) are both plain `hx-get`-debounced partials —
  neither calls `fetch()` client-side (the pre-P5.2 pattern of preloading JSON and filtering
  in Alpine `x-data` is retired for these two pickers). Follow this pattern for any new
  typeahead: render the options list server-side and swap it in via HTMX, never ship data to
  filter in JS.
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

### HTMX view routers (`htmx_views.py` split)

The HTMX/Alpine frontend partials are served by `app/routers/htmx_views.py` (the
historical monolith) plus a growing **per-domain package `app/routers/htmx/`** that it
is being split into one cohesive slice at a time. All sub-routers keep the same `/v2/...`
URL space and the `htmx-views` tag. Most are mounted by `main.py` alongside
`htmx_views_router` (so URLs are unchanged); the final 5 (`my_day.py`, `email_views.py`,
`insights_views.py`, `search_views.py`, `requisitions_edit.py` — see below) are instead
aggregated internally by `htmx_views.py` itself so `main.py` needed zero new mount lines:

- `app/routers/htmx/_shared.py` — shared module-level helpers/state used by both the
  monolith and the sub-routers: the Vite manifest loader (`_vite_manifest`/`_vite_assets`),
  `_base_ctx()` (the common template context), `_parse_date_safe()`, the `_DASH` em-dash
  fallback, the CRM list hx-target/push-url allowlists + `_sanitize_hx_params()` (used
  by both the vendors and companies sub-routers), and the form coercers `_safe_int()`/
  `_safe_float()` + the ops-group check `_is_ops_member()` (each shared between a moved
  cluster and a route that stayed in the monolith). Single source of truth — sub-routers and
  (where still used) `htmx_views.py` import these names so behavior is unchanged.
- `app/routers/htmx/requisitions.py` — **first extracted domain**: the Requisition partials
  (`GET/POST /v2/partials/requisitions/*` list, unified create/import modal + AI parse/save,
  detail shell, requirement add, search-all, detail tabs) plus the AI customer
  lookup/quick-create (`POST /v2/partials/customers/lookup` + `/quick-create`) that the
  create modal uses. `htmx_views.py` re-imports `requisitions_list_partial` /
  `requisition_tab` from here because its offer/response routes re-render those partials.
- `app/routers/htmx/vendors.py` — **CRM-cluster split (vendor slice)**: the vendor + vendor-
  contact partials (`/v2/partials/vendors/*` + `/v2/partials/vendor-contacts`) — vendor list,
  global vendor-contacts list, vendor CRUD, detail shell + tabs, vendor-contact CRUD, vendor
  ownership (claim/release/badge), vendor custom fields, reviews/nudges, and the AI contact
  finder (find/save/promote/delete). `htmx_views.py` re-imports `vendor_tab` (its vendor
  activity add-note route re-renders the Activity tab).
- `app/routers/htmx/companies/` — **CRM-cluster split (company/customer + contact slice)**,
  itself further split into a package (P4.3+) along its audited seams: `core.py` (customers/
  account list, company CRUD, CSV bulk import preview/confirm for companies + contacts, bulk
  actions), `detail.py` (the company detail shell + `company_tab` render path), `contacts.py`
  (Contacts-tab CRUD, bulk actions, suggested-contacts + AI contact-discovery loops, contact
  notes/history/files, contact move), `sites.py` (CustomerSite + site-contacts CRUD, account
  collaborators), `merge.py` (company + contact duplicate merge), `tags.py` (segment/contact
  tags), `custom_fields.py`, `saved_views.py` (filter presets), and `_registries.py` (the
  inline-edit field registry — `apply_company_field`/`apply_contact_field` + `CANONICAL_ROLES`/
  `FIELD_LABELS`). Every submodule imports and decorates the SAME `router` instance created in
  `__init__.py` (byte-for-byte the object every route registers on), so the URL space is
  unchanged (`/v2/partials/customers/*` + `/v2/partials/companies/*` redirects +
  `/v2/partials/contacts/*`). **`__init__.py` re-exports every name the old single-file module
  made patchable/importable** off `app.routers.htmx.companies` — the late-resolution package-
  attribute lookup (`app.routers.htmx.companies.X`) keeps every existing
  `unittest.mock.patch("app.routers.htmx.companies.X")` target working unchanged across the
  split, same as `app.main` importing only `router`. `htmx_views.py` re-imports `company_tab`
  (its company activity add-note route re-renders the Activity tab); tests import
  `_staleness_tier` from here.
- `app/routers/htmx/buy_plans.py` — **deal/sourcing-cluster split (Buy Plans / Approvals slice)**:
  the buy-plan workflow routes the Approvals Workspace drives —
  `/v2/partials/buy-plans/sales-orders/new|create` (origination, now SELF-HOSTED in
  `#so-origination`/`#main-content`), the prepay decide (`/prepay-requests/{id}/decide`),
  the single-plan detail (`/v2/partials/buy-plans/{id}`), and the per-plan lifecycle actions
  submit/approve/halt/receive/confirm-po/resource/reject-received/claim/verify-po/issue/cancel/reset
  (`origin=approvals_workspace` re-renders the pane in place + fires `awListRefresh`;
  `origin=approvals_hub` re-renders the workspace tab body; anything else falls through to
  the detail partial). The old **Buy Plans hub retired post-parity** (spec §11.1;
  `docs/APPROVALS_PARITY_CHECKLIST.md`): `GET /v2/partials/buy-plans` (and its
  `?new=1` origination entry), `/v2/partials/buy-plans/{my-queue,pipeline}` and
  `/v2/partials/buy-plans/pipeline-archive` are now **308 redirects** onto the workspace
  equivalents (`/v2/partials/approvals?tab=buy-plans`, the picker partial,
  `/v2/partials/approvals/buy-plans[?scope=]`, `/v2/partials/approvals/buy-plans/list?show_closed=true`);
  unknown lens values still 404. Owns the cluster-private helpers
  `_can_supervise`/`_can_resource`/`_require_po_cutter` and `_PO_CUTTER_ROLES`. Imports
  `_is_ops_member` from `_shared` (a quotes route in the monolith still uses
  it). **Trap:** the `settings/ops-group|users` routes are interleaved in the source
  between `buy_plan_cancel` and `buy_plan_reset` but belong to the settings domain — they now
  live in `app/routers/htmx/settings.py`.
- `app/routers/htmx/offers/` — **deal/sourcing-cluster split (offer/RFQ/follow-up slice)**,
  itself further split into a package (P4.3+) along its audited seams: `crud.py` (AI offer
  parsing — `/v2/partials/requisitions/{id}/parse-email|paste-offer|parse-offer|
  save-parsed-offers` — plus offer CRUD + review/promote/reject/changelog
  (`/v2/partials/offers/*`) and quote-from-offers), `rfq.py` (RFQ compose form, AI cleanup/
  rephrase, RFQ send, `rfq_prepare_panel`), `follow_ups.py` (follow-up queue
  list/send/ai-draft/batch/badge), and `replies.py` (vendor-response review/reply, manual
  activity/phone-call logging). `__init__.py` builds its own `router` and `include_router()`s
  all four sub-routers so `app/main.py` keeps mounting a single `htmx_offers_router` with no
  change. **Test-patch note:** `template_response`/`requisition_tab`/`maybe_release_on_offer`/
  `offer_review_queue` are re-exported at package level, but every sub-module call site
  re-pulls them via a FUNCTION-LOCAL `from . import X` (never a module-level import) so
  `patch("app.routers.htmx.offers.X")` still intercepts every call site post-split — a
  module-level import would bind the pre-patch object permanently at import time. Imports
  `requisition_tab` from `requisitions` (every offer route re-renders the requisition
  offers/responses tab) and `_safe_int`/`_safe_float` from `_shared`. **Trap:** the interleaved
  requisition-management routes (bulk action, inline edit/win-prob/opp-value/row-action,
  delete/update requirement, poll-inbox) are NOT offers — they live in
  `app/routers/htmx/requisitions_edit.py` (see below).
- `app/routers/htmx/sourcing.py` — **deal/sourcing-cluster split (sourcing-engine slice)**:
  the self-contained sourcing surface (`/v2/sourcing/*` pages + `/v2/partials/sourcing/*`) —
  results page/stream, manual search trigger, lead detail/status/feedback, and the split-panel
  workspace (page, list, lead panel).
- `app/routers/htmx/quotes.py` — **tail split (quote slice)**: the quote partials
  (`/v2/partials/quotes/*` preview/delete/reopen/edit-metadata, recent-terms,
  `/v2/partials/pricing-history/{mpn}`, the quote detail panel + quote-line CRUD, add-offer,
  send/result/revise/apply-markup, `/v2/partials/requisitions/{id}/add-offers-to-quote`, and
  build-buy-plan-from-quote).
- `app/routers/htmx/prospecting.py` — **tail split (prospecting slice)**: the prospect list/grid,
  stats, add-domain, detail panel, claim/dismiss/release/enrich + enrich-status poller, and the
  manager-only `/v2/partials/prospects/{id}/assign[-form]` action (rep picker → assigns the account
  to a chosen rep; the O-rework successor to the retired reclaim/reassign controls). Owns the
  prospect-context helpers
  (`_prospect_card_ctx`/`_prospect_detail_ctx`/`_prospect_stats_ctx`/`_prospect_action_response`/
  `_status_visible_under_filter`/`_wants_detail`/`_enrich_is_stale`/`_prospect_toast`).
- `app/routers/htmx/settings.py` — **tail split (settings/ops/user-mgmt slice)**: the
  `settings/ops-group|users|sources|system|profile|data-ops|connectors|api-keys` tabs,
  inbox scan-now, the `/api/user/*` toggle endpoints, connector test-all + card, and the CRM
  vendor/company merge + dedup admin actions (`/v2/partials/admin/*`) plus the admin api-health
  partial (real per-connector rows assembled by `app/services/connector_health.py`) +
  data-ops partials. Owns `settings_toast` (re-imported by `routers/sources.py` and
  `routers/admin/buy_plan_ops.py`) and `_run_inbox_scan_now` (imported by
  `app/routers/htmx/requisitions_edit.py` because its poll-inbox route calls it).
- `app/routers/htmx/materials.py` — **tail split (materials-partials slice; distinct from the
  domain router `app/routers/materials.py`)**: the faceted list + filter sidebars
  (manufacturers/global/tree/sub), manufacturer search/add, AI interpret, faceted results,
  add-form, enrich-status poller, conflict-accept, FRU lookup, the material detail panel + tabs,
  card update, and the enrich/find-crosses/insights actions. Owns the shared faceted-filter param
  parsers (`_parse_filter_json`/`_pop_manufacturers`/`_parse_card_filter_params`).
- `app/routers/htmx/proactive.py` — **tail split (proactive slice)**: the proactive part-match
  list, refresh/scan, batch-dismiss, the prepare page + draft + send flow (`/v2/proactive/*`),
  the inline add-contact affordance on Prepare
  (`POST /v2/partials/proactive/prepare/{site_id}/add-contact` → re-renders `_contact_picker.html`
  into `#proactive-contact-list`, auto-selecting the new contact so Send unblocks — PROACTIVE-04),
  the legacy send/convert routes, scorecard, badge, and do-not-offer.
- `app/routers/htmx/parts.py` — **tail split (parts-workspace body slice)**: the parts list, the
  detail tabs (offers/sourcing/req-details/quotes/activity/comms/notes), the header + inline cell +
  spec editors, notes save, per-part tasks, and the part archive/unarchive (single + bulk) actions.
  **Trap:** the workspace SHELL entry (`GET /v2/partials/parts/workspace`) stays in `htmx_views.py`.
- `app/routers/htmx/archive.py` — **tail split (tasks/tickets lifecycle slice)**: trouble-ticket
  workspace/list/detail, account + contact + vendor tasks (add-form/create/list), task
  complete/delete/edit/snooze, and the account/contact + vendor activity add-note forms. Imports
  `company_tab`/`vendor_tab` (its activity add-note routes re-render those tabs); `htmx_views.py`
  re-imports `_build_ticket_list_context` for `error_reports.analyze_tickets`.
- `app/routers/htmx/my_day.py` — **P4.3 final split (My Day / Tasks slice)**: the Tasks
  page (`GET /v2/partials/my-day` + filter-bar re-render) plus create/snooze/reopen
  mutations. Owns `_my_day_filtered_tasks`/`_my_day_results_response`; imports
  `_coerce_task_priority` from `requisitions.py`.
- `app/routers/htmx/email_views.py` — **P4.3 final split (email integration slice)**:
  the Sprint 7 email thread viewer + AI summary, reply send (with the DNC hard-block),
  and the email-intelligence dashboard partial.
- `app/routers/htmx/insights_views.py` — **P4.3 final split (AI insights/knowledge/
  dashboard slice)**: the Phase 6 AI-insights panels (requisitions/vendors/customers/
  dashboard) + their refresh actions, the AI activity-digest cards
  (requisition + customer), the top-level dashboard stats partial, and the Sprint 9
  knowledge-base list/create routes. Moved verbatim — this is the surface P0.1/P2.8
  most recently touched, so behavior must stay byte-for-byte identical.
- `app/routers/htmx/search_views.py` — **P4.3 final split (search slice)**: global
  type-ahead + AI search + full results page, the Part Dossier search-form entry point
  + "what we know" history panel, the streaming MPN search (`search/run` + SSE
  `search/stream` + `search/filter` + `search/lead-detail`), and the requisition-picker
  "add shortlisted results to a requisition" flow. Owns `_get_enabled_sources` /
  `_get_cached_search_results` (Redis-backed search-result cache reads).
- `app/routers/htmx/requisitions_edit.py` — **P4.3 final split (requisition bulk +
  inline-edit slice)**: the requisitions-list bulk action (owner reassign), inline
  cell edit + save (name/status/urgency/deadline/owner), win-probability +
  opportunity-value inline edits, row-level actions (claim/unclaim/won/lost/clone),
  the inbox-poll trigger, and the requirement delete/update endpoints. Imports
  `_best_quote_status`/`requisitions_list_partial` from `requisitions.py` and
  `_run_inbox_scan_now` from `settings.py`.

After this final split, `htmx_views.py` retains only the cross-cutting surface: the
full-page entry points (`v2_page` + `/v2/quotes` redirect), the parts-workspace shell
(`GET /v2/partials/parts/workspace`), the vendor stock-list import
(`/v2/partials/vendors/import-stock`), and the shared nav/access-gate constants
(`_NAV_ID_ALIAS`, `_VIEW_ACCESS`, `_MODULE_ENTRY_URLS`). `v2_page` also owns the canonical
full-page URLs for the two buyer queues that have **no bottom-nav tab** — `/v2/follow-ups`
(→ `/v2/partials/follow-ups`) and `/v2/offers/review-queue` (→ `/v2/partials/offers/review-queue`,
`offers` view segment) — both surfaced via the **Sightings workspace quick-links** bar
(`sightings/list.html`; `sightings_workspace` computes the pending-review + follow-up counts
once, off the table-refresh path). The retired Buy Plans hub URLs (`/v2/buy-plans[/{id}]`)
**308-redirect** to `/v2/approvals?tab=buy-plans` — a detail deep link adds
`&select={id}` so the workspace preselects that plan's pane (spec §11.1 retirement;
`docs/APPROVALS_PARITY_CHECKLIST.md`).
It no longer defines any
`/v2/partials/*` route directly for search, email, insights/knowledge/dashboard, My Day,
or requisition bulk/inline-edit — those now live in the 5 modules above.

**Registration pattern for this final split** differs from every prior domain above:
instead of `main.py` importing and mounting each new sub-router individually (which
would require touching `main.py`), `htmx_views.py` imports the 5 new routers and
aggregates them into its own exported `router` via `router.include_router(...)` at
module load time. `main.py` keeps its single, unchanged
`app.include_router(htmx_views_router)` line — route registration is identical, only
internally re-composed. `htmx_views.py` also re-imports every name tests
patch/import directly at `app.routers.htmx_views.X` (`_get_cached_search_results`,
`_get_enabled_sources`, `add_to_requisition`, `requisition_picker`, `search_filter`,
`search_run`, `send_email_reply`, `update_requirement`) so those call sites keep
resolving; a few `unittest.mock.patch("app.routers.htmx_views.X")` targets that
actually intercept a collaborator called *from inside* the moved function (not just
imported for re-export) were repointed to `app.routers.htmx.search_views.X` /
`app.routers.htmx.requisitions_edit.X` so the patch still takes effect post-split.

When extracting a domain: move the cohesive route block verbatim into a new
`app/routers/htmx/<domain>.py` with its own `APIRouter(tags=["htmx-views"])`, pull any
genuinely cross-cutting helpers into `_shared.py` (re-imported back into the monolith), and
register the sub-router in `main.py` (or, if `main.py` must stay untouched, aggregate it
into the parent module's `router` via `include_router()` as `htmx_views.py` does above).
Verify route parity (dump `app.routes` method+path, sort, diff — must be empty) before and
after.

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
| Requisitions | 32 | partials/requisitions/ — incl. `_customer_typeahead_results.html` (server-rendered debounced customer-picker dropdown, `GET /v2/partials/requisitions/customer-typeahead`, swapped into `#customer-typeahead-results` inside `unified_modal.html`'s `customerPicker()` Alpine scope) |
| Vendors | 16 | partials/vendors/ |
| Customers | 14 | partials/customers/ |
| Materials | 13 | partials/materials/ |
| Resell | 11 | partials/resell/ — resell-brokerage workspace (replaced the removed `partials/excess/`; router `routers/resell.py`) |
| Parts | 13 | partials/parts/ |
| Quotes | 5 | partials/quotes/ — `list.html` removed (standalone Quotes tab retired); detail/macros/line_row/preview/pricing_history remain |
| Sightings | 7 | partials/sightings/ — incl. `_vendor_search_results.html` ("Find any vendor" server-rendered debounced dropdown, `GET /v2/partials/sightings/vendor-search`, swapped into `#vendor-search-results` inside `vendor_modal.html`'s `rfqVendorModal` Alpine scope). `list.html` (the workspace shell) carries a top **quick-links** bar with count-badged entry points to the offer-review queue + follow-up queue (both nav-swap `#main-content`, pushing their canonical URLs). |
| Search | 13 | partials/search/ — incl. the Part Dossier ("Bench") at `/v2/search?mpn=`: `dossier_shell/hero/specs/recent/market.html` (routes in `routers/part_dossier.py`). |
| Prospecting | 8 | partials/prospecting/ — list/_card/_macros/detail/stats/add_result/enrich_status/_action_oob; buyer-ready ranking via `services/prospect_priority.build_priority_snapshot` (single source of truth); background enrich polls `/enrich-status` (HTTP 286 stops); grid actions OOB-remove cards + refresh `#prospect-stats` |
| Proactive | 4 | partials/proactive/ |
| Emails | 4 | partials/emails/ |
| Tickets | 4 | partials/tickets/ |
| Settings | 9 | partials/settings/ — tabs: **Connectors** (unified, replaces Sources + API Keys; admin-only), Profile, System, Data Ops, **Data export** (capability-gated on `EXPORT_BULK_DATA` — admin-by-default, per-user manager override possible; ISS-028 — the five bulk dataset CSV exports, `data_export.html`, `GET /v2/partials/settings/data-export`; see Authorization & Access Control's Bulk dataset export row), Ops Group, **Users** (admin-only); legacy `/sources` + `/api-keys` routes 302 → Connectors. Users tab = `users.html` (invite/role/activate table) + `user_access_panel.html` (per-user access editor modal) + `users_audit.html` (audit-log viewer); see Authorization & Access Control. |
| Shared | 18 | partials/shared/ |
| Approvals | 12 | partials/buy_plans/ + partials/approvals/ — the **Approvals module**, ONE surface: the **Approvals Workspace** at `/v2/approvals` (4-tab split view — Sales Orders / Buy Plans / Purchase Orders / Prepayments; `routers/htmx/approvals_hub.py`, see the Approvals Workspace section). The old two-lens Buy Plans hub (My Queue + Pipeline) **RETIRED post-parity** (spec §11.1; `docs/APPROVALS_PARITY_CHECKLIST.md`): `buy_plans/hub.html`, `approvals/_surface_my_queue.html`, `approvals/_surface_pipeline.html`, `approvals/_pipeline_macros.html` and `approvals/_pipeline_archive_rows.html` are deleted, `/v2/buy-plans[/{id}]` and the hub partial URLs 308 onto the workspace, and the hub read models (`my_queue`/`QueueRow`, `deals_board`, `completed_archive`, `open_avg_margin`, `supervise_overview`, the line queues) are deleted — `services/buyplan_hub.py` survives only as the shared helpers (`_customer_name`, `_age_hours`, `_line_mpn`, `_query_po_pending_verify`) the workspace PO queue (`services/approvals/po_queue.py`) imports. Origination (`_sales_order_new.html`, self-hosted in `#so-origination`) and the single-plan `detail.html`/`_macros.html` stay under `routers/htmx/buy_plans.py`. The write-side state machine, `services/buyplan_workflow.py` (1,855 lines), is now a **package** (P4.3+): `buyplan_approval.py` (submit/approve/reject, halt/resume, reset/cancel/resubmit, `check_completion`), `buyplan_po.py` (buyer PO confirmation + approver PO verification + `mark_line_received`), `buyplan_lines.py` (claim/flag/resolve/resource + the line-editing API), and `buyplan_reports.py` (favoritism detection + case-report generation) — `__init__.py` re-exports every public AND internal name reached via `app.services.buyplan_workflow.<name>` so no caller/test needs to change its import path. The retired `/v2/reporting` page folded its analytics in here + the Sales Hub pipeline chip + the CRM coverage chip — `partials/reporting/` and the `reporting_dashboard` route are gone. |

### Shared Template Components

| Component | File | Purpose |
|-----------|------|---------|
| `_mpn_chips.html` | partials/shared/ | Renders all MPNs (primary + substitutes) as equal inline pill chips with overflow toggle; clickable chips open material card modal when a `link_map` entry exists |
| `status_badge` macro | partials/shared/_macros.html | Unified status badge rendering used by all pages (requisitions, sightings, parts, etc.). Thin wrappers over it apply entity-specific color maps: `req_status_badge` (Requisition.status), `quote_status_badge` (Quote.status + RFQ Contact.status — one canonical map so "sent" is brand everywhere), `account_type_badge` (Company.account_type). |
| `activity_icon` / `activity_row` macros | partials/shared/_macros.html | Canonical activity-timeline icon + row. Every entity timeline (requisitions, parts, sightings, vendors, customers) renders through these — the customer activity tab calls `activity_icon` directly rather than re-declaring an inline icon map. |
| `cadence_hero` / `cadence_clocks` macros | partials/shared/_macros.html | Shared cadence card. `cadence_clocks(entity, now_utc)` is the dual-clock (Last Out / Last Reply) render used by both `cadence_hero` (vendor) and the customer Account Cadence card in `customers/detail.html` + `customers/header.html`. |
| `suggested_contact_row` macro | partials/shared/_contact_row.html | Single source of truth for a discovered/suggested contact card + Add form. Consumed by the Contacts-tab `customers/tabs/_suggested_contacts.html` (Add → `#contacts-tab-list`, innerHTML) and the Enrich result panel `shared/_enrich_result.html` (Add → `closest li`, outerHTML, `from_enrich=1`). Target/swap are macro params. |
| `_enrich_result.html` | partials/shared/ | Result panel swapped into `#enrich-results-{id}` by `enrich_company`/`enrich_vendor_card` (HTMX path). Firmographics grid (Updated/Current pills) + source/freshness + discovered contacts (companies). Replaced the old raw-JSON dump. |
| `list.html` | partials/customers/ | CDM account workspace: split-panel layout (left = scrollable account list, right = `#cdm-detail`), resizable divider via the `splitPanel` Alpine component (panel id `cdm`). Modeled on the requisitions2 workspace. **Responsive (Wave 7):** the `#split-cdm`/`#split-rq2` containers are `flex flex-col md:flex-row` — panes stack full-width on phones (`w-full md:w-auto`, drag divider `hidden md:block`) and restore the side-by-side split at `md:`+ . The inline `:style` width binding is guarded by `window.innerWidth >= 768` so `w-full` governs on phones without `!important` (mirrors `sightings/list.html`). |
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
| `seed_sample_data.py` | `ALLOW_SAMPLE_DATA_SEED=true python -m app.management.seed_sample_data [--owner EMAIL]` / `python -m app.management.seed_sample_data --wipe` | Populate staging with a realistic, interconnected sample dataset (companies/contacts/vendors, requisitions+requirements, offers across statuses, quotes incl. revised/won + chosen offers, buy plans, resell/excess lists with competing per-line + take-all broker offers and a customer bid-back, sightings, dated activities, account/contact tasks, outreach + buyer scores, material cards via the F1 ladder) so every workflow can be exercised end-to-end. **Production guard:** seeding REFUSES to run (loud non-zero exit, zero rows written, no DB session opened) unless `ALLOW_SAMPLE_DATA_SEED=true` is explicitly set — so a stray invocation can never inject demo data into the real production DB; `--wipe` is exempt (it only deletes tagged sample rows). Idempotent-additive (re-run creates 0 rows; get-or-create on natural keys), every sample row carries the `AVSAMPLE`/`avsample` marker, and `--wipe` deletes ONLY tagged sample rows (FK-safe) — never real data. `--owner EMAIL` assigns the deals to that user (redirecting the seeder/sales/buyer/trader roles) so they show in that user's own-work scopes (the Approvals workspace Mine lists, resell "Open to Me"); re-owning needs `--wipe` first (rows are never UPDATEd), and an unknown email pre-provisions a real, never-wiped account. ORM-only, zero outbound effects. |

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

## Other Notable Service Modules

Single-file services worth flagging individually (not grouped under a shared package):

| Module | Purpose |
|--------|---------|
| `app/services/pricing_history.py` | One preload query over recent Quotes (sent/won/lost — a quote only counts as a real market price once it left draft) building an MPN/`material_card_id` -> last-quoted-price lookup dict. Seeds the smart default sell price on Build-Quote tab / builder-modal lines. Called by `app.services.quote_builder_service` + `app.routers.crm._helpers` (re-exported for the Quote-detail and Quote-list pricing-history panels). |
| `app/services/vendor_reachability.py` | Two batched (no N+1) "can we actually reach this vendor/buyer card" gates — `cards_with_resolvable_email` (non-empty VendorContact email) and `dnc_emails_for_cards` (which of those are Do-Not-Contact flagged) — mirroring the RFQ/offer send-path contact resolution exactly. Advisory only (TOCTOU: the send path itself is the authoritative skip). Called by `app.routers.sightings` (vendor coverage modal, RFQ preview/send) and `app.services.buyer_affinity_service` (resell who-to-offer ranking). |
| `app/services/company_import_service.py` | CSV bulk import for companies + contacts: parses into a status-flagged preview (no writes), then creates rows from the confirmed payload, deduped by normalized name/website/email, with authz-aware row flags for non-manager reps. Called by `app.routers.htmx.companies.core` (`import_companies_preview/confirm`, `import_contacts_preview/confirm`). |
| `app/services/connector_registry.py` | Central connector-metadata registry (`get_connector_for_source`, `source_has_test_path`) — P4.1 extracted this out of `routers/sources.py` so `health_monitor.py` can resolve a connector's test path without importing the router; `routers/sources.py` still imports it back under its original private name for its own Test-button call site. See APP_MAP_INTERACTIONS.md § 9a (Settings → Connectors Tab) "Testability & Test-all concurrency" for the full testability contract. |
| `app/services/connector_health.py` | Connector-health dashboard assembly: `get_health_dashboard(db)` builds per-connector rows (name, effective status, last success/error, search + 24h error counts, avg response ms) from the `api_sources` telemetry columns that `health_monitor` and the search path maintain, plus an active-sources overall roll-up (`down` requires an ERROR-status active source with none live; an all-degraded fleet — heuristic-degraded but still serving traffic — reports `degraded`). `effective_status(src)` is the single home of the auto-degrade heuristic (>=4 errors in 24h and more failures than successes → `degraded`), shared with the `/api/admin/connector-health` and `/api/admin/api-health/dashboard` JSON endpoints so all admin surfaces agree about the same connector. Called by `app.routers.htmx.settings.admin_api_health` (renders `htmx/partials/admin/api_health.html`, lazy-loaded by the settings System tab) and `app.routers.admin.system` (`api_connector_health`, `api_health_dashboard`). The route previously imported this module before it existed (missing since the Sprint 8-10 commit) and always fell back to an empty dashboard. |

## Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `backfill_oem_enrichment.py` | Dry-run-first backfill over `not_found` / `not_catalogued` cards through the OEM tiers. Writes a coverage CSV; rolls back unless `--commit`. Shared `web_meter` budget cap (`--max-web-calls`, default 300) halts mid-run to prevent API overspend. The paced worker drains any remainder. |
| `backup.sh` / `restore.sh` | `pg_dump` custom-format backups (verify → optional encrypt → checksum → rotate) and their restore. Optional at-rest encryption: when `BACKUP_GPG_PASSPHRASE` is set, the verified `.gz` is gpg-symmetric AES256-encrypted (`.dump.gz.gpg`) and the plaintext removed; unset → plaintext with a loud warning. The passphrase is fed via **stdin** (`--passphrase-fd 0`), never argv, so it never appears in `ps`. `restore.sh` transparently decrypts `.gpg` backups (needs the same passphrase) and encrypts its pre-restore safety dump too. (Repo-root `backup.sh` is the equivalent host-cron variant.) |
| `backup-to-spaces.sh` | Off-site upload of the latest backup to DigitalOcean Spaces. Requests server-side encryption (`SPACES_SSE`, default `AES256`; set `SPACES_SSE=none` to disable) — defence-in-depth on top of the optional gpg-at-rest encryption. Skips cleanly when `DO_SPACES_*` is unset. |
| `verify-backup.sh` | P1.5: scheduled verification that the newest backup is actually restorable — runs `restore.sh --verify` (checksum + `pg_restore --list` sanity check, read-only) against the `LATEST` marker inside the `db-backup` container. Exits nonzero with a clear message on failure so a corrupt backup can't sit unnoticed for the full 30-day retention. Installed as a weekly (Sun 04:00) systemd timer — units in `scripts/systemd/avail-backup-verify.{service,timer}`; install one-liner is in the script's own header comment. A failing run's `OnFailure=` fires `avail-backup-verify-alert.service`, which runs `backup-verify-alert.sh`: self-contained (no host mail/SMTP convention exists) — `systemd-cat -p err` + a `wall` broadcast + a durable `/root/backups/VERIFY_FAILED` marker that `deploy.sh`'s final step checks and re-surfaces on every deploy until manually cleared (`rm -f /root/backups/VERIFY_FAILED`). |

## CI & DevOps

- **CI (`.github/workflows/ci.yml`)** — per-PR runs the cheap, high-value Alembic
  checks: single-head assertion, fresh `upgrade head` + schema-drift gate, and a
  single-step `downgrade -1` reversibility check on the new migration. The
  expensive full chain-replay (`downgrade base → upgrade head` over all
  migrations, needs `ALEMBIC_ALLOW_CASCADE`) runs **nightly** via the `schedule`
  trigger as the `migration-full-cycle` job, not on every PR (HIGH-DEVOPS-7).
- **Deploy (`deploy.sh`)** — the commit step stages with `git add -u`
  (tracked-file modifications/deletions only), never untracked files, so a stray
  secret/key/DB dump can't be swept into a deploy commit (CRIT-DEVOPS-2); brand-new
  files must be `git add`ed deliberately first. A sensitive-path denylist is the
  defence-in-depth backstop.

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

---

## Approvals Workspace (spec v4 rebuild — Phases 0–1)

`/v2/approvals` is now the **Approvals Workspace**: one page, four tabs — **Sales
Orders · Buy Plans · Purchase Orders · Prepayments** — four lenses on the same
pipeline rooted at the sales order (`specs/approvals-workspace.md`). The 3-tab decide
console was rebuilt **in place** (D12); legacy tab keys (`buy-plan` / `po-approval` /
`prepayment`) alias onto the new tabs (`LEGACY_TAB_ALIASES`), so old pushed URLs and
the `origin=approvals_hub` decide re-renders keep working. The **approvals engine is
untouched** — every decision posts the existing `buy_plans.py` / `prepayments.py`
routes; new `origin=approvals_workspace` branches re-render the deciding pane in place
and fire `awListRefresh` so the left list repaints. The `/v2/buy-plans` personal hub
**retired post-Phase-3 parity** (spec §11.1; `docs/APPROVALS_PARITY_CHECKLIST.md`):
all its full-page and partial URLs 308 onto the workspace.

### Router — `app/routers/htmx/approvals_hub.py` (rebuilt)

- `GET /v2/partials/approvals` — shell (`require_access(BUY_PLANS)`): 4 pills with
  **per-viewer badges** (`_viewer_badges` — decidable engine requests per gate;
  the PO badge adds verifiable PENDING_VERIFY lines + the viewer's own AWAITING_PO
  lines) + lazy tab body.
- `GET /v2/partials/approvals/{tab}` → `render_tab_body` → `_workspace_split.html`
  (drag-resizable split view; panes **stack below `md`**; `aw-select`/`aw-default`
  selection events; the list container listens for `awListRefresh from:body`).
- `GET /v2/partials/approvals/{tab}/list?q&scope&show_closed` →
  `_workspace_list.html`: debounced search, Mine/All, Live/Closed filter, age chip +
  SO#/PO# **copy chips** on every row, **"Needs your approval" grouped first** with
  the oldest decision default-selected (`aw-default`, applied only when nothing is
  selected). Read models: `buy_plan_tracking_rows` (now carrying the sanctioned
  read-side `order_type`), `build_po_queue_view`, `pending/resolved_rows_for_gate`.
- Panes: `GET /plan/{id}/pane?lens=` (`_pane_sales_order.html` — one anatomy for both
  SO/BP lenses), `GET /po/{line_id}/pane` (`_pane_po_line.html` — buyer confirm-PO
  form vs manager decide), `GET /po/{line_id}/sent-check` (**display-only**
  `verify_po_sent` detection — never auto-verifies), `GET /prepayments/{id}/pane`
  (`_pane_prepayment.html`) and `POST /prepayments/{id}/method` (approver-only,
  REQUESTED-only, stale-guarded, field-audited method adjust).
- `PO_DECISION_LABELS` — spec §5 display vocabulary (`pending_verify` → "Pending
  approval", `verified` → "Approved"); display map only, backend names unchanged.
- CSV export retained per tab (legacy keys alias).

### Templates — `partials/approvals/`

`approvals_hub.html` (4-pill shell) · `_workspace_split.html` · `_workspace_list.html`
· `_pane_sales_order.html` · `_pane_po_line.html` · `_pane_prepayment.html` ·
`_sales_order_new.html` (order-type select + lite branch). The old
`_tab_buy_plan/_tab_po_approval/_tab_prepayment.html` are **deleted** (the
`scope_toggle` macro retired with them). Shared atoms (Phase 0): `copy_chip` +
`age_chip` in `shared/_macros.html`.

### Services (Phase 0 foundations + Phase 1)

- `app/services/field_audit.py` — who/field/old→new audit rows: `diff_fields`,
  `log_field_edits` (ONE batched `FIELD_EDIT` ActivityLog row per save,
  `details={"edits": [...]}`), `edits_since`, `manager_edited_line_ids`.
- `app/services/stale_guard.py` — optimistic-concurrency guard: `stale_token`
  (Jinja global), `ensure_not_stale`, `stale_conflict_response` (non-destructive 409
  + toast).
- `app/services/qp_workspace.py` — `apply_qp_purchasing`: folds the confirm-PO form's
  QP-purchasing answers (incl. AS9120B) onto the **(plan, vendor)** QualityPlan row
  (D11; find-or-create, whitelisted columns, explicit yes/no booleans, blanks never
  clear, returns the FieldEdit diff); `qp_for_line` read helper.
- `app/services/buyplan_builder.py` — `create_sales_order_from_offers` gains
  keyword-only `order_type` (sourcing types only); NEW `create_lite_sales_order`
  (zero-line DRAFT plan for Stock Sale / Testing Service / Comps — the **lite path**:
  approve goes ACTIVE, generates zero buyer tasks, never auto-completes).
- `app/services/buyplan_workflow/buyplan_po.py` — `confirm_po` gains keyword-only
  `payment_method` (validated against `PO_LINE_PAYMENT_METHODS`).
- `app/routers/prepayments.py` — request-modal methods derive from
  `PREPAYMENT_METHODS` (ACH in, **COD never**); router-level COD guard (friendly 400)
  before `create_prepayment` on both HTMX + JSON creates (`prepayment_service.py`
  untouched).

### Phase 2 — editing layer

- **Stale guard + field audit on every edit route** (`routers/htmx/buy_plans.py`):
  `/so-number`, `/lines/add`, `/lines/{id}/edit`, `/lines/{id}/remove`,
  `/lines/bulk`, `confirm-po` all round-trip `expected_updated_at` (narrowest-object
  token: plan for so-number/add/bulk, line for edit/remove/confirm-po) →
  `ensure_not_stale` → non-destructive 409. Line diffs are computed inside
  `_apply_line_edit`'s return (`buyplan_workflow/buyplan_lines.py`) and logged at
  service depth — ONE `FIELD_EDIT` row per save; bulk batches every touched line
  (edits/adds/removals) into one row with per-edit `line_id` attribution
  (`FieldEdit.line_id`). confirm-po merges the line's PO fields with the
  QP-purchasing diff into one row.
- **QP-sales editing**: `POST /v2/partials/approvals/plan/{id}/qp-sales`
  (`approvals_hub.py`) → `qp_workspace.apply_qp_sales` (+ `can_edit_qp_sales`
  matrix: draft → owner/manager, pending → MANAGER only, else locked;
  `qp_sales_row` read helper). Inline display→edit→save editor on
  `_pane_sales_order.html`.
- **Two-part approve** (`buy_plan_approve_partial`): `handoff=proceed|send_back` —
  proceed → existing approve + `write_in_app` change summary to the submitter
  (`field_audit.format_change_summary` over `edits_since(plan, submitted_at)`);
  send_back → existing reject→draft, blank note auto-fills `SEND_BACK_DEFAULT_NOTE`.
  `_change_summary.html` renders "was X → now Y" in the approval block. Every
  reject/send-back also lands its note as a decision-tagged NOTE row
  (`workspace_notes.add_note`, `details={"decision": "rejected"|"sent_back"}`) +
  in-app notification to the fixer (submitter / line buyer / prepay requester).
- **Manager edit-anything at verify** (`_manager_verify_override` in
  `buyplan_lines.py`): MANAGER/ADMIN on a PENDING_VERIFY line may change quantity
  (cut-PO refusal relaxed) plus `po_number` / `estimated_ship_date` / `unit_cost`
  (new `edit_buy_plan_line` kwargs). Vendor stays offer-swap-only for everyone; the
  bulk editor keeps the strict guards. `_pane_po_line.html` manager edit form +
  "Edits here do not change Acctivate" warning + "Edited by manager" marker
  (`manager_edited_line_ids`).
- **Notes + attachments** (`app/services/workspace_notes.py`: `add_note` /
  `notes_thread` / `note_counts` — narrowest-subject scoping; never status-locked).
  Routes in `approvals_hub.py`: `POST /v2/partials/approvals/notes`,
  `POST /v2/partials/approvals/attachments` (multipart → shared `store_and_attach`
  with `BuyPlanAttachment` + subject fk_field; `validate_subject()`; ATTACH_ADDED),
  `DELETE /v2/partials/approvals/attachments/{id}` (uploader or manager;
  ATTACH_REMOVED). `_notes_thread.html` embedded in all three panes.
- **Lifecycle controls on the pane**: manager-only halt/resume/cancel/reset block on
  `_pane_sales_order.html` posting the existing `buy_plans.py` routes with
  `origin=approvals_workspace` (shared `_workspace_pane_response`).
  `plan_needs_approver_reason` stall warnings on BP-tab list rows
  (`WorkspaceRow.stalled`) and on the pane.

### Phase 3 — PO kanban

- `app/services/kanban_lanes.py` (NEW — deliberately NOT under `services/approvals/`;
  the engine stays untouched): `kanban_lane` (pure per-line lane placement with the
  spec-§6 precedence — cancelled hidden; resourcing > received > paid-risk (COD
  excluded, outranks verified) > approved > pending approval > awaiting PO),
  `build_kanban` (the whole board as `KanbanLaneView`/`KanbanCard` DTOs —
  batch-resolved prepay badge amount/payee/paid_at, per-lane age anchors,
  edited-by-manager marker, note/file counts, line N of M + partial-ship,
  `can_receive`), `LANE_ORDER` / `LANE_LABELS`.
- `app/services/buyplan_workflow/buyplan_po.py` — NEW additive `mark_line_received`
  (buyer/manager/admin; VERIFIED or paid-risk; idempotent; stamps
  `received_at`/`received_by_id`; `LINE_RECEIVED` activity; no plan-status changes)
  + route `POST /v2/partials/buy-plans/{plan}/lines/{line}/receive`
  (`routers/htmx/buy_plans.py`).
- `app/templates/htmx/partials/approvals/_pane_kanban.html` (NEW) — replaces the
  Phase-1 placeholder in `_pane_sales_order.html` (ACTIVE/INBOUND sourcing orders
  only): retired-Pipeline-style column grid, deal_card-shaped cards, risk
  lane amber-tinted with amount+payee on the face + paid_at aging (3d/7d), claim
  button on Re-sourcing cards, Mark received on eligible cards, **no drag** — card
  tap `hx-get`s the PO-line pane into `#aw-pane` (explicit target).
- Tests: `tests/test_kanban_lanes.py`, `tests/test_mark_received.py`,
  `tests/test_kanban_render.py`.
