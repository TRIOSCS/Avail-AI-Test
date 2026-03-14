# AvailAI Code Review — March 2026 (Full Independent Review)

## Executive Summary

AvailAI is a **well-structured, production-grade** electronic component sourcing platform built on a solid FastAPI + SQLAlchemy + PostgreSQL foundation. The codebase (~740 Python files, 76 Alembic migrations, 314 test files, 8,605+ test functions) demonstrates strong engineering discipline overall. Auth middleware, configuration management, connector patterns, and Alembic discipline are standout strengths.

This review was conducted file-by-file across all major layers: routers, services, connectors, models, startup, and configuration.

**Overall Grade: B+** — Solid foundation with a handful of issues that need attention before further scaling.

---

## P0 — Fix Immediately (Security / Data Loss)

### 1. `/auth/status` Exposes All Employees to Any Authenticated User
**File:** `app/routers/auth.py` ~line 278
**Severity:** CRITICAL

`GET /api/auth/status` fetches all users who have a refresh token and returns their names, emails, roles, and M365 error messages to any logged-in user. A buyer or sales rep can enumerate every employee's account, role, and M365 error details.

**Fix:** Add `require_admin` dependency to this endpoint.

---

### 2. Agent API Key Timing Attack
**File:** `app/dependencies.py` ~line 54
**Severity:** CRITICAL

```python
if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
```
Plain string `==` comparison is vulnerable to timing side-channel attacks. An attacker can measure response latency to brute-force the key.

**Fix:**
```python
import hmac
if agent_key and settings.agent_api_key and hmac.compare_digest(agent_key, settings.agent_api_key):
```

---

### 3. AI-Controlled `setattr` Without Whitelist (Prompt Injection Risk)
**File:** `app/services/enrichment_orchestrator.py` ~line 348
**Severity:** CRITICAL

```python
setattr(entity, field, value)  # field comes from Claude's JSON response
```
Claude's response determines which model attributes get written. A compromised or malicious enrichment source could inject `"id"`, `"created_at"`, or `"role"` into Claude's response and overwrite protected fields. `hasattr()` check is not sufficient.

**Fix:** Enforce a whitelist of enrichable fields per entity type before calling `setattr`.

---

### 4. `send_teams_dm` Will Always Return 400 (Non-Functional)
**File:** `app/services/teams_notifications.py` ~lines 81–93
**Severity:** CRITICAL (data integrity — Teams DMs silently never send)

Microsoft Graph's `POST /chats` for `oneOnOne` type requires **both** participants in `members`. The current code sends only the recipient. This call always fails with `400 Bad Request`, meaning Teams DMs have never worked.

**Fix:** Add the sender (service account) as the second member with `"roles": ["owner"]`.

---

### 5. `db.rollback()` in Healing Loop Loses All Previously Healed Records
**File:** `app/services/integrity_service.py` ~lines 165–167, 196–198, 226–228
**Severity:** CRITICAL (data loss risk)

```python
except Exception as e:
    logger.warning("INTEGRITY_HEAL_FAIL: ...", r.id, r.primary_mpn, e)
    db.rollback()  # destroys ALL work from this entire loop iteration
```
A rollback inside the loop reverts every previously healed record in the same transaction, not just the failing one. A single bad record causes all healing work in that batch to be discarded silently.

**Fix:** Use `db.begin_nested()` (savepoints) per record so only the failed record is rolled back.

---

### 6. `db.rollback()` / `db.commit()` Inside Service Layer (Transaction Ownership Violation)
**Files:**
- `app/services/email_intelligence_service.py` ~line 270 (`db.rollback()`)
- `app/services/enrichment_orchestrator.py` ~line 377 (`db.commit()`)
**Severity:** HIGH (silent data loss / broken atomicity)

Services must never own transactions. `db.rollback()` in `email_intelligence_service` silently discards successfully extracted `KnowledgeEntry` records. `db.commit()` in `enrichment_orchestrator` breaks the caller's transaction atomicity.

**Fix:** Replace `db.commit()` with `db.flush()`. Remove `db.rollback()` from services — use savepoints (`db.begin_nested()`) if partial-rollback is needed.

---

### 7. New Azure AD Users Created With No Role
**File:** `app/routers/auth.py` ~lines 145–153
**Severity:** HIGH

