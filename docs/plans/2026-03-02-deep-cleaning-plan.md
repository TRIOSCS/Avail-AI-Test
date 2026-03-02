# Deep Cleaning Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Full-stack deep cleaning — split oversized files, consolidate duplicates, modernize patterns, clean frontend, reorganize tests, harden security.

**Architecture:** Hybrid approach — split oversized files into cohesive sub-modules within existing directories, consolidate duplicate service patterns, modernize SQLAlchemy queries, modularize frontend JS via Vite, and tighten CSP. Clean break — no backward-compat re-exports.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2, vanilla JS + Vite, PostgreSQL 16, pytest

**Concurrency:** Tasks 1–5 are sequential (backend routers). Tasks 6–8 (services) depend on Task 5. Tasks 9–11 (frontend) are independent. Tasks 12–14 (tests) are independent. Task 15 (security) is independent. **Run tasks 9–15 concurrently with tasks 6–8.**

---

## Phase 1: Split Oversized Routers

### Task 1: Split vendors.py (2,289 lines → 4 files + helpers)

**Files:**
- Read: `app/routers/vendors.py`
- Create: `app/routers/vendors_crud.py` (vendor CRUD + reviews, ~480 lines)
- Create: `app/routers/vendor_contacts.py` (contact lookup + CRUD + metrics, ~580 lines)
- Create: `app/routers/materials.py` (material cards CRUD + operations, ~485 lines)
- Create: `app/routers/vendor_analytics.py` (offer history + parts summary, ~230 lines)
- Create: `app/utils/vendor_helpers.py` (shared helpers: `get_or_create_card`, `card_to_dict`, `clean_emails`, `clean_phones`, `is_private_url`, `scrape_website_contacts`, `merge_contact_into_card`, `_load_entity_tags`, `_background_enrich_vendor`)
- Modify: `app/main.py:914-920` (replace single `vendors_router` with 4 new routers)
- Delete: `app/routers/vendors.py`

**Step 1: Create vendor_helpers.py**
Extract these helper functions from vendors.py:
- `get_or_create_card()` (lines 77–149)
- `_background_enrich_vendor()` (lines 152–183)
- `_load_entity_tags()` (lines 186–204)
- `card_to_dict()` (lines 207–332)
- `clean_emails()` (lines 689–705)
- `clean_phones()` (lines 708–720)
- `is_private_url()` (lines 723–734)
- `scrape_website_contacts()` (lines 737–804)
- `merge_contact_into_card()` (lines 807–827)
- Constants: `_EMAIL_RE`, `_JUNK_EMAILS`, `_JUNK_DOMAINS` (lines 651–686)

Include all necessary imports from vendors.py that these helpers need.

**Step 2: Create vendors_crud.py**
Move these routes (keeping `router = APIRouter(tags=["vendors"])`):
- `check_vendor_duplicate()` (lines 335–387)
- `list_vendors()` (lines 389–493)
- `autocomplete_names()` (lines 494–547)
- `get_vendor()` (lines 548–556)
- `update_vendor()` (lines 557–580)
- `toggle_blacklist()` (lines 581–596)
- `delete_vendor()` (lines 597–609)
- `add_review()` (lines 610–630)
- `delete_review()` (lines 631–831)

Import helpers from `app.utils.vendor_helpers`.

**Step 3: Create vendor_contacts.py**
Move these routes (tag: `["vendors"]`):
- `lookup_vendor_contact()` (lines 833–979)
- `bulk_vendor_contacts()` (lines 980–1026)
- `list_vendor_contacts()` (lines 1027–1062)
- `get_contact_timeline()` (lines 1063–1102)
- `get_contact_nudges()` (lines 1103–1118)
- `get_contact_summary()` (lines 1119–1132)
- `log_contact_call()` (lines 1133–1169)
- `add_vendor_contact()` (lines 1170–1214)
- `update_vendor_contact()` (lines 1215–1258)
- `delete_vendor_contact()` (lines 1259–1281)
- `vendor_email_metrics()` (lines 1282–1345)
- `add_email_to_card()` (lines 1346–1526)

