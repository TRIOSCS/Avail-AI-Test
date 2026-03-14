# AVAIL AI — Test Suite Analysis

**Date:** 2026-03-14
**Analyst:** Code Review Agent
**Scope:** Full `tests/` directory — infrastructure, coverage, quality, gaps, anti-patterns

---

## 1. Suite Overview

| Metric | Value |
|---|---|
| Total test files | **287** (285 in `tests/`, 2 in `tests/e2e/`) |
| Total test functions | **8,147** |
| Total test classes | **1,331** |
| Async tests (`@pytest.mark.asyncio`) | **962** (~12% of total) |
| Total lines of test code | **137,859** |
| Smallest test file | `test_routers_vendors.py` — 11 lines (deprecated stub) |
| Largest test file | `test_routers_crm.py` — 3,714 lines |
| conftest.py files | 2 (`tests/conftest.py`, `tests/e2e/conftest.py`) |
| Sub-directories | `tests/test_models/`, `tests/test_services/`, `tests/e2e/` |

---

## 2. Test Infrastructure (conftest.py)

### Main conftest (`tests/conftest.py` — 467 lines)

**Strengths:**
- **In-memory SQLite** with `StaticPool` — fast, no external DB dependency
- **Type patching** for PostgreSQL-specific types (`ARRAY → JSON`, `TSVECTOR → TEXT`, `JSONB → JSON`) — good pragmatic workaround
- **Auth overrides** — all four dependency levels (`require_user`, `require_buyer`, `require_sales`, `require_fresh_token`) are mocked out
- **Autouse `db_session` fixture** — every test gets a fresh session; rows are deleted in FK-safe order between tests (much faster than drop/create)
- **Autouse `_reset_ai_gate_state`** — clears NC/ICS worker classification cache between tests to prevent order-dependent failures
- **`TESTING=1` env var** set before any app imports — disables scheduler, real API calls
- **Rich factory fixtures** — `test_user`, `sales_user`, `admin_user`, `manager_user`, `trader_user`, `test_company`, `test_requisition`, `test_vendor_card`, `test_material_card`, `test_offer`, `test_quote`, `test_proactive_offer`, `test_vendor_contact`, `test_customer_site`, `test_activity`
- **`nest_asyncio.apply()`** — prevents cross-test event loop contamination
- **Event loop fixture** — creates a fresh loop per test

**Minor concerns:**
- Tables are created once at import time via `Base.metadata.create_all()`. This means if a model changes in a test, the schema won't reflect it. However, for unit tests this is acceptable.
- The `_PG_ONLY_TABLES` set only contains `buyer_profiles`. If more PG-only tables are added, this needs manual update.

### E2E conftest (`tests/e2e/conftest.py` — 140 lines)

- Targets Playwright browser tests against live Docker app
- Auto-detects Docker container IP or falls back to remote URL
- Forges Starlette session cookies to bypass Azure OAuth
- `session`-scoped fixtures for browser context and base URL — appropriate for E2E

---

## 3. Representative Test File Analysis

### 3.1 `test_connectors.py` (2,398 lines) — Connector Tests

**Quality:** HIGH

- Tests `CircuitBreaker` state machine (closed → open → half-open → reset) thoroughly
- Tests `BaseConnector` with custom subclasses (`GoodConnector`, `FlakyConnector`, `TimeoutConnector`)
- All HTTP calls are mocked via `MagicMock(spec=httpx.Response)` — no real API traffic
- Edge cases: retry on generic error, immediate fail on connect timeout, breaker caching
- Properly cleans up `_breakers` dict between tests to prevent state leakage
- Tests each vendor connector (Nexar, BrokerBin, DigiKey, Mouser, OEMSecrets, Sourcengine, Element14, eBay)

### 3.2 `test_services_sourcing_score.py` (1,178 lines) — Scoring Engine

**Quality:** EXCELLENT

- Tests pure mathematical functions (`_sigmoid`) with 10 distinct properties (midpoint, symmetry, monotonicity, steepness)
- Tests `score_requirement()` composite scoring with boundary values (all zeros, moderate, maximum)
- Tests color band classification (`_color`), signal breakdown (`_build_signals`), level classification
- DB-backed tests for `compute_requisition_scores()` using conftest fixtures
- Good use of `pytest.approx()` for floating-point comparisons
- Follows arrange-act-assert pattern consistently

