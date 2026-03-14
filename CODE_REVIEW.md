# AVAIL AI — Full Code Review

**Date:** 2026-03-14
**Scope:** All layers — models, services, routers, schemas, connectors, frontend, tests, security, deployment
**Codebase:** 698 Python files, 12 JS files, 83 HTML templates, 76 Alembic migrations

---

## Executive Summary

The AVAIL AI codebase is a substantial, feature-rich platform with solid fundamentals — Alembic-managed migrations, structured logging via Loguru, proper async patterns, and good test coverage (8,000+ tests). However, the review uncovered **12 Critical**, **31 High**, **53 Medium**, and **35 Low** issues across all layers. The most urgent issues are security-related: an agent API key that bypasses normal auth on all endpoints, missing auth on SSE/HTMX endpoints, XSS in templates, and several SQL LIKE injection vectors.

| Severity | Count | Top Concern |
|----------|-------|-------------|
| Critical | 12 | Security bypasses, data loss risks, XSS |
| High | 31 | Unbounded queries, missing validation, business logic in routers |
| Medium | 53 | Broad exception handling, missing timestamps, inconsistencies |
| Low | 35 | Naming conventions, hardcoded values, minor cleanup |

---

## CRITICAL Issues (Fix Immediately)

### SEC-1: Agent API key bypasses all auth
- **File:** `app/dependencies.py` (lines 52–55)
- **Issue:** `require_user` accepts an `x-agent-key` header as an alternative to session auth. If the header matches `settings.agent_api_key`, the request is treated as `agent@availai.local` for ALL endpoints — including admin endpoints. If this key is weak or leaked, an attacker has full access.
- **Fix:** Create a dedicated `require_agent` dependency for agent-only routes. Do not let the agent key satisfy `require_user` on normal user endpoints.

### SEC-2: Missing auth on SSE stream endpoint
- **File:** `app/routers/requisitions2.py` (lines 97–125)
- **Issue:** `GET /requisitions2/stream` has no auth dependency. Anyone can subscribe to requisition table-refresh events and potentially see real-time data changes.
- **Fix:** Add `user: User = Depends(require_user)`.

### SEC-3: Missing role-based access on requisitions2 mutations
- **File:** `app/routers/requisitions2.py` (lines 197, 209, 271, 348)
- **Issue:** `inline_edit_cell`, `inline_save`, `row_action`, and `bulk_action` do not verify the user owns the requisition. A sales user could edit/archive/claim requisitions they don't own.
- **Fix:** Add `get_req_for_user(db, user, req_id)` checks before mutations.

### SEC-4: XSS in Jinja template via JavaScript context injection
- **File:** `app/templates/partials/sourcing/results.html` (line 9)
- **Issue:** `filter` from the query string is injected into a JS string without JS-safe escaping: `x-data="{ filter: '{{ filter or 'all' }}' }"`. A value like `filter=all';alert(1);//` breaks out of the string.
- **Fix:** Use `{{ (filter or 'all') | tojson }}` to produce a JSON-safe string.

### SEC-5: CSRF token missing on direct fetch call
- **File:** `app/static/app.js` (lines 576–581)
- **Issue:** `logCallInitiated` uses raw `fetch()` instead of `apiFetch()`, so the CSRF header is not sent.
- **Fix:** Use `apiFetch()` or manually include the CSRF header.

### SEC-6: OneDrive path traversal
- **File:** `app/routers/crm/offers.py` (lines 798–816)
- **Issue:** The `path` parameter in `browse_onedrive` is user-controlled and passed directly to Graph API as `f"/me/drive/root:/{path}:/children"`. No validation for `..` or other traversal sequences.
- **Fix:** Validate and sanitize `path` — reject `..`, leading slashes, and other unsafe segments.

### SEC-7: Sensitive data in OAuth error logs
- **File:** `app/routers/auth.py` (lines 117–119)
- **Issue:** `logger.error(f"Azure token exchange returned {resp.status_code}: {resp.text[:500]}")` can log tokens or secrets from Azure's error responses.
- **Fix:** Log only the status code and a generic message.

### SEC-8: Default database password in deployment
- **Files:** `scripts/deploy.sh` (line 155), `docker-compose.yml` (lines 23–24)
- **Issue:** `POSTGRES_PASSWORD` defaults to `availai`. The deploy script does not set a strong password.
- **Fix:** Require a strong `POSTGRES_PASSWORD` in deploy and fail if missing.

