# AvailAI MVP Strip-Down Design

**Date**: 2026-03-13
**Goal**: Strip AvailAI to a focused MVP — manage requisitions, customers, vendors, and a world-class sourcing engine. Clean cuts, no dead code.

---

## Guiding Principles

1. **Clean cuts** — when something goes, it goes completely. No commented-out code, no deprecated stubs.
2. **Surgical removal** — delete from existing repo, keep git history, migrations, and deployment pipeline.
3. **If the user needs it later, they rebuild it** — don't preserve "just in case."

---

## What Stays

### Full Sourcing Engine (enhanced — see "World Class Sourcing Engine" section below)
- 4 search layers fire simultaneously: Live Stock, Historical Sightings, Vendor Affinity (new), AI Research (enhanced)
- Unified confidence scoring (0-100%) across all result types
- Material cards with manufacturer data and enrichment
- AI classification — auto-tagging parts by commodity type, confidence scoring, Nexar validation
- Email mining — scanning Outlook for vendor offers/stock lists, parsing attachments
- NC/ICS workers — background search workers that continuously refresh availability

### Requisitions & Requirements
- Full CRUD, archive, outcomes, sourcing score
- Requirement child records, attachment handling
- Task management on requisitions

### Companies & Customer Sites
- Company CRUD with owners, tags, denormalized counts
- Customer site management
- Site contacts

### Vendor Cards & Contacts
- Vendor master with health scoring, normalized dedup
- Contact management with bulk ops
- Vendor analytics and scorecards

### Quotes & Offers
- Quote lifecycle, line items, follow-up alerts
- Vendor offers with AI analysis and import

### Buy Plans (v1 + v3 merged into one)
- Phase 2 of this project merges the two systems into a single clean buy plan workflow

### Prospecting
- Prospect discovery (Apollo, email-based, Explorium)
- Prospect scoring, signals, pool management
- Warm intros, claim/unclaim, scheduling

### Activity Tracking
- Activity logging and timeline
- Engagement scoring, click-to-call

### Email Intelligence
- Fact extraction from email threads (durable facts for CRM context)

### RFQ Response Parser
- Claude parses vendor email replies into structured offers

### AI Part Normalizer
- Normalize part numbers via Claude Haiku

### Enrichment (redesigned — Phase 4)
- On-demand only — single "Enrich" button on customer/vendor cards
- All sources fire in parallel (Lusha, Hunter, Apollo, Clearbit, RocketReach, Gradient)
- Claude orchestrates — reviews all results, picks best data, merges intelligently
- 90% confidence gate — only data Claude rates >=90% confident gets applied
- No background enrichment jobs — user-initiated only

### 8x8 VoIP (strengthened — Phase 4)
- Call logging, CDR polling, click-to-call

### M365/Outlook (strengthened — Phase 4)
- Inbox scanning, email mining, Graph API for sending RFQs, contact sync

### Tags & Classification
- Material tagging system, prefix lookup, Nexar validation, AI batch classification

---

## What Gets Deleted

### Dashboard (entire module)
- KPI overview, drill-downs, multi-perspective views
- Leaderboard, rankings, scorecards
- Morning briefing service (pre-computed 6AM briefings)
- **Files**: `app/routers/dashboard/` (5 files), `app/services/dashboard_briefing.py`

### Deal Risk Predictor
- 5-signal scoring on at-risk deals
- **Files**: `app/services/deal_risk.py`

### AI Quote Analyzer
- Quote comparison and recommendation
- **Files**: `app/services/ai_quote_analyzer.py`

### AI Email Drafter
- Auto-compose sales emails via Claude
- **Files**: `app/services/ai_email_drafter.py`

### Smart Notification Intelligence
- 5-tier priority classification (critical/high/medium/low/noise)
- Two-stage classification (rule-based + Claude Haiku)
- Staleness detection, quiet hours, engagement-based downgrade
- Batch digest processing
- **Files**: `app/services/notify_intelligence.py`, `app/services/activity_insights.py`, `app/jobs/notify_intelligence_jobs.py`

### Teams Bot
- Conversational bot with intent classification (7 intents)
- Query handlers (pipeline, quotes, deals, vendors, companies, risk, help)
- Redis conversation context
- **Files**: `app/routers/teams_bot.py`, `app/services/teams_bot_service.py`

