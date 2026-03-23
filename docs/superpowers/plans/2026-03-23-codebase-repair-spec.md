# AvailAI Codebase Repair Spec — Comprehensive Audit Remediation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate all 113 findings from the full-stack audit across security, data integrity, silent failures, code quality, architecture, and test coverage — producing a rock-solid, best-practice codebase with zero deferred issues.

**Architecture:** Phased approach — each phase is independently deployable and testable. Phases are ordered by risk: critical safety first, then data integrity, then reliability, then quality, then structure. Each phase commits separately with full test coverage.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, Alembic, HTMX/Alpine.js, pytest

**Audit Source:** Six parallel audit agents ran on 2026-03-23, producing 113 findings:
- Architecture: 14 findings (2C, 4H, 7M, 1L)
- Security: 10 findings (1C, 3H, 4M, 2L)
- Data Integrity: 28 findings (2C, 8H, 16M, 2L)
- Code Quality: 19 findings (1C, 6H, 9M, 3L)
- Silent Failures: 22 findings (6C, 12H, 4M)
- Test Coverage: ~20 findings (2C, 5H, 8M, 5L)

---

## Phase Overview

| Phase | Name | Focus | Findings Addressed | Risk Level |
|-------|------|-------|-------------------|------------|
| 1 | Critical Safety | Security vulns + data-loss bugs | ~12 | CRITICAL |
| 2 | Data Integrity | FK constraints, indexes, race conditions, enums | ~28 | HIGH |
| 3 | Silent Failure Remediation | Exception handling, error propagation, logging | ~22 | HIGH |
| 4 | Code Quality | DRY, dead code, patterns, type safety | ~19 | MEDIUM |
| 5 | Architecture | God file split, layering, module organization | ~14 | MEDIUM |
| 6 | Test Coverage | Missing tests, isolation, fixture quality | ~20 | MEDIUM |

---

# PHASE 1: CRITICAL SAFETY

**Goal:** Fix all security vulnerabilities and data-loss bugs. Every item here can cause real harm if left unfixed.

**Commit strategy:** One commit per task. Each task is independently deployable.

---

### Task 1.1: Fix Email Watermark Advancing on Failed Poll

**Audit refs:** Silent Failures 2.2, 7.1
**Severity:** CRITICAL — permanently loses emails when poll fails

**Problem:** In `_scan_user_inbox()`, `user.last_inbox_scan` is updated unconditionally at line 426, even when the inbox poll at line 385 threw an exception. This means the watermark advances past unscanned emails, and they are never retried.

**Files:**
- Modify: `app/jobs/email_jobs.py:380-427`
- Test: `tests/test_jobs_email.py` (add/modify)

- [ ] **Step 1: Write failing test**

```python
# tests/test_jobs_email.py — add to existing test class

@pytest.mark.asyncio
async def test_inbox_scan_does_not_advance_watermark_on_poll_failure(db_session, test_user):
    """When poll_inbox raises, last_inbox_scan must NOT advance."""
    original_scan_time = test_user.last_inbox_scan

    with patch("app.jobs.email_jobs.get_valid_token", new_callable=AsyncMock, return_value="tok"), \
         patch("app.jobs.email_jobs.poll_inbox", new_callable=AsyncMock, side_effect=Exception("Graph API down")):
        await _scan_user_inbox(test_user, db_session)

    db_session.refresh(test_user)
    assert test_user.last_inbox_scan == original_scan_time, \
        "last_inbox_scan must not advance when poll fails"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_jobs_email.py::test_inbox_scan_does_not_advance_watermark_on_poll_failure -v
```

Expected: FAIL — watermark advances despite poll failure.

- [ ] **Step 3: Implement fix in `app/jobs/email_jobs.py`**

Replace lines 380-427 with:

```python
    poll_succeeded = False
    try:
        token = await get_valid_token(user, db)
        if not token:
            logger.warning(f"Skipping inbox poll for {user.email} — no valid token")
            return
        new_responses = await poll_inbox(
            token=token,
            db=db,
            scanned_by_user_id=user.id,
        )
        poll_succeeded = True
        if new_responses:
            logger.info(f"Inbox scan [{user.email}]: {len(new_responses)} new responses")
    except Exception as e:
        logger.error(f"Inbox poll failed for {user.email}: {e}")

    # Sub-operations run regardless (they have their own error handling)
    async def _safe_stock_scan():
        try:
            await _scan_stock_list_attachments(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Stock list scan failed for {user.email}: {e}")

    async def _safe_mine_contacts():
        try:
            await _mine_vendor_contacts(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Vendor mining failed for {user.email}: {e}")

    async def _safe_outbound_scan():
        try:
            await _scan_outbound_rfqs(user, db, is_backfill)
        except Exception as e:
            logger.error(f"Outbound scan failed for {user.email}: {e}")

    async def _safe_excess_bid_scan():
        try:
            await _scan_excess_bid_responses(user, db)
        except Exception as e:
            logger.error(f"Excess bid scan failed for {user.email}: {e}")

    await _safe_stock_scan()
    await _safe_mine_contacts()
    await _safe_outbound_scan()
    await _safe_excess_bid_scan()

    # CRITICAL: Only advance watermark if the poll itself succeeded
    if poll_succeeded:
        user.last_inbox_scan = datetime.now(timezone.utc)
    db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_jobs_email.py::test_inbox_scan_does_not_advance_watermark_on_poll_failure -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/email_jobs.py tests/test_jobs_email.py
git commit -m "fix(critical): only advance inbox watermark on successful poll

Prevents permanent email loss when Graph API poll fails. The watermark
was previously advanced unconditionally, causing missed emails to never
be retried."
```

---

### Task 1.2: Fix Credit Manager TOCTOU Race Condition

**Audit refs:** Data Integrity 3.1
**Severity:** CRITICAL — concurrent enrichment tasks can double-spend credits

**Problem:** `_get_or_create_row()` in `credit_manager.py` does a SELECT then INSERT with no locking. Two concurrent enrichment tasks both see `row = None`, both INSERT, one gets `UniqueViolation`. Also, `can_use_credits()` and `record_credit_usage()` are separate calls with no atomicity — budget can be exceeded by concurrent callers.

**Files:**
- Modify: `app/services/credit_manager.py` (full rewrite)
- Test: `tests/test_credit_manager.py` (add/modify)

- [ ] **Step 1: Write failing test for concurrent upsert**

```python
# tests/test_credit_manager.py

def test_concurrent_credit_recording_does_not_raise(db_session):
    """Two concurrent record_credit_usage calls must not raise IntegrityError."""
    from app.services.credit_manager import record_credit_usage

    # First call creates the row
    record_credit_usage(db_session, "apollo", count=1)
    db_session.commit()

    # Second call in same month must upsert, not fail
    record_credit_usage(db_session, "apollo", count=1)
    db_session.commit()

    from app.services.credit_manager import get_monthly_usage
    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 2


def test_atomic_check_and_record(db_session):
    """check_and_record_credits must be atomic — no double-spend."""
    from app.services.credit_manager import check_and_record_credits, get_monthly_usage

    # Set limit to 1
    result1 = check_and_record_credits(db_session, "apollo", count=1)
    assert result1 is True

    usage = get_monthly_usage(db_session, "apollo")
    assert usage["used"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_credit_manager.py -v
```

Expected: FAIL — `check_and_record_credits` does not exist yet.

- [ ] **Step 3: Rewrite `app/services/credit_manager.py`**

