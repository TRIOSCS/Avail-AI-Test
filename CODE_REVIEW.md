# AvailAI Code Review — March 2026

## Executive Summary

AvailAI is a **well-structured, production-grade** electronic component sourcing platform. The codebase is large (~740 Python files, 40K lines of services, 80+ ORM models, 60+ Alembic migrations) and demonstrates solid architecture. This document reflects a full deep-dive review conducted in March 2026, covering core infrastructure, services, routers, models, connectors, and tests.

**Overall Grade: B+** — Strong foundation with a clear set of improvements needed before the next scaling phase.

---

## Table of Contents

1. [Critical / High Severity Bugs](#critical--high-severity-bugs)
2. [Security Issues](#security-issues)
3. [Architecture Violations — Routers](#architecture-violations--routers)
4. [Service Layer Issues](#service-layer-issues)
5. [Data Model & Schema Issues](#data-model--schema-issues)
6. [Connector Issues](#connector-issues)
7. [Configuration & Infrastructure](#configuration--infrastructure)
8. [Test Coverage Gaps](#test-coverage-gaps)
9. [Architecture Strengths](#architecture-strengths)
10. [Recommended Next Steps](#recommended-next-steps)

---

## Critical / High Severity Bugs

### BUG-1: Concurrent SQLAlchemy Session in `asyncio.gather` — Data Corruption Risk
**File:** `app/services/ownership_service.py:719-742`
**Severity:** CRITICAL

`send_manager_digest_email` runs multiple coroutines concurrently via `asyncio.gather`, and each closure captures the same synchronous `Session` object and calls `db.query(...)` concurrently. SQLAlchemy `Session` objects are not thread-safe or coroutine-safe — concurrent access produces undefined behavior including corrupted session state and wrong query results.

```python
# Current (broken)
await asyncio.gather(*[_send_digest_to_admin(e) for e in settings.admin_emails])

# Fix: run sequentially
for e in settings.admin_emails:
    await _send_digest_to_admin(e)
```

---

### BUG-2: `@abstractmethod _do_search` is Dead Code
**File:** `app/connectors/sources.py:155-161`
**Severity:** HIGH

The `@abstractmethod` decorator for `_do_search` is placed **inside** the body of `_parse_retry_after()`, after its `return` statement. It is unreachable. `BaseConnector` is not truly abstract — Python will not prevent direct instantiation, and `_do_search` is not an enforced interface. All subclasses implement it correctly by convention only.

```python
# Current (broken) — abstractmethod is after the return statement, inside another method
def _parse_retry_after(response: httpx.Response) -> float:
    ...
    return 5.0 + random.uniform(0, 2)   # function returns here

    @abstractmethod                       # DEAD CODE — never reached
    async def _do_search(self, part_number: str) -> list[dict]:
        pass
```

**Fix:** Move `@abstractmethod _do_search` to be a proper method inside the `BaseConnector` class body, not inside another method.

---

### BUG-3: Health Check — Scheduler Error Status is Unreachable
**File:** `app/main.py:491`
**Severity:** HIGH

`scheduler_status` is only ever set to `"ok"` or `"off"` — never `"error"`. The degraded condition `scheduler_status == "error"` can never be true, so the health endpoint will never return `503` due to a scheduler failure. The application will silently report healthy even if background jobs have stopped.

**Fix:** Add an `"error"` branch when `APScheduler` state is not `STATE_RUNNING`.

---

### BUG-4: Disclaimer Stripping Regex Broken After Whitespace Collapse
**File:** `app/services/response_parser.py:233-251`
**Severity:** HIGH

`_clean_email_body` collapses all whitespace to single spaces **before** running disclaimer removal regexes. The disclaimer patterns use `(?=\n\n|\Z)` as a lookahead — after whitespace collapse, there are no `\n\n` sequences left. Disclaimers only strip if they happen to reach end-of-string, meaning legal boilerplate from most vendor emails consumes tokens and pollutes the AI parsing prompt.

**Fix:** Strip disclaimers first, then collapse whitespace. Or use `re.DOTALL` and remove the `\n\n` requirement.

---

### BUG-5: `thinking_budget` Exceeds `max_tokens` in Response Parser
**File:** `app/services/response_parser.py:160-167`
**Severity:** HIGH

`thinking_budget=2048` is set with `max_tokens=1024`. In the Anthropic extended-thinking API, thinking tokens count against the output token budget. `thinking_budget` must be less than `max_tokens`. This call will either error at the API level or be silently downgraded.

**Fix:** Set `max_tokens >= thinking_budget + expected_output_tokens`. A safe value here is `max_tokens=4096`.

---

### BUG-6: Silent Data Loss in `clone_requisition` with Duplicate MPNs
**File:** `app/services/requisition_service.py:128-134`
**Severity:** HIGH

`mpn_to_new_id` is a dict keyed by `primary_mpn`. If a requisition has two line items with the same MPN (valid — different target prices, different quantities), the second entry overwrites the first. All offers linked to the first requirement are then silently re-linked to the wrong cloned requirement.

**Fix:** Key the map by requirement `id`, not `primary_mpn`:
```python
req_map = {old_r.id: new_r.id for old_r, new_r in zip(source_req.requirements, new_reqs)}
```

---

### BUG-7: Silent FK Reassignment Failure in Vendor Merge
**File:** `app/services/vendor_merge_service.py:86-94`
**Severity:** HIGH

FK reassignment failures during vendor merge are caught with `except Exception` and logged at `debug` level. If one FK table fails, related records are orphaned with no rollback. The merge operation completes "successfully" while leaving data in an inconsistent state.

**Fix:** Wrap the entire merge in a single transaction and raise on failure.

---

### BUG-8: Unreachable Code in `startup.py` — TESTING Branch
**File:** `app/startup.py:53-54`
**Severity:** MEDIUM

`run_startup_migrations()` returns early at line 34 when `TESTING=1`. The `os.environ.get("TESTING") == "1"` condition on line 53 inside that same function can therefore never be reached — the function has already returned.

---

## Security Issues

### SEC-1: Agent API Key Timing Attack Vulnerability
**File:** `app/dependencies.py:50-55`
**Severity:** CRITICAL

Agent API key comparison uses `==` (string equality), which is vulnerable to timing attacks. A sufficiently precise attacker can determine key length and prefix by measuring response times.

**Fix:** Replace with `secrets.compare_digest(agent_key, settings.agent_api_key)`. Also add `logger.info("Agent API key used from IP {}", request.client.host)` for audit logging.

---

### SEC-2: SQL Injection Risk in `vendor_analytics.py`
**File:** `app/routers/vendor_analytics.py:185-238`
**Severity:** HIGH

`mpn_filter` is inserted into an f-string SQL query via `sqltext(f"...")`. While the immediate value uses parameterized `:mpn_pattern`, the f-string interpolation of `{mpn_filter}` means a future developer can easily introduce injection. This pattern is inherently fragile.

**Fix:** Refactor to always include the WHERE clause with a `%` parameter for the match-all case.

---

### SEC-3: No Rate Limiting on Password Login
**File:** `app/routers/auth.py:216`
**Severity:** HIGH

The password login endpoint has no brute-force protection. While `ENABLE_PASSWORD_LOGIN=false` by default, when enabled there is no `@limiter.limit()` decorator or lockout mechanism.

**Fix:** Add `@limiter.limit("5/minute")` to the password login endpoint.

---

### SEC-4: Password Hashing Uses SHA-256
**File:** `app/startup.py:63-80`
**Severity:** MEDIUM

Default user creation in startup uses `hashlib.sha256` for password hashing. SHA-256 is not suitable for passwords — it is too fast and highly vulnerable to GPU-based brute force.

**Fix:** Switch to `bcrypt` or `argon2`. Limited blast radius — only affects the `ENABLE_PASSWORD_LOGIN` code path.

---

### SEC-5: API Keys Logged in URL Parameters
**File:** `app/connectors/mouser.py:42`
**Severity:** MEDIUM

Mouser's API key is sent as a URL query parameter (`params={"apiKey": self.api_key}`). This key appears in access logs, error stack traces, and any Sentry breadcrumbs that record the URL.

**Fix:** Switch to `Authorization` header if Mouser supports it. Otherwise mask the key in log output using a loguru `filter` that redacts `apiKey=...` from URL strings.

---

### SEC-6: CSP Uses `unsafe-inline` for `script-src`
**File:** `app/main.py:325`
**Severity:** MEDIUM

The Content Security Policy includes `'unsafe-inline'` in `script-src`. This is a significant XSS mitigation weakener. Acknowledged in a comment, but should be tracked as an explicit security debt item with a target date to migrate to nonces or hashes.

---

### SEC-7: XSS via `javascript:` URLs in HTML Sanitizer
**File:** `app/static/app.js:458`
**Severity:** MEDIUM

`sanitizeRichHtml` whitelists `href` on `<a>` tags but does not validate the URL scheme. `<a href="javascript:alert(1)">` bypasses the sanitizer and is executed by some browsers regardless of CSP.

**Fix:** Validate `href` starts with `http://`, `https://`, or `/` before allowing it.

---

### SEC-8: Retry-After Header Not Capped
**File:** `app/connectors/sources.py:153`
**Severity:** MEDIUM

`max(float(header), 1.0)` has no upper bound. A buggy or malicious API returning `Retry-After: 999999` would block the connector for 11+ days without any alert.

**Fix:** `min(max(float(header), 1.0), 300.0)` — cap at 5 minutes.

---

## Architecture Violations — Routers

The router-thin / service-fat rule from CLAUDE.md is violated most severely in `rfq.py` and `requirements.py`. These are not minor infractions — they represent hundreds of lines of un-testable service logic buried in HTTP handlers.

### ARCH-1: `rfq.py` — `_enrich_with_vendor_cards` (141 lines of service logic)
**File:** `app/routers/rfq.py:860-1001`
**Severity:** HIGH

This function auto-creates `VendorCard` records, merges emails and phones, commits to the DB, and applies blacklist/garbage filtering. It is imported by `requisitions/__init__.py` and re-exported. This is core business logic that belongs in `services/vendor_enrichment_service.py`.

---

### ARCH-2: `rfq.py` — `get_activity` (225 lines of analytics logic)
**File:** `app/routers/rfq.py:277-502`
**Severity:** HIGH

This endpoint performs multi-source joins, contains a vendor-status derivation decision tree (quoted/declined/replied), builds lookup maps, and constructs phone/vendor grouping structures. None of this is HTTP-layer work. It belongs in `services/activity_service.py`.

---

### ARCH-3: `rfq.py` — `rfq_prepare` (160 lines of enrichment logic)
**File:** `app/routers/rfq.py:506-667`
**Severity:** HIGH

Builds an exhaustion map (which parts sent to each vendor), handles past-RFQ email reuse, runs async contact lookups with a `Semaphore`, and writes enriched emails/phones back to `VendorCard`. Belongs in `services/rfq_service.py`.

---

### ARCH-4: `rfq.py` — Follow-up endpoints build emails and call Graph API directly
**File:** `app/routers/rfq.py:752-856`
**Severity:** MEDIUM

`send_follow_up` and `send_follow_up_batch` assemble email subjects and bodies, then call `gc.post_json("/me/sendMail", ...)` directly. Email construction and Graph API calls belong in `email_service.py`. The router should only call `await email_service.send_follow_up(...)`.

---

### ARCH-5: `requirements.py` — `add_requirements` and `upload_requirements`
**File:** `app/routers/requisitions/requirements.py:265-545`
**Severity:** HIGH

These two endpoints each contain 140+ lines of inline business logic: MPN normalization, deduplication, material card resolution, DB insert, tag propagation, NC/ICS enqueueing, and duplicate detection. Each step is a distinct business rule that should live in a service.

---

### ARCH-6: `requirements.py` — Duplicate Queue Closures
**File:** `app/routers/requisitions/requirements.py:339-370, 520-543`
**Severity:** MEDIUM

`_nc_enqueue_batch` and `_ics_enqueue_batch` are defined twice as closures inside `add_requirements` and again inside `upload_requirements`. A top-level `_enqueue_ics_nc_batch` exists but is only used in `search_all`. All three should be consolidated into a single service call.

---

### ARCH-7: `requirements.py` — `import_stock_list` Constructs Sighting Objects Inline
**File:** `app/routers/requisitions/requirements.py:912-1001`
**Severity:** MEDIUM

Full import logic — constructing `Sighting` objects with all field normalization — runs inline in the router. Should be `sourcing_service.import_vendor_stock(rows, vendor_name, req, db)`.

---

## Service Layer Issues

### SVC-1: N+1 Query — MaterialCard Lookup in Loop
**File:** `app/search_service.py:771-774`
**Severity:** HIGH

One `db.query(MaterialCard)` call per part number inside a loop.

**Fix:** Use `db.query(MaterialCard).filter(MaterialCard.mpn.in_(pns)).all()` to load all cards in a single query, then build a lookup dict.

---

### SVC-2: N+1 Query — VendorContact Lookup in Nested Loop
**File:** `app/search_service.py:1095-1097`
**Severity:** HIGH

One `db.query(VendorContact)` per email per vendor inside nested loops.

**Fix:** Pre-fetch all contacts for all relevant `vendor_card_id`s in one query before the loop.

---

### SVC-3: Misleading Variable Name — `has_price_below_target`
**File:** `app/search_service.py:765-768`
**Severity:** MEDIUM

The variable name implies "a result is priced below the buyer's target price," but the actual check is "any result has a non-zero price at all." The target price from `req` is never referenced. This causes `should_trigger_ai_search` to behave differently from what a reader would expect, making AI search trigger decisions hard to reason about.

---

### SVC-4: Hardcoded `source="brokerbin"` for All Connectors
**File:** `app/search_service.py:1104`
**Severity:** MEDIUM

`_propagate_vendor_emails` hardcodes `source="brokerbin"` when creating `VendorContact` records. If DigiKey, Nexar, or any other connector returns vendor contacts, those are incorrectly tagged as BrokerBin sourced. Should be derived from `sighting["source_type"]`.

---

### SVC-5: Silent Redis Failures — No Logging
**File:** `app/search_service.py:93-96, 115-116`
**Severity:** MEDIUM

Redis connection failures are swallowed with bare `except Exception: pass`. If Redis goes down, search caching silently stops and the issue only manifests as elevated API call volume — invisible in logs.

**Fix:** Add `logger.warning("Redis unavailable: {}", exc_info=True)`.

---

### SVC-6: Multiple `db.commit()` Within a Single Request
**File:** `app/search_service.py` — `_upsert_material_card`
**Severity:** MEDIUM

Two separate `db.commit()` calls exist within a single material card upsert (after vendor history upsert, then after tag classification). If the second commit fails, the first is already permanent. The request ends with partial state.

**Fix:** Use a single commit at the end, or use a savepoint for the second operation.

---

### SVC-7: Fast-Path Score Approximation Produces Wrong Values
**File:** `app/services/sourcing_score.py:236-237`
**Severity:** MEDIUM

`sighting_count = int(sourced_ratio * 5)` converts a 0–1 ratio to a fake sighting count of 0–5. For `sourced_ratio=0.6` with 50 requirements, this gives `sighting_count=3` when the actual number is 30. This creates visible score inconsistencies between list-view (fast path) and detail-view (full path).

---

### SVC-8: Requisition-Level Signals Shown Per Requirement
**File:** `app/services/sourcing_score.py`
**Severity:** MEDIUM

`_build_signals` is called with `raw_rfqs=rfq_sent` (total RFQs for the whole requisition) for every individual line item. The tooltip displays "RFQs sent: 12" on every requirement card, even though those RFQs were spread across all requirements. Misleading in the UI.

---

### SVC-9: Live Confidence Hard-Floored at 70%
**File:** `app/scoring.py:280-282`
**Severity:** MEDIUM

A raw score of 0 (new vendor, no price, no qty, no lead time) still shows `70%` confidence to buyers because of the hard floor. Combined with the confidence color logic, a completely empty live sighting could display green. The intent (live API data is more trustworthy than historical) is sound, but 70% is misleadingly high for empty results.

---

### SVC-10: Unreachable Fallback Branch in `score_unified`
**File:** `app/scoring.py:350-358`
**Severity:** LOW

The `"# Fallback (shouldn't happen)"` branch at the end of `score_unified` is unreachable — all non-historical/affinity/ai_live_web source types are caught by the first `if` condition and routed to the live-API scoring branch. The labeled fallback is dead code.

---

### SVC-11: Deferred `import re` Inside Function Body
**File:** `app/services/response_parser.py:235`
**Severity:** LOW

`import re` appears inside `_clean_email_body`. Python caches module imports so it is not a performance issue, but it is unconventional and violates the PEP 8 rule that all imports should be at the top of the file.

---

### SVC-12: Magic Number `999` Repeated Three Times
**File:** `app/services/ownership_service.py:65, 172, 527`
**Severity:** LOW

The sentinel value `999` (meaning "no activity / force clear") appears three times without documentation.

**Fix:**
```python
_NO_ACTIVITY_DAYS_SENTINEL = 999  # Forces ownership clear regardless of rules
```

---

### SVC-13: `_fetch_fresh` is 304 Lines Long
**File:** `app/search_service.py`
**Severity:** LOW

`_fetch_fresh` handles credential lookup, connector instantiation, parallel execution, stats persistence, and AI trigger logic in one function. Per the project's own architecture rules, this should be decomposed into sub-functions or a `FreshSearchOrchestrator` class.

---

### SVC-14: Unbounded `while True` Loop in Startup Backfill
**File:** `app/startup.py:387-415`
**Severity:** MEDIUM

`_backfill_sighting_vendor_normalized()` paginates in batches of 10,000 with no iteration cap. On a fresh database with millions of sightings, startup could stall significantly. This should have a maximum iteration limit or be extracted to a background job that runs post-startup.

---

### SVC-15: `vinod@trioscs.com` Hardcoded in Source
**File:** `app/startup.py:103-128`
**Severity:** LOW

A specific employee's email address is baked into source code. If this person leaves or the email changes, a code change and redeployment are required.

**Fix:** Replace with an `ADMIN_SEED_EMAIL` environment variable read from `settings`.

---

## Data Model & Schema Issues

### MDL-1: No Shared `TimestampMixin` — Inconsistent `created_at`/`updated_at`
**File:** `app/models/base.py`
**Severity:** HIGH

`models/base.py` defines only an empty `DeclarativeBase`. There is no shared mixin for `id`, `created_at`, or `updated_at`. As a result:
- `User` has no `updated_at` at all
- `Requirement` has no `updated_at` at all
- `Requisition.updated_at` has no `onupdate=lambda: datetime.now(timezone.utc)` — it never auto-updates
- `Sighting.created_at` uses `DateTime` (no timezone), while `source_searched_at` uses `DateTime(timezone=True)`
- `Company` and `CustomerSite` have `last_activity_at` with `UTCDateTime` but `created_at` with plain `DateTime`

**Fix:** Create a `TimestampMixin`:
```python
class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc), nullable=False)
```
Then apply via Alembic migration for models that are missing columns.

---

### MDL-2: Missing Index on `material_cards.deleted_at`
**File:** `app/models/intelligence.py:49`
**Severity:** HIGH

Soft-delete queries (`WHERE deleted_at IS NULL`) run on every `MaterialCard` lookup. Without an index this is a full table scan that grows with every upserted material card.

**Fix:** Add migration: `Index("ix_material_cards_deleted_at", "deleted_at")`.

---

### MDL-3: Missing Indexes on High-Traffic Filter Columns
**Severity:** HIGH

The following columns are used in frequent `WHERE`/`ORDER BY` but have no index:

| Model | Column | Usage |
|---|---|---|
| `User` | `role` | Every request role check |
| `User` | `is_active` | User listing |
| `User` | `token_expires_at` | Scheduler token refresh |
| `Sighting` | `created_at` | Recency filtering |
| `VendorCard` | `is_blacklisted` | Sighting enrichment |
| `VendorCard` | `vendor_score` | Vendor list ordering |
| `Company` | `is_active` | CRM list |
| `Company` | `last_enriched_at` | Enrichment scheduler |
| `SiteContact` | `needs_refresh` | Enrichment scheduler |

**Fix:** Generate a single Alembic migration adding all indexes.

---

### MDL-4: Missing `ondelete` on Critical Foreign Keys
**Severity:** HIGH

Several FKs lack `ondelete` behavior, risking dangling references if a parent is deleted outside the ORM:

| Model | FK Column | Missing |
|---|---|---|
| `VendorReview` | `vendor_card_id` | `ondelete="CASCADE"` (conflicts with ORM cascade) |
| `Company` | `account_owner_id` | `ondelete="SET NULL"` |
| `CustomerSite` | `owner_id` | `ondelete="SET NULL"` |
| `Sighting` | `source_company_id` | `ondelete="SET NULL"` |
| `Requisition` | `cloned_from_id` | `ondelete="SET NULL"` |

---

### MDL-5: Dual Contact Storage on `VendorCard`
**File:** `app/models/vendors.py`
**Severity:** MEDIUM

`VendorCard` has both a `contacts = Column(JSON)` blob and a separate `VendorContact` table with a SQLAlchemy relationship. These are two sources of truth for the same data. Over time, callers that write to one but read from the other will produce stale results.

**Fix:** Document which is canonical (the relational table is the right answer), and deprecate/migrate the JSON column.

---

### MDL-6: Missing Unique Constraint on `site_contacts`
**File:** `app/models/crm.py:149-188`
**Severity:** MEDIUM

No unique constraint on `(customer_site_id, email)` — duplicate contacts per site are allowed at the database level.

---

### MDL-7: Missing Unique Constraint on `VendorReview` — ORM vs DB Cascade Conflict
**File:** `app/models/vendors.py:156-171`
**Severity:** MEDIUM

`VendorReview.vendor_card_id` FK lacks `ondelete="CASCADE"` at the DB level, but the ORM relationship uses `cascade="all, delete-orphan"`. If the card is deleted via raw SQL (e.g., in a migration or admin script), reviews are orphaned at the DB level — the ORM cascade only fires when the deletion goes through SQLAlchemy.

---

### MDL-8: 101 Relationships Missing `back_populates`
**Severity:** MEDIUM

Many relationships are one-way (no inverse). Worst offenders: `offers.py` (4 User FKs), `buy_plan.py` (6 User FKs), `strategic.py` (uses deprecated `backref=`). One-way relationships prevent ORM cache invalidation and make relationship traversal unreliable in both directions.

**Fix:** Replace `backref=` with explicit `back_populates` on both sides.

---

### MDL-9: `OfferStatus.sold` vs `OfferStatus.won` Undocumented
**File:** `app/enums.py:28-31`
**Severity:** LOW

Both `sold` and `won` exist as terminal offer states with no documentation on the distinction. This risks inconsistent usage across services (some mark `sold`, others mark `won`, reports look at one or the other).

---

## Connector Issues

### CON-1: `@abstractmethod _do_search` is Dead Code (see BUG-2)
**File:** `app/connectors/sources.py`
**Severity:** HIGH (duplicated from BUG-2 for discoverability)

---

### CON-2: No Unit Tests for Any Connector
**Severity:** HIGH

No direct tests exist for `MouserConnector`, `DigiKeyConnector`, `OEMSecretsConnector`, `NexarConnector`, `BrokerBinConnector`, or `Element14Connector`. These are only tested indirectly via `_fetch_fresh` with mocked connectors. The actual HTTP logic, field parsing, error handling paths, and circuit breaker transitions are untested.

---

### CON-3: CircuitBreaker Not Tested
**File:** `app/connectors/sources.py`
**Severity:** MEDIUM

Open/half-open/closed transitions, `reset_timeout` window, and the `record_failure` accumulation logic for the `CircuitBreaker` are never exercised in the test suite.

---

### CON-4: No Logging When Circuit Opens or Resets
**File:** `app/connectors/sources.py`
**Severity:** MEDIUM

The `CircuitBreaker` silently transitions states. When a connector circuit opens (after 5 consecutive failures), no log entry is written. Production incidents from a connector outage would be invisible until someone notices that search results are empty.

**Fix:** Add `logger.warning("Circuit breaker OPEN for {}", connector_name)` and `logger.info("Circuit breaker RESET for {}", connector_name)`.

---

### CON-5: `import re` Inside Loop in Mouser Connector
**File:** `app/connectors/mouser.py`
**Severity:** LOW

`import re` appears inside a loop body. Python caches module imports so this is not a functional issue, but it should be at the module top level per convention.

---

### CON-6: DigiKey 429 Double-Retry Complexity
**File:** `app/connectors/digikey.py`
**Severity:** LOW

Two retry layers exist for 429: one inside `_do_search` (waits for `Retry-After`, retries once), and an outer one from `BaseConnector._search_with_retry`. This is correct in behavior but complex to reason about. Add a comment explaining the layered behavior.

---

## Configuration & Infrastructure

### CFG-1: `main.py` Exceeds Architecture Size Limit
**File:** `app/main.py`
**Severity:** MEDIUM

`main.py` is 675 lines. The architecture rule from CLAUDE.md caps it at ~50 lines. `_seed_api_sources()` (80+ lines), the health check endpoint (100+ lines), and the service worker endpoint all belong in dedicated modules:
- `_seed_api_sources()` → `app/startup.py` or `app/services/admin_service.py`
- Health check logic → `app/services/health_service.py`

---

### CFG-2: `mvp_mode=True` Default Silently Disables Features
**File:** `app/config.py:199`
**Severity:** MEDIUM

`mvp_mode` defaults to `True`, silently disabling Dashboard/Analytics, Enrichment, Teams alerts, and Task Manager unless `MVP_MODE=false` is in `.env`. A fresh deployment looks broken without understanding why features are missing.

**Fix:** Default to `False`, or add a prominent startup log: `logger.warning("MVP mode enabled — Dashboard, Enrichment, Teams, and Task Manager are disabled")`.

---

### CFG-3: `anthropic_model` Default Needs Verification
**File:** `app/config.py:96`
**Severity:** MEDIUM

The default `"claude-sonnet-4-6"` is an internal shorthand. Verify it matches the exact model ID accepted by the Anthropic Python SDK. An invalid model ID fails at runtime, not at import time.

---

### CFG-4: CSV Fields Typed as `str` but Runtime Type is `list`/`frozenset`
**File:** `app/config.py:237-248`
**Severity:** LOW

`admin_emails`, `stock_sale_vendor_names`, and `stock_sale_notify_emails` are declared as `str` but mutated to `list` via `model_validator`. Static type checkers (mypy, pyright) will flag usage of these fields as `list` as type errors. Declare the field types correctly and use a custom validator that returns the parsed type.

---

### CFG-5: Pool Size May Exceed PostgreSQL `max_connections`
**File:** `app/database.py`
**Severity:** MEDIUM

`pool_size=20` + `max_overflow=40` = 60 possible connections from the app. PostgreSQL's default `max_connections` is 100. On a single DigitalOcean droplet, other tools (pgAdmin, backups, direct queries) also consume connections. Run `SHOW max_connections;` and `SELECT count(*) FROM pg_stat_activity;` to verify headroom.

---

### CFG-6: `get_db()` Missing Explicit Rollback on Exception
**File:** `app/database.py:76-81`
**Severity:** LOW

SQLAlchemy will roll back on `session.close()` when `autocommit=False`, but making it explicit is safer and clearer:
```python
def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

---

### CFG-7: `STABLE.md` Registry is Incomplete
**File:** `STABLE.md`
**Severity:** LOW

The following stable files are not listed:
- `app/enums.py` — underpins all status checks across routers and services
- `app/scheduler.py` — broken scheduler = no background jobs
- `app/routers/htmx_views.py`, `app/routers/views.py` — default UI entry points

---

### CFG-8: `require_admin` Does Not Use `is_admin()` Helper
**File:** `app/dependencies.py`
**Severity:** LOW

`is_admin()` is defined at line 64 but `require_admin` duplicates its logic inline. These should be consistent.

---

### CFG-9: `UserRole` Enum Not Used in Dependency Role Checks
**File:** `app/dependencies.py`
**Severity:** LOW

Role tuples in `require_buyer` and `require_sales` are plain string literals rather than `UserRole` enum values. If a new role is added to the enum, the dependency tuples must also be manually updated — no compile-time enforcement.

---

## Test Coverage Gaps

### TEST-1: No Direct Connector Unit Tests
**Severity:** HIGH

No tests cover `mouser.py`, `digikey.py`, `oemsecrets.py`, `nexar.py`, or `brokerbin.py` directly. HTTP logic, response parsing, 401/429 retry paths, and error handling are all untested.

---

### TEST-2: No N+1 Query Tests
**Severity:** MEDIUM

Zero tests verify eager-loading behavior. List endpoints for Offers, Requisitions, and ActivityLog likely have N+1 patterns under load.

**Fix:** Add a pytest fixture that wraps `sqlalchemy.event.listen(engine, "before_cursor_execute", ...)` to count queries per endpoint and assert a max.

---

### TEST-3: No Cascade Delete Tests
**Severity:** MEDIUM

No tests verify that deleting a `Requisition` properly cascades through `Requirements` → `Sightings` → `Offers` → `Quotes`.

---

### TEST-4: No Alembic Downgrade Tests
**Severity:** MEDIUM

60+ migrations have `downgrade()` functions but none are tested. A production rollback from a bad migration could fail silently in `downgrade()`.

---

### TEST-5: CircuitBreaker State Machine Not Tested
**Severity:** MEDIUM

Open/half-open/closed transitions and reset timeout behavior are never exercised.

---

### TEST-6: Schemas Missing `from_attributes=True`
**Severity:** LOW

Only 6 of 24 Pydantic schema files have `ConfigDict(from_attributes=True)`. ORM → Pydantic serialization silently fails without it when accessing relationship fields.

---

## Architecture Strengths

1. **Clean layer separation** — Routers → services → models is enforced (violations are the exception, not the rule).
2. **Alembic discipline** — 60+ migrations, DDL-only-in-migrations enforced in `CLAUDE.md`.
3. **Auth middleware** — Clean dependency chain: `get_user` → `require_user` → `require_buyer/admin/sales`.
4. **UTC everywhere** — `UTCDateTime` type decorator + DB event listener enforces timezone consistency.
5. **Rate limiting** — slowapi with configurable limits (120/min default, 20/min for search).
6. **Comprehensive config** — 80+ settings with validators, CSV parsing, fail-fast on bad values.
7. **Circuit breaker pattern** — Prevents cascading connector failures; per-connector concurrency limits.
8. **Delta query caching** — Incremental inbox sync (not full scans) via Graph API.
9. **Connector credential management** — API keys injected at runtime via `credential_service`, no hardcoded secrets.
10. **Test isolation** — In-memory SQLite, fresh event loop per test, proper auth overrides, full role matrix fixtures.
11. **Broad test suite** — 314 test files, 8,605 test functions covering routers, services, scoring math, and schemas.
12. **Background enrichment** — Fire-and-forget enrichment keeps request latency low.
13. **Loguru consistency** — No `print()` calls found. Request IDs propagated through log context.
14. **Sentry integration** — Sensitive field scrubbing is production-ready.

---

## Recommended Next Steps (Priority Order)

### Quick Wins (< 1 hour each)
| # | Action | File | Effort |
|---|--------|------|--------|
| 1 | `secrets.compare_digest()` for agent API key | `dependencies.py:50` | 15 min |
| 2 | Add rate limit to password login | `routers/auth.py:216` | 15 min |
| 3 | Cap `Retry-After` header at 300s | `connectors/sources.py:153` | 10 min |
| 4 | Validate `href` schemes in JS sanitizer | `static/app.js:458` | 20 min |
| 5 | Add `logger.warning` for silent Redis failures | `search_service.py:93` | 10 min |
| 6 | Add `logger.warning` when circuit breaker opens | `connectors/sources.py` | 15 min |
| 7 | Fix `is_admin()` usage in `require_admin` | `dependencies.py` | 5 min |
| 8 | Move `@abstractmethod _do_search` outside `_parse_retry_after` | `connectors/sources.py` | 5 min |

### High Priority (1-4 hours each)
| # | Action | File | Effort |
|---|--------|------|--------|
| 9 | Fix concurrent session access in `asyncio.gather` | `ownership_service.py:719` | 30 min |
| 10 | Fix `thinking_budget > max_tokens` in response parser | `response_parser.py:160` | 15 min |
| 11 | Fix disclaimer stripping regex (strip before whitespace collapse) | `response_parser.py:233` | 30 min |
| 12 | Fix duplicate-MPN data loss in `clone_requisition` | `requisition_service.py:128` | 30 min |
| 13 | Wrap vendor merge in single transaction | `vendor_merge_service.py:86` | 1 hr |
| 14 | Fix SQL f-string injection risk in `vendor_analytics.py` | `routers/vendor_analytics.py:185` | 1 hr |
| 15 | Fix `has_price_below_target` misleading variable | `search_service.py:765` | 15 min |
| 16 | Fix hardcoded `source="brokerbin"` in contact propagation | `search_service.py:1104` | 30 min |
| 17 | Add migration: indexes on `User.role`, `User.token_expires_at`, `Sighting.created_at`, etc. | `alembic/versions/` | 2 hrs |
| 18 | Add migration: `material_cards.deleted_at` index | `alembic/versions/` | 30 min |
| 19 | Upgrade password hashing from SHA-256 to bcrypt | `startup.py:63` | 1 hr |

### Medium Priority (2-8 hours each)
| # | Action | Effort |
|---|--------|--------|
| 20 | Extract `_enrich_with_vendor_cards`, `get_activity`, `rfq_prepare` to services | 4-6 hrs |
| 21 | Extract `add_requirements` / `upload_requirements` logic to services | 4-6 hrs |
| 22 | Add `TimestampMixin` and Alembic migration for missing `updated_at` columns | 2-3 hrs |
| 23 | Fix `ondelete` on 5 critical FK columns (Alembic migration) | 1-2 hrs |
| 24 | Resolve dual contact storage (`VendorCard.contacts` JSON vs `VendorContact` table) | 3-4 hrs |
| 25 | Add unit tests for connector classes and `CircuitBreaker` | 4-6 hrs |
| 26 | Add N+1 query count assertions to key list endpoints | 2-3 hrs |
| 27 | Add cascade delete tests for `Requisition` → `Requirement` → `Sighting` chain | 1-2 hrs |
| 28 | Add GitHub Actions CI (pytest + ruff + Alembic drift check) | 2-3 hrs |
| 29 | Refactor `main.py` to <100 lines (extract seed, health, CSP logic) | 2-3 hrs |
| 30 | Replace `backref=` with `back_populates` across all models | 2 hrs |

---

## Session Close Checklist

- **Migration required:** No schema changes in this review session (review only)
- **STABLE.md updated:** No — see CFG-7 for recommended additions
- **Tests touched:** None modified
- **Tech debt noted:** See all items above, especially BUG-1 through BUG-7 and ARCH-1 through ARCH-7