### 3.3 `test_email_service.py` (2,752 lines) — Email/Graph API

**Quality:** HIGH

- 74 async tests covering send, poll, parse, classify, batch processing
- All Microsoft Graph API calls mocked via `AsyncMock` and `patch`
- Tests noise email detection with all domains and prefixes
- Tests response classification (OOO, quote, info request, decline, etc.)
- Tests contact status progression state machine
- Tests batch result processing lifecycle (pending → completed → timeout → error)

### 3.4 `test_search_service.py` (2,568 lines) — Search Orchestration

**Quality:** HIGH

- 27 async tests covering full search lifecycle
- All 8 connector classes patched simultaneously via helper function `_all_connector_patches()`
- Tests deduplication, vendor email propagation, material card upsert, history scoring
- Tests connector failure isolation (one connector fails, others still return)
- Factory helpers create DB fixtures inline rather than relying solely on conftest

### 3.5 `test_services_ownership.py` (711 lines) — Ownership Business Rules

**Quality:** HIGH

- Tests 30-day inactivity sweep, at-risk detection, auto-claim logic
- Uses inline factory helpers (`_make_company`, `_make_sales_user`) for test-specific data
- Tests role-based access control (buyer cannot claim accounts)
- Tests manager digest email generation with mocked Graph API
- Good class-based organization (`TestCheckAndClaimOpenAccount`, `TestGetAccountsAtRisk`, etc.)

### 3.6 `test_routers_rfq.py` (2,393 lines) — RFQ Router

**Quality:** HIGH

- Tests both pure logic (garbage vendor filtering, blacklist) and HTTP endpoints
- Uses `_client_as_user()` context manager for multi-user testing
- Tests follow-up detection, contact listing, response listing, RFQ preparation
- Tests vendor review submission and phone call logging
- Properly mocks `send_batch_rfq` and `poll_inbox` for async operations

### 3.7 `test_routers_crm.py` (3,714 lines) — CRM Router

**Quality:** HIGH (largest test file)

- Tests quote workflow (create, send, revise, reopen, won/lost)
- Tests company CRUD, duplicate detection, Acctivate sync endpoints
- Tests site management, contact management, offer management
- Uses `MagicMock` objects for quote serialization tests (fast, no DB needed)

### 3.8 `test_nc_worker_full.py` (3,591 lines) — NetComponents Worker

**Quality:** HIGH

- Covers 15 sub-modules of the nc_worker package
- Tests MPN normalization with 15+ suffix patterns (TR, CT, PBF, NOPB, etc.)
- Tests HTML result parser with realistic HTML snippets
- Tests circuit breaker, human behavior delays, queue management
- Tests scheduler with time-based business hour logic
- Tests AI gate classification with cache and fallback

### 3.9 `test_schemas_crm.py` (509 lines) — Pydantic Schema Validation

**Quality:** HIGH

- Tests validation rules: blank names, missing fields, whitespace stripping
- Tests `model_dump(exclude_unset=True)` behavior for PATCH endpoints
- Tests enum validation (quote result must be "won" or "lost")
- Tests default values and optional fields
- Good boundary testing

---

## 4. Test Gaps

### 4.1 Routers Without Dedicated Test Files

| Router | File | Status |
|---|---|---|
| `knowledge.py` | 404 lines | **NO TEST FILE** — Knowledge Ledger CRUD, Q&A, AI insights |
| `nc_admin.py` | — | No dedicated test (some indirect coverage in `test_nc_phase*` files) |
| `tagging_admin.py` | — | Indirect coverage only via `test_tagging_api.py` |
| `requisitions2.py` | — | Covered by `test_requisitions2_routes.py` and `test_requisitions2_templates.py` |

### 4.2 Services Without Dedicated Test Files

| Service | Status |
|---|---|
| `teams_notifications.py` | **NO TEST FILE** — Teams webhook integration |
| `teams_action_tokens.py` | **NO TEST FILE** — Teams action token generation |
| `prospect_discovery_explorium.py` | **NO TEST FILE** — Explorium discovery integration |
| `prospect_free_enrichment.py` | **NO TEST FILE** — Free enrichment pipeline |
| `prospect_claim.py` | **NO TEST FILE** — Prospect claiming logic |
| `enrichment_utils.py` | **NO TEST FILE** — Enrichment utility functions |
| `freeform_parser_service.py` | Covered by `test_free_text_parser.py` |
| `crm_service.py` | Covered indirectly by `test_routers_crm.py` |
| `vendor_scorecard.py` | Covered indirectly by `test_vendor_score*.py` |
| `customer_enrichment_batch.py` | No direct test file |
| `customer_enrichment_service.py` | No direct test file |