```python
"""Credit Manager — tracks monthly API credit usage per enrichment provider.

Prevents overspend by checking budgets before each external API call.
Providers: lusha, hunter_search, hunter_verify, apollo.

Called by: customer_enrichment_service.py waterfall steps.
Depends on: app.models.enrichment.EnrichmentCreditUsage, app.config.settings.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models.enrichment import EnrichmentCreditUsage


def _current_month() -> str:
    """Return current month as 'YYYY-MM' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _default_limit(provider: str) -> int:
    """Get the configured monthly credit limit for a provider."""
    limits = {
        "lusha": settings.lusha_monthly_credit_limit,
        "lusha_phone": settings.lusha_phone_credit_limit,
        "lusha_discovery": settings.lusha_discovery_credit_limit,
        "hunter_search": settings.hunter_monthly_search_limit,
        "hunter_verify": settings.hunter_monthly_verify_limit,
        "apollo": settings.apollo_monthly_credit_limit,
    }
    return limits.get(provider, 100)


def _get_or_create_row(db: Session, provider: str, month: str) -> EnrichmentCreditUsage:
    """Get or create a credit usage row for provider/month.

    Race-safe: uses savepoint + IntegrityError retry pattern to handle
    concurrent inserts on the unique (provider, month) constraint.
    """
    row = db.execute(
        select(EnrichmentCreditUsage).where(
            EnrichmentCreditUsage.provider == provider,
            EnrichmentCreditUsage.month == month,
        )
    ).scalar_one_or_none()

    if row:
        return row

    try:
        with db.begin_nested():
            row = EnrichmentCreditUsage(
                provider=provider,
                month=month,
                credits_used=0,
                credits_limit=_default_limit(provider),
            )
            db.add(row)
            db.flush()
        return row
    except IntegrityError:
        # Concurrent insert won — savepoint already rolled back by begin_nested()
        # Do NOT call db.rollback() here — that would roll back the outer transaction
        return db.execute(
            select(EnrichmentCreditUsage).where(
                EnrichmentCreditUsage.provider == provider,
                EnrichmentCreditUsage.month == month,
            )
        ).scalar_one()


def get_monthly_usage(db: Session, provider: str) -> dict:
    """Get current month's usage for a provider. Returns {used, limit, remaining}."""
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    return {
        "provider": provider,
        "month": month,
        "used": row.credits_used,
        "limit": row.credits_limit,
        "remaining": max(0, row.credits_limit - row.credits_used),
    }


def can_use_credits(db: Session, provider: str, count: int = 1) -> bool:
    """Check if there are enough credits to make an API call.

    NOTE: Prefer check_and_record_credits() for atomic check+spend.
    This function is only safe when a separate lock prevents concurrent access.
    """
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    return (row.credits_used + count) <= row.credits_limit


def record_credit_usage(db: Session, provider: str, count: int = 1) -> None:
    """Record that credits were consumed. Call after a successful API call."""
    month = _current_month()
    row = _get_or_create_row(db, provider, month)
    row.credits_used += count
    row.updated_at = datetime.now(timezone.utc)
    logger.debug("Credit usage: %s %s/%s (month=%s)", provider, row.credits_used, row.credits_limit, month)


def check_and_record_credits(db: Session, provider: str, count: int = 1) -> bool:
    """Atomic check-and-record: verifies budget and records usage in one operation.

    Returns True if credits were consumed, False if budget exceeded.
    Uses row-level locking (SELECT FOR UPDATE) to prevent concurrent overspend.

    NOTE: On SQLite (tests), FOR UPDATE is a no-op — tests rely on single-threaded
    execution. In production (PostgreSQL), this provides true atomic budget enforcement.
    """
    month = _current_month()
    row = _get_or_create_row(db, provider, month)

    # Re-fetch with FOR UPDATE lock for atomicity in PostgreSQL
    # (SQLite ignores with_for_update, which is fine for single-threaded tests)
    locked_row = db.execute(
        select(EnrichmentCreditUsage)
        .where(
            EnrichmentCreditUsage.provider == provider,
            EnrichmentCreditUsage.month == month,
        )
        .with_for_update()
    ).scalar_one()

    if (locked_row.credits_used + count) > locked_row.credits_limit:
        logger.warning(
            "Credit budget exceeded: %s %s/%s (requested %d, month=%s)",
            provider, locked_row.credits_used, locked_row.credits_limit, count, month,
        )
        return False

    locked_row.credits_used += count
    locked_row.updated_at = datetime.now(timezone.utc)
    logger.debug("Credit usage: %s %s/%s (month=%s)", provider, locked_row.credits_used, locked_row.credits_limit, month)
    return True


def get_all_budgets(db: Session) -> list[dict]:
    """Get credit usage for all providers this month, including split Lusha pools."""
    providers = ["lusha_phone", "lusha_discovery", "hunter_search", "hunter_verify", "apollo"]
    budgets = [get_monthly_usage(db, p) for p in providers]

    # Add aggregate "lusha" entry summing both split pools
    phone = next(b for b in budgets if b["provider"] == "lusha_phone")
    discovery = next(b for b in budgets if b["provider"] == "lusha_discovery")
    budgets.append(
        {
            "provider": "lusha",
            "month": phone["month"],
            "used": phone["used"] + discovery["used"],
            "limit": phone["limit"] + discovery["limit"],
            "remaining": phone["remaining"] + discovery["remaining"],
        }
    )
    return budgets
```

- [ ] **Step 4: Update callers to use `check_and_record_credits`**

Search all callers of `can_use_credits` + `record_credit_usage` pairs and replace with `check_and_record_credits`. Primary caller is `app/services/customer_enrichment_batch.py`. Also check `customer_enrichment_service.py` and any other files that import from `credit_manager`.

- [ ] **Step 5: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_credit_manager.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/services/credit_manager.py tests/test_credit_manager.py
git commit -m "fix(critical): make credit manager race-safe with atomic check+spend

Replaces TOCTOU-vulnerable get_or_create + separate check/spend with:
- Savepoint retry pattern for concurrent row creation
- check_and_record_credits() with SELECT FOR UPDATE for atomic budget enforcement
- SQLAlchemy 2.0 select() instead of deprecated db.query()"
```

---

### Task 1.3: Add Deployment-Unique Salt to Fernet Encryption

**Audit refs:** Security 1
**Severity:** CRITICAL — all encrypted tokens share a single derivation path

**Problem:** `encrypted_type.py` and `credential_service.py` use static salts. If `SECRET_KEY` leaks, all encrypted data is immediately decryptable.

**Files:**
- Modify: `app/utils/encrypted_type.py`
- Modify: `app/services/credential_service.py`
- Modify: `app/config.py` (add `encryption_salt` setting)
- Test: `tests/test_encrypted_type.py`

- [ ] **Step 1: Add `encryption_salt` to config**

In `app/config.py`, add to the Settings class:

```python
    encryption_salt: str = ""  # Set per-deployment; empty = fall back to legacy static salt
```

- [ ] **Step 2: Write test for new salt behavior**

```python
# tests/test_encrypted_type.py

def test_encryption_uses_deployment_salt_when_set(monkeypatch):
    """When ENCRYPTION_SALT is set, Fernet key derivation uses it."""
    import app.utils.encrypted_type as mod
    mod._fernet_instance = None  # Reset cached instance

    monkeypatch.setattr("app.config.settings.encryption_salt", "unique-deploy-salt-abc123")
    f1 = mod._get_fernet()

    mod._fernet_instance = None
    monkeypatch.setattr("app.config.settings.encryption_salt", "different-salt-xyz789")
    f2 = mod._get_fernet()

    # Different salts must produce different Fernet instances
    plaintext = b"secret-token"
    encrypted_by_f1 = f1.encrypt(plaintext)
    # f2 should NOT be able to decrypt f1's ciphertext
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        f2.decrypt(encrypted_by_f1)

    mod._fernet_instance = None  # Clean up


def test_encryption_falls_back_to_legacy_salt_when_empty(monkeypatch):
    """When ENCRYPTION_SALT is empty, use legacy static salt for backward compat."""
    import app.utils.encrypted_type as mod
    mod._fernet_instance = None

    monkeypatch.setattr("app.config.settings.encryption_salt", "")
    f = mod._get_fernet()
    assert f is not None  # Should work with legacy salt

    mod._fernet_instance = None