### DATA-1: Inbox poll rollback discards entire batch
- **File:** `app/email_service.py` (lines 536–540)
- **Issue:** In `poll_inbox()`, when saving a single `VendorResponse` fails, `db.rollback()` rolls back the entire transaction, discarding ALL previously added `VendorResponse` and `ProcessedMessage` rows in that batch.
- **Fix:** Use `db.begin_nested()` (savepoint) per message so only the failing message is rolled back.

### DATA-2: Quote number race condition
- **File:** `app/services/crm_service.py` (function `next_quote_number()`, lines 15–27)
- **Issue:** Two concurrent requests can read the same `last` quote number and generate duplicates. No locking mechanism.
- **Fix:** Use `SELECT ... FOR UPDATE`, an advisory lock, or a database sequence.

### MODEL-1: Partial index uses unbound Column
- **Files:** `app/models/ics_search_queue.py` (lines 46, 52), `app/models/nc_search_queue.py` (lines 46, 52)
- **Issue:** `postgresql_where=(Column("status") == "queued")` creates a new, unbound `Column` object, which can produce incorrect SQL for the partial index.
- **Fix:** Reference the model's actual column: `postgresql_where=(status == "queued")`.

### MODEL-2: Orphaned SelfHealLog model breaks imports
- **File:** `app/models/self_heal_log.py`
- **Issue:** Contains only "REMOVED" text, but `scripts/ux_repair_engine.py` still imports `SelfHealLog`. Import will crash.
- **Fix:** Either restore the model or update/remove the script.

---

## HIGH Issues (Fix Soon)

### Security

| # | ID | File | Issue |
|---|----|------|-------|
| 1 | SEC-9 | `app/routers/tags.py:32` | `Tag.name.ilike(f"%{q}%")` — user input `q` not escaped with `escape_like()` |
| 2 | SEC-10 | `app/services/strategic_vendor_service.py:274` | `ilike(f"%{search}%")` without `escape_like()` |
| 3 | SEC-11 | `app/services/vendor_affinity_service.py:179` | `ilike(f"%{category}%")` without `escape_like()` (AI-derived input) |
| 4 | SEC-12 | `app/services/response_analytics.py:81` | `ilike(f"%@{domain}")` without `escape_like()` |
| 5 | SEC-13 | `app/routers/auth.py:271-312` | `/auth/status` exposes all users' M365 info to any authenticated user |
| 6 | SEC-14 | `app/cache/intel_cache.py:54` | Logs full Redis URL which may include auth credentials |
| 7 | SEC-15 | `app/routers/htmx_views.py:65-70` | `v2_page` uses `get_user` instead of `require_user` — auth not enforced by FastAPI |

### Data Integrity

| # | ID | File | Issue |
|---|----|------|-------|
| 8 | DATA-3 | `app/email_service.py:387-396` | Unbounded query loads ALL contacts from last 180 days with no LIMIT |
| 9 | DATA-4 | `app/services/ownership_service.py:47-54` | Unbounded query loads ALL owned companies with no LIMIT |
| 10 | DATA-5 | `app/services/enrichment_orchestrator.py:325-343` | Contact enrichment not implemented — `_load_entity` only supports company/vendor |
| 11 | DATA-6 | `app/services/enrichment_orchestrator.py:346-348` | `setattr(entity, field, value)` with field from Claude output — no allowlist |

### Architecture

| # | ID | File | Issue |
|---|----|------|-------|
| 12 | ARCH-1 | `app/routers/rfq.py:279-756` | Large business logic blocks in router (activity grouping, RFQ prep, enrichment) |
| 13 | ARCH-2 | Most routers | Direct `db.query()` calls in routers instead of services |
| 14 | ARCH-3 | `app/routers/auth.py:144-312` | Auth logic and DB access in router — should be in `services/auth_service.py` |

### Validation

