# AVAIL AI — Full Code Review
**Date:** March 14, 2026  
**Reviewer:** Claude (AI Code Review Agent)  
**Scope:** Full codebase — models, routers, services, connectors, auth, tests, deployment  
**Files reviewed:** ~740 Python files, 40 model files, 76 Alembic migrations, all test files, Dockerfile, CI/CD workflows

---

## Overall Grade: B+

AVAIL AI is a well-structured, production-grade electronic component sourcing platform. The architecture is sound, Alembic discipline is strong, and the test suite is broad. The issues below are ordered by severity — fix Criticals first, then High, then work through the rest systematically.

---

## CRITICAL — Fix Immediately

---

### C1. No Rate Limiting on `POST /auth/login` (Brute-Force)
**File:** `app/routers/auth.py:215–244`

The password login endpoint has zero rate limiting. An attacker can send unlimited attempts. The OAuth callback at line 93 has `@limiter.limit("10/minute")`, but the password path has nothing.

```python
# Fix: add this decorator
@limiter.limit("5/minute")
@router.post("/auth/login")
async def password_login(request: Request, ...):
```

Also add rate limits to `GET /auth/login` and `GET /auth/login-form`.

---

### C2. CSRF Exemption Covers `POST /auth/logout` (Force-Logout Attack)
**File:** `app/main.py:285–296`

The CSRF middleware exempts the entire `/auth/.*` namespace, including `POST /auth/logout`. Any malicious website can silently log out an authenticated user by embedding an image that fires the POST.

```python
# Current — too broad:
exempt_urls=[re.compile(r"/auth/.*"), ...]

# Fix — exempt only specific safe paths:
exempt_urls=[
    re.compile(r"/auth/callback"),
    re.compile(r"/auth/login$"),
    re.compile(r"/auth/login-form$"),
    ...
]
```

---

### C3. SSE Stream Has No Authentication
**File:** `app/routers/requisitions2.py:97–125`

The SSE endpoint has zero authentication. Any unauthenticated user on the internet can connect and receive live table-refresh events.

```python
# Current — no auth:
@router.get("/stream")
async def requisitions_stream(request: Request):

# Fix: add require_user dependency
@router.get("/stream")
async def requisitions_stream(request: Request, user: User = Depends(require_user)):
```

---

### C4. Quote Number Race Condition — Duplicate Quote Numbers Possible
**File:** `app/services/crm_service.py:14–26`

`next_quote_number()` reads the last quote number, increments it, and returns it — without any locking. Two concurrent requests will both read the same "last" quote and both compute the same next number. If `quote_number` has a unique constraint, the second insert raises `IntegrityError`. If it doesn't, customers receive duplicate quote numbers (compliance risk).

```python
# Current — TOCTOU race:
last = db.query(Quote).order_by(Quote.id.desc()).first()
seq = int(last.quote_number.split("-")[-1]) + 1

# Fix: use SELECT FOR UPDATE or a PostgreSQL sequence via Alembic migration
```

---

### C5. `bulk_archive` Has No Role Guard — Any User Can Archive Others' Requisitions
**File:** `app/routers/requisitions/core.py:542–554`

A single authenticated API call can archive all active requisitions not created by the caller. No manager/admin check, no confirmation required.

```python
# Fix: require manager role
@router.put("/bulk-archive")
async def bulk_archive(user: User = Depends(require_manager), db: Session = Depends(get_db)):
```

---

### C6. Email Thread Summary Cached with `db.flush()` but Never Committed
**File:** `app/services/email_intelligence_service.py:476`

`summarize_thread()` writes a thread summary back to the `EmailIntelligence` record, calls `db.flush()`, and returns. The flush pushes the change to the session but never commits. On the next call, the cache lookup misses and Claude is called again — wasting AI tokens on every request for the same thread.

```python
# Line 476 — add commit:
intel.thread_summary = result
db.flush()
db.commit()  # ← missing
```

---

## HIGH — Fix This Sprint

---

### H1. Agent API Key Compared With `==` Instead of `hmac.compare_digest`
**File:** `app/dependencies.py:51–55`

Plain `==` is vulnerable to timing attacks. An attacker who can measure response times can determine the correct key one character at a time.