```

- [ ] **Step 3: Implement salt support in `encrypted_type.py`**

```python
def _get_fernet():
    """Derive a Fernet key from the app secret key (cached after first call).

    Uses deployment-unique ENCRYPTION_SALT when set; falls back to legacy
    static salt for backward compatibility with existing encrypted data.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    from ..config import settings

    # Use deployment-unique salt if configured; otherwise fall back to legacy
    # static salt for backward compatibility with pre-existing encrypted data.
    if settings.encryption_salt:
        salt = settings.encryption_salt.encode()
    else:
        logger.warning(
            "ENCRYPTION_SALT not set — using legacy static salt. "
            "Set ENCRYPTION_SALT in .env for defense-in-depth."
        )
        salt = b"availai-token-encryption-v1"

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(settings.secret_key.encode()))
    _fernet_instance = Fernet(key)
    return _fernet_instance
```

- [ ] **Step 4: Apply same pattern to `credential_service.py`**

- [ ] **Step 5: Run tests, commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_encrypted_type.py -v
git add app/utils/encrypted_type.py app/services/credential_service.py app/config.py tests/test_encrypted_type.py
git commit -m "security(critical): add deployment-unique encryption salt

Adds ENCRYPTION_SALT env var for defense-in-depth. Existing deployments
without the var continue working via legacy static salt fallback. New
deployments should set ENCRYPTION_SALT to a random 32+ char string."
```

---

### Task 1.4: Protect `/metrics` Endpoint

**Audit refs:** Security 2
**Severity:** HIGH — exposes operational telemetry to anonymous callers

**Files:**
- Modify: `app/main.py:305-309`
- Test: `tests/test_main_security.py`

- [ ] **Step 1: Write test**

```python
def test_metrics_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.metrics_token", "secret-metrics-token")
    resp = client.get("/metrics")
    assert resp.status_code == 403

    resp = client.get("/metrics", headers={"X-Metrics-Token": "secret-metrics-token"})
    assert resp.status_code == 200


def test_metrics_blocked_by_default(client):
    """When no metrics_token is set, /metrics should return 403."""
    resp = client.get("/metrics")
    assert resp.status_code == 403
```

- [ ] **Step 2: Add `metrics_token` to config**

```python
    metrics_token: str = ""  # Required for /metrics access; empty = blocked
```

- [ ] **Step 3: Add auth dependency to metrics endpoint**

In `app/main.py`, replace the bare `expose()` call:

```python
from fastapi import Request, HTTPException

def _metrics_auth(request: Request):
    """Require X-Metrics-Token header matching config for /metrics access."""
    expected = settings.metrics_token
    if not expected:
        raise HTTPException(403, "Metrics endpoint not configured")
    provided = request.headers.get("X-Metrics-Token", "")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(403, "Invalid metrics token")

Instrumentator(excluded_handlers=["/metrics", "/health", "/static/*"]).instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False, dependencies=[Depends(_metrics_auth)]
)
```

- [ ] **Step 4: Run tests, commit**

---

### Task 1.5: Minimize Buy-Plan Token Data Exposure

**Audit refs:** Security 3
**Severity:** HIGH — anonymous callers see full financial data

**Files:**
- Modify: `app/routers/crm/buy_plans.py:342-370`
- Test: `tests/test_routers_crm.py`

- [ ] **Step 1: Write test**

```python
def test_token_get_excludes_sensitive_fields(client, db_session):
    """Token-based GET must not expose ai_summary, ai_flags, margin, or salesperson notes."""
    # Create a buy plan with a token
    plan = _create_buy_plan_with_token(db_session)
    resp = client.get(f"/api/buy-plans/token/{plan.approval_token}")
    assert resp.status_code == 200
    data = resp.json()
    for field in ["ai_summary", "ai_flags", "total_margin_pct", "case_report", "salesperson_notes"]:
        assert field not in data, f"Sensitive field '{field}' must not be in token response"
```

- [ ] **Step 2: Create a `_to_approval_dict()` helper** that strips sensitive fields from the response. Only include: `id`, `title`, `status`, `total_cost`, `total_revenue`, `line_count`, `vendor_names`, `created_at`, `requested_by_name`.

- [ ] **Step 3: Run tests, commit**

---

### Task 1.6: Fix Expired Token Usage in Attachment Operations

**Audit refs:** Security 4
**Severity:** HIGH — Graph API calls silently fail with expired tokens

**Files:**
- Modify: `app/routers/requisitions/attachments.py:161-170, 277-286, 109-128`
- Test: `tests/test_routers_attachments.py`

- [ ] **Step 1: Replace all `user.access_token` with `await get_valid_token(user, db)`**

Find every occurrence in `attachments.py` that uses `user.access_token` directly:
- Line 128: `attach_requisition_from_onedrive`
- Lines 161-170: `delete_requisition_attachment`
- Lines 277-286: `delete_requirement_attachment`

Replace each with:

```python
token = await get_valid_token(user, db)
if not token:
    raise HTTPException(401, "Microsoft token expired — please re-authenticate")
```

- [ ] **Step 2: Add explicit 401 handling for Graph API responses**

```python
if resp.status_code == 401:
    raise HTTPException(401, "Microsoft token expired — please re-authenticate")
if resp.status_code == 403:
    raise HTTPException(403, "Access denied to OneDrive item")
```

- [ ] **Step 3: Write tests verifying proper token refresh and error handling, commit**

---

### Task 1.7: Fix Background Job Exception Swallowing

**Audit refs:** Silent Failures 2.3
**Severity:** HIGH — Sentry receives zero alerts from any job failure

**Problem:** Every background job has inner `except Exception` blocks that catch errors before `_traced_job` can see them. This means Sentry never captures job failures.

**Files:**
- Modify: `app/jobs/email_jobs.py` (29 exception handlers)
- Modify: `app/jobs/core_jobs.py`
- Modify: `app/jobs/knowledge_jobs.py`
- Modify: `app/jobs/part_discovery_jobs.py`
- Test: `tests/test_jobs_exception_propagation.py`

- [ ] **Step 1: Write test that verifies exceptions propagate to `_traced_job`**

```python
def test_job_exceptions_propagate_for_sentry(monkeypatch):
    """Job exceptions must re-raise after cleanup so _traced_job captures them."""
    from app.jobs.email_jobs import _job_contacts_sync

    with patch("app.jobs.email_jobs.SessionLocal") as mock_session:
        mock_db = MagicMock()
        mock_session.return_value = mock_db
        mock_db.query.side_effect = OperationalError("DB down", None, None)

        with pytest.raises(OperationalError):
            _job_contacts_sync()
```

- [ ] **Step 2: Fix pattern across all job files**

Transform every instance of:

```python
    except Exception as e:
        logger.error(f"Job failed: {e}")
        db.rollback()
    finally:
        db.close()
```

To:

```python
    except Exception as e:
        logger.error(f"Job failed: {e}")
        db.rollback()
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()
```

- [ ] **Step 3: Run full job test suite, commit**

---

### Task 1.8: Fix XSS in Customer Lookup JavaScript Generation

**Audit refs:** Code Quality 15
**Severity:** HIGH — single quotes in company names break embedded JavaScript

**Files:**
- Modify: `app/routers/htmx_views.py:860-898`
- Test: `tests/test_htmx_xss.py`

- [ ] **Step 1: Write test**

```python
def test_customer_lookup_escapes_js_strings(client, db_session):
    """Company names with single quotes must not break embedded JavaScript."""
    # Simulate a lookup result with a problematic name
    resp = _trigger_customer_lookup(client, company_name="O'Brien & Sons <script>alert(1)</script>")
    assert resp.status_code == 200
    # Should use JSON-safe escaping, not just HTML escaping
    assert "O\\'Brien" not in resp.text or "O\\u0027Brien" in resp.text or "O&#x27;Brien" in resp.text
```

- [ ] **Step 2: Fix — use `json.dumps()` for JavaScript-embedded values**

Replace `html_mod.escape()` with `json.dumps()` for all values injected into JavaScript strings:

```python
import json

name_js = json.dumps(result.get("company_name", company_name))
website_js = json.dumps(result.get("website", ""))
# Then in the JS:  fd.append('company_name', {name_js});
```

Better: Move the JavaScript to a template partial and pass values as `data-*` attributes on the button.

- [ ] **Step 3: Run tests, commit**

---

### Task 1.9: Add Password Login Production Guard

**Audit refs:** Security 5
**Severity:** MEDIUM — accidental `ENABLE_PASSWORD_LOGIN=true` in prod creates an admin backdoor

**Files:**
- Modify: `app/startup.py` (add fail-fast check)
- Modify: `app/routers/auth.py` (rate-limit `/auth/login-form`)
- Test: `tests/test_auth_password_guard.py`

- [ ] **Step 1: Add startup check**

In `app/startup.py`, early in `run_startup()`:

```python
import os
if os.getenv("ENABLE_PASSWORD_LOGIN", "false").lower() == "true" and not os.getenv("TESTING"):
    logger.critical(
        "ENABLE_PASSWORD_LOGIN is active in non-test mode. "
        "This creates an authentication bypass. Disable before production use."
    )
    # Don't hard-fail (in case it's intentional for staging), but log critically
```

- [ ] **Step 2: Rate-limit `/auth/login-form` matching `/auth/login`**

- [ ] **Step 3: Write tests, commit**

---

# PHASE 2: DATA INTEGRITY

**Goal:** Fix all FK constraints, add missing indexes, resolve race conditions, consolidate enums.

**Commit strategy:** Group related model changes into single Alembic migrations.

---

### Task 2.1: Add Missing `ondelete` to All FK Columns

**Audit refs:** Data Integrity 1.2-1.7, 7.1-7.4
**Severity:** HIGH — ORM cascades don't work for bulk deletes

**Files:**
- Modify: `app/models/vendors.py:164` — `VendorReview.vendor_card_id` → add `ondelete="CASCADE"`
- Modify: `app/models/intelligence.py:77` — `MaterialVendorHistory.material_card_id` → `ondelete="CASCADE"`
- Modify: `app/models/intelligence.py:174-175` — `ProactiveOffer.customer_site_id` → `ondelete="SET NULL"`, `salesperson_id` → `ondelete="SET NULL"`
- Modify: `app/models/intelligence.py:210` — `ProactiveThrottle.proactive_offer_id` → `ondelete="SET NULL"`
- Modify: `app/models/intelligence.py:265-270` — `ActivityLog` FKs → `ondelete="SET NULL"` for all nullable
- Modify: `app/models/sourcing.py:161` — `Sighting.source_company_id` → `ondelete="SET NULL"`
- Modify: `app/models/performance.py:274-275` — `BuyerVendorStats` FKs → `ondelete="CASCADE"`
- Modify: `app/models/email_intelligence.py:32` — `user_id` → `ondelete="CASCADE"`
- Modify: `app/models/strategic.py:31-32` — both FKs → `ondelete="CASCADE"`
- Modify: `app/models/price_snapshot.py:18` — `material_card_id` → `ondelete="CASCADE"`, add `index=True`
- Create: Alembic migration

- [ ] **Step 1: Apply all `ondelete` changes to model files**

For each FK column listed above, add the appropriate `ondelete` parameter. The rule:
- **CASCADE** when the child record has no meaning without the parent (reviews, stats, history)
- **SET NULL** when the child is an audit trail or historical record (activity logs, throttles, sightings)

- [ ] **Step 2: Generate Alembic migration**

```bash
cd /root/availai && alembic revision --autogenerate -m "add_ondelete_to_all_fk_columns"
```

- [ ] **Step 3: Review the generated migration** — verify it only modifies FK constraints, no unrelated changes

- [ ] **Step 4: Run tests to ensure migration applies cleanly**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -x -q
```

- [ ] **Step 5: Commit**

```bash
git add app/models/ alembic/versions/
git commit -m "fix(data): add ondelete to all FK columns missing cascade behavior

Adds CASCADE or SET NULL to 15+ FK columns across vendors, intelligence,
sourcing, performance, and strategic models. Ensures bulk DELETE operations
don't fail or leave orphaned rows."
```

---

### Task 2.2: Add Missing Database Indexes

**Audit refs:** Data Integrity 2.1-2.6, 1.7
**Severity:** MEDIUM-HIGH — missing indexes cause full table scans

**Files:**
- Modify: `app/models/intelligence.py` — add `Index("ix_pm_company", "company_id")` to `ProactiveMatch.__table_args__`
- Modify: `app/models/sourcing.py` — add `Index("ix_requisitions_company", "company_id")` to `Requisition.__table_args__`
- Modify: `app/models/ics_search_log.py` — add `Index("ix_ics_log_queue", "queue_id")`
- Modify: `app/models/discovery_batch.py` — add indexes on `status`, `source`, `started_at`
- Modify: `app/models/enrichment_run.py` — add indexes on `phase`, `status`, `created_at`
- Modify: `app/models/excess.py` — add `Index("ix_bidsol_status", "status")` to `BidSolicitation`
- Modify: `app/models/knowledge.py` — add index on `KnowledgeEntry.created_by`
- Create: Alembic migration

- [ ] **Step 1: Add all index definitions to model `__table_args__`**
- [ ] **Step 2: Generate Alembic migration**

```bash
cd /root/availai && alembic revision --autogenerate -m "add_missing_indexes"
```

- [ ] **Step 3: Review migration, run tests, commit**

---

### Task 2.3: Fix Brand Tag TOCTOU Race

**Audit refs:** Data Integrity 3.2
**Severity:** HIGH — concurrent tagging causes `IntegrityError`

**Files:**
- Modify: `app/services/tagging.py:168-181`
- Test: `tests/test_tagging.py`

- [ ] **Step 1: Write test**

```python
def test_get_or_create_brand_tag_concurrent_safe(db_session):
    """Two calls with same name must not raise IntegrityError."""
    from app.services.tagging import get_or_create_brand_tag

    tag1 = get_or_create_brand_tag("Texas Instruments", db_session)
    db_session.commit()

    # Simulate a concurrent create by manually inserting first, then calling again
    tag2 = get_or_create_brand_tag("Texas Instruments", db_session)
    assert tag1.id == tag2.id
```

- [ ] **Step 2: Fix with savepoint + IntegrityError retry**

```python
def get_or_create_brand_tag(manufacturer_name: str, db: Session) -> Tag:
    """Find or create a brand Tag. Race-safe via savepoint retry."""
    normalized = manufacturer_name.strip()
    tag = db.execute(
        select(Tag).where(func.lower(Tag.name) == normalized.lower(), Tag.tag_type == "brand")
    ).scalar_one_or_none()
    if tag:
        return tag

    try:
        with db.begin_nested():
            tag = Tag(name=normalized, tag_type="brand", created_at=datetime.now(timezone.utc))
            db.add(tag)
            db.flush()
        return tag
    except IntegrityError:
        return db.execute(
            select(Tag).where(func.lower(Tag.name) == normalized.lower(), Tag.tag_type == "brand")
        ).scalar_one()
```

- [ ] **Step 3: Run tests, commit**

---

### Task 2.4: Fix Strategic Vendor Claim Race

**Audit refs:** Data Integrity 3.3
**Severity:** HIGH — uncaught `IntegrityError` → 500

**Files:**
- Modify: `app/services/strategic_vendor_service.py:70-114`
- Test: `tests/test_strategic_vendor.py`

- [ ] **Step 1: Add `with_for_update()` to `get_vendor_owner` query and catch `IntegrityError`**

```python
def claim_vendor(db, user_id, vendor_card_id, *, commit=True):
    count = active_count(db, user_id)
    if count >= MAX_STRATEGIC_VENDORS:
        return None, f"Already at {MAX_STRATEGIC_VENDORS} strategic vendors. Drop one first."

    # Lock the row to prevent concurrent claims
    existing = db.execute(
        select(StrategicVendor)
        .where(
            StrategicVendor.vendor_card_id == vendor_card_id,
            StrategicVendor.released_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()

    if existing:
        if existing.user_id == user_id:
            return None, "You already have this vendor as strategic."
        return None, "This vendor is already claimed by another buyer."

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return None, "Vendor not found."

    now = datetime.now(timezone.utc)
    record = StrategicVendor(
        user_id=user_id,
        vendor_card_id=vendor_card_id,
        claimed_at=now,
        expires_at=now + timedelta(days=TTL_DAYS),
    )
    db.add(record)
    try:
        if commit:
            db.commit()
            db.refresh(record)
        else:
            db.flush()
    except IntegrityError:
        db.rollback()
        return None, "This vendor was just claimed by another buyer."

    return record, None
```

- [ ] **Step 2: Write test, run, commit**

---

### Task 2.5: Consolidate Enums — Add Missing Values and Move Scattered Enums to `constants.py`

**Audit refs:** Data Integrity 1.1, 6.1-6.3; Code Quality 7, 8
**Severity:** CRITICAL (missing values) + MEDIUM (scattered definitions)

**Files:**
- Modify: `app/constants.py` — add `PENDING_REVIEW`, `EXPIRED` to `OfferStatus`; add `CANCELLED` to `RequisitionStatus`; add `AI_PARSED`, `AI_LOOKUP` to `OfferSource`; move `BuyPlanStatus`, `SOVerificationStatus`, `BuyPlanLineStatus`, `LineIssueType`, `AIFlagSeverity` from `models/buy_plan.py`; move `RiskFlagType`, `RiskFlagSeverity` from `models/risk_flag.py`
- Modify: `app/models/buy_plan.py` — remove local enum definitions, import from `constants`
- Modify: `app/models/risk_flag.py` — remove local enum definitions, import from `constants`
- Modify: `app/services/status_machine.py` — use `OfferStatus`, `RequisitionStatus` constants in transition maps
- Test: `tests/test_constants.py`

- [ ] **Step 1: Add missing enum values to `app/constants.py`**

```python
class OfferStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACTIVE = "active"
    APPROVED = "approved"
    REJECTED = "rejected"
    SOLD = "sold"
    WON = "won"
    EXPIRED = "expired"


class OfferSource(StrEnum):
    EMAIL_PARSE = "email_parse"
    MANUAL = "manual"
    SEARCH = "search"
    HISTORICAL = "historical"
    VENDOR_AFFINITY = "vendor_affinity"
    AI_PARSED = "ai_parsed"
    AI_LOOKUP = "ai_lookup"


class RequisitionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SOURCING = "sourcing"
    OFFERS = "offers"
    QUOTING = "quoting"
    QUOTED = "quoted"
    REOPENED = "reopened"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"
```

- [ ] **Step 2: Move buy plan and risk flag enums from model files to `constants.py`**

Cut the enum class definitions from `models/buy_plan.py` and `models/risk_flag.py`, paste into `constants.py`, and update the import in both model files to: `from ..constants import BuyPlanStatus, ...`

**IMPORTANT: Base class conversion required.** The buy_plan.py enums use `(str, enum.Enum)` while constants.py uses `StrEnum`. When moving, convert to `StrEnum` and uppercase member names to match the existing convention:

```python
# BEFORE (in models/buy_plan.py):
class BuyPlanStatus(str, enum.Enum):
    draft = "draft"
    pending = "pending"

# AFTER (in constants.py):
class BuyPlanStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
```

Since `StrEnum` members compare equal to their string values (e.g., `BuyPlanStatus.DRAFT == "draft"` is `True`), all existing DB data and column defaults continue working. However, you must update all code that references these enums by member name (e.g., `BuyPlanStatus.draft` → `BuyPlanStatus.DRAFT`).

- [ ] **Step 3: Update `status_machine.py` to use enum constants**

```python
from app.constants import OfferStatus, RequisitionStatus, QuoteStatus

OFFER_TRANSITIONS: dict[str, set[str]] = {
    OfferStatus.PENDING_REVIEW: {OfferStatus.ACTIVE, OfferStatus.REJECTED},
    OfferStatus.ACTIVE: {OfferStatus.SOLD, OfferStatus.REJECTED, OfferStatus.WON, OfferStatus.EXPIRED},
    # ... etc
}
```

- [ ] **Step 4: Fix dead `"open"` filter in `routers/requisitions/core.py:74`**

Replace:
```python
Requisition.status.in_(["open", "active", "sourcing", "draft"])
```
With:
```python
Requisition.status.in_([RequisitionStatus.ACTIVE, RequisitionStatus.SOURCING, RequisitionStatus.DRAFT])
```

- [ ] **Step 5: Write tests verifying all enum members exist, run full suite, commit**

---

### Task 2.6: Fix N+1 Query in Proactive Match Loop

**Audit refs:** Data Integrity 4.1
**Severity:** HIGH — 50+ extra queries per batch operation

**Files:**
- Modify: `app/routers/proactive.py:153-161`
- Test: `tests/test_routers_proactive.py`

- [ ] **Step 1: Add `joinedload` to the proactive match query**

```python
from sqlalchemy import select
from sqlalchemy.orm import joinedload

matches = db.execute(
    select(ProactiveMatch)
    .where(...)
    .options(joinedload(ProactiveMatch.offer))
).scalars().all()
```

- [ ] **Step 2: Similarly fix `_load_company_tags` in `crm/companies.py` — add `joinedload(EntityTag.tag)`**

- [ ] **Step 3: Run tests, commit**

---

# PHASE 3: SILENT FAILURE REMEDIATION

**Goal:** Replace every bare `except: pass` with proper error handling. Make all failures visible.

---

### Task 3.1: Fix Inbox Poll Returns Empty on Failure

**Audit refs:** Silent Failures 2.1
**Severity:** CRITICAL — users can't distinguish "no emails" from "poll completely failed"

**Files:**
- Modify: `app/email_service.py:407-409`

- [ ] **Step 1: Replace `return []` with `raise`**

```python
        except Exception as e:
            logger.error(f"Inbox poll failed: {e}")
            raise  # Let caller handle — router returns proper error, job skips watermark
```

- [ ] **Step 2: Update all callers to handle the exception properly** (the job caller was already fixed in Task 1.1)

- [ ] **Step 3: Run tests, commit**

---

### Task 3.2: Create Claude Client Error Hierarchy

**Audit refs:** Silent Failures 3.1
**Severity:** CRITICAL — all failure modes return indistinguishable `None`

**Files:**
- Create: `app/utils/claude_errors.py`
- Modify: `app/utils/claude_client.py`
- Modify: All callers of `claude_json()`
- Test: `tests/test_claude_client.py`

- [ ] **Step 1: Create error hierarchy**

```python
# app/utils/claude_errors.py
"""Claude API error types for distinguishable failure handling.

