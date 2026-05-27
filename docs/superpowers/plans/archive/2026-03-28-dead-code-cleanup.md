# Dead Code Cleanup & Coverage to 85%

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all dead code to shrink the untested denominator and push coverage toward 85%.

**Architecture:** Ordered by coverage impact (biggest denominator reduction first). Dead router files are the single largest category (~6,400 lines). Each task is a self-contained deletion commit. No new features, no refactors — pure removal.

**Tech Stack:** Python, SQLAlchemy, Jinja2, Alpine.js, CSS, pytest

**Key rule:** After each task, run `pytest --tb=short -q` to ensure no regressions. Dead code removal should never break tests — if it does, we found a false positive.

---

### Task 1: Delete 19 entirely-dead router files + unmount from main.py

These routers have ZERO template/JS references. ~6,400 lines of dead production code.

**Files:**
- Delete: `app/routers/prospect_pool.py`
- Delete: `app/routers/strategic.py`
- Delete: `app/routers/outreach.py`
- Delete: `app/routers/task.py`
- Delete: `app/routers/knowledge.py`
- Delete: `app/routers/tagging_admin.py`
- Delete: `app/routers/nc_admin.py`
- Delete: `app/routers/ics_admin.py`
- Delete: `app/routers/vendor_inquiry.py`
- Delete: `app/routers/emails.py`
- Delete: `app/routers/rfq.py`
- Delete: `app/routers/vendor_analytics.py`
- Delete: `app/routers/crm/buy_plans.py`
- Delete: `app/routers/crm/sites.py`
- Delete: `app/routers/admin/data_ops.py`
- Delete: `app/routers/admin/users.py`
- Delete: `app/routers/v13_features/sales.py`
- Delete: `app/routers/v13_features/prospecting.py`
- Delete: `app/routers/command_center.py`
- Modify: `app/main.py` — remove `include_router()` calls + imports for each deleted router
- Modify: `app/routers/__init__.py` — remove re-exports if any

**IMPORTANT:** `app/routers/v13_features/activity.py` is NOT fully dead — it contains the live `/api/webhooks/graph` endpoint. Do NOT delete that file. Only delete `sales.py` and `prospecting.py` from v13_features.

- [ ] **Step 1: Delete all 19 router files**

```bash
rm app/routers/prospect_pool.py \
   app/routers/strategic.py \
   app/routers/outreach.py \
   app/routers/task.py \
   app/routers/knowledge.py \
   app/routers/tagging_admin.py \
   app/routers/nc_admin.py \
   app/routers/ics_admin.py \
   app/routers/vendor_inquiry.py \
   app/routers/emails.py \
   app/routers/rfq.py \
   app/routers/vendor_analytics.py \
   app/routers/crm/buy_plans.py \
   app/routers/crm/sites.py \
   app/routers/admin/data_ops.py \
   app/routers/admin/users.py \
   app/routers/v13_features/sales.py \
   app/routers/v13_features/prospecting.py \
   app/routers/command_center.py
```

- [ ] **Step 2: Remove imports and include_router() calls from `app/main.py`**

Remove all import lines and `app.include_router(...)` calls for the deleted routers. The routers to remove from main.py are:
- `prospect_pool_router`
- `strategic_router`
- `outreach_router`
- `task_router` and `my_tasks_router`
- `knowledge_router`, `knowledge_insights_router`, `knowledge_sprinkles_router`
- `tagging_admin_router`
- `nc_admin_router`
- `ics_admin_router`
- `vendor_inquiry_router`
- `emails_router`
- `rfq_router`
- `vendor_analytics_router`
- `command_center_router`

Also check `app/routers/__init__.py` and remove any re-exports for deleted routers.

For v13: remove the sales and prospecting imports from `app/routers/v13_features/__init__.py` but keep activity.py and its router.

For crm: remove buy_plans and sites imports from `app/routers/crm/__init__.py`.

For admin: remove data_ops and users imports from `app/routers/admin/__init__.py`.

- [ ] **Step 3: Remove companion test files for deleted routers**