```python
# Fix:
import hmac
if agent_key and settings.agent_api_key and hmac.compare_digest(agent_key, settings.agent_api_key):
```

---

### H2. Quote Mutations (6 Endpoints) Have No Ownership Check
**File:** `app/routers/crm/quotes.py:265–522`

`update_quote`, `delete_quote`, `send_quote`, `quote_result`, `revise_quote`, and `reopen_quote` all check only `require_user`. Any authenticated user can edit, delete, or send any other user's quotes.

```python
# Add ownership check to each mutation endpoint:
if quote.created_by_id != user.id and user.role not in ("manager", "admin"):
    raise HTTPException(403, "Not your quote")
```

---

### H3. Vendor Update / Blacklist — Any Authenticated User Can Do It
**Files:** `app/routers/vendors_crud.py:385, 409`

`update_vendor()` and `toggle_blacklist()` require only `require_user`. Any role (including `sales`) can blacklist a vendor or update its contact details. `toggle_blacklist` should require at minimum `require_manager`.

---

### H4. `/auth/status` Exposes All Users' M365 Metadata to Any Authenticated User
**File:** `app/routers/auth.py:271–311`

Returns M365 connection status, last inbox scan time, M365 error reasons, and last contacts sync for every connected user — visible to any buyer or sales rep.

```python
# Fix: restrict the users array to admins only
if user.role in ("admin", "manager"):
    users_status = [...]  # full list
else:
    users_status = [current_user_status]  # own status only
```

---

### H5. `require_fresh_token` Skips Expiry Check When `token_expires_at` Is None
**File:** `app/dependencies.py:144–166`

If `token_expires_at` is `None` (missing from DB), the function skips the expiry check entirely and returns the potentially expired token.

```python
# Fix: treat None as expired
needs_refresh = True  # default to refresh
if user.token_expires_at:
    if datetime.now(timezone.utc) < expiry - timedelta(minutes=15):
        needs_refresh = False
```

---

### H6. API Keys in URL Query Parameters (Mouser, OEMSecrets, element14, Nexar REST)
**Files:** `app/connectors/mouser.py:42`, `app/connectors/oemsecrets.py:32`, `app/connectors/element14.py:50`, `app/connectors/sources.py:279`

API keys appear in query strings, which end up in web server access logs, Sentry error events, and proxy headers. Move them to `X-Api-Key` or `Authorization` headers wherever the vendor API supports it.

---

### H7. eBay and Nexar Connector Tokens Cached Forever — No Expiry Tracking
**Files:** `app/connectors/ebay.py:22–42`, `app/connectors/sources.py:217–235`

Both connectors cache their access token in a plain instance variable with no expiry. DigiKey correctly tracks `self._token_expires_at`. Copy that pattern to eBay and Nexar so tokens refresh proactively instead of silently failing on 401.

---

### H8. `POST /auth/logout` Does Not Revoke M365 Tokens
**File:** `app/routers/auth.py:185–188`

Logout only clears the session cookie. The user's M365 `refresh_token` stays in the database and keeps powering background jobs (inbox scan, email mining) after logout. Add `user.refresh_token = None; user.m365_connected = False; db.commit()` on logout.

---

### H9. `/health` Endpoint Exposes System Internals — No Authentication
**File:** `app/main.py:459–506`

Returns DB health, Redis status, scheduler status, connector count, backup freshness, and exact app version to unauthenticated callers. At minimum remove `backup`, `connectors_enabled`, and precise version from the public response. Gate the detailed view behind admin auth.

---

### H10. `deploy.sh` Writes `SECRET_KEY` but Entrypoint Validates `SESSION_SECRET`
**Files:** `scripts/deploy.sh:157`, `docker-entrypoint.sh:4`

A fresh deploy produces a `.env` with `SECRET_KEY` set but no `SESSION_SECRET`. The entrypoint warns but continues, starting the app with no session secret. Add `SESSION_SECRET=${SECRET_KEY}` to the deploy script.

---

### H11. `update.sh` Has No Pre-Deploy DB Backup and No Rollback
**File:** `scripts/update.sh`

The manual update script (`git pull && docker compose up -d --build`) takes no backup and has no rollback path. The automated GitHub Actions deploy does take a backup — the manual path should too.

