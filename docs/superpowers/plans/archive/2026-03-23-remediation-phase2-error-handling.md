# Phase 2: Error Handling & Observability Remediation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix ~130 error handling issues — silent exception swallowing, wrong log levels, missing job decorators, webhook failure masking, and inconsistent error response format.

**Architecture:** Batch similar fixes into logical commits. No migrations or architecture changes. All fixes are log-level upgrades, missing rollbacks, or adding a global exception handler.

**Tech Stack:** Python 3.12, FastAPI, Loguru, APScheduler

**Spec:** `docs/superpowers/specs/2026-03-23-code-remediation-design.md` (Phase 2)

**Detailed checklists from mapping agents:**
- 42 except-Exception-pass findings (PASS/SILENT_RETURN/LOW_LOG_LEVEL)
- 83 DEBUG-level error log findings across 41 files

---

### Task 1: Fix silent exception swallowing — `except Exception: pass` (2.1)

**Files:**
- Modify: `app/routers/htmx_views.py:2922-2923`
- Modify: `app/connectors/email_mining.py:299-300,329-330,543-544`
- Modify: `app/jobs/core_jobs.py:203,238`

- [ ] **Step 1: Fix htmx_views.py Redis cache pass**

At line 2922, replace `pass` with `logger.warning("Redis cache lookup failed for search %s", search_id, exc_info=True)`.

- [ ] **Step 2: Fix email_mining.py date parse passes (2 locations)**

At lines 299-300 and 329-330, replace `pass` with `logger.debug("Failed to parse receivedDateTime for message", exc_info=True)`.

- [ ] **Step 3: Fix email_mining.py flush pass**

At line 543, after `self.db.rollback()`, add `logger.error("Failed to flush email mining dedup records", exc_info=True)`.

- [ ] **Step 4: Fix core_jobs.py passes**

At line 203, add `logger.warning("Inbox scan timeout commit failed", exc_info=True)`.
At line 238, add `logger.debug("Batch results cleanup rollback", exc_info=True)`.

- [ ] **Step 5: Run affected tests**

Run: `TESTING=1 python3 -m pytest tests/test_main.py tests/test_routers_crm.py -v --timeout=30`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add app/routers/htmx_views.py app/connectors/email_mining.py app/jobs/core_jobs.py
git commit -m "fix: replace except-Exception-pass with logging in 6 locations

Silent exception swallowing in Redis cache, email date parsing, email
flush, and inbox scan timeout now logged for observability."
```

---

### Task 2: Fix silent return patterns (2.2)

**Files:**
- Modify: `app/services/health_monitor.py:91`
- Modify: `app/services/auto_dedup_service.py:199,213`
- Modify: `app/services/contact_intelligence.py:210`
- Modify: `app/services/global_search_service.py:44`

- [ ] **Step 1: Add logging to each silent return**

For each file, add `logger.warning(...)` or `logger.error(...)` before the `return None/False/{}`:

- `health_monitor.py:91`: `logger.error("Failed to create connector for source '%s'", source.name, exc_info=True)`
- `auto_dedup_service.py:199`: `logger.warning("AI confirm vendor merge failed for '%s'", name_a, exc_info=True)`
- `auto_dedup_service.py:213`: `logger.warning("AI confirm company merge failed", exc_info=True)`
- `contact_intelligence.py:210`: `logger.warning("_run_sync_helper failed", exc_info=True)`
- `global_search_service.py:44`: `logger.debug("AI search cache read failed", exc_info=True)`

- [ ] **Step 2: Run tests**

Run: `TESTING=1 python3 -m pytest tests/ --tb=line -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/services/health_monitor.py app/services/auto_dedup_service.py app/services/contact_intelligence.py app/services/global_search_service.py
git commit -m "fix: add logging to 5 silent-return exception handlers