**Step 4: Create materials.py**
Move these routes (tag: `["vendors"]`):
- `list_materials()` (lines 1527–1624)
- `get_material()` (lines 1625–1632)
- `get_material_by_mpn()` (lines 1633–1642)
- `update_material()` (lines 1643–1678)
- `enrich_material()` (lines 1679–1717)
- `delete_material()` (lines 1718–1741)
- `restore_material()` (lines 1742–1766)
- `merge_material_cards()` (lines 1767–1910)
- `import_stock_list_standalone()` (lines 1911–2037)

**Step 5: Create vendor_analytics.py**
Move these routes (tag: `["vendors"]`):
- `get_vendor_offer_history()` (lines 2038–2094)
- `get_vendor_confirmed_offers()` (lines 2095–2145)
- `get_vendor_parts_summary()` (lines 2146–2270)
- `analyze_vendor_materials()` (lines 2271–2289)

**Step 6: Update main.py**
Replace:
```python
from .routers import vendors as vendors_mod
vendors_router = vendors_mod.router
app.include_router(vendors_router)
```
With:
```python
from .routers import vendors_crud, vendor_contacts, materials, vendor_analytics
app.include_router(vendors_crud.router)
app.include_router(vendor_contacts.router)
app.include_router(materials.router)
app.include_router(vendor_analytics.router)
```

**Step 7: Run tests**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers_vendors.py -v
```
Expected: All pass. Fix any import errors.

**Step 8: Run full test suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q
```
Expected: 7,296+ pass.

**Step 9: Delete vendors.py and commit**
```bash
rm app/routers/vendors.py
git add app/routers/vendors_crud.py app/routers/vendor_contacts.py app/routers/materials.py app/routers/vendor_analytics.py app/utils/vendor_helpers.py app/main.py
git add -u  # captures the deletion
git commit -m "refactor: split vendors.py (2289 lines) into 4 domain routers + helpers"
```

---

### Task 2: Split requisitions.py (1,648 lines → 4 files)

**Files:**
- Read: `app/routers/requisitions.py`
- Create: `app/routers/requisitions_crud.py` (~550 lines — CRUD + helpers `_build_requisition_list`, `_compute_sourcing_score`)
- Create: `app/routers/requirements_crud.py` (~400 lines — line item CRUD + upload + `_enqueue_ics_nc_batch`)
- Create: `app/routers/requirements_search.py` (~300 lines — search + sightings + stock import)
- Create: `app/routers/requisitions_attachments.py` (~250 lines — all attachment endpoints)
- Modify: `app/main.py` (replace single `reqs_router` with 4 new routers)
- Delete: `app/routers/requisitions.py`

**Step 1: Create requisitions_crud.py**
Move routes + helpers:
- `_compute_sourcing_score()` (lines 79–91)
- `_build_requisition_list()` (lines 126–449)
- `requisition_counts()` (lines 94–107)
- `list_requisitions()` (lines 108–125)
- `create_requisition()` (lines 465–484)
- `toggle_archive()` (lines 485–498)
- `bulk_archive()` (lines 499–512)
- `dismiss_new_offers()` (lines 513–524)
- `update_requisition()` (lines 525–546)
- `requisition_sourcing_score()` (lines 450–464)

**Step 2: Create requirements_crud.py**
Move routes:
- `_enqueue_ics_nc_batch()` (lines 1020–1038)
- `list_requirements()` (lines 547–622)
- `add_requirements()` (lines 623–776)
- `upload_requirements()` (lines 777–919)
- `delete_requirement()` (lines 920–932)
- `update_requirement()` (lines 933–1019)

**Step 3: Create requirements_search.py**
Move routes:
- `search_all()` (lines 1041–1107) — includes rate limiter
- `search_one()` (lines 1108–1137) — includes rate limiter
- `get_saved_sightings()` (lines 1138–1274)
- `mark_unavailable()` (lines 1275–1294)
- `import_stock_list()` (lines 1295–1395)

**Step 4: Create requisitions_attachments.py**
Move all 6 attachment routes (lines 1396–1648).

**Step 5: Update main.py and run tests**
Replace single router with 4. Run full suite.

