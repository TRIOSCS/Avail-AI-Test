# Sightings Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Sightings page — a buyer-facing cross-requisition view of all open requirements at `/v2/sightings`, with split-panel layout, vendor status tracking, batch inquiry workflow, and activity timeline.

**Architecture:** New dedicated router (`app/routers/sightings.py`) with 9 HTMX endpoints, delegating to existing services (`sighting_status.py`, `search_service`, `email_service`). Two new columns on `Requirement` model (`priority_score`, `assigned_buyer_id`) plus one new column on `ActivityLog` (`requirement_id`) via Alembic migration. Five new Jinja2 templates in `htmx/partials/sightings/`. One shared extracted template (`activity_timeline.html`). One new scoring function in `scoring.py`. One config setting (`sighting_stale_days`).

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Jinja2/HTMX 2.x, Alpine.js 3.x, Tailwind CSS, pytest

**Spec:** `docs/superpowers/specs/2026-03-23-sightings-page-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `app/models/sourcing.py:75-113` | Add `priority_score`, `assigned_buyer_id` to Requirement |
| Modify | `app/models/intelligence.py:257-363` | Add `requirement_id` FK to ActivityLog |
| Create | `alembic/versions/xxx_add_sightings_page_columns.py` | Migration for 3 new columns |
| Modify | `app/config.py` | Add `sighting_stale_days: int = 3` |
| Modify | `app/scoring.py` | Add `score_requirement_priority()` |
| Create | `app/routers/sightings.py` | 9 HTMX endpoints for sightings page |
| Modify | `app/main.py:505-580` | Register sightings router |
| Modify | `app/routers/htmx_views.py:158-238` | Add `/v2/sightings` to `v2_page()` |
| Modify | `app/templates/htmx/partials/shared/mobile_nav.html:16-27` | Add Sightings nav item |
| Create | `app/templates/htmx/partials/sightings/list.html` | Split-panel layout |
| Create | `app/templates/htmx/partials/sightings/table.html` | Requirements table with group-by |
| Create | `app/templates/htmx/partials/sightings/detail.html` | Requirement detail + vendor breakdown |
| Create | `app/templates/htmx/partials/sightings/vendor_modal.html` | Vendor selection + email compose |
| Create | `app/templates/htmx/partials/shared/activity_timeline.html` | Shared timeline (extracted) |
| Modify | `app/services/ai_service.py:232` | Add `user_draft` param to `draft_rfq()` |
| Create | `tests/test_sightings_router.py` | Router endpoint tests |
| Create | `tests/test_sightings_scoring.py` | Priority scoring tests |

---

### Task 1: Migration — Add columns to Requirement and ActivityLog

**Files:**
- Modify: `app/models/sourcing.py:75-113`
- Modify: `app/models/intelligence.py:257-363`
- Create: new Alembic migration

- [ ] **Step 1: Write the failing test**

Create `tests/test_sightings_scoring.py` with a test that uses the new columns:

```python
"""Tests for sightings page priority scoring.

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

from app.models.sourcing import Requirement, Requisition
from app.models.intelligence import ActivityLog


def test_requirement_has_priority_score(db_session):
    """Requirement model should have a priority_score column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-001",
        manufacturer="TestMfr",
        priority_score=72.5,
    )
    db_session.add(r)
    db_session.flush()
    assert r.priority_score == 72.5


def test_requirement_has_assigned_buyer_id(db_session):
    """Requirement model should have an assigned_buyer_id column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-002",
        manufacturer="TestMfr",
        assigned_buyer_id=1,
    )
    db_session.add(r)
    db_session.flush()
    assert r.assigned_buyer_id == 1


def test_activity_log_has_requirement_id(db_session):
    """ActivityLog model should have a requirement_id FK column."""
    req = Requisition(name="Test RFQ", status="active")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-003",
        manufacturer="TestMfr",
    )
    db_session.add(r)
    db_session.flush()
    log = ActivityLog(
        user_id=1,
        activity_type="rfq_sent",
        channel="email",
        requisition_id=req.id,
        requirement_id=r.id,
    )
    db_session.add(log)
    db_session.flush()
    assert log.requirement_id == r.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_scoring.py -v`
Expected: FAIL — `TypeError: 'priority_score' is an invalid keyword argument`

- [ ] **Step 3: Add columns to Requirement model**

In `app/models/sourcing.py`, add after line 99 (`sourcing_status` column):

```python
    priority_score = Column(Float, nullable=True)  # AI-computed 0-100 for sort order
    assigned_buyer_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
```

Add relationship after line 106:

```python
    assigned_buyer = relationship("User", foreign_keys=[assigned_buyer_id])
```

- [ ] **Step 4: Add requirement_id to ActivityLog model**

In `app/models/intelligence.py`, add after line 270 (`requisition_id` column):

```python
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
```

Add relationship after line 304 (`requisition` relationship):

```python
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
```

Add index to `__table_args__` (inside the tuple, before the closing paren):

```python
        Index(
            "ix_activity_requirement",
            "requirement_id",
            "created_at",
            postgresql_where=Column("requirement_id").isnot(None),
        ),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_scoring.py -v`
Expected: PASS (SQLite creates columns from model in test mode)

- [ ] **Step 6: Generate Alembic migration**

```bash
cd /root/availai && alembic revision --autogenerate -m "add sightings page columns to requirements and activity_log"
```

Review the generated migration — it should add `priority_score` + `assigned_buyer_id` to `requirements` and `requirement_id` to `activity_log`.

- [ ] **Step 7: Commit**

```bash
git add app/models/sourcing.py app/models/intelligence.py alembic/versions/*sightings_page* tests/test_sightings_scoring.py
git commit -m "feat: add sightings page columns to Requirement and ActivityLog models"
```

---

### Task 2: Config + Priority Scoring Function

**Files:**
- Modify: `app/config.py`
- Modify: `app/scoring.py`
- Modify: `tests/test_sightings_scoring.py`

- [ ] **Step 1: Add config setting**

In `app/config.py`, add to the Settings class (near `buyplan_stale_offer_days`):

```python
    sighting_stale_days: int = 3  # Days before a requirement is flagged stale
```

- [ ] **Step 2: Write failing tests for scoring function**

Append to `tests/test_sightings_scoring.py`:

```python
from app.scoring import score_requirement_priority


class TestScoreRequirementPriority:
    """Test AI priority scoring for requirements."""

    def test_high_urgency_scores_high(self):
        """Hot/critical urgency should produce high scores."""
        score = score_requirement_priority(
            urgency="hot",
            opportunity_value=50000,
            sighting_count=10,
            days_since_created=1,
            vendors_contacted=0,
        )
        assert score >= 70

    def test_normal_urgency_scores_lower(self):
        """Normal urgency with ample sightings should score lower."""
        score = score_requirement_priority(
            urgency="normal",
            opportunity_value=5000,
            sighting_count=20,
            days_since_created=0,
            vendors_contacted=5,
        )
        assert score <= 50

    def test_zero_sightings_boosts_priority(self):
        """No sightings should increase priority (scarcity)."""
        score_no_sightings = score_requirement_priority(
            urgency="normal",
            opportunity_value=10000,
            sighting_count=0,
            days_since_created=3,
            vendors_contacted=0,
        )
        score_many_sightings = score_requirement_priority(
            urgency="normal",
            opportunity_value=10000,
            sighting_count=50,
            days_since_created=3,
            vendors_contacted=0,
        )
        assert score_no_sightings > score_many_sightings

    def test_no_contact_boosts_priority(self):
        """Not yet contacted vendors should boost priority."""
        score_no_contact = score_requirement_priority(
            urgency="normal",
            opportunity_value=10000,
            sighting_count=5,
            days_since_created=2,
            vendors_contacted=0,
        )
        score_contacted = score_requirement_priority(
            urgency="normal",
            opportunity_value=10000,
            sighting_count=5,
            days_since_created=2,
            vendors_contacted=5,
        )
        assert score_no_contact > score_contacted

    def test_score_clamped_0_100(self):
        """Score should always be between 0 and 100."""
        score = score_requirement_priority(
            urgency="critical",
            opportunity_value=999999,
            sighting_count=0,
            days_since_created=100,
            vendors_contacted=0,
        )
        assert 0 <= score <= 100
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_scoring.py::TestScoreRequirementPriority -v`
Expected: FAIL — `ImportError: cannot import name 'score_requirement_priority'`

- [ ] **Step 4: Implement scoring function**

In `app/scoring.py`, add at the end:

```python
def score_requirement_priority(
    urgency: str = "normal",
    opportunity_value: float = 0,
    sighting_count: int = 0,
    days_since_created: float = 0,
    vendors_contacted: int = 0,
) -> float:
    """Compute buyer priority score (0-100) for a requirement.

    Weights: urgency 30%, customer value 20%, sighting scarcity 20%,
    age 15%, contact progress 15%.

    Called by: sightings router, priority refresh job
    Depends on: nothing (pure function)
    """
    # Urgency (30%) — hot/critical get high scores
    urgency_map = {"critical": 100, "hot": 90, "urgent": 70, "normal": 30, "low": 10}
    urgency_score = urgency_map.get(urgency, 30)

    # Customer value (20%) — log scale, cap at 100
    import math
    value_score = min(100, math.log10(max(opportunity_value, 1)) * 25) if opportunity_value > 0 else 20

    # Sighting scarcity (20%) — fewer sightings = higher priority
    scarcity_score = max(0, 100 - sighting_count * 5)

    # Age (15%) — older = higher priority, caps at 30 days
    age_score = min(100, days_since_created * (100 / 30))

    # Contact progress (15%) — no contact = high priority
    contact_score = max(0, 100 - vendors_contacted * 20)

    total = (
        urgency_score * 0.30
        + value_score * 0.20
        + scarcity_score * 0.20
        + age_score * 0.15
        + contact_score * 0.15
    )
    return round(min(100, max(0, total)), 1)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_scoring.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/scoring.py tests/test_sightings_scoring.py
git commit -m "feat: add priority scoring function and SIGHTING_STALE_DAYS config"
```

---

### Task 3: Sightings Router — List endpoint

**Files:**
- Create: `app/routers/sightings.py`
- Modify: `app/main.py:505-580`
- Create: `tests/test_sightings_router.py`

- [ ] **Step 1: Write failing tests for list endpoint**

Create `tests/test_sightings_router.py`:

```python
"""Tests for sightings page router endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