```bash
# Add before docker compose up:
docker compose exec -T db pg_dump -U availai availai > /root/backups/pre-update-$(date +%Y%m%d_%H%M).sql
```

---

### H12. Automated Deploy Health Check Will Always Fail
**File:** `.github/workflows/deploy.yml:56–57`

The health check uses `curl http://localhost:8000/health` from the DigitalOcean host, but port 8000 is only exposed on the internal Docker bridge — not on the host. Every GitHub Actions deploy will time out and trigger the rollback even when the deploy succeeded.

```bash
# Fix: check via Caddy or Docker health status:
docker compose ps app --format '{{.Status}}' | grep -q healthy
```

---

### H13. Hardcoded Secret Key in E2E Test Fixtures
**File:** `tests/e2e/conftest.py:88`

A full 64-hex-char secret key is committed to the repo as a fallback. If accidentally used as the production `SESSION_SECRET`, anyone with repo access can forge valid session cookies for any user.

```python
# Fix: raise instead of defaulting
raise RuntimeError("E2E tests require SESSION_SECRET env var to be set")
```

---

### H14. No End-to-End Integration Test for the Critical RFQ → Parse → Offer Flow

The business-critical pipeline — vendor email reply → Graph API poll → Claude parsing → confidence ≥ 0.8 → Offer auto-created — has unit tests for each step but no integration test running all three layers against a single DB session. A bug at any handoff point would not be caught.

---

## MEDIUM — Fix This Month

---

### M1. Missing Cascade `ondelete` on Many FK Columns
**Files:** `app/models/intelligence.py`, `app/models/vendors.py`, `app/models/quotes.py`, `app/models/strategic.py`, `app/models/performance.py`, `app/models/config.py`

12+ FK columns have no `ondelete` rule. Direct SQL deletes (from migrations or scripts) will violate FK constraints. Key examples:

| Column | Table | Should Be |
|---|---|---|
| `vendor_card_id` | `vendor_reviews` | `ON DELETE CASCADE` |
| `user_id` | `vendor_reviews` | `ON DELETE CASCADE` |
| All polymorphic FKs on `activity_log` | `intelligence.py:257–265` | `ON DELETE SET NULL` |
| `user_id` on `change_log` | `intelligence.py:233` | `ON DELETE SET NULL` |
| `user_id` on `strategic_vendors` | `strategic.py:31` | `ON DELETE RESTRICT` |
| `user_id` on `graph_subscriptions` | `config.py:66` | `ON DELETE CASCADE` |

---

### M2. `updated_at` Missing `onupdate` on Several Models
**Files:** `app/models/teams_alert_config.py:31`, `app/models/ics_search_queue.py:38`, `app/models/nc_search_queue.py:38`, `app/models/ics_worker_status.py:35`

These columns have a `default` but no `onupdate`. Every ORM update leaves `updated_at` frozen at creation time, making it impossible to know when a record was last changed.

```python
# Add onupdate to each:
updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                    onupdate=lambda: datetime.now(timezone.utc))
```

Also: `TroubleTicket.updated_at` (line 62) has `onupdate` but no `default` — it's `NULL` on insert.

---

### M3. `Requisition.deadline` Stored as `String` Instead of `Date`
**File:** `app/models/sourcing.py:44`

`deadline = Column(String(50))` stores "ASAP" or an ISO date string. SQL date comparisons (`WHERE deadline < :cutoff`, `ORDER BY deadline`) are lexicographic, not chronological, producing incorrect sort orders. Fix: add `deadline_date = Column(Date)` and `is_asap = Column(Boolean, default=False)` via migration.

---

### M4. Claude API Client Has No Retry Logic
**File:** `app/utils/claude_client.py:121–164`

A single 429, 503, or transient network error returns `None` immediately with no retry. Every user-facing AI feature — `company_intel`, `draft_rfq`, `enrich_contacts_websearch`, `parse_vendor_response` — inherits this fragility. `gradient_service.py` has a correct retry pattern with exponential backoff — apply it to `claude_client.py`.

---

### M5. Ownership Sweep Loads Entire Table Into Memory
**File:** `app/services/ownership_service.py:48`