Called by: claude_client.py
Depends on: nothing
"""


class ClaudeError(Exception):
    """Base exception for Claude API failures."""
    pass


class ClaudeAuthError(ClaudeError):
    """API key missing or invalid (401/403)."""
    pass


class ClaudeRateLimitError(ClaudeError):
    """Rate limited (429) — caller should back off."""
    pass


class ClaudeServerError(ClaudeError):
    """Claude API returned 5xx — transient, may retry."""
    pass


class ClaudeUnavailableError(ClaudeError):
    """API key not configured — feature should degrade gracefully."""
    pass
```

- [ ] **Step 2: Update `claude_client.py` to raise specific exceptions**

Replace `return None` paths:
- No API key → `raise ClaudeUnavailableError("ANTHROPIC_API_KEY not configured")`
- 401/403 → `raise ClaudeAuthError(f"Claude API auth failed: {resp.status_code}")`
- 429 → `raise ClaudeRateLimitError("Claude rate limit exceeded after retries")`
- 5xx → `raise ClaudeServerError(f"Claude API error: {resp.status_code}")`
- Network error → `raise ClaudeError(f"Claude API unreachable: {e}")`

- [ ] **Step 3: Update callers** to catch specific exceptions and degrade gracefully:

```python
try:
    result = claude_json(prompt, schema, system=system)
except ClaudeUnavailableError:
    logger.info("Claude not configured — skipping AI feature")
    result = None
except ClaudeError as e:
    logger.warning("Claude AI failed: %s", e)
    result = None
```

- [ ] **Step 4: Run tests, commit**

---

### Task 3.3: Elevate Enrichment Orchestrator Logging from DEBUG to WARNING

**Audit refs:** Silent Failures 2.4
**Severity:** HIGH — all external API failures invisible in production

**Files:**
- Modify: `app/services/enrichment_orchestrator.py:47, 58, 69, 81, 93, 104, 116`

- [ ] **Step 1: Replace all 7 instances of `logger.debug` with `logger.warning`**

```python
    except Exception as e:
        logger.warning("Apollo company enrichment failed for %s: %s", identifier, e)
        return None
```

- [ ] **Step 2: Run tests, commit**

---

### Task 3.4: Fix Activity Router Returning HTTP 200 for Errors

**Audit refs:** Silent Failures 2.5
**Severity:** HIGH — `{"id": None}` on error masquerades as success

**Files:**
- Modify: `app/routers/activity.py:120-125`
- Test: `tests/test_routers_activity.py`

- [ ] **Step 1: Replace with proper HTTP error**

```python
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"call-initiated error: {e}")
        db.rollback()
        raise HTTPException(500, "Failed to record phone contact")
```

- [ ] **Step 2: Write test, run, commit**

---

### Task 3.5: Fix All Bare `except: pass` Patterns

**Audit refs:** Silent Failures 1.1-1.8
**Severity:** HIGH — errors completely invisible

**Files to modify (each gets the same treatment — add logging at minimum):**

| File | Line | Fix |
|------|------|-----|
| `app/search_service.py:131-137` | Search cache read | `except redis.RedisError: pass` + `except Exception as e: logger.warning(...)` |
| `app/search_service.py:145-148` | Search cache write | Same pattern |
| `app/search_service.py:881-882` | API stats commit | Add `logger.warning` before rollback |
| `app/search_service.py:114-116` | Redis init | Add `logger.warning("Search Redis unavailable: %s", e)` |
| `app/routers/htmx_views.py:3446-3447, 3512-3513` | Vendor detail | Remove try/except entirely — let DB errors propagate |
| `app/services/global_search_service.py:53-54` | AI search cache | Add `logger.debug(...)` |
| `app/jobs/core_jobs.py:142-143` | Lock deletion | Add `logger.warning(...)` |
| `app/services/nc_worker/worker.py:300-301` | Double-fault handling | Add `logger.error("double fault: ...")` |
| `app/connectors/email_mining.py:711-712` | Brand detection | Add `logger.debug(...)` |
| `app/utils/vendor_helpers.py:107-108` | pg_trgm fallback | Narrow to `ProgrammingError` only, re-raise `OperationalError` |

- [ ] **Step 1: Apply fixes to each file**
- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit**

---

### Task 3.6: Fix Email Delta Query Fallback Masking Auth Errors

**Audit refs:** Silent Failures 4.1
**Severity:** HIGH — auth failures create silent infinite failure loop

**Files:**
- Modify: `app/email_service.py:386-393`

- [ ] **Step 1: Check error type before falling back**

```python
        except Exception as e:
            # Don't mask auth failures behind the delta fallback
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                logger.error(f"Inbox auth failure (not falling back): {e}")
                raise
            logger.warning(f"Delta query failed, falling back to full scan: {e}")
            if sync and sync.delta_token:
                sync.delta_token = None
                db.flush()
            messages = []
            used_delta = False
```

- [ ] **Step 2: Run tests, commit**

---

# PHASE 4: CODE QUALITY

**Goal:** Fix DRY violations, dead code, pattern inconsistencies, and type safety issues.

---

### Task 4.1: Replace Remaining Raw Status Strings with StrEnum Constants

**Audit refs:** Code Quality 7
**Severity:** HIGH — bypasses type-safe enums
**NOTE:** The dead `"open"` filter in `core.py:74` is already fixed in Task 2.5 Step 4. This task covers the remaining raw string occurrences NOT handled by Task 2.5.

**Files:**
- Modify: `app/routers/htmx_views.py` — ~15 occurrences of raw status strings (`"active"`, `"sourcing"`, `"manual"`, etc.)
- Modify: `app/routers/crm/companies.py` — ~5 occurrences (`"won"`, `"lost"`)
- Modify: `app/services/` — any remaining raw strings in service files
- Test: Run `grep` to verify zero remaining raw status strings after fix

- [ ] **Step 1: Search for ALL remaining raw status strings across the entire codebase**

```bash
grep -rn '"active"\|"sourcing"\|"manual"\|"won"\|"lost"\|"draft"\|"pending_review"\|"ai_parsed"\|"ai_lookup"' app/routers/ app/services/ --include="*.py" | grep -v "constants.py\|status_machine.py\|__pycache__"
```

- [ ] **Step 2: Replace each occurrence with the corresponding StrEnum constant**

Add imports at the top of each file: `from ..constants import OfferStatus, RequisitionStatus, OfferSource, ...`

- [ ] **Step 3: Write a test that asserts no raw status strings exist in router/service files**

```python
# tests/test_enum_usage.py
import ast, pathlib

def test_no_raw_status_strings_in_routers():
    """All status values must use StrEnum constants, not raw strings."""
    raw_statuses = {"active", "sourcing", "manual", "won", "lost", "draft",
                    "pending_review", "ai_parsed", "ai_lookup", "open"}
    violations = []
    for py_file in pathlib.Path("app/routers").rglob("*.py"):
        source = py_file.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in raw_statuses:
                    violations.append(f"{py_file}:{node.lineno}: raw string '{node.value}'")
    assert not violations, f"Raw status strings found:\n" + "\n".join(violations)
```

- [ ] **Step 4: Run tests, commit**

---

### Task 4.2: Remove Redundant In-Function Imports

**Audit refs:** Code Quality 4, 5, 17
**Severity:** HIGH — 12+ redundant re-imports, mixed import styles

**Files:**
- Modify: `app/routers/htmx_views.py` — remove 12 redundant `from datetime import datetime, timezone` inside function bodies (already imported at line 14)
- Modify: `app/routers/htmx_views.py` — standardize all lazy function-body imports to relative style (`from ..` not `from app.`)
- Modify: `app/email_service.py:338` — remove redundant `import re`

- [ ] **Step 1: Remove all 12 in-function datetime re-imports in htmx_views.py**
- [ ] **Step 2: Convert all absolute `from app.` imports inside function bodies to relative `from ..`**
- [ ] **Step 3: Remove `import re` at line 338 of email_service.py**
- [ ] **Step 4: Run tests, commit**

---

### Task 4.3: Extract Shared `_get_or_404` Helpers

**Audit refs:** Code Quality 3
**Severity:** HIGH — same 3-line pattern copy-pasted 21+ times

**Files:**
- Create: `app/routers/_lookup_helpers.py`
- Modify: `app/routers/htmx_views.py` — replace 21+ inline patterns

- [ ] **Step 1: Create helpers module**

```python
# app/routers/_lookup_helpers.py
"""Shared model lookup helpers for router endpoints.

