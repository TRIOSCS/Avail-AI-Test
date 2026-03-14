# AvailAI Code Review — March 2026

## Executive Summary

AvailAI is a **well-structured, production-grade** electronic component sourcing platform. The codebase is large (~740 Python files, 40K lines of services, 81 ORM models, 60 Alembic migrations) and demonstrates solid architecture. The review below identifies areas for improvement ordered by priority.

**Overall Grade: B+** — Strong foundation with a few areas needing attention before scaling.

---

## Critical Issues (Fix Soon)

### 1. Agent API Key Timing Attack Vulnerability
**File:** `app/dependencies.py:50-55`
**Severity:** CRITICAL

The agent API key comparison uses `==` (string equality), which is vulnerable to timing attacks:
```python
if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
```

**Fix:** Use `secrets.compare_digest()` for constant-time comparison. Also add audit logging when the agent key is used.

### 2. SQL Injection Risk in `vendor_analytics.py`
**File:** `app/routers/vendor_analytics.py:185-238`
**Severity:** HIGH

The `mpn_filter` variable is inserted into an f-string SQL query via `sqltext(f"...")`. While the filter string itself uses parameterized `:mpn_pattern`, the f-string interpolation of `{mpn_filter}` means the query structure changes based on runtime input. Currently safe (hardcoded string), but fragile — a future developer could introduce injection.

**Fix:** Refactor to always include the WHERE clause and use a parameter that's either `%` (match all) or the actual filter value, eliminating the f-string entirely.

### 3. Silent FK Reassignment Failure in Vendor Merge
**File:** `app/services/vendor_merge_service.py:86-94`
**Severity:** HIGH

FK reassignment failures during vendor merge are silently caught with `except Exception` and logged at `debug` level. If one FK table fails, related records become orphaned with no rollback.

**Fix:** Wrap the entire merge in a single transaction. Raise on failure instead of silently continuing.

### 4. Password Hashing Uses SHA-256
**File:** `app/startup.py:63-80`
**Severity:** MEDIUM

Default user creation uses `hashlib.sha256` for password hashing. SHA-256 is not suitable for passwords — it's too fast and vulnerable to brute force.

**Fix:** Switch to `bcrypt` or `argon2`. Only affects the `ENABLE_PASSWORD_LOGIN` code path, so limited blast radius.

### 5. Broad Exception Catching in Services
**Severity:** MEDIUM

92 instances of `except Exception` across 30 service files. Key offenders:
- `enrichment_orchestrator.py` (8), `buyplan_notifications.py` (6), `nc_worker/session_manager.py` (6)

**Fix:** Replace with specific exception types. Where broad catch is intentional, ensure `logger.exception()` is used.

### 6. Missing Input Validation on Query Parameters
**File:** `app/routers/vendor_analytics.py:42-44`
**Severity:** MEDIUM

`int()` conversion on query params with no try/except — non-numeric input causes 500 instead of 400.

**Fix:** Use Pydantic `Query()` params with type constraints.

### 7. Token Refresh Blocks HTTP Requests
**File:** `app/dependencies.py:130-166`
**Severity:** MEDIUM

`require_fresh_token` calls async token refresh inside the HTTP request handler, causing latency spikes when tokens need refresh.

**Fix:** Consider background refresh via scheduler with a tighter buffer window.

### 8. No Rate Limiting on Password Login
**File:** `app/routers/auth.py:216`
**Severity:** HIGH

The password login endpoint has no brute-force protection. While `ENABLE_PASSWORD_LOGIN` is off by default, when enabled there's no slowapi decorator or lockout mechanism.

**Fix:** Add `@limiter.limit("5/minute")` to the password login endpoint.

### 9. Retry-After Header Not Capped
**File:** `app/connectors/sources.py:153`
**Severity:** MEDIUM

`max(float(header), 1.0)` has no upper bound. A malicious or buggy API returning `Retry-After: 999999` would block the connector for 11+ days.

**Fix:** Cap at 300 seconds: `min(max(float(header), 1.0), 300.0)`.

### 10. XSS via `javascript:` URLs in HTML Sanitizer
**File:** `app/static/app.js:458`
**Severity:** MEDIUM

The `sanitizeRichHtml` function whitelists `href` attributes on `<a>` tags but doesn't validate the URL scheme. `<a href="javascript:alert(1)">` bypasses CSP in some browsers.

**Fix:** Validate that `href` starts with `http://`, `https://`, or `/`.

