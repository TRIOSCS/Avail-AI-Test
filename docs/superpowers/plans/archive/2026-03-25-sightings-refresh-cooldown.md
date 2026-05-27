# Sightings Manual Refresh with Cooldown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manual sightings refresh buttons (per-requirement + workspace-wide) with a 3-day per-requirement cooldown that managers/admins can bypass.

**Architecture:** New `last_refreshed_at` column on `Requirement` model tracks when each requirement was last searched. A `sighting_helpers.py` service enforces cooldown logic. Both the single-refresh and batch-refresh endpoints gain cooldown gating, and the detail template renders a disabled state with countdown text when on cooldown.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, HTMX, Jinja2, pytest

**Spec:** `docs/superpowers/specs/2026-03-25-sightings-refresh-cooldown-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `app/services/sighting_helpers.py` | Cooldown gate logic + countdown formatting |
| Create | `alembic/versions/<rev>_add_last_refreshed_at.py` | Migration: add column + backfill |
| Modify | `app/models/sourcing.py:102` | Add `last_refreshed_at` column to Requirement |
| Modify | `app/config.py:170` | Add `sighting_refresh_cooldown_days` config |
| Modify | `app/routers/sightings.py:256-335` | Cooldown checks in detail context, single refresh, batch refresh |
| Modify | `app/templates/htmx/partials/sightings/detail.html:19-28` | Conditional refresh button with disabled state |
| Modify | `app/templates/htmx/partials/sightings/table.html:10-28` | Add "Refresh All" button in toolbar |
| Modify | `app/jobs/sourcing_refresh_jobs.py:64-76` | Set `last_refreshed_at` after auto-refresh |
| Create | `tests/test_sighting_helpers.py` | Unit tests for cooldown helper |
| Modify | `tests/test_routers.py` | Integration tests for refresh endpoints |

---

## Task 1: Add Config Value

**Files:**
- Modify: `app/config.py:170`

- [ ] **Step 1: Add `sighting_refresh_cooldown_days` config**

In `app/config.py`, after line 170 (`sighting_stale_days: int = 3`), add:

```python
    sighting_refresh_cooldown_days: int = 3  # Days before manual refresh is allowed again
```

- [ ] **Step 2: Verify no breakage**

Run: `TESTING=1 PYTHONPATH=/root/availai python -c "from app.config import settings; print(settings.sighting_refresh_cooldown_days)"`
Expected: `3`

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat: add sighting_refresh_cooldown_days config value"
```

---

## Task 2: Add `last_refreshed_at` Column to Requirement Model

**Files:**
- Modify: `app/models/sourcing.py:102`

- [ ] **Step 1: Add column after `created_at`**

In `app/models/sourcing.py`, after line 102 (`created_at = Column(DateTime, ...)`), add:

```python
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 2: Verify model loads**

Run: `TESTING=1 PYTHONPATH=/root/availai python -c "from app.models.sourcing import Requirement; print(Requirement.last_refreshed_at)"`
Expected: `Requirement.last_refreshed_at` column object printed

- [ ] **Step 3: Commit**

```bash
git add app/models/sourcing.py
git commit -m "feat: add last_refreshed_at column to Requirement model"
```

---

## Task 3: Create Alembic Migration

**Files:**
- Create: `alembic/versions/<rev>_add_last_refreshed_at.py`

- [ ] **Step 1: Check current alembic head**

Run: `cd /root/availai && docker compose exec app alembic heads`
Note the current head revision ID (expected: `838cd7ddccf1`).

- [ ] **Step 2: Generate migration**

Run: `cd /root/availai && docker compose exec app alembic revision --autogenerate -m "add last_refreshed_at to requirements"`

- [ ] **Step 3: Review generated migration**

Open the generated file. Verify the upgrade function contains:
```python
op.add_column("requirements", sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True))
```

And the downgrade function contains:
```python
op.drop_column("requirements", "last_refreshed_at")
```

- [ ] **Step 4: Add backfill to upgrade**

After the `add_column` call in upgrade, add a backfill that populates `last_refreshed_at` from the most recent sighting per requirement. This prevents a burst of API calls on first deploy:

```python
# Backfill from latest sighting created_at to prevent post-deploy API burst
op.execute("""
    UPDATE requirements r
    SET last_refreshed_at = sub.max_created
    FROM (
        SELECT requirement_id, MAX(created_at) AS max_created
        FROM sightings
        GROUP BY requirement_id
    ) sub
    WHERE r.id = sub.requirement_id
      AND sub.max_created IS NOT NULL
""")
```

- [ ] **Step 5: Run migration**

Run: `cd /root/availai && docker compose exec app alembic upgrade head`
Expected: Migration applies successfully.

- [ ] **Step 6: Verify roundtrip**

Run: `cd /root/availai && docker compose exec app alembic downgrade -1 && docker compose exec app alembic upgrade head`
Expected: Both succeed.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/
git commit -m "feat: migration to add last_refreshed_at with sighting backfill"
```