| # | ID | File | Issue |
|---|----|------|-------|
| 15 | VAL-1 | Multiple routers | `body: dict` used instead of Pydantic schemas (rfq.py, core.py, requirements.py, prospect_suggested.py, knowledge.py, admin/system.py, admin/data_ops.py) |
| 16 | VAL-2 | `app/schemas/crm.py:424` | `BuyPlanApprove.line_items: list[dict]` — untyped |
| 17 | VAL-3 | `app/schemas/ai.py:235` | `ApplyFreeformRfqRequest.requirements: list[dict]` — allows arbitrary dicts |
| 18 | VAL-4 | Multiple schemas | `extra="allow"` on many schemas weakens validation contracts |

### Connectors

| # | ID | File | Issue |
|---|----|------|-------|
| 19 | CONN-1 | `app/connectors/sourcengine.py:33-38` | No handling for 429, 401, 403, 5xx, or JSON parse errors |
| 20 | CONN-2 | `app/connectors/element14.py:54-58` | No 429, 401, 403, or 5xx handling; `r.json()` not wrapped in try/except |
| 21 | CONN-3 | `app/connectors/ebay.py:24-41` | OAuth token never expires — cache reused indefinitely |
| 22 | CONN-4 | `app/connectors/sources.py:437-439` | BrokerBin: no 401/403/429/5xx handling |

### Frontend

| # | ID | File | Issue |
|---|----|------|-------|
| 23 | FE-1 | `app/static/app.js:92-114` | `renderResponsiveTable` can inject unescaped HTML via format functions |
| 24 | FE-2 | `app/static/touch.js:127-139` | `prospectDrawer` missing from close map — handler call fails silently |

### Models

| # | ID | File | Issue |
|---|----|------|-------|
| 25 | MODEL-3 | `app/models/config.py:65` | `GraphSubscription.user_id` FK missing `ondelete` |
| 26 | MODEL-4 | `app/models/vendors.py:157` | `VendorReview.vendor_card_id` FK missing `ondelete` |
| 27 | MODEL-5 | `app/models/auth.py:45` | `User` model missing `updated_at` column |

### Tests

| # | ID | File | Issue |
|---|----|------|-------|
| 28 | TEST-1 | Multiple test files | Using `asyncio.get_event_loop().run_until_complete()` instead of `@pytest.mark.asyncio` |

### Deployment

| # | ID | File | Issue |
|---|----|------|-------|
| 29 | DEPLOY-1 | `scripts/deploy.sh:181-185` | Deploy script overwrites Caddyfile, dropping security headers |
| 30 | DEPLOY-2 | `Dockerfile:54` | `--forwarded-allow-ips "*"` trusts ALL proxies for `X-Forwarded-*` headers |
| 31 | DEPLOY-3 | `app/routers/auth.py:190-198` | Password login can be enabled in production with `ENABLE_PASSWORD_LOGIN=true` |

---

## MEDIUM Issues (Plan to Fix)

### Models — Missing Timestamps

Many models are missing `created_at` and/or `updated_at` columns (project rules require both):

| Model | File | Missing |
|-------|------|---------|
| ApiUsageLog | `models/config.py` | both |
| GraphSubscription | `models/config.py` | `updated_at` |
| DiscoveryBatch | `models/discovery_batch.py` | `updated_at` |
| EmailIntelligence | `models/email_intelligence.py` | `updated_at` |
| EnrichmentJob, EnrichmentQueue | `models/enrichment.py` | `updated_at` |
| ErrorReport | `models/error_report.py` | `updated_at` |
| IcsClassificationCache, IcsSearchLog | `models/ics_*.py` | both |
| NcClassificationCache, NcSearchLog | `models/nc_*.py` | both |
| VendorMetricsSnapshot | `models/performance.py` | `updated_at` |
| QuoteLine, BuyPlan | `models/quotes.py` | both / `updated_at` |
| VendorContact | `models/vendors.py` | both |
| Tag, MaterialTag, EntityTag, TagThresholdConfig | `models/tags.py` | various |
| ProcessedMessage | `models/pipeline.py` | uses composite PK without `id` |
| SyncState, ColumnMappingCache, PendingBatch | `models/pipeline.py` | various |

### Services — Error Handling