### 4.3 Connectors

All 9 connector modules have test coverage via `test_connectors.py`, `test_connector_coverage_100.py`, `test_element14_connector.py`, and `test_ai_live_web_connector.py`. **No connector gaps identified.**

### 4.4 Schemas

Most schemas have corresponding test files (`test_schemas_crm.py`, `test_schemas_rfq.py`, `test_schemas_ai.py`, `test_schemas_vendors.py`, `test_schemas_sources.py`, `test_schemas_errors.py`, `test_schemas_v13_features.py`). Missing:
- `schemas/knowledge.py` — no test
- `schemas/explorium.py` — no test
- `schemas/apollo.py` — no test
- `schemas/enrichment.py` — no test
- `schemas/prospect_account.py` — covered by `test_models/test_prospect_account.py`

---

## 5. Naming Conventions

### Consistent Patterns (GOOD)

- **Router tests:** `test_routers_{module}.py` — e.g., `test_routers_rfq.py`, `test_routers_crm.py`, `test_routers_admin.py`
- **Service tests:** `test_services_{module}.py` or `test_{service_name}.py` — e.g., `test_services_ownership.py`, `test_search_service.py`
- **Schema tests:** `test_schemas_{module}.py` — e.g., `test_schemas_crm.py`
- **Test classes:** `class Test{Feature}` — consistently used
- **Test functions:** `def test_{behavior_description}` — clear snake_case names

### Naming Inconsistencies (MINOR)

- **Duplicate naming patterns:** Some services have both `test_{name}.py` and `test_services_{name}.py`:
  - `test_vendor_score.py` + `test_vendor_score_gaps.py` + `test_scoring_coverage.py`
  - `test_startup.py` + `test_startup_full.py` + `test_startup_gaps.py`
- **Coverage-chasing files:** 11 files with "coverage" in the name (see section 6.3)
- **Phase-based naming:** `test_nc_phase2.py` through `test_nc_phase9.py`, `test_email_intelligence_phase1.py` through `test_email_intelligence_phase6.py` — these reflect development phases rather than feature domains
- **Deprecated stub:** `test_routers_vendors.py` is empty (11 lines), with a note to delete it

---

## 6. Anti-Patterns & Concerns

### 6.1 Coverage-Chasing Test Files (MODERATE concern)

There are **11 files** with "coverage" in the name, totaling ~8,200 lines:

```
test_coverage_gaps.py           (1,076 lines)
test_coverage_remaining.py      (533 lines)
test_coverage_quick_wins.py     (556 lines)
test_coverage_100.py            (467 lines)
test_coverage_gaps_final.py     (489 lines)
test_coverage_gaps_services.py  (737 lines)
test_coverage_final_gaps.py     (689 lines)
test_coverage_routers_final.py  (1,639 lines)
test_coverage_utils_final.py    (695 lines)
test_coverage_services_100.py   (1,225 lines)
test_coverage_final.py          (44 lines)
test_final_coverage_100.py      (1,531 lines)
```

These files exist solely to "close coverage gaps" and target specific line numbers (e.g., "Cover line 371: substring match in check-duplicate"). While they increase coverage metrics, they:
- Don't test meaningful behavior — they verify code *executes* without checking correctness
- Create maintenance burden — line-number references break when code changes
- Scatter tests across files — the same module may have tests in 3-4 different files

**Recommendation:** Merge coverage-gap tests into the primary test file for each module.

### 6.2 Module-Scoped Fixtures (LOW concern)

Several test files use `@pytest.fixture(scope="module")`:
- `test_ux_reshape.py` (4 fixtures)
- `test_shared_framework.py` (3 fixtures)
- `test_rfq_frontend_validation.py` (8 fixtures)
- `test_rfq_redesign.py` (1 fixture)
- `test_sales_sourcing_tabs.py` (2 fixtures)

These are used for parsing HTML/JS files (read-only operations), so the shared state is safe. However, they could cause confusion if someone adds mutating tests.

### 6.3 Potential Test Order Dependencies