---

## Task 4: Create Cooldown Helper Service

**Files:**
- Create: `app/services/sighting_helpers.py`
- Create: `tests/test_sighting_helpers.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_sighting_helpers.py`:

```python
"""Tests for sighting_helpers — cooldown gate logic.

Called by: pytest
Depends on: app.services.sighting_helpers, app.models, app.constants
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.constants import UserRole
from app.services.sighting_helpers import (
    RefreshEligibility,
    format_cooldown_remaining,
    is_refresh_allowed,
)


class FakeUser:
    def __init__(self, role: str = "buyer"):
        self.role = role


class FakeRequirement:
    def __init__(self, last_refreshed_at=None):
        self.last_refreshed_at = last_refreshed_at


class TestIsRefreshAllowed:
    def test_manager_always_allowed(self):
        req = FakeRequirement(last_refreshed_at=datetime.now(timezone.utc))
        result = is_refresh_allowed(req, FakeUser("manager"), 3)
        assert result.allowed is True
        assert result.remaining is None

    def test_admin_always_allowed(self):
        req = FakeRequirement(last_refreshed_at=datetime.now(timezone.utc))
        result = is_refresh_allowed(req, FakeUser("admin"), 3)
        assert result.allowed is True
        assert result.remaining is None

    def test_null_last_refreshed_allowed(self):
        req = FakeRequirement(last_refreshed_at=None)
        result = is_refresh_allowed(req, FakeUser("buyer"), 3)
        assert result.allowed is True
        assert result.remaining is None

    def test_past_cooldown_allowed(self):
        req = FakeRequirement(
            last_refreshed_at=datetime.now(timezone.utc) - timedelta(days=4)
        )
        result = is_refresh_allowed(req, FakeUser("buyer"), 3)
        assert result.allowed is True
        assert result.remaining is None

    def test_within_cooldown_blocked(self):
        req = FakeRequirement(
            last_refreshed_at=datetime.now(timezone.utc) - timedelta(hours=12)
        )
        result = is_refresh_allowed(req, FakeUser("buyer"), 3)
        assert result.allowed is False
        assert result.remaining is not None
        assert result.remaining > timedelta(days=2)

    def test_exactly_at_boundary_allowed(self):
        req = FakeRequirement(
            last_refreshed_at=datetime.now(timezone.utc) - timedelta(days=3)
        )
        result = is_refresh_allowed(req, FakeUser("sales"), 3)
        assert result.allowed is True

    def test_naive_datetime_handled(self):
        """SQLite returns naive datetimes — helper must handle them."""
        req = FakeRequirement(
            last_refreshed_at=datetime.utcnow() - timedelta(hours=1)
        )
        result = is_refresh_allowed(req, FakeUser("buyer"), 3)
        assert result.allowed is False

    def test_return_type_is_named_tuple(self):
        result = is_refresh_allowed(
            FakeRequirement(), FakeUser("buyer"), 3
        )
        assert isinstance(result, RefreshEligibility)
        assert hasattr(result, "allowed")
        assert hasattr(result, "remaining")


class TestFormatCooldownRemaining:
    def test_days_and_hours(self):
        assert format_cooldown_remaining(timedelta(days=2, hours=14)) == "2d 14h"

    def test_hours_only(self):
        assert format_cooldown_remaining(timedelta(hours=5, minutes=30)) == "5h"

    def test_less_than_hour(self):
        result = format_cooldown_remaining(timedelta(minutes=45))
        assert result == "<1h"

    def test_exactly_one_day(self):
        assert format_cooldown_remaining(timedelta(days=1)) == "1d 0h"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.sighting_helpers'`