import pytest

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary


def _seed_data(db_session):
    """Create requisition + requirement + sighting for testing."""
    req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="TEST-MPN-001",
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status="open",
    )
    db_session.add(r)
    db_session.flush()
    s = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Good Vendor",
        estimated_qty=200,
        listing_count=2,
        score=75.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.commit()
    return req, r, s


class TestSightingsListPartial:
    """Test GET /v2/partials/sightings."""

    def test_returns_200(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200

    def test_contains_requirement_mpn(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings")
        assert "TEST-MPN-001" in resp.text

    def test_filter_by_status(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=open")
        assert resp.status_code == 200
        assert "TEST-MPN-001" in resp.text

    def test_filter_by_status_excludes(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?status=won")
        assert "TEST-MPN-001" not in resp.text

    def test_pagination_defaults(self, client, db_session):
        _seed_data(db_session)
        resp = client.get("/v2/partials/sightings?page=1")
        assert resp.status_code == 200


class TestSightingsDetailPartial:
    """Test GET /v2/partials/sightings/{requirement_id}/detail."""

    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_contains_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert "Good Vendor" in resp.text

    def test_404_for_missing(self, client, db_session):
        resp = client.get("/v2/partials/sightings/99999/detail")
        assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`
Expected: FAIL — 404 (router not registered)

- [ ] **Step 3: Create sightings router**

Create `app/routers/sightings.py`:

```python
"""sightings.py — Buyer-facing sightings page HTMX endpoints.

Cross-requisition view of all open requirements with vendor status tracking,
batch inquiry workflow, and activity timeline.

Called by: main.py (router mount)
Depends on: models (Requirement, Requisition, Sighting, VendorSightingSummary,
            ActivityLog, VendorCard, Contact, Offer), sighting_status service,
            scoring.py, search_service, email_service, template_env
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..database import get_db
from ..dependencies import require_fresh_token, require_user
from ..models import User
from ..models.intelligence import ActivityLog
from ..models.offers import Contact, Offer
from ..models.sourcing import Requirement, Requisition, Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..services.sighting_status import compute_vendor_statuses
from ..template_env import templates

router = APIRouter(tags=["sightings"])


@router.get("/v2/partials/sightings/workspace", response_class=HTMLResponse)
async def sightings_workspace(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return the split-panel workspace layout. The table loads via hx-get inside."""
    ctx = {"request": request, "user": user}
    return templates.TemplateResponse(
        "htmx/partials/sightings/list.html", ctx
    )


@router.get("/v2/partials/sightings", response_class=HTMLResponse)
async def sightings_list(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    status: str = "",
    sales_person: str = "",
    assigned: str = "",  # "mine" or "" for all
    q: str = "",
    group_by: str = "",  # "" (flat), "brand", "manufacturer"
    sort: str = "priority",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Return the sightings table partial with filters and pagination."""
    query = (
        db.query(Requirement)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == "active")
        .options(joinedload(Requirement.requisition))
    )

    # Filters
    if status:
        query = query.filter(Requirement.sourcing_status == status)
    if sales_person:
        query = query.join(User, Requisition.created_by == User.id).filter(
            User.name.ilike(f"%{sales_person}%")
        )
    if assigned == "mine":
        query = query.filter(Requirement.assigned_buyer_id == user.id)
    if q:
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{q}%")
            | Requisition.customer_name.ilike(f"%{q}%")
        )

    # Count before pagination
    total = query.count()

    # Sorting
    sort_map = {
        "priority": Requirement.priority_score.desc().nullslast(),
        "mpn": Requirement.primary_mpn.asc(),
        "created": Requirement.created_at.desc(),
        "status": Requirement.sourcing_status.asc(),
    }
    order = sort_map.get(sort, Requirement.priority_score.desc().nullslast())
    if dir == "asc" and sort in sort_map:
        order = getattr(Requirement, sort if sort != "priority" else "priority_score").asc()
    query = query.order_by(order)

    # Pagination
    offset = (page - 1) * limit
    requirements = query.offset(offset).limit(limit).all()
    total_pages = max(1, (total + limit - 1) // limit)

    # Stat pill counts — lifecycle status counts across ALL active requirements
    stat_counts = dict(
        db.query(Requirement.sourcing_status, sqlfunc.count())
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == "active")
        .group_by(Requirement.sourcing_status)
        .all()
    )

    # Top vendor per requirement (best VendorSightingSummary score)
    top_vendors = {}
    if requirements:
        req_ids = [r.id for r in requirements]
        summaries = (
            db.query(
                VendorSightingSummary.requirement_id,
                VendorSightingSummary.vendor_name,
                VendorSightingSummary.score,
            )
            .filter(VendorSightingSummary.requirement_id.in_(req_ids))
            .order_by(
                VendorSightingSummary.requirement_id,
                VendorSightingSummary.score.desc(),
            )
            .all()
        )
        for s in summaries:
            if s.requirement_id not in top_vendors:
                top_vendors[s.requirement_id] = {
                    "vendor_name": s.vendor_name,
                    "score": s.score,
                }

    # Stale detection — last activity per requirement
    stale_threshold = datetime.now(timezone.utc) - timedelta(
        days=settings.sighting_stale_days
    )
    stale_req_ids: set[int] = set()
    if requirements:
        req_ids = [r.id for r in requirements]
        last_activities = (
            db.query(
                ActivityLog.requirement_id,
                sqlfunc.max(ActivityLog.created_at).label("last_at"),
            )
            .filter(ActivityLog.requirement_id.in_(req_ids))
            .group_by(ActivityLog.requirement_id)
            .all()
        )
        activity_map = {a.requirement_id: a.last_at for a in last_activities}
        for rid in req_ids:
            last = activity_map.get(rid)
            if last is None or last < stale_threshold:
                stale_req_ids.add(rid)

    # Group-by logic
    groups = None
    if group_by in ("brand", "manufacturer"):
        from collections import OrderedDict

        groups = OrderedDict()
        for r in requirements:
            key = getattr(r, group_by, "") or "Unknown"
            groups.setdefault(key, []).append(r)

    ctx = {
        "request": request,
        "requirements": requirements,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "limit": limit,
        "status": status,
        "sales_person": sales_person,
        "assigned": assigned,
        "q": q,
        "group_by": group_by,
        "sort": sort,
        "dir": dir,
        "stat_counts": stat_counts,
        "top_vendors": top_vendors,
        "stale_req_ids": stale_req_ids,
        "groups": groups,
        "user": user,
    }
    return templates.TemplateResponse(
        "htmx/partials/sightings/table.html", ctx
    )


@router.get(
    "/v2/partials/sightings/{requirement_id}/detail",
    response_class=HTMLResponse,
)
async def sightings_detail(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return the detail panel for a single requirement."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    requisition = db.get(Requisition, requirement.requisition_id)

    # Vendor summaries for this requirement
    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc())
        .all()
    )

    # Vendor statuses
    vendor_statuses = compute_vendor_statuses(
        requirement_id, requirement.requisition_id, db
    )

    # Pending-review offers
    pending_offers = (
        db.query(Offer)
        .filter(
            Offer.requirement_id == requirement_id,
            Offer.status == "pending_review",
        )
        .all()
    )

    # Vendor phone lookup
    vendor_phones = {}
    for s in summaries:
        if s.vendor_phone:
            vendor_phones[s.vendor_name] = s.vendor_phone
            continue
        card = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name == s.vendor_name.strip().lower())
            .first()
        )
        if card and card.phones:
            vendor_phones[s.vendor_name] = card.phones[0] if isinstance(card.phones, list) else card.phones

    # Activity timeline
    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    # All users for buyer assignment dropdown
    all_buyers = db.query(User).filter(User.is_active.is_(True)).all()

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
    }
    return templates.TemplateResponse(
        "htmx/partials/sightings/detail.html", ctx
    )


@router.post(
    "/v2/partials/sightings/{requirement_id}/refresh",
    response_class=HTMLResponse,
)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run search pipeline for a requirement. Returns updated detail panel."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        from ..search_service import search_requirement

        await search_requirement(requirement, db)
    except Exception:
        logger.warning("Search refresh failed for requirement %s", requirement_id, exc_info=True)

    return await sightings_detail(request, requirement_id, db, user)


@router.post(
    "/v2/partials/sightings/batch-refresh",
    response_class=HTMLResponse,
)
async def sightings_batch_refresh(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements."""
    import json

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    success = 0
    failed = 0
    for rid in requirement_ids:
        req_obj = db.get(Requirement, int(rid))
        if not req_obj:
            failed += 1
            continue
        try:
            from ..search_service import search_requirement

            await search_requirement(req_obj, db)
            success += 1
        except Exception:
            logger.warning("Batch refresh failed for requirement %s", rid, exc_info=True)
            failed += 1

    msg = f"Refreshed {success}/{success + failed} requirements."
    if failed:
        msg += f" {failed} failed."
    return HTMLResponse(
        f'<div hx-swap-oob="true" id="toast-trigger" '
        f'x-init="$store.toast.show(\'{msg}\', \'{"warning" if failed else "success"}\')">'
        f"</div>"
    )


@router.post(
    "/v2/partials/sightings/{requirement_id}/mark-unavailable",
    response_class=HTMLResponse,
)
async def sightings_mark_unavailable(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Mark all sightings for a vendor on this requirement as unavailable."""
    form = await request.form()
    vendor_name = form.get("vendor_name", "")
    if not vendor_name:
        raise HTTPException(status_code=400, detail="vendor_name required")

    from ..vendor_utils import normalize_vendor_name

    normalized = normalize_vendor_name(vendor_name)
    sightings = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == requirement_id,
            sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name)) == normalized,
        )
        .all()
    )
    for s in sightings:
        s.is_unavailable = True
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)


@router.patch(
    "/v2/partials/sightings/{requirement_id}/assign",
    response_class=HTMLResponse,
)
async def sightings_assign_buyer(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update the assigned buyer for a requirement."""
    form = await request.form()
    buyer_id_str = form.get("assigned_buyer_id", "")
    buyer_id = int(buyer_id_str) if buyer_id_str else None

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    requirement.assigned_buyer_id = buyer_id
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)


@router.get(
    "/v2/partials/sightings/vendor-modal",
    response_class=HTMLResponse,
)
async def sightings_vendor_modal(
    request: Request,
    requirement_ids: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return vendor selection + email compose modal content."""
    req_id_list = [int(x) for x in requirement_ids.split(",") if x.strip().isdigit()]

    requirements = (
        db.query(Requirement)
        .filter(Requirement.id.in_(req_id_list))
        .all()
    ) if req_id_list else []

    parts = [
        {
            "mpn": r.primary_mpn,
            "qty": r.target_qty,
            "target_price": float(r.target_price) if r.target_price else None,
        }
        for r in requirements
    ]

    # Suggest vendors: those with sightings for these requirements, ranked by score
    suggested_vendors = (
        db.query(VendorCard)
        .join(
            VendorSightingSummary,
            sqlfunc.lower(sqlfunc.trim(VendorSightingSummary.vendor_name))
            == VendorCard.normalized_name,
        )
        .filter(
            VendorSightingSummary.requirement_id.in_(req_id_list),
            VendorCard.is_blacklisted.is_(False),
        )
        .order_by(VendorCard.engagement_score.desc().nullslast())
        .distinct()
        .limit(20)
        .all()
    ) if req_id_list else []

    ctx = {
        "request": request,
        "suggested_vendors": suggested_vendors,
        "requirement_ids": req_id_list,
        "parts": parts,
    }
    return templates.TemplateResponse(
        "htmx/partials/sightings/vendor_modal.html", ctx
    )


@router.post(
    "/v2/partials/sightings/send-inquiry",
    response_class=HTMLResponse,
)
async def sightings_send_inquiry(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
):
    """Send batch RFQ to selected vendors for selected requirements.

    Uses require_fresh_token to get a valid Graph API token for email sending.
    """
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.isdigit()]
    vendor_names = form.getlist("vendor_names")
    email_body = form.get("email_body", "")

    if not requirement_ids or not vendor_names or not email_body:
        raise HTTPException(
            status_code=400,
            detail="requirement_ids, vendor_names, and email_body required",
        )

    requirements = (
        db.query(Requirement)
        .filter(Requirement.id.in_(requirement_ids))
        .all()
    )

    # Get requisition for context
    req_ids = {r.requisition_id for r in requirements}
    requisition_id = next(iter(req_ids)) if req_ids else None

    # Build vendor_groups in the format send_batch_rfq expects:
    # [{vendor_name, vendor_email, parts, subject, body}]
    vendor_groups = []
    for vn in vendor_names:
        # Look up vendor email from VendorCard → VendorContact
        card = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name == vn.strip().lower())
            .first()
        )
        vendor_email = ""
        if card:
            from ..models.vendors import VendorContact
            contact = (
                db.query(VendorContact)
                .filter(VendorContact.vendor_card_id == card.id)
                .first()
            )
            if contact and contact.email:
                vendor_email = contact.email

        vendor_groups.append({
            "vendor_name": vn,
            "vendor_email": vendor_email,
            "parts": [
                {"mpn": r.primary_mpn, "qty": r.target_qty}
                for r in requirements
            ],
            "subject": f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}",
            "body": email_body,
        })

    sent_count = 0
    failed_vendors = []
    try:
        from ..email_service import send_batch_rfq

        results = await send_batch_rfq(
            token=token,
            db=db,
            user_id=user.id,
            requisition_id=requisition_id,
            vendor_groups=vendor_groups,
        )
        sent_count = len(results)

        # Log activity per requirement per vendor
        for r in requirements:
            for vn in vendor_names:
                log = ActivityLog(
                    user_id=user.id,
                    activity_type="rfq_sent",
                    channel="email",
                    requisition_id=r.requisition_id,
                    requirement_id=r.id,
                    notes=f"RFQ sent to {vn}",
                )
                db.add(log)
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        failed_vendors = vendor_names

    db.commit()

    total = len(vendor_names)
    if failed_vendors:
        msg = f"Sent to {sent_count}/{total} vendors. Failed: {', '.join(failed_vendors)}."
    else:
        msg = f"RFQ sent to {sent_count} vendor{'s' if sent_count != 1 else ''}."

    return HTMLResponse(
        f'<div hx-swap-oob="true" id="toast-trigger" '
        f'x-init="$store.toast.show(\'{msg}\', \'{"warning" if failed_vendors else "success"}\')">'
        f"</div>"
    )
```

- [ ] **Step 4: Register router in main.py**

In `app/main.py`, add import (after line 530, near `rfq_router`):

```python
from .routers.sightings import router as sightings_router
```

Add registration (after line 568, near `rfq_router`):

```python
app.include_router(sightings_router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add app/routers/sightings.py app/main.py tests/test_sightings_router.py
git commit -m "feat: add sightings page router with list and detail endpoints"
```

---

### Task 4: Wire Sightings into Navigation

**Files:**
- Modify: `app/routers/htmx_views.py:158-238`
- Modify: `app/templates/htmx/partials/shared/mobile_nav.html:16-27`

- [ ] **Step 1: Add sightings to v2_page()**

In `app/routers/htmx_views.py`, inside `v2_page()`, add after the `/search` elif (line 188) and before the `/requisitions` elif:

```python
    elif "/sightings" in path:
        current_view = "sightings"
```

Also, in the `partial_url` section (after the `trouble-tickets` workspace case around line 199), add:

```python
    elif current_view == "sightings":
        partial_url = "/v2/partials/sightings/workspace"
```

- [ ] **Step 2: Add Sightings to mobile nav**

In `app/templates/htmx/partials/shared/mobile_nav.html`, update the `nav_items` list (line 16-27). Insert Sightings as the second item (after Reqs, before Search):

```python
    {% set nav_items = [
      ('requisitions', 'Reqs', '/v2/requisitions', '/v2/partials/parts/workspace',
       'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2'),
      ('sightings', 'Sightings', '/v2/sightings', '/v2/partials/sightings',
       'M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z'),
      ('search', 'Search', '/v2/search', '/v2/partials/search',
       'M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z'),
      ('buy-plans', 'Buy Plans', '/v2/buy-plans', '/v2/partials/buy-plans',
       'M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 100 4 2 2 0 000-4z'),
      ('vendors', 'Vendors', '/v2/vendors', '/v2/partials/vendors',
       'M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4'),
      ('customers', 'Customers', '/v2/customers', '/v2/partials/customers',
       'M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z')
    ] %}
```

Note: Now 6 primary items. The nav uses `flex justify-around` so it self-adjusts.

- [ ] **Step 3: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/shared/mobile_nav.html
git commit -m "feat: add Sightings to navigation and v2_page routing"
```

---

### Task 5: Templates — Split Panel Layout + Table

**Files:**
- Create: `app/templates/htmx/partials/sightings/list.html`
- Create: `app/templates/htmx/partials/sightings/table.html`

- [ ] **Step 1: Create template directory**

```bash
mkdir -p /root/availai/app/templates/htmx/partials/sightings
```

- [ ] **Step 2: Create split-panel layout (list.html)**

Create `app/templates/htmx/partials/sightings/list.html`:

```html
{# Sightings page — split-panel layout for buyer sourcing command view.
   Left: requirements table. Right: requirement detail with vendor breakdown.
   Called by: GET /v2/sightings → v2_page() → base_page.html lazy load
   Depends on: sightings/table.html, sightings/detail.html, Alpine.js, HTMX
#}

{% include "htmx/partials/shared/_macros.html" %}

<div class="h-[calc(100vh-90px)] flex"
     x-data="{
       splitRatio: parseFloat(localStorage.getItem('sightings-split') || '0.50'),
       dragging: false,
       selectedReqId: null,
       startDrag(e) {
         this.dragging = true;
         e.preventDefault();
       },
       onDrag(e) {
         if (!this.dragging) return;
         const container = this.$refs.container;
         const rect = container.getBoundingClientRect();
         const ratio = (e.clientX - rect.left) / rect.width;
         this.splitRatio = Math.max(0.25, Math.min(0.75, ratio));
       },
       stopDrag() {
         if (this.dragging) {
           this.dragging = false;
           localStorage.setItem('sightings-split', this.splitRatio.toFixed(3));
         }
       },
       selectReq(id) {
         this.selectedReqId = id;
       }
     }"
     x-ref="container"
     @mousemove.window="onDrag($event)"
     @mouseup.window="stopDrag()">

  {# ── Left Panel: Requirements Table ──────────────────────── #}
  <div class="flex flex-col min-w-0 border-r border-gray-200"
       :style="'width: ' + (splitRatio * 100) + '%'">
    <div id="sightings-table" class="flex-1 flex flex-col min-h-0"
         hx-get="/v2/partials/sightings"
         hx-trigger="load"
         hx-target="#sightings-table"
         hx-swap="innerHTML">
      <div class="p-8 text-center text-gray-400">Loading requirements...</div>
    </div>
  </div>

  {# ── Drag Handle ──────────────────────────────────────────── #}
  <div class="w-[3px] cursor-col-resize bg-gray-200 hover:bg-brand-400 transition-colors flex-shrink-0 relative group"
       :class="dragging ? 'bg-brand-500' : ''"
       @mousedown="startDrag($event)">
    <div class="absolute inset-y-0 -left-2 -right-2"></div>
    <div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
      <div class="w-0.5 h-0.5 rounded-full bg-white"></div>
      <div class="w-0.5 h-0.5 rounded-full bg-white"></div>
      <div class="w-0.5 h-0.5 rounded-full bg-white"></div>
    </div>
  </div>

  {# ── Right Panel: Requirement Detail ─────────────────────── #}
  <div class="flex-1 min-w-0 flex flex-col bg-white overflow-y-auto">
    {# Empty state #}
    <div x-show="!selectedReqId" class="flex flex-col items-center justify-center h-full text-gray-400">
      <svg class="h-12 w-12 mb-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1">
        <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
        <path stroke-linecap="round" stroke-linejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/>
      </svg>
      <p class="text-sm font-medium text-gray-500">Select a requirement</p>
      <p class="text-xs text-gray-400 mt-1">Click a row to view vendors, offers & activity</p>
    </div>
    {# Detail content — loaded via HTMX when row selected #}
    <div x-show="selectedReqId" x-cloak id="sightings-detail" class="p-3"></div>
  </div>
</div>
```

- [ ] **Step 3: Create requirements table (table.html)**

Create `app/templates/htmx/partials/sightings/table.html`:

```html
{# Sightings table — requirements list with stat pills, filters, group-by.
   Called by: GET /v2/partials/sightings (sightings router)
   Depends on: _macros.html, pagination.html
   Context: requirements, total, page, total_pages, stat_counts, top_vendors,
            stale_req_ids, groups, status, q, sort, dir, group_by, assigned
#}

{% import "htmx/partials/shared/_macros.html" as m %}

{# ── Stat Pills ──────────────────────────────────────────────── #}
<div class="px-3 pt-3 pb-2 border-b border-gray-100 flex items-center gap-2 flex-wrap">
  {% set pills = [
    ('', 'All', total),
    ('open', 'New', stat_counts.get('open', 0)),
    ('sourcing', 'Contacted', stat_counts.get('sourcing', 0)),
    ('offered', 'Responded', stat_counts.get('offered', 0)),
    ('quoted', 'Offer In', stat_counts.get('quoted', 0) + stat_counts.get('won', 0)),
  ] %}
  {% for val, label, count in pills %}
  <button hx-get="/v2/partials/sightings?status={{ val }}&q={{ q }}&group_by={{ group_by }}&assigned={{ assigned }}"
          hx-target="#sightings-table"
          hx-swap="innerHTML"
          class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-colors
                 {{ 'bg-brand-100 text-brand-700' if status == val else 'bg-gray-100 text-gray-600 hover:bg-gray-200' }}">
    {{ label }}
    <span class="text-[10px] {{ 'text-brand-500' if status == val else 'text-gray-400' }}">{{ count }}</span>
  </button>
  {% endfor %}
</div>

{# ── Filter Bar ──────────────────────────────────────────────── #}
<div class="px-3 py-2 border-b border-gray-100 flex items-center gap-2 flex-wrap">
  <input type="text" name="q" value="{{ q }}"
         placeholder="Search MPN or customer..."
         hx-get="/v2/partials/sightings"
         hx-trigger="keyup changed delay:300ms"
         hx-target="#sightings-table"
         hx-include="closest div"
         class="flex-1 min-w-[150px] rounded-lg border border-gray-200 px-3 py-1.5 text-sm focus:border-brand-400 focus:ring-1 focus:ring-brand-200 outline-none">
  <select name="group_by"
          hx-get="/v2/partials/sightings"
          hx-trigger="change"
          hx-target="#sightings-table"
          hx-include="closest div"
          class="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-600">
    <option value="" {{ 'selected' if not group_by }}>Flat</option>
    <option value="brand" {{ 'selected' if group_by == 'brand' }}>By Brand</option>
    <option value="manufacturer" {{ 'selected' if group_by == 'manufacturer' }}>By Manufacturer</option>
  </select>
  <button hx-get="/v2/partials/sightings?assigned={{ 'mine' if assigned != 'mine' else '' }}&status={{ status }}&q={{ q }}&group_by={{ group_by }}"
          hx-target="#sightings-table"
          hx-swap="innerHTML"
          class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors
                 {{ 'bg-brand-50 border-brand-300 text-brand-700' if assigned == 'mine' else 'border-gray-200 text-gray-600 hover:bg-gray-50' }}">
    My Items
  </button>
  <input type="hidden" name="status" value="{{ status }}">
  <input type="hidden" name="assigned" value="{{ assigned }}">
</div>

{# ── Table ───────────────────────────────────────────────────── #}
<div class="flex-1 overflow-y-auto overflow-x-auto"
     x-data="{ selectedIds: new Set(), toggleAll: false }">

  {% if not requirements %}
  <div class="p-8 text-center text-gray-400">
    <p class="text-sm">No requirements match your filters</p>
  </div>
  {% else %}

  <table class="compact-table w-full">
    <thead>
      <tr>
        <th class="px-2 py-2 w-8">
          <input type="checkbox" x-model="toggleAll"
                 @change="document.querySelectorAll('.req-checkbox').forEach(cb => { cb.checked = toggleAll; if (toggleAll) selectedIds.add(parseInt(cb.value)); else selectedIds.clear(); })"
                 class="rounded border-gray-300">
        </th>
        <th class="px-3 py-2 text-left">MPN</th>
        <th class="px-3 py-2 text-left">Qty</th>
        <th class="px-3 py-2 text-left">Customer</th>
        <th class="px-3 py-2 text-left">Sales</th>
        <th class="px-3 py-2 text-left">Top Vendor</th>
        <th class="px-3 py-2 text-left">Status</th>
        <th class="px-3 py-2 text-center">Priority</th>
        <th class="px-3 py-2 text-center">Stale</th>
      </tr>
    </thead>
    <tbody>
      {% macro render_row(r) %}
      <tr class="group cursor-pointer"
          :class="selectedReqId == {{ r.id }} ? 'row-selected' : ''"
          @click="selectReq({{ r.id }}); htmx.ajax('GET', '/v2/partials/sightings/{{ r.id }}/detail', {target: '#sightings-detail', swap: 'innerHTML'})"
          data-req-id="{{ r.id }}">
        <td class="px-2 py-2" @click.stop>
          <input type="checkbox" class="req-checkbox rounded border-gray-300" value="{{ r.id }}"
                 @change="$event.target.checked ? selectedIds.add({{ r.id }}) : selectedIds.delete({{ r.id }})">
        </td>
        <td class="px-3 py-2 font-mono font-medium text-gray-900">{{ r.primary_mpn }}</td>
        <td class="px-3 py-2">{{ r.target_qty or '—' }}</td>
        <td class="px-3 py-2 text-gray-600" style="font-family: 'DM Sans', sans-serif;">
          {{ r.requisition.customer_name or '—' }}
        </td>
        <td class="px-3 py-2 text-gray-500 text-xs" style="font-family: 'DM Sans', sans-serif;">
          {{ r.requisition.creator.name if r.requisition.creator else '—' }}
        </td>
        <td class="px-3 py-2 text-gray-600" style="font-family: 'DM Sans', sans-serif;">
          {% if top_vendors.get(r.id) %}
            {{ top_vendors[r.id].vendor_name }}
            <span class="ml-1 text-[10px] text-gray-400">{{ top_vendors[r.id].score|round|int }}%</span>
          {% else %}
            <span class="text-gray-400">—</span>
          {% endif %}
        </td>
        <td class="px-3 py-2">
          {% set status_styles = {
            'open': 'bg-gray-100 text-gray-600',
            'sourcing': 'bg-blue-50 text-blue-700',
            'offered': 'bg-amber-50 text-amber-700',
            'quoted': 'bg-emerald-50 text-emerald-700',
            'won': 'bg-green-50 text-green-700',
            'lost': 'bg-gray-100 text-gray-500',
          } %}
          <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ status_styles.get(r.sourcing_status, 'bg-gray-100 text-gray-600') }}">
            {{ r.sourcing_status|capitalize }}
          </span>
        </td>
        <td class="px-3 py-2 text-center">
          {% if r.priority_score %}
            {% set pri = r.priority_score %}
            {% set pri_color = 'text-rose-600' if pri >= 70 else ('text-amber-600' if pri >= 40 else 'text-gray-400') %}
            {% set pri_label = 'High' if pri >= 70 else ('Med' if pri >= 40 else 'Low') %}
            <span class="text-[10px] font-medium {{ pri_color }}">{{ pri_label }}</span>
          {% endif %}
        </td>
        <td class="px-3 py-2 text-center">
          {% if r.id in stale_req_ids %}
          <span class="inline-block w-2 h-2 rounded-full bg-amber-400" title="No activity in {{ sighting_stale_days|default(3) }}+ days"></span>
          {% endif %}
        </td>
      </tr>
      {% endmacro %}

      {% if groups %}
        {% for group_name, group_reqs in groups.items() %}
        <tr class="bg-gray-50 border-b border-gray-200">
          <td colspan="9" class="px-3 py-2">
            <span class="font-medium text-gray-700 text-sm">{{ group_name }}</span>
            <span class="ml-2 text-xs text-gray-400">{{ group_reqs|length }} part{{ 's' if group_reqs|length != 1 else '' }}</span>
          </td>
        </tr>
        {% for r in group_reqs %}
          {{ render_row(r) }}
        {% endfor %}
        {% endfor %}
      {% else %}
        {% for r in requirements %}
          {{ render_row(r) }}
        {% endfor %}
      {% endif %}
    </tbody>
  </table>

  {# ── Pagination ──────────────────────────────────────────── #}
  {% if total_pages > 1 %}
  <div class="px-3 py-2 border-t border-gray-100">
    {% set base_url = "/v2/partials/sightings" %}
    {% include "htmx/partials/shared/pagination.html" %}
  </div>
  {% endif %}

  {# ── Action Bar (appears on multi-select) ────────────────── #}
  <div x-show="selectedIds.size > 0" x-cloak
       class="sticky bottom-0 bg-white border-t border-brand-200 px-3 py-2 flex items-center gap-3 shadow-lg">
    <span class="text-xs font-medium text-gray-600" x-text="selectedIds.size + ' selected'"></span>
    <button @click="$dispatch('open-modal', {url: '/v2/partials/sightings/vendor-modal?requirement_ids=' + Array.from(selectedIds).join(',')})"
            class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-brand-500 text-white text-xs font-medium hover:bg-brand-600 transition-colors">
      Send to Vendors
    </button>
    <button class="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors"
            hx-post="/v2/partials/sightings/batch-refresh"
            hx-vals='js:{requirement_ids: JSON.stringify(Array.from(selectedIds))}'
            hx-target="#sightings-table">
      Refresh Sightings
    </button>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/sightings/list.html app/templates/htmx/partials/sightings/table.html
git commit -m "feat: add sightings page split-panel layout and table templates"
```

---

### Task 6: Templates — Detail Panel + Activity Timeline

**Files:**
- Create: `app/templates/htmx/partials/sightings/detail.html`
- Create: `app/templates/htmx/partials/shared/activity_timeline.html`

- [ ] **Step 1: Create shared activity timeline**

Create `app/templates/htmx/partials/shared/activity_timeline.html`:

```html
{# activity_timeline.html — Shared timeline of ActivityLog entries.
   Called by: sightings/detail.html, parts/tabs/activity.html
   Depends on: HTMX, Tailwind
   Context vars: activities (list of ActivityLog)
#}

<div class="space-y-0">
  {% if not activities %}
  <p class="text-xs text-gray-400 py-4 text-center">No activity yet</p>
  {% endif %}
  {% for a in activities %}
  <div class="flex items-start gap-2 py-2 {{ 'border-t border-gray-100' if not loop.first }}">
    {# Dot — filled = human action, empty = system #}
    <div class="mt-1.5 flex-shrink-0">
      {% if a.user_id %}
      <div class="w-2 h-2 rounded-full bg-brand-500"></div>
      {% else %}
      <div class="w-2 h-2 rounded-full border border-gray-400"></div>
      {% endif %}
    </div>
    <div class="flex-1 min-w-0">
      <p class="text-xs text-gray-700">
        {% if a.user and a.user.name %}
        <span class="font-medium">{{ a.user.name }}</span> —
        {% endif %}
        {{ a.details or a.activity_type|replace('_', ' ')|capitalize }}
      </p>
      <p class="text-[10px] text-gray-400 mt-0.5">
        {{ a.created_at.strftime('%b %d, %H:%M') if a.created_at else '' }}
      </p>
    </div>
  </div>
  {% endfor %}
</div>
```

- [ ] **Step 2: Create detail panel**

Create `app/templates/htmx/partials/sightings/detail.html`:

```html
{# Sightings detail panel — requirement info + vendor breakdown + activity.
   Called by: GET /v2/partials/sightings/{id}/detail (sightings router)
   Depends on: _macros.html, activity_timeline.html, source_badge.html
   Context: requirement, requisition, summaries, vendor_statuses,
            pending_offers, vendor_phones, activities, all_buyers, user
#}

{% import "htmx/partials/shared/_macros.html" as m %}

{# ── Part Header ─────────────────────────────────────────────── #}
<div class="border-b border-gray-100 pb-3 mb-3">
  <div class="flex items-start justify-between">
    <div>
      <h3 class="text-base font-semibold text-gray-900 font-mono">{{ requirement.primary_mpn }}</h3>
      {% if requirement.manufacturer %}
      <p class="text-xs text-gray-500">{{ requirement.manufacturer }}</p>
      {% endif %}
    </div>
    <button hx-post="/v2/partials/sightings/{{ requirement.id }}/refresh"
            hx-target="#sightings-detail"
            hx-swap="innerHTML"
            hx-indicator="#refresh-spinner"
            class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg border border-gray-200 text-xs font-medium text-gray-600 hover:bg-gray-50 transition-colors">
      <svg id="refresh-spinner" class="h-3.5 w-3.5 htmx-indicator animate-spin" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
      </svg>
      Refresh
    </button>
  </div>

  <div class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
    <div>
      <span class="text-gray-400">Qty:</span>
      <span class="font-mono text-gray-700">{{ requirement.target_qty or '—' }}</span>
    </div>
    <div>
      <span class="text-gray-400">Target:</span>
      <span class="font-mono text-gray-700">{{ '$%.2f'|format(requirement.target_price) if requirement.target_price else '—' }}</span>
    </div>
    <div>
      <span class="text-gray-400">Customer:</span>
      <a href="/v2/requisitions/{{ requisition.id }}" class="text-brand-600 hover:underline">
        {{ requisition.customer_name or '—' }}
      </a>
    </div>
    <div>
      <span class="text-gray-400">Buyer:</span>
      <select hx-patch="/v2/partials/sightings/{{ requirement.id }}/assign"
              hx-target="#sightings-detail"
              hx-swap="innerHTML"
              name="assigned_buyer_id"
              class="inline-block border-0 bg-transparent text-xs text-gray-700 p-0 focus:ring-0 cursor-pointer">
        <option value="">Unassigned</option>
        {% for b in all_buyers %}
        <option value="{{ b.id }}" {{ 'selected' if requirement.assigned_buyer_id == b.id }}>
          {{ b.name }}
        </option>
        {% endfor %}
      </select>
    </div>
  </div>
</div>

{# ── Vendor Breakdown Table ──────────────────────────────────── #}
<div class="mb-4">
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">
    Vendors ({{ summaries|length }})
  </h4>

  {% if not summaries %}
  <p class="text-xs text-gray-400 py-4 text-center border border-dashed border-gray-200 rounded-lg">
    No sightings yet — sightings will appear after search completes
  </p>
  {% else %}
  <table class="compact-table w-full">
    <thead>
      <tr>
        <th class="px-2 py-1.5 text-left">Vendor</th>
        <th class="px-2 py-1.5 text-left">Status</th>
        <th class="px-2 py-1.5 text-right">Qty</th>
        <th class="px-2 py-1.5 text-right">Best Price</th>
        <th class="px-2 py-1.5 text-center">Score</th>
        <th class="px-2 py-1.5 text-center">Phone</th>
        <th class="px-2 py-1.5 text-center">Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for s in summaries %}
      {% set vs = vendor_statuses.get(s.vendor_name, 'sighting') %}
      <tr class="group">
        <td class="px-2 py-1.5 font-medium text-gray-900" style="font-family: 'DM Sans', sans-serif;">
          {{ s.vendor_name }}
        </td>
        <td class="px-2 py-1.5">
          {% set vs_styles = {
            'sighting': 'bg-gray-100 text-gray-600',
            'contacted': 'bg-blue-50 text-blue-700',
            'offer-in': 'bg-emerald-50 text-emerald-700',
            'unavailable': 'bg-gray-100 text-gray-500',
            'blacklisted': 'bg-red-50 text-red-700',
          } %}
          {% set vs_labels = {
            'sighting': 'Sighting',
            'contacted': 'Contacted',
            'offer-in': 'Offer In',
            'unavailable': 'Unavailable',
            'blacklisted': 'Blacklisted',
          } %}
          <span class="inline-flex px-1.5 py-0.5 text-[10px] font-medium rounded-full {{ vs_styles.get(vs, 'bg-gray-100 text-gray-600') }}">
            {{ vs_labels.get(vs, vs|capitalize) }}
          </span>
        </td>
        <td class="px-2 py-1.5 text-right font-mono">{{ s.estimated_qty or '—' }}</td>
        <td class="px-2 py-1.5 text-right font-mono">
          {{ '$%.2f'|format(s.best_price) if s.best_price else '—' }}
        </td>
        <td class="px-2 py-1.5 text-center">
          {% set score_color = 'text-emerald-600' if s.score >= 70 else ('text-amber-600' if s.score >= 40 else 'text-gray-500') %}
          <span class="font-mono text-xs {{ score_color }}">{{ s.score|round|int }}%</span>
        </td>
        <td class="px-2 py-1.5 text-center">
          {% if vendor_phones.get(s.vendor_name) %}
          <a href="tel:{{ vendor_phones[s.vendor_name] }}" class="text-brand-500 hover:text-brand-700 text-xs">
            {{ vendor_phones[s.vendor_name] }}
          </a>
          {% else %}
          <span class="text-gray-300">—</span>
          {% endif %}
        </td>
        <td class="px-2 py-1.5 text-center">
          {% if vs != 'blacklisted' and vs != 'unavailable' %}
          <button hx-post="/v2/partials/sightings/{{ requirement.id }}/mark-unavailable"
                  hx-vals='{"vendor_name": "{{ s.vendor_name }}"}'
                  hx-target="#sightings-detail"
                  hx-swap="innerHTML"
                  hx-confirm="Mark {{ s.vendor_name }} as unavailable for this part?"
                  class="text-[10px] text-gray-400 hover:text-rose-500 opacity-0 group-hover:opacity-100 transition-all">
            Unavail
          </button>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  {# ── Pending Offers (AI-parsed, need approval) ──────────── #}
  {% if pending_offers %}
  <div class="mt-3 border-t border-gray-100 pt-3">
    <h4 class="text-xs font-semibold text-amber-600 uppercase tracking-wider mb-2">
      Pending Review ({{ pending_offers|length }})
    </h4>
    {% for o in pending_offers %}
    <div class="flex items-center justify-between py-1.5 border-b border-gray-50 last:border-0">
      <div class="text-xs">
        <span class="font-medium text-gray-700">{{ o.vendor_name }}</span>
        <span class="text-gray-400 ml-2">{{ o.qty_available or '?' }} pcs @ ${{ '%.2f'|format(o.unit_price) if o.unit_price else '?' }}</span>
      </div>
      <div class="flex gap-1">
        <button hx-put="/api/offers/{{ o.id }}/approve"
                hx-target="#sightings-detail"
                hx-swap="innerHTML"
                class="px-2 py-0.5 text-[10px] font-medium rounded bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
          Approve
        </button>
        <button hx-put="/api/offers/{{ o.id }}/reject"
                hx-target="#sightings-detail"
                hx-swap="innerHTML"
                class="px-2 py-0.5 text-[10px] font-medium rounded bg-gray-50 text-gray-500 hover:bg-gray-100">
          Reject
        </button>
      </div>
    </div>
    {% endfor %}
  </div>
  {% endif %}
</div>

{# ── Activity Timeline ──────────────────────────────────────── #}
<div>
  <h4 class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Activity</h4>
  {% include "htmx/partials/shared/activity_timeline.html" %}
</div>
```

- [ ] **Step 3: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add app/templates/htmx/partials/sightings/detail.html app/templates/htmx/partials/shared/activity_timeline.html
git commit -m "feat: add sightings detail panel and shared activity timeline"
```

---

### Task 7: Vendor Modal + Email Cleanup

**Files:**
- Create: `app/templates/htmx/partials/sightings/vendor_modal.html`
- Modify: `app/services/ai_service.py:232`

- [ ] **Step 1: Add user_draft param to draft_rfq()**

In `app/services/ai_service.py`, modify `draft_rfq()` signature (line 232) to add `user_draft`:

```python
async def draft_rfq(
    vendor_name: str,
    parts: list[dict],
    vendor_history: dict | None = None,
    user_name: str = "",
    user_draft: str | None = None,
) -> str | None:
```

Add at the top of the function body (after the docstring, before `history_context`):

```python
    # If buyer provided their own draft, clean it up instead of generating from scratch
    if user_draft:
        parts_str = "\n".join(
            f"- {p.get('mpn', '?')}: {p.get('qty', '?')} pcs"
            + (f" (target: ${p['target_price']})" if p.get("target_price") else "")
            for p in parts[:20]
        )
        cleanup_prompt = (
            f"Clean up this buyer's RFQ email draft. Fix grammar/formatting, "
            f"ensure all parts are referenced, preserve the buyer's tone.\n\n"
            f"Parts:\n{parts_str}\n\n"
            f"Buyer's draft:\n{user_draft}"
        )
        from app.utils.llm_router import routed_text

        return await routed_text(cleanup_prompt, model_tier="fast")
```

- [ ] **Step 2: Create vendor modal template**

Create `app/templates/htmx/partials/sightings/vendor_modal.html`:

```html
{# Vendor selection + email compose modal for batch RFQ.
   Called by: "Send to Vendors" action bar button via @open-modal dispatch
   Depends on: modal.html pattern, HTMX, Alpine.js
   Context: suggested_vendors, requirement_ids, parts
#}

<div class="p-4" x-data="{
  selectedVendors: new Set({{ suggested_vendors|map(attribute='normalized_name')|list|tojson }}),
  emailBody: '',
  cleaning: false,
  toggleVendor(name) {
    if (this.selectedVendors.has(name)) this.selectedVendors.delete(name);
    else this.selectedVendors.add(name);
  }
}">
  <h3 class="text-lg font-semibold text-gray-900 mb-3">Send Inquiry</h3>

  {# Vendor list #}
  <div class="mb-4">
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
      Select Vendors ({{ suggested_vendors|length }} suggested)
    </label>
    <div class="max-h-40 overflow-y-auto border border-gray-200 rounded-lg divide-y divide-gray-100">
      {% for v in suggested_vendors %}
      <label class="flex items-center gap-3 px-3 py-2 hover:bg-gray-50 cursor-pointer">
        <input type="checkbox"
               :checked="selectedVendors.has('{{ v.normalized_name }}')"
               @change="toggleVendor('{{ v.normalized_name }}')"
               class="rounded border-gray-300">
        <div class="flex-1 min-w-0">
          <span class="text-sm font-medium text-gray-700">{{ v.display_name }}</span>
          {% if v.response_rate %}
          <span class="ml-2 text-[10px] text-gray-400">{{ (v.response_rate * 100)|round|int }}% response</span>
          {% endif %}
        </div>
        {% if v.engagement_score %}
        <span class="text-[10px] text-gray-400">Score: {{ v.engagement_score|round|int }}</span>
        {% endif %}
      </label>
      {% endfor %}
    </div>
  </div>

  {# Parts reference (read-only) #}
  <div class="mb-4">
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wider mb-1">Parts</label>
    <div class="text-xs text-gray-500 bg-gray-50 rounded-lg p-2 max-h-24 overflow-y-auto font-mono">
      {% for p in parts %}
      <div>{{ p.mpn }} — {{ p.qty }} pcs{{ ' @ $%.2f'|format(p.target_price) if p.target_price else '' }}</div>
      {% endfor %}
    </div>
  </div>

  {# Email compose #}
  <div class="mb-4">
    <div class="flex items-center justify-between mb-1">
      <label class="text-xs font-medium text-gray-500 uppercase tracking-wider">Message</label>
      <button @click="cleaning = true; fetch('/api/ai/clean-rfq-draft', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({draft: emailBody, parts: {{ parts|tojson }}})}).then(r => r.json()).then(d => { emailBody = d.cleaned || emailBody; cleaning = false; }).catch(() => cleaning = false)"
              :disabled="!emailBody || cleaning"
              class="text-[10px] text-brand-500 hover:text-brand-700 disabled:text-gray-300">
        <span x-show="!cleaning">Clean Up</span>
        <span x-show="cleaning">Cleaning...</span>
      </button>
    </div>
    <textarea x-model="emailBody" rows="5" placeholder="Write your inquiry..."
              class="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm focus:border-brand-400 focus:ring-1 focus:ring-brand-200 outline-none"></textarea>
  </div>

  {# Actions #}
  <div class="flex justify-end gap-2">
    <button @click="$dispatch('close-modal')"
            class="px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50 rounded-lg transition-colors">
      Cancel
    </button>
    <button @click="
      const form = new FormData();
      {{ requirement_ids|tojson }}.forEach(id => form.append('requirement_ids', id));
      selectedVendors.forEach(v => form.append('vendor_names', v));
      form.append('email_body', emailBody);
      htmx.ajax('POST', '/v2/partials/sightings/send-inquiry', {values: Object.fromEntries(form), target: '#sightings-table'});
      $dispatch('close-modal');
    "
            :disabled="selectedVendors.size === 0 || !emailBody"
            class="px-4 py-2 text-sm font-medium text-white bg-brand-500 hover:bg-brand-600 rounded-lg transition-colors disabled:bg-gray-300">
      Send to <span x-text="selectedVendors.size"></span> Vendor<span x-show="selectedVendors.size !== 1">s</span>
    </button>
  </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git add app/templates/htmx/partials/sightings/vendor_modal.html app/services/ai_service.py
git commit -m "feat: add vendor selection modal and user_draft param to draft_rfq"
```

---

### Task 8: Full Test Suite + Integration Verification

**Files:**
- Test: `tests/test_sightings_router.py` (extend)
- Test: `tests/test_sightings_scoring.py` (already done)

- [ ] **Step 1: Add integration tests**

Append to `tests/test_sightings_router.py`:

```python
class TestSightingsRefresh:
    """Test POST /v2/partials/sightings/{id}/refresh."""

    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
        assert resp.status_code == 200

    def test_404_for_missing(self, client, db_session):
        resp = client.post("/v2/partials/sightings/99999/refresh")
        assert resp.status_code == 404


class TestSightingsMarkUnavailable:
    """Test POST /v2/partials/sightings/{id}/mark-unavailable."""

    def test_marks_sightings_unavailable(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        # Add a sighting
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Good Vendor",
            mpn_matched="TEST-MPN-001",
        )
        db_session.add(s)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 200

    def test_400_without_vendor_name(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={},
        )
        assert resp.status_code == 400


class TestSightingsAssignBuyer:
    """Test PATCH /v2/partials/sightings/{id}/assign."""

    def test_assigns_buyer(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": "1"},
        )
        assert resp.status_code == 200

    def test_unassigns_buyer(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": ""},
        )
        assert resp.status_code == 200


class TestSightingsWorkspace:
    """Test GET /v2/partials/sightings/workspace."""

    def test_returns_200(self, client, db_session):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200

    def test_contains_split_panel(self, client, db_session):
        resp = client.get("/v2/partials/sightings/workspace")
        assert "sightings-table" in resp.text
        assert "sightings-detail" in resp.text


class TestSightingsSendInquiry:
    """Test POST /v2/partials/sightings/send-inquiry."""

    def test_400_without_params(self, client, db_session):
        resp = client.post("/v2/partials/sightings/send-inquiry", data={})
        assert resp.status_code == 400

    def test_400_missing_body(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": str(r.id), "vendor_names": "Acme"},
        )
        assert resp.status_code == 400


class TestSightingsBatchRefresh:
    """Test POST /v2/partials/sightings/batch-refresh."""

    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        import json
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([r.id])},
        )
        assert resp.status_code == 200


class TestSightingsVendorModal:
    """Test GET /v2/partials/sightings/vendor-modal."""

    def test_returns_200(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200
```

- [ ] **Step 2: Run sightings tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py tests/test_sightings_scoring.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=120`
Expected: No regressions

- [ ] **Step 4: Commit if any fixes needed**

```bash
git add tests/test_sightings_router.py
git commit -m "test: add comprehensive sightings page integration tests"
```

---

### Task 9: Final Cleanup + Verification

- [ ] **Step 1: Verify navigation works end-to-end**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sightings_router.py -v -k workspace`
Expected: PASS — confirms the full page load → workspace → table chain works.

- [ ] **Step 2: Run full suite one final time**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --timeout=120`
Expected: All PASS, no regressions

- [ ] **Step 3: Final commit if needed**

```bash
git add -A && git commit -m "fix: address any remaining test failures"
```

---

## Dependencies Between Tasks

```
Task 1 (migration) ─────┐
Task 2 (scoring/config) ─┤
                          ├──→ Task 3 (router) ──→ Task 4 (nav)
                          │                              │
                          ├──→ Task 5 (list+table) ──────┤
                          │                              │
                          ├──→ Task 6 (detail+timeline) ─┤
                          │                              │
                          └──→ Task 7 (modal+ai) ────────┤
                                                         │
                                                         ↓
                                             Task 8 (tests) → Task 9 (final)
```

Tasks 1-2 can run in parallel. Tasks 5-7 can run in parallel (templates are independent). Tasks 3-4 must be sequential.
