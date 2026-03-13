# CHANGELOG

All notable changes to the project are logged here.

## 2026-03-13 — Test library refinement (HTMX/Alpine rebuild)

### Applied
- **pytest-timeout:** Added to requirements-dev.txt so pytest.ini `--timeout=30` is enforced (prevents hanging tests)
- **conftest:** Set `MVP_MODE=false` so enrichment, performance, teams, apollo routers are included in tests
- **conftest:** Made `event_loop` fixture autouse and set as default loop (fixes `run_until_complete` in sync tests)
- **conftest:** Added `run_async` fixture for running async coros from sync tests
- **ai router:** Fixed relative imports (`...` → `..`) in apply-freeform-rfq and save-freeform-offers (ImportError)
- **test_schemas_ai:** Removed obsolete IntakeDraftRequest/Response/RequirementItem tests (schemas removed in rebuild)
- **test_routers_ai:** Skipped intake-draft tests (endpoint removed; intake uses client-side parse)
- **test_shared_framework:** Updated intake placeholder and AI endpoint assertions for HTMX/Alpine changes
- **vitest.config.js:** Added explicit config for frontend tests (include, timeout, environment)
- **scripts/run_all_tests.sh:** New unified test runner (pytest + vitest + optional e2e)

### Result
- Backend: 8781 passed, 80 failed, 5 skipped (down from 392 failures)
- Failures remain in: rfq_frontend_validation, nc_worker, search_service, scheduler, workflow_state_clarity, etc.

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