`run_ownership_sweep()` calls `.all()` on the companies table. As the CRM grows to thousands of accounts, this nightly job will spike memory. Use `.yield_per(100)` or paginate with limit/offset.

---

### M6. Auto-Apply Threshold Bug — Regex Match Sets `confidence = 0.80` Without AI Verification
**File:** `app/services/email_intelligence_service.py:212–221`

With exactly 2 keyword regex matches: `confidence = 0.7 + 2 * 0.05 = 0.80`, which equals `CONFIDENCE_AUTO`. Combined with `has_pricing: True` (assumed, not extracted), `auto_applied = True` fires and draft offers are auto-created — without any Claude verification. This is a significant false-positive risk.

---

### M7. Missing DB-Level Check Constraints on Enum-Backed Columns

No check constraints exist for columns that have documented valid values:

| Table | Column | Valid Values | Fix |
|---|---|---|---|
| `users` | `role` | buyer, sales, trader, manager, admin | `CheckConstraint` |
| `vendor_reviews` | `rating` | 1–5 | `CheckConstraint("rating BETWEEN 1 AND 5")` |
| `buy_plans_v3` | `status` | enum values | `CheckConstraint` |
| `tags` | `tag_type` | brand, commodity | `CheckConstraint` |

---

### M8. Missing Indexes on High-Traffic Filter Columns

| Table | Column | Reason |
|---|---|---|
| `users` | `role`, `is_active`, `token_expires_at` | Queried constantly |
| `vendor_cards` | `is_blacklisted`, `vendor_score` | Sourcing filters and ranking |
| `nc_search_log` | `queue_id`, `searched_at` | Join from queue to log |
| `knowledge_entries` | `entry_type`, `is_resolved` | Primary Q&A filters |
| `vendor_responses` | `status`, `needs_action`, `received_at` | Inbox polling pattern |
| `notifications` | `is_read`, `ticket_id` | Unread badge query |
| `email_intelligence` | `message_id` (should be `unique=True`) | Deduplication |

---

### M9. Global Exception Handler Leaks Internal Exception Type to Clients
**File:** `app/main.py:244–265`

The 500 response includes `"type": type(exc).__name__` — exposing `"AttributeError"`, `"ProgrammingError"`, etc. to any client that triggers a server error. Remove the `"type"` key from the error response body.

---

### M10. Static PBKDF2 Salt — No Credential Key Rotation Path
**File:** `app/services/credential_service.py:30–39`

The hardcoded salt `b"availai-credential-salt-v1"` means a `secret_key` rotation immediately breaks all stored API credentials (Mouser key, DigiKey key, etc.) — they become permanently unreadable without the old key. Add a `CREDENTIAL_SALT` env var override and document the rotation procedure.

---

### M11. Fat Routers — Business Logic That Belongs in Services

Several router functions exceed 100–200 lines of business logic:

| Function | File | Lines | Issue |
|---|---|---|---|
| `get_activity()` | `rfq.py:277` | ~225 lines | Groups contacts, resolves vendor cards, fetches phones |
| `rfq_prepare()` | `rfq.py:506` | ~160 lines | DB queries, enrichment calls, commits |
| `_build_requisition_list()` | `requisitions/core.py:104` | ~332 lines | 20+ subqueries, role filtering, serialization |
| `import_stock_list_standalone()` | `materials.py:626` | ~135 lines | CSV parsing, multi-model upsert |
| `material_card_to_dict()` | `materials.py:82` | ~113 lines | 4 separate DB queries inside router |

---

### M12. Missing Input Validation — Raw `request.json()` Without Pydantic Schemas

| Endpoint | File | Issue |
|---|---|---|
| `PATCH /vendor-responses/{id}/status` | `rfq.py:230` | `body: dict` |
| `POST /follow-ups/send-batch` | `rfq.py:798` | raw `request.json()` |
| `PUT /requisitions/{id}/outcome` | `requisitions/core.py:509` | `body: dict` |
| `POST /quick-search` | `materials.py:311` | raw `request.json()` |
| `POST /materials/{id}/enrich` | `materials.py:388` | raw `request.json()` |
| `POST /materials/merge` | `materials.py:472` | raw `request.json()` |
| `int()` on query params | `admin/system.py:314–317` | `ValueError` → 500 |

---

