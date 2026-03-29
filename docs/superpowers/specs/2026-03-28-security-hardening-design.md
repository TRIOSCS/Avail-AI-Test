# Security Hardening + Test Isolation + htmx Decomposition — Design Spec

**Date:** 2026-03-28
**Scope:** 3 remaining security fixes, 5 data model fixes, parallel test isolation, htmx_views.py decomposition

---

## Phase 1: Security Fixes (3 remaining — 8 of 11 already fixed)

Prior audits resolved: timing attack (#1), SQL injection (#2), FK failure (#3), password hashing (#4), query validation (#6), rate limiting (#8), retry-after cap (#9), XSS (#10).

### Fix A: Reduce broad `except Exception` (479 instances across 127 files)

Focus on top offenders first. Replace with specific exception types. Ensure `logger.exception()` used for unexpected errors.

**Approach:** Categorize the 479 instances:
- Intentional catch-all with proper logging → leave as-is with `# noqa` comment
- Missing specific types → replace with `(ValueError, TypeError, KeyError)` etc.
- Silent swallowing → add `logger.exception()`

### Fix B: Mouser API key in URL params
- **File:** `app/connectors/mouser.py:47`
- **Current:** `params={"apiKey": self.api_key}` — key appears in access logs, Sentry
- **Fix:** Mouser API requires key as URL param (no header option). Add Sentry `before_send` scrubbing for `apiKey=` in URLs. Also mask in httpx request logging.

### Fix C: Token refresh blocks HTTP requests
- **File:** `app/dependencies.py:164`
- **Current:** `await refresh_user_token(user, db)` called synchronously in request handler
- **Fix:** Move token refresh to APScheduler background job with tighter buffer (refresh 5 min before expiry instead of on-demand). Dependency just checks token validity; if expired and no refresh available, return 401.

---

## Phase 2: Data Model Fixes (5 issues, via Alembic migrations)

### Migration 1: Add index on material_cards.deleted_at (HIGH)
```python
op.create_index("ix_material_cards_deleted_at", "material_cards", ["deleted_at"])
```

### Migration 2: Add unique constraint on site_contacts (MEDIUM)
```python
op.create_unique_constraint("uq_site_contacts_site_email", "site_contacts", ["customer_site_id", "email"])
```

### Migration 3: Make count columns non-nullable (LOW)
```python
op.alter_column("companies", "site_count", nullable=False, server_default="0")
op.alter_column("companies", "open_req_count", nullable=False, server_default="0")
```

### Issue 13 (back_populates) and Issue 14 (cascade rules): Deferred
These require careful review of 101 relationships and buy plan cascade behavior. Better as a separate focused spec.

---

## Phase 3: Parallel Test Isolation (~30 failures)

Research agent still running. Will update with findings. Common fixes:
- Add `autouse` fixture to reset module-level caches between tests
- Use unique IDs in test data (UUID-based) to prevent collision
- Ensure each test cleans up its own DB state

---

## Phase 4: htmx_views.py Decomposition (9,892 lines → 22 modules)

### Target Structure

```
app/routers/
├── htmx_shared.py        # 143 lines — helpers: _vite_assets, _base_ctx, _safe_int, etc.
├── htmx_page.py          # 112 lines — universal /v2/* page loader
└── htmx/
    ├── __init__.py        # Router combiner
    ├── trouble_tickets.py # 84 lines, 3 endpoints (LOWEST risk)
    ├── knowledge.py       # 68 lines, 2 endpoints
    ├── settings.py        # 161 lines, 5 endpoints
    ├── search.py          # 52 lines, 3 endpoints
    ├── emails.py          # 130 lines, 3 endpoints
    ├── follow_ups.py      # 130 lines, 4 endpoints
    ├── dashboard.py       # 168 lines, 8 endpoints
    ├── prospecting.py     # 365 lines, 7 endpoints
    ├── materials.py       # 419 lines, 12 endpoints
    ├── sourcing.py        # 742 lines, 9 endpoints
    ├── vendors.py         # 725 lines, 14 endpoints
    ├── proactive.py       # 472 lines, 8 endpoints
    ├── customers.py       # 1,017 lines, 16 endpoints
    ├── quotes.py          # 390 lines, 16 endpoints
    ├── buy_plans.py       # 364 lines, 10 endpoints
    ├── parts.py           # 1,149 lines, 20 endpoints
    └── requisitions.py    # 2,614 lines, 42 endpoints
```

### Extraction Order (by risk)

| Phase | Modules | Lines | Risk |
|-------|---------|-------|------|
| 1. Foundation | htmx_shared.py + htmx_page.py | 255 | LOW |
| 2. Isolated | trouble_tickets, knowledge, settings, search | 365 | LOWEST |
| 3. Low-coupling | emails, follow_ups, dashboard, prospecting | 793 | LOW |
| 4. Medium | materials, sourcing, vendors | 1,886 | MEDIUM |
| 5. High-coupling | quotes + buy_plans, proactive, customers | 2,243 | HIGH |
| 6. The monolith | requisitions + parts | 3,763 | HIGHEST |

### Cross-domain Dependencies
- `quotes ↔ buy_plans` — bidirectional (build buy plan from quote, add offer to quote)
- `requisitions ↔ parts` — bidirectional (tab switching, UI state)
- `proactive → quotes` — one-way (convert match to quote)
- All → `htmx_shared` — shared helpers

---

## Execution Strategy

**Phase 1 (security) + Phase 2 (data model)** can run in parallel — independent files.

**Phase 3 (test isolation)** — depends on Phase 1-2 landing cleanly.

**Phase 4 (htmx decomposition)** — runs after Phases 1-3 since security fixes touch htmx_views.py. Each extraction phase is a separate commit with full test verification.

### Estimated Scope
- Phase 1: 3 security fixes (~2 hours)
- Phase 2: 3 migrations (~1 hour)
- Phase 3: ~30 test fixes (~2 hours)
- Phase 4: 6 extraction phases (~8 hours across multiple sessions)
