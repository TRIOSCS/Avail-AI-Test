# AvailAI Code Remediation — Design Spec

**Date:** 2026-03-23
**Scope:** 131 findings across 13 analysis dimensions
**Goal:** Systematically fix security, reliability, performance, and code quality issues discovered via deep-dive audit of the full codebase.

---

## Phase 1: CRITICAL — Security & Data Integrity

**Estimated scope:** ~15 files, ~200 lines changed. All tests must pass after each fix.

### 1.1 Shared DB session in concurrent asyncio.gather (DATA CORRUPTION)

- **File:** `app/search_service.py:177-199`, `app/routers/requisitions/requirements.py:797`
- **Problem:** `search_all` runs N `search_requirement` tasks concurrently via `asyncio.gather`, all sharing one `db` session. SQLAlchemy sessions are not coroutine-safe for concurrent writes.
- **Fix:** Each search task creates its own `SessionLocal()`, commits/closes independently. Parent session used only for final reads.
- **Test:** Add test that runs 3 concurrent searches on same session — verify no `InvalidRequestError`.

### 1.2 SQL injection pattern in vendor_analytics

- **File:** `app/routers/vendor_analytics.py:216`
- **Problem:** `last_price_expr` and `last_qty_expr` interpolated into `sqltext(f"...")`. Currently safe (server-generated strings) but fragile.
- **Fix:** Two separate complete parameterized queries (one per dialect), no f-string interpolation.
- **Test:** Existing tests should pass; add test with special characters in MPN filter.

### 1.3 OAuth state timing attack

- **File:** `app/routers/auth.py:78`
- **Problem:** `state != expected_state` uses Python `!=` — timing-vulnerable.
- **Fix:** `if not expected_state or not hmac.compare_digest(state, expected_state):`
- **Test:** Existing auth callback tests cover this path.

### 1.4 Webhook client_state timing attack

- **File:** `app/services/webhook_service.py:287`
- **Problem:** `sub.client_state != client_state` — same timing vulnerability.
- **Fix:** `hmac.compare_digest(sub.client_state, client_state or "")`
- **Test:** Existing webhook validation tests.

### 1.5 Missing admin auth on tagging_admin (18 endpoints)

- **File:** `app/routers/tagging_admin.py`
- **Problem:** All endpoints use `Depends(require_user)` — any authenticated user can trigger expensive batch operations (Nexar validation, AI tagging, data purges).
- **Fix:** Change all to `Depends(require_admin)`.
- **Test:** Add test that non-admin user gets 403 on each endpoint.

### 1.6 Missing admin auth on ICS admin (5 endpoints)

- **File:** `app/routers/ics_admin.py`
- **Problem:** Same as 1.5 — `require_user` instead of `require_admin`.
- **Fix:** Change all to `Depends(require_admin)`.
- **Test:** Add test for 403 on non-admin.

### 1.7 Dead status machine — transitions unvalidated

- **File:** `app/services/status_machine.py` (defined but never imported/called)
- **Problem:** All offer/quote/requisition status transitions are direct assignments with no validation. Terminal states (SOLD, WON) can be bypassed. Any status can be set to any other.
- **Fix:** Wire `validate_transition()` into the HTMX and API routers that change offer, quote, and requisition status. Raise HTTPException on invalid transitions.
- **Test:** Add tests for invalid transitions (e.g., rejected→sold should fail).

### 1.8 StrategicVendor.status query on nonexistent column

- **File:** `app/routers/htmx_views.py:3251`
- **Problem:** Filters on `StrategicVendor.status == "active"` but model has no `status` column. Always returns empty results.
- **Fix:** Change to `StrategicVendor.released_at.is_(None)` (active = not released).
- **Test:** Add test creating a StrategicVendor and verifying it appears in the list.

### 1.9 Credential decrypt failure invisible in prod

- **File:** `app/services/credential_service.py:91`
- **Problem:** Decryption failure logged at DEBUG — never appears in production logs (LOG_LEVEL=INFO). System silently falls back to possibly stale env vars.
- **Fix:** Change to `logger.error(...)`.
- **Test:** Existing tests; verify log output in test.

### 1.10 Missing rollback in material enrichment job

- **File:** `app/jobs/tagging_jobs.py:305-307`
- **Problem:** Exception caught, logged, but no `db.rollback()` and no `raise`. Partial writes persist, connection returns dirty to pool.
- **Fix:** Add `db.rollback()` and `raise`.
- **Test:** Existing job tests.

---

## Phase 2: HIGH — Error Handling & Observability

**Estimated scope:** ~30 files, ~150 lines changed. Mostly log level upgrades and missing rollbacks.

### 2.1 Silent exception swallowing (3 locations)

- `app/routers/htmx_views.py:2918-2919` — Redis cache `except Exception: pass`
- `app/connectors/email_mining.py:299-300, 329-330` — date parse `except Exception: pass`
- **Fix:** Add `logger.warning(...)` with context.

### 2.2 Health monitor silently returns None

- `app/services/health_monitor.py:89-92`
- **Fix:** Add `logger.error("Failed to create connector for source '%s'", ...)`.

