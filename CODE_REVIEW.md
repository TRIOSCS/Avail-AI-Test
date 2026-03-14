# AvailAI Full Code Review — March 14, 2026

## Executive Summary

AvailAI is a **well-structured, production-grade** electronic component sourcing platform. The codebase is large (~700 Python files, ~15,700 lines of frontend JS, 76 Alembic migrations, 314 test files with 8,605 test functions) and demonstrates solid architecture overall. This review covers every layer — models, services, routers, connectors, frontend, infrastructure, and tests — organized by severity.

**Overall Grade: B+** — Strong foundation with security items and architectural debt to address before scaling.

---

## Table of Contents

1. [Critical Issues](#critical-issues)
2. [High Priority Issues](#high-priority-issues)
3. [Medium Priority Issues](#medium-priority-issues)
4. [Low Priority Issues](#low-priority-issues)
5. [Architecture Strengths](#architecture-strengths)
6. [Recommended Action Plan](#recommended-action-plan)

---

## Critical Issues

### C1. Agent API Key Timing Attack
**File:** `app/dependencies.py:50-55`  
**Layer:** Security

The agent API key comparison uses `==` (string equality), which is vulnerable to timing attacks:
```python
if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
```

**Fix:** Use `secrets.compare_digest()` for constant-time comparison. Add audit logging when the agent key is used.

---

### C2. SQL Injection via Unescaped ILIKE — Strategic Vendor Service
**File:** `app/services/strategic_vendor_service.py:274`  
**Layer:** Services

`search` is interpolated into an ILIKE pattern without `escape_like()`:
```python
q = q.filter(VendorCard.display_name.ilike(f"%{search}%"))
```
User-controlled `search` can inject `%` or `_` wildcards to manipulate query behavior.

**Fix:** Use `escape_like(search)` before interpolation.

---

### C3. SQL Injection via Unescaped ILIKE — Vendor Affinity Service
**File:** `app/services/vendor_affinity_service.py:179`  
**Layer:** Services

`category` from AI classification is used in ILIKE without escaping:
```python
.filter(MaterialCard.category.ilike(f"%{category}%"))
```

**Fix:** Use `escape_like(category)` before interpolation.

---

### C4. Quote Number Race Condition
**File:** `app/services/crm_service.py:15-27`  
**Layer:** Services

`next_quote_number()` reads the last quote and increments without locking or atomic sequence. Concurrent requests can return the same quote number.

**Fix:** Use `SELECT ... FOR UPDATE`, a PostgreSQL sequence, or advisory locks.

---

### C5. XSS via Unescaped `onedrive_url` in href
**File:** `app/static/app.js:5278`  
**Layer:** Frontend

`onedrive_url` from API is inserted into `href` without escaping. A URL containing `"` or a `javascript:` scheme can break out of the attribute or execute code.

**Fix:** Use `escAttr()` and validate the URL scheme (allow only `https:`).

---

### C6. Missing `AZURE_CLIENT_SECRET` Validation in Entrypoint
**File:** `docker-entrypoint.sh:4-9`  
**Layer:** Infrastructure

The entrypoint validates `DATABASE_URL`, `SESSION_SECRET`, `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` but not `AZURE_CLIENT_SECRET`. The app can start without the OAuth client secret.

**Fix:** Add `AZURE_CLIENT_SECRET` to the required environment variables check.

---

### C7. Partial Index Uses Unbound Column
**Files:** `app/models/ics_search_queue.py:46,52`, `app/models/nc_search_queue.py:47,53`  
**Layer:** Models

`postgresql_where=(Column("status") == "queued")` creates an unbound column reference that may not bind correctly to the table at migration time.

**Fix:** Use `text("status = 'queued'")` instead.

---

### C8. Routers Contain Heavy Business Logic (Architecture Violation)
**Layer:** Routers

Multiple routers contain direct DB queries and business logic instead of delegating to services:
- `app/routers/views.py` — 30+ `db.query()` calls
- `app/routers/crm/quotes.py` — 15+ DB calls
- `app/routers/crm/offers.py` — 40+ DB calls
- `app/routers/crm/companies.py` — 20+ DB calls
- `app/routers/crm/sites.py` — 15+ DB calls
- `app/routers/materials.py` — 35+ DB calls
- `app/routers/sources.py` — 25+ DB calls
- `app/routers/rfq.py` — 15+ DB calls

This violates the "thin routers / fat services" architecture rule and makes the logic harder to test and reuse.

**Fix:** Extract DB queries and logic into service functions. Prioritize the heaviest offenders (`views.py`, `offers.py`, `materials.py`).

---

## High Priority Issues

### H1. No Rate Limiting on Password Login
**File:** `app/routers/auth.py:216`  
**Layer:** Security

The password login endpoint has no brute-force protection. While `ENABLE_PASSWORD_LOGIN` is off by default, when enabled there's no slowapi decorator or lockout mechanism.

**Fix:** Add `@limiter.limit("5/minute")` to the password login endpoint.

---

### H2. Information Disclosure in `/auth/status`
**File:** `app/routers/auth.py:272-311`  
**Layer:** Security

When logged in, returns all users with refresh tokens (id, name, email, role, status, M365 details). Any authenticated user can see other users' emails and roles.

**Fix:** Restrict to admins or return only the current user's data.

---

### H3. Silent FK Reassignment Failure in Vendor Merge
**File:** `app/services/vendor_merge_service.py:86-94`  
**Layer:** Services

FK reassignment failures during vendor merge are silently caught with `except Exception` and logged at `debug` level. Related records can become orphaned with no rollback.

**Fix:** Wrap the entire merge in a single transaction. Raise on failure instead of silently continuing.

---

### H4. Missing Index on `material_cards.deleted_at`
**File:** `app/models/intelligence.py:49`  
**Layer:** Models

Soft-delete queries (`WHERE deleted_at IS NULL`) run on every MaterialCard lookup without an index — full table scan on a hot table.

**Fix:** Add migration: `Index("ix_material_cards_deleted_at", "deleted_at")`.

---

### H5. XSS — `esc()` Used Instead of `escAttr()` for href Attributes
**Files:** `app/static/crm.js:1587,2442`, `app/static/app.js:7186,14607`, `app/static/crm.js:2041`  
**Layer:** Frontend

`esc()` is for text content; it does not escape `"` which is needed for HTML attribute values. All `href="..."` attributes with dynamic values should use `escAttr()`.

**Fix:** Replace `esc()` with `escAttr()` for all `href` attribute values and validate URL schemes.

---

### H6. Bare `except Exception` Without Logging (92 instances)
**Layer:** Services

92 instances of `except Exception` across 30 service files. Key offenders:
- `enrichment_orchestrator.py` (8), `buyplan_notifications.py` (6), `nc_worker/session_manager.py` (6)
- `auto_dedup_service.py` (6), `vendor_affinity_service.py`, `strategic_vendor_service.py`
- `tagging.py`, `account_summary_service.py`, `customer_analysis_service.py`
- `activity_service.py`, `credential_service.py`

**Fix:** Replace with specific exception types. Where broad catch is intentional, use `logger.exception()`.

---

### H7. Sync Session in Async Background Tasks
**File:** `app/search_service.py:1480-1499`, `app/main.py:136-147,406-447`  
**Layer:** Services / Infrastructure

`_schedule_background_enrichment` uses `asyncio.create_task()` with sync `SessionLocal()`. `_warm_caches()` and `_seed_api_sources` use sync sessions during async lifespan. Sync DB calls inside async tasks block the event loop.

**Fix:** Use `run_in_executor` for sync DB work, or switch to async session usage.

---

### H8. CSRF Exemption Missing for Buy Plan V3 Token Endpoints
**File:** `app/main.py:289-294`  
**Layer:** Infrastructure

CSRF exempts `/api/buy-plans/token/.*` but not `/api/buy-plans-v3/token/.*`. V3 approval/reject PUTs can fail with CSRF errors when the user has a session.

**Fix:** Add `re.compile(r"/api/buy-plans-v3/token/.*")` to `exempt_urls`.

---

### H9. Missing `updated_at` Defaults on Multiple Models
**Layer:** Models

Several models have `updated_at` columns without `default` or `onupdate`:
- `Offer.updated_at` (`app/models/offers.py:73`)
- `Requisition.updated_at` (`app/models/sourcing.py:56`)
- `TroubleTicket.updated_at` (`app/models/trouble_ticket.py:62`)

**Fix:** Add `default=func.now()` and `onupdate=func.now()` via Alembic migration.

---

### H10. Missing `created_at`/`updated_at` on Multiple Models
**Layer:** Models

Project rules require both timestamps on every table. Missing from:
- `User` — missing `updated_at` (`app/models/auth.py:44`)
- `VendorContact` — missing both (`app/models/vendors.py:114-152`)
- `VendorReview` — missing `updated_at` (`app/models/vendors.py:155-169`)
- `QuoteLine` — missing both (`app/models/quotes.py:72-95`)
- `IcsSearchLog`, `NcSearchLog` — missing `created_at`
- `SystemConfig` — missing `created_at` (`app/models/config.py:45-57`)
- `GraphSubscription` — missing `updated_at` (`app/models/config.py:60-78`)
- `ProcessedMessage`, `SyncState` — missing both (`app/models/pipeline.py`)
- `ErrorReport` — missing `updated_at` (`app/models/error_report.py:37`)

**Fix:** Add via Alembic migration with `server_default=func.now()`.

---

### H11. Missing FK Indexes
**Layer:** Models

Foreign key columns used in JOINs but not indexed:
- `RequisitionAttachment.requisition_id` (`app/models/sourcing.py:179`)
- `RequirementAttachment.requirement_id` (`app/models/sourcing.py:194`)
- `ActivityLog.buy_plan_id` (`app/models/intelligence.py:259`)
- `DiscoveryBatch.status` (`app/models/discovery_batch.py`)

**Fix:** Add indexes via Alembic migration.

---

### H12. Connector Rate-Limit Handling Gaps
**Layer:** Connectors

- **Sourcengine** (`app/connectors/sourcengine.py:35`): No explicit 429 handling; `raise_for_status()` will raise on 429.
- **Element14** (`app/connectors/element14.py:57`): No 429/401 handling.
- **eBay** (`app/connectors/ebay.py:68-84`): 401 refresh only; 429 would raise.

**Fix:** Add 429 handling with retry/backoff to match other connectors.

---

### H13. Retry-After Header Not Capped
**File:** `app/connectors/sources.py:153`  
**Layer:** Connectors

`max(float(header), 1.0)` has no upper bound. A malicious or buggy API returning `Retry-After: 999999` would block the connector for 11+ days.

**Fix:** Cap at 300 seconds: `min(max(float(header), 1.0), 300.0)`.

---

### H14. Frontend Files Are Extremely Large
**Layer:** Frontend

- `app/static/app.js` — ~15,700 lines
- `app/static/crm.js` — ~8,400 lines

These monolithic files are very hard to navigate, review, and maintain.

**Fix:** Plan modularization into ES modules or use the existing Vite config to split by feature.

---

## Medium Priority Issues

### M1. SQL f-string in Vendor Analytics
**File:** `app/routers/vendor_analytics.py:185-238`  
**Layer:** Routers

`mpn_filter` is interpolated into a `sqltext(f"...")`. Currently safe (hardcoded string), but fragile.

**Fix:** Use parameterized query for both branches.

---

### M2. Password Hashing Uses SHA-256 in Startup Seed
**File:** `app/startup.py:63-80`  
**Layer:** Infrastructure

Default user creation uses `hashlib.sha256`. SHA-256 is too fast for passwords and vulnerable to brute force. (Note: the auth router uses PBKDF2-HMAC-SHA256 with 200K iterations, which is better.)

**Fix:** Align the startup seed to use the same PBKDF2 hashing as `auth.py`.

---

### M3. XSS via `javascript:` URLs in HTML Sanitizer
**File:** `app/static/app.js:458`  
**Layer:** Frontend

`sanitizeRichHtml` whitelists `href` on `<a>` tags but doesn't validate the URL scheme. `<a href="javascript:alert(1)">` can bypass CSP.

**Fix:** Validate that `href` starts with `http://`, `https://`, or `/`.

---

### M4. API Keys in URL Parameters
**Files:** `app/connectors/mouser.py:42`, `app/connectors/sources.py:279`  
**Layer:** Connectors

Mouser sends API key as query parameter; Nexar REST v4 sends `apikey` in query string. These appear in access logs, error traces, and Sentry events.

**Fix:** Switch to header-based auth if supported, or mask keys in log output.

---

### M5. Missing Input Validation
**Layer:** Services / Routers

- `app/routers/vendor_analytics.py:42-44` — `int()` conversion on query params with no try/except; non-numeric input causes 500.
- `vendor_affinity_service.py` — `find_vendor_affinity(mpn, db)` — no `mpn` length/type validation.
- `strategic_vendor_service.py:get_open_pool` — `limit`/`offset` not validated.

**Fix:** Use Pydantic `Query()` params with type constraints.

---

### M6. Token Refresh Blocks HTTP Requests
**File:** `app/dependencies.py:130-166`  
**Layer:** Infrastructure

`require_fresh_token` calls async token refresh inside the HTTP request handler, causing latency spikes.

**Fix:** Background refresh via scheduler with a tighter buffer window.

---

### M7. Unbounded Query in Ownership Sweep
**File:** `app/services/ownership_service.py:47-54`  
**Layer:** Services

`run_ownership_sweep` loads all owned companies with `.all()` and no limit.

**Fix:** Use `yield_per()` or batch processing.

---

### M8. Redis Cache Exceptions Silently Swallowed
**File:** `app/search_service.py:109-128`  
**Layer:** Services

`_get_search_cache` and `_set_search_cache` catch exceptions with `except Exception: pass`.

**Fix:** Log at `logger.warning` before returning/continuing.

---

### M9. Missing FK `ondelete` Cascades
**Layer:** Models

- `GraphSubscription.user_id` (`app/models/config.py:65`) — deleting a user leaves orphaned subscriptions.
- `ProspectAccount.discovery_batch_id` (`app/models/prospect_account.py:43`) — deleting a batch leaves orphaned references.

**Fix:** Add appropriate `ondelete="CASCADE"` or `ondelete="SET NULL"` via migration.

---

### M10. EnrichmentQueue Polymorphic Target Without Constraint
**File:** `app/models/enrichment.py:55-57`  
**Layer:** Models

`vendor_card_id`, `company_id`, `vendor_contact_id` are all nullable with no CHECK constraint that exactly one is set.

**Fix:** Add a CHECK constraint ensuring exactly one is non-null.

---

### M11. 101 Relationships Missing `back_populates`
**Layer:** Models

Many relationships are one-way (no inverse). Worst offenders: `offers.py` (4 User FKs), `buy_plan.py` (6 User FKs), `strategic.py` (2 using deprecated `backref=`).

**Fix:** Replace `backref=` with explicit `back_populates` on both sides.

---

### M12. Missing Response Models on Endpoints
**Layer:** Routers

Many endpoints lack `response_model`, so responses are not validated or documented in OpenAPI:
- `app/routers/crm/offers.py`, `app/routers/materials.py`, `app/routers/sources.py`, `app/routers/htmx_views.py`

**Fix:** Add Pydantic response models to all endpoints.

---

### M13. Inconsistent Cascade Rules on BuyPlanLine
**File:** `app/models/buy_plan.py:209-210`  
**Layer:** Models

`requirement_id` and `offer_id` use `ondelete="SET NULL"` — orphaned BuyPlanLine rows remain with NULL FK.

**Fix:** Use CASCADE or implement application-level cleanup.

---

### M14. Missing Unique Constraint on `site_contacts`
**File:** `app/models/crm.py:149-188`  
**Layer:** Models

No unique constraint on `(customer_site_id, email)` — duplicate contacts per site are allowed.

**Fix:** Add unique constraint via Alembic migration.

---

### M15. Hardcoded User in Startup Seed
**File:** `app/startup.py:103-127`  
**Layer:** Infrastructure

Seeds `vinod@trioscs.com` as admin with a hardcoded name and role.

**Fix:** Drive admin bootstrap from `ADMIN_EMAILS` env var instead.

---

### M16. `/health` Exposes Internal Status
**File:** `app/main.py:348-391`  
**Layer:** Infrastructure

Returns DB, Redis, scheduler, connector, and backup status to any caller.

**Fix:** Restrict detailed health to authenticated/admin callers; return minimal data for unauthenticated.

---

### M17. Frontend Accessibility Gaps
**Layer:** Frontend

- Many buttons have only `title`; missing `aria-label` for screen readers.
- Dynamically created modals/overlays may lack focus management and `aria-modal`.
- Missing `aria-live` for toasts, `aria-expanded` for accordions, `role="alert"` for errors.

---

### M18. Test Coverage Gaps
**Layer:** Tests

Routers with limited or no dedicated tests:
- `views.py`, `htmx_views.py`, `outreach.py`, `command_center.py`, `knowledge.py`, `tags.py`, `activity.py`, `task.py`, `vendor_inquiry.py`, `nc_admin.py`, `tagging_admin.py`, `prospect_pool.py`

Services with limited coverage:
- `email_intelligence_service.py`, `mailbox_intelligence.py`, `gradient_service.py`, `sse_broker.py`, `teams_notifications.py`, `teams_action_tokens.py`, `data_cleanup_service.py`, `integrity_service.py`, `credential_service.py`, `calendar_intelligence.py`, `freeform_parser_service.py`

---

### M19. No N+1 Query Tests
**Layer:** Tests

Zero tests verify eager-loading behavior. List endpoints for Offers, Requisitions, and ActivityLog likely have N+1 patterns under load.

**Fix:** Add pytest hook using `sqlalchemy.event` to assert max query count per endpoint.

---

### M20. Mixed `DateTime` vs `DateTime(timezone=True)`
**Layer:** Models

Some models use `DateTime`, others `DateTime(timezone=True)`. While `database.py` normalizes to UTC via event listener, explicit timezone-awareness is clearer.
- `notification.py`, `knowledge.py`, `teams_alert_config.py` use `DateTime(timezone=True)`
- `auth.py`, `sourcing.py` use `DateTime`

---

## Low Priority Issues

### L1. Denormalized Count Columns Allow NULL
**File:** `app/models/crm.py:54-55`  
`site_count` and `open_req_count` have `default=0` but are nullable. Add `nullable=False`.

### L2. Schemas Missing `from_attributes=True`
Only 6 of 24 Pydantic schema files have `ConfigDict(from_attributes=True)`.

### L3. Redundant `index=True` on Unique Columns
5 models have both `unique=True` and `index=True` — index is implicit with unique.
- `EmailSignatureExtract` (`app/models/enrichment.py:118-119`)
- `TeamsAlertConfig` (`app/models/teams_alert_config.py:35`)

### L4. Business Logic in Model Validator
**File:** `app/models/sourcing.py:156-160`  
`Sighting._coerce_moq` uses `@validates("moq")` to coerce `moq <= 0` to `None`. Validation logic belongs in services.

### L5. Buy Plan V1 Deprecation
`buy_plan_v1_enabled: bool = False` — if V1 is fully retired, remove its code paths.

### L6. MVP Mode Flag
`mvp_mode: bool = True` disables several features. Decide: flip to `False` or remove dead code.

### L7. Global Search Stub
**File:** `app/routers/views.py:56-68`  
`global_search` returns an empty list with a TODO.

### L8. Mixed Logging Styles
Some services use f-strings with loguru, others use structured `logger.info("msg", arg1, arg2)`. Prefer structured logging for consistency and tooling.

### L9. Code Duplication
- `run_ownership_sweep` and `run_site_ownership_sweep` share similar logic.
- `nc_worker` and `ics_worker` have similar session/queue patterns.

### L10. `VerificationGroupMember` Uses `added_at` Instead of `created_at`
**File:** `app/models/buy_plan.py:287`  
Naming differs from project convention.

### L11. Fetch Calls Without User Feedback
Several frontend fetch calls log errors to console without showing user-facing toasts:
- `app/static/app.js:576-581` — call-initiated activity
- `app/static/app.js:2806` — dismiss new offers
- `app/static/app.js:7842-7849` — score fetch

---

## Architecture Strengths

1. **Clean layer separation** — Thin routers → services → models pattern is well-established (though some routers need refactoring).
2. **Alembic discipline** — 76 migrations, strict rules enforced in CI. No raw DDL in startup.
3. **Auth middleware** — Well-designed dependency chain: `get_user` → `require_user` → `require_buyer/admin/sales`.
4. **UTC everywhere** — `UTCDateTime` type decorator + event listener ensures timezone consistency.
5. **Database tuning** — Connection pool (20+40 overflow), statement timeout (30s), lock timeout (5s), `pool_pre_ping`.
6. **Security headers** — CSRF, CSP, GZip, Sentry scrubbing of sensitive fields.
7. **Rate limiting** — slowapi with configurable limits (120/min default, 20/min for search).
8. **Comprehensive config** — 80+ settings with validators, CSV parsing, fail-fast on bad values.
9. **Test suite** — 314 test files, 8,605 test functions using in-memory SQLite with auth overrides.
10. **Circuit breaker** — Connector pattern prevents cascading failures with per-connector concurrency limits.
11. **Frontend sanitization** — `esc()`, `escAttr()`, and `sanitizeRichHtml()` are used widely.
12. **OAuth state validation** — Proper CSRF protection on auth flow.
13. **Email mining dedup** — `ProcessedMessage` with savepoint protection prevents duplicates.
14. **No `eval()` or `Function()`** — Frontend avoids dangerous JS patterns.
15. **Delta query caching** — Incremental inbox sync (not full scans).
16. **AbortController usage** — Frontend properly cancels fetch requests on view changes.
17. **Event listener cleanup** — Modal/popover handlers properly remove listeners on close.

---

## Recommended Action Plan

### Phase 1: Security Quick Wins (< 1 day)

| # | Action | File | Effort | Impact |
|---|--------|------|--------|--------|
| 1 | `secrets.compare_digest()` for agent API key | `dependencies.py` | 15 min | Fixes timing attack |
| 2 | Rate limit password login endpoint | `routers/auth.py` | 15 min | Prevents brute force |
| 3 | Cap Retry-After header at 300s | `connectors/sources.py` | 15 min | Prevents connector lockup |
| 4 | Validate `href` schemes in JS sanitizer | `static/app.js` | 30 min | Closes XSS vector |
| 5 | Use `escAttr()` for all `href` attributes | `static/app.js`, `crm.js` | 30 min | Closes XSS vector |
| 6 | Escape ILIKE patterns in services | `strategic_vendor_service.py`, `vendor_affinity_service.py` | 30 min | Fixes SQL injection |
| 7 | Add `AZURE_CLIENT_SECRET` to entrypoint validation | `docker-entrypoint.sh` | 5 min | Prevents misconfigured boot |
| 8 | Add CSRF exemption for V3 buy plan tokens | `main.py` | 5 min | Fixes token approval flow |
| 9 | Fix `onedrive_url` XSS | `static/app.js` | 15 min | Fixes critical XSS |

### Phase 2: Data Integrity (1-2 days)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 10 | Fix quote number race condition | 1 hour | Prevents duplicate quotes |
| 11 | Add index on `material_cards.deleted_at` | 30 min | Soft-delete performance |
| 12 | Add missing `created_at`/`updated_at` timestamps | 2 hours | Schema compliance |
| 13 | Add missing FK indexes | 1 hour | Query performance |
| 14 | Fix partial index unbound columns | 30 min | Migration correctness |
| 15 | Add unique constraint on site_contacts email | 30 min | Data integrity |
| 16 | Wrap vendor merge in proper transaction | 1 hour | Prevents data orphaning |

### Phase 3: Code Quality (1-2 weeks)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 17 | Extract DB logic from routers to services | 3-5 days | Architecture compliance |
| 18 | Narrow `except Exception` in top 10 files | 2 hours | Debuggability |
| 19 | Add response models to endpoints | 2 hours | API documentation |
| 20 | Replace sync sessions in async tasks | 2 hours | Event loop safety |
| 21 | Add 429 handling to remaining connectors | 2 hours | Resilience |
| 22 | Fix Redis cache silent failures | 30 min | Observability |
| 23 | Add N+1 query tests | 2 hours | Performance safety |
| 24 | Add missing FK `ondelete` cascades | 1 hour | Referential integrity |

### Phase 4: Long-term Improvements

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 25 | Split `app.js` and `crm.js` into modules | 1-2 weeks | Maintainability |
| 26 | Add `back_populates` to all relationships | 2 hours | ORM correctness |
| 27 | Normalize `DateTime(timezone=True)` usage | 1 hour | Consistency |
| 28 | Add tests for uncovered routers/services | 3-5 days | Test coverage |
| 29 | Organize services into subdirectories | 3-4 hours | Developer experience |
| 30 | Restrict `/health` endpoint | 30 min | Security hardening |
| 31 | Frontend accessibility improvements | 2-3 days | WCAG compliance |
| 32 | Restrict `/auth/status` user list to admins | 30 min | Information security |

---

## Summary by Layer

| Layer | Critical | High | Medium | Low |
|-------|----------|------|--------|-----|
| Security | 2 | 2 | 1 | — |
| Models | 1 | 3 | 6 | 4 |
| Services | 2 | 3 | 3 | 2 |
| Routers | 1 | 1 | 3 | 1 |
| Connectors | — | 2 | 1 | — |
| Frontend | 1 | 2 | 2 | 1 |
| Infrastructure | 1 | 1 | 2 | 1 |
| Tests | — | — | 2 | 1 |
| **Total** | **8** | **14** | **20** | **10** |

---

*Review conducted March 14, 2026. Covers all Python (698 files), JavaScript (app.js + crm.js), HTML templates, Docker/infrastructure configuration, and 76 Alembic migrations.*
