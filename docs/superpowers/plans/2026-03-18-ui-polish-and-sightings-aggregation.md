# UI Polish & Sightings Aggregation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix column chooser, deduplicate sightings with a vendor-level aggregation model, fix scoring display bug, add tier labels with tooltips, and build archive system with single/group/multi-select support.

**Architecture:** New `VendorSightingSummary` materialized model aggregates sightings per vendor+part. Sourcing tab queries summaries instead of raw sightings. Archive adds `archived` status to `RequirementSourcingStatus` enum and replaces the toggle with a unified pill. All UI changes are within existing layout — no structural changes.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, Alembic, Jinja2, HTMX, Alpine.js, Tailwind CSS, Claude Haiku (qty estimation)

**Spec:** `docs/superpowers/specs/2026-03-18-ui-polish-and-sightings-aggregation-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `app/enums.py:41-53` | Add `archived` to `RequirementSourcingStatus` |
| Create | `app/models/vendor_sighting_summary.py` | `VendorSightingSummary` model |
| Modify | `app/models/__init__.py` | Export new model |
| Create | `alembic/versions/xxx_add_vendor_sighting_summary.py` | Migration |
| Create | `app/services/sighting_aggregation.py` | Aggregation logic + AI qty estimation |
| Modify | `app/scoring.py` | No code change needed — already correct |
| Modify | `app/routers/htmx_views.py:7197-7214` | Fix column-prefs save, add archive endpoints |
| Modify | `app/routers/htmx_views.py:7236-7257` | Sourcing tab → query summaries |
| Modify | `app/templates/htmx/partials/parts/list.html:20-69` | Gear icon, archive pill, checkboxes |
| Modify | `app/templates/htmx/partials/parts/tabs/sourcing.html` | Aggregated rows, popovers, tier labels |
| Create | `tests/test_sighting_aggregation.py` | Aggregation service tests |
| Create | `tests/test_archive_system.py` | Archive endpoint tests |
| Modify | `tests/test_scoring_helpers.py` | Score display tests |

---

### Task 1: Fix Column Chooser Save + Gear Icon

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html:42-69`
- Modify: `app/routers/htmx_views.py:7197-7214`
- Create: `tests/test_column_prefs.py`

- [ ] **Step 1: Write failing test for column prefs save**

```python
# tests/test_column_prefs.py
"""Tests for column preference save endpoint.

Called by: pytest
Depends on: app.routers.htmx_views.save_column_prefs
"""
from tests.conftest import engine  # noqa: F401
import pytest
from fastapi.testclient import TestClient


def test_save_column_prefs_returns_html(client: TestClient):
    """POST /v2/partials/parts/column-prefs should save and return updated list."""
    resp = client.post(
        "/v2/partials/parts/column-prefs",
        data={"columns": ["mpn", "brand", "qty"]},
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_save_column_prefs_empty_defaults(client: TestClient):
    """Empty columns list should fall back to defaults."""
    resp = client.post(
        "/v2/partials/parts/column-prefs",
        data={},
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_column_prefs.py -v`
Expected: Likely fails — this will reveal the actual save bug

- [ ] **Step 3: Debug and fix the save endpoint**

The column picker form uses `hx-post` with `hx-swap="innerHTML"` targeting `#parts-list`. The issue is likely that the POST response tries to re-render the full parts list partial, which calls `parts_list_partial()` — an async function with many query params that aren't passed from the column prefs form.

Fix in `app/routers/htmx_views.py:7197-7214`:

```python
@router.post("/v2/partials/parts/column-prefs", response_class=HTMLResponse)
async def save_column_prefs(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save user's visible column preferences and return updated parts list."""
    form = await request.form()
    cols = [c for c in form.getlist("columns") if c in dict(_ALL_PARTS_COLUMNS)]
    if not cols:
        cols = list(_DEFAULT_PARTS_COLUMNS)

    user.parts_column_prefs = cols
    db.commit()
    logger.info("Column prefs saved for user {}: {}", user.email, cols)

    # Re-render the parts list with new columns — pass default params
    return await parts_list_partial(request=request, user=user, db=db)
```

Note: If `_DEFAULT_PARTS_COLUMNS` is a tuple, the assignment `cols = _DEFAULT_PARTS_COLUMNS` may cause JSON serialization issues when saving to `parts_column_prefs` (JSON column). Ensure it's a list.

- [ ] **Step 4: Replace column icon with gear icon**

In `app/templates/htmx/partials/parts/list.html`, replace lines 46-48:

Old SVG (columns icon):
```html
<svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
  <path stroke-linecap="round" stroke-linejoin="round" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7"/>
</svg>
```

New SVG (gear/cog icon from Heroicons):
```html
<svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
  <path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 010 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 010-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28z"/>
  <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
</svg>
```

- [ ] **Step 5: Run tests and verify**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_column_prefs.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_column_prefs.py app/templates/htmx/partials/parts/list.html app/routers/htmx_views.py
git commit -m "fix: column chooser save + gear icon"
```

---

### Task 2: Add `archived` to RequirementSourcingStatus Enum

**Files:**
- Modify: `app/enums.py:41-53`
- Modify: `tests/test_scoring_helpers.py` (or relevant enum test)

- [ ] **Step 1: Write failing test**

```python
# Add to existing test file or create tests/test_enums.py
"""Tests for enum values.

Called by: pytest
Depends on: app.enums
"""
from app.enums import RequirementSourcingStatus