```bash
rm -f tests/test_prospect_pool.py \
     tests/test_strategic_vendors.py \
     tests/test_strategic_vendor_service.py \
     tests/test_outreach.py \
     tests/test_command_center.py \
     tests/test_jobs_task.py \
     tests/test_task_service.py \
     tests/test_routers_knowledge.py \
     tests/test_services_knowledge_service.py \
     tests/test_knowledge_service_comprehensive.py \
     tests/test_jobs_knowledge.py \
     tests/test_tagging_api.py \
     tests/test_nc_admin_router.py \
     tests/test_routers_ics_admin.py \
     tests/test_vendor_inquiry.py \
     tests/test_routers_emails.py \
     tests/test_rfq.py \
     tests/test_routers_rfq.py \
     tests/test_schemas_rfq.py \
     tests/test_phase3c_rfq_draft.py \
     tests/test_rfq_layout_changes.py \
     tests/test_rfq_workspace.py \
     tests/test_sprint6_rfq_depth.py \
     tests/test_routers_vendor_analytics.py \
     tests/test_buy_plan_router.py \
     tests/test_buy_plan_schemas.py \
     tests/test_phase4_sites_contacts.py \
     tests/test_buy_plan_v4_htmx.py
```

Note: Keep `test_buy_plan_models.py` and `test_buy_plan_service.py` if the buy_plan service and models are still alive. Only remove test files that directly test the deleted router endpoints.

Check each test file before deleting — if a test file imports from a non-deleted module, keep it.

- [ ] **Step 4: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

Expected: All previously-passing tests still pass. Some tests may fail if they imported from deleted routers — fix imports or delete those tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove 19 dead router files (~6,400 lines) from old React API layer"
```

---

### Task 2: Delete dead endpoints from partially-dead router files

These routers have some live endpoints and some dead ones. Remove only the dead endpoint functions.

**Files:**
- Modify: `app/routers/ai.py` — remove 15 dead endpoints (keep only `standardize-description`)
- Modify: `app/routers/sources.py` — remove 6 dead endpoints
- Modify: `app/routers/crm/offers.py` — remove 8 dead endpoints
- Modify: `app/routers/crm/companies.py` — remove dead endpoints (keep `typeahead`)
- Modify: `app/routers/crm/enrichment.py` — remove dead endpoints (keep `enrich/company`, `enrich/vendor`)
- Modify: `app/routers/crm/quotes.py` — remove dead endpoints (keep `quotes/{id}/pdf`)
- Modify: `app/routers/vendors_crud.py` — remove dead endpoints
- Modify: `app/routers/vendor_contacts.py` — remove dead endpoints (keep POST/PUT/DELETE contacts, log-call)
- Modify: `app/routers/materials.py` — remove all dead endpoints
- Modify: `app/routers/admin/system.py` — remove dead endpoints (keep PUT `/api/admin/config/{key}`)
- Modify: `app/routers/v13_features/activity.py` — remove dead endpoints (keep `/api/webhooks/graph`)

**CRITICAL:** For each file, read it first. Identify the live endpoints (listed above). Remove all other endpoint functions and their decorators. Keep imports that the live endpoints need.

- [ ] **Step 1: Read each partially-dead router file**
- [ ] **Step 2: Remove dead endpoint functions from each file**
- [ ] **Step 3: Clean up now-unused imports in each modified file**
- [ ] **Step 4: Remove/update test files that only test deleted endpoints**
- [ ] **Step 5: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove ~100 dead endpoints from partially-dead router files"
```

---

### Task 3: Delete 7 dead service/utility files + companion tests

**Files:**
- Delete: `app/services/contact_quality.py` (187 lines)
- Delete: `app/services/engagement_scorer.py` (410 lines)
- Delete: `app/utils/sanitize.py` (51 lines)
- Delete: `app/schemas/errors.py` (13 lines)
- Delete: `app/services/search_worker_base/monitoring.py` (116 lines)
- Delete: `app/services/ics_worker/monitoring.py` (41 lines)
- Delete: `app/services/nc_worker/monitoring.py` (41 lines)
- Delete: `tests/test_contact_quality.py`
- Delete: `tests/test_sanitize.py`
- Delete: `tests/test_schemas_errors.py`
- Delete: `tests/test_services_engagement.py`
- Modify: `app/services/search_worker_base/__init__.py` — remove monitoring re-export if any
- Modify: `app/services/ics_worker/__init__.py` — remove monitoring re-export if any
- Modify: `app/services/nc_worker/__init__.py` — remove monitoring re-export if any

- [ ] **Step 1: Delete files**