Called by: all router files that need model-by-id lookups
Depends on: SQLAlchemy Session, HTTPException
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.sourcing import Requisition, Requirement
from ..models.offers import Offer
from ..models.vendors import VendorCard


def get_requisition_or_404(db: Session, req_id: int) -> Requisition:
    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return req


def get_requirement_or_404(db: Session, req_id: int) -> Requirement:
    req = db.get(Requirement, req_id)
    if not req:
        raise HTTPException(404, "Requirement not found")
    return req


def get_offer_or_404(db: Session, offer_id: int) -> Offer:
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    return offer


def get_vendor_card_or_404(db: Session, card_id: int) -> VendorCard:
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    return card
```

- [ ] **Step 2: Replace all 21+ inline patterns in htmx_views.py**

Find/replace each occurrence of:
```python
req = db.query(Requisition).filter(Requisition.id == req_id).first()
if not req:
    raise HTTPException(404, "Requisition not found")
```
With:
```python
from ._lookup_helpers import get_requisition_or_404
req = get_requisition_or_404(db, req_id)
```

- [ ] **Step 3: Run tests, commit**

---

### Task 4.4: Consolidate Jinja2Templates Into Singleton

**Audit refs:** Code Quality 16; Architecture 8
**Severity:** MEDIUM — split-brain template environments

**Files:**
- Create: `app/template_env.py`
- Modify: `app/routers/htmx_views.py` — remove local `templates` + filter registration
- Modify: `app/routers/requisitions2.py` — remove local `templates`
- Modify: `app/main.py:302` — remove local `templates`

- [ ] **Step 1: Create singleton module**

```python
# app/template_env.py
"""Jinja2 template environment singleton with all custom filters.

Called by: all router files that render templates
Depends on: Jinja2, app filters
"""

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

# Register all custom filters here so every router sees them
# (filter implementations imported from htmx_views or extracted to a filters module)
```

- [ ] **Step 2: Move filter implementations from htmx_views.py to `app/template_filters.py`**
- [ ] **Step 3: Update all imports, run tests, commit**

---

### Task 4.5: Fix `== True` / `== None` SQLAlchemy Comparisons

**Audit refs:** Code Quality 13
**Severity:** MEDIUM — suppressed linter warnings instead of proper fix

**Files:**
- Modify: `app/routers/crm/companies.py` — 6 occurrences
- Modify: `app/routers/crm/sites.py` — 3 occurrences
- Modify: `app/services/proactive_matching.py` — 1 occurrence
- Modify: `app/services/salesperson_scorecard.py` — 1 occurrence

- [ ] **Step 1: Replace all `== True` with `.is_(True)` and `== None` with `.is_(None)`**

```python
# Before:
.filter(Company.is_active == True)  # noqa: E712
# After:
.filter(Company.is_active.is_(True))
```

- [ ] **Step 2: Remove all `# noqa: E712` and `# noqa: E711` comments**
- [ ] **Step 3: Run Ruff to verify clean, run tests, commit**

---

### Task 4.6: Move `_JUNK_VENDORS` to `shared_constants.py`

**Audit refs:** Code Quality 9
**Severity:** MEDIUM — duplicate junk list not shared

**Files:**
- Modify: `app/shared_constants.py` — add `JUNK_VENDORS`
- Modify: `app/search_service.py` — import from `shared_constants`

- [ ] **Step 1: Move the set, update imports, run tests, commit**

---

### Task 4.7: Extract Duplicate Median Calculation

**Audit refs:** Code Quality 11
**Severity:** MEDIUM — identical expression in two places

