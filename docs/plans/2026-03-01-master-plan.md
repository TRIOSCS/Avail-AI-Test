# Master Plan: Test Fixes + Coverage + API Stability Monitoring

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 3 failing tests, close every coverage gap from 95% to 100%, then implement the full 12-task API Stability Monitoring system — all with zero coverage regression.

**Architecture:** Three phases run sequentially. Phase 1 (green suite) fixes broken tests so we start from a clean baseline. Phase 2 (100% coverage) closes every gap file-by-file with targeted tests. Phase 3 (API health monitoring) implements the full feature from the existing plan at `docs/plans/2026-03-01-api-stability-monitoring-plan.md`, with tests written alongside each task.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, APScheduler, Alembic, vanilla JS, httpx, pytest, pytest-cov

**Test commands:**
- Run all: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
- Coverage: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q`
- Single file: `TESTING=1 PYTHONPATH=/root/availai pytest tests/<file> -v`

**Current baseline:** 6675 passed, 3 failed, 2 skipped — 95% coverage (1487 statements uncovered across ~45 files)

---

## Phase 1: Green Suite (fix 3 failing tests)

**Commit after this phase. Estimated: 1 task.**

### Task 1.1: Fix 3 ICS Search Engine Tests

**Files:**
- Modify: `tests/test_ics_worker_full.py` (TestSearchEngine class, lines ~314-399)
- Reference: `app/services/ics_worker/search_engine.py` (the refactored source)

**Context:** Commit `9f6f8d6` refactored `search_engine.py` to a resilient multi-selector pattern but the 3 tests weren't updated. The new code calls `page.evaluate()` 3+ times (diagnostic dict, html, count) but tests only mock 2 calls (html, count). The new code also calls `locator.is_visible()` and `page.screenshot()` which aren't mocked.

**Root cause:** `page.evaluate()` first call now returns a diagnostic dict (`{buttons, forms, url, title}`), but tests supply an HTML string — code does `page_info.get("url")` on a string → AttributeError.

**Step 1:** Read `app/services/ics_worker/search_engine.py` fully to map all mock call points in the `search_part()` method — every `page.evaluate()`, `page.locator()`, `locator.is_visible()`, `locator.wait_for()`, `page.screenshot()`, and button click calls.

**Step 2:** Rewrite all 3 test methods to match the new call sequence:
- `page.evaluate` side_effects: `[{diagnostic_dict}, html_string, result_count]`
- Mock `locator.is_visible()` as AsyncMock returning True
- Mock `page.screenshot()` as AsyncMock
- For `test_search_part_fallback_field`: mock first locator `is_visible()` → False, second → True
- For `test_search_part_selector_timeout`: mock `locator.wait_for()` to raise TimeoutError

**Step 3:** Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ics_worker_full.py::TestSearchEngine -v`
Expected: All 3 PASS

**Step 4:** Run full suite to confirm no regressions:
`TESTING=1 PYTHONPATH=/root/availai pytest tests/ --tb=short -q`
Expected: 6678 passed, 0 failed

**Step 5:** Commit:
```bash
git add tests/test_ics_worker_full.py
git commit -m "fix: update ICS search engine tests for resilient selector refactor"
```

---

## Phase 2: 100% Coverage

**Commit after each task grouping. Organized largest-gap-first within logical clusters.**

### Coverage Gap Inventory (1487 uncovered statements)

**Tier A — Scraper workers (heavy, Playwright-dependent):** ~450 lines
| File | Stmts | Miss | Cov | Missing |
|------|-------|------|-----|---------|
| ics_worker/worker.py | 180 | 111 | 38% | 25-26, 121-318, 322 |
| ics_worker/search_engine.py | 75 | 48 | 36% | 54-55, 85-214 |
| ics_worker/session_manager.py | 97 | 45 | 54% | 47-64, 78-88, 109-172 |
| ics_worker/ai_gate.py | 87 | 33 | 62% | 156, 159-206 |
| nc_worker/session_manager.py | 133 | 62 | 53% | 52, 64-66, 70-91, 128-129, 161-208, 222-223, 228-239 |
| nc_worker/search_engine.py | 66 | 29 | 56% | 52-53, 80-82, 91-138, 153 |
| nc_worker/result_parser.py | 142 | 48 | 66% | 78, 90-92, 118-197 |
| nc_worker/worker.py | 186 | 14 | 92% | 26-27, 60-61, 139-156, 253, 295-296, 316, 325 |