### M13. Missing Pagination on List Endpoints

| Endpoint | File | Issue |
|---|---|---|
| `GET /requisitions/{id}/contacts` | `rfq.py:129` | No limit/offset |
| `GET /follow-ups` | `rfq.py:671` | Hardcoded `limit(500)`, no offset |
| `GET /email-intelligence` | `emails.py:190` | Has limit but no offset |
| `GET /requisitions/{id}/quotes` | `crm/quotes.py:88` | No limit/offset |
| `GET /pricing-history/{mpn}` | `crm/quotes.py:528` | Fetches 500, returns 50 — no pagination |
| `GET /ai/prospect-contacts` | `ai.py:186` | Hardcoded `limit(50)`, no offset |

---

### M14. `bandit` Security Scanner Never Fails CI
**File:** `.github/workflows/security.yml:38`

`bandit ... || true` ensures the step always passes regardless of findings. A high-severity finding is silently ignored. Remove `|| true` or use `--exit-zero` only for acknowledged false-positives.

---

### M15. `alembic/env.py` Missing `compare_type=True`
**File:** `alembic/env.py:59–62`

Without this flag, `alembic revision --autogenerate` will not detect column type changes (e.g., `String(100)` → `Text`, `Integer` → `BigInteger`). Type changes silently produce empty migrations.

```python
context.configure(
    connection=connection,
    target_metadata=target_metadata,
    compare_type=True,  # ← add this
)
```

---

### M16. `tax_id` and Masked API Keys Returned to Non-Admin Users
**Files:** `app/routers/crm/companies.py:170, 379`, `app/routers/sources.py:378–434`

Customer `tax_id` values (PII) are returned on all company list and detail endpoints accessible to any authenticated user. Masked API key values are also returned to any `require_user`, exposing which API integrations are configured.

---

### M17. Chromium Installed in Production Docker Image
**File:** `Dockerfile:31–32`

Chromium adds ~300 MB to the image and all current Chromium CVEs to the attack surface. The browser is only needed for the Playwright self-heal agent. Move it to a dedicated sidecar container or separate worker image.

---

### M18. Test Suite Role Override Maps `require_sales` → Buyer User
**File:** `tests/conftest.py:261`

The global `client` fixture maps `require_sales` to `test_user` (role=buyer). Any test using `client` and hitting a sales-gated endpoint will succeed — even if the endpoint should reject a buyer. Sales-gated security tests must use a dedicated fixture.

---

## LOW — Backlog

---

### L1. Ghost/Stub Model Files Should Be Deleted
**Files:** `app/models/notification_engagement.py`, `app/models/self_heal_log.py`

Both files contain only a docstring: `"REMOVED — this module was removed during simplification."` They confuse Alembic metadata discovery and mislead future developers. Delete them.

---

### L2. `notification.py` Uses Absolute Import Instead of Relative
**File:** `app/models/notification.py:13`

All other model files use `from .base import Base`. This one uses `from app.models.base import Base`. In some test configurations, this can cause the same `Base` class to appear as two different objects, breaking Alembic detection. Change to relative import.

---

### L3. Dual JSON + Normalized Row Storage on Quote and BuyPlan V1
**Files:** `app/models/quotes.py:33, 110`, `app/models/buy_plan.py`

`Quote` has both `line_items = Column(JSON)` and a `QuoteLine` table. They are not auto-synced. BuyPlan V1 has the same issue. V3 correctly removes JSON storage — V1 and Quote should follow.

---

### L4. Dead `asyncio.Semaphore` in `enrichment.py`
**File:** `app/services/enrichment.py:119`

A `Semaphore(concurrency)` is created but processing is sequential (`await _process_one(mpn)` in a loop). The semaphore never limits anything. Remove it to avoid misleading future maintainers.

---

### L5. Dead Code — `_do_search` Abstract Method Inside Function Body
**File:** `app/connectors/sources.py:148–161`

The `@abstractmethod` declaration for `_do_search` appears inside `_parse_retry_after()`, after a `return` statement — it is unreachable. Move it to the `BaseConnector` class body so Python enforces implementation in subclasses.

---

### L6. Teams Notification Stubs Producing Dead Log Lines
**File:** `app/services/buyplan_notifications.py:194, 316, 375, 477, 534, 562`