### Teams Alerts
- Webhook configuration, rule builder, alert routing
- **Files**: `app/routers/teams_alerts.py`, `app/services/teams_alert_service.py`, `app/jobs/teams_alert_jobs.py`
- **KEEP**: `app/services/teams.py` (channel posting, card formatting — used by buy plan notifications and admin) and `app/services/teams_notifications.py` (helpers) and `app/routers/teams_actions.py` (buy plan approval cards)

### Self-Heal Pipeline (entire system)
- Trouble tickets, AI diagnosis, patch generation
- Execution service, rollback, pattern tracker
- Cost controller, find trouble, site tester
- Host watcher script
- **Files**: `app/routers/trouble_tickets.py`, `app/services/{trouble_ticket_service,diagnosis_service,execution_service,patch_generator,rollback_service,cost_controller,pattern_tracker,find_trouble_service,site_tester,file_mapper,prompt_generator,test_prompts,ai_trouble_prompt,ticket_consolidation}.py`, `scripts/self_heal_watcher.sh`, `scripts/apply_patches.py`

### MPN Resurfacing Hints
- Inline MPN hints in requisition views
- **Files**: `app/services/resurfacing_service.py`

### Explorium Router
- Redundant — basic API integration, MCP available if needed
- **Files**: `app/routers/explorium.py`

### Notification System (in-app)
- In-app notification bell, engagement tracking, priority levels
- **Files**: `app/routers/notifications.py`, `app/services/notification_service.py`

---

## World Class Sourcing Engine

### Overview

Two entry points into sourcing:
1. **Requisition flow** — bulk parts from customer (manual entry, paste with AI cleanup, Excel upload with AI cleanup) → sourcing kicks off automatically
2. **Ad-hoc search** — single part number lookup by buyer or salesperson

All four search layers fire simultaneously. Results merged into a single unified list sorted by confidence score.

### Search Layers

| Layer | Source | Confidence Range | What It Finds |
|-------|--------|-----------------|---------------|
| **Live Stock** | 6 API connectors (BB, Nexar, DigiKey, Mouser, OEMSecrets, Element14) | 85-99% | Currently posted inventory |
| **Historical Sightings** | AvailAI sightings table (includes Salesforce import, email-mined stock, past offers) | 40-85% (decays with age) | Previously seen availability |
| **Vendor Affinity** (new) | Claude analysis of vendor history in AvailAI | 30-75% | Vendors likely to carry it based on 3 levels |
| **AI Research** (enhanced) | Claude web search (smart trigger + manual) | 20-60% | Brokers, distributor locators, excess inventory |

### Confidence Scoring

Every result gets a single confidence score (0-100%) — "likelihood this source actually has stock right now."

- **Live API result** — base 90%, modified by: qty vs need, price reasonableness, source reliability history, data freshness
- **Historical sighting** — base 80%, decays ~5% per month, boosted by: repeat sightings from same vendor, vendor response rate history
- **Vendor affinity** — Claude assigns based on reasoning depth:
  - Level 1 (same manufacturer): 40-60%
  - Level 2 (same product family, e.g. "Samsung DDR4 DIMM"): 55-75%
  - Level 3 (same platform/system, e.g. "IBM System x3650 parts"): 30-50%
  - Boosted by: recency of related stock, vendor size/reliability, number of related parts stocked
- **AI research** — Claude assigns 20-60% based on: source credibility, listing recency, specificity of match

### Vendor Affinity Engine (new service)

```
app/services/vendor_affinity_service.py
```

How it works:
1. For searched MPN, extract: manufacturer, product family, platform/system tags (from material card + AI classification)
2. Query sightings + offers + material cards to find vendors who have stocked:
   - **Level 1**: Any part from same manufacturer
   - **Level 2**: Parts in same product family (e.g., "DDR4 DIMM", "SAS HDD", "x86 processor")
   - **Level 3**: Parts for same platform/system (e.g., "IBM System x3650", "Dell PowerEdge R740")
3. Claude reviews the matches with real reasoning depth — not just counting, but understanding vendor specialization patterns
4. Returns ranked vendor suggestions with confidence scores and explanations

### Smart Trigger for AI Research

AI web search fires automatically when:
- Fewer than 5 results from API connectors
- No results under target price
- Part flagged as obsolete or hard-to-find
- Zero sightings in last 6 months

Always available via manual "Search the web" button.

### Unified Results Presentation