**Tier B — Services (business logic):** ~300 lines
| File | Stmts | Miss | Cov | Missing |
|------|-------|------|-----|---------|
| customer_enrichment_service.py | 248 | 62 | 75% | 110, 125-138, 203-204, 220-221, 232, 234-236, 241-272, 280-281, 292-312, 357-360, 383-385, 420, 439 |
| response_analytics.py | 141 | 25 | 82% | 83, 121, 182-188, 228-254, 298, 333-335, 340-342 |
| proactive_matching.py | 156 | 27 | 83% | 43, 45, 56, 70, 72, 100, 114, 162, 174, 187, 200, 216, 245, 322, 328-332, 337-340, 364, 366, 376, 378 |
| unified_score_service.py | 186 | 22 | 88% | 93, 148, 242-267, 314-320, 426, 445-446 |
| startup.py | 235 | 26 | 89% | 57-89, 542 |
| prospect_discovery_explorium.py | 141 | 15 | 89% | 78, 133, 185-188, 212, 216, 246, 248, 250, 252, 255-258, 320 |
| multiplier_score_service.py | 210 | 15 | 93% | 73, 369-370, 374-379, 418, 481, 516, 536-537, 600 |
| prospect_scoring.py | 248 | 16 | 94% | 121-122, 128-129, 132-133, 192, 246-247, 249-250, 266, 360, 390, 482-483 |
| prospect_scheduler.py | 213 | 15 | 93% | 35, 191-194, 313-317, 403-407 |
| prospect_contacts.py | 224 | 16 | 93% | 308, 315, 318-325, 333, 381, 427, 517-519 |
| contact_quality.py | 99 | 13 | 87% | 63, 68-69, 73, 75, 143, 171, 176-181 |
| email_intelligence_service.py | 110 | 10 | 91% | 84-85, 101-108, 220, 250-253 |
| integrity_service.py | 124 | 10 | 92% | 162-164, 186-188, 210-212, 338 |
| material_enrichment_service.py | 74 | 8 | 89% | 131-133, 137-141 |
| customer_enrichment_batch.py | 76 | 8 | 89% | 62-63, 71-74, 126, 132 |
| prospect_discovery_email.py | 85 | 7 | 92% | 68-72, 172-173 |
| buyplan_v3_notifications.py | 167 | 2 | 99% | 66-67 |
| deep_enrichment_service.py | 480 | 2 | 99% | 537-538 |
| mailbox_intelligence.py | 40 | 2 | 95% | 95-96 |
| requisition_state.py | 20 | 2 | 90% | 60-61 |
| ownership_service.py | 282 | 2 | 99% | 583, 601 |
| proactive_service.py | 263 | 4 | 98% | 105, 212, 233, 393 |
| calendar_intelligence.py | 61 | 4 | 93% | 90, 115-117 |
| company_merge_service.py | 66 | 4 | 94% | 125-126, 136-137 |
| prospect_discovery_apollo.py | 116 | 1 | 99% | 35 |
| nc_worker/circuit_breaker.py | 59 | 4 | 93% | 44-45, 47-48 |
| nc_worker/sighting_writer.py | 40 | 2 | 95% | 73, 86 |

