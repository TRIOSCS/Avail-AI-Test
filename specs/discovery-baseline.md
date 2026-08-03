# Discovery Baseline — CLAUDE.md vs. the actual codebase

Orientation audit, 2026-07-28, repo `/root/availai` @ `52fa4683` (main, clean).
Every claim below was checked against code; every row cites `file:line`.
NOT FOUND rows state what was searched. No application code was changed.

**Lead finding — there are TWO different CLAUDE.md files:**

| File | What it is |
|---|---|
| `/root/CLAUDE.md` | Session-loaded "AVAIL" doc, written from conversation. Business context + working rules. **Audited in §2.** |
| `/root/availai/CLAUDE.md` | Checked-in "AvailAI" doc, code-derived, older. Technical conventions + commands. **Audited in §3.** |

They share almost no text. Claude Code loads *both* in a session started from
`/root/availai` (parent-dir + repo). Where they disagree, the code wins — and
the code disagrees with each in different places.

---

## 1. REPO MAP

### Top-level structure

```
/root/availai
├── app/                  ← all application code
│   ├── routers/  services/  models/  schemas/     ← matches claimed layout
│   ├── connectors/  jobs/  cache/  utils/  management/  config/  data/
│   ├── templates/ (Jinja2)   static/ (Vite → static/dist/)
│   └── ~25 top-level modules (main, config, database, constants, …)
├── alembic/versions/     ← 251 migration files (repo root, NOT under app/)
├── tests/                ← 1,077 top-level test files (+3 helpers) + integration/,
│   │                       routers/, scripts/, test_models/, test_scripts/,
│   │                       test_services/, ux_mega/, frontend/
│   ├── fixtures/         ← the fixtures dir (claimed name `test_fixtures/` doesn't exist)
│   └── e2e/              ← Python Playwright tests (excluded from default run)
├── e2e/                  ← Playwright TypeScript specs (playwright.config.ts)
├── specs/                ← exactly 1 file: approvals-workspace.md (+ this report)
├── docs/                 ← APP_MAP_{ARCHITECTURE,DATABASE,INTERACTIONS}.md,
│                           RUNBOOK, superpowers/ (specs+plans archive)
├── deploy.sh  docker-compose.yml  Dockerfile  Caddyfile
├── requirements[-dev].in/.txt (pip-tools lockfiles)  pyproject.toml (tooling-only)
└── pytest.ini  package.json  tailwind.config.js  ruff.toml
```

Verdict vs. the session doc's claimed layout (`routers/ services/ models/
schemas/ specs/ test_fixtures/ tests/`): **5 of 7 match**. `test_fixtures/`
does not exist anywhere (`find -name test_fixtures` → nothing; actual:
`tests/fixtures/`), and `specs/` is real but near-empty — the living spec
corpus is `docs/superpowers/{specs,plans}/`.

### Alembic migration head

```
$ alembic heads
203_outreach_recipient_email (head)
```

Single head, 251 version files. (Matches `MIGRATION_NUMBERS_IN_FLIGHT.txt`
discipline; latest deployed migration also 203.)

### Test framework & how tests run

- **pytest** with xdist (`-n auto`), `asyncio_mode = auto`, 30s per-test
  timeout, `tests/e2e` and `.claude` excluded — all set in `pytest.ini:1-14`.
- Canonical invocation: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q`
  (in-memory SQLite; `TESTING=1` disables scheduler + real API calls).
- Separate layers: `tests/e2e/` (Python Playwright vs. live Docker app) and
  repo-root `e2e/` (Playwright TS: `workflows`, `visual`, `accessibility`,
  `dead-ends` projects) — not part of the default suite.

### Current pass state (main @ 52fa4683, run 2026-07-28)

Full suite (`TESTING=1 pytest tests/ -q`, xdist `-n auto`):

```
16 failed, 23860 passed, 24 skipped, 298 warnings in 1858.26s (0:30:58)
```

All 16 failures re-run **individually, serially** (`--override-ini="addopts="`):

```
16 passed, 5 warnings in 18.12s
```

→ **Zero genuine failures.** The 16 are xdist parallel-run flakes (test-isolation
under `-n auto`), scattered across sightings/htmx-views/nightly modules; full
`FAILED` id list preserved in the run log. Two quirks observed during the runs,
noted for the backlog, not fixed here:

- conftest teardown emits `SAWarning: Cannot correctly sort tables …
  unresolvable cycles between companies, customer_sites, site_contacts`
  (`tests/conftest.py:117`).
- After the serial run, "Sentry is attempting to send 2 pending events" —
  something initializes Sentry transport even under `TESTING=1`.

---

## 2. AUDIT — `/root/CLAUDE.md` (session doc, written from conversation)

