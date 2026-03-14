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
10. **Test suite** — 301 test files using in-memory SQLite with auth overrides.

---

## Medium Priority Improvements

### 4. Test Execution Verification
301 test files exist but I couldn't collect them (pytest timed out). Verify tests actually pass:
```bash
pytest tests/ -x --timeout=60 -q
```
If tests are slow or broken, that's a significant risk. Consider running a CI pipeline.

### 5. Service File Count is High (106 files)
The `app/services/` directory has grown to 106 files (~40K lines). While each file is focused, the sheer count makes discovery hard.

**Suggestion:** Group into subdirectories by domain:
```
services/
  sourcing/       # search, scoring, connectors
  crm/            # companies, contacts, activity
  rfq/            # email, RFQ, offers
  enrichment/     # Apollo, Hunter, orchestrator
  buyplan/        # buy plan v3, PO, notifications
  intelligence/   # email mining, proactive matching
```
Some subdirectories already exist (`ics_worker/`, `nc_worker/`, `search_worker_base/`).

### 6. Missing Index Audit
With 81 models and frequent queries, verify indexes exist on:
- All foreign key columns (SQLAlchemy doesn't auto-create them)
- Columns used in `filter_by()` / `WHERE` clauses
- `normalized_name`, `vendor_name_normalized`, `mpn_matched` (used in search/analytics)

Run in production:
```sql
SELECT schemaname, tablename, indexname FROM pg_indexes
WHERE schemaname = 'public' ORDER BY tablename;
```

### 7. No CI/CD Pipeline Visible
No `.github/workflows/`, `.gitlab-ci.yml`, or similar CI config found. Deployment is via manual `scripts/deploy.sh`.

**Recommendation:** Add GitHub Actions for:
- `pytest` on every PR
- `ruff` linting
- Alembic migration check (ensure no drift)

### 8. Frontend is Vanilla JS (10 files, 82 HTML templates)
The frontend (`app.js`, `crm.js`, plus 82 HTMX templates) is growing. The recent HTMX/Alpine.js migration (PR #35) is a good step. Continue this direction rather than adding more vanilla JS.

---

## Low Priority / Tech Debt

### 9. MVP Mode Flag
`mvp_mode: bool = True` in config disables Dashboard/Analytics, Enrichment, Teams, Task Manager. Clarify the roadmap: if these features are stable, flip to `False`. If not needed, remove the dead code.

### 10. Duplicate Config Pattern
`config.py` uses `str` fields with `model_validator` to convert CSV strings to lists. This works but is surprising. Consider documenting this more prominently or using a custom type.

### 11. Buy Plan V1 Deprecation
`buy_plan_v1_enabled: bool = False` exists alongside V3 code. If V1 is fully retired, remove its code paths to reduce maintenance burden.

### 12. Single TODO in Codebase
`app/routers/views.py:64` — `results = []  # TODO: aggregate search across requisitions, companies, vendors`. Either implement or remove if not planned.

---

## Recommended Next Steps (Priority Order)

| # | Action | Effort | Impact |
|---|--------|--------|--------|
| 1 | Use `secrets.compare_digest()` for agent API key | 15 min | Fixes timing attack |
| 2 | Fix SQL f-string pattern in `vendor_analytics.py` | 1 hour | Eliminates injection risk |
| 3 | Wrap vendor merge in proper transaction | 1 hour | Prevents data orphaning |
| 4 | Upgrade password hashing to bcrypt/argon2 | 1 hour | Security hardening |
| 5 | Add GitHub Actions CI (pytest + ruff) | 2-3 hours | Prevents regressions |
| 6 | Verify full test suite passes | 1 hour | Confidence baseline |
| 7 | Add Pydantic Query validation on raw int() params | 1 hour | Prevents 500 errors |
| 8 | Audit and add missing DB indexes | 2 hours | Performance |
| 9 | Narrow `except Exception` catches in top 5 files | 2 hours | Debuggability |
| 10 | Organize services into subdirectories | 3-4 hours | Developer experience |

---

## What's Working Well

- The Alembic migration discipline is exceptional for a project this size
- Auth middleware is clean and well-documented
- Config validation catches bad values at startup
- Sentry integration with sensitive data scrubbing is production-ready
- The connector pattern (parallel search via `asyncio.gather()`) is well-designed
- Loguru usage is consistent (no `print()` calls found)
- Test coverage is broad (301 files covering routers, services, connectors, schemas)