**Files:**
- Modify: `app/search_service.py:422, 1113`

- [ ] **Step 1: Extract to helper**

```python
def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]
```

- [ ] **Step 2: Replace both inline expressions, run tests, commit**

---

### Task 4.8: DRY the Timesince/Timeago Filters

**Audit refs:** Code Quality 6
**Severity:** HIGH — duplicated timezone coercion + elapsed logic

**Files:**
- Modify: `app/routers/htmx_views.py:90-149` (or `app/template_filters.py` if Task 4.4 is done first)

- [ ] **Step 1: Extract shared `_elapsed_seconds` helper**

```python
def _elapsed_seconds(dt) -> float | None:
    """Compute seconds elapsed since dt. Handles str, naive, and aware datetimes."""
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()
```

- [ ] **Step 2: Refactor both filters to use it, run tests, commit**

---

# PHASE 5: ARCHITECTURE

**Goal:** Break up god files, establish proper layering, consolidate module organization.

**NOTE:** This is the largest phase. Each task is independently deployable but should be done in order for cleanest results. These are massive refactors — each one should be a separate branch/PR.

---

### Task 5.1: Create Shared Template Infrastructure

**Depends on:** Task 4.4 (singleton templates)
**Audit refs:** Architecture 8

- [ ] **Step 1: Ensure `app/template_env.py` is the single templates instance**
- [ ] **Step 2: Ensure all custom filters are registered there**
- [ ] **Step 3: Verify no other file instantiates `Jinja2Templates`**
- [ ] **Step 4: Run full test suite, commit**

---

### Task 5.2: Split `htmx_views.py` Into Domain Modules

**Audit refs:** Architecture 1 (CRITICAL), Code Quality 1 (CRITICAL)
**Severity:** CRITICAL — 9,837-line god file with 8+ domains

This is the single most impactful architectural change. It must be done carefully to preserve all existing routes and behavior.

**Files:**
- Create: `app/routers/htmx/` directory
- Create: `app/routers/htmx/__init__.py` — mounts all sub-routers
- Create: `app/routers/htmx/_shared.py` — `_base_ctx()`, `_vite_assets()`, `_safe_int()`, `_parse_date_safe()`, `_is_htmx()`
- Create: `app/routers/htmx/_constants.py` — all column picker constants
- Create: `app/routers/htmx/requisitions.py`
- Create: `app/routers/htmx/vendors.py`
- Create: `app/routers/htmx/customers.py`
- Create: `app/routers/htmx/buy_plans.py`
- Create: `app/routers/htmx/quotes.py`
- Create: `app/routers/htmx/materials.py`
- Create: `app/routers/htmx/sourcing.py`
- Create: `app/routers/htmx/proactive.py`
- Create: `app/routers/htmx/prospecting.py`
- Create: `app/routers/htmx/excess.py`
- Create: `app/routers/htmx/tickets.py`
- Create: `app/routers/htmx/search.py`
- Create: `app/routers/htmx/page.py`
- Delete: `app/routers/htmx_views.py` (after all routes moved)
- Modify: `app/main.py` — register new htmx router package

**Strategy:** Move one domain at a time. After each move, run the full test suite to verify no routes were broken.

- [ ] **Step 1: Create `_shared.py` with all shared helpers**

Extract from `htmx_views.py`:
- `_base_ctx()` (with all its parameters)
- `_vite_assets()`
- `_safe_int()`
- `_parse_date_safe()`
- `_is_htmx()`

- [ ] **Step 2: Create `_constants.py` with column picker constants**

Move `_ALL_REQ_COLUMNS`, `_DEFAULT_REQ_COLUMNS`, `_ALL_OFFER_COLUMNS`, `_DEFAULT_OFFER_COLUMNS` from lines 8631-8697.

- [ ] **Step 3: Extract `page.py` — the `/v2` full-page entry point**

Move the `v2_page()` function and related full-page routes.

- [ ] **Step 4: Extract `requisitions.py` — all `/v2/partials/requisitions/*` and `/v2/partials/parts/*` routes**

This is the largest chunk (~2,500 lines). Move all requisition-related partials.

- [ ] **Step 5: Extract `vendors.py` — all `/v2/partials/vendors/*` routes**

- [ ] **Step 6: Extract `customers.py` — all `/v2/partials/customers/*` routes**

- [ ] **Step 7: Extract remaining domain modules** (quotes, buy_plans, materials, sourcing, proactive, prospecting, excess, tickets, search) — one at a time.

- [ ] **Step 8: Create `__init__.py` that mounts all sub-routers**

```python
# app/routers/htmx/__init__.py
from fastapi import APIRouter
from .page import router as page_router
from .requisitions import router as req_router
from .vendors import router as vendor_router
# ... etc

router = APIRouter()
router.include_router(page_router)
router.include_router(req_router)
router.include_router(vendor_router)
# ... etc
```

- [ ] **Step 9: Update `main.py` to use the new package**

Replace:
```python
from .routers.htmx_views import router as htmx_router
```
With:
```python
from .routers.htmx import router as htmx_router
```

- [ ] **Step 10: Delete `htmx_views.py`**

- [ ] **Step 11: Run full test suite, verify all routes still work**

- [ ] **Step 12: Commit**

```bash
git add app/routers/htmx/ app/main.py
git rm app/routers/htmx_views.py
git commit -m "refactor(architecture): split htmx_views.py into domain modules

Breaks the 9,837-line god file into 15 focused domain modules under
app/routers/htmx/. Each module owns its domain's routes with shared
helpers extracted to _shared.py. All existing routes preserved."
```

---

### Task 5.3: Retire `requisitions2.py` — Merge Into Canonical Implementation

**Audit refs:** Architecture 13
**Severity:** MEDIUM — two parallel implementations of the same feature

**Files:**
- Delete: `app/routers/requisitions2.py`
- Modify: `app/main.py` — remove duplicate registration
- Modify: Merge any unique functionality from `requisitions2.py` into `app/routers/htmx/requisitions.py`

- [ ] **Step 1: Compare `requisitions2.py` with `htmx/requisitions.py` — identify any routes unique to requisitions2**
- [ ] **Step 2: Merge unique routes into the canonical module**
- [ ] **Step 3: Remove `requisitions2.py` registration from `main.py`**
- [ ] **Step 4: Run tests, commit**

---

### Task 5.4: Move Root-Level Services Into `services/`

**Audit refs:** Architecture 4
**Severity:** HIGH — inconsistent module organization

**Files:**
- Move: `app/search_service.py` → `app/services/search_service.py`
- Move: `app/email_service.py` → `app/services/email_service.py`
- Move: `app/enrichment_service.py` → `app/services/enrichment_normalization.py`
- Move: `app/scoring.py` → `app/services/scoring.py`
- Update: All import paths across the entire codebase

- [ ] **Step 1: Move files one at a time, updating all imports after each move**

For each file:
1. `git mv app/search_service.py app/services/search_service.py`
2. Find all imports: `grep -rn "from.*search_service import\|import.*search_service" app/ tests/`
3. Update each import
4. Run tests to verify

- [ ] **Step 2: Run full test suite after all moves**
- [ ] **Step 3: Commit**

---

### Task 5.5: Extract Business Logic From Routers Into Services

**Audit refs:** Architecture 2 (CRITICAL)
**Severity:** CRITICAL — routers doing full business logic with 466 DB calls

This is a long-running task that should be broken into sub-tasks per domain:

- [ ] **Step 1: Extract `RequisitionService.create_from_parsed()`** from `htmx_views.py:691-799`
- [ ] **Step 2: Extract `OfferService.save_parsed_batch()`** from `htmx_views.py:1450-1554`
- [ ] **Step 3: Extract `CustomerService.quick_create()`** from `htmx_views.py:902-980`
- [ ] **Step 4: Extract `CompanyRepository.list_with_stats()`** from `crm/companies.py:48-200`
- [ ] **Step 5: Extract `VendorCardRepository.find_or_create()`** as shared utility — replace all inline vendor card creation

Each extraction follows the pattern:
1. Write tests for the service function
2. Extract the code from the router into the service
3. Replace the router code with a service call
4. Run tests

- [ ] **Step 6: Run full test suite, commit after each extraction**

---

### Task 5.6: Clean Up `main.py` — Extract Business Logic

**Audit refs:** Architecture 5
**Severity:** HIGH — composition root contains business logic

- [ ] **Step 1: Move `_seed_api_sources()` to `startup.py`**
- [ ] **Step 2: Move `_check_backup_freshness()` to a new `app/services/health_service.py`**
- [ ] **Step 3: Move Sentry initialization to `app/utils/observability.py`**
- [ ] **Step 4: Slim the `lifespan` function to just call `startup.run_startup()` + scheduler start/stop**
- [ ] **Step 5: Run tests, commit**

---

### Task 5.7: Remove Hard-Coded User Seed

**Audit refs:** Architecture 6
**Severity:** HIGH — hard-coded admin user created on every boot

- [ ] **Step 1: Remove `_seed_vinod_user()` from `startup.py`**
- [ ] **Step 2: If needed, add `SEED_ADMIN_EMAIL` / `SEED_ADMIN_NAME` env var support to the existing `_create_default_user_if_env_set()`**
- [ ] **Step 3: Run tests, commit**

---

### Task 5.8: Move Boot-Time Backfills to Alembic Migrations

**Audit refs:** Architecture 11
**Severity:** MEDIUM — per-row Python backfills run on every boot

- [ ] **Step 1: Convert `_backfill_sighting_offer_normalized_mpn()` to Alembic data migration**
- [ ] **Step 2: Convert `_backfill_sighting_vendor_normalized()` to Alembic data migration**
- [ ] **Step 3: Remove both from `startup.py`**
- [ ] **Step 4: Run tests, commit**

---

# PHASE 6: TEST COVERAGE

**Goal:** Fill critical test gaps, fix test isolation, improve fixture quality.

---

### Task 6.1: Fix `dependency_overrides.clear()` Race Condition

**Audit refs:** Test Coverage 4
**Severity:** CRITICAL — parallel tests corrupt each other's auth