| # | File | Issue |
|---|------|-------|
| 1 | `app/search_service.py:94-127` | `except Exception: pass` in Redis/cache operations hides all errors |
| 2 | `app/email_service.py` (multiple) | 8+ `except Exception` blocks that log and continue |
| 3 | `app/services/enrichment.py` (multiple) | Broad exception handling in batch operations |
| 4 | `app/email_service.py:203-219` | Tag propagation failure is silent (logged at DEBUG) |
| 5 | `app/search_service.py:1140-1151` | `asyncio.create_task` fire-and-forget — RuntimeError on no loop |
| 6 | `app/services/enrichment.py:114-153` | Shared session across long async enrichment runs |
| 7 | `app/services/ownership_service.py:116-127` | Redundant query in `check_and_claim_open_account` |

### Routers — Architecture

| # | File | Issue |
|---|------|-------|
| 8 | `app/routers/requisitions2.py:211-252` | `inline_save` accepts any string for `field` — no enum validation |
| 9 | `app/routers/materials.py:313-415` | `quick_search`, `enrich_material`, `merge_material_cards` use raw JSON bodies |
| 10 | `app/routers/requisitions/core.py:105-370` | `_build_requisition_list` is heavy logic in the router |
| 11 | `app/routers/crm/companies.py:244-403` | Duplicate company normalization logic |
| 12 | `app/routers/requisitions/core.py:510` | `mark_outcome` returns plain dict without explicit status code |

### Schemas

| # | File | Issue |
|---|------|-------|
| 13 | `app/schemas/emails.py:54-59` | `EmailReplyRequest` missing email format validation |
| 14 | `app/schemas/ai.py:27-30` | `ProspectFinderRequest.entity_id` optional when `entity_type` implies required |
| 15 | Various | Inconsistent naming (`RequisitionOut` vs `RequisitionListResponse`) |

### Connectors

| # | File | Issue |
|---|------|-------|
| 16 | `app/connectors/digikey.py:39-48` | No token refresh failure handling |
| 17 | `app/connectors/sources.py:219-223` | Nexar: no token refresh failure handling |
| 18 | `app/connectors/mouser.py:61` | `r.json()` called before `raise_for_status` |
| 19 | `app/connectors/element14.py:81` | `prices[0].get("cost")` crashes if prices is None/empty |
| 20 | `app/connectors/sources.py:281` | Nexar API key in URL params |

### Frontend

| # | File | Issue |
|---|------|-------|
| 21 | `app/static/app.js` (13 locations) | `console.warn`/`console.error` left in production code |
| 22 | Multiple | Inconsistent error handling in API calls — some errors swallowed |
| 23 | `app/static/touch.js:375-428` | Event listeners re-attached on dynamic elements without cleanup |
| 24 | `app/static/htmx_app.js:24-26` | Alpine store mutated directly — may not trigger reactivity |

### Deployment

| # | File | Issue |
|---|------|-------|
| 25 | `app/main.py` | No `CORSMiddleware` configured |
| 26 | `app/main.py:268-274` | Session cookie `max_age=86400` (24h) may be too long |
| 27 | `requirements.txt:4` | `psycopg2-binary` not recommended for production |
| 28 | `requirements.txt` | Transitive dependencies not pinned |
| 29 | `alembic/versions/076_*` | Data migration downgrade is lossy |

### Models

| # | File | Issue |
|---|------|-------|
| 30 | `app/models/offers.py:74` | `Offer.updated_at` has no default or onupdate |
| 31 | `app/models/trouble_ticket.py:59` | `TroubleTicket.updated_at` has no default |
| 32 | `app/models/ics_search_queue.py:37` | `updated_at` missing `onupdate` |
| 33 | `app/models/nc_search_queue.py:38` | `updated_at` missing `onupdate` |

### Utilities

| # | File | Issue |
|---|------|-------|
| 34 | `app/http_client.py:24-33` | Single 30s timeout — no separate connect vs read timeout |
| 35 | `app/file_utils.py:40-51` | `openpyxl.load_workbook` on large files can use excessive memory |
| 36 | `app/utils/graph_client.py:159-160` | 401 response body may contain tokens |
| 37 | `app/utils/token_manager.py:104` | Token refresh error logs may contain sensitive data |
| 38 | `app/cache/decorators.py:36-37` | `cached_endpoint` wrapper is sync — async endpoints may not be awaited |

### Tests

| # | File | Issue |
|---|------|-------|
| 39 | `tests/conftest.py:115-125` | `_reset_ai_gate_state` imports at runtime — fragile |
| 40 | Multiple test files | Mixing `asyncio.run()` and `@pytest.mark.asyncio` patterns |