**Tier C — Routers:** ~300 lines
| File | Stmts | Miss | Cov | Missing |
|------|-------|------|-----|---------|
| vendors.py | 867 | 74 | 91% | 92-115, 127-130, 132-145, 331-367, 444, 1786-1813, 1818, 1820, 1825 |
| buy_plans.py | 501 | 64 | 87% | 176-203, 213-248, 271, 344, 658, 972, 978, 990-1003 |
| dashboard.py | 440 | 32 | 93% | 218, 254, 303-310, 402-403, 405-413, 808-809, 887-888, 890-891, 1031-1036, 1063-1064, 1275, 1343 |
| enrichment.py | 406 | 36 | 91% | 678, 721-762, 820-822, 877, 881 |
| buy_plans_v3.py | 213 | 27 | 87% | 244, 253, 285, 307, 366, 368, 374, 419, 428-429, 457, 464-465, 492-493, 499, 521, 577-580, 586-587, 608-609, 630, 634 |
| companies.py | 193 | 23 | 88% | 361-369, 371, 438-445, 485-494 |
| ics_admin.py | 46 | 27 | 41% | 30, 41-48, 74-79, 89-96, 105-126 |
| prospect_suggested.py | 173 | 21 | 88% | 76-77, 85, 210, 287-308, 334-335 |
| requisitions.py | 687 | 19 | 97% | 896-899, 1182, 1201-1224, 1241, 1372-1375, 1540, 1568 |
| proactive.py | 132 | 17 | 87% | 108-144, 180-182 |
| admin.py | 463 | 17 | 96% | 323-325, 336-350, 456, 472 |
| offers.py | 329 | 13 | 96% | 173, 192-217, 399-400, 785, 796-797 |
| quotes.py | 263 | 8 | 97% | 189-193, 567, 583-584 |
| performance.py | 142 | 4 | 97% | 265-268 |
| v13_features.py | 343 | 4 | 99% | 864, 866, 922, 924 |

**Tier D — Infrastructure & utils:** ~120 lines
| File | Stmts | Miss | Cov | Missing |
|------|-------|------|-----|---------|
| scheduler.py | 1086 | 102 | 91% | 604, 924-939, 945-960, 1021-1022, 1043-1045, 1057, 1069-1074, 1574-1575, 1587-1588, 1599-1601, 1638-1641, 1662-1664, 1804-1825, 1838-1860, 1913-1929, 1935-1951, 1966-1968 |
| main.py | 275 | 3 | 99% | 320-323 |
| auth.py | 100 | 2 | 98% | 157-158 |
| llm_router.py | 53 | 2 | 96% | 174-175 |
| lusha_client.py | 116 | 4 | 97% | 204, 263-265 |
| email_mining.py | 372 | 4 | 99% | 343-344, 362-363 |
| tme.py | 76 | 3 | 96% | 77-79 |
| sources.py | 289 | 1 | 99% | 333 |
| enrichment.py (router) | 154 | 2 | 99% | 66-67 |
| sites.py | 138 | 1 | 99% | 45 |

**Tier E — Worker __main__ files (excluded):** 5 lines total
| File | Stmts | Miss | Note |
|------|-------|------|------|
| ics_worker/__main__.py | 3 | 3 | Entry point, add pragma: no cover |
| nc_worker/__main__.py | 2 | 2 | Entry point, add pragma: no cover |
| ics_worker/monitoring.py | 48 | 2 | Import-time sentry init |
| nc_worker/monitoring.py | 48 | 2 | Import-time sentry init |
| ics_worker/scheduler.py | 44 | 7 | APScheduler init |
| nc_worker/scheduler.py | 44 | 4 | APScheduler init |
| ics_worker/queue_manager.py | 77 | 1 | Single line |

---

### Task 2.1: Exclude Untestable Entry Points + Mark Pragma Lines

**Files:**
- Modify: `app/services/ics_worker/__main__.py`
- Modify: `app/services/nc_worker/__main__.py`
- Modify: `app/services/ics_worker/monitoring.py` (lines 18-19)
- Modify: `app/services/nc_worker/monitoring.py` (lines 18-19)

**Step 1:** Read each file. For `__main__.py` files (which are just `if __name__ == "__main__": asyncio.run()`) and Sentry SDK init lines in monitoring.py, add `# pragma: no cover` to lines that are genuinely untestable (process entry points, Sentry SDK init that requires real DSN).

