# AVAIL AI — Full Code Review

**Date:** 2026-03-14
**Branch:** `cursor/full-code-review-25bb`
**Scope:** Complete codebase — models, routers, services, connectors, frontend, tests, security, infrastructure

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Critical Issues (Fix Now)](#2-critical-issues)
3. [Security Findings](#3-security-findings)
4. [Models & Database](#4-models--database)
5. [Routers](#5-routers)
6. [Services](#6-services)
7. [Connectors](#7-connectors)
8. [Frontend](#8-frontend)
9. [Tests](#9-tests)
10. [Infrastructure & Deployment](#10-infrastructure--deployment)
11. [What's Working Well](#11-whats-working-well)
12. [Prioritized Action Plan](#12-prioritized-action-plan)

---

## 1. Executive Summary

**Codebase size:** 40 models, 51 router modules, 134 services, 9 connectors, 287 test files (8,147 tests)

**Overall grade: B+** — The codebase is operationally sound with strong architectural patterns, excellent test coverage, and zero `print()` usage. The main concerns are: (1) a handful of security issues in auth/session handling, (2) several routers that violate the "thin router" rule, (3) missing `updated_at` on 27 models, and (4) some connectors with unsafe type casts.

| Area | Grade | Top Concern |
|------|-------|-------------|
| Models | B | 27 models missing `updated_at`; 45+ FKs without `ondelete` |
| Routers | B- | 5 "fat" routers with inline business logic; `tagging_admin.py` missing admin auth |
| Services | A- | Zero `print()` calls; good separation; a few atomicity gaps |
| Connectors | B+ | Dead code bug in `sources.py`; unsafe casts in NexarConnector |
| Frontend | B- | XSS in `onclick` handlers; `app.js` at 15,766 lines |
| Tests | B+ | 8,147 tests, zero real API calls; some newer features untested |
| Security | B- | Timing-unsafe agent key; hardcoded salt; CSRF gap on `/v2/*` |
| Infrastructure | A- | Solid Docker setup; minor container hardening gaps |

---

## 2. Critical Issues

These should be fixed before any other work. They represent active bugs or security vulnerabilities.

### CRIT-01: Dead code — abstract method unreachable (`sources.py:159-161`)

The `@abstractmethod` decorator for `_do_search` is indented inside the `_parse_retry_after` function, after a `return` statement. It's unreachable dead code. Subclasses all define `_do_search` so it works in practice, but `BaseConnector` doesn't enforce the contract.

**Fix:** Unindent lines 159-161 so they're methods of `BaseConnector`, and move them before `_parse_retry_after`.

### CRIT-02: Timing-unsafe agent key comparison (`dependencies.py:54`)

The `x-agent-key` header is compared with `==` instead of `hmac.compare_digest()`, enabling timing attacks to brute-force the key character by character.

**Fix:** Replace `== settings.agent_api_key` with `hmac.compare_digest(header_value, settings.agent_api_key)`.

### CRIT-03: `tagging_admin.py` — 18 admin endpoints only require `require_user`

Any authenticated user can trigger AI backfills, bulk enrichment, and purge operations. These endpoints should use `require_admin`.

**Fix:** Change the dependency from `require_user` to `require_admin` on all endpoints in `tagging_admin.py`.

### CRIT-04: Decryption silently falls back to plaintext (`utils/encrypted_type.py:54-59`)

When Fernet decryption fails (including `InvalidToken`), the raw value is returned instead of raising an error. This silently degrades encryption to a no-op.

**Fix:** Log a warning and raise or return a sentinel value instead of silently returning the ciphertext as plaintext.

### CRIT-05: Non-atomic multi-entity creation (`proactive_service.py:534-680`)

`convert_proactive_to_win` creates Requisition + Requirements + Offers + Quote + BuyPlan without proper rollback protection. A failure partway through leaves orphaned records.

**Fix:** Wrap the entire operation in a single transaction with proper savepoints.

### CRIT-06: Unsafe `int()`/`float()` casts in NexarConnector (`sources.py:350-351, 481-482, 526-527`)

Raw `int(qty)` and `float(price)` will crash on non-numeric strings (e.g., `"many"`, `"call"`). Every other connector uses `safe_int`/`safe_float`.

**Fix:** Replace with `safe_int(qty)` and `safe_float(price)` at all six locations.

### CRIT-07: SSE stream endpoint has no authentication (`requisitions2.py`)

The Server-Sent Events endpoint is open to unauthenticated requests. Anyone with the URL can subscribe to real-time updates.

**Fix:** Add `require_user` dependency to the SSE endpoint.

---

## 3. Security Findings

### High Severity

| ID | Issue | Location | Fix |
|----|-------|----------|-----|
| SEC-01 | Agent key timing attack | `dependencies.py:54` | Use `hmac.compare_digest()` |
| SEC-02 | Agent key — no rate limit or IP restriction | `dependencies.py:50-55` | Add rate limiting, log failures |
| SEC-03 | Hardcoded encryption salt | `utils/encrypted_type.py:25` | Generate random salt per secret, store alongside |
| SEC-04 | Decryption falls back to plaintext | `utils/encrypted_type.py:54-59` | Raise error on decryption failure |
| SEC-05 | Default PostgreSQL password `availai` | `docker-compose.yml:23-25` | Use env var / Docker secret |
| SEC-06 | Redis has no authentication | `docker-compose.yml:53` | Set `requirepass` |
| SEC-07 | Entire `/v2/*` is CSRF-exempt | `main.py:294` | Narrow exemption to specific endpoints |
| SEC-08 | `ENABLE_PASSWORD_LOGIN` can bypass Azure AD | `auth.py:191-199` | Ensure it's `false` in production |

### Medium Severity

| ID | Issue | Location | Fix |
|----|-------|----------|-----|
| SEC-09 | Default user role is `admin` | `startup.py:74` | Default to `viewer` or `buyer` |
| SEC-10 | No server-side session store | `main.py:268-274` | Use Redis-backed sessions |
| SEC-11 | Hardcoded admin user on every boot | `startup.py:103-128` | Move to migration seed data |
| SEC-12 | `/auth/status` leaks all users' info | `auth.py:271-311` | Restrict to admin or return only current user |
| SEC-13 | CSP uses `'unsafe-inline'` | `main.py:325` | Refactor inline scripts to separate files |
| SEC-14 | `--forwarded-allow-ips "*"` | `Dockerfile:53` | Restrict to Caddy's IP |
| SEC-15 | No rate limit on password login | `auth.py:215` | Add `slowapi` limiter |
| SEC-16 | Logout doesn't clear M365 tokens from DB | `auth.py:185-188` | Delete stored tokens on logout |
| SEC-17 | Admin auto-promotion overrides manual demotions | `auth.py:156-158` | Only promote if role is unset |

### Low Severity

| ID | Issue | Location |
|----|-------|----------|
| SEC-18 | `/metrics` exposed on app port 8000 | Caddy blocks it, but direct access works |
| SEC-19 | `X-Frame-Options` conflict (Caddy: SAMEORIGIN, FastAPI: DENY) | Conflicting headers |
| SEC-20 | Missing env vars produce WARNING not hard exit | `docker-entrypoint.sh:10-12` |

---

## 4. Models & Database

### Summary: 40 model files, ~3,200 lines

#### P0 — Data Integrity

| Issue | Count | Details |
|-------|-------|---------|
| `TeamsNotificationLog.user_id` is plain Integer, not ForeignKey | 1 | No referential integrity |
| `TroubleTicket.updated_at` has `onupdate` but no `default` | 1 | NULL on creation |
| FKs referencing `users.id` with no `ondelete` clause | 30+ | User deletion throws constraint errors |

#### P1 — Performance

| Issue | Count | Details |
|-------|-------|---------|
| Duplicate indexes (column `index=True` + explicit `Index()`) | 12 | Wasted disk, slower writes |
| Missing indexes on FK columns | 5+ | `requisition_attachments.requisition_id`, `requirement_attachments.requirement_id`, etc. |
| Float used for financial values | 6 | `Sighting.unit_price`, `VendorCard.total_revenue`, etc. — use `Numeric` |
| Default lazy loading on all relationships | ~all | N+1 risk on `Requisition.requirements`, `BuyPlanV3.lines`, etc. |

#### P2 — Consistency

| Issue | Count | Details |
|-------|-------|---------|
| Models missing `updated_at` | 27 | Project rule: every table gets `created_at` + `updated_at` |
| Inconsistent DateTime types | 3 patterns | Plain `DateTime`, `DateTime(timezone=True)`, `UTCDateTime` |
| FK columns without `relationship()` | 7 | `VendorResponse`, `EmailIntelligence`, `Notification`, etc. |
| Old-style `backref=` instead of `back_populates` | 2 | `StrategicVendor`, `QuoteLine` |

---

## 5. Routers

### Summary: 51 router modules, ~15,000 lines

#### Fat Router Violations (Worst Offenders)

| File | Lines | Issue |
|------|-------|-------|
| `views.py` | 1,337 | 7 query-builder functions, inline HTML, data mutation |
| `requirements.py` | 1,336 | Massive endpoint definitions |
| `rfq.py` | 882 | 225-line activity endpoint, 160-line RFQ prepare |
| `sources.py` | 724 | 9 test connector classes defined in the router file |
| `materials.py` | 674 | Manufacturer inference, merge operation, stock import pipeline |
| `ai.py` | 651 | Offer creation, requisition creation inline |
| `data_ops.py` | 666 | Admin data operations with inline logic |

**Action:** Extract business logic from each into corresponding service files.

#### Auth/Permission Issues

| File | Issue |
|------|-------|
| `tagging_admin.py` (all 18 endpoints) | Uses `require_user` instead of `require_admin` |
| `nc_admin.py` / `ics_admin.py` | Admin mutation endpoints open to any authenticated user |
| `requisitions2.py` (SSE endpoint) | No authentication at all |

#### Input Validation Issues

| File | Issue |
|------|-------|
| `tags.py` | Search `q` parameter used in ILIKE without `escape_like()` — wildcard injection |
| `vendor_analytics.py` | SQL constructed with f-string in `sqltext()` — fragile pattern |

#### Other Issues

- GET endpoints that mutate DB state (should be POST/PUT)
- Missing response models on several endpoints
- Inconsistent error status codes

---

## 6. Services

### Summary: 134 service files, ~25,000 lines

#### P0 — Critical

| Issue | File | Lines |
|-------|------|-------|
| Non-atomic multi-entity creation | `proactive_service.py` | 534-680 |
| Sync DB sessions in async functions (blocks event loop) | `ownership_service.py`, `email_intelligence_service.py`, `email_threads.py`, `proactive_service.py`, `engagement_scorer.py`, `vendor_score.py` | Multiple |
| OData injection risk | `email_threads.py` | 322, 359, 429 |

#### P1 — High

| Issue | File | Lines |
|-------|------|-------|
| N+1 queries (individual queries per item in loops) | `avail_score_service.py` | 387-403, 412-433, 699-730 |
| `run_until_complete()` crash in running event loop | `auto_dedup_service.py` | 181, 201 |
| Mixed commit responsibility (service + router both commit) | `buyplan_notifications.py` | Multiple |
| TOCTOU race condition (check-then-insert without lock) | `proactive_service.py` | 121-131 |
| Merge service has no permission validation | `vendor_merge_service.py` | — |
| Quote number generation race condition | `crm_service.py` | 14-26 |
| Dual writes to `VendorCard.engagement_score` | `vendor_score.py:217`, `engagement_scorer.py:290` | Non-deterministic |

#### P2 — Medium

| Issue | Details |
|-------|---------|
| `HTTPException` raised from service layer | `requisition_service.py` — should use domain exceptions |
| Python's `PermissionError` used for authorization | `buyplan_workflow.py` — should use custom exception |
| `_clean_email_body()` duplicated with different behavior | `ai_email_parser.py` vs `response_parser.py` |
| Global mutable state without thread safety | `_last_proactive_scan`, `_thread_cache`, `_ROUTING_MAPS` |

#### Positive Notes

- Zero `print()` statements across all 134 files — 100% loguru compliance
- Every file has proper docstring headers
- Good use of `with_for_update()` for ownership claims
- AI confidence routing (auto >= 0.8, review 0.5-0.8) consistently implemented
- Batch processing in scoring services to limit memory usage

---

## 7. Connectors

### Summary: 9 connector files, ~2,000 lines

#### Critical

| Issue | File | Lines |
|-------|------|-------|
| `_do_search` abstract method is unreachable dead code | `sources.py` | 159-161 |
| Unsafe `int()`/`float()` — crash on non-numeric strings | `sources.py` (NexarConnector) | 350, 351, 481, 482, 526, 527 |
| Sync DB calls block async event loop | `email_mining.py` | All DB operations |

#### High

| Issue | File | Lines |
|-------|------|-------|
| Missing `try/except` on `r.json()` | `sourcengine.py:36`, `mouser.py:62`, `ebay.py:85`, `element14.py:59`, `digikey.py:115` |
| No token expiry tracking (extra 401 round-trips) | `sources.py` (Nexar), `ebay.py` | Multiple |
| Silent exception swallowing with no logging | `email_mining.py` | 119-121 |
| AI classification errors logged at DEBUG (invisible in prod) | `email_mining.py` | 349-350 |
| Deprecated `asyncio.get_event_loop()` pattern | `email_mining.py` | 334 |

#### Medium

| Issue | File |
|-------|------|
| `import re` inside method body (every call) | `mouser.py:93` |
| Accumulated `last_body` memory leak | `email_mining.py:695` |
| Seller name not URL-encoded | `ebay.py:140` |
| `0.0` price treated as falsy (shows `None`) | `element14.py:95` |

#### What's Good

- Circuit breaker pattern with per-connector semaphores — excellent resilience
- `safe_int`/`safe_float` used correctly in 7 of 9 connectors
- AI output quality gate in `ai_live_web.py` — strong validation of non-deterministic data
- DigiKey token handling with expiry tracking — gold standard, should be template for others
- No hardcoded credentials anywhere

---

## 8. Frontend

### Summary: `app.js` (15,766 lines), `crm.js` (~7,000 lines), `index.html` (Jinja2 template)

#### High Severity

| Issue | Location | Details |
|-------|----------|---------|
| XSS in `onclick` handlers | `app.js:7755, 7779, 8114`, `crm.js:6585` | Vendor names escaped with `.replace(/'/g, "\\'")` only — doesn't escape `"`, `<`, `>` |
| XSS in `<option>` tags | `crm.js:6660` | User name/email injected without escaping |
| 705 inline `onclick` handlers | Throughout | Forces CSP `'unsafe-inline'`, prevents migration to strict CSP |
| Code organization | `app.js` | 15,766 lines, 150+ functions on `window` — maintainability risk |

#### Medium Severity

| Issue | Location | Details |
|-------|----------|---------|
| CSRF bypass | `app.js:576` | Raw `fetch()` skips CSRF-protected `apiFetch()` wrapper |
| Silent error handling | `crm.js:923, 1474, 7042, 7060` | Empty `catch` blocks swallow API failures |
| Memory leaks | `crm.js:5062` | `setInterval` never cleared; event listeners added without cleanup |
| Uncaught JSON.parse | `app.js:2620` | `JSON.parse` on localStorage data without try/catch |
| Accessibility | Throughout | ~50 inputs without labels, 12 total ARIA attributes across 700+ interactive elements |

#### Positive

- Auth/session management is well-implemented (CSRF double-submit cookie, session expiry redirect)
- No hardcoded secrets, API keys, or IP addresses in frontend
- `showToast` already sanitizes innerHTML (recent XSS hardening)

---

## 9. Tests

### Summary: 287 test files, 8,147 tests, 137,859 lines

#### Strengths (Grade: A- overall)

- **Zero real API calls** — every external integration is properly mocked
- **Well-engineered conftest.py** — in-memory SQLite with PG type patching, autouse session cleanup, auth overrides
- **Scoring tests** verify mathematical properties (monotonicity, symmetry, midpoint)
- **Connector tests** exhaustively test circuit breaker state machine
- **Email tests** cover classification branches, noise filtering, batch lifecycle

#### Gaps

**Missing test files (P0):**
- `routers/knowledge.py` (349 lines, zero tests)
- `services/teams_notifications.py` (no tests)
- `services/teams_action_tokens.py` (no tests)
- `services/prospect_discovery_explorium.py` (no tests)
- `services/prospect_free_enrichment.py` (no tests)
- `services/prospect_claim.py` (no tests)
- Schemas: `knowledge.py`, `explorium.py`, `apollo.py`, `enrichment.py` (no tests)

#### Anti-Patterns

- **11 "coverage-chasing" files** (~8,200 lines) targeting specific line numbers rather than behaviors
- **Deprecated stub** `test_routers_vendors.py` (empty, should be deleted)
- **Inconsistent naming** — both `test_{name}.py` and `test_services_{name}.py` patterns
- **Phase-based naming** (`test_nc_phase2-9.py`, `test_email_intelligence_phase1-6.py`) reflects development history, not feature domains

---

## 10. Infrastructure & Deployment

### Docker Compose — Grade: A-

- Well-configured PostgreSQL 16 with tuned params and 2G memory limit
- Redis 7 with maxmemory and eviction policy
- Multi-stage build (Node for Vite frontend, Python for backend)
- Non-root user `appuser` in container
- Caddy with auto-HTTPS

**Issues:**
- Default `availai` password for PostgreSQL (use Docker secrets)
- Redis has no authentication
- `--forwarded-allow-ips "*"` allows IP spoofing

### Dockerfile — Grade: B+

- Multi-stage build is efficient
- Chromium for Playwright/Patchright included (large image size)
- Non-root user is good security practice

### Missing

- No health check defined in `docker-compose.yml` for the app container (only `depends_on`)
- No resource limits on the app container (only DB has memory limit)
- No log rotation configuration for app logs

---

## 11. What's Working Well

These are genuine strengths worth preserving:

1. **100% loguru compliance** — zero `print()` across 134 service files
2. **Test discipline** — 8,147 tests with zero real API calls and proper mocking
3. **Circuit breaker + semaphore pattern** in connectors — production-grade resilience
4. **Fat services, thin routers** architecture — mostly followed correctly
5. **AI confidence routing** (auto >= 0.8, review 0.5-0.8) — consistently implemented
6. **Docstring headers** on every file — purpose, dependencies, callers documented
7. **STABLE.md** registry — prevents accidental refactoring of critical files
8. **Alembic-only DDL rule** — enforced in CI with DDL grep check
9. **DigiKey token handling** with expiry tracking — gold standard connector pattern
10. **AI output quality gate** in `ai_live_web.py` — strong validation of non-deterministic data

---

## 12. Prioritized Action Plan

### Phase 1 — Security Fixes (Do This Week)

| # | Task | Effort | Risk if Skipped |
|---|------|--------|-----------------|
| 1 | Fix agent key timing attack (`hmac.compare_digest`) | 10 min | Active vulnerability |
| 2 | Add `require_admin` to `tagging_admin.py` (18 endpoints) | 15 min | Privilege escalation |
| 3 | Add auth to SSE endpoint in `requisitions2.py` | 5 min | Data leak |
| 4 | Fix `nc_admin.py` / `ics_admin.py` auth | 10 min | Privilege escalation |
| 5 | Fix decryption fallback to plaintext | 15 min | Silent security degradation |
| 6 | Narrow CSRF exemption from `/v2/*` | 30 min | CSRF on state-changing endpoints |
| 7 | Add rate limiting to password login | 15 min | Brute-force risk |

### Phase 2 — Data Integrity (Next Sprint)

| # | Task | Effort |
|---|------|--------|
| 8 | Fix `TeamsNotificationLog.user_id` — make it a ForeignKey | Migration |
| 9 | Add `ondelete` to 30+ FK columns (batch migration) | Migration |
| 10 | Fix `proactive_service.py` atomicity (wrap in transaction) | 2 hrs |
| 11 | Fix quote number generation race condition | 1 hr |
| 12 | Replace Float with Numeric for 6 financial columns | Migration |

### Phase 3 — Performance & Correctness (Next Sprint)

| # | Task | Effort |
|---|------|--------|
| 13 | Fix dead code bug in `sources.py:159-161` | 10 min |
| 14 | Replace unsafe `int()`/`float()` in NexarConnector | 15 min |
| 15 | Add `try/except` to `r.json()` in 5 connectors | 30 min |
| 16 | Fix N+1 queries in `avail_score_service.py` | 2 hrs |
| 17 | Fix `run_until_complete()` crash in `auto_dedup_service.py` | 30 min |
| 18 | Remove 12 duplicate indexes (migration) | Migration |
| 19 | Add missing indexes on 5+ FK columns (migration) | Migration |

### Phase 4 — Code Quality (Ongoing)

| # | Task | Effort |
|---|------|--------|
| 20 | Extract business logic from 5 fat routers to services | 8-12 hrs total |
| 21 | Fix XSS in `onclick` handlers (use `escAttr()`) | 2 hrs |
| 22 | Add `updated_at` to 27 models (batch migration) | Migration |
| 23 | Unify DateTime type usage to `UTCDateTime` | Migration |
| 24 | Add token expiry tracking to Nexar + eBay connectors | 2 hrs |
| 25 | Write tests for untested services (teams, knowledge, prospect) | 4-6 hrs |

### Phase 5 — Frontend & Long-Term

| # | Task | Effort |
|---|------|--------|
| 26 | Split `app.js` (15,766 lines) into modules | Multi-day |
| 27 | Replace 705 inline `onclick` handlers with event delegation | Multi-day |
| 28 | Add accessibility (ARIA, labels, focus management) | Multi-day |
| 29 | Migrate to Redis-backed sessions | 4-6 hrs |
| 30 | Add Docker health checks and resource limits | 1 hr |

---

*Review conducted 2026-03-14. Total issues found: 87 (7 critical, 18 high, 24 medium, 15 low, 23 improvement recommendations).*
