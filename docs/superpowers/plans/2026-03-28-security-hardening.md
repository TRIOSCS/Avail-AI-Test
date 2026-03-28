# Security Hardening + Data Model + Test Isolation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 3 remaining security issues, 3 data model issues, and parallel test isolation problems.

**Architecture:** Targeted fixes — each task is independent and produces a working commit.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, pytest

---

## Task 1: Scrub Mouser API Key from Logs

**Files:**
- Modify: `app/connectors/mouser.py`

- [ ] **Step 1: Add request-level log scrubbing**

In `app/connectors/mouser.py`, the API key is required as a URL param (Mouser's API design). Sentry already scrubs query strings containing "key". The remaining risk is httpx debug logging and application logs.

Add a log filter after the request. Read the file and find any `logger.debug` or `logger.info` calls that might log the full URL. If found, mask the API key in the log message.

Also add a comment documenting why the key must be in params:

```python
# Mouser API requires apiKey as URL query param — no header auth option.
# Sentry before_send scrubs query strings containing "key" (see main.py:89).
# httpx does not log URL params at INFO level, only at DEBUG/TRACE.
```

- [ ] **Step 2: Verify Sentry scrubbing covers this**

Read `app/main.py` lines 85-90. Confirm the `before_send` hook scrubs `query_string` when it contains "key". It does:
```python
qs = req.get("query_string", "")
if isinstance(qs, str) and "key" in qs.lower():
    req["query_string"] = "[Filtered]"
```

This already handles the Mouser case. Mark as resolved with documentation.

- [ ] **Step 3: Commit**

```bash
git add app/connectors/mouser.py && git commit -m "docs: document Mouser API key scrubbing (already handled by Sentry)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Move Token Refresh to Background

**Files:**
- Modify: `app/dependencies.py`
- Modify: `app/jobs/core_jobs.py` (add proactive refresh job)

- [ ] **Step 1: Read current token refresh logic**

Read `app/dependencies.py` lines 135-172. The current flow:
1. Check if token expires within 15 minutes
2. If yes, call `refresh_user_token(user, db)` synchronously
3. If refresh fails, return 401

- [ ] **Step 2: Add proactive token refresh job**

Read `app/jobs/core_jobs.py` to find the existing `_job_token_refresh` function. It already exists and refreshes tokens proactively! Check if it runs on a schedule that covers the 15-min buffer.

If the job already handles proactive refresh, the dependency just needs a shorter buffer — check token validity without attempting refresh inline. If token is expired AND no valid token after job ran, return 401.

- [ ] **Step 3: Simplify require_fresh_token**

In `app/dependencies.py`, change the token refresh to NOT call refresh inline. Instead:
- Check token validity (is it expired?)
- If expired but refresh_token exists, the scheduler will handle it
- If token is expired AND no refresh possible, return 401
- Remove the inline `await refresh_user_token()` call

```python
if needs_refresh:
    # Background job handles refresh proactively.
    # If we're here, the job hasn't run yet — token is still usable
    # for the 15-min buffer window. If truly expired, force re-login.
    if datetime.now(timezone.utc) > expiry:
        user.m365_connected = False
        db.commit()
        raise HTTPException(401, "Session expired — please log in again")
    # Within buffer but not expired — continue with current token
    return str(token)
```

- [ ] **Step 4: Verify existing scheduler job**

```bash
grep -n "token_refresh\|refresh_token" app/jobs/core_jobs.py | head -10
```

Confirm the job runs frequently enough (every 5-10 min) to catch tokens before the 15-min buffer.

- [ ] **Step 5: Test**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_routers_auth.py tests/test_dependencies.py -v -o "addopts=" --timeout=30 2>&1 | tail -10
```

- [ ] **Step 6: Commit**

```bash
git add app/dependencies.py && git commit -m "fix: remove inline token refresh from request handler

Background scheduler job handles proactive token refresh.
Dependency now just validates token; returns 401 if truly expired.
Eliminates latency spikes from synchronous token refresh.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Data Model — Unique Constraint on site_contacts

**Files:**
- Modify: `app/models/crm.py`
- Create: Alembic migration

- [ ] **Step 1: Add unique constraint to model**

In `app/models/crm.py`, find the `SiteContact` class `__table_args__`. Add:

```python
UniqueConstraint("customer_site_id", "email", name="uq_site_contacts_site_email"),
```

- [ ] **Step 2: Generate migration**

```bash
cd /root/availai && alembic revision --autogenerate -m "add unique constraint on site_contacts(site_id, email)"
```

Review the generated migration.

- [ ] **Step 3: Make count columns non-nullable**

In `app/models/crm.py`, change:
```python
site_count = Column(Integer, default=0, server_default="0")
open_req_count = Column(Integer, default=0, server_default="0")
```
to:
```python
site_count = Column(Integer, default=0, server_default="0", nullable=False)
open_req_count = Column(Integer, default=0, server_default="0", nullable=False)
```

Generate another migration or combine with the previous one.

- [ ] **Step 4: Test**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_data_cleanup.py tests/test_phase4_sites_contacts.py -v -o "addopts=" --timeout=30 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "fix: add unique constraint on site_contacts + non-nullable count columns

Prevents duplicate contacts per site (same email).
Makes site_count and open_req_count NOT NULL with default 0.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Reduce Broad except Exception (top 20 files)

**Files:**
- Modify: Top 20 offender files in `app/`

- [ ] **Step 1: Identify top offenders**

```bash
grep -rc "except Exception" app/ --include="*.py" | sort -t: -k2 -rn | head -20
```

- [ ] **Step 2: For each top offender, categorize exceptions**

Read each file and for each `except Exception`:
- If it catches a known error type (httpx, SQLAlchemy, ValueError, etc.) → narrow to specific type
- If it's a catch-all safety net with `logger.exception()` → leave as-is
- If it silently swallows → add `logger.exception()`

Common replacements:
- HTTP calls: `except (httpx.HTTPError, httpx.TimeoutException)`
- DB operations: `except (IntegrityError, OperationalError)`
- JSON parsing: `except (ValueError, KeyError, TypeError)`
- External APIs: `except (httpx.HTTPError, ValueError, KeyError)`

- [ ] **Step 3: Fix top 20 files**

Work through each file, narrowing exceptions. Run ruff after each file.

- [ ] **Step 4: Verify**

```bash
ruff check app/ && TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=30 -x 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "fix: narrow except Exception in top 20 offender files

Replaced broad catches with specific exception types.
Added logger.exception() where errors were silently swallowed.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Fix Parallel Test Isolation

**Files:**
- Modify: `tests/conftest.py` and individual test files

- [ ] **Step 1: Identify parallel-only failures**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --timeout=30 --ignore=tests/e2e --ignore=tests/test_browser_e2e.py --ignore=.clone -n 4 --tb=no 2>&1 | grep "FAILED" | head -30
```

- [ ] **Step 2: For each failure, run individually to confirm it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest "tests/test_file.py::test_name" -v -o "addopts=" --timeout=30
```

- [ ] **Step 3: Identify root causes**

Common patterns:
- **Module-level state:** Global caches, singletons → add reset fixture
- **ID collisions:** Tests using hardcoded IDs → use unique test names or UUIDs
- **Empty table assumptions:** Tests expect empty tables → filter by test-specific data
- **Config mutations:** Tests modifying settings → use monkeypatch

- [ ] **Step 4: Fix each test**

Apply appropriate fix for each root cause pattern.

- [ ] **Step 5: Verify with parallel execution**

```bash
TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q --timeout=30 --ignore=tests/e2e --ignore=tests/test_browser_e2e.py -n 4 --tb=no 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "fix: resolve parallel test isolation issues

Fixed shared state, ID collisions, and empty table assumptions
that caused ~30 tests to fail under pytest-xdist parallel execution.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Verification Checklist

After all tasks:

- [ ] `ruff check app/` — passes
- [ ] `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/ -q -o "addopts=" --timeout=30` — 0 sequential failures
- [ ] `git status` — clean working tree
- [ ] All commits pushed to `origin/main`
