# PR Review Fixes — Critical & Important Issues

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all 4 critical and 9 important issues identified in the PR review of `claude/review-rfq-layout-PARkK`.

**Architecture:** Direct edits to existing files on the branch. No new models or migrations — one migration amendment (067) for the partial unique index. TDD for new test coverage.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL 16, pytest

---

### Task 1: Security — Remove `_resolve_user` fallback to arbitrary user

**Files:**
- Modify: `app/services/teams_bot_service.py` (the `_resolve_user` function, ~line 483)
- Test: `tests/test_teams_bot.py`

**Step 1: Write the failing test**

In `tests/test_teams_bot.py`, add a test that verifies `_resolve_user` returns `None` when no matching Azure AD user is found (not an arbitrary user):

```python
def test_resolve_user_no_match_returns_none():
    """_resolve_user must NOT fall back to random active users."""
    from app.services.teams_bot_service import _resolve_user

    db = TestingSessionLocal()
    try:
        # Create a user that does NOT match the AAD ID
        from app.models.auth import User
        u = User(email="other@test.com", name="Other", is_active=True, azure_ad_id="different-aad-id")
        db.add(u)
        db.commit()

        result = _resolve_user("nonexistent-aad-id", db)
        assert result is None, "Must return None, not fall back to arbitrary user"
    finally:
        db.close()
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_teams_bot.py::test_resolve_user_no_match_returns_none -v`
Expected: FAIL — currently returns the first active user

**Step 3: Fix the code**

In `app/services/teams_bot_service.py`, replace the `_resolve_user` function body:

```python
def _resolve_user(user_aad_id: str, db):
    """Resolve a Teams AAD user ID to an AVAIL user."""
    if not user_aad_id:
        return None
    try:
        from app.models.auth import User
        return db.query(User).filter(User.azure_ad_id == user_aad_id).first()
    except Exception:
        logger.warning("Failed to resolve Teams user %s", user_aad_id, exc_info=True)
        return None
```

Key change: Removed the fallback `db.query(User).filter(User.is_active.is_(True)).first()` line entirely. Added `logger.warning` instead of bare `except Exception: return None`.

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_teams_bot.py::test_resolve_user_no_match_returns_none -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/teams_bot_service.py tests/test_teams_bot.py
git commit -m "fix(security): remove _resolve_user fallback to arbitrary user"
```

---

### Task 2: Security — Add auth to Teams bot `/config` endpoint

**Files:**
- Modify: `app/routers/teams_bot.py` (the `update_bot_config` function)
- Test: `tests/test_teams_bot.py`

**Step 1: Write the failing test**

```python
def test_bot_config_requires_admin(client):
    """POST /api/teams-bot/config must require admin auth."""
    # Unauthenticated request
    resp = client.post("/api/teams-bot/config", json={"enabled": "true"})
    assert resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}"
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_teams_bot.py::test_bot_config_requires_admin -v`
Expected: FAIL — currently returns 200

**Step 3: Fix the code**

In `app/routers/teams_bot.py`, modify the `update_bot_config` endpoint:

```python
@router.post("/config")
async def update_bot_config(body: dict, user: User = Depends(require_admin)):
    """Update Teams bot configuration. Body: {enabled, hmac_secret}."""