**Step 6: Commit**
```bash
git commit -m "refactor: split requisitions.py (1648 lines) into 4 domain routers"
```

---

### Task 3: Split dashboard.py (1,533 lines → 3 files)

**Files:**
- Create: `app/routers/dashboard_summary.py` (~600 lines — needs_attention, attention_feed, morning_brief)
- Create: `app/routers/dashboard_analytics.py` (~800 lines — buyer_brief, team_leaderboard, unified_leaderboard, scoring_info)
- Create: `app/routers/dashboard_alerts.py` (~130 lines — hot_offers, reactivation_signals + helpers `_ensure_aware`, `_age_label`)
- Modify: `app/main.py` (replace `dashboard_router`)
- Delete: `app/routers/dashboard.py`

**Step 1: Create dashboard_summary.py**
Move: `needs_attention()` (27–183), `attention_feed()` (187–436), `morning_brief()` (440–582).
All use `@cached_endpoint()` and `require_user`.

**Step 2: Create dashboard_analytics.py**
Move: `buyer_brief()` (647–1297), `team_leaderboard()` (1300–1452), `unified_leaderboard()` (1505–1525), `scoring_info()` (1528–1533).
Note: `buyer_brief` is 650 lines — the largest single endpoint. Keep as-is for now.

**Step 3: Create dashboard_alerts.py**
Move: `hot_offers()` (585–644), `reactivation_signals()` (1474–1499).
Move helpers: `_ensure_aware()` (1455–1459), `_age_label()` (1462–1468).

**Step 4: Update main.py, run tests, commit**
```bash
git commit -m "refactor: split dashboard.py (1533 lines) into 3 domain routers"
```

---

### Task 4: Split admin.py (1,125 lines → 4 files)

**Files:**
- Create: `app/routers/admin_users.py` (~130 lines — user CRUD)
- Create: `app/routers/admin_system.py` (~350 lines — config, health, credentials)
- Create: `app/routers/admin_data.py` (~350 lines — integrity, dedup, import, transfer)
- Create: `app/routers/admin_teams.py` (~200 lines — Teams integration)
- Modify: `app/main.py`
- Delete: `app/routers/admin.py`

**Step 1: Create admin_users.py**
Move user CRUD routes (lines 64–129) + local schemas (`CreateUserRequest`, `UserUpdateRequest`).

**Step 2: Create admin_system.py**
Move: config endpoints (134–157), health endpoints (162–273), credential endpoints (278–374).
Move local schema: `ConfigUpdateRequest`.

**Step 3: Create admin_data.py**
Move: integrity (379–435), vendor dedup (440–481), company dedup (486–603), data import (608–800), transfer (986–1107).
Move helper: `_upsert_config()` (1109–1126).
Move local schemas: `VendorMergeRequest`.

**Step 4: Create admin_teams.py**
Move: Teams config/channels/test (813–981).
Move local schema: `TeamsConfigRequest`.

**Step 5: Update main.py, run tests, commit**
```bash
git commit -m "refactor: split admin.py (1125 lines) into 4 domain routers"
```

---

### Task 5: Dissolve v13_features.py (1,003 lines → merge into domain routers)

**Files:**
- Create: `app/routers/activities.py` (~350 lines — activity log + unmatched queue)
- Create: `app/routers/sales.py` (~300 lines — account ownership, notifications, manager digest)
- Modify: `app/routers/prospect_pool.py` (add site/account pool routes, ~350 lines)
- Move: Graph webhook to `app/routers/auth.py` (1 route)
- Move: Strategic toggle to `app/routers/crm/companies.py` (1 route)
- Modify: `app/main.py` (remove v13_router, add activities_router, sales_router)
- Delete: `app/routers/v13_features.py`

**Step 1: Create activities.py**
Move from v13_features.py:
- `_activity_to_dict()` helper (lines 87–108)
- Company activity routes (111–171): list, log call, log note
- Vendor activity routes (173–304): list, log call, log note
- User activity routes (182–193)
- Generic activity routes (195–247): email click, phone call
- Company/vendor activity-status endpoints (385–446)
- Unmatched activity queue (311–383): list, attribute, dismiss