---

## LOW Issues (Address When Convenient)

### Models
1. `NotificationEngagement` model removed but table still exists (migration 062)
2. Inconsistent `DateTime` usage — some use `DateTime(timezone=True)`, others plain `DateTime`
3. `KnowledgeConfig`, `SyncState` missing timestamps
4. `VerificationGroupMember` uses `added_at` instead of `created_at`
5. `QuoteLine` uses `backref` instead of `back_populates`

### Services
6. Hardcoded values scattered (180-day cutoff, 4000 char truncation, 15-min cache TTL, semaphore 5)
7. `_history_to_result` timezone handling mixes naive and aware datetimes
8. No `print()` usage found (good)
9. SQL injection risk is low — ORM parameterized queries used consistently

### Routers
10. Few endpoints use `response_model` for OpenAPI docs
11. `call_initiated` swallows non-HTTPException errors
12. `batch_assign` naming mismatch (`claimed_by_id` vs "assign owner")

### Frontend
13. Global variable pollution — many functions on `window` instead of a namespace
14. Logout inline CSRF handling inconsistent with `apiFetch` pattern
15. Missing ARIA attributes on some interactive elements
16. Vendor URLs should validate no `javascript:` scheme

### Connectors
17. `sources.py:154` — `_parse_retry_after` returns default 5.0+random when header missing
18. `email_mining.py:331-334` — `asyncio.get_event_loop()` usage in async context

### Deployment
19. CSP allows `unsafe-inline` for scripts
20. `/health` endpoint exposes internal details
21. `oemsecrets.py` logs `r.text[:200]` which could include API keys
22. Caddy health check uses admin API
23. `alembic/env.py` instantiates `Settings()` at import time

### Tests
24. `element14` test uses `type("FakeHTTP", ...)` instead of `MagicMock`
25. `test_file_utils.py` corrupt file test assertion could be stronger
26. `conftest.py` uses `nest_asyncio.apply()` which can mask loop issues

---

## Recommended Fix Order

### Phase 1 — Security (Do Now)
1. SEC-1: Restrict agent API key to agent-only routes
2. SEC-2, SEC-3, SEC-15: Add auth to unprotected endpoints
3. SEC-4: Fix XSS in Jinja template
4. SEC-5: Fix CSRF on `logCallInitiated`
5. SEC-6: Validate OneDrive path parameter
6. SEC-7, SEC-14: Sanitize sensitive data in logs
7. SEC-8: Require strong DB password in deploy
8. SEC-9 through SEC-12: Add `escape_like()` on all LIKE queries with user input

### Phase 2 — Data Integrity (This Sprint)
1. DATA-1: Use savepoints in inbox poll
2. DATA-2: Add locking for quote number generation
3. DATA-3, DATA-4: Add LIMIT to unbounded queries
4. MODEL-1: Fix partial index `postgresql_where`
5. MODEL-2: Resolve orphaned SelfHealLog import

### Phase 3 — Architecture (Next Sprint)
1. ARCH-1, ARCH-2, ARCH-3: Move business logic and DB queries from routers to services
2. VAL-1 through VAL-4: Replace `dict` bodies with Pydantic schemas
3. CONN-1 through CONN-4: Add proper error handling to connectors

### Phase 4 — Hardening (Ongoing)
1. Add missing `created_at`/`updated_at` columns via migrations
2. Add missing `ondelete` cascades to FKs
3. Standardize exception handling patterns
4. Add `response_model` to router endpoints
5. Clean up frontend console statements
6. Pin transitive dependencies

---

## What's Working Well

- **Migration discipline:** 76 clean Alembic migrations with proper upgrade/downgrade
- **Logging:** Consistent Loguru usage, no `print()` found in services
- **Test coverage:** 8,000+ tests with good mocking patterns
- **Scoring engine:** Pure logic, no I/O, well-structured
- **Async patterns:** Proper `asyncio.gather()` for parallel connector searches
- **Session management:** `get_db()` properly yields and closes sessions
- **Startup safety:** No DDL in `startup.py` — only triggers, seeds, and backfills
- **Vendor normalization:** Consistent `normalized_name` pattern for dedup
- **Cache layer:** Redis with proper TTL and graceful fallback