```bash
rm app/services/contact_quality.py \
   app/services/engagement_scorer.py \
   app/utils/sanitize.py \
   app/schemas/errors.py \
   app/services/search_worker_base/monitoring.py \
   app/services/ics_worker/monitoring.py \
   app/services/nc_worker/monitoring.py \
   tests/test_contact_quality.py \
   tests/test_sanitize.py \
   tests/test_schemas_errors.py \
   tests/test_services_engagement.py
```

- [ ] **Step 2: Clean up __init__.py re-exports**

Check and remove any re-exports of deleted modules from their parent `__init__.py` files.

- [ ] **Step 3: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove 7 dead service/utility files + 4 companion test files (~860 lines)"
```

---

### Task 4: Delete 8 dead Python functions from live files

**Files:**
- Modify: `app/services/buyer_leaderboard.py` — remove `get_buyer_leaderboard` (line 195), `get_buyer_leaderboard_months` (line 269), `compute_stock_list_hash` (line 284), `check_and_record_stock_list` (line 300)
- Modify: `app/services/customer_enrichment_service.py` — remove `_ensure_site` (line 80), `_dedup_contacts` (line 93), `_save_contact` (line 100)
- Modify: `app/utils/vendor_helpers.py` — remove `_extract_domain_from_name` (line 40)

- [ ] **Step 1: Read each file and remove the dead functions**
- [ ] **Step 2: Clean up now-unused imports in each file**
- [ ] **Step 3: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove 8 dead functions from buyer_leaderboard, customer_enrichment, vendor_helpers"
```

---

### Task 5: Delete 26 dead Pydantic schemas

**Files:**
- Modify: `app/schemas/errors.py` — already deleted in Task 3
- Modify: `app/schemas/responses.py` — remove `BuyPlanListItem`, `VendorScorecardListResponse`, `EnrichmentQueueResponse`
- Modify: `app/schemas/requisitions.py` — remove `RequirementOut`
- Modify: `app/schemas/crm.py` — remove `OfferOut`
- Modify: `app/schemas/buy_plan.py` — remove `BuyPlanSubmit`, `BuyPlanApproval`, `BuyPlanResponse` (and nested `BuyPlanLineResponse`, `AIFlag`), `BuyPlanListItem`, `OfferComparisonResponse` (and nested `OfferComparisonItem`), `VerificationGroupMemberResponse`
- Modify: `app/schemas/ai.py` — remove `ParseEmailResponse` (line 128), `NormalizedPart`, `RfqDraftEmailRequest`, `CompareQuotesRequest`
- Modify: `app/schemas/excess.py` — remove `ExcessLineItemUpdate`, `ExcessLineItemImportRow`, `BidCreate`, `BidSolicitationCreate`
- Modify: `app/schemas/prospect_account.py` — remove `ProspectAccountRead`, `PoolStats`, `ProspectClaimRequest`, `ProspectDismissRequest`, `ProspectFilters`, `ProspectAddRequest`, `DiscoveryBatchRead`

- [ ] **Step 1: Read each schema file**
- [ ] **Step 2: Remove dead schema classes from each file**
- [ ] **Step 3: Clean up now-unused imports**
- [ ] **Step 4: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove 26 dead Pydantic schemas across 7 files"
```

---

### Task 6: Delete dead Celery jobs + no-op scheduled jobs

**Files:**
- Delete: `app/jobs/lifecycle_jobs.py`
- Delete: `tests/test_lifecycle_jobs.py`
- Modify: `app/jobs/__init__.py` — remove `register_lifecycle_jobs` import and call
- Modify: `app/jobs/knowledge_jobs.py` — remove `_job_deliver_question_batches` and `_job_send_knowledge_digests` functions + their scheduler registrations in `register_knowledge_jobs()`
- Modify: `tests/test_jobs_knowledge.py` — remove tests for the two no-op jobs (if this test file still exists after Task 1)

- [ ] **Step 1: Delete lifecycle_jobs files**

```bash
rm app/jobs/lifecycle_jobs.py tests/test_lifecycle_jobs.py
```

- [ ] **Step 2: Remove lifecycle import/call from `app/jobs/__init__.py`**

Read `app/jobs/__init__.py`, find the import of `register_lifecycle_jobs` and its call, remove both.

- [ ] **Step 3: Remove no-op jobs from knowledge_jobs.py**

Read `app/jobs/knowledge_jobs.py`. Remove:
- The two no-op function definitions (`_job_deliver_question_batches`, `_job_send_knowledge_digests`)
- Their scheduler registrations in `register_knowledge_jobs()`

- [ ] **Step 4: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove dead lifecycle_jobs module + 2 no-op knowledge scheduled jobs"
```