**Step 2:** Run coverage to confirm those lines are excluded.

**Step 3:** Commit:
```bash
git commit -m "chore: pragma no-cover on process entry points and sentry init"
```

---

### Task 2.2: ICS Worker Coverage — search_engine, session_manager, worker, ai_gate

**Files:**
- Modify: `tests/test_ics_worker_full.py`
- Reference: `app/services/ics_worker/search_engine.py` (36% → 100%)
- Reference: `app/services/ics_worker/session_manager.py` (54% → 100%)
- Reference: `app/services/ics_worker/worker.py` (38% → 100%)
- Reference: `app/services/ics_worker/ai_gate.py` (62% → 100%)

**Step 1:** Read each source file and identify all uncovered code paths.

**Step 2:** Write tests for each uncovered path. All Playwright calls should be mocked with AsyncMock. Key patterns:
- `search_engine.py:85-214`: Full search flow — multi-selector loop, 3 button-click strategies, screenshot on diagnostics
- `session_manager.py:47-64,78-88,109-172`: Browser launch, page creation, context setup, cleanup
- `worker.py:121-318`: Main `run()` loop — queue pop, search, parse, write sightings, error handling
- `ai_gate.py:159-206`: ICS-specific AI gate methods

**Step 3:** Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_ics_worker_full.py -v --cov=app/services/ics_worker --cov-report=term-missing`
Expected: 100% on all 4 files

**Step 4:** Commit:
```bash
git commit -m "test: 100% coverage for ICS worker modules"
```

---

### Task 2.3: NC Worker Coverage — search_engine, session_manager, result_parser, worker, circuit_breaker, sighting_writer

**Files:**
- Modify: `tests/test_nc_worker_full.py`
- Reference: `app/services/nc_worker/search_engine.py` (56% → 100%)
- Reference: `app/services/nc_worker/session_manager.py` (53% → 100%)
- Reference: `app/services/nc_worker/result_parser.py` (66% → 100%)
- Reference: `app/services/nc_worker/worker.py` (92% → 100%)
- Reference: `app/services/nc_worker/circuit_breaker.py` (93% → 100%)
- Reference: `app/services/nc_worker/sighting_writer.py` (95% → 100%)

**Step 1:** Read each source file, map uncovered paths.

**Step 2:** Write tests with mocked Playwright. Similar patterns to ICS worker.

**Step 3:** Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_nc_worker_full.py -v --cov=app/services/nc_worker --cov-report=term-missing`
Expected: 100% on all files

**Step 4:** Commit:
```bash
git commit -m "test: 100% coverage for NC worker modules"
```

---

### Task 2.4: Worker Scheduler + Queue Manager Coverage

**Files:**
- Modify or create tests for:
  - `app/services/ics_worker/scheduler.py` (84% → 100%, missing: 20-21, 42, 48, 53-56)
  - `app/services/nc_worker/scheduler.py` (91% → 100%, missing: 20-21, 42, 51)
  - `app/services/ics_worker/queue_manager.py` (99% → 100%, missing: 42)
  - `app/services/ics_worker/result_parser.py` (96% → 100%, missing: 161-163)

**Step 1:** Read each file, identify missing lines (APScheduler setup, edge cases).

**Step 2:** Add targeted tests.

**Step 3:** Commit:
```bash
git commit -m "test: 100% coverage for worker schedulers and queue managers"
```

---

### Task 2.5: Service Coverage — customer_enrichment, response_analytics, proactive_matching

**Files:**
- Modify/create tests for the 3 largest service gaps:
  - `customer_enrichment_service.py` (75% → 100%, 62 lines)
  - `response_analytics.py` (82% → 100%, 25 lines)
  - `proactive_matching.py` (83% → 100%, 27 lines)

**Step 1:** Read each source file. Identify uncovered branches — likely error paths, empty-result guards, optional feature paths.

**Step 2:** Write tests targeting each uncovered path. Mock external calls (Claude, Graph API, DB queries).

**Step 3:** Run coverage for just these modules and verify 100%.