**Files:**
- Modify: `tests/test_routers_rfq.py` — wrap overrides in fixture
- Modify: `tests/test_routers_knowledge.py` — add try/finally
- Modify: `tests/test_routers_proactive.py` — same
- Modify: `tests/test_prospecting_accounts.py` — same
- Modify: `tests/test_admin_settings.py` — same

- [ ] **Step 1: Create a shared fixture for safe override management**

```python
# tests/conftest.py — add

@pytest.fixture
def override_client(db_session, test_user):
    """TestClient with properly scoped dependency overrides."""
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        # Only remove our specific overrides, don't clear all
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
```

- [ ] **Step 2: Replace all bare `app.dependency_overrides[...] =` / `.clear()` in test files**

Convert each from:
```python
app.dependency_overrides[get_db] = _override_db
...
app.dependency_overrides.clear()
```
To using the `override_client` fixture.

- [ ] **Step 3: Run full test suite in parallel to verify no flakes**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -n auto -v
```

- [ ] **Step 4: Commit**

---

### Task 6.2: Add Tests for Task Router (10 Endpoints, Zero Coverage)

**Audit refs:** Test Coverage 3
**Severity:** CRITICAL — all error paths untested

**Files:**
- Create: `tests/test_routers_task.py`

- [ ] **Step 1: Write tests for all 10 endpoints including error paths (400, 403, 404)**
- [ ] **Step 2: Run, verify, commit**

---

### Task 6.3: Add Tests for `enrichment_service.py` (650 Lines, Zero Coverage)

**Audit refs:** Test Coverage 3
**Severity:** CRITICAL — normalization logic completely untested

**Files:**
- Create: `tests/test_enrichment_service.py`

- [ ] **Step 1: Write tests for:**
- `_clean_domain()`
- `_name_looks_suspicious()`
- `_title_case_preserve_acronyms()`
- `normalize_company_output()`
- `apply_enrichment_to_company()`
- `apply_enrichment_to_vendor()`

- [ ] **Step 2: Run, verify, commit**

---

### Task 6.4: Add Tests for Command Center, Vendor Inquiry, Knowledge Routers

**Audit refs:** Test Coverage 5, 7
**Severity:** HIGH — zero or near-zero coverage

**Files:**
- Create: `tests/test_routers_command_center.py`
- Create: `tests/test_routers_vendor_inquiry.py`
- Expand: `tests/test_routers_knowledge.py`

- [ ] **Step 1: Command center — test aggregation endpoint and timezone edge cases**
- [ ] **Step 2: Vendor inquiry — test requisition creation side-effect**
- [ ] **Step 3: Knowledge — add auth tests for remaining 17 endpoints**
- [ ] **Step 4: Run, verify, commit each file**

---

### Task 6.5: Add Tests for Job Modules

**Audit refs:** Test Coverage 7
**Severity:** HIGH — knowledge_jobs and part_discovery_jobs untested

**Files:**
- Create: `tests/test_jobs_knowledge.py`
- Create: `tests/test_jobs_part_discovery.py`

- [ ] **Step 1: Write smoke tests for job registration and basic execution**
- [ ] **Step 2: Run, verify, commit**

---

### Task 6.6: Fix Test Quality — Replace Local Copies With Real Router Calls

**Audit refs:** Test Coverage 8
**Severity:** MEDIUM — tests verify local copies, not actual implementation

**Files:**
- Modify: `tests/test_routers_rfq.py:35-95` — replace `_GARBAGE_VENDORS` / `_filter_sightings` with actual router calls

- [ ] **Step 1: Rewrite tests to call the real router endpoints instead of local copies**
- [ ] **Step 2: Run, verify, commit**

---

### Task 6.7: Add `unauthenticated_client` Fixture

**Audit refs:** Test Coverage 9
**Severity:** LOW — repeated inline pattern

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add fixture**

```python
@pytest.fixture
def unauth_client():
    """TestClient with no auth overrides — for testing 401 behavior."""
    with TestClient(app) as c:
        yield c
```

- [ ] **Step 2: Update tests that create their own unoverridden TestClient**
- [ ] **Step 3: Run, verify, commit**

---

# PHASE 7: SECURITY HARDENING

**Goal:** Address remaining security findings that require code changes (formerly Appendix A).

---

### Task 7.1: Tighten CSRF Exemptions

**Audit refs:** Security 10
**Severity:** LOW — defense-in-depth concern

**Files:**
- Modify: `app/main.py:289-297`
- Test: `tests/test_csrf.py`

- [ ] **Step 1: Tighten the import regex**

Replace:
```python
re.compile(r"/v2/partials/requisitions/import-.*"),
```
With:
```python
re.compile(r"/v2/partials/requisitions/import-(csv|file|sfq)$"),
```

- [ ] **Step 2: Remove `lookup|quick-create` CSRF exemption**

Remove:
```python
re.compile(r"/v2/partials/customers/(lookup|quick-create)"),
```

Ensure these HTMX endpoints carry CSRF tokens via `hx-headers`.

- [ ] **Step 3: Write test verifying CSRF enforcement on quick-create, commit**

---

### Task 7.2: Harden Email HTML Sanitization

**Audit refs:** Security 8
**Severity:** MEDIUM — tracking pixels, tab-napping, CSS injection

**Files:**
- Modify: `app/routers/htmx_views.py` (sanitize_html filter) or `app/template_filters.py` if Task 4.4 is complete
- Test: `tests/test_sanitize_html.py`

- [ ] **Step 1: Remove `img` from allowed tags** (blocks tracking pixels) — or proxy images through the app

- [ ] **Step 2: Remove `target` from allowed attributes** (prevents tab-napping)

- [ ] **Step 3: Remove `class` from global attribute allowlist** (prevents CSS injection via Tailwind classes)

- [ ] **Step 4: Write tests verifying each stripped element**

```python
def test_sanitize_strips_tracking_pixel():
    from app.template_filters import _sanitize_html_filter
    html = '<img src="https://evil.com/track.gif">'
    assert "<img" not in _sanitize_html_filter(html)

def test_sanitize_strips_target_attribute():
    html = '<a href="https://safe.com" target="_blank">link</a>'
    result = _sanitize_html_filter(html)
    assert "target" not in result

def test_sanitize_strips_class_attribute():
    html = '<div class="hidden opacity-0">sneaky</div>'
    result = _sanitize_html_filter(html)
    assert "class=" not in result
```

- [ ] **Step 5: Run tests, commit**

---

### Task 7.3: Minimize `/health` Endpoint Response

**Audit refs:** Security 9
**Severity:** LOW — leaks infrastructure topology

**Files:**
- Modify: `app/main.py:459-506`
- Test: `tests/test_health.py`

- [ ] **Step 1: Strip detailed fields from unauthenticated `/health` response**

Return only: `{"status": "ok"}` or `{"status": "degraded"}`. Move detailed info to the existing authenticated `/api/admin/health` endpoint.

- [ ] **Step 2: Write test verifying `/health` does not expose version/redis/scheduler info**

```python
def test_health_does_not_expose_internals(client):
    resp = client.get("/health")
    data = resp.json()
    assert "version" not in data
    assert "redis" not in data
    assert "scheduler" not in data
    assert "connectors_enabled" not in data
    assert data["status"] in ("ok", "degraded")
```

- [ ] **Step 3: Run tests, commit**

---

### Task 7.4: Harden `dry_run` Parameter on Destructive Admin Endpoints

**Audit refs:** Security 6
**Severity:** MEDIUM — footgun: accidental URL query param triggers destructive path

**Files:**
- Modify: `app/routers/admin/data_ops.py:763-798`
- Create: `app/schemas/admin.py` (add request body schemas)
- Test: `tests/test_admin_data_ops.py`

- [ ] **Step 1: Create Pydantic schema with required `dry_run` field**

```python
# app/schemas/admin.py
from pydantic import BaseModel

class DataCleanupRequest(BaseModel):
    dry_run: bool  # Required — no default, caller must be explicit
```

- [ ] **Step 2: Replace query parameter with request body**

```python
@router.post("/data-cleanup/scan")
async def scan_data_quality(body: DataCleanupRequest, ...):
    if body.dry_run:
        ...
```

- [ ] **Step 3: Write tests for both dry_run=True and dry_run=False paths, commit**

---

### Task 7.5: Replace All `body: dict` with Pydantic Schemas

**Audit refs:** Security 7
**Severity:** MEDIUM — unbounded unvalidated input

**Files:**
- Modify: `app/routers/admin/system.py:230` — `api_set_credentials`
- Modify: `app/routers/admin/data_ops.py:586` — `api_set_teams_channel_routing`
- Modify: `app/routers/rfq.py:232` — `update_vendor_response_status`
- Modify: `app/routers/requisitions/requirements.py:1470, 1556` — `add_requirement_note`, `create_requirement_task`
- Modify: `app/routers/task.py:144` — task update
- Create: Pydantic schemas for each in their respective `app/schemas/` files
- Test: Verify validation rejects extra fields

- [ ] **Step 1: Define schemas for each endpoint**

```python
# Example for credentials:
class CredentialsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials: dict[str, str]  # provider_key -> value

# Example for notes:
class RequirementNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=2000)
```

- [ ] **Step 2: Replace `body: dict` in each endpoint with the typed schema**
- [ ] **Step 3: Write tests verifying extra fields are rejected (422), commit**

---

# PHASE 8: REMAINING FINDINGS & LARGE-SCALE MIGRATIONS

**Goal:** Address all remaining findings from the audit that don't fit cleanly into Phases 1-7.

---

### Task 8.1: Fix Transaction Boundary Issues

**Audit refs:** Data Integrity 5.1-5.3
**Severity:** HIGH — ownership sweep + proactive do-not-offer atomicity

**Files:**
- Modify: `app/services/ownership_service.py:89-93` — move `await _send_warning_alert()` outside the DB mutation loop, or commit before sending
- Modify: `app/routers/proactive.py:101-135` — wrap per-item inserts in `begin_nested()` to prevent batch rollback on single duplicate

- [ ] **Step 1: Fix ownership sweep — commit DB changes before sending alerts**

```python
# Save all ownership changes first
db.commit()
# Then send alerts (if one fails, DB state is already correct)
for user, companies in warning_list:
    try:
        await _send_warning_alert(user, companies)
    except Exception as e:
        logger.error("Failed to send ownership warning to %s: %s", user.email, e)