**Step 2: Create sales.py**
Move from v13_features.py:
- `my-accounts` (453–459)
- `at-risk` (461–467)
- `open-pool` (469–475)
- `claim/{company_id}` (477–495)
- `manager-digest` (523–531)
- Notifications CRUD (548–632): list, read, read-all, count
- `_NOTIFICATION_TYPES` constant (533–545)

**Step 3: Merge prospecting routes into prospect_pool.py**
Move from v13_features.py:
- `SITE_CAP_PER_USER = 200` constant (line 792) — **Note: admin.py:1070 imports this, update that import**
- All `/api/prospecting/*` routes (639–1004): pool, claim, release, my-sites, at-risk, owner assign, my-accounts, account sites, capacity

**Step 4: Move webhook to auth.py**
Move Graph webhook handler (lines 46–80) to `app/routers/auth.py`.

**Step 5: Move strategic toggle to crm/companies.py**
Move `/api/companies/{company_id}/strategic` PUT (lines 497–521).

**Step 6: Update imports in admin.py (or admin_data.py)**
Fix: `from app.routers.v13_features import SITE_CAP_PER_USER` → import from `prospect_pool.py`

**Step 7: Update main.py, run tests, commit**
```bash
git commit -m "refactor: dissolve v13_features.py into domain routers (activities, sales, prospect_pool)"
```

---

## Phase 2: Consolidate Duplicate Services

### Task 6: Extract shared enrichment utilities

**Files:**
- Create: `app/services/enrichment_utils.py` (~80 lines)
- Modify: `app/services/enrichment.py`
- Modify: `app/services/customer_enrichment_batch.py`
- Modify: `app/services/deep_enrichment_service.py`

**Step 1: Create enrichment_utils.py**
Extract shared patterns:
- Batch processing helper: `run_enrichment_batch(entities, process_fn, batch_size=50, concurrency=5)` with semaphore + progress logging
- Credential validation: `check_enrichment_credentials(db, source_names: list[str]) -> dict[str, bool]`
- Contact deduplication: `deduplicate_contacts(contacts: list[dict], key="email") -> list[dict]`

**Step 2: Refactor enrichment.py and customer_enrichment_batch.py**
Replace inline batch loops with `run_enrichment_batch()`.
Replace inline credential checks with `check_enrichment_credentials()`.

**Step 3: Run tests**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrichment*.py tests/test_customer_enrichment*.py -v
```

**Step 4: Commit**
```bash
git commit -m "refactor: extract shared enrichment utilities, reduce duplication"
```

---

### Task 7: Extract shared scoring helpers

**Files:**
- Create: `app/services/scoring_helpers.py` (~40 lines)
- Modify: `app/services/avail_score_service.py`
- Modify: `app/services/multiplier_score_service.py`

**Step 1: Create scoring_helpers.py**
Extract `_month_range()` (duplicated in both avail_score and multiplier_score).
Standardize month boundary calculation.

**Step 2: Update both services to import from scoring_helpers**

**Step 3: Run tests, commit**
```bash
git commit -m "refactor: extract shared scoring helpers, deduplicate _month_range"
```

---

### Task 8: Extract shared Teams notification logic from buyplan services

**Files:**
- Create: `app/services/teams_notifications.py` (~100 lines)
- Modify: `app/services/buyplan_service.py` (remove `_post_teams_channel`, `_send_teams_dm`)
- Modify: `app/services/buyplan_v3_notifications.py` (import from teams_notifications instead of buyplan_service)

**Step 1: Create teams_notifications.py**
Extract from buyplan_service.py:
- `_post_teams_channel()`
- `_send_teams_dm()`
- Any shared Teams card formatting helpers

**Step 2: Update imports in buyplan_service.py and buyplan_v3_notifications.py**

**Step 3: Run tests, commit**
```bash
git commit -m "refactor: extract Teams notification logic to shared module"
```

---

## Phase 3: Modernize Backend Patterns

### Task 9: Add limits to unbounded .all() queries

**Files to modify** (add `.limit()` or pagination):
- `app/company_utils.py:35` — `db.query(Company).all()` → add `.limit(5000)`
- `app/vendor_utils.py:206` — `db.query(VendorCard).all()` → add `.limit(5000)`
- `app/routers/enrichment.py:533` — `db.query(User).filter(is_active).all()` → add `.limit(500)`
- `app/routers/enrichment.py:443` — vendor cards with emails → add `.limit(1000)`
- `app/routers/crm/sites.py:251` — contacts per site → add `.limit(500)`
- `app/routers/requisitions.py` (or its successor) — sightings query → add `.limit(5000)`

**Step 1: Add limits to each unbounded query**
For each file, add a reasonable `.limit()` before `.all()`. Use conservative limits that won't break functionality.

**Step 2: Run full test suite**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q
```

