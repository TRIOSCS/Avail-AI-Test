# AVAIL AI — Full Code Review

**Date:** 2026-03-14
**Scope:** Complete codebase review — models, routers, services, connectors, schemas, frontend, tests, security
**Codebase:** 698 Python files, 12 JS files, 83 HTML templates

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Security Findings](#security-findings)
3. [Models Review](#models-review)
4. [Routers Review](#routers-review)
5. [Services Review](#services-review)
6. [Connectors Review](#connectors-review)
7. [Schemas Review](#schemas-review)
8. [Frontend Review](#frontend-review)
9. [Test Suite Review](#test-suite-review)
10. [Recommended Action Plan](#recommended-action-plan)

---

## Executive Summary

The AVAIL AI codebase is a substantial, production-running application with strong foundations: proper separation of concerns (thin routers / fat services), consistent use of Loguru over print(), SQLAlchemy models with Alembic migrations, CSRF protection, session security, and a comprehensive test suite (8,000+ tests). The architecture is sound.

However, this review identified **97 findings** across all areas. The most urgent items relate to security (token logging, missing rate limiting, XSS), data integrity (missing cascade rules, missing timestamps), and reliability (concurrent session usage, failed email marking records as sent).

### Finding Distribution

| Severity | Count | Key Areas |
|----------|-------|-----------|
| **Critical** | 17 | Security (token logging, XSS), data integrity (cascades, broken ABC), reliability |
| **High** | 30 | Fat routers, missing auth, N+1 queries, connector error handling |
| **Medium** | 27 | Missing indexes, hardcoded values, validation gaps, CSP |
| **Low** | 23 | Accessibility, naming, documentation, code organization |

---

## Security Findings

### CRITICAL

**S1. OAuth token exposure in logs** — `app/routers/auth.py:119,124`
Azure token exchange errors log up to 500 chars of `resp.text`, which can include `access_token` and `refresh_token`. These could appear in Sentry, log aggregation, or stdout.
**Fix:** Log only the status code and a generic error message.

**S2. Graph API error responses may expose tokens** — `app/utils/graph_client.py:159-164,192-193`
Graph API error responses are logged and returned to clients (up to 300 chars). Error payloads can contain sensitive data.
**Fix:** Return sanitized error messages only. Do not log raw response bodies.

### HIGH

**S3. Password login lacks rate limiting** — `app/routers/auth.py:215-244`
`POST /auth/login` has no `@limiter.limit()` decorator. OAuth callback is rate-limited (10/min), but password login is unprotected. Enables brute-force attacks.
**Fix:** Add `@limiter.limit("5/minute")` to the password login route.

**S4. Path traversal in OneDrive browse** — `app/routers/crm/offers.py:810-817`
User-supplied `path` query parameter is interpolated into a Graph API URL: `f"/me/drive/root:/{path}:/children"`. Values containing `..` could traverse the OneDrive folder hierarchy.
**Fix:** Validate `path` — block `..`, leading `/`, and other traversal patterns.

**S5. Default credentials in startup** — `app/startup.py:72-96`
When `DEFAULT_USER_EMAIL` and `DEFAULT_USER_PASSWORD` env vars are set, a default admin user is created. If these are weak or left in production, they become a backdoor.
**Fix:** Gate behind `TESTING=1` or add a startup check that fails in production.

**S6. Agent API key bypass** — `app/dependencies.py:52-56`
A valid `x-agent-key` grants full access as `agent@availai.local`. If `agent_api_key` is empty in config, agent auth is skipped. No validation that the key is non-empty when agent auth is required.
**Fix:** Validate `agent_api_key` is non-empty at startup; use strong random keys; consider IP allowlisting.

### MEDIUM

**S7. SQL injection risk in vendor analytics** — `app/routers/vendor_analytics.py:184-217`
`mpn_filter` is built from `escape_like(q)` but interpolated via f-string into raw SQL. While `escape_like` handles LIKE metacharacters, this pattern is fragile.
**Fix:** Use parameterized queries with `:param` bindings for all dynamic SQL.

**S8. Information disclosure — full user list** — `app/routers/crm/enrichment.py:218-230`
`GET /api/users/list` returns all users (id, name, email, role) to any authenticated user.
**Fix:** Restrict to managers/admins or return only names/IDs needed for dropdowns.

**S9. Auth status exposes all users' M365 connection data** — `app/routers/auth.py:271-313`
Any authenticated user can see M365 connection status for all users with refresh tokens.
**Fix:** Restrict to admins or return only the current user's status.

**S10. Buy plan token endpoints are unauthenticated** — `app/routers/crm/buy_plans_v3.py:276-312`
Token-based approval/reject endpoints use no session auth. Ensure tokens are cryptographically strong, single-use, and time-limited.

---

## Models Review

### CRITICAL

**M1. User model missing `updated_at`** — `app/models/auth.py:44-45`
Project rules require `created_at` and `updated_at` on every table. `User` has `created_at` but no `updated_at`.

**M2. VendorResponse FKs missing cascade rules** — `app/models/offers.py:177-178`
`contact_id` and `requisition_id` have no `ondelete`. Deleting a Contact or Requisition leaves invalid FK references.
**Fix:** Add `ondelete="SET NULL"`.

**M3. VendorReview FKs missing cascade rules** — `app/models/vendors.py:157-158`
`vendor_card_id` and `user_id` have no `ondelete`. FK constraint violations on delete.
**Fix:** Add `ondelete="CASCADE"` for `vendor_card_id`, `ondelete="SET NULL"` for `user_id`.

**M4. Quote.customer_site_id missing cascade** — `app/models/quotes.py:28`
No `ondelete`. Deleting a CustomerSite can fail or leave orphaned quotes.

### HIGH

**M5. QuoteLine missing `created_at`/`updated_at`** — `app/models/quotes.py:72-94`

**M6. ActivityLog partial index syntax issue** — `app/models/intelligence.py:298-341`
Partial indexes use `postgresql_where=Column("company_id").isnot(None)`, which creates an unbound column. Should reference the model's column directly.

**M7-M15. Multiple FK columns missing `ondelete`** across:
- `sourcing.py` — `Requisition.updated_by_id`, `RequisitionAttachment.uploaded_by_id`
- `offers.py` — `Offer.entered_by_id`, `updated_by_id`, `approved_by_id`, `promoted_by_id`
- `intelligence.py` — `ProactiveOffer.converted_requisition_id`, `ProactiveMatch.salesperson_id`
- `config.py` — `GraphSubscription.user_id`

**M16. ApiUsageLog missing `created_at`** — `app/models/config.py:84-95`

**M17. Notification missing `updated_at`** — `app/models/notification.py:17-27`

**M18. TagThresholdConfig/KnowledgeConfig missing timestamps** — `app/models/tags.py:101-114`, `app/models/knowledge.py:85-96`

### MEDIUM

**M19. Missing indexes on frequently filtered columns:**
- `users.role`, `users.is_active`
- `vendor_cards.domain`, `vendor_cards.is_new_vendor`
- `material_cards.deleted_at`, `material_cards.manufacturer`
- `vendor_responses.status`, `vendor_responses.received_at`
- `discovery_batches.status`, `discovery_batches.source`
- `prospect_accounts.company_id`, `prospect_accounts.claimed_by`

**M20-M25. Additional missing cascades** across `crm.py`, `buy_plan.py`, `strategic.py`, `performance.py`, `email_intelligence.py`.

---

## Routers Review

### CRITICAL

**R1. SSE stream without auth** — `app/routers/requisitions2.py:98-126`
`GET /requisitions2/stream` has no `Depends(require_user)`. Any client can subscribe to real-time requisition updates.
**Fix:** Add `require_user` dependency.

**R2-R5. Raw dict input instead of Pydantic** — Multiple endpoints accept `body: dict` with no schema:
- `app/routers/rfq.py:231-247` — PATCH vendor response status
- `app/routers/rfq.py:605-616` — POST batch follow-up
- `app/routers/knowledge.py:164-182` — PUT knowledge config
- `app/routers/admin/system.py:230`, `admin/data_ops.py:580` — credentials and channel routing

### HIGH

**R6. Fat routers with business logic:**
- `app/routers/rfq.py:278-388,396-519,629-768` — `get_activity`, `rfq_prepare`, `_enrich_with_vendor_cards` contain DB queries, grouping, enrichment logic
- `app/routers/views.py:76-140,416-440,530-560` — `_query_requisitions`, `_query_companies`, etc. perform DB queries directly
- Multiple routers contain direct `db.query(...)` calls: `auth.py`, `command_center.py`, `sources.py`, `vendors_crud.py`, `materials.py`

**R7. Missing auth on user activities** — `app/routers/v13_features/activity.py:179-189`
`get_user_activities(target_user_id)` returns any user's activities without scope check.

**R8. Missing pagination on list endpoints** — `rfq.py`, `ai.py`, `vendor_contacts.py`
Multiple list endpoints return unbounded or loosely bounded results (`.limit(50)`). No offset/cursor-based pagination.

**R9. Inconsistent error responses** — `knowledge.py:396`, `strategic.py:73-86`
Some routes return 200 on errors or use `JSONResponse` instead of `HTTPException`.

### MEDIUM

**R10. Missing `response_model`** on many routes across `rfq.py`, `sources.py`, `command_center.py`, `ai.py`.

**R11. `entity_type` not validated** — `tags.py:86`, `ai.py:128-145`
Accepts any string; should use an enum or allowlist.

**R12. Date parsing without try/except** — `activity.py:146-147,191-193`
Invalid ISO strings raise unhandled exceptions.

**R13. Admin check via string split** — `knowledge.py:173-174`
Manual `settings.ADMIN_EMAILS.split(",")` instead of a proper `require_admin` dependency.

**R14. Raw `int()` on query params** — `vendors_crud.py:334-336`, `materials.py:204-206`, `vendor_analytics.py:45-46`
Can raise `ValueError` on invalid input. Use FastAPI `Query()` parameters instead.

---

## Services Review

### CRITICAL

**SV1. Proactive offer marked as sent when email fails** — `app/services/proactive_service.py:338-354`
If Graph API `sendMail` fails, the exception is caught and logged, but execution continues. Match statuses are set to `"sent"` and the transaction is committed. Users see offers that were never delivered.
**Fix:** Re-raise the exception or mark the offer as `"failed"`.

**SV2. LIKE wildcard injection** — `app/services/strategic_vendor_service.py:274`
User-controlled `search` is used in `ilike(f"%{search}%")` without escaping.
**Fix:** Use `escape_like()`.

**SV3. Session used concurrently in async tasks** — `app/services/ownership_service.py:542-742`
`send_manager_digest_email` passes the same `db` session into multiple coroutines run with `asyncio.gather`. SQLAlchemy sessions are not safe for concurrent use.
**Fix:** Use a separate session per coroutine or run sequentially.

### HIGH

**SV4. Integrity healing rolls back entire batch on single failure** — `app/services/integrity_service.py:165-226`
On exception, `db.rollback()` discards all previously healed records in the batch.
**Fix:** Use savepoints (`db.begin_nested()`) or per-record commits.

**SV5. N+1 in fact extraction** — `app/services/email_intelligence_service.py:433-634`
`dedup_q.count()` runs a query per fact inside a loop.
**Fix:** Preload existing facts in one query.

**SV6. Preload limits can skew vendor scores** — `app/services/vendor_score.py:130-156`
`.limit(50000)` and `.limit(10000)` on offer/quote queries. Large vendors may have incomplete data.

**SV7. Engagement scorer caps vendor cards at 5,000** — `app/services/engagement_scorer.py:175`

### MEDIUM

**SV8. Missing `escape_like`** — `app/services/vendor_affinity_service.py:179`
User-controlled `category` used in `ilike` without escaping.

**SV9. Hardcoded tunable values** across multiple services:
- `sourcing_score.py` — sigmoid midpoints, weights
- `vendor_score.py` — `MIN_OFFERS_FOR_SCORE = 5`, `BATCH_SIZE = 1000`
- `engagement_scorer.py` — `COLD_START_SCORE = 50`, `VELOCITY_IDEAL_HOURS = 4`
- `health_monitor.py` — `QUOTA_WARN_THRESHOLD = 80`
- `proactive_service.py` — `cost * 1.3` default margin
**Fix:** Move to config or environment variables.

**SV10. `check_and_claim_open_account` does not commit** — `app/services/ownership_service.py:109-136`
Uses `db.flush()` but never `db.commit()`. Callers must remember to commit.

---

## Connectors Review

### CRITICAL

**C1. Broken abstract method in BaseConnector** — `app/connectors/sources.py:158-161`
`@abstractmethod` and `async def _do_search` are indented inside `_parse_retry_after`, making them unreachable. The ABC contract is broken.
**Fix:** Move to the `BaseConnector` class body at the correct indentation level.

**C2. eBay connector: no token expiry** — `app/connectors/ebay.py:23-41`
Tokens are cached indefinitely. Expired tokens cause repeated 401s.
**Fix:** Add `_token_expires_at` tracking like DigiKey.

**C3. Nexar connector: no token expiry** — `app/connectors/sources.py:221-236`
Same issue as eBay.

### HIGH

**C4. Element14: no 429/403 handling** — `app/connectors/element14.py:54-60`
Only handles 400. Rate limit (429) and auth (403) errors propagate as unhandled exceptions.

**C5. Sourcengine: no rate limit or auth error handling** — `app/connectors/sourcengine.py:34-38`
No handling for 429, 401, or 403.

**C6. Sourcengine: no JSON parse error handling** — `app/connectors/sourcengine.py:36`
`r.json()` can raise on invalid JSON.

**C7. Mouser: `r.json()` before error body check** — `app/connectors/mouser.py:61-64`
JSON parsing can fail before error handling runs.

### MEDIUM

**C8. Missing concurrency limits** — `app/connectors/sources.py:60-66`
`_CONNECTOR_CONCURRENCY` omits EbayConnector, Element14Connector, SourcengineConnector, and AIWebSearchConnector (defaults to 3).

**C9. DigiKey token refresh race** — `app/connectors/digikey.py:86-95`
Concurrent requests can all trigger redundant token refreshes on 401.

**C10. Deprecated `asyncio.get_event_loop()`** — `app/connectors/email_mining.py:331-334`
Deprecated in Python 3.10+.
**Fix:** Use `asyncio.get_running_loop()`.

---

## Schemas Review

### CRITICAL

**SC1. Unvalidated `list[dict]` in `ApplyFreeformRfqRequest`** — `app/schemas/ai.py:228-236`
`requirements: list[dict]` accepts arbitrary dicts with no schema. Expected fields (`primary_mpn`, `target_qty`, etc.) are not enforced.

**SC2-SC3. Missing email validation** — `app/schemas/emails.py:56-60`, `app/schemas/v13_features.py:109-113`
`EmailReplyRequest.to` and `EmailClickLog.email` have no email format validation.

### HIGH

**SC4. Duplicate schema names — `PhoneCallLog`** — `app/schemas/rfq.py` vs `app/schemas/v13_features.py`
Two different schemas with the same name but different structures.

**SC5. Duplicate schema names — `PoolStats`** — `app/schemas/prospect_account.py` vs `app/schemas/prospect_pool.py`
Same issue.

**SC6. Missing phone validation** — `app/schemas/crm.py:144-145,221-222`
`contact_phone_2` lacks the validation applied to `contact_phone`.

**SC7. Missing numeric constraints on `OfferUpdate`** — `app/schemas/crm.py:396-412`
`qty_available`, `unit_price`, `moq` lack `ge=0` (unlike `OfferCreate`).

**SC8-SC9. Missing email validation** on multiple fields across `crm.py`, `enrichment.py`.

### MEDIUM

**SC10. Inconsistent optional typing** — Mix of `Optional[int]` and `int | None` across schemas.

**SC11. No numeric constraints on `QuoteLineItem`** — `app/schemas/crm.py:446-455`

**SC12. `EmailReplyRequest.body` no max length** — `app/schemas/emails.py:60`

**SC13. `BuyPlanApprove.line_items` untyped dict** — `app/schemas/crm.py:419-422`

---

## Frontend Review

### CRITICAL

**F1. XSS in `renderResponsiveTable`** — `app/static/app.js:92-114`
`row[col.key]` and `col.format()` output are concatenated into HTML without escaping. `row.id` is injected into `onclick` without `escAttr()`.
**Fix:** Apply `esc()` for text content and `escAttr()` for attribute values.

**F2. XSS via `col.format`** — `app/static/app.js:92-114`
Format functions can return unescaped HTML with user data.
**Fix:** Document that `format` must return sanitized HTML, or escape by default.

### HIGH

**F3. 150+ `innerHTML` usages with potential user data** — `app/static/app.js`, `app/static/crm.js`
Many `innerHTML` assignments mix user data. Several risky patterns found:
- `crm.js:1336` — notes rendered without escaping
- `crm.js:4324` — contact data rendered directly
- `crm.js:5385` — proactive site contacts rendered directly

### MEDIUM

**F4. Error handling gaps** — Multiple catch blocks log to console without user feedback (e.g., `crm.js:629`, `crm.js:4313`, `app.js:2806`).

**F5. Inconsistent loading states** — Some flows show skeletons/loading text, others update silently.

**F6. CSP includes `'unsafe-inline'` for `script-src`** — `app/main.py:312-366`
Needed for inline onclick handlers. Plan migration to nonces.

### LOW

**F7. File sizes** — `app.js` ~15,766 lines, `crm.js` ~8,468 lines. Consider module splitting.

**F8. Accessibility** — Some interactive elements lack `aria-label`, `role`, or `tabindex`.

**F9. Memory** — Caches (`_companyDetailCache`, `_ddReqCache`) grow without eviction.

---

## Test Suite Review

### What's Strong
- 8,000+ tests passing
- Solid conftest with role-based users and core entity fixtures
- External APIs properly mocked (connectors, Graph, AI)
- Good DB isolation (rollback + FK-safe delete, SQLite, `TESTING=1`)
- Strong coverage for: scoring, email service, search service, connectors, ownership

### Coverage Gaps — Services Without Tests
- `acctivate_sync` (listed as core in project rules)
- `sse_broker`
- `teams_notifications`, `teams_action_tokens`
- `response_analytics`
- `salesperson_scorecard`, `vendor_scorecard`
- `knowledge_service`
- `calendar_intelligence`, `mailbox_intelligence`
- `buyer_leaderboard`, `health_monitor`
- `freeform_parser_service`, `customer_enrichment_batch`
- `prospect_discovery_email`, `prospect_free_enrichment`

### Quality Issues
- Some tests only assert `status_code == 200` without checking response payload (e.g., `test_tagging_backfill.py`)
- A few "doesn't crash" style tests (e.g., `test_buyplan_scoring.py:85`)
- Very large test files: `test_routers_rfq.py` (~2,400 lines), `test_nc_worker_full.py` (~3,500 lines)
- Multiple `test_*_coverage*.py` and `test_*_gaps*.py` files suggest incremental patches rather than focused tests

### Recommendations
- Add tests for the 15+ untested services listed above
- Replace status-code-only assertions with behavior-focused checks
- Split large test files by feature area
- Add boundary tests (max lengths, empty collections, rate limits)

---

## Recommended Action Plan

### Phase 1: Security (Immediate — this week)

| # | Item | Effort |
|---|------|--------|
| 1 | Stop logging OAuth/Graph response bodies (S1, S2) | 30 min |
| 2 | Add rate limiting to password login (S3) | 15 min |
| 3 | Sanitize OneDrive browse path (S4) | 30 min |
| 4 | Add auth to SSE stream endpoint (R1) | 15 min |
| 5 | Fix XSS in `renderResponsiveTable` (F1, F2) | 1 hr |
| 6 | Validate `agent_api_key` is non-empty at startup (S6) | 15 min |

### Phase 2: Data Integrity (Next sprint)

| # | Item | Effort |
|---|------|--------|
| 7 | Add `updated_at` to User model (M1) — Alembic migration | 30 min |
| 8 | Add missing timestamps (QuoteLine, ApiUsageLog, Notification, etc.) | 1 hr |
| 9 | Add `ondelete` cascade rules to ~25 FK columns | 2 hr |
| 10 | Fix broken ABC in BaseConnector (C1) | 15 min |
| 11 | Add token expiry to eBay/Nexar connectors (C2, C3) | 1 hr |
| 12 | Fix proactive offer marking sent on email failure (SV1) | 30 min |

### Phase 3: Code Quality (Ongoing)

| # | Item | Effort |
|---|------|--------|
| 13 | Replace raw dict inputs with Pydantic schemas (R2-R5, SC1) | 2 hr |
| 14 | Move business logic from routers to services (R6) | 4 hr |
| 15 | Fix concurrent session usage in ownership service (SV3) | 1 hr |
| 16 | Add escape_like() for all user-controlled LIKE queries (SV2, SV8) | 30 min |
| 17 | Add missing indexes (M19) | 1 hr |
| 18 | Add 429/403 handling to Element14/Sourcengine (C4, C5) | 1 hr |
| 19 | Resolve duplicate schema names (SC4, SC5) | 30 min |

### Phase 4: Test Coverage (Ongoing)

| # | Item | Effort |
|---|------|--------|
| 20 | Add tests for 15+ untested services | 8 hr |
| 21 | Replace status-code-only assertions | 2 hr |
| 22 | Split large test files | 2 hr |
| 23 | Add boundary/edge case tests | 4 hr |

### Phase 5: Polish (When time allows)

| # | Item | Effort |
|---|------|--------|
| 24 | Move hardcoded values to config (SV9) | 2 hr |
| 25 | Add pagination to unbounded list endpoints (R8) | 2 hr |
| 26 | Migrate CSP away from `unsafe-inline` (F6) | 4 hr |
| 27 | Split app.js/crm.js into modules (F7) | 8 hr |
| 28 | Audit all innerHTML usages (F3) | 4 hr |
| 29 | Add accessibility improvements (F8) | 4 hr |
| 30 | Add `response_model` to routes (R10) | 2 hr |

---

## Positive Observations

The codebase has many things done well:

1. **Clean separation of concerns** — Routers are mostly thin with logic in services (a few exceptions noted above)
2. **Consistent logging** — Loguru used everywhere, no `print()` found in services
3. **Security controls** — CSRF middleware, session security (HTTPS-only, SameSite), rate limiting, Sentry scrubbing
4. **Migration discipline** — All schema changes in Alembic, no DDL in startup
5. **Comprehensive test suite** — 8,000+ tests with proper mocking and DB isolation
6. **Error monitoring** — Sentry integration with sensitive data filtering
7. **Structured configuration** — Pydantic Settings, env-var driven, feature flags
8. **Connector pattern** — BaseConnector with retry, circuit breaker, and concurrency control
9. **Session management** — Proper HTTP-only cookies, no tokens in localStorage
10. **Auth middleware** — Role-based access control with `require_user`, `require_buyer`, `require_sales` dependencies