### 11. API Keys Logged in URL Parameters
**File:** `app/connectors/mouser.py:42`
**Severity:** MEDIUM

Mouser connector sends API key as a URL query parameter (`params={"apiKey": self.api_key}`), which appears in access logs, error traces, and Sentry events.

**Fix:** Switch to header-based auth if supported, or mask keys in log output.

---

## Architecture Strengths

1. **Clean layer separation** — Thin routers → services → models. No business logic in route handlers.
2. **Alembic discipline** — 60 migrations, strict rules enforced in CLAUDE.md. No raw DDL in startup.
3. **Auth middleware** — Well-designed dependency chain: `get_user` → `require_user` → `require_buyer/admin/sales`.
4. **Agent API key auth** — Service-to-service auth via `x-agent-key` header is a good pattern.
5. **UTC everywhere** — `UTCDateTime` type decorator + event listener ensures timezone consistency.
6. **Database tuning** — Connection pool (20+40 overflow), statement timeout (30s), lock timeout (5s), `pool_pre_ping`.
7. **Security headers** — CSRF, CSP, GZip, Sentry scrubbing of sensitive fields.
8. **Rate limiting** — slowapi with configurable limits (120/min default, 20/min for search).
9. **Comprehensive config** — 80+ settings with validators, CSV parsing, fail-fast on bad values.
10. **Test suite** — 314 test files, 8,605 test functions using in-memory SQLite with auth overrides.

---

## Data Model & Schema Issues

### 12. Missing Index on `material_cards.deleted_at`
**File:** `app/models/intelligence.py:49`
**Severity:** HIGH

Soft-delete queries (`WHERE deleted_at IS NULL`) run on every MaterialCard lookup without an index — full table scan.

**Fix:** Add migration with `Index("ix_material_cards_deleted_at", "deleted_at")`.

### 13. 101 Relationships Missing `back_populates`
**Severity:** MEDIUM

Many relationships are one-way (no inverse). Worst offenders: `offers.py` (4 User FKs), `buy_plan.py` (6 User FKs), `strategic.py` (2 using deprecated `backref=`).

**Fix:** Replace `backref=` with explicit `back_populates` on both sides. Prioritize models used in list endpoints.

### 14. Inconsistent Cascade Rules on BuyPlanLine
**File:** `app/models/buy_plan.py:209-210`
**Severity:** MEDIUM

`requirement_id` and `offer_id` use `ondelete="SET NULL"` — when a Requirement is deleted, orphaned BuyPlanLine rows remain with NULL FK. Should be CASCADE or handled by application cleanup.

### 15. Missing Unique Constraint on `site_contacts`
**File:** `app/models/crm.py:149-188`
**Severity:** MEDIUM

No unique constraint on `(customer_site_id, email)` — duplicate contacts per site are allowed.

### 16. Denormalized Count Columns Allow NULL
**File:** `app/models/crm.py:54-55`
**Severity:** LOW

`site_count` and `open_req_count` have `default=0` and `server_default="0"` but are nullable. Add `nullable=False`.

---

## Test & Schema Gaps

### 17. No N+1 Query Tests
**Severity:** MEDIUM

Zero tests verify eager-loading behavior. List endpoints for Offers, Requisitions, and ActivityLog likely have N+1 patterns under load.

**Fix:** Add pytest hook using `sqlalchemy.event` to assert max query count per endpoint.

### 18. No Cascade Delete Tests
**Severity:** MEDIUM

No tests verify that deleting a Requisition properly cascades through Requirements → Sightings → Offers → Quotes.

### 19. Schemas Missing `from_attributes=True`
**Severity:** LOW

Only 6 of 24 Pydantic schema files have `ConfigDict(from_attributes=True)`. ORM → Pydantic serialization breaks silently without it.

### 20. No Alembic Downgrade Tests
**Severity:** MEDIUM

76 migrations have `downgrade()` functions but none are tested. Production rollbacks could fail silently.

---

## Medium Priority Improvements

### 21. Test Execution Verification
314 test files with 8,605 test functions exist. Verify they pass:
```bash
pytest tests/ -x --timeout=60 -q
```

### 22. Service File Count is High (106 files)
Group into subdirectories by domain (sourcing, crm, rfq, enrichment, buyplan, intelligence). Some already exist (`ics_worker/`, `nc_worker/`).

### 23. No CI/CD Pipeline
No `.github/workflows/` found. Add GitHub Actions for pytest, ruff, and Alembic drift check.

### 24. Missing Backup/Restore Documentation
CLAUDE.md has deploy rules but no pre-migration backup procedure. Add `pg_dump` step before migrations.