**Step 3: Commit**
```bash
git commit -m "fix: add limits to unbounded .all() queries to prevent memory issues"
```

---

### Task 10: Clean up scheduler.py re-exports

**Files:**
- Modify: `app/scheduler.py` (remove backward-compat re-export lines)
- Modify: Any test files that import job functions from `app.scheduler` instead of `app.jobs.*`

**Step 1: Find all imports from app.scheduler that should come from app.jobs**
Search for `from app.scheduler import` and `from app import scheduler` in tests/.

**Step 2: Update imports to point at app.jobs.* modules**

**Step 3: Remove re-export lines from scheduler.py**

**Step 4: Run tests, commit**
```bash
git commit -m "refactor: remove scheduler.py backward-compat re-exports, update test imports"
```

---

## Phase 4: Frontend Cleanup

### Task 11: Split app.js into modules (11,560 lines → 11 modules)

**Files:**
- Create: `app/static/modules/` directory
- Create: `app/static/modules/bootstrap.js` (~200 lines — config, localStorage, mobile detection)
- Create: `app/static/modules/utils.js` (~700 lines — debounce, escape, formatting, visuals)
- Create: `app/static/modules/navigation.js` (~800 lines — view management, hash routing)
- Create: `app/static/modules/requisitions.js` (~2,700 lines — detail view, sightings, offers)
- Create: `app/static/modules/vendors.js` (~1,400 lines — vendor list, contacts)
- Create: `app/static/modules/materials.js` (~300 lines — material list, filtering)
- Create: `app/static/modules/dashboard.js` (~2,000 lines — dashboard, metrics, leaderboard)
- Create: `app/static/modules/contacts.js` (~1,200 lines — contact list, drawer)
- Create: `app/static/modules/modals.js` (~1,000 lines — modal/toast state)
- Create: `app/static/modules/health.js` (~200 lines — M365, API monitoring)
- Create: `app/static/modules/search.js` (~800 lines — search UI, forms)
- Modify: `app/static/app.js` (becomes hub: imports from modules/ and re-exports for crm.js)
- Modify: `vite.config.js` (add `modules` alias if needed)

**Step 1: Create modules/ directory**
```bash
mkdir -p app/static/modules
```

**Step 2: Extract modules bottom-up**
Start with leaf modules (no dependencies on other modules):
1. `bootstrap.js` — lines 1–225
2. `utils.js` — lines 229–553
3. `health.js` — lines 725–820
4. `modals.js` — lines 2373–2406
Then extract the domain modules that depend on utils:
5. `navigation.js`, `search.js`, `dashboard.js`, `requisitions.js`, `vendors.js`, `materials.js`, `contacts.js`

**Step 3: Convert app.js to hub**
```javascript
// app.js — hub module
export * from './modules/bootstrap.js';
export * from './modules/utils.js';
export * from './modules/navigation.js';
// ... etc for all modules
```

**Step 4: Verify Vite build**
```bash
cd /root/availai && npx vite build
```
Ensure dist/ output includes all module chunks.

**Step 5: Test in browser** (manual verification)
Verify all views load: dashboard, requisitions, vendors, materials, contacts, settings.

**Step 6: Commit**
```bash
git commit -m "refactor: split app.js (11560 lines) into 11 domain modules"
```

---

### Task 12: Split crm.js into modules (8,378 lines → 7 modules)