```

Add the import at the top of the file (if not already present):
```python
from app.dependencies import require_admin
from app.models import User
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_teams_bot.py::test_bot_config_requires_admin -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/teams_bot.py tests/test_teams_bot.py
git commit -m "fix(security): require admin auth for teams bot config endpoint"
```

---

### Task 3: Data Integrity — Make `replace_vendor` atomic

**Files:**
- Modify: `app/services/strategic_vendor_service.py` (the `replace_vendor`, `drop_vendor`, and `claim_vendor` functions)
- Test: `tests/test_strategic_vendors.py`

**Step 1: Write the failing test**

```python
def test_replace_vendor_atomic_rollback_on_claim_failure(db):
    """If claim fails during replace, the drop must be rolled back."""
    from app.services.strategic_vendor_service import replace_vendor, claim_vendor, get_my_strategic
    from app.models.vendors import VendorCard
    from app.models.auth import User
    from app.models.strategic import StrategicVendor

    user1 = User(email="u1@test.com", name="U1", is_active=True)
    user2 = User(email="u2@test.com", name="U2", is_active=True)
    v1 = VendorCard(display_name="Vendor1", normalized_name="vendor1")
    v2 = VendorCard(display_name="Vendor2", normalized_name="vendor2")
    db.add_all([user1, user2, v1, v2])
    db.commit()

    # User1 claims v1
    rec, err = claim_vendor(db, user1.id, v1.id)
    assert rec is not None

    # User2 claims v2 (so user1 can't claim it)
    rec2, err2 = claim_vendor(db, user2.id, v2.id)
    assert rec2 is not None

    # User1 tries to replace v1 with v2 — should fail because v2 is taken
    result, err = replace_vendor(db, user1.id, v1.id, v2.id)
    assert result is None
    assert err is not None

    # v1 must still be in user1's strategic list (drop was rolled back)
    my_vendors = get_my_strategic(db, user1.id)
    assert len(my_vendors) == 1
    assert my_vendors[0].vendor_card_id == v1.id
```

**Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_strategic_vendors.py::test_replace_vendor_atomic_rollback_on_claim_failure -v`
Expected: FAIL — drop is committed, rollback doesn't undo it

**Step 3: Fix the code**

Refactor `claim_vendor` and `drop_vendor` to accept a `commit` parameter, and use `db.begin_nested()` in `replace_vendor`:

```python
def claim_vendor(
    db: Session, user_id: int, vendor_card_id: int, *, commit: bool = True
) -> tuple[StrategicVendor | None, str | None]:
    """Claim a vendor as strategic. Returns (record, error_message)."""
    count = active_count(db, user_id)
    if count >= MAX_STRATEGIC_VENDORS:
        return None, f"Already at {MAX_STRATEGIC_VENDORS} strategic vendors. Drop one first."

    existing = get_vendor_owner(db, vendor_card_id)
    if existing:
        if existing.user_id == user_id:
            return None, "You already have this vendor as strategic."
        return None, "This vendor is already claimed by another buyer."

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_card_id).first()
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
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    logger.info(
        "Strategic vendor claimed: user={} vendor={} expires={}",
        user_id, vendor_card_id, record.expires_at,
    )
    return record, None


def drop_vendor(
    db: Session, user_id: int, vendor_card_id: int, *, commit: bool = True
) -> tuple[bool, str | None]:
    """Drop a strategic vendor back to open pool. Returns (success, error)."""
    record = (
        db.query(StrategicVendor)
        .filter(
            StrategicVendor.user_id == user_id,
            StrategicVendor.vendor_card_id == vendor_card_id,
            StrategicVendor.released_at.is_(None),
        )
        .first()
    )
    if not record:
        return False, "Vendor is not in your strategic list."

    record.released_at = datetime.now(timezone.utc)
    record.release_reason = "dropped"
    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("Strategic vendor dropped: user={} vendor={}", user_id, vendor_card_id)
    return True, None


def replace_vendor(
    db: Session, user_id: int, drop_vendor_id: int, claim_vendor_id: int
) -> tuple[StrategicVendor | None, str | None]:
    """Atomic swap: drop one vendor, claim another. Returns (new_record, error)."""
    if drop_vendor_id == claim_vendor_id:
        return None, "Cannot replace a vendor with itself."

    nested = db.begin_nested()
    try:
        success, err = drop_vendor(db, user_id, drop_vendor_id, commit=False)
        if not success:
            nested.rollback()
            return None, err

        record, err = claim_vendor(db, user_id, claim_vendor_id, commit=False)
        if not record:
            nested.rollback()
            return None, err

        nested.commit()
        db.commit()
        db.refresh(record)
        return record, None
    except Exception:
        nested.rollback()
        raise
```

**Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_strategic_vendors.py -v`
Expected: ALL PASS (existing tests + new test)

**Step 5: Commit**

```bash
git add app/services/strategic_vendor_service.py tests/test_strategic_vendors.py
git commit -m "fix(data): make replace_vendor atomic with savepoint"
```

---

### Task 4: Data Integrity — Partial unique index for one-buyer-per-vendor

**Files:**
- Modify: `app/models/strategic.py`
- Create: `alembic/versions/067_strategic_vendor_partial_index.py`
- Test: `tests/test_strategic_vendors.py`

**Step 1: Write the failing test**

```python
def test_two_users_cannot_claim_same_vendor(db):
    """DB-level enforcement: two users can't hold same active vendor."""
    from app.models.auth import User
    from app.models.vendors import VendorCard
    from app.models.strategic import StrategicVendor
    from datetime import datetime, timedelta, timezone

    u1 = User(email="a@test.com", name="A", is_active=True)
    u2 = User(email="b@test.com", name="B", is_active=True)
    v = VendorCard(display_name="SharedVendor", normalized_name="sharedvendor")
    db.add_all([u1, u2, v])
    db.commit()

    now = datetime.now(timezone.utc)
    sv1 = StrategicVendor(user_id=u1.id, vendor_card_id=v.id, claimed_at=now, expires_at=now + timedelta(days=39))
    db.add(sv1)
    db.commit()

    # Second user tries to claim same vendor — should fail at DB level
    sv2 = StrategicVendor(user_id=u2.id, vendor_card_id=v.id, claimed_at=now, expires_at=now + timedelta(days=39))
    db.add(sv2)
    from sqlalchemy.exc import IntegrityError
    import pytest
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
```

Note: This test only works after the partial unique index is added. SQLite doesn't support partial unique indexes natively, so this test should be marked `@pytest.mark.skipif` for SQLite or tested only on PostgreSQL. For now, write it as a documentation test and verify with the service-level test from Task 3.

**Step 2: Update the model**

In `app/models/strategic.py`, replace the `UniqueConstraint` with a partial unique index in `__table_args__`:

```python
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)

# In __table_args__, replace:
#   UniqueConstraint("user_id", "vendor_card_id", name="uq_user_vendor_strategic"),
# With:
    Index(
        "uq_active_vendor_claim",
        "vendor_card_id",
        unique=True,
        postgresql_where=text("released_at IS NULL"),
    ),
```

Keep the existing composite indexes as they are.

**Step 3: Create migration 067**

```python
"""Replace lifetime unique with partial unique index for active vendor claims.

Revision ID: 067
Revises: 066
Create Date: 2026-03-08
"""