On first Azure AD login, new users are committed with no role assigned. The role check happens after the commit. Non-admin users are committed role-less, and their first subsequent request to any protected endpoint fails with 403. The user must be manually role-assigned in the DB before they can use the app.

**Fix:** Assign a default role (e.g., `role="buyer"`) at creation time, or prompt an admin to assign the role before commit.

---

### 8. `_do_search` Abstract Method Is Unreachable Dead Code (No ABC Enforcement)
**File:** `app/connectors/sources.py` ~lines 159–161
**Severity:** HIGH

The `@abstractmethod` definition for `_do_search` appears **after a `return` statement** inside `_parse_retry_after`, making it unreachable. `BaseConnector` effectively has no abstract contract for `_do_search`. Subclasses that omit this method receive an `AttributeError` at runtime, not a `TypeError` at class definition time.

**Fix:** Move `_do_search` to the correct indentation level as a top-level method on `BaseConnector`.

---

## P1 — Fix Soon (High-Impact Bugs and Security)

### 9. No Rate Limiting on Password Login Endpoint
**File:** `app/routers/auth.py` ~line 216
**Severity:** HIGH

The password login endpoint has no brute-force protection. `ENABLE_PASSWORD_LOGIN` defaults to off, but when enabled there is no `@limiter.limit()` decorator or account lockout.

**Fix:** Add `@limiter.limit("5/minute")` to the endpoint.

---

### 10. SQL Injection Pattern in `vendor_analytics.py`
**File:** `app/routers/vendor_analytics.py` ~lines 184–218
**Severity:** HIGH