Six `logger.debug("Teams notification skipped (removed)")` calls pollute DEBUG logs for a removed feature. Either wire up the real Teams notifications or remove the dead calls.

---

### L7. `AVAIL_SCORE_WEIGHTS` Described Differently in CLAUDE.md vs. Code
**File:** `app/scoring.py:87`

CLAUDE.md documents: "price 30%, reliability 25%, lead time 20%, geography 15%, terms 10%". The code uses: trust 30%, price 25%, qty 20%, freshness 15%, completeness 10%. The mapping from business terminology to code variables is undocumented. Add a comment block above the weights explaining the mapping.

---

### L8. `Requirement` and `Sighting` Models Missing `updated_at`
**Files:** `app/models/sourcing.py:71, 109`

Both are actively updated (status changes, score updates) but have only `created_at`. There is no way to know whether the pricing data in a sighting is fresh or stale. Add `updated_at` columns via migration.

---

### L9. `int()` Casts on Query Params With No Error Handling
**Files:** `app/routers/admin/system.py:314–317`, `app/routers/vendors_crud.py:329–332`, `app/routers/materials.py:204–205`

Non-numeric input causes unhandled `ValueError` → 500 instead of 422. Use Pydantic `Query(ge=0)` params:

```python
# Fix:
limit: int = Query(default=100, ge=1, le=500)
offset: int = Query(default=0, ge=0)
```

---

### L10. `EnrichmentCreditUsage.month` Stored as `String(7)` vs. `Date` Elsewhere
**File:** `app/models/enrichment.py:171`

All other monthly snapshot tables use `Column(Date)`. This outlier uses `String(7)` ("2026-02" format), making cross-table month joins require casting. Change to `Date` (first day of month).

---

### L11. `get_quote()` Returns `None` (HTTP 200) Instead of 404
**File:** `app/routers/crm/quotes.py:55`

`response_model=QuoteDetailResponse | None` with `return None` sends HTTP 200 with a null body. Callers must special-case null. Return `HTTPException(404)` when no quote is found.

---

### L12. `/materials/backfill-manufacturers` Missing `/api/` Prefix
**File:** `app/routers/materials.py:616`

All other material routes use `/api/materials/*`. This route is at `/materials/backfill-manufacturers` — inconsistent.

---

### L13. `KnowledgeEntry.assigned_to_ids` — JSON Array With No Referential Integrity
**File:** `app/models/knowledge.py:43`

User IDs stored as JSON array. Deleting a user silently leaves stale IDs. This is a many-to-many relationship that should use a join table.

---

### L14. Redundant `index=True` on Primary Key Columns
**Files:** `app/models/notification.py:19`, `app/models/task.py:31`

Primary keys are always indexed. `index=True` on a PK column is noise. Remove these.

---

### L15. Azure Token Exchange Error Logs Full Response Body
**File:** `app/routers/auth.py:118–120`

`logger.error(f"Azure token exchange returned {resp.status_code}: {resp.text[:500]}")` logs the raw Azure response, which may contain OAuth details that shouldn't appear in Sentry. Log only the status code and parsed `error_codes` field.

---

## Architecture Strengths — What's Working Well

These are genuine strengths that should be preserved and used as patterns:

1. **Alembic discipline** — 76 migrations with upgrade + downgrade, DDL grep enforced in CI. Excellent.
2. **Layer separation** — Thin routers → fat services → clean models. The architecture holds across the entire codebase.
3. **Auth middleware chain** — `get_user` → `require_user` → `require_buyer/sales/manager/admin`. Clean and composable.
4. **UTC timestamp enforcement** — `UTCDateTime` type decorator + session event listener ensures timezone consistency globally.
5. **Connector pattern** — `asyncio.gather()` for parallel vendor searches with per-connector semaphores and circuit breakers. Well-designed.
6. **Test suite breadth** — 314 test files, 8,600+ test functions, in-memory SQLite with auth overrides, 20+ role fixtures, auto-rollback. The foundation is solid.
7. **Loguru usage** — No `print()` calls found across service files. Consistent structured logging throughout.
8. **Config validation** — 80+ settings with Pydantic validators, fail-fast on bad values, startup secret key guard.
9. **CSRF + security headers** — Middleware correctly applied with sensible defaults.
10. **DigiKey token refresh pattern** — Proactive expiry tracking with 60s safety margin. Use this as the template for eBay and Nexar.
11. **`with_for_update()` in ownership service** — Correctly used for claim operations. Extend this pattern to quote number generation.
12. **`enrich_batch()` identity map flush** — `db.expire_all()` every 100 rows prevents SQLAlchemy memory accumulation on long jobs. Good pattern.
13. **Email dedup via `ProcessedMessage`** — Savepoint protection for inbox scanning. Correct.

