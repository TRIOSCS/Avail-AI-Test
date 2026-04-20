# CHANGELOG

All notable changes to the project are logged here.

## 2026-04-17 — Search “Details” did not open lead drawer

### Bug Fixes
- **`form.html` / lead drawer:** “Details →” loaded `lead_detail` into `#lead-drawer-content` via HTMX but the drawer never opened (`data-open` was not reactive to external updates). Replaced with Alpine `drawerOpen`, `@htmx:after-swap` on the content container to open after load, and aligned backdrop / close / Escape with the same flag.
- **`htmx_app.js`:** Scoped `document.body.style.overflow` reset in `htmx:afterSwap` to `#main-content` swaps only (was clearing scroll lock on every swap). Call `Alpine.initTree` after swaps into `#lead-drawer-content` so `@click` on loaded lead detail works.

---

## 2026-04-17 — Search page SSE showed raw JSON instead of vendor cards

### Bug Fixes
- **`stream_search_mpn`:** HTMX `sse-swap="results"` appends the SSE **message body as HTML**. Events were publishing JSON (`{"cards": [...]}`), so users saw raw JSON. Results and card-update events now publish rendered `vendor_card.html` fragments (with `hx-swap-oob` on updates).

### Tests
- **`test_search_streaming.test_stream_search_publishes_events`:** Asserts `results` payloads contain `vendor-card` markup and not the legacy `"cards"` JSON wrapper.

---

## 2026-04-17 — HTMX lazy-load swap target (insights & admin health)

### Bug Fixes
- **AI insights lazy-load:** Added explicit `hx-target="this"` on requisition, vendor, and customer detail insight loaders so HTMX does not inherit `<main id="main-content" hx-target="this">` and swap the insights fragment into the entire main column.
- **Dashboard pipeline insights & admin API health:** Same `hx-target="this"` fix for `hx-trigger="load"` partials inside `#main-content`.

### Tests
- **test_htmx_views:** `TestRequisitionDetail.test_detail`, `TestVendorDetail.test_detail`, `TestCustomerDetail.test_detail`, and `TestInsights.test_dashboard_partial_pipeline_loader_targets_self` assert lazy-load `hx-get` blocks include `hx-target="this"`.
- **test_sprint9_10_materials_admin.TestAdminDataOps.test_data_ops_renders:** Asserts API health lazy-load includes `hx-target="this"`.

---

## 2026-04-17 — Materials manufacturer filter blank labels

### Bug Fixes
- **`manufacturers.html`:** Manufacturer rows showed checkboxes and counts but no names because `x-data="{ name: {{ mfr.name | tojson }} }"` placed JSON double quotes inside a double-quoted HTML attribute, which truncated the attribute and broke Alpine. Fixed with `x-data="{{ {'mfrLabel': mfr.name}|tojson|e }}"` and Alpine bindings renamed to `mfrLabel`.

### Tests
- **`test_faceted_routes.test_manufacturer_filter_partial_renders`:** Inserts a material card with manufacturer `MemCo` and asserts the partial HTML contains the name, `mfrLabel`, and does not regress to the broken `x-data="{name:` pattern.

---

## 2026-03-13 — Frontend XSS hardening (innerHTML sanitization)

### Security
- **showToast (app.js):** Message is now escaped with `esc()` before insertion into innerHTML so API or user-controlled toast text cannot inject HTML/script. Defense-in-depth in case callers pass unescaped data.
- **Connector test error (crm.js):** Source test result `data.error` is now escaped when rendered in the test result div so connector error messages cannot inject HTML.

---

## 2026-03-13 — Requisition status consistency & archive count fix

### Bug Fixes
- **Requisition archive count always zero:** `GET /api/requisitions/counts` was counting `status == "archive"` but the DB stores `"archived"`. Fixed to count `status.in_(["archived", "won", "lost", "closed"])` so the dashboard archive widget shows the correct total.
- **Data cleanup quarantine wrong status:** `data_cleanup_service.scan_junk_data()` set quarantined requisitions to `"archive"` instead of `"archived"`, leaving invalid state. Now sets `"archived"` and filter uses `!= "archived"`.
- **Status machine mismatch:** `status_machine.REQUISITION_TRANSITIONS` used `"archive"` as key and target; canonical value is `"archived"` (RequisitionStatus.archived). Updated all transitions to use `"archived"` for consistency with enums and routers.