---

## Low Priority / Tech Debt

### 25. MVP Mode Flag — DEFERRED
`mvp_mode: bool = True` disables several features (apollo_sync, enrichment, performance routers).
**Decision**: Keep as-is. The flag is actively used with a design spec (`docs/superpowers/specs/2026-03-13-mvp-strip-down-design.md`). Set `MVP_MODE=false` in env when ready to enable enrichment features.

### 26. Buy Plan V1 Deprecation — DEFERRED
`buy_plan_v1_enabled: bool = False` is defined but never checked. However, V1 `BuyPlan` model is still actively used by:
- `vendor_scorecard.py` and `vendor_score.py` for PO conversion rate history
- `buyplan_po.py` for PO verification on historical records
- `inventory_jobs.py` for auto-completing stuck V1 plans
**Decision**: Keep V1 code until all historical V1 plans reach terminal status. Migration 076 handles V1→V3 data migration. The V1 router already returns 410 Gone for mutations.

### 27. Redundant `index=True` on Unique Columns — FIXED
Removed redundant `index=True` from 5 columns that already have `unique=True` (SQLAlchemy auto-creates the index):
- `system_config.key`
- `intel_cache.cache_key`
- `material_cards.normalized_mpn`
- `offers.message_id` (InboundEmail)
- `vendor_cards.normalized_name`

---

## Recommended Next Steps (Priority Order)

### Quick Wins (< 1 hour each)
| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 1 | `secrets.compare_digest()` for agent API key | 15 min | Fixes timing attack |
| 2 | Rate limit password login endpoint | 15 min | Prevents brute force |
| 3 | Cap Retry-After header at 300s | 15 min | Prevents connector lockup |
| 4 | Validate `href` schemes in JS sanitizer | 30 min | Closes XSS vector |
| 5 | Pydantic Query validation on raw int() params | 30 min | Prevents 500 errors |

### High Priority (1-2 hours each)
| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 6 | Fix SQL f-string in `vendor_analytics.py` | 1 hour | Eliminates injection risk |
| 7 | Wrap vendor merge in proper transaction | 1 hour | Prevents data orphaning |
| 8 | Add index on `material_cards.deleted_at` | 30 min | Soft-delete performance |
| 9 | Upgrade password hashing to bcrypt/argon2 | 1 hour | Security hardening |
| 10 | Mask API keys in connector log output | 1 hour | Prevents credential leaks |
| 11 | Add unique constraint on site_contacts email | 30 min | Data integrity |

### Medium Priority (2+ hours each)
| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 12 | Add GitHub Actions CI (pytest + ruff) | 2-3 hours | Prevents regressions |
| 13 | Verify 8,605 tests pass end-to-end | 1 hour | Confidence baseline |
| 14 | Add N+1 query tests to list endpoints | 2 hours | Performance safety |
| 15 | Audit FK indexes across all 42 tables | 2 hours | Query performance |
| 16 | Replace `backref=` with `back_populates` | 2 hours | ORM correctness |
| 17 | Narrow `except Exception` in top 5 files | 2 hours | Debuggability |
| 18 | Add backup/restore docs to CLAUDE.md | 30 min | Operational safety |
| 19 | Organize services into subdirectories | 3-4 hours | Developer experience |

---

## What's Working Well

- The Alembic migration discipline is exceptional for a project this size
- Auth middleware is clean and well-documented
- Config validation catches bad values at startup
- Sentry integration with sensitive data scrubbing is production-ready
- The connector pattern (parallel search via `asyncio.gather()`) is well-designed
- Loguru usage is consistent (no `print()` calls found)
- Test coverage is broad (314 files, 8,605 functions covering routers, services, connectors, schemas)
- Test isolation is excellent: fresh event loop, auto-rollback, FK enforcement via PRAGMA
- 20+ reusable fixtures for User roles (buyer, seller, admin, manager, trader)
- Type adapters handle SQLite/PostgreSQL incompatibilities (ARRAY→JSON, TSVECTOR→TEXT)
- Circuit breaker pattern in connectors prevents cascading failures
- Per-connector concurrency limits prevent API hammering
- OAuth state validation provides proper CSRF protection
- Password login uses PBKDF2-HMAC-SHA256 with 200K iterations (auth.py, not startup.py)
- Email mining has proper dedup via ProcessedMessage with savepoint protection
- No `eval()`, `Function()`, or open redirects in frontend code
- Delta query caching for incremental inbox sync (not full scans)