Single list sorted by confidence score. Each result shows:
- **Source badge**: Live Stock / Historical / Vendor Match / AI Found
- **Confidence score**: percentage with color coding (green >75%, amber 50-75%, red <50%)
- **Vendor name**, qty, price, lead time (where available)
- **Reasoning** (for affinity/AI results): e.g. "This vendor has stocked 12 Samsung DDR4 modules in the last 6 months"

### Changes to Existing Code

- **`app/services/search_service.py`** — add vendor affinity and AI research as parallel search layers alongside existing connector calls
- **`app/services/vendor_affinity_service.py`** — new service for vendor-part relationship analysis
- **`app/services/scoring.py`** / **`scoring_helpers.py`** — extend to produce unified confidence scores across all four layers
- **`app/connectors/ai_live_web.py`** — enhance with smart trigger logic and better result parsing

---

## Phase 1: Strip (remove cut features)

### Step 1: Delete router files
```
DELETE app/routers/dashboard/          (entire directory — 5 files)
DELETE app/routers/explorium.py
DELETE app/routers/teams_bot.py
DELETE app/routers/teams_alerts.py
DELETE app/routers/trouble_tickets.py
DELETE app/routers/notifications.py
```

### Step 2: Delete service files
```
DELETE app/services/activity_insights.py
DELETE app/services/ai_email_drafter.py
DELETE app/services/ai_quote_analyzer.py
DELETE app/services/ai_trouble_prompt.py
DELETE app/services/cost_controller.py
DELETE app/services/dashboard_briefing.py
DELETE app/services/deal_risk.py
DELETE app/services/diagnosis_service.py
DELETE app/services/execution_service.py
DELETE app/services/file_mapper.py
DELETE app/services/find_trouble_service.py
DELETE app/services/notify_intelligence.py
DELETE app/services/patch_generator.py
DELETE app/services/pattern_tracker.py
DELETE app/services/prompt_generator.py
DELETE app/services/resurfacing_service.py
DELETE app/services/rollback_service.py
DELETE app/services/site_tester.py
DELETE app/services/teams_bot_service.py
DELETE app/services/test_prompts.py
DELETE app/services/ticket_consolidation.py
DELETE app/services/trouble_ticket_service.py
DELETE app/services/notification_service.py
DELETE app/services/teams_alert_service.py
```

### Step 3: Delete job files
```
DELETE app/jobs/notify_intelligence_jobs.py
DELETE app/jobs/teams_alert_jobs.py
```

### Step 4: Delete scripts
```
DELETE scripts/self_heal_watcher.sh
DELETE scripts/apply_patches.py
```

### Step 5: Delete test files (46 files)

**Self-heal & diagnostics (19 files):**
```
DELETE tests/test_activity_insights.py
DELETE tests/test_cost_controller.py
DELETE tests/test_deal_risk.py
DELETE tests/test_diagnosis_service.py
DELETE tests/test_execution_service.py
DELETE tests/test_file_mapper.py
DELETE tests/test_find_trouble.py
DELETE tests/test_notify_intelligence.py
DELETE tests/test_patch_generator.py
DELETE tests/test_pattern_tracker.py
DELETE tests/test_prompt_generator.py
DELETE tests/test_rollback_service.py
DELETE tests/test_scheduler_selfheal.py
DELETE tests/test_selfheal_integration.py
DELETE tests/test_site_tester.py
DELETE tests/test_test_prompts.py
DELETE tests/test_ticket_consolidation.py
DELETE tests/test_trouble_prompt.py
DELETE tests/test_trouble_tickets.py
```

**Dashboard & leaderboard (8 files):**
```
DELETE tests/test_dashboard_briefing.py
DELETE tests/test_dashboard_kpi_all_statuses.py
DELETE tests/test_dashboard_morning_brief.py
DELETE tests/test_dashboard_needs_attention.py
DELETE tests/test_dashboard_attention_feed.py
DELETE tests/test_team_leaderboard.py
DELETE tests/test_unified_leaderboard_endpoint.py
DELETE tests/test_buyer_dashboard.py
```

**AI features (2 files):**
```
DELETE tests/test_email_drafter.py
DELETE tests/test_quote_analyzer.py
```

**Notifications & intelligence (5 files):**
```
DELETE tests/test_notification_service.py
DELETE tests/test_notification_router.py
DELETE tests/test_notifications_overhaul.py
DELETE tests/test_resurfacing.py
DELETE tests/test_teams_bot.py
```