```python
mpn_filter = "AND mc.normalized_mpn LIKE :mpn_pattern"
query = sqltext(f"... {mpn_filter} ...")
```
`{mpn_filter}` is an f-string interpolated into a raw SQL string. Currently safe (it's a hardcoded string), but this trains future developers to interpolate into raw SQL. One careless future change could introduce a real injection vector.

**Fix:** Use SQLAlchemy `and_()` for conditional clauses instead of string interpolation.

---

### 11. Unvalidated `int()` on Query Parameters → 500 Instead of 422
**File:** `app/routers/vendor_analytics.py` ~lines 43–44; `app/routers/materials.py` ~line 204
**Severity:** HIGH

```python
limit = min(int(request.query_params.get("limit", "200")), 1000)
```
Non-numeric input raises `ValueError` which FastAPI surfaces as an unstructured 500. Users and clients get no useful error message.

**Fix:** Use Pydantic `Query()` params with type constraints:
```python
async def list_materials(limit: int = Query(200, ge=1, le=1000), ...):
```

---

### 12. N+1 Query on `o.entered_by.name` in Vendor Analytics
**File:** `app/routers/vendor_analytics.py` ~line 126
**Severity:** HIGH

`o.entered_by.name` inside a list comprehension triggers a lazy-load per offer. With 50 offers, this is 51 queries.

**Fix:** Add `.options(joinedload(Offer.entered_by))` to the query before the list comprehension.

---

### 13. Silent FK Reassignment Failures in Vendor Merge
**File:** `app/services/vendor_merge_service.py` ~lines 86–94
**Severity:** HIGH

FK reassignment failures are swallowed by `except Exception` and logged at **DEBUG** level (invisible in production). If any table fails, records become orphaned with no rollback and the merge reports success.

**Fix:** Log at WARNING/ERROR. Wrap the entire merge in a single transaction with `db.begin_nested()`.

---

### 14. `vendor_name` String Fields Not Updated During Merge
**File:** `app/services/vendor_merge_service.py` ~lines 85–94
**Severity:** HIGH

The FK reassignment loop updates FK columns but `sightings.vendor_name` and `material_vendor_history.vendor_name` store vendor names as **strings** — not FKs. These are silently skipped during merge. All historical sightings for the merged vendor continue to reference the old name, making analytics post-merge incomplete.

**Fix:** Add explicit UPDATE for `vendor_name` string fields after the FK loop.

---

### 15. Mouser Rate Limits Bypass Circuit Breaker
**File:** `app/connectors/mouser.py` ~lines 49–59
**Severity:** HIGH

When Mouser returns 403 or 429, the connector returns `[]` before calling `raise_for_status()`, bypassing the base class circuit breaker entirely. Consecutive rate-limit responses never open the breaker, so every search attempt hits the API and burns quota.

**Fix:** Call `self._breaker.record_failure()` before returning `[]` on rate-limit responses.

---

### 16. Retry-After Header Has No Upper Bound
**File:** `app/connectors/sources.py` ~line 153
**Severity:** HIGH

```python
return max(float(header), 1.0)
```
A buggy or malicious API returning `Retry-After: 999999` would block the connector for 11+ days.

**Fix:**
```python
return min(max(float(header), 1.0), 300.0)
```

---

### 17. GET Endpoints Writing to the Database
**Files:**
- `app/routers/materials.py` ~lines 302–308, 343–349 (infer manufacturer on GET)
- `app/routers/sources.py` ~lines 388–393 (update `ApiSource.status` on GET list)
**Severity:** HIGH

GET endpoints are expected to be idempotent. Writing to the DB on every GET breaks HTTP semantics, causes unnecessary DB load from frontend polling (sources endpoint is polled every 60s), and is surprising to any client-side caching layer.

**Fix:** Move mutation logic to background tasks or to write endpoints (PUT/PATCH).

---

### 18. Password Hashing Uses SHA-256 in Startup Seed
**File:** `app/startup.py` ~lines 63–80
**Severity:** MEDIUM-HIGH

Default user seeding uses `hashlib.sha256` for password hashing. SHA-256 is not a password hash function — it is orders of magnitude too fast and vulnerable to brute force. (Note: `auth.py`'s live login path correctly uses PBKDF2-HMAC-SHA256 with 200K iterations — this is only the seed path.)

**Fix:** Use `bcrypt` or `argon2` for the seed path, or use the same `hashlib.pbkdf2_hmac` function from `auth.py`.

---

### 19. Race Condition on Concurrent Token Refresh in DigiKey Connector
**File:** `app/connectors/digikey.py` ~lines 34–56
**Severity:** HIGH

Two concurrent part searches can both see `self._token = None` simultaneously and both fire a DigiKey token refresh. Under parallel search load (`asyncio.gather()`), this results in redundant OAuth calls.

**Fix:** Guard `_get_token` with `asyncio.Lock()`:
```python
self._token_lock = asyncio.Lock()
async def _get_token(self):
    async with self._token_lock:
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        ...
```

---

### 20. No Authorization on Material Card Enrich Endpoint
**File:** `app/routers/materials.py` ~lines 388–392
**Severity:** HIGH

`POST /api/materials/{card_id}/enrich` uses only `require_user`. Any authenticated user (buyers, traders, sales) can overwrite manufacturer, description, lifecycle status, and other enrichment fields on any material card.

**Fix:** Restrict to `require_buyer` or add an explicit role/permission check.

---

### 21. XSS via `javascript:` URLs in HTML Sanitizer
**File:** `app/static/app.js` ~line 458
**Severity:** MEDIUM-HIGH

`sanitizeRichHtml` whitelists `href` on `<a>` tags but does not validate the URL scheme. `<a href="javascript:alert(1)">` bypasses CSP in some browsers.

**Fix:** Validate that `href` starts with `http://`, `https://`, or `/` before allowing the attribute.

---

### 22. API Keys in URL Query Parameters (Log Exposure)
**Files:** `app/connectors/mouser.py` ~line 42; `app/connectors/oemsecrets.py` ~lines 32–36
**Severity:** HIGH

Both connectors send API keys as URL query parameters (`?apiKey=...`). Keys in URLs appear in access logs, proxy logs, Sentry events, and browser history.

**Fix:** Move keys to `Authorization` or `X-Api-Key` headers if supported by the respective APIs. Document as known-risk if not supported.

---

## P2 — Medium Priority

### 23. Missing Index on `material_cards.deleted_at`
**File:** `app/models/intelligence.py` ~line 49
**Severity:** HIGH-MEDIUM

Soft-delete queries (`WHERE deleted_at IS NULL`) execute on every MaterialCard lookup without an index, causing full table scans as the table grows.

**Fix:** Add Alembic migration:
```python
Index("ix_material_cards_deleted_at", "deleted_at")
```

---

### 24. `get_company` Cache Is Ineffective
**File:** `app/routers/crm/companies.py` ~lines 293–306
**Severity:** MEDIUM

The most expensive part of `get_company` — a query with 3 joined relationships (`selectinload` + `joinedload`) — runs unconditionally on every request, **before** the cache check inside `_fetch`. The cache only saves a subsequent counting query.

**Fix:** Move the eager-load query inside the cached closure so the cache hit skips it entirely.

---

### 25. `check_company_duplicate` Loads Up to 2,000 Companies Into Memory
**File:** `app/routers/crm/companies.py` ~lines 262–282
**Severity:** MEDIUM

2,000 company names are loaded into Python for string comparison. The same pattern is repeated in `create_company`. With a trigram index (`pg_trgm`) on `companies.name`, this entire operation becomes a single DB query.

**Fix:** Use `CREATE EXTENSION pg_trgm` + GIN index + `SIMILARITY()` function for server-side fuzzy matching.

---

### 26. Duplicate Unique Index on `companies.sf_account_id`
**File:** `app/models/crm.py` ~lines 61, 86
**Severity:** MEDIUM

`sf_account_id = Column(String(255), unique=True)` already creates a unique index. The explicit `Index("ix_companies_sf_account_id", "sf_account_id", unique=True)` creates a **second** identical unique index, wasting storage and slowing every write.

**Fix:** Remove the explicit `Index()` declaration.

---

### 27. OData Injection in Graph API Filter String
**File:** `app/services/email_intelligence_service.py` ~line 426
**Severity:** MEDIUM

```python
"$filter": f"conversationId eq '{conversation_id}'"
```
`conversation_id` from Graph API responses is interpolated without sanitization. A crafted `conversation_id` containing a single quote could break the OData filter.

**Fix:** Validate `conversation_id` matches `[A-Za-z0-9_=-]+` before interpolation.

---

### 28. N+1 COUNT Queries in Email Intelligence Dedup Loop
**File:** `app/services/email_intelligence_service.py` ~lines 624–633
**Severity:** MEDIUM

`dedup_q.count()` is called inside a `for fact in result["facts"]` loop. For a vendor email with 10 facts, that's 10 separate `COUNT(*)` round-trips.

**Fix:** Batch all fact keys into a single `SELECT fact_key, COUNT(*) ... WHERE fact_key IN (...)` query before the loop.

---

### 29. CSRF Exempt Scope Too Broad — `/v2/.*` Fully Exempt
**File:** `app/main.py` ~lines 289–296
**Severity:** MEDIUM

All HTMX endpoints under `/v2/` bypass CSRF middleware. HTMX POST/PUT/DELETE requests that mutate state should still be CSRF-protected. HTMX's `HX-Request: true` header could serve as a CSRF signal, but it is not validated anywhere.

**Fix:** Narrow the exemption to GET requests only, or add `HX-Request` header validation middleware for HTMX mutation endpoints.

---

### 30. `unsafe-inline` in Content Security Policy
**File:** `app/main.py` ~line 325
**Severity:** MEDIUM

`script-src 'unsafe-inline'` completely disables XSS protection for inline scripts in modern browsers. Acknowledged in code comments but unresolved.

**Fix:** Migrate inline `onclick=` handlers to `addEventListener`, then use a nonce-based CSP.

---

### 31. Hardcoded Personal Email in Source Code
**File:** `app/startup.py` ~lines 113–128
**Severity:** MEDIUM

`vinod@trioscs.com` is seeded as an admin user on every deployment. A person's admin elevation should not be hardcoded — it should be in `ADMIN_EMAILS` env var or a seed config file.

**Fix:** Move this seed to an env-var-driven config key.

---

### 32. `_analyze_hot_tables` Uses String Concatenation for Table Name
**File:** `app/startup.py` ~line 539
**Severity:** MEDIUM

```python
"ANALYZE " + tbl
```
`tbl` is from a hardcoded list now, but this pattern violates the parameterized style used everywhere else and is a maintenance hazard.

**Fix:** Use a whitelist check + direct string formatting from the whitelist, and add a comment documenting why parameterized is not applicable for `ANALYZE`.

---

### 33. Backfill Loops Issue N+1 Updates
**File:** `app/startup.py` ~lines 286–372
**Severity:** MEDIUM

`_backfill_normalized_mpn()` and `_backfill_sighting_offer_normalized_mpn()` fetch all rows then execute one UPDATE per row. For 10,000 rows this is 10,001 queries.

**Fix:** Use a batch `UPDATE ... FROM (VALUES ...) AS v(id, key) WHERE requirements.id = v.id` query.

---

### 34. DB Session Captured in Cached Closure
**File:** `app/routers/vendor_analytics.py` ~lines 155–169
**Severity:** MEDIUM

A `@cached_endpoint` decorator wraps a closure `_fetch_parts` that captures a live `db` session. On a cache hit, the stale session reference is returned. DB sessions must not be stored in caches.

**Fix:** Move the cache key derivation out of the closure and cache only the serializable result dict, not the function that holds a session.

---

### 35. Graph API Calls in Router (Architectural Violation)
**File:** `app/routers/rfq.py` ~lines 771–793
**Severity:** MEDIUM

`send_follow_up` and `send_follow_up_batch` construct Graph API payloads and call `GraphClient` directly inside the router. This is business logic that belongs in `email_service.py`.

**Fix:** Create a `send_follow_up_email(token, contact, body)` function in `email_service.py` and call it from the router.

---

### 36. Unvalidated Raw JSON Body in Batch Follow-Up Endpoint
**File:** `app/routers/rfq.py` ~lines 806–808
**Severity:** MEDIUM

```python
raw = await request.json()
contact_ids = raw.get("contact_ids", [])
```
No Pydantic schema validates this body. `contact_ids` could be any type. Replace with a proper `class BatchFollowUpRequest(BaseModel): contact_ids: list[int]`.

---

### 37. Tag Filtering Uses Fragile JSON-Cast LIKE
**File:** `app/routers/crm/companies.py` ~lines 73–77
**Severity:** MEDIUM

```python
sqlfunc.lower(sqlfunc.cast(Company.brand_tags, String)).contains(safe_tag)
```
Casting a JSONB/array column to a string and using LIKE is fragile — `"ic"` matches `"price"`, `"service"`, `"device"`. Use PostgreSQL's `@>` operator or `ANY()` for array containment.

---

### 38. No Authorization Check on `update_company`
**File:** `app/routers/crm/companies.py` ~lines 538–553
**Severity:** MEDIUM

Any authenticated user can update any field on any company via `PUT /api/companies/{id}`. No role check, no ownership check, no field allowlist.

**Fix:** Require at least an AM or admin role. Add a field allowlist for non-admin users.

---

### 39. `tax_id` Returned to All Authenticated Users
**File:** `app/routers/crm/companies.py` ~line 167
**Severity:** MEDIUM

EIN/VAT numbers (sensitive financial PII) are included in the company list payload returned to every authenticated user. Should be restricted to admin/AM roles.

---

### 40. Disclaimer Regex Patterns Are Dead Code
**File:** `app/services/response_parser.py` ~lines 241–250
**Severity:** MEDIUM

`_clean_email_body` first collapses all newlines into spaces, then tries to match `\n\n` in the result — which never exists after collapsing. Disclaimer stripping never fires.

**Fix:** Apply disclaimer stripping **before** whitespace collapsing.

---

### 41. AI Confidence Not Clamped in `response_parser.py`
**File:** `app/services/response_parser.py` ~line 157
**Severity:** MEDIUM

`confidence = result.get("confidence", 0)` is used directly in threshold comparisons. If the AI returns `1.5` or a string like `"high"`, the comparison behaves unexpectedly. `email_intelligence_service.py` clamps this correctly but `response_parser.py` does not.

**Fix:**
```python
confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
```

---

### 42. Dead Code: Contact Enrichment Pipeline
**File:** `app/services/enrichment_orchestrator.py` ~lines 441–460
**Severity:** MEDIUM

`_load_entity` returns `None` for `entity_type == "contact"` and `_get_identifier` also returns `None` for contacts. `enrich_on_demand` for contacts always returns `{"error": "not found"}`. The contact enrichment pipeline is non-functional.

**Fix:** Either implement contact loading or remove the dead code paths.

---

### 43. `synchronize_session="fetch"` Loads All Rows Into Memory
**File:** `app/services/vendor_merge_service.py` ~line 86
**Severity:** MEDIUM

For vendors with thousands of linked records (ActivityLog, Sightings), `synchronize_session="fetch"` pulls all rows into Python memory before the UPDATE. Use `synchronize_session=False` since the session objects aren't needed after the merge.

---

### 44. `should_flag_review` Ignores Email Classification Type
**File:** `app/services/response_parser.py` ~lines 254–263
**Severity:** MEDIUM

`should_flag_review` flags for human review based purely on confidence threshold, regardless of classification. An `ooo_bounce` with 0.65 confidence would be queued for review, wasting time. The check should skip obvious non-actionable classifications.

---

### 45. `Offer.updated_at` Has No `onupdate` Handler
**File:** `app/models/offers.py` ~line 74
**Severity:** MEDIUM

`updated_at = Column(DateTime)` — no default, no `onupdate`. Every edit to an Offer leaves `updated_at = NULL`, making audit trails incomplete for all offers.

**Fix:** `updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))`

---

### 46. Enums Defined But Not Enforced at the Database Level
**File:** `app/models/buy_plan.py` ~lines 114–115, 226, 239
**Severity:** MEDIUM

Five Python `Enum` classes are used only for their `.value` as string defaults. The underlying columns are plain `String`, so PostgreSQL accepts any string. An invalid status like `"ACTIVE"` stores silently.

**Fix:** Use `Column(Enum(BuyPlanStatus))` to enforce valid values at the DB level.

---

### 47. `ProactiveMatch` and `ProactiveOffer` Missing `updated_at`
**File:** `app/models/intelligence.py` ~lines 125–190
**Severity:** MEDIUM

Both models have status fields that change over time (`new` → `sent` → `dismissed` → `converted`) but no `updated_at` column. Conversion timing analytics are impossible.

---

### 48. GET `/api/sources` Issues ~60 DB Queries Per Request
**File:** `app/routers/sources.py` ~lines 385–404
**Severity:** MEDIUM

For each source × each env var: `credential_is_set` is called in the status loop, called again in the serialization loop, and `get_credential` is called for each set credential. With 10+ sources × 2 env vars = ~60 queries per request, and the frontend polls this every 60 seconds.

**Fix:** Load all credentials in a single query before the loop.

---

### 49. `clear_dangling_fks` Loads All Dangling Records Without Limit
**File:** `app/services/integrity_service.py` ~lines 253–264
**Severity:** MEDIUM

Unlike `heal_orphaned_records` which uses `.limit(batch_size)`, `clear_dangling_fks` loads ALL dangling records. In a failure scenario with thousands of dangling FKs, this could OOM the app container.

**Fix:** Add `.limit(batch_size)` with a loop.

---

## P3 — Low Priority / Cleanup

### 50. `getattr(user, "is_active", True)` Default Is Insecure
**File:** `app/dependencies.py` ~line 58
**Severity:** LOW

Defaults to `True` if the attribute is missing. A model change that renames this field silently passes every user through the active check. Default should be `False` (deny-by-default).

---

### 51. `TESTING` Environment Variable Check Inconsistency
**File:** `app/startup.py` ~lines 33, 53
**Severity:** LOW

Line 33 uses `os.environ.get("TESTING")` (truthy for any non-empty value). Line 53 checks `os.environ.get("TESTING") == "1"`. `TESTING=true` passes the first gate but skips the second, causing default user creation to run unexpectedly.

---

### 52. `ai_features_enabled` Is an Unconstrained String
**File:** `app/config.py` ~line 122
**Severity:** LOW

Valid values are `"mike_only"`, `"all"`, `"off"`. An invalid value silently behaves like `"off"`.

**Fix:** Use `Literal["all", "mike_only", "off"]` as the type.

---

### 53. `misfire_grace_time=300` Can Mask Systemic Scheduler Problems
**File:** `app/scheduler.py` ~lines 40–43
**Severity:** LOW

A 5-minute grace window means a job that missed by up to 5 minutes still fires. On a heavily loaded server this can cause burst execution.

**Fix:** Reduce to 60–120s. Log a WARNING when a job fires in grace window.

---

### 54. Duplicate Normalization Logic (DRY Violation)
**File:** `app/routers/crm/companies.py` ~lines 243–255, 419–430
**Severity:** LOW

`_suffixes` regex and `_norm`/`_normalize` function defined twice in the same file. Extract to a module-level helper.

---

### 55. `BackgroundTask` Not Used for Long-Running AI Analysis
**File:** `app/routers/vendor_analytics.py` ~lines 263–273
**Severity:** LOW

`_analyze_vendor_materials()` can take 30+ seconds. It is awaited synchronously in the HTTP handler, which will hit proxy/load-balancer timeouts.

**Fix:** Return `202 Accepted` and dispatch via `BackgroundTask`.

---

### 56. 101 Relationships Missing `back_populates`
**Severity:** LOW

Many SQLAlchemy relationships are one-directional. Worst offenders: `offers.py` (4 User FKs), `buy_plan.py` (6 User FKs), `strategic.py` (using deprecated `backref=`).

**Fix:** Replace `backref=` with explicit `back_populates` on both sides.

---

### 57. `BuyPlanLine.requirement_id` and `offer_id` Use `SET NULL` on Delete
**File:** `app/models/buy_plan.py` ~lines 209–210
**Severity:** LOW

When a Requirement or Offer is deleted, the BuyPlanLine becomes a ghost — no MPN, no price, no quantity context. Should be `CASCADE` or handled by application-level cleanup with a status transition.

---

### 58. Backward-Compat Re-Exports Growing in Scheduler
**File:** `app/scheduler.py` ~lines 49–53
**Severity:** LOW

Private utility functions (`_utc`, `get_valid_token`, `refresh_user_token`) are re-exported from the scheduler for backward compatibility. Files importing from `app.scheduler` should be migrated to import from `app.utils.token_manager` directly.

---

### 59. `REQ-{id:03d}` Format Breaks Consistency at 1,000+ Requisitions
**File:** `app/services/requisition_service.py` ~line 156
**Severity:** LOW

At 1,000 requisitions/month, IDs will exceed 999 quickly, producing `REQ-001` vs `REQ-1500` inconsistency. Use `REQ-{id:06d}` for 6-digit padding.

---

### 60. Missing `server_default` on Boolean Columns
**File:** `app/models/crm.py` ~lines 160–161
**Severity:** LOW

`phone_verified` and `email_verified` have Python `default=False` but no `server_default`. Rows inserted via raw SQL get `NULL` instead of `False`.

---

### 61. No N+1 Query Tests on List Endpoints
**Severity:** MEDIUM

Zero tests verify eager-loading behavior. List endpoints for Offers, Requisitions, and ActivityLog likely have N+1 patterns under load.

**Fix:** Add pytest hook using `sqlalchemy.event` to assert max query count per endpoint.

---

### 62. No Alembic Downgrade Tests
**Severity:** MEDIUM

76 migrations have `downgrade()` functions but none are tested. Production rollbacks could fail silently.

---

### 63. No CI/CD Pipeline
**Severity:** MEDIUM

No `.github/workflows/` found. Add GitHub Actions for pytest, ruff, and Alembic drift check.

---

## Architecture Strengths

1. **Clean layer separation** — Thin routers → services → models. No business logic in route handlers (with the rfq.py exceptions noted above).
2. **Alembic discipline** — 76 migrations, strict rules enforced in CLAUDE.md. No raw DDL in startup.
3. **Auth middleware** — Well-designed dependency chain: `get_user` → `require_user` → `require_buyer/admin/sales`.
4. **UTC everywhere** — `UTCDateTime` type decorator + event listener ensures timezone consistency.
5. **Database tuning** — Connection pool (20+40 overflow), statement timeout (30s), lock timeout (5s), `pool_pre_ping`.
6. **Security headers** — CSRF, CSP, GZip, Sentry scrubbing of sensitive fields.
7. **Rate limiting** — slowapi with configurable limits (120/min default, 20/min for search).
8. **Comprehensive config** — 80+ settings with validators, fail-fast on bad values.
9. **Test suite** — 314 test files, 8,605+ test functions using in-memory SQLite with auth overrides.
10. **Circuit breaker pattern** — Per-connector breakers prevent cascading failures.
11. **Per-connector concurrency limits** — Semaphores prevent API hammering.
12. **OAuth state validation** — Proper CSRF protection for OAuth flow.
13. **Password login** — PBKDF2-HMAC-SHA256 with 200K iterations in `auth.py` live login path.
14. **Email mining dedup** — ProcessedMessage with savepoint protection prevents duplicate processing.
15. **Delta query caching** — Incremental inbox sync avoids full scans.
16. **No `eval()`, `Function()`, or open redirects** — Frontend code is clean.
17. **Loguru throughout** — No stray `print()` calls found.
18. **Sentry `_before_send` hook** — Scrubs sensitive headers, query strings, and frame variables.
19. **Connector parallel search** — `asyncio.gather()` with `return_exceptions=True` is correct.
20. **Evidence tiers** — Trust-tier system on Offer model is a sophisticated provenance design.

---

## Recommended Fix Priority Order

### Quick Wins (< 30 minutes each)
| # | Fix | File |
|---|-----|------|
| 1 | `hmac.compare_digest()` for agent API key | `dependencies.py` |
| 2 | Add `require_admin` to `/auth/status` | `auth.py` |
| 3 | Rate limit password login endpoint | `auth.py` |
| 4 | Cap `Retry-After` at 300s | `connectors/sources.py` |
| 5 | Fix `_do_search` indentation (ABC enforcement) | `connectors/sources.py` |
| 6 | Clamp AI confidence in `response_parser.py` | `services/response_parser.py` |
| 7 | Fix redundant unique index on `sf_account_id` | Migration |
| 8 | Add `updated_at` onupdate to `Offer` model | `models/offers.py` |
| 9 | Fix `TESTING` env check inconsistency | `startup.py` |
| 10 | Validate `href` schemes in JS sanitizer | `static/app.js` |

### High Priority (1–2 hours each)
| # | Fix | File |
|---|-----|------|
| 11 | Fix Teams DM (add self-member to chat creation) | `services/teams_notifications.py` |
| 12 | Fix `rollback()` in integrity healing loop → use savepoints | `services/integrity_service.py` |
| 13 | Fix `db.rollback()` in email intelligence service | `services/email_intelligence_service.py` |
| 14 | Fix `db.commit()` in enrichment orchestrator | `services/enrichment_orchestrator.py` |
| 15 | Add `asyncio.Lock` to DigiKey token refresh | `connectors/digikey.py` |
| 16 | Assign default role to new Azure AD users | `routers/auth.py` |
| 17 | Add `require_buyer` to material enrich endpoint | `routers/materials.py` |
| 18 | Whitelist fields in `setattr` from AI response | `services/enrichment_orchestrator.py` |
| 19 | Wrap vendor merge in single transaction | `services/vendor_merge_service.py` |
| 20 | Update `vendor_name` string fields during merge | `services/vendor_merge_service.py` |
| 21 | Move Graph API calls out of rfq.py router | `routers/rfq.py` |
| 22 | Fix GET endpoints writing to DB | `routers/materials.py`, `routers/sources.py` |
| 23 | Add index on `material_cards.deleted_at` | Migration |
| 24 | Fix SQL f-string in `vendor_analytics.py` | `routers/vendor_analytics.py` |
| 25 | Use `Query()` params instead of raw `int()` | `routers/vendor_analytics.py`, `routers/materials.py` |

### Medium Priority (2–4 hours each)
| # | Fix | File |
|---|-----|------|
| 26 | Add GitHub Actions CI (pytest + ruff) | `.github/workflows/` |
| 27 | Fix cache pattern in `get_company` | `routers/crm/companies.py` |
| 28 | Batch credential queries in `list_api_sources` | `routers/sources.py` |
| 29 | Add `updated_at` to `ProactiveMatch`/`ProactiveOffer` | Migration |
| 30 | Enforce enum types at DB level in `buy_plan.py` | Migration |
| 31 | Fix disclaimer regex dead code | `services/response_parser.py` |
| 32 | Batch N+1 in email intelligence dedup loop | `services/email_intelligence_service.py` |
| 33 | Add `server_default` to boolean columns in crm.py | Migration |
| 34 | Batch backfill UPDATEs in `startup.py` | `startup.py` |
| 35 | Add `back_populates` to replace `backref=` | Models (multiple) |
| 36 | Add pg_trgm-based company duplicate detection | `routers/crm/companies.py` |
| 37 | Restrict `tax_id` to admin/AM roles | `routers/crm/companies.py` |

---

## Session Close Checklist

**CHANGELOG entry:** Full independent code review conducted — updated CODE_REVIEW.md with 63 findings spanning 25+ files.

**Migration flag:** No migrations added in this session (migrations are required for issues: 23, 26, 29, 30, 33).

**STABLE.md flag:** No stable files changed.

**Test files touched:** None (review only).

**Tech debt noted:**
- Teams DM feature is currently non-functional (P0)
- Contact enrichment pipeline is dead code (medium)
- search_worker_base directory referenced in docs but not yet created
- No CI/CD pipeline exists
- No Alembic downgrade tests