### 2.3 ~25 real errors logged at DEBUG (invisible in prod)

Key locations:
- `app/routers/crm/offers.py:379,389,422,439,492,505` — offer side-effects
- `app/routers/requisitions/requirements.py:221-226,449-465,427-428,640-641` — search/tag failures
- `app/services/enrichment.py:112-114,474-476,557-558` — connector failures
- `app/services/buyplan_workflow.py:453` — task auto-gen
- **Fix:** Upgrade all to `logger.warning` or `logger.error` as appropriate.

### 2.4 Webhook returns 200 on failure

- `app/routers/v13_features/activity.py:69-71`
- **Fix:** `raise HTTPException(500, "Processing failed")` in except block so Graph retries.

### 2.5 ~10 jobs swallow exceptions without re-raising

- `app/jobs/maintenance_jobs.py:57,77-80,100-103,205-206`
- `app/jobs/inventory_jobs.py:60-63,97-98`
- `app/jobs/offers_jobs.py:93-95`
- `app/jobs/task_jobs.py:93-95`
- **Fix:** Add `raise` after `db.rollback()` in each.

### 2.6 Five functions missing @_traced_job

- `app/jobs/knowledge_jobs.py:45,162,167,172`
- `app/jobs/tagging_jobs.py:288`
- **Fix:** Add `@_traced_job` decorator.

### 2.7 Add elapsed time to _traced_job

- `app/scheduler.py:18-34`
- **Fix:** `start = time.monotonic()`, log `f"Job finished in {elapsed:.1f}s"`.

### 2.8 Azure token error response logged

- `app/routers/auth.py:98`
- **Fix:** `logger.error(f"Azure token exchange returned {resp.status_code}")` — drop `resp.text`.

### 2.9 Global error response format handler

- `app/main.py`
- **Fix:** Add `@app.exception_handler(HTTPException)` that returns `JSONResponse({"error": exc.detail, "status_code": exc.status_code, "request_id": ...})`. This fixes the `{"detail":...}` vs `{"error":...}` inconsistency across all ~200 endpoints in one shot.

---

## Phase 3: HIGH — Database & Performance

**Estimated scope:** 2-3 Alembic migrations, ~20 model files, ~10 router/service files.

### 3.1 Missing FK indexes (29 columns)

Single Alembic migration adding indexes to all FK columns identified in the DB audit. Key tables: offers (5), sourcing (5), buy_plan (5), intelligence (4), prospect_account (4), enrichment (3), trouble_ticket (2), nc_search_log (1).

### 3.2 Missing ondelete (44 columns)

Single Alembic migration. Most audit-trail FKs get `SET NULL`. `ProactiveMatch.customer_site_id` gets `CASCADE`. `GraphSubscription.user_id` gets `CASCADE`.

### 3.3 Float→Numeric for price columns (5 columns)

Alembic migration: `Sighting.unit_price`, `MaterialVendorHistory.last_price`, `ProactiveMatch.customer_last_price`, `ProactiveMatch.our_cost`, `ProactiveMatch.margin_pct` → `Numeric(12,4)` / `Numeric(5,2)`.

### 3.4 Fix N+1 queries (4 locations)

- `htmx_views.py:1089` — sighting count via lazy load → `selectinload` or subquery count
- `sightings.py:231-238` — vendor phone loop → batch `VendorCard.in_()`
- `rfq.py:818-823` — contact fetch loop → single `.in_()` query
- `crm/buy_plans.py:342,374` — offer lazy load → `joinedload(BuyPlan.lines).joinedload(BuyPlanLine.offer)`

### 3.5 Remove global ORM event listener

- `app/database.py:65-73` — `_loaded_as_persistent` iterates all columns of every loaded row.
- **Fix:** Apply `UTCDateTime` type decorator to DateTime columns in models, remove the listener.

### 3.6 DB connection held during API calls

- `app/search_service.py:177-199`
- **Fix:** Release session before `asyncio.gather` of API connectors. Re-acquire for DB writes after.

### 3.7 Startup backfills: row-by-row → batch

- `app/startup.py:372-506` — Three backfill loops update rows one at a time.
- **Fix:** Batch UPDATE using `UPDATE ... FROM (VALUES ...)` or chunked `.in_()`.

### 3.8 Deprecated backref= (4 locations)

- `app/models/strategic.py:44-45`, `quotes.py:91`, `vendor_sighting_summary.py:49`
- **Fix:** Convert to explicit `back_populates` on both sides.

### 3.9 Deprecated db.query(Model).get(id) (6 locations)

- `app/routers/htmx_views.py:9275,9300,9369,9406,9434`
- `app/services/vendor_email_lookup.py:367`
- **Fix:** Replace with `db.get(Model, id)`.

### 3.10 Nullable columns that should be NOT NULL (14 columns)

Alembic migration: `Company.site_count`, `Company.open_req_count`, `User.role`, `User.is_active`, `Requisition.status`, `Requirement.sourcing_status`, `Offer.status`, and count/status columns with defaults.

---

