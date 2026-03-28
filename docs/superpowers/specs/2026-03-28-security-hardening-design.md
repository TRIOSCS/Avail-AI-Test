# Security Hardening + Test Isolation + htmx Decomposition — Design Spec

**Date:** 2026-03-28
**Scope:** Fix 11 security issues, 5 data model issues, ~30 parallel test isolation issues, decompose htmx_views.py
**Status:** Research in progress — 3 agents gathering exact code locations

---

## Phase 1: Critical Security Fixes (11 issues from CODE_REVIEW.md)

### Issue 1: Agent API Key Timing Attack (CRITICAL)
- **File:** `app/dependencies.py:50-55`
- **Fix:** Replace `==` with `secrets.compare_digest()` + add audit logging

### Issue 2: SQL Injection Risk (HIGH)
- **File:** `app/routers/vendor_analytics.py:185-238`
- **Fix:** Eliminate f-string SQL, use parameterized query with `%` wildcard for match-all

### Issue 3: Silent FK Failure in Vendor Merge (HIGH)
- **File:** `app/services/vendor_merge_service.py:86-94`
- **Fix:** Single transaction wrapper, raise on failure instead of silent continue

### Issue 4: SHA-256 Password Hashing (MEDIUM)
- **File:** `app/startup.py:63-80`
- **Fix:** Switch to `bcrypt` or `argon2` (only affects `ENABLE_PASSWORD_LOGIN` path)

### Issue 5: Broad except Exception (MEDIUM)
- **92 instances across 30 files**
- **Fix:** Replace with specific exception types, ensure `logger.exception()` used

### Issue 6: Missing Input Validation (MEDIUM)
- **File:** `app/routers/vendor_analytics.py:42-44`
- **Fix:** Use Pydantic `Query()` params with type constraints

### Issue 7: Token Refresh Blocks Requests (MEDIUM)
- **File:** `app/dependencies.py:130-166`
- **Fix:** Background refresh via scheduler with tighter buffer window

### Issue 8: No Rate Limiting on Password Login (HIGH)
- **File:** `app/routers/auth.py:216`
- **Fix:** Add `@limiter.limit("5/minute")` decorator

### Issue 9: Retry-After Not Capped (MEDIUM)
- **File:** `app/connectors/sources.py:153`
- **Fix:** `min(max(float(header), 1.0), 300.0)`

### Issue 10: XSS via javascript: URLs (MEDIUM)
- **File:** `app/static/app.js:458`
- **Fix:** Validate `href` starts with `http://`, `https://`, or `/`

### Issue 11: API Keys in URL Parameters (MEDIUM)
- **File:** `app/connectors/mouser.py:42`
- **Fix:** Switch to header-based auth or mask keys in log output

---

## Phase 2: Data Model Fixes (5 issues from CODE_REVIEW.md)

### Issue 12: Missing Index on material_cards.deleted_at (HIGH)
- Add migration with `Index("ix_material_cards_deleted_at", "deleted_at")`

### Issue 13: 101 Relationships Missing back_populates (MEDIUM)
- Replace `backref=` with explicit `back_populates` on both sides
- Prioritize models used in list endpoints

### Issue 14: Inconsistent Cascade Rules on BuyPlanLine (MEDIUM)
- Review and fix cascade behavior for requirement_id and offer_id

### Issue 15: Missing Unique Constraint on site_contacts (MEDIUM)
- Add `UniqueConstraint("customer_site_id", "email")` + migration

### Issue 16: Denormalized Count Columns Allow NULL (LOW)
- Add `nullable=False` to site_count, open_req_count + migration

---

## Phase 3: Parallel Test Isolation (~30 failures)

Research agent investigating root causes. Common patterns:
- Shared module-level state (global variables, caches)
- Tests that INSERT specific IDs that collide
- Tests that rely on empty tables
- Tests that modify module-level configs

---

## Phase 4: htmx_views.py Decomposition (9,888 lines)

Research agent mapping endpoint groups and dependencies. Target: split into ~10 domain-specific modules under `app/routers/htmx/`.

---

## Execution Strategy

Phases 1-3 can run in parallel (independent). Phase 4 is the largest and should run after Phase 1-3 to avoid merge conflicts with security fixes in htmx_views.py.

**Awaiting research agent results to fill in exact code locations and decomposition map.**