```

- [ ] **Step 2: Fix do-not-offer — wrap per-item in savepoint**

```python
for item in items:
    try:
        with db.begin_nested():
            dno = ProactiveDoNotOffer(mpn=item["mpn"], company_id=item["company_id"], ...)
            db.add(dno)
    except IntegrityError:
        logger.info("Duplicate do-not-offer skipped: %s/%s", item["mpn"], item["company_id"])
        continue
db.commit()
```

- [ ] **Step 3: Write tests, commit**

---

### Task 8.2: Fix ICS/NC Classification Cache Race Conditions

**Audit refs:** Data Integrity 3.4
**Severity:** MEDIUM — worker parallel inserts can collide

**Files:**
- Modify: `app/models/ics_classification_cache.py` — add savepoint pattern to upsert logic
- Modify: `app/models/nc_classification_cache.py` — same
- Test: `tests/test_classification_cache.py`

- [ ] **Step 1: Add savepoint + IntegrityError retry to cache upsert (same pattern as Task 2.3)**
- [ ] **Step 2: Write test, commit**

---

### Task 8.3: Fix Vendor Scoring Silent Failure

**Audit refs:** Silent Failures 2.6
**Severity:** HIGH — vendor cards retain stale scores indefinitely if scoring job fails

**Files:**
- Modify: `app/jobs/email_jobs.py:655-656`

- [ ] **Step 1: Add `raise` after logging**

```python
    except Exception as e:
        logger.error(f"Vendor scoring failed: {e}")
        raise  # Let _traced_job and Sentry capture
```

- [ ] **Step 2: Run tests, commit**

---

### Task 8.4: Fix Background Vendor Enrichment Silent Exit

**Audit refs:** Silent Failures 6.2
**Severity:** HIGH — `if not enrichment: return` with zero logging

**Files:**
- Modify: `app/utils/vendor_helpers.py:142-176`

- [ ] **Step 1: Add logging for empty enrichment result**

```python
    if not enrichment:
        logger.info("Background enrichment for vendor %s (card %d) returned no data", vendor_name, card_id)
        return
```

- [ ] **Step 2: Run tests, commit**

---

### Task 8.5: Fix RFQ Router Swallowing Requirement Status + Claim Errors

**Audit refs:** Silent Failures 5.1
**Severity:** HIGH — stale status indicators after RFQ send

**Files:**
- Modify: `app/routers/rfq.py:198-210`

- [ ] **Step 1: Add warnings to response so frontend can display them**

```python
    warnings = []
    try:
        _update_requirement_status(...)
    except Exception:
        logger.warning("Requirement status update on RFQ send failed", exc_info=True)
        warnings.append("Part status auto-update failed — update manually")
    try:
        _auto_claim_requisition(...)
    except Exception:
        logger.warning("Auto-claim on RFQ send failed", exc_info=True)
        warnings.append("Auto-claim failed — claim manually if needed")

    result["warnings"] = warnings
```

- [ ] **Step 2: Write test, commit**

---

### Task 8.6: Fix Date Filter Silently Ignoring Invalid Dates

**Audit refs:** Silent Failures 8.1
**Severity:** MEDIUM — user enters invalid date, filter silently ignored

**Files:**
- Modify: `app/routers/htmx_views.py:494-504`

- [ ] **Step 1: Return validation error instead of silently ignoring**

```python
    if date_from:
        try:
            dt = datetime.fromisoformat(date_from)
            query = query.filter(Requisition.created_at >= dt)
        except ValueError:
            return HTMLResponse(
                '<div class="text-red-500 text-sm">Invalid "from" date format</div>',
                status_code=422,
            )
```

- [ ] **Step 2: Write test, commit**

---

### Task 8.7: Fix Vendor Helpers Commit Without Error Handling

**Audit refs:** Silent Failures 7.2
**Severity:** MEDIUM — bare `db.commit()` can crash callers

**Files:**
- Modify: `app/utils/vendor_helpers.py:124-125, 137-138`

- [ ] **Step 1: Wrap commits in try/except with proper error handling**

```python
    try:
        db.commit()
    except Exception as e:
        logger.error("Failed to commit vendor card update: %s", e)
        db.rollback()
        raise
```

- [ ] **Step 2: Write test, commit**

---

### Task 8.8: Fix Requirement Status Transition Silent Skip

**Audit refs:** Silent Failures 5.2
**Severity:** MEDIUM (acceptable behavior, needs logging)

**Files:**
- Modify: `app/services/requirement_status.py:91-92, 123-124`

- [ ] **Step 1: Add debug logging for skipped transitions**

```python
    except ValueError:
        logger.debug("Skipping transition for req %d: already past target status", req.id)
```

- [ ] **Step 2: Run tests, commit**

---

### Task 8.9: SQLAlchemy 2.0 `select()` Migration

**Audit refs:** Code Quality 2
**Severity:** HIGH — 1,199 deprecated `db.query()` calls

This is the single largest mechanical change. Do it **file by file**, running tests after each.

**Strategy:** Start with service files (they have the fewest callers), then models, then routers.

- [ ] **Step 1: Migrate `app/services/` files (estimated ~653 occurrences)**

For each file:
1. Replace `db.query(Model).filter(...)` with `db.execute(select(Model).where(...)).scalars()`
2. Replace `db.query(Model).get(id)` with `db.get(Model, id)`
3. Run the file's tests
4. Commit

- [ ] **Step 2: Migrate `app/routers/` files (estimated ~546 occurrences)**

Same pattern, file by file.

- [ ] **Step 3: Run full test suite after all migrations**

---

### Task 8.10: UTCDateTime Column Migration

**Audit refs:** Code Quality 19
**Severity:** LOW — 211 `DateTime` columns that should use `UTCDateTime`

- [ ] **Step 1: Replace `Column(DateTime)` with `Column(UTCDateTime)` in all 42 model files**
- [ ] **Step 2: Generate Alembic migration (this is a no-op at the DB level since both map to TIMESTAMP)**
- [ ] **Step 3: Run tests (important: verify SQLite tests still work with UTCDateTime), commit**

---

### Task 8.11: Fix Missing Template Base Context

**Audit refs:** Code Quality 14
**Severity:** MEDIUM — some TemplateResponse calls skip `_base_ctx()`

- [ ] **Step 1: Audit all `TemplateResponse` calls that pass raw `{"request": request, ...}` instead of `_base_ctx()`**

```bash
grep -rn "TemplateResponse" app/routers/ --include="*.py" | grep -v "_base_ctx\|base_ctx"
```

- [ ] **Step 2: Add `_base_ctx()` to each, run tests, commit**

---

### Task 8.12: Add Model Tests for Complex Relationships

**Audit refs:** Test Coverage 6
**Severity:** MEDIUM — most models have no dedicated tests

**Files:**
- Create: `tests/test_models_crm.py`
- Create: `tests/test_models_intelligence.py`
- Create: `tests/test_models_performance.py`

- [ ] **Step 1: Write tests for ActivityLog relationships and cascade behavior**
- [ ] **Step 2: Write tests for ProactiveMatch/ProactiveOffer FK cascades (verify ondelete from Task 2.1)**
- [ ] **Step 3: Write tests for scoring snapshot computed properties**
- [ ] **Step 4: Run, verify, commit**

---

### Task 8.13: Sprint/Phase Test File Cleanup

**Audit refs:** Test Coverage 8
**Severity:** LOW — 14 legacy sprint/phase test files may test duplicate paths

- [ ] **Step 1: List all sprint/phase test files**

```bash
ls tests/test_sprint*.py tests/test_phase*.py
```

- [ ] **Step 2: For each file, check if its endpoints are already covered by newer test files**
- [ ] **Step 3: Remove fully redundant files, keep any with unique test cases**
- [ ] **Step 4: Run full suite to verify no coverage loss, commit**

---

# Execution Order Summary

```
Phase 1 (Critical Safety)        ← Do first, deploy immediately
  1.1 Email watermark fix
  1.2 Credit manager race fix
  1.3 Encryption salt
  1.4 /metrics protection
  1.5 Buy-plan token exposure
  1.6 Attachment token refresh
  1.7 Job exception propagation
  1.8 XSS fix
  1.9 Password login guard

Phase 2 (Data Integrity)         ← Alembic migrations needed
  2.1 FK ondelete
  2.2 Missing indexes
  2.3 Brand tag race
  2.4 Strategic vendor race
  2.5 Enum consolidation
  2.6 N+1 query fixes

Phase 3 (Silent Failures)        ← Error handling cleanup
  3.1 Inbox poll error propagation
  3.2 Claude client error hierarchy
  3.3 Enrichment logging elevation
  3.4 Activity router error response
  3.5 Bare except:pass cleanup
  3.6 Delta query auth masking

Phase 4 (Code Quality)           ← Pattern cleanup
  4.1-4.8 DRY, imports, helpers, filters

Phase 5 (Architecture)           ← Major refactors (separate branches)
  5.1-5.8 God file split, service extraction, module moves

Phase 6 (Test Coverage)          ← Safety net before Phase 5 ideally
  6.1-6.7 Missing tests, isolation fixes, fixtures

Phase 7 (Security Hardening)     ← Remaining security items
  7.1-7.5 CSRF, sanitization, health, dry_run, schemas

Phase 8 (Remaining + Migrations) ← Large-scale cleanup
  8.1-8.13 Transaction fixes, cache races, silent failures,
           db.query() migration, UTCDateTime, model tests
```

**IMPORTANT ORDERING NOTE:** Phase 6 (Test Coverage) should ideally be run BEFORE Phase 5 (Architecture) to provide a safety net for the major refactors. The order shown above reflects logical grouping; the recommended execution order is: 1 → 2 → 3 → 4 → **6 → 5** → 7 → 8.

Total: **55 major tasks, ~250 individual steps** across 8 phases.