---

## Recommended Fix Order

### Immediate (< 1 day each)

| # | Fix | File | Impact |
|---|---|---|---|
| 1 | Add `@limiter.limit("5/minute")` to password login | `auth.py:215` | Blocks brute force |
| 2 | Narrow CSRF exemption to specific paths | `main.py:285` | Closes force-logout |
| 3 | Add `require_user` to SSE stream endpoint | `requisitions2.py:97` | Closes data leak |
| 4 | Fix `db.flush()` → `db.commit()` in `summarize_thread` | `email_intelligence_service.py:476` | Stops AI token waste |
| 5 | Use `hmac.compare_digest` for agent key | `dependencies.py:54` | Closes timing attack |
| 6 | Narrow `/auth/status` to own data for non-admins | `auth.py:271` | Stops PII exposure |
| 7 | Fix deploy.sh `SECRET_KEY` vs `SESSION_SECRET` mismatch | `scripts/deploy.sh` | Prevents broken deploys |

### This Sprint (1–3 days each)

| # | Fix | File | Impact |
|---|---|---|---|
| 8 | Fix quote number race condition (`SELECT FOR UPDATE`) | `crm_service.py:14` | Compliance |
| 9 | Add ownership check to quote mutation endpoints (6) | `crm/quotes.py` | Authorization |
| 10 | Add role guard to `bulk_archive` | `requisitions/core.py:542` | Data protection |
| 11 | Add `compare_type=True` to `alembic/env.py` | `alembic/env.py:59` | Migration correctness |
| 12 | Add retry logic to `claude_client.py` | `utils/claude_client.py` | Reliability |
| 13 | Fix eBay and Nexar token expiry tracking | `connectors/ebay.py` | Sourcing reliability |
| 14 | Remove `|| true` from bandit in CI | `security.yml:38` | Enforce security scans |
| 15 | Fix health check in deploy.yml | `deploy.yml:56` | Fix broken CI |
| 16 | Add pre-deploy backup to `update.sh` | `scripts/update.sh` | Data safety |

### This Month

| # | Fix | Effort |
|---|---|---|
| 17 | Add `ondelete` to 12+ bare FK columns (Alembic migration) | Medium |
| 18 | Add `onupdate` to 5 `updated_at` columns (Alembic migration) | Small |
| 19 | Add indexes on 15+ high-traffic filter columns (Alembic migration) | Medium |
| 20 | Fix `Requisition.deadline` String → Date (migration + data backfill) | Large |
| 21 | Add check constraints on rating, role, status columns | Small |
| 22 | Move business logic out of fat routers into services | Large |
| 23 | Add Pydantic schemas to 7 raw-dict endpoints | Medium |
| 24 | Add pagination to 6 unbounded list endpoints | Medium |
| 25 | Gate `tax_id` and API credentials behind admin role | Small |
| 26 | Move Chromium to sidecar/separate image | Medium |
| 27 | Add end-to-end test for email → parse → Offer flow | Large |

---

## Session Close

**Migration flag:** Yes — many of the fixes above (FK cascades, indexes, updated_at onupdate, check constraints, deadline column type change) each require a new Alembic migration. Create them one at a time, test upgrade + downgrade before deploying.

**STABLE.md flag:** Review before touching `dependencies.py`, `scoring.py`, `email_intelligence_service.py`, `crm_service.py`, and `requisitions/core.py`.

**Tech debt noted:**
- BuyPlan V1 dual JSON/row storage
- Dead stub model files
- `require_sales` fixture maps to buyer — latent false-negatives in security tests
- `compare_type=True` missing from Alembic env — type drift invisible to autogenerate