- [ ] **Step 3: Write implementation**

Create `app/services/sighting_helpers.py`:

```python
"""sighting_helpers.py — Cooldown gate logic for manual sighting refresh.

Enforces a per-requirement cooldown on manual refreshes to avoid burning
through external API quotas. Managers and admins bypass the cooldown.

Called by: routers/sightings.py, jobs/sourcing_refresh_jobs.py
Depends on: constants.UserRole
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from ..constants import UserRole


class RefreshEligibility(NamedTuple):
    """Result of a cooldown check: whether refresh is allowed and remaining wait time."""

    allowed: bool
    remaining: timedelta | None


def is_refresh_allowed(requirement, user, cooldown_days: int) -> RefreshEligibility:
    """Check if a requirement can be manually refreshed by this user.

    Returns RefreshEligibility(allowed, remaining_cooldown).
    - Manager/Admin always get (True, None)
    - NULL last_refreshed_at => (True, None)
    - Past cooldown => (True, None)
    - Within cooldown => (False, remaining_timedelta)
    """
    if user.role in (UserRole.MANAGER, UserRole.ADMIN):
        return RefreshEligibility(True, None)

    last = requirement.last_refreshed_at
    if last is None:
        return RefreshEligibility(True, None)

    # Normalize naive datetimes (SQLite compat) to UTC
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    cooldown = timedelta(days=cooldown_days)
    elapsed = now - last

    if elapsed >= cooldown:
        return RefreshEligibility(True, None)

    return RefreshEligibility(False, cooldown - elapsed)


def format_cooldown_remaining(remaining: timedelta) -> str:
    """Format a timedelta as 'Xd Xh' or 'Xh' or '<1h' for display."""
    total_seconds = int(remaining.total_seconds())
    if total_seconds < 3600:
        return "<1h"
    days = remaining.days
    hours = remaining.seconds // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_helpers.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Lint**

Run: `cd /root/availai && ruff check app/services/sighting_helpers.py tests/test_sighting_helpers.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add app/services/sighting_helpers.py tests/test_sighting_helpers.py
git commit -m "feat: add cooldown helper service with tests"
```

---

## Task 5: Wire Cooldown into Single Refresh Endpoint

**Files:**
- Modify: `app/routers/sightings.py:24,256-293`

- [ ] **Step 1: Add imports**

In `app/routers/sightings.py`, after line 24 (`from ..constants import OfferStatus, RequisitionStatus`), add:

```python
from ..constants import OfferStatus, RequisitionStatus, UserRole
from ..services.sighting_helpers import format_cooldown_remaining, is_refresh_allowed
```

(Replace the existing constants import line to add `UserRole`.)

- [ ] **Step 2: Add cooldown context to `sightings_detail`**

In `app/routers/sightings.py`, replace the context dict (lines 256-267) with:

```python
    _elig = is_refresh_allowed(requirement, user, settings.sighting_refresh_cooldown_days)

    ctx = {
        "request": request,
        "requirement": requirement,
        "requisition": requisition,
        "summaries": summaries,
        "vendor_statuses": vendor_statuses,
        "pending_offers": pending_offers,
        "vendor_phones": vendor_phones,
        "activities": activities,
        "all_buyers": all_buyers,
        "user": user,
        "refresh_allowed": _elig.allowed,
        "refresh_available_at": format_cooldown_remaining(_elig.remaining) if not _elig.allowed else None,
        "is_manager": user.role in (UserRole.MANAGER, UserRole.ADMIN),
    }