## Phase 4: MEDIUM — Code Quality & Hardening

### 4.1 Centralize AI model names

7 service files hardcode `"claude-haiku-4-5-20251001"`. Route all through `claude_client.py` using `model_tier="fast"` / `"smart"`.

### 4.2 Remaining raw dict bodies (3 endpoints)

Create Pydantic schemas for: `prospect_suggested.py:272,359`, `knowledge.py:166`.

### 4.3 Request body size limits

Add middleware capping JSON body at 1MB. Alternatively, convert remaining `request.json()` calls to Pydantic models.

### 4.4 Cap bulk operation list sizes

`htmx_views.py:9549,9585` — validate `len(requirement_ids) <= 500`.

### 4.5 Rate limiting on email-sending endpoints

Priority: `rfq.py` follow-up/compose, `proactive.py` send, `vendors_crud.py` mutations.

### 4.6 Fix CSRF for multipart forms

Remove exemptions for `import-save` and `quick-create`. Fix CSRF token propagation for `multipart/form-data`.

### 4.7 File upload validation

`requisitions/attachments.py`, `crm/offers.py` — validate extension allowlist + content type.

### 4.8 Requisition import file size check

`htmx_views.py:488` — add `MAX_UPLOAD_BYTES` check before `file.read()`.

### 4.9 Cache invalidation gaps

Add `invalidate_prefix()` for: vendor edits (`htmx_views.py:3568`), company creation via lookup, vendor CSV import.

### 4.10 Missing StrEnum definitions

Create `ProspectAccountStatus`, `TaskStatus`, `PendingBatchStatus`, `DiscoveryBatchStatus` in `constants.py`.

---

## Phase 5: MEDIUM — Architecture & Simplification

### 5.1 Complete htmx_views.py split

Split 9,698-line file into ~10 domain modules under `app/routers/htmx/`. The directory already has `_shared.py`, `_constants.py`, `page.py`. Delete duplicated helpers from `htmx_views.py` and import from `_shared.py`.

### 5.2 Deduplicate ics_worker/nc_worker

Parameterize `human_behavior.py` (identical), `monitoring.py`, `ai_gate.py`, `config.py`, `scheduler.py` in `search_worker_base/`. ~500 lines eliminated.

### 5.3 Extract @with_db_session decorator

Replace 148 try/except/rollback/close repetitions (especially in `app/jobs/`) with a context manager or decorator.

### 5.4 Move business logic from HTMX routes to services

Priority: `save_parsed_offers` (duplicates `ai_offer_service.py`), `customer_quick_create`, `requisition_create`, `proactive_batch_dismiss`, `dashboard_partial`.

### 5.5 Fix layering violations

- `services/health_monitor.py` imports from `routers/sources.py` → extract to `connector_factory.py`
- `services/quote_builder_service.py` imports from `routers/crm/_helpers.py` → move to service

### 5.6 Delete dead code

- `app/routers/htmx/page.py` — duplicate, never imported in `main.py`
- Commented job functions in `core_jobs.py`
- `app/jobs/part_discovery_jobs.py` — never registered
- `app/config.py: use_htmx`, `mvp_mode` — never read

### 5.7 Extend @cached_endpoint for HTMX responses

Currently silently does nothing on `TemplateResponse` / `HTMLResponse`. Extend to handle `Response` objects or add HTMX-aware caching layer.

### 5.8 Rename confusing modules

`app/enrichment_service.py` (company enrichment) vs `app/services/enrichment.py` (material enrichment) — rename to `company_enrichment_service.py`.

---

## Phase 6: LOW — Polish

### 6.1 OpenAPI tags on 12 untagged routers
### 6.2 Status code 201 on 12+ creation endpoints
### 6.3 Pagination on 20+ unpaginated list endpoints
### 6.4 Accessibility: ARIA attributes, contrast fixes, focus indicators
### 6.5 CSP: nonce-based instead of unsafe-inline
### 6.6 Frontend: SSE event listener leak cleanup
### 6.7 Docker: Redis auth, pinned base images
### 6.8 Pin 8 production dependencies to exact versions
### 6.9 Unify secret_key sentinel values
### 6.10 Test coverage: 0% services, htmx_views error paths, pagination edge cases

---

## Implementation Strategy

- **Phases 1-2** are pure fixes — no architecture changes, no feature work. Safe to ship incrementally with one commit per finding or per logical group.
- **Phase 3** requires Alembic migrations — batch the 3 migrations into one sprint. Test downgrade path.
- **Phase 4** is hardening — can be interleaved with feature work.
- **Phase 5** is structural — the htmx_views split (5.1) is the highest-leverage single change and should be its own focused effort.
- **Phase 6** is polish — do opportunistically alongside other work.

## Success Criteria

- All 8,485+ tests pass after every phase
- Zero CRITICAL findings remaining after Phase 1
- Coverage ≥ 85% after Phase 6 (currently 81%)
- No `except Exception: pass` anywhere in codebase
- No `backref=` or `db.query(Model).get()` remaining
- All status transitions validated via status machine
- All AI calls routed through `claude_client.py`