---

### Task 7: Delete dead templates + dead JS/CSS/static files

**Files:**
- Delete: `app/templates/htmx/partials/shared/modal.html`
- Delete: `app/templates/htmx/partials/shared/toast.html`
- Delete: `app/static/intake_helpers.mjs`
- Delete: `app/static/sw.js`
- Delete: `app/static/offline.html`
- Delete: `app/static/manifest.json`
- Delete: `public/sw.js` (if exists)
- Delete: `dist/sw.js` (if exists)
- Delete: `public/avail_logo_backup.png` (if exists)
- Delete: `dist/avail_logo_backup.png` (if exists)
- Delete: `public/avail_logo_white_bg_backup.png` (if exists)
- Delete: `public/avail_logo_cropped.png` (if exists)
- Delete: `public/trio_honeycomb.svg` (if exists)
- Delete: `public/trio_logo.png` (if exists)
- Delete: `dist/avail_logo_white_bg_backup.png` (if exists)
- Delete: `dist/avail_logo_cropped.png` (if exists)
- Delete: `dist/trio_honeycomb.svg` (if exists)
- Delete: `dist/trio_logo.png` (if exists)
- Modify: `app/static/htmx_app.js` — remove dead `sourcingProgress` Alpine.data component + dead `preferences` Alpine.store
- Modify: `app/static/styles.css` — remove dead CSS classes: `badge-secondary`, `badge-neutral`, `badge-danger`, `btn-secondary`, `btn-danger`, `btn-sm`, `card`, `card-padded`, `filter-bar`, `col-picker` (+ label variants), `qb-panel-enter`, `sightings-action-bar`, `source-chip--sourcengine`, `source-chip--ebay`
- Modify: `app/static/htmx_mobile.css` — remove dead CSS classes: `responsive-table` (all rules), `filter-btn`, `tab-btn`, `page-header`, `toolbar`, `quick-filters`, `tab-bar`, `mobile-more-menu`, `mobile-more-item`, `mobile-nav-badge`, `drawer-back-btn`, `modal-box`, `modal-backdrop`, `sidebar`, `app-layout`

- [ ] **Step 1: Delete dead template and static files**

```bash
rm -f app/templates/htmx/partials/shared/modal.html \
     app/templates/htmx/partials/shared/toast.html \
     app/static/intake_helpers.mjs \
     app/static/sw.js \
     app/static/offline.html \
     app/static/manifest.json \
     public/sw.js dist/sw.js \
     public/avail_logo_backup.png dist/avail_logo_backup.png \
     public/avail_logo_white_bg_backup.png dist/avail_logo_white_bg_backup.png \
     public/avail_logo_cropped.png dist/avail_logo_cropped.png \
     public/trio_honeycomb.svg dist/trio_honeycomb.svg \
     public/trio_logo.png dist/trio_logo.png
```

- [ ] **Step 2: Remove dead Alpine.js from htmx_app.js**

Read `app/static/htmx_app.js`. Remove:
- The `sourcingProgress` component (lines 218-260 approx)
- The `preferences` store (lines 92-96 approx)
- Any dead icon/SVG templates referenced only by sourcingProgress

- [ ] **Step 3: Remove dead CSS classes from styles.css**

Read `app/static/styles.css`. Remove all classes listed above.

- [ ] **Step 4: Remove dead CSS classes from htmx_mobile.css**

Read `app/static/htmx_mobile.css`. Remove all classes listed above. This file is ~60% dead.