```

- [ ] **Step 3: Rewrite `sightings_refresh` endpoint**

Replace lines 271-293 of `app/routers/sightings.py` with:

```python
@router.post("/v2/partials/sightings/{requirement_id}/refresh", response_class=HTMLResponse)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run search pipeline for a requirement with cooldown enforcement."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    elig = is_refresh_allowed(requirement, user, settings.sighting_refresh_cooldown_days)
    if not elig.allowed:
        msg = f"Refresh available in {format_cooldown_remaining(elig.remaining)}"
        detail_resp = await sightings_detail(request, requirement_id, db, user)
        toast = _oob_toast(msg, "warning")
        return HTMLResponse(detail_resp.body.decode() + toast.body.decode())

    # Log manager/admin bypass
    if user.role in (UserRole.MANAGER, UserRole.ADMIN) and requirement.last_refreshed_at:
        cooldown_days = settings.sighting_refresh_cooldown_days
        _elig_check = is_refresh_allowed(
            requirement, type("U", (), {"role": "buyer"})(), cooldown_days
        )
        if not _elig_check.allowed:
            logger.info(
                "Manager cooldown bypass: user={} requirement={}",
                user.id,
                requirement_id,
            )

    # Set optimistically before search to prevent race conditions
    prev_refreshed = requirement.last_refreshed_at
    requirement.last_refreshed_at = datetime.now(timezone.utc)
    db.flush()

    try:
        from ..search_service import search_requirement

        result = await search_requirement(requirement, db)

        # Check if search actually succeeded (at least 1 source OK)
        source_stats = result.get("source_stats", [])
        any_ok = any(s.get("status") == "ok" for s in source_stats if isinstance(s, dict))
        if not any_ok and source_stats:
            # All connectors failed — roll back the optimistic timestamp
            requirement.last_refreshed_at = prev_refreshed
            db.flush()
            logger.error("All search sources failed for requirement {}", requirement_id)
            detail_resp = await sightings_detail(request, requirement_id, db, user)
            toast = _oob_toast("Search failed — all sources unavailable. Please try again later.", "error")
            return HTMLResponse(detail_resp.body.decode() + toast.body.decode())

        db.commit()
    except Exception:
        logger.error("Search refresh failed for requirement %s", requirement_id, exc_info=True)
        requirement.last_refreshed_at = prev_refreshed
        db.flush()
        db.commit()
        detail_resp = await sightings_detail(request, requirement_id, db, user)
        toast = _oob_toast("Search failed — please try again later.", "error")
        return HTMLResponse(detail_resp.body.decode() + toast.body.decode())

    return await sightings_detail(request, requirement_id, db, user)
```

- [ ] **Step 4: Run existing tests to check for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -v -k sighting --timeout=30`
Expected: Existing tests still pass

- [ ] **Step 5: Commit**

```bash
git add app/routers/sightings.py
git commit -m "feat: add cooldown enforcement to single sightings refresh"
```

---

## Task 6: Wire Cooldown into Batch Refresh Endpoint

**Files:**
- Modify: `app/routers/sightings.py:296-335`

- [ ] **Step 1: Rewrite `sightings_batch_refresh`**

Replace lines 296-335 of `app/routers/sightings.py` with:

```python
@router.post("/v2/partials/sightings/batch-refresh", response_class=HTMLResponse)
async def sightings_batch_refresh(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements with cooldown enforcement."""
    from ..search_service import search_requirement

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            raise ValueError("requirement_ids must be a list")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid requirement_ids: {e}")

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    # Batch-fetch all requirements in one query
    reqs_by_id = {}
    if requirement_ids:
        reqs = db.query(Requirement).filter(Requirement.id.in_([int(rid) for rid in requirement_ids])).all()
        reqs_by_id = {r.id: r for r in reqs}

    success = 0
    skipped = 0
    failed = 0
    failed_ids = []
    total = len(requirement_ids)
    cooldown_days = settings.sighting_refresh_cooldown_days

    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            failed_ids.append(rid)
            continue

        elig = is_refresh_allowed(req_obj, user, cooldown_days)
        if not elig.allowed:
            skipped += 1
            continue

        prev_refreshed = req_obj.last_refreshed_at
        req_obj.last_refreshed_at = datetime.now(timezone.utc)
        db.flush()

        try:
            result = await search_requirement(req_obj, db)
            source_stats = result.get("source_stats", [])
            any_ok = any(s.get("status") == "ok" for s in source_stats if isinstance(s, dict))
            if not any_ok and source_stats:
                req_obj.last_refreshed_at = prev_refreshed
                db.flush()
                failed += 1
                failed_ids.append(rid)
            else:
                success += 1
        except Exception:
            logger.error("Batch refresh failed for requirement %s", rid, exc_info=True)
            req_obj.last_refreshed_at = prev_refreshed
            db.flush()
            failed += 1
            failed_ids.append(rid)

    db.commit()

    if failed_ids:
        logger.error("Batch refresh failures: requirement_ids={}", failed_ids)

    # Build summary message
    # Build summary message
    msg = f"Refreshed {success} of {total}"
    extras = []
    if skipped:
        extras.append(f"{skipped} skipped")
    if failed:
        extras.append(f"{failed} failed")
    if extras:
        msg += f" ({', '.join(extras)})"
    level = "success" if not failed else "warning"
    return _oob_toast(msg, level)
```

- [ ] **Step 2: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -v -k "sighting or batch" --timeout=30`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add app/routers/sightings.py
git commit -m "feat: add cooldown enforcement to batch sightings refresh"
```

---

## Task 7: Update Detail Template

**Files:**
- Modify: `app/templates/htmx/partials/sightings/detail.html:19-28`

- [ ] **Step 1: Replace refresh button with conditional block**

In `detail.html`, replace lines 19-28 (the existing refresh button) with:

```html
    {% if refresh_allowed %}
    <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
            hx-target="#sightings-detail"
            hx-swap="innerHTML"
            hx-indicator="#refresh-spinner-{{ requirement.id }}"
            class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
      <svg id="refresh-spinner-{{ requirement.id }}" class="h-3.5 w-3.5 htmx-indicator animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
      </svg>
      Refresh
    </button>
    {% else %}
    <button disabled
            title="Refresh available in {{ refresh_available_at }}"
            class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-100 text-xs font-medium text-gray-400 cursor-not-allowed">
      <svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
      </svg>
      {{ refresh_available_at }}
    </button>
    {% endif %}
```

- [ ] **Step 2: Verify template renders**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py -v -k sightings_detail --timeout=30`
Expected: Pass (template context now includes `refresh_allowed`)

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/detail.html
git commit -m "feat: conditional refresh button with cooldown display"
```

---

## Task 8: Add "Refresh All" Button to Workspace

**Files:**
- Modify: `app/templates/htmx/partials/sightings/table.html:10-11`
- Modify: `app/routers/sightings.py` (sightings_list context)

- [ ] **Step 1: Add hidden requirement ID inputs to table rows**

In `table.html`, find where each requirement row is rendered. Inside each row's `<tr>` or card element, add a hidden input:

```html
<input type="hidden" name="req_id_item" value="{{ req.id }}" class="batch-refresh-id">
```

This lets `hx-include` collect all visible requirement IDs.

- [ ] **Step 2: Add "Refresh All" button to the stat pills toolbar**

In `table.html`, after the stat pills `</div>` (after line 28), insert:

```html
{# ── Refresh All Button ────────────────────────────────────── #}
<div class="px-3 py-1.5 border-b border-gray-100 flex justify-end">
  <button hx-post="/v2/partials/sightings/batch-refresh"
          hx-vals='js:{"requirement_ids": JSON.stringify([...document.querySelectorAll(".batch-refresh-id")].map(i => i.value))}'
          hx-indicator="#batch-refresh-spinner"
          hx-swap="none"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
    <svg id="batch-refresh-spinner" class="h-3.5 w-3.5 htmx-indicator animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
      <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
    </svg>
    Refresh All
  </button>
</div>
```

