# Complete All Pending Work — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate all branches, commit stashed sightings work, fix 11 remaining audit findings, add test coverage for 10 untested modules, and clean up.

**Architecture:** Sequential phases — merge first, then fix code, then add tests. Each phase's tasks are independent and parallelizable within the phase.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, HTMX, Jinja2

---

## Phase 1: Consolidate & Merge (sequential — must complete before Phase 2)

### Task 1.1: Commit Unstaged Changes on Current Branch

**Files:**
- Modified: `app/routers/htmx_views.py`, `app/static/styles.css`, `app/templates/base.html`, `app/templates/htmx/base.html`, `app/templates/htmx/login.html`, `app/templates/htmx/partials/manufacturers/search_results.html`, `app/templates/htmx/partials/requisitions/detail_header.html`, `app/templates/htmx/partials/requisitions/tabs/parts.html`, `app/templates/htmx/partials/requisitions/unified_modal.html`, `app/templates/htmx/partials/shared/mobile_nav.html`, `app/templates/htmx/partials/sightings/vendor_modal.html`, `tailwind.config.js`

- [ ] **Step 1: Stage and commit all unstaged changes**

```bash
cd /root/availai
git add app/routers/htmx_views.py app/static/styles.css app/templates/base.html app/templates/htmx/base.html app/templates/htmx/login.html app/templates/htmx/partials/manufacturers/search_results.html app/templates/htmx/partials/requisitions/detail_header.html app/templates/htmx/partials/requisitions/tabs/parts.html app/templates/htmx/partials/requisitions/unified_modal.html app/templates/htmx/partials/shared/mobile_nav.html app/templates/htmx/partials/sightings/vendor_modal.html tailwind.config.js
git commit -m "feat: UI refinements — modal redesign, vendor modal, parts tab, mobile nav"
```

- [ ] **Step 2: Delete stale send_log JSON files**

```bash
rm -f send_log_*.json
```

### Task 1.2: Merge Current Branch to Main

- [ ] **Step 1: Merge fix/requisition-modal-redesign-and-bugfixes into main**

```bash
cd /root/availai
git checkout main
git merge fix/requisition-modal-redesign-and-bugfixes --no-ff -m "merge: requisition modal redesign, audit fixes, UI polish"
```

- [ ] **Step 2: Push to remote**

```bash
git push origin main
```

### Task 1.3: Pop Sightings Stash and Commit

The stash `stash@{0}` contains 17 files (2,434 lines) of completed Sightings Phase 4 work: SSE live updates, status state machine, activity logging, auto-progress, email preview, vendor overlap, mobile responsive, CSS animations, and 4 test files.

- [ ] **Step 1: Pop the sightings stash**

```bash
cd /root/availai
git stash pop stash@{0}
```

- [ ] **Step 2: Run sightings tests to verify**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_advance_status.py tests/test_sightings_log_activity.py tests/test_sightings_sse.py tests/test_sourcing_auto_progress.py tests/test_sightings_router.py -v
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "feat(sightings): Phase 4 — SSE live updates, status machine, activity log, auto-progress, email preview, vendor overlap, mobile responsive"
```

- [ ] **Step 4: Push**

```bash
git push origin main
```

### Task 1.4: Clean Up Stale Branches and Stashes

- [ ] **Step 1: Delete merged branches**

The sentry branch (`fix/sentry-bulk-fixes-2026-03-23`) is already merged into main. The requisition branch was just merged.

```bash
cd /root/availai
git branch -d fix/sentry-bulk-fixes-2026-03-23
git branch -d fix/requisition-modal-redesign-and-bugfixes
git push origin --delete fix/sentry-bulk-fixes-2026-03-23
git push origin --delete fix/requisition-modal-redesign-and-bugfixes
```

- [ ] **Step 2: Drop stale stashes**

Stashes 1-3 are WIP snapshots on main from before the current work. They're superseded by committed code.

```bash
git stash drop stash@{2}
git stash drop stash@{1}
git stash drop stash@{0}
```

Note: After popping stash@{0} in Task 1.3, the old stash@{1} becomes stash@{0}, etc. Drop from highest index down.

---

## Phase 2: Audit Security Fixes (3 parallel tasks)

### Task 2.1: H3 — File Size Limits on CSV/Excel Import

**Files:**
- Modify: `app/routers/admin/data_ops.py:239,352`
- Test: `tests/test_admin_data_ops.py`

- [ ] **Step 1: Write failing tests**

```python
# In tests/test_admin_data_ops.py — add these tests