**Teams alerts (5 files):**
```
DELETE tests/test_teams_alert_service.py
DELETE tests/test_teams_alert_requisition.py
DELETE tests/test_teams_alert_vendor_quote.py
DELETE tests/test_teams_alert_director.py
DELETE tests/test_teams_alert_briefing.py
```

**Patches & browser testing (2 files):**
```
DELETE tests/test_apply_patches.py
DELETE tests/test_browser_e2e.py
```

**KEEP** (tests that look similar but test kept systems):
- `test_routers_error_reports.py` — error report router (kept)
- `test_ticket_coordination.py` — basic ticket CRUD (kept)
- `test_tickets_frontend.py` — tickets.js UI (kept)
- `test_routers_teams_actions.py` — Teams approval cards (kept)
- `test_teams_coverage.py` — Teams core functionality (kept)

### Step 6: Clean imports and registrations

**6a. app/main.py** — Remove these specific lines:

| What | Line | Action |
|------|------|--------|
| `from .routers.dashboard import router as dashboard_router` | 605 | DELETE import |
| `from .routers.notifications import router as notifications_router` | 616 | DELETE import |
| `from .routers.teams_alerts import router as teams_alerts_router` | 631 | DELETE import |
| `app.include_router(notifications_router)` | 654 | DELETE registration |
| `app.include_router(dashboard_router)` | 676 | DELETE registration (inside MVP-mode block) |
| `app.include_router(teams_alerts_router)` | 680 | DELETE registration (inside MVP-mode block) |
| `explorium_router` try/except block | 682-686 | DELETE entire block |

**KEEP**: `teams_actions_router` (line 630/679) — handles buy plan approval cards, NOT teams bot

**6b. app/jobs/__init__.py** — Remove these specific lines:

| What | Line | Action |
|------|------|--------|
| `from .teams_alert_jobs import register_teams_alert_jobs` | 42 | DELETE import |
| `register_teams_alert_jobs(scheduler, settings)` | 45 | DELETE call |

Keep the MVP-mode conditional block but remove only the teams_alert_jobs lines. `enrichment_jobs` stays.

**6c. CRITICAL — Kept files that import from deleted modules (11 imports, will crash at runtime):**

| Kept File | Imports From (deleted) | Import Type | Fix |
|-----------|----------------------|-------------|-----|
| `app/services/knowledge_service.py:18` | `notification_service.create_notification` | TOP-LEVEL | Remove import and all `create_notification()` calls |
| `app/services/health_monitor.py:95` | `notification_service.create_notification` | lazy | Remove `_notify_admins()` call or replace with loguru warning |
| `app/email_service.py:587` (note: app root, not services/) | `teams_alert_service.send_alert` | lazy | Remove the teams alert call in `send_batch_rfq()` |
| `app/routers/crm/offers.py:552` | `teams_alert_service.send_alert` | lazy | Remove the teams alert call |
| `app/routers/requisitions/core.py:507` | `teams_alert_service.send_alert_to_role` | lazy | Remove the teams alert call |
| `app/routers/knowledge.py:417` | `resurfacing_service.get_mpn_hints` | lazy | Remove the `/resurfacing/hints` endpoint entirely |
| `app/services/teams_qa_service.py:372` | `teams_bot_service._resolve_user` | lazy | Inline the `_resolve_user` logic (simple user lookup) |
| `app/jobs/knowledge_jobs.py:231` | `dashboard_briefing.generate_briefing` | lazy | Remove briefing block in `_job_deliver_question_batches()` |
| `app/jobs/knowledge_jobs.py:263` | `dashboard_briefing.generate_briefing` | lazy | Remove briefing block in `_job_send_knowledge_digests()` |
| `app/routers/ai.py:648` | `ai_email_drafter.draft_rfq_email` | lazy | Remove the draft email endpoint |
| `app/routers/ai.py:678` | `ai_quote_analyzer.compare_quotes` | lazy | Remove the compare quotes endpoint |

**Also clean up dead code:**
- `app/jobs/knowledge_jobs.py:304` — `_send_briefing_to_teams()` helper becomes dead after briefing removal, delete it