**Step 4:** Commit:
```bash
git commit -m "test: 100% coverage for enrichment, analytics, and matching services"
```

---

### Task 2.6: Service Coverage — unified_score, startup, multiplier_score, prospect modules

**Files:**
- `unified_score_service.py` (88% → 100%, 22 lines)
- `startup.py` (89% → 100%, 26 lines — lines 57-89 are PG-specific startup, line 542)
- `multiplier_score_service.py` (93% → 100%, 15 lines)
- `prospect_discovery_explorium.py` (89% → 100%, 15 lines)
- `prospect_scoring.py` (94% → 100%, 16 lines)
- `prospect_scheduler.py` (93% → 100%, 15 lines)
- `prospect_contacts.py` (93% → 100%, 16 lines)

**Step 1:** Read each file, map uncovered paths.

**Step 2:** Write targeted tests. For `startup.py` lines 57-89 (PG-only FTS/trigger creation), mock the DB connection.

**Step 3:** Verify 100% on each, commit:
```bash
git commit -m "test: 100% coverage for scoring, startup, and prospect services"
```

---

### Task 2.7: Service Coverage — Remaining Small Gaps

**Files (all 1-10 lines uncovered):**
- `contact_quality.py` (87% → 100%, 13 lines)
- `email_intelligence_service.py` (91% → 100%, 10 lines)
- `integrity_service.py` (92% → 100%, 10 lines)
- `material_enrichment_service.py` (89% → 100%, 8 lines)
- `customer_enrichment_batch.py` (89% → 100%, 8 lines)
- `prospect_discovery_email.py` (92% → 100%, 7 lines)
- `calendar_intelligence.py` (93% → 100%, 4 lines)
- `company_merge_service.py` (94% → 100%, 4 lines)
- `proactive_service.py` (98% → 100%, 4 lines)
- `buyplan_v3_notifications.py` (99% → 100%, 2 lines — quote site_name without company)
- `deep_enrichment_service.py` (99% → 100%, 2 lines)
- `mailbox_intelligence.py` (95% → 100%, 2 lines)
- `requisition_state.py` (90% → 100%, 2 lines)
- `ownership_service.py` (99% → 100%, 2 lines)
- `prospect_discovery_apollo.py` (99% → 100%, 1 line)

**Step 1:** Read each file's missing lines, write one targeted test per gap.

**Step 2:** Run coverage, verify all at 100%, commit:
```bash
git commit -m "test: 100% coverage for remaining service modules"
```

---

### Task 2.8: Router Coverage — vendors, buy_plans, dashboard, enrichment

**Files (largest router gaps):**
- `vendors.py` (91% → 100%, 74 lines)
- `buy_plans.py` (87% → 100%, 64 lines)
- `dashboard.py` (93% → 100%, 32 lines)
- `enrichment.py` (91% → 100%, 36 lines)

**Step 1:** Read each router, map uncovered endpoints/branches. Likely: error paths, edge cases in CRUD, auth edge cases.

**Step 2:** Write endpoint tests using FastAPI TestClient. Mock DB/services as needed.

**Step 3:** Commit:
```bash
git commit -m "test: 100% coverage for vendors, buy_plans, dashboard, enrichment routers"
```

---

### Task 2.9: Router Coverage — buy_plans_v3, companies, ics_admin, prospect_suggested, proactive

**Files:**
- `buy_plans_v3.py` (87% → 100%, 27 lines)
- `companies.py` (88% → 100%, 23 lines)
- `ics_admin.py` (41% → 100%, 27 lines)
- `prospect_suggested.py` (88% → 100%, 21 lines)
- `proactive.py` (87% → 100%, 17 lines)

**Step 1:** Read each router, identify uncovered endpoints.

**Step 2:** Write tests. `ics_admin.py` is the biggest gap (41%) — likely has no test file at all, create one.

**Step 3:** Commit:
```bash
git commit -m "test: 100% coverage for v3, companies, ics_admin, prospect, proactive routers"
```

---

### Task 2.10: Router Coverage — Remaining Small Gaps