**How it works:** Alpine.js `@click` handler collects all `.batch-refresh-id` hidden inputs into a JSON array, sets it as the `requirement_ids` form value. The batch endpoint already accepts this parameter. `hx-swap="none"` because the response is an OOB toast only.

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/table.html
git commit -m "feat: add Refresh All button to sightings workspace"
```

---

## Task 9: Update Auto-Refresh Job

**Files:**
- Modify: `app/jobs/sourcing_refresh_jobs.py:64-76`

- [ ] **Step 1: Set `last_refreshed_at` after successful auto-refresh**

In `sourcing_refresh_jobs.py`, modify the inner loop (lines 65-74) to set `last_refreshed_at` on success:

Replace lines 66-73:

```python
        for req in stale_reqs:
            try:
                result = await search_requirement(req, db)
                sighting_count = len(result.get("sightings", []))
                if sighting_count > 0:
                    refreshed += 1
                    logger.debug(f"Auto-refresh: req {req.id} ({req.primary_mpn}) → {sighting_count} sightings")
            except Exception as e:
                logger.warning(f"Auto-refresh failed for req {req.id}: {e}")
                continue
```

With:

```python
        for req in stale_reqs:
            try:
                result = await search_requirement(req, db)
                source_stats = result.get("source_stats", [])
                any_ok = any(s.get("status") == "ok" for s in source_stats if isinstance(s, dict))
                sighting_count = len(result.get("sightings", []))
                if any_ok:
                    req.last_refreshed_at = datetime.now(timezone.utc)
                    refreshed += 1
                    logger.debug(f"Auto-refresh: req {req.id} ({req.primary_mpn}) → {sighting_count} sightings")
            except Exception as e:
                logger.warning(f"Auto-refresh failed for req {req.id}: {e}", exc_info=True)
                continue
```

- [ ] **Step 2: Add `db.commit()` after loop**

After the for loop and before the final log line (line 76), add:

```python
        db.commit()
```

- [ ] **Step 3: Run existing tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "refresh_job or sourcing_refresh" --timeout=30`
Expected: Pass

- [ ] **Step 4: Commit**

```bash
git add app/jobs/sourcing_refresh_jobs.py
git commit -m "feat: set last_refreshed_at in auto-refresh job + improve error logging"
```

---

## Task 10: Write Integration Tests for Refresh Endpoints

**Files:**
- Modify: `tests/test_routers.py`

- [ ] **Step 1: Add integration tests**

Append to `tests/test_routers.py` (add `from unittest.mock import AsyncMock, patch` to imports if not already present):