**Verified clean** — no remaining imports from: `deal_risk`, `notify_intelligence`, `trouble_ticket_service`, `find_trouble_service`, `site_tester`, `diagnosis_service`, `execution_service`, `patch_generator`, `rollback_service`, `cost_controller`, `pattern_tracker`, `file_mapper`, `prompt_generator`, `test_prompts`, `ai_trouble_prompt`, `ticket_consolidation`

**6d. app/jobs/knowledge_jobs.py** — Remove:
- `_job_precompute_briefings` function and its scheduler registration
- Briefing import block inside `_job_send_knowledge_digests` (lines ~229-256)
- Keep: fact extraction, knowledge digest delivery (minus briefing block)

**6e. General sweep** — After all deletions, run:
```bash
grep -r "from.*notification_service" app/ --include="*.py"
grep -r "from.*teams_alert_service" app/ --include="*.py"
grep -r "from.*teams_bot_service" app/ --include="*.py"
grep -r "from.*dashboard_briefing" app/ --include="*.py"
grep -r "from.*resurfacing_service" app/ --include="*.py"
grep -r "from.*deal_risk" app/ --include="*.py"
grep -r "from.*notify_intelligence" app/ --include="*.py"
grep -r "from.*trouble_ticket" app/ --include="*.py"
grep -r "from.*find_trouble" app/ --include="*.py"
grep -r "from.*site_tester" app/ --include="*.py"
```
Every match in a kept file must be cleaned up.

### Step 7: Clean frontend references
- **app/static/app.js**: Remove dashboard tab, briefing card, find trouble button, notification bell UI, deal risk badges
- **app/static/crm.js**: Remove dashboard drill-down, briefing cards, risk indicators
- **app/templates/index.html**: Remove dashboard nav items, Teams bot controls, notification bell

### Step 8: Verify
```bash
python -m py_compile app/main.py
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x --tb=short -q
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
docker compose up -d --build && docker compose logs -f app
```

---

## Phase 2: Merge Buy Plans v1 + v3

### Goal
Consolidate `buy_plans.py` (v1) and `buy_plans_v3.py` (v3) into a single `buy_plans.py` with one unified workflow.

### Approach
- Audit both versions to identify the superset of functionality
- Keep the v3 workflow (newer, has notifications) as the base
- Pull in any v1-only features that are still needed
- Single router, single service, single set of endpoints
- Migrate any v1-specific frontend calls to the unified API
- Delete v1-specific files after merge

### Files affected
```
MERGE  app/routers/crm/buy_plans.py + buy_plans_v3.py → buy_plans.py
MERGE  app/services/buyplan_service.py + buy_plan_v3_service.py → buyplan_service.py
MERGE  app/services/buyplan_notifications.py + buyplan_v3_notifications.py → buyplan_notifications.py
KEEP   app/services/buyplan_builder.py (if still needed)
KEEP   app/services/buyplan_po.py
KEEP   app/services/buyplan_scoring.py
KEEP   app/services/buyplan_workflow.py
UPDATE app/schemas/ (unify buy plan schemas)
UPDATE tests/ (merge buy plan tests)
```

---

## Phase 3: Frontend Rewrite — HTMX + Alpine.js

### Goal
Replace 26K lines of vanilla JS (app.js + crm.js) with server-rendered HTMX + Alpine.js. Rock solid, well-partitioned, stable.

### Architecture
- **HTMX** handles all server communication — partial page updates, form submissions, search, infinite scroll
- **Alpine.js** handles client-side interactivity — dropdowns, modals, tabs, form validation, local state
- **Jinja2 partials** — server renders HTML fragments that HTMX swaps in
- **No client-side routing** — server controls all navigation

### Template structure
```
app/templates/
  base.html                    # Shell: nav, sidebar, HTMX/Alpine script tags
  partials/
    requisitions/
      list.html                # Requisition table (HTMX paginated)
      detail.html              # Requisition detail view
      requirement_row.html     # Single requirement row (HTMX swap target)
      search_results.html      # Sourcing results partial
    companies/
      list.html                # Company table with filters
      detail.html              # Company detail drawer/page
      site_row.html            # Customer site row
    vendors/
      list.html                # Vendor list
      detail.html              # Vendor detail with tabs
      contact_row.html         # Contact row
    quotes/
      list.html                # Quote list
      detail.html              # Quote detail with line items
    buy_plans/
      list.html                # Buy plan list
      detail.html              # Buy plan workflow view
    prospecting/
      pool.html                # Prospect pool with filters
      detail.html              # Prospect detail
    sourcing/
      results.html             # Search results grid
      material_card.html       # Material card detail
      sighting_row.html        # Sighting row
    shared/
      modal.html               # Reusable modal shell
      toast.html               # Toast notifications
      pagination.html          # Pagination controls
      empty_state.html         # Empty state placeholder
      enrich_button.html       # Enrichment button + results
```