### Tests
- **test_remediation_waves:** Assertion updated to expect `status == "archived"` after quarantine.
- **test_requisition_counts_archive_includes_archived:** New test ensures archive count includes archived/won/lost/closed requisitions.

---

## 2026-03-12 — PR review fixes (docs/plans/2026-03-08-pr-review-fixes.md)

### Applied
- **Task 1 (security):** `_resolve_user` in `teams_bot_service.py` — fixed column name `azure_ad_id` → `azure_id`; already returned `None` when no match (no fallback to arbitrary user). Added tests in `tests/test_teams_bot.py`.
- **Tasks 3 & 4 (data):** `replace_vendor` already atomic (savepoint); migration 066 already added partial unique index. Removed redundant `UniqueConstraint` from `app/models/strategic.py` so model matches DB.
- **Task 7:** Strategic router already used `_vendor_to_dict` with `_ensure_utc` and `JSONResponse` for errors.
- **Task 9:** Upgraded strategic vendor clock reset failure in `email_service.py` from `logger.debug` to `logger.warning`.
- **Task 10:** No unused `and_` in `strategic_vendor_service.py` (already clean).
- **Task 12:** Added screenshot size limit (2MB) to error report create schema and test `test_create_error_report_screenshot_too_large`.

### Skipped (modules removed)
- Tasks 2, 5, 6, 8, 11: `routers/teams_bot.py`, `services/notify_intelligence.py`, `routers/trouble_tickets.py`, and `jobs/selfheal_jobs.py` are stubbed as "REMOVED" — no code to change.

---

## 2026-03-12 — System optimization & tech debt cleanup

### Bug Fixes
- **Fixed quota_map key mismatch** in `main.py`: `hunter` → `hunter_enrichment`, `clearbit` → `clearbit_enrichment` — quotas were not being applied to these enrichment sources
- **Fixed sync blocking call** in `email_jobs.py`: `run_site_ownership_sweep` now runs in executor instead of blocking the event loop
- **Fixed deprecated `asyncio.get_event_loop()`** in `email_jobs.py` and `tagging_admin.py` (6 occurrences) — replaced with `asyncio.get_running_loop()`

### Code Cleanup
- **Extracted 350-line inline SOURCES list** from `main.py` into `app/data/api_sources.json` (already existed) — `main.py` reduced from ~1090 to ~684 lines
- **Consolidated router registration** in `main.py`: grouped all imports at top, organized `include_router` calls by core vs MVP-gated
- **Added logging to silent `except` blocks** in `teams_alert_service.py`, `teams.py`, `dashboard_briefing.py`, `vendor_email_lookup.py` — failures were being swallowed silently
- **Removed one-off data fix** from `startup.py` (req 23446 deadline correction) — should not run on every boot
- **Fixed duplicate section header** in `startup.py`
- **Fixed `_exec` docstring** in `startup.py`: said "DDL" but function executes data operations
- **Deleted dead `selfheal_jobs.py`** module (stub with only a "REMOVED" comment) and its stub test `test_retry_stuck_diagnosed.py`
- **Added auto-expiry** to Clear-Site-Data header: self-disables after 2026-03-17 instead of requiring manual removal

### Unchanged (pre-existing — flagged for future work)
- 336 pre-existing test failures (no regressions introduced)
- Bare `except` blocks in NC/ICS worker code (browser automation — intentional)
- Duplicate monitoring/scheduler code across `nc_worker`/`ics_worker` (candidates for consolidation into `search_worker_base`)
- `type: ignore` comments (5 instances, mostly in test schemas)
- Pre-existing F821 lint errors in `teams_alert_service.py` (undefined names from planned intelligence layer)

## 2026-03-12

- Prep for human testing: added STABLE.md (registry of critical files) and this CHANGELOG. No code behavior change.
- Added a master follow-up prompt for debt sweep, live verification, safe cleanup, and evidence-based optimization passes.
- Debt/cleanup follow-up: replaced dated Clear-Site-Data TODO with auto-expiry logic, optimized API source quota backfill (no extra quota queries), added regression tests, and fixed two high-risk lint defects (missing VendorCard import + duplicate reject handler name).