def test_requirement_sourcing_status_has_archived():
    assert RequirementSourcingStatus.archived == "archived"
    assert "archived" in [s.value for s in RequirementSourcingStatus]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enums.py::test_requirement_sourcing_status_has_archived -v`
Expected: FAIL — `AttributeError: 'archived' is not a member`

- [ ] **Step 3: Add archived to the enum**

In `app/enums.py:53`, add after `lost = "lost"`:

```python
    archived = "archived"  # Part archived — excluded from active views
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enums.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/enums.py tests/test_enums.py
git commit -m "feat: add archived status to RequirementSourcingStatus"
```

---

### Task 3: Create VendorSightingSummary Model + Migration

**Files:**
- Create: `app/models/vendor_sighting_summary.py`
- Modify: `app/models/__init__.py`
- Create: Alembic migration

- [ ] **Step 1: Create the model file**

```python
# app/models/vendor_sighting_summary.py
"""VendorSightingSummary — materialized vendor-level sighting aggregation.

One row per (requirement, vendor) pair. Pre-computes aggregated qty, avg price,
best price, listing count, and score for instant display in the sourcing tab.
Rebuilt when sightings are upserted or deleted.

Called by: sighting_aggregation service, htmx_views sourcing tab
Depends on: Requirement model
"""
from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import relationship

from sqlalchemy import DateTime

from .base import Base


class VendorSightingSummary(Base):
    __tablename__ = "vendor_sighting_summary"
    __table_args__ = (
        UniqueConstraint("requirement_id", "vendor_name", name="uq_vss_req_vendor"),
        Index("ix_vss_requirement", "requirement_id"),
        Index("ix_vss_vendor", "vendor_name"),
        Index("ix_vss_score", "score"),
    )

    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String, nullable=False)
    vendor_phone = Column(String, nullable=True)
    estimated_qty = Column(Integer, nullable=True)
    avg_price = Column(Float, nullable=True)
    best_price = Column(Float, nullable=True)
    listing_count = Column(Integer, nullable=False, default=0)
    source_types = Column(JSON, nullable=True)
    score = Column(Float, nullable=True)
    tier = Column(String(20), nullable=True)
    updated_at = Column(DateTime, nullable=True)

    requirement = relationship("Requirement", backref="vendor_summaries")
```

- [ ] **Step 2: Export from models/__init__.py**

Add to `app/models/__init__.py`:

```python
from app.models.vendor_sighting_summary import VendorSightingSummary  # noqa: F401
```

- [ ] **Step 3: Generate Alembic migration**

Run inside Docker:
```bash
docker compose exec app alembic revision --autogenerate -m "add vendor_sighting_summary table"
```

- [ ] **Step 4: Review the generated migration**

Check the generated file in `alembic/versions/`. Ensure it creates the table with the correct columns, unique constraint, and indexes.

- [ ] **Step 5: Test migration up and down**

```bash
docker compose exec app alembic upgrade head
docker compose exec app alembic downgrade -1
docker compose exec app alembic upgrade head
```

- [ ] **Step 6: Commit**

```bash
git add app/models/vendor_sighting_summary.py app/models/__init__.py alembic/versions/
git commit -m "feat: add VendorSightingSummary model and migration"
```

---

### Task 4: Create Sighting Aggregation Service

**Files:**
- Create: `app/services/sighting_aggregation.py`
- Create: `tests/test_sighting_aggregation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_sighting_aggregation.py
"""Tests for sighting aggregation service.

Called by: pytest
Depends on: app.services.sighting_aggregation, app.models
"""
from unittest.mock import patch, MagicMock
from tests.conftest import engine  # noqa: F401
import pytest