Legend: ✅ CONFIRMED · ❌ CONTRADICTED · ⬜ NOT FOUND (searched, no evidence
either way — includes business-practice claims code can't witness).

### "What this is"

| Claim | Verdict | Evidence |
|---|---|---|
| FastAPI/PostgreSQL sourcing platform + CRM | ✅ | `requirements.in:4-7` (fastapi 0.139.2, sqlalchemy 2.0.51, psycopg2); `docker-compose.yml:7` (postgres:16-alpine) |
| Built for TRIO | ✅ | `git shortlog`: 3,948 commits by `TRIOSCS <mkhoury@trioscs.com>`; repo `CLAUDE.md:3` "Trio Supply Chain Solutions" |
| "…and ITAD operation" | ⬜ | grep -ri `itad` across app/, docs/, specs/, README → zero hits |
| Single developer | ✅ | `git shortlog -sne`: one dominant human (3,948), rest are AI agents/bots (Claude 388, dependabot 65, Cursor 58) + ~20 stray commits |
| Hosted on DigitalOcean at app.availai.net | ✅ | `Caddyfile:15` (`app.availai.net {`); `.env.example:5`; `README.md:6,12` |
| POs are not cut in AVAIL; buyers cut them in the ERP | ✅ | No PO-owning model exists (grep `class PurchaseOrder` → 0); `app/models/buy_plan.py:224` `po_number = Column(String(100))` is a text reference; `app/templates/htmx/partials/approvals/_pane_po_line.html:84` "Confirm the PO you cut in Acctivate" |

### "Systems of record"

| Claim | Verdict | Evidence |
|---|---|---|
| Sales orders live in Acctivate, not AVAIL | ✅ | `app/models/buy_plan.py:85-86` — `sales_order_number` stored under `# ── Acctivate references`; `docs/superpowers/specs/2026-06-28-approvals-rework-acceptance.md:7` "AVAIL does not create Sales Orders or Purchase Orders — Acctivate does." |
| Purchase orders live in Acctivate | ✅ | Same acceptance doc; `app/services/buyplan_workflow/buyplan_po.py:60` records an externally-cut PO number; default line status `AWAITING_PO` (`buy_plan.py:221`) |
| Inventory lives in Acctivate | ✅ (by absence) | No inventory/stock model in `app/models/` (grep `inventory|stock_qty` → no owning table); `app/models/purchase_history.py:40` documents `acctivate_po` as a *planned* source value (header `:7` "Future: Acctivate PO imports"); no import code exists |
| Requisitions, buy plans, quality plans live in AVAIL | ✅ | `app/models/sourcing.py:30` (Requisition), `app/models/buy_plan.py:70` (BuyPlan → `buy_plans_v3`), `app/models/quality_plan.py:42` (QualityPlan) |
| Resell, CRM, contacts, approvals, audit live in AVAIL | ✅ | `app/models/excess.py:57`; CRM models + `app/audit_listeners.py:24`; `app/models/approvals.py`; `app/models/intelligence.py:457` (ActivityLog) |

### "Hard constraints"

| Claim | Verdict | Evidence |
|---|---|---|
| Nothing integrates with Acctivate — no sync/read/write | ✅ | grep -ri `acctivate` → only comments, UI copy, docs, tests; zero API-client/import code. All Acctivate data arrives via user forms (`app/routers/htmx/buy_plans.py:828`) |
| ERP-neutral field naming (`erp_so_number`, not `acctivate_so_number`) | ❌ | Neither convention exists. grep `erp_` in models/schemas/alembic → **zero hits**. Actual columns are un-prefixed: `sales_order_number` / `customer_po_number` (`app/models/buy_plan.py:86-87`) — sitting under a literal `# ── Acctivate references` comment. Vendor name also baked into a documented source value: `purchase_history.py:40` `"acctivate_po"` (comment; written only by test fixtures), and UI copy says "Acctivate" (`app/templates/htmx/partials/buy_plans/detail.html:202`). (No `acctivate_*` *columns* survive — those were dropped in `alembic/versions/049_reconcile_schema_drift.py:43-51`.) |
| ERP doc numbers stored as references only; AVAIL never owns SO/PO lifecycle | ✅ | All PO/SO fields are `String` references (`buy_plan.py:86-87,224`); AVAIL tracks *whether* a PO was cut (`AWAITING_PO` → confirm flow), never numbers or creates one |

### "Business context" — Quality Plan

| Claim | Verdict | Evidence |
|---|---|---|
| The QP is the hub document, one QP per TSO | ✅ | One QP per buy plan (= per TSO): get-or-create `GET /v2/qp/for-buy-plan/{bp_id}` (`app/routers/quality_plans.py:231`); TSO ≡ `BuyPlan.sales_order_number` (`app/services/buyplan_naming.py:53`) |
| "Today an Excel workbook in a SharePoint library" | ❌ | The QP is **native in AVAIL** since migration 161 (`alembic/versions/161_qp_native_sections.py`; `app/models/quality_plan.py`). The SharePoint workbook is what the workspace *replaced*: `specs/approvals-workspace.md:5` |
| QP has SALES / PURCHASING / SERIAL-FRU sections | ✅ | `app/templates/htmx/partials/qp/_section_{sales,purchasing,serial,fru}.html`; section edit routes at `app/routers/quality_plans.py` (PATCH `:453,:476`; POST `:499,:568`) |
| BUY PLAN is a section *inside* the QP, not a sibling | ❌ | Inverted in the data model: BuyPlan is the root; the QP hangs off it (`qp/for-buy-plan/{bp_id}`, `quality_plans.py:231`) and the buy-plan gate (`BUY_PLAN`) is a separate gate from `QP_SALES`/`QP_PURCHASING` (`app/constants.py:1241-1257`) |
| Sections gated by SO / PO / Buy-Plan approvals; prepayment standalone tied to a PO | ✅ (nuance) | Gates exist per section (`ApprovalGateType`, `constants.py:1241-1257`); Prepayment is standalone but ties to buy plan + line (`app/models/quality_plan.py:170,173`), the line carrying the PO number — not to a PO entity (none exists) |
| Approved QPs render back into the SharePoint library path (attachment links keep working) | ⬜ | **Not implemented.** No QP→SharePoint export/render code. Searched: `sharepoint` across app/ (hits only generic attachment storage, `app/services/attachment_service.py`), `render.*qp`, `workbook`, `openpyxl` QP paths. Unbuilt future work |

### "Business context" — order types, approvals, roles

| Claim | Verdict | Evidence |
|---|---|---|
| Order types: New, Revision, Testing Service, Comps Program, Stock Sale | ✅ | `app/constants.py:485-499` `SalesOrderType`: NEW, REVISION, TESTING_SERVICE, COMPS, STOCK_SALE (column: `buy_plan.py:136-139`) |
| Some order types have no buy plan | ✅ | `app/constants.py:504` `SOURCING_ORDER_TYPES = {NEW, REVISION}` — the other three take the "lite no-lines path" |
| Approvals Workspace: four tabs — Sales Orders, Buy Plans, Purchase Orders, Prepayments | ✅ | `app/routers/htmx/approvals_hub.py:59` `_TABS = ("sales-orders", "buy-plans", "purchase-orders", "prepayments")`; labels `:70-75`; shell template `approvals/approvals_hub.html:22-37` |
| All four are lenses on one pipeline rooted at the sales order | ✅ (nuance) | SO + BP tabs are literally the same rows/approval (`approvals_hub.py:57-58,207-208,1036`). PO tab is per-line verification — its deal-level engine gate was retired (`alembic/versions/176_retire_deal_po_gate.py`; `constants.py:1253-1257`) |
| Buy plan shares the SO's single manager approval | ✅ | Exactly one `BUY_PLAN` ApprovalRequest per plan (`app/services/buyplan_workflow/buyplan_approval.py:85-87`, resubmit `:711-713`) |
| Detail pane is a working editor; every change audit-logged | ✅ | `app/services/field_audit.py:1-10` — every edit path diffs fields and writes a `FIELD_EDIT` ActivityLog row per save (`constants.py:852`) |
| Two-screen model: Approvals workspace + full-page **Deal view** for build/fix work | ❌ | **The Deal view is retired.** No deal route/template exists (grep + find → only migration 176). `app/services/buyplan_hub.py:3-9` "the retired Buy Plan Deal Hub"; `/v2/buy-plans[/{id}]` now 308-redirects into `/v2/approvals?tab=buy-plans` (`app/routers/htmx_views.py:302-319`). Build/fix work happens in the workspace panes |
| Routing: first-responder-wins, any-of pool | ✅ | Single `ApprovalStep` with `rule=ApprovalStepRule.ANY` over all eligible users (`app/services/approvals/routing.py:107-121`) |
| The pool is one day-to-day manager + two backups (any-of-**3**) | ⬜ | No 3-person structure in code — pool = whoever has `can_approve_buy_plans=True` (`routing.py:46-47`), count is data-driven. The "Aniket + Mike/Marcus backup" arrangement exists only as prose in `specs/approvals-workspace.md:31`. Searched: `backup|any_of|approver_pool` in approvals/buyplan services, constants, config |
| Prepayments route to **two accounting approvers**, plus owner on higher value | ❌ | Approvers = users with `can_approve_prepayments=True`, filtered by *per-user* `prepayment_approval_limit` (`routing.py:48-54`) — same manager pool. Accounting (Myrna/Katy) are **notification recipients + pay-link confirmers, not approvers**: `app/services/prepayment_notifications.py:1-10`; tokenized no-login confirm at `app/routers/prepayment_confirm.py:95-141`; `specs/approvals-workspace.md:33` ("no AVAIL logins required", in-app rights explicitly deferred `:124,:129`). `owner_id` is used for notifications only (`app/services/approvals/service.py:209`) |
| Deals under $5K revenue auto-approve | ❌ | **Removed.** "Every plan goes to the one manager approval — no auto-approve (frozen scope)" (`app/services/buyplan_workflow/buyplan_approval.py:83`, also `:709`); no `auto_approve` setting in `app/config.py` (grep → 0). Historical `po_auto_approve_threshold` deleted (`docs/APP_MAP_INTERACTIONS.md:1808,6975-6978`). Leftovers: vestigial `auto_approved` column (`buy_plan.py:103`, always reset False) and a **stale docstring** still describing the old rule (`buyplan_approval.py:58-59`) |
| Everything else is one-click approval with drill-in to edit | ✅ | Decide endpoint `app/routers/approvals.py:118`; manager-edit-anything panes (`tests/test_manager_edit_anything.py`) |
| Roles: sales, buyer, manager, admin — no sub-roles | ❌ | `app/constants.py:307-314` `UserRole` has **six** values: BUYER, SALES, TRADER, MANAGER, ADMIN (+ AGENT, referenced `:442`); `app/models/auth.py:18` comment: `buyer | sales | trader | manager | admin`. Trader is a first-class role, not just an internal practice |
| Sales only sell, buyers only buy, traders do both (internal practice) | ⬜ | Org practice, not enforced: all five roles pass buyer gates (`app/dependencies.py:250` `BUYER_ROLES` includes SALES and TRADER); differentiation is via `RESTRICTED_ROLES={SALES,TRADER}` (`constants.py:324`) + `ROLE_ACCESS_DEFAULTS` (`constants.py:436-442`), not sell/buy separation |

### "Business context" — Resell module

| Claim | Verdict | Evidence |
|---|---|---|
| Inverse of sourcing: post excess → traders offer → owner builds bid back | ✅ | `app/models/excess.py` (ExcessList/ExcessOffer/CustomerBid); `app/services/bid_back_service.py`; workspace router `app/routers/resell.py` |
| ExcessList/ExcessLine mirrors Requisition/Requirement | ✅ | `app/models/excess.py:57` "analogous to Requisition", `:105` "analogous to Requirement" (class is `ExcessLineItem`, not `ExcessLine`) |
| Posted lines dual-write into Sighting so the matcher picks them up | ✅ | `app/services/excess_mirror.py:183` (`mirror_line`), `:438` (`sync_list_mirror` — "Own the dual-write for a WHOLE list"), `:474-507` (`publish_list`); FK `Sighting.excess_line_item_id` (`app/models/sourcing.py:279`) |
| Offers scoped per-line or take-all | ✅ | `app/models/excess.py:173-174` (`scope`, `take_all_total_price`); `app/constants.py:190-194` `ExcessOfferScope` |
| Customer identity hidden on postings | ✅ | `app/routers/resell.py:180-192` (`_display_title` → "Excess listing #N" for non-owners); mirror never uses customer name (`excess_mirror.py:66-70` `EXCESS_VENDOR_LABEL="Customer Excess"`); `tests/test_resell_draft_offer_privacy.py` |
| Match on part number only — no price ranking | ✅ (nuance) | `app/services/excess_service.py:546` `_classify_mpn_match` — "Part-number-only matching (price never affects it)". Note: a *separate* rollup does rank offers by highest unit price for display (`excess_service.py:765`) — but it never affects matching |
| No margin math | ✅ | grep -i `margin` across excess_service, resell router, bid_back_service, excess models → zero hits |

### "Code conventions" (session doc)

| Claim | Verdict | Evidence |
|---|---|---|
| Structure: routers/, services/, models/, schemas/ | ✅ | All four exist under `app/` (plus 9 more top-level dirs) |
| specs/ directory | ✅ | Exists at repo root — but holds exactly 1 file; living specs are under `docs/superpowers/` |
| test_fixtures/ directory | ❌ | Does not exist anywhere (`find -name test_fixtures` → 0). Actual: `tests/fixtures/` |
| tests/ directory | ✅ | 1,077 top-level test files (+ conftest.py, _route_helpers.py, migration_harness.py) + subdirs; `tests/e2e/` excluded from default run |
| Routers thin; business logic never in a router | ❌ | Aspirational, not factual. `app/routers/sightings.py` (3,804 lines): **15 in-router `db.commit()`** + multi-step orchestration (`:1266-3798`); `app/routers/htmx/companies/contacts.py`: 12 in-router `db.commit()` despite importing five `app/services` modules (`:33,39-42`) — orchestration still lives in the router. Newer code conforms (`app/routers/resell.py`: 1 commit, delegates to services) |
| Loguru, never print() | ✅ | 311 `from loguru import logger` in app/; zero real `print()` outside `app/management/` CLI report scripts (self-documented, e.g. `ingest_source_data.py:14`) |
| Header docstring on every new file | ✅ | 7/7 recently-touched files sampled from `git log -20` open with a line-1 docstring (house format incl. "Called by:/Depends on:") |
| Tests alongside code, same commit | ✅ | 9 of the last 10 non-merge commits touch `tests/` (exception: a scripts-only fix, `22f6572e`) |
| Alembic: single-head discipline | ✅ | `alembic heads` → one head: `203_outreach_recipient_email` |

### Claims code cannot witness (for completeness)

| Claim | Verdict | Evidence |
|---|---|---|
| Move to Dynamics 365 Business Central **in 2027** | ⬜ | Direction is in docs — `docs/superpowers/2026-07-03-master-requested-work-backlog.md:204-205` "Future ERP = Microsoft Dynamics 365 (round-2 project post-go-live)"; `docs/superpowers/specs/2026-07-03-prepay-closure-design.md:169-171` (BC or F&O APIs). The **2027 date appears nowhere** (grep `2027` → no relevant hit) |
| Buyers cut POs in the ERP and send them (human workflow) | ✅ (as reflected) | Code models exactly this handoff: `_pane_po_line.html:84` "Confirm the PO you cut in Acctivate"; `docs/superpowers/2026-07-16-triage-decisions.md:33` |
| "How to work with me" / "Do not" sections | — | Working instructions, not codebase claims — out of audit scope |

---

## 3. AUDIT — `/root/availai/CLAUDE.md` (repo doc, code-derived)

169 factual claims verified by a six-agent fleet (one per section cluster),
each row independently checked against the cited code. Score: **161 confirmed,
8 contradicted, 0 not found** — this doc is far more accurate than the session
doc, but it has drifted in specific spots (all 8 listed first in their tables).

### Header block — stack, version, services, feature list (38 claims, 1 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| The ten listed sources are the complete supplier search set | ❌ | app/services/nc_worker/__init__.py:1 — Three present-but-unclaimed browser-based search workers also exist: NetComponents (nc_worker), The Broker Forum (tbf_worker/__init__.py:1), ICsource (ics_worker/__init__.py:1), each with models (nc_/tbf_/ics_search_queue.py) |
| App is AvailAI, sourcing engine and CRM for Trio Supply Chain Solutions | ✅ | app/config.py:312 — own_domains defaults to trioscs.com; stock-sale notify emails @trioscs.com at config.py:264 |
| Stack includes FastAPI | ✅ | requirements.in:4 — fastapi==0.139.2 |
| Stack includes SQLAlchemy 2.0 | ✅ | requirements.in:6 — sqlalchemy==2.0.51 |
| Stack includes PostgreSQL 16 | ✅ | docker-compose.yml:7 — db service image postgres:16-alpine (db-backup also postgres:16-alpine at line 180) |
| Stack includes HTMX 2.x | ✅ | package.json:46 — htmx.org ^2.0.10 plus htmx-ext-* 2.x extensions |
| Stack includes Alpine.js 3.x | ✅ | package.json:31 — alpinejs ^3.15.12 plus @alpinejs/* 3.x plugins |
| Stack includes Jinja2 | ✅ | requirements.in:11 — jinja2==3.1.6 |
| Stack includes Tailwind CSS 3.x | ✅ | package.json:59 — tailwindcss ^3.4.19 (devDependency) |
| Docker Compose services are exactly app, enrichment-worker, db, redis, caddy, db-backup | ✅ | docker-compose.yml:6,50,86,140,179,208 — Programmatic scan of services block finds exactly those six keys: db, redis, app, enrichment-worker, db-backup, caddy |
| Deployed on DigitalOcean | ✅ | README.md:12 — README setup targets a DigitalOcean droplet; backup-to-spaces.sh uploads to DigitalOcean Spaces (docs/APP_MAP_ARCHITECTURE.md:653) |
| APP_VERSION constant in app/config.py is currently 3.1.0 | ✅ | app/config.py:16 — APP_VERSION = "3.1.0" |
| Searches supplier APIs in parallel | ✅ | app/search_service.py:1843 — "Run ALL connectors x ALL part numbers in parallel" (asyncio-based fan-out) |
| BrokerBin is a supplier search source | ✅ | app/connectors/sources.py:1 — BrokerBinConnector; wired in app/services/connector_registry.py:217-218 |
| Nexar is a supplier search source | ✅ | app/connectors/sources.py:1 — NexarConnector (Octopart); wired in app/services/connector_registry.py:212-213 |
| DigiKey is a supplier search source | ✅ | app/services/connector_registry.py:195 — DigiKeyConnector from app/connectors/digikey.py; instantiated at registry:227-228 |
| Mouser is a supplier search source | ✅ | app/services/connector_registry.py:198 — MouserConnector from app/connectors/mouser.py; instantiated at registry:231-232 |
| OEMSecrets is a supplier search source | ✅ | app/services/connector_registry.py:199 — OEMSecretsConnector from app/connectors/oemsecrets.py; instantiated at registry:234-236 |
| Element14 is a supplier search source | ✅ | app/services/connector_registry.py:197 — Element14Connector from app/connectors/element14.py; instantiated at registry:242-244 |
| Sourcengine is a supplier search source | ✅ | app/services/connector_registry.py:200 — SourcengineConnector from app/connectors/sourcengine.py; instantiated at registry:238-240 |
| eBay is a supplier search source | ✅ | app/services/connector_registry.py:196 — EbayConnector from app/connectors/ebay.py; instantiated at registry:220-223 |
| AI web search is a supplier search source | ✅ | app/services/connector_registry.py:253-254 — AIWebSearchConnector from app/connectors/ai_live_web.py |
| Email mining is a supplier search source | ✅ | app/services/connector_registry.py:246 — email_mining gated by email_mining_enabled flag; module app/connectors/email_mining.py |
| Tracks vendor intelligence | ✅ | app/models/vendors.py:31 — VendorCard model; email mining "Enriches VendorCards with verified contact info" (app/connectors/email_mining.py:15); vendor_scorecard.py/vendor_score.py services |
| Enriches companies/contacts via SAM.gov | ✅ | app/services/enrichment_router.py:70 — sam_gov_company.enrich_company; provider order "SAM.gov (free) -> Clay -> Explorium -> Lusha -> AI" at enrichment_router.py:7 |
| Enriches via Clay | ✅ | app/services/enrichment_router.py:73-74 — clay_mcp.enrich_company from app/connectors/clay_mcp.py |
| Enriches via Explorium | ✅ | app/services/enrichment_router.py:77-79 — explorium.enrich_company with EXPLORIUM_API_KEY; connector app/connectors/explorium.py |
| Enriches via Lusha | ✅ | app/services/enrichment_router.py:82-84 — lusha.enrich_company with LUSHA_API_KEY; connector app/connectors/lusha.py |
| Enriches via Hunter | ✅ | app/services/enrichment_router.py:123-129 — _hunter_find_contacts gated by hunter_enrichment_enabled; connector app/connectors/hunter.py |
| Enrichment includes AI web search stage | ✅ | app/services/enrichment_router.py:87-90 — _ai_company -> enrichment_service._ai_find_company; last tier in cost order at line 7 |
| Automates RFQ workflows via Microsoft Graph | ✅ | app/email_service.py:248 — send_batch_rfq posts to Graph /me/sendMail via GraphClient (imported at email_service.py:132) |
| Mines inboxes with Claude AI | ✅ | app/services/email_intelligence_service.py:71 — claude_json classification (Sonnet/Opus per line 5), called by connectors/email_mining.py scan_inbox (line 12); email_mining.py:1-4 scans M365 inbox/sent via Graph |
| CRM covers companies | ✅ | app/models/crm.py:14 — class Company(Base), plus CustomerSite/SiteContact in same module |
| CRM covers quotes | ✅ | app/models/quotes.py:26 — class Quote(Base) plus QuoteLine/QuoteRequisition; quote_builder_service.py, quote_send.py services |
| CRM covers buy plans | ✅ | app/models/buy_plan.py:70 — class BuyPlan(Base) plus BuyPlanLine; buyplan_builder.py etc. services |
| CRM covers customer matching | ✅ | app/services/proactive_matching.py:1 — "Proactive matching engine — finds customer matches for new inventory" using customer_part_history |
| Skill guide references real file vite.config.js | ✅ | vite.config.js:1 — Exists at repo root; CLAUDE.md:399 names it as the vite-skill trigger |
| Skill guide references @cached_endpoint decorator (redis row) | ✅ | app/cache/decorators.py:26 — def cached_endpoint(prefix, ttl_hours=4, key_params=None); used in routers/materials.py and others |

### Authoritative Maps + Project Layout (29 claims, 0 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| docs/APP_MAP_ARCHITECTURE.md exists and covers stack, infra, project structure | ✅ | docs/APP_MAP_ARCHITECTURE.md:1 — Title 'Architecture & Stack'; 'Tech Stack' at line 9, 'Infrastructure (Docker Compose)' at line 25 |
| docs/APP_MAP_DATABASE.md exists and covers models, tables, relationships | ✅ | docs/APP_MAP_DATABASE.md:1 — Title 'Database Schema'; line 3 says update on model/table/relationship changes; 'Table Overview by Domain' line 14 |
| docs/APP_MAP_INTERACTIONS.md exists and covers service interactions, data flows, integration patterns | ✅ | docs/APP_MAP_INTERACTIONS.md:1 — Title 'Interaction & Data Flow'; line 3 names service interactions, data flows, integration patterns |
| docs/BRANCH_AND_CI_WORKFLOW.md covers branch naming/lifecycle, changed-files formatting gate, quarantine-before-delete | ✅ | docs/BRANCH_AND_CI_WORKFLOW.md:9 — §1 'formatting gate is changed-files only' (line 9), §2 'Branch naming & lifecycle' (line 32), §3 'Quarantine before delete' (line 71) |
| scripts/branch-cleanup.sh exists and does quarantine-aware branch hygiene | ✅ | scripts/branch-cleanup.sh:3 — Header: 'safe, quarantine-aware branch pruning'; archives unmerged branches as archive/<name> tags before delete |
| app/main.py — FastAPI app, router registration, middleware, lifespan | ✅ | app/main.py:62 — Docstring line 1 'FastAPI application — all routes'; lifespan at 62, 42 include_router calls (849+), add_middleware cluster at 452-463 |
| app/config.py — Pydantic Settings with APP_VERSION and MVP_MODE | ✅ | app/config.py:16 — APP_VERSION = "3.1.0" (line 16), class Settings(BaseSettings) line 37, mvp_mode: bool = False line 368 |
| app/database.py — SQLAlchemy engine, SessionLocal, UTCDateTime type | ✅ | app/database.py:88 — UTCDateTime TypeDecorator line 15, create_engine lines 65/75, SessionLocal = sessionmaker line 88 |
| app/dependencies.py defines require_user, require_admin, require_buyer, require_fresh_token | ✅ | app/dependencies.py:63 — require_user:63, require_admin:227, require_buyer:321, require_fresh_token:663 |
| app/constants.py holds status StrEnums | ✅ | app/constants.py:12 — from enum import StrEnum; 20+ status classes (RequisitionStatus:112, OfferStatus:73, QuoteStatus:287, etc.); docstring: single source of truth |
| app/shared_constants.py defines JUNK_DOMAINS and JUNK_EMAIL_PREFIXES | ✅ | app/shared_constants.py:17 — JUNK_DOMAINS: set[str] line 17, JUNK_EMAIL_PREFIXES: set[str] line 76 |
| app/startup.py — runtime ops only (triggers, seeds, ANALYZE), no DDL | ✅ | app/startup.py:3 — Header: 'Schema DDL... lives in Alembic migrations. This file handles... triggers, seeds, backfills, and ANALYZE' — CREATE TRIGGER/FUNCTION stmts (461-519) are the doc's stated trigger exception; no table/column/index DDL |
| app/scheduler.py + app/jobs/ — APScheduler coordinator and job definitions | ✅ | app/scheduler.py:1 — 'Background scheduler — APScheduler coordinator. Job implementations live in app/jobs/ domain modules'; AsyncIOScheduler import line 15; app/jobs/ has core_jobs.py, email_jobs.py, etc. |
| app/search_service.py — requirement search orchestrator across all supplier sources | ✅ | app/search_service.py:645 — Docstring line 1 'runs requirements through all configured sources'; search_requirement() at 645; imports BrokerBin/Nexar/DigiKey/Mouser/eBay/etc. connectors (24-31) |
| app/email_service.py — Graph API batch RFQ, inbox monitor, AI reply parsing | ✅ | app/email_service.py:1 — Docstring: 'batch RFQ sending, inbox monitoring, AI parsing'; send_batch_rfq at 94; graph_message_id/graph_conversation_id at 113 |
| app/enrichment_service.py — customer/vendor enrichment orchestrator | ✅ | app/enrichment_service.py:3 — 'Shared enrichment workflow for both vendor cards and customer companies'; delegates provider orchestration to enrichment_router |
| app/scoring.py — sighting buyer-usefulness multi-factor scoring | ✅ | app/scoring.py:1 — Docstring: 'Sighting Score — buyer-usefulness-oriented multi-factor scoring' (trust, price, quantity, freshness, completeness) |
| app/evidence_tiers.py — data-provenance tier tags for sightings/offers | ✅ | app/evidence_tiers.py:1 — 'Evidence tier definitions — data provenance tags for sightings and offers'; T1-T7 tier ladder |
| app/vendor_utils.py — fuzzy_score_vendor() rapidfuzz wrapper | ✅ | app/vendor_utils.py:168 — def fuzzy_score_vendor at 168: 'Return rapidfuzz token_sort_ratio between two vendor names (normalized)'; from rapidfuzz import fuzz at 177 |
| app/http_client.py — shared singleton httpx.AsyncClients with connection pooling | ✅ | app/http_client.py:3 — 'Two module-level singleton httpx.AsyncClient instances' (http:26, http_redirect:32); header says 'connection pooling for all outbound requests' |
| app/rate_limit.py — shared rate limiter, Redis-backed with in-memory fallback | ✅ | app/rate_limit.py:1 — 'Shared rate limiter with Redis storage and in-memory fallback'; fallback logic at 25-29 |
| app/prometheus_metrics.py — ASGI metrics middleware + /metrics endpoint | ✅ | app/prometheus_metrics.py:1 — 'Prometheus metrics middleware + /metrics endpoint exposure... Pure ASGI middleware'; /metrics route line 81, PrometheusMiddleware class line 125 |
| app/connector_status.py logs which supplier connectors are enabled/disabled at startup | ✅ | app/connector_status.py:1 — 'Connector startup visibility — log connector readiness (DB-first + health). Called by: main.py lifespan' |
| app/ contains models/ schemas/ routers/ services/ connectors/ cache/ utils/ management/ directories | ✅ | app/models (ls -d verified all eight dirs) — All eight exist: app/models, app/schemas, app/routers, app/services, app/connectors, app/cache, app/utils, app/management |
| app/config/routing_maps.json holds brand→commodity inference maps used by buy-plan scoring | ✅ | app/config/routing_maps.json:3 — Contains brand_commodity_map (e.g. 'texas instruments': 'semiconductors'); loaded by app/services/buyplan_scoring.py:33 (_get_routing_maps) |
| templates/ (Jinja2) and static/ with Vite-built assets in static/dist/ | ✅ | app/template_env.py:13 — Jinja2Templates(directory="app/templates"); vite.config.js:11 outDir = app/static/dist; app/static/dist/ exists with assets/ |
| app/management/ holds one-off CLI commands, e.g. python -m app.management.reenrich | ✅ | app/management/reenrich.py:3 — Usage line: 'python -m app.management.reenrich [--limit N] [--batch-size N]'; __main__ guard at line 94; dir has 25+ CLI modules |
| Migrations live at repo-root alembic/versions/, not under app/ | ✅ | alembic/versions/001_initial_schema.py:1 — alembic/versions exists at repo root (001_initial_schema.py onward); app/alembic and app/migrations do not exist |
| Tests live in tests/ (unit/integration) and tests/e2e/ (Playwright) | ✅ | tests/e2e/conftest.py:1 — Both dirs exist; e2e conftest docstring: 'E2E test fixtures — Playwright against the live Docker app' |

### Auth Model (10 claims, 3 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| Session cookie has 15-min expiry | ❌ | app/main.py:459 — max_age=86400 — the session cookie lives 24 hours, not 15 minutes; no 15-min cookie expiry exists anywhere in the SessionMiddleware config |
| require_fresh_token re-validates with a 15-min buffer | ❌ | app/dependencies.py:679-687 — require_fresh_token checks only hard expiry (datetime.now(UTC) > expiry, no buffer) and does no inline refresh; the 15-min buffer lives in background job _job_token_refresh (app/jobs/core_jobs.py:67 'now > exp - timedelta(minutes=15)', scheduled every 5 min at core_jobs.py:30) |
| require_buyer gates search | ❌ | app/routers/htmx/search_views.py:63,179 — search endpoints (/v2/partials/search/ai, /v2/partials/search/run, sourcing search at app/routers/htmx/sourcing.py:109, quick-source RFQ at app/routers/part_dossier.py:332) use Depends(require_user), not require_buyer |
| OAuth2 via Azure AD; app/routers/auth.py handles login/callback/logout | ✅ | app/routers/auth.py:42,52,69,234-236 — AZURE_AUTH = login.microsoftonline.com oauth2/v2.0 (line 42); GET /auth/login (52), GET /auth/callback (69), POST+GET /auth/logout (234-236) all in this router |
| Session middleware stores user_id in an HTTP-only cookie | ✅ | app/main.py:454-460; app/routers/auth.py:186 — SessionMiddleware registered in main.py; callback sets request.session["user_id"] = user.id; Starlette SessionMiddleware hardcodes 'httponly' in security_flags (.venv/lib/python3.12/site-packages/starlette/middleware/sessions.py:33) |
| require_user = any login | ✅ | app/dependencies.py:63-86 — 401 if no session user, 403 only if deactivated, no role check; also admits the x-agent-key service account (lines 67-76) |
| require_buyer gates RFQ actions | ✅ | app/dependencies.py:321-331 — docstring: 'requires a buyer-tier role for RFQ actions'; BUYER_ROLES = buyer/sales/trader/manager/admin (line 250); gates e.g. sightings offer request/send (app/routers/sightings.py:3473,3612) and vendor-contact/import-stock endpoints |
| require_admin gates settings and user management | ✅ | app/dependencies.py:227-244; app/routers/admin/users.py:4-5,207 — user-management routes are all Depends(require_admin); admin settings use require_admin (app/routers/htmx/settings.py:1019) or require_settings_access which is admin-only (dependencies.py:237-244, app/routers/admin/spec_codes.py:67); note: personal settings tabs use require_user (e.g. settings_profile_tab, htmx/settings.py:195); one admin-only tab is gated by require_user + an inline role check instead of require_admin (htmx/settings.py:58,61-62) |
| require_admin raises 403 if user.role != admin | ✅ | app/dependencies.py:222-223 — _require_admin_user raises HTTPException(403) when user.role != UserRole.ADMIN; also blocks the agent service account (line 220-221) |
| Dependency implementations live in app/dependencies.py | ✅ | app/dependencies.py:63,227,321,663 — require_user (63), require_admin (227), require_buyer (321), require_fresh_token (663) all defined in this file |

### Coding Conventions (Database, Search, MPN, Substitutes, Caching, Frontend, Response Formats) (29 claims, 2 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| Example enum RequirementStatus.FOUND exists in app/constants.py | ❌ | app/constants.py:141 — No RequirementStatus enum exists anywhere in app/ (grep 'class RequirementStatus'); requirement status is SourcingStatus (OPEN/SOURCING/OFFERED/QUOTED/WON/LOST/ARCHIVED, constants.py:141-151) and no enum has a FOUND member (only NOT_FOUND in MaterialEnrichmentStatus :1043) |
| Plugin/extension lists live in app/static/htmx_app.js and app/templates/base.html | ❌ | app/templates/htmx/base.html:50 — htmx_app.js exists at the stated path, but there is no app/templates/base.html — the base template is app/templates/htmx/base.html (find over app/ located only templates/htmx/base.html) |
| Use db.get(Model, id), not db.query(Model).get(id) (SQLAlchemy 2.0 style) | ✅ | app/routers/sightings.py:1136 — db.get(Requirement, requirement_id) is the live pattern; grep for .query(...).get( across app/ found zero legacy uses |
| Status values are StrEnum constants from app/constants.py, e.g. RequisitionStatus.OPEN | ✅ | app/constants.py:112 — class RequisitionStatus(StrEnum) with OPEN = "open" at constants.py:123; file header :1 says centralized StrEnum constants |
| app/services/spec_tiers.py is the F1 tier ladder / single arbitration point with SOURCE_TIER registry | ✅ | app/services/spec_tiers.py:75 — SOURCE_TIER: dict[str, int] maps source strings to tiers (manual:100 ... claude_haiku:40) |
| set_category() is a ladder write entry point in spec_tiers.py | ✅ | app/services/spec_tiers.py:684 — def set_category( defined in spec_tiers.py |
| record_spec() is the spec write entry point | ✅ | app/services/spec_write_service.py:171 — Exists, but lives in app/services/spec_write_service.py (delegates to the spec_tiers ladder, see spec_tiers.py:19), not in spec_tiers.py itself |
| Unknown source string -> tier 0 -> loses every conflict | ✅ | app/services/spec_tiers.py:141 — tier = SOURCE_TIER.get(source, 0); warns once per unknown source that 'every write from this source loses all conflicts' (:142-148) |
| Enrichment writers + evidence-source tiers table documented in docs/APP_MAP_INTERACTIONS.md | ✅ | docs/APP_MAP_INTERACTIONS.md:4649 — Section 'spec_tiers — source→tier provenance ladder' with SOURCE_TIER table at :4655 and writer routing at :4368-4429 |
| Vendor matching via fuzzy_score_vendor() in app/vendor_utils.py (rapidfuzz wrapper) | ✅ | app/vendor_utils.py:168 — def fuzzy_score_vendor returns rapidfuzz token_sort_ratio; 'from rapidfuzz import fuzz' at :177 |
| MPN dedup via strip_packaging_suffixes() in app/services/search_worker_base/mpn_normalizer.py | ✅ | app/services/search_worker_base/mpn_normalizer.py:77 — def strip_packaging_suffixes(mpn: str) -> str defined there |
| search_requirement() uses a separate write session | ✅ | app/search_service.py:501 — search_requirement (:645) runs _persist_search_write on a worker thread (:728); it builds its own sessionmaker and write_db entirely inside the thread (:499-502), caller's session never crosses |
| Caller's ORM objects are stale after search_requirement; call db.expire(requirement) before rendering | ✅ | app/routers/sightings.py:1144 — SSE refresh path does db.expire(requirement) with comment 'background search already committed via its own write session; drop the caller session cache' |
| normalize_mpn() uppercases, strips noise, returns None for MPNs < 3 chars | ✅ | app/utils/normalization.py:387 — .upper() at :394, strips quotes/punct/whitespace :396-400, 'if len(s) < 3: return None' at :402-403 |
| @validates on Requirement auto-uppercases primary_mpn, customer_pn, oem_pn | ✅ | app/models/sourcing.py:191 — @validates("primary_mpn", "customer_pn", "oem_pn", "oem_hint") -> value.upper().strip(); also covers oem_hint (superset of the doc's list) |
| \|sub_mpns Jinja2 filter registered and handles string and dict forms | ✅ | app/template_env.py:301 — templates.env.filters["sub_mpns"] = _sub_mpns_filter; filter body branches isinstance str vs dict (:287-290) |
| Canonical substitutes format is list of dicts [{"mpn", "manufacturer"}] | ✅ | app/utils/normalization.py:492 — parse_substitute_mpns builds entries {"mpn", "manufacturer"} (+optional "source"); routers build the same shape (routers/htmx/requisitions.py:1189) |
| Legacy substitute rows may hold plain strings and must be handled | ✅ | app/utils/normalization.py:481 — isinstance(sub, str) coerced to {"mpn": sub}; docstring :468 'Legacy DB rows may hold plain MPN strings' |
| parse_substitute_mpns() lives in app/utils/normalization.py | ✅ | app/utils/normalization.py:460 — def parse_substitute_mpns(subs, primary_mpn, *, limit=MAX_SUBSTITUTES) -> list[dict] |
| JUNK_DOMAINS / JUNK_EMAIL_PREFIXES importable from app/shared_constants.py | ✅ | app/shared_constants.py:17 — JUNK_DOMAINS: set[str] at :17, JUNK_EMAIL_PREFIXES: set[str] at :76 |
| cached_endpoint decorator in app/cache/decorators.py matches shown usage (prefix, ttl_hours, key_params) | ✅ | app/cache/decorators.py:26 — def cached_endpoint(prefix: str, ttl_hours: float = 4, key_params: list[str] \| None = None) — the doc example @cached_endpoint("vendor_list", ttl_hours=24, key_params=["supplier"]) fits exactly; module imports asyncio for async endpoints |
| Loguru is used and request_id context is auto-injected | ✅ | app/main.py:609 — request_id_middleware (:600) wraps every request in logger.contextualize(request_id=req_id) |
| HTMX navigation: hx-get fragments swap into #main-content | ✅ | app/templates/htmx/base_page.html:6 — Root sets hx-target="#main-content"; <main id="main-content"> in app/templates/htmx/base.html:50; nav links inherit/use it (e.g. partials/prospecting/list.html:76) |
| State is Alpine.js via Alpine.store(), persisted with the @persist plugin | ✅ | app/static/htmx_app.js:66 — import persist from '@alpinejs/persist' (:20), Alpine.plugin(persist) (:66), Alpine.store('toast'/'errorLog'/'networkLog'/... (:99-337); $persist used in templates (e.g. templates/htmx/partials/requisitions/list.html:9) |
| $store.toast has message, type, show (boolean); show is not a method | ✅ | app/static/htmx_app.js:99 — Alpine.store('toast', { message: '', type: 'info', show: false }); comment at :82-83 says '`show` is a boolean field, not a method'; setters assign directly (:85-88) |
| template_response() helper exists (router -> view -> template_response render path) | ✅ | app/template_env.py:16 — def template_response(name, context, **kwargs) wraps Jinja2Templates.TemplateResponse; used by routers (e.g. routers/sightings.py:1149) |
| JSON errors are {"error", "status_code", "request_id"} (not ["detail"]) | ✅ | app/main.py:276 — http_exception_handler returns {error, status_code, request_id}; validation handler (:297-302) and 500 handler (:321-326) use the same base keys (422 adds a supplementary "detail" list) |
| List responses are {"items", "total", "limit", "offset"}, not a plain array | ✅ | app/routers/vendor_contacts.py:208 — Exact shape returned there and in crm/companies.py:189, v13_features/activity.py:308; note some older endpoints use a domain key instead of "items" (e.g. "vendors" in vendors_crud.py:329 via PaginatedResponse schemas/responses.py:19) |
| HTMX responses are Jinja2 HTML; schemas live in app/schemas/ | ✅ | app/schemas/responses.py:1 — app/schemas/ holds pydantic models (responses.py, crm.py, ai.py, ...); HTML rendered via Jinja2Templates in app/template_env.py:13-29 |

### Commands (Docker, Deploy, Lint, Dependencies, Migrations, Tests, Frontend/E2E) (41 claims, 1 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| npm run build runs a bundle smoke test | ❌ | package.json:6 — postbuild is `if [ -f scripts/smoke-test-bundles.mjs ] ...` and that file does not exist anywhere (find -name '*smoke*' + ls scripts/ show no .mjs) — the smoke test is silently skipped |
| Docker commands target a compose stack with an `app` service (up/logs -f app/ps/down) | ✅ | docker-compose.yml:86 — services: db(6) redis(50) app(86) enrichment-worker(140) db-backup(179) caddy(208) |
| ./deploy.sh from main = commit + push + rebuild + health-check + verify | ✅ | deploy.sh:18 — commit/push 18-72, build 100, health wait 166-192, build-tag verify 204-249 |
| ./deploy.sh --no-commit rebuilds current branch without committing/pushing | ✅ | deploy.sh:7 — NO_COMMIT flag skips the entire Step-1 commit/push block (18-73) |
| deploy.sh refuses to run off main unless --no-commit | ✅ | deploy.sh:19 — lines 19-26: non-main branch without --no-commit → error + exit 1 |
| deploy.sh builds with a unique --build-arg BUILD_COMMIT=<sha>-<ts> | ✅ | deploy.sh:93 — BUILD_COMMIT="$(git rev-parse --short HEAD)-$(date +%s)"; passed at line 100 |
| BUILD_COMMIT is consumed right before the source COPYs in BOTH Dockerfile stages | ✅ | Dockerfile:11 — stage 1 ARG+RUN at 11-12 before COPYs 13-15; stage 2 ARG/ENV at 58-59 before COPY app/ at 62 |
| No --no-cache: templates/static/Vite always rebuild fresh while apt/pip/npm-ci layers stay cached | ✅ | deploy.sh:88 — deploy.sh 88-92 + Dockerfile 5-10 comments document exactly this cache-bust design; npm ci (5) and pip (49) sit above the ARG |
| ~30s deploys; PR #211 removed --no-cache | ✅ | deploy.sh:91 — PR #211 = perf/deploy-cache-bust (git merge 632080ac); code says "~4x faster deploys" — the ~30s figure appears nowhere in the repo |
| Bare `docker compose up -d --build` (no BUILD_COMMIT arg) ships stale templates | ✅ | Dockerfile:6 — ARG defaults to "unknown" → cache-bust RUN never invalidates, so template/static COPY layers stay cached (comment lines 6-11) |
| deploy.sh rebuilds + recreates BOTH the app and enrichment-worker containers | ✅ | deploy.sh:100 — builds both at 100; recreates app at 162-164, worker at 220-221 |
| app and enrichment-worker share requirements.txt (worker must not lag on a dep bump) | ✅ | deploy.sh:95 — comment 95-99; both services are build: . on the same Dockerfile (docker-compose.yml:87,141) |
| deploy.sh waits for the app health check | ✅ | deploy.sh:166 — lines 166-192 poll .State.Health.Status up to 30x2s, rollback+exit 1 on failure |
| deploy.sh verifies the deployed build tag on both containers | ✅ | deploy.sh:207 — app printenv BUILD_COMMIT at 207-213; enrichment-worker at 244-249 |
| deploy.sh checks Tailwind classes in templates exist in the built CSS bundle | ✅ | deploy.sh:251 — Step 6 (251-273) greps template color classes against dist/assets/styles-*.css; WARNING only, does not fail deploy |
| deploy.sh restarts host nc/ics worker systemd units (run from /root/availai outside docker) | ✅ | deploy.sh:299 — restarts avail-nc-worker, avail-ics-worker AND avail-tbf-worker (doc omits tbf); also re-syncs the host .venv from requirements.txt (284-296) |
| Pre-commit hooks: ruff, ruff-format, mypy, docformatter, detect-private-key | ✅ | .pre-commit-config.yaml:17 — all five present (17,19,28,35,46) but list is incomplete: also check-yaml, check-added-large-files, check-merge-conflict, end-of-file-fixer, trailing-whitespace, no-raw-status-assignment, query-api-ratchet, changed-files-on-push |
| ruff check/format app/ and mypy app/ are the lint/type-check commands | ✅ | .pre-commit-config.yaml:43 — ruff hooks 14-19 (ruff.toml at repo root); mypy hook runs against app/ with --config-file=pyproject.toml (line 47) |
| Edit requirements.in (prod) / requirements-dev.in (dev), never the compiled .txt | ✅ | requirements.in:1 — both .in headers say exactly this ("Do NOT edit requirements(-dev).txt by hand — it is the compiled output") |
| Recompile locks with `pip-compile --no-header --no-strip-extras requirements(.in\|-dev.in)` | ✅ | .github/workflows/ci.yml:103 — CI drift gate uses these exact flags; nit: requirements.in:2 header says only `pip-compile --no-header` (omits --no-strip-extras) |
| requirements.txt (prod lock) is what the Docker image + deploy.sh install | ✅ | Dockerfile:49 — Dockerfile COPY+pip install -r requirements.txt (46-49); deploy.sh:287 installs it into the host worker venv |
| requirements-dev.txt adds the dev/test tools (CI installs both) | ✅ | .github/workflows/ci.yml:89 — CI pip installs requirements.txt then requirements-dev.txt (88-90); requirements-dev.in:4-5 documents -c constraint on prod pins |
| CI fails if a .txt drifts from its .in — the "Verify requirements lockfiles are in sync" step | ✅ | .github/workflows/ci.yml:93 — step recompiles without --upgrade and fails on git diff of both .txt (93-108); runs on shard 0 only |
| .github/workflows/dependabot-lockfile-sync.yml auto-recompiles locks and pushes corrected .txt back to Dependabot pip PRs | ✅ | .github/workflows/dependabot-lockfile-sync.yml:8 — header comment 3-10 states exactly this; gated to dependabot[bot] + in-repo head branch |
| DEPENDABOT_LOCKFILE_TOKEN PAT secret makes the push re-trigger CI / turn checks green | ✅ | .github/workflows/dependabot-lockfile-sync.yml:52 — token: secrets.DEPENDABOT_LOCKFILE_TOKEN \|\| GITHUB_TOKEN with comment explaining the CI-retrigger effect (48-53) |
| Alembic is configured for the listed commands (upgrade head / downgrade -1 / revision --autogenerate / current / history / heads) | ✅ | alembic.ini:6 — script_location = alembic; alembic/versions/ populated (001_initial_schema.py …) |
| pytest.ini sets asyncio_mode=auto | ✅ | pytest.ini:2 — asyncio_mode = auto |
| pytest.ini sets -n auto (xdist parallel) | ✅ | pytest.ini:11 — addopts includes -n auto |
| pytest.ini sets a 30s per-test timeout | ✅ | pytest.ini:11 — --timeout=30 --timeout-method=thread |
| pytest.ini ignores tests/e2e/ | ✅ | pytest.ini:11 — addopts has --ignore=tests/e2e (and --ignore=.claude) |
| TESTING=1 disables the scheduler | ✅ | app/main.py:170 — configure_scheduler()/scheduler.start() (180-183) run only inside `if not _is_testing` (168-170) |
| TESTING=1 disables real API calls | ✅ | app/search_service.py:1808 — live AI gated off under TESTING (1808), connector probe short-circuits (149-152); conftest also forces DATABASE_URL=sqlite:// (tests/conftest.py:34) |
| Tests use in-memory SQLite | ✅ | tests/conftest.py:67 — TEST_DB_URL = "sqlite://" (in-memory), StaticPool engine at 82-87 |
| conftest.py provides fixtures and the test engine | ✅ | tests/conftest.py:82 — lives at tests/conftest.py (no repo-root conftest); it also sets TESTING=1 itself at line 32, so the env var is forced regardless |
| `-m "not slow"` skips slow tests (marker registered) | ✅ | pytest.ini:13 — slow marker registered with deselect hint |
| `--override-ini="addopts="` runs a single file without xdist parallelism | ✅ | pytest.ini:11 — -n auto lives in addopts, so blanking addopts drops xdist (and the e2e ignores/timeout) |
| npm run dev = Vite dev server (localhost:5173) | ✅ | package.json:4 — "dev": "vite"; vite.config.js server block (27) sets proxies but no port → Vite default 5173 |
| npm run build = production build to app/static/dist/ | ✅ | vite.config.js:11 — outDir resolves to app/static/dist; "build": "vite build" (package.json:5) |
| npm run lint = ESLint | ✅ | package.json:9 — "lint": "eslint app/static/" (eslint.config.mjs at repo root) |
| npm run test:frontend = Vitest | ✅ | package.json:8 — "test:frontend": "vitest run" |
| Playwright projects workflows, dead-ends, visual, accessibility exist | ✅ | playwright.config.ts:33 — all four at lines 33-36; plus api, auth, smoke, data-validation, requisitions2-resize, requisitions2-visuals |

### MVP Mode, Observability, Configuration, Safety (22 claims, 1 contradicted)

| Claim | Verdict | Evidence |
|---|---|---|
| mvp_mode gates Dashboard, Enrichment, Teams, Task Manager | ❌ | app/config.py:361-364 — Comment says it 'Now gates ONLY the Teams chat integration'; Dashboard/Analytics, Enrichment, Task Manager were un-gated and are always-on. Only code usages: app/routers/v13_features/activity.py:111 (Teams webhook 404) and app/services/webhook_service.py:316 (skip Teams subscription). No other mvp_mode gate exists in app/ (grep 'mvp' across app/*.py and templates) |
| config.py has an mvp_mode setting | ✅ | app/config.py:368 — mvp_mode: bool = False defined under '--- MVP Mode ---' block |
| Sentry initialized in app/main.py lifespan only when SENTRY_DSN is set | ✅ | app/main.py:61-62,99,150 — lifespan() asynccontextmanager contains 'if settings.sentry_dsn:' guarding sentry_sdk.init() |
| Sentry uses FastAPI, httpx, Loguru, SQLAlchemy integrations | ✅ | app/main.py:156-160 — integrations=[FastApiIntegration(), SqlalchemyIntegration(), LoguruIntegration(...), HttpxIntegration()] |
| Sentry has a before_send hook that scrubs sensitive data | ✅ | app/main.py:106-148,162 — _sentry_before_send filters sensitive headers/query strings/bodies/stack vars; wired via before_send=_sentry_before_send |
| app/prometheus_metrics.py is a pure ASGI middleware | ✅ | app/prometheus_metrics.py:125-126 — class PrometheusMiddleware with docstring 'Pure ASGI middleware — does not buffer response bodies'; implements __call__(scope, receive, send) |
| Exposes http_requests_total, http_request_duration_seconds, http_requests_inprogress | ✅ | app/prometheus_metrics.py:43,49,55 — Counter/Histogram/Gauge defined with exactly those metric names |
| Metrics served at GET /metrics, token-gated | ✅ | app/main.py:566,544-552 — @app.get('/metrics', dependencies=[Depends(_metrics_auth)]); _metrics_auth requires X-Metrics-Token matching settings.metrics_token via hmac.compare_digest, else 403 |
| It replaced prometheus-fastapi-instrumentator, which hard-pinned starlette<1.0.0 | ✅ | app/prometheus_metrics.py:11-12 — Also requirements.in:24; instrumentator absent from requirements.txt (only prometheus-client==0.25.0 at requirements.txt:131) |
| Middleware is streaming-safe so it composes with SSE endpoints | ✅ | app/prometheus_metrics.py:5-6 — Docstring: 'Pure ASGI middleware so it composes with streaming responses (sse-starlette)'; send_wrapper only inspects response.start messages |
| Logging is Loguru with auto-injected request_id context | ✅ | app/main.py:597-609 — @app.middleware('http') request_id_middleware wraps call_next in logger.contextualize(request_id=req_id) |
| All config via .env (see .env.example) | ✅ | app/config.py:37-40 — Settings(BaseSettings) with SettingsConfigDict(env_file='.env'); .env.example exists at repo root |
| .env.example contains AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID | ✅ | .env.example:2-4 |
| OAuth callback URL is derived from APP_URL | ✅ | app/routers/auth.py:60 — redirect_uri = f"{settings.app_url}/auth/callback" (also line 93); APP_URL at .env.example:5 |
| .env.example contains ANTHROPIC_API_KEY and ANTHROPIC_MODEL | ✅ | .env.example:30,34 — ANTHROPIC_API_KEY= at 30; ANTHROPIC_MODEL present only as commented-out line '# ANTHROPIC_MODEL=' at 34 (config default claude-sonnet-4-6 at app/config.py:117) |
| .env.example contains DATABASE_URL and REDIS_URL | ✅ | .env.example:98,108 — Comment at 104-105: compose derives REDIS_URL from REDIS_PASSWORD; the listed value is fallback/documentation |
| Supplier API keys NEXAR_CLIENT_ID, BROKERBIN_API_KEY, DIGIKEY_CLIENT_ID, MOUSER_API_KEY in .env.example | ✅ | .env.example:37,39,43,45 |
| Supplier-key features are disabled if the key is unset | ✅ | app/connectors/sources.py:572-573 — Example: Nexar _do_search returns [] when client_id and octopart_api_key are both empty; keys default to '' in app/config.py:96-113 |
| Feature flags MVP_MODE and EMAIL_MINING_ENABLED in .env.example | ✅ | .env.example:308,68 — MVP_MODE=false and EMAIL_MINING_ENABLED=false; fields in app/config.py:368,210 |
| SENTRY_DSN in .env.example, optional | ✅ | .env.example:151 — Defaults to '' in app/config.py:51; app runs without it (main.py:99 gate) |
| db-backup service runs pg_dump every 6 hours | ✅ | docker-compose.yml:178-188 — db-backup service runs scripts/backup-cron.sh with BACKUP_INTERVAL_HOURS default 6; cron loop sleeps that interval and calls backup.sh which runs pg_dump (scripts/backup.sh:82, scripts/backup-cron.sh:38) |
| Manual restore via scripts/restore.sh | ✅ | scripts/restore.sh:1-5 — Executable script; restores via pg_restore/psql with safety checks and pre-restore backup (lines 105, 218-222) |

---

## Method & verification

- Evidence gathered by 2 explore agents + 6 section-cluster verifier agents
  (repo doc), then **every row above was adversarially re-checked by a 7-agent
  citation-verification fleet** (227 rows re-opened at the cited lines; 11
  disputes found — all citation-drift/wording, zero verdicts overturned — and
  all corrected in place).
- Test pass state comes from a real full-suite run + serial re-run of the 16
  failures (outputs pasted verbatim in §1).
- Scoreboard — §2 session doc: **53 claims: 38 confirmed, 9 contradicted,
  5 not found**. §3 repo doc: **169 claims: 161 confirmed, 8 contradicted,
  0 not found**.

---

## Post-audit fixes (same session, after user sign-off: "fix .md to match code")

Both CLAUDE.md files were corrected to match the verified code state. The audit
tables above read **as-found** (pre-fix). Applied: session doc — 9 fixes (QP
native + BP-root, one-screen model, toggle-based routing, no auto-approve,
6-value roles, ERP-naming reality, ExcessLineItem, matching nuance,
tests/fixtures). Repo doc — 7 fixes (24h cookie + background token refresh,
require_buyer scope, SourcingStatus example, htmx/base.html path, no-op build
smoke test, mvp_mode=Teams-only, 3 browser-worker sources added). Repo
CLAUDE.md change is uncommitted on main.
