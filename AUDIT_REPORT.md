# AvailAI Codebase Audit Report — Stage 1

**Date**: 2026-02-16
**Scope**: Full codebase audit (76 Python files, 37 models, 28 test files)
**Tools**: ruff, bandit, vulture, radon, detect-secrets, manual review

---

## Executive Summary

The codebase is well-structured with a clear FastAPI + SQLAlchemy layered architecture.
The audit identified **1 critical data-loss bug**, **4 high-priority performance issues**,
**4 high-priority stability issues**, and numerous medium-priority improvements.
All critical and high-priority issues have been fixed.

---

## Critical Bugs Found & Fixed

### 1. SQLAlchemy filter no-op (scheduler.py:159)
**Severity**: CRITICAL — silent data-loss behavior

```python
# BEFORE (always True — Python identity check, not SQL filter)
.filter(Requisition.last_searched_at is not None)

# AFTER (correct SQL IS NOT NULL filter)
.filter(Requisition.last_searched_at.isnot(None))
```

This caused the stale requisition filter to be a **complete no-op** — all requisitions
were treated as recently searched, meaning stale ones were never re-searched or archived.

### 2. Undefined variable (scheduler.py:548)
**Severity**: HIGH — NameError at runtime

```python
# BEFORE
mpn = (row.get("mpn") or "").strip().upper()
...
card = db.query(MaterialCard).filter_by(normalized_mpn=norm_mpn).first()  # NameError

# AFTER
card = db.query(MaterialCard).filter_by(normalized_mpn=mpn).first()
```

---

## High-Priority Fixes Applied

### Performance: N+1 Query Elimination

| File | Line | Issue | Fix |
|------|------|-------|-----|
| `crm.py` | 178 | COUNT query per site in list_companies | Pre-fetched all open req counts in single query |
| `rfq.py` | 377 | Requisition query per contact in follow-ups | Pre-fetched all req names with batch query |

### Performance: Unbounded Query Protection

| File | Line | Fix |
|------|------|-----|
| `requisitions.py` | 136 | Added `.limit(500)` to list_requisitions |
| `crm.py` | 168 | Added `.limit(500)` to list_companies |
| `crm.py` | 1624 | Added `.limit(200)` to list_buy_plans |
| `rfq.py` | 373 | Added `.limit(500)` to follow-ups query |

### Stability: Background Task Session Safety

| File | Lines | Issue | Fix |
|------|-------|-------|-----|
| `crm.py` | 1610 | `notify_buyplan_submitted` shared request-scoped session | Background task creates own `SessionLocal()` |
| `crm.py` | 1681 | `notify_buyplan_approved` shared request-scoped session | Background task creates own `SessionLocal()` |
| `crm.py` | 1712 | `notify_buyplan_rejected` shared request-scoped session | Background task creates own `SessionLocal()` |
| `crm.py` | 1751 | `verify_po_sent` shared request-scoped session | Background task creates own `SessionLocal()` |

When request-scoped DB sessions were passed to `asyncio.create_task()`, the session
could close before the background task finished, causing `DetachedInstanceError` or
stale reads.

### Stability: Auth Hardening (auth.py)

- Added `timeout=15.0` on Azure token exchange HTTP call
- Added `try/except httpx.HTTPError` around token exchange
- Changed `tokens["access_token"]` to `tokens.get("access_token")` with null check
- Added `timeout=10.0` on Graph `/me` HTTP call
- Added `try/except httpx.HTTPError` around Graph `/me` call
- Added status code check on Graph `/me` response

### Stability: Health Endpoint (main.py)

- Enhanced `/health` to check DB connectivity (returns `"degraded"` if DB unreachable)
- Uses FastAPI dependency injection for `get_db` (works correctly in test mode)

### Infrastructure: Connection Pool (database.py)

- Added `max_overflow=20` to SQLAlchemy engine (was using default of 10)
- Combined with existing `pool_size=10` and `pool_pre_ping=True`

---

## Medium-Priority Fixes Applied

### Code Quality: Bare Exception Handlers

Fixed 9 instances of bare `except:` → `except (ValueError, TypeError):` in:
- `connectors/digikey.py` — `_safe_int`, `_safe_float`
- `connectors/ebay.py` — `_safe_int`, `_safe_float`
- `connectors/mouser.py` — `_safe_int`, `_safe_float`
- `connectors/oemsecrets.py` — `_safe_int`, `_safe_float`
- `connectors/sourcengine.py` — `_safe_int`, `_safe_float`

### Code Quality: Ambiguous Variable Names

- `claude_client.py:248` — `l` → `line` in list comprehension
- `crm.py:752-764` — `l` → `entry` in sync log serialization

### Code Formatting

- 56 files reformatted by `ruff format` for consistent style

---

## Static Analysis Results

### Ruff (84 initial, 73 after autofix)
- **Fixed**: 9 bare excepts (E722), 2 ambiguous variables (E741), 11 auto-fixed
- **Accepted**: E402 (module-level import order) — `setup_logging()` must run before imports
- **False positives**: Pydantic `cls` parameters flagged by vulture (required by Pydantic)

### Bandit (security)
- **1 medium**: SQL injection in `acctivate_sync.py` — already parameterized, false positive
- **Low**: `hashlib.md5(usedforsecurity=False)` — correctly marked as non-security usage

### Radon (complexity)
- **Average**: C (19.2)
- **High complexity** (D/E/F): `scheduler.py` email mining loops, `email_service.py` delta query,
  `rfq.py` activity aggregation, `search_service.py` result dedup
- These are inherently complex business logic — no artificial simplification recommended

### detect-secrets
- **False positives only**: `main.py` default secret check (intentional), `search_service.py`
  connector name "oemsecrets" (not a secret)

---

## Known Issues (Deferred)

These are real issues but lower priority or require larger refactoring:

### N+1 Queries (scheduler background jobs)
- `scheduler.py:536` — MaterialCard query per row in stock import loop
- `scheduler.py:553` — MaterialVendorHistory query per row
- These run in background scheduler (not user-facing), so impact is lower

### Missing Pagination
Several list endpoints return up to 500 rows but lack cursor/offset pagination.
The `.limit()` additions prevent unbounded queries; full pagination can be added
when needed.

### LIKE Injection (low risk)
Some `ilike` queries use user input for search patterns. All instances now escape
`%` and `_` wildcards (e.g., `requisitions.py:129`, `crm.py:166`). The remaining
risk is performance-only (no data exposure), since PostgreSQL LIKE is read-only.

---

## Test Results

```
350 passed, 0 failed (59.21s)
```

All existing tests pass after audit fixes.

---

## Files Modified

| File | Changes |
|------|---------|
| `app/scheduler.py` | Critical `.isnot(None)` fix, undefined var fix |
| `app/database.py` | `max_overflow=20` |
| `app/main.py` | Health endpoint with DB check, proper DI |
| `app/routers/auth.py` | Timeout, error handling, `.get()` safety |
| `app/routers/requisitions.py` | `.limit(500)` on list query |
| `app/routers/crm.py` | N+1 fix, `.limit()`, background session safety (4 sites) |
| `app/routers/rfq.py` | N+1 fix, `.limit(500)` on follow-ups |
| `app/connectors/digikey.py` | Bare except → typed except |
| `app/connectors/ebay.py` | Bare except → typed except |
| `app/connectors/mouser.py` | Bare except → typed except |
| `app/connectors/oemsecrets.py` | Bare except → typed except |
| `app/connectors/sourcengine.py` | Bare except → typed except |
| `app/utils/claude_client.py` | Ambiguous variable rename |
| 56 files | `ruff format` reformatting |
