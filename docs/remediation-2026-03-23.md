# Full-Stack Remediation Audit — 2026-03-23

## Status: FINDINGS COLLECTED, FIXES NOT YET APPLIED

All audit streams completed except test coverage (still running).
Skills/agents/hooks committed and deployed (commit `d159972f`).

---

## Audit Results Summary

| Stream | Tool | Result |
|--------|------|--------|
| Ruff linting | python3 -m ruff | PASS — 0 issues |
| Pytest | python3 -m pytest | PASS — 8,544 passed, 0 failed, 9 skipped |
| Mypy | python3 -m mypy | 2,198 errors (95% SQLAlchemy Column[X] noise, ~5 real bugs) |
| Bandit | python3 -m bandit | 7 medium, 0 high (1 real: mktemp race condition) |
| Silent failure hunter | PR toolkit subagent | 3 critical, 6 high, 8 medium |
| Code quality reviewer (feature-dev) | Subagent | 3 critical, 6 important |
| Security engineer | New agent | 4 medium, 2 low-medium |
| Code reviewer | New agent | 5 critical/high, 7 medium, 4 low |
| Test coverage audit | New agent | PENDING (was still running) |

---

## CRITICAL / HIGH FIXES NEEDED

### From Silent Failure Hunter

1. **auto_attribution_service.py:163** — Silent `except Exception: return {}` with zero logging
2. **email_mining.py:119** — `except Exception` catches all errors, not just IntegrityError for duplicate keys
3. **search_service.py:132,146** — Redis errors silently swallowed with `pass`
4. **email_service.py:620** — Batch commit failure returns empty list (200 status), hiding data loss
5. **email_service.py:1181** — Auto-create draft offers swallows all errors at warning level
6. **lifecycle_jobs.py:75** — Missing `raise` after exception (inconsistent with all other jobs)
7. **enrichment_service.py:304,343,404,439** — Overly broad `except Exception` on provider calls
8. **enrichment_service.py:326** — Non-200 from Explorium contacts returns empty with no logging
9. **routers/emails.py:45,72,99** — Returns 200 with error payload instead of proper HTTP status

### From Code Quality Reviewer (feature-dev)

1. **dependencies.py:58** — Agent user not seeded; valid API key silently fails with misleading 401
2. **routers/error_reports.py:305-488** — Trouble ticket read/update/analyze has no admin gate
3. **services/crm_service.py:18** — Race condition in `next_quote_number` (no locking)
4. **search_service.py:207-239** — New sync session inside async function; rollback corrupts state
5. **routers/rfq.py:49-55** — BUYER role bypasses requisition scope enforcement
6. **routers/error_reports.py:280** — Path traversal guard missing separator check
7. **database.py:76-81** — `get_db()` missing `db.rollback()` on exception
8. **routers/auth.py:137** — Raw string `"admin"` instead of `UserRole.ADMIN`
9. **search_service.py:82-113** — Redis connection failure permanently disables search cache

### From Security Engineer

1. **htmx_views.py:3028,7733,7759 + requisitions2.py:280** — Reflected XSS via unescaped exception messages in HTMLResponse
2. **template_env.py:101-148** — Email sanitizer allows `<img src>` (tracking pixels)
3. **main.py:262-270** — CSRF exemptions on authenticated data-mutation endpoints
4. **template_env.py:140-147** — Reverse tabnabbing via `<a target>` without `rel="noopener"`

### From Code Reviewer (new agent)

1. **connectors/sources.py:147** — `raise last_err` can raise `None`
2. **connectors/sources.py:46-75** — Race condition in module-level mutable dicts (_breakers, _connector_semaphores)
3. **services/prospect_free_enrichment.py:192 + routers/prospect_suggested.py:335** — Nested DB sessions cause stale reads
4. **services/prospect_free_enrichment.py:295** — Recursive session nesting in batch
5. **jobs/eight_by_eight_jobs.py:179** — Invalid requisition statuses in filter (`"open"`, `"in_progress"` don't exist)
6. **jobs/task_jobs.py:27** — Invalid statuses `"open"` and `"rfq_sent"` in filter
7. **Multiple jobs files** — Raw status strings instead of StrEnum constants
8. **email_mining.py** — Legacy `db.query(Model).get(id)` (6 occurrences)
9. **connectors/mouser.py:95** — `import re` inside for-loop
10. **email_mining.py:332** — Deprecated `asyncio.get_event_loop()` check
11. **jobs/knowledge_jobs.py:59-161** — Missing `db.rollback()` before close on exception

### From Bandit

1. **services/tagging_ai_triage.py:229** — `tempfile.mktemp()` is insecure (race condition), use `NamedTemporaryFile`

### From Mypy (real bugs only, not Column[X] noise)

1. **htmx_views.py:8142** — `convert_proactive_offer` doesn't exist (attr-defined)
2. **htmx_views.py:8704** — `.desc`/`.asc` on `object` type (wrong column reference)
3. **main.py:103** — LoguruIntegration expects `int` level, getting `str`

---

## MEDIUM / LOW FIXES (also fix)

- Filter JSON parsing returns empty dict without logging (htmx/_shared.py:54)
- Requisition state transition logging failure swallowed (requisition_state.py:59)
- Search service `_run_one` catches too broadly (search_service.py:855)
- Enrichment orchestrator uniform broad catch pattern (enrichment_orchestrator.py)
- http_client.py shutdown RuntimeError silently swallowed
- Requirement status `on_rfq_sent` swallows ValueError (requirement_status.py:91)
- claude_client.py `safe_json_parse` swallows JSONDecodeError silently
- Password login can be enabled in production via env var (auth.py:171)
- Unauthenticated page routes use inline auth check (htmx_views.py:155)

---

## TEST WARNINGS TO CLEAN UP

- Unawaited coroutines in tests (`_background_enrich_vendor`, `_enrich_cards`, `run_ai_gate`)
- Legacy `Query.get()` in test_unified_score_service.py:322
- SAWarning identity map conflict in test_search_service.py

---

## NEXT STEPS

1. Wait for test coverage audit results (may already be done)
2. Fix ALL findings above — start with critical/high, then medium/low
3. Run full test suite after fixes
4. Run mypy/bandit/ruff to verify clean
5. Commit as single remediation commit
6. Deploy
