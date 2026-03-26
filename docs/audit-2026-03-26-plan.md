# AvailAI Audit Fix Plan — 2026-03-26

## Simplifier Corrections Applied

3 findings **REJECTED** as false positives:
- **C3** (auto-attribution never commits) — `run_auto_attribution()` commits internally
- **C9** (vendor card returns uncommitted object) — code actually re-raises exception
- **C5-tagging** (tagging jobs swallow exceptions) — all 5 tagging jobs DO have `raise`

## Revised Finding Count: 41 → 25 actionable

| Severity | Original | Revised |
|----------|----------|---------|
| Critical | 9 | 2 (C1, C4) |
| High | 12 | 3 (H1, C2, C5-partial) |
| Medium | 20 | 14 |
| Low | 0 | 6 |
| Rejected | 0 | 3 |

---

## Phase 1: Critical + High Bugs (Batch A — 5 parallel agents)

### Agent A1: C1 — integrity_service.py savepoints
- File: `app/services/integrity_service.py:150-227`
- Change: Replace `db.rollback()` with `db.begin_nested()` savepoints per record in all 3 loops
- Test: `tests/test_integrity_service.py` — add `test_heal_savepoint_isolation`
- Risk: SQLite savepoints behave differently than PostgreSQL — verify on PG

### Agent A2: C2 — rfq.py role scope (QUICK WIN)
- File: `app/routers/rfq.py:49-55`
- Change: `if user.role != UserRole.SALES: return` (only restrict SALES)
- Test: `tests/test_routers_rfq.py` — parametrized test for BUYER/MANAGER/TRADER access

### Agent A3: C4 + C6 + C7 — core_jobs.py (same file, serialize)
- File: `app/jobs/core_jobs.py`
- C4: Guard `r.delete(lock_key)` with `if acquired` (line 141)
- C6: Set `user.m365_error_reason` in token refresh `except` (line 138-139)
- C7: Set `user.m365_error_reason` in inbox scan `except` (line 207-209)
- Test: `tests/test_jobs_core.py`

### Agent A4: C5 — 4 jobs missing `raise` (QUICK WIN)
- Files: `maintenance_jobs.py:127,183`, `health_jobs.py:60,78`
- Change: Add `raise` after `db.rollback()` in 4 locations
- Test: Extend existing exception propagation tests

### Agent A5: C8 — email sub-ops failure tracking
- File: `app/jobs/email_jobs.py:407-434`
- Change: Track sub-op failures in list, log partial-failure summary
- Test: `tests/test_email_jobs.py`

---

## Phase 2: Security (Batch B — 4 parallel agents)

### Agent B1: H1 — admin auth fix (QUICK WIN)
- File: `app/routers/htmx_views.py:8466,8486,8532`
- Change: `require_user` → `require_admin` (3 locations)

### Agent B2: H2 + H5 + H7 — template XSS + prompt cap + cache key (QUICK WINS)
- H2: `templates/.../preview_inquiry.html:59` — add `|sanitize_html`
- H5: `services/ai_service.py:262` — add `user_draft = user_draft[:12000]`
- H7: `services/ai_service.py:189` — include domain in cache key

### Agent B3: H3 + H4 — data_ops.py (same file)
- H3: Add `MAX_UPLOAD_BYTES` check before `file.read()` (lines 239, 352)
- H4: Move `dry_run` into request body with `confirm: bool`

### Agent B4: H6 — offers file type validation
- File: `app/routers/crm/offers.py:712`
- Change: Add `ALLOWED_OFFER_EXTENSIONS` check

---

## Phase 3: Data Integrity (Batch C — 4 parallel agents)

### Agent C1: H8 — AI contact field truncation
- File: `app/routers/ai.py:167-184`

### Agent C2: H9 — SALES requisition count filter (QUICK WIN)
- File: `app/routers/requisitions/core.py:67-91`

### Agent C3: H10 — stale data warning toast
- File: `app/routers/sightings.py:515-522`
- Add HX-Trigger header with warning toast on search refresh failure

### Agent C4: H11 + H12 — qty fallback flag + credential health
- H11: `services/sighting_aggregation.py:73-75` — `max()` fallback + flag
- H12: `services/credential_service.py:89-93` — health check flag

---

## Phase 4: Test Coverage (Batch D — 10 parallel agents)

All independent, no production risk:
- T1: `tests/test_auth_deps_unit.py` (new)
- T2: `tests/test_teams_action_tokens.py` (new)
- T3: `tests/test_routers_crm_clone.py` (new)
- T4: `tests/test_vendor_inquiry.py` (new)
- T5: `tests/test_services_credential.py` (new)
- T6: `tests/test_command_center.py` (new)
- T7: `tests/test_events_sse.py` (new)
- T8: Extend existing job test files
- T9: `tests/test_email_mining_patterns.py` (new)
- T10: Fix `test_agent_auth.py:56` assertion

---

## Phase 5: Type System (sequential gate)

1. **M5 first**: Alembic migration adding CheckConstraints (verify against prod data)
2. **M4**: Add `@validates` decorators on key models
3. **M1**: StrEnum sweep (after M5 committed)
4. **M2**: Typed response schemas
5. **M3**: AI response Pydantic models

---

## Phase 6: Remaining Medium

- M6: `startup.py` — re-raise critical seed failures
- M7: `htmx_views.py:1067` — `debug` → `warning`
- M8: `main.py` — CSP comment (deferred, Alpine.js requirement)
- M9: 4 routers — add `escape_like()` (already exists in `sql_helpers.py`)
- M10: `enrichment.py` — classify connector errors + retry transients

---

## Quick Wins (12 items, ~30 lines total)

| ID | File | Change |
|----|------|--------|
| C2 | `rfq.py:51` | 1 line |
| C4 | `core_jobs.py:141` | 2 lines |
| C5 | 4 job files | 4x `raise` |
| C6 | `core_jobs.py:139` | 3 lines |
| C7 | `core_jobs.py:208` | 4 lines |
| H1 | `htmx_views.py` | 3 word changes |
| H2 | `preview_inquiry.html:59` | 1 filter |
| H5 | `ai_service.py:262` | 1 line |
| H7 | `ai_service.py:189` | 1 line |
| H9 | `requisitions/core.py` | 3 lines |
| M6 | `startup.py:111` | 1 line |
| M7 | `htmx_views.py:1067` | 1 word |

---

## Parallelization Summary

| Phase | Parallel Agents | Est. Files Changed |
|-------|----------------|-------------------|
| 1 — Critical | 5 | 6 source + 5 test |
| 2 — Security | 4 | 5 source + 1 template |
| 3 — Data Integrity | 4 | 6 source |
| 4 — Tests | 10 | 9 new test files |
| 5 — Types | Sequential then 4 | 6+ source + 1 migration |
| 6 — Medium | 5 | 7 source |

## Key Risks
1. C1 savepoints: SQLite vs PostgreSQL behavior difference
2. C2 role change: test with real non-admin users post-deploy
3. H4 breaking change: search templates for `?dry_run=false` calls
4. H11 return type change: grep all callers of `estimate_unique_qty`
5. M5 migration: dry-run against prod DB snapshot first