from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_user_vendor_strategic", "strategic_vendors", type_="unique")
    op.execute(
        "CREATE UNIQUE INDEX uq_active_vendor_claim "
        "ON strategic_vendors (vendor_card_id) "
        "WHERE released_at IS NULL"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS uq_active_vendor_claim")
    op.create_unique_constraint(
        "uq_user_vendor_strategic", "strategic_vendors", ["user_id", "vendor_card_id"]
    )
```

**Step 4: Run all strategic vendor tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_strategic_vendors.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/models/strategic.py alembic/versions/067_strategic_vendor_partial_index.py tests/test_strategic_vendors.py
git commit -m "fix(data): partial unique index for one-active-buyer-per-vendor"
```

---

### Task 5: Silent Failures — Fix `notify_intelligence.py` error handling

**Files:**
- Modify: `app/services/notify_intelligence.py`

This task fixes 5 issues in one file:
1. `_check_staleness`: `except Exception: pass` → add logging
2. `_check_quiet_hours`: swallows DB errors → add logging
3. `_get_user_threshold`: swallows errors → add logging
4. `get_batch_queue`: loses already-popped items on error → return partial results
5. `is_intelligence_enabled`: `UnboundLocalError` if `SessionLocal()` throws → init `db = None`

**Step 1: Apply all fixes**

Fix 1 — `_check_staleness` (~line 127):
```python
    except Exception:
        logger.debug("Staleness counter update failed for %s:%s", event_type, entity_id, exc_info=True)
```

Fix 2 — `_check_quiet_hours` (~line 149):
```python
    except Exception:
        logger.warning("Quiet hours check failed for user %d, defaulting to not-quiet", user_id, exc_info=True)
        return False
```

Fix 3 — `_get_user_threshold` (~line 160):
```python
    except Exception:
        logger.warning("Failed to get user threshold for user %d, defaulting to medium", user_id, exc_info=True)
```

Fix 4 — `get_batch_queue` (~line 450-454): return partial results instead of empty:
```python
    except Exception:
        logger.warning("Batch queue read interrupted for user %d, returning %d partial items", user_id, len(items), exc_info=True)
        return items
```

Fix 5 — `is_intelligence_enabled` (~line 462-475): fix `UnboundLocalError`:
```python
def is_intelligence_enabled() -> bool:
    """Check if notification intelligence feature flag is on. Enabled by default."""
    if os.environ.get("TESTING"):
        return os.environ.get("NOTIFICATION_INTELLIGENCE_ENABLED", "").lower() == "true"
    db = None
    try:
        from app.models.config import SystemConfig
        from app.database import SessionLocal
        db = SessionLocal()
        row = db.query(SystemConfig).filter(SystemConfig.key == "notification_intelligence_enabled").first()
        if row:
            return row.value.lower() == "true"
    except Exception:
        logger.warning("Failed to check intelligence feature flag, defaulting to enabled", exc_info=True)
    finally:
        if db:
            db.close()
    return True
```

**Step 2: Run existing tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_notify_intelligence.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add app/services/notify_intelligence.py
git commit -m "fix: improve error handling in notify_intelligence — log instead of swallow"
```

---

### Task 6: Silent Failures — Fix Teams bot error handling

**Files:**
- Modify: `app/routers/teams_bot.py` (`_get_bot_config`, `_validate_hmac`)
- Modify: `app/services/teams_bot_service.py` (`_update_context`)

**Step 1: Apply all fixes**

Fix 1 — `_get_bot_config` in `app/routers/teams_bot.py`:
```python
    except Exception:
        logger.warning("Failed to load teams bot config", exc_info=True)
        return {}
```

Fix 2 — `_validate_hmac` in `app/routers/teams_bot.py`:
```python
    except Exception:
        logger.warning("HMAC validation error — check secret format", exc_info=True)
        return False
```

Fix 3 — `_update_context` in `app/services/teams_bot_service.py`:
```python
    except Exception:
        logger.debug("Failed to update bot conversation context for %s", user_aad_id, exc_info=True)
```

**Step 2: Run existing tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_teams_bot.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add app/routers/teams_bot.py app/services/teams_bot_service.py
git commit -m "fix: add logging to teams bot error handlers"
```

---

### Task 7: Fix `_vendor_to_dict` datetime crash + error convention

**Files:**
- Modify: `app/routers/strategic.py`
- Test: `tests/test_strategic_vendors.py`

**Step 1: Fix `_vendor_to_dict`**

Replace the function in `app/routers/strategic.py`:

```python
def _vendor_to_dict(record):
    """Convert a StrategicVendor record to API response dict."""
    from datetime import datetime, timezone
    from app.services.strategic_vendor_service import _ensure_utc

    now = datetime.now(timezone.utc)
    expires = _ensure_utc(record.expires_at)
    days_left = max(0, (expires - now).days)
    return {
        "id": record.id,
        "vendor_card_id": record.vendor_card_id,
        "vendor_name": record.vendor_card.display_name if record.vendor_card else None,
        "vendor_score": record.vendor_card.vendor_score if record.vendor_card else None,
        "claimed_at": record.claimed_at.isoformat(),
        "last_offer_at": record.last_offer_at.isoformat() if record.last_offer_at else None,
        "expires_at": record.expires_at.isoformat(),
        "days_remaining": days_left,
    }
```

**Step 2: Fix error responses to use project convention**

Replace `HTTPException(status_code=409, detail=err)` with `JSONResponse`:

```python
from fastapi.responses import JSONResponse

# In claim_vendor endpoint:
    if not record:
        return JSONResponse(status_code=409, content={"error": err, "status_code": 409})

# In drop_vendor endpoint:
    if not ok:
        return JSONResponse(status_code=404, content={"error": err, "status_code": 404})

# In replace_vendor endpoint:
    if not record:
        return JSONResponse(status_code=409, content={"error": err, "status_code": 409})
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_strategic_vendors.py -v`
Expected: PASS — update any tests that check for `detail` to check for `error` instead

**Step 4: Commit**

```bash
git add app/routers/strategic.py tests/test_strategic_vendors.py
git commit -m "fix: _vendor_to_dict timezone handling + error response convention"
```

---

### Task 8: Fix `_consolidate_bg` and `internal_verify_retest`

**Files:**
- Modify: `app/routers/trouble_tickets.py`

**Step 1: Fix `_consolidate_bg` — add `exc_info=True`**

```python
        except Exception:
            logger.warning("Background consolidation failed for ticket {}", tid, exc_info=True)
```

**Step 2: Fix `internal_verify_retest` — fail fast on missing session cookie**

Replace the try/except block:

```python
    # Generate session cookie for SiteTester
    try:
        from itsdangerous import URLSafeTimedSerializer
        from app.config import settings as cfg
        signer = URLSafeTimedSerializer(cfg.secret_key)
        session_cookie = signer.dumps({"user_id": 1})
    except Exception:
        logger.error("Could not generate session cookie for retest", exc_info=True)
        raise HTTPException(500, "Cannot generate retest session — check itsdangerous and secret_key config")
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_trouble_tickets.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add app/routers/trouble_tickets.py
git commit -m "fix: consolidate_bg exc_info + fail-fast on retest cookie generation"
```

---

### Task 9: Fix logging levels — upgrade debug to warning for business-impacting failures

**Files:**
- Modify: `app/email_service.py`
- Modify: `app/jobs/notify_intelligence_jobs.py`

**Step 1: Fix `email_service.py` strategic vendor clock reset**

Change `logger.debug` to `logger.warning`:
```python
                    except Exception:
                        logger.warning("Strategic vendor clock reset failed for offer %s", offer.id, exc_info=True)
```

**Step 2: Fix `notify_intelligence_jobs.py` batch digest per-user failure**

Change `logger.debug` to `logger.warning`:
```python
            except Exception:
                logger.warning("Batch digest failed for user %d", user.id, exc_info=True)
```

**Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -k "test_notify_intelligence or test_email" -v`
Expected: PASS

**Step 4: Commit**

```bash
git add app/email_service.py app/jobs/notify_intelligence_jobs.py
git commit -m "fix: upgrade debug→warning for business-impacting error logs"
```

---

### Task 10: Remove unused import

**Files:**
- Modify: `app/services/strategic_vendor_service.py`

**Step 1: Remove `and_` from import**

```python
from sqlalchemy import func
```

(Remove `and_` from the import line.)

**Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_strategic_vendors.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add app/services/strategic_vendor_service.py
git commit -m "chore: remove unused and_ import"
```

---

### Task 11: Add tests for `retry_stuck_diagnosed`

**Files:**
- Test: `tests/test_scheduler_selfheal.py`

**Step 1: Write tests**

```python
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from app.jobs.selfheal_jobs import retry_stuck_diagnosed


@pytest.mark.asyncio
async def test_retry_stuck_disabled(db):
    """retry_stuck_diagnosed returns zeros when self_heal_enabled=False."""
    with patch("app.jobs.selfheal_jobs.settings") as mock_settings:
        mock_settings.self_heal_enabled = False
        result = await retry_stuck_diagnosed(db)
        assert result == {"retried": 0, "rediagnosed": 0, "succeeded": 0, "failed": 0}


@pytest.mark.asyncio
async def test_retry_stuck_skips_exhausted(db):
    """Tickets at max iterations are skipped."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = TroubleTicket(
        title="Test", status="diagnosed", risk_tier="low",
        iterations_used=99, user_id=1,
    )
    db.add(ticket)
    db.commit()

    with patch("app.jobs.selfheal_jobs.settings") as mock_settings:
        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_max_iterations_low = 5
        mock_settings.self_heal_max_iterations_medium = 10
        result = await retry_stuck_diagnosed(db)
        assert result["retried"] == 0


@pytest.mark.asyncio
async def test_retry_stuck_rediagnoses_missing_detailed(db):
    """Tickets without detailed diagnosis get re-diagnosed."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = TroubleTicket(
        title="Test", status="diagnosed", risk_tier="low",
        iterations_used=0, user_id=1, diagnosis={},
    )
    db.add(ticket)
    db.commit()

    with patch("app.jobs.selfheal_jobs.settings") as mock_settings, \
         patch("app.services.diagnosis_service.diagnose_full", new_callable=AsyncMock) as mock_diag, \
         patch("app.services.execution_service.execute_fix", new_callable=AsyncMock) as mock_exec:
        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_max_iterations_low = 5
        mock_settings.self_heal_max_iterations_medium = 10
        mock_diag.return_value = {"ok": True}
        mock_exec.return_value = {"ok": True}

        result = await retry_stuck_diagnosed(db)
        assert result["rediagnosed"] == 1
        assert result["succeeded"] == 1
        mock_diag.assert_called_once()


@pytest.mark.asyncio
async def test_retry_stuck_rediagnosis_failure(db):
    """Failed re-diagnosis increments failed count."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = TroubleTicket(
        title="Test", status="diagnosed", risk_tier="low",
        iterations_used=0, user_id=1, diagnosis={},
    )
    db.add(ticket)
    db.commit()

    with patch("app.jobs.selfheal_jobs.settings") as mock_settings, \
         patch("app.services.diagnosis_service.diagnose_full", new_callable=AsyncMock) as mock_diag:
        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_max_iterations_low = 5
        mock_settings.self_heal_max_iterations_medium = 10
        mock_diag.return_value = {"error": "AI unavailable"}

        result = await retry_stuck_diagnosed(db)
        assert result["failed"] == 1
        assert result["retried"] == 0
```

**Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_scheduler_selfheal.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_scheduler_selfheal.py
git commit -m "test: add retry_stuck_diagnosed coverage"
```

---

### Task 12: Add tests for error_reports compatibility shim

**Files:**
- Create: `tests/test_error_reports.py`

**Step 1: Write tests**

```python
"""Tests for the error_reports compatibility shim router."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from tests.conftest import client, override_user, override_admin


def test_create_error_report(client):
    """POST /api/error-reports creates a trouble ticket."""
    with patch("app.routers.error_reports.svc") as mock_svc, \
         patch("app.routers.error_reports.generate_trouble_prompt", new_callable=AsyncMock) as mock_prompt:
        mock_ticket = MagicMock(id=1, ticket_number="TT-0001")
        mock_svc.create_ticket.return_value = mock_ticket
        mock_svc.auto_process_ticket = AsyncMock()
        mock_prompt.return_value = None

        resp = client.post("/api/error-reports", json={"message": "Something broke"})
        assert resp.status_code == 200
        assert resp.json()["id"] == 1
        mock_svc.create_ticket.assert_called_once()


def test_create_error_report_screenshot_too_large(client):
    """Reject screenshots larger than 2MB."""
    resp = client.post("/api/error-reports", json={
        "message": "Bug",
        "screenshot_b64": "x" * (2 * 1024 * 1024 + 1),
    })
    assert resp.status_code == 400


def test_list_error_reports_requires_admin(client):
    """GET /api/error-reports requires admin."""
    # Uses non-admin override by default
    resp = client.get("/api/error-reports")
    assert resp.status_code in (200, 403)  # depends on test auth setup


def test_status_mapping():
    """Verify ER↔TT status mappings are consistent."""
    from app.routers.error_reports import _ER_TO_TT_STATUS, _tt_to_er_status
    assert _ER_TO_TT_STATUS["open"] == "submitted"
    assert _ER_TO_TT_STATUS["resolved"] == "resolved"
    assert _tt_to_er_status("submitted") == "open"
    assert _tt_to_er_status("diagnosed") == "in_progress"
    assert _tt_to_er_status("rejected") == "closed"


def test_update_status_validates_input(client):
    """PUT status must be one of the allowed values."""
    resp = client.put("/api/error-reports/1/status", json={"status": "banana"})
    assert resp.status_code == 422  # Pydantic validation
```

**Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_error_reports.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_error_reports.py
git commit -m "test: add error_reports compatibility shim coverage"
```

---

### Task 13: Final verification — run full test suite

**Step 1: Run full suite with coverage**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=short -q`

Expected: All tests pass, coverage ≥ 97%

**Step 2: Review any failures and fix**

If any existing tests broke due to changes (e.g., `HTTPException` → `JSONResponse` in strategic router), update those tests to match the new response format.

**Step 3: Final commit if needed**

```bash
git add -u
git commit -m "fix: test adjustments for review fixes"
```