Health monitor, auto-dedup AI confirm, contact intelligence, and
search cache now log errors instead of silently returning defaults."
```

---

### Task 3: Upgrade DEBUG→WARNING in batch 1 — routers (2.3a)

**Files (routers only — 20 locations):**
- Modify: `app/routers/crm/offers.py:379,389,422,439,492,505`
- Modify: `app/routers/requisitions/requirements.py:222,226,393,428,437,450,465,486,641,662,727`
- Modify: `app/routers/htmx_views.py:1056,2786`
- Modify: `app/routers/rfq.py:648,650`

- [ ] **Step 1: Upgrade all logger.debug to logger.warning in router except blocks**

For each location, change `logger.debug(` to `logger.warning(` — keep the message and `exc_info=True` unchanged.

- [ ] **Step 2: Run router tests**

Run: `TESTING=1 python3 -m pytest tests/test_routers_crm.py tests/test_routers_requisitions.py tests/test_routers_rfq.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/routers/crm/offers.py app/routers/requisitions/requirements.py app/routers/htmx_views.py app/routers/rfq.py
git commit -m "fix: upgrade 20 router error logs from DEBUG to WARNING

Offer side-effects, requirement enqueue/search/tag failures, and RFQ
contact lookup errors were invisible in production at LOG_LEVEL=INFO."
```

---

### Task 4: Upgrade DEBUG→WARNING in batch 2 — services (2.3b)

**Files (services — 50+ locations across ~25 files):**

Key files: `credential_service.py` (3→ERROR), `enrichment.py` (3), `contact_intelligence.py` (7), `buyplan_workflow.py` (1), `signature_parser.py` (3), `website_scraper.py` (2), `email_service.py` (4), `enrichment_service.py` (2), `tagging_nexar.py` (1), `prospect_free_enrichment.py` (2), `requisition_state.py` (1), `global_search_service.py` (1), `sighting_aggregation.py` (1), `health_service.py` (1).

- [ ] **Step 1: Upgrade credential_service.py to ERROR (3 locations)**

Lines 92, 114, 151: Change `logger.debug` to `logger.error` — these are crypto failures.

- [ ] **Step 2: Upgrade remaining services to WARNING**

For all other service files listed above, change `logger.debug(` to `logger.warning(` in except blocks.

- [ ] **Step 3: Run service tests**

Run: `TESTING=1 python3 -m pytest tests/ --tb=line -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/services/ app/enrichment_service.py app/email_service.py
git commit -m "fix: upgrade 50+ service error logs from DEBUG to WARNING/ERROR

Connector failures, AI call errors, signature parse failures, cache
write errors, and credential decryption failures now visible in
production logs. Credential failures elevated to ERROR."
```

---

### Task 5: Upgrade DEBUG→WARNING in batch 3 — jobs, cache, connectors, database (2.3c)

**Files:**
- Modify: `app/cache/decorators.py:107,122`
- Modify: `app/cache/intel_cache.py:79,96,111,147,158,182`
- Modify: `app/connectors/email_mining.py:350,712`
- Modify: `app/connectors/ai_live_web.py:124` (INFO→WARNING)
- Modify: `app/database.py:73`
- Modify: `app/jobs/email_jobs.py:576,771`
- Modify: `app/jobs/inventory_jobs.py:276`
- Modify: `app/routers/auth.py:154`
- Modify: `app/routers/sources.py:611,698`

- [ ] **Step 1: Upgrade all to WARNING**

Change `logger.debug(` to `logger.warning(` in each except block. For `ai_live_web.py:124`, change `logger.info` to `logger.warning`.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 python3 -m pytest tests/ --tb=line -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/cache/ app/connectors/ app/database.py app/jobs/ app/routers/auth.py app/routers/sources.py
git commit -m "fix: upgrade 20 cache/connector/job/db error logs from DEBUG to WARNING

Redis invalidation, cache read/write, email mining AI classification,
timezone handling, flush conflicts, and connector errors now visible
in production logs."
```

---

### Task 6: Fix webhook returning 200 on failure (2.4)

**Files:**
- Modify: `app/routers/v13_features/activity.py:69-71`
- Test: `tests/test_routers_v13.py`

- [ ] **Step 1: Read current code**

Read `app/routers/v13_features/activity.py:60-75`.

- [ ] **Step 2: Fix: raise HTTPException on failure**

```python
except Exception:
    logger.exception("Webhook notification processing failed")
    raise HTTPException(500, "Processing failed")
# Remove or move the return {"status": "accepted"} to before the except
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 python3 -m pytest tests/test_routers_v13.py -v`
Expected: All pass (may need to update test expectations if tests mock the processing)

- [ ] **Step 4: Commit**

```bash
git add app/routers/v13_features/activity.py
git commit -m "fix: return 500 on webhook processing failure instead of 200

Microsoft Graph will retry failed webhooks. Previously returned 200
on exception, causing Graph to believe processing succeeded and skip
retries — silently losing activity notifications."
```

---

### Task 7: Add raise to jobs that swallow exceptions (2.5)

**Files:**
- Modify: `app/jobs/maintenance_jobs.py:57,77-80,100-103,205-206`
- Modify: `app/jobs/inventory_jobs.py:60-63,97-98`
- Modify: `app/jobs/offers_jobs.py:93-95`
- Modify: `app/jobs/task_jobs.py:93-95`
- Modify: `app/connectors/email_mining.py:543`

- [ ] **Step 1: Add `raise` after each `db.rollback()` in except blocks**

For each location, add `raise` as the last line of the except block, after `db.rollback()` and `logger.exception(...)`.

- [ ] **Step 2: Also add missing `db.rollback()` where absent**

Check each location — if `db.rollback()` is missing before the `raise`, add it.

- [ ] **Step 3: Run job tests**

Run: `TESTING=1 python3 -m pytest tests/ -k "job or maintenance or inventory or offers_job or task_job" -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/jobs/ app/connectors/email_mining.py
git commit -m "fix: re-raise exceptions in 10 background jobs

Jobs that caught exceptions without re-raising bypassed the
_traced_job decorator and Sentry alerting. All job exceptions
now properly rollback, log, and re-raise."
```

---

### Task 8: Add @_traced_job to 5 undecorated functions (2.6)

**Files:**
- Modify: `app/jobs/knowledge_jobs.py:45,162,167,172`
- Modify: `app/jobs/tagging_jobs.py:288`

- [ ] **Step 1: Add decorator to each function**

Add `@_traced_job` decorator above each function definition. Import it at the top of each file if not already imported.

- [ ] **Step 2: Run tests**

Run: `TESTING=1 python3 -m pytest tests/test_routers_knowledge.py tests/test_tagging_backfill.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/jobs/knowledge_jobs.py app/jobs/tagging_jobs.py
git commit -m "fix: add @_traced_job decorator to 5 undecorated job functions

knowledge_jobs (4 functions) and tagging_jobs (1 function) were
missing trace_id correlation and start/finish logging."
```

---

### Task 9: Add elapsed time logging to _traced_job (2.7)

**Files:**
- Modify: `app/scheduler.py:18-34`

- [ ] **Step 1: Read current decorator**

Read `app/scheduler.py:18-34`.

- [ ] **Step 2: Add timing**

```python
import time

def _traced_job(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        trace_id = uuid.uuid4().hex[:8]
        start = time.monotonic()
        logger.info("Job started: {} [{}]", func.__name__, trace_id)
        try:
            return await func(*args, **kwargs)
        except Exception:
            logger.exception("Job failed: {} [{}]", func.__name__, trace_id)
            raise
        finally:
            elapsed = time.monotonic() - start
            logger.info("Job finished: {} [{}, {:.1f}s]", func.__name__, trace_id, elapsed)
    return wrapper
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 python3 -m pytest tests/ --tb=line -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add app/scheduler.py
git commit -m "feat: add elapsed time logging to _traced_job decorator

Job finish logs now include duration in seconds for performance
monitoring without needing to correlate timestamps manually."
```

---

### Task 10: Fix Azure token error response logging (2.8)

**Files:**
- Modify: `app/routers/auth.py:98`

- [ ] **Step 1: Truncate logged response**

Replace `logger.error(f"Azure token exchange returned {resp.status_code}: {resp.text[:500]}")` with `logger.error(f"Azure token exchange returned {resp.status_code}")`.

- [ ] **Step 2: Commit**

```bash
git add app/routers/auth.py
git commit -m "fix: remove response body from Azure token error log

Azure error responses can contain partial tokens, client IDs, or
redirect URIs with embedded state. Log only the status code."
```

---

### Task 11: Add global HTTPException handler for consistent error format (2.9)

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write test for error format**

```python
# tests/test_main.py — add test
def test_http_exception_returns_error_format(client):
    """HTTPException responses should use {"error": ...} format, not {"detail": ...}."""
    resp = client.get("/api/requisitions/99999999")
    assert resp.status_code in (404, 401)
    data = resp.json()
    assert "error" in data
    assert "detail" not in data
```

- [ ] **Step 2: Add exception handler to main.py**

```python
from fastapi.responses import JSONResponse
from fastapi import Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException

@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: FastAPIHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
        },
    )
```

- [ ] **Step 3: Run tests — some existing tests may need updating**

Run: `TESTING=1 python3 -m pytest tests/ --tb=short -q`

If any tests check `response.json()["detail"]`, update them to check `response.json()["error"]` instead. The CLAUDE.md standard is `{"error": ...}`.

- [ ] **Step 4: Commit**

```bash
git add app/main.py tests/
git commit -m "fix: add global exception handler for consistent error format

All HTTPException responses now return {\"error\": \"message\",
\"status_code\": N} instead of FastAPI's default {\"detail\": ...}.
Aligns with the project's documented API contract in CLAUDE.md."
```

---

### Task 12: Final verification

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 python3 -m pytest tests/ --tb=line -q`
Expected: 8485+ passed, 0 failed

- [ ] **Step 2: Run ruff**

Run: `ruff check app/`
Expected: No errors

- [ ] **Step 3: Grep for remaining issues**

Run: `grep -rn "except Exception" app/ | grep "pass$" | grep -v test | grep -v "# noqa"` — should return 0 results.