**Files:**
- `requisitions.py` (97% → 100%, 19 lines)
- `admin.py` (96% → 100%, 17 lines)
- `offers.py` (96% → 100%, 13 lines)
- `quotes.py` (97% → 100%, 8 lines)
- `performance.py` (97% → 100%, 4 lines)
- `v13_features.py` (99% → 100%, 4 lines)
- `auth.py` (98% → 100%, 2 lines)
- `crm/enrichment.py` (99% → 100%, 2 lines)
- `crm/sites.py` (99% → 100%, 1 line)

**Step 1:** Add targeted tests for each file's missing lines.

**Step 2:** Commit:
```bash
git commit -m "test: 100% coverage for remaining router modules"
```

---

### Task 2.11: Infrastructure Coverage — scheduler, connectors, utils

**Files:**
- `scheduler.py` (91% → 100%, 102 lines — the biggest single file gap)
- `connectors/email_mining.py` (99% → 100%, 4 lines)
- `connectors/lusha_client.py` (97% → 100%, 4 lines)
- `connectors/tme.py` (96% → 100%, 3 lines)
- `connectors/sources.py` (99% → 100%, 1 line)
- `main.py` (99% → 100%, 3 lines)
- `llm_router.py` (96% → 100%, 2 lines)

**Step 1:** For `scheduler.py`, the 102 uncovered lines are mostly background job functions (prospect jobs, cleanup jobs). Read and write tests with mocked DB sessions and external calls.

**Step 2:** For connectors, add edge-case tests.

**Step 3:** Commit:
```bash
git commit -m "test: 100% coverage for scheduler, connectors, and utils"
```

---

### Task 2.12: Final Coverage Verification

**Step 1:** Run full suite with coverage:
```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=short -q
```
Expected: 0 failures, 100% coverage (or 99%+ with justified pragma exclusions)

**Step 2:** If any files still below 100%, add targeted tests.

**Step 3:** Commit:
```bash
git commit -m "test: achieve 100% test coverage across entire codebase"
```

---

## Phase 3: API Stability Monitoring (12 Tasks)

**Full implementation plan already exists at:** `docs/plans/2026-03-01-api-stability-monitoring-plan.md`

**Execute that plan's 12 tasks in order.** Summary:

| Task | Description | Key Files |
|------|-------------|-----------|
| 3.1 | Database migration 038 + models | `alembic/versions/038_*`, `app/models/config.py` |
| 3.2 | Health monitor service | `app/services/health_monitor.py` (new) |
| 3.3 | Fix status accuracy (remove credential-based status) | `app/routers/sources.py:370-376` |
| 3.4 | Scheduler integration (5 new jobs) | `app/scheduler.py` |
| 3.5 | System alerts endpoint | `app/routers/sources.py` (new endpoint) |
| 3.6 | API health dashboard endpoint | `app/routers/admin.py` (new endpoint) |
| 3.7 | Frontend warning banner | `app/templates/index.html`, `app/static/app.js` |
| 3.8 | Frontend dashboard tab | `index.html`, `app.js`, `crm.js` |
| 3.9 | Enhanced settings panel | `sources.py`, `crm.js` |
| 3.10 | Seed known quotas | `app/startup.py` |
| 3.11 | Test suite for all new code | `tests/test_api_health.py` (new) |
| 3.12 | Deploy + verify | Docker rebuild, log check |

**Note:** Uncommitted partial work already exists for Task 3.1 (migration file + model changes). Start by reviewing and completing that.

**Critical rule:** Every task must maintain 100% coverage. Write tests alongside implementation, not after.

**Commit after each task.**

---

## Execution Summary

| Phase | Tasks | Est. Commits | Key Metric |
|-------|-------|-------------|------------|
| 1. Green Suite | 1 | 1 | 0 failures |
| 2. 100% Coverage | 12 | ~10 | 100% coverage |
| 3. API Monitoring | 12 | ~12 | Feature complete + 100% cov |
| **Total** | **25** | **~23** | **All green, 100% cov, feature shipped** |