```python
class TestSightingsRefreshCooldown:
    """Integration tests for sightings refresh cooldown enforcement."""

    def _make_requirement(self, db, requisition_id, last_refreshed_at=None):
        req = Requirement(
            requisition_id=requisition_id,
            primary_mpn="TEST-MPN-001",
            manufacturer="TestMfg",
            last_refreshed_at=last_refreshed_at,
        )
        db.add(req)
        db.commit()
        return req

    def test_refresh_blocked_shows_toast(self, client, db, user):
        """Non-manager within cooldown gets disabled button + warning toast."""
        user.role = "buyer"
        db.commit()
        from app.models.sourcing import Requisition
        reqn = Requisition(name="Test", created_by=user.id, status="active")
        db.add(reqn)
        db.commit()
        req = self._make_requirement(
            db, reqn.id,
            last_refreshed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        resp = client.post(f"/v2/partials/sightings/{req.id}/refresh")
        assert resp.status_code == 200
        assert "Refresh available in" in resp.text
        assert "toast" in resp.text.lower() or "warning" in resp.text.lower()

    def test_refresh_allowed_past_cooldown(self, client, db, user):
        """Non-manager past cooldown can refresh."""
        user.role = "buyer"
        db.commit()
        from app.models.sourcing import Requisition
        reqn = Requisition(name="Test", created_by=user.id, status="active")
        db.add(reqn)
        db.commit()
        req = self._make_requirement(
            db, reqn.id,
            last_refreshed_at=datetime.now(timezone.utc) - timedelta(days=4),
        )
        resp = client.post(f"/v2/partials/sightings/{req.id}/refresh")
        assert resp.status_code == 200
        # Should not contain cooldown warning
        assert "Refresh available in" not in resp.text

    def test_manager_bypasses_cooldown(self, client, db, user):
        """Manager can refresh even within cooldown."""
        user.role = "manager"
        db.commit()
        from app.models.sourcing import Requisition
        reqn = Requisition(name="Test", created_by=user.id, status="active")
        db.add(reqn)
        db.commit()
        req = self._make_requirement(
            db, reqn.id,
            last_refreshed_at=datetime.now(timezone.utc),
        )
        resp = client.post(f"/v2/partials/sightings/{req.id}/refresh")
        assert resp.status_code == 200
        assert "Refresh available in" not in resp.text

    def test_batch_refresh_skips_cooled_down(self, client, db, user):
        """Batch refresh skips requirements within cooldown for non-managers."""
        user.role = "buyer"
        db.commit()
        from app.models.sourcing import Requisition
        reqn = Requisition(name="Test", created_by=user.id, status="active")
        db.add(reqn)
        db.commit()
        fresh = self._make_requirement(
            db, reqn.id,
            last_refreshed_at=datetime.now(timezone.utc),
        )
        stale = self._make_requirement(db, reqn.id, last_refreshed_at=None)
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([fresh.id, stale.id])},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()

    def test_batch_refresh_invalid_json(self, client, db, user):
        """Invalid JSON in requirement_ids returns 400."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "not-json"},
        )
        assert resp.status_code == 400

    @patch(
        "app.routers.sightings.search_requirement",
        new_callable=AsyncMock,
        side_effect=Exception("API timeout"),
    )
    def test_refresh_failure_clears_timestamp(self, mock_search, client, db, user):
        """Failed search rolls back last_refreshed_at and shows error toast."""
        user.role = "buyer"
        db.commit()
        from app.models.sourcing import Requisition
        reqn = Requisition(name="Test", created_by=user.id, status="active")
        db.add(reqn)
        db.commit()
        original_ts = datetime.now(timezone.utc) - timedelta(days=5)
        req = self._make_requirement(db, reqn.id, last_refreshed_at=original_ts)
        resp = client.post(f"/v2/partials/sightings/{req.id}/refresh")
        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "try again" in resp.text.lower()
        db.refresh(req)
        assert req.last_refreshed_at == original_ts  # Rolled back
```

- [ ] **Step 2: Run integration tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_routers.py::TestSightingsRefreshCooldown -v --timeout=30`
Expected: All pass (may need to mock `search_requirement` depending on test setup)

**Note:** If `search_requirement` is called during tests and fails because external APIs aren't available, mock it:
```python
from unittest.mock import AsyncMock, patch

@patch("app.routers.sightings.search_requirement", new_callable=AsyncMock, return_value={"sightings": [], "source_stats": [{"status": "ok"}]})
def test_refresh_allowed_past_cooldown(self, mock_search, client, db, user):
    ...
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_routers.py
git commit -m "test: add integration tests for sightings refresh cooldown"
```

---

## Task 11: Full Test Suite + Lint

**Files:** All modified files

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=30`
Expected: All tests pass, no regressions

- [ ] **Step 2: Run linter**

Run: `cd /root/availai && ruff check app/ tests/`
Expected: No errors

- [ ] **Step 3: Fix any issues found**

Address any test failures or lint errors.

- [ ] **Step 4: Final commit if fixes were needed**

```bash
git add -u
git commit -m "fix: address lint/test issues from sightings refresh feature"
```

---

## Summary

| Task | What | Files |
|------|------|-------|
| 1 | Config value | `config.py` |
| 2 | Model column | `models/sourcing.py` |
| 3 | Migration + backfill | `alembic/versions/` |
| 4 | Cooldown helper + tests | `services/sighting_helpers.py`, `tests/test_sighting_helpers.py` |
| 5 | Single refresh endpoint | `routers/sightings.py` |
| 6 | Batch refresh endpoint | `routers/sightings.py` |
| 7 | Detail template | `templates/.../detail.html` |
| 8 | Refresh All button | `templates/.../table.html` |
| 9 | Auto-refresh job | `jobs/sourcing_refresh_jobs.py` |
| 10 | Integration tests | `tests/test_routers.py` |
| 11 | Full test + lint | All |