8 files reference `depends_on` or `requires` patterns:
- `test_buy_plan_schemas.py`, `test_buy_plan_v3_router.py`, `test_multiplier_score.py`
- `test_avail_score.py`, `test_integration_quote_workflow.py`
- `test_outreach.py`, `test_routers_v13.py`, `test_v13_activity_ownership.py`

On closer inspection, these use `pytest.mark.skipif` conditions (e.g., "skip if model doesn't have attribute X") rather than true inter-test dependencies. This is acceptable but could be refactored to use `pytest.importorskip` or conditional imports.

### 6.4 `time.sleep()` in Tests (LOW concern)

Only 3 files use `time.sleep()`:
- `test_connectors.py` — 20ms sleep to test circuit breaker timeout (acceptable)
- `test_circuit_breaker.py` — 150ms sleep for reset timeout (acceptable)
- `test_nc_worker_full.py` — reference in docstring only

These are short, deterministic sleeps for testing timeout behavior. No concern.

### 6.5 Real HTTP Client References (LOW concern)

2 files reference `httpx.post`/`httpx.get` directly:
- `test_eight_by_eight_strengthen.py`
- `test_8x8_service.py`

Both properly mock these calls via `@patch("app.services.eight_by_eight_service.httpx.post")`. No real API calls are made.

### 6.6 Global State Manipulation

6 files manipulate module-level state (clearing caches, patching globals). All properly restore state after tests via fixtures or context managers.

### 6.7 Deprecated/Empty Test File

`test_routers_vendors.py` (11 lines) is an empty stub with a note: "Delete it once the split is confirmed stable." Its 197 tests were moved to 4 domain-specific files. This file should be deleted.

---

## 7. Strengths Summary

1. **Excellent mocking discipline** — All external APIs (Graph, Nexar, BrokerBin, DigiKey, Mouser, Claude, 8x8) are mocked. Zero real API calls in the test suite.

2. **Strong test isolation** — Autouse `db_session` fixture cleans all rows between tests. AI gate state is reset. Event loops are fresh per test.

3. **Comprehensive async test support** — 962 async tests using `pytest-asyncio` with proper `AsyncMock` usage for async service functions.

4. **Rich fixture ecosystem** — 15+ factory fixtures in conftest covering all major model types, plus inline factory helpers in individual test files.

5. **Business rule coverage** — Tests explicitly verify domain logic: 30-day ownership sweep, scoring weights, noise email filtering, RFQ state machines, vendor blacklisting.

6. **Edge case coverage** — `None` inputs, empty strings, malformed data, concurrent access patterns, database commit failures.

7. **Integration testing** — Multiple integration test files (`test_integration_*.py`, `test_e2e_sourcing_flow.py`) test cross-service workflows.

8. **Smoke tests** — `test_integration_smoke.py` validates that test infrastructure itself works correctly.

---

## 8. Recommendations (Priority Order)

### P0 — Gaps to Close

1. **Add tests for `routers/knowledge.py`** — 404-line router with zero test coverage (CRUD + AI insights)
2. **Add tests for `services/teams_notifications.py`** — Teams webhook integration is in-progress feature, needs tests before shipping
3. **Add tests for `services/teams_action_tokens.py`** — Teams action tokens need security tests

### P1 — Cleanup

4. **Delete `test_routers_vendors.py`** — deprecated empty stub
5. **Consolidate coverage-gap files** — merge the 11 `test_coverage_*.py` files into their primary test files (e.g., merge CRM coverage gaps into `test_routers_crm.py`)
6. **Add tests for missing schemas** — `schemas/knowledge.py`, `schemas/explorium.py`, `schemas/apollo.py`

### P2 — Improvement

7. **Standardize naming** — adopt either `test_{service}.py` or `test_services_{service}.py`, not both
8. **Eliminate phase-based naming** — rename `test_nc_phase{N}.py` and `test_email_intelligence_phase{N}.py` to feature-descriptive names
9. **Add property-based tests** — scoring functions and normalizers are ideal candidates for Hypothesis testing
10. **Add mutation testing** — validate that tests actually catch bugs (not just execute code)

---

## 9. Overall Grade: **B+**

The test suite is **large, well-structured, and operationally sound**. External API isolation is excellent, fixtures are clean, and business rules are well-tested. The main issues are organizational: coverage-chasing files fragment test logic, naming is inconsistent, and a few newer features (knowledge ledger, Teams notifications) lack test coverage entirely. Fixing the P0 gaps and consolidating the P1 cleanup items would bring this to an A.