- [ ] **Step 5: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: remove dead templates, JS, CSS, and static assets"
```

---

### Task 8: Remove dead SQLAlchemy columns and relationships

Dead columns and relationships on live models. No migration needed for column removal from the ORM — columns stay in the DB but are removed from the Python model definition. If the column has never been populated, it's pure dead weight.

**Files:**
- Modify: `app/models/vendors.py` — remove columns: `raw_response`, `rma_rate`, `specialty_confidence`, `email_history_scanned_at`
- Modify: `app/models/auth.py` — remove column: `last_deep_email_scan`
- Modify: `app/models/buy_plan.py` — remove column: `migrated_from_v1`; remove relationships: `cancelled_by`, `halted_by`, `po_verified_by`
- Modify: `app/models/crm.py` — remove column: `ownership_cooldown_until`
- Modify: `app/models/offers.py` — remove columns: `match_method`, `teams_alert_sent_at`; remove relationships: `material_card`, `promoted_by`
- Modify: `app/models/intelligence.py` — remove column: `source_url` (on ActivityLog); remove relationships: `vendor_history`, `spec_facets` (on MaterialCard), `material_card` (on ProactiveMatch), `salesperson` (on ProactiveMatch, ProactiveOffer), `site_contact` (on ActivityLog)
- Modify: `app/models/enrichment.py` — remove column: `verified_at` (on ProspectContact)
- Modify: `app/models/excess.py` — remove columns: `demand_score`, `market_price` (on ExcessLineItem), `email_track_id`, `body_preview` (on BidSolicitation), `bidder_contact_id` (on Bid)
- Modify: `app/models/sourcing.py` — remove relationships: `material_card` (on Requirement, Sighting), `vendor_summaries` (on Requirement), `source_company` (on Sighting), `assigned_buyer` (on Requirement)
- Modify: `app/models/purchase_history.py` — remove relationship: `material_card` (on CustomerPartHistory)

**IMPORTANT:** Only remove the Column/relationship() definitions from the Python model class. Do NOT create Alembic migrations to drop columns from the database — the columns can stay in the DB harmlessly. Removing them from the ORM just means SQLAlchemy stops loading them.

- [ ] **Step 1: Read each model file**
- [ ] **Step 2: Remove dead columns and relationships**
- [ ] **Step 3: Clean up now-unused imports (e.g., `relationship` if no longer used in file)**
- [ ] **Step 4: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove 18 dead columns + 17 dead relationships from ORM models"
```

---

### Task 9: Delete 4 dead SQLAlchemy models

**Files:**
- Delete or gut: `app/models/enrichment.py` — remove `EnrichmentCreditUsage` class (keep other classes in the file if any are alive)
- Delete: `app/models/enrichment_run.py`
- Delete or gut: `app/models/notification.py` — remove `Notification` class
- Modify: `app/models/performance.py` — remove `BuyerVendorStats` class (keep other classes in the file)
- Modify: `app/models/__init__.py` — remove re-exports: `EnrichmentCreditUsage`, `EnrichmentRun`, `Notification`, `BuyerVendorStats`

**IMPORTANT:** Do NOT create Alembic migrations to drop tables. The tables can stay in the DB harmlessly. Just remove the ORM classes.

- [ ] **Step 1: Read each model file to understand what else lives there**
- [ ] **Step 2: Remove dead model classes (delete file if the class is the only thing in it)**
- [ ] **Step 3: Remove re-exports from `app/models/__init__.py`**
- [ ] **Step 4: Run tests**

```bash
pytest --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove 4 dead SQLAlchemy model classes"
```

---

### Task 10: Fix phantom references (bonus)

**Files:**
- Modify: `app/routers/htmx_views.py:5266` — fix reference to nonexistent template `htmx/partials/follow_ups/batch_result.html` (either create the template or remove the endpoint that references it)
- Modify: `app/templates/htmx/partials/sightings/vendor_modal.html:94` — fix reference to nonexistent endpoint `/api/ai/clean-rfq-draft` (remove the fetch call or wire it to a real endpoint)

- [ ] **Step 1: Read htmx_views.py around line 5266 to understand the batch_result reference**
- [ ] **Step 2: Read vendor_modal.html around line 94 to understand the clean-rfq-draft reference**
- [ ] **Step 3: Fix both references (likely remove dead code paths)**
- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: remove phantom template and endpoint references"
```

---

## Estimated Impact

| Category | Lines Removed (approx) |
|----------|----------------------|
| Dead router files (Task 1) | ~6,400 |
| Dead endpoints in live routers (Task 2) | ~2,000 |
| Dead service/utility files (Task 3) | ~860 |
| Dead functions (Task 4) | ~200 |
| Dead schemas (Task 5) | ~500 |
| Dead Celery jobs (Task 6) | ~80 |
| Dead templates/JS/CSS/static (Task 7) | ~400 |
| Dead columns/relationships (Task 8) | ~80 |
| Dead models (Task 9) | ~120 |
| **Total** | **~10,600 lines** |

Plus removal of ~30 companion test files, which won't affect coverage % but reduces test suite runtime.