**Files:**
- Create: `app/static/modules/crm/` directory
- Create: `app/static/modules/crm/state.js` (~300 lines — currency, cache, debounced handlers)
- Create: `app/static/modules/crm/customers.js` (~2,700 lines — list, filtering, bulk actions)
- Create: `app/static/modules/crm/customer-drawer.js` (~1,500 lines — detail drawer, tabs)
- Create: `app/static/modules/crm/offers.js` (~800 lines — offer gallery, lightbox)
- Create: `app/static/modules/crm/quotes.js` (~1,100 lines — quote CRUD, email)
- Create: `app/static/modules/crm/buyplans.js` (~2,300 lines — v2/v3 workflows)
- Create: `app/static/modules/crm/enrichment.js` (~800 lines — AI panel, activity logging)
- Modify: `app/static/crm.js` (becomes hub importing from modules/crm/)

**Step 1–4:** Same pattern as Task 11.

**Step 5: Commit**
```bash
git commit -m "refactor: split crm.js (8378 lines) into 7 domain modules"
```

---

### Task 13: CSS cleanup

**Files:**
- Modify: `app/static/styles.css`
- Modify: `app/static/mobile.css`
- Modify: `app/templates/index.html`

**Step 1: Replace inline style="display:none" with .u-hidden class**
Add to styles.css:
```css
.u-hidden { display: none; }
```
Then in index.html, replace all 143+ `style="display:none"` with `class="u-hidden"`.
In JS files, replace `el.style.display = 'none'` with `el.classList.add('u-hidden')` and `el.style.display = ''` with `el.classList.remove('u-hidden')`.

**Step 2: Eliminate !important overrides**
Audit all 15+ `!important` declarations. Fix specificity issues at the source instead.

**Step 3: Extract hardcoded colors to CSS variables**
Find all hex/rgb colors used more than once. Add to `:root {}` block and replace.

**Step 4: Remove duplicate CSS rules**
Remove duplicate `.btn-danger` (line 816) and any other duplicates found.

**Step 5: Vite build + commit**
```bash
git commit -m "refactor: CSS cleanup — .u-hidden class, remove !important, extract color variables"
```

---

## Phase 5: Test Reorganization

### Task 14: Split test_scheduler.py to match jobs/ structure

**Files:**
- Read: `tests/test_scheduler.py` (5,365 lines)
- Create: `tests/test_jobs_core.py`
- Create: `tests/test_jobs_email.py`
- Create: `tests/test_jobs_enrichment.py`
- Create: `tests/test_jobs_health.py`
- Create: `tests/test_jobs_inventory.py`
- Create: `tests/test_jobs_maintenance.py`
- Create: `tests/test_jobs_offers.py`
- Create: `tests/test_jobs_prospecting.py`
- Create: `tests/test_jobs_selfheal.py`
- Create: `tests/test_jobs_tagging.py`
- Keep: `tests/test_scheduler.py` (reduced to scheduler config tests only, ~200 lines)

**Step 1: Identify test boundaries**
Map each test class/function to its job domain using function names and imports.

**Step 2: Create domain test files**
Move tests to matching files. Update imports to point at `app.jobs.*` (not `app.scheduler`).

**Step 3: Keep scheduler config tests**
UTC conversion tests, configuration tests, and `_traced_job` tests stay in test_scheduler.py.

**Step 4: Run full suite to verify nothing lost**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_jobs_*.py tests/test_scheduler.py -v --tb=short
```

**Step 5: Commit**
```bash
git commit -m "refactor: split test_scheduler.py (5365 lines) into 10 domain test files matching jobs/"
```

---

### Task 15: Split test_routers_vendors.py and test_routers_crm.py

**Files:**
- Read: `tests/test_routers_vendors.py` (3,513 lines)
- Read: `tests/test_routers_crm.py` (4,071 lines)
- Create: `tests/test_routers_vendors_crud.py`
- Create: `tests/test_routers_vendor_contacts.py`
- Create: `tests/test_routers_materials.py`
- Create: `tests/test_routers_vendor_analytics.py`
- Create domain-matching test files for CRM sub-routers

**Step 1: Split vendor tests to match new router structure**
Map test functions to the 4 new vendor router files.

**Step 2: Split CRM tests by sub-domain**
Map test functions to companies, quotes, offers, buy plans, etc.

**Step 3: Run full suite, commit**
```bash
git commit -m "refactor: split vendor and CRM test files to match new router structure"
```

---

### Task 16: Fix flaky tests + fill coverage gaps

**Files:**
- Modify: Tests for `test_delete_requirement` (add DB state isolation)
- Modify: Tests for `TestAiGate` (3 tests — reset `_last_api_failure` + `_classification_cache`)
- Create: New tests for `buyplan_v3_notifications.py` (currently 19% → target 100%)

**Step 1: Fix 4 flaky order-dependent tests**
Add proper fixture isolation — reset module-level state in setUp/tearDown.

**Step 2: Add buyplan_v3_notifications tests**
Write 40–50 tests covering all `notify_v3_*` functions. Mock Teams webhook, GraphClient, token validation.

**Step 3: Run full suite with coverage**
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```
Expected: 100% or as close as possible.