def test_import_customers_csv_rejects_oversized_file(admin_client, db_session):
    """H3: CSV import must reject files >10MB."""
    big_content = b"company_name,contact_email\n" + b"x" * (11 * 1024 * 1024)
    resp = admin_client.post(
        "/api/admin/import-customers",
        files={"file": ("big.csv", big_content, "text/csv")},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"].lower()


def test_import_vendors_csv_rejects_oversized_file(admin_client, db_session):
    """H3: Vendor CSV import must reject files >10MB."""
    big_content = b"vendor_name,domain\n" + b"x" * (11 * 1024 * 1024)
    resp = admin_client.post(
        "/api/admin/import-vendors",
        files={"file": ("big.csv", big_content, "text/csv")},
    )
    assert resp.status_code == 400
    assert "too large" in resp.json()["error"].lower()
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_admin_data_ops.py -k "oversized" -v
```

- [ ] **Step 3: Add size check to both import endpoints**

In `app/routers/admin/data_ops.py`, add a constant at the top of the file:

```python
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
```

At line 239 (customer CSV import), replace:
```python
    content = await file.read()
```
with:
```python
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
```

At line 352 (vendor CSV import), apply the same change:
```python
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_admin_data_ops.py -k "oversized" -v
```

- [ ] **Step 5: Commit**

```bash
git add app/routers/admin/data_ops.py tests/test_admin_data_ops.py
git commit -m "fix(security): H3 — add 10MB file size limit on CSV/Excel imports"
```

### Task 2.2: H4 — Move dry_run to Request Body

**Files:**
- Modify: `app/routers/admin/data_ops.py:764-799`
- Modify: any templates that call `?dry_run=false` (search first)
- Test: `tests/test_admin_data_ops.py`

- [ ] **Step 1: Search for callers**

```bash
cd /root/availai && grep -rn "dry_run" app/templates/ app/static/ --include="*.html" --include="*.js"
```

- [ ] **Step 2: Write failing test**

```python
def test_data_cleanup_scan_requires_body_not_query(admin_client, db_session):
    """H4: dry_run must be in request body, not query param."""
    # Query param should be ignored — always dry_run=True by default
    resp = admin_client.post(
        "/api/admin/data-cleanup/scan",
        json={"dry_run": True},
    )
    assert resp.status_code == 200

    # Confirm body param works
    resp = admin_client.post(
        "/api/admin/data-cleanup/scan",
        json={"dry_run": False, "confirm": True},
    )
    assert resp.status_code == 200
```

- [ ] **Step 3: Create Pydantic schema and update endpoints**

Add to `app/schemas/admin.py` (or create if needed):

```python
from pydantic import BaseModel

class DataCleanupRequest(BaseModel):
    dry_run: bool = True
    confirm: bool = False
```

Update both endpoints in `app/routers/admin/data_ops.py`:

```python
from ...schemas.admin import DataCleanupRequest

@router.post("/api/admin/data-cleanup/scan")
async def scan_data_quality(
    body: DataCleanupRequest = DataCleanupRequest(),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not body.dry_run and not body.confirm:
        raise HTTPException(400, "Set confirm=true to execute destructive operations")
    from ...services.data_cleanup_service import scan_junk_data
    return scan_junk_data(db, dry_run=body.dry_run)
```

Apply the same pattern to `fix_sentinel_dates_endpoint`.

- [ ] **Step 4: Update any template callers to use fetch body instead of query param**

- [ ] **Step 5: Run tests — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_admin_data_ops.py -k "cleanup" -v
```

- [ ] **Step 6: Commit**

```bash
git add app/routers/admin/data_ops.py app/schemas/admin.py tests/test_admin_data_ops.py
git commit -m "fix(security): H4 — move dry_run to request body with confirm guard"
```

### Task 2.3: H6 — File Type Validation on Offer Attachments

**Files:**
- Modify: `app/routers/crm/offers.py:701-724`
- Test: `tests/test_offers.py`

- [ ] **Step 1: Write failing test**

```python
def test_offer_attachment_rejects_executable(auth_client, db_session, sample_offer):
    """H6: Only safe file types allowed for offer attachments."""
    resp = auth_client.post(
        f"/api/offers/{sample_offer.id}/attachments",
        files={"file": ("malware.exe", b"MZ...", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "file type" in resp.json()["error"].lower()
```

- [ ] **Step 2: Add allowed extensions check**

In `app/routers/crm/offers.py`, add near the top:

```python
ALLOWED_OFFER_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".txt", ".zip"}
```

In `upload_offer_attachment`, after the size check at line 714, add:

```python
    import os
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_OFFER_EXTENSIONS:
        raise HTTPException(400, f"File type '{ext}' not allowed. Accepted: {', '.join(sorted(ALLOWED_OFFER_EXTENSIONS))}")
```

- [ ] **Step 3: Run tests — expect PASS**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_offers.py -k "attachment" -v
```

- [ ] **Step 4: Commit**

```bash
git add app/routers/crm/offers.py tests/test_offers.py
git commit -m "fix(security): H6 — validate file types on offer attachment upload"
```

---

## Phase 3: Audit Data Integrity Fixes (4 parallel tasks)

### Task 3.1: H8 — AI Contact Field Truncation

**Files:**
- Modify: `app/routers/ai.py:167-184`
- Test: `tests/test_routers_ai.py`

The `ProspectContact` model has `String(255)` on `full_name`, `title`, `email`, `phone`, `linkedin_url`, `source`, `confidence`. Claude can return arbitrarily long strings.

- [ ] **Step 1: Write failing test**

```python
def test_ai_contacts_truncates_long_fields(auth_client, db_session):
    """H8: Fields from Claude must be truncated to column max lengths."""
    # Mock Claude returning long strings, verify DB fields are ≤255 chars
    ...
```

- [ ] **Step 2: Add truncation helper before DB insert**

In `app/routers/ai.py`, before the `for c in merged:` loop (line 168), add a truncation step:

```python
    _MAX_LEN = {"full_name": 255, "title": 255, "email": 255, "phone": 50, "linkedin_url": 512, "source": 100}
    for c in merged:
        for field, max_len in _MAX_LEN.items():
            if c.get(field) and len(c[field]) > max_len:
                c[field] = c[field][:max_len]
```

- [ ] **Step 3: Run tests — expect PASS**

- [ ] **Step 4: Commit**

```bash
git add app/routers/ai.py tests/test_routers_ai.py
git commit -m "fix(data): H8 — truncate AI contact fields to column max lengths"
```

### Task 3.2: H10 — Search Refresh Stale Data Warning

**Files:**
- Modify: `app/routers/sightings.py:515-522`
- Test: `tests/test_sightings_router.py`

Currently when `search_requirement()` fails, the endpoint silently returns old data.

- [ ] **Step 1: Write failing test**

```python
def test_sightings_refresh_returns_warning_on_failure(auth_client, db_session, sample_requirement):
    """H10: Failed refresh must include HX-Trigger warning toast."""
    # Mock search_requirement to raise
    with patch("app.routers.sightings.search_requirement", side_effect=Exception("timeout")):
        resp = auth_client.post(f"/v2/partials/sightings/{sample_requirement.id}/refresh")
    assert resp.status_code == 200
    trigger = resp.headers.get("HX-Trigger")
    assert trigger and "warning" in trigger.lower()
```

- [ ] **Step 2: Add HX-Trigger header on failure**

In `app/routers/sightings.py`, modify the refresh endpoint (around line 519):

```python
    refresh_failed = False
    try:
        from ..search_service import search_requirement
        await search_requirement(requirement, db)
    except Exception:
        logger.warning("Search refresh failed for requirement %s", requirement_id, exc_info=True)
        refresh_failed = True

    response = await sightings_detail(request, requirement_id, db, user)
    if refresh_failed:
        response.headers["HX-Trigger"] = '{"showToast": {"message": "Search refresh failed — showing cached results", "type": "warning"}}'
    return response
```

- [ ] **Step 3: Run tests — expect PASS**

- [ ] **Step 4: Commit**

```bash
git add app/routers/sightings.py tests/test_sightings_router.py
git commit -m "fix(ux): H10 — show warning toast when search refresh fails"
```

### Task 3.3: H11 — AI Qty Estimation Fallback Flag

**Files:**
- Modify: `app/services/sighting_aggregation.py:60-75`
- Test: `tests/test_sighting_aggregation.py`

When Claude fails to estimate unique qty, the fallback `sum()` over-counts because the same stock appears on multiple platforms.

- [ ] **Step 1: Write failing test**

```python
def test_estimate_unique_qty_flags_fallback(db_session):
    """H11: When AI fails, return max(quantities) instead of sum, and flag as approximate."""
    with patch("app.services.sighting_aggregation.client") as mock_client:
        mock_client.messages.create.side_effect = Exception("API down")
        result = estimate_unique_qty([100, 100, 50])
    # Should use max() as safer fallback, not sum()
    assert result["qty"] == 100
    assert result["approximate"] is True
```

- [ ] **Step 2: Change return type and fallback logic**

In `app/services/sighting_aggregation.py`, change the function to return a dict:

```python
def estimate_unique_qty(quantities: list[int]) -> dict:
    """Estimate unique available qty from potentially overlapping sightings.

    Returns: {"qty": int, "approximate": bool}
    """
    non_null = [q for q in quantities if q and q > 0]
    if not non_null:
        return {"qty": 0, "approximate": False}
    if len(non_null) == 1:
        return {"qty": non_null[0], "approximate": False}
    try:
        # ... existing Claude call ...
        return {"qty": int(text), "approximate": False}
    except Exception:
        logger.warning("AI qty estimation failed, using max fallback")
        return {"qty": max(non_null), "approximate": True}
```

- [ ] **Step 3: Update all callers to handle the new return type**

```bash
grep -rn "estimate_unique_qty" app/ --include="*.py"
```

Update each caller to unpack the dict: `result = estimate_unique_qty(qtys); qty = result["qty"]`

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add app/services/sighting_aggregation.py tests/test_sighting_aggregation.py
git commit -m "fix(data): H11 — use max() fallback for qty estimation, flag as approximate"
```

### Task 3.4: H12 — Credential Decryption Health Check

**Files:**
- Modify: `app/services/credential_service.py:83-93`
- Test: `tests/test_credential_service.py`

When decryption fails, `get_credential()` silently falls back to the env var which may be stale/wrong.

- [ ] **Step 1: Write failing test**

```python
def test_get_credential_logs_fallback_warning(db_session, caplog):
    """H12: Decryption failure must log a warning when falling back to env var."""
    # Set up an ApiSource with corrupted encrypted credentials
    # Verify that the fallback is logged and flagged
    ...
```

- [ ] **Step 2: Add health flag and explicit logging**

In `app/services/credential_service.py:get_credential()`:

```python
def get_credential(db: Session, source_name: str, env_var_name: str) -> str | None:
    """Get a credential value: DB first, then env var fallback."""
    src = db.query(ApiSource).filter_by(name=source_name).first()
    if src and src.credentials:
        encrypted = src.credentials.get(env_var_name)
        if encrypted:
            try:
                return decrypt_value(encrypted)
            except Exception:
                logger.error(
                    "Credential decrypt FAILED for %s/%s — falling back to env var. "
                    "DB credentials may be corrupted. Re-save credentials in admin panel.",
                    source_name, env_var_name, exc_info=True,
                )
    fallback = os.getenv(env_var_name) or None
    if fallback and src and src.credentials and src.credentials.get(env_var_name):
        logger.warning("Using env var fallback for %s/%s — DB credential decrypt failed", source_name, env_var_name)
    return fallback
```

- [ ] **Step 3: Run tests — expect PASS**

- [ ] **Step 4: Commit**

```bash
git add app/services/credential_service.py tests/test_credential_service.py
git commit -m "fix(security): H12 — log explicit warning on credential decryption fallback"
```

---

## Phase 4: Audit Medium Fixes (3 parallel tasks)

### Task 4.1: M9 — LIKE Pattern Injection Escape

**Files:**
- Modify: `app/routers/sightings.py:115,120`
- Modify: `app/routers/tags.py:32`
- Modify: `app/routers/crm/offers.py:286` (if applicable — check if this is user input)
- Test: `tests/test_like_escape.py`

`escape_like()` already exists in `app/utils/sql_helpers.py` and is used in 13+ files. These 3 were missed.

- [ ] **Step 1: Write failing test**

```python
def test_sightings_list_escapes_like_wildcards(auth_client, db_session):
    """M9: Wildcard chars in search query must be escaped."""
    # Search for literal "%" should not match everything
    resp = auth_client.get("/v2/partials/sightings?q=%25%25%25")
    assert resp.status_code == 200
    # Should return 0 results, not all records
```

- [ ] **Step 2: Add escape_like to sightings.py**

At top of `app/routers/sightings.py`, add import:
```python
from ..utils.sql_helpers import escape_like
```

At line 115 and 120, wrap user input:
```python
# Before:
if filters.sales_person:
    query = query.join(User, ...).filter(User.name.ilike(f"%{filters.sales_person}%"))
if filters.q:
    query = query.filter(
        Requirement.primary_mpn.ilike(f"%{filters.q}%") | Requisition.customer_name.ilike(f"%{filters.q}%")
    )

# After:
if filters.sales_person:
    safe = escape_like(filters.sales_person)
    query = query.join(User, ...).filter(User.name.ilike(f"%{safe}%"))
if filters.q:
    safe_q = escape_like(filters.q)
    query = query.filter(
        Requirement.primary_mpn.ilike(f"%{safe_q}%") | Requisition.customer_name.ilike(f"%{safe_q}%")
    )
```

- [ ] **Step 3: Add escape_like to tags.py**

At top of `app/routers/tags.py`:
```python
from ..utils.sql_helpers import escape_like
```

At line 32:
```python
# Before:
if q:
    query = query.filter(Tag.name.ilike(f"%{q}%"))
# After:
if q:
    query = query.filter(Tag.name.ilike(f"%{escape_like(q)}%"))
```

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py app/routers/tags.py tests/test_like_escape.py
git commit -m "fix(security): M9 — escape LIKE wildcards in sightings and tags search"
```

### Task 4.2: M10 — Enrichment Connector Error Classification

**Files:**
- Modify: `app/services/enrichment.py:112-114`
- Test: `tests/test_enrichment.py`

Currently all connector failures (timeout, auth, rate limit) are treated identically.

- [ ] **Step 1: Write failing test**

```python
def test_enrichment_classifies_timeout_errors(db_session):
    """M10: Timeout errors should be retried, auth errors should not."""
    ...
```

- [ ] **Step 2: Classify errors in the except block**

```python
# In app/services/enrichment.py, replace the generic except:
    except asyncio.TimeoutError:
        logger.warning("Connector %s timed out for %s — will retry", config["name"], mpn)
        return None  # Caller can retry
    except Exception as exc:
        err_str = str(exc).lower()
        if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
            logger.error("Connector %s auth failure for %s — skipping", config["name"], mpn)
        elif "429" in err_str or "rate" in err_str:
            logger.warning("Connector %s rate limited for %s — backoff needed", config["name"], mpn)
        else:
            logger.warning("Connector %s failed for %s", config["name"], mpn, exc_info=True)
        return None
```

- [ ] **Step 3: Run tests — expect PASS**

- [ ] **Step 4: Commit**

```bash
git add app/services/enrichment.py tests/test_enrichment.py
git commit -m "fix(reliability): M10 — classify enrichment connector errors for better retry logic"
```

### Task 4.3: M4 + M5 — Model Validators and CheckConstraints

**Files:**
- Modify: `app/models/requisition.py` (or wherever Requisition, Offer, Sighting, User are)
- Create: Alembic migration for CheckConstraints
- Test: `tests/test_model_validators.py`

- [ ] **Step 1: Add @validates decorators**

```python
# On User model:
@validates("role")
def validate_role(self, key, value):
    UserRole(value)  # Raises ValueError if invalid
    return value

# On Requisition model:
@validates("status")
def validate_status(self, key, value):
    RequisitionStatus(value)
    return value

# On Offer model:
@validates("status")
def validate_status(self, key, value):
    OfferStatus(value)
    return value

# On Sighting model — already has @validates("moq"), add:
@validates("confidence")
def validate_confidence(self, key, value):
    if value is not None and not (0.0 <= float(value) <= 1.0):
        raise ValueError(f"Confidence must be 0.0-1.0, got {value}")
    return value
```

- [ ] **Step 2: Write tests**

```python
import pytest
from app.models import User, Requisition, Offer, Sighting

def test_user_rejects_invalid_role():
    with pytest.raises(ValueError):
        u = User(email="test@test.com", role="SUPERADMIN")

def test_requisition_rejects_invalid_status():
    with pytest.raises(ValueError):
        r = Requisition(status="YOLO")
```

- [ ] **Step 3: Create Alembic migration for CheckConstraints**

```bash
cd /root/availai
alembic revision --autogenerate -m "add check constraints on status columns"
```

Review and edit migration to add:
```python
op.create_check_constraint("ck_user_role", "users", "role IN ('admin','buyer','sales','trader','manager')")
op.create_check_constraint("ck_requisition_status", "requisitions", "status IN ('draft','active','completed','archived','cancelled')")
```

- [ ] **Step 4: Test migration up/down**

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

- [ ] **Step 5: Run model tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_model_validators.py -v
```

- [ ] **Step 6: Commit**

```bash
git add app/models/ alembic/versions/ tests/test_model_validators.py
git commit -m "fix(types): M4+M5 — add @validates decorators and DB CheckConstraints on status columns"
```

---

## Phase 5: Test Coverage (10 parallel tasks — all independent, no production risk)

### Task 5.1: T1 — Auth Dependencies Unit Tests

**Files:**
- Create: `tests/test_auth_deps_unit.py`
- Reference: `app/dependencies.py`

Test `require_user`, `require_admin`, `require_buyer`, `require_fresh_token` directly — not through dependency overrides. Cover: valid user, no session, deactivated user, agent key auth, admin blocks agent key, expired token.

- [ ] **Step 1: Write comprehensive auth dep tests**

```python
"""Tests for app/dependencies.py auth functions directly."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from app.dependencies import require_user, require_admin, require_buyer, get_user
from app.constants import UserRole

class TestRequireUser:
    def test_returns_user_when_session_valid(self, db_session, sample_user):
        request = MagicMock()
        request.session = {"user_id": sample_user.id}
        result = require_user(request, db_session)
        assert result.id == sample_user.id

    def test_raises_401_when_no_session(self, db_session):
        request = MagicMock()
        request.session = {}
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)
        assert exc_info.value.status_code == 401

    def test_raises_403_when_deactivated(self, db_session, deactivated_user):
        request = MagicMock()
        request.session = {"user_id": deactivated_user.id}
        with pytest.raises(HTTPException) as exc_info:
            require_user(request, db_session)
        assert exc_info.value.status_code == 403

    def test_agent_key_authenticates(self, db_session, agent_user):
        request = MagicMock()
        request.session = {}
        request.headers = {"x-agent-key": "test-agent-key-secret"}
        with patch("app.config.settings") as mock_settings:
            mock_settings.agent_api_key = "test-agent-key-secret"
            result = require_user(request, db_session)
        assert result.email == "agent@availai.local"

class TestRequireAdmin:
    def test_blocks_agent_service_account(self, db_session, agent_user):
        # Agent key should get 403 on admin endpoints
        ...

    def test_allows_admin_user(self, db_session, admin_user):
        ...

    def test_blocks_non_admin(self, db_session, buyer_user):
        ...
```

- [ ] **Step 2: Run tests**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_auth_deps_unit.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth_deps_unit.py
git commit -m "test: T1 — direct unit tests for auth dependency functions"
```

### Task 5.2: T2 — Teams Action Tokens Tests

Note: `app/routers/teams_action_tokens.py` does not exist. Search for the actual file that handles team action tokens — likely in `app/services/` or `app/routers/teams.py`. If no such module exists, skip this task and document why.

- [ ] **Step 1: Find the actual module**

```bash
grep -rn "action_token\|ActionToken\|teams.*token" app/ --include="*.py" | head -20
```

- [ ] **Step 2: Write tests for whatever token validation exists**
- [ ] **Step 3: Run and commit**

### Task 5.3: T3 — Requisition Clone Tests

**Files:**
- Create: `tests/test_routers_crm_clone.py`
- Reference: `app/routers/crm/clone.py`

- [ ] **Step 1: Read clone.py to understand endpoints**
- [ ] **Step 2: Write tests covering: successful clone, clone with missing req, clone preserving data, clone not copying IDs**
- [ ] **Step 3: Run and commit**

### Task 5.4: T4 — Vendor Inquiry Tests

**Files:**
- Create: `tests/test_vendor_inquiry.py`
- Reference: `app/routers/vendor_inquiry.py`

- [ ] **Step 1: Read vendor_inquiry.py to understand endpoints**
- [ ] **Step 2: Write tests covering: send inquiry, missing vendor, email validation, rate limiting**
- [ ] **Step 3: Run and commit**

### Task 5.5: T5 — Credential Service Round-Trip Tests

**Files:**
- Create: `tests/test_credential_service.py`
- Reference: `app/services/credential_service.py`

- [ ] **Step 1: Write encrypt/decrypt round-trip test**

```python
from app.services.credential_service import encrypt_value, decrypt_value

def test_encrypt_decrypt_roundtrip():
    plaintext = "sk-ant-api03-secret-key-here"
    encrypted = encrypt_value(plaintext)
    assert encrypted != plaintext
    assert decrypt_value(encrypted) == plaintext

def test_decrypt_corrupted_raises():
    with pytest.raises(Exception):
        decrypt_value("not-valid-fernet-token")

def test_mask_credential():
    from app.services.credential_service import mask_credential
    assert mask_credential("sk-ant-api03-abcd") == "●●●●●●●●abcd"
    assert mask_credential("") == ""
    assert mask_credential("abc") == "****"
```

- [ ] **Step 2: Run and commit**

### Task 5.6: T6 — Command Center Tests

**Files:**
- Create: `tests/test_command_center.py`
- Reference: `app/routers/command_center.py`

- [ ] **Step 1: Read command_center.py**
- [ ] **Step 2: Write tests for all dashboard endpoints**
- [ ] **Step 3: Run and commit**

### Task 5.7: T7 — SSE Events Auth Tests

**Files:**
- Create: `tests/test_events_sse.py`
- Reference: `app/routers/events.py`

- [ ] **Step 1: Read events.py to understand SSE endpoints**
- [ ] **Step 2: Write tests: unauthenticated request returns 401, authenticated request connects, stream format correct**
- [ ] **Step 3: Run and commit**

### Task 5.8: T8 — Job Happy-Path Tests

**Files:**
- Modify: existing job test files
- Reference: `app/jobs/maintenance_jobs.py`, `app/jobs/health_jobs.py`

- [ ] **Step 1: Add happy-path tests for knowledge, sourcing_refresh, part_discovery jobs**
- [ ] **Step 2: Run and commit**

### Task 5.9: T9 — Email Mining Pattern Tests

**Files:**
- Create: `tests/test_email_mining_patterns.py`
- Reference: `app/services/email_mining.py`

- [ ] **Step 1: Read email_mining.py to find OFFER_PATTERNS, MPN_PATTERN, PHONE_PATTERN**
- [ ] **Step 2: Write tests with real-world sample strings**

```python
import re
from app.services.email_mining import OFFER_PATTERNS, MPN_PATTERN, PHONE_PATTERN

class TestMPNPattern:
    def test_standard_mpn(self):
        assert re.search(MPN_PATTERN, "LM358N")
    def test_mpn_with_dash(self):
        assert re.search(MPN_PATTERN, "STM32F103C8T6")
    def test_no_false_positive_on_common_words(self):
        assert not re.search(MPN_PATTERN, "Hello World")

class TestPhonePattern:
    def test_us_phone(self):
        assert re.search(PHONE_PATTERN, "(555) 123-4567")
    def test_international(self):
        assert re.search(PHONE_PATTERN, "+1-555-123-4567")
```

- [ ] **Step 3: Run and commit**

### Task 5.10: T10 — Fix Weak Agent Auth Assertion

**Files:**
- Modify: `tests/test_agent_auth.py:56`

- [ ] **Step 1: Fix the assertion**

```python
# Before (line 56):
assert resp.status_code != 401

# After:
assert resp.status_code in (200, 204), f"Agent key auth failed with {resp.status_code}"
```

- [ ] **Step 2: Run and commit**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/test_agent_auth.py -v
git add tests/test_agent_auth.py
git commit -m "test: T10 — fix weak agent auth assertion to require 200/204"
```

---

## Phase 6: Final Sweep

### Task 6.1: Full Test Suite Verification

- [ ] **Step 1: Run complete test suite**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short 2>&1 | tail -50
```

- [ ] **Step 2: Fix any failures**

- [ ] **Step 3: Run linting**

```bash
cd /root/availai && ruff check app/ tests/
```

### Task 6.2: Deploy

- [ ] **Step 1: Final commit if needed, then deploy**

```bash
cd /root/availai && ./deploy.sh
```

### Task 6.3: Update Memory

- [ ] **Step 1: Update audit memory to reflect all items fixed**
- [ ] **Step 2: Update sightings WIP memory to reflect committed**
- [ ] **Step 3: Update active investigations memory**