### Migration strategy
- Build new HTMX views alongside existing JS (feature flag `USE_HTMX=true`)
- Migrate one domain at a time: requisitions → companies → vendors → quotes → buy plans → prospecting → sourcing
- Each domain: create partials, add HTMX endpoints to existing routers, test, then remove old JS section
- After all domains migrated, delete app.js + crm.js entirely

### Key patterns
- **Search**: `<input hx-get="/api/requisitions" hx-trigger="keyup changed delay:300ms" hx-target="#results">`
- **Infinite scroll**: `hx-trigger="revealed" hx-get="?page=2"` on sentinel element
- **Modals**: Alpine.js `x-data="{ open: false }"` + HTMX loads content on open
- **Forms**: `hx-post` with `hx-swap="outerHTML"` returns updated row
- **Tabs**: `hx-get="/companies/123/tab/activity"` loads tab content on click
- **Toast notifications**: HTMX `hx-on::after-request` triggers Alpine toast
- **Enrichment button**: `hx-post="/api/enrich/company/123"` with `hx-indicator` spinner, returns enriched card

---

## Phase 4: Strengthen & Optimize

### 4A: On-Demand Enrichment (new system)

**Current**: Background waterfall (Lusha → Hunter → Apollo) with scheduled batch jobs.
**New**: Single "Enrich" button on every customer and vendor card.

#### Flow
1. User clicks "Enrich" on a company or vendor card
2. Backend fires ALL sources in parallel: Lusha, Hunter, Apollo, Clearbit, RocketReach, Gradient
3. Raw results collected from all sources
4. Claude reviews all results together:
   - Deduplicates across sources
   - Picks the most reliable data for each field (phone, email, address, revenue, employee count, etc.)
   - Assigns confidence score (0-100%) to each data point
5. Only data with >=90% confidence gets applied to the record
6. User sees a summary: what was found, what was applied, what was rejected (with reasons)
7. No background enrichment jobs — entirely user-initiated

#### Endpoint
```
POST /api/enrich/{entity_type}/{entity_id}
  entity_type: "company" | "vendor" | "contact"
  Response: { applied: [...], rejected: [...], sources_used: [...], cost: "$0.XX" }
```

#### Service
```
app/services/enrichment_orchestrator.py  (new — replaces waterfall)
  - fire_all_sources(entity_type, entity_id) → raw results
  - claude_merge(raw_results) → merged data with confidence scores
  - apply_confident_data(entity, merged, threshold=0.90) → applied fields
```

### 4B: 8x8 VoIP (strengthen)
- Audit current integration, fix any gaps
- Ensure click-to-call works reliably across all vendor/contact views
- CDR polling: verify data completeness, add retry logic if needed
- Surface call history prominently in vendor/contact detail views

### 4C: M365/Outlook (strengthen)
- Audit inbox scanning reliability, fix timeout/retry issues
- Strengthen email mining: better attachment detection, vendor domain matching
- RFQ send: verify Graph API reliability, add delivery confirmation
- Contact sync: ensure bidirectional sync is clean and dedup-aware

---

## Database Impact

- **No schema changes in Phase 1** — all models kept for DB continuity, orphaned tables remain dormant
- **Phase 2** may add a migration to consolidate buy plan tables (TBD during implementation)
- **Phase 3** adds no schema changes — HTMX endpoints use existing API
- **Phase 4** may add columns to track enrichment source/confidence per field

---

## Models — Keep All

All 88 models are kept. Even models for deleted features (TroubleTicket, SelfHealLog, NotificationEngagement, etc.) remain in the codebase to preserve database schema continuity. The tables stay in PostgreSQL — they just won't be written to anymore.

If cleanup is desired later, a future migration can drop orphaned tables.

---

## Verification Criteria

After each phase:
1. `python -m py_compile app/main.py` — no import errors
2. `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q` — all tests pass
3. `docker compose up -d --build` — app starts clean, no errors in logs
4. Manual smoke test of core flows: search, create requisition, view company, view vendor