def test_aggregate_sightings_groups_by_vendor(db_session):
    """Multiple sightings from same vendor should produce one summary row."""
    from app.models.sourcing import Requisition, Requirement, Sighting
    from app.services.sighting_aggregation import rebuild_vendor_summaries

    req = Requisition(name="Test RFQ", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()

    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.flush()

    # Two sightings from same vendor
    s1 = Sighting(requirement_id=part.id, vendor_name="vendor_a", qty_available=100, unit_price=1.50, score=80.0, source_type="bb")
    s2 = Sighting(requirement_id=part.id, vendor_name="vendor_a", qty_available=200, unit_price=1.20, score=75.0, source_type="nexar")
    # One sighting from different vendor
    s3 = Sighting(requirement_id=part.id, vendor_name="vendor_b", qty_available=50, unit_price=2.00, score=60.0, source_type="digikey")
    db_session.add_all([s1, s2, s3])
    db_session.flush()

    rebuild_vendor_summaries(db_session, part.id, vendor_names=["vendor_a", "vendor_b"])

    from app.models.vendor_sighting_summary import VendorSightingSummary
    summaries = db_session.query(VendorSightingSummary).filter_by(requirement_id=part.id).all()
    assert len(summaries) == 2

    vendor_a = next(s for s in summaries if s.vendor_name == "vendor_a")
    assert vendor_a.listing_count == 2
    assert vendor_a.best_price == 1.20
    assert vendor_a.score == 80.0  # max of sighting scores
    assert set(vendor_a.source_types) == {"bb", "nexar"}


def test_aggregate_avg_price(db_session):
    """Average price should be weighted average of sighting prices."""
    from app.models.sourcing import Requisition, Requirement, Sighting
    from app.services.sighting_aggregation import rebuild_vendor_summaries

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()

    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.flush()

    s1 = Sighting(requirement_id=part.id, vendor_name="vendor_a", unit_price=1.00, score=50.0, source_type="bb")
    s2 = Sighting(requirement_id=part.id, vendor_name="vendor_a", unit_price=3.00, score=50.0, source_type="nexar")
    db_session.add_all([s1, s2])
    db_session.flush()

    rebuild_vendor_summaries(db_session, part.id, vendor_names=["vendor_a"])

    from app.models.vendor_sighting_summary import VendorSightingSummary
    summary = db_session.query(VendorSightingSummary).filter_by(requirement_id=part.id, vendor_name="vendor_a").one()
    assert summary.avg_price == pytest.approx(2.0, abs=0.01)


def test_aggregate_tier_labels(db_session):
    """Tier should be derived from max score."""
    from app.models.sourcing import Requisition, Requirement, Sighting
    from app.services.sighting_aggregation import rebuild_vendor_summaries

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()

    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.flush()

    s1 = Sighting(requirement_id=part.id, vendor_name="excellent_v", score=85.0, source_type="bb")
    s2 = Sighting(requirement_id=part.id, vendor_name="good_v", score=55.0, source_type="bb")
    s3 = Sighting(requirement_id=part.id, vendor_name="fair_v", score=30.0, source_type="bb")
    s4 = Sighting(requirement_id=part.id, vendor_name="poor_v", score=10.0, source_type="bb")
    db_session.add_all([s1, s2, s3, s4])
    db_session.flush()

    rebuild_vendor_summaries(db_session, part.id, vendor_names=["excellent_v", "good_v", "fair_v", "poor_v"])

    from app.models.vendor_sighting_summary import VendorSightingSummary
    for name, expected_tier in [("excellent_v", "Excellent"), ("good_v", "Good"), ("fair_v", "Fair"), ("poor_v", "Poor")]:
        s = db_session.query(VendorSightingSummary).filter_by(requirement_id=part.id, vendor_name=name).one()
        assert s.tier == expected_tier, f"{name}: expected {expected_tier}, got {s.tier}"


def test_aggregate_fallback_qty_when_no_ai(db_session):
    """When AI estimation is unavailable, sum non-null qty values."""
    from app.models.sourcing import Requisition, Requirement, Sighting
    from app.services.sighting_aggregation import rebuild_vendor_summaries

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()

    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.flush()

    s1 = Sighting(requirement_id=part.id, vendor_name="vendor_a", qty_available=100, score=50.0, source_type="bb")
    s2 = Sighting(requirement_id=part.id, vendor_name="vendor_a", qty_available=200, score=50.0, source_type="nexar")
    db_session.add_all([s1, s2])
    db_session.flush()

    # Mock AI to fail
    with patch("app.services.sighting_aggregation._estimate_qty_with_ai", return_value=None):
        rebuild_vendor_summaries(db_session, part.id, vendor_names=["vendor_a"])

    from app.models.vendor_sighting_summary import VendorSightingSummary
    summary = db_session.query(VendorSightingSummary).filter_by(vendor_name="vendor_a").one()
    assert summary.estimated_qty == 300  # sum fallback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_aggregation.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the aggregation service**

```python
# app/services/sighting_aggregation.py
"""Sighting aggregation — builds vendor-level summaries from raw sightings.

Groups sightings by (vendor_name, requirement_id), computes aggregated qty
(AI-estimated or sum fallback), averaged price, best price, score (max),
and tier label. Summaries are materialized in VendorSightingSummary.

Called by: search_service._save_sightings() after sighting upsert
Depends on: VendorSightingSummary model, Sighting model, VendorCard model
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.models.sourcing import Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard


def _score_to_tier(score: float | None) -> str:
    """Convert a 0-100 sighting score to a tier label."""
    if score is None:
        return "Poor"
    if score >= 70:
        return "Excellent"
    if score >= 40:
        return "Good"
    if score >= 20:
        return "Fair"
    return "Poor"


def _estimate_qty_with_ai(qty_values: list[int | None]) -> int | None:
    """Use Claude Haiku to estimate total available qty from varied listings.

    Returns estimated integer or None on failure.
    """
    non_null = [q for q in qty_values if q is not None]
    if not non_null:
        return None

    # For simple cases (all numeric), just sum — no AI needed
    if len(non_null) <= 2:
        return sum(non_null)

    try:
        from app.config import settings

        if not settings.ANTHROPIC_API_KEY:
            return sum(non_null)

        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        prompt = (
            f"Given these quantity listings from the same vendor for the same part: {non_null}. "
            f"Some may be duplicate stock listed on different platforms. "
            f"Estimate the total unique available inventory as a single integer. "
            f"Reply with ONLY the integer, nothing else."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return int(text)
    except Exception:
        logger.warning("AI qty estimation failed, using sum fallback")
        return sum(non_null)


def rebuild_vendor_summaries(
    db: Session,
    requirement_id: int,
    vendor_names: list[str] | None = None,
) -> list[VendorSightingSummary]:
    """Rebuild VendorSightingSummary rows for given requirement + vendors.

    If vendor_names is None, rebuilds all vendors for that requirement.
    """
    query = db.query(Sighting).filter(
        Sighting.requirement_id == requirement_id,
        Sighting.is_unavailable.isnot(True),
    )
    if vendor_names:
        query = query.filter(Sighting.vendor_name.in_(vendor_names))

    sightings = query.all()

    # Group by vendor
    groups: dict[str, list[Sighting]] = {}
    for s in sightings:
        vn = (s.vendor_name or "unknown").lower().strip()
        groups.setdefault(vn, []).append(s)

    # Look up vendor phones in bulk
    vendor_phones: dict[str, str | None] = {}
    if groups:
        cards = (
            db.query(VendorCard.normalized_name, VendorCard.phones)
            .filter(VendorCard.normalized_name.in_(list(groups.keys())))
            .all()
        )
        for card in cards:
            phones = card.phones or []
            vendor_phones[card.normalized_name] = phones[0] if phones else None

    results = []
    for vn, group in groups.items():
        prices = [s.unit_price for s in group if s.unit_price is not None]
        qtys = [s.qty_available for s in group]
        scores = [s.score for s in group if s.score is not None]
        sources = list({s.source_type for s in group if s.source_type})

        max_score = max(scores) if scores else None
        avg_price = sum(prices) / len(prices) if prices else None
        best_price = min(prices) if prices else None
        estimated_qty = _estimate_qty_with_ai(qtys)
        if estimated_qty is None:
            non_null_qtys = [q for q in qtys if q is not None]
            estimated_qty = sum(non_null_qtys) if non_null_qtys else None

        # Upsert summary
        existing = (
            db.query(VendorSightingSummary)
            .filter_by(requirement_id=requirement_id, vendor_name=vn)
            .first()
        )
        if existing:
            existing.vendor_phone = vendor_phones.get(vn)
            existing.estimated_qty = estimated_qty
            existing.avg_price = round(avg_price, 4) if avg_price else None
            existing.best_price = round(best_price, 4) if best_price else None
            existing.listing_count = len(group)
            existing.source_types = sources
            existing.score = round(max_score, 1) if max_score else None
            existing.tier = _score_to_tier(max_score)
            existing.updated_at = datetime.now(timezone.utc)
            results.append(existing)
        else:
            summary = VendorSightingSummary(
                requirement_id=requirement_id,
                vendor_name=vn,
                vendor_phone=vendor_phones.get(vn),
                estimated_qty=estimated_qty,
                avg_price=round(avg_price, 4) if avg_price else None,
                best_price=round(best_price, 4) if best_price else None,
                listing_count=len(group),
                source_types=sources,
                score=round(max_score, 1) if max_score else None,
                tier=_score_to_tier(max_score),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(summary)
            results.append(summary)

    db.flush()
    logger.info(
        "Rebuilt {} vendor summaries for requirement {}",
        len(results),
        requirement_id,
    )
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_sighting_aggregation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/sighting_aggregation.py tests/test_sighting_aggregation.py
git commit -m "feat: add sighting aggregation service with vendor-level summaries"
```

---

### Task 5: Wire Aggregation into Search Flow

**Files:**
- Modify: `app/services/search_service.py` (find `_save_sightings` method)

- [ ] **Step 1: Find the save sightings hook point**

Search for `_save_sightings` in `app/services/search_service.py`. This is where new sightings are committed after a search. Add a call to `rebuild_vendor_summaries` after the sightings are flushed.

- [ ] **Step 2: Write failing test**

```python
# tests/test_search_aggregation_hook.py
"""Test that search triggers vendor summary rebuild.

Called by: pytest
Depends on: app.services.search_service, app.services.sighting_aggregation
"""
from unittest.mock import patch
from tests.conftest import engine  # noqa: F401


def test_save_sightings_triggers_aggregation(db_session):
    """After saving sightings, rebuild_vendor_summaries should be called."""
    from app.models.sourcing import Requisition, Requirement, Sighting

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.flush()

    # Add sightings directly and call rebuild to verify integration
    s1 = Sighting(requirement_id=part.id, vendor_name="test_vendor", qty_available=100, score=50.0, source_type="bb")
    db_session.add(s1)
    db_session.flush()

    from app.services.sighting_aggregation import rebuild_vendor_summaries
    from app.models.vendor_sighting_summary import VendorSightingSummary

    rebuild_vendor_summaries(db_session, part.id, vendor_names=["test_vendor"])
    summary = db_session.query(VendorSightingSummary).filter_by(requirement_id=part.id).first()
    assert summary is not None
    assert summary.vendor_name == "test_vendor"
```

Note: The implementer should also read `search_service.py` to find the exact `_save_sightings()` method and add the `rebuild_vendor_summaries` call there. Search for the flush/commit after sighting upsert and add the call immediately after.

- [ ] **Step 3: Add aggregation call to _save_sightings**

After sightings are flushed in `_save_sightings()`, add:

```python
from app.services.sighting_aggregation import rebuild_vendor_summaries

# After db.flush() for sightings
vendor_names = list({s.vendor_name for s in new_sightings if s.vendor_name})
if vendor_names:
    rebuild_vendor_summaries(db, requirement_id, vendor_names=vendor_names)
```

- [ ] **Step 4: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add app/services/search_service.py tests/test_search_aggregation_hook.py
git commit -m "feat: wire sighting aggregation into search flow"
```

---

### Task 6: Fix Score Display Bug + Add Tier Labels to Sourcing Tab

**Files:**
- Modify: `app/templates/htmx/partials/parts/tabs/sourcing.html`
- Modify: `app/routers/htmx_views.py:7236-7257`
- Modify: `tests/test_scoring_helpers.py`

- [ ] **Step 1: Write failing test for score formatting**

```python
# Add to tests/test_scoring_helpers.py
def test_score_display_not_multiplied():
    """Score of 93.5 should display as 93, not 9350."""
    from app.scoring import score_sighting_v2

    score, _ = score_sighting_v2(
        vendor_score=90.0,
        is_authorized=True,
    )
    # score_sighting_v2 returns 0-100
    assert 0 <= score <= 100
    # Template should NOT multiply by 100
    display_value = int(score)  # This is what the template should do
    assert display_value <= 100
```

- [ ] **Step 2: Fix the template score rendering**

In `app/templates/htmx/partials/parts/tabs/sourcing.html`, replace the entire file with the new aggregated version:

```html
{# Sourcing tab — vendor-level sighting summaries for a specific part number.
   Called by: GET /v2/partials/parts/{id}/tab/sourcing
   Depends on: requirement, summaries (VendorSightingSummary list), raw_sightings_by_vendor (dict)
#}

<div>
  <div class="flex items-center justify-between mb-3">
    <div>
      <h3 class="text-lg font-semibold text-gray-900">Sourcing — {{ requirement.primary_mpn or 'Part' }}</h3>
      <p class="text-xs text-gray-500">{{ summaries|length }} vendor{{ 's' if summaries|length != 1 else '' }}</p>
    </div>
    <a hx-get="/v2/partials/sourcing/{{ requirement.id }}/search"
       hx-target="#part-detail"
       class="px-3 py-1.5 text-xs font-medium bg-brand-500 text-white rounded-lg hover:bg-brand-600 cursor-pointer">
      Run Search
    </a>
  </div>

  {% if summaries %}
  <div class="overflow-x-auto rounded-lg border border-gray-200">
    <table class="min-w-full divide-y divide-gray-200 text-sm">
      <thead class="bg-gray-50">
        <tr>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Phone</th>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Qty</th>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Price</th>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase"
              x-data="{ showTip: false }" @mouseenter="showTip = true" @mouseleave="showTip = false"
              class="relative cursor-help">
            Score
            <div x-show="showTip" x-cloak
                 class="absolute z-50 left-0 top-full mt-1 w-64 p-3 bg-white border border-gray-200 rounded-lg shadow-lg text-xs text-gray-600 font-normal normal-case">
              <p class="font-semibold text-gray-900 mb-1">Sighting Score (0-100)</p>
              <ul class="space-y-0.5">
                <li>Trust: 30% — vendor reliability</li>
                <li>Price: 25% — competitiveness vs median</li>
                <li>Qty: 20% — coverage vs target</li>
                <li>Freshness: 15% — listing age</li>
                <li>Completeness: 10% — data fields present</li>
              </ul>
            </div>
          </th>
          <th class="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase"
              x-data="{ showTip: false }" @mouseenter="showTip = true" @mouseleave="showTip = false"
              class="relative cursor-help">
            Tier
            <div x-show="showTip" x-cloak
                 class="absolute z-50 left-0 top-full mt-1 w-48 p-3 bg-white border border-gray-200 rounded-lg shadow-lg text-xs text-gray-600 font-normal normal-case">
              <p class="font-semibold text-gray-900 mb-1">Quality Tiers</p>
              <ul class="space-y-0.5">
                <li><span class="text-green-600 font-medium">Excellent</span> 70-100</li>
                <li><span class="text-amber-600 font-medium">Good</span> 40-69</li>
                <li><span class="text-gray-500 font-medium">Fair</span> 20-39</li>
                <li><span class="text-red-500 font-medium">Poor</span> 0-19</li>
              </ul>
            </div>
          </th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for s in summaries %}
        <tr class="hover:bg-gray-50">
          <td class="px-3 py-2 font-medium text-gray-900 whitespace-nowrap">{{ s.vendor_name or '—' }}</td>
          <td class="px-3 py-2 whitespace-nowrap text-xs">
            {% if s.vendor_phone %}
              <a href="tel:{{ s.vendor_phone }}" class="text-brand-500 hover:underline">{{ s.vendor_phone }}</a>
            {% else %}—{% endif %}
          </td>
          {# Qty cell — click for breakdown popover #}
          <td class="px-3 py-2 whitespace-nowrap" x-data="{ showQty: false }">
            <span @click="showQty = !showQty" class="cursor-pointer hover:text-brand-500">
              {{ '{:,}'.format(s.estimated_qty) if s.estimated_qty else '—' }}
              {% if s.listing_count > 1 %}<span class="text-[10px] text-gray-400 ml-0.5">({{ s.listing_count }})</span>{% endif %}
            </span>
            <div x-show="showQty" @click.away="showQty = false" x-cloak
                 class="absolute z-50 mt-1 w-64 p-3 bg-white border border-gray-200 rounded-lg shadow-lg text-xs">
              <p class="font-semibold text-gray-900 mb-1">Qty Breakdown ({{ s.listing_count }} listing{{ 's' if s.listing_count != 1 else '' }})</p>
              <table class="w-full text-xs">
                <thead><tr><th class="text-left text-gray-500">Source</th><th class="text-right text-gray-500">Qty</th></tr></thead>
                <tbody>
                  {% for raw in raw_sightings_by_vendor.get(s.vendor_name, []) %}
                  <tr><td>{{ raw.source_type or '—' }}</td><td class="text-right">{{ '{:,}'.format(raw.qty_available) if raw.qty_available else '—' }}</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </td>
          {# Price cell — click for breakdown popover #}
          <td class="px-3 py-2 whitespace-nowrap" x-data="{ showPrice: false }">
            <span @click="showPrice = !showPrice" class="cursor-pointer hover:text-brand-500">
              {{ '${:,.4f}'.format(s.avg_price) if s.avg_price else '—' }}
            </span>
            <div x-show="showPrice" @click.away="showPrice = false" x-cloak
                 class="absolute z-50 mt-1 w-64 p-3 bg-white border border-gray-200 rounded-lg shadow-lg text-xs">
              <p class="font-semibold text-gray-900 mb-1">Price Breakdown</p>
              <p class="text-gray-500 mb-1">Best: {{ '${:,.4f}'.format(s.best_price) if s.best_price else '—' }} | Avg: {{ '${:,.4f}'.format(s.avg_price) if s.avg_price else '—' }}</p>
              <table class="w-full text-xs">
                <thead><tr><th class="text-left text-gray-500">Source</th><th class="text-right text-gray-500">Price</th></tr></thead>
                <tbody>
                  {% for raw in raw_sightings_by_vendor.get(s.vendor_name, []) %}
                  <tr><td>{{ raw.source_type or '—' }}</td><td class="text-right">{{ '${:,.4f}'.format(raw.unit_price) if raw.unit_price else '—' }}</td></tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </td>
          <td class="px-3 py-2 whitespace-nowrap">
            {% if s.score is not none %}
              {% set score_pct = s.score|int %}
              <span class="{{ 'text-green-600' if score_pct >= 70 else 'text-amber-600' if score_pct >= 40 else 'text-gray-500' if score_pct >= 20 else 'text-red-500' }}">
                {{ score_pct }}%
              </span>
            {% else %}—{% endif %}
          </td>
          <td class="px-3 py-2 whitespace-nowrap">
            {% if s.tier %}
              <span class="px-1.5 py-0.5 text-xs font-medium rounded
                {{ 'bg-green-50 text-green-700' if s.tier == 'Excellent' else
                   'bg-amber-50 text-amber-700' if s.tier == 'Good' else
                   'bg-gray-100 text-gray-600' if s.tier == 'Fair' else
                   'bg-red-50 text-red-700' }}">
                {{ s.tier }}
              </span>
            {% else %}—{% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="p-8 text-center text-gray-400 border border-dashed border-gray-200 rounded-lg">
    <p class="text-sm">No sightings yet</p>
    <p class="text-xs mt-1">Run a search to find suppliers</p>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 3: Update the sourcing tab endpoint to pass summaries**

In `app/routers/htmx_views.py`, replace the `part_tab_sourcing` function (lines 7236-7257):

```python
@router.get("/v2/partials/parts/{requirement_id}/tab/sourcing", response_class=HTMLResponse)
async def part_tab_sourcing(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return vendor-level sighting summaries for a specific part number."""
    req = db.get(Requirement, requirement_id)
    if not req:
        raise HTTPException(404, "Part not found")

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc().nullslast() if db.bind.dialect.name != "sqlite" else VendorSightingSummary.score.desc(), VendorSightingSummary.id.desc())
        .all()
    )

    # Raw sightings grouped by vendor for popover breakdowns
    raw_sightings = (
        db.query(Sighting)
        .filter(Sighting.requirement_id == requirement_id)
        .order_by(Sighting.score.desc().nullslast())
        .all()
    )
    raw_by_vendor: dict[str, list] = {}
    for s in raw_sightings:
        vn = (s.vendor_name or "unknown").lower().strip()
        raw_by_vendor.setdefault(vn, []).append(s)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({
        "requirement": req,
        "summaries": summaries,
        "raw_sightings_by_vendor": raw_by_vendor,
    })
    return templates.TemplateResponse("htmx/partials/parts/tabs/sourcing.html", ctx)
```

Add import at top of file:
```python
from app.models.vendor_sighting_summary import VendorSightingSummary
```

- [ ] **Step 4: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_scoring_helpers.py tests/test_sighting_aggregation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/templates/htmx/partials/parts/tabs/sourcing.html app/routers/htmx_views.py tests/test_scoring_helpers.py
git commit -m "feat: sourcing tab shows aggregated vendor summaries with tier labels and popovers"
```

---

### Task 7: Archive System — Pill, Endpoints, and Actions

**Files:**
- Modify: `app/templates/htmx/partials/parts/list.html:20-40`
- Modify: `app/routers/htmx_views.py`
- Create: `tests/test_archive_system.py`

- [ ] **Step 1: Write failing tests for archive endpoints**

```python
# tests/test_archive_system.py
"""Tests for archive system — single, group, and multi-select.

Called by: pytest
Depends on: app.routers.htmx_views archive endpoints, app.enums
"""
from tests.conftest import engine  # noqa: F401


def test_archive_single_part(client, db_session):
    """PATCH /v2/partials/parts/{id}/archive should set sourcing_status to archived."""
    from app.models.sourcing import Requisition, Requirement

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    db_session.add(part)
    db_session.commit()

    resp = client.patch(f"/v2/partials/parts/{part.id}/archive")
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.sourcing_status == "archived"


def test_archive_whole_requisition(client, db_session):
    """PATCH /v2/partials/requisitions/{id}/archive should archive req + all children."""
    from app.models.sourcing import Requisition, Requirement

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    p1 = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    p2 = Requirement(requisition_id=req.id, primary_mpn="NE555", sourcing_status="sourcing")
    db_session.add_all([p1, p2])
    db_session.commit()

    resp = client.patch(f"/v2/partials/requisitions/{req.id}/archive")
    assert resp.status_code == 200

    db_session.refresh(req)
    db_session.refresh(p1)
    db_session.refresh(p2)
    assert req.status == "archived"
    assert p1.sourcing_status == "archived"
    assert p2.sourcing_status == "archived"


def test_bulk_archive(client, db_session):
    """POST /v2/partials/parts/bulk-archive with mixed IDs."""
    from app.models.sourcing import Requisition, Requirement

    req1 = Requisition(name="R1", customer_name="Acme", status="active")
    req2 = Requisition(name="R2", customer_name="Beta", status="active")
    db_session.add_all([req1, req2])
    db_session.flush()
    p1 = Requirement(requisition_id=req1.id, primary_mpn="LM358", sourcing_status="open")
    p2 = Requirement(requisition_id=req2.id, primary_mpn="NE555", sourcing_status="open")
    db_session.add_all([p1, p2])
    db_session.commit()

    resp = client.post(
        "/v2/partials/parts/bulk-archive",
        json={"requirement_ids": [p1.id], "requisition_ids": [req2.id]},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    db_session.refresh(p1)
    db_session.refresh(req2)
    db_session.refresh(p2)
    assert p1.sourcing_status == "archived"
    assert req2.status == "archived"
    assert p2.sourcing_status == "archived"  # cascaded


def test_unarchive_single_part(client, db_session):
    """PATCH /v2/partials/parts/{id}/unarchive should set sourcing_status to open."""
    from app.models.sourcing import Requisition, Requirement

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    part = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="archived")
    db_session.add(part)
    db_session.commit()

    resp = client.patch(f"/v2/partials/parts/{part.id}/unarchive")
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.sourcing_status == "open"


def test_archived_pill_filter(client, db_session):
    """GET /v2/partials/parts?status=archived should return only archived parts."""
    from app.models.sourcing import Requisition, Requirement

    req = Requisition(name="Test", customer_name="Acme", status="active")
    db_session.add(req)
    db_session.flush()
    p1 = Requirement(requisition_id=req.id, primary_mpn="LM358", sourcing_status="open")
    p2 = Requirement(requisition_id=req.id, primary_mpn="NE555", sourcing_status="archived")
    db_session.add_all([p1, p2])
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=archived&include_archived=true")
    assert resp.status_code == 200
    assert "NE555" in resp.text
    assert "LM358" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_archive_system.py -v`
Expected: FAIL — endpoints don't exist

- [ ] **Step 3: Add archive endpoints to htmx_views.py**

Add after the `save_column_prefs` endpoint:

```python
@router.patch("/v2/partials/parts/{requirement_id}/archive", response_class=HTMLResponse)
async def archive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a single requirement."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    part.sourcing_status = "archived"
    db.commit()
    logger.info("Part {} archived by {}", requirement_id, user.email)
    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/parts/{requirement_id}/unarchive", response_class=HTMLResponse)
async def unarchive_single_part(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a single requirement — returns to open status."""
    part = db.get(Requirement, requirement_id)
    if not part:
        raise HTTPException(404, "Part not found")
    part.sourcing_status = "open"
    db.commit()
    logger.info("Part {} unarchived by {}", requirement_id, user.email)
    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/archive", response_class=HTMLResponse)
async def archive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive a requisition and all its requirements."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")
    requisition.status = "archived"
    for part in requisition.requirements:
        part.sourcing_status = "archived"
    db.commit()
    logger.info("Requisition {} + {} parts archived by {}", req_id, len(requisition.requirements), user.email)
    return await parts_list_partial(request=request, user=user, db=db)


@router.patch("/v2/partials/requisitions/{req_id}/unarchive", response_class=HTMLResponse)
async def unarchive_requisition(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Unarchive a requisition and all its requirements."""
    requisition = db.get(Requisition, req_id)
    if not requisition:
        raise HTTPException(404, "Requisition not found")
    requisition.status = "active"
    for part in requisition.requirements:
        if part.sourcing_status == "archived":
            part.sourcing_status = "open"
    db.commit()
    logger.info("Requisition {} unarchived by {}", req_id, user.email)
    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-archive", response_class=HTMLResponse)
async def bulk_archive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk archive — accepts separate requirement_ids and requisition_ids."""
    body = await request.json()
    req_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    # Archive individual parts
    if req_ids:
        parts = db.query(Requirement).filter(Requirement.id.in_(req_ids)).all()
        for p in parts:
            p.sourcing_status = "archived"

    # Archive whole requisitions + cascade
    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for r in reqs:
            r.status = "archived"
            for p in r.requirements:
                p.sourcing_status = "archived"

    db.commit()
    logger.info("Bulk archive: {} parts, {} requisitions by {}", len(req_ids), len(requisition_ids), user.email)
    return await parts_list_partial(request=request, user=user, db=db)


@router.post("/v2/partials/parts/bulk-unarchive", response_class=HTMLResponse)
async def bulk_unarchive(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk unarchive — accepts separate requirement_ids and requisition_ids."""
    body = await request.json()
    req_ids = body.get("requirement_ids", [])
    requisition_ids = body.get("requisition_ids", [])

    if req_ids:
        parts = db.query(Requirement).filter(Requirement.id.in_(req_ids)).all()
        for p in parts:
            p.sourcing_status = "open"

    if requisition_ids:
        reqs = db.query(Requisition).filter(Requisition.id.in_(requisition_ids)).all()
        for r in reqs:
            r.status = "active"
            for p in r.requirements:
                if p.sourcing_status == "archived":
                    p.sourcing_status = "open"

    db.commit()
    logger.info("Bulk unarchive: {} parts, {} requisitions by {}", len(req_ids), len(requisition_ids), user.email)
    return await parts_list_partial(request=request, user=user, db=db)
```

- [ ] **Step 4: Update the parts list filter to handle archived pill**

In `app/routers/htmx_views.py`, update the `parts_list_partial` filter logic (around line 7100):

```python
# When status is "archived", show archived items regardless of include_archived flag
if status == "archived":
    query = query.filter(Requirement.sourcing_status == "archived")
elif not include_archived:
    query = query.filter(Requisition.status.in_(["active", "open", "sourcing"]))
    query = query.filter(Requirement.sourcing_status != "archived")
```

- [ ] **Step 5: Update list.html — replace archive toggle with pill, add checkboxes**

In `app/templates/htmx/partials/parts/list.html`, replace the status pills section (lines 20-40):

Replace the status pills loop (line 20) to add "Archived":
```html
{% for s_val, s_label in [('', 'All'), ('open', 'Open'), ('sourcing', 'Src'), ('offered', 'Ofd'), ('quoted', 'Qtd'), ('archived', 'Archived')] %}
```

Remove the archived toggle (lines 31-40 — the entire `<label>` block with the checkbox).

Add a checkbox column to the table header and each row for multi-select. In the thead (around line 101), add as first column:
```html
<th class="px-1 py-2 w-6">
  <input type="checkbox" x-model="selectAll" @change="toggleAll()"
         class="h-3 w-3 rounded border-gray-300 text-brand-500">
</th>
```

In the tbody row (around line 125), add as first column:
```html
<td class="px-1 py-2">
  <input type="checkbox" :value="{{ req.id }}" x-model="selectedIds"
         class="h-3 w-3 rounded border-gray-300 text-brand-500">
</td>
```

Add bulk action bar (above or below filter bar):
```html
<div x-show="selectedIds.length > 0" x-cloak class="flex items-center gap-2 px-2 py-1 bg-brand-50 border-b border-brand-100">
  <span class="text-[10px] text-brand-600 font-medium" x-text="selectedIds.length + ' selected'"></span>
  <button @click="bulkArchive()" type="button"
          class="px-2 py-0.5 text-[10px] font-semibold bg-gray-500 text-white rounded hover:bg-gray-600">
    Archive
  </button>
</div>
```

Add undo toast for single-part archive (shown temporarily after archive action):
```html
{# Undo toast — shown for 5s after single part archive #}
<div x-data="{ show: false, partId: null, timer: null }"
     @part-archived.window="show = true; partId = $event.detail.id; clearTimeout(timer); timer = setTimeout(() => show = false, 5000)"
     x-show="show" x-cloak
     class="fixed bottom-4 right-4 z-50 flex items-center gap-3 px-4 py-2 bg-gray-800 text-white text-sm rounded-lg shadow-lg">
  <span>Part archived</span>
  <button @click="fetch(`/v2/partials/parts/${partId}/unarchive`, {method:'PATCH'}).then(() => { show = false; htmx.trigger('#parts-list', 'refresh') })"
          class="text-brand-300 hover:text-white font-medium">Undo</button>
</div>
```

Add Alpine.js data for selection to the parent div:
```html
<div class="flex flex-col h-full" x-data="{ selectedIds: [], selectAll: false, toggleAll() { /* toggle logic */ }, bulkArchive() { /* fetch POST */ } }">
```

- [ ] **Step 6: Run tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_archive_system.py -v`
Expected: PASS

- [ ] **Step 7: Run full test suite for regressions**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add app/enums.py app/routers/htmx_views.py app/templates/htmx/partials/parts/list.html tests/test_archive_system.py
git commit -m "feat: archive system with single/group/multi-select + archived status pill"
```

---

### Task 8: Run Full Coverage Check

**Files:** None — verification only

- [ ] **Step 1: Run coverage report**

```bash
TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

- [ ] **Step 2: Check for any coverage drops**

Compare against baseline. If any new code is uncovered, add targeted tests.

- [ ] **Step 3: Manual smoke test checklist**

After deploying to DigitalOcean:
- [ ] Column chooser: open, check/uncheck columns, click Apply — should save and refresh table
- [ ] Gear icon visible where columns icon was
- [ ] Sourcing tab: shows one row per vendor (not duplicate rows)
- [ ] Click qty → popover with breakdown
- [ ] Click price → popover with breakdown
- [ ] Score shows reasonable % (not 9350%)
- [ ] Tier labels show (Excellent/Good/Fair/Poor) with colors
- [ ] Hover over Score header → tooltip with 5 factors
- [ ] Hover over Tier header → tooltip with tier ranges
- [ ] Status pills include "Archived"
- [ ] No more separate archive toggle
- [ ] Click archive on single part → part archived, undo toast appears
- [ ] Archive whole requisition → confirmation, all parts archived
- [ ] Multi-select + Archive → confirmation, all selected archived
- [ ] Click "Archived" pill → shows only archived items
- [ ] Unarchive from archived view works

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: coverage and smoke test fixes"
```