**Step 4: Commit**
```bash
git commit -m "test: fix 4 flaky tests, add buyplan_v3_notifications coverage (19% → 100%)"
```

---

## Phase 6: Security Hardening

### Task 17: Tighten CSP — remove unsafe-inline

**Files:**
- Read: `app/main.py:301-319` (CSP middleware)
- Modify: `app/main.py` (remove `'unsafe-inline'` from script-src and style-src)
- Modify: `app/templates/index.html` (add `nonce="{{ csp_nonce }}"` to all `<script>` and `<style>` tags)
- Modify: `app/static/styles.css` (move any remaining inline styles to classes)

**Step 1: Verify CSP nonce is already generated**
Check `main.py` — nonce is generated at line 307: `secrets.token_urlsafe(16)` and stored in `request.state.csp_nonce`. Good.

**Step 2: Add nonce to all script/style tags in index.html**
Replace:
```html
<script src="..."></script>
```
With:
```html
<script nonce="{{ csp_nonce }}" src="..."></script>
```
Same for `<style>` and `<link rel="stylesheet">` tags.

**Step 3: Remove unsafe-inline from CSP**
In main.py CSP middleware, change:
- `script-src 'self' 'unsafe-inline'` → `script-src 'self' 'nonce-{nonce}'`
- `style-src 'self' 'unsafe-inline'` → `style-src 'self' 'nonce-{nonce}'`

**Step 4: Move any remaining inline styles to CSS**
Search index.html for `style="..."` attributes. Move to CSS classes.

**Step 5: Test locally**
```bash
docker compose up -d --build && docker compose logs -f app --tail=50
```
Verify no CSP violations in browser console.

**Step 6: Commit**
```bash
git commit -m "security: remove unsafe-inline from CSP, use nonce-based policy"
```

---

## Execution Summary

| Task | Phase | Depends On | Est. Complexity | Can Parallelize With |
|------|-------|-----------|----------------|---------------------|
| 1 | Router Split | — | High | — |
| 2 | Router Split | Task 1 | High | — |
| 3 | Router Split | Task 2 | Medium | — |
| 4 | Router Split | Task 3 | Medium | — |
| 5 | Router Split | Task 4 | High | — |
| 6 | Services | Task 5 | Medium | Tasks 11–17 |
| 7 | Services | Task 5 | Low | Tasks 11–17 |
| 8 | Services | Task 5 | Low | Tasks 11–17 |
| 9 | Backend | Task 5 | Low | Tasks 11–17 |
| 10 | Backend | Task 5 | Low | Tasks 11–17 |
| 11 | Frontend | — | High | Tasks 1–10, 14–17 |
| 12 | Frontend | Task 11 | High | Tasks 1–10, 14–17 |
| 13 | Frontend | Task 12 | Medium | Tasks 1–10, 14–17 |
| 14 | Tests | — | Medium | Tasks 1–13 |
| 15 | Tests | Task 5 | Medium | Tasks 6–13 |
| 16 | Tests | Task 15 | Medium | Tasks 6–13 |
| 17 | Security | — | Medium | Tasks 1–16 |

**Maximum parallelism:** After Task 5, run Tasks 6–10 as one stream and Tasks 11–17 as independent concurrent agents.
